"""Internal final checks for a computed local canonical page (not a lease)."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from ..store import overlay_index, page_inputs
from ..transport import key_observation
from ..transport.authority_observation import _position_locked
from ..transport.mirror_observation import MirrorExpectedPosition
from . import epoch_inputs, local_key_inputs
from .local_page_source import LocalSourceReceipt
from .page_selection import PageSelection


class PageFenceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class PageFence:
    position: page_inputs.PageInputPosition
    source: MirrorExpectedPosition | LocalSourceReceipt
    proofs: tuple[tuple[str, str, bool | None], ...]
    epochs: tuple[epoch_inputs.EpochObservation, ...] = field(repr=False)
    selection: PageSelection | None = field(repr=False)


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
    owned = PageFence(position, source, value.proofs, epochs, selected)
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
    selected = tuple((path, key) for path, key, _valid in fence.proofs)
    return overlay_index._proofs(conn, mesh.store.path, fence.position.overlays, selected) == tuple(
        valid for _path, _key, valid in fence.proofs)


def matches_mirror_locked(mesh, prepared):
    # The caller already holds the SAME nonreentrant transport mutex.
    return (_position_locked(mesh.tx) == prepared.fence.source
            and key_observation._matches_key_wraps_locked(mesh.tx, prepared.wraps))
