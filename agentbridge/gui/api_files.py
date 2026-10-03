"""Attachments + avatars: upload staging, sealed chat blobs (R13 closes the
R9 OPEN item — file bytes ride chat keys), decrypt-serving, OS handoff.

Staged uploads are account-scoped local files under ``<home>/gui_uploads``.
Posting seals each into a durable local delivery spool and commits its stable
manifest with the message; the outbox uploads that exact blob before appending
the envelope, then removes the spool only after acknowledgement. Serving
verifies membership first and (when the signed message carried a sha256)
verifies provenance before a single byte leaves the endpoint.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import mimetypes
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from ..core.lock import SingleInstance
from ..mesh.paths import P
from ..mesh.attachments import attachment_path
from . import desktop
from .attachment_read import read_attachment
from .routing import Response, authed, authed_read_token

__all__ = ["GET", "POST", "RAW_POST", "safe_name", "stage_dir",
           "prepare_attachments"]

_TOKEN_RE = re.compile(r"^[a-f0-9]{16}_[^/\\]+$")
_STAGE_MAX_AGE_S = 24 * 3600.0
_STAGE_MAX_BYTES = 1024 * 1024 * 1024
_STAGE_MAX_FILES = 100


def safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name).strip() or "file"
    return name[:120]


def cache_filename(name: str, blob_id: str) -> str:
    """Readable prefix, collision-resistant suffix, original OS extension.

    Bound UTF-8 bytes as well as characters for common filesystem limits.
    Internal blob IDs and pre-existing cache files are unchanged.
    """
    readable = Path(safe_name(name))
    stem = readable.stem.encode("utf-8")[:160].decode("utf-8", errors="ignore")
    suffix = readable.suffix.encode("utf-8")[:40].decode("utf-8", errors="ignore")
    identity = hashlib.sha256(blob_id.encode("utf-8")).hexdigest()[:24]
    return f"{stem}_{identity}{suffix}"


def stage_dir(app, user: str = ""):
    account = re.sub(r"[^a-z0-9_-]", "_", (user or app.user or "signed-out").lower())
    base = app.home / "gui_uploads"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune_stage(base)  # age out pre-R118 flat staging files too
    d = base / account
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune_stage(d)
    return d


def _prune_stage(root) -> int:
    now = time.time()
    entries = []
    pruned = 0
    for path in root.iterdir():
        try:
            if path.is_symlink() or (path.name.startswith(".")
                                     and path.name.endswith(".tmp")):
                path.unlink(missing_ok=True)
                pruned += 1
            elif path.is_file():
                stat = path.stat()
                entries.append((path, stat.st_size, stat.st_mtime))
        except OSError:
            continue
    kept_bytes = 0
    kept_files = 0
    for path, size, mtime in sorted(entries, key=lambda x: x[2], reverse=True):
        expired = mtime < now - _STAGE_MAX_AGE_S
        over = kept_files >= _STAGE_MAX_FILES or kept_bytes + size > _STAGE_MAX_BYTES
        if expired or over:
            try:
                path.unlink()
                pruned += 1
            except OSError:
                pass
            continue
        kept_files += 1
        kept_bytes += size
    return pruned


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
    cap = int(getattr(mesh.tx, "max_upload_bytes", 0) or 0)
    if cap and len(raw) > cap:
        return {"error": "file exceeds this connection's storage limit"}
    name = safe_name(req.params.get("name", "file"))
    token = f"{secrets.token_hex(8)}_{name}"
    target = stage_dir(app, mesh.user) / token
    tmp = target.with_name(f".{token}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_bytes(raw)
        tmp.chmod(0o600)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "token": token, "name": name, "bytes": len(raw)}


def prepare_attachments(app, mesh, chat_id: str, tokens: list) -> tuple[list, list]:
    """Stage tokens into durable message-owned sealed spools.

    Staging files remain until the envelope+manifest commit succeeds. A partial
    prepare failure removes only newly created spools, so retrying the composer
    never needs the member to upload the originals again.
    """
    mesh.require_send(chat_id)  # authorization precedes file reads and sealing
    prepared = []
    staged_paths = []
    try:
        for token in tokens or []:
            token = str(token or "")
            if not _TOKEN_RE.match(token):
                continue  # not one of ours; never touch arbitrary paths
            staged = stage_dir(app, mesh.user) / token
            if not staged.is_file() or staged.is_symlink():
                continue
            raw = staged.read_bytes()
            name = safe_name(token.split("_", 1)[1])
            prepared.append(mesh.prepare_attachment(chat_id, name, raw))
            staged_paths.append(staged)
    except Exception:
        mesh.cancel_attachments(prepared)
        raise
    return prepared, staged_paths


# ----------------------------------------------------------------- serving
def _file_error(state, *, saved=None):
    out = {'error': state['reason'].replace('_', ' '), **state}
    if state.get('status') == 'locked':
        out['locked'] = True
    if saved is not None:
        out['saved'] = saved
    return out


def _current_handoff(app, token):
    return not app.lock.expire_if_idle() and app.validate_session_read(token)


def _write_private(path: Path, data: bytes, *, unlink_on_error=True) -> None:
    """Create our own 0600 file; shared destinations are scrubbed only by fd.

    An untrusted directory entry cannot be identity-checked and unlinked
    atomically. Shared save failures may leave an empty reserved filename.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    try:
        # Unbuffered writes let failure cleanup truncate the held descriptor
        # without a later buffer flush restoring partial plaintext.
        with os.fdopen(fd, 'wb', buffering=0) as handle:
            owned_fd = fd
            fd = -1  # the context now owns and closes this descriptor
            try:
                if handle.write(data) != len(data):
                    raise OSError('short file write')
                handle.flush()
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise OSError('file replaced during write')
            except BaseException:
                if not unlink_on_error:
                    with contextlib.suppress(OSError):
                        os.ftruncate(owned_fd, 0)
                raise
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if unlink_on_error:
            with contextlib.suppress(OSError):
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
        raise


