"""Message-level operations: star/pin/edit/delete/clear/react/forward,
chat flags (archive / pin-chat / hide-chat / mark-unread), message info,
typing heartbeats and the livefeed.

Every handler is a thin shim over the facade — the membership gate lives in
the mesh services (``_require_member``), never re-implemented here.
"""

from __future__ import annotations

import re

from ..core.timekit import new_id, utcnow_iso
from .routing import authed
from .serialize import chat_json, message_json

__all__ = ["GET", "POST"]

_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")


# ------------------------------------------------------------ standard ops
@authed
def star(app, req, mesh) -> dict:
    d = req.data
    ids = [str(d.get("msg_id") or "")] if d.get("msg_id") else [
        str(i) for i in (d.get("ids") or [])
    ]
    if d.get("starred", True):
        mesh.star(d.get("chat_id") or "", ids)
    else:
        mesh.unstar(d.get("chat_id") or "", ids)
    return {"ok": True, "starred": bool(d.get("starred", True))}


@authed
def starred(app, req, mesh) -> dict:
    """One chat's stars, or (no id) every chat's — the Starred page."""
    chat_id = req.params.get("id", "")
    out = []
    ids = [chat_id] if chat_id else [s.id for s in mesh.chats_for()]
    for cid in ids:
        for m in mesh.starred(cid):
            out.append({**message_json(m, mesh.user), "chat_id": cid})
    out.sort(key=lambda m: m["ns"])
    return {"starred": out}


@authed
def pin(app, req, mesh) -> dict:
    d = req.data
    hours = d.get("hours")
    mesh.pin(d.get("chat_id") or "", str(d.get("msg_id") or ""),
             hours=float(hours) if hours else None)
    return {"ok": True}


@authed
def unpin(app, req, mesh) -> dict:
    mesh.unpin(req.data.get("chat_id") or "", str(req.data.get("msg_id") or ""))
    return {"ok": True}


@authed
def edit_message(app, req, mesh) -> dict:
    d = req.data
    mesh.edit(d.get("chat_id") or "", str(d.get("msg_id") or ""),
              d.get("body") or "")
    return {"ok": True}


@authed
def delete_messages(app, req, mesh) -> dict:
    """scope 'me' = private hide (reversible); 'everyone' = redact
    (sender-only, enforced in the mesh, irreversible)."""
    d = req.data
    chat_id = d.get("chat_id") or ""
    ids = [str(i) for i in (d.get("ids") or [])]
    if d.get("scope") == "everyone":
        mesh.redact(chat_id, ids)
        return {"ok": True, "scope": "everyone", "deleted": len(ids)}
    mesh.hide(chat_id, ids)
    return {"ok": True, "scope": "me", "hidden": len(ids)}


@authed
def undelete_messages(app, req, mesh) -> dict:
    mesh.unhide(req.data.get("chat_id") or "",
                [str(i) for i in (req.data.get("ids") or [])])
    return {"ok": True}


@authed
def clear_chat(app, req, mesh) -> dict:
    mesh.clear_chat(req.data.get("chat_id") or "",
                    keep_starred=bool(req.data.get("keep_starred")))
    return {"ok": True}


@authed
def react(app, req, mesh) -> dict:
    """One reaction per user per message (WhatsApp, D14); null removes."""
    d = req.data
    mesh.react(d.get("chat_id") or "", str(d.get("msg_id") or ""),
               d.get("emoji") or None)
    return {"ok": True}


@authed
def forward(app, req, mesh) -> dict:
    """Re-post a message into target chats with provenance. Attachments are
    re-sealed for each target (blob keys are chat-scoped)."""
    d = req.data
    src_chat = d.get("chat_id") or ""
    msg_id = str(d.get("msg_id") or "")
    original = next(
        (m for m in mesh.messages_for(src_chat) if m.id == msg_id), None
    )
    if original is None or original.deleted:
        return {"error": "That message is no longer available"}
    fwd = {"from": original.from_, "ts": original.ts}
    sent = 0
    for target in d.get("targets") or []:
        files = []
        for f in original.files or []:
            raw = _open_file_blob(app, mesh, src_chat, f)
            if raw is None:
                continue
            files.append(_put_file_blob(app, mesh, target, f.get("name", "file"), raw))
        mesh.post(target, original.body, files=files or None, fwd=fwd)
        sent += 1
    return {"ok": True, "forwarded": sent}


@authed
def message_info(app, req, mesh) -> dict:
    chat_id = req.params.get("id", "")
    msg_id = req.params.get("msg", "")
    out = mesh.message_info(chat_id, msg_id)
    # agent replies carry the task steps their harness recorded (R15) — the
    # membership gate already ran inside message_info
    doc = mesh.tx.get_doc(f"chats/{chat_id}/tasks/{msg_id}.json")
    if isinstance(doc, dict) and doc.get("tasks"):
        out["tasks"] = doc["tasks"]
    return out


# ------------------------------------------------------------- chat flags
@authed
def archive(app, req, mesh) -> dict:
    val = bool(req.data.get("archived", True))
    mesh.set_chat_flag(req.data.get("chat_id") or "", "archived", val)
    return {"ok": True, "archived": val}


@authed
def pin_chat(app, req, mesh) -> dict:
    val = bool(req.data.get("pinned", True))
    mesh.set_chat_flag(req.data.get("chat_id") or "", "pinned", val)
    return {"ok": True, "pinned": val}


@authed
def hide_chat(app, req, mesh) -> dict:
    """Delete-for-me of a whole chat (undo=true restores) — per-user flag,
    nothing shared changes. Distinct from the admin-only delete_chat."""
    val = not req.data.get("undo")
    mesh.set_chat_flag(req.data.get("chat_id") or "", "deleted", val)
    return {"ok": True, "deleted": val}


