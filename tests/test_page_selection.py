"""Canonical bounded-page selection against the established full-fold oracle."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_selection import (
    CanonicalPageAccumulator,
    MorePageInputRequired,
    PageDependencyPending,
    select_message_page,
)
from agentbridge.mesh.readmodel import build_messages, transcript_visible
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "mirror:room"
VIEWER = "viewer"
SEALER = PlainSealer()


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    opened.prepare_page_input_index()
    source = opened.publish_document_batch(
        opened.capture_document_position(SOURCE), {}, cursor=1, full=True,
        retain_tombstones=False,
    )
    observation = opened.capture_document_observation(SOURCE)
    opened.page_index = opened.publish_overlay_index(
        prepare_overlay_index(observation, CHAT),
    )
    assert opened.page_index.source == source
    yield opened
    opened.close()


def _message(ident, ns, sender="alice", body=None, **body_fields):
    sealed = SEALER.seal(
        CHAT, ident, ns,
        BodyRecord(body=body if body is not None else ident, **body_fields),
    )
    return Envelope(
        id=ident, ns=ns, ts="t", from_=sender, kind=MsgKind.MESSAGE,
        epoch=sealed["epoch"], nonce=sealed["nonce"], ct=sealed["ct"],
        sig=sealed["sig"],
    ).to_dict()


def _info(ident, ns, event, sender="alice"):
    return Envelope(
        id=ident, ns=ns, ts="t", from_=sender, kind=MsgKind.INFO, event=event,
    ).to_dict()


def _capture(store, records, *, raw_limit=None, exact_ids=()):
    store.upsert_messages(CHAT, records)
    return store.capture_page_inputs(
        store.page_index,
        raw_limit=len(records) if raw_limit is None else raw_limit,
        exact_ids=exact_ids,
    )


def _oracle(records, *, limit, **fold):
    visible = [
        message
        for message in build_messages(CHAT, VIEWER, records, SEALER, **fold)
        if transcript_visible(message, VIEWER)
    ]
    return visible[-limit:]


def _shape(messages):
    return [
        (m.id, m.ns, m.from_, m.body, m.deleted, m.undecrypted, m.reply_to)
        for m in messages
    ]


def test_hidden_clear_delete_heavy_tail_matches_complete_fold(store):
    records = [
        _message("old-star", 1),
        _message("old-clear", 2),
        _message("old-delete", 3),
        _message("tie-a", 8, sender="alice"),
        _message("tie-z", 8, sender="zoe"),
        _info("quiet", 9, {"type": "key_rotated"}),
        _message("hidden", 10),
        _message("redacted", 11),
        _message("visible", 12),
    ]
    fold = {
        "state": {
            "hidden": ["hidden"],
            "starred": ["old-star"],
            "cleared": {"ns": 2, "keep_starred": True},
            "deleted": 3,
        },
        "redactions": {"redacted": {"by": "alice"}},
    }
    inputs = _capture(store, records)
    selected = select_message_page(inputs, VIEWER, SEALER, limit=4, **fold)

    assert _shape(selected.messages) == _shape(_oracle(records, limit=4, **fold))
    assert [m.id for m in selected.messages] == [
        "tie-a", "tie-z", "redacted", "visible",
    ]
    assert selected.oldest_examined.id == "tie-a"
    assert selected.raw_examined == 6


def test_visible_tombstone_and_undecrypted_placeholder_consume_limit(store):
    deleted = _message("deleted", 3)
    encrypted = _message("encrypted", 4)
    encrypted["epoch"] = 7
    records = [_message("older", 2), deleted, encrypted]
    fold = {"redactions": {"deleted": {"by": "alice"}}}
    selected = select_message_page(
        _capture(store, records), VIEWER, SEALER, limit=2, **fold,
    )

    assert _shape(selected.messages) == _shape(_oracle(records, limit=2, **fold))
    assert [m.id for m in selected.messages] == ["deleted", "encrypted"]
    assert selected.messages[0].deleted is True
    assert selected.messages[1].undecrypted is True
    assert selected.raw_examined == 2


def test_limit_stops_at_oldest_examined_not_prefetch_or_lookahead(store):
    records = [_message(f"m{i}", i) for i in range(1, 8)]
    inputs = _capture(store, records, raw_limit=6)
    selected = select_message_page(inputs, VIEWER, SEALER, limit=2)

    assert [m.id for m in selected.messages] == ["m6", "m7"]
    assert selected.oldest_examined.id == "m6"
    assert selected.raw_examined == 2
    assert inputs.rows[-1].key.id == "m2"
    assert inputs.lookahead.id == "m1"
    assert selected.has_more and not selected.history_exhausted


def test_scan_budget_needs_more_and_actual_exhaustion_are_distinct(store):
    records = [_message(f"m{i}", i) for i in range(1, 6)]
    state = {"hidden": ["m3", "m4", "m5"]}

    budgeted = select_message_page(
        _capture(store, records), VIEWER, SEALER, limit=2, scan_budget=2,
        state=state,
    )
    assert budgeted.raw_examined == 2 and budgeted.scan_budget_exhausted
    assert budgeted.has_more and not budgeted.needs_more_input

    prefetched = select_message_page(
        _capture(store, records, raw_limit=3), VIEWER, SEALER,
        limit=2, scan_budget=4, state=state,
    )
    assert prefetched.raw_examined == 3 and prefetched.needs_more_input
    assert prefetched.has_more and not prefetched.scan_budget_exhausted

    store.forget_chat(CHAT)
    exhausted = select_message_page(
        _capture(store, records[:3]), VIEWER, SEALER,
        limit=2, scan_budget=4, state={"hidden": ["m1", "m2", "m3"]},
    )
    assert exhausted.messages == () and exhausted.history_exhausted
    assert not exhausted.has_more and not exhausted.needs_more_input


@pytest.mark.parametrize("quote", [True, False])
def test_offpage_hidden_parent_honors_verified_delete_and_quote_shape(store, quote):
    parent = _message("parent", 1, body="secret")
    reply = {"id": "parent", "from": "alice", "body": "secret"}
    if not quote:
        reply["quote"] = False
    child = _message("child", 2, sender="bob", reply_to=reply)
    records = [parent, child]
    inputs = _capture(store, records, raw_limit=1, exact_ids=("parent",))
    fold = {
        "state": {"hidden": ["parent"]},
        "redactions": {"parent": {"by": "alice"}},
        "verify_redaction": lambda ident, redaction, sender: True,
    }
    selected = select_message_page(inputs, VIEWER, SEALER, limit=1, **fold)

    assert _shape(selected.messages) == _shape(_oracle(records, limit=1, **fold))
    expected = {"id": "parent", "deleted": True}
    if not quote:
        expected["quote"] = False
    assert selected.messages[0].reply_to == expected
    assert selected.parents_examined == 1


def test_forged_offpage_delete_does_not_blank_quote(store):
    parent = _message("parent", 1, body="secret")
    reply = {"id": "parent", "from": "alice", "body": "secret"}
    child = _message("child", 2, sender="bob", reply_to=reply)
    records = [parent, child]
    fold = {
        "state": {"hidden": ["parent"]},
        "redactions": {"parent": {"by": "mallory"}},
        "verify_redaction": lambda ident, redaction, sender: False,
    }
    selected = select_message_page(
        _capture(store, records, raw_limit=1, exact_ids=("parent",)),
        VIEWER, SEALER, limit=1, **fold,
    )

    assert _shape(selected.messages) == _shape(_oracle(records, limit=1, **fold))
    assert selected.messages[0].reply_to == reply


def test_missing_parent_is_pending_until_explicit_absence_is_captured(store):
    child = _message(
        "child", 2, sender="bob",
        reply_to={"id": "missing", "from": "alice", "body": "quoted"},
    )
    inputs = _capture(store, [child], raw_limit=1)
    with pytest.raises(PageDependencyPending) as raised:
        select_message_page(inputs, VIEWER, SEALER, limit=1)
    assert raised.value.ids == ("missing",)

    resolved = _capture(store, [child], raw_limit=1, exact_ids=("missing",))
    selected = select_message_page(resolved, VIEWER, SEALER, limit=1)
    assert selected.messages[0].reply_to["id"] == "missing"
    assert selected.parents_examined == 0


def test_direct_parent_budget_is_bounded_and_deduplicated(store):
    parents = [_message("p1", 1), _message("p2", 2)]
    children = [
        _message("c1", 3, reply_to={"id": "p1", "body": "p1"}),
        _message("c2", 4, reply_to={"id": "p2", "body": "p2"}),
        _message("c3", 5, reply_to={"id": "p2", "body": "p2"}),
    ]
    inputs = _capture(
        store, parents + children, raw_limit=3, exact_ids=("p1", "p2"),
    )
    with pytest.raises(OverflowError, match="parent dependency budget"):
        select_message_page(inputs, VIEWER, SEALER, limit=3, parent_limit=1)

    selected = select_message_page(inputs, VIEWER, SEALER, limit=3, parent_limit=2)
    assert [m.id for m in selected.messages] == ["c1", "c2", "c3"]
    assert selected.parents_examined == 2


def test_unknown_and_confirmed_absent_parents_charge_before_resolution(store):
    child = _message(
        "child", 2,
        reply_to={"id": "missing", "from": "alice", "body": "quoted"},
    )
    unknown = _capture(store, [child], raw_limit=1)
    with pytest.raises(OverflowError, match="parent dependency budget"):
        select_message_page(unknown, VIEWER, SEALER, limit=1, parent_limit=0)

    absent = _capture(store, [child], raw_limit=1, exact_ids=("missing",))
    with pytest.raises(OverflowError, match="parent dependency budget"):
        select_message_page(absent, VIEWER, SEALER, limit=1, parent_limit=0)
    selected = select_message_page(absent, VIEWER, SEALER, limit=1, parent_limit=1)
    assert selected.parents_required == 1
    assert selected.parents_examined == 0


def test_missing_parent_worklist_is_bounded_sorted_and_deduplicated(store):
    children = [
        _message("c1", 3, reply_to={"id": "z-missing", "body": "z"}),
        _message("c2", 4, reply_to={"id": "a-missing", "body": "a"}),
        _message("c3", 5, reply_to={"id": "z-missing", "body": "z"}),
    ]
    inputs = _capture(store, children, raw_limit=3)
    with pytest.raises(PageDependencyPending) as raised:
        select_message_page(inputs, VIEWER, SEALER, limit=3, parent_limit=2)
    assert raised.value.ids == ("a-missing", "z-missing")
    with pytest.raises(OverflowError, match="parent dependency budget"):
        select_message_page(inputs, VIEWER, SEALER, limit=3, parent_limit=1)


def test_exact_only_capture_cannot_claim_history_exhaustion(store):
    records = [_message("parent", 1)]
    inputs = _capture(store, records, raw_limit=0, exact_ids=("parent",))
    assert inputs.raw_window_captured is False
    with pytest.raises(ValueError, match="no history exhaustion evidence"):
        select_message_page(inputs, VIEWER, SEALER, limit=1)


def test_accumulator_fills_page_across_real_contiguous_store_windows(store):
    records = [_message(f"m{i}", i) for i in range(1, 11)]
    state = {"hidden": [f"m{i}" for i in range(5, 11)]}
    store.upsert_messages(CHAT, records)
    accumulator = CanonicalPageAccumulator(
        VIEWER, SEALER, limit=3, scan_budget=10,
    )
    before = None
    expected = None
    windows = 0
    while True:
        inputs = store.capture_page_inputs(
            store.page_index, expected=expected, before=before, raw_limit=3,
        )
        expected = inputs.position
        windows += 1
        if not accumulator.feed(inputs, state=state):
            break
        before = inputs.rows[-1].key

    selected = accumulator.finish()
    assert windows == 3
    assert [m.id for m in selected.messages] == ["m2", "m3", "m4"]
    assert _shape(selected.messages) == _shape(_oracle(records, limit=3, state=state))
    assert selected.raw_examined == 9
    assert selected.oldest_examined.id == "m2"


def test_accumulator_enforces_total_scan_and_parent_budgets(store):
    records = [
        _message("p1", 1), _message("p2", 2),
        _message("c1", 3, reply_to={"id": "p1", "body": "p1"}),
        _message("hidden", 4),
        _message("c2", 5, reply_to={"id": "p2", "body": "p2"}),
    ]
    store.upsert_messages(CHAT, records)
    first = store.capture_page_inputs(
        store.page_index, raw_limit=2, exact_ids=("p2",),
    )
    second = store.capture_page_inputs(
        store.page_index, expected=first.position, before=first.rows[-1].key,
        raw_limit=2, exact_ids=("p1",),
    )
    fold = {"state": {"hidden": ["hidden"]}}

    scan_limited = CanonicalPageAccumulator(
        VIEWER, SEALER, limit=3, scan_budget=3, parent_limit=2,
    )
    assert scan_limited.feed(first, **fold)
    assert scan_limited.feed(second, **fold) is False
    terminal = scan_limited.finish()
    assert terminal.raw_examined == 3 and terminal.scan_budget_exhausted
    assert [m.id for m in terminal.messages] == ["c1", "c2"]

    parent_limited = CanonicalPageAccumulator(
        VIEWER, SEALER, limit=3, scan_budget=5, parent_limit=1,
    )
    assert parent_limited.feed(first, **fold)
    with pytest.raises(OverflowError, match="parent dependency budget"):
        parent_limited.feed(second, **fold)


def test_accumulator_rejects_gaps_position_changes_and_pending_finish(store):
    records = [_message(f"m{i}", i) for i in range(1, 7)]
    state = {"hidden": ["m4", "m5", "m6"]}
    store.upsert_messages(CHAT, records)
    first = store.capture_page_inputs(store.page_index, raw_limit=2)

    empty = CanonicalPageAccumulator(VIEWER, SEALER, limit=2)
    with pytest.raises(MorePageInputRequired):
        empty.finish()
    assert empty.feed(first, state=state)
    with pytest.raises(MorePageInputRequired):
        empty.finish()

    gap = store.capture_page_inputs(
        store.page_index, expected=first.position,
        before=first.lookahead, raw_limit=2,
    )
    with pytest.raises(ValueError, match="not contiguous"):
        empty.feed(gap, state=state)

    changed = CanonicalPageAccumulator(VIEWER, SEALER, limit=2)
    assert changed.feed(first, state=state)
    store.upsert_messages(CHAT, [_message("newer", 7)])
    new_position = store.capture_page_inputs(
        store.page_index, before=first.rows[-1].key, raw_limit=2,
    )
    with pytest.raises(ValueError, match="inputs changed"):
        changed.feed(new_position, state=state)


def test_accumulator_charges_same_offpage_parent_once_across_windows(store):
    parent = _message("parent", 1)
    reply = {"id": "parent", "from": "alice", "body": "parent"}
    records = [
        parent,
        _message("older-child", 2, reply_to=reply),
        _message("hidden", 3),
        _message("newer-child", 4, reply_to=reply),
    ]
    store.upsert_messages(CHAT, records)
    first = store.capture_page_inputs(
        store.page_index, raw_limit=2, exact_ids=("parent",),
    )
    second = store.capture_page_inputs(
        store.page_index, expected=first.position, before=first.rows[-1].key,
        raw_limit=1, exact_ids=("parent",),
    )
    accumulator = CanonicalPageAccumulator(
        VIEWER, SEALER, limit=2, scan_budget=4, parent_limit=1,
    )

    assert accumulator.feed(first, state={"hidden": ["hidden"]})
    assert accumulator.feed(second, state={"hidden": ["hidden"]}) is False
    selected = accumulator.finish()

    assert [m.id for m in selected.messages] == ["older-child", "newer-child"]
    assert selected.parent_ids == frozenset({"parent"})
    assert selected.parents_required == 1
    assert selected.parents_examined == 2


def test_forged_captured_bytes_rejects_before_sealer_callback(store):
    inputs = _capture(store, [_message("m1", 1)], raw_limit=1)
    forged = replace(inputs, captured_bytes=0)
    calls = []

    class CallbackSealer:
        def unseal(self, chat_id, envelope):
            calls.append((chat_id, envelope.id))
            raise AssertionError("sealer callback ran before byte validation")

    with pytest.raises(OverflowError, match="captured input byte accounting"):
        select_message_page(forged, VIEWER, CallbackSealer(), limit=1)
    assert calls == []
