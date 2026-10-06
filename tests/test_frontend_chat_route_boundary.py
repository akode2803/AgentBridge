"""Execute production mandatory acquisition and bounded route boundaries."""
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
def test_public_refresh_always_dispatches_bounded_acquisition(tmp_path: Path):
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    state = (JS / "state.js").read_text(encoding="utf-8")
    assert "V.renderMeshChat = renderMeshChat;" in chat
    _run(tmp_path, _DISPATCH_RUNNER,
         CAPS=_between(state, "export function meshCaps()", "export function isV2()"),
         DISPATCH=_between(chat, "async function renderMeshChat(",
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
                           "// Acquired data only."))


_DISPATCH_RUNNER = r'''
import assert from "node:assert/strict";
const calls = [], App = {state: null}, Mesh = {state: null, chatId: "room"};
const build = new Function("App", "Mesh", "renderPagedChat",
  __CAPS__.replace("export ", "") + __DISPATCH__ + ";return renderMeshChat;");
const render = build(App, Mesh, (...args) => calls.push(args));
for (const caps of [undefined, null, {}, {chat_page_v1:false}, {chat_page_v1:true}]) {
  Mesh.state = {caps}; App.state = {caps:{chat_page_v1:true}};
  await render(true, {started_ms:1}, {data:{messages:[]},paged:false});
  assert.deepEqual(calls.pop(), [true], "display metadata cannot select a retired acquisition route");
}
await render(false, "first");
assert.deepEqual(calls.pop(), [false, "first"], "explicit live-tail acquisition is preserved");
Mesh.chatId = null; await render(true); assert.deepEqual(calls, []);
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
for (const unsupported of [false, undefined, null, "true", 1]) {
  assert.equal(await paint(false, null,
    {paged:unsupported, data:{error:"stale private error"}}), false);
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
  const retired = [], sidebars = [];
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
    retireDeniedMeshChat: chatId => retired.push(chatId),
    renderSidebar: () => sidebars.push("render"),
    beginLoading: () => () => {}, endLoading: () => {},
    diagnostic: () => {}, performance: {now: () => 1},
    setTimeout: (fn, delay) => {timers.push({fn, delay}); return timers.length;},
    clearTimeout: () => {},
    paintMeshChat: prepared => {paints.push(prepared); return true;},
  };
  const build = new Function(...Object.keys(deps), __PAGE_OWNER__ + __RESET__ +
    __PAGED__ + __DISPATCH__ + ";return renderMeshChat;");
  const render = build(...Object.values(deps));
  return {render, pending, calls, paints, timers, events, locks, content, location, retired, sidebars};
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
  if (response?.status === "forbidden") {
    assert.equal(h.location.hash, "#/chats"); assert.equal(h.content.innerHTML, "");
    assert.deepEqual(h.retired, ["room"]); assert.deepEqual(h.sidebars, ["render"]);
  } else { assert.deepEqual(h.retired, []); assert.deepEqual(h.sidebars, []); }
  // A pending retry stays on canonical paging as well.
  if (h.timers.length) {h.timers.shift().fn(); await Promise.resolve(); await Promise.resolve();
    assert(h.calls.every(([path]) => path.startsWith("/api/mesh/chat_page?")));}
}
'''
