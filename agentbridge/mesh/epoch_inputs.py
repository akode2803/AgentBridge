"""Bounded, internal epoch inputs and canonical cache progress, not authority.

Observations are request-local secret-bearing objects. Never serialize them into
logs, page cursors, SQLite, or agent-facing responses. Page/session/membership
fences remain the caller's responsibility. No serving caller exists yet.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field

from .. import crypto
from ..crypto import dpapi
from ..transport import key_observation as wraps
from .keyring import ChatKeyService, KeyStore, _KEYSTORE_LOCK

MAX_IDENTITY_BYTES = 64 * 1024


class EpochInputsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class IdentityObservation:
    path: str
    raw: bytes | None = field(repr=False)


@dataclass(frozen=True)
class EpochObservation:
    owner: str
    chat_id: str
    epoch: int
    viewer: str
    resident: bytes | None = field(repr=False)
    wrap: wraps.KeyWrapObservation | None = field(repr=False)
    identity: IdentityObservation | None = field(repr=False)
    captured_bytes: int


def _owner(service):
    if type(service) is not ChatKeyService or type(service.keystore) is not KeyStore:
        raise ValueError('unsupported epoch owner')


def _limit(value):
    if type(value) is not int or not 0 <= value <= MAX_IDENTITY_BYTES:
        raise ValueError('invalid epoch byte budget')


def _read_identity(path, max_bytes):
    """Cap reads before decoding. Nonregular files cannot stall a page read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise EpochInputsUnavailable('identity_not_regular')
            raw = stream.read(max_bytes + 1)
    except OSError:
        raw = None  # canonical load treats missing/unreadable as unavailable key
    if raw is not None and len(raw) > max_bytes:
        raise EpochInputsUnavailable('identity_byte_budget')
    return IdentityObservation(str(path), raw)


def _decode_identity(raw):
    if raw is None:
        return None
    # Preserve canonical UnicodeDecodeError (read_text was outside its try).
    text = raw.decode('utf-8').strip()
    try:
        if text.startswith(KeyStore._WRAPPED):
            return dpapi.unprotect(crypto.b64d(text[len(KeyStore._WRAPPED):]))
        return crypto.b64d(text)
    except (ValueError, OSError):
        return None


def capture_epoch(service, chat_id, epoch, *, max_bytes=MAX_IDENTITY_BYTES):
    """Resident first; otherwise one observed wrap then bounded identity bytes.

    Does not unwrap, mutate a cache, upgrade a file, or perform read-through.
    Unknown-online is retained as work-required, distinct from a missing key.
    """
    _owner(service)
    chat, epoch, viewer, _path = wraps.selection(chat_id, epoch, service.user)
    _limit(max_bytes)
    with service._cache_lock:
        resident = service._cache.get((chat, epoch))
    if resident is not None:
        if type(resident) is not bytes or len(resident) > max_bytes:
            raise EpochInputsUnavailable('resident_key_budget')
        return EpochObservation(service._epoch_owner, chat, epoch, viewer, resident,
                                None, None, len(resident))
    wrap = wraps.capture_key_wrap(service.tx, chat, epoch, viewer, max_bytes=max_bytes)
    identity, size = None, wrap.captured_bytes
    if wrap.shape == 'valid':
        with _KEYSTORE_LOCK:
            identity = _read_identity(service.keystore._path(viewer), max_bytes - size)
        size += len(identity.raw) if identity.raw is not None else 0
    return EpochObservation(service._epoch_owner, chat, epoch, viewer, None, wrap, identity, size)


def _copy(service, expected):
    _owner(service)
    if type(expected) is not EpochObservation:
        raise ValueError('invalid epoch observation')
    chat, epoch, viewer, _path = wraps.selection(expected.chat_id, expected.epoch, expected.viewer)
    if (type(expected.owner) is not str or len(expected.owner) != 32
            or any(c not in '0123456789abcdef' for c in expected.owner)):
        raise ValueError('invalid epoch owner')
    if expected.owner != service._epoch_owner or viewer != service.user:
        raise ValueError('epoch owner changed')
    _limit(expected.captured_bytes)
    resident, wrap, identity = expected.resident, expected.wrap, expected.identity
    if resident is not None:
        if type(resident) is not bytes or wrap is not None or identity is not None:
            raise ValueError('invalid resident observation')
        size = len(resident)
    else:
        wrap, _args = wraps._copy(wrap)
        if (wrap.chat_id, wrap.epoch, wrap.viewer) != (chat, epoch, viewer):
            raise ValueError('inconsistent epoch wrap')
        size = wrap.captured_bytes
        if wrap.shape == 'valid':
            if type(identity) is not IdentityObservation:
                raise ValueError('missing identity observation')
            if type(identity.path) is not str or identity.path != str(service.keystore._path(viewer)):
                raise ValueError('identity owner changed')
            if identity.raw is not None and type(identity.raw) is not bytes:
                raise ValueError('invalid identity bytes')
            size += len(identity.raw) if identity.raw is not None else 0
            identity = IdentityObservation(identity.path, identity.raw)
        elif identity is not None:
            raise ValueError('unexpected identity observation')
    if size != expected.captured_bytes:
        raise ValueError('inconsistent epoch byte count')
    return EpochObservation(service._epoch_owner, chat, epoch, viewer, resident, wrap, identity, size)


