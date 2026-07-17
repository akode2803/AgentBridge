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
    lock = getattr(app, "lock", None)   # V111: the lock page keys off this
    return {
        "configured": True,
        "v": 2,
        "gui_version": app.app_version,
        "caps": {"sse": True, "receipts": "delivered", "admins": True},
        "paused": bool(ctl.get("paused")),
        "user": app.user,
        # V125: a blind session restore in flight — the frontend holds the
        # boot surface instead of flashing the sign-in page
        "restoring": bool(getattr(app, "restoring", False)),
        "connection": _connection(app),
        "app_lock": lock.status() if lock is not None
        else {"enabled": False, "locked": False, "autolock_min": 0},
    }


def _live_by_chat(app: GuiApp, mesh) -> dict[str, list[dict]]:
    """V66: who is typing / which agent run is mid-flight, per chat — one
    pass over the mirror's status docs (free; no cloud call rides this).
    Membership is applied by the CALLER: only chats already in the viewer's
    own list get annotated, so nothing leaks about rooms they aren't in.
    Thresholds mirror the in-chat feed: typing heartbeats go stale at 12s,
    a run silent for 10+ minutes is a ghost. V109: a "running" doc from a
    locally-hosted agent whose runner PROCESS is dead is a ghost NOW, not
    in ten minutes — process truth beats the stale doc."""
    from .api_agents import runner_state
    from .api_messages import _age_s
    from .livefeed import expand_runs

    live: dict[str, list[dict]] = {}
    try:
        paths = mesh.tx.list_docs("status")
    except Exception:  # noqa: BLE001 — liveliness is decoration, never a 500
        return live
    for path in paths:
        leaf = path.rsplit("/", 1)[-1]
        is_typing = leaf.startswith("typing_")
        doc = mesh.tx.get_doc(path)
        if not isinstance(doc, dict):
            continue
        if is_typing:
            cid = doc.get("chat_id") or ""
            age = _age_s(doc.get("updated", ""))
            who = doc.get("user") or ""
            if (not cid or not who or who == mesh.user or age is None
                    or age > 12):
                continue
            live.setdefault(cid, []).append({"user": who, "typing": True})
            continue
        for run in expand_runs(path, doc):
            if run.get("state") != "running":
                continue
            cid = run.get("chat_id") or ""
            if not cid:
                continue
            age = _age_s(run.get("updated", ""))
            if age is not None and age > 600:
                continue
            who = run.get("agent") or ""
            if runner_state(app, mesh, who) is False:
                continue
            live.setdefault(cid, []).append(
                {"user": who, "run_id": run.get("run_id") or "",
                 "activity": " ".join(
                     str(run.get("activity") or "").split())[:80]})
    for entries in live.values():
        entries.sort(key=lambda item: (not item.get("typing"),
                                       item.get("user", ""),
                                       item.get("run_id", "")))
    return live


def state(app: GuiApp, req) -> dict:
    """The boot/sidebar payload. Logged out: enough for the login screen.
    Logged in: the privacy-filtered directory + my chats with unread info."""
    # V111: this endpoint is deliberately pre-auth (the login screen reads
    # it), so the authed gate doesn't cover it — refuse explicitly while
    # locked; it carries the whole directory + chat list
    lock = getattr(app, "lock", None)
    if lock is not None and lock.locked:
        return {"error": "App is locked", "locked": True}
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
    users: dict = {}
    for name in mesh.directory.names():
        acc = mesh.directory.get(name)
        if acc is None:
            continue
        profile = mesh.visible_profile(name)
        presence = {
            k: v for k, v in mesh.visible_presence(name).items() if v is not None
        }
        entry = user_json(acc, profile, presence or None, me=mesh.user)
        if app.encrypt:
            # R31: the trusted-key fingerprint + out-of-band verified state,
            # for the DM info Encryption card (compare over another channel)
            entry["key_fp"] = mesh.key_pins.fingerprint(
                name, acc.keys.sign_pub, acc.keys.agree_pub)
            entry["key_verified"] = mesh.key_pins.verified(name)
        users[name] = entry
    out["users"] = users
    chats = []
    live = _live_by_chat(app, mesh)
    for snap in mesh.chats_for():
        overview = mesh.chat_overview(snap.id)
        entry = chat_json(snap, overview=overview)
        if live.get(snap.id):  # V66: sidebar liveliness — set only when active
            entry["live"] = live[snap.id]
        chats.append(entry)
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
    # V62: the per-chat agents stand-down flag (shared, any member flips it)
    ctl = mesh.tx.get_doc(f"chats/{chat_id}/control.json")
    meta["agents_paused"] = bool(isinstance(ctl, dict) and ctl.get("paused"))
    # V102: the blocker's own view of a blocked DM — the frontend swaps the
    # composer for a "You blocked @X · Unblock" bar. ONLY the viewer's own
    # block list feeds this; being blocked BY the peer never leaks (the
    # WhatsApp rule — that side just gets "@X is not available" on send).
    if meta["kind"] == "dm":
        other = next((m for m in snap.members if m != me), None)
        acc = mesh.directory.get(me)
        meta["blocked"] = bool(other and acc and other in acc.blocked)
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
