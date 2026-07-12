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
    return {
        "meta": {**chat_json(snap, full=True), "pins": mesh.pins(chat_id)},
        "messages": payload,
        "me": me,
        "starred": mine["starred"],
        "read_ns": mine["read_ns"],
        "total": len(msgs),
    }


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
