"""Internal, bounded epoch-wrap observations; never membership authority.

Only provider-observed CachingTransport ownership is supported here. No provider
read-through, key-prefix enumeration, or other viewers' wrap copy is performed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from .authority_observation import (
    AuthorityObservationUnavailable, _owner, _part, _position_locked,
)
from .mirror_observation import MirrorExpectedPosition, _valid_path

MAX_WRAP_BYTES = 64 * 1024


@dataclass(frozen=True)
class KeyWrapObservation:
    chat_id: str
    epoch: int
    viewer: str
    mirror: MirrorExpectedPosition
    mode: str
    shape: str
    fields: tuple[str, str, str] | None = field(repr=False)
    captured_bytes: int = 0


def selection(chat_id, epoch, viewer):
    chat, user = _part(chat_id), _part(viewer)
    if type(epoch) is not int or not 1 <= epoch <= 2**63 - 1:
        raise ValueError('invalid epoch')
    path = f'chats/{chat}/keys/{epoch}.json'
    if not _valid_path(path):
        raise ValueError('invalid epoch path')
    return chat, epoch, user, path


def _budget(max_bytes):
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_WRAP_BYTES:
        raise ValueError('invalid key wrap budget')


def _capture_locked(transport, chat, epoch, viewer, path, max_bytes):
    mirror = _position_locked(transport)
    size = len(path.encode()) + len(viewer.encode())
    if size > max_bytes:
        raise AuthorityObservationUnavailable('key_wrap_budget')
    shape, fields = 'absent', None
    if path in transport._docs:
        mode = 'present'
        if path in transport._authority_unsafe:
            raise AuthorityObservationUnavailable('key_wrap_ineligible')
        doc = transport._docs[path]
        shape = 'document_not_dict'
        if type(doc) is dict:
            wraps = doc.get('wrapped')
            shape = 'wrapped_not_dict'
            if type(wraps) is dict:
                wrap = wraps.get(viewer)
                shape = 'viewer_not_dict'
                if type(wrap) is dict:
                    values = tuple(wrap.get(k) for k in ('eph', 'nonce', 'ct'))
                    shape = 'invalid_fields'
                    if all(type(v) is str for v in values):
                        # Check character count before allocating UTF-8 bytes.
                        for value in values:
                            if len(value) > max_bytes - size:
                                raise AuthorityObservationUnavailable('key_wrap_budget')
                            size += len(value.encode())
                            if size > max_bytes:
                                raise AuthorityObservationUnavailable('key_wrap_budget')
                        shape, fields = 'valid', values
    elif path in transport._neg:
        mode = 'known_negative'
    elif transport._health_state != 'online':
        mode = 'offline_absent'
    else:
        mode = 'online_readthrough_required'
    return KeyWrapObservation(chat, epoch, viewer, mirror, mode, shape, fields, size)


def capture_key_wrap(transport, chat_id, epoch, viewer, *, max_bytes=MAX_WRAP_BYTES):
    _owner(transport)
    args = selection(chat_id, epoch, viewer)
    _budget(max_bytes)
    with transport._lock:
        return _capture_locked(transport, *args, max_bytes)


def _copy(expected):
    if type(expected) is not KeyWrapObservation:
        raise ValueError('invalid key wrap observation')
    args = selection(expected.chat_id, expected.epoch, expected.viewer)
    if type(expected.mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid key mirror')
    mirror = MirrorExpectedPosition(expected.mirror.root_identity, expected.mirror.cache_identity,
                                    expected.mirror.instance_nonce, expected.mirror.revision)
    if type(expected.mode) is not str or expected.mode not in ('present', 'known_negative', 'offline_absent', 'online_readthrough_required'):
        raise ValueError('invalid key lookup mode')
    if type(expected.shape) is not str or expected.shape not in ('absent', 'document_not_dict', 'wrapped_not_dict', 'viewer_not_dict',
                              'invalid_fields', 'valid'):
        raise ValueError('invalid key shape')
    values = expected.fields
    if expected.shape == 'valid':
        if type(values) is not tuple or len(values) != 3 or any(type(v) is not str for v in values):
            raise ValueError('invalid key fields')
        if any(len(v) > MAX_WRAP_BYTES for v in values):
            raise ValueError('invalid key field budget')
    elif values is not None:
        raise ValueError('unexpected key fields')
    if (expected.mode == 'present') == (expected.shape == 'absent'):
        raise ValueError('inconsistent key shape')
    size = len(args[3].encode()) + len(args[2].encode())
    if values is not None:
        size += sum(len(v.encode()) for v in values)
    _budget(expected.captured_bytes)
    if size != expected.captured_bytes:
        raise ValueError('inconsistent key byte count')
    return KeyWrapObservation(*args[:3], mirror, expected.mode, expected.shape, values, size), args


def _prepare_key_wraps(expected):
    if type(expected) is not tuple or len(expected) > 64:
        raise ValueError('invalid epoch selection')
    prepared = tuple(_copy(item) for item in expected)
    if len({args for _view, args in prepared}) != len(prepared):
        raise ValueError('duplicate epoch selection')
    return prepared


def _matches_key_wraps_locked(transport, prepared):
    """Owner-private: caller prepared bounded DTOs before taking mirror lock."""
    try:
        return all(_capture_locked(transport, *args, copied.captured_bytes) == copied
                   for copied, args in prepared)
    except (AuthorityObservationUnavailable, UnicodeError):
        return False


@contextmanager
def locked_matching_key_wraps(transport, expected):
    """Match <=64 selected wraps at one common mirror point (<=4MiB inputs).

    Caller order: epoch cache -> identity -> pin -> SQLite -> mirror.
    No provider calls, crypto, or acquisition of earlier locks inside this scope.
    Do not nest single-wrap contexts: the mirror mutex is not reentrant.
    """
    _owner(transport)
    prepared = _prepare_key_wraps(expected)
    with transport._lock:
        yield _matches_key_wraps_locked(transport, prepared)


@contextmanager
def locked_matching_key_wrap(transport, expected):
    with locked_matching_key_wraps(transport, (expected,)) as matched:
        yield matched


def matches_key_wrap(transport, expected):
    with locked_matching_key_wrap(transport, expected) as matched:
        return matched
