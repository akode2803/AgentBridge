"""The read model — folding raw envelopes + overlays into what a viewer may
see. Successor to v1's ``mesh.messages_for`` choke point: EVERY reader (GUI,
CLI, harness context builder) goes through ``build_messages``, so nobody ever
reads a deleted / hidden / pre-clear body.

Fold order (v1 rules, kept):
  dedup by id -> sort (ns, from) -> unseal -> apply edits -> apply redactions
  (redaction WINS over edit) -> blank reply-quotes to redacted parents ->
  viewer's hidden -> viewer's cleared (keep_starred spares their stars) ->
  attach reactions.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.models import BodyRecord, Envelope, Message, MsgKind
from .sealer import Sealer

__all__ = ["build_messages", "unread_info", "parse_tags"]

_TAG_RE = re.compile(r"@([a-z0-9_][a-z0-9_.-]*)", re.IGNORECASE)


def parse_tags(body: str) -> list[str]:
    """Plain ``@name`` mentions (lowercased, deduped, order kept). ``all`` is
    the reserved everyone-mention."""
    seen: dict[str, None] = {}
    for m in _TAG_RE.findall(body or ""):
        seen.setdefault(m.lower())
    return list(seen)


def build_messages(
    chat_id: str,
    viewer: str,
    envelopes: list[dict],
    sealer: Sealer,
    *,
    edits: dict[str, dict] | None = None,
    redactions: dict[str, dict] | None = None,
    reactions: dict[str, dict[str, list[str]]] | None = None,
    state: dict[str, Any] | None = None,
    history_from_ns: int = 0,
) -> list[Message]:
    edits = edits or {}
    redactions = redactions or {}
    reactions = reactions or {}
    state = state or {}

    # dedup by id (at-least-once transport), deterministic order
    by_id: dict[str, dict] = {}
    for rec in envelopes:
        rid = rec.get("id")
        if rid:
            by_id.setdefault(rid, rec)
    ordered = sorted(by_id.values(), key=lambda r: (r.get("ns", 0), r.get("from", "")))

    hidden = set(state.get("hidden", []))
    starred = set(state.get("starred", []))
    cleared = state.get("cleared") or {}
    cut_ns = int(cleared.get("ns", 0))
    keep_starred = bool(cleared.get("keep_starred"))

    out: list[Message] = []
    for rec in ordered:
        env = Envelope.from_dict(rec)
        if (
            history_from_ns
            and env.kind is MsgKind.MESSAGE
            and env.ns < history_from_ns
        ):
            continue  # history-on-join: pre-join messages stay invisible
        msg = Message(
            id=env.id, chat_id=chat_id, from_=env.from_, ns=env.ns, ts=env.ts,
            kind=env.kind, event=env.event,
        )

        if env.kind is MsgKind.MESSAGE:
            body = sealer.unseal(chat_id, env) or BodyRecord()
            msg.body, msg.tags = body.body, body.tags
            msg.reply_to, msg.files, msg.fwd = body.reply_to, body.files, body.fwd

            edit = edits.get(env.id)
            if edit and edit.get("by") == env.from_:  # author-only, enforced on read too
                eb = sealer.unseal(chat_id, Envelope.from_dict({**edit, "id": env.id}))
                if eb is not None:
                    msg.body, msg.tags = eb.body, eb.tags
                    msg.edited = {"at": edit.get("at", ""), "ns": int(edit.get("ns", 0))}

            if env.id in redactions:  # redaction WINS over edit
                msg.deleted = True
                msg.body, msg.tags, msg.files = "", [], []
                msg.reply_to = msg.fwd = msg.edited = None

            msg.reactions = reactions.get(env.id, {})

        # viewer-only drops
        if env.id in hidden:
            continue
        if cut_ns and env.ns <= cut_ns and not (keep_starred and env.id in starred):
            continue

        out.append(msg)

    # blank reply-quotes that point at redacted parents
    for msg in out:
        if msg.reply_to and msg.reply_to.get("id") in redactions:
            msg.reply_to = {"id": msg.reply_to.get("id"), "deleted": True}

    return out


def unread_info(msgs: list[Message], viewer: str, state: dict[str, Any]) -> dict[str, Any]:
    """Unread derivation for one viewer. Includes v2's edit-marks-unread: an
    edit landing AFTER my read cursor makes an already-read message count as
    unread again — derived locally, no cross-user write exists."""
    read_ns = int(state.get("read_ns", 0))
    unread = 0
    first_unread_ns = 0
    for m in msgs:
        if m.from_ == viewer or m.kind is not MsgKind.MESSAGE:
            continue
        is_new = m.ns > read_ns
        edited_after_read = bool(
            not is_new and m.edited and int(m.edited.get("ns", 0)) > read_ns
        )
        if is_new or edited_after_read:
            unread += 1
            if not first_unread_ns:
                first_unread_ns = m.ns
    return {
        "unread": unread,
        "first_unread_ns": first_unread_ns,
        "forced_unread": bool(state.get("forced_unread")),
    }
