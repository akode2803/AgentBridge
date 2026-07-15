"""Membership service — chats, DMs, self-chats, multi-admin groups.

Mutations emit INFO EVENTS into the actor's own log (the source of truth) and
then rematerialize ``meta.json`` from the local fold. A snapshot written from
a partial local view self-heals: every other member's next mutation (or an
explicit ``refold``) reproduces the canonical state from the union of events.

Multi-admin model (replaces v1's owner): admins appoint/dismiss admins, agents
can never be admins, and the fold auto-promotes the longest-standing human if
a group would go admin-less. The free-chatting invariant (owner pull-in) is
ported from v1's verified ``_missing_owners``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

from ..core.errors import PermissionDenied, TransportError, ValidationError
from ..core.models import ChatKind, ChatPermissions, ChatSnapshot, Role, UserKind
from ..store.db import Store
from ..transport.base import Transport
from . import authz, events
from .directory import Directory
from .messaging import MessagingService
from .paths import P
from .privacy import PrivacyService

__all__ = ["MembershipService"]

log = logging.getLogger("agentbridge.mesh")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "chat"


class MembershipService:
    def __init__(
        self,
        tx: Transport,
        store: Store,
        directory: Directory,
        messaging: MessagingService,
        privacy: PrivacyService | None = None,
        keys=None,  # ChatKeyService — wired by the facade when E2EE is on
    ) -> None:
        self.tx = tx
        self.store = store
        self.directory = directory
        self.messaging = messaging
        self.privacy = privacy
        self.keys = keys
        self.user = messaging.user

    def _gate_add(self, target: str) -> None:
        """R6 add-to-group gate (public setting; the reason is showable)."""
        if self.privacy is None or target == self.user:
            return
        ok, why = self.privacy.can_add_to_group(self.user, target)
        if not ok:
            raise PermissionDenied(why)

    def _require_alive(self, name: str) -> None:
        """R56 (V49): a DELETED account (soft — ``deactivated`` set) is not a
        valid chat target; its name stays resolvable only so old transcripts
        keep their sender. Distinct from active=False alone (a paused agent
        is still a member)."""
        acc = self.directory.get(name)
        if acc is None:
            raise ValidationError(f"unknown user @{name}")
        if acc.deactivated:
            raise ValidationError(f"@{name} has left the mesh")

    def _cascading_agents(self, snap: ChatSnapshot, owner: str) -> list[str]:
        """The agents that will silently leave WITH ``owner`` (the fold's
        heal drops any agent whose responsible member is gone)."""
        if snap.kind is not ChatKind.GROUP:
            return []
        return [
            m for m in snap.members
            if self.directory.kind(m) is UserKind.AGENT
            and self.directory.owner_of(m) == owner
        ]

    # ------------------------------------------------------------- creation
    def create_chat(
        self,
        name: str,
        members: list[str] | None = None,
        *,
        kind: ChatKind = ChatKind.GROUP,
        permissions: dict | None = None,
        auto_dm: bool = False,
        _message_gated: frozenset[str] = frozenset(),
    ) -> ChatSnapshot:
        me = self.user
        roster = list(dict.fromkeys([me, *(members or [])]))
        for m in roster:
            self._require_alive(m)  # unknown OR deleted (V49)
        if kind is ChatKind.GROUP and not (name or "").strip() and not auto_dm:
            name = "New Group"  # v1 UX: empty name falls back, never errors

        pulled = self.directory.missing_owners(roster)
        roster += [o for o in pulled if o not in roster]

        # R6: creating a group WITH people IS adding them — gate every target
        # (pulled owners included: if the owner can't be added, the agent
        # can't be chatted — the invariant wins). A DM peer arriving from
        # create_dm was message-gated there instead.
        if kind is ChatKind.GROUP:
            for m in roster:
                if m not in _message_gated:
                    self._gate_add(m)

        # GENESIS ADMIN RULE (Aryan 2026-07-12, "agents tied to their owners"):
        # a chat born from MESSAGING AN AGENT (auto_dm, either direction) or
        # CREATED BY an agent makes EVERY human at genesis an admin — equal
        # rights, full oversight. A deliberate human-created group keeps the
        # classic creator-is-admin. Agents are never admins anywhere.
        creator_is_agent = self.directory.kind(me) is UserKind.AGENT
        if creator_is_agent:
            owner = self.directory.owner_of(me)
            if owner is None or owner not in roster:
                raise ValidationError("an agent-created chat needs its responsible member")

        def genesis_role(m: str) -> str:
            if kind is not ChatKind.GROUP:
                return Role.MEMBER.value
            if auto_dm or creator_is_agent:
                is_human = self.directory.kind(m) is UserKind.HUMAN
                return Role.ADMIN.value if is_human else Role.MEMBER.value
            return Role.ADMIN.value if m == me else Role.MEMBER.value

        member_roles = {m: genesis_role(m) for m in roster}
        event = {
            "type": events.EV_CREATED,
            "kind": kind.value,
            "name": (name or "").strip(),
            "members": member_roles,
            "auto_dm": auto_dm,
            "creator": me,
            # a random nonce makes the genesis (and thus the chat id) unique
            # even for an identical roster, and unguessable to a forger
            "nonce": secrets.token_hex(8),
        }
        if permissions:
            event["permissions"] = permissions
        # R13.5: the id COMMITS to the genesis (the `-g` marks a v2 gid-bound
        # id) — the fold rejects any `created` for this id that doesn't
        # re-hash to the same gid, so no forged/backdated genesis can hijack
        # an existing chat.
        chat_id = f"{_slug(name)}-g{events.genesis_gid(event)}"
        if pulled:
            # {owner: agent} — the UI renders "X joined as Y's responsible
            # member". Added AFTER the gid (it's a volatile hint, not part of
            # the commitment — genesis_gid deliberately excludes it).
            event["pulled"] = pulled

        env = self.messaging.build_event(chat_id, event)
        # meta FIRST (fold of the genesis), so the member gate holds from here on
        snap = events.fold(chat_id, [env.to_dict()], self.directory)
        self.tx.put_doc(P.meta(chat_id), snap.to_dict())
        self.messaging.commit_envelope(chat_id, env)
        return snap

    def create_dm(self, other: str) -> ChatSnapshot:
        me = self.user
        if other == me:
            raise ValidationError("a direct chat needs someone else")
        if not self.directory.exists(other):
            raise ValidationError(f"unknown user @{other}")

        # R6 messaging gate — the PUBLIC one an agent can check beforehand
        if self.privacy is not None:
            ok, why = self.privacy.can_message(me, other)
            if not ok:
                raise PermissionDenied(why)

        pulled = self.directory.missing_owners([me, other])
        if not pulled:
            for snap in self._snapshots():
                if snap.kind is ChatKind.DM and set(snap.members) == {me, other}:
                    return snap  # DMs dedupe (v1)
            return self.create_chat("", members=[other], kind=ChatKind.DM)

        # agent + non-owner: a two-person chat can't hold three — it is born
        # as a small GROUP with the owner in (v1 auto_dm semantics, verified
        # symmetric in both directions). The peer was message-gated above;
        # the pulled owner still passes the add-to-group gate in create_chat.
        roster = [me, other, *pulled]
        for snap in self._snapshots():
            if snap.auto_dm and set(snap.members) == set(roster):
                return snap
        name = ", ".join(self.directory.display(m) for m in roster)[:60]
        return self.create_chat(
            name, members=[other], kind=ChatKind.GROUP, auto_dm=True,
            _message_gated=frozenset({other}),
        )

    def create_self_chat(self) -> ChatSnapshot:
        """Message-yourself (WhatsApp note-to-self)."""
        for snap in self._snapshots():
            if snap.kind is ChatKind.SELF and set(snap.members) == {self.user}:
                return snap
        return self.create_chat("", kind=ChatKind.SELF)

    # ------------------------------------------------------------ mutations
    def add_members(self, chat_id: str, names: list[str]) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        my_owner = (
            self.directory.owner_of(self.user)
            if self.directory.kind(self.user) is UserKind.AGENT
            else None
        )
        if not authz.can_add_members(snap, self.user, agent_owner=my_owner):
            raise PermissionDenied(
                "this group doesn't allow you (an agent) to add members"
                if my_owner is not None
                else "you may not add members to this chat"
            )
        todo = [n for n in dict.fromkeys(names) if n not in snap.members]
        for n in todo:
            self._require_alive(n)  # unknown OR deleted (V49)
        pulled = self.directory.missing_owners(list(snap.members) + todo)
        for n in [*todo, *pulled]:  # R6 gate — pulled owners included
            self._gate_add(n)
        for n in todo:
            self.messaging.post_event(
                chat_id, {"type": events.EV_MEMBER_ADDED, "who": n, "by": self.user}
            )
        for owner, agent in pulled.items():
            self.messaging.post_event(
                chat_id,
                {"type": events.EV_MEMBER_ADDED, "who": owner, "by": self.user,
                 "reason": "responsible_member", "agent": agent},
            )
        healed = self.refold(chat_id)
        if self.keys is not None:
            newcomers = [n for n in [*todo, *pulled] if n in healed.members]
            self.keys.on_members_added(chat_id, healed, newcomers)
        return healed

    def remove_member(self, chat_id: str, who: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if who == self.user:
            raise ValidationError("use leave() to exit a chat yourself")
        if who not in snap.members:
            raise ValidationError(f"@{who} is not a member")
        if self.directory.kind(self.user) is UserKind.AGENT:
            raise PermissionDenied("agents can add members but never remove them")
        owns_target = (
            self.directory.kind(who) is UserKind.AGENT
            and self.directory.owner_of(who) == self.user
        )
        if not authz.can_remove_member(snap, self.user, owns_target=owns_target):
            raise PermissionDenied("only an admin can remove members")
        self.messaging.post_event(
            chat_id, {"type": events.EV_MEMBER_REMOVED, "who": who, "by": self.user}
        )
        # R56 (V37): the fold's heal silently cascades the removed member's
        # agents out — RECORD those departures so the transcript tells the
        # story. The events are fold no-ops (heal already dropped them); the
        # pills render from the log.
        for agent in self._cascading_agents(snap, who):
            self.messaging.post_event(
                chat_id, {"type": events.EV_MEMBER_REMOVED, "who": agent,
                          "by": self.user, "reason": "with_owner", "owner": who}
            )
        healed = self.refold(chat_id)
        if self.keys is not None:  # rotate away from the removed member now
            self.keys.on_members_removed(chat_id, healed)
        return healed

    def leave(self, chat_id: str) -> ChatSnapshot:
        # R56 (V37): record the owned agents the heal will cascade out with
        # me — posted BEFORE my own departure (a member may still write).
        snap = self.messaging.snapshot(chat_id)
        for agent in self._cascading_agents(snap, self.user):
            self.messaging.post_event(
                chat_id, {"type": events.EV_MEMBER_REMOVED, "who": agent,
                          "by": self.user, "reason": "with_owner",
                          "owner": self.user}
            )
        self.messaging.post_event(chat_id, {"type": events.EV_MEMBER_LEFT})
        healed = self.refold(chat_id)
        if self.keys is not None:  # R69: rotate the epoch away from me on exit
            self.keys.on_member_left(chat_id, healed)
        return healed

    def grant_admin(self, chat_id: str, who: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_grant_admin(snap, self.user):
            raise PermissionDenied("only an admin can appoint admins")
        if who not in snap.members:
            raise ValidationError(f"@{who} is not a member")
        if self.directory.kind(who) is UserKind.AGENT:
            raise ValidationError("agents cannot be group admins")
        self.messaging.post_event(
            chat_id, {"type": events.EV_ADMIN_GRANTED, "who": who, "by": self.user}
        )
        return self.refold(chat_id)

    def revoke_admin(self, chat_id: str, who: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_grant_admin(snap, self.user):
            raise PermissionDenied("only an admin can dismiss admins")
        if who not in snap.members or not authz.is_admin(snap, who):
            raise ValidationError(f"@{who} is not an admin")
        self.messaging.post_event(
            chat_id, {"type": events.EV_ADMIN_REVOKED, "who": who, "by": self.user}
        )
        return self.refold(chat_id)

    def rename(self, chat_id: str, name: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_edit_settings(snap, self.user):
            raise PermissionDenied("you may not edit this chat's settings")
        if not (name or "").strip():
            raise ValidationError("a chat needs a name")
        self.messaging.post_event(
            chat_id, {"type": events.EV_RENAMED, "name": name.strip(), "by": self.user}
        )
        return self.refold(chat_id)

    def set_description(self, chat_id: str, text: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_edit_settings(snap, self.user):
            raise PermissionDenied("you may not edit this chat's settings")
        self.messaging.post_event(
            chat_id, {"type": events.EV_DESCRIPTION, "text": text or "", "by": self.user}
        )
        return self.refold(chat_id)

    def set_avatar(self, chat_id: str, data: bytes) -> ChatSnapshot:
        """Group photo: blob + an EV_AVATAR event carrying its sha (the
        marker lives in the FOLD, rebuildable — never a LWW meta field)."""
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_edit_settings(snap, self.user):
            raise PermissionDenied("you may not edit this chat's settings")
        if not data:
            raise ValidationError("empty image")
        sha = hashlib.sha256(data).hexdigest()
        self.tx.put_blob(P.chat_avatar(chat_id), data)
        self.messaging.post_event(
            chat_id, {"type": events.EV_AVATAR, "sha": sha, "by": self.user}
        )
        return self.refold(chat_id)

    def clear_avatar(self, chat_id: str) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_edit_settings(snap, self.user):
            raise PermissionDenied("you may not edit this chat's settings")
        self.messaging.post_event(
            chat_id, {"type": events.EV_AVATAR, "sha": "", "by": self.user}
        )
        return self.refold(chat_id)

    def delete_chat(self, chat_id: str) -> ChatSnapshot:
        """Group deletion for everyone — admin-only, TERMINAL in the fold
        (members empty afterwards, so every read and write refuses). Bodies
        stay on disk until a future janitor round reclaims them."""
        snap = self.messaging.snapshot(chat_id)
        if snap.kind is not ChatKind.GROUP:
            raise ValidationError("only groups can be deleted for everyone")
        if not authz.is_admin(snap, self.user):
            raise PermissionDenied("only an admin can delete this group")
        self.messaging.post_event(
            chat_id, {"type": events.EV_DELETED, "by": self.user}
        )
        return self.refold(chat_id)

    def set_permissions(self, chat_id: str, changes: dict) -> ChatSnapshot:
        snap = self.messaging.snapshot(chat_id)
        if not authz.can_change_permissions(snap, self.user):
            raise PermissionDenied("only an admin can change group permissions")
        known = set(ChatPermissions().__dict__)
        unknown = set(changes) - known
        if unknown:
            raise ValidationError(f"unknown permission(s): {sorted(unknown)}")
        self.messaging.post_event(
            chat_id, {"type": events.EV_PERMISSIONS, "permissions": changes, "by": self.user}
        )
        return self.refold(chat_id)

    # -------------------------------------------------------------- snapshots
    def refold(self, chat_id: str) -> ChatSnapshot:
        """Recompute the snapshot from the event log and rewrite meta.json.
        Heals any clobbered/stale meta (the whole point of tenet 3).

        Guard: a local cache that hasn't synced the chat's GENESIS yet would
        fold to an empty snapshot — never clobber meta with that; partial-but-
        genesis-bearing folds converge via every member's next refold."""
        snap = events.fold(chat_id, self.store.messages(chat_id), self.directory)
        if not snap.members and not snap.deleted:
            return self.messaging.snapshot(chat_id)
        try:
            self.tx.put_doc(P.meta(chat_id), snap.to_dict())
        except TransportError:
            # meta is a REBUILDABLE CACHE (tenet 3): a transiently locked
            # write (scanner/sync holding the file) must never fail the user
            # action that triggered it — the fold result is still correct and
            # the next mutation/refold rewrites the snapshot. Windows-CI burn.
            log.warning("meta write for %s deferred (transient lock)", chat_id)
        return snap

    def chats_for(self) -> list[ChatSnapshot]:
        """Every chat THIS identity is a member of (visibility = membership)."""
        return [s for s in self._snapshots() if s.is_member(self.user)]

    def _snapshots(self):
        for chat_id in self.tx.list_chat_ids():
            doc = self.tx.get_doc(P.meta(chat_id))
            if isinstance(doc, dict):
                yield ChatSnapshot.from_dict(doc)
