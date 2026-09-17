"""Bounded raw authority inputs and current lookup policy, never permission."""
from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass

from .mirror_observation import MirrorExpectedPosition, _valid_path

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 200_000
MAX_DEPTH = 64


class AuthorityObservationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorityDocuments:
    chat_id: str
    mirror: MirrorExpectedPosition
    records: tuple[tuple[str, str | None], ...]
    serialized_bytes: int


@dataclass(frozen=True)
class LookupPolicy:
    chat_id: str
    mirror: MirrorExpectedPosition
    accounts: tuple[tuple[str, str], ...]
    meta: tuple[str, str]


def _part(value):
    if (type(value) is not str or not value or len(value) > 4096
            or any(c in value for c in ('/', '\\', '\x00'))):
        raise ValueError('invalid authority identity')
    if not _valid_path('users/' + value + '.json'):
        raise ValueError('invalid authority path')
    return value


def _owner(transport):
    from .cache import CachingTransport
    if type(transport) is not CachingTransport:
        raise AuthorityObservationUnavailable('unsupported')


def _position_locked(transport):
    if (not transport._warm or transport._mirror_invalid_reason is not None
            or not transport._mirror_selection_keys_valid
            or transport._mirror_provenance != 'provider_observed'):
        raise AuthorityObservationUnavailable('authority_mirror_pending')
    return MirrorExpectedPosition(transport._mirror_root_identity,
                                  transport._mirror_cache_identity,
                                  transport._mirror_instance_nonce,
                                  transport._mirror_revision)


def _limit(value, ceiling):
    if type(value) is not int or not 0 <= value <= ceiling:
        raise ValueError('invalid authority budget')
    return value


class _JSONBudget:
    """Exact wire-byte accounting and bounded detached JSON construction."""
    def __init__(self, size, nodes=MAX_NODES):
        self.remaining = size
        self.nodes = nodes
        self.active = set()

    def charge(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise AuthorityObservationUnavailable('authority_byte_budget')

    def string(self, value):
        if len(value) > self.remaining:
            raise AuthorityObservationUnavailable('authority_byte_budget')
        size = 2 + len(value.encode('utf-8'))
        for char in value:
            if char in ('"', '\\', '\b', '\f', '\n', '\r', '\t'):
                size += 1
            elif ord(char) < 32:
                size += 5
        self.charge(size)
        return value

    def copy(self, value, depth=0):
        self.nodes -= 1
        if self.nodes < 0 or depth > MAX_DEPTH:
            raise AuthorityObservationUnavailable('authority_structure_budget')
        kind = type(value)
        if kind is str:
            return self.string(value)
        if value is None:
            self.charge(4)
        elif kind is bool:
            self.charge(4 if value else 5)
        elif kind is int:
            if value.bit_length() > max(0, self.remaining) * 4:
                raise AuthorityObservationUnavailable('authority_byte_budget')
            self.charge(len(str(value)))
        elif kind is float:
            if not math.isfinite(value):
                raise AuthorityObservationUnavailable('invalid_authority_json')
            self.charge(len(json.dumps(value, allow_nan=False)))
        elif kind in (dict, list):
            if id(value) in self.active:
                raise AuthorityObservationUnavailable('invalid_authority_json')
            if len(value) * (2 if kind is dict else 1) > self.nodes:
                raise AuthorityObservationUnavailable('authority_structure_budget')
            self.charge(2 + max(0, len(value) - 1) + (len(value) if kind is dict else 0))
            self.active.add(id(value))
            try:
                if kind is list:
                    return [self.copy(v, depth + 1) for v in value]
                result = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise AuthorityObservationUnavailable('invalid_authority_json')
                    self.nodes -= 1
                    result[self.string(key)] = self.copy(item, depth + 1)
                return result
            finally:
                self.active.remove(id(value))
        else:
            raise AuthorityObservationUnavailable('invalid_authority_json')
        return value


def capture_authority_documents(transport, chat_id, *, max_documents=20_000,
                                max_bytes=MAX_BYTES, max_examined_paths=100_000):
    """Background-only cut. Owned references are copied/serialized off-lock."""
    _owner(transport)
    chat = _part(chat_id)
    meta = f'chats/{chat}/meta.json'
    if not _valid_path(meta):
        raise ValueError('invalid authority metadata path')
    _limit(max_documents, 20_000)
    _limit(max_bytes, MAX_BYTES)
    _limit(max_examined_paths, 100_000)
    with transport._lock:
        position = _position_locked(transport)
        if len(transport._docs) > max_examined_paths:
            raise AuthorityObservationUnavailable('authority_path_budget')
        refs = [(meta, transport._docs.get(meta), meta in transport._docs)]
        used = 16 + len(meta.encode())
        for name, value in transport._docs.items():
            if name.startswith(('users/', 'lifecycle/')):
                refs.append((name, value, True))
                used += 16 + len(name.encode())
                if len(refs) > max_documents or used > max_bytes:
                    raise AuthorityObservationUnavailable('authority_selection_budget')
        if len(refs) > max_documents or used > max_bytes:
            raise AuthorityObservationUnavailable('authority_selection_budget')
        if any(name in transport._authority_unsafe for name, _value, present in refs if present):
            raise AuthorityObservationUnavailable("authority_value_ineligible")
    budget = _JSONBudget(max_bytes - used)
    records = []
    try:
        for name, value, present in refs:
            if not present:
                records.append((name, None))
                continue
            before = budget.remaining
            detached = budget.copy(value)
            raw = json.dumps(detached, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':'))
            if len(raw.encode()) != before - budget.remaining:
                raise AuthorityObservationUnavailable('authority_serialization_changed')
            records.append((name, raw))
    except (ValueError, UnicodeError, TypeError, RecursionError, RuntimeError, MemoryError) as exc:
        if isinstance(exc, AuthorityObservationUnavailable):
            raise
        raise AuthorityObservationUnavailable('invalid_authority_json') from exc
    with transport._lock:
        if _position_locked(transport) != position:
            raise AuthorityObservationUnavailable('authority_mirror_changed')
    return AuthorityDocuments(chat, position, tuple(sorted(records)), max_bytes - budget.remaining)


