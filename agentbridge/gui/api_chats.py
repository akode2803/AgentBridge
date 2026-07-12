"""Core chat endpoints: the sidebar state, the transcript, posting, reading.

Shapes follow v1 where sane (``/api/mesh/state`` and ``/api/mesh/chat`` are
the two payloads the whole frontend hangs off) with the v2 fields added
alongside: ``admins`` (multi-admin, D12), ``handle``, ``status``, receipts
with the Delivered tier, per-user ``archived``.
"""

from __future__ import annotations

from .context import GuiApp
from .routing import authed
from .serialize import chat_json, message_json, user_json

__all__ = ["GET", "POST"]


def bridge_state(app: GuiApp, req) -> dict:
    """The v1 ``/api/state`` shape the shared frontend boots + polls on. v2 has
    no bridge/setup wizard, so it always reports configured; the fields the
    frontend actually reads are configured/v/caps/paused (+ version)."""
    mesh = app.mesh
    ctl = (mesh.tx.get_doc("control.json") if mesh else
           app.directory0.tx.get_doc("control.json")) or {}
    return {
        "configured": True,
        "v": 2,
        "gui_version": app.app_version,
        "caps": {"sse": True, "receipts": "delivered", "admins": True},
        "paused": bool(ctl.get("paused")),
        "user": app.user,
    }


def state(app: GuiApp, req) -> dict:
    """The boot/sidebar payload. Logged out: enough for the login screen.
    Logged in: the privacy-filtered directory + my chats with unread info."""
    out: dict = {
        "available": True,
        "v": 2,
        "user": app.user,
        "gui_version": app.app_version,
        "encrypted": app.encrypt,
        "caps": {"sse": True, "receipts": "delivered", "admins": True},
        "max_upload_bytes": None,
    }
    mesh = app.mesh
    # the any-human stand-down switch (the R15 harness reads the same doc)
    ctl = app.directory0.tx.get_doc("control.json") if mesh is None else \
        mesh.tx.get_doc("control.json")
    out["paused"] = bool((ctl or {}).get("paused"))
    if mesh is None:
        # pre-auth: names only — profile fields need a viewer to filter for
        out["users"] = {
            n: {"name": n, "username": n, "kind": acc.kind.value,
                "display": acc.display or n, "active": acc.active}
            for n in app.directory0.names()
            if (acc := app.directory0.get(n)) is not None
        }
        return out
    users: dict = {}
    for name in mesh.directory.names():
        acc = mesh.directory.get(name)
        if acc is None:
            continue
        profile = mesh.visible_profile(name)
        presence = {
            k: v for k, v in mesh.visible_presence(name).items() if v is not None
        }
        users[name] = user_json(acc, profile, presence or None)
    out["users"] = users
    chats = []
    for snap in mesh.chats_for():
        overview = mesh.chat_overview(snap.id)
        chats.append(chat_json(snap, overview=overview))
    out["chats"] = chats
    return out


@authed
def chat(app: GuiApp, req, mesh) -> dict:
    """The transcript: messages (choke-point filtered), receipts on my own
    messages, active pins, my starred ids + read cursor."""
    chat_id = req.params.get("id", "")
    tail = req.int_param("tail", 200, 1, 1000)
    snap = mesh.snapshot(chat_id)
    msgs = mesh.messages_for(chat_id)  # raises NotAMember for outsiders
    me = mesh.user
    receipts = mesh.receipts_for(chat_id)
    payload = []
    for m in msgs[-tail:]:
        d = message_json(m, me)
        r = receipts.get(m.id)
        if r:
            d["receipt"] = r
        payload.append(d)
    mine = mesh.my_state(chat_id)
    meta = chat_json(snap, full=True)
    meta["created"] = _created_iso(msgs)
    meta["created_by"] = _created_by(msgs)
    meta["pins"] = _pins_list(mesh.pins(chat_id), msgs)
    return {
        "meta": meta,
        "messages": payload,
        "me": me,
        "starred": mine["starred"],
        "read_ns": mine["read_ns"],
        "total": len(msgs),
    }


def _pins_list(pins: dict, msgs) -> list[dict]:
    """The frontend banner wants an ARRAY of {id, until, body}, latest message
    first. Resolve bodies from the already-filtered read model (a redacted
    message reads as its tombstone, never its old body)."""
    by_id = {m.id: m for m in msgs}
    out = []
    for msg_id, doc in pins.items():
        m = by_id.get(msg_id)
        if m is None or m.deleted:
            continue  # pinned message gone/redacted: drop it from the banner
        out.append({"id": msg_id, "until": doc.get("until_ns", 0),
                    "body": m.body, "ns": m.ns})
    out.sort(key=lambda p: p["ns"], reverse=True)
    return out


def _created_iso(msgs) -> str:
    for m in msgs:  # the genesis info event is first in ns order
        if m.event and m.event.get("type") == "created":
            return m.ts
    return ""


def _created_by(msgs) -> str:
    for m in msgs:
        if m.event and m.event.get("type") == "created":
            return m.from_
    return ""


@authed
def post(app: GuiApp, req, mesh) -> dict:
    data = req.data
    chat_id = data.get("chat_id") or ""
    files = None
    if data.get("attachments"):
        from .api_files import seal_attachments

        files = seal_attachments(app, mesh, chat_id, data["attachments"])
        if not files and not (data.get("body") or "").strip():
            return {"error": "attachments were not found — upload them again"}
    env = mesh.post(
        chat_id,
        data.get("body") or "",
        reply_to=data.get("reply_to"),
        files=files,
    )
    mesh.mark_read(chat_id)
    return {"ok": True, "id": env.id, "ns": env.ns}


@authed
def read(app: GuiApp, req, mesh) -> dict:
    mesh.mark_read(req.data.get("chat_id") or "")
    return {"ok": True}


@authed
def create_chat(app: GuiApp, req, mesh) -> dict:
    data = req.data
    members = [
        r for m in (data.get("members") or [])
        if (r := mesh.directory.resolve((m or "").strip().lower()))
    ]
    snap = mesh.create_chat((data.get("name") or "").strip(), members)
    return {"ok": True, "chat": chat_json(snap, full=True)}


@authed
def create_dm(app: GuiApp, req, mesh) -> dict:
    ref = (req.data.get("username") or req.data.get("user") or "").strip().lower()
    other = mesh.directory.resolve(ref)
    if other is None:
        return {"error": f"unknown user @{ref}"}
    snap = mesh.create_dm(other)
    return {"ok": True, "chat": chat_json(snap, full=True)}


@authed
def create_self(app: GuiApp, req, mesh) -> dict:
    snap = mesh.create_self_chat()
    return {"ok": True, "chat": chat_json(snap, full=True)}


GET = {
    "/api/state": bridge_state,
    "/api/mesh/state": state,
    "/api/mesh/chat": chat,
}
POST = {
    "/api/mesh/post": post,
    "/api/mesh/read": read,
    "/api/mesh/create_chat": create_chat,
    "/api/mesh/create_dm": create_dm,
    "/api/mesh/create_self": create_self,
}
