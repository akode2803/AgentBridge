"""Executable contracts for the bound-session initial selected-chat path."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _run(tmp_path: Path, name: str, source: str) -> None:
    runner = tmp_path / name
    runner.write_text(source, encoding="utf-8")
    done = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


@requires_node
def test_drafts_use_ready_session_viewer_and_never_question_identity(tmp_path: Path):
    source = (ROOT / "gui/static/js/state.js").read_text(encoding="utf-8")
    start = source.index("export function currentDraftViewer()")
    end = source.index("// a details subview", start)
    _run(tmp_path, "drafts.mjs", _DRAFT_RUNNER.replace(
        "__SOURCE__", json.dumps(source[start:end].replace("export ", ""))
    ))


_DRAFT_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__;
let session = {mode: "bound", ready: true, exhausted: false,
  binding: {viewer: "aryan"}};
const BrowserSession = {snapshot: () => session};
const Mesh = {state: {user: "mallory"}, drafts: {}};
const reads = [], writes = [], removed = [];
const localStorage = {
  getItem(key) { reads.push(key); return key === "ab:draft:aryan:room" ? "saved" : ""; },
  setItem(key, value) { writes.push([key, value]); },
  removeItem(key) { removed.push(key); },
};
const factory = new Function("BrowserSession", "Mesh", "localStorage",
  `${source}; return {currentDraftViewer, meshDraft, saveDraft};`);
const api = factory(BrowserSession, Mesh, localStorage);
assert.equal(api.currentDraftViewer(), "aryan");
assert.equal(api.meshDraft("room").body, "saved");
Mesh.drafts.room.body = "new"; api.saveDraft("room");
assert.deepEqual(writes, [["ab:draft:aryan:room", "new"]]);
assert.deepEqual(reads, ["ab:draft:aryan:room"]);
session = {mode: "bound", ready: false, exhausted: false, binding: {viewer: "aryan"}};
Mesh.drafts = {}; assert.equal(api.meshDraft("private").body, "");
Mesh.drafts.private.body = "memory"; api.saveDraft("private");
assert.equal(reads.some(key => key.includes("?")), false);
assert.equal(writes.length, 1);
session = {mode: "legacy", ready: true, exhausted: false};
Mesh.state.user = "legacy"; assert.equal(api.currentDraftViewer(), null);
Mesh.drafts = {}; api.meshDraft("unbound"); api.saveDraft("unbound");
assert.equal(reads.length, 1); assert.equal(writes.length, 1);
'''