def capture_lookup_policy(transport, chat_id, account_names=(), *, expected=None):
    _owner(transport)
    chat = _part(chat_id)
    if type(account_names) is not tuple or len(account_names) > 128:
        raise ValueError('invalid authority account selection')
    paths = tuple('users/' + _part(name) + '.json' for name in account_names)
    if len(set(paths)) != len(paths) or sum(len(p.encode()) for p in paths) > 64 * 1024:
        raise ValueError('invalid authority account selection')
    meta = f'chats/{chat}/meta.json'
    if not _valid_path(meta):
        raise ValueError('invalid authority metadata path')
    if expected is not None and type(expected) is not MirrorExpectedPosition:
        raise ValueError('invalid expected authority mirror')
    if expected is not None:
        expected = MirrorExpectedPosition(expected.root_identity, expected.cache_identity,
                                          expected.instance_nonce, expected.revision)
    with transport._lock:
        return _lookup_policy_locked(transport, chat, paths, meta, expected)


def _lookup_policy_locked(transport, chat, paths, meta, expected):
    position = _position_locked(transport)
    if expected is not None and position != expected:
        raise AuthorityObservationUnavailable('authority_mirror_changed')
    statuses = []
    for path in paths:
        if path in transport._docs:
            mode = 'present'
        elif path in transport._neg:
            mode = 'known_negative'
        elif transport._health_state != 'online':
            mode = 'offline_absent'
        else:
            mode = 'online_readthrough_required'
        statuses.append((path, mode))
    return LookupPolicy(chat, position, tuple(statuses),
                        (meta, 'present' if meta in transport._docs else 'mirror_absent'))


def _copy_lookup_policy(expected):
    if type(expected) is not LookupPolicy:
        raise ValueError('invalid lookup policy')
    chat, mirror, accounts, meta = expected.chat_id, expected.mirror, expected.accounts, expected.meta
    if type(accounts) is not tuple or len(accounts) > 128 or type(meta) is not tuple or len(meta) != 2:
        raise ValueError('invalid lookup policy fields')
    if type(mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid lookup mirror')
    mirror = MirrorExpectedPosition(mirror.root_identity, mirror.cache_identity,
                                    mirror.instance_nonce, mirror.revision)
    chat = _part(chat)
    paths = []
    for item in accounts:
        if (type(item) is not tuple or len(item) != 2 or any(type(v) is not str for v in item)
                or len(item[0]) > 4107
                or not item[0].startswith('users/') or not item[0].endswith('.json')):
            raise ValueError('invalid lookup policy account')
        name = _part(item[0][6:-5])
        path = 'users/' + name + '.json'
        if item[1] not in ('present', 'known_negative', 'offline_absent', 'online_readthrough_required'):
            raise ValueError('invalid lookup policy mode')
        paths.append(path)
    if any(type(v) is not str for v in meta):
        raise ValueError('invalid lookup policy metadata')
    if (meta[0] != f'chats/{chat}/meta.json' or not _valid_path(meta[0])
            or meta[1] not in ('present', 'mirror_absent')
            or len(set(paths)) != len(paths) or sum(len(p.encode()) for p in paths) > 64 * 1024):
        raise ValueError('invalid lookup policy selection')
    return LookupPolicy(chat, mirror, accounts, meta)


def matches_lookup_policy(transport, expected):
    with _locked_matching_lookup_policy(transport, expected) as matched:
        return matched


@contextmanager
def _locked_matching_lookup_policy(transport, expected):
    """Hold raw policy exclusion through the coordinator's prepared SQL commit.

    Caller order is pin -> SQLite -> mirror. No provider, parsing, crypto,
    callbacks or SQLite acquisition is permitted inside this private context.
    This is a current raw-input comparison, never an authority grant.
    """
    _owner(transport)
    copied = _copy_lookup_policy(expected)
    paths = tuple(path for path, _mode in copied.accounts)
    with transport._lock:
        yield _lookup_policy_locked(transport, copied.chat_id, paths,
                                    copied.meta[0], copied.mirror) == copied


def _eligible_ingress_path(path):
    # Keys share raw ownership protection, never the authority-source allowlist.
    return type(path) is str and (
        path.startswith(('users/', 'lifecycle/'))
        or (path.startswith('chats/') and (path.endswith('/meta.json') or '/keys/' in path))
    )


def detach_ingress_value(path, value):
    """Give eligible JSON a private graph; leave legacy unsupported values intact.

    Run after legacy deepcopy, outside the mirror lock. A malicious deepcopy may
    return an aliased *exact* dict; this second, hook-free bounded copy breaks that
    alias. Failure marks only authority eligibility, not canonical write success.
    """
    if not _eligible_ingress_path(path):
        return value, True
    try:
        return _JSONBudget(MAX_BYTES).copy(value), True
    except (AuthorityObservationUnavailable, ValueError, TypeError, UnicodeError,
            RecursionError, RuntimeError, MemoryError):
        return value, False


def detach_ingress_documents(docs):
    unsafe = set()
    for path, value in docs.items():
        if not _eligible_ingress_path(path):
            continue
        docs[path], safe = detach_ingress_value(path, value)
        if not safe:
            unsafe.add(path)
    return docs, unsafe
