#!/usr/bin/env python3
"""Measure AgentBridge GUI transport counters without touching room data.

Examples:
  python -m agentbridge.benchmarks.realtime --duration 50
  python -m agentbridge.benchmarks.realtime --duration 20 --active

The localhost reads are mirror-only. ``--active`` grants the same expiring
foreground lease as a focused app window and always releases it in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

COUNTERS = (
    "queries", "rx_bytes", "blob_bytes", "rt_open_attempts", "rt_ready",
    "rt_disconnects", "rt_socket_closes", "broadcast_sent",
    "broadcast_failures", "broadcast_skipped",
)


def request_json(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def snapshot(base: str, *, now=time.time) -> dict:
    state = request_json(base, "/api/state")
    mirror = (state.get("connection") or {}).get("mirror") or {}
    transfer = mirror.get("transfer") or {}
    return {
        "at": now(), "instance_id": state.get("instance_id", ""),
        "version": state.get("gui_version", ""),
        "state": mirror.get("state", ""),
        "interactive": bool(mirror.get("interactive")),
        "refresh_s": mirror.get("refresh_s"),
        "realtime": mirror.get("realtime", ""),
        "rt_active": int(transfer.get("rt_active", 0)),
        "rt_active_peak": int(transfer.get("rt_active_peak", 0)),
        **{name: int(transfer.get(name, 0)) for name in COUNTERS},
    }


def report(start: dict, end: dict) -> dict:
    if not start.get("instance_id") or start["instance_id"] != end.get("instance_id"):
        raise ValueError("GUI process changed during the sample")
    elapsed = max(0.001, float(end["at"]) - float(start["at"]))
    delta = {name: int(end[name]) - int(start[name]) for name in COUNTERS}
    return {
        "elapsed_s": round(elapsed, 3), "start": start, "end": end,
        "delta": delta,
        "hourly_extrapolation": {
            "queries": round(delta["queries"] * 3600 / elapsed, 2),
            "rx_bytes": round(delta["rx_bytes"] * 3600 / elapsed, 2),
            "blob_bytes": round(delta["blob_bytes"] * 3600 / elapsed, 2),
        },
        "caveats": [
            "Short-sample hourly values are extrapolations, not p95 gates.",
            "Counters include concurrent work in this GUI process.",
            "Hosted agent processes require separate process-local samples.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7787")
    parser.add_argument("--duration", type=float, default=50.0)
    parser.add_argument("--active", action="store_true")
    args = parser.parse_args()
    if not 1.0 <= args.duration <= 3600.0:
        parser.error("--duration must be between 1 and 3600 seconds")
    try:
        if args.active:
            request_json(args.base, "/api/mesh/activity", {"active": True})
        start = snapshot(args.base)
        time.sleep(args.duration)
        end = snapshot(args.base)
    finally:
        if args.active:
            try:
                request_json(args.base, "/api/mesh/activity", {"active": False})
            except Exception:
                pass
    print(json.dumps(report(start, end), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
