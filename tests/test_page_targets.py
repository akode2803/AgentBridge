"""Exact message targets are canonical bounded inputs, never history pages."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_selection import PageDependencyPending, select_exact_messages
from agentbridge.mesh.readmodel import build_messages, transcript_visible
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.store import page_inputs
from agentbridge.store.db import Store


CHAT = 'room'
VIEWER = 'viewer'
SEALER = PlainSealer()


def _message(ident, ns, *, sender='alice', reply_to=None):
    sealed = SEALER.seal(CHAT, ident, ns, BodyRecord(body=ident, reply_to=reply_to))
    return Envelope(id=ident, ns=ns, ts='2026-01-01T00:00:00Z', from_=sender,
                    kind=MsgKind.MESSAGE, epoch=0, nonce=sealed['nonce'],
                    ct=sealed['ct'], sig=sealed['sig']).to_dict()


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / 'store.sqlite')
    opened.prepare_page_input_index()
    opened.publish_document_batch(opened.capture_document_position('source'), {},
                                  cursor=1, full=True, retain_tombstones=False)
    opened.page_index = opened.publish_overlay_index(prepare_overlay_index(
        opened.capture_document_observation('source'), CHAT))
    try:
        yield opened
    finally:
        opened.close()


def _capture(store, records, ids):
    store.upsert_messages(CHAT, records)
    return store.capture_page_inputs(store.page_index, raw_limit=0, exact_ids=ids)


def _oracle(records, target_ids, **fold):
    return tuple(m for m in build_messages(CHAT, VIEWER, records, SEALER, **fold)
                 if m.id in target_ids and transcript_visible(m, VIEWER))


def _shape(messages):
    return [(m.id, m.body, m.deleted, m.reply_to, m.undecrypted) for m in messages]


def test_visibility_cuts_and_redaction_match_complete_fold(store):
    records = [_message('starred-preclear', 1), _message('old', 2),
               _message('hidden', 3), _message('departed', 4, sender='departed'),
               _message('redacted', 5), _message('visible', 6)]
    inputs = _capture(store, records, tuple(m['id'] for m in records))
    assert inputs.rows == () and not inputs.raw_window_captured
    assert inputs.lookahead is None
    fold = dict(
        state={'hidden': ['hidden'], 'starred': ['starred-preclear'],
               'cleared': {'ns': 3, 'keep_starred': True}},
        tenure={'departed': [[1, 4]]}, history_from_ns=2,
        redactions={'redacted': {'by': 'alice'}},
    )
    selected = select_exact_messages(inputs, tuple(m['id'] for m in records),
                                     VIEWER, SEALER, **fold)
    assert _shape(selected) == _shape(_oracle(records, tuple(m['id'] for m in records), **fold))
    assert [m.id for m in selected] == ['redacted', 'visible']
    assert selected[0].body == '' and selected[0].deleted


def test_keep_starred_and_history_cut_are_independent(store):
    records = [_message('early-star', 1), _message('late-star', 2), _message('after', 3)]
    inputs = _capture(store, records, ('early-star', 'late-star', 'after'))
    fold = dict(state={'starred': ['early-star', 'late-star'],
                       'cleared': {'ns': 2, 'keep_starred': True}},
                history_from_ns=2)
    selected = select_exact_messages(inputs, ('early-star', 'late-star', 'after'),
                                     VIEWER, SEALER, **fold)
    assert [m.id for m in selected] == ['late-star', 'after']
    assert _shape(selected) == _shape(_oracle(records, ('early-star', 'late-star', 'after'), **fold))


def test_reply_parent_requires_explicit_same_cut_and_honored_redaction_blanks_quote(store):
    parent = _message('parent', 1)
    reply = _message('reply', 100, reply_to={'id': 'parent', 'from': 'alice',
                                            'body': 'parent', 'quote': True})
    missing_dependency = _capture(store, [parent, reply], ('reply',))
    with pytest.raises(PageDependencyPending) as pending:
        select_exact_messages(missing_dependency, ('reply',), VIEWER, SEALER,
                              redactions={'parent': {'by': 'alice'}})
    assert pending.value.ids == ('parent',)
    inputs = store.capture_page_inputs(store.page_index, raw_limit=0,
                                       exact_ids=('reply', 'parent'),
                                       expected=missing_dependency.position)
    selected = select_exact_messages(inputs, ('reply',), VIEWER, SEALER,
                                     redactions={'parent': {'by': 'alice'}})
    assert len(selected) == 1 and selected[0].reply_to == {'id': 'parent', 'deleted': True}
    assert _shape(selected) == _shape(_oracle([parent, reply], ('reply',),
                                               redactions={'parent': {'by': 'alice'}}))

    # Forged/ignored redaction cannot blank the embedded original quote.
    ignored = select_exact_messages(inputs, ('reply',), VIEWER, SEALER,
                                    redactions={'parent': {'by': 'alice'}},
                                    verify_redaction=lambda *_: False)
    assert ignored[0].reply_to['body'] == 'parent'


def test_explicit_absence_and_unrequested_id_are_distinct(store):
    inputs = _capture(store, [_message('present', 1)], ('missing',))
    assert inputs.absent_ids == ('missing',)
    assert select_exact_messages(inputs, ('missing',), VIEWER, SEALER) == ()
    with pytest.raises(PageDependencyPending) as pending:
        select_exact_messages(inputs, ('present',), VIEWER, SEALER)
    assert pending.value.ids == ('present',)
    present = store.capture_page_inputs(store.page_index, raw_limit=0,
                                        exact_ids=('present',), expected=inputs.position)
    with pytest.raises(ValueError, match='inconsistent exact absence'):
        select_exact_messages(replace(present, absent_ids=('present',)),
                              ('present',), VIEWER, SEALER)


def test_more_than_sixty_four_target_and_parent_dependencies_fail(store):
    records = [_message(f't{i}', i + 100,
                        reply_to={'id': f'p{i}', 'from': 'alice', 'body': f'p{i}'})
               for i in range(64)]
    inputs = _capture(store, records, tuple(f't{i}' for i in range(64)))
    with pytest.raises(OverflowError, match='dependency budget'):
        select_exact_messages(inputs, tuple(f't{i}' for i in range(64)), VIEWER, SEALER)
    with pytest.raises(ValueError, match='invalid exact target'):
        select_exact_messages(inputs, tuple(f't{i}' for i in range(65)), VIEWER, SEALER)


def test_old_exact_target_unseals_only_requested_rows_and_position_changes(store):
    records = [_message(f'm{i}', i + 1) for i in range(600)]
    inputs = _capture(store, records, ('m0', 'm599'))
    assert inputs.rows == () and [r.key.id for r in inputs.exact_rows] == ['m0', 'm599']

    class CountingSealer:
        def __init__(self):
            self.ids = []

        def unseal(self, chat, env):
            self.ids.append(env.id)
            return SEALER.unseal(chat, env)

    sealer = CountingSealer()
    selected = select_exact_messages(inputs, ('m0',), VIEWER, sealer)
    assert [m.id for m in selected] == ['m0'] and sealer.ids == ['m0']
    assert inputs.position.messages.chat_id == CHAT
    assert inputs.position.overlays == store.page_index
    store.upsert_messages(CHAT, [_message('late', 601)])
    with pytest.raises(page_inputs.PageInputsChanged, match='message_position_changed'):
        store.capture_page_inputs(store.page_index, raw_limit=0,
                                  exact_ids=('m0',), expected=inputs.position)