@authed
def mark_unread(app, req, mesh) -> dict:
    val = bool(req.data.get("unread", True))
    mesh.set_chat_flag(req.data.get("chat_id") or "", "forced_unread", val)
    return {"ok": True}


@authed
def mute(app, req, mesh) -> dict:
    """True = forever; hours = until then; false = unmute (R10 semantics)."""
    d = req.data
    chat_id = d.get("chat_id") or ""
    if d.get("hours"):
        from ..core.timekit import next_ns

        value = next_ns() + int(float(d["hours"]) * 3600 * 1e9)
    else:
        value = bool(d.get("muted", True))
    mesh.set_chat_flag(chat_id, "mute", value)
    return {"ok": True, "mute": value}


# ------------------------------------------------------- chat-info + feeds
@authed
def chat_info(app, req, mesh) -> dict:
    """The info pane: meta + media/links walk in one pass (v1 shape)."""
    chat_id = req.params.get("id", "")
    snap = mesh.snapshot(chat_id)
    msgs = mesh.messages_for(chat_id)  # gate + overlays
    files, links = [], []
    for m in msgs:
        if m.event is not None or m.deleted:
            continue
        for f in m.files or []:
            files.append({**f, "from": m.from_, "ts": m.ts, "msg_id": m.id})
        for url in _LINK_RE.findall(m.body or ""):
            links.append({"url": url, "from": m.from_, "ts": m.ts})
    return {
        "meta": {**chat_json(snap, full=True), "pins": mesh.pins(chat_id)},
        "files": files,
        "links": links,
        "count": len(msgs),
        "starred": mesh.my_state(chat_id)["starred"],
    }


@authed
def typing(app, req, mesh) -> dict:
    """Composer heartbeat — one doc per user (single writer), readers drop
    stale ones. Runtime lane: deliberately outside the message log."""
    mesh.tx.put_doc(f"status/typing_{mesh.user}.json", {
        "user": mesh.user,
        "chat_id": (req.data.get("chat_id") or "")[:80],
        "updated": utcnow_iso(),
    })
    return {"ok": True}


@authed
def livefeed(app, req, mesh) -> dict:
    """Agent runs (status/<agent>_run.json — the R15 harness writes these;
    shape kept from v1) + humans typing, for one chat or all mine."""
    chat_id = req.params.get("id", "")
    if chat_id and not mesh.snapshot(chat_id).is_member(mesh.user):
        return {"feeds": []}  # never leak who's typing where
    feeds = []
    for path in mesh.tx.list_docs("status"):
        doc = mesh.tx.get_doc(path)
        if not isinstance(doc, dict):
            continue
        leaf = path.rsplit("/", 1)[-1]
        age = _age_s(doc.get("updated", ""))
        if leaf.endswith("_run.json"):
            if doc.get("state") != "running":
                continue
            if chat_id and doc.get("chat_id") != chat_id:
                continue
            if age is not None and age > 7200:
                continue  # a run that died without a finish write
            feeds.append({**doc, "age_s": age})
        elif leaf.startswith("typing_"):
            if not doc.get("user") or doc["user"] == mesh.user:
                continue
            if chat_id and doc.get("chat_id") != chat_id:
                continue
            if age is None or age > 12:
                continue  # heartbeat every ~3s while typing; stale = stopped
            feeds.append({"agent": doc["user"], "human": True,
                          "typing": True, "age_s": age})
    return {"feeds": feeds}


def _age_s(updated: str) -> float | None:
    import calendar
    import time

    try:
        return max(0.0, time.time() - calendar.timegm(
            time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------ blob helpers
# (shared with api_files; defined here to avoid a circular import)
def _open_file_blob(app, mesh, chat_id: str, f: dict) -> bytes | None:
    blob_id = f.get("id") or ""
    data = mesh.tx.get_blob(_file_path(chat_id, blob_id))
    if data is None:
        return None
    return mesh.sealer.open_blob(chat_id, blob_id, data)


def _put_file_blob(app, mesh, chat_id: str, name: str, raw: bytes) -> dict:
    import hashlib

    from .api_files import safe_name

    name = safe_name(name)
    blob_id = new_id("f") + _suffix(name)
    sealed = mesh.sealer.seal_blob(chat_id, blob_id, raw)
    mesh.tx.put_blob(_file_path(chat_id, blob_id), sealed)
    return {"id": blob_id, "name": name, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def _file_path(chat_id: str, blob_id: str) -> str:
    from ..mesh.paths import P

    return P.file(chat_id, blob_id)


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:][:12].lower() if dot > 0 else ""


GET = {
    "/api/mesh/starred": starred,
    "/api/mesh/message_info": message_info,
    "/api/mesh/chat_info": chat_info,
    "/api/mesh/livefeed": livefeed,
}
POST = {
    "/api/mesh/star": star,
    "/api/mesh/pin": pin,
    "/api/mesh/unpin": unpin,
    "/api/mesh/edit_message": edit_message,
    "/api/mesh/delete_messages": delete_messages,
    "/api/mesh/undelete_messages": undelete_messages,
    "/api/mesh/clear_chat": clear_chat,
    "/api/mesh/react": react,
    "/api/mesh/forward": forward,
    "/api/mesh/archive": archive,
    "/api/mesh/pin_chat": pin_chat,
    "/api/mesh/hide_chat": hide_chat,
    "/api/mesh/mark_unread": mark_unread,
    "/api/mesh/mute": mute,
    "/api/mesh/typing": typing,
}
