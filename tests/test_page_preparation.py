"""Page preflight owns bounded background facts, without request-side writes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentbridge import crypto
from agentbridge.mesh import page_preparation
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_preparation import PagePreparation
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport


def _mesh(store, *, user='viewer', machine='laptop'):
    return SimpleNamespace(store=store, messaging=SimpleNamespace(user=user, machine=machine))


def _store(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    source = store.publish_document_batch(
        store.capture_document_position('source'), {}, cursor=1, full=True,
    )
    index = store.publish_overlay_index(prepare_overlay_index(
        store.capture_document_observation(source.source_id), 'room',
    ))
    return store, source, index


def test_requests_only_queue_coalesced_bounded_work_and_validate_scope(tmp_path, monkeypatch):
    store, _source, index = _store(tmp_path)
    try:
        preflight = PagePreparation(_mesh(store))
        def forbidden(*_args, **_kwargs):
            pytest.fail('page preparation request performed database or provider work')

        for name in ('prepare_terminal_observation', 'prepare_membership_suffix_index',
                     'prepare_page_input_index', 'refresh_terminal_observation',
                     'verify_overlay_signature'):
            monkeypatch.setattr(store, name, forbidden)
        monkeypatch.setattr(page_preparation.lifecycle_inputs, 'prepare', forbidden)
        pub = crypto.identity_pubs(crypto.generate_identity())[0]
        proof = (P.state('room', 'viewer'), pub)
        assert preflight.request('room', index=index, proofs=(proof, proof))
        assert preflight.request('room', index=index, proofs=(proof,))
        assert list(preflight._terminals) == ['room']
        assert len(preflight._proofs) == 1
        for n in range(150):
            assert preflight.request(f'room{n}')
        assert len(preflight._terminals) == 128
        assert len(preflight._proofs) == 1
        assert 'room' not in preflight._terminals
        for offset in (0, 200):
            batch = tuple((P.state('room', f'viewer{i}'), pub)
                          for i in range(offset, min(offset + 200, 300)))
            assert preflight.request('room', index=index, proofs=batch)
        assert len(preflight._proofs) == 256
        with pytest.raises(ValueError):
            preflight.request('other', index=index, proofs=(proof,))
        with pytest.raises(ValueError):
            preflight.request('room', index=index, proofs=((P.state('other', 'viewer'), pub),))
        with pytest.raises(ValueError):
            preflight.request('room', index=index, proofs=(('x', 'invalid'),))
        with pytest.raises(ValueError):
            preflight.request('room', proofs=(proof,))
        assert len(preflight._proofs) == 256
    finally:
        store.close()


def test_background_schema_terminal_and_exact_signature_generation(tmp_path):
    store, source, index = _store(tmp_path)
    try:
        bundle = crypto.generate_identity()
        pub = crypto.identity_pubs(bundle)[0]
        path = P.state('room', 'viewer')
        # The exact generation is part of the queued proof key. The worker
        # verifies signatures, never assumes them valid merely because queued.
        from agentbridge.mesh.events import state_signing_bytes
        doc = {'ns': 2, 'hidden': [], 'sig': crypto.sign(
            bundle, state_signing_bytes('room', 'viewer', 2, {'hidden': []}),
        )}
        source = store.publish_document_batch(source, {path: doc}, cursor=2, full=True)
        index = store.publish_overlay_index(prepare_overlay_index(
            store.capture_document_observation(source.source_id), 'room',
        ))
        preflight = PagePreparation(_mesh(store))
        preflight.select('room')
        assert preflight.request('room', index=index, proofs=((path, pub),))
        assert preflight.run_one()  # Schema preparation; no request-side DDL.
        assert preflight.run_one()  # Terminal classification.
        assert store.capture_terminal_observation('room|viewer@laptop') is not None
        assert preflight.run_one()  # Signature proof of this precise build.
        assert store.capture_overlay_proofs(index, ((path, pub),)) == (True,)
        assert not preflight.run_one()

        assert preflight.request('room', index=index, proofs=((path, pub),))
        store.publish_document_batch(source, {path: doc}, cursor=3, full=True)
        assert preflight.run_one()  # Pending terminal work may still proceed.
        with pytest.raises(Exception):
            preflight.run_one()  # Old index cannot publish a new-generation proof.
    finally:
        store.close()


def test_stop_discards_work_and_fairly_services_proofs(tmp_path, monkeypatch):
    store, _source, index = _store(tmp_path)
    try:
        preflight = PagePreparation(_mesh(store))
        pub = crypto.identity_pubs(crypto.generate_identity())[0]
        path = P.state('room', 'viewer')
        work = []
        preflight._schema_ready = True
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda target: work.append(('terminal', target)))
        monkeypatch.setattr(store, 'verify_overlay_signature',
                            lambda *args: work.append(('proof', args)))
        assert preflight.request('room', index=index, proofs=((path, pub),))
        for n in range(12):
            assert preflight.request(f'other{n}')
            assert preflight.run_one()
        assert any(kind == 'proof' for kind, _ in work), 'continuous terminals starved proofs'
        before = len(work)
        preflight.close()
        assert not preflight.request('room')
        assert not preflight.run_one()
        assert len(work) == before
    finally:
        store.close()


def test_runtime_owns_worker_execution_and_stops_page_work(tmp_path, monkeypatch):
    mesh = Mesh(FolderTransport(tmp_path / 'provider'), 'viewer', 'laptop',
                home=tmp_path / 'home', store_path=tmp_path / 'mesh.sqlite',
                local_inputs=True)
    runtime = mesh.local_inputs
    try:
        events = []
        monkeypatch.setattr(mesh.store, 'prepare_terminal_observation',
                            lambda: events.append('schema'))
        monkeypatch.setattr(mesh.store, 'prepare_membership_suffix_index', lambda: None)
        monkeypatch.setattr(mesh.store, 'prepare_page_input_index', lambda: None)
        monkeypatch.setattr(page_preparation.lifecycle_inputs, 'prepare', lambda _conn: None)
        monkeypatch.setattr(mesh.store, 'refresh_terminal_observation',
                            lambda target: events.append(target))
        assert runtime.request_page('room')
        assert events == []
        assert runtime.prepare_one()
        assert runtime.prepare_one()
        assert events == ['schema', 'room|viewer@laptop']
        monkeypatch.setattr(mesh.store, 'refresh_terminal_observation',
                            lambda _target: (_ for _ in ()).throw(RuntimeError('interrupted')))
        assert runtime.request_page('room')
        assert runtime.prepare_one() is False  # Worker survives a failed attempt.
        runtime.stop()
        assert runtime.request_page('room') is False
        assert runtime.prepare_one() is False
    finally:
        mesh.close()


def test_failed_schema_and_terminal_health_clears_after_background_retry(tmp_path, monkeypatch):
    store, _source, _index = _store(tmp_path)
    try:
        preflight = PagePreparation(_mesh(store))
        assert preflight.request('room')
        original_schema = store.prepare_terminal_observation
        monkeypatch.setattr(store, 'prepare_terminal_observation',
                            lambda: (_ for _ in ()).throw(RuntimeError('schema unavailable')))
        with pytest.raises(RuntimeError, match='schema unavailable'):
            preflight.run_one()
        assert preflight.health('room') == 'schema_preparation_failed'
        monkeypatch.setattr(store, 'prepare_terminal_observation', original_schema)
        assert preflight.run_one()
        assert preflight.health('room') is None
        store.outbox_add('append', 'room|viewer@laptop', {'kind': 'message'})
        original_terminal = store.refresh_terminal_observation
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda target: (_ for _ in ()).throw(RuntimeError('terminal unavailable')))
        with pytest.raises(RuntimeError, match='terminal unavailable'):
            preflight.run_one()
        assert preflight.health('room') == 'terminal_preparation_failed'
        assert preflight.request('room')
        monkeypatch.setattr(store, 'refresh_terminal_observation', original_terminal)
        assert preflight.run_one()
        assert preflight.health('room') is None
    finally:
        store.close()


def test_terminal_failure_health_is_bounded_under_many_rooms(tmp_path, monkeypatch):
    store, _source, _index = _store(tmp_path)
    try:
        preflight = PagePreparation(_mesh(store))
        preflight._schema_ready = True
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda _target: (_ for _ in ()).throw(RuntimeError('temporary')))
        for n in range(150):
            assert preflight.request(f'room{n}')
            with pytest.raises(RuntimeError, match='temporary'):
                preflight.run_one()
        assert len(preflight._terminal_errors) == 128
        assert preflight.health('room0') is None
        assert preflight.health('room149') == 'terminal_preparation_failed'
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["terminal", "proof"])
def test_repeated_inventory_requests_do_not_starve_queued_preparation(tmp_path, monkeypatch, kind):
    """A readiness wake may trigger a full inventory before the next quantum."""
    store, _source, index = _store(tmp_path)
    try:
        preflight = PagePreparation(_mesh(store))
        preflight._schema_ready = True
        completed = []
        monkeypatch.setattr(store, "capture_terminal_observation",
                            lambda _target: SimpleNamespace(position=0))
        monkeypatch.setattr(store, "refresh_terminal_observation", lambda _target: 0)
        if kind == "terminal":
            def capture(target):
                completed.append(target.split("|", 1)[0])
                return SimpleNamespace(position=0)
            monkeypatch.setattr(store, "capture_terminal_observation", capture)
            expected = {"selected", "ordinary-a", "ordinary-b"}
            def inventory():
                for chat in ("selected", "ordinary-a", "ordinary-b"):
                    preflight.request(chat)
        else:
            public = crypto.identity_pubs(crypto.generate_identity())[0]
            paths = tuple(P.state("room", who) for who in ("selected", "ordinary-a", "ordinary-b"))
            expected = set(paths)
            monkeypatch.setattr(store, "capture_overlay_proofs",
                                lambda _position, keys: tuple((path, pub, True) for path, pub in keys))
            def verify(_position, path, _public):
                completed.append(path)
                return True
            monkeypatch.setattr(store, "verify_overlay_signature", verify)
            def inventory():
                preflight.request("room", index=index, proofs=tuple((path, public) for path in paths))
        # The caller reissues the same pending work after every background step.
        # Every original job must still make progress; duplicate wakes aren't
        # new jobs and must not displace the uncompleted ones.
        for _ in range(8):
            inventory()
            assert preflight.run_one()
        assert expected <= set(completed)
    finally:
        store.close()


@pytest.mark.parametrize("payload", [None, {"kind": "message"},
                                     {"kind": "info", "event": {"type": "member_left"}}])
def test_ready_terminal_preparation_has_no_reclassification_writes(tmp_path, monkeypatch, payload):
    store, _source, _index = _store(tmp_path)
    try:
        store.prepare_terminal_observation()
        target = 'room|viewer@laptop'
        if payload is not None:
            store.outbox_add('append', target, payload)
            store.refresh_terminal_observation(target)
        before = store.capture_terminal_observation(target)
        preflight = PagePreparation(_mesh(store), on_ready=lambda *_: pytest.fail('unchanged readiness emitted'))
        preflight._schema_ready = True
        preflight._terminal_errors['room'] = None
        calls = []
        original = store.refresh_terminal_observation
        def refresh(selected):
            calls.append(selected)
            return original(selected)
        monkeypatch.setattr(store, 'refresh_terminal_observation', refresh)
        changes = store._conn().total_changes
        for _ in range(3):
            assert preflight.request('room')
            assert preflight.run_one()
        assert calls == [], 'fresh valid classifications were rebuilt'
        assert store._conn().total_changes == changes
        assert store.capture_terminal_observation(target) == before
        assert preflight.health('room') is None
    finally:
        store.close()


@pytest.mark.parametrize('mutation', ['insert', 'payload', 'state', 'delete'])
def test_mutated_terminal_preparation_rebuilds_only_invalid_generation(tmp_path, monkeypatch, mutation):
    store, _source, _index = _store(tmp_path)
    try:
        store.prepare_terminal_observation()
        target = 'room|viewer@laptop'
        seq = store.outbox_add('append', target, {'kind': 'message'})
        old = store.refresh_terminal_observation(target)
        if mutation == 'insert':
            store.outbox_add('append', target, {'kind': 'info', 'event': {'type': 'member_left'}})
        else:
            with store._conn() as conn:
                if mutation == 'payload':
                    conn.execute("UPDATE outbox SET payload=? WHERE seq=?",
                                 ('{"kind":"info","event":{"type":"member_left"}}', seq))
                elif mutation == 'state':
                    conn.execute("UPDATE outbox SET state='dead' WHERE seq=?", (seq,))
                else:
                    conn.execute('DELETE FROM outbox WHERE seq=?', (seq,))
        with pytest.raises(page_preparation.terminal_observation.TerminalObservationUnavailable):
            store.capture_terminal_observation(target, expected=old)
        calls = []
        original = store.refresh_terminal_observation
        def refresh(selected):
            calls.append(selected)
            return original(selected)
        monkeypatch.setattr(store, 'refresh_terminal_observation', refresh)
        ready = []
        preflight = PagePreparation(_mesh(store), on_ready=lambda *args: ready.append(args))
        preflight._schema_ready = True
        for _ in range(2):
            assert preflight.request('room')
            assert preflight.run_one()
        assert calls == [target]
        assert ready == [('chat', 'room')]
        current = store.capture_terminal_observation(target)
        assert current.position != old
        assert current.pending is (mutation in ('insert', 'payload'))
    finally:
        store.close()


def test_mutation_after_valid_capture_keeps_consumers_pending(tmp_path, monkeypatch):
    store, _source, _index = _store(tmp_path)
    try:
        store.prepare_terminal_observation()
        target = 'room|viewer@laptop'
        store.outbox_add('append', target, {'kind': 'message'})
        old = store.refresh_terminal_observation(target)
        original = store.capture_terminal_observation
        def capture_then_mutate(selected):
            captured = original(selected)
            store.outbox_add('append', target, {'kind': 'info', 'event': {'type': 'member_left'}})
            return captured
        monkeypatch.setattr(store, 'capture_terminal_observation', capture_then_mutate)
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda *_: pytest.fail('valid capture unnecessarily rebuilt'))
        preflight = PagePreparation(_mesh(store))
        preflight._schema_ready = True
        assert preflight.request('room') and preflight.run_one()
        with pytest.raises(page_preparation.terminal_observation.TerminalObservationUnavailable):
            original(target)
        with pytest.raises(page_preparation.terminal_observation.TerminalObservationUnavailable):
            original(target, expected=old)
    finally:
        store.close()


@pytest.mark.parametrize('switch_route', [False, True])
def test_selected_terminal_bypasses_inventory_but_third_turn_serves_background(
        tmp_path, monkeypatch, switch_route):
    store, _source, _index = _store(tmp_path)
    try:
        prep = PagePreparation(_mesh(store))
        prep._schema_ready = True
        work = []
        def absent(_target):
            raise page_preparation.terminal_observation.TerminalObservationUnavailable('pending')
        monkeypatch.setattr(store, 'capture_terminal_observation', absent)
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda target: work.append(target.split('|')[0]))
        for n in range(70):
            prep.request(f'background{n}')
        for n in range(9):
            selected = f'selected{n}' if switch_route else 'selected'
            assert prep.select(selected)
            prep.request(selected)
            assert prep.run_one()
        assert work[0] == ('selected0' if switch_route else 'selected')
        assert work[2] == 'background0'
        assert work[5] == 'background1'
        assert work[8] == 'background2'
        assert len(prep._terminals) <= 128
    finally:
        store.close()


def test_selected_terminal_priority_keeps_proof_alternation_and_clear_fifo(tmp_path, monkeypatch):
    store, _source, index = _store(tmp_path)
    try:
        prep = PagePreparation(_mesh(store))
        prep._schema_ready = True
        work = []
        def absent(_target):
            raise page_preparation.terminal_observation.TerminalObservationUnavailable('pending')
        monkeypatch.setattr(store, 'capture_terminal_observation', absent)
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda target: work.append(target.split('|')[0]))
        monkeypatch.setattr(store, 'capture_overlay_proofs', lambda *_a: ())
        monkeypatch.setattr(store, 'verify_overlay_signature', lambda *_a: work.append('proof'))
        pub = crypto.identity_pubs(crypto.generate_identity())[0]
        prep.request('oldest')
        prep.request('room', index=index, proofs=((P.state('room', 'viewer'), pub),))
        prep.select('room')
        assert prep.run_one()
        assert prep.run_one()
        assert work == ['room', 'proof']
        prep.request('room')
        prep.select(None)
        assert prep.run_one()
        assert work[-1] == 'oldest'
        prep.close()
        assert not prep.select('room')
        assert not prep.run_one()
    finally:
        store.close()


@pytest.mark.parametrize('selected', ['', True, 5, 'foreign/room', 'x' * 257])
def test_selected_preparation_hint_rejects_invalid_room_without_io(tmp_path, selected):
    store, _source, _index = _store(tmp_path)
    try:
        prep = PagePreparation(_mesh(store))
        with pytest.raises((ValueError, TypeError)):
            prep.select(selected)
        assert not prep._terminals and not prep._proofs
    finally:
        store.close()



def test_proof_jobs_do_not_reset_selected_terminal_fairness(tmp_path, monkeypatch):
    store, _source, index = _store(tmp_path)
    try:
        prep = PagePreparation(_mesh(store))
        prep._schema_ready = True
        work = []
        def absent(_target):
            raise page_preparation.terminal_observation.TerminalObservationUnavailable('pending')
        monkeypatch.setattr(store, 'capture_terminal_observation', absent)
        monkeypatch.setattr(store, 'refresh_terminal_observation',
                            lambda target: work.append(target.split('|')[0]))
        monkeypatch.setattr(store, 'capture_overlay_proofs', lambda *_a: ())
        monkeypatch.setattr(store, 'verify_overlay_signature', lambda *_a: work.append('proof'))
        pub = crypto.identity_pubs(crypto.generate_identity())[0]
        prep.select('room')
        prep.request('background')
        for _ in range(3):
            prep.request('room', index=index, proofs=((P.state('room', 'viewer'), pub),))
            assert prep.run_one()
            assert prep.run_one()
        assert work == ['room', 'proof', 'room', 'proof', 'background', 'proof']
    finally:
        store.close()
