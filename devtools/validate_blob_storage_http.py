#!/usr/bin/env python3
"""Exercise the inactive typed blob manifest through real Supabase HTTP APIs.

The script never prints passwords, tokens or the publishable key.  Run
``identities`` first, prepare/seal a disposable root for the returned user IDs,
then run ``acceptance`` with that root and protocol epoch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from storage3.exceptions import StorageApiError, StorageException
from supabase import Client, create_client


BUCKET = "ab-mesh"
OBJECT_NOT_VISIBLE = frozenset({(404, "not_found")})
OBJECT_ALREADY_EXISTS = frozenset({(409, "Duplicate")})
OBJECT_WRITE_FORBIDDEN = frozenset({(403, "Unauthorized")})


class GateFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    client: Client
    user_id: UUID


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise GateFailure(f"required environment variable {name} is missing")
    return value


def _principal(url: str, key: str, email_env: str, password_env: str) -> Principal:
    client = create_client(url, key)
    response = client.auth.sign_in_with_password({
        "email": _required(email_env),
        "password": _required(password_env),
    })
    if response.user is None:
        raise GateFailure(f"{email_env} did not produce an authenticated user")
    return Principal(client=client, user_id=UUID(str(response.user.id)))


def _object_row(bucket: Any, directory: str, blob: str) -> dict[str, Any]:
    rows = bucket.list(directory, {"limit": 100, "search": blob})
    exact = [row for row in rows if row.get("name") == blob]
    if len(exact) != 1:
        raise GateFailure(f"expected one exact staged object, found {len(exact)}")
    try:
        UUID(str(exact[0]["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise GateFailure("Storage listing omitted a UUID object id") from exc
    return exact[0]


def _expect_storage_denial(
    action: Any,
    label: str,
    allowed: frozenset[tuple[int, str]],
) -> dict[str, Any]:
    try:
        action()
    except StorageApiError as exc:
        try:
            failure = (int(exc.status), str(exc.code))
        except (TypeError, ValueError) as malformed:
            raise GateFailure(
                f"{label} returned malformed storage failure evidence"
            ) from malformed
        if failure not in allowed:
            raise GateFailure(
                f"{label} failed with unexpected storage status/code"
            ) from exc
        return {"status": failure[0], "code": failure[1]}
    except StorageException as exc:
        raise GateFailure(
            f"{label} failed with an unclassified storage exception"
        ) from exc
    raise GateFailure(f"{label} unexpectedly succeeded")


def _identities(owner: Principal, reader: Principal) -> dict[str, Any]:
    return {
        "mode": "identities",
        "owner_id": str(owner.user_id),
        "reader_id": str(reader.user_id),
    }


def _acceptance(
    owner: Principal,
    reader: Principal,
    *,
    root: str,
    protocol_epoch: UUID,
    chat: str,
) -> dict[str, Any]:
    blob = "http-" + uuid4().hex
    directory = f"{root}/blob-stage/{chat}"
    path = f"{directory}/{blob}"
    moved_path = path + "-moved"
    payload = secrets.token_bytes(4096)
    sealed_sha256 = hashlib.sha256(payload).hexdigest()
    manifest_digest = hashlib.sha256(
        json.dumps({
            "blob_id": blob,
            "chat_id": chat,
            "sealed_bytes": len(payload),
            "sealed_sha256": sealed_sha256,
        }, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    client_digest = hashlib.sha256(
        ("blob-manifest:" + manifest_digest).encode()
    ).hexdigest()
    operation_id = uuid4()
    owner_bucket = owner.client.storage.from_(BUCKET)
    reader_bucket = reader.client.storage.from_(BUCKET)
    uploaded = False
    cleanup_rows = 0
    try:
        owner_bucket.upload(path, payload, {
            "cache-control": "3600",
            "content-type": "application/octet-stream",
            "upsert": "false",
        })
        uploaded = True
        row = _object_row(owner_bucket, directory, blob)
        object_id = UUID(str(row["id"]))
        recovered = owner_bucket.download(path)
        if recovered != payload:
            raise GateFailure("owner round-trip bytes changed")
        if hashlib.sha256(recovered).hexdigest() != sealed_sha256:
            raise GateFailure("owner round-trip digest changed")

        reader_before = _expect_storage_denial(
            lambda: reader_bucket.download(path),
            "room-member read before manifest publication",
            OBJECT_NOT_VISIBLE,
        )
        duplicate = _expect_storage_denial(
            lambda: owner_bucket.upload(path, payload, {
                "content-type": "application/octet-stream",
                "upsert": "false",
            }),
            "create-only response-loss retry",
            OBJECT_ALREADY_EXISTS,
        )
        recovered_row = _object_row(owner_bucket, directory, blob)
        if recovered_row.get("id") != str(object_id):
            raise GateFailure("create-only retry recovery changed object id")

        receipt = owner.client.rpc("ab_mutation_blob_manifest", {
            "p_root": root,
            "p_protocol_epoch": str(protocol_epoch),
            "p_operation_id": str(operation_id),
            "p_payload_digest": client_digest,
            "p_chat": chat,
            "p_blob": blob,
            "p_object_id": str(object_id),
            "p_sealed_bytes": len(payload),
            "p_sealed_sha256": sealed_sha256,
            "p_manifest_digest": manifest_digest,
        }).execute().data
        replay = owner.client.rpc("ab_mutation_blob_manifest", {
            "p_root": root,
            "p_protocol_epoch": str(protocol_epoch),
            "p_operation_id": str(operation_id),
            "p_payload_digest": client_digest,
            "p_chat": chat,
            "p_blob": blob,
            "p_object_id": str(object_id),
            "p_sealed_bytes": len(payload),
            "p_sealed_sha256": sealed_sha256,
            "p_manifest_digest": manifest_digest,
        }).execute().data
        if receipt != replay:
            raise GateFailure("HTTP RPC exact replay changed")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if result != {
            "blob_id": blob,
            "chat_id": chat,
            "object_id": str(object_id),
            "sealed_bytes": len(payload),
        }:
            raise GateFailure("HTTP RPC receipt result changed")

        reader_after_bytes = reader_bucket.download(path)
        if reader_after_bytes != payload:
            raise GateFailure("room-member download changed published bytes")
        upsert = _expect_storage_denial(
            lambda: owner_bucket.upload(path, payload, {
                "content-type": "application/octet-stream",
                "upsert": "true",
            }),
            "staged-object upsert",
            OBJECT_WRITE_FORBIDDEN,
        )
        move = _expect_storage_denial(
            lambda: owner_bucket.move(path, moved_path),
            "staged-object in-place move",
            OBJECT_NOT_VISIBLE,
        )
        if _object_row(owner_bucket, directory, blob).get("id") != str(object_id):
            raise GateFailure("denied overwrite or move changed object id")

        return {
            "mode": "acceptance",
            "result": "passed",
            "root": root,
            "chat": chat,
            "blob_id": blob,
            "object_id": str(object_id),
            "sealed_bytes": len(payload),
            "sealed_sha256": sealed_sha256,
            "owner_id": str(owner.user_id),
            "reader_id": str(reader.user_id),
            "owner_round_trip": True,
            "reader_prepublication_denial": reader_before,
            "create_only_retry_denial": duplicate,
            "exact_object_id_recovered": True,
            "manifest_receipt_replay": True,
            "reader_published_round_trip": True,
            "upsert_denial": upsert,
            "move_denial": move,
            "cleanup_via_storage_api": True,
        }
    finally:
        if uploaded:
            removed = owner_bucket.remove([path])
            cleanup_rows = len(removed)
            remaining = owner_bucket.list(directory, {"limit": 100, "search": blob})
            if cleanup_rows != 1 or any(row.get("name") == blob for row in remaining):
                raise GateFailure("Storage API cleanup did not remove exact object")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identities", "acceptance"))
    parser.add_argument("--root")
    parser.add_argument("--protocol-epoch", type=UUID)
    parser.add_argument("--chat", default="room")
    args = parser.parse_args()

    url = _required("AGENTBRIDGE_SCRATCH_SUPABASE_URL")
    key = _required("AGENTBRIDGE_SCRATCH_SUPABASE_PUBLISHABLE_KEY")
    owner = _principal(
        url, key, "AGENTBRIDGE_SCRATCH_OWNER_EMAIL",
        "AGENTBRIDGE_SCRATCH_OWNER_PASSWORD",
    )
    reader = _principal(
        url, key, "AGENTBRIDGE_SCRATCH_READER_EMAIL",
        "AGENTBRIDGE_SCRATCH_READER_PASSWORD",
    )
    if args.mode == "identities":
        result = _identities(owner, reader)
    else:
        if not args.root or args.protocol_epoch is None:
            parser.error("acceptance requires --root and --protocol-epoch")
        result = _acceptance(
            owner, reader, root=args.root,
            protocol_epoch=args.protocol_epoch, chat=args.chat,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}))
        raise SystemExit(1) from exc
