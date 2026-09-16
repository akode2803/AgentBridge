"""Execute the production R193 warm-switch orchestration behind barriers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _warm_source() -> str:
    source = (ROOT / "gui" / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    start = source.index("export async function renderWarmChat")
    return source[start : source.index("// the structural signature", start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_warm_switch_executes_fresh_fold_and_rejects_stale_continuations(tmp_path: Path):
    """The exact route function runs against deferred API barriers, not a copy."""
    runner = tmp_path / "warm-orchestration.mjs"
    (tmp_path / "warm-context.mjs").write_text(
        (ROOT / "gui/static/js/warm-context.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "latest-read.mjs").write_text(
        (ROOT / "gui/static/js/latest-read.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runner.write_text(
        _RUNNER % json.dumps(_warm_source()), encoding="utf-8"
    )
    completed = subprocess.run(
        ["node", str(runner)], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_warm_switch_is_registered_as_the_production_route_path():
    source = (ROOT / "gui" / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    assert "export async function renderWarmChat" in source
    assert "await renderWarmChat(force, openTrace, accelerated, {" in source
    assert "fetchSeq, routeSeq, chatId: Mesh.chatId, operationId," in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_accepted_state_removal_clears_an_active_warm_surface(tmp_path: Path):
    """Exercise the central accepted-state event, outside warm hydration itself."""
    source = (ROOT / "gui" / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    start = source.index("let activeWarmSurface = null;")
    end = source.index("const chatOpenObservations", start)
    handler = source[start:end].replace(
        "let activeWarmSurface = null;", "let activeWarmSurface = seed;"
    )
    (tmp_path / "latest-read.mjs").write_text(
        (ROOT / "gui/static/js/latest-read.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runner = tmp_path / "accepted-removal.mjs"
    runner.write_text(_REMOVAL_RUNNER.replace("__SOURCE__", json.dumps(handler)), encoding="utf-8")
    completed = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_state_generation_change_queues_one_cold_restart_only_when_identity_holds(tmp_path: Path):
    """A pending selected fold may fall back once, but never across a newer route."""
    source = (ROOT / "gui" / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    start = source.index("function restartColdAfterStateInvalidation")
    end = source.index("function applyCurrentWarmError", start)
    runner = tmp_path / "cold-restart.mjs"
    runner.write_text(_COLD_RUNNER.replace("__SOURCE__", json.dumps(source[start:end])), encoding="utf-8")
    completed = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_state_removal_between_base_dom_commit_and_renderer_resume_clears_surface(tmp_path: Path):
    """The active surface must exist before awaiting the shared base renderer."""
    chat = (ROOT / "gui" / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    listener_start = chat.index("let activeWarmSurface = null;")
    listener_end = chat.index("const chatOpenObservations", listener_start)
    listener = chat[listener_start:listener_end].replace(
        "let activeWarmSurface = null;", "activeWarmSurface = null;"
    )
    for name in ("warm-context", "latest-read"):
        (tmp_path / f"{name}.mjs").write_text(
            (ROOT / f"gui/static/js/{name}.js").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    runner = tmp_path / "base-render-removal.mjs"
    runner.write_text(
        _MID_RENDER_RUNNER.replace("__LISTENER__", json.dumps(listener)).replace(
            "__WARM__", json.dumps(_warm_source())
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_warm_base_telemetry_records_after_two_frames_and_drops_stale(tmp_path: Path):
    """Run the exact warm function's schedule-only telemetry callbacks."""
    runner = tmp_path / "warm-telemetry.mjs"
    for name in ("warm-context", "latest-read"):
        (tmp_path / f"{name}.mjs").write_text(
            (ROOT / f"gui/static/js/{name}.js").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    runner.write_text(
        _TELEMETRY_RUNNER.replace("__WARM__", json.dumps(_warm_source())),
        encoding="utf-8",
    )
    completed = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr


_RUNNER = r'''
import assert from "node:assert/strict";
import {presentationFromState} from "./warm-context.mjs";
import {createLatestRead} from "./latest-read.mjs";
const source = %s;
const body = source.replace("export async function renderWarmChat", "async function renderWarmChat");
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function make({current = () => true, session = () => true, applyState = () => true} = {}) {
  const calls = [], renders = [], errors = [], cold = [], state = deferred(), chat = deferred();
  const aux = [deferred(), deferred()]; let auxIndex = 0;
  const content = {innerHTML: ""}; const Mesh = {renderedChat: "old", structKey: "old"};
  const api = (path) => {
    calls.push(path);
    if (path.startsWith("/api/mesh/chat?")) return chat.promise;
    if (path === "/api/mesh/state") return state.promise;
    return aux[auxIndex++].promise;
  };
  const factory = new Function("api", "captureSessionEpoch", "captureWarmStateRequest", "sessionMayApply", "applyMeshState",
    "meshStateSnapshot", "renderMeshChat", "requestAnimationFrame", "warmOperationCurrent",
    "applyCurrentWarmError", "restartColdAfterStateInvalidation", "Mesh", "$", "location", "renderSidebar",
    "presentationFromState", "sidebarRead",
    `let chatRenderSeq = 0, activeWarmSurface = null;
     const SIDEBAR_TIMEOUT_MS = 60000;
     const restartColdAfterInitialFailure = () => false;
     const beginInitialSelectedView = () => ({}), endInitialSelectedView = () => true,
       markInitialSelectedViewReady = () => true, startAskPoll = () => {};
     const V = {closeAuthPage() {}, closeConnectingPage() {}};
     ${body}; return renderWarmChat;`);
  const fn = factory(api, () => ({epoch: 1}), () => ({sessionEpoch: 1, lockEpoch: 1}), session, applyState, () => ({stateGeneration: 2}),
    async (_force, trace, options) => { renders.push({trace, options}); },
    callback => callback(), current, (_operation, value) => errors.push(value), () => cold.push("fallback"), Mesh,
    selector => { assert.equal(selector, "#content"); return content; }, {hash: "#/chats/open"}, () => {},
    presentationFromState, createLatestRead(0));
  return {fn, calls, renders, errors, cold, state, chat, aux, content, Mesh};
}
const warm = {chatId: "room", presentation: {user: "aryan"}, sessionEpoch: 1, lockEpoch: 1,
  stateGeneration: 1, routeSeq: 1, operationId: 1, chatRenderSeq: 0};
const route = {chatId: "room", routeSeq: 1, operationId: 1, sessionEpoch: 1, lockEpoch: 1, stateGeneration: 1};
const selected = {me: "aryan", meta: {id: "room", members: ["aryan"]},
  messages: [{text: "fresh"}], presentation: {user: "aryan",
    users: {aryan: {display: "Aryan", display_kind: "human"}}}};

// One selected chat builds the base; hydration reuses its exact payload.
{
  const h = make(); const pending = h.fn(false, null, warm, route);
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room"]);
  h.chat.resolve(selected); for (let i = 0; i < 6; i += 1) await Promise.resolve();
  assert.equal(h.renders.length, 1); assert.equal(h.renders[0].options.data, selected);
  assert.equal(h.renders[0].options.warmBase, true);
  for (let i = 0; i < 20 && !h.calls.includes("/api/mesh/state"); i++)
    await new Promise(resolve => setTimeout(resolve, 0));
  assert(h.calls.includes("/api/mesh/state"));
  assert.equal(h.renders[0].options.presentation.users.aryan.display, "Aryan");
  h.state.resolve({chats: [{id: "room"}]}); h.aux.forEach((item, i) => item.resolve(i ? {tasks: []} : {feeds: []}));
  await pending;
  assert.equal(h.calls.filter(path => path.startsWith("/api/mesh/chat?")).length, 1);
  assert.equal(h.renders.length, 2); assert.equal(h.renders[1].options.data, selected);
  assert.equal(h.renders[1].options.aux.length, 2);
}
// A lock change between base paint and state acceptance rejects the second fold.
{
  const h = make({applyState: () => false}); const pending = h.fn(false, null, warm, route);
  h.chat.resolve(selected); for (let i = 0; i < 6; i += 1) await Promise.resolve();
  assert.equal(h.renders.length, 1);
  h.state.resolve({chats: [{id: "room"}]}); h.aux.forEach((item, i) => item.resolve(i ? {tasks: []} : {feeds: []}));
  await pending; assert.equal(h.renders.length, 1);
}
// Stale route/session continuations cannot render plaintext or invoke error effects.
{
  let live = true; const h = make({current: () => live}); const pending = h.fn(false, null, warm, route);
  live = false; h.chat.resolve({error: "old private text"}); await pending;
  assert.deepEqual(h.renders, []); assert.deepEqual(h.errors, []); assert.deepEqual(h.cold, ["fallback"]);
}
{
  const h = make({session: () => false}); const pending = h.fn(false, null, warm, route);
  h.chat.resolve(selected); await pending; assert.deepEqual(h.renders, []);
}
// A post-paint accepted state omitting the room clears the old display and prevents hydration.
{
  const h = make(); const pending = h.fn(false, null, warm, route); h.chat.resolve(selected);
  for (let i = 0; i < 6; i += 1) await Promise.resolve(); assert.equal(h.renders.length, 1);
  h.state.resolve({chats: []}); h.aux.forEach((item, i) => item.resolve(i ? {tasks: []} : {feeds: []}));
  await pending;
  assert.equal(h.renders.length, 1); assert.equal(h.Mesh.renderedChat, null);
  assert.equal(h.content.innerHTML, '<div class="chat-loading"></div>');
}
'''


_REMOVAL_RUNNER = r'''
import assert from "node:assert/strict";
import {createLatestRead} from "./latest-read.mjs";
const handlers = new Map();
const content = {innerHTML: "old plaintext"};
const App = {page: "chats"};
const Mesh = {chatId: "room", renderedChat: "room"};
const location = {hash: "#/chats/room"};
const seed = {operationId: 1, sessionEpoch: 3, viewer: "aryan", chatId: "room"};
const document = {addEventListener: (name, handler) => handlers.set(name, handler)};
const $ = selector => { assert.equal(selector, "#content"); return content; };
const meshStateSnapshot = () => ({sessionEpoch: 3, viewer: "aryan"});
const CustomEvent = class { constructor(name, init = {}) { this.type = name; this.detail = init.detail; } };
const source = __SOURCE__;
const factory = new Function("document", "meshStateSnapshot", "App", "Mesh", "$", "location", "seed", "CustomEvent", "handlers", "createLatestRead",
  `let chatRenderSeq = 0, chatsFetchSeq = 0, warmOperationSeq = 1, warmOperationsExhausted = false; ${source}; return handlers;`);
const bound = factory(document, meshStateSnapshot, App, Mesh, $, location, seed, CustomEvent, handlers, createLatestRead);
bound.get("ab:mesh-state-accepted")({detail: {state: {user: "aryan", chats: []}}});
assert.equal(content.innerHTML, '<div class="chat-loading"></div>');
assert.equal(Mesh.renderedChat, null);
assert.equal(location.hash, "#/chats");
'''


_COLD_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__;
let state = {stateGeneration: 2}; let identity = true; const renders = [];
const factory = new Function("warmIdentityCurrent", "meshStateSnapshot", "renderChats", "queueMicrotask",
  `let activeWarmSurface = {old: true}; let skipWarmChatId = null; ${source};
   return {restart: restartColdAfterStateInvalidation, skip: () => skipWarmChatId};`);
const owner = factory(() => identity, () => state, force => renders.push(force), queueMicrotask);
const operation = {chatId: "room", stateGeneration: 1};
assert.equal(owner.restart(operation, true), true);
assert.equal(owner.restart(operation, true), false);
await Promise.resolve();
assert.deepEqual(renders, [true]); assert.equal(owner.skip(), "room");
const newerRoute = {chatId: "room", stateGeneration: 1}; identity = false;
assert.equal(owner.restart(newerRoute, false), false);
await Promise.resolve(); assert.deepEqual(renders, [true]);
'''


_MID_RENDER_RUNNER = r'''
import assert from "node:assert/strict";
import {presentationFromState} from "./warm-context.mjs";
import {createLatestRead} from "./latest-read.mjs";
const handlers = new Map(); const document = {addEventListener: (name, handler) => handlers.set(name, handler)};
const content = {innerHTML: "old"}; const App = {page: "chats", routeSeq: 1};
const Mesh = {chatId: "room", renderedChat: "old", structKey: "old"}; const location = {hash: "#/chats/room"};
const $ = () => content; const listener = __LISTENER__; const warmSource = __WARM__;
const warmBody = warmSource.replace("export async function renderWarmChat", "async function renderWarmChat");
const factory = new Function("document", "meshStateSnapshot", "App", "Mesh", "$", "location", "handlers", "createLatestRead", "presentationFromState",
  `let chatRenderSeq = 0, chatsFetchSeq = 0, warmOperationSeq = 1, warmOperationsExhausted = false,
       activeWarmSurface = null; ${listener}
   const api = path => path.startsWith("/api/mesh/chat?")
     ? Promise.resolve({me: "aryan", meta: {id: "room", members: ["aryan"]}, messages: []})
     : new Promise(() => {});
   const captureSessionEpoch = () => ({epoch: 3}); const captureWarmStateRequest = () => ({sessionEpoch: 3, lockEpoch: 1});
   const sessionMayApply = () => true; const applyMeshState = () => true;
   const renderMeshChat = async (_force, _trace, options) => {
     if (options.warmBase) { Mesh.renderedChat = "room"; queueMicrotask(() =>
       handlers.get("ab:mesh-state-accepted")({detail: {state: {user: "aryan", chats: []}}})); }
   };
   const requestAnimationFrame = callback => callback(); const warmOperationCurrent = () => true;
   const applyCurrentWarmError = () => {}; const restartColdAfterStateInvalidation = () => {};
   const restartColdAfterInitialFailure = () => false;
   const beginInitialSelectedView = () => ({}), endInitialSelectedView = () => true,
     markInitialSelectedViewReady = () => true, startAskPoll = () => {};
   const V = {closeAuthPage() {}, closeConnectingPage() {}}; const renderSidebar = () => {};
   ${warmBody}; return renderWarmChat;`);
const snapshot = () => ({sessionEpoch: 3, viewer: "aryan", stateGeneration: 1});
const renderWarmChat = factory(document, snapshot, App, Mesh, $, location, handlers, createLatestRead, presentationFromState);
const warm = {chatId: "room", presentation: {user: "aryan"}, sessionEpoch: 3, lockEpoch: 1,
  stateGeneration: 1, routeSeq: 1, operationId: 1, chatRenderSeq: 0};
renderWarmChat(false, null, warm, {chatId: "room", routeSeq: 1, operationId: 1});
await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
assert.equal(content.innerHTML, '<div class="chat-loading"></div>');
assert.equal(Mesh.renderedChat, null); assert.equal(location.hash, "#/chats");
'''


_TELEMETRY_RUNNER = r'''
import assert from "node:assert/strict";
import {presentationFromState} from "./warm-context.mjs";
import {createLatestRead} from "./latest-read.mjs";
const source = __WARM__.replace("export async function renderWarmChat", "async function renderWarmChat");
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function make() {
  const frames = [], records = [], renders = [], state = deferred(); let live = true;
  const factory = new Function("api", "captureSessionEpoch", "captureWarmStateRequest", "sessionMayApply", "applyMeshState",
    "meshStateSnapshot", "renderMeshChat", "requestAnimationFrame", "warmOperationCurrent", "applyCurrentWarmError",
    "restartColdAfterStateInvalidation", "recordChatOpen", "performance", "Mesh", "$", "location", "renderSidebar",
    "presentationFromState", "sidebarRead",
    `let chatRenderSeq = 0, activeWarmSurface = null;
     const SIDEBAR_TIMEOUT_MS = 60000;
     const restartColdAfterInitialFailure = () => false;
     const beginInitialSelectedView = () => ({}), endInitialSelectedView = () => true,
       markInitialSelectedViewReady = () => true, startAskPoll = () => {};
     const V = {closeAuthPage() {}, closeConnectingPage() {}};
     ${source}; return renderWarmChat;`);
  const fn = factory(path => path.startsWith("/api/mesh/chat?")
      ? Promise.resolve({me: "aryan", meta: {id: "room", members: ["aryan"]}, messages: []}) : state.promise,
    () => ({epoch: 1}), () => ({sessionEpoch: 1, lockEpoch: 1}), () => true, () => true,
    () => ({stateGeneration: 1}), async (_force, _trace, options) => renders.push(options),
    callback => frames.push(callback), () => live, () => {}, () => {}, record => records.push(record),
    {now: () => 42}, {structKey: ""}, () => ({innerHTML: ""}), {hash: "#/chats/room"}, () => {},
    presentationFromState, createLatestRead(0));
  return {fn, frames, records, renders, state, stale: () => { live = false; }};
}
const warm = {chatId: "room", presentation: {user: "aryan"}, sessionEpoch: 1, lockEpoch: 1,
  stateGeneration: 1, routeSeq: 1, operationId: 1, chatRenderSeq: 0};
const route = {chatId: "room", routeSeq: 1, operationId: 1};
// Both telemetry frames execute schedule-only: one record, one selected render/request.
{
  const h = make(); h.fn(false, {v: 1, started_ms: 10, secret: "not-content"}, warm, route);
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  while (h.frames.length) h.frames.shift()();
  assert.equal(h.records.length, 1); assert.equal(h.records[0].mode, "warm");
  assert.equal(h.records[0].sidebar_fetch_ms, 0); assert.equal(h.records[0].secret, "not-content");
  assert(!("started_ms" in h.records[0])); assert.equal(h.renders.length, 1);
}
// Staleness before the inner frame produces no observation or extra render.
{
  const h = make(); h.fn(false, {v: 1, started_ms: 10}, warm, route);
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  h.frames.shift()(); h.stale(); while (h.frames.length) h.frames.shift()();
  assert.deepEqual(h.records, []); assert.equal(h.renders.length, 1);
}
'''
