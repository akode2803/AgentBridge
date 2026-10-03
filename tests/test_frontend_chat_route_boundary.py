"""Execute production acquisition dispatch and interrupted warm-route boundaries."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "gui/static/js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _between(source: str, start: str, end: str) -> str:
    first = source.index(start)
    return source[first:source.index(end, first)]


def _run(tmp_path: Path, runner: str, **sources: str) -> None:
    for marker, source in sources.items():
        runner = runner.replace(f"__{marker}__", json.dumps(source))
    path = tmp_path / "route-boundary.mjs"
    path.write_text(runner, encoding="utf-8")
    result = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_node
def test_public_refresh_preserves_capability_precedence_and_truthiness(tmp_path: Path):
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    state = (JS / "state.js").read_text(encoding="utf-8")
    assert "V.renderMeshChat = renderMeshChat;" in chat
    _run(tmp_path, _DISPATCH_RUNNER,
         CAPS=_between(state, "export function meshCaps()", "export function isV2()"),
         DISPATCH=_between(chat, "async function renderMeshChat(",
                           "async function renderLegacyMeshChat("))


@requires_node
def test_warm_capability_changes_retire_current_owner_before_each_paint(tmp_path: Path):
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    (tmp_path / "warm-context.mjs").write_text(
        (JS / "warm-context.js").read_text(encoding="utf-8"), encoding="utf-8",
    )
    _run(tmp_path, _WARM_RUNNER,
         OWNERS=_between(chat, "function warmOperationCurrent(",
                         "function restartColdAfterStateInvalidation("),
         WARM=_between(chat, "export async function renderWarmChat",
                       "// the structural signature"))


@requires_node
def test_legacy_acquisition_cannot_paint_or_fetch_aux_after_paged_transition(tmp_path: Path):
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    _run(tmp_path, _LEGACY_RUNNER,
         SOURCE=_between(chat, "async function renderMeshChat(",
                         "// Acquired data only."))


@requires_node
def test_painter_requires_acquired_data_and_never_loads_legacy_hot_reads(tmp_path: Path):
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    painter = _between(chat, "async function paintMeshChat(", "V.renderMeshChat =")
    for endpoint in ("/api/mesh/chat?", "/api/mesh/livefeed?", "/api/mesh/runtime_tasks?"):
        assert endpoint not in painter
    _run(tmp_path, _PAINTER_RUNNER, PAINTER=painter)


@requires_node
def test_paged_dispatch_errors_never_fall_back_to_legacy_gets(tmp_path: Path):
    """Run the real dispatcher, bounded page owner, and page transport closure."""
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    for name in ("chat-pages.js", "chat-page-read.js"):
        (tmp_path / name).write_text((JS / name).read_text(encoding="utf-8"), encoding="utf-8")
    _run(tmp_path, _PAGED_RUNNER,
         PAGE_OWNER=_between(chat, "const pageRead =", "export function isPagedChatViewReady"),
         RESET=_between(chat, "function abortPagedAux(",
                        'document.addEventListener("ab:session-reset"'),
         PAGED=_between(chat, "async function renderPagedChat(", "let chatRenderSeq ="),
         DISPATCH=_between(chat, "async function renderMeshChat(",
                           "async function renderLegacyMeshChat("))


_DISPATCH_RUNNER = r'''
import assert from "node:assert/strict";
const calls = [], App = {state: null}, Mesh = {state: null, chatId: "room"};
const build = new Function("App", "Mesh", "renderPagedChat", "renderLegacyMeshChat",
  __CAPS__.replace("export ", "") + __DISPATCH__ + ";return renderMeshChat;");
const render = build(App, Mesh, (...args) => calls.push(["paged", ...args]),
  (...args) => calls.push(["legacy", ...args]));
const trace = {started_ms: 1};
for (const [mesh, app, expected] of [
  [null, null, "legacy"],
  [{}, {}, "legacy"],
  [null, {caps: {chat_page_v1: true}}, "paged"],
  [{caps: {}}, {caps: {chat_page_v1: true}}, "legacy"],
  [{caps: {chat_page_v1: false}}, {caps: {chat_page_v1: true}}, "legacy"],
  [{caps: {chat_page_v1: true}}, {caps: {chat_page_v1: false}}, "paged"],
  [{caps: null}, {caps: {chat_page_v1: true}}, "paged"],
  [{caps: {chat_page_v1: 0}}, null, "legacy"],
  [{caps: {chat_page_v1: ""}}, null, "legacy"],
  [{caps: {chat_page_v1: "yes"}}, null, "paged"],
  [{caps: {chat_page_v1: 1}}, null, "paged"],
  [{caps: {chat_page_v1: {}}}, null, "paged"],
]) {
  Mesh.state = mesh; App.state = app;
  // Extra arguments from old/private callers cannot bypass dispatch anymore.
  await render(true, trace, {data: {messages: []}, paged: expected !== "paged"});
  assert.deepEqual(calls.pop(), expected === "paged" ? ["paged", true]
    : ["legacy", true, trace]);
}
Mesh.chatId = null;
await render(true, trace);
assert.deepEqual(calls, []);
'''


_WARM_RUNNER = r'''
import assert from "node:assert/strict";
import {presentationFromState, sameWarmOperation} from "./warm-context.mjs";
const source = __OWNERS__ + __WARM__.replace("export ", "");
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
async function flush() { for (let i = 0; i < 16; i++) await Promise.resolve(); }
const selected = {me: "alice", meta: {id: "room", members: ["alice"]}, messages: [],
  presentation: {user: "alice", users: {alice: {display: "Alice"}}}};
function make({initial = false, paged = false, holdFrames = false} = {}) {
  const chat = deferred(), state = deferred(), aux = [deferred(), deferred()];
  const calls = [], paints = [], reroutes = [], errors = [], cold = [], queued = [], frames = [];
  const App = {page: "chats", routeSeq: 1};
  const Mesh = {chatId: "room", state: {user: "alice", caps: {chat_page_v1: paged}}};
  const snapshot = {sessionEpoch: 1, lockEpoch: 1, stateGeneration: 1};
  const content = {innerHTML: "prior"}, location = {hash: "#/chats/room"};
  let auxIndex = 0, owner = null, ended = 0, sidebars = 0;
  const deps = {
    App, Mesh, meshCaps: () => Mesh.state.caps, meshStateSnapshot: () => snapshot,
    sameWarmOperation, presentationFromState,
    api: path => {
      calls.push(path);
      if (path.startsWith("/api/mesh/chat?")) return chat.promise;
      if (path === "/api/mesh/state") return state.promise;
      assert(!Mesh.state.caps.chat_page_v1, "no new legacy GET once paged is known");
      return aux[auxIndex++].promise;
    },
    captureSessionEpoch: () => ({epoch: 1}), captureWarmStateRequest: () => ({}),
    sessionMayApply: () => true,
    applyMeshState: (_ticket, fresh) => {
      snapshot.stateGeneration++;
      Mesh.state = {...Mesh.state, ...fresh};
      return true;
    },
    sidebarRead: {request: (read, current) => current() ? read() : Promise.resolve(null)},
    requestAnimationFrame: fn => holdFrames ? frames.push(fn) : fn(),
    queueMicrotask: fn => queued.push(fn),
    renderMeshChat: force => reroutes.push({force, chatId: Mesh.chatId}),
    restartColdAfterInitialFailure: operation => { if (!operation.initial) return false;
      cold.push("initial"); return true; },
    restartColdAfterStateInvalidation: () => cold.push("state"),
    applyCurrentWarmError: (_operation, error) => errors.push(error),
    beginInitialSelectedView: () => (owner = {}),
    endInitialSelectedView: token => { if (token && token === owner) { owner = null; ended++; } },
    markInitialSelectedViewReady: () => {}, startAskPoll: () => {},
    V: {closeAuthPage() {}, closeConnectingPage() {}},
    renderSidebar: () => { sidebars++; }, $: () => content, location,
  };
  const build = new Function(...Object.keys(deps), "paints", `
    let chatRenderSeq = 0, chatsFetchSeq = 1, warmOperationSeq = 1, activeWarmSurface = null;
    const SIDEBAR_TIMEOUT_MS = 1000, INITIAL_SELECTED_TIMEOUT_MS = 1000;
    async function paintMeshChat(force, trace, prepared) {
      ++chatRenderSeq;
      if (!prepared.guard()) return false;
      paints.push(prepared); Mesh.renderedChat = "room"; return true;
    }
    ${source}
    return {renderWarmChat, redirectWarmToPaged, warmOperationCurrent,
      active: () => activeWarmSurface,
      operation: () => ({sessionEpoch: snapshot().sessionEpoch, lockEpoch: snapshot().lockEpoch,
        stateGeneration: snapshot().stateGeneration, routeSeq: App.routeSeq,
        chatId: Mesh.chatId, operationId: warmOperationSeq, chatRenderSeq, fetchSeq: chatsFetchSeq}),
      staleRender: () => { chatRenderSeq++; }, staleFetch: () => { chatsFetchSeq++; },
      staleOperation: () => { warmOperationSeq++; }};
    function snapshot() { return meshStateSnapshot(); }
  `);
  const bound = build(...Object.values(deps), paints);
  const warm = {initial, chatId: "room", presentation: {user: "alice"},
    sessionEpoch: 1, lockEpoch: 1, stateGeneration: 1};
  const route = {chatId: "room", routeSeq: 1, fetchSeq: 1, operationId: 1};
  return {...bound, App, Mesh, snapshot, chat, state, aux, paints, calls,
    errors, cold, reroutes, queued, frames, location, content,
    start: () => bound.renderWarmChat(true, null, warm, route),
    values: () => ({owner, ended, sidebars}),
    paged: () => { Mesh.state.caps = {chat_page_v1: true}; },
    runQueued: () => { while (queued.length) queued.shift()(); },
    resolveAux: () => aux.forEach((item, i) => item.resolve(i ? {tasks: []} : {feeds: []})),
  };
}
// Once mode is known before entry there is no legacy acquisition at all.
{
  const h = make({paged: true}); await h.start();
  assert.deepEqual(h.calls, []); assert.deepEqual(h.paints, []);
  assert.equal(h.queued.length, 1); h.runQueued();
  assert.deepEqual(h.reroutes, [{force: true, chatId: "room"}]);
}
// A current selected response retires before the base paint, even for errors.
for (const initial of [false, true]) for (const response of [selected, {error: "No such chat"}]) {
  const h = make({initial}); const done = h.start();
  h.paged(); h.chat.resolve(response); await done;
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room"]);
  assert.deepEqual(h.paints, []); assert.deepEqual(h.errors, []); assert.deepEqual(h.cold, []);
  assert.equal(h.values().owner, null);
  if (initial) assert.equal(h.values().ended, 1);
  h.runQueued(); assert.equal(h.reroutes.length, 1);
}
// Rejection from the retired legacy read must not trigger a cold fallback.
for (const initial of [false, true]) {
  const h = make({initial}); const done = h.start();
  h.paged(); h.chat.reject(new Error("old transport failed")); await done;
  assert.deepEqual(h.paints, []); assert.deepEqual(h.cold, []);
  assert.equal(h.values().owner, null);
  h.runQueued(); assert.equal(h.reroutes.length, 1);
}
// A capability change during the frame boundary cannot launch legacy aux GETs.
{
  const h = make({holdFrames: true}); const done = h.start();
  h.chat.resolve(selected); await flush();
  assert.equal(h.paints.length, 1); assert.equal(h.frames.length, 1);
  h.paged(); h.frames.shift()(); await done;
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room"]);
  assert.equal(h.paints.length, 1); h.runQueued(); assert.equal(h.reroutes.length, 1);
}
// A separate accepted update can enable paging while the sidebar request waits.
// The retired request's error payload must not clear/reroute the current surface.
for (const response of [{error: "No such chat"}, {error: "App is locked", locked: true}, null]) {
  const h = make(); const done = h.start(); h.chat.resolve(selected); await flush();
  h.paged(); h.state.resolve(response); await done;
  assert.deepEqual(h.errors, []); assert.deepEqual(h.cold, []);
  assert.equal(h.paints.length, 1); assert.equal(h.location.hash, "#/chats/room");
  h.runQueued(); assert.equal(h.reroutes.length, 1); h.resolveAux(); await flush();
}
// The just-accepted sidebar can enable paging while legacy aux is still pending.
for (const initial of [false, true]) {
  const h = make({initial}); const done = h.start(); h.chat.resolve(selected); await flush();
  assert.equal(h.paints.length, 1); assert(h.calls.includes("/api/mesh/state"));
  h.state.resolve({chats: [{id: "room"}], caps: {chat_page_v1: true}}); await done;
  assert.equal(h.paints.length, 1); assert.equal(h.active(), null);
  assert.deepEqual(h.cold, []); h.runQueued(); assert.equal(h.reroutes.length, 1);
  h.resolveAux(); await flush(); assert.equal(h.paints.length, 1);
}
// Capability can change after accepted state, during the auxiliary wait.
for (const initial of [false, true]) {
  const h = make({initial}); const done = h.start(); h.chat.resolve(selected); await flush();
  h.state.resolve({chats: [{id: "room"}], caps: {chat_page_v1: false}}); await flush();
  assert.equal(h.values().sidebars, 1); assert.equal(h.paints.length, 1);
  h.paged(); h.resolveAux(); await done;
  assert.equal(h.paints.length, 1); assert.equal(h.active(), null);
  assert.deepEqual(h.cold, []); h.runQueued(); assert.equal(h.reroutes.length, 1);
}
// Every identity dimension must suppress reroute from an old continuation.
const supersede = [
  h => { h.App.routeSeq++; }, h => { h.App.page = "settings"; },
  h => { h.Mesh.chatId = "new-room"; }, h => { h.snapshot.sessionEpoch++; },
  h => { h.snapshot.lockEpoch++; }, h => h.staleRender(),
  h => h.staleFetch(), h => h.staleOperation(),
];
for (const stale of supersede) {
  const h = make(); const done = h.start(); stale(h); h.paged(); h.chat.resolve(selected);
  await done; h.runQueued();
  assert.deepEqual(h.paints, []); assert.deepEqual(h.reroutes, []);
  assert.deepEqual(h.errors, []); assert.deepEqual(h.cold, []);
}
// The microtask must recheck ownership, not just the original response guard.
for (const stale of supersede) {
  const h = make(); const done = h.start(); h.paged(); h.chat.resolve(selected); await done;
  assert.equal(h.queued.length, 1); stale(h); h.runQueued();
  assert.deepEqual(h.reroutes, []);
}
// State-generation advancement alone retains identity, and only one redirect is queued.
{
  const h = make(); const operation = h.operation(); h.paged(); h.snapshot.stateGeneration++;
  assert.equal(h.redirectWarmToPaged(operation, true), true);
  assert.equal(h.redirectWarmToPaged(operation, true), true);
  assert.equal(operation.modeRestarted, true);
  assert.equal(h.warmOperationCurrent(operation), false);
  assert.equal(h.queued.length, 1); h.runQueued(); assert.equal(h.reroutes.length, 1);
}
// Any newer route/session/lock/render/fetch owner while aux waits suppresses reroute.
for (const stale of supersede) {
  const h = make(); const done = h.start(); h.chat.resolve(selected); await flush();
  h.state.resolve({chats: [{id: "room"}]}); await flush();
  stale(h); h.paged(); h.resolveAux(); await done;
  h.runQueued(); assert.equal(h.paints.length, 1); assert.deepEqual(h.reroutes, []);
}
'''


_LEGACY_RUNNER = r'''
import assert from "node:assert/strict";
function deferred() { let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject}; }
async function flush() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
const selected = {me: "alice", meta: {id: "room", members: ["alice"]}, messages: []};
function make() {
  const chat = deferred(), aux = [deferred(), deferred()], calls = [], paints = [], paged = [];
  const App = {page: "chats", routeSeq: 1};
  const Mesh = {chatId: "room", state: {user: "alice", caps: {chat_page_v1: false}}};
  let session = 1, lock = 1, index = 0;
  const deps = {App, Mesh, meshCaps: () => Mesh.state.caps,
    captureSessionEpoch: () => session, sessionMayApply: ticket => ticket === session,
    meshStateSnapshot: () => ({lockEpoch: lock}), performance: {now: () => 7},
    api: path => { calls.push(path); return path.startsWith("/api/mesh/chat?")
      ? chat.promise : aux[index++].promise; },
    renderPagedChat: force => { paged.push(force); },
    paintMeshChat: async (_force, _trace, prepared) => { paints.push(prepared); return true; },
  };
  const build = new Function(...Object.keys(deps), `let chatRenderSeq = 0;
    ${__SOURCE__}; return {renderMeshChat, staleRender: () => {chatRenderSeq++;}};`);
  const bound = build(...Object.values(deps));
  return {...bound, App, Mesh, chat, aux, calls, paints, paged,
    start: () => bound.renderMeshChat(true),
    setPaged: () => { Mesh.state.caps.chat_page_v1 = true; },
    staleSession: () => { session++; }, staleLock: () => { lock++; },
    resolveAux: () => aux.forEach((item, i) => item.resolve(i ? {tasks: []} : {feeds: []})),
  };
}
// Old-server acquisition remains functional and passes exact acquired inputs.
{
  const h = make(); const presentation = h.Mesh.state;
  const done = h.start(); h.chat.resolve(selected); await flush();
  h.Mesh.state = {...h.Mesh.state, users: {alice: {display: "Updated"}}};
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room", "/api/mesh/livefeed?id=room",
    "/api/mesh/runtime_tasks?id=room"]);
  h.resolveAux(); await done; assert.equal(h.paints.length, 1);
  assert.equal(h.paints[0].data, selected); assert.equal(h.paints[0].readStarted, 7);
  assert.equal(h.paints[0].presentation, presentation);
  assert.deepEqual(h.paints[0].aux, [{feeds: []}, {tasks: []}]);
  assert.equal(h.paints[0].guard(), true); assert.deepEqual(h.paged, []);
}
// A selected response acquired before capability admission must not acquire aux.
for (const response of [selected, {error: "old failure"}]) {
  const h = make(); const done = h.start(); h.setPaged(); h.chat.resolve(response); await done;
  assert.deepEqual(h.calls, ["/api/mesh/chat?id=room"]);
  assert.deepEqual(h.paints, []); assert.deepEqual(h.paged, [true]);
}
// Rejected old transports follow the same current-owner transition rule.
for (const phase of ["selected", "aux"]) for (const modeChanged of [false, true]) {
  const h = make(); const done = h.start();
  if (phase === "aux") {h.chat.resolve(selected); await flush();}
  if (modeChanged) h.setPaged();
  const failure = new Error("old transport failed");
  const rejected = modeChanged ? null : assert.rejects(done, error => error === failure);
  if (phase === "aux") {h.aux[0].reject(failure); h.aux[1].resolve({tasks: []});}
  else h.chat.reject(failure);
  if (modeChanged) await done; else await rejected;
  assert.deepEqual(h.paints, []); assert.deepEqual(h.paged, modeChanged ? [true] : []);
  assert.equal(h.calls.length, phase === "aux" ? 3 : 1);
}
// Current owners reroute after aux; superseded same-chat route/session/lock
// owners neither paint their data nor reroute somebody else's view.
const supersede = [
  h => { h.App.routeSeq++; }, h => { h.App.page = "settings"; },
  h => { h.Mesh.chatId = "new-room"; }, h => h.staleSession(),
  h => h.staleLock(), h => h.staleRender(),
];
for (const stale of [null, ...supersede]) {
  const h = make(); const done = h.start(); h.chat.resolve(selected); await flush();
  assert.equal(h.calls.length, 3); if (stale) stale(h); h.setPaged(); h.resolveAux(); await done;
  assert.deepEqual(h.paints, []); assert.deepEqual(h.paged, stale ? [] : [true]);
  assert.equal(h.calls.length, 3);
}
for (const stale of supersede) {
  const h = make(); const done = h.start(); stale(h); h.setPaged(); h.chat.resolve(selected);
  await done; assert.deepEqual(h.paints, []); assert.deepEqual(h.paged, []);
  assert.equal(h.calls.length, 1);
}
'''


_PAINTER_RUNNER = r'''
import assert from "node:assert/strict";
let paged = true;
const calls = [], toasts = [], location = {hash: "#/chats/room"};
const deps = {Mesh: {chatId: "room"}, meshCaps: () => ({chat_page_v1: paged}),
  captureSessionEpoch: () => 1, sessionMayApply: () => true,
  api: path => {calls.push(path); throw new Error("painting cannot acquire data");},
  toast: (...args) => toasts.push(args), location,
};
const build = new Function(...Object.keys(deps), `let chatRenderSeq = 0;
  ${__PAINTER__}; return paintMeshChat;`);
const paint = build(...Object.values(deps));
for (const prepared of [undefined, null, {}, {data: null}, {data: false}]) {
  await assert.rejects(paint(false, null, prepared), TypeError);
}
for (const expectedPaged of [false, true]) {
  paged = expectedPaged;
  assert.equal(await paint(false, null,
    {paged: !expectedPaged, data: {error: "stale private error"}}), false);
  assert.equal(location.hash, "#/chats/room"); assert.deepEqual(toasts, []);
}
paged = true;
await paint(false, null, {paged: true, data: {error: "No such chat"}});
assert.equal(location.hash, "#/chats"); assert.deepEqual(calls, []);
'''


_PAGED_RUNNER = r'''
import assert from "node:assert/strict";
import {createChatPageRead, pageRetryDelay} from "./chat-page-read.js";
const binding = {instance_id: "app", session_generation: "7", viewer: "alice"};
function deferred() { let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject}; }
function make() {
  const pending = deferred(), calls = [], paints = [], timers = [], events = [], locks = [];
  const App = {page: "chats", routeSeq: 1};
  const Mesh = {chatId: "room", state: {user: "alice", caps: {chat_page_v1: true}}};
  const content = {innerHTML: "prior", dataset: {}}, retry = {};
  const location = {hash: "#/chats/room"};
  const deps = {App, Mesh, createChatPageRead, pageRetryDelay, location,
    BrowserSession: {snapshot: () => ({binding})},
    meshCaps: () => Mesh.state.caps,
    captureSessionEpoch: () => 1, sessionMayApply: () => true,
    meshStateSnapshot: () => ({lockEpoch: 1}),
    api: (path, body) => {calls.push([path, body]); return pending.promise;},
    $: selector => selector === "#content" ? content : selector === "#page-retry" ? retry : null,
    document: {dispatchEvent: event => events.push(event.type)},
    CustomEvent: class {constructor(type) {this.type = type;}},
    observeLockState: value => locks.push(value),
    beginLoading: () => () => {}, endLoading: () => {},
    diagnostic: () => {}, performance: {now: () => 1},
    setTimeout: (fn, delay) => {timers.push({fn, delay}); return timers.length;},
    clearTimeout: () => {},
    paintMeshChat: prepared => {paints.push(prepared); return true;},
    renderLegacyMeshChat: () => {throw new Error("paged acquisition fell back to legacy");},
  };
  const build = new Function(...Object.keys(deps), __PAGE_OWNER__ + __RESET__ +
    __PAGED__ + __DISPATCH__ + ";return renderMeshChat;");
  const render = build(...Object.values(deps));
  return {render, pending, calls, paints, timers, events, locks, content, location};
}
for (const response of [
  {status: "pending", reason: "local_inputs_pending", session_binding: binding},
  {status: "reset_required", reason: "view_changed", session_binding: binding},
  {status: "unavailable", reason: "incomplete", session_binding: binding},
  {status: "forbidden", session_binding: binding},
  {error: "App is locked", locked: true},
  {error: "transport error"},
  {status: "page", meta: null},
  null,
  new Error("network rejected"),
]) {
  const h = make(); const done = h.render(true);
  assert.equal(h.calls.length, 1); assert.match(h.calls[0][0], /^\/api\/mesh\/chat_page\?/);
  assert.equal(h.calls[0][1], undefined);
  if (response instanceof Error) h.pending.reject(response); else h.pending.resolve(response);
  await done;
  assert.deepEqual(h.paints, []);
  assert(h.calls.every(([path]) => path.startsWith("/api/mesh/chat_page?")));
  if (response?.locked) {assert.deepEqual(h.locks, [true]); assert.deepEqual(h.events, ["ab:locked"]);}
  if (response?.status === "forbidden") assert.equal(h.location.hash, "#/chats");
  // A pending retry stays on canonical paging as well.
  if (h.timers.length) {h.timers.shift().fn(); await Promise.resolve(); await Promise.resolve();
    assert(h.calls.every(([path]) => path.startsWith("/api/mesh/chat_page?")));}
}
'''
