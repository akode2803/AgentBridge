"""Summarize explicitly selected, local GUI diagnostic JSONL files offline.

Usage: python devtools/summarize_gui_diagnostics.py --json events.2.jsonl events.jsonl

The allowlists and line limit come from agentbridge.gui.diagnostics. Unknown
fields, identifiers, timestamps, paths and exception text are never returned.
Only regular files are read; no discovery, network access or application startup
is performed. Each file is read up to its size at opening, in argument order.
Repeated/overlapping files are counted as supplied, without inferred deduplication.

Budgets apply across all files, including malformed and oversized records. A
record budget counts physical lines, including blank ones. Byte budgets count
bytes delivered by the buffered reader (which may prefetch one small buffer).
Exact nearest-rank percentiles retain at most max_records duration samples.

Durations are observations, not additive stages: browser page_paint includes
page_read, which can include multiple client_request calls; server_request can
include page_stage work. Server ingestion timestamps cannot align browser clocks,
and this summary does not pair the recorder's opaque request references. Never
subtract or sum these series.
Missing page_stage durations are expected for untimed prepare/finalize events.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
import sys
from typing import Sequence

# Permit direct invocation from any working directory, as well as python -m.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge.gui.diagnostics import (  # noqa: E402
    ERROR_TYPES, EVENTS, MAX_BYTES, MAX_FILES, MAX_LINE_BYTES, MODES,
    OUTCOMES, PHASES, REASONS, ROUTES, STATUSES,
)

MAX_INPUT_BYTES = MAX_BYTES * MAX_FILES
MAX_INPUT_RECORDS = 100_000
MAX_INPUT_FILES = 32
_DIMENSIONS = {"route": ROUTES, "phase": PHASES, "mode": MODES}
_COUNTERS = {
    "status": STATUSES, "reason": REASONS, "outcome": OUTCOMES,
    "error_type": ERROR_TYPES,
}
_RECONCILIATION_NUMBERS = (
    "duration_ms", "capture_claim_ms", "change_check_ms", "stage_open_ms", "collect_ms",
    "stage_write_ms", "seal_ms", "compare_ms", "admit_ms",
    "source_finalize_ms", "cleanup_ms",
)
_RECONCILIATION_INTEGERS = (
    "documents_examined", "documents_selected", "document_bytes",
    "document_batches",
)
_NOTES = [
    "Duration series overlap; do not add or subtract them.",
    "Browser page_paint includes page_read, not just rendering time.",
    "Server page_stage work can be included in server_request durations.",
    "Missing durations are not zero; many page_stage events are untimed.",
    "No cross-clock alignment, request pairing, or file deduplication is inferred.",
    "These best-effort observations do not establish successful visual presentation.",
]


def _enum(value: object, allowed: frozenset[str]) -> str:
    if value is None:
        return "unknown"
    return value if type(value) is str and value in allowed else "other"


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    result = {"samples": len(ordered), "min": None, "p50": None,
              "p95": None, "p99": None, "max": None}
    if ordered:
        result.update(min=ordered[0], max=ordered[-1])
        for percentile in (50, 95, 99):
            index = (percentile * len(ordered) + 99) // 100 - 1
            result[f"p{percentile}"] = ordered[index]
    return result


@dataclass
class _Group:
    events: int = 0
    durations: list[float] = field(default_factory=list)
    missing: int = 0
    invalid: int = 0
    counters: dict[str, Counter] = field(
        default_factory=lambda: {name: Counter() for name in _COUNTERS})

    def observe(self, record: dict) -> None:
        self.events += 1
        for name, allowed in _COUNTERS.items():
            self.counters[name][_enum(record.get(name), allowed)] += 1
        if "duration_ms" not in record:
            self.missing += 1
            return
        duration = record["duration_ms"]
        # Match the recorder's range, excluding bool and non-finite JSON numbers.
        # Check the range before isfinite so arbitrarily large ints cannot overflow.
        if (type(duration) not in (int, float) or not 0 <= duration <= 1e9
                or not math.isfinite(duration)):
            self.invalid += 1
            return
        self.durations.append(float(duration))

    def summary(self, event: str, route: str, phase: str, mode: str) -> dict:
        return {
            "event": event, "route": route, "phase": phase, "mode": mode,
            "events": self.events,
            "duration_ms": {**_distribution(self.durations),
                            "missing": self.missing, "invalid": self.invalid},
            **{f"{name}_counts": dict(sorted(counts.items()))
               for name, counts in self.counters.items()},
        }


@dataclass
class _ReconciliationProfile:
    events: int = 0
    values: dict[str, list[float]] = field(default_factory=lambda: {
        name: [] for name in _RECONCILIATION_NUMBERS + _RECONCILIATION_INTEGERS
    })
    missing: Counter = field(default_factory=Counter)
    invalid: Counter = field(default_factory=Counter)

    def observe(self, record: dict) -> None:
        self.events += 1
        for name in self.values:
            if name not in record:
                self.missing[name] += 1
                continue
            value = record[name]
            valid_type = (type(value) in (int, float)
                          if name in _RECONCILIATION_NUMBERS
                          else type(value) is int)
            if (not valid_type or not 0 <= value <= 1e9
                    or not math.isfinite(value)):
                self.invalid[name] += 1
                continue
            self.values[name].append(float(value))

    def summary(self) -> dict:
        return {
            "events": self.events,
            "metrics": {
                name: {**_distribution(values),
                       "missing": self.missing[name],
                       "invalid": self.invalid[name]}
                for name, values in self.values.items()
            },
        }


def _observe(raw: bytes, groups: dict, profiles: _ReconciliationProfile,
             counts: dict) -> None:
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        counts["malformed_lines"] += 1
        return
    if (type(record) is not dict or type(record.get("event")) is not str
            or record["event"] not in EVENTS
            or type(record.get("origin")) is not str
            or record["origin"] not in {"browser", "server"}):
        counts["ignored_records"] += 1
        return
    origin = record["origin"]
    category = "page_stage" if origin == "server" and record["event"] == "page_stage" else origin
    key = (category, record["event"],
           *(_enum(record.get(name), allowed) for name, allowed in _DIMENSIONS.items()))
    if key not in groups:
        groups[key] = _Group()
    groups[key].observe(record)
    if (origin == "server" and record["event"] == "delivery"
            and record.get("phase") == "source_reconciliation"):
        profiles.observe(record)
    counts["accepted_records"] += 1


def summarize(paths: Sequence[str | Path], *, max_bytes: int = MAX_INPUT_BYTES,
              max_records: int = MAX_INPUT_RECORDS) -> dict:
    """Return an allowlisted summary, with explicit partial-read/error counters.

    input.complete means all selected file snapshots were consumed, not that
    their records were valid, unique, or sufficient to diagnose a UI problem.
    Malformed lines are skipped. A valid final JSON object without a newline is
    accepted and counted as unterminated; budget-cut fragments are never parsed.
    IO failures expose only a file's zero-based argument index and fixed category.
    Budgets can be lowered, but not raised beyond the fixed safety ceilings.
    """
    if not 1 <= len(paths) <= MAX_INPUT_FILES:
        raise ValueError(f"supply between 1 and {MAX_INPUT_FILES} explicit file paths")
    for name, value, ceiling in (("max_bytes", max_bytes, MAX_INPUT_BYTES),
                                 ("max_records", max_records, MAX_INPUT_RECORDS)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer between 1 and {ceiling}")

    counts = dict.fromkeys(("files_opened", "files_completed", "bytes_read", "lines_read",
                           "accepted_records", "malformed_lines", "oversized_lines",
                           "unterminated_lines", "budget_cut_lines", "ignored_records"), 0)
    errors = []
    stopped_by = None
    groups = {}
    profiles = _ReconciliationProfile()
    for index, path in enumerate(paths):
        if counts["bytes_read"] == max_bytes or counts["lines_read"] == max_records:
            stopped_by = "max_bytes" if counts["bytes_read"] == max_bytes else "max_records"
            break
        fd = None
        try:
            # O_NONBLOCK prevents a named pipe from blocking before fstat rejects it.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                errors.append({"file_index": index, "kind": "not_regular_file"})
                continue
            with os.fdopen(fd, "rb") as stream:
                fd = None
                counts["files_opened"] += 1
                remaining = info.st_size
                while remaining:
                    if counts["bytes_read"] == max_bytes or counts["lines_read"] == max_records:
                        stopped_by = ("max_bytes" if counts["bytes_read"] == max_bytes
                                      else "max_records")
                        break
                    line = b""
                    line_bytes = 0
                    terminated = False
                    while remaining and counts["bytes_read"] < max_bytes:
                        chunk = stream.readline(min(MAX_LINE_BYTES + 1, remaining,
                                                    max_bytes - counts["bytes_read"]))
                        if not chunk:
                            # A concurrent rotation/truncation cannot look like a clean EOF.
                            errors.append({"file_index": index, "kind": "file_shrank"})
                            break
                        counts["bytes_read"] += len(chunk)
                        remaining -= len(chunk)
                        if not line_bytes:
                            counts["lines_read"] += 1
                        line_bytes += len(chunk)
                        if line_bytes <= MAX_LINE_BYTES:
                            line += chunk
                        terminated = chunk.endswith(b"\n")
                        if terminated:
                            break
                    if line_bytes > MAX_LINE_BYTES:
                        counts["oversized_lines"] += 1
                    if not terminated and remaining:
                        if counts["bytes_read"] == max_bytes:
                            counts["budget_cut_lines"] += 1
                            stopped_by = "max_bytes"
                        break
                    if not terminated:
                        counts["unterminated_lines"] += 1
                    if line_bytes <= MAX_LINE_BYTES:
                        _observe(line, groups, profiles, counts)
                if not remaining:
                    counts["files_completed"] += 1
        except (OSError, ValueError, TypeError):
            errors.append({"file_index": index, "kind": "unreadable_file"})
        finally:
            if fd is not None:
                os.close(fd)
        if stopped_by is not None:
            break

    series = {"browser": [], "server": [], "page_stage": []}
    for (category, *dimensions), group in sorted(groups.items()):
        series[category].append(group.summary(*dimensions))
    return {
        "schema_version": 2,
        "input": {"files_requested": len(paths), **counts, "errors": errors,
                  "stopped_by": stopped_by,
                  "complete": stopped_by is None and not errors,
                  "max_bytes": max_bytes, "max_records": max_records},
        "percentile_method": "nearest_rank",
        "notes": list(_NOTES),
        "series": series,
        "source_reconciliation": profiles.summary(),
    }


def _bounded_integer(ceiling: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = 0
        if not 1 <= number <= ceiling:
            raise argparse.ArgumentTypeError(f"must be between 1 and {ceiling}")
        return number
    return parse


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse's default errors can echo unknown options, file paths and
        # unexpected values. Neither its raw error nor argv belongs in output.
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments; use --help for supported options\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _PrivateArgumentParser(prog="summarize_gui_diagnostics", description=__doc__,
                                    allow_abbrev=False)
    parser.add_argument("paths", nargs="+", help="explicit JSONL file paths; no discovery")
    parser.add_argument("--json", action="store_true", help="emit only the JSON summary")
    parser.add_argument("--max-bytes", type=_bounded_integer(MAX_INPUT_BYTES),
                        default=MAX_INPUT_BYTES, help="total input byte ceiling")
    parser.add_argument("--max-records", type=_bounded_integer(MAX_INPUT_RECORDS),
                        default=MAX_INPUT_RECORDS, help="total physical-line ceiling")
    args = parser.parse_intermixed_args(argv)
    if len(args.paths) > MAX_INPUT_FILES:
        parser.error(f"at most {MAX_INPUT_FILES} explicit file paths are allowed")
    result = summarize(args.paths, max_bytes=args.max_bytes, max_records=args.max_records)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    else:
        counts = result["input"]
        print(f"Accepted {counts['accepted_records']} records from "
              f"{counts['files_opened']}/{counts['files_requested']} files; "
              f"read {counts['bytes_read']} bytes and {counts['lines_read']} lines.")
        print("Complete input:", counts["complete"], " Stop reason:", counts["stopped_by"])
        print("Input quality:", json.dumps(counts, sort_keys=True))
        for note in result["notes"]:
            print(note)
        print("Percentiles: nearest rank. All durations are milliseconds.")
        for category, rows in result["series"].items():
            print(f"\n{category}:")
            for row in rows:
                print(json.dumps(row, sort_keys=True, allow_nan=False))
    # Keep partial JSON usable, while making IO failures or exhausted budgets visible
    # to scripts. Malformed records are counted but do not prevent a summary.
    return 0 if result["input"]["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
