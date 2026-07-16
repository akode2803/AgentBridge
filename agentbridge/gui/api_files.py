"""Attachments + avatars: upload staging, sealed chat blobs (R13 closes the
R9 OPEN item — file bytes ride chat keys), decrypt-serving, OS handoff.

Staged uploads are one-shot local files under ``<home>/gui_uploads``; posting
seals them into ``chats/<id>/files/`` and deletes the staging copy. Serving
verifies membership first and (when the signed message carried a sha256)
verifies provenance before a single byte leaves the endpoint.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets

from ..core.timekit import new_id
from ..mesh.paths import P
from . import desktop
from .routing import Response, authed

__all__ = ["GET", "POST", "RAW_POST", "safe_name", "stage_dir", "seal_attachments"]

_TOKEN_RE = re.compile(r"^[a-f0-9]{16}_[^/\\]+$")


def safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name).strip() or "file"
    return name[:120]


def stage_dir(app):
    d = app.home / "gui_uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------- blob disk cache (R76/V84)
# Content-addressed by the PLAINTEXT sha256 (the signed record / avatar
# marker), so a metered transport pays Storage egress once per content
# version per machine instead of on every render/hard-reload. Trust model:
# the cache lives beside the keystore on the member's own disk — same trust.
_CACHE_CAP_BYTES = 500 * 1024 * 1024

def _cache_dir(app):
    d = app.home / "gui_cache" / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_get(app, sha: str) -> bytes | None:
    if not sha or not re.fullmatch(r"[a-f0-9]{16,64}", sha):
        return None
    f = _cache_dir(app) / sha
    try:
        return f.read_bytes() if f.is_file() else None
    except OSError:
        return None


def _cache_put(app, sha: str, data: bytes) -> None:
    if not sha or not re.fullmatch(r"[a-f0-9]{16,64}", sha) or not data:
        return
    d = _cache_dir(app)
    tmp = d / f".{sha}.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(d / sha)
    except OSError:
        tmp.unlink(missing_ok=True)
        return
    try:  # bound the cache: drop oldest-touched entries beyond the cap
        entries = [(f.stat().st_mtime, f.stat().st_size, f)
                   for f in d.iterdir() if f.is_file()]
        total = sum(s for _, s, _ in entries)
        for _, size, f in sorted(entries):
            if total <= _CACHE_CAP_BYTES:
                break
            f.unlink(missing_ok=True)
            total -= size
    except OSError:
        pass


# ------------------------------------------------------------------ upload
@authed
def upload(app, req, mesh, raw: bytes) -> dict:
    """POST raw file body, ``?name=`` — returns the one-shot staging token
    the client passes back in post()'s ``attachments``."""
    if not raw:
        return {"error": "empty upload"}
    name = safe_name(req.params.get("name", "file"))
    token = f"{secrets.token_hex(8)}_{name}"
    (stage_dir(app) / token).write_bytes(raw)
    return {"ok": True, "token": token, "name": name, "bytes": len(raw)}


