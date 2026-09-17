"""Internal exact local key-wrap inputs; no key recovery or viewer authority.

Raw key documents may contain other viewers' wraps. Keep records and fields out
of logs/cursors/responses. Parse only after charging their full serialized input,
and prepare semantic comparisons before entering a final root/Store interval.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..store import document_observation as docs
from ..transport import key_observation as wraps
from .local_page_source import LocalPageSource, LocalSourceReceipt

MAX_SOURCE_BYTES = 4 * 1024 * 1024
_SHAPES = ('absent', 'document_not_dict', 'wrapped_not_dict', 'viewer_not_dict',
           'invalid_fields', 'valid')


@dataclass(frozen=True)
class LocalKeyWrapObservation:
    chat_id: str
    epoch: int
    viewer: str
    receipt: LocalSourceReceipt
    record: docs.SerializedDocumentRecord = field(repr=False)
    mode: str
    shape: str
    fields: tuple[str, str, str] | None = field(repr=False)
    captured_bytes: int
    source_bytes: int


@dataclass(frozen=True)
class _PreparedLocalKeyWrap:
    observation: LocalKeyWrapObservation = field(repr=False)


def _owner(reader):
    if type(reader) is not LocalPageSource:
        raise ValueError('expected local key source owner')


def _source_limit(value):
    if type(value) is not int or not 0 <= value <= MAX_SOURCE_BYTES:
        raise ValueError('invalid key document budget')


def _invalid_constant(_value):
    raise ValueError('invalid local key JSON constant')


def _parse(record, viewer, maximum):
    # Malformed stored JSON is an unavailable raw input, not an absent key.
    document = None if record.deleted else json.loads(record.payload_json, parse_constant=_invalid_constant)
    return wraps.classify_wrap(document, viewer, present=not record.deleted,
        base_bytes=len(record.path.encode()) + len(viewer.encode()), max_bytes=maximum)


def capture(reader, receipt, epoch, viewer, *, charge_source,
            max_source_bytes=MAX_SOURCE_BYTES, max_bytes=wraps.MAX_WRAP_BYTES):
    _owner(reader)
    _source_limit(max_source_bytes)
    wraps._budget(max_bytes)
    if not callable(charge_source):
        raise ValueError('raw key input requires an operation charge')
    receipt = reader._receipt(receipt)
    chat, epoch, viewer, _path = wraps.selection(reader.chat, epoch, viewer)
    record = reader.capture_key_document(receipt, epoch, viewer, max_bytes=max_source_bytes)
    source_bytes = len(record.path.encode()) + len((record.payload_json or '').encode())
    charge_source(source_bytes)  # outside SQL, before JSON materialization
    shape, fields, size = _parse(record, viewer, max_bytes)
    return LocalKeyWrapObservation(chat, epoch, viewer, receipt, record,
        'known_negative' if record.deleted else 'present', shape, fields, size, source_bytes)


def copy_observation(reader, expected):
    """Bounded structural copy only; semantic preparation is a separate step."""
    _owner(reader)
    if type(expected) is not LocalKeyWrapObservation:
        raise ValueError('invalid local key observation')
    chat, epoch, viewer, path = wraps.selection(expected.chat_id, expected.epoch, expected.viewer)
    receipt = reader._receipt(expected.receipt)
    if chat != receipt.chat_id:
        raise ValueError('key receipt belongs to another chat')
    record = expected.record
    if type(record) is not docs.SerializedDocumentRecord:
        raise ValueError('invalid local key record')
    name, raw, deleted = record.path, record.payload_json, record.deleted
    if type(name) is not str or name != path or type(deleted) is not bool:
        raise ValueError('invalid local key record identity')
    if (deleted and raw is not None) or (not deleted and type(raw) is not str):
        raise ValueError('invalid local key record payload')
    _source_limit(expected.source_bytes)
    if len(name) + len(raw or '') > expected.source_bytes:
        raise ValueError('invalid local key source bytes')
    source_bytes = len(name.encode()) + len((raw or '').encode())
    if source_bytes != expected.source_bytes:
        raise ValueError('invalid local key source bytes')
    mode, shape, fields = expected.mode, expected.shape, expected.fields
    if type(mode) is not str or mode != ('known_negative' if deleted else 'present'):
        raise ValueError('invalid local key mode')
    if type(shape) is not str or shape not in _SHAPES or (shape == 'absent') != deleted:
        raise ValueError('invalid local key shape')
    size = len(path.encode()) + len(viewer.encode())
    if shape == 'valid':
        if type(fields) is not tuple or len(fields) != 3 or any(type(v) is not str for v in fields):
            raise ValueError('invalid local key fields')
        for value in fields:
            if len(value) > wraps.MAX_WRAP_BYTES - size:
                raise ValueError('invalid local key fields budget')
            size += len(value.encode())
    elif fields is not None:
        raise ValueError('unexpected local key fields')
    wraps._budget(size)
    wraps._budget(expected.captured_bytes)
    if size != expected.captured_bytes:
        raise ValueError('invalid local key selected bytes')
    return LocalKeyWrapObservation(chat, epoch, viewer, receipt,
        docs.SerializedDocumentRecord(path, raw, deleted), mode, shape, fields, size, source_bytes)


def prepare(reader, expected, *, charge_source):
    """Recompute supplied fields from bounded raw input outside final locks."""
    if not callable(charge_source):
        raise ValueError('raw key input requires an operation charge')
    value = copy_observation(reader, expected)
    charge_source(value.source_bytes)
    if _parse(value.record, value.viewer, value.captured_bytes) != (
            value.shape, value.fields, value.captured_bytes):
        raise ValueError('local key fields do not match raw input')
    return _PreparedLocalKeyWrap(value)


def _matches_prepared_in_transaction(reader, conn, prepared):
    """Internal: caller prepared semantics off-lock and owns finalization cut.

    Source matching alone is not a viewer/key permission check. This only binds
    the already-parsed raw record at the same cut as the other page dependencies.
    """
    if type(prepared) is not _PreparedLocalKeyWrap:
        raise ValueError('expected semantically prepared local key input')
    value = copy_observation(reader, prepared.observation)
    if not reader.matches_in_transaction(conn, value.receipt):
        return False
    captured = docs._capture_selected(conn, reader.store.path, value.receipt.source.raw,
        (value.record.path,), max_documents=1, max_bytes=value.source_bytes)
    return captured.records == (value.record,)
