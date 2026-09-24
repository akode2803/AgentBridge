"""Process-local pagination handles carry raw seek state, never authority."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge.gui.context import SessionReadToken
from agentbridge.gui.page_cursors import PageCursorRegistry
from agentbridge.mesh.page_selection import PageSelection
from agentbridge.store.document_observation import DocumentPosition
from agentbridge.store.membership_input_position import MembershipInputPosition
from agentbridge.store.overlay_index import OverlayIndexPosition
from agentbridge.store.page_inputs import MessageKey, PageInputPosition


class ViewerMesh:
    def __init__(self, user, tmp_path):
        self.user = user
        self.store = SimpleNamespace(path=tmp_path / 'db.sqlite')


def selection(tmp_path, *, before=MessageKey(7, 'bob', 'b'), chat='room',
              more=True, visible=()):
    path = str(tmp_path / 'db.sqlite')
    pos = PageInputPosition(
        MembershipInputPosition(path, 'a' * 32, 'b' * 64, chat, 1),
        OverlayIndexPosition(
            DocumentPosition(path, 'a' * 32, 'mirror:room', 1, 1, True),
            chat, 'c' * 32,
        ),
    )
    return PageSelection(pos, visible, before, 1, 0, 0, more,
                         not more, False, False)


def test_raw_composite_seek_key_and_detached_position_survive_empty_filtered_page(tmp_path):
    registry = PageCursorRegistry()
    session = SessionReadToken('app', 1, ViewerMesh('alice', tmp_path))
    page = selection(tmp_path, before=MessageKey(7, 'bob', 'b'))
    token = registry.issue(session, 'room', page)
    assert len(token) == 64 and token.isascii()
    continuation = registry.resolve(token, session, 'room')
    assert continuation.before == MessageKey(7, 'bob', 'b')
    assert continuation.position == page.position
    assert continuation.position is not page.position
    assert continuation.position.messages is not page.position.messages
    assert continuation.position.overlays is not page.position.overlays
    assert continuation.before is not page.oldest_examined
    assert not hasattr(continuation, 'messages')
    # The next SQL seek must include sender and ID even when ns ties.
    assert MessageKey(7, 'alice', 'z') < continuation.before
    assert MessageKey(7, 'bob', 'a') < continuation.before
    assert registry.issue(session, 'room', replace(page, has_more=False)) is None
    assert registry.issue(session, 'room', replace(page, has_more=False,
                                                   oldest_examined=None, raw_examined=0)) is None
    assert registry.issue(session, 'room', replace(page,
                                                   oldest_examined=None, raw_examined=0)) is None


def test_tampering_and_cross_session_viewer_chat_rejected(tmp_path):
    registry = PageCursorRegistry()
    mesh = ViewerMesh('alice', tmp_path)
    session = SessionReadToken('app', 3, mesh)
    token = registry.issue(session, 'room', selection(tmp_path))
    for invalid in (token[:-1], token[:-1] + ('0' if token[-1] != '0' else '1'),
                    token.upper(), token + '0', 'x' * 1_000_000, None):
        assert registry.resolve(invalid, session, 'room') is None
    assert registry.resolve(token, SessionReadToken('other', 3, mesh), 'room') is None
    assert registry.resolve(token, SessionReadToken('app', 4, mesh), 'room') is None
    assert registry.resolve(token, SessionReadToken('app', 3, ViewerMesh('alice', tmp_path)), 'room') is None
    assert registry.resolve(token, session, 'another') is None
    mesh.user = 'another'
    assert registry.resolve(token, session, 'room') is None
    mesh.user = 'alice'
    assert registry.resolve(token, session, 'room') is not None
    registry.clear()
    assert registry.resolve(token, session, 'room') is None


def test_ttl_lru_and_strict_expiration(tmp_path):
    now = [10.0]
    registry = PageCursorRegistry(clock=lambda: now[0])
    session = SessionReadToken('app', 1, ViewerMesh('alice', tmp_path))
    page = selection(tmp_path)
    first = registry.issue(session, 'room', page)
    now[0] += 1
    middle = [registry.issue(session, 'room', page) for _ in range(127)]
    assert registry.resolve(first, session, 'room') is not None
    extra = registry.issue(session, 'room', page)
    assert registry.resolve(middle[0], session, 'room') is None
    assert registry.resolve(first, session, 'room') is not None
    assert registry.resolve(extra, session, 'room') is not None
    now[0] = 910.0
    assert registry.resolve(first, session, 'room') is None
    assert registry.resolve(extra, session, 'room') is not None
    now[0] = 911.0
    assert registry.resolve(extra, session, 'room') is None


def test_bad_positions_and_incomplete_pages_never_issue(tmp_path):
    registry = PageCursorRegistry()
    session = SessionReadToken('app', 1, ViewerMesh('alice', tmp_path))
    page = selection(tmp_path)
    for malformed in (
        replace(page, oldest_examined=MessageKey(0, 'bob', 'b')),
        replace(page, oldest_examined=MessageKey(7, 'bob', '')),
        replace(page, needs_more_input=True),
        replace(page, raw_examined=0),
        replace(page, position=replace(page.position,
                                        overlays=replace(page.position.overlays, chat_id='other'))),
    ):
        with pytest.raises((ValueError, TypeError)):
            registry.issue(session, 'room', malformed)
    with pytest.raises(ValueError):
        registry.issue(session, 'another', page)
    oversized = replace(page.position.messages, database_path='a' * 4096)
    oversized_source = replace(page.position.overlays.source,
                               database_path=oversized.database_path)
    long_key = MessageKey(7, 'x' * 4096, 'y' * 4096)
    with pytest.raises(ValueError, match='byte budget'):
        registry.issue(session, 'room', replace(page, oldest_examined=long_key,
                                               position=PageInputPosition(
            oversized, replace(page.position.overlays, source=oversized_source))))


def test_version_is_stable_for_raw_position_but_fenced_by_session_and_generation(tmp_path):
    registry = PageCursorRegistry()
    mesh = ViewerMesh('alice', tmp_path)
    session = SessionReadToken('app', 1, mesh)
    page = selection(tmp_path)
    version = registry.version(session, 'room', page)
    assert len(version) == 64 and version.isascii()
    assert 'room' not in version and str(mesh.store.path) not in version
    assert registry.version(session, 'room', replace(
        page, oldest_examined=MessageKey(7, 'alice', 'a'))) == version
    assert registry.version(session, 'room', replace(
        page, position=replace(page.position, messages=replace(
            page.position.messages, generation=2)))) != version
    assert registry.version(session, 'room', replace(
        page, position=replace(page.position, overlays=replace(
            page.position.overlays, build='d' * 32)))) != version
    assert registry.version(SessionReadToken('app', 2, mesh), 'room', page) != version
    assert registry.version(SessionReadToken('other', 1, mesh), 'room', page) != version
    assert registry.version(session, 'room', page,
                            local_trust_version='1' * 64) != version
    assert registry.version(session, 'room', page,
                            local_trust_version='2' * 64) != registry.version(
                                session, 'room', page, local_trust_version='1' * 64)
    with pytest.raises(ValueError, match='trust input'):
        registry.version(session, 'room', page, local_trust_version='raw-path')
    mesh.user = 'other'
    assert registry.version(session, 'room', page) != version
    mesh.user = 'alice'
    assert PageCursorRegistry().version(session, 'room', page) != version


def test_cursor_rejects_page_from_another_store(tmp_path):
    registry = PageCursorRegistry()
    page = selection(tmp_path)
    foreign = SessionReadToken('app', 1, ViewerMesh('alice', tmp_path / 'other'))
    with pytest.raises(ValueError, match='another store'):
        registry.issue(foreign, 'room', page)
    with pytest.raises(ValueError, match='another database'):
        registry.version(foreign, 'room', page)
