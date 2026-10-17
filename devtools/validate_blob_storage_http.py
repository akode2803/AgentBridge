#!/usr/bin/env python3
"""Exercise the inactive typed blob manifest through real Supabase HTTP APIs.

The script never prints passwords, tokens or the publishable key.  Run
``identities`` first, prepare/seal a disposable root for the returned user IDs,
then run ``acceptance`` with that root and protocol epoch.

``listing`` checks what the janitor's purge depends on: the production
``SupabaseTransport.list_blobs`` walk over real Storage HTTP (past 100 names,
into nested folders), RLS-filtered removes answering an empty success, and
batched removes of 100 keys. It needs a root where both users are members of
``--chat`` (sealed or not; Storage is outside the seal), and cleans up after
itself.
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


LISTING_FILES = 150
LISTING_STAGED = 3


def _janitor_listing(bucket: Any, root: str, chat: str) -> list[str]:
    """The janitor's three folders, walked by the production list_blobs."""
    from types import SimpleNamespace

    from agentbridge.transport.supabase import SupabaseTransport

    transport = SimpleNamespace(root=root, _retry=lambda fn: fn(),
                                _store=lambda: bucket)
    found: set[str] = set()
    for folder in (f"chats/{chat}", f"chats/{chat}/files", f"blob-stage/{chat}"):
        found.update(SupabaseTransport.list_blobs(transport, folder))
    return sorted(found)


def _remove_batched(bucket: Any, root: str, paths: list[str]) -> list[int]:
    """Remove in calls of 100 keys, as the bound janitor does; rows per call."""
    keys = [f"{root}/{path}" for path in paths]
    return [len(bucket.remove(keys[at:at + 100]))
            for at in range(0, len(keys), 100)]


def _listing(
    owner: Principal,
    reader: Principal,
    *,
    root: str,
    chat: str,
) -> dict[str, Any]:
    owner_bucket = owner.client.storage.from_(BUCKET)
    reader_bucket = reader.client.storage.from_(BUCKET)
    chat_paths = sorted(
        [f"chats/{chat}/files/f{i:03d}" for i in range(LISTING_FILES)]
        + [f"chats/{chat}/avatar.png", f"chats/{chat}/odd/deep/x"])
    staged_paths = sorted(f"blob-stage/{chat}/{uuid4().hex}"
                          for _ in range(LISTING_STAGED))
    options = {"content-type": "application/octet-stream", "upsert": "false"}
    uploaded_chat: list[str] = []
    uploaded_staged: list[str] = []
    try:
        for path in chat_paths:
            owner_bucket.upload(f"{root}/{path}", secrets.token_bytes(16), options)
            uploaded_chat.append(path)
        for path in staged_paths:
            reader_bucket.upload(f"{root}/{path}", secrets.token_bytes(16), options)
            uploaded_staged.append(path)

        owner_sees = _janitor_listing(owner_bucket, root, chat)
        reader_sees = _janitor_listing(reader_bucket, root, chat)
        if owner_sees != chat_paths:
            raise GateFailure("owner listing differs from the uploaded room files")
        if reader_sees != sorted(chat_paths + staged_paths):
            raise GateFailure("uploader listing differs from its visible objects")

        parent = f"{root}/chats/{chat}/files"
        exact = [row for row in owner_bucket.list(
            parent, {"limit": 100, "offset": 0, "search": "f105"})
            if row.get("name") == "f105" and row.get("id") is not None]
        if len(exact) != 1:
            raise GateFailure("exact-name search missed a file past the first page")

        # Another member's unpublished staged objects are invisible to the
        # owner, so its remove must be an empty success that changes nothing.
        hidden_remove = _remove_batched(owner_bucket, root, staged_paths)
        if hidden_remove != [0]:
            raise GateFailure("RLS-hidden remove did not answer an empty success")
        if _janitor_listing(reader_bucket, root, chat) != reader_sees:
            raise GateFailure("RLS-hidden remove changed the uploader's objects")

        chat_remove = _remove_batched(owner_bucket, root, chat_paths)
        if chat_remove != [100, len(chat_paths) - 100]:
            raise GateFailure("batched remove did not report every removed key")
        uploaded_chat = []
        staged_remove = _remove_batched(reader_bucket, root, staged_paths)
        if staged_remove != [LISTING_STAGED]:
            raise GateFailure("uploader remove did not report its staged keys")
        uploaded_staged = []
        if _janitor_listing(owner_bucket, root, chat) or _janitor_listing(
                reader_bucket, root, chat):
            raise GateFailure("listing is not empty after the batched removes")

        return {
            "mode": "listing",
            "result": "passed",
            "root": root,
            "chat": chat,
            "owner_id": str(owner.user_id),
            "reader_id": str(reader.user_id),
            "room_objects": len(chat_paths),
            "staged_objects": len(staged_paths),
            "owner_listed": len(owner_sees),
            "reader_listed": len(reader_sees),
            "exact_search_past_first_page": True,
            "rls_hidden_remove_rows": hidden_remove,
            "batched_remove_rows": chat_remove,
            "staged_remove_rows": staged_remove,
            "empty_after_remove": True,
        }
    finally:
        if uploaded_chat:
            _remove_batched(owner_bucket, root, uploaded_chat)
        if uploaded_staged:
            _remove_batched(reader_bucket, root, uploaded_staged)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identities", "acceptance", "listing"))
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
    elif args.mode == "listing":
        if not args.root:
            parser.error("listing requires --root")
        result = _listing(owner, reader, root=args.root, chat=args.chat)
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
