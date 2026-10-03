"""The production page constructor has one explicit local-input authority mode."""
from types import SimpleNamespace

import pytest

from agentbridge.gui.api_page_aux import AuxiliaryPageOperation
from agentbridge.gui.attachment_read import _ObservedBlobPage
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.page_owner_asks import OwnerAskPageOperation


def _construct(kind, mesh, **kwargs):
    if kind == 'auxiliary':
        return AuxiliaryPageOperation(None, mesh, 'room', **kwargs)
    if kind == 'owner_ask':
        return OwnerAskPageOperation(mesh, 'room', **kwargs)
    if kind == 'attachment':
        return _ObservedBlobPage(mesh, 'room', sealed=b'', blob_id='blob', **kwargs)
    return PageOperation(mesh, 'room', **kwargs)


@pytest.mark.parametrize('kind', ['page', 'auxiliary', 'owner_ask', 'attachment'])
def test_missing_source_reader_is_an_explicit_required_keyword(kind):
    # No Mesh/Store/provider is needed or inspected for a missing argument.
    with pytest.raises(TypeError, match='source_reader'):
        _construct(kind, object())


@pytest.mark.parametrize('kind', ['page', 'auxiliary', 'owner_ask', 'attachment'])
@pytest.mark.parametrize('invalid', ['none', 'wrong_type', 'wrong_store', 'wrong_chat'])
def test_invalid_local_reader_fails_before_capture_or_side_effects(kind, invalid):
    # Deliberately incomplete objects make any attempt to capture/read/resolve
    # before structural validation fail. The owner mismatch must be the error.
    mesh = SimpleNamespace(store=object())
    reader = None if invalid == 'none' else object()
    if invalid in ('wrong_store', 'wrong_chat'):
        reader = object.__new__(LocalPageSource)
        reader.store = object() if invalid == 'wrong_store' else mesh.store
        reader.chat = 'other' if invalid == 'wrong_chat' else 'room'
    with pytest.raises(ValueError, match='requires a LocalPageSource for this chat and Store'):
        _construct(kind, mesh, source_reader=reader)