def seal_attachments(app, mesh, chat_id: str, tokens: list) -> list[dict]:
    """Staged tokens -> sealed chat blobs -> files[] records (id, name,
    bytes, sha256). The sha rides the SIGNED message = provenance."""
    files = []
    for token in tokens or []:
        token = str(token or "")
        if not _TOKEN_RE.match(token):
            continue  # not one of ours; never touch arbitrary paths
        staged = stage_dir(app) / token
        if not staged.is_file():
            continue
        raw = staged.read_bytes()
        name = safe_name(token.split("_", 1)[1])
        dot = name.rfind(".")
        blob_id = new_id("f") + (name[dot:][:12].lower() if dot > 0 else "")
        sealed = mesh.sealer.seal_blob(chat_id, blob_id, raw)
        mesh.tx.put_blob(P.file(chat_id, blob_id), sealed)
        files.append({"id": blob_id, "name": name, "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})
        staged.unlink(missing_ok=True)  # one-shot
    return files


# ----------------------------------------------------------------- serving
def _find_file_record(mesh, chat_id: str, blob_id: str) -> dict | None:
    """The files[] entry naming this blob, from the READ MODEL — so a
    deleted message's attachment stops being servable too."""
    for m in mesh.messages_for(chat_id):
        if m.deleted:
            continue
        for f in m.files or []:
            if f.get("id") == blob_id:
                return f
    return None


def _open_blob(mesh, chat_id: str, blob_id: str) -> bytes | None:
    data = mesh.tx.get_blob(P.file(chat_id, blob_id))
    if data is None:
        return None
    return mesh.sealer.open_blob(chat_id, blob_id, data)


@authed
def file(app, req, mesh):
    """GET ?chat=&id= — membership-gated decrypt-serve with provenance
    check against the signed message's sha256."""
    chat_id = req.params.get("chat", "")
    blob_id = req.params.get("id", "")
    rec = _find_file_record(mesh, chat_id, blob_id)  # gates membership too
    if rec is None:
        return {"error": "file not found"}
    sha = str(rec.get("sha256") or "")
    raw = _cache_get(app, sha)               # R76: Storage pays once per sha
    if raw is None:
        raw = _open_blob(mesh, chat_id, blob_id)
        if raw is None:
            return {"error": "file not available"}
        if sha and hashlib.sha256(raw).hexdigest() != sha:
            return {"error": "file failed verification"}
        _cache_put(app, sha, raw)
    name = safe_name(rec.get("name") or blob_id)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(body=raw, ctype=ctype, headers={
        "Content-Disposition": f'inline; filename="{name}"',
        # a blob id names ONE immutable upload — safe to cache long
        "Cache-Control": "private, max-age=604800",
    })


def avatar(app, req):
    """GET ?user= | ?chat= — profile photos are matrix-gated (photo
    audience), group photos are member-gated. NOT @authed by design: it
    branches, but every branch checks the session itself."""
    lock = getattr(app, "lock", None)   # V111: no photos while locked
    if lock is not None and lock.locked:
        return {"error": "App is locked", "locked": True}
    mesh = app.mesh
    if mesh is None:
        return {"error": "Sign in first"}
    user = req.params.get("user", "")
    chat_id = req.params.get("chat", "")
    if user:
        target = mesh.directory.resolve(user.strip().lower())
        if target is None:
            return {"error": "unknown user"}
        if not mesh.profile_allows("photo", target, mesh.user):
            return {"error": "not available"}
        acc = mesh.directory.get(target)
        sha = str(((acc.avatar if acc else None) or {}).get("sha256") or "")
        data = _cache_get(app, sha)          # R76: Storage pays once per sha
        if data is None:
            data = mesh.tx.get_blob(P.avatar(target))
            _cache_put(app, sha, data or b"")
    elif chat_id:
        snap = mesh.snapshot(chat_id)
        if not snap.is_member(mesh.user) or not snap.avatar:
            return {"error": "not available"}
        sha = str(snap.avatar or "")
        data = _cache_get(app, sha)
        if data is None:
            data = mesh.tx.get_blob(P.chat_avatar(chat_id))
            _cache_put(app, sha, data or b"")
    else:
        return {"error": "user or chat required"}
    if not data:
        return {"error": "no photo"}
    # the URL is sha-versioned (&v=), so the bytes behind it never change
    return Response(body=data, ctype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, "
                                              "immutable"})


# ----------------------------------------------------------------- avatars
@authed
def set_avatar(app, req, mesh, raw: bytes) -> dict:
    acc = mesh.accounts.set_avatar(raw)
    return {"ok": True, "avatar": acc.avatar}


@authed
def set_agent_avatar(app, req, mesh, raw: bytes) -> dict:
    acc = mesh.accounts.set_avatar(raw, agent=(req.params.get("agent") or "")
                                   .strip().lower() or None)
    return {"ok": True, "avatar": acc.avatar}


@authed
def set_group_avatar(app, req, mesh, raw: bytes) -> dict:
    snap = mesh.set_avatar(req.params.get("chat", ""), raw)
    return {"ok": True, "avatar": snap.avatar}


@authed
def clear_avatar(app, req, mesh) -> dict:
    mesh.accounts.clear_avatar()
    return {"ok": True}


@authed
def clear_agent_avatar(app, req, mesh) -> dict:
    mesh.accounts.clear_avatar(agent=(req.data.get("agent") or "")
                               .strip().lower() or None)
    return {"ok": True}


@authed
def clear_group_avatar(app, req, mesh) -> dict:
    mesh.membership.clear_avatar(req.data.get("chat_id") or "")
    return {"ok": True}


# --------------------------------------------------------------- OS handoff
@authed
def open_file(app, req, mesh) -> dict:
    """Decrypt to the local cache and open with the OS default handler."""
    chat_id = req.data.get("chat_id") or ""
    blob_id = str(req.data.get("id") or "")
    rec = _find_file_record(mesh, chat_id, blob_id)
    if rec is None:
        return {"error": "File not found — it may still be syncing"}
    raw = _open_blob(mesh, chat_id, blob_id)
    if raw is None:
        return {"error": "File not available"}
    cache = app.home / "files_cache" / chat_id
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{blob_id}_{safe_name(rec.get('name') or 'file')}"
    if not target.is_file() or target.stat().st_size != len(raw):
        target.write_bytes(raw)
    desktop.open_path(target)
    return {"ok": True}


@authed
def save(app, req, mesh) -> dict:
    """Save attachments OUT to a user-chosen folder (WhatsApp 'download')."""
    chat_id = req.data.get("chat_id") or ""
    ids = [str(i) for i in (req.data.get("ids") or [])]
    if not ids:
        return {"error": "Nothing to save"}
    items = []
    for blob_id in ids:
        rec = _find_file_record(mesh, chat_id, blob_id)
        raw = _open_blob(mesh, chat_id, blob_id) if rec else None
        if rec is None or raw is None:
            return {"error": "A file was not found — it may still be syncing"}
        items.append((safe_name(rec.get("name") or blob_id), raw))
    dest = desktop.pick_folder()
    if not dest:
        return {"ok": True, "saved": 0, "cancelled": True}
    from pathlib import Path

    dest_dir = Path(dest)
    saved = 0
    for name, raw in items:
        out = dest_dir / name
        i = 1
        while out.exists():  # never clobber: name, name (1), name (2)…
            stem, dot, suf = name.rpartition(".")
            out = dest_dir / (f"{stem} ({i}).{suf}" if dot else f"{name} ({i})")
            i += 1
        try:
            out.write_bytes(raw)
            saved += 1
        except OSError:
            pass
    return {"ok": True, "saved": saved, "dest": str(dest_dir)}


def open_target(app, req) -> dict:
    """The Settings → Connection 'open' buttons (v1 parity — the route was
    missing in v2, leaving them dead). Targets are FIXED names, never a
    client-supplied path: 'home' = the local config dir, 'shared' = a folder
    mesh root. A cloud root has no folder to open."""
    lock = getattr(app, "lock", None)   # V111: opens Explorer — locked = no
    if lock is not None and lock.locked:
        return {"error": "App is locked", "locked": True}
    target = (req.data.get("target") or "").strip()
    if target == "home":
        desktop.open_path(app.home)
        return {"ok": True}
    if target == "shared":
        from pathlib import Path

        if not isinstance(app.root, Path):
            return {"error": "The mesh lives in a cloud service — there is "
                             "no folder to open"}
        desktop.open_path(app.root)
        return {"ok": True}
    return {"error": f"unknown open target {target!r}"}


GET = {
    "/api/mesh/file": file,
    "/api/mesh/avatar": avatar,
}
POST = {
    "/api/open": open_target,
    "/api/mesh/open_file": open_file,
    "/api/mesh/save": save,
    "/api/mesh/clear_avatar": clear_avatar,
    "/api/mesh/clear_agent_avatar": clear_agent_avatar,
    "/api/mesh/clear_group_avatar": clear_group_avatar,
}
RAW_POST = {
    "/api/mesh/upload": upload,
    "/api/mesh/set_avatar": set_avatar,
    "/api/mesh/set_agent_avatar": set_agent_avatar,
    "/api/mesh/set_group_avatar": set_group_avatar,
}
