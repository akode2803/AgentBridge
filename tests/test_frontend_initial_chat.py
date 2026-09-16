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
def test_initial_context_requires_exact_bound_unlocked_bootstrap(tmp_path: Path):
    source = (ROOT / "gui/static/js/warm-context.js").read_text(encoding="utf-8")
    module = tmp_path / "warm-context.mjs"
    module.write_text(source, encoding="utf-8")
    _run(tmp_path, "context.mjs", _CONTEXT_RUNNER)


@requires_node
def test_drafts_use_ready_session_viewer_and_never_question_identity(tmp_path: Path):
    source = (ROOT / "gui/static/js/state.js").read_text(encoding="utf-8")
    start = source.index("export function currentDraftViewer()")
    end = source.index("// a details subview", start)
    _run(tmp_path, "drafts.mjs", _DRAFT_RUNNER.replace(
        "__SOURCE__", json.dumps(source[start:end].replace("export ", ""))
    ))


@requires_node
def test_initial_orchestration_paints_then_hydrates_and_falls_back_once(tmp_path: Path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    start = source.index("export async function renderWarmChat")
    end = source.index("// the structural signature", start)
    _run(tmp_path, "initial-orchestration.mjs", _INITIAL_RUNNER.replace(
        "__SOURCE__", json.dumps(source[start:end])
    ))


_CONTEXT_RUNNER = r'''
import assert from "node:assert/strict";
import {initialChatContext} from "./warm-context.mjs";
const binding = {instance_id: "i", viewer: "aryan", session_generation: "7"};
const session = {mode: "bound", ready: true, exhausted: false, binding};
const bootstrap = {configured: true, restoring: false, user: "aryan",
  app_lock: {locked: false}, session_binding: {...binding}};
const snapshot = {sessionEpoch: 1, lockEpoch: 2, stateGeneration: 3,
  locked: false, exhausted: false};
const args = {state: null, snapshot, chatId: "room", opening: true,
  details: false, restarting: false};
const accepted = initialChatContext(session, bootstrap, args);
assert.equal(accepted.initial, true); assert.equal(accepted.presentation.user, "aryan");
assert.equal(Object.getPrototypeOf(accepted.presentation.users), null);
for (const changed of [
  [{...session, mode: "legacy"}, bootstrap, args],
  [{...session, ready: false}, bootstrap, args],
  [{...session, exhausted: true}, bootstrap, args],
  [session, {...bootstrap, restoring: true}, args],
  [session, {...bootstrap, app_lock: {locked: true}}, args],
  [session, {...bootstrap, session_binding: {...binding, session_generation: "8"}}, args],
  [session, bootstrap, {...args, snapshot: {...snapshot, locked: true}}],
  [session, bootstrap, {...args, snapshot: {...snapshot, exhausted: true}}],
  [session, bootstrap, {...args, state: {user: "aryan"}}],
  [session, bootstrap, {...args, details: true}],
  [session, bootstrap, {...args, restarting: true}],
]) assert.equal(initialChatContext(...changed), null);
'''


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
Mesh.state.user = "legacy"; assert.equal(api.currentDraftViewer(), "legacy");
'''


_INITIAL_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__.replace("export async function renderWarmChat",
  "async function renderWarmChat");
function deferred() { let resolve, reject; const promise = new Promise((a, b) =>
  {resolve = a; reject = b;}); return {promise, resolve, reject}; }
function make({throwBase = false} = {}) {
  const chat = deferred(), state = deferred(), frames = [], calls = [], renders = [];
  let pending = false, ready = false, asks = 0, fallback = 0, owner = null;
  const api = path => {
    calls.push(path);
    if (path.startsWith("/api/mesh/chat?")) return chat.promise;
    if (path === "/api/mesh/state") return state.promise;
    return Promise.resolve(null); // null aux must hydrate with empty decorations
  };
  const factory = new Function("api", "captureSessionEpoch", "captureWarmStateRequest",
    "sessionMayApply", "applyMeshState", "meshStateSnapshot", "renderMeshChat",
    "requestAnimationFrame", "warmOperationCurrent", "applyCurrentWarmError",
    "restartColdAfterStateInvalidation", "restartColdAfterInitialFailure",
    "beginInitialSelectedView", "endInitialSelectedView", "markInitialSelectedViewReady",
    "startAskPoll", "V", "Mesh", "$", "location", "renderSidebar",
    `let chatRenderSeq = 0, activeWarmSurface = null;
     const INITIAL_SELECTED_TIMEOUT_MS = 10000;
     ${source}; return renderWarmChat;`);
  const fn = factory(api, () => ({epoch: 1}), () => ({}), () => true, () => true,
    () => ({stateGeneration: 2}), async (_force, _trace, options) => {
      renders.push(options); if (throwBase && options.warmBase) throw new Error("bad payload");
    }, callback => frames.push(callback), () => true, () => {}, () => {},
    operation => { fallback += 1; if (operation.initialViewOwner === owner) pending = false; return true; },
    () => { owner = {}; pending = true; ready = false; return owner; },
    token => { if (token !== owner) return false; pending = false; owner = null; return true; },
    token => { if (token !== owner) return false; ready = true; return true; },
    () => { asks += 1; }, {closeAuthPage() {}, closeConnectingPage() {}},
    {renderedChat: null, structKey: ""}, () => ({innerHTML: ""}),
    {hash: "#/chats/room"}, () => {});
  return {fn, chat, state, frames, calls, renders,
    values: () => ({pending, ready, asks, fallback})};
}
const warm = {initial: true, chatId: "room", presentation: {user: "aryan", users: {}},
  sessionEpoch: 1, lockEpoch: 1, stateGeneration: 1, routeSeq: 1,
  operationId: 1, chatRenderSeq: 0};
const route = {chatId: "room", routeSeq: 1, operationId: 1};
const selected = {me: "aryan", meta: {id: "room", members: ["aryan"]}, messages: []};
{
  const h = make(); const done = h.fn(true, null, warm, route);
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room"]); h.chat.resolve(selected);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  assert.equal(h.renders.length, 1); assert.equal(h.values().pending, true);
  while (h.frames.length) h.frames.shift()();
  for (let i = 0; i < 4; i++) await Promise.resolve();
  assert.equal(h.values().ready, true); assert.equal(h.calls[1], "/api/mesh/state");
  h.state.resolve({chats: [{id: "room"}]}); await done;
  assert.equal(h.calls.filter(p => p.startsWith("/api/mesh/chat?")).length, 1);
  assert.equal(h.renders.length, 2); assert.equal(h.values().asks, 1);
  assert.equal(h.values().pending, false); assert.equal(h.values().fallback, 0);
}
{
  const h = make({throwBase: true}); const done = h.fn(true, null, warm, route);
  h.chat.resolve(selected); await done;
  assert.equal(h.values().fallback, 1); assert.equal(h.values().pending, false);
}
{
  const h = make(); const done = h.fn(true, null, warm, route);
  h.chat.resolve({...selected, messages: null}); await done;
  assert.equal(h.renders.length, 0); assert.equal(h.values().fallback, 1);
  assert.equal(h.values().pending, false);
}
'''
