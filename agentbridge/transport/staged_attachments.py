"""Inactive two-phase Storage publication for journal-bound chat attachments.

The durable outbox owns the blob ID and sealed digest. Storage's create-only
object ID is verified before a typed manifest operation is prepared. A retained
operation always wins over fresh Storage discovery on replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID, uuid5

from ..core.errors import TransportError, ValidationError
from .application_mutations import ApplicationMutationWriter
from .outbox_mutations import _payload as validated_outbox_payload
from .supabase import BUCKET


_MANIFEST_NAMESPACE = UUID("513ff869-a730-4c24-bdef-e046f3a0b8a0")
_STORAGE_SIZE = re.compile(r"[0-9]{1,8}\Z")


def _digest(chat_id: str, manifest: dict) -> str:
    material = ["blob_manifest", chat_id, manifest["blob_id"],
                manifest["sealed_bytes"], manifest["sealed_sha256"],
                manifest["record"]]
    try:
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValidationError("invalid staged attachment manifest") from None
    return hashlib.sha256(encoded).hexdigest()


class StagedAttachmentPublisher:
    """Publish one exact staged object through a bound typed mutation writer."""

    def __init__(self, writer: ApplicationMutationWriter) -> None:
        if type(writer) is not ApplicationMutationWriter:
            raise TypeError("invalid staged attachment writer")
        self.writer = writer
        self.rpc = writer.driver.rpc
        self.rpc._check_identity()

    def _prepare_storage(self) -> None:
        """Only fresh publication needs Storage; receipt replay does not."""
        self.client = self.rpc._client
        self.storage = self.client.storage
        self.bucket = self.storage.from_(BUCKET)
        self.http_client = getattr(self.bucket, "_client", None)
        self._check_identity()

    def _check_identity(self) -> None:
        self.rpc._check_identity()
        headers = getattr(self.bucket, "_headers", None)
        if headers is None:
            headers = getattr(self.bucket, "headers", None)
        http_client = getattr(self.bucket, "_client", None)
        http_headers = getattr(http_client, "headers", None)
        if headers is None:
            headers = http_headers
        base_url = getattr(self.bucket, "_base_url", None)
        if base_url is None:
            base_url = getattr(self.bucket, "base_url", None)
        try:
            authorized = (headers.get("Authorization")
                          == f"Bearer {self.rpc._access_token}"
                          and (http_headers is None or http_headers.get("Authorization")
                               == f"Bearer {self.rpc._access_token}"))
        except (AttributeError, TypeError):
            authorized = False
        if (self.client.storage is not self.storage
                or http_client is not self.http_client
                or not authorized
                or str(base_url).rstrip("/") != self.rpc.endpoint + "/storage/v1"):
            raise ValidationError("staged attachment Storage identity changed")

    def _call(self, operation):
        self._check_identity()
        try:
            value = operation()
        except Exception:
            self._check_identity()
            raise TransportError("staged attachment Storage request failed") from None
        self._check_identity()
        return value

    def _object(self, directory: str, blob_id: str):
        rows = self._call(lambda: self.bucket.list(
            directory, {"limit": 100, "search": blob_id}))
        if type(rows) is not list or len(rows) >= 100:
            raise ValidationError("staged attachment listing is ambiguous")
        exact = [row for row in rows if type(row) is dict and row.get("name") == blob_id]
        if len(exact) > 1:
            raise ValidationError("staged attachment identity is ambiguous")
        if not exact:
            return None
        row = exact[0]
        try:
            object_id = str(UUID(str(row["id"])))
        except (KeyError, TypeError, ValueError):
            raise ValidationError("staged attachment object ID is unavailable") from None
        metadata = row.get("metadata")
        size = metadata.get("size") if type(metadata) is dict else None
        if not _STORAGE_SIZE.fullmatch(str(size)):
            raise ValidationError("staged attachment size is unavailable")
        return object_id, int(size)

    def remove(self, relative_paths: list[str]) -> None:
        """Remove objects as the bound writer's principal, 100 keys a call.

        Receipt-free and idempotent: Storage is outside the seal and a missing
        object is already the goal. Storage answers an RLS-filtered delete
        with an empty success, so callers that must know (the janitor before
        a purge) list the folder again.
        """
        self._prepare_storage()
        keys = [f"{self.writer.root}/{path}" for path in relative_paths]
        for at in range(0, len(keys), 100):
            self._call(lambda chunk=keys[at:at + 100]: self.bucket.remove(chunk))

    def _identity(self, chat_id: str, manifest: dict):
        from ..mesh.attachments import attachment_path

        if (type(manifest) is not dict or manifest.get("v") != 2
                or type(manifest.get("record")) is not dict):
            raise ValidationError("invalid staged attachment manifest")
        blob_id = manifest["blob_id"]
        relative_path = attachment_path(chat_id, blob_id, manifest["record"])
        operation_id = str(uuid5(_MANIFEST_NAMESPACE, json.dumps(
            [self.writer.root, chat_id, blob_id], separators=(",", ":"),
            ensure_ascii=False)))
        manifest_digest = _digest(chat_id, manifest)
        expected = {
            "p_chat": chat_id, "p_blob": blob_id,
            "p_sealed_bytes": manifest["sealed_bytes"],
            "p_sealed_sha256": manifest["sealed_sha256"],
            "p_manifest_digest": manifest_digest,
        }
        return blob_id, relative_path, operation_id, expected

    def recover(self, chat_id: str, manifest: dict) -> bool:
        """Resume an already journaled manifest without fresh Storage access."""
        _blob_id, _path, operation_id, expected = self._identity(chat_id, manifest)
        retained = self.writer.driver.journal.get_operation(operation_id)
        if retained is None:
            return False
        if retained.request.expectation.kind != "blob_manifest":
            raise ValidationError("staged attachment operation changed")
        arguments = json.loads(retained.request.arguments_json)
        if any(arguments.get(key) != value for key, value in expected.items()):
            raise ValidationError("staged attachment manifest changed")
        self.writer.submit("blob_manifest", arguments, operation_id=operation_id)
        return True

    @staticmethod
    def _outbox_leg(store, item, chat_id: str, manifest: dict):
        """Bind this manifest to one exact persisted append outbox row."""
        from ..store.db import OutboxItem

        if (type(item) is not OutboxItem or item.kind != 'append_log'
                or not item.target.startswith(chat_id + '|')
                or not item.target[len(chat_id) + 1:]):
            raise ValidationError('staged attachment outbox owner changed')
        try:
            retained = validated_outbox_payload(item)
            manifests = retained['attachments']
        except (TypeError, ValueError, KeyError):
            raise ValidationError('staged attachment outbox payload changed') from None
        if sum(value == manifest for value in manifests) != 1:
            raise ValidationError('staged attachment outbox manifest changed')
        row = store.outbox_owner_row(item.seq)
        if (row is None or row[1] != item.owner_digest
                or row[2:] != (item.kind, item.target)):
            raise ValidationError('staged attachment outbox owner changed')
        return 'manifest:' + manifest['blob_id'], item.owner_digest, row[0]

    def recover_outbox(self, store, item, chat_id: str, manifest: dict) -> bool:
        """Lookup-only replay of the exact ticket linked to this outbox leg."""
        _blob_id, _path, operation_id, expected = self._identity(chat_id, manifest)
        leg, digest, _state = self._outbox_leg(store, item, chat_id, manifest)
        journal = self.writer.driver.journal
        ticket = journal.get_outbox_leg(store, item.seq, leg, digest)
        if ticket is None:
            if journal.get_operation(operation_id) is not None:
                raise ValidationError('drain pre-consumer manifest before typed outbox activation')
            return False
        retained = journal.get_ticket(ticket).request
        arguments = json.loads(retained.arguments_json)
        if (retained.expectation.kind != 'blob_manifest'
                or set(arguments) != set(expected) | {'p_object_id'}
                or any(arguments.get(key) != value for key, value in expected.items())):
            raise ValidationError('staged attachment manifest changed')
        self.writer.driver.resume(ticket.protocol_epoch, ticket.operation_id)
        return True

    def publish_outbox(self, store, item, chat_id: str,
                       manifest: dict, spool: Path) -> None:
        """Stage only fresh work, then allocate one atomic outbox ticket."""
        blob_id, relative_path, _operation_id, expected = self._identity(chat_id, manifest)
        leg, digest, state = self._outbox_leg(store, item, chat_id, manifest)
        if self.recover_outbox(store, item, chat_id, manifest):
            return
        if state != 'pending':
            raise ValidationError('staged attachment outbox is already sent')
        arguments = self._stage(chat_id, manifest, spool, blob_id, relative_path, expected)
        ticket = self.writer.driver.journal.allocate_outbox_ticket(
            store, item.seq, leg, digest, 'blob_manifest', arguments)
        self.writer.driver.resume(ticket.protocol_epoch, ticket.operation_id)

    def publish(self, chat_id: str, manifest: dict, spool: Path) -> None:
        blob_id, relative_path, operation_id, expected = self._identity(chat_id, manifest)
        if self.recover(chat_id, manifest):
            return

        arguments = self._stage(chat_id, manifest, spool, blob_id, relative_path, expected)
        self.writer.submit('blob_manifest', arguments, operation_id=operation_id)

    def _stage(self, chat_id: str, manifest: dict, spool: Path,
               blob_id: str, relative_path: str, expected: dict) -> dict:

        self._prepare_storage()
        path = f"{self.writer.root}/{relative_path}"
        directory = f"{self.writer.root}/blob-stage/{chat_id}"
        object_row = self._object(directory, blob_id)
        if object_row is None:
            try:
                sealed = spool.read_bytes()
            except OSError:
                raise ValidationError("attachment spool is missing") from None
            if (len(sealed) != manifest["sealed_bytes"]
                    or hashlib.sha256(sealed).hexdigest() != manifest["sealed_sha256"]):
                raise ValidationError("attachment spool failed verification")
            try:
                self._call(lambda: self.bucket.upload(
                    path, sealed,
                    {"content-type": "application/octet-stream", "upsert": "false"}))
            except Exception:
                # A lost create-only response is resolved by the exact listing
                # and byte check below. Other failures remain failures if no
                # object can be proved.
                object_row = self._object(directory, blob_id)
                if object_row is None:
                    raise
        if object_row is None:
            object_row = self._object(directory, blob_id)
        if object_row is None or object_row[1] != manifest["sealed_bytes"]:
            raise ValidationError("staged attachment object is unavailable")
        recovered = self._call(lambda: self.bucket.download(path))
        if (type(recovered) is not bytes
                or len(recovered) != manifest["sealed_bytes"]
                or hashlib.sha256(recovered).hexdigest() != manifest["sealed_sha256"]):
            raise ValidationError("staged attachment bytes changed")
        return {**expected, 'p_object_id': object_row[0]}


__all__ = ["StagedAttachmentPublisher"]