@contextmanager
def locked_matching_epoch_local(service, expected):
    """Hold selected resident/identity evidence, leaving the mirror unlocked.

    This is ONLY the local half of the fence, not full epoch validation. It lets
    the page owner insert pin and SQLite scopes before its final mirror check.
    Order: epoch cache -> identity -> pin -> SQLite -> mirror. It is safe to
    nest a bounded number of local views of the SAME service (reentrant locks).
    """
    view = _copy(service, expected)
    with service._cache_lock, _KEYSTORE_LOCK:
        matched = service._cache.get((view.chat_id, view.epoch)) == view.resident
        if matched and view.identity is not None:
            try:
                ceiling = len(view.identity.raw) if view.identity.raw is not None else 0
                matched = _read_identity(view.identity.path, ceiling) == view.identity
            except EpochInputsUnavailable:
                matched = False
        yield matched


@contextmanager
def locked_matching_epoch(service, expected):
    """Standalone complete epoch fence; mirror held, do not acquire SQL/pins."""
    view = _copy(service, expected)
    with locked_matching_epoch_local(service, view) as local_matches:
        if view.wrap is None:
            yield local_matches
        else:
            with wraps.locked_matching_key_wrap(service.tx, view.wrap) as wrap_matches:
                yield local_matches and wrap_matches


def matches_epoch(service, expected):
    with locked_matching_epoch(service, expected) as matched:
        return matched


def publish_epoch(service, expected):
    """Canonical unwrap/cache progress outside any caller SQL transaction.

    Returns published/identity_progress (caller MUST restart), resident, missing, readthrough, or
    conflict. It never hands a newly recovered key directly to page decryption.
    """
    view = _copy(service, expected)
    if view.resident is not None:
        return 'resident' if matches_epoch(service, view) else 'conflict'
    if view.wrap.mode == 'online_readthrough_required':
        return 'readthrough' if matches_epoch(service, view) else 'conflict'
    key = None
    if view.wrap.shape == 'valid':
        bundle = _decode_identity(view.identity.raw)
        if bundle and not view.identity.raw.decode('utf-8').strip().startswith(KeyStore._WRAPPED) and dpapi.available():
            # Canonical load upgrades a legacy plain file even if unwrap later
            # fails. Keep that side effect outside the mirror/SQL interval.
            with locked_matching_epoch_local(service, view) as local_matches:
                with wraps.locked_matching_key_wrap(service.tx, view.wrap) as wrap_matches:
                    matched = local_matches and wrap_matches
                if not matched:
                    return 'conflict'
                ceiling = MAX_IDENTITY_BYTES - view.wrap.captured_bytes
                try:
                    service.keystore.save(view.viewer, bundle, max_bytes=ceiling)
                except OverflowError as exc:
                    raise EpochInputsUnavailable('identity_byte_budget') from exc
                current = _read_identity(view.identity.path, ceiling)
                if current != view.identity:
                    return 'identity_progress'  # caller MUST discard and restart
                # DPAPI protect failure keeps the canonical plain fallback.
                # Identical content is not progress; do not loop forever.
        if bundle is not None:
            try:
                key = crypto.unwrap_key_with(bundle, dict(zip(('eph', 'nonce', 'ct'), view.wrap.fields)))
            except crypto.CryptoFail:
                pass
    with locked_matching_epoch(service, view) as matched:
        if not matched:
            return 'conflict'
        if key is None:
            return 'missing'
        if type(key) is not bytes or len(key) > MAX_IDENTITY_BYTES:
            raise EpochInputsUnavailable('recovered_key_budget')
        service._cache[(view.chat_id, view.epoch)] = key
        return 'published'
