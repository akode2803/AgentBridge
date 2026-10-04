"""Server-Sent Events off the R10 bus — the frontend's realtime signal.

Frames are deliberately MINIMAL (type + chat + ids): the client refetches
through the read model, so no body — encrypted or not — ever rides the
stream. Queue gaps request a fresh catch-up, never replayed permission data.

One exception (R42/Q26): frames that DESERVE a desktop notification carry a
``notify`` lane — chat name, sender, 120-char preview — decided by the R10
Notifier (membership, not-from-me, mute, read-state) and decrypted by this
identity's own sealer. The stream is the signed-in owner's authed session;
the client displays it (or not: window focus + its own prefs) but never has
to refetch just to ping.
"""

from __future__ import annotations

import json
import time
from typing import Iterator

from ..mesh import eventbus
from ..mesh.eventbus import Event, Subscription
from ..mesh.notify import Notifier
from .context import GuiApp

__all__ = ["frame", "stream"]


def frame(ev: Event, notifier: Notifier | None = None, latency=None) -> dict:
    out = {"type": ev.type, "chat_id": ev.chat_id, "ns": ev.ns,
           "server_ns": time.time_ns()}
    if ev.type == eventbus.MESSAGE:
        out["id"] = ev.data.get("id", "")
        out["from"] = ev.data.get("from", "")
    elif ev.type == eventbus.CHAT_UPDATE:
        out["event"] = (ev.data.get("event") or {}).get("type", "")
    elif ev.type == eventbus.ADDED_TO_CHAT:
        out["by"] = ev.data.get("by", "")
    elif ev.type == eventbus.READ_MODEL:
        from ..mesh.read_events import SCOPES
        scope = ev.data.get('scope')
        out['scope'] = scope if type(scope) is str and scope in SCOPES else 'global'
    out["trace_ref"] = str(out.get("id") or f"{ev.type}-{ev.ns}")
    if notifier is not None and ev.type in (
        eventbus.MESSAGE, eventbus.ADDED_TO_CHAT, eventbus.REACTION,
    ):
        try:
            note = notifier.consider(ev)
        except Exception:  # noqa: BLE001 — a notify hiccup never drops the frame
            note = None
        if note is not None:
            out["notify"] = {
                "kind": note.kind, "chat_name": note.chat_name,
                "chat_kind": note.chat_kind,
                "from": note.from_, "preview": note.preview, "ns": note.ns,
                "emoji": note.emoji,
            }
    from ..core.delivery_trace import reference
    diagnostic_ref = reference(out["trace_ref"])
    if diagnostic_ref is not None:
        out["diagnostic_ref"] = diagnostic_ref
    if latency is not None:
        latency.observe(
            "sse_frame", out["trace_ref"], lane="local")
    return out


def _reason(app, sub, token):
    # Same lock order as canonical GUI handout; no notifier work under gates.
    with app.lock._mx:
        if app.lock._expire_if_idle_locked():
            return 'locked'
        with app._lock:
            if (token is None or token.mesh is None or not app.validate_session_read(token)
                    or sub._bus is not token.mesh.bus):
                return 'session_changed'
    return None


def _control(reason):
    return ('data: ' + json.dumps({'type': 'control', 'reason': reason}) + '\n\n').encode()


def _health(app, mesh):
    observe = getattr(mesh.tx, 'mirror_status', None)
    try:
        status = observe() if callable(observe) else {}
        # Local cached health/config only; never a provider or broad page read.
        return (status.get('state'), status.get('realtime'),
                getattr(app.lock, 'enabled', False), getattr(app.lock, 'autolock_min', 0),
                getattr(app, 'frontend_revision', None))
    except Exception:
        return 'unavailable', None


def stream(app: GuiApp, sub: Subscription, ping_s: float, *, token=None) -> Iterator[bytes]:
    """Yield SSE frames until the session changes (logout) or the caller's
    write fails (client gone). Idle gaps carry content-free heartbeat frames
    so the browser can distinguish a quiet stream from a half-open one."""
    token = app.capture_session_read() if token is None else token
    reason = _reason(app, sub, token)
    if reason is not None:
        yield _control(reason)
        return
    mesh = token.mesh
    yield b": connected\n\n"
    last_ping = time.monotonic()
    health = _health(app, mesh)
    while True:
        reason = _reason(app, sub, token)
        if reason is not None:
            yield _control(reason)
            return
        current_health = _health(app, mesh)
        if current_health != health:
            health = current_health
            yield _control('server_state_changed')
        if sub.take_gap():
            yield _control('resync')
        # A local health check is not a broad read-model poll. It also applies
        # idle lock deadlines when no data event arrives to wake this stream.
        ev = sub.get(timeout=min(1.0, max(0.01, ping_s)))
        reason = _reason(app, sub, token)
        if reason is not None:
            yield _control(reason)
            return
        if ev is None:
            if time.monotonic() - last_ping >= min(ping_s, 15.0):
                last_ping = time.monotonic()
                # Visible to JS so a half-open stream cannot masquerade as a
                # healthy event source forever. This never requests a read.
                yield b'data: {"type":"heartbeat"}\n\n'
            continue
        encoded = f"data: {json.dumps(frame(ev, mesh.notifier, app.latency))}\n\n".encode()
        reason = _reason(app, sub, token)
        if reason is not None:
            yield _control(reason)
            return
        yield encoded
