"""Info events — the SOURCE OF TRUTH for chat state (FORMAT2 tenet 3).

``fold`` replays every info event in ``(ns, from, id)`` order and produces the
ChatSnapshot deterministically. ``meta.json`` is only a materialized cache of
this fold: clobber it, and ``refold`` reproduces it bit-for-bit.

The fold is also the permission system REPLAYED: an event only takes effect if
its author had the authority at that point (a forged ``member_added`` from a
non-member, or an ``admin_granted`` targeting an agent, is simply ignored).
Under E2EE nobody can be stopped from *writing* files — but the fold decides
what counts.

Standing rules enforced here:
- agents can never hold the admin role;
- removing a member cascades out any agent left without its owner (the
  free-chatting invariant: no agent in a room without a responsible member);
- a group never stays admin-less while human members remain — the
  longest-standing human is auto-promoted (WhatsApp semantics).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .. import crypto
from ..core.models import (
    ChatKind,
    ChatPermissions,
    ChatSnapshot,
    Member,
    Role,
    UserKind,
)
from . import authz

__all__ = [
    "EV_CREATED", "EV_MEMBER_ADDED", "EV_MEMBER_REMOVED", "EV_MEMBER_LEFT",
    "EV_ADMIN_GRANTED", "EV_ADMIN_REVOKED", "EV_RENAMED", "EV_DESCRIPTION",
    "EV_PERMISSIONS", "EV_AVATAR", "EV_DELETED", "EV_KEY_ROTATED",
    "EV_REACTION",
    "Resolver", "fold", "advance", "signing_bytes", "redaction_signing_bytes",
    "unredaction_signing_bytes",
    "reaction_signing_bytes", "pin_signing_bytes", "state_signing_bytes",
    "genesis_gid", "GID_LEN", "is_legacy_chat_id",
    "InfoEventIdentity", "InfoEventOutcome", "InfoSuffixEvaluation", "advance_with_outcomes",
]

# ------------------------------------------------------------- R13.5 integrity
# Genesis binding: a v2 chat id ends in "-<gid>" where gid commits to the
# genesis event's content (+ a random nonce the creator chose). The fold
# accepts a `created` for such an id ONLY if the event re-hashes to that gid,
# so no one can forge an ALTERNATIVE (e.g. backdated) genesis for an existing
# chat — the id itself pins the one true genesis. Legacy/migrated ids carry no
# gid and are accepted as-is (documented residual in docs/THREAT_MODEL.md).
GID_LEN = 16


def genesis_gid(event: dict) -> str:
    """The genesis commitment: sha256 over the created-event's identity-
    bearing fields (everything but the volatile `pulled` hint and any `sig`)."""
    core = {
        k: event[k]
        for k in ("type", "kind", "name", "description", "members",
                  "permissions", "auto_dm", "creator", "nonce")
        if k in event
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:GID_LEN]


def signing_bytes(chat_id: str, env: dict) -> bytes:
    """Canonical bytes an info event's author signs (R13.5): chat | id | ns |
    from | canonical(event). chat binds the signature to ONE room (no
    cross-chat replay); signer and verifier MUST agree byte-for-byte."""
    event = env.get("event") or {}
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return (
        f"{chat_id}|{env.get('id', '')}|{env.get('ns', 0)}|"
        f"{env.get('from', '')}|{body}"
    ).encode()


def redaction_signing_bytes(chat_id: str, msg_id: str, by: str, ns: int) -> bytes:
    """Canonical bytes the author of a delete-for-everyone signs (R25). Binds
    the chat (no cross-room replay), the target message, the author, and the
    ns — so a folder writer can't forge a redaction attributed to the sender
    (the read model verifies this before honoring a tombstone; unsigned/forged
    redactions are ignored and the message stays visible)."""
    return f"{chat_id}|redact|{msg_id}|{by}|{ns}".encode()


def unredaction_signing_bytes(
    chat_id: str, msg_id: str, red_ns: int, by: str, ns: int,
) -> bytes:
    """Canonical bytes an UNDO of a delete-for-everyone signs (R44). Binds
    the chat, the target message, **the ns of the redaction it voids**
    (a saved undo can't be replayed onto a later re-delete of the same
    message), the actor and the undo's own ns. The undo is presence-based —
    the redaction doc keeps its original signed record and gains a signed
    ``void`` — so restoring never means deleting a doc (absence can't be
    authenticated; a forged void just fails verification and the tombstone
    stands)."""
    return f"{chat_id}|unredact|{msg_id}|{red_ns}|{by}|{ns}".encode()


def reaction_signing_bytes(chat_id: str, user: str, ns: int, mapping: dict) -> bytes:
    """Canonical bytes a user signs over their whole per-chat reaction file
    (R31 — same recipe as redactions). The file is single-writer, so the
    signature covers the full {msg_id: emoji} mapping plus the write's ns:
    a transport writer can no longer fabricate a reaction attributed to
    someone else (readers ignore files whose signature doesn't verify)."""
    body = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
    return f"{chat_id}|react|{user}|{ns}|{body}".encode()


def pin_signing_bytes(
    chat_id: str, msg_id: str, by: str, ns: int, until_ns: int = 0
) -> bytes:
    """Canonical bytes the pinner signs (R31). Binds the chat, the target
    message, the pinner, the ns, and the expiry — so a dropped-in pin doc
    attributed to a member (or a tampered expiry) doesn't verify."""
    return f"{chat_id}|pin|{msg_id}|{by}|{ns}|{until_ns}".encode()


def state_signing_bytes(chat_id: str, user: str, ns: int, fields: dict) -> bytes:
    """Canonical bytes over a user's per-chat state overlay (R31.5 — same
    recipe as reactions). The doc shapes what its OWNER sees (``hidden``/
    ``cleared``) and what others derive from it (the ``read_ns`` cursor
    behind read receipts, ``mute`` behind notifications), so it must be
    attributable: verified readers treat an unsigned or mis-signed doc as
    absent. ``fields`` is the doc without its ``ns``/``sig`` envelope."""
    body = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return f"{chat_id}|state|{user}|{ns}|{body}".encode()

EV_CREATED = "created"
EV_MEMBER_ADDED = "member_added"
EV_MEMBER_REMOVED = "member_removed"
EV_MEMBER_LEFT = "member_left"
EV_ADMIN_GRANTED = "admin_granted"
EV_ADMIN_REVOKED = "admin_revoked"
EV_RENAMED = "renamed"
EV_DESCRIPTION = "description"
EV_PERMISSIONS = "permissions_changed"
EV_AVATAR = "avatar"
EV_DELETED = "chat_deleted"     # terminal: admins only, groups only (R13)
EV_KEY_ROTATED = "key_rotated"  # applied in R9
# V50: a notification BREADCRUMB, not chat state — the fold ignores it (the
# unknown-type rule already made that safe on old clients). The per-user
# reaction OVERLAY doc stays the authoritative store; this event exists only
# so the sync bus can see a reaction happen (overlay docs never ride the log).
EV_REACTION = "reaction"


class Resolver(Protocol):
    """What the fold needs to know about accounts (Directory satisfies it)."""

    def kind(self, name: str) -> UserKind | None: ...
    def owner_of(self, name: str) -> str | None: ...
    def sign_pub(self, name: str) -> str | None: ...  # R13.5 signature verify


@dataclass(frozen=True)
class InfoEventIdentity:
    """Exact composite identity, not an event acceptance or authority result."""
    ns: int
    sender: str
    id: str

    def __post_init__(self):
        if (type(self.ns) is not int or not 0 <= self.ns <= 2**63 - 1
                or not _bounded_utf8(self.sender, 4096)
                or not _bounded_utf8(self.id, 4096, nonempty=True)):
            raise ValueError('invalid info event identity')


@dataclass(frozen=True)
class InfoEventOutcome:
    identity: InfoEventIdentity
    status: str  # applied / ignored / outside_proven_suffix
    authentication: str  # signed_author / gid_content / unproven
    actor: str = ''
    event_type: str = ''


@dataclass(frozen=True)
class InfoSuffixEvaluation:
    """Request-only computation; requires canonical finalization before use.

    These outcomes cannot be persisted or reused as continuing authority. An
    ignored outcome means _apply evaluated the captured event without applying
    a state change; it does not classify a reaction breadcrumb's authenticity.
    """
    snapshot: ChatSnapshot
    outcomes: tuple[InfoEventOutcome, ...]


def fold(chat_id: str, envelopes: list[dict], directory: Resolver) -> ChatSnapshot:
    """Deterministic replay of a chat's info events into its snapshot."""
    infos = sorted(
        (e for e in envelopes if e.get("kind") == "info" and isinstance(e.get("event"), dict)),
        key=lambda e: (e.get("ns", 0), e.get("from", ""), e.get("id", "")),
    )
    snap = ChatSnapshot(id=chat_id, kind=ChatKind.GROUP, name="")
    snap.members = {}
    for env in infos:
        if _apply(snap, env, directory):
            snap.materialized_ns = max(
                snap.materialized_ns, int(env.get("ns", 0)),
            )
    return snap


def advance(
    materialized: ChatSnapshot, envelopes: list[dict], directory: Resolver,
) -> ChatSnapshot:
    """Apply a newer authenticated event suffix to a trusted materialization.

    Unlike ``fold``, this does not assume the local cache contains genesis or
    every earlier writer log. ``materialized_ns`` is the boundary: only newer
    records are eligible, so an in-progress multi-log sync cannot reconstruct
    authority from a partial history and accidentally widen membership.
    """
    return _advance_suffix(materialized, envelopes, directory)[0]


def advance_with_outcomes(materialized: ChatSnapshot, envelopes: list[dict],
                          directory: Resolver, requested: tuple[InfoEventIdentity, ...]
                          ) -> InfoSuffixEvaluation:
    """Evaluate only a bounded captured suffix with exact requested outcomes.

    Caller must establish the predecessor's canonical provenance, complete
    suffix capture, and fresh final validation of all raw/source/authority
    dependencies. This pure computation proves none of those itself. Targets
    at/below materialized_ns or absent from the supplied suffix stay explicitly
    outside_proven_suffix, even if their signatures or current membership match.
    Genesis GID binding proves event content, not envelope sender/id/ns.
    Legacy integer IDs normalize to canonical text before composite ordering.
    Count caps here do not bound raw bytes, nesting, directory/account work or
    cryptography. The caller's capture and operation ledger must enforce those
    limits before and during this computation.
    """
    if (type(materialized) is not ChatSnapshot or type(envelopes) is not list
            or len(envelopes) > 128 or any(type(e) is not dict for e in envelopes)
            or type(requested) is not tuple or len(requested) > 32
            or any(type(i) is not InfoEventIdentity for i in requested)
            or len(set(requested)) != len(requested)):
        raise ValueError('invalid bounded info suffix evaluation')
    snapshot, outcomes = _advance_suffix(materialized, envelopes, directory, requested)
    return InfoSuffixEvaluation(snapshot, tuple(
        outcomes.get(identity, InfoEventOutcome(identity, 'outside_proven_suffix', 'unproven'))
        for identity in requested))


def _advance_suffix(materialized, envelopes, directory, requested=None):
    snap = ChatSnapshot.from_dict(materialized.to_dict())
    wanted, outcomes = set(requested or ()), {}
    infos = list(
        (
            e for e in envelopes
            if e.get("kind") == "info"
            and isinstance(e.get("event"), dict)
            and int(e.get("ns", 0)) > materialized.materialized_ns
        ),
    )
    if requested is None:
        infos.sort(key=lambda e: (e.get('ns', 0), e.get('from', ''), e.get('id', '')))
    else:
        # Normalize legacy integer IDs exactly as local admission does, before
        # ordering and collision checks. Untargeted rows still affect authority.
        captured, seen = [], set()
        for env in infos:
            ident = env.get('id')
            if type(ident) is int:
                ident = str(ident)
            identity = InfoEventIdentity(env.get('ns', 0), env.get('from', ''), ident)
            if identity in seen:
                raise ValueError('duplicate captured info identity')
            seen.add(identity)
            captured.append((identity, env))
        captured.sort(key=lambda item: (item[0].ns, item[0].sender, item[0].id))
        infos = [env for _identity, env in captured]
    for env in infos:
        identity = None
        if requested is not None:
            # Admission normalizes legacy integer IDs to text. Do not coerce
            # arbitrary objects or booleans into projected identities.
            ident = env.get('id')
            if type(ident) is int:
                ident = str(ident)
            identity = InfoEventIdentity(env.get('ns', 0), env.get('from', ''), ident)
        applied = _apply(snap, env, directory)
        if applied:
            snap.materialized_ns = max(
                snap.materialized_ns, int(env.get("ns", 0)),
            )
        if identity in wanted:
            event = env['event']
            etype = event.get('type', '')
            if not _bounded_utf8(etype, 256):
                # Unknown/malformed types are ignored by _apply. Preserve that
                # semantic without exposing an unbounded value or blocking the
                # rest of this captured suffix's outcomes.
                etype = ''
            genesis = etype == EV_CREATED
            actor = event.get('creator', '') if genesis else env.get('from', '')
            if not _bounded_utf8(actor, 4096):
                actor = ''  # No bounded actor attribution; never truncate one.
            outcomes[identity] = InfoEventOutcome(identity,
                'applied' if applied else 'ignored',
                ('gid_content' if genesis else 'signed_author') if applied else 'unproven',
                actor if applied else '', etype)
    return snap, outcomes


def _bounded_utf8(value, maximum: int, *, nonempty: bool = False) -> bool:
    """Validate bounded wire text without leaking surrogate encoding errors."""
    if type(value) is not str or '\x00' in value or nonempty and not value:
        return False
    try:
        return len(value.encode('utf-8')) <= maximum
    except UnicodeEncodeError:
        return False


def _authentic(chat_id: str, env: dict, etype: str, author: str, d: Resolver) -> bool:
    """The authenticity gate (R13.5; tightened R16.5), run BEFORE any event
    takes effect.

    - genesis (`created`): the chat id MUST be gid-bound and the event must
      re-hash to that gid — no forged/backdated alternative can match, and a
      non-gid id folds to nothing (the migrated era ended with R16.5's purge).
    - every other event: it MUST carry the author's valid signature against
      their published key. An author without published keys cannot mutate —
      fail closed (identity keys are minted at signup/login/agent adoption,
      so every live writer has them)."""
    if etype == EV_CREATED:
        gid = _id_gid(chat_id)
        if gid is None:
            return False  # v2 accepts only genesis-bound chat ids
        return genesis_gid(env.get("event") or {}) == gid
    pub = d.sign_pub(author)
    sig = env.get("sig") or ""
    return bool(pub) and bool(sig) and crypto.verify(
        pub, sig, signing_bytes(chat_id, env))


def is_legacy_chat_id(chat_id: str) -> bool:
    """True for a migrated v1 chat id (no genesis binding). Since the R16.5
    purge nothing legacy remains on the mesh and the fold refuses non-gid
    genesis outright — this predicate stays for TOOLING only (the exporter's
    ``--legacy-only`` inventory selector)."""
    return _id_gid(chat_id) is None


def _id_gid(chat_id: str) -> str | None:
    """The gid a v2 chat id commits to, or None for a legacy id. v2 ids end in
    ``-g<16 hex>`` — the literal ``g`` marker means a migrated v1 id (which
    never used this scheme) is never mistaken for gid-bound, so its genesis
    is accepted as legacy rather than spuriously rejected."""
    tail = chat_id.rsplit("-g", 1)[-1] if "-g" in chat_id else ""
    if len(tail) == GID_LEN and all(c in "0123456789abcdef" for c in tail):
        return tail
    return None


def _tenure_open(snap: ChatSnapshot, name: str, ns: int) -> None:
    spans = snap.tenure.setdefault(name, [])
    if spans and spans[-1][1] == 0:
        return  # already inside an open interval
    spans.append([ns, 0])


def _tenure_close(snap: ChatSnapshot, name: str, ns: int) -> None:
    spans = snap.tenure.get(name)
    if spans and spans[-1][1] == 0:
        spans[-1][1] = ns


def _apply(snap: ChatSnapshot, env: dict, d: Resolver) -> bool:
    ev = env["event"]
    etype = ev.get("type")
    author = env.get("from", "")
    ns = int(env.get("ns", 0))

    if snap.deleted:
        return False  # terminal: nothing folds after deletion

    if not _authentic(snap.id, env, etype, author, d):
        return False  # forged / unsigned / mis-signed: never counts

    if etype == EV_CREATED:
        if snap.members:
            return False  # first created wins; later ones are noise
        snap.kind = ChatSnapshot.from_dict({"kind": ev.get("kind", "group")}).kind
        snap.name = str(ev.get("name") or "")
        snap.description = str(ev.get("description") or "")
        snap.auto_dm = bool(ev.get("auto_dm"))
        snap.permissions = ChatPermissions.from_dict(ev.get("permissions"))
        for name, role in (ev.get("members") or {}).items():
            wants_admin = role == Role.ADMIN.value
            is_agent = d.kind(name) is UserKind.AGENT
            snap.members[name] = Member(
                role=Role.ADMIN if (wants_admin and not is_agent) else Role.MEMBER,
                joined_ns=ns,
            )
            _tenure_open(snap, name, ns)
        _heal(snap, d, ns)
        return True

    if not snap.members:
        return False  # nothing exists before genesis

    fixed_membership = snap.kind in (ChatKind.DM, ChatKind.SELF)

    if etype == EV_MEMBER_ADDED:
        who = ev.get("who", "")
        if fixed_membership or not who or who in snap.members:
            return False
        author_owner = (
            d.owner_of(author) if d.kind(author) is UserKind.AGENT else None
        )
        if not authz.can_add_members(snap, author, agent_owner=author_owner):
            return False
        # pull-ins into a PREEXISTING group join as plain members — genesis
        # is the only moment humans get admin automatically (Aryan 2026-07-12)
        snap.members[who] = Member(role=Role.MEMBER, joined_ns=ns)
        _tenure_open(snap, who, ns)
        return True

    elif etype == EV_MEMBER_REMOVED:
        who = ev.get("who", "")
        if fixed_membership or who == author or who not in snap.members:
            return False
        if not authz.can_remove_member(
            snap, author,
            is_agent=d.kind(author) is UserKind.AGENT,
            owns_target=(d.kind(who) is UserKind.AGENT and d.owner_of(who) == author),
        ):
            return False
        del snap.members[who]
        _tenure_close(snap, who, ns)
        _heal(snap, d, ns)
        return True

    elif etype == EV_MEMBER_LEFT:
        if fixed_membership or author not in snap.members:
            return False
        del snap.members[author]
        _tenure_close(snap, author, ns)
        _heal(snap, d, ns)
        return True

    elif etype == EV_ADMIN_GRANTED:
        who = ev.get("who", "")
        if (fixed_membership
                or not authz.can_grant_admin(snap, author)
                or who not in snap.members
                or d.kind(who) is UserKind.AGENT):
            return False
        snap.members[who].role = Role.ADMIN
        return True

    elif etype == EV_ADMIN_REVOKED:
        who = ev.get("who", "")
        if (fixed_membership
                or not authz.can_grant_admin(snap, author)
                or who not in snap.members):
            return False
        snap.members[who].role = Role.MEMBER
        _heal(snap, d, ns)
        return True

    elif etype in (EV_RENAMED, EV_DESCRIPTION, EV_AVATAR):
        if not authz.can_edit_settings(snap, author):
            return False
        if etype == EV_RENAMED:
            snap.name = str(ev.get("name") or snap.name)
        elif etype == EV_DESCRIPTION:
            snap.description = str(ev.get("text") or "")
        else:
            snap.avatar = str(ev.get("sha") or "")  # "" clears the photo
        return True

    elif etype == EV_DELETED:
        # groups only, admins only — a DM/self chat is cleared, never deleted
        if snap.kind is not ChatKind.GROUP or not authz.is_admin(snap, author):
            return False
        snap.deleted = True
        for name in list(snap.members):
            _tenure_close(snap, name, ns)
        snap.members = {}  # nobody is a member of a dead chat (reads all stop)
        return True

    elif etype == EV_PERMISSIONS:
        if fixed_membership or not authz.can_change_permissions(snap, author):
            return False
        merged = {**snap.permissions.__dict__, **(ev.get("permissions") or {})}
        snap.permissions = ChatPermissions.from_dict(
            {k: getattr(v, "value", v) for k, v in merged.items()}
        )
        return True

    # unknown event types: ignore (a newer peer may emit ones we don't know)
    return False


def _heal(snap: ChatSnapshot, d: Resolver, ns: int = 0) -> None:
    """Post-change invariants: cascade ownerless agents out, then make sure a
    group with human members never stays admin-less. ``ns`` stamps the tenure
    close for any agent cascaded out here (R25), so a cascaded-out agent can't
    inject old-epoch messages after its owner leaves either."""
    if snap.kind is not ChatKind.GROUP:
        return
    # free-chatting invariant: every agent needs its responsible member present
    changed = True
    while changed:
        changed = False
        for name in list(snap.members):
            if d.kind(name) is not UserKind.AGENT:
                continue
            owner = d.owner_of(name)
            if owner is not None and owner not in snap.members:
                del snap.members[name]
                _tenure_close(snap, name, ns)
                changed = True
    # auto-promote the longest-standing human if no admin remains
    if snap.members and not snap.admins():
        humans = [
            (m.joined_ns, name)
            for name, m in snap.members.items()
            if d.kind(name) is UserKind.HUMAN
        ]
        if humans:
            humans.sort()
            snap.members[humans[0][1]].role = Role.ADMIN
