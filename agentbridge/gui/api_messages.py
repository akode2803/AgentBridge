"""Message-level operations: star/pin/edit/delete/clear/react/forward,
chat flags (archive / pin-chat / hide-chat / mark-unread), message info,
typing heartbeats. Bounded collections and page auxiliaries own read feeds.

Every handler is a thin shim over the facade — the membership gate lives in
the mesh services (``_require_member``), never re-implemented here.
"""

from __future__ import annotations

from ..core.timekit import utcnow_iso
from .context import session_read_binding
from .routing import authed, authed_read_token

__all__ = ["GET", "POST"]


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
    """scope 'me' = private hide (reversible); 'everyone' = redact (the
    sender, or their responsible member for an agent's message — enforced
    in the mesh, R44)."""
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
def restore_message(app, req, mesh) -> dict:
    """Undo a delete-for-everyone (R44): oversight for the responsible
    member — a wrongly deleted agent message comes back for every member.
    The mesh enforces who may (author or their owner) and signs the void."""
    mesh.unredact(req.data.get("chat_id") or "",
                  str(req.data.get("msg_id") or ""))
    return {"ok": True}


@authed
def clear_chat(app, req, mesh) -> dict:
    from .api_runtime import contributor_rows

    chat_id = req.data.get("chat_id") or ""
    runtime_ids = tuple(
        row["id"] for row in contributor_rows(mesh, chat_id, limit=None)
    )
    mesh.clear_chat(
        chat_id, keep_starred=bool(req.data.get("keep_starred")),
        runtime_ids=runtime_ids,
    )
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
    targets = list(dict.fromkeys(str(t or "") for t in (d.get("targets") or [])))
    for target in targets:
        mesh.require_send(target)  # all targets before any source blob read
    sent = 0
    for target in targets:
        prepared = []
        try:
            for f in original.files or []:
                raw = _open_file_blob(app, mesh, src_chat, f)
                if raw is None:
                    continue
                prepared.append(mesh.prepare_attachment(
                    target, f.get("name", "file"), raw))
            mesh.post(target, original.body, attachments=prepared, fwd=fwd)
        except Exception:
            mesh.cancel_attachments(prepared)
            raise
        sent += 1
    return {"ok": True, "forwarded": sent}


@authed_read_token
def message_info(app, req, mesh, token) -> dict:
    chat_id = req.params.get("id", "")
    msg_id = req.params.get("msg", "")
    out = mesh.message_info(chat_id, msg_id)
    out["session_binding"] = session_read_binding(token)
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
    nothing shared changes. Distinct from the admin-only delete_chat. The
    flag stores the deletion ns so the transcript empties for me and the
    chat reappears (new messages only) when someone posts again."""
    chat_id = req.data.get("chat_id") or ""
    if req.data.get("undo"):
        mesh.set_chat_flag(chat_id, "deleted", False)
        return {"ok": True, "deleted": False}
    mesh.delete_chat_for_me(chat_id)
    return {"ok": True, "deleted": True}


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
    return mesh.open_attachment(chat_id, blob_id)


GET = {
    "/api/mesh/message_info": message_info,
}
POST = {
    "/api/mesh/star": star,
    "/api/mesh/pin": pin,
    "/api/mesh/unpin": unpin,
    "/api/mesh/edit_message": edit_message,
    "/api/mesh/delete_messages": delete_messages,
    "/api/mesh/undelete_messages": undelete_messages,
    "/api/mesh/restore_message": restore_message,
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
