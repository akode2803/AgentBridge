"""Offline GUI summaries remain bounded, content-free and non-additive."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentbridge.gui.diagnostics import Diagnostics, MAX_LINE_BYTES
from devtools import summarize_gui_diagnostics as summary


def _event(event="server_request", origin="server", **fields):
    return {"event": event, "origin": origin, "route": "/api/mesh/chat_page",
            "status": "page", **fields}


def _line(record):
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def _write(tmp_path, records, name="events.jsonl"):
    path = tmp_path / name
    path.write_bytes(b"".join(_line(record) for record in records))
    return path


def _one(result, category="server"):
    assert len(result["series"][category]) == 1
    return result["series"][category][0]


def test_exact_nearest_rank_percentiles_and_deterministic_output(tmp_path):
    path = _write(tmp_path, [_event(duration_ms=n) for n in range(100, 0, -1)])
    result = summary.summarize([path])
    row = _one(result)
    assert row["events"] == 100
    assert row["duration_ms"] == {
        "samples": 100, "min": 1, "p50": 50, "p95": 95, "p99": 99, "max": 100,
        "missing": 0, "invalid": 0,
    }
    assert row["status_counts"] == {"page": 100}
    assert result["percentile_method"] == "nearest_rank"
    assert result == summary.summarize([path])
    assert result["input"]["complete"] is True
    assert result["input"]["bytes_read"] == path.stat().st_size


@pytest.mark.parametrize(("durations", "expected"), [
    ([0], (0, 0, 0)), ([10, 20], (10, 20, 20)), ([4, 4, 4], (4, 4, 4)),
])
def test_small_percentile_samples(tmp_path, durations, expected):
    path = _write(tmp_path, [_event(duration_ms=n) for n in durations])
    values = _one(summary.summarize([path]))["duration_ms"]
    assert (values["p50"], values["p95"], values["p99"]) == expected


def test_overlapping_layers_and_phases_are_never_combined(tmp_path):
    # These are deliberately nested, with identical identifiers/timestamps. They
    # cannot be paired reliably by the actual schema, or summed as latency stages.
    common = {"ts": "2026-09-30T00:00:00.000+00:00", "server_pid": 123,
              "tab_ref": "0123456789abcdef", "chat_ref": "fedcba9876543210"}
    records = [
        _event("client_request", "browser", duration_ms=60, **common),
        _event("page_read", "browser", duration_ms=80, mode="first", **common),
        _event("page_paint", "browser", duration_ms=100, mode="first",
               outcome="completed", **common),
        _event(duration_ms=50, **common),
        _event("page_stage", phase="sidebar", duration_ms=40, **common),
        _event("page_stage", phase="inputs", duration_ms=5, **common),
        _event("page_stage", phase="prepare", **common),
        _event("page_stage", phase="finalize", **common),
    ]
    result = summary.summarize([_write(tmp_path, records)])
    browser = {row["event"]: row for row in result["series"]["browser"]}
    assert browser["page_paint"]["duration_ms"]["p50"] == 100
    assert browser["page_read"]["duration_ms"]["p50"] == 80
    assert browser["client_request"]["duration_ms"]["p50"] == 60
    assert _one(result)["duration_ms"]["p50"] == 50
    stages = {row["phase"]: row for row in result["series"]["page_stage"]}
    assert stages["sidebar"]["duration_ms"]["p50"] == 40
    assert stages["inputs"]["duration_ms"]["p50"] == 5
    assert stages["prepare"]["duration_ms"]["missing"] == 1
    assert stages["finalize"]["duration_ms"]["p50"] is None
    assert "overlap" in result["notes"][0]
    assert not {"total_ms", "render_ms", "network_ms"} & set(result)
    # Server-only stage categorization must not relabel client-supplied events.
    path = _write(tmp_path, [_event("page_stage", "browser", duration_ms=2)])
    assert _one(summary.summarize([path]), "browser")["event"] == "page_stage"


def test_source_reconciliation_metrics_are_bounded_and_non_additive(tmp_path):
    complete = _event(
        "delivery", phase="source_reconciliation", status="ok",
        duration_ms=30, capture_claim_ms=2, change_check_ms=0.5, stage_open_ms=3,
        collect_ms=20, stage_write_ms=12, seal_ms=1, compare_ms=4,
        admit_ms=2, source_finalize_ms=1, cleanup_ms=1,
        documents_examined=20_000, documents_selected=5,
        document_bytes=1000, document_batches=2,
    )
    partial = _event(
        "delivery", phase="source_reconciliation", status="error",
        duration_ms=10, collect_ms=True, documents_examined="private",
    )
    result = summary.summarize([_write(tmp_path, [complete, partial])])
    profile = result["source_reconciliation"]

    assert result["schema_version"] == 2
    assert profile["events"] == 2
    assert profile["metrics"]["change_check_ms"]["samples"] == 1
    assert profile["metrics"]["change_check_ms"]["p50"] == 0.5
    assert profile["metrics"]["duration_ms"]["p50"] == 10
    assert profile["metrics"]["duration_ms"]["p95"] == 30
    assert profile["metrics"]["collect_ms"]["samples"] == 1
    assert profile["metrics"]["collect_ms"]["invalid"] == 1
    assert profile["metrics"]["documents_examined"]["invalid"] == 1
    assert profile["metrics"]["documents_selected"]["missing"] == 1
    assert profile["metrics"]["stage_write_ms"]["p50"] == 12
    assert "private" not in json.dumps(result)
    assert "overlap" in result["notes"][0]


def test_accepts_actual_recorder_schema_without_starting_app(tmp_path):
    recorder = Diagnostics(tmp_path)
    try:
        assert recorder.set_enabled(True, sample_rate=1)
        assert recorder.record({"event": "client_request", "route": "/api/mesh/state",
                                "duration_ms": 10, "status": "ready"}, client=True)
        recorder.stage("/api/mesh/state", "private-chat", "prepare", "prepared",
                       duration_ms=3)
        assert recorder.flush()
        raw = recorder.path.read_text()
        result = summary.summarize([recorder.path])
    finally:
        recorder.close()
    assert result["input"]["accepted_records"] == 2
    assert _one(result, "page_stage")["phase"] == "prepare"
    assert _one(result, "browser")["route"] == "/api/mesh/state"
    assert "private-chat" not in raw and "private-chat" not in json.dumps(result)


@pytest.mark.parametrize("duration", [
    -1, True, False, None, "123", [], {}, float("nan"), float("inf"),
    float("-inf"), 1_000_000_001, 10**500,
])
def test_invalid_durations_are_counted_not_coerced(tmp_path, duration):
    path = _write(tmp_path, [_event(duration_ms=duration), _event(), _event(duration_ms=0)])
    values = _one(summary.summarize([path]))["duration_ms"]
    assert values == {"samples": 1, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0,
                      "missing": 1, "invalid": 1}
    json.dumps(values, allow_nan=False)


def test_missing_duration_has_null_percentiles(tmp_path):
    result = summary.summarize([_write(tmp_path, [_event()])])
    assert _one(result)["duration_ms"] == {
        "samples": 0, "missing": 1, "invalid": 0,
        "min": None, "max": None, "p50": None, "p95": None, "p99": None,
    }


def test_malformed_utf8_deep_json_blank_lines_and_truncated_tail(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(_event(duration_ms=4)) + b"garbage\n\xff\n\n"
                     + b"[" * 1000 + b"]" * 999 + b"\n"
                     + b'{"event":"server_request",')
    result = summary.summarize([path])
    assert result["input"]["accepted_records"] == 1
    assert result["input"]["malformed_lines"] == 5
    assert result["input"]["unterminated_lines"] == 1
    assert result["input"]["complete"] is True
    assert _one(result)["duration_ms"]["p50"] == 4


def test_valid_final_line_without_newline_is_accepted(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(_event(duration_ms=4)).rstrip(b"\n"))
    result = summary.summarize([path])
    assert result["input"]["accepted_records"] == 1
    assert result["input"]["unterminated_lines"] == 1
    assert result["input"]["malformed_lines"] == 0


@pytest.mark.parametrize("record", [
    [], None, 1, "secret", {}, {"event": []}, {"event": "private-event"},
    _event(origin="private-origin"), _event(origin=[]),
])
def test_unknown_events_origins_and_nonobjects_are_ignored(tmp_path, record):
    result = summary.summarize([_write(tmp_path, [record])])
    assert result["input"]["ignored_records"] == 1
    assert result["series"] == {"browser": [], "server": [], "page_stage": []}


def test_secret_bearing_fields_never_appear_in_summary_or_errors(tmp_path, capsys):
    secret = "SECRET_BODY_PASSWORD_TOKEN"
    record = _event(duration_ms=4, route="/api/mesh/chat_page?token=" + secret,
                    status=secret, phase=secret, mode=secret, reason=secret,
                    outcome=secret, error_type=secret, ts=secret, server_pid=secret,
                    chat_ref=secret, tab_ref=secret, body={secret: secret}, stack=secret,
                    **{secret: secret})
    path = _write(tmp_path, [record], name=secret + ".jsonl")
    result = summary.summarize([path, tmp_path / (secret + "-missing")])
    encoded = json.dumps(result)
    assert secret not in encoded
    assert str(tmp_path) not in encoded
    row = _one(result)
    assert row["route"] == row["phase"] == row["mode"] == "other"
    assert row["status_counts"] == {"other": 1}
    assert result["input"]["errors"] == [{"file_index": 1, "kind": "unreadable_file"}]
    assert summary.main([str(path)]) == 0
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_oversized_line_is_discarded_and_next_line_is_processed(tmp_path):
    path = tmp_path / "events.jsonl"
    # A valid, allowlisted event with a secret-bearing unknown field still cannot
    # exceed the recorder's line ceiling. Never parse a suffix as a new record.
    path.write_bytes(_line(_event(body="x" * (MAX_LINE_BYTES * 4)))
                     + _line(_event(duration_ms=8)))
    result = summary.summarize([path])
    assert result["input"]["oversized_lines"] == 1
    assert result["input"]["lines_read"] == 2
    assert result["input"]["accepted_records"] == 1
    assert _one(result)["duration_ms"]["p50"] == 8


def test_line_limit_includes_newline(tmp_path):
    prefix = _line(_event())[:-1]
    path = tmp_path / "events.jsonl"
    path.write_bytes(prefix + b" " * (MAX_LINE_BYTES - len(prefix) - 1) + b"\n")
    assert summary.summarize([path])["input"]["accepted_records"] == 1
    path.write_bytes(path.read_bytes()[:-1] + b" \n")
    assert summary.summarize([path])["input"]["oversized_lines"] == 1


@pytest.mark.parametrize("prefix", [b"", b"garbage\n", b"\n", b"x" * 5000 + b"\n"])
def test_record_budget_counts_every_physical_line(tmp_path, prefix):
    path = tmp_path / "events.jsonl"
    path.write_bytes(prefix + _line(_event(duration_ms=1)) + _line(_event(duration_ms=2)))
    result = summary.summarize([path], max_records=1)
    assert result["input"]["lines_read"] == 1
    assert result["input"]["accepted_records"] == (0 if prefix else 1)
    assert result["input"]["stopped_by"] == "max_records"
    assert result["input"]["complete"] is False


def test_byte_budget_is_global_and_never_parses_cut_fragment(tmp_path):
    first = _write(tmp_path, [_event(duration_ms=1)], "first.jsonl")
    second = _write(tmp_path, [_event(duration_ms=2)], "second.jsonl")
    # A cut just before the newline is itself valid JSON, but it isn't known to
    # be a complete physical record and must not count as a duration observation.
    budget = first.stat().st_size + second.stat().st_size - 1
    result = summary.summarize([first, second], max_bytes=budget)
    assert result["input"]["bytes_read"] == budget
    assert result["input"]["accepted_records"] == 1
    assert result["input"]["files_completed"] == 1
    assert result["input"]["budget_cut_lines"] == 1
    assert result["input"]["stopped_by"] == "max_bytes"
    assert result["input"]["complete"] is False


def test_byte_budget_cannot_be_evaded_by_oversized_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"x" * 20_000 + b"\n" + _line(_event(duration_ms=1)))
    result = summary.summarize([path], max_bytes=7000)
    assert result["input"]["bytes_read"] == 7000
    assert result["input"]["oversized_lines"] == 1
    assert result["input"]["accepted_records"] == 0
    assert result["input"]["budget_cut_lines"] == 1


def test_exact_eof_budgets_are_complete_and_next_file_is_not_opened(tmp_path):
    path = _write(tmp_path, [_event(duration_ms=1)])
    result = summary.summarize([path], max_bytes=path.stat().st_size, max_records=1)
    assert result["input"]["complete"] is True
    assert result["input"]["stopped_by"] is None
    result = summary.summarize([path, tmp_path / "missing"], max_records=1)
    assert result["input"]["files_opened"] == 1
    assert result["input"]["errors"] == []
    assert result["input"]["stopped_by"] == "max_records"


def test_empty_and_explicit_files_only_with_no_implicit_deduplication(tmp_path):
    empty = _write(tmp_path, [], "empty.jsonl")
    assert summary.summarize([empty])["input"]["files_completed"] == 1
    selected = _write(tmp_path, [_event(duration_ms=10)], "events.1.jsonl")
    _write(tmp_path, [_event(duration_ms=999)], "events.jsonl")
    result = summary.summarize([selected, selected])
    assert _one(result)["duration_ms"]["samples"] == 2
    assert _one(result)["duration_ms"]["max"] == 10


@pytest.mark.parametrize(("name", "value"), [
    ("max_bytes", 0), ("max_bytes", True), ("max_bytes", 1.5),
    ("max_bytes", summary.MAX_INPUT_BYTES + 1), ("max_records", -1),
    ("max_records", summary.MAX_INPUT_RECORDS + 1),
])
def test_invalid_budgets_rejected_before_any_read(tmp_path, name, value):
    with pytest.raises(ValueError, match=name):
        summary.summarize([tmp_path / "absent"], **{name: value})


def test_path_count_is_bounded():
    for paths in ([], ["absent"] * (summary.MAX_INPUT_FILES + 1)):
        with pytest.raises(ValueError, match="explicit file paths"):
            summary.summarize(paths)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_fifo_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    result = summary.summarize([path])
    assert result["input"]["errors"] == [{"file_index": 0, "kind": "not_regular_file"}]
    assert result["input"]["complete"] is False


def test_file_shrink_reports_partial_snapshot_and_append_is_excluded(tmp_path, monkeypatch):
    path = _write(tmp_path, [_event(duration_ms=1), _event(duration_ms=2)])
    original_fdopen = summary.os.fdopen

    def shrink(fd, *args, **kwargs):
        path.write_bytes(_line(_event(duration_ms=1)))
        return original_fdopen(fd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(summary.os, "fdopen", shrink)
        result = summary.summarize([path])
    assert result["input"]["errors"] == [{"file_index": 0, "kind": "file_shrank"}]
    assert result["input"]["complete"] is False
    assert result["input"]["accepted_records"] == 1
    assert result["input"]["lines_read"] == 1

    def append(fd, *args, **kwargs):
        with path.open("ab") as stream:
            stream.write(_line(_event(duration_ms=999)))
        return original_fdopen(fd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(summary.os, "fdopen", append)
        result = summary.summarize([path])
    assert result["input"]["complete"] is True
    assert _one(result)["duration_ms"]["max"] == 1


def test_cli_json_from_another_working_directory_and_exit_codes(tmp_path):
    path = _write(tmp_path, [_event(duration_ms=12), _event(duration_ms=24)])
    script = Path(summary.__file__).resolve()

    def run(*args):
        return subprocess.run([sys.executable, str(script), "--json", *args],
                              cwd=tmp_path, capture_output=True, text=True, timeout=10)

    process = run(str(path))
    assert process.returncode == 0 and not process.stderr
    assert json.loads(process.stdout) == summary.summarize([path])
    process = run("--max-records", "1", str(path))
    assert process.returncode == 1 and not process.stderr
    assert json.loads(process.stdout)["input"]["stopped_by"] == "max_records"
    process = run(str(tmp_path / "secret-path-missing"))
    assert process.returncode == 1
    assert "secret-path" not in process.stdout + process.stderr
    assert json.loads(process.stdout)["input"]["errors"]
    process = run("--max-bytes", "invalid-secret-number", str(path))
    assert process.returncode == 2
    assert "invalid-secret-number" not in process.stdout + process.stderr
    assert run().returncode == 2


def test_module_import_does_not_load_runtime_or_network_dependencies(tmp_path):
    root = Path(summary.__file__).resolve().parents[1]
    code = (f"import sys; sys.path.insert(0, {str(root)!r}); "
            "from devtools import summarize_gui_diagnostics; "
            "assert 'agentbridge.gui.app' not in sys.modules; "
            "assert 'agentbridge.gui.context' not in sys.modules; "
            "assert 'supabase' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=10)


@pytest.mark.parametrize("arguments", [
    ["--json=/private/SECRET/events.jsonl"],
    ["--SECRET", "/private/SECRET/events.jsonl"],
    ["--max-records", "/private/SECRET/events.jsonl"],
    ["/private/SECRET/events.jsonl", "--json", "--max-records", "1", "--SECRET"],
    ["--max-bytes=SECRET"],
    ["--max-records", "0", "/private/SECRET/events.jsonl"],
    ["--json", "/private/SECRET/events.jsonl", "--max-records"],
    ["--json", *["/private/SECRET/events.jsonl"] * (summary.MAX_INPUT_FILES + 1)],
    [],
])
def test_all_cli_parser_errors_redact_arguments_and_invocation_path(
        arguments, capsys, monkeypatch):
    def unexpected_read(*_args, **_kwargs):
        pytest.fail("invalid arguments must be rejected before any input is read")

    monkeypatch.setattr(summary, "summarize", unexpected_read)
    monkeypatch.setattr(sys, "argv", ["/private/SECRET/summary.py"])
    with pytest.raises(SystemExit) as failure:
        summary.main(arguments)
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert "SECRET" not in output.err and "/private" not in output.err
    assert "invalid arguments; use --help" in output.err


def test_options_can_be_interspersed_with_explicit_paths(tmp_path, capsys):
    first = _write(tmp_path, [_event(duration_ms=1)], name="first.jsonl")
    second = _write(tmp_path, [_event(duration_ms=2)], name="SECRET-second.jsonl")
    assert summary.main(["--json", str(first), "--max-records", "4", str(second)]) == 0
    output = capsys.readouterr()
    assert not output.err and "SECRET" not in output.out
    result = json.loads(output.out)
    assert result["input"]["accepted_records"] == 2
    assert result["input"]["files_completed"] == 2
