"""Crash-safe ownership for outbound message attachments.

The message outbox owns a small manifest; sealed bytes stay in a local spool
until that outbox row is acknowledged. Remote writes therefore use stable blob
ids across retries, and a process death after upload leaves a durable owner that
can finish the message instead of an unreferenced ciphertext blob.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ValidationError
from ..core.timekit import new_id
from .paths import P

__all__ = ["AttachmentDelivery", "PreparedAttachment", "attachment_path"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
SPOOL_MAX_AGE_S = 7 * 86400.0
SPOOL_MAX_BYTES = 2 * 1024 * 1024 * 1024
SPOOL_MAX_FILES = 200


@dataclass(frozen=True)
class PreparedAttachment:
    chat_id: str
    manifest: dict

    @property
    def record(self) -> dict:
        return dict(self.manifest["record"])


def _safe_name(name: str) -> str:
    value = str(name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[^\w.\- ()\[\]]", "_", value).strip() or "file"
    return value[:120]


def attachment_path(chat_id: str, blob_id: str) -> str:
    """Return the only valid remote path for a stable attachment id."""
    blob_id = str(blob_id or "")
    if not _ID_RE.fullmatch(blob_id):
        raise ValidationError("malformed attachment blob id")
    return P.file(chat_id, blob_id)


class AttachmentDelivery:
    """Prepare, upload and clean attachment manifests for one local Store."""

    def __init__(self, home: Path, store, tx, sealer) -> None:
        self.store = store
        self.tx = tx
        self.sealer = sealer
        self.root = Path(home) / "artifacts" / "outbox" / store.path.stem
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune_unreferenced()

    def prepare(self, chat_id: str, name: str, raw: bytes) -> PreparedAttachment:
        if not isinstance(raw, bytes) or not raw:
            raise ValidationError("empty attachment")
        name = _safe_name(name)
        dot = name.rfind(".")
        suffix = name[dot:][:12].lower() if dot > 0 else ""
        blob_id = new_id("f") + suffix
        sealed = self.sealer.seal_blob(chat_id, blob_id, raw)
        cap = int(getattr(self.tx, "max_upload_bytes", 0) or 0)
        if cap and len(sealed) > cap:
            raise ValidationError("file exceeds the storage limit after encryption")
        spool = self._spool(blob_id)
        tmp = spool.with_name(f".{spool.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(sealed)
            tmp.chmod(0o600)
            os.replace(tmp, spool)
        finally:
            tmp.unlink(missing_ok=True)
        record = {
            "id": blob_id,
            "name": name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        manifest = {
            "v": 1,
            "blob_id": blob_id,
            "sealed_bytes": len(sealed),
            "sealed_sha256": hashlib.sha256(sealed).hexdigest(),
            "record": record,
        }
        return PreparedAttachment(chat_id=chat_id, manifest=manifest)

    def upload(self, chat_id: str, manifest: dict) -> None:
        item = self._validate(chat_id, manifest)
        spool = self._spool(item["blob_id"])
        try:
            sealed = spool.read_bytes()
        except OSError:
            # Cleanup runs only after outbox acknowledgement, so this branch is
            # exceptional. A verified remote copy can still complete a retry.
            sealed = self.tx.get_blob(attachment_path(chat_id, item["blob_id"]))
            if sealed is None:
                raise ValidationError("attachment spool is missing")
        if (len(sealed) != item["sealed_bytes"]
                or hashlib.sha256(sealed).hexdigest() != item["sealed_sha256"]):
            raise ValidationError("attachment spool failed verification")
        self.tx.put_blob(attachment_path(chat_id, item["blob_id"]), sealed)

    def cancel(self, prepared: list[PreparedAttachment]) -> None:
        for item in prepared or []:
            if not isinstance(item, PreparedAttachment):
                continue
            with contextlib.suppress(OSError, ValidationError):
                manifest = self._validate(item.chat_id, item.manifest)
                self._spool(manifest["blob_id"]).unlink(missing_ok=True)

    def cleanup_payload(self, payload: dict) -> None:
        for manifest in self.manifests(payload):
            blob_id = str(manifest.get("blob_id") or "")
            if _ID_RE.fullmatch(blob_id):
                with contextlib.suppress(OSError):
                    self._spool(blob_id).unlink(missing_ok=True)

    def local_sealed(self, blob_id: str) -> bytes | None:
        """Sender-side optimistic reads while the durable row is still pending."""
        try:
            return self._spool(blob_id).read_bytes()
        except (OSError, ValidationError):
            return None

    @staticmethod
    def manifests(payload: dict) -> list[dict]:
        if not isinstance(payload, dict) or not isinstance(payload.get("envelope"), dict):
            return []
        return [m for m in (payload.get("attachments") or []) if isinstance(m, dict)]

    @staticmethod
    def envelope(payload: dict) -> dict:
        if isinstance(payload, dict) and isinstance(payload.get("envelope"), dict):
            return payload["envelope"]
        return payload  # legacy rows stored the envelope directly

    def prune_unreferenced(self) -> int:
        active = set()
        for payload in self.store.outbox_payloads():
            for manifest in self.manifests(payload):
                blob_id = str(manifest.get("blob_id") or "")
                if _ID_RE.fullmatch(blob_id):
                    active.add(blob_id)
        now = time.time()
        candidates: list[tuple[Path, int, float]] = []
        for path in self.root.iterdir():
            try:
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_file() and path.name not in active:
                    stat = path.stat()
                    candidates.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
        kept_bytes = 0
        kept_files = 0
        pruned = 0
        for path, size, mtime in sorted(candidates, key=lambda x: x[2], reverse=True):
            expired = mtime < now - SPOOL_MAX_AGE_S
            over = kept_files >= SPOOL_MAX_FILES or kept_bytes + size > SPOOL_MAX_BYTES
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

    def _spool(self, blob_id: str) -> Path:
        if not _ID_RE.fullmatch(str(blob_id or "")):
            raise ValidationError("malformed attachment blob id")
        return self.root / blob_id

    def _validate(self, chat_id: str, manifest: dict) -> dict:
        if not isinstance(manifest, dict) or manifest.get("v") != 1:
            raise ValidationError("malformed attachment manifest")
        blob_id = str(manifest.get("blob_id") or "")
        record = manifest.get("record")
        if not _ID_RE.fullmatch(blob_id) or not isinstance(record, dict):
            raise ValidationError("malformed attachment manifest")
        if record.get("id") != blob_id or not _SHA_RE.fullmatch(str(record.get("sha256") or "")):
            raise ValidationError("attachment record does not match its manifest")
        try:
            plain_bytes = int(record.get("bytes", -1))
            sealed_bytes = int(manifest.get("sealed_bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValidationError("malformed attachment sizes") from exc
        if plain_bytes < 0 or sealed_bytes <= 0:
            raise ValidationError("malformed attachment sizes")
        sealed_sha = str(manifest.get("sealed_sha256") or "")
        if not _SHA_RE.fullmatch(sealed_sha):
            raise ValidationError("malformed sealed attachment hash")
        # Deriving the remote path here keeps a tampered outbox payload from
        # choosing an arbitrary transport destination.
        attachment_path(chat_id, blob_id)
        return {
            **manifest,
            "blob_id": blob_id,
            "sealed_bytes": sealed_bytes,
            "sealed_sha256": sealed_sha,
            "record": dict(record),
        }
