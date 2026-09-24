"""Request-owned canonical page operation integration contracts."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from agentbridge.mesh import page_operation as page_module
from agentbridge.mesh.authority_source import publish_authority_source
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.overlay_source import publish_overlay_source
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.gui.applock import AppLock
from agentbridge.gui.context import GuiApp, SessionReadToken
from agentbridge.store import lifecycle_inputs
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


COUNT = 200


def _message(ident, ns, sender="aryan"):
    return {
        "id": ident,
        "ns": ns,
        "ts": f"2026-01-01T00:00:{ns % 60:02d}Z",
        "from": sender,
        "kind": "message",
        "epoch": 0,
        "nonce": "",
        "ct": json.dumps({"body": ident}),
        "sig": "",
    }


@pytest.fixture
def world(tmp_path):
    provider = FolderTransport(tmp_path / "mesh")
    provider.cache_key = "page-operation-cache"
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror._mirror_root_identity = "page-operation-root"
    mirror._mirror_cache_identity = "page-operation-cache"
    mesh = Mesh(mirror, "aryan", "page-box", home=tmp_path / "home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mirror.refresh()
        chat = mesh.membership.create_chat("Pages")
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        meta = mirror.get_doc(P.meta(chat.id))
        base_ns = max(member["joined_ns"] for member in meta["members"].values()) + 1
        mesh.store.forget_chat(chat.id)
        mesh.store.upsert_messages(
            chat.id,
            [_message(f"m{index:03d}", base_ns + index) for index in range(COUNT)],
        )
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        mesh.store.prepare_terminal_observation()
        lifecycle_inputs.prepare(mesh.store._conn())
        yield mesh, mirror, provider, chat.id, base_ns
    finally:
        mesh.close()


def _inputs(mesh, mirror, chat, *, state=None):
    if state is not None:
        mirror.put_doc(P.state(chat, mesh.messaging.user), state)
    mirror.refresh()
    mesh.store.refresh_terminal_observation(
        f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"
    )
    authority = publish_authority_source(mirror, mesh.store, chat)
    overlay = publish_overlay_source(mirror, mesh.store, chat)
    assert authority.mirror == overlay.mirror
    observed = mesh.store.capture_document_observation(overlay.position.source_id)
    index = mesh.store.publish_overlay_index(prepare_overlay_index(observed, chat))
    return authority, overlay, index


def _finalize(operation, inputs):
    prepared = operation.prepare(*inputs)
    assert prepared.status == "prepared", prepared
    finalized = prepared.prepared.finalize()
    assert finalized.status == "page", finalized
    assert finalized.result.status == "page"
    assert finalized.result.candidate is None
    assert finalized.result.page is not None
    return finalized.result.page


def _gui_for_page(mesh, home):
    app = GuiApp.__new__(GuiApp)
    app.instance_id = "page-read-gate"
    app.mesh = mesh
    app._session_generation = 7
    app._session_reads_exhausted = False
    app._session_read_ready = True
    app._lock = threading.RLock()
    app.lock = AppLock(home)
    return app, SessionReadToken(app.instance_id, 7, mesh)


def test_two_hundred_messages_page_and_keyset_pagination(world):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    first = _finalize(PageOperation(mesh, chat, limit=50), inputs)
    assert [message.id for message in first.messages] == [
        f"m{index:03d}" for index in range(150, 200)
    ]
    assert first.oldest_examined.id == "m150"
    assert first.raw_examined == 50 and first.has_more

    second = _finalize(
        PageOperation(
            mesh, chat, before=first.oldest_examined,
            expected_position=first.position, limit=50,
        ),
        inputs,
    )
    assert [message.id for message in second.messages] == [
        f"m{index:03d}" for index in range(100, 150)
    ]
    assert not set(message.id for message in first.messages).intersection(
        message.id for message in second.messages
    )


def test_presentation_is_page_local_and_joins_starred_across_raw_windows(world):
    mesh, mirror, _provider, chat, base = world
    mesh.store.upsert_messages(chat, [
        _message(f"m{index:03d}", base + index) for index in range(200, 400)
    ])
    # One visible message in the first 200 raw rows forces a second bounded
    # capture. Stars from both windows must survive; an older star must not
    # pretend to be part of this page's complete inventory.
    state = {
        "hidden": [f"m{index:03d}" for index in range(200, 399)],
        "starred": ["m399", "m199", "m001"],
        "read_ns": base + 42,
        "archived": True,
    }
    inputs = _inputs(mesh, mirror, chat, state=state)
    prepared = PageOperation(mesh, chat, limit=10).prepare(*inputs)
    assert prepared.status == "prepared", prepared
    result = prepared.prepared.finalize()
    assert result.status == "page", result
    page, presentation = result.result.page, result.result.presentation
    assert page.raw_examined == 209
    assert [m.id for m in page.messages] == [
        *(f"m{index:03d}" for index in range(191, 200)), "m399",
    ]
    assert presentation.starred == ("m199", "m399")
    assert json.loads(presentation.viewer_state_json) == {
        "read_ns": base + 42, "archived": True,
    }
    assert json.loads(presentation.snapshot_json)["id"] == chat
    assert result.result.candidate is None


@pytest.mark.parametrize("mutation", ["delayed_message", "new_edit"])
def test_continuation_requires_exact_previous_cut(world, mutation):
    mesh, mirror, _provider, chat, base = world
    initial = _inputs(mesh, mirror, chat)
    first = _finalize(PageOperation(mesh, chat, limit=20), initial)
    with pytest.raises(ValueError, match="continuation"):
        PageOperation(mesh, chat, before=first.oldest_examined, limit=20)

    if mutation == "delayed_message":
        mesh.store.upsert_messages(chat, [_message("delayed", base + 50)])
    else:
        mirror.put_doc(P.edit(chat, "m050"), {"by": "aryan", "epoch": 0, "ct": "late"})
    current = _inputs(mesh, mirror, chat)
    continued = PageOperation(
        mesh, chat, before=first.oldest_examined,
        expected_position=first.position, limit=20,
    ).prepare(*current)
    assert (continued.status, continued.reason) == ("unavailable", "continuation_changed")


def test_equal_ns_composite_order_and_clear_heavy_raw_cursor(world):
    mesh, mirror, _provider, chat, base = world
    mesh.store.upsert_messages(chat, [
        _message("tie-a", base + 500, "zara"),
        _message("tie-c", base + 500, "bob"),
        _message("tie-b", base + 500, "bob"),
    ])
    inputs = _inputs(mesh, mirror, chat, state={"cleared": {"ns": base + 179}})
    page = _finalize(PageOperation(mesh, chat, limit=30, scan_budget=1000), inputs)
    ids = [message.id for message in page.messages]
    assert ids[-3:] == ["tie-b", "tie-c", "tie-a"]
    assert ids[:3] == ["m180", "m181", "m182"]
    assert len(ids) == 23
    assert page.raw_examined == 203
    assert page.history_exhausted and not page.has_more


def test_new_prepare_supersedes_earlier_finalizer(world):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    operation = PageOperation(mesh, chat, limit=20)
    old = operation.prepare(*inputs)
    current = operation.prepare(*inputs)
    assert old.status == current.status == "prepared"
    assert old.prepared.finalize().reason == "operation_superseded"
    finalized = current.prepared.finalize()
    assert finalized.status == "page" and finalized.result.page is not None
    assert current.prepared.finalize().reason == "operation_superseded"


def test_finalizer_rejects_presentation_with_noncanonical_snapshot(world):
    mesh, mirror, _provider, chat, _base = world
    prepared = PageOperation(mesh, chat, limit=10).prepare(*_inputs(mesh, mirror, chat))
    assert prepared.status == "prepared"
    finalizer = prepared.prepared
    fence = finalizer._fence
    altered = json.loads(fence.presentation.snapshot_json)
    altered["name"] = "forged name"
    finalizer._fence = replace(fence, presentation=replace(
        fence.presentation,
        snapshot_json=json.dumps(altered, sort_keys=True, separators=(",", ":")),
    ))
    result = finalizer.finalize()
    assert (result.status, result.reason, result.result) == (
        "unavailable", "page_presentation_changed", None,
    )


def test_scan_budget_one_never_captures_more_than_one_payload_row(world, monkeypatch):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    original = page_module.page_inputs.capture_page_inputs
    captured_rows = []

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.raw_window_captured:
            captured_rows.append(len(result.rows))
        return result

    monkeypatch.setattr(page_module.page_inputs, "capture_page_inputs", capture)
    page = _finalize(
        PageOperation(mesh, chat, limit=10, scan_budget=1), inputs,
    )
    assert captured_rows and max(captured_rows) <= 1
    assert len(page.messages) == 1 and page.raw_examined == 1
    assert page.scan_budget_exhausted and page.has_more


@pytest.mark.parametrize("mutation", ["edit", "membership", "message"])
def test_late_input_mutations_reject_prepared_page(world, mutation):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    operation = PageOperation(mesh, chat, limit=20)
    prepared = operation.prepare(*inputs)
    assert prepared.status == "prepared"
    if mutation == "edit":
        mirror.put_doc(P.edit(chat, "m199"), {"by": "aryan", "ct": "late"})
    elif mutation == "membership":
        mesh.store.upsert_messages(chat, [{
            "id": "late-membership",
            "ns": time.time_ns(),
            "from": "aryan",
            "kind": "info",
            "event": {"type": "renamed", "name": "Late"},
        }])
    else:
        mesh.store.upsert_messages(chat, [_message("late-message", time.time_ns())])
    finalized = prepared.prepared.finalize()
    assert finalized.status == "unavailable"
    assert finalized.result is None  # no page or presentation escapes the failed fence
    assert finalized.reason in {
        "page_mirror_changed", "membership_inputs_changed", "page_inputs_changed",
        "inputs_unavailable",
    }


def test_prepare_and_finalize_do_not_use_full_messages_or_provider(world, monkeypatch):
    mesh, mirror, provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail("page operation invoked full-fold/provider path")

    monkeypatch.setattr(mesh.messaging, "messages_for", forbidden)
    monkeypatch.setattr(mesh.messaging, "snapshot", forbidden)
    for name in ("get_doc", "list_docs", "snapshot_docs", "read_log", "list_logs"):
        monkeypatch.setattr(provider, name, forbidden)
    page = _finalize(PageOperation(mesh, chat, limit=25), inputs)
    assert len(page.messages) == 25


def test_gui_page_gate_hands_out_only_same_session_and_mesh(world, tmp_path):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    prepared = PageOperation(mesh, chat, limit=12).prepare(*inputs)
    assert prepared.status == "prepared"
    app, token = _gui_for_page(mesh, tmp_path / "gate-home")
    result = app.finalize_page_read(token, prepared.prepared)
    assert result.status == "page"
    assert result.result.candidate is None
    assert len(result.result.page.messages) == 12

    stale = PageOperation(mesh, chat, limit=12).prepare(*inputs)
    result = app.finalize_page_read(
        SessionReadToken(app.instance_id, token.generation + 1, mesh),
        stale.prepared,
    )
    assert (result.status, result.reason) == ("unavailable", "session_changed")

    other = PageOperation(mesh, chat, limit=12).prepare(*inputs)
    other_mesh = object()
    app.mesh = other_mesh
    result = app.finalize_page_read(token, other.prepared)
    assert (result.status, result.reason) == ("unavailable", "session_changed")


def test_gui_page_gate_withholds_when_locked_before_or_during_final(world, tmp_path,
                                                                    monkeypatch):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    app, token = _gui_for_page(mesh, tmp_path / "lock-home")

    locked = PageOperation(mesh, chat, limit=10).prepare(*inputs)
    app.lock.locked = True
    result = app.finalize_page_read(token, locked.prepared)
    assert (result.status, result.reason) == ("locked", "app_locked")

    app.lock.locked = False
    crossing = PageOperation(mesh, chat, limit=10).prepare(*inputs)
    checks = 0

    def expires_on_postcheck():
        nonlocal checks
        checks += 1
        if checks == 2:
            app.lock.locked = True
        return app.lock.locked

    monkeypatch.setattr(app.lock, "_expire_if_idle_locked", expires_on_postcheck)
    result = app.finalize_page_read(token, crossing.prepared)
    assert checks == 2
    assert (result.status, result.reason) == ("locked", "app_locked")


def test_gui_page_gate_holds_session_lock_through_final_validation(world, tmp_path,
                                                                   monkeypatch):
    mesh, mirror, _provider, chat, _base = world
    inputs = _inputs(mesh, mirror, chat)
    prepared = PageOperation(mesh, chat, limit=10).prepare(*inputs)
    assert prepared.status == "prepared"
    app, token = _gui_for_page(mesh, tmp_path / "barrier-home")
    entered = threading.Event()
    release = threading.Event()
    adopted = threading.Event()
    original = type(prepared.prepared).finalize

    def held_finalize(owner):
        entered.set()
        assert release.wait(10), "finalization barrier timed out"
        return original(owner)

    monkeypatch.setattr(type(prepared.prepared), "finalize", held_finalize)
    outcome = []
    final_thread = threading.Thread(
        target=lambda: outcome.append(app.finalize_page_read(token, prepared.prepared)),
    )

    def adopt_session():
        with app._lock:
            app._session_generation += 1
            adopted.set()

    adopt_thread = threading.Thread(target=adopt_session)
    final_thread.start()
    try:
        assert entered.wait(10), "finalizer did not reach the session-owned barrier"
        adopt_thread.start()
        assert not adopted.wait(0.1)
    finally:
        release.set()
        final_thread.join(10)
        adopt_thread.join(10)
    assert not final_thread.is_alive() and not adopt_thread.is_alive()
    assert outcome and outcome[0].status == "page"
    assert adopted.is_set()
