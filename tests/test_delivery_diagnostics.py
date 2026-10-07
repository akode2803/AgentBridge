import json
import sqlite3
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from agentbridge.core import delivery_trace as trace
from agentbridge.gui.diagnostics import Diagnostics
from agentbridge.gui.routing import Request, dispatch
from agentbridge.store.db import Store
from agentbridge.store.outbox import OutboxWorker


def _writer_controls(sink):
    config = sink.configuration()
    return {name: config[name] for name in (
        'writer_queue', 'write_failures', 'context_rows', 'context_evicted',
        'context_dropped', 'rate_dropped', 'rate_rejected', 'queue_overflow',
        'admission_dropped')}


def rows(sink):
    drained = sink.flush()
    controls = _writer_controls(sink)
    if not drained:
        # Never inspect a partial file as a complete retention result, and
        # never dump configuration's path or the queued private observations.
        raise AssertionError(f'diagnostics writer did not drain within its existing bound: {controls!r}')
    if controls['write_failures']:
        raise AssertionError(f'diagnostics writer failed: {controls!r}')
    return [
        json.loads(line)
        for path in sorted(sink.directory.glob("events*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def test_rows_reject_partial_trigger_first_write_with_bounded_safe_diagnostics(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path / 'PRIVATE_PATH')
    assert sink.set_enabled(True, slow_ms=100, sample_rate=0)
    blocked = threading.Event()
    release = threading.Event()
    original = sink.record
    calls = []

    def hold_context(data, **kwargs):
        calls.append(data.get('phase'))
        if len(calls) == 2:
            blocked.set()
            if not release.wait(5):
                raise RuntimeError('bounded test writer gate expired')
        return original(data, **kwargs)

    monkeypatch.setattr(sink, 'record', hold_context)
    try:
        common = {'event': 'delivery', 'chat_ref': 'c' * 16, 'body': 'PRIVATE_BODY'}
        assert sink.flight_record({**common, 'phase': 'preparation_queued'})
        assert sink.flight_record({**common, 'phase': 'preparation_claimed', 'duration_ms': 1})
        assert sink.flight_record({**common, 'phase': 'preparation_claimed', 'queue_wait_ms': 100})
        assert blocked.wait(1), 'writer did not reach second admission'
        partial = [json.loads(line) for line in sink.path.read_text().splitlines()]
        assert any(row.get('queue_wait_ms') == 100 for row in partial)
        assert not any(row.get('phase') == 'preparation_queued' for row in partial)
        with pytest.raises(AssertionError, match='writer did not drain') as raised:
            rows(sink)
        message = str(raised.value)
        assert "'writer_queue': 1" in message
        assert "'write_failures': 0" in message
        assert 'PRIVATE' not in message
    finally:
        release.set()
        sink.close()
        if sink._worker is not None:
            sink._worker.join(1)
            assert not sink._worker.is_alive(), 'test writer was not reaped'
    retained = rows(sink)
    assert any(row.get('queue_wait_ms') == 100 for row in retained)
    assert any(row.get('phase') == 'preparation_queued' for row in retained)
    assert all('PRIVATE' not in json.dumps(row) for row in retained)


def test_slow_context_privacy_and_bounds(tmp_path):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True, slow_ms=100, sample_rate=0)
    for n in range(1200):
        assert sink.flight_record(
            {
                "event": "delivery",
                "phase": "request_started",
                "request_ref": "1234567890abcdef",
                "duration_ms": 1,
                "body": "PRIVATE",
                "sql": "SECRET",
                "path": "SENSITIVE",
            }
        )
    config = sink.configuration()
    assert config["context_rows"] <= 512 and config["context_bytes"] <= 256 * 1024
    assert config["context_evicted"] > 0 and config["sampled_out"] > 0
    assert config["context_dropped"] == 0
    assert not sink.path.exists()
    sink.flight_record(
        {
            "event": "server_request",
            "phase": "request_finished",
            "request_ref": "1234567890abcdef",
            "duration_ms": 101,
            "status": "ok",
        }
    )
    result = rows(sink)
    assert any(row.get("duration_ms") == 101 for row in result)
    assert len(result) <= 48
    raw = sink.path.read_text()
    assert all(word not in raw for word in ("PRIVATE", "SECRET", "SENSITIVE"))
    assert all(row["clock_ref"] == sink.clock_ref for row in result)
    assert sink.set_enabled(False)
    assert sink.configuration()["context_rows"] == 0
    assert not sink.flight_record({"event": "delivery", "phase": "outbox_attempt"})


def test_optout_deferred_generation_and_faults(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=1)
    with trace.transaction("root", tmp_path / "private.sqlite") as tx:
        with tx.acquiring():
            pass
        assert not sink.path.exists(), "no telemetry disk writes under transaction"
        assert sink.set_enabled(False)
        assert sink.set_enabled(True)
    assert not sink.path.exists(), "old generation never flushes after re-enable"
    monkeypatch.setattr(
        sink, "flight_record", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    with trace.transaction("root", tmp_path / "private.sqlite") as tx:
        with tx.acquiring():
            pass
        with tx.finishing("db_commit"):
            pass
    assert not trace._holders
    trace.emit("outbox_retry", message="private", status="error")


def test_db_wait_holder_and_original_exception(tmp_path):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=1)
    database = tmp_path / "SECRET.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE test(n)")
    conn.close()
    owned = threading.Event()
    release = threading.Event()

    def holder():
        con = sqlite3.connect(database)
        with trace.transaction("root", database) as tx:
            with tx.acquiring():
                con.execute("BEGIN IMMEDIATE")
            owned.set()
            release.wait(2)
            with tx.finishing("db_commit"):
                con.commit()
            con.close()

    thread = threading.Thread(target=holder)
    thread.start()
    assert owned.wait(2)
    con = sqlite3.connect(database, timeout=0.02)
    try:
        with pytest.raises(sqlite3.OperationalError):
            with trace.transaction("root", database) as tx:
                try:
                    with tx.acquiring():
                        con.execute("BEGIN IMMEDIATE")
                finally:
                    con.close()
    finally:
        release.set()
        thread.join(2)
    result = rows(sink)
    wait = next(
        row for row in result if row.get("phase") == "db_acquire" and row["status"] == "error"
    )
    assert wait["holder_count"] == 1 and wait["holder_coverage"] == "acquisition_start_only"
    assert wait["duration_ms"] >= 10 and wait["database_ref"]
    assert not trace._holders
    assert "SECRET" not in sink.path.read_text() and "BEGIN" not in sink.path.read_text()


def test_config_invalid_sampling_error_rate_and_disk_failure(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path)
    for value in (-1, float("nan"), True, 100000):
        assert not sink.set_enabled(True, slow_ms=value)
    assert sink.set_enabled(True, sample_rate=0)
    assert Diagnostics(tmp_path).sample_rate == 0
    trace.install(sink)
    for n in range(200):
        sink.flight_record({"event": "delivery", "phase": "outbox_retry", "status": "error"})
    assert len(rows(sink)) <= 64 and sink.configuration()["rate_dropped"] > 0
    monkeypatch.setattr(sink, "record", lambda *_a, **_k: False)
    assert dispatch(
        lambda *_: {"ok": True}, SimpleNamespace(diagnostics=sink), Request(path="/api/state")
    ) == {"ok": True}


def test_local_snapshot_admission_is_retained_as_delivery_breadcrumb(tmp_path):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True, sample_rate=0)
    trace.emit(
        'local_append_completed', message='private-message',
        chat='private-chat', outcome='completed',
    )
    trace.emit(
        'local_snapshot_admitted', message='private-message',
        chat='private-chat', outcome='completed',
    )
    result = rows(sink)
    assert any(row.get('phase') == 'local_append_completed' for row in result)
    admitted = next(
        row for row in result if row.get('phase') == 'local_snapshot_admitted'
    )
    assert admitted['outcome'] == 'completed'
    assert len(admitted['trace_ref']) == len(admitted['chat_ref']) == 16
    assert 'private-message' not in sink.path.read_text()
    assert 'private-chat' not in sink.path.read_text()


