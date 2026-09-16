"""Executable ownership regressions for global mesh state and modal reads."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "gui/static/js/state.js"
MODAL = ROOT / "gui/static/js/modal.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _run(tmp_path: Path, program: str) -> None:
    runner = tmp_path / "runner.mjs"
    runner.write_text(program, encoding="utf-8")
    done = subprocess.run(
        [shutil.which("node") or "node", str(runner)],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _ownership_source() -> str:
    source = STATE.read_text(encoding="utf-8")
    start = source.index("let meshStateGeneration = 0;")
    end = source.index("// agent reply-rule vocabulary", start)
    return source[start:end].replace("export function ", "function ")


def _function_source(text: str, signature: str) -> str:
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(brace, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


@requires_node
def test_mesh_state_reads_use_latest_accepted_order_and_are_one_shot(tmp_path: Path):
    """A pending newer read does not starve A, but accepted B permanently rejects A."""
    _run(tmp_path, _STATE_RUNNER.replace("__SOURCE__", json.dumps(_ownership_source())))


@requires_node
def test_modal_read_owner_is_invalidated_by_close_swap_and_view_change(tmp_path: Path):
    """Modal-local continuations require both the exact modal nonce and view owner."""
    source = MODAL.read_text(encoding="utf-8")
    start = source.index("let activePhotoClose")
    end = source.index("// WhatsApp-style confirmation dialog", start)
    # Keep only ownership and modal lifecycle functions; imports are supplied by the harness.
    owned = source[start:end].replace("export function ", "function ")
    _run(tmp_path, _MODAL_RUNNER.replace("__SOURCE__", json.dumps(owned)))


def test_modal_consumers_keep_state_local_and_capture_post_open_action_owner():
    """Member and forward pickers must not turn their modal read into global authority."""
    for filename, entry in (("members.js", "showAddMembers"), ("forward.js", "openForwardPicker")):
        source = (ROOT / "gui/static/js" / filename).read_text(encoding="utf-8")
        start = source.index(f"async function {entry}")
        body = source[start:]
        begin = body.index("const ticket = beginModalRead()")
        state = body.index('await api("/api/mesh/state")', begin)
        validate = body.index("modalReadMayApply(ticket, ms)", state)
        opened = body.index("const box = openModal", validate)
        action = body.index("const actionOwner = captureModalRead()", opened)
        callback_guard = body.index("modalReadMayApply(actionOwner)", action)
        assert begin < state < validate < opened < action < callback_guard
        assert "applyMeshState(" not in body[:callback_guard]


@requires_node
def test_old_member_search_and_forward_state_cannot_continue_after_navigation(tmp_path: Path):
    """Permanent form of R198_REPRO: old local reads neither clobber nor open UI."""
    members = (ROOT / "gui/static/js/members.js").read_text(encoding="utf-8")
    forward = (ROOT / "gui/static/js/forward.js").read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _function_source(members, "function userRow"),
            _function_source(members, "function pickerSections"),
            _function_source(members, "async function showAddMembers"),
            _function_source(members, "async function showSearchMembers"),
            _function_source(forward, "async function openForwardPicker"),
        ]
    )
    program = _CONSUMER_RUNNER.replace("__FUNCTIONS__", json.dumps(functions))
    _run(tmp_path, program)


_STATE_RUNNER = r'''
import assert from "node:assert/strict";
const events = [];
globalThis.performance = {now: () => 12};
globalThis.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } };
globalThis.document = {dispatchEvent: event => events.push(event)};
const App = {routeSeq: 1, page: "chats"};
const Mesh = {chatId: "a", detailsView: null, state: null};
let sessionEpoch = 1;
const BrowserSession = {
  capture: () => Object.freeze({epoch: sessionEpoch}),
  snapshot: () => ({mode: "bound", ready: true, binding: {viewer: "aryan", instance_id: "i"}}),
  mayApply: (ticket, response = {}) => ticket?.epoch === sessionEpoch
    && (!Object.hasOwn(response, "instance_id") || response.instance_id === "i"),
  invalidate: () => { sessionEpoch += 1; },
};
const resetSubviews = () => {};
const source = __SOURCE__;
const factory = new Function("App", "Mesh", "BrowserSession", "CustomEvent", "document", "performance", "resetSubviews",
  `${source}; return {captureMeshStateRead, applyMeshState, advanceSelectedView, observeLockState};`);
const api = factory(App, Mesh, BrowserSession, CustomEvent, document, performance, resetSubviews);
const ticket = BrowserSession.capture();
const a = api.captureMeshStateRead(ticket);
const b = api.captureMeshStateRead(ticket);
assert.equal(api.applyMeshState(ticket, {user: "aryan", value: "A"}, a), true);
assert.equal(Mesh.state.value, "A");
assert.equal(api.applyMeshState(ticket, {user: "aryan", value: "B"}, b), true);
assert.equal(api.applyMeshState(ticket, {user: "aryan", value: "A-late"}, a), false);
assert.equal(api.applyMeshState(ticket, {user: "aryan", value: "B-again"}, b), false);
assert.equal(Mesh.state.value, "B");
assert.equal(events.filter(event => event.type === "ab:mesh-state-accepted").length, 2);
const eventCount = events.length;
assert.equal(api.applyMeshState(ticket, {user: "aryan", value: "missing-owner"}), false);
assert.equal(Mesh.state.value, "B"); assert.equal(events.length, eventCount);

const routeOwner = api.captureMeshStateRead(ticket);
App.routeSeq = 2;
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, routeOwner), false);
App.routeSeq = 3;
const pageOwner = api.captureMeshStateRead(ticket);
App.page = "settings";
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, pageOwner), false);
App.page = "chats";
const chatOwner = api.captureMeshStateRead(ticket);
Mesh.chatId = "b";
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, chatOwner), false);
Mesh.chatId = "a";
const detailsOwner = api.captureMeshStateRead(ticket);
Mesh.detailsView = {};
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, detailsOwner), false);
Mesh.detailsView = null;
const selectionOwner = api.captureMeshStateRead(ticket);
api.advanceSelectedView();
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, selectionOwner), false);
const sessionOwner = api.captureMeshStateRead(ticket);
BrowserSession.invalidate();
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, sessionOwner), false);
const lockOwner = api.captureMeshStateRead(ticket);
api.observeLockState(true);
assert.equal(api.applyMeshState(ticket, {user: "aryan"}, lockOwner), false);
assert.equal(api.captureMeshStateRead(ticket), null);
'''


_MODAL_RUNNER = r'''
import assert from "node:assert/strict";
const listeners = new Map(); let view = 1; let modal = null;
const document = {
  addEventListener: (name, handler) => listeners.set(name, handler),
  querySelector: selector => selector === ".modal-scrim" ? modal : null,
  createElement: () => ({className: "", innerHTML: "", addEventListener() {},
    querySelector: () => ({className: "modal-box", innerHTML: ""})}),
  body: {appendChild: value => { value.remove = () => { modal = null; }; modal = value; }},
};
const window = {addEventListener: (name, handler) => listeners.set(name, handler)};
const captureViewRead = () => ({view});
const viewReadMayApply = owner => owner?.view === view;
const esc = value => value; const ICONS = {};
const source = __SOURCE__;
const factory = new Function("document", "window", "captureViewRead", "viewReadMayApply", "esc", "ICONS",
  `${source}; return {captureModalRead, beginModalRead, modalReadMayApply, closeModal, swapModal};`);
const api = factory(document, window, captureViewRead, viewReadMayApply, esc, ICONS);
const first = api.beginModalRead();
assert.equal(api.modalReadMayApply(first), true);
api.closeModal(); assert.equal(api.modalReadMayApply(first), false);
const second = api.captureModalRead();
view = 2; assert.equal(api.modalReadMayApply(second), false);
const third = api.beginModalRead();
api.swapModal("new"); assert.equal(api.modalReadMayApply(third), false);
const fourth = api.captureModalRead();
listeners.get("hashchange")(); assert.equal(api.modalReadMayApply(fourth), false);
'''


_CONSUMER_RUNNER = r'''
import assert from "node:assert/strict";
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; }
let routeSeq = 1, modalOwner = {}, opened = 0;
const Mesh = {state: {marker: "fresh-C"}, structKey: "", detailsKey: ""};
const captureViewRead = () => ({routeSeq});
const viewReadMayApply = owner => !!owner && owner.routeSeq === routeSeq;
const closeModal = () => { modalOwner = {}; };
const beginModalRead = () => { closeModal(); return {owner: modalOwner, view: captureViewRead()}; };
const captureModalRead = () => ({owner: modalOwner, view: captureViewRead()});
const modalReadMayApply = ticket => ticket?.owner === modalOwner && viewReadMayApply(ticket.view);
const box = {isConnected: true, classList: {add() {}}, parentElement: {classList: {add() {}}},
  querySelector: () => ({addEventListener() {}, classList: {add() {}}, dataset: {}})};
const openModal = () => { closeModal(); opened += 1; return box; };
const bindModalFilter = () => {}; const bindPicker = () => {};
const pickerRow = () => "row"; const pickerSection = (_title, body) => body;
const pickerFooter = () => "footer"; const esc = String;
const meshDn = (name, context) => context?.users?.[name]?.display || name;
const meshAvatarInner = () => ""; const meshChatAvatarInner = () => "";
const toast = () => {}; const ICONS = {close: "", search: "", check: "", send: ""};
const V = {renderChats() {}, exitSelect() {}};
let calls = [], responses = [];
const api = path => { calls.push(path); const next = responses.shift(); return next?.promise || Promise.resolve(next); };
const functions = __FUNCTIONS__;
const factory = new Function("api", "beginModalRead", "captureModalRead", "modalReadMayApply", "openModal",
  "closeModal", "bindModalFilter", "bindPicker", "pickerRow", "pickerSection", "pickerFooter", "esc", "toast",
  "ICONS", "Mesh", "meshDn", "meshAvatarInner", "meshChatAvatarInner", "V",
  `${functions}; return {showAddMembers, showSearchMembers, openForwardPicker};`);
const funcs = factory(api, beginModalRead, captureModalRead, modalReadMayApply, openModal,
  closeModal, bindModalFilter, bindPicker, pickerRow, pickerSection, pickerFooter, esc, toast,
  ICONS, Mesh, meshDn, meshAvatarInner, meshChatAvatarInner, V);
const state = {user: "aryan", users: {aryan: {username: "aryan", display: "Aryan", kind: "human"}}, chats: []};
const chat = {meta: {members: []}};

for (const [name, invoke] of [
  ["add", () => funcs.showAddMembers("A")],
  ["search", () => funcs.showSearchMembers("A")],
  ["forward", () => funcs.openForwardPicker("A", ["m1"])],
]) {
  calls = []; opened = 0; const held = deferred(); responses = [held];
  const pending = invoke(); assert.deepEqual(calls, ["/api/mesh/state"], name);
  routeSeq += 1; held.resolve(state); await pending;
  assert.deepEqual(calls, ["/api/mesh/state"], `${name}: no room/action read`);
  assert.equal(opened, 0, `${name}: no stale modal`);
  assert.equal(Mesh.state.marker, "fresh-C", `${name}: no global clobber`);
}

// Current owners still complete: add/search fetch their room and all three open.
for (const [name, invoke, expected, queued] of [
  ["add", () => funcs.showAddMembers("A"), ["/api/mesh/state", "/api/mesh/chat?id=A"], [state, chat]],
  ["search", () => funcs.showSearchMembers("A"), ["/api/mesh/state", "/api/mesh/chat?id=A"], [state, chat]],
  ["forward", () => funcs.openForwardPicker("A", ["m1"]), ["/api/mesh/state"], [state]],
]) {
  calls = []; opened = 0; responses = queued; await invoke();
  assert.deepEqual(calls, expected, name); assert.equal(opened, 1, name);
}
'''
