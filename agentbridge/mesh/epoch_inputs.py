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
from . import local_key_inputs

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
    wrap: wraps.KeyWrapObservation | local_key_inputs.LocalKeyWrapObservation | None = field(repr=False)
    identity: IdentityObservation | None = field(repr=False)
    captured_bytes: int
    local_transport: object | None = field(default=None, repr=False, compare=False)


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


def _local_owner(service, reader):
    from ..transport.local_mutations import LocalMutationTransport, root_identity
    local_key_inputs._owner(reader)
    transport = service.tx
    if type(transport) is LocalMutationTransport:
        if transport._coordinator is not reader.coordinator:
            raise ValueError('local mutation owner mismatch')
        transport = transport._transport
    if root_identity(transport) != reader.coordinator.identity:
        raise ValueError('local key source transport mismatch')


def capture_epoch(service, chat_id, epoch, *, max_bytes=MAX_IDENTITY_BYTES,
                  source_reader=None, receipt=None, charge_source=None,
                  max_source_bytes=local_key_inputs.MAX_SOURCE_BYTES):
    """Resident first; otherwise one observed wrap then bounded identity bytes.

    Does not unwrap, mutate a cache, upgrade a file, or perform read-through.
    Unknown-online is retained as work-required, distinct from a missing key.
    """
    _owner(service)
    chat, epoch, viewer, _path = wraps.selection(chat_id, epoch, service.user)
    _limit(max_bytes)
    transport = None
    if source_reader is not None:
        _local_owner(service, source_reader)
        if chat != source_reader.chat:
            raise ValueError('local epoch belongs to another chat')
        receipt = source_reader._receipt(receipt)
        transport = service.tx
    elif receipt is not None or charge_source is not None:
        raise ValueError('local key inputs require an explicit source reader')
    with service._cache_lock:
        resident = service._cache.get((chat, epoch))
    if resident is not None:
        if type(resident) is not bytes or len(resident) > max_bytes:
            raise EpochInputsUnavailable('resident_key_budget')
        return EpochObservation(service._epoch_owner, chat, epoch, viewer, resident,
                                None, None, len(resident), transport)
    if source_reader is None:
        wrap = wraps.capture_key_wrap(service.tx, chat, epoch, viewer, max_bytes=max_bytes)
    else:
        wrap = local_key_inputs.capture(source_reader, receipt, epoch, viewer,
            charge_source=charge_source, max_source_bytes=max_source_bytes, max_bytes=max_bytes)
    identity, size = None, wrap.captured_bytes
    if wrap.shape == 'valid':
        with _KEYSTORE_LOCK:
            identity = _read_identity(service.keystore._path(viewer), max_bytes - size)
        size += len(identity.raw) if identity.raw is not None else 0
    return EpochObservation(service._epoch_owner, chat, epoch, viewer, None, wrap, identity, size, transport)


def _copy(service, expected, *, source_reader=None):
    _owner(service)
    if type(expected) is not EpochObservation:
        raise ValueError('invalid epoch observation')
    chat, epoch, viewer, _path = wraps.selection(expected.chat_id, expected.epoch, expected.viewer)
    if (type(expected.owner) is not str or len(expected.owner) != 32
            or any(c not in '0123456789abcdef' for c in expected.owner)):
        raise ValueError('invalid epoch owner')
    if expected.owner != service._epoch_owner or viewer != service.user:
        raise ValueError('epoch owner changed')
    if source_reader is not None:
        _local_owner(service, source_reader)
        if expected.local_transport is not service.tx or chat != source_reader.chat:
            raise ValueError('local epoch transport changed')
    elif expected.local_transport is not None:
        raise ValueError('local epoch requires its source reader')
    _limit(expected.captured_bytes)
    resident, wrap, identity = expected.resident, expected.wrap, expected.identity
    if resident is not None:
        if type(resident) is not bytes or wrap is not None or identity is not None:
            raise ValueError('invalid resident observation')
        size = len(resident)
    else:
        if source_reader is None:
            wrap, _args = wraps._copy(wrap)
        else:
            wrap = local_key_inputs.copy_observation(source_reader, wrap)
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
    return EpochObservation(service._epoch_owner, chat, epoch, viewer, resident, wrap, identity, size, expected.local_transport)


