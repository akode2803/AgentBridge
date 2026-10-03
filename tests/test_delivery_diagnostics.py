import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from agentbridge.core import delivery_trace as trace
from agentbridge.gui.diagnostics import Diagnostics
from agentbridge.gui.routing import Request, dispatch


def rows(sink):
    sink.flush()
    return [
        json.loads(line)
        for path in sorted(sink.directory.glob("events*.jsonl"))
        for line in path.read_text().splitlines()
    ]


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
    assert config["context_dropped"] > 0 and config["sampled_out"] > 0
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
