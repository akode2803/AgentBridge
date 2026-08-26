#!/usr/bin/env python3
"""Resumable isolated-room provider latency p95 benchmark.

Twenty completed samples are required for a p95 result. Each sample gets a
fresh disposable room, and local state is saved after every completion. A
process interruption abandons that partial timing sample on resume rather than
mixing clock domains.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_STATE = Path.home() / ".agentbridge" / "benchmarks" / "codex-p95.json"


def request_json(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path, *, base: str, agent: str, samples: int) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        expected = (base.rstrip("/"), agent, samples)
        actual = (state.get("base"), state.get("agent"), state.get("target"))
        if state.get("v") != 1 or actual != expected:
            raise ValueError("saved benchmark does not match base/agent/sample target")
        return state
    return {
        "v": 1, "base": base.rstrip("/"), "agent": agent,
        "target": samples, "completed": [], "failures": [],
        "pending": None, "finished": False,
    }


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile needs at least one value")
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


def summary(state: dict) -> dict:
    rows = state.get("completed") or []
    metrics = ("post_return_ms", "first_feed_ms", "reply_ms",
               "claim_to_preparation_ms", "preparation_to_feed_ms",
               "provider_ms")
    out = {"completed": len(rows), "target": state["target"]}
    if len(rows) < 20:
        out["note"] = "p95 requires at least 20 completed samples"
        return out
    out["metrics"] = {}
    for name in metrics:
        values = [row[name] for row in rows if row.get(name) is not None]
        if len(values) != len(rows):
            continue
        out["metrics"][name] = {
            "p50": round(percentile(values, 0.50), 1),
            "p95": round(percentile(values, 0.95), 1),
            "min": round(min(values), 1), "max": round(max(values), 1),
        }
    out["access_seen"] = sum(bool(row.get("access_seen")) for row in rows)
    return out


def trace_segments(base: str, agent: str, message_id: str) -> dict:
    traces = request_json(base, "/api/mesh/latency?limit=1000")
    rows = [row for row in (traces.get("agents") or {}).get(agent, [])
            if row.get("trace_ref") == message_id]
    by_stage = {row.get("stage"): row for row in rows}

    def duration(start: str, end: str) -> float | None:
        left, right = by_stage.get(start), by_stage.get(end)
        if (not left or not right or left.get("clock_id") != right.get("clock_id")
                or not left.get("mono_ns") or not right.get("mono_ns")):
            return None
        return round((right["mono_ns"] - left["mono_ns"]) / 1_000_000, 1)

    return {
        "claim_to_preparation_ms": duration(
            "queue_claimed", "preparation_started"),
        "preparation_to_feed_ms": duration(
            "preparation_started", "feed_started"),
        "provider_ms": duration("provider_started", "provider_finished"),
    }


def cleanup_pending(base: str, state: dict) -> None:
    pending = state.get("pending") or {}
    chat_id = pending.get("chat_id")
    if chat_id:
        try:
            request_json(base, "/api/mesh/delete_chat", {"chat_id": chat_id})
        except Exception:
            pass
    state["pending"] = None


def run_sample(base: str, agent: str, index: int, timeout_s: float,
               state: dict, state_path: Path) -> tuple[dict | None, str | None]:
    expected = f"benchmark-ok-{index}"
    name = f"p95-{agent}-{index:02d}-{secrets.token_hex(3)}"
    room = request_json(base, "/api/mesh/create_chat", {
        "name": name, "members": [agent],
    })["chat"]
    chat_id = room["id"]
    state["pending"] = {"chat_id": chat_id, "name": name, "index": index}
    save_state(state_path, state)
    started = time.perf_counter()
    message_id = ""
    try:
        posted = request_json(base, "/api/mesh/post", {
            "chat_id": chat_id,
            "body": f"@{agent} Reply with exactly: {expected}. Do not use tools.",
        })
        message_id = posted["id"]
        post_return_ms = round((time.perf_counter() - started) * 1000, 1)
        first_feed_ms = None
        access_seen = False
        deadline = time.monotonic() + timeout_s
        reply = None
        while time.monotonic() < deadline:
            now = time.perf_counter()
            feed = request_json(
                base, "/api/mesh/livefeed?id=" + urllib.parse.quote(chat_id)
            ).get("feeds", [])
            if feed and first_feed_ms is None:
                first_feed_ms = round((now - started) * 1000, 1)
            run_ids = [item["run_id"] for item in feed
                       if str(item.get("run_id", "")).startswith("r-")]
            if run_ids:
                authority = request_json(base, "/api/mesh/runtime_authority", {
                    "chat_id": chat_id, "run_ids": run_ids[:20],
                })
                access_seen = access_seen or bool(authority.get("runs"))
            chat = request_json(
                base, "/api/mesh/chat?id=" + urllib.parse.quote(chat_id))
            replies = [message for message in chat.get("messages", [])
                       if message.get("from") == agent]
            reply = next((message for message in reversed(replies)
                          if str(message.get("body", "")).strip() == expected), None)
            if reply is not None:
                break
            time.sleep(0.2)
        if reply is None:
            return None, f"sample {index} timed out after {timeout_s:.0f}s"
        row = {
            "index": index, "message_id": message_id,
            "reply_id": reply.get("id", ""), "post_return_ms": post_return_ms,
            "first_feed_ms": first_feed_ms,
            "reply_ms": round((time.perf_counter() - started) * 1000, 1),
            "access_seen": access_seen,
            **trace_segments(base, agent, message_id),
        }
        return row, None
    finally:
        try:
            request_json(base, "/api/mesh/delete_chat", {"chat_id": chat_id})
        except Exception:
            # Keep the room id durable so the next invocation can clean it.
            save_state(state_path, state)
            raise
        else:
            state["pending"] = None
            save_state(state_path, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7787")
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("--samples must be at least 20 for p95")
    state = load_state(
        args.state, base=args.base, agent=args.agent, samples=args.samples)
    if state.get("pending"):
        cleanup_pending(args.base, state)
        save_state(args.state, state)
    while len(state["completed"]) < args.samples:
        index = len(state["completed"]) + 1
        row, error = run_sample(
            args.base, args.agent, index, args.timeout, state, args.state)
        if error:
            state["failures"].append({"index": index, "error": error})
            save_state(args.state, state)
            print(json.dumps({"error": error, **summary(state)}, indent=2))
            return 2
        state["completed"].append(row)
        save_state(args.state, state)
        print(json.dumps({"latest": row, **summary(state)}, indent=2), flush=True)
    state["finished"] = True
    save_state(args.state, state)
    print(json.dumps(summary(state), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
