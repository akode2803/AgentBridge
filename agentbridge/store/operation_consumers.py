"""Bounded durable consumer slots for exact typed mutation tickets.

The first adapter binds outbox rows. This module performs only local SQL and
validation; provider work and consumer callbacks never run under these cuts.
"""
from __future__ import annotations

import re

from ..transport.mutation_protocol import _uuid
from ..transport.receipt_retirement import OperationTicket
from . import local_source

MAX_CONSUMERS = 4096
SCHEMA = ('CREATE TABLE typed_operation_consumers('
          'owner_kind TEXT NOT NULL,owner_path TEXT NOT NULL,owner_incarnation TEXT NOT NULL,'
          'owner_epoch TEXT NOT NULL,owner_seq INTEGER NOT NULL,leg TEXT NOT NULL,digest TEXT NOT NULL,'
          'protocol_epoch TEXT NOT NULL,operation_id TEXT NOT NULL,consumed INTEGER NOT NULL CHECK(consumed IN (0,1)),'
          'PRIMARY KEY(owner_kind,owner_path,owner_incarnation,owner_seq,leg),'
          'UNIQUE(protocol_epoch,operation_id))')
_LEG = re.compile(r'(?:append|manifest:[A-Za-z0-9][A-Za-z0-9._-]{0,179})\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _fail(reason):
    raise local_source.SourceChanged('typed_consumer_' + reason)


def audit_consumers(conn, *, required):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='typed_operation_consumers'").fetchone()
    if not required:
        if sql is not None:
            _fail('unexpected_schema')
        return
    if sql != (SCHEMA,):
        _fail('schema')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='typed_operation_consumers' LIMIT 1").fetchone():
        _fail('trigger')
    if conn.execute('SELECT 1 FROM typed_operation_consumers LIMIT 1 OFFSET ?', (MAX_CONSUMERS,)).fetchone():
        _fail('budget')
    if conn.execute("SELECT 1 FROM typed_operation_consumers WHERE owner_kind!='outbox' OR typeof(owner_path)!='text' OR length(CAST(owner_path AS BLOB))>4096 OR typeof(owner_incarnation)!='text' OR length(CAST(owner_incarnation AS BLOB))>4096 OR typeof(owner_epoch)!='text' OR length(owner_epoch)!=32 OR typeof(owner_seq)!='integer' OR owner_seq<1 OR typeof(leg)!='text' OR length(CAST(leg AS BLOB))>189 OR typeof(digest)!='text' OR length(digest)!=64 OR typeof(protocol_epoch)!='text' OR length(protocol_epoch)!=36 OR typeof(operation_id)!='text' OR length(operation_id)!=36 OR typeof(consumed)!='integer' OR consumed NOT IN (0,1) LIMIT 1").fetchone():
        _fail('invalid')
    for epoch, operation, leg, digest in conn.execute('SELECT protocol_epoch,operation_id,leg,digest FROM typed_operation_consumers'):
        try:
            _uuid(epoch, 'consumer epoch')
            _uuid(operation, 'consumer operation')
            if _LEG.fullmatch(leg) is None or _DIGEST.fullmatch(digest) is None:
                raise ValueError('invalid consumer slot')
        except ValueError:
            _fail('invalid')
    if conn.execute("SELECT 1 FROM typed_operation_consumers c LEFT JOIN mutation_stores s ON s.path=c.owner_path WHERE s.path IS NULL OR s.incarnation!=c.owner_incarnation OR s.source_epoch!=c.owner_epoch LIMIT 1").fetchone():
        _fail('store_changed')
    if conn.execute("SELECT 1 FROM typed_operation_consumers c LEFT JOIN typed_operation_tickets t USING(protocol_epoch,operation_id) LEFT JOIN typed_operations o USING(protocol_epoch,operation_id) WHERE (c.consumed=0 AND (t.operation_id IS NULL OR o.operation_id IS NULL OR t.released!=0)) OR (c.consumed=1 AND o.operation_id IS NOT NULL AND o.state!='acknowledged') LIMIT 1").fetchone():
        _fail('ticket_changed')


def check_key(store, seq, leg, digest):
    from .db import Store

    if type(store) is not Store or len(str(store.path).encode('utf-8')) > 4096:
        raise ValueError('invalid outbox Store')
    if type(seq) is not int or not 1 <= seq <= 2**63 - 1:
        raise ValueError('invalid outbox sequence')
    if type(leg) is not str or _LEG.fullmatch(leg) is None:
        raise ValueError('invalid outbox leg')
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise ValueError('invalid outbox digest')


def validate_owner(root, conn, store_conn, store, seq, leg, digest, *, states):
    from .db import outbox_owner_row

    check_key(store, seq, leg, digest)
    incarnation, epoch = root._registered_store(conn, str(store.path))
    root._check_store(store_conn, store, incarnation, epoch)
    row = outbox_owner_row(store_conn, seq)
    if row is None or row[0] not in states or row[1] != digest or row[2] != 'append_log':
        _fail('outbox_changed')
    if type(row[3]) is not str or row[3].count('|') != 1:
        _fail('outbox_target')
    key = ('outbox', str(store.path), incarnation, epoch, seq, leg, digest)
    return key, row[0], tuple(row[3].split('|'))


def find_slot(conn, key):
    row = conn.execute('SELECT owner_epoch,digest,protocol_epoch,operation_id,consumed FROM typed_operation_consumers WHERE owner_kind=? AND owner_path=? AND owner_incarnation=? AND owner_seq=? AND leg=?',
                       (key[0], key[1], key[2], key[4], key[5])).fetchone()
    if row is None:
        return None
    if row[:2] != (key[3], key[6]):
        _fail('owner_changed')
    return OperationTicket(row[2], row[3]), row[4]


def validate_request(key, target, request):
    import json

    arguments = json.loads(request.arguments_json)
    kind = request.expectation.kind
    if key[5] == 'append':
        if (kind != 'log_append' or arguments['p_chat'] != target[0]
                or arguments['p_log'] != target[1]):
            _fail('request_changed')
    elif (kind != 'blob_manifest' or arguments['p_chat'] != target[0]
          or arguments['p_blob'] != key[5][len('manifest:'):]):
        _fail('request_changed')


def insert_slot(conn, key, request):
    if conn.execute('SELECT count(*) FROM typed_operation_consumers').fetchone()[0] >= MAX_CONSUMERS:
        _fail('budget')
    expected = request.expectation
    conn.execute('INSERT INTO typed_operation_consumers VALUES(?,?,?,?,?,?,?,?,?,0)',
                 (*key, expected.protocol_epoch, expected.operation_id))


def require_consumed(conn, ticket):
    if conn.execute("SELECT format FROM mutation_root WHERE singleton=1").fetchone() != (7,):
        return
    if conn.execute('SELECT 1 FROM typed_operation_consumers WHERE protocol_epoch=? AND operation_id=? AND consumed=0 LIMIT 1',
                    (ticket.protocol_epoch, ticket.operation_id)).fetchone():
        _fail('unsettled')


def owner_payload(store_conn, seq):
    """Targeted bounded payload read, after validate_owner's scalar preflight."""
    import json

    payload = json.loads(store_conn.execute('SELECT payload FROM outbox WHERE seq=?', (seq,)).fetchone()[0])
    if type(payload) is not dict:
        _fail('payload')
    if 'envelope' not in payload:
        return payload, {}
    attachments = payload.get('attachments')
    if (set(payload) != {'v', 'envelope', 'attachments'}
            or type(payload['v']) is not int or payload['v'] != 1
            or type(payload['envelope']) is not dict
            or type(attachments) is not list or not 1 <= len(attachments) <= 1024):
        _fail('payload')
    manifests = {}
    for item in attachments:
        blob = item.get('blob_id') if type(item) is dict else None
        if (type(blob) is not str or re.fullmatch(r'f2-[A-Za-z0-9._-]{1,177}', blob) is None
                or item.get('v') != 2 or blob in manifests or type(item.get('record')) is not dict):
            _fail('payload')
        manifests[blob] = item
    return payload['envelope'], manifests


def validate_payload(store_conn, seq, key, target, request):
    import hashlib
    import json

    envelope, manifests = owner_payload(store_conn, seq)
    args = json.loads(request.arguments_json)
    validate_request(key, target, request)
    if key[5] == 'append':
        if json.dumps(args['p_record'], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) != json.dumps(envelope, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False):
            _fail('request_changed')
        return
    manifest = manifests.get(key[5][len('manifest:'):])
    if manifest is None:
        _fail('request_changed')
    try:
        material = ['blob_manifest', target[0], manifest['blob_id'],
                    manifest['sealed_bytes'], manifest['sealed_sha256'], manifest['record']]
        digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if (type(args['p_sealed_bytes']) is not type(manifest['sealed_bytes'])
                or args['p_sealed_bytes'] != manifest['sealed_bytes']
                or args['p_sealed_sha256'] != manifest['sealed_sha256']
                or args['p_manifest_digest'] != digest):
            _fail('request_changed')
    except (KeyError, TypeError, ValueError):
        _fail('request_changed')


def reap_deleted_owners(conn, store_conn, key):
    """Reclaim settled slots only after durable deletion in this exact Store."""
    rows = tuple(conn.execute('SELECT DISTINCT owner_seq FROM typed_operation_consumers WHERE owner_kind=? AND owner_path=? AND owner_incarnation=? AND consumed=1', key[:3]))
    for offset in range(0, len(rows), 256):
        seqs = tuple(row[0] for row in rows[offset:offset + 256])
        present = {row[0] for row in store_conn.execute('SELECT seq FROM outbox WHERE seq IN (' + ','.join('?' for _ in seqs) + ')', seqs)}
        for seq in seqs:
            if seq not in present:
                conn.execute('DELETE FROM typed_operation_consumers WHERE owner_kind=? AND owner_path=? AND owner_incarnation=? AND owner_seq=? AND consumed=1', (*key[:3], seq))


def require_preceding_manifests(conn, store_conn, key):
    if key[5] != 'append':
        return
    _, manifests = owner_payload(store_conn, key[4])
    for blob in manifests:
        manifest_key = (*key[:5], 'manifest:' + blob, key[6])
        slot = find_slot(conn, manifest_key)
        if slot is None or slot[1]:
            _fail('manifest_unacknowledged')
        if conn.execute('SELECT state FROM typed_operations WHERE protocol_epoch=? AND operation_id=?',
                        (slot[0].protocol_epoch, slot[0].operation_id)).fetchone() != ('acknowledged',):
            _fail('manifest_unacknowledged')
