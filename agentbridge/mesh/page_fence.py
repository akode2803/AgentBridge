"""Internal final checks for a computed local canonical page (not a lease)."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from ..store import overlay_index, page_inputs
from ..transport import key_observation
from ..transport.authority_observation import _position_locked
from ..transport.mirror_observation import MirrorExpectedPosition
from . import epoch_inputs
from .page_selection import PageSelection


class PageFenceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class PageFence:
    position: page_inputs.PageInputPosition
    mirror: MirrorExpectedPosition
    proofs: tuple[tuple[str, str, bool | None], ...]
    epochs: tuple[epoch_inputs.EpochObservation, ...] = field(repr=False)
    selection: PageSelection | None = field(repr=False)


@dataclass(frozen=True)
class PreparedPageFence:
    fence: PageFence
    wraps: tuple = field(repr=False)
    comparison_bytes: int


def prepare(mesh, value, suffix_position, authority_mirror):
    if type(value) is not PageFence or type(value.position) is not page_inputs.PageInputPosition:
        raise ValueError('invalid page fence')
    position = page_inputs.PageInputPosition(
        page_inputs.messages._copy_expected(value.position.messages, str(mesh.store.path)),
        overlay_index._wanted(value.position.overlays, mesh.store.path))
    if type(value.mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid page mirror')
    mirror = MirrorExpectedPosition(value.mirror.root_identity, value.mirror.cache_identity,
                                    value.mirror.instance_nonce, value.mirror.revision)
    # All page sources and demanded keys must belong to this exact local cut.
    if position.messages != suffix_position or mirror != authority_mirror:
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
    epochs = tuple(epoch_inputs._copy(mesh.keys, v) for v in value.epochs)
    if len({(v.chat_id, v.epoch) for v in epochs}) != len(epochs):
        raise ValueError('duplicate page epoch')
    for view in epochs:
        if view.chat_id != position.messages.chat_id or view.viewer != mesh.messaging.user:
            raise ValueError('wrong page epoch binding')
        if view.wrap is not None and view.wrap.mirror != authority_mirror:
            raise PageFenceChanged('key_authority_cut_changed')
        size += view.captured_bytes
    selected = value.selection
    if selected is not None and (type(selected) is not PageSelection or selected.position != position):
        raise ValueError('inconsistent canonical page selection')
    owned = PageFence(position, mirror, value.proofs, epochs, selected)
    return PreparedPageFence(owned, key_observation._prepare_key_wraps(
        tuple(v.wrap for v in epochs if v.wrap is not None)), size)


@contextmanager
def local_scope(mesh, prepared):
    # Keep all local epochs held while the caller acquires pins/SQLite/mirror.
    with ExitStack() as stack:
        for view in prepared.fence.epochs:
            if not stack.enter_context(epoch_inputs.locked_matching_epoch_local(mesh.keys, view)):
                raise PageFenceChanged('epoch_inputs_changed')
        yield


def matches_store(conn, mesh, prepared):
    fence = prepared.fence
    if not page_inputs.matches_position(conn, mesh.store.path, fence.position):
        return False
    selected = tuple((path, key) for path, key, _valid in fence.proofs)
    return overlay_index._proofs(conn, mesh.store.path, fence.position.overlays, selected) == tuple(
        valid for _path, _key, valid in fence.proofs)


def matches_mirror_locked(mesh, prepared):
    # The caller already holds the SAME nonreentrant transport mutex.
    return (_position_locked(mesh.tx) == prepared.fence.mirror
            and key_observation._matches_key_wraps_locked(mesh.tx, prepared.wraps))
