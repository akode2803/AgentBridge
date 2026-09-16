"""Executable contracts for the production latest-only read coordinator."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_latest_read_serializes_and_keeps_only_latest_pending(tmp_path: Path):
    (tmp_path / "latest-read.mjs").write_text(
        (ROOT / "gui/static/js/latest-read.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runner = tmp_path / "runner.mjs"
    runner.write_text(_RUNNER, encoding="utf-8")
    done = subprocess.run(["node", str(runner)], text=True, capture_output=True)
    assert done.returncode == 0, done.stdout + done.stderr


_RUNNER = r'''
import assert from "node:assert/strict";
import {createLatestRead} from "./latest-read.mjs";

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

// A is active; B is replaced without starting; C is the sole successor.
{
  const gateA = deferred(), calls = [];
  const owner = createLatestRead(0);
  const a = owner.request(async () => { calls.push("A"); return gateA.promise; }, () => true);
  await tick();
  const b = owner.request(async () => { calls.push("B"); return "b"; }, () => true);
  const c = owner.request(async () => { calls.push("C"); return "c"; }, () => true);
  assert.equal(await b, null);
  assert.deepEqual(calls, ["A"]);
  gateA.resolve("a");
  assert.equal(await a, "a");
  assert.equal(await c, "c");
  assert.deepEqual(calls, ["A", "C"]);
}

// Cancellation resolves queued work and a changed session/lock predicate
// prevents the queued reader from being invoked after the active slot clears.
{
  const gate = deferred(), calls = [];
  const owner = createLatestRead(0);
  const active = owner.request(async () => { calls.push("active"); return gate.promise; }, () => true);
  await tick();
  let current = true;
  const queued = owner.request(async () => { calls.push("stale"); return "bad"; }, () => current);
  current = false;
  owner.cancel();
  assert.equal(await queued, null);
  gate.resolve("kept-by-caller-guard");
  assert.equal(await active, "kept-by-caller-guard");
  await tick();
  assert.deepEqual(calls, ["active"]);
}

// An exception is translated to null, releases the slot, and permits the
// latest pending request to run. No rejection or poisoned active flag remains.
{
  const gate = deferred(), calls = [];
  const owner = createLatestRead(0);
  const failed = owner.request(async () => {
    calls.push("throw"); await gate.promise; throw new Error("boom");
  }, () => true);
  await tick();
  const next = owner.request(async () => { calls.push("next"); return 7; }, () => true);
  gate.resolve();
  assert.equal(await failed, null);
  assert.equal(await next, 7);
  assert.deepEqual(calls, ["throw", "next"]);
}
'''