def test_source_reconciliation_profile_keeps_only_bounded_stage_metrics(tmp_path):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True, sample_rate=1)
    trace.emit(
        'source_reconciliation', chat='private-chat', duration_ms=12.5,
        change_check_ms=0.25, collect_ms=7.5, stage_write_ms=2.0, compare_ms=1.0,
        documents_examined=20_000, documents_selected=3,
        document_bytes=144, document_batches=1,
        private_path='/do/not/record',
    )
    event = next(
        row for row in rows(sink)
        if row.get('phase') == 'source_reconciliation'
    )
    assert event['duration_ms'] == 12.5
    assert event['change_check_ms'] == 0.25
    assert event['collect_ms'] == 7.5
    assert event['stage_write_ms'] == 2.0
    assert event['documents_examined'] == 20_000
    assert event['documents_selected'] == 3
    assert event['document_bytes'] == 144
    assert event['document_batches'] == 1
    assert 'private_path' not in event
    assert 'private-chat' not in sink.path.read_text()


def test_source_reconciliation_sampling_key_and_two_collection_bounds(tmp_path):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True, sample_rate=0.01)
    assert sink.flight_record({
        'event': 'delivery', 'phase': 'source_reconciliation', 'status': 'ok',
        'sample_ref': '0' * 16, 'documents_examined': 4_000_000,
        'documents_selected': 2_000_000,
        'document_bytes': 1024 * 1024 * 1024,
        'document_batches': 2_000_000,
    })
    event = next(row for row in rows(sink)
                 if row.get('phase') == 'source_reconciliation')
    assert event['sample_ref'] == '0' * 16
    assert event['documents_examined'] == 4_000_000
    assert event['documents_selected'] == 2_000_000
    assert event['document_bytes'] == 1024 * 1024 * 1024
    assert event['document_batches'] == 2_000_000


