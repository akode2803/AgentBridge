"""Server-Sent Events off the R10 bus — the frontend's realtime signal.

Frames are deliberately MINIMAL (type + chat + ids): the client refetches
through the read model, so no body — encrypted or not — ever rides the
stream. A dropped frame only delays a repaint until the fallback poll.

One exception (R42/Q26): frames that DESERVE a desktop notification carry a
``notify`` lane — chat name, sender, 120-char preview — decided by the R10
Notifier (membership, not-from-me, mute, read-state) and decrypted by this
identity's own sealer. The stream is the signed-in owner's authed session;
the client displays it (or not: window focus + its own prefs) but never has
to refetch just to ping.
"""

from __future__ import annotations

import json
from typing import Iterator

from ..mesh import eventbus
from ..mesh.eventbus import Event, Subscription
from ..mesh.notify import Notifier
from .context import GuiApp

__all__ = ["frame", "stream"]


def frame(ev: Event, notifier: Notifier | None = None) -> dict:
    out = {"type": ev.type, "chat_id": ev.chat_id, "ns": ev.ns}
    if ev.type == eventbus.MESSAGE:
        out["id"] = ev.data.get("id", "")
        out["from"] = ev.data.get("from", "")
    elif ev.type == eventbus.CHAT_UPDATE:
        out["event"] = (ev.data.get("event") or {}).get("type", "")
    elif ev.type == eventbus.ADDED_TO_CHAT:
        out["by"] = ev.data.get("by", "")
    if notifier is not None and ev.type in (eventbus.MESSAGE, eventbus.ADDED_TO_CHAT):
        try:
            note = notifier.consider(ev)
        except Exception:  # noqa: BLE001 — a notify hiccup never drops the frame
            note = None
        if note is not None:
            out["notify"] = {
                "kind": note.kind, "chat_name": note.chat_name,
                "chat_kind": note.chat_kind,
                "from": note.from_, "preview": note.preview, "ns": note.ns,
            }
    return out


def stream(app: GuiApp, sub: Subscription, ping_s: float) -> Iterator[bytes]:
    """Yield SSE frames until the session changes (logout) or the caller's
    write fails (client gone). Idle gaps carry comment pings so proxies and
    the client can tell a quiet stream from a dead one."""
    mesh = app.mesh
    yield b": connected\n\n"
    while app.mesh is mesh:
        ev = sub.get(timeout=ping_s)
        if app.mesh is not mesh:
            break
        if ev is None:
            yield b": ping\n\n"
            continue
        yield f"data: {json.dumps(frame(ev, mesh.notifier))}\n\n".encode()
