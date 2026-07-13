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
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import crypto
from ..core.config import DEFAULT_HOME
from ..core.errors import CryptoError
from ..core.models import ChatSnapshot
from ..core.timekit import next_ns, utcnow_iso
from ..crypto import dpapi
from ..transport.base import Transport
from .directory import Directory
from .paths import P

__all__ = ["KeyStore", "ChatKeyService"]


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

    def save(self, name: str, bundle: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        wrapped = dpapi.protect(bundle) if dpapi.available() else None
        text = (
            self._WRAPPED + crypto.b64e(wrapped) if wrapped is not None
            else crypto.b64e(bundle)  # plain fallback — never lose a key
        )
        # atomic replace: load() upgrades legacy files in place, so a
        # concurrent reader must never see a half-written key file
        tmp = self._path(name).with_suffix(".key.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path(name))

    def load(self, name: str) -> bytes | None:
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
        if bundle and dpapi.available():
            self.save(name, bundle)  # transparent one-time upgrade to wrapped
        return bundle

    def forget(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class ChatKeyService:
    def __init__(
        self, tx: Transport, directory: Directory, keystore: KeyStore, user: str
    ) -> None:
        self.tx = tx
        self.directory = directory
        self.keystore = keystore
        self.user = user
        self._cache: dict[tuple[str, int], bytes] = {}  # (chat, epoch) -> key

    # ------------------------------------------------------------- reading
    def epochs(self, chat_id: str) -> list[tuple[int, dict]]:
        """Every epoch doc of a chat, oldest→newest by id (id = ns ordinal)."""
        out = []
        for path in self.tx.list_docs(f"chats/{chat_id}/keys"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and isinstance(doc.get("epoch"), int):
                out.append((doc["epoch"], doc))
        return sorted(out)

    def latest(self, chat_id: str) -> tuple[int, dict] | None:
        eps = self.epochs(chat_id)
        return eps[-1] if eps else None

    def my_key(self, chat_id: str, epoch: int) -> bytes | None:
        cached = self._cache.get((chat_id, epoch))
        if cached is not None:
            return cached
        doc = self.tx.get_doc(P.keys(chat_id, epoch))
        wrapped = (doc or {}).get("wrapped", {}).get(self.user)
        bundle = self.keystore.load(self.user)
        if not wrapped or bundle is None:
            return None
        try:
            key = crypto.unwrap_key_with(bundle, wrapped)
        except crypto.CryptoFail:
            return None
        self._cache[(chat_id, epoch)] = key
        return key

    # ------------------------------------------------------------- rotation
    def _wrap_for(self, members: list[str], key: bytes) -> dict:
        wrapped = {}
        for name in members:
            acc = self.directory.get(name)
            if acc and acc.keys.agree_pub:
                wrapped[name] = crypto.wrap_key_for(acc.keys.agree_pub, key)
        return wrapped

    def rotate(self, chat_id: str, members: list[str]) -> tuple[int, bytes]:
        """New epoch wrapped for exactly ``members``. The actor must be one
        of them (you can't mint keys for rooms you're not in)."""
        if self.user not in members:
            raise CryptoError("cannot rotate keys for a chat you're not in")
        epoch = next_ns()
        key = crypto.new_chat_key()
        self.tx.put_doc(
            P.keys(chat_id, epoch),
            {"epoch": epoch, "by": self.user, "created": utcnow_iso(),
             "wrapped": self._wrap_for(members, key)},
        )
        self._cache[(chat_id, epoch)] = key
        return epoch, key

    def ensure(self, chat_id: str, snap: ChatSnapshot) -> tuple[int, bytes]:
        """The self-heal: called before every seal. Rotates when there is no
        epoch yet, when the newest epoch's member set drifted from the
        snapshot (someone removed/left/was added-by-a-clobbered-writer), or
        when I can't unwrap my copy (I'm newer than the wrap)."""
        current = self.latest(chat_id)
        members = sorted(snap.members)
        if current is not None:
            epoch, doc = current
            if sorted(doc.get("wrapped", {})) == members:
                key = self.my_key(chat_id, epoch)
                if key is not None:
                    return epoch, key
        return self.rotate(chat_id, members)

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