def test_forged_correlation_clock_and_crash_breadcrumb(tmp_path):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=0)
    result = dispatch(
        lambda *_: {"ok": True, "id": "m-private"},
        SimpleNamespace(diagnostics=sink),
        Request(
            method="POST",
            path="/api/mesh/post",
            data={"chat_id": "room-private"},
            diagnostic_ref="password-private",
        ),
    )
    assert len(result["_diagnostics"]["trace_ref"]) == 16
    # Request start is durable even though its finish can be unsampled.
    assert any(row.get("phase") == "request_started" for row in rows(sink))
    assert "password-private" not in sink.path.read_text()
    sink.flight_record(
        {
            "event": "delivery",
            "phase": "native_ack",
            "tab_ref": "0" * 16,
            "monotonic_ms": 123,
            "clock_ref": "f" * 16,
            "trace_ref": "a" * 16,
        },
        client=True,
    )
    final = rows(sink)[-1]
    assert (
        final["origin"] == "browser"
        and final["clock_ref"] == "0" * 16
        and final["monotonic_ms"] == 123
    )


def test_late_observed_completion_and_overflow_are_fenced(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=1)

    @trace.observed("ingestion")
    def work():
        sink.set_enabled(False)
        sink.set_enabled(True)
        return 7

    assert work() == 7
    sink.flush()
    assert not sink.path.exists()
    calls = []
    original = sink.note_drop
    monkeypatch.setattr(sink, "note_drop", lambda *args: (calls.append(args), original(*args))[1])
    with trace.transaction("root", tmp_path / "hidden.sqlite"):
        for _ in range(150):
            trace.emit("db_body", duration_ms=1)
        assert not calls, "overflow never takes recorder lock while in transaction"
    assert calls and calls[0] == ("context", 22)


@pytest.mark.parametrize("replacement", [False, True], ids=["off-on", "new-recorder"])
@pytest.mark.parametrize("request_scope", [False, True], ids=["background", "request"])
def test_nested_outbox_completions_keep_original_recorder_ownership(
    tmp_path, replacement, request_scope
):
    store = Store(tmp_path / "private.sqlite")
    store.outbox_add("post", "private-room", {"id": "old-message"})
    sink = Diagnostics(tmp_path / "first")
    assert sink.set_enabled(True, sample_rate=1)
    active_sink = sink
    started, release = threading.Event(), threading.Event()
    results, failures = [], []

    @trace.observed("page_prepare")
    def nested():
        with trace.transaction("root", store.path) as tx:
            with tx.acquiring():
                pass
            trace.emit("transport_read", message="private-message")
            with tx.finishing("db_commit"):
                pass

    def provider(_target, payload):
        if payload["id"] == "old-message":
            started.set()
            assert release.wait(2), "test must release the blocked provider"
        # A fresh nested decorator/transaction must retain the stale outer owner.
        nested()

    worker = OutboxWorker(store, {"post": provider})

    def flush():
        try:
            scope = trace.request_context("a" * 16, 1) if request_scope else nullcontext()
            with scope:
                results.append(worker.flush_once())
            # Same-thread work after scope exit must be admitted without request leakage.
            trace.emit("preparation", message="fresh-after-scope")
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=flush, daemon=True)
    thread.start()
    try:
        assert started.wait(2)
        before = rows(sink)
        assert [row["phase"] for row in before] == ["outbox_attempt"]
        assert before[0].get("request_ref") == ("a" * 16 if request_scope else None)
        if replacement:
            active_sink = Diagnostics(tmp_path / "replacement")
            assert active_sink.set_enabled(True, sample_rate=1)
            assert active_sink.generation == sink.generation
        else:
            assert sink.set_enabled(False)
            assert sink.set_enabled(True)
        release.set()
        thread.join(2)
        assert not thread.is_alive() and not failures and results == [1]
        assert store.outbox_counts() == {}, "tracing never changes delivery completion"
        completed = rows(active_sink)
        if replacement:
            assert rows(sink) == before
        else:
            completed = completed[len(before):]
        assert [row["phase"] for row in completed] == ["preparation"]
        assert "request_ref" not in completed[0]

        # Fresh work records all nested phases and retains its enclosing request tag.
        store.outbox_add("post", "private-room", {"id": "new-message"})
        with trace.request_context("c" * 16, 2):
            assert worker.flush_once() == 1
        fresh = [row for row in rows(active_sink) if row.get("request_ref") == "c" * 16]
        assert {row["phase"] for row in fresh} == {
            "outbox_attempt", "transport_read", "page_prepare", "db_acquire",
            "db_body", "db_commit", "transport_append", "append_ack_observed", "outbox_batch",
        }
        assert all(row.get("request_seq") == 2 for row in fresh)
        assert not trace._holders
        assert "private-room" not in active_sink.path.read_text()
    finally:
        release.set()
        thread.join(2)
        store.close()
        sink.close()
        if active_sink is not sink:
            active_sink.close()