@contextmanager
def locked_matching_epoch_local(service, expected, *, source_reader=None):
    """Hold selected resident/identity evidence, leaving the mirror unlocked.

    This is ONLY the local half of the fence, not full epoch validation. It lets
    the page owner insert pin and SQLite scopes before its final mirror check.
    Order: epoch cache -> identity -> pin -> root (local source) -> SQLite
    -> mirror (mirror source only). It is safe to
    nest a bounded number of local views of the SAME service (reentrant locks).
    """
    view = _copy(service, expected, source_reader=source_reader)
    with service._cache_lock, _KEYSTORE_LOCK:
        matched = service._cache.get((view.chat_id, view.epoch)) == view.resident
        if matched and view.identity is not None:
            try:
                ceiling = len(view.identity.raw) if view.identity.raw is not None else 0
                matched = _read_identity(view.identity.path, ceiling) == view.identity
            except EpochInputsUnavailable:
                matched = False
        yield matched


def _prepare_source(source_reader, view, charge_source):
    if source_reader is None or view.wrap is None:
        return None
    return local_key_inputs.prepare(source_reader, view.wrap, charge_source=charge_source)


def _charge_comparison(prepared, charge_source):
    if prepared is not None:
        charge_source(prepared.observation.source_bytes)


@contextmanager
def _source_scope(service, view, source_reader, prepared):
    if view.wrap is None:
        yield True
    elif source_reader is None:
        with wraps.locked_matching_key_wrap(service.tx, view.wrap) as matched:
            yield matched
    else:
        # Every parse and ledger callback precedes the root/Store interval.
        _local_owner(service, source_reader)
        if service.tx is not view.local_transport:
            yield False
            return
        with source_reader.finalization(view.wrap.receipt) as conn:
            yield local_key_inputs._matches_prepared_in_transaction(source_reader, conn, prepared)


@contextmanager
def locked_matching_epoch(service, expected, *, source_reader=None, charge_source=None):
    """Complete epoch fence; local raw comparisons share a held root/Store cut."""
    view = _copy(service, expected, source_reader=source_reader)
    prepared = _prepare_source(source_reader, view, charge_source)
    _charge_comparison(prepared, charge_source)
    with locked_matching_epoch_local(service, view, source_reader=source_reader) as local_matches:
        with _source_scope(service, view, source_reader, prepared) as source_matches:
            yield local_matches and source_matches


def matches_epoch(service, expected, *, source_reader=None, charge_source=None):
    with locked_matching_epoch(service, expected, source_reader=source_reader,
                               charge_source=charge_source) as matched:
        return matched


def publish_epoch(service, expected, *, source_reader=None, charge_source=None):
    """Canonical unwrap/cache progress outside any caller SQL transaction.

    Returns published/identity_progress (caller MUST restart), resident, missing, readthrough, or
    conflict. It never hands a newly recovered key directly to page decryption.
    """
    view = _copy(service, expected, source_reader=source_reader)
    if view.resident is not None:
        return 'resident' if matches_epoch(service, view, source_reader=source_reader,
                                         charge_source=charge_source) else 'conflict'
    if view.wrap.mode == 'online_readthrough_required':
        return 'readthrough' if matches_epoch(service, view) else 'conflict'
    prepared = _prepare_source(source_reader, view, charge_source)
    key = None
    if view.wrap.shape == 'valid':
        bundle = _decode_identity(view.identity.raw)
        if bundle and not view.identity.raw.decode('utf-8').strip().startswith(KeyStore._WRAPPED) and dpapi.available():
            # Canonical load upgrades a legacy plain file even if unwrap later
            # fails. Keep that side effect outside the mirror/SQL interval.
            _charge_comparison(prepared, charge_source)
            with locked_matching_epoch_local(service, view, source_reader=source_reader) as local_matches:
                with _source_scope(service, view, source_reader, prepared) as wrap_matches:
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
    _charge_comparison(prepared, charge_source)
    with locked_matching_epoch_local(service, view, source_reader=source_reader) as local_matches:
        installed = False
        try:
            with _source_scope(service, view, source_reader, prepared) as source_matches:
                if not (local_matches and source_matches):
                    return 'conflict'
                if key is None:
                    return 'missing'
                if type(key) is not bytes or len(key) > MAX_IDENTITY_BYTES:
                    raise EpochInputsUnavailable('recovered_key_budget')
                installed = True
                service._cache[(view.chat_id, view.epoch)] = key
            return 'published'
        except BaseException:
            # A failed final cut/SQLite commit must not leave recovered state.
            # The cache lock still excludes another winner while rolling back.
            if installed and service._cache.get((view.chat_id, view.epoch)) is key:
                service._cache.pop((view.chat_id, view.epoch), None)
            raise
