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
from typing import Any, Callable

from ..core.models import BodyRecord, Envelope, Message, MsgKind
from .sealer import Sealer

__all__ = ["build_messages", "unread_info", "parse_tags", "member_at"]

_TAG_RE = re.compile(r"@([a-z0-9_][a-z0-9_.-]*)", re.IGNORECASE)


def member_at(spans: list[list[int]] | None, ns: int) -> bool:
    """Was a user a member at ``ns``, given their tenure intervals (R25)?
    An open interval has leave == 0. Absent tenure → True (fail open: legacy
    meta carries none, so the drop only fires on positive evidence)."""
    if not spans:
        return True
    return any(j <= ns and (e == 0 or ns < e) for j, e in spans)


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
    tenure: dict[str, list[list[int]]] | None = None,
    verify_redaction: Callable[[str, dict, str], bool] | None = None,
    owner_of: Callable[[str], str | None] | None = None,
) -> list[Message]:
    edits = edits or {}
    redactions = redactions or {}
    reactions = reactions or {}
    state = state or {}
    tenure = tenure or {}

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
    # delete-for-me of the whole chat (WhatsApp "Delete chat"): the flag holds
    # the deletion-moment ns; everything at or before it is invisible to this
    # viewer, with NO starred exemption (the chat "starts over"). A legacy
    # boolean flag carries no cut. Undo drops the flag and everything returns.
    del_flag = state.get("deleted")
    del_ns = int(del_flag) if isinstance(del_flag, int) and not isinstance(del_flag, bool) else 0

    out: list[Message] = []
    honored: set[str] = set()   # redactions that verified — reply-quotes follow
    for rec in ordered:
        env = Envelope.from_dict(rec)
        if env.kind is MsgKind.INFO and (env.event or {}).get("type") == "reaction":
            # V50 breadcrumbs are notification fuel (sync bus → notifier),
            # never viewer content — the reaction OVERLAY is what renders
            continue
        if (
            history_from_ns
            and env.kind is MsgKind.MESSAGE
            and env.ns < history_from_ns
        ):
            continue  # history-on-join: pre-join messages stay invisible
        # R25: drop a MESSAGE whose sender wasn't a member at its ns — closes
        # the removed-member injection (a departed member who kept the old
        # epoch key can still SEAL+SIGN a fresh old-epoch envelope that current
        # members can decrypt; the fold's tenure says they'd already left).
        if (
            env.kind is MsgKind.MESSAGE
            and env.from_ in tenure
            and not member_at(tenure.get(env.from_), env.ns)
        ):
            continue
        msg = Message(
            id=env.id, chat_id=chat_id, from_=env.from_, ns=env.ns, ts=env.ts,
            kind=env.kind, event=env.event,
        )

        if env.kind is MsgKind.MESSAGE:
            body = sealer.unseal(chat_id, env)
            if body is None:
                # R66: won't open for this reader RIGHT NOW — usually a fresh
                # key epoch (new chat / rotation) the read mirror hasn't
                # pulled yet; it heals on the next refresh. Flag it so
                # consumers can tell "not yet readable" from "empty".
                msg.undecrypted = True
                body = BodyRecord()
            msg.body, msg.tags = body.body, body.tags
            msg.reply_to, msg.files, msg.fwd = body.reply_to, body.files, body.fwd

            edit = edits.get(env.id)
            # enforced on read too: the author, or — for an agent's message —
            # the author's responsible member (R44). The edit body is SEALED
            # AS its editor (AAD + signature bind the sealer), so unsealing
            # with the claimed `by` is what authenticates the claim: a doc
            # naming an actor who didn't seal it simply refuses to open.
            edit_by = (edit or {}).get("by") or ""
            if edit and (edit_by == env.from_ or (
                    owner_of is not None and edit_by
                    and owner_of(env.from_) == edit_by)):
                eb = sealer.unseal(
                    chat_id,
                    Envelope.from_dict({**edit, "id": env.id, "from": edit_by}),
                )
                if eb is not None:
                    msg.body, msg.tags = eb.body, eb.tags
                    msg.edited = {"at": edit.get("at", ""), "ns": int(edit.get("ns", 0))}

            # redaction WINS over edit — but only an AUTHENTIC one (R25). Under
            # E2EE the caller passes a verifier (valid sig from the ORIGINAL
            # sender); a forged/unsigned redaction dropped on the shared folder
            # is ignored and the message stays visible. verify_redaction is None
            # only for plaintext/dev meshes, where there's no crypto boundary.
            red = redactions.get(env.id)
            if red is not None and (
                verify_redaction is None
                or verify_redaction(env.id, red, env.from_)
            ):
                honored.add(env.id)
                msg.deleted = True
                msg.body, msg.tags, msg.files = "", [], []
                msg.reply_to = msg.fwd = msg.edited = None

            msg.reactions = reactions.get(env.id, {})

        # viewer-only drops
        if env.id in hidden:
            continue
        if cut_ns and env.ns <= cut_ns and not (keep_starred and env.id in starred):
            continue
        if del_ns and env.ns <= del_ns:
            continue

        out.append(msg)

    # blank reply-quotes that point at redacted parents (only HONORED ones —
    # a forged redaction that was ignored must not blank a quote either, R25)
    for msg in out:
        if msg.reply_to and msg.reply_to.get("id") in honored:
            blank = {"id": msg.reply_to.get("id"), "deleted": True}
            if msg.reply_to.get("quote") is False:  # unthreaded stays unthreaded
                blank["quote"] = False
            msg.reply_to = blank

    return out


def unread_info(msgs: list[Message], viewer: str, state: dict[str, Any]) -> dict[str, Any]:
    """Unread derivation for one viewer. Includes v2's edit-marks-unread: an
    edit landing AFTER my read cursor makes an already-read message count as
    unread again — derived locally, no cross-user write exists. ``mention``
    is true when an unread message TAGS the viewer (@name / @all) or REPLIES
    to one of the viewer's messages — the sidebar's WhatsApp-style @ badge
    (V115; the client applies it to groups only)."""
    read_ns = int(state.get("read_ns", 0))
    unread = 0
    first_unread_ns = 0
    mention = False
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
            if not mention and (
                viewer in (m.tags or []) or "all" in (m.tags or [])
                or (m.reply_to or {}).get("from") == viewer
            ):
                mention = True
    return {
        "unread": unread,
        "first_unread_ns": first_unread_ns,
        "mention": mention,
        "forced_unread": bool(state.get("forced_unread")),
    }
