"""GUI bootstrap, bounded sidebar state, posting and explicit read actions.

Selected transcripts are served by api_pages; signed-in sidebar rows are
served solely by canonical page summaries.
"""

from __future__ import annotations

import json
import os
import threading

from ..core.errors import ValidationError
from ..mesh.pins import key_fingerprint
from .context import GuiApp, SessionReadToken, session_read_binding
from .routing import authed
from .api_sidebar_pages import refresh_sidebar
from .serialize import chat_json

__all__ = ["GET", "POST"]


def _connection(app: GuiApp) -> dict:
    """Cloud connection health from the GUI's shared Supabase mirror."""
    tx = app.transport
    out = {"scheme": tx.scheme, "root": str(app.root),
           "host": str(getattr(tx, "host", "") or "")}
    status = getattr(tx, "mirror_status", None)
    if callable(status):
        out["mirror"] = status()
        out["state"] = out["mirror"].get("state", "loading")
    return out


def bridge_state(app: GuiApp, req) -> dict:
    """The v1 ``/api/state`` shape the shared frontend boots + polls on. v2 has
    no bridge/setup wizard, so it always reports configured; the fields the
    frontend actually reads are configured/v/caps/paused/connection
    (+ version)."""
    token = app.capture_session_read()
    if token is None:
        return {"error": "Session changed"}
    try:
        result = _bridge_state_captured(app, token)
    except Exception:
        if not app.validate_session_read(token):
            return {"error": "Session changed"}
        raise
    return result if app.validate_session_read(token) else {"error": "Session changed"}


def _bridge_state_captured(app: GuiApp, token: SessionReadToken) -> dict:
    """Build bootstrap state solely from one exact R187 observation."""
    lock = getattr(app, "lock", None)   # V111: the lock page keys off this
    binding = session_read_binding(token)
    return {
        "configured": True,
        "v": 2,
        "gui_version": app.app_version,
        "frontend_revision": app.frontend_revision,
        "instance_id": token.app_identity,
        "server_pid": os.getpid(),
        "caps": {"sse": True, "receipts": "delivered", "admins": True,
                 "sse_refresh_v1": bool(token.mesh is not None and token.mesh.tx.scheme == 'supabase'),
                 "session_binding_v1": True,
                 "chat_page_v1": True},
        "paused": False,  # compatibility field; mesh-global pause is retired
        "user": binding["viewer"],
        "session_binding": binding,
        "diagnostics": {"enabled": app.diagnostics.enabled},
        # V125: a blind session restore in flight — the frontend holds the
        # boot surface instead of flashing the sign-in page
        "restoring": bool(getattr(app, "restoring", False)),
        "connection": _connection(app),
        "app_lock": lock.status() if lock is not None
        else {"enabled": False, "locked": False, "autolock_min": 0},
    }


def state(app: GuiApp, req) -> dict:
    """The boot/sidebar payload. Logged out: enough for the login screen.
    Logged in: the privacy-filtered directory + my chats with unread info."""
    # V111: this endpoint is deliberately pre-auth (the login screen reads
    # it), so the authed gate doesn't cover it — refuse explicitly while
    # locked; it carries the whole directory + chat list
    lock = getattr(app, "lock", None)
    if lock is not None and lock.locked:
        return {"error": "App is locked", "locked": True}
    token = app.capture_session_read()
    if token is None:
        return {"error": "Sign in first"}
    try:
        result = _state_captured(app, req, token)
    except Exception:
        if not app.validate_session_read(token):
            return {"error": "Sign in first"}
        raise
    return result if app.validate_session_read(token) else {"error": "Sign in first"}


