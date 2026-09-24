"""Request-owned canonical page computation and final local-input validation.

Inactive: GUI/session handout and source scheduling remain separate activation
gates. There is no network, preparation rebuild, or full-fold fallback.
An operation is never serialized, reused by another request, or treated as a
membership lease. Every progress step discards its entire computed page.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from ..core.models import ChatKind, MsgKind
from ..store import document_observation, overlay_index, local_source, page_metadata, presence_index, page_inputs as raw_pages
from ..transport import authority_observation
from . import authority_source, epoch_inputs, events, membership_coordinator as membership
from . import page_fence, page_inputs, page_overlays, page_pins, page_receipts
from .overlay_source import OverlaySourceUnavailable
from .page_selection import CanonicalPageAccumulator, PageDependencyPending, select_exact_messages
from .paths import P
from .pin_storage import PinStoreUnavailable
from .redaction_verification import redaction_verifier
from .sealer import E2EESealer, PlainSealer


@dataclass(frozen=True)
class PageOperationLimits:
    max_rounds: int = 128
    max_steps: int = 8192
    max_bytes: int = 64 * 1024 * 1024
    max_crypto: int = 8192
    max_epochs: int = 64


@dataclass(frozen=True)
class PageWorkResult:
    status: str
    reason: str = ''
    work: tuple = ()
    prepared: _PreparedPage | None = field(default=None, repr=False)
    result: membership.CoordinatorResult | None = field(default=None, repr=False)


class _Work(RuntimeError):
    def __init__(self, kind, items=()):
        self.kind, self.items = kind, tuple(items)


class _Ledger:
    def __init__(self, limits):
        if type(limits) is not PageOperationLimits:
            raise ValueError('invalid page operation limits')
        copied = {}
        for name in limits.__dataclass_fields__:
            value = getattr(limits, name)
            if type(value) is not int or not 0 <= value <= getattr(PageOperationLimits(), name):
                raise ValueError('invalid page operation limit')
            copied[name] = value
        self.operation_limits = PageOperationLimits(**copied)
        # Per-round authority/lifecycle ceilings remain unchanged. Cached actor
        # lookups are free; this operation tracks repeated expensive work.
        self.limits = membership.CoordinatorLimits()
        self.bytes = self.steps = self.rounds = self.crypto = 0
        self.epochs = set()

    def charge(self, size):
        if type(size) is not int or size < 0:
            raise ValueError('invalid work charge')
        if size > self.operation_limits.max_bytes - self.bytes:
            raise membership._Stop('unavailable', 'operation_byte_budget')
        self.bytes += size

    def remaining(self):
        return min(4 * 1024 * 1024, self.operation_limits.max_bytes - self.bytes)

    def step(self):
        self.steps += 1
        if self.steps > self.operation_limits.max_steps:
            raise membership._Stop('unavailable', 'operation_step_budget')

    def start(self):
        self.rounds += 1
        if self.rounds > self.operation_limits.max_rounds:
            raise membership._Stop('unavailable', 'operation_round_budget')

    def unseal(self, env):
        self.crypto += 1
        if self.crypto > self.operation_limits.max_crypto:
            raise membership._Stop('unavailable', 'operation_crypto_budget')
        self.charge(sum(len(v.encode()) for v in (env.nonce, env.ct, env.sig) if isinstance(v, str)))

    def epoch(self, value):
        self.epochs.add(value)
        if len(self.epochs) > self.operation_limits.max_epochs:
            raise membership._Stop('unavailable', 'operation_epoch_budget')

    def signature(self, data):
        self.crypto += 1
        if self.crypto > self.operation_limits.max_crypto:
            raise membership._Stop('unavailable', 'operation_crypto_budget')
        self.charge(len(data))


class _Sealer:
    def __init__(self, round_, epochs):
        self.round, self.epochs = round_, epochs

    def unseal(self, chat, env):
        mesh, ledger = self.round.mesh, self.round.ledger
        if type(mesh.sealer) is PlainSealer:
            ledger.unseal(env)
            return mesh.sealer.unseal(chat, env)
        if env.epoch == 0 or not all(isinstance(v, str) for v in (env.nonce, env.ct, env.sig)):
            return None
        pub = self.round.sign_pub(env.from_)
        if not pub:
            return None
        ledger.unseal(env)
        ledger.epoch(env.epoch)
        if env.epoch not in self.epochs:
            local = self.round.source_reader
            source_args = {} if local is None else dict(source_reader=local,
                receipt=self.round.receipt, charge_source=ledger.charge, max_source_bytes=ledger.remaining())
            view = epoch_inputs.capture_epoch(mesh.keys, chat, env.epoch,
                max_bytes=min(epoch_inputs.MAX_IDENTITY_BYTES, ledger.remaining()), **source_args)
            ledger.charge(view.captured_bytes)
            if view.wrap is not None and (
                    (view.wrap.mirror != self.round.receipt.mirror) if local is None
                    else (view.wrap.receipt != self.round.receipt)):
                raise _Work('source_refresh')
            self.epochs[env.epoch] = view
            if view.resident is None:
                # Charge decode/unwrap and exact comparison as well as capture.
                ledger.charge(2 * view.captured_bytes)
                publish_args = {} if local is None else dict(source_reader=local, charge_source=ledger.charge)
                status = epoch_inputs.publish_epoch(mesh.keys, view, **publish_args)
                if status == 'readthrough':
                    raise _Work('epoch_readthrough', (P.keys(chat, env.epoch),))
                if status in ('published', 'identity_progress'):
                    raise membership._Stop('restart', status)
                if status != 'missing':
                    raise membership._Stop('restart', 'epoch_conflict')
        return mesh.sealer.unseal_observed(chat, env, pub, self.epochs[env.epoch].resident)


def _overlays(inputs):
    chat = inputs.position.messages.chat_id
    wanted = {P.edit(chat, r.key.id): ('edit', r.key.id) for r in inputs.rows + inputs.exact_rows}
    wanted.update({P.redaction(chat, r.key.id): ('redaction', r.key.id) for r in inputs.rows + inputs.exact_rows})
    if type(inputs.documents) is not document_observation.DocumentObservation:
        raise ValueError('invalid exact overlay capture')
    if inputs.documents.position != inputs.position.overlays.source:
        raise ValueError('inconsistent exact overlay source')
    records = inputs.documents.records
    if type(records) is not tuple or len(records) != len(wanted):
        raise ValueError('incomplete exact overlays')
    edits, redactions, seen = {}, {}, set()
    for record in records:
        if type(record) is not document_observation.SerializedDocumentRecord or record.path not in wanted or record.path in seen:
            raise ValueError('invalid exact overlay path')
        seen.add(record.path)
        if record.deleted:
            continue
        doc = json.loads(record.payload_json)
        if isinstance(doc, dict):
            kind, ident = wanted[record.path]
            (edits if kind == 'edit' else redactions)[ident] = doc
    return edits, redactions


def _failure(exc):
    if (isinstance(exc, membership.terminal_observation.TerminalObservationUnavailable)
            and exc.args == ('terminal_classification_pending',)):
        return PageWorkResult('work', 'terminal_classification_pending')
    if isinstance(exc, _Work):
        return PageWorkResult('work', exc.kind, exc.items)
    if isinstance(exc, authority_source.AuthorityReadThroughRequired):
        return PageWorkResult('work', 'account_readthrough', (exc.paths[0],))
    if isinstance(exc, membership._Stop):
        return PageWorkResult(exc.status, exc.reason)
    if isinstance(exc, MemoryError):
        return PageWorkResult('unavailable', 'resource_unavailable')
    if isinstance(exc, OverflowError):
        return PageWorkResult('unavailable', 'budget_exhausted')
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError)):
        return PageWorkResult('unavailable', 'invalid_inputs')
    if isinstance(exc, (OSError, membership.sqlite3.Error)):
        return PageWorkResult('unavailable', 'storage_unavailable')
    if isinstance(exc, PinStoreUnavailable):
        return PageWorkResult('unavailable', 'pins_unavailable')
    return PageWorkResult('unavailable', 'inputs_unavailable')


_ERRORS = (local_source.SourceChanged, membership._Stop, _Work, authority_source.AuthorityReadThroughRequired,
    page_metadata.PageMetadataUnavailable,
    presence_index.PresenceIndexUnavailable,
    authority_source.AuthoritySourceUnavailable, authority_observation.AuthorityObservationUnavailable,
    OverlaySourceUnavailable, overlay_index.OverlayIndexUnavailable, raw_pages.PageInputsChanged,
    page_overlays.PageOverlaysUnavailable, page_fence.PageFenceChanged,
    epoch_inputs.EpochInputsUnavailable, document_observation.DocumentObservationConflict,
    membership.lifecycle_inputs.LifecycleInputsUnavailable, membership.membership_suffix.MembershipSuffixUnavailable,
    membership.terminal_observation.TerminalObservationUnavailable, membership.MembershipInputUnavailable,
    membership.LifecycleUnavailable, PinStoreUnavailable, membership.sqlite3.Error,
    OSError, MemoryError, OverflowError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


class _PreparedPage:
    """Private one-use finalizer. GUI owner must hold its session gate outside."""
    def __init__(self, operation, serial, round_, snapshot, fence):
        self._operation, self._serial = operation, serial
        self._round, self._snapshot, self._fence = round_, snapshot, fence
        self._used = False

    def finalize(self):
        op = self._operation
        with op._lock:
            if self._used or self._serial != op._serial or not op._bound():
                return PageWorkResult('unavailable', 'operation_superseded')
            self._used = True
            try:
                result = self._round.final(self._snapshot, None, page=self._fence)
                return PageWorkResult(result.status, result.reason or '', result=result)
            except _ERRORS as exc:
                return _failure(exc)


class PageOperation:
    """One server-owned request; counters survive every discard/restart.

    Source/index/proof preparation and read-through are explicit caller work,
    never performed here. Each prepare invalidates any earlier finalizer.
    """
    def __init__(self, mesh, chat_id, *, before=None, expected_position=None, window_before=None,
                 limit=50, scan_budget=1000, limits=PageOperationLimits(), source_reader=None):
        authority_observation._part(chat_id)
        self.mesh, self.chat = mesh, chat_id
        if source_reader is not None:
            from .local_page_source import LocalPageSource
            if (type(source_reader) is not LocalPageSource or source_reader.store is not mesh.store
                    or source_reader.chat != chat_id):
                raise ValueError('invalid local page owner')
        self.source_reader = source_reader
        self._transport, self._store = mesh.tx, mesh.store
        self.viewer, self.machine = mesh.messaging.user, mesh.messaging.machine
        self.before = None if before is None else raw_pages._key(before)
        if (before is None) != (expected_position is None):
            raise ValueError('continuation requires its original page position')
        if window_before is not None:
            if before is not None or expected_position is not None:
                raise ValueError('window positioning cannot be a continuation')
            # Position only. Every prepare still captures current inputs and
            # recomputes membership, trust, keys and canonical visibility.
            self.before = raw_pages._key(window_before)
        self.expected_position = None
        if expected_position is not None:
            if type(expected_position) is not raw_pages.PageInputPosition:
                raise ValueError('invalid continuation position')
            self.expected_position = raw_pages.PageInputPosition(
                raw_pages.messages._copy_expected(expected_position.messages, str(mesh.store.path)),
                overlay_index._wanted(expected_position.overlays, mesh.store.path))
            if (self.expected_position.messages.chat_id != chat_id
                    or self.expected_position.overlays.chat_id != chat_id):
                raise ValueError('wrong continuation chat')
        # Use the selector's canonical argument validation, without reading data.
        CanonicalPageAccumulator(self.viewer, mesh.sealer, limit=limit, scan_budget=scan_budget)
        self.limit, self.scan_budget = limit, scan_budget
        self.ledger, self._lock, self._serial = _Ledger(limits), threading.RLock(), 0

    def _bound(self):
        return ((self.mesh.messaging.user, self.mesh.messaging.machine) == (self.viewer, self.machine)
                and self.mesh.tx is self._transport and self.mesh.store is self._store)

    def prepare(self, authority_receipt, overlay_receipt, index):
        with self._lock:
            self._serial += 1
            try:
                self.ledger.start()
                if not self._bound():
                    raise membership._Stop('unavailable', 'identity_changed')
                return self._prepare(authority_receipt, overlay_receipt, index)
            except _ERRORS as exc:
                return _failure(exc)

    def _prepare(self, authority_receipt, overlay_receipt, index):
        mesh, ledger = self.mesh, self.ledger
        round_ = membership._Round(mesh, authority_receipt, ledger, source_reader=self.source_reader)
        if round_.chat != self.chat or overlay_receipt.chat_id != self.chat:
            raise ValueError('wrong page chat')
        if self.source_reader is None:
            source_binding = overlay_receipt.mirror
            if round_.receipt.mirror != source_binding:
                raise _Work('source_refresh')
        else:
            source_binding = self.source_reader._receipt(overlay_receipt)
            if round_.receipt != source_binding:
                raise _Work('source_refresh')
            if index.source != source_binding.source.raw or index.chat_id != self.chat:
                raise ValueError('index belongs to another local source')
        if self.expected_position is not None and (
                self.expected_position.messages != round_.suffix.position
                or self.expected_position.overlays != index):
            raise membership._Stop('unavailable', 'continuation_changed')
        if type(mesh.sealer) not in (PlainSealer, E2EESealer):
            raise ValueError('unsupported page sealer')
        encrypted = type(mesh.sealer) is E2EESealer
        if encrypted and (mesh.sealer.keys is not mesh.keys or mesh.keys.tx is not mesh.tx
                          or mesh.keys.user != self.viewer):
            raise ValueError('inconsistent page key owner')
        epochs, proofs, captured = {}, {}, None
        viewer_state, page_starred = None, set()
        snapshot = None
        try:
            snapshot = events.advance(round_.snapshot,
                [json.loads(r.payload_json) for r in round_.suffix.rows], round_)
            if not snapshot.is_member(self.viewer):
                return PageWorkResult('forbidden', 'viewer_not_member')
            history = (snapshot.members[self.viewer].joined_ns
                       if snapshot.kind is ChatKind.GROUP and not snapshot.permissions.send_history else 0)
            sealer = _Sealer(round_, epochs)
            accumulator = CanonicalPageAccumulator(self.viewer, sealer, limit=self.limit, scan_budget=self.scan_budget)
            before, expected, exact, proof_keys = self.before, self.expected_position, (), ()
            verifier = redaction_verifier(self.chat, round_) if encrypted else None
            while True:
                ledger.step()
                consumed = accumulator._selection.raw_examined if accumulator._selection is not None else 0
                raw_limit = min(raw_pages.MAX_RAW_ROWS, self.scan_budget - consumed)
                selection_args = dict(before=before, expected=expected, raw_limit=raw_limit,
                    exact_ids=exact, state_paths=(P.state(self.chat, self.viewer),),
                    proof_keys=proof_keys, include_reactions=True, max_bytes=ledger.remaining())
                if self.source_reader is None:
                    inputs = page_inputs.capture_page_inputs(mesh.tx, mesh.store,
                        overlay_receipt, index, **selection_args)
                else:
                    inputs = self.source_reader.capture_page(source_binding, index, **selection_args)
                ledger.charge(inputs.captured_bytes)
                if inputs.position.messages != round_.suffix.position:
                    raise page_fence.PageFenceChanged('page_membership_cut_changed')
                captured, expected = inputs, inputs.position
                for path, key, valid in inputs.proofs:
                    old = proofs.get((path, key), valid)
                    if old != valid:
                        raise page_fence.PageFenceChanged('page_proofs_changed')
                    proofs[(path, key)] = valid
                if len(proofs) > overlay_index.MAX_DEPENDENCIES:
                    raise membership._Stop('unavailable', 'operation_proof_budget')
                try:
                    overlays = page_overlays.assemble_page_overlays(inputs, self.viewer, snapshot,
                        directory=round_, crypto_boundary=encrypted)
                except page_overlays.PageOverlayProofsPending as pending:
                    requested = tuple(dict.fromkeys(proof_keys + pending.keys))
                    if requested == proof_keys:
                        raise _Work('overlay_proofs', pending.keys) from pending
                    proof_keys = requested
                    continue
                if viewer_state is None:
                    viewer_state = overlays.viewer_state
                elif viewer_state != overlays.viewer_state:
                    raise page_fence.PageFenceChanged('viewer_state_changed_between_windows')
                edits, redactions = _overlays(inputs)
                try:
                    more = accumulator.feed(inputs, edits=edits, redactions=redactions,
                        reactions=overlays.reactions, state=overlays.state,
                        tenure=snapshot.tenure, history_from_ns=history,
                        owner_of=round_.owner_of, verify_redaction=verifier)
                except PageDependencyPending as parents:
                    exact = tuple(sorted(set(exact).union(parents.ids)))
                    if len(exact) > raw_pages.MAX_EXACT_IDS:
                        raise membership._Stop('unavailable', 'operation_parent_budget') from parents
                    continue
                page_starred.update(overlays.state.get('starred', ()))
                if not more:
                    selection = accumulator.finish()
                    pins_json = self._pin_presentation(round_, snapshot, source_binding, index,
                        expected, sealer, history, verifier, proofs) if self.source_reader is not None else None
                    receipts_json, presence = self._receipt_presentation(round_, snapshot,
                        source_binding, index, expected, selection, proofs)
                    snapshot_json = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(',', ':'))
                    viewer_metadata = {
                        'read_ns': int((viewer_state or {}).get('read_ns', 0)),
                        'archived': bool((viewer_state or {}).get('archived')),
                    }
                    if snapshot.kind is ChatKind.DM:
                        account = round_.get(self.viewer)
                        other = next((name for name in snapshot.members if name != self.viewer), None)
                        viewer_metadata['blocked'] = bool(other and account and other in account.blocked)
                    viewer_state_json = json.dumps(viewer_metadata, sort_keys=True, separators=(',', ':'))
                    visible = {message.id for message in selection.messages}
                    starred = tuple(message.id for message in selection.messages
                                    if message.id in page_starred and message.id in visible)
                    ledger.charge(len(snapshot_json.encode()) + len(viewer_state_json.encode())
                                  + sum(len(ident.encode()) for ident in starred))
                    presentation = page_fence.PagePresentation(snapshot_json, viewer_state_json, starred,
                                                               pins_json, receipts_json)
                    fence = page_fence.PageFence(expected, source_binding,
                        tuple((p, k, v) for (p, k), v in sorted(proofs.items())), tuple(epochs.values()),
                        selection, presentation, presence)
                    return PageWorkResult('prepared', prepared=_PreparedPage(self, self._serial, round_, snapshot, fence))
                before = accumulator._selection.oldest_examined
                exact, proof_keys = (), ()
        except membership._Proposal as ready:
            if ready.value is None:
                raise membership._Stop('unavailable', 'invalid_proposal') from ready
            fence = None if captured is None else page_fence.PageFence(captured.position,
                source_binding, tuple((p, k, v) for (p, k), v in sorted(proofs.items())),
                tuple(epochs.values()), None)
            result = round_.final(None, ready.value, page=fence)
            return PageWorkResult(result.status, result.reason or '')

    def _pin_presentation(self, round_, snapshot, receipt, index, expected,
                          sealer, history, verifier, proofs):
        manifest = self.source_reader.capture_pin_manifest(receipt, index)
        if manifest.position != expected:
            raise page_fence.PageFenceChanged('pin_manifest_cut_changed')
        pins = page_pins.verified_pins(manifest, snapshot, round_,
                                     encrypted=type(self.mesh.sealer) is E2EESealer)
        targets, exact, keys = tuple(sorted(pins)), tuple(sorted(pins)), ()
        if not targets:
            return '[]'
        while True:
            self.ledger.step()
            inputs = self.source_reader.capture_page(receipt, index, raw_limit=0,
                expected=expected, exact_ids=exact, state_paths=(P.state(self.chat, self.viewer),),
                proof_keys=keys, include_reactions=True, max_bytes=self.ledger.remaining())
            self.ledger.charge(inputs.captured_bytes)
            for path, key, valid in inputs.proofs:
                if proofs.get((path, key), valid) != valid:
                    raise page_fence.PageFenceChanged('pin_proofs_changed')
                proofs[(path, key)] = valid
            if len(proofs) > overlay_index.MAX_DEPENDENCIES:
                raise membership._Stop('unavailable', 'operation_proof_budget')
            try:
                overlays = page_overlays.assemble_page_overlays(inputs, self.viewer, snapshot,
                    directory=round_, crypto_boundary=type(self.mesh.sealer) is E2EESealer)
            except page_overlays.PageOverlayProofsPending as pending:
                wanted = tuple(dict.fromkeys(keys + pending.keys))
                if wanted == keys:
                    raise _Work('overlay_proofs', pending.keys) from pending
                keys = wanted
                continue
            edits, redactions = _overlays(inputs)
            try:
                messages = select_exact_messages(inputs, targets, self.viewer, sealer,
                    edits=edits, redactions=redactions, reactions=overlays.reactions,
                    state=overlays.state, tenure=snapshot.tenure, history_from_ns=history,
                    owner_of=round_.owner_of, verify_redaction=verifier)
            except PageDependencyPending as parents:
                exact = tuple(sorted(set(exact).union(parents.ids)))
                if len(exact) > raw_pages.MAX_EXACT_IDS:
                    raise membership._Stop('unavailable', 'pin_parent_budget') from parents
                continue
            shown = sorted((m for m in messages if not m.deleted),
                           key=lambda m: (m.ns, m.from_, m.id), reverse=True)
            encoded = json.dumps([{'id': m.id, 'until': pins[m.id], 'body': m.body, 'ns': m.ns}
                                  for m in shown], ensure_ascii=False, allow_nan=False)
            self.ledger.charge(len(encoded.encode()))
            if len(encoded.encode()) > page_metadata.MAX_PIN_BYTES:
                raise OverflowError('pins presentation byte budget')
            return encoded

    def _receipt_presentation(self, round_, snapshot, receipt, index, expected, selection, proofs):
        if self.source_reader is None:
            return None, None
        own = tuple(m for m in selection.messages if m.from_ == self.viewer
                    and m.kind is MsgKind.MESSAGE and not m.deleted)
        if not own:
            return '{}', None
        members = tuple(sorted(name for name in snapshot.members if name != self.viewer))
        if len(set(members).union(round_.accounts, (self.viewer,))) > round_.ledger.limits.max_accounts:
            return None, None
        presence = None
        cursors, visible, floors = {}, {}, {}
        if members:
            runtime = getattr(self.mesh, 'local_inputs', None)
            source = None if runtime is None else runtime.presence
            if source is None:
                return None, None
            source.request()  # Queue only; collection never runs on the request.
            try:
                _reader, presence_receipt, observed = source.inputs(members)
            except (local_source.SourceChanged, presence_index.PresenceIndexUnavailable,
                    OSError, membership.sqlite3.Error):
                return None, None  # Unknown delivery floors are not Sent receipts.
            # Presentation freshness only. A recent ingestion still cannot
            # prove a hard remote-staleness bound or authorize this viewer.
            if not 0 <= round_.now - observed.observed_ns < 30 * 1_000_000_000:
                return None, None
            freshness_end = observed.observed_ns + 30 * 1_000_000_000
            round_.deadline = freshness_end if round_.deadline is None else min(round_.deadline, freshness_end)
            presence = page_fence.PresenceFence(presence_receipt, observed)
            floors = dict(observed.floors)
            keys = ()
            while True:
                self.ledger.step()
                inputs = self.source_reader.capture_page(receipt, index, raw_limit=0,
                    expected=expected, exact_ids=(), state_paths=tuple(P.state(self.chat, m) for m in members),
                    proof_keys=keys, include_reactions=False, max_bytes=self.ledger.remaining())
                self.ledger.charge(inputs.captured_bytes)
                for path, key, valid in inputs.proofs:
                    if proofs.get((path, key), valid) != valid:
                        raise page_fence.PageFenceChanged('receipt_proofs_changed')
                    proofs[(path, key)] = valid
                if len(proofs) > overlay_index.MAX_DEPENDENCIES:
                    raise membership._Stop('unavailable', 'operation_proof_budget')
                try:
                    cursors = page_receipts.verified_cursors(inputs, members, round_,
                        crypto_boundary=type(self.mesh.sealer) is E2EESealer)
                    break
                except page_overlays.PageOverlayProofsPending as pending:
                    wanted = tuple(dict.fromkeys(keys + pending.keys))
                    if wanted == keys:
                        raise _Work('overlay_proofs', pending.keys) from pending
                    keys = wanted
            visible = page_receipts.receipt_visibility(self.viewer, members, round_)
        values = page_receipts.assemble_receipts(own, self.viewer, members, cursors, visible, floors)
        encoded = json.dumps(values, sort_keys=True, separators=(',', ':'), allow_nan=False)
        self.ledger.charge(len(encoded.encode()))
        if len(encoded.encode()) > 1024 * 1024:
            raise OverflowError('receipt presentation byte budget')
        return encoded, presence