@authed_read_token
def file(app, req, mesh, token):
    """GET ?chat=&id=&message_id= — exact canonical two-pass handout."""
    chat_id = req.params.get("chat", "")
    blob_id = req.params.get("id", "")
    message_id = req.params.get("message_id", "")
    state, result = read_attachment(app, mesh, token, chat_id, message_id, blob_id)
    if state:
        return _file_error(state)
    rec, raw = result
    name = safe_name(rec.get("name") or blob_id)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(body=raw, ctype=ctype, headers={
        "Content-Disposition": f'inline; filename="{name}"',
        "Cache-Control": "no-store",
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
_HANDOFF_LOCK = threading.Lock()


def _cache_identity(info):
    # CPython 3.12 Windows lstat uses creation time for ctime, while fstat can
    # report change time. Birth time is comparable across both APIs there.
    generation = getattr(info, 'st_birthtime_ns', info.st_ctime_ns)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, generation)


def _verified_cache_identity(path, size, digest):
    """Verify a private regular cache file without another full-size allocation.

    Windows privacy relies on the existing trusted app-home ACL boundary;
    st_mode there does not describe ACLs. Reject reparse points on that platform.
    """
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size != size
                or getattr(before, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            return None
        if os.name != 'nt' and (before.st_uid != os.getuid() or before.st_mode & 0o077):
            return None
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        flags |= getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if _cache_identity(opened) != _cache_identity(before):
                return None
            observed = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return None
                observed.update(chunk)
                remaining -= len(chunk)
            extra = handle.read(1)
            after = os.fstat(handle.fileno())
            if (extra or observed.hexdigest() != digest
                    or _cache_identity(after) != _cache_identity(before)
                    # Compare change time within the same descriptor API;
                    # normalization must not hide mutations during hashing.
                    or after.st_ctime_ns != opened.st_ctime_ns
                    or (after.st_mode, after.st_nlink, after.st_uid)
                    != (opened.st_mode, opened.st_nlink, opened.st_uid)
                    or _cache_identity(path.lstat()) != _cache_identity(before)):
                return None
        return _cache_identity(before)
    except OSError:
        return None


def _unlink_cache_identity(path, identity):
    # A handler may still own the file. Cleanup must not break a successful open.
    try:
        if identity is not None and _cache_identity(path.lstat()) == identity:
            path.unlink()
    except OSError:
        pass


def _cache_handoff_target(target, raw, digest):
    """At most one canonical copy and one fallback per attachment, under lock."""
    fallback = target.with_name(f'.{target.stem}.handoff{target.suffix}')
    # Keep a handed-off fallback even after canonical replacement becomes
    # possible: an asynchronous OS handler may not have opened that path yet.
    # Reusing one fixed slot bounds retention without guessing reader lifetimes.
    if _verified_cache_identity(target, len(raw), digest) is not None:
        return target, None
    temp = target.with_name(f'.{target.name}.{secrets.token_hex(8)}.tmp')
    identity = None
    try:
        _write_private(temp, raw)
        identity = _cache_identity(temp.lstat())
        try:
            os.replace(temp, target)
        except PermissionError:
            if _verified_cache_identity(fallback, len(raw), digest) is not None:
                return fallback, None
            # Exclusive creation: unsafe or stale collisions are never replaced.
            try:
                fallback.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(errno.EEXIST, 'attachment cache slot is occupied',
                                      str(fallback)) from None
            _write_private(fallback, raw)
            return fallback, _cache_identity(fallback.lstat())
        return target, None
    finally:
        _unlink_cache_identity(temp, identity)


@authed_read_token
def open_file(app, req, mesh, token) -> dict:
    """Decrypt to the local cache and open with the OS default handler."""
    chat_id = req.data.get("chat_id") or ""
    blob_id = str(req.data.get("id") or "")
    state, result = read_attachment(app, mesh, token, chat_id,
                                    req.data.get('message_id'), blob_id)
    if state:
        return _file_error(state)
    rec, raw = result
    if not _current_handoff(app, token):
        return {"error": "session changed"}
    cache = app.home / "files_cache" / chat_id
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / cache_filename(rec.get("name") or "file", blob_id)
    # Serialize publication through the OS handoff, including other GUI
    # processes using this home. Keep the lock file: unlinking it splits locks.
    with _HANDOFF_LOCK, SingleInstance(app.home / 'files_cache.lock', fail_open=False) as guard:
        if not guard.acquired:
            return {"error": "Attachment cache is busy; try again"}
        if not _current_handoff(app, token):
            return {"error": "session changed"}
        selected, created = _cache_handoff_target(target, raw, rec['sha256'])
        handed_off = False
        try:
            if not _current_handoff(app, token):
                return {"error": "session changed"}
            desktop.open_path(selected)
            handed_off = True
            return {"ok": True}
        finally:
            if not handed_off:
                _unlink_cache_identity(selected, created)


@authed_read_token
def save(app, req, mesh, token) -> dict:
    """Save attachments OUT to a user-chosen folder (WhatsApp 'download')."""
    chat_id = req.data.get("chat_id") or ""
    files = req.data.get('files')
    if type(files) is not list or not files:
        return {"error": "Nothing to save"}
    if len(files) > 100 or any(type(item) is not dict
                               or type(item.get('message_id')) is not str
                               or not item['message_id']
                               or type(item.get('id')) is not str for item in files):
        return {"error": "invalid file selection"}
    for item in files:
        attachment_path(chat_id, item['id'])
    dest = desktop.pick_folder()
    if not dest:
        return {"ok": True, "saved": 0, "cancelled": True}
    dest_dir = Path(dest)
    saved = total = 0
    for item in files:
        state, result = read_attachment(app, mesh, token, chat_id,
                                        item['message_id'], item['id'])
        if state:
            return _file_error(state, saved=saved)
        rec, raw = result
        if len(raw) > 512 * 1024 * 1024 - total:
            return {"error": "save batch byte budget", "saved": saved}
        name = safe_name(rec.get('name') or item['id'])
        if not _current_handoff(app, token):
            return {"error": "session changed", "saved": saved}
        try:
            for i in range(1000):
                stem, dot, suf = name.rpartition('.')
                candidate = (name if i == 0 else
                             f'{stem} ({i}).{suf}' if dot else f'{name} ({i})')
                output = dest_dir / candidate
                if not _current_handoff(app, token):
                    return {"error": "session changed", "saved": saved}
                try:
                    # A shared directory can replace a closed staging pathname.
                    # Write verified bytes only through our exclusive output
                    # descriptor, as on filesystems without hardlink support.
                    _write_private(output, raw, unlink_on_error=False)
                    break
                except FileExistsError:
                    continue
            else:
                return {"error": "file name collision limit", "saved": saved,
                        "dest": str(dest_dir)}
            saved += 1
            total += len(raw)
        except OSError:
            return {"error": "file write failed", "saved": saved, "dest": str(dest_dir)}
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
