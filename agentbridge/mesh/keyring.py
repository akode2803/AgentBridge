"""Keyring (R9): the local unlocked keystore + the chat-epoch key service.

Epoch model (docs/THREAT_MODEL.md):
- epoch ids are ns ordinals → concurrent rotations can't collide on a file;
- ``ensure()`` runs before EVERY seal: if the newest epoch's wrapped-set no
  longer matches the members, it rotates on the spot — so the next message
  after any membership change (remove, leave, clobbered rotation) is always
  sealed under a correct key. Removed members keep old epochs (D4 history
  semantics) and never appear in new ones.
- adding with send_history=ON re-wraps every epoch I hold for the newcomer;
  with send_history=OFF membership rotation happens FIRST and the newcomer
  only ever sees the new epoch — the history gate is cryptographic.

Under a bound typed writer, a new epoch is one exact create keyed by its path
(its wraps retained before dispatch; an earlier unsettled epoch settles before
another is minted), and a history re-wrap is one conditional update per epoch
document, driven by a durable Store row bound to the newcomers' membership
instances, so a crash resumes without re-wrapping or wrapping for anyone who
has since left.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import crypto
from ..core.config import DEFAULT_HOME
from ..core.errors import CryptoError, NotAMember
from ..core.lock import _try_lock
from ..core.models import ChatSnapshot
from ..core.timekit import next_ns, utcnow_iso
from ..crypto import dpapi
from ..node.security import LocalSecurityError, private_directory, protect_path, verify_private_path
from ..transport.base import Transport
from .directory import Directory
from .paths import P

__all__ = ["KeyStore", "ChatKeyService"]

# All cooperating KeyStore instances participate; external writers are observed
# by bounded content reads, not excluded by this process-local lock.
_KEYSTORE_LOCK = threading.RLock()
# Per (keys directory, name) identity-creation locks: reentrant in a thread,
# and each outermost holder also holds that name's cross-process file lock.
_CREATION_LOCKS: dict[tuple[str, str], list] = {}
# Per keys directory install-lock depth. Every holder also holds
# _KEYSTORE_LOCK, so one thread at a time can nest without self-deadlock.
_INSTALL_DEPTH: dict[str, int] = {}
_INSTALL_NAME = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z", re.ASCII)
_MAX_KEY_FILE_BYTES = 64 * 1024


def _sync_directory(path: Path) -> None:
    """Flush a newly published directory entry where the OS exposes it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _private_install_lock(path: Path):
    """Serialize installers across processes without following a lock symlink."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise LocalSecurityError("identity lock cannot be a symlink")
    fd = os.open(path, flags, 0o600)
    fh = os.fdopen(fd, "r+b", buffering=0)
    acquired = False
    try:
        # Windows byte-range locks need a byte at offset zero.
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            fh.write(b"\0")
            os.fsync(fd)
        value = path.lstat()
        opened = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or
                (value.st_dev, value.st_ino) != (opened.st_dev, opened.st_ino)):
            raise LocalSecurityError("identity lock changed")
        protect_path(path, directory=False)
        verify_private_path(path, directory=False)
        deadline = time.monotonic() + 10
        while True:
            result = _try_lock(fh)
            if result is True:
                acquired = True
                break
            if result is None:
                raise LocalSecurityError("identity lock unavailable")
            if time.monotonic() >= deadline:
                raise LocalSecurityError("identity lock busy")
            time.sleep(0.02)
        yield
    finally:
        if acquired and os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()


class KeyStore:
    """Unlocked identity bundles on THIS machine. One file per identity under
    ``~/.agentbridge/keys``. On Windows the file is DPAPI-wrapped (R31.5,
    per-OS-user scope — a copied file is unreadable off this machine/user);
    a legacy plain-base64 file is upgraded in place on first load. Elsewhere
    the plain format remains (the OS-user boundary, as before)."""

    _WRAPPED = "dpapi1:"  # never valid base64 (the ':'), so shapes can't mix

    def __init__(self, home: Path | None = None) -> None:
        self.dir = (home or DEFAULT_HOME) / "keys"

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.key"

    @contextmanager
    def _install_lock(self):
        """Hold this directory's cross-process key-install lock.

        Every writer of a key file (install, save, forget, recovery codes)
        holds it, so no cooperating process can replace a key between another
        writer's check and its publication. Reentrant within the keystore lock.
        """
        with _KEYSTORE_LOCK:
            key = str(self.dir.resolve())
            depth = _INSTALL_DEPTH.get(key, 0)
            if depth:
                _INSTALL_DEPTH[key] = depth + 1
                try:
                    yield
                finally:
                    _INSTALL_DEPTH[key] -= 1
                return
            with _private_install_lock(self.dir / ".identity-install.lock"):
                _INSTALL_DEPTH[key] = 1
                try:
                    yield
                finally:
                    _INSTALL_DEPTH.pop(key, None)

    def install_if_absent_or_identical(self, name: str, bundle: bytes) -> bool:
        """Durably install one identity, or accept its exact prior installation.

        Return True for a new installation and False for an identical existing
        bundle. Unlike ``load``, a corrupt or unreadable file is never absence.
        This does not change the historical ``save`` overwrite API.
        """
        if type(name) is not str or not _INSTALL_NAME.fullmatch(name):
            raise ValueError("invalid identity name")
        if type(bundle) is not bytes or len(bundle) != 64:
            raise ValueError("invalid identity bundle")
        path = self._path(name)
        with _KEYSTORE_LOCK:
            private_directory(self.dir)
            # The key directory itself may have been created this call. Flush
            # its entry before treating any contained key as durable.
            _sync_directory(self.dir.parent)
            with self._install_lock():
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    current = None
                if current is not None:
                    if not stat.S_ISREG(current.st_mode):
                        raise LocalSecurityError("invalid identity key path")
                    # Existing keys from save() may predate private mode. Fix
                    # permissions, but never replace or reinterpret the data.
                    protect_path(path, directory=False)
                    # Open read/write so fsync also works on Windows. The
                    # legacy save path did not sync this inode before rename.
                    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(path, flags)
                    try:
                        opened = os.fstat(fd)
                        now = path.lstat()
                        if (not stat.S_ISREG(opened.st_mode) or
                                (now.st_dev, now.st_ino) !=
                                (opened.st_dev, opened.st_ino)):
                            raise LocalSecurityError("identity key changed")
                        verify_private_path(path, directory=False)
                        parts = []
                        remaining = _MAX_KEY_FILE_BYTES + 1
                        while remaining:
                            part = os.read(fd, remaining)
                            if not part:
                                break
                            parts.append(part)
                            remaining -= len(part)
                        raw = b"".join(parts)
                        if len(raw) > _MAX_KEY_FILE_BYTES:
                            raise LocalSecurityError("identity key is too large")
                        try:
                            encoded = raw.decode("utf-8").strip()
                            if encoded.startswith(self._WRAPPED):
                                ciphertext = base64.b64decode(
                                    encoded[len(self._WRAPPED):], validate=True)
                                existing = dpapi.unprotect(ciphertext)
                            else:
                                existing = base64.b64decode(encoded, validate=True)
                        except (UnicodeError, ValueError, binascii.Error, OSError) as exc:
                            raise LocalSecurityError("identity key is unreadable") from exc
                        if existing is None or len(existing) != 64:
                            raise LocalSecurityError("identity key is unreadable")
                        if not hmac.compare_digest(existing, bundle):
                            raise ValueError("identity key conflicts with existing key")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    # Also covers a retry after publication succeeded but the
                    # first caller lost its directory-fsync acknowledgement.
                    _sync_directory(self.dir)
                    return False

                wrapped = dpapi.protect(bundle) if dpapi.available() else None
                encoded = (
                    self._WRAPPED + crypto.b64e(wrapped) if wrapped is not None
                    else crypto.b64e(bundle)
                ).encode("utf-8")
                if len(encoded) > _MAX_KEY_FILE_BYTES:
                    raise LocalSecurityError("identity key is too large")
                fd = -1
                tmp_name = ""
                try:
                    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.dir)
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        fd = -1
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temp_path = Path(tmp_name)
                    protect_path(temp_path, directory=False)
                    verify_private_path(temp_path, directory=False)
                    # A hard link publishes the fully synced inode atomically
                    # with create-only semantics. Unlike replace/rename, even
                    # a non-cooperating legacy save cannot make us overwrite
                    # a key that appeared after our absent check.
                    try:
                        os.link(temp_path, path)
                    except FileExistsError as exc:
                        raise LocalSecurityError(
                            "identity key appeared during install") from exc
                    staged, published = temp_path.lstat(), path.lstat()
                    if ((staged.st_dev, staged.st_ino) !=
                            (published.st_dev, published.st_ino)):
                        raise LocalSecurityError("identity key changed during install")
                    temp_path.unlink()
                    tmp_name = ""
                    verify_private_path(path, directory=False)
                    _sync_directory(self.dir)
                    return True
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if tmp_name:
                        Path(tmp_name).unlink(missing_ok=True)

    def save(self, name: str, bundle: bytes, *, max_bytes: int | None = None) -> None:
        with _KEYSTORE_LOCK:
            if max_bytes is not None:
                if type(max_bytes) is not int or max_bytes < 0:
                    raise ValueError("invalid key file budget")
                if len(bundle) > max_bytes:
                    raise OverflowError("key file byte budget")
            wrapped = dpapi.protect(bundle) if dpapi.available() else None
            if max_bytes is not None:
                encoded_size = 4 * ((len(wrapped if wrapped is not None else bundle) + 2) // 3)
                if wrapped is not None:
                    encoded_size += len(self._WRAPPED)
                if encoded_size > max_bytes:
                    raise OverflowError("key file byte budget")
            self.dir.mkdir(parents=True, exist_ok=True)
            text = (
                self._WRAPPED + crypto.b64e(wrapped) if wrapped is not None
                else crypto.b64e(bundle)  # plain fallback — never lose a key
            )
            # atomic replace: load() upgrades legacy files in place, so a
            # concurrent reader must never see a half-written key file
            tmp = self._path(name).with_suffix(".key.tmp")
            with self._install_lock():
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self._path(name))

    def load(self, name: str, *, upgrade: bool = True) -> bytes | None:
        with _KEYSTORE_LOCK:
            try:
                text = self._path(name).read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                return None
            try:
                if text.startswith(self._WRAPPED):
                    # fail-closed: a wrapped file only opens for this OS user
                    return dpapi.unprotect(crypto.b64d(text[len(self._WRAPPED):]))
                bundle = crypto.b64d(text)
            except (ValueError, OSError):
                return None
            if upgrade and bundle and dpapi.available():
                self.save(name, bundle)  # transparent one-time upgrade to wrapped
            return bundle

    def forget(self, name: str) -> None:
        with _KEYSTORE_LOCK:
            if not self.dir.is_dir():
                return
            with self._install_lock():
                self._path(name).unlink(missing_ok=True)

    @contextmanager
    def creating(self, name: str):
        """Serialize creating identity ``name`` on this machine.

        Finding a pending creation, reading the provider and freezing a new
        one must be one step, or two callers could each freeze different keys
        for the same name. Reentrant within a thread; other threads and
        processes wait (a busy file lock fails closed after its bound).
        """
        if type(name) is not str or not _INSTALL_NAME.fullmatch(name):
            raise ValueError("invalid identity name")
        with _KEYSTORE_LOCK:
            private_directory(self.dir)
            key = (str(self.dir.resolve()), name)
            entry = _CREATION_LOCKS.setdefault(key, [threading.RLock(), 0])
        with entry[0]:
            if entry[1]:
                entry[1] += 1
                try:
                    yield
                finally:
                    entry[1] -= 1
                return
            with _private_install_lock(self.dir / f".create-{name}.lock"):
                entry[1] = 1
                try:
                    yield
                finally:
                    entry[1] = 0

    def forget_if_identical(self, name: str, bundle: bytes) -> bool:
        """Remove ``name``'s key only if it is exactly ``bundle``.

        For a key proven never published; any other key stays untouched.
        """
        if type(name) is not str or not _INSTALL_NAME.fullmatch(name):
            raise ValueError("invalid identity name")
        if type(bundle) is not bytes or len(bundle) != 64:
            raise ValueError("invalid identity bundle")
        with _KEYSTORE_LOCK:
            if not self.dir.is_dir():
                return False
            with self._install_lock():
                current = self.load(name, upgrade=False)
                if current is None or not hmac.compare_digest(current, bundle):
                    return False
                self._path(name).unlink()
                _sync_directory(self.dir)
                return True

    # One-time recovery codes awaiting display. A typed sign-up publishes the
    # account before the GUI can show its code, so the code is kept here, with
    # the same protection as the key it recovers, until the user confirms it.
    def _recovery_path(self, name: str) -> Path:
        if type(name) is not str or not _INSTALL_NAME.fullmatch(name):
            raise ValueError("invalid identity name")
        return self.dir / f"{name}.recovery"

    def hold_recovery_code(self, name: str, code: str) -> None:
        """Durably keep ``code`` until released; the identical code is a no-op."""
        path = self._recovery_path(name)
        if type(code) is not str or not code or len(code) > 64:
            raise ValueError("invalid recovery code")
        with _KEYSTORE_LOCK:
            private_directory(self.dir)
            _sync_directory(self.dir.parent)
            with self._install_lock():
                held = self.pending_recovery_code(name)
                if held is not None:
                    if not hmac.compare_digest(held, code):
                        raise ValueError("a different recovery code is held")
                    return
                raw = code.encode("utf-8")
                wrapped = dpapi.protect(raw) if dpapi.available() else None
                text = (self._WRAPPED + crypto.b64e(wrapped) if wrapped is not None
                        else crypto.b64e(raw))
                fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.dir)
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        fd = -1
                        stream.write(text.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    protect_path(Path(tmp_name), directory=False)
                    os.replace(tmp_name, path)
                    tmp_name = ""
                    verify_private_path(path, directory=False)
                    _sync_directory(self.dir)
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if tmp_name:
                        Path(tmp_name).unlink(missing_ok=True)

    def pending_recovery_code(self, name: str) -> str | None:
        path = self._recovery_path(name)
        with _KEYSTORE_LOCK:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                return None
            try:
                if text.startswith(self._WRAPPED):
                    raw = dpapi.unprotect(crypto.b64d(text[len(self._WRAPPED):]))
                else:
                    raw = crypto.b64d(text)
                return raw.decode("utf-8") if raw else None
            except (ValueError, OSError, UnicodeError):
                return None

    def release_recovery_code(self, name: str) -> bool:
        """Forget a held code once shown; False when none was held."""
        path = self._recovery_path(name)
        with _KEYSTORE_LOCK:
            if not self.dir.is_dir():
                return False
            with self._install_lock():
                try:
                    path.unlink()
                except FileNotFoundError:
                    return False
                _sync_directory(self.dir)
                return True


class ChatKeyService:
    def __init__(
        self, tx: Transport, directory: Directory, keystore: KeyStore, user: str,
        *, store=None,
    ) -> None:
        self.tx = tx
        self.directory = directory
        self.keystore = keystore
        self.user = user
        self.store = store  # durable history re-wrap progress (bound writer)
        self._cache: dict[tuple[str, int], bytes] = {}  # (chat, epoch) -> key
        self._cache_lock = threading.RLock()
        self._epoch_owner = uuid.uuid4().hex
        # Epoch documents this process created or extended through a bound
        # typed writer. A receipt does not refresh the local mirror, so until
        # the mirror shows them they are overlaid here; wraps are only ever
        # added to an epoch, so the overlay can never hide a newer recipient.
        self._published: dict[tuple[str, int], dict] = {}
        self._rotation_lock = threading.RLock()

    def _typed(self) -> bool:
        from ..transport.local_mutations import LocalMutationTransport

        return isinstance(self.tx, LocalMutationTransport) and self.tx._bound_writer() is not None

    # ------------------------------------------------------------- reading
    def projection_facts(self, chat_id: str, docs: dict) -> dict:
        """Opaque local key facts without read-through or key-cache mutation."""
        bundle = self.keystore.load(self.user, upgrade=False)
        with self._cache_lock:
            resident = {
                epoch: key for (room, epoch), key in tuple(self._cache.items())
                if room == chat_id
            }

        def fingerprint(key: bytes | None) -> str:
            return hashlib.sha256(b"ab-projection-key\0" + key).hexdigest() if key else ""

        available = {}
        epochs = set(resident)
        for path, doc in docs.items():
            epoch = doc.get("epoch") if isinstance(doc, dict) else None
            if (isinstance(epoch, int) and not isinstance(epoch, bool)
                    and path == P.keys(chat_id, epoch)):
                epochs.add(epoch)
        for epoch in sorted(epochs):
            doc = docs.get(P.keys(chat_id, epoch))
            wrapped = doc.get("wrapped") if isinstance(doc, dict) else None
            wrapped = wrapped.get(self.user) if isinstance(wrapped, dict) else None
            unwrapped = None
            if bundle and wrapped:
                try:
                    unwrapped = crypto.unwrap_key_with(bundle, wrapped)
                except (crypto.CryptoFail, TypeError, ValueError, KeyError):
                    pass
            available[str(epoch)] = {
                "resident": fingerprint(resident.get(epoch)),
                "unwrap": fingerprint(unwrapped),
            }
        return {"identity": fingerprint(bundle), "epochs": available}

    def epochs(self, chat_id: str) -> list[tuple[int, dict]]:
        """Every epoch doc of a chat, oldest→newest by id (id = ns ordinal)."""
        out = []
        for path in self.tx.list_docs(f"chats/{chat_id}/keys"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and isinstance(doc.get("epoch"), int):
                out.append((doc["epoch"], doc))
        with self._cache_lock:
            published = [(epoch, doc) for (room, epoch), doc in self._published.items()
                         if room == chat_id]
        if published:
            out = self._overlay(chat_id, dict(out), published)
        return sorted(out)

    def _overlay(self, chat_id: str, listed: dict, published: list) -> list:
        for epoch, doc in published:
            seen = listed.get(epoch)
            if seen is None:
                listed[epoch] = copy.deepcopy(doc)
                continue
            wrapped = seen.get("wrapped") if isinstance(seen.get("wrapped"), dict) else {}
            if ((seen.get("by"), seen.get("created")) != (doc.get("by"), doc.get("created"))
                    or set(doc["wrapped"]) <= set(wrapped)):
                # The mirror caught up (or shows another document): it wins,
                # unless this entry was replaced meanwhile by a newer one.
                with self._cache_lock:
                    if self._published.get((chat_id, epoch)) is doc:
                        del self._published[(chat_id, epoch)]
                continue
            listed[epoch] = {**seen, "wrapped": {**copy.deepcopy(doc["wrapped"]), **wrapped}}
        return list(listed.items())

    def latest(self, chat_id: str) -> tuple[int, dict] | None:
        eps = self.epochs(chat_id)
        return eps[-1] if eps else None

    def my_key(self, chat_id: str, epoch: int) -> bytes | None:
        with self._cache_lock:
            cached = self._cache.get((chat_id, epoch))
        if cached is not None:
            return cached
        with self._cache_lock:
            doc = copy.deepcopy(self._published.get((chat_id, epoch)))
        if doc is None:
            doc = self.tx.get_doc(P.keys(chat_id, epoch))
        wraps = doc.get("wrapped") if isinstance(doc, dict) else None
        wrapped = wraps.get(self.user) if isinstance(wraps, dict) else None
        if not isinstance(wrapped, dict) or not all(
            isinstance(wrapped.get(field), str) for field in ("eph", "nonce", "ct")
        ):
            return None
        bundle = self.keystore.load(self.user)
        if not wrapped or bundle is None:
            return None
        try:
            key = crypto.unwrap_key_with(bundle, wrapped)
        except crypto.CryptoFail:
            return None
        with self._cache_lock:
            # A concurrent recovery/rotation may have installed a resident key
            # while this cold read was unwrapping. Resident precedence wins.
            return self._cache.setdefault((chat_id, epoch), key)

    # ------------------------------------------------------------- rotation
    def _wrap_for(self, members: list[str], key: bytes) -> dict:
        wrapped = {}
        for name in members:
            acc = self.directory.get(name)
            if acc and acc.keys.agree_pub:
                wrapped[name] = crypto.wrap_key_for(acc.keys.agree_pub, key)
        return wrapped

    def _settle_epochs(self, prefix: str, *, skip_failures: bool = False) -> int:
        """Bring earlier epoch creates whose replies were lost to their exact
        outcome (retained wraps, never re-minted); created ones are overlaid
        so they are usable before the mirror shows them. A recovery sweep
        skips one that still cannot settle; a chat's own rotation does not."""
        def created(path: str, doc) -> None:
            parts = path.split("/")
            if (len(parts) == 4 and parts[2] == "keys" and isinstance(doc, dict)
                    and isinstance(doc.get("epoch"), int)
                    and path == P.keys(parts[1], doc["epoch"])
                    and isinstance(doc.get("wrapped"), dict)):
                with self._cache_lock:
                    self._published[(parts[1], doc["epoch"])] = copy.deepcopy(doc)

        # Only epoch documents: meta creates belong to the membership owner,
        # which must consume their outcome before their tickets are released.
        return self.tx.settle_exact_creates(
            prefix, created, skip_failures=skip_failures,
            include=lambda path: path.split("/")[2:3] == ["keys"])

    def _publish_epoch(self, chat_id: str, members: list[str], key: bytes) -> int:
        """Publish one new epoch wrapping ``key`` for exactly ``members``."""
        typed = self._typed()
        if typed:
            self._settle_epochs(f"chats/{chat_id}/keys/")
        epoch = next_ns()
        doc = {"epoch": epoch, "by": self.user, "created": utcnow_iso(),
               "wrapped": self._wrap_for(members, key)}
        if not typed:
            self.tx.put_doc(P.keys(chat_id, epoch), doc)
            return epoch
        self.tx.create_exact_doc(P.keys(chat_id, epoch), doc)
        with self._cache_lock:
            self._published[(chat_id, epoch)] = copy.deepcopy(doc)
        return epoch

    def rotate(self, chat_id: str, members: list[str]) -> tuple[int, bytes]:
        """New epoch wrapped for exactly ``members``. The actor must be one
        of them (you can't mint keys for rooms you're not in)."""
        if self.user not in members:
            raise CryptoError("cannot rotate keys for a chat you're not in")
        key = crypto.new_chat_key()
        epoch = self._publish_epoch(chat_id, members, key)
        with self._cache_lock:
            self._cache[(chat_id, epoch)] = key
        return epoch, key

    def _current(self, chat_id: str, snap: ChatSnapshot) -> tuple[int, bytes] | None:
        current = self.latest(chat_id)
        if current is None:
            return None
        epoch, doc = current
        creator_ok = doc.get("by", "") in snap.members
        if creator_ok and sorted(doc.get("wrapped", {})) == sorted(snap.members):
            key = self.my_key(chat_id, epoch)
            if key is not None:
                return epoch, key
        return None

    def ensure(self, chat_id: str, snap: ChatSnapshot) -> tuple[int, bytes]:
        """The self-heal: called before every seal. Rotates when there is no
        epoch yet, when the newest epoch's member set drifted from the
        snapshot (someone removed/left/was added-by-a-clobbered-writer), when
        the newest epoch was CREATED BY someone no longer a member (R69: a
        departed member's key is never trusted as current — a remaining
        member re-keys on the next post, neutralizing a key a leaver's client
        may have kept), or when I can't unwrap my copy (I'm newer than the
        wrap). Rotations in this process are serialized, so concurrent sends
        mint one epoch, not one each."""
        found = self._current(chat_id, snap)
        if found is not None:
            return found
        with self._rotation_lock:
            found = self._current(chat_id, snap)
            if found is not None:
                return found
            if self._typed() and self._settle_epochs(f"chats/{chat_id}/keys/"):
                found = self._current(chat_id, snap)
                if found is not None:
                    return found
            return self.rotate(chat_id, sorted(snap.members))

    # ------------------------------------------------------ membership hooks
    def on_members_added(
        self, chat_id: str, snap: ChatSnapshot, newcomers: list[str]
    ) -> None:
        """send_history ON: re-wrap every epoch I can open for the newcomers
        (full history readable). OFF: rotate — newcomers only ever get the
        new epoch, so pre-join ciphertext stays sealed to them FOREVER."""
        if not snap.permissions.send_history:
            self.rotate(chat_id, sorted(snap.members))
            return
        if self._typed():
            self._rewrap_history(chat_id, snap, newcomers)
            return
        for epoch, doc in self.epochs(chat_id):
            key = self.my_key(chat_id, epoch)
            if key is None:
                continue  # I don't hold this one (I joined late myself)
            wrapped = doc.get("wrapped", {})
            added = False
            for name in newcomers:
                if name in wrapped:
                    continue
                acc = self.directory.get(name)
                if acc and acc.keys.agree_pub:
                    wrapped[name] = crypto.wrap_key_for(acc.keys.agree_pub, key)
                    added = True
            if added:
                doc["wrapped"] = wrapped
                self.tx.put_doc(P.keys(chat_id, epoch), doc)

    def on_members_removed(self, chat_id: str, snap: ChatSnapshot) -> None:
        """Rotate away from the departed immediately (ensure() would catch it
        at the next seal anyway — this just shrinks the overlay window)."""
        self.rotate(chat_id, sorted(snap.members))

    def on_member_left(self, chat_id: str, snap: ChatSnapshot) -> None:
        """R69: I am leaving — rotate the epoch away from myself so the chat
        stops using a key I hold. Unlike ``rotate``/``on_members_removed``
        the actor (me) is NOT in the new member set, so this mints the epoch
        directly and deliberately does NOT cache my own copy (I keep no key
        for a room I've left). The new epoch is created ``by`` me but wrapped
        only for the remaining members; ensure()'s departed-creator check
        then has a remaining member re-key on their next post, so even a
        modified client that kept this key gains nothing lasting. Last one
        out (no remaining members) has nothing to rotate for."""
        members = sorted(n for n in snap.members if n != self.user)
        if not members:
            return
        self._publish_epoch(chat_id, members, crypto.new_chat_key())
        # no self._cache write: a left chat leaves no key on this device

    # ------------------------------------------- bound history re-wrapping
    def _rewrap_history(self, chat_id: str, snap: ChatSnapshot,
                        newcomers: list[str]) -> None:
        """Re-wrap history for newcomers through a durable progress row."""
        if self.store is None:
            raise CryptoError("history re-wrapping needs this user's local store")
        bound = {name: snap.members[name].joined_ns
                 for name in newcomers if name in snap.members}
        if bound:
            self._resume_rewrap(self.store.key_rewrap_add(chat_id, bound), snap)

    def _resume_rewrap(self, row, snap: ChatSnapshot) -> None:
        # Only the exact membership instances the add created receive history,
        # and only while history stays visible and I am still a member: a
        # newcomer who left (even if re-added since) gets nothing more.
        names = {}
        if self.user in snap.members and snap.permissions.send_history:
            names = {name: joined for name, joined in row.newcomers.items()
                     if joined > 0 and name in snap.members
                     and snap.members[name].joined_ns == joined}
        # A newcomer without a published agreement key cannot be wrapped yet:
        # wrap the others but keep the row (and its epoch position) so a
        # later recover() pass gives that newcomer the same history. Repeat
        # passes only add wraps still missing.
        keyless = {name for name in names
                   if not ((acc := self.directory.get(name)) and acc.keys.agree_pub)}
        if names:
            for epoch, _doc in self.epochs(row.chat_id):
                if epoch <= row.after_epoch:
                    continue
                self._rewrap_epoch(row.chat_id, epoch, names)
                if not keyless:
                    self.store.key_rewrap_advance(row.id, epoch)
        if not keyless:
            self.store.key_rewrap_finish(row.id)

    def _rewrap_epoch(self, chat_id: str, epoch: int, names: dict) -> None:
        bundle = self.keystore.load(self.user)
        pubs = {}
        for name in names:
            acc = self.directory.get(name)
            if acc and acc.keys.agree_pub:
                pubs[name] = acc.keys.agree_pub

        def build(current):
            # Repeatable over the fresh provider document: only recipients
            # still missing are added, so a concurrent wrap is kept and an
            # existing one is never redone. The key comes from my own wrap
            # in this exact document, not from a cache.
            if (not isinstance(current, dict) or current.get("epoch") != epoch
                    or not isinstance(current.get("wrapped"), dict)):
                return None
            wrapped = current["wrapped"]
            missing = {name: pub for name, pub in pubs.items() if name not in wrapped}
            mine = wrapped.get(self.user)
            if not missing or bundle is None or not isinstance(mine, dict):
                return None
            try:
                key = crypto.unwrap_key_with(bundle, mine)
            except (crypto.CryptoFail, TypeError, ValueError, KeyError):
                return None
            doc = copy.deepcopy(current)
            for name, pub in missing.items():
                doc["wrapped"][name] = crypto.wrap_key_for(pub, key)
            return doc

        document, changed = self.tx.update_doc(P.keys(chat_id, epoch), build)
        if changed:
            with self._cache_lock:
                self._published[(chat_id, epoch)] = copy.deepcopy(document)

    def recover(self, snapshot) -> int:
        """Finish this user's interrupted key work under a bound writer.

        Settles epoch creates whose replies were lost and resumes history
        re-wraps against a fresh ``snapshot(chat_id)``. Returns the number of
        items finished; a no-op without a bound writer.
        """
        if not self._typed():
            return 0
        done = self._settle_epochs("chats/", skip_failures=True)
        if self.store is None:
            return done
        after = 0
        while True:
            rows = self.store.key_rewraps_pending(after_id=after)
            for row in rows:
                try:
                    snap = snapshot(row.chat_id)
                except NotAMember:
                    self.store.key_rewrap_finish(row.id)
                else:
                    self._resume_rewrap(row, snap)
                done += 1
            if len(rows) < 64:
                return done
            after = rows[-1].id
