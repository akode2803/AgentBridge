"""Membership service — chats, DMs, self-chats, multi-admin groups.

Mutations emit INFO EVENTS into the actor's own log (the source of truth) and
then rematerialize ``meta.json`` from the local fold. A snapshot written from
a partial local view self-heals: every other member's next mutation (or an
explicit ``refold``) reproduces the canonical state from the union of events.

Multi-admin model (replaces v1's owner): admins appoint/dismiss admins, agents
can never be admins, and the fold auto-promotes the longest-standing human if
a group would go admin-less. The free-chatting invariant (owner pull-in) is
ported from v1's verified ``_missing_owners``.

Under a bound typed writer the order flips (meta first): each action is one
compare-and-swap of ``meta.json`` whose build re-checks authority against the
fresh provider copy and mints the action's events above its watermark; the
events are logged only once that write applied. A ``membership_actions``
Store row carries them across a crash in between.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
from uuid import UUID, uuid5

from ..core.errors import NotAMember, PermissionDenied, TransportError, ValidationError
from ..core.models import ChatKind, ChatPermissions, ChatSnapshot, Role, UserKind
from ..store.db import Store
from ..transport.application_mutations import DocumentContended
from ..transport.base import Transport
from ..transport.typed_mutations import TypedMutationRPCError
from . import authz, events
from .directory import Directory
from .messaging import MessagingService
from .paths import P
from .privacy import PrivacyService

__all__ = ["MembershipService"]

log = logging.getLogger("agentbridge.mesh")

# Permanent domain for a membership action's meta CAS operation id, derived
# from its first envelope so the exact attempt is findable after a crash.
_ACTION_NAMESPACE = UUID("5fbe1499-3434-46e4-8fe1-406262e61fa7")


def _meta_doc(snap: ChatSnapshot) -> dict:
    """The exact JSON a typed meta write carries (enums as their values)."""
    return json.loads(json.dumps(snap.to_dict()))


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
        self._action_lock = threading.RLock()

    def _typed(self) -> bool:
        from ..transport.local_mutations import LocalMutationTransport

        return isinstance(self.tx, LocalMutationTransport) and self.tx._bound_writer() is not None

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
        if self._typed():
            return self._create_typed(chat_id, env.to_dict(), snap)
        # Genesis is an INSERT, not an UPSERT. Supabase RLS intentionally
        # admits this one birth row before ab_is_member can resolve the chat;
        # later meta materializations keep using put_doc below.
        self.tx.create_doc(P.meta(chat_id), snap.to_dict())
        self.messaging.commit_envelope(chat_id, env)
        return snap

    def _create_typed(self, chat_id: str, envelope: dict, snap: ChatSnapshot) -> ChatSnapshot:
        """Genesis under a bound writer: one exact create of the meta (keyed
        by its unique path), then the genesis event. The Store row holds the
        envelope until the create's outcome is known."""
        path = P.meta(chat_id)
        doc = _meta_doc(snap)
        with self._action_lock:
            action = self.store.membership_action_add(
                chat_id, self.tx.exact_create_id(path), [envelope])
            self.tx.create_exact_doc(path, doc, keep_ticket=True)
            self.messaging.remember_meta(chat_id, doc)
            self.messaging.commit_action(action)
            self.tx.release_operation(action.operation_id)
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

        if self._typed():
            # Settle a lost genesis first and hold the lock across the scan
            # and the create, so a retry finds the chat instead of a twin.
            with self._action_lock:
                self._settle_actions(strict=True, genesis_only=True)
                return self._create_dm(me, other)
        return self._create_dm(me, other)

    def _create_dm(self, me: str, other: str) -> ChatSnapshot:
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
        if self._typed():
            with self._action_lock:
                self._settle_actions(strict=True, genesis_only=True)
                return self._create_self_chat()
        return self._create_self_chat()

    def _create_self_chat(self) -> ChatSnapshot:
        for snap in self._snapshots():
            if snap.kind is ChatKind.SELF and set(snap.members) == {self.user}:
                return snap
        return self.create_chat("", kind=ChatKind.SELF)

    # ------------------------------------------------------------ mutations
    def add_members(self, chat_id: str, names: list[str]) -> ChatSnapshot:
        planned: list[str] = []

        def plan(snap):
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
            planned[:] = [*todo, *pulled]
            return [
                *(({"type": events.EV_MEMBER_ADDED, "who": n, "by": self.user}, True)
                  for n in todo),
                *(({"type": events.EV_MEMBER_ADDED, "who": owner, "by": self.user,
                    "reason": "responsible_member", "agent": agent}, True)
                  for owner, agent in pulled.items()),
            ]

        healed = self._act(chat_id, plan)
        if self.keys is not None:
            newcomers = [n for n in planned if n in healed.members]
            self.keys.on_members_added(chat_id, healed, newcomers)
        return healed

    def remove_member(self, chat_id: str, who: str) -> ChatSnapshot:
        def plan(snap):
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
            # R56 (V37): the fold's heal silently cascades the removed member's
            # agents out — RECORD those departures so the transcript tells the
            # story. The events are fold no-ops (heal already dropped them); the
            # pills render from the log.
            return [
                ({"type": events.EV_MEMBER_REMOVED, "who": who, "by": self.user}, True),
                *(({"type": events.EV_MEMBER_REMOVED, "who": agent, "by": self.user,
                    "reason": "with_owner", "owner": who}, False)
                  for agent in self._cascading_agents(snap, who)),
            ]

        healed = self._act(chat_id, plan)
        if self.keys is not None:  # rotate away from the removed member now
            self.keys.on_members_removed(chat_id, healed)
        return healed

    def leave(self, chat_id: str, *, reason: str = "") -> ChatSnapshot:
        # Refuse the whole action before queuing any cascading agent removals.
        self.messaging._require_event_supported({'type': events.EV_MEMBER_LEFT})
        # R56 (V37): record the owned agents the heal will cascade out with
        # me — posted BEFORE my own departure (a member may still write).
        snap = self.messaging.snapshot(chat_id)
        for agent in self._cascading_agents(snap, self.user):
            self.messaging.post_event(
                chat_id, {"type": events.EV_MEMBER_REMOVED, "who": agent,
                          "by": self.user, "reason": "with_owner",
                          "owner": self.user}
            )
        # ``reason`` rides the event for the pill ("owner_changed", V69 —
        # an agent departing because its responsible member changed); the
        # fold ignores extra keys, so old clients render a plain "left"
        event = {"type": events.EV_MEMBER_LEFT}
        if reason:
            event["reason"] = reason
        self.messaging.post_event(chat_id, event)
        healed = self.materialize(chat_id, write=False)
        if self.keys is not None:  # R69: rotate the epoch away from me on exit
            self.keys.on_member_left(chat_id, healed)
        self.messaging.flush_outbox()
        return healed

    def grant_admin(self, chat_id: str, who: str) -> ChatSnapshot:
        def plan(snap):
            if not authz.can_grant_admin(snap, self.user):
                raise PermissionDenied("only an admin can appoint admins")
            if who not in snap.members:
                raise ValidationError(f"@{who} is not a member")
            if self.directory.kind(who) is UserKind.AGENT:
                raise ValidationError("agents cannot be group admins")
            return [({"type": events.EV_ADMIN_GRANTED, "who": who, "by": self.user}, True)]

        return self._act(chat_id, plan)

    def revoke_admin(self, chat_id: str, who: str) -> ChatSnapshot:
        def plan(snap):
            if not authz.can_grant_admin(snap, self.user):
                raise PermissionDenied("only an admin can dismiss admins")
            if who not in snap.members or not authz.is_admin(snap, who):
                raise ValidationError(f"@{who} is not an admin")
            return [({"type": events.EV_ADMIN_REVOKED, "who": who, "by": self.user}, True)]

        return self._act(chat_id, plan)

    def _settings(self, chat_id: str, event: dict) -> ChatSnapshot:
        def plan(snap):
            if not authz.can_edit_settings(snap, self.user):
                raise PermissionDenied("you may not edit this chat's settings")
            return [(event, True)]

        return self._act(chat_id, plan)

    def rename(self, chat_id: str, name: str) -> ChatSnapshot:
        if not (name or "").strip():
            # Authority first, as before: a non-editor learns nothing more.
            snap = self.messaging.snapshot(chat_id)
            if not authz.can_edit_settings(snap, self.user):
                raise PermissionDenied("you may not edit this chat's settings")
            raise ValidationError("a chat needs a name")
        return self._settings(
            chat_id, {"type": events.EV_RENAMED, "name": name.strip(), "by": self.user})

    def set_description(self, chat_id: str, text: str) -> ChatSnapshot:
        return self._settings(
            chat_id, {"type": events.EV_DESCRIPTION, "text": text or "", "by": self.user})

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
        return self._settings(
            chat_id, {"type": events.EV_AVATAR, "sha": sha, "by": self.user})

    def clear_avatar(self, chat_id: str) -> ChatSnapshot:
        return self._settings(
            chat_id, {"type": events.EV_AVATAR, "sha": "", "by": self.user})

    def delete_chat(self, chat_id: str) -> ChatSnapshot:
        """Group deletion for everyone — admin-only, TERMINAL in the fold
        (members empty afterwards, so every read and write refuses). Bodies
        stay on disk until a future janitor round reclaims them."""
        self.messaging._require_event_supported({'type': events.EV_DELETED})
        snap = self.messaging.snapshot(chat_id)
        if snap.kind is not ChatKind.GROUP:
            raise ValidationError("only groups can be deleted for everyone")
        if not authz.is_admin(snap, self.user):
            raise PermissionDenied("only an admin can delete this group")
        self.messaging.post_event(
            chat_id, {"type": events.EV_DELETED, "by": self.user}
        )
        preview = self.materialize(chat_id, write=False)
        self.messaging.flush_outbox()
        return preview

    def set_permissions(self, chat_id: str, changes: dict) -> ChatSnapshot:
        def plan(snap):
            if not authz.can_change_permissions(snap, self.user):
                raise PermissionDenied("only an admin can change group permissions")
            known = set(ChatPermissions().__dict__)
            unknown = set(changes) - known
            if unknown:
                raise ValidationError(f"unknown permission(s): {sorted(unknown)}")
            return [({"type": events.EV_PERMISSIONS, "permissions": changes,
                      "by": self.user}, True)]

        return self._act(chat_id, plan)

    # ---------------------------------------------------------- the owner
    def _act(self, chat_id: str, plan) -> ChatSnapshot:
        """Run one membership action. ``plan(snap)`` checks authority against
        ``snap`` and returns ``[(event, primary), ...]``; a primary event must
        take effect in the fold (cascade pills are fold no-ops)."""
        if not self._typed():
            snap = self.messaging.snapshot(chat_id)
            for event, _primary in plan(snap):
                self.messaging.post_event(chat_id, event)
            return self.materialize(chat_id)
        return self._act_typed(chat_id, plan)

    def _fresh(self, chat_id: str, current) -> ChatSnapshot:
        """The provider meta advanced by locally ingested newer state events."""
        if not isinstance(current, dict):
            raise NotAMember(f"unknown chat {chat_id}")
        base = ChatSnapshot.from_dict(current)
        return events.advance(
            base, self.store.state_events_after(chat_id, base.materialized_ns),
            self.directory)

    def _act_typed(self, chat_id: str, plan) -> ChatSnapshot:
        """Meta first: one CAS whose build re-checks authority on the fresh
        provider meta and applies the newly minted events on top of it, in
        CAS order. The events are logged only after the swap applied; the
        Store row bridges a crash in between and names the exact attempt,
        whose journal ticket is released only once the row is consumed."""
        with self._action_lock:
            self._settle_actions(chat_id, strict=True)
            minted: list[list[dict]] = []
            recorded: list = []

            def drop_recorded():
                # Each recorded attempt definitely did not apply.
                for stale in recorded:
                    self.store.membership_action_discard(stale.id)
                    self.tx.release_operation(stale.operation_id)
                recorded.clear()

            def build(current):
                drop_recorded()  # a rebuild follows only a conflict
                snap = self._fresh(chat_id, current)
                if not snap.is_member(self.user):
                    raise NotAMember(f"{self.user} is not a member of {chat_id}")
                planned = plan(snap)
                if not planned:
                    return None
                envelopes, primary = [], set()
                for event, is_primary in planned:
                    env = self.messaging.build_event(chat_id, event).to_dict()
                    envelopes.append(env)
                    if is_primary:
                        primary.add(env["id"])
                after, applied = events.apply_own(snap, envelopes, self.directory)
                if not primary <= applied:
                    raise PermissionDenied("this change is not allowed in this chat any more")
                minted[:] = [envelopes]
                return _meta_doc(after)

            def operation_for(_payload):
                (envelopes,) = minted
                action = self.store.membership_action_add(
                    chat_id, str(uuid5(_ACTION_NAMESPACE, f"{chat_id}\x00{envelopes[0]['id']}")),
                    envelopes)
                recorded.append(action)
                return action.operation_id

            try:
                document, changed = self.tx.update_doc(
                    P.meta(chat_id), build, operation_for=operation_for)
            except DocumentContended:
                drop_recorded()
                raise
            if not changed:
                return self._fresh(chat_id, document)
            (action,) = recorded
            self.messaging.remember_meta(chat_id, document)
            self.messaging.commit_action(action)
            self.tx.release_operation(action.operation_id)
            return ChatSnapshot.from_dict(document)

    def _consume(self, action, document: dict) -> None:
        """An interrupted action applied: show its meta, log its events, and
        run the key steps its caller never reached."""
        self.messaging.remember_meta(action.chat_id, document)
        self.messaging.commit_action(action)
        if self.keys is None:
            return
        added = [(env["event"].get("who"), env["ns"]) for env in action.envelopes
                 if env["event"].get("type") == events.EV_MEMBER_ADDED]
        removed = [env["event"].get("who") for env in action.envelopes
                   if env["event"].get("type") == events.EV_MEMBER_REMOVED]
        if not added and not removed:
            return
        try:
            snap = self.messaging.snapshot(action.chat_id)
            # Bound to this exact membership instance, and only when the
            # current view agrees: a stale view must never re-key for (or
            # re-wrap history to) someone who has since left or been removed.
            newcomers = [who for who, ns in added
                         if who in snap.members and snap.members[who].joined_ns == ns]
            if newcomers:
                self.keys.on_members_added(action.chat_id, snap, newcomers)
            if removed and not any(who in snap.members for who in removed):
                self.keys.on_members_removed(action.chat_id, snap)
        except Exception:  # noqa: BLE001 — ensure() re-keys on the next send
            log.warning("key step after a recovered membership action failed",
                        exc_info=True)

    def _settle_actions(self, chat_id: str | None = None, *, strict: bool = False,
                        genesis_only: bool = False) -> int:
        """Bring leftover actions to their outcome: log the events of an
        attempt that applied, drop the rest. ``strict`` re-raises (a chat's
        unknown outcome blocks its next action); otherwise one stuck chat
        never stops the others. Returns how many rows were settled."""
        settled, after = 0, 0
        while True:
            rows = self.store.membership_actions_pending(
                chat_id=chat_id, limit=64, after_id=after)
            for action in rows:
                if (genesis_only
                        and action.envelopes[0]["event"].get("type") != events.EV_CREATED):
                    continue
                try:
                    self._settle_one(action)
                    settled += 1
                except Exception:
                    if strict:
                        raise
                    log.warning("membership action for %s is still unsettled",
                                action.chat_id, exc_info=True)
            if len(rows) < 64:
                return settled
            after = rows[-1].id

    def _settle_one(self, action) -> None:
        try:
            outcome = self.tx.settle_operation(action.operation_id)
        except TypedMutationRPCError as exc:
            # A refused swap whose document moved on can never apply: every
            # membership change bumps its version. Anything else stays.
            if exc.code is None or not self.tx.attempt_superseded(action.operation_id):
                raise
            self.store.membership_action_discard(action.id)
            return
        if outcome is None:  # never prepared, so never dispatched
            self.store.membership_action_discard(action.id)
            return
        result, arguments = outcome
        if result.get("applied") is True or result.get("created") is True:
            self._consume(action, arguments["p_data"])
        else:
            self.store.membership_action_discard(action.id)
        self.tx.release_operation(action.operation_id)

    def recover(self) -> int:
        """Finish every membership action a crash or lost reply interrupted.
        Activation wiring runs it (before ``ChatKeyService.recover``)."""
        if not self._typed():
            return 0
        with self._action_lock:
            return self._settle_actions()

    # -------------------------------------------------------------- snapshots
    def materialize(self, chat_id: str, *, write: bool = True) -> ChatSnapshot:
        """Persist the effective snapshot without rebuilding local history.

        Routine mutations start from the trusted remote materialization and
        advance it with authenticated local state events. This preserves a
        contracted ACL even when the local cache temporarily lacks an older
        writer log. Under a bound writer this is a CAS over the fresh
        provider meta (no write when nothing advanced).
        """
        snap = self.messaging.snapshot(chat_id)
        if not write:
            return snap
        if self._typed():
            with self._action_lock:
                self._settle_actions(chat_id, strict=True)

                def build(current):
                    doc = _meta_doc(self._fresh(chat_id, current))
                    return None if doc == current else doc

                document, changed = self.tx.update_doc(P.meta(chat_id), build)
                if changed:
                    self.messaging.remember_meta(chat_id, document)
                return ChatSnapshot.from_dict(document)
        try:
            self.tx.put_doc(P.meta(chat_id), snap.to_dict())
        except TransportError:
            log.warning("meta write for %s deferred (transient lock)", chat_id)
        return snap

    def refold(self, chat_id: str, *, write: bool = True) -> ChatSnapshot:
        """Recompute the snapshot from the event log and rewrite meta.json.
        Heals any clobbered/stale meta (the whole point of tenet 3).

        This explicit repair path performs a complete replay. Routine sync and
        authorization reads never call it because a transient folder listing
        is not proof that every writer log is locally present.
        """
        snap = events.fold(chat_id, self.store.messages(chat_id), self.directory)
        if not snap.members and not snap.deleted:
            return self.messaging.snapshot(chat_id)
        if not write:
            return snap
        if self._typed():
            # Only the pre-R25 tenure backfill: a local fold may lack another
            # writer's log, so it never replaces members, roles or settings
            # of the provider meta it would race.
            with self._action_lock:
                self._settle_actions(chat_id, strict=True)

                def build(current):
                    if not isinstance(current, dict) or "tenure" in current:
                        return None
                    fold = events.fold(chat_id, self.store.messages(chat_id), self.directory)
                    doc = dict(current, tenure=_meta_doc(fold)["tenure"])
                    doc["materialized_ns"] = ChatSnapshot.from_dict(current).materialized_ns + 1
                    return doc

                document, changed = self.tx.update_doc(P.meta(chat_id), build)
                if changed:
                    self.messaging.remember_meta(chat_id, document)
                return ChatSnapshot.from_dict(document)
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
        return list(self.iter_chats_for())

    def iter_chats_for(self):
        """Yield visible chats lazily so latency-sensitive consumers can act
        between complete snapshot folds without weakening membership checks."""
        for snap in self._snapshots():
            if snap.is_member(self.user):
                yield snap

    def _snapshots(self):
        listed = list(self.tx.list_chat_ids())
        written = [c for c in self.messaging.written_chat_ids() if c not in listed]
        for chat_id in [*listed, *written]:
            try:
                yield self.messaging.snapshot(chat_id)
            except (NotAMember, ValueError, TypeError):
                continue
