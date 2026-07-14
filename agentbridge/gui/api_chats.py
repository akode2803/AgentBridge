"""Core chat endpoints: the sidebar state, the transcript, posting, reading.

Shapes follow v1 where sane (``/api/mesh/state`` and ``/api/mesh/chat`` are
the two payloads the whole frontend hangs off) with the v2 fields added
alongside: ``admins`` (multi-admin, D12), ``handle``, ``status``, receipts
with the Delivered tier, per-user ``archived``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..mesh.pins import key_fingerprint
from .context import GuiApp
from .routing import authed
from .serialize import chat_json, message_json, user_json

__all__ = ["GET", "POST"]


def _connection(app: GuiApp) -> dict:
    """Transport-aware status for the Connection panel (no-chat home +
    Settings → Connection). A folder root keeps the v1 checks — does the
    folder exist, is the sync client alive; a cloud root reports the warm
    mirror's health instead (there is no folder or OneDrive to check, and
    the folder checks read "✗ No — check OneDrive" on a healthy cloud mesh:
    wrong and alarming)."""
    tx = app.transport
    scheme = getattr(tx, "scheme", "folder")
    out = {"scheme": scheme, "root": str(app.root)}
    if scheme == "folder":
        from .desktop import sync_client_running

        out["shared_ok"] = isinstance(app.root, Path) and app.root.is_dir()
        out["sync_client"] = sync_client_running()
        return out
    out["host"] = str(getattr(tx, "host", "") or "")
    status = getattr(tx, "mirror_status", None)
    if callable(status):
        out["mirror"] = status()
    return out


def bridge_state(app: GuiApp, req) -> dict:
    """The v1 ``/api/state`` shape the shared frontend boots + polls on. v2 has
    no bridge/setup wizard, so it always reports configured; the fields the
    frontend actually reads are configured/v/caps/paused/connection
    (+ version)."""
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
        "connection": _connection(app),
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
        entry = user_json(acc, profile, presence or None)
        if app.encrypt:
            # R31: the trusted-key fingerprint + out-of-band verified state,
            # for the DM info Encryption card (compare over another channel)
            entry["key_fp"] = mesh.key_pins.fingerprint(
                name, acc.keys.sign_pub, acc.keys.agree_pub)
            entry["key_verified"] = mesh.key_pins.verified(name)
        users[name] = entry
    out["users"] = users
    chats = []
    for snap in mesh.chats_for():
        overview = mesh.chat_overview(snap.id)
        chats.append(chat_json(snap, overview=overview))
    out["chats"] = chats
    # R27: pin-mismatch alerts (an account's published keys changed) — the
    # sidebar shows a banner until the signed-in human acknowledges
    out["key_alerts"] = [
        {"name": a.get("name", ""), "seen_sign_pub": a.get("seen_sign_pub", ""),
         "first_seen": a.get("first_seen", ""),
         # both fingerprints, so the human can compare out-of-band (R31):
         # pinned = what this machine trusts, seen = what the doc now claims
         "pinned_fp": mesh.key_pins.fingerprint(a.get("name", "")),
         "seen_fp": key_fingerprint(
             a.get("name", ""), a.get("seen_sign_pub", ""),
             a.get("seen_agree_pub", ""))}
        for a in mesh.key_alerts()
    ]
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
    meta["archived"] = mine["archived"]  # per-user flag; the header menu flips on it
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
    # the read-cursor write is one cloud round-trip on a cloud root — keep it
    # OFF the response path (composer latency = this endpoint's latency). A
    # lost cursor write just re-shows an unread badge; posting stays durable
    # through the outbox either way.
    def _mark() -> None:
        try:
            mesh.mark_read(chat_id)
        except Exception:  # noqa: BLE001 — cursor advance is best-effort
            pass

    threading.Thread(target=_mark, daemon=True, name="ab-mark-read").start()
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


@authed
def key_alert_ack(app: GuiApp, req, mesh) -> dict:
    """Acknowledge a key-change alert (R27). The pin stays in place — the
    machine keeps trusting the keys it knew; this only clears the banner."""
    name = (req.data.get("name") or "").strip().lower()
    if not name:
        return {"error": "name required"}
    mesh.ack_key_alert(name, req.data.get("seen_sign_pub") or "")
    return {"ok": True}


@authed
def key_verify(app: GuiApp, req, mesh) -> dict:
    """Mark an account's pinned keys as verified out-of-band (R31): the
    signed-in human compared fingerprints over another channel. Machine-local,
    like the pin — it never touches the directory."""
    name = (req.data.get("name") or "").strip().lower()
    if not name:
        return {"error": "name required"}
    mesh.mark_key_verified(name)
    return {"ok": True, **mesh.key_fingerprint(name)}


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
    "/api/mesh/key_alert_ack": key_alert_ack,
    "/api/mesh/key_verify": key_verify,
}
