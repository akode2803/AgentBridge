"""Internal final checks for a computed local canonical page (not a lease)."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import json

from ..store import overlay_index, page_inputs, presence_index
from ..transport import key_observation
from ..transport.authority_observation import _position_locked
from ..transport.mirror_observation import MirrorExpectedPosition
from . import epoch_inputs, local_key_inputs
from .local_page_source import LocalSourceReceipt
from .local_presence_source import LocalPresenceSource, PresenceSourceReceipt
from .page_selection import PageSelection


class PageFenceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class PagePresentation:
    """Bounded request-only data; never a continuing membership authority."""
    snapshot_json: str
    viewer_state_json: str
    starred: tuple[str, ...]
    pins_json: str | None = None
    receipts_json: str | None = None


@dataclass(frozen=True)
class PresenceFence:
    receipt: PresenceSourceReceipt
    inputs: presence_index.PresenceInputs


@dataclass(frozen=True)
class PageFence:
    position: page_inputs.PageInputPosition
    source: MirrorExpectedPosition | LocalSourceReceipt
    proofs: tuple[tuple[str, str, bool | None], ...]
    epochs: tuple[epoch_inputs.EpochObservation, ...] = field(repr=False)
    selection: PageSelection | None = field(repr=False)
    presentation: PagePresentation | None = field(default=None, repr=False)
    presence: PresenceFence | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PreparedPageFence:
    fence: PageFence
    wraps: tuple = field(repr=False)
    comparison_bytes: int


def prepare(mesh, value, suffix_position, authority_source, *, source_reader=None, charge_source=None):
    if type(value) is not PageFence or type(value.position) is not page_inputs.PageInputPosition:
        raise ValueError('invalid page fence')
    position = page_inputs.PageInputPosition(
        page_inputs.messages._copy_expected(value.position.messages, str(mesh.store.path)),
        overlay_index._wanted(value.position.overlays, mesh.store.path))
    if source_reader is None:
        if type(value.source) is not MirrorExpectedPosition:
            raise ValueError('invalid page mirror')
        source = MirrorExpectedPosition(value.source.root_identity, value.source.cache_identity,
                                        value.source.instance_nonce, value.source.revision)
    else:
        source = source_reader._receipt(value.source)
        if source.chat_id != position.messages.chat_id or position.overlays.source != source.source.raw:
            raise PageFenceChanged('page_local_source_changed')
    # All page sources and demanded keys must belong to this exact local cut.
    if position.messages != suffix_position or source != authority_source:
        raise PageFenceChanged('page_authority_cut_changed')
    if (type(value.proofs) is not tuple or len(value.proofs) > overlay_index.MAX_DEPENDENCIES
            or type(value.epochs) is not tuple or len(value.epochs) > 64):
        raise ValueError('invalid page fence bounds')
    size, keys = 0, set()
    for row in value.proofs:
        if type(row) is not tuple or len(row) != 3:
            raise ValueError('invalid page proof')
        path, public, valid = row
        overlay_index._doc_path(path, position.messages.chat_id)
        overlay_index._key(public)
        if valid is not None and type(valid) is not bool:
            raise ValueError('invalid page proof value')
        if (path, public) in keys:
            raise ValueError('duplicate page proof')
        keys.add((path, public))
        size += len(path.encode()) + len(public.encode()) + 1
    epochs = tuple(epoch_inputs._copy(mesh.keys, v, source_reader=source_reader) for v in value.epochs)
    if len({(v.chat_id, v.epoch) for v in epochs}) != len(epochs):
        raise ValueError('duplicate page epoch')
    for view in epochs:
        if view.chat_id != position.messages.chat_id or view.viewer != mesh.messaging.user:
            raise ValueError('wrong page epoch binding')
        if view.wrap is not None:
            binding = view.wrap.mirror if source_reader is None else view.wrap.receipt
            if binding != authority_source:
                raise PageFenceChanged('key_authority_cut_changed')
        size += view.captured_bytes
    selected = value.selection
    if selected is not None and (type(selected) is not PageSelection or selected.position != position):
        raise ValueError('inconsistent canonical page selection')
    presentation = value.presentation
    if presentation is not None:
        if selected is None or type(presentation) is not PagePresentation:
            raise ValueError('page presentation requires a selection')
        if (type(presentation.snapshot_json) is not str
                or type(presentation.viewer_state_json) is not str
                or type(presentation.starred) is not tuple):
            raise ValueError('invalid page presentation')
        if (len(presentation.snapshot_json.encode()) > 4 * 1024 * 1024
                or len(presentation.viewer_state_json.encode()) > 65536
                or len(presentation.starred) > 200):
            raise OverflowError('page presentation exceeds budget')
        state = json.loads(presentation.viewer_state_json)
        if (type(state) is not dict or set(state) not in ({'read_ns', 'archived'}, {'read_ns', 'archived', 'blocked'})
                or type(state['read_ns']) is not int
                or type(state['archived']) is not bool
                or ('blocked' in state and type(state['blocked']) is not bool)):
            raise ValueError('invalid viewer presentation')
        visible = {msg.id for msg in selected.messages}
        if (any(type(ident) is not str or ident not in visible for ident in presentation.starred)
                or len(set(presentation.starred)) != len(presentation.starred)):
            raise ValueError('starred IDs outside selected page')
        size += sum(len(ident.encode()) for ident in presentation.starred)
        size += len(presentation.snapshot_json.encode()) + len(presentation.viewer_state_json.encode())
        presentation = PagePresentation(presentation.snapshot_json,
                                        presentation.viewer_state_json, tuple(presentation.starred),
                                        presentation.pins_json, presentation.receipts_json)
        if presentation.pins_json is not None:
            if type(presentation.pins_json) is not str:
                raise ValueError('invalid pins presentation')
            size += len(presentation.pins_json.encode())
            if len(presentation.pins_json.encode()) > 1024 * 1024:
                raise OverflowError('pins presentation budget')
            pins = json.loads(presentation.pins_json)
            if type(pins) is not list or len(pins) > 64:
                raise ValueError('invalid pins presentation')
            seen = set()
            for pin in pins:
                if (type(pin) is not dict or set(pin) != {'id', 'until', 'body', 'ns'}
                        or type(pin['id']) is not str or not pin['id'] or pin['id'] in seen
                        or type(pin['until']) is not int or type(pin['ns']) is not int
                        or type(pin['body']) is not str):
                    raise ValueError('invalid pin presentation')
                seen.add(pin['id'])
        if presentation.receipts_json is not None:
            if type(presentation.receipts_json) is not str:
                raise ValueError('invalid receipt presentation')
            used = len(presentation.receipts_json.encode())
            if used > 1024 * 1024:
                raise OverflowError('receipt presentation budget')
            receipts = json.loads(presentation.receipts_json)
            if type(receipts) is not dict or not set(receipts).issubset(visible):
                raise ValueError('receipt IDs outside selected page')
            size += used
    presence = value.presence
    if presence is not None:
        if source_reader is None or type(presence) is not PresenceFence:
            raise ValueError('invalid companion presence fence')
        reader = LocalPresenceSource(source_reader.coordinator, mesh.store)
        receipt = reader._receipt(presence.receipt)
        inputs = presence.inputs
        if (type(inputs) is not presence_index.PresenceInputs
                or inputs.position != receipt.source.raw
                or type(inputs.build) is not str or len(inputs.build) != 32
                or type(inputs.floors) is not tuple or len(inputs.floors) > presence_index.MAX_MEMBERS
                or type(inputs.observed_ns) is not int or not 0 <= inputs.observed_ns <= 2**63 - 1):
            raise ValueError('invalid presence input position')
        floors = tuple((presence_index._user(name), presence_index._floor(ns))
                       for name, ns in inputs.floors)
        if len({name for name, _ in floors}) != len(floors):
            raise ValueError('duplicate presence subject')
        size += sum(len(name.encode()) + 16 for name, _ in floors)
        presence = PresenceFence(receipt, presence_index.PresenceInputs(inputs.position, inputs.build, floors,
                                                                       inputs.observed_ns))
    owned = PageFence(position, source, value.proofs, epochs, selected, presentation, presence)
    wraps = tuple(v.wrap for v in epochs if v.wrap is not None)
    if source_reader is None:
        prepared_wraps = key_observation._prepare_key_wraps(wraps)
    else:
        prepared_wraps = tuple(local_key_inputs.prepare(source_reader, v,
            charge_source=charge_source) for v in wraps)
        size += sum(v.source_bytes for v in wraps)
    return PreparedPageFence(owned, prepared_wraps, size)


@contextmanager
def local_scope(mesh, prepared, *, source_reader=None):
    # Keep all local epochs held while the caller acquires pins/SQLite/mirror.
    with ExitStack() as stack:
        for view in prepared.fence.epochs:
            if not stack.enter_context(epoch_inputs.locked_matching_epoch_local(mesh.keys, view, source_reader=source_reader)):
                raise PageFenceChanged('epoch_inputs_changed')
        yield


def matches_store(conn, mesh, prepared, *, source_reader=None):
    fence = prepared.fence
    if source_reader is not None:
        if not source_reader.matches_in_transaction(conn, fence.source):
            return False
        if not all(local_key_inputs._matches_prepared_in_transaction(source_reader, conn, v)
                   for v in prepared.wraps):
            return False
    elif type(fence.source) is not MirrorExpectedPosition:
        raise ValueError('local page requires its source reader')
    if not page_inputs.matches_position(conn, mesh.store.path, fence.position):
        return False
    if fence.presence is not None:
        if source_reader is None:
            return False
        reader = LocalPresenceSource(source_reader.coordinator, mesh.store)
        evidence = fence.presence
        if not reader.matches_in_transaction(conn, evidence.receipt):
            return False
        if reader.capture_in_transaction(conn, evidence.receipt,
                                         tuple(name for name, _ in evidence.inputs.floors)) != evidence.inputs:
            return False
    selected = tuple((path, key) for path, key, _valid in fence.proofs)
    return overlay_index._proofs(conn, mesh.store.path, fence.position.overlays, selected) == tuple(
        valid for _path, _key, valid in fence.proofs)


def matches_mirror_locked(mesh, prepared):
    # The caller already holds the SAME nonreentrant transport mutex.
    return (_position_locked(mesh.tx) == prepared.fence.source
            and key_observation._matches_key_wraps_locked(mesh.tx, prepared.wraps))