@pytest.mark.parametrize("replacement", [False, True], ids=["off-on", "new-recorder"])
def test_request_context_retains_owner_across_nested_request(tmp_path, replacement):
    sink = Diagnostics(tmp_path / "first")
    assert sink.set_enabled(True, sample_rate=1)
    active_sink = sink
    try:
        with trace.request_context("a" * 16, 1):
            if replacement:
                active_sink = Diagnostics(tmp_path / "replacement")
                assert active_sink.set_enabled(True, sample_rate=1)
                assert active_sink.generation == sink.generation
            else:
                assert sink.set_enabled(False)
                assert sink.set_enabled(True)
            # A nested request can update correlation, but cannot reopen admission.
            with trace.request_context("b" * 16, 2):
                trace.emit("request_started")
            trace.emit("request_finished")
        trace.emit("native_ack")
        result = rows(active_sink)
        assert [row["phase"] for row in result] == ["native_ack"]
        assert "request_ref" not in result[0]
        if replacement:
            assert rows(sink) == []
    finally:
        sink.close()
        if active_sink is not sink:
            active_sink.close()


@pytest.mark.parametrize("replacement", [False, True], ids=["off-on", "new-recorder"])
def test_observed_restores_ownership_after_original_base_exception(tmp_path, replacement):
    sink = Diagnostics(tmp_path / "first")
    assert sink.set_enabled(True, sample_rate=1)
    active_sink = sink
    failure = KeyboardInterrupt("original private failure")

    @trace.observed("ingestion")
    def failing():
        nonlocal active_sink
        if replacement:
            active_sink = Diagnostics(tmp_path / "replacement")
            assert active_sink.set_enabled(True, sample_rate=1)
            assert active_sink.generation == sink.generation
        else:
            assert sink.set_enabled(False)
            assert sink.set_enabled(True)
        with trace.request_context("b" * 16, 9):
            trace.emit("transport_read")
        raise failure

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            failing()
        assert caught.value is failure
        trace.emit("native_ack")
        result = rows(active_sink)
        assert [row["phase"] for row in result] == ["native_ack"]
        assert "request_ref" not in result[0]
        if replacement:
            assert rows(sink) == []
    finally:
        sink.close()
        if active_sink is not sink:
            active_sink.close()


def test_delivery_admission_never_waits_for_log_disk(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=1)
    started = threading.Event()
    release = threading.Event()
    original = sink.record

    def blocked_write(*args, **kwargs):
        started.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(sink, "record", blocked_write)
    sink.flight_record({"event": "delivery", "phase": "outbox_attempt"})
    assert started.wait(1)
    admitted = threading.Event()
    thread = threading.Thread(
        target=lambda: (
            sink.flight_record({"event": "delivery", "phase": "outbox_retry", "status": "error"}),
            admitted.set(),
        )
    )
    thread.start()
    try:
        assert admitted.wait(0.2), "delivery admission does not wait on writer disk lock"
    finally:
        release.set()
        thread.join(1)
        sink.flush()


def test_persisted_enable_thread_failure_cannot_break_startup(tmp_path, monkeypatch):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True)
    sink.close()
    monkeypatch.setattr(
        Diagnostics,
        "_start_writer",
        lambda *_: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )
    recovered = Diagnostics(tmp_path)
    assert recovered.enabled and recovered.configuration()["write_failures"] == 1
    assert recovered.flight_record({"event": "delivery", "phase": "outbox_attempt"})