def _state_captured(app: GuiApp, req, token: SessionReadToken) -> dict:
    """Build state solely from one captured anonymous or viewer session."""
    mesh = token.mesh
    out: dict = {
        "available": True,
        "v": 2,
        "user": mesh.user if mesh is not None else None,
        "gui_version": app.app_version,
        "instance_id": token.app_identity,
        "server_pid": os.getpid(),
        "encrypted": app.encrypt,
        "caps": {"sse": True, "receipts": "delivered", "admins": True,
                 "sse_refresh_v1": bool(mesh is not None and mesh.tx.scheme == 'supabase'),
                 "session_binding_v1": True,
                 "chat_page_v1": True},
        "max_upload_bytes": None,
        "connection": _connection(app),
        "session_binding": session_read_binding(token),
        "diagnostics": {"enabled": app.diagnostics.enabled},
    }
    out["paused"] = False  # compatibility field; mesh-global pause is retired
    if mesh is None:
        # V125: signed-out-with-a-pending-restore is NOT signed-out
        out["restoring"] = bool(getattr(app, "restoring", False))
        # pre-auth: names only — profile fields need a viewer to filter for
        out["users"] = {
            n: {"name": n, "username": n, "kind": acc.kind.value,
                "display": acc.display or n, "active": acc.active}
            for n in app.directory0.names()
            if (acc := app.directory0.get(n)) is not None
        }
        return out
    from .api_sidebar_pages import MAX_RESPONSE_BYTES, capture_sidebar

    out.update(capture_sidebar(app, mesh, token))
    out['key_alerts'] = [
        {'name': a.get('name', ''), 'seen_sign_pub': a.get('seen_sign_pub', ''),
         'first_seen': a.get('first_seen', ''),
         'pinned_fp': mesh.key_pins.fingerprint(a.get('name', '')),
         'seen_fp': key_fingerprint(a.get('name', ''),
                                     a.get('seen_sign_pub', ''),
                                     a.get('seen_agree_pub', ''))}
        for a in mesh.key_alerts()
    ]
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        out.update(users={}, chats=[], key_alerts=[], chats_complete=False,
                   users_complete=False, user_status='response_byte_budget',
                   sidebar_status='response_byte_budget')
    return out


@authed
def post(app: GuiApp, req, mesh) -> dict:
    data = req.data
    client_ref = data.get("client_ref") or ""
    if (not isinstance(client_ref, str) or (client_ref and len(client_ref) != 32)
            or any(c not in "0123456789abcdef" for c in client_ref)):
        return {"error": "Invalid send reference"}
    chat_id = data.get("chat_id") or ""
    prepared = []
    staged = []
    if data.get("attachments"):
        from .api_files import prepare_attachments

        prepared, staged = prepare_attachments(
            app, mesh, chat_id, data["attachments"])
        if not prepared and not (data.get("body") or "").strip():
            return {"error": "attachments were not found — upload them again"}
    try:
        env = mesh.post(
            chat_id,
            data.get("body") or "",
            reply_to=data.get("reply_to"),
            attachments=prepared, client_ref=client_ref,
        )
    except Exception:
        mesh.cancel_attachments(prepared)
        raise
    for path in staged:
        path.unlink(missing_ok=True)  # durable outbox owns the sealed copy now
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
    chat_id = req.data.get("chat_id") or ""
    if "up_to_ns" in req.data:
        cut = req.data["up_to_ns"]
        if type(cut) is str:
            if (not cut or len(cut) > 19 or not cut.isascii() or not cut.isdecimal()
                    or (len(cut) > 1 and cut[0] == '0')):
                raise ValidationError("Invalid read cursor")
            cut = int(cut)
            valid = cut <= 2**63 - 1
        else:
            # JSON numbers above this bound have already lost nanosecond
            # precision in the browser; use the decimal string form instead.
            valid = type(cut) is int and 0 <= cut <= 2**53 - 1
        if not valid:
            raise ValidationError("Invalid read cursor")
        mesh.mark_read(chat_id, up_to_ns=cut)
    else:
        mesh.mark_read(chat_id)
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


@authed
def activity(app: GuiApp, req, mesh) -> dict:
    """Renew or release the foreground mirror lease from this local GUI."""
    active = bool(req.data.get("active"))
    setter = getattr(mesh.tx, "set_interactive", None)
    if callable(setter):
        setter(active)
    return {"ok": True, "active": active}


GET = {
    "/api/state": bridge_state,
    "/api/mesh/state": state,
}
POST = {
    "/api/mesh/sidebar_refresh": refresh_sidebar,
    "/api/mesh/post": post,
    "/api/mesh/read": read,
    "/api/mesh/create_chat": create_chat,
    "/api/mesh/create_dm": create_dm,
    "/api/mesh/create_self": create_self,
    "/api/mesh/key_alert_ack": key_alert_ack,
    "/api/mesh/key_verify": key_verify,
    "/api/mesh/activity": activity,
}
