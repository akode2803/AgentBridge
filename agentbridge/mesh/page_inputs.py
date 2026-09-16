"""Live-source bracketing for raw page inputs; canonical authority stays outside."""
from __future__ import annotations

from dataclasses import replace

from ..store import document_observation, overlay_index
from ..transport.mirror_observation import MirrorExpectedPosition

from ..store.page_inputs import RawPageInputs
from .overlay_source import capture_overlay_inputs


def capture_page_inputs(transport, store, receipt, index, **selection) -> RawPageInputs:
    """Capture one current local cut, never an authorization or paging lease.

    Callers still perform current session/membership/key/trust checks and visible
    folding. A second dependency capture supplies the first result's position;
    message/source/build changes then reject rather than mix generations.
    """
    from ..store.overlay_index import OverlayIndexPosition
    from .overlay_source import OverlaySourceReceipt

    if type(index) is not OverlayIndexPosition or type(receipt) is not OverlaySourceReceipt:
        raise TypeError('expected index position and source receipt')
    index = overlay_index._wanted(index, store.path)
    chat, position, mirror = receipt.chat_id, receipt.position, receipt.mirror
    if type(mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid mirror position')
    receipt = OverlaySourceReceipt(chat, document_observation._validate_expected(position, store.path),
                                   replace(mirror))
    if index.source != receipt.position or index.chat_id != receipt.chat_id:
        raise ValueError('index and source receipt do not match')
    capture_overlay_inputs(transport, store, receipt, ())
    result = store.capture_page_inputs(index, **selection)
    store.capture_page_inputs(index, expected=result.position, raw_limit=0)
    capture_overlay_inputs(transport, store, receipt, ())
    return result