def test_fast_sender_reconciliation_is_retained_without_sampling(tmp_path):
    sink = Diagnostics(tmp_path)
    sink.set_enabled(True, sample_rate=0)
    assert sink.flight_record({'event':'delivery', 'phase':'send_reconciled',
                              'trace_ref':'f'*16, 'duration_ms':1, 'flow':'sent'})
    assert any(row.get('phase') == 'send_reconciled' for row in rows(sink))


@pytest.mark.parametrize("client,phase", [(False, "preparation_claimed"), (True, "refresh_started")])
def test_slow_queue_wait_retains_private_context_without_sampling(tmp_path, client, phase):
    sink = Diagnostics(tmp_path)
    assert sink.set_enabled(True, slow_ms=100, sample_rate=0)
    try:
        common = {"event": "delivery", "chat_ref": "c" * 16,
                  "body": "PRIVATE_BODY", "sql": "PRIVATE_SQL"}
        assert sink.flight_record({**common, "phase": "preparation_queued"}, client=client)
        # Nested or overlapping intervals must not be summed into a slow trigger.
        assert sink.flight_record({**common, "phase": phase, "duration_ms": 60,
                                   "queue_wait_ms": 60}, client=client)
        for invalid in (-1, float("nan"), float("inf"), "PRIVATE_WAIT"):
            assert sink.flight_record({**common, "phase": phase, "queue_wait_ms": invalid},
                                      client=client)
        assert rows(sink) == []
        assert sink.flight_record({**common, "phase": phase, "duration_ms": 1,
                                   "queue_wait_ms": 100}, client=client)
        retained = rows(sink)
        assert any(row.get("queue_wait_ms") == 100 for row in retained)
        if not any(row.get("phase") == "preparation_queued" for row in retained):
            raise AssertionError(f'queued context absent after successful writer drain: {_writer_controls(sink)!r}')
        assert len(retained) <= 48
        assert all("PRIVATE" not in json.dumps(row) for row in retained)
    finally:
        sink.close()


@pytest.mark.parametrize("payload,message", [
    ({"id": "PRIVATE_MESSAGE", "body": "PRIVATE_BODY"}, "PRIVATE_MESSAGE"),
    ({"envelope": {"id": "PRIVATE_MESSAGE", "body": "PRIVATE_BODY"}}, "PRIVATE_MESSAGE"),
    ({"id": 123, "body": "PRIVATE_BODY"}, None),
    (["PRIVATE_BODY"], None),
])
def test_missing_outbox_handler_retains_sanitized_dead_outcome(tmp_path, payload, message):
    store = Store(tmp_path / "PRIVATE_STORE.sqlite")
    sink = Diagnostics(tmp_path / "diagnostics")
    assert sink.set_enabled(True, sample_rate=0)
    try:
        store.outbox_add("PRIVATE_KIND", "PRIVATE_TARGET", payload)
        worker = OutboxWorker(store, {})
        assert worker.flush_once() == 0
        assert store.outbox_counts() == {"dead": 1}
        assert worker.flush_once() == 0
        retained = rows(sink)
        dead = [row for row in retained if row.get("phase") == "outbox_dead"]
        assert len(dead) == 1 and dead[0]["status"] == "error"
        assert dead[0].get("trace_ref") == (sink.chat_ref(message) if message else None)
        assert not any(row.get("phase") == "outbox_attempt" for row in retained)
        assert "PRIVATE" not in sink.path.read_text()
    finally:
        store.close()
        sink.close()


def test_missing_handler_failed_dead_transition_keeps_original_error(tmp_path, monkeypatch):
    store = Store(tmp_path / "cache.sqlite")
    sink = Diagnostics(tmp_path / "diagnostics")
    assert sink.set_enabled(True, sample_rate=0)
    failure = OSError("PRIVATE_DEAD_FAILURE")

    def fail_dead(*_):
        raise failure

    try:
        store.outbox_add("unknown", "target", {"id": "PRIVATE_MESSAGE"})
        monkeypatch.setattr(store, "outbox_dead", fail_dead)
        with pytest.raises(OSError) as caught:
            OutboxWorker(store, {}).flush_once()
        assert caught.value is failure
        assert store.outbox_counts() == {"pending": 1}
        assert not any(row.get("phase") == "outbox_dead" for row in rows(sink))
        assert "PRIVATE" not in sink.path.read_text()
    finally:
        store.close()
        sink.close()
