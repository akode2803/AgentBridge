"""Serialized raw account/meta/lifecycle inputs; never a membership authority memo."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass

from ..store import document_observation as documents, lifecycle_inputs
from ..transport import authority_observation as observation
from ..transport.mirror_observation import MirrorExpectedPosition


class AuthoritySourceUnavailable(RuntimeError):
    pass


class AuthorityReadThroughRequired(AuthoritySourceUnavailable):
    def __init__(self, paths):
        self.paths = tuple(paths)
        super().__init__('authority_readthrough_required')


@dataclass(frozen=True)
class AuthoritySourceReceipt:
    chat_id: str
    position: documents.DocumentPosition
    mirror: MirrorExpectedPosition


@dataclass(frozen=True)
class AuthorityInputs:
    receipt: AuthoritySourceReceipt
    policy: observation.LookupPolicy
    documents: documents.DocumentObservation


def _observe(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except observation.AuthorityObservationUnavailable as exc:
        raise AuthoritySourceUnavailable(str(exc)) from exc


def _source(chat, mirror):
    raw = json.dumps([chat, mirror.root_identity, mirror.cache_identity])
    return 'authority-v1:' + hashlib.sha256(raw.encode()).hexdigest()


def _copy_receipt(transport, store, receipt):
    _observe(observation._owner, transport)
    return _copy_source_receipt(store, receipt)


def _copy_source_receipt(store, receipt):
    if type(receipt) is not AuthoritySourceReceipt:
        raise TypeError('expected AuthoritySourceReceipt')
    chat, position, mirror = receipt.chat_id, receipt.position, receipt.mirror
    chat = observation._part(chat)
    if type(mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid authority mirror')
    mirror = MirrorExpectedPosition(mirror.root_identity, mirror.cache_identity,
                                    mirror.instance_nonce, mirror.revision)
    position = documents._validate_expected(position, store.path)
    if position.source_id != _source(chat, mirror):
        raise ValueError('invalid authority source binding')
    return AuthoritySourceReceipt(chat, position, mirror)


def _match_mirror(transport, receipt):
    _observe(observation.capture_lookup_policy, transport, receipt.chat_id, expected=receipt.mirror)


def publish_authority_source(transport, store, chat_id, **limits):
    """Explicit background work, never called from a foreground input read.

    Oversize or changed capture publishes nothing. Existing receipts remain
    constrained by their original live mirror; failed publication stays pending.
    """
    captured = _observe(observation.capture_authority_documents, transport, chat_id, **limits)
    current = store.capture_document_position(_source(captured.chat_id, captured.mirror))
    pending = store.invalidate_document_observation(current)
    draft = AuthoritySourceReceipt(captured.chat_id, pending, captured.mirror)
    _match_mirror(transport, draft)
    payloads = {path: json.loads(raw) for path, raw in captured.records if raw is not None}
    position = store.publish_document_batch(
        pending, payloads, cursor=pending.cursor, full=True, retain_tombstones=False,
        max_documents=limits.get('max_documents', 20_000),
        max_bytes=limits.get('max_bytes', observation.MAX_BYTES),
    )
    result = AuthoritySourceReceipt(captured.chat_id, position, captured.mirror)
    _match_mirror(transport, result)
    return result


def capture_authority_inputs(transport, store, receipt, account_names=(), *, max_bytes=4 * 1024 * 1024):
    """Size-preflight exact meta/accounts and current canonical miss policy.

    Online unknowns return work via an exception; the owner schedules canonical
    get_doc outside foreground/pin/SQLite locks and retries from a new cut.
    This function never returns a fabricated absent account for such a miss.
    """
    receipt = _copy_receipt(transport, store, receipt)
    observation._limit(max_bytes, 4 * 1024 * 1024)
    policy = _observe(observation.capture_lookup_policy, transport, receipt.chat_id, account_names,
                                               expected=receipt.mirror)
    work = tuple(path for path, mode in policy.accounts if mode == 'online_readthrough_required')
    if work:
        raise AuthorityReadThroughRequired(work)
    paths = (policy.meta[0],) + tuple(path for path, _ in policy.accounts)
    captured = store.capture_selected_documents(receipt.position, paths,
                                                max_documents=129, max_bytes=max_bytes)
    modes = (policy.meta,) + policy.accounts
    for record, (path, mode) in zip(captured.records, modes):
        if record.path != path or (not record.deleted) != (mode == 'present'):
            raise AuthoritySourceUnavailable('authority_source_policy_mismatch')
    if store.capture_document_position(receipt.position.source_id) != receipt.position:
        raise AuthoritySourceUnavailable('authority_source_changed')
    if not _observe(observation.matches_lookup_policy, transport, policy):
        raise AuthoritySourceUnavailable('authority_lookup_policy_changed')
    return AuthorityInputs(receipt, policy, captured)


def capture_authority_subject(transport, store, receipt, subject, **limits):
    """Use the SAME authority source for lifecycle dependencies, not a second copy."""
    receipt = _copy_receipt(transport, store, receipt)
    _match_mirror(transport, receipt)
    conn = documents._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        result = lifecycle_inputs.capture_subject(conn, store.path, receipt.position, subject, **limits)
    finally:
        conn.close()
    if store.capture_document_position(receipt.position.source_id) != receipt.position:
        raise AuthoritySourceUnavailable('authority_source_changed')
    _match_mirror(transport, receipt)
    return result


def matches_inputs_in_transaction(conn, store, expected, *, max_bytes=4 * 1024 * 1024):
    """Compare bounded raw rows within the caller's final Store transaction.

    Live mirror/policy, pins and membership remain separate owner fences. This
    helper neither acquires those locks nor parses/authorizes document payloads.
    """
    if not conn.in_transaction:
        raise sqlite3.OperationalError('authority matching requires an active transaction')
    actual = conn.execute('PRAGMA database_list').fetchone()[2]
    if not actual or Path(actual).resolve() != Path(store.path).resolve():
        raise ValueError('authority connection belongs to another database')
    observation._limit(max_bytes, 4 * 1024 * 1024)
    if type(expected) is not AuthorityInputs:
        raise TypeError('expected AuthorityInputs')
    receipt, policy, captured = expected.receipt, expected.policy, expected.documents
    receipt = _copy_source_receipt(store, receipt)
    policy = observation._copy_lookup_policy(policy)
    if policy.chat_id != receipt.chat_id or policy.mirror != receipt.mirror:
        raise ValueError('authority policy binding mismatch')
    if type(captured) is not documents.DocumentObservation:
        raise TypeError('expected DocumentObservation')
    position, records = captured.position, captured.records
    position = documents._validate_expected(position, store.path)
    modes = (policy.meta,) + policy.accounts
    if position != receipt.position or type(records) is not tuple or len(records) != len(modes):
        raise ValueError('authority document binding mismatch')
    copied = []
    used = 0
    for record, (path, mode) in zip(records, modes):
        if type(record) is not documents.SerializedDocumentRecord:
            raise ValueError('invalid authority record')
        name, payload, deleted = record.path, record.payload_json, record.deleted
        if (type(name) is not str or name != path or type(deleted) is not bool
                or mode == 'online_readthrough_required'
                or deleted != (mode != 'present')
                or (deleted and payload is not None)
                or (not deleted and type(payload) is not str)):
            raise ValueError('invalid authority record policy')
        # Cheap character lower bound precedes UTF-8 allocation.
        if len(name) + (len(payload) if payload is not None else 0) > max_bytes - used:
            raise OverflowError('authority comparison byte budget')
        used += len(name.encode()) + (len(payload.encode()) if payload is not None else 0)
        if used > max_bytes:
            raise OverflowError('authority comparison byte budget')
        copied.append(documents.SerializedDocumentRecord(name, payload, deleted))
    wanted = documents.DocumentObservation(position, tuple(copied))
    if documents._capture_position(conn, store.path, position.source_id) != position:
        return False
    if not position.initialized:
        return False
    return documents._capture_selected(conn, store.path, position,
        tuple(path for path, _mode in modes), max_documents=129, max_bytes=max_bytes) == wanted
