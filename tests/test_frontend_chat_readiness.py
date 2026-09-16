"""Executable contracts for the canonical selected-chat surface readiness."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _function(source: str, name: str) -> str:
    """Return one complete production function without copying its implementation."""
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(source)):
        char = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unterminated function {name}")


def _between(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


@requires_node
def test_base_chat_is_interactive_and_repaints_do_not_stack_handlers(tmp_path: Path):
    chat = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    api = (ROOT / "gui/static/js/api.js").read_text(encoding="utf-8")
    production = "\n".join([
        _function(api, "bindOpenFile"),
        _function(chat, "reconcileRows"),
        _function(chat, "syncReceiptTicks"),
        _function(chat, "receiptTicks"),
        _between(chat, "function bindTranscript(", "function openMsgMenu("),
        _between(chat, "async function renderMeshChat(", "V.renderMeshChat = renderMeshChat;"),
    ])
    runner = tmp_path / "readiness.mjs"
    runner.write_text(_RUNNER.replace("__PRODUCTION__", json.dumps(production)), encoding="utf-8")
    done = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


@requires_node
def test_base_reply_and_edit_use_canonical_presentation_without_mesh_state(tmp_path: Path):
    composer = (ROOT / "gui/static/js/composer.js").read_text(encoding="utf-8")
    production = ("\n".join([
        _function(composer.replace("export ", ""), "syncSendState"),
        _between(composer, "export function renderReplyArea(", "function cancelEdit("),
    ])).replace("export ", "")
    runner = tmp_path / "base-actions.mjs"
    runner.write_text(
        _BASE_ACTIONS_RUNNER.replace("__PRODUCTION__", json.dumps(production)),
        encoding="utf-8",
    )
    done = subprocess.run(["node", str(runner)], text=True, capture_output=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


_RUNNER = r'''
import assert from "node:assert/strict";

class Classes {
  constructor(value = "") { this.s = new Set(value.split(/\s+/).filter(Boolean)); }
  add(...xs) { xs.forEach(x => this.s.add(x)); }
  remove(...xs) { xs.forEach(x => this.s.delete(x)); }
  contains(x) { return this.s.has(x); }
}
class El {
  constructor(tag = "div", attrs = {}) {
    this.tagName = tag.toUpperCase(); this.id = attrs.id || "";
    this.dataset = attrs.dataset || {}; this.classList = new Classes(attrs.class || "");
    this.children = []; this.parentElement = null; this.listeners = {};
    this.style = {}; this.hidden = !!attrs.hidden; this.isConnected = true;
    this.scrollHeight = 100; this.scrollTop = 0; this.clientHeight = 100;
  }
  appendChild(x) { x.parentElement = this; this.children.push(x); return x; }
  insertBefore(x, before) { if (x.parentElement) x.remove(); x.parentElement = this;
    const i = before ? this.children.indexOf(before) : -1;
    if (i < 0) this.children.push(x); else this.children.splice(i, 0, x); return x; }
  before(x) { this.parentElement?.insertBefore(x, this); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(v => v !== this); this.parentElement = null; this.isConnected = false; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(v => v !== fn); }
  async click() { const e = {target: this}; for (const fn of this.listeners.click || []) await fn(e); }
  focus() {} setSelectionRange() {} setAttribute(k, v) { this[k] = v; }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() { if (!this.parentElement) return null; const a = this.parentElement.children; return a[a.indexOf(this) + 1] || null; }
  querySelectorAll(sel) { return descendants(this).filter(x => matches(x, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let x = this; x; x = x.parentElement) if (matches(x, sel)) return x; return null; }
}
function descendants(root) { return root.children.flatMap(x => [x, ...descendants(x)]); }
function matches(x, sel) {
  if (sel === ".mesh-att") return x.classList.contains("mesh-att");
  if (sel === ".msg:last-of-type") return x.classList.contains("msg");
  if (sel === ".msg[data-mid]") return x.classList.contains("msg") && x.dataset.mid !== undefined;
  if (sel === "button") return x.tagName === "BUTTON";
  if (sel === '[data-act="clear"]') return x.dataset.act === "clear";
  if (sel === '#chat-menu [data-act="clear"]') return x.dataset.act === "clear";
  if (sel.startsWith("#")) return x.id === sel.slice(1);
  return false;
}
const ids = new Map();
function register(x) { if (x.id) ids.set(x.id, x); x.children.forEach(register); return x; }
function rowFrom(html) {
  const row = new El("div", {class: "msg", dataset: {mid: html.match(/data-mid="([^"]*)"/)?.[1] || ""}});
  for (const hit of html.matchAll(/class="([^"]*mesh-att[^"]*)"[^>]*data-id="([^"]+)"/g))
    row.appendChild(new El("button", {class: hit[1], dataset: {id: hit[2]}}));
  return row;
}
const content = new El("main", {id: "content"});
Object.defineProperty(content, "innerHTML", {set(html) {
  this.html = html; this.children = []; ids.clear(); register(this);
  const top = this.appendChild(new El("div", {id: "chat-top"}));
  top.appendChild(new El("button", {id: "chat-back"}));
  top.appendChild(new El("button", {id: "chat-more"}));
  const menu = top.appendChild(new El("div", {id: "chat-menu", hidden: true}));
  for (const act of [...html.matchAll(/<button data-act="([^"]+)"/g)].map(m => m[1])) menu.appendChild(new El("button", {dataset: {act}}));
  const tr = this.appendChild(new El("div", {id: "transcript"}));
  const transcriptHtml = html.match(/<div id="transcript"[^>]*>([\s\S]*?)<\/div>\s*<div id="pending-area"/)?.[1] || "";
  if (transcriptHtml.includes('class="day-sep"')) tr.appendChild(new El("div", {class: "day-sep"}));
  for (const chunk of transcriptHtml.split(/(?=<div class="msg )/).filter(x => x.includes('class="msg '))) tr.appendChild(rowFrom(chunk));
  this.appendChild(new El("div", {id: "pending-area"})); this.appendChild(new El("div", {id: "ask-bar"}));
  this.appendChild(new El("div", {id: "reply-area"})); this.appendChild(new El("textarea", {id: "mesh-body"}));
  register(this);
}});
const document = {hasFocus: () => false, addEventListener() {}, removeEventListener() {},
  querySelectorAll: sel => descendants(content).filter(x => matches(x, sel)),
  createElement(tag) { if (tag === "template") { const t = new El(tag); t.content = {};
      Object.defineProperty(t, "innerHTML", {set(v) { t.content.firstElementChild = rowFrom(v); }}); return t; }
    return new El(tag); }};
const $ = sel => sel === "#content" ? content : sel.startsWith("#") ? ids.get(sel.slice(1)) || null : null;
const calls = [];
async function api(path, body) { calls.push([path, body]); return {}; }
const Mesh = {chatId: "room", state: {user: "aryan", chats: [{id: "room"}], users: {}},
  chatKey: "", structKey: "", select: {on: false}};
const App = {page: "chats"}; const V = {}; const location = {hash: "#/chats/room"};
let chatRenderSeq = 0; const ICONS = new Proxy({}, {get: () => ""});
const passthrough = x => String(x ?? "");
const captureSessionEpoch = () => ({}), sessionMayApply = () => true;
const chatStructKey = (id, m) => `${id}|${m.kind}|${(m.members || []).join(",")}`;
const isDmLike = m => m.kind === "dm" || m.kind === "self";
const chatAdmins = () => ["aryan"], chatDisplay = () => "Ready room";
const meshDn = passthrough, meshAvatarInner = () => "", meshInfoText = () => "";
const meshChatAvatarInner = () => "", meshMuteActive = () => false;
const esc = passthrough, md = passthrough, timeOnly = passthrough, dayLabel = () => "Today";
const receiptTicks = () => "", rxBadge = () => "", replyQuote = () => "";
const isImg = () => false, fileUrl = () => "", extIcon = () => "", fmtSize = () => "1 KB";
const currentRunAuthority = () => [], runAccessDetails = () => "", presenceLine = () => "";
const noop = () => {}; const setTaggable = noop, syncDmHeaderPresence = noop, closeMenus = noop;
const syncPinBanner = noop, captureRxSigs = () => [], animateRxChanges = noop;
const clampLong = noop, jumpToMessage = noop, initComposer = noop, renderReplyArea = noop;
const renderMeshPending = noop, startAskPoll = noop, markReadNow = noop, applySelectAfterRender = noop;
const reconcileSends = noop, pendingSendRows = () => [];
const clearSelectMode = noop;
const endLoading = noop;
const toast = noop, enterSelect = noop, muteDialog = noop, clearChatDialog = noop, deleteChatDialog = noop;
const innerWidth = 1200, innerHeight = 800, performance = {now: () => 0};

const factory = new Function("deps", `with (deps) { ${__PRODUCTION__}; return {renderMeshChat}; }`);
const {renderMeshChat} = factory({api, document, $, Mesh, App, V, location, ICONS, captureSessionEpoch,
  sessionMayApply, chatStructKey, isDmLike, chatAdmins, chatDisplay, meshDn, meshAvatarInner,
  meshInfoText, meshChatAvatarInner, meshMuteActive, esc, md, timeOnly, dayLabel, receiptTicks,
  rxBadge, replyQuote, isImg, fileUrl, extIcon, fmtSize, currentRunAuthority, runAccessDetails,
  presenceLine, setTaggable, syncDmHeaderPresence, closeMenus, syncPinBanner, captureRxSigs,
  animateRxChanges, clampLong, jumpToMessage, initComposer, renderReplyArea, renderMeshPending,
  startAskPoll, markReadNow, applySelectAfterRender, toast, enterSelect, muteDialog,
  clearChatDialog, deleteChatDialog, innerWidth, innerHeight, performance, encodeURIComponent,
  Date, Map, Set, JSON, Object, Math, chatRenderSeq, clearSelectMode, endLoading,
  reconcileSends, pendingSendRows});
const base = {me: "aryan", meta: {id: "room", kind: "dm", members: ["aryan", "bot"], pins: []},
  messages: [{id: "m1", from: "aryan", mine: true, ts: "2026-09-16T00:00:00Z", body: "file", files: [{id: "blob-1", name: "notes.txt", bytes: 4}]}], starred: []};
const presentation = {user: "aryan", chats: [{id: "room"}], users: {aryan: {}, bot: {}}};

await renderMeshChat(true, null, {data: base, presentation, warmBase: true});
const more = $("#chat-more"), menu = $("#chat-menu"), tr = $("#transcript"), file = tr.querySelector(".mesh-att");
assert(more && menu && file, "base paint must expose options and canonical file action");
await more.click(); assert.equal(menu.hidden, false, "base options must open immediately");
assert.equal(file.listeners.click.length, 1); assert.equal(tr.listeners.click.length, 1);
await file.click();
assert.deepEqual(calls.at(-1), ["/api/mesh/open_file", {chat_id: "room", id: "blob-1"}]);
assert(!content.html.includes("verification-banner") && !content.html.includes("e2ee-banner"));
assert.equal(content.children.findIndex(x => x.id === "transcript"), 1, "verification must not insert above transcript");

// Hydration rebuild: same canonical rows, richer auxiliary state.
Mesh.structKey = "";
await renderMeshChat(true, null, {data: base, presentation, aux: [{feeds: []}, {tasks: []}]});
const hydratedTr = $("#transcript"), hydratedFile = hydratedTr.querySelector(".mesh-att");
assert.equal(hydratedFile.listeners.click.length, 1); assert.equal(hydratedTr.listeners.click.length, 1);

// Partial reconciliation reuses m1; binders must neither vanish nor stack.
const partial = {...base, messages: [...base.messages, {id: "m2", from: "bot", mine: false,
  ts: "2026-09-16T00:00:01Z", body: "hello", files: []}]};
await renderMeshChat(false, null, {data: partial, presentation, aux: [{feeds: []}, {tasks: []}]});
assert.strictEqual($("#transcript"), hydratedTr);
assert.strictEqual($("#transcript").querySelector(".mesh-att"), hydratedFile);
assert.equal(hydratedFile.listeners.click.length, 1); assert.equal(hydratedTr.listeners.click.length, 1);

// Signature skip must retain the same live elements and listener counts.
await renderMeshChat(true, null, {data: partial, presentation, aux: [{feeds: []}, {tasks: []}]});
assert.strictEqual($("#transcript"), hydratedTr);
assert.equal(hydratedFile.listeners.click.length, 1); assert.equal(hydratedTr.listeners.click.length, 1);
'''


_BASE_ACTIONS_RUNNER = r'''
import assert from "node:assert/strict";
const draft = {body: "draft", atts: [], reply: null, editing: null};
const area = {html: "", set innerHTML(value) { this.html = value; },
  get innerHTML() { return this.html; }};
const body = {value: "", focus() {}, dispatchEvent() {},
  setSelectionRange() {}};
const send = {disabled: false, innerHTML: "", title: ""};
const cancel = {addEventListener() {}};
const $ = selector => ({"#reply-area": area, "#mesh-body": body,
  "#mesh-send-btn": send, "#reply-cancel": cancel})[selector] || null;
const Mesh = {state: null};
const ICONS = {pencil: "", close: "", check: "", send: ""};
const meshDraft = () => draft, saveDraft = () => {};
const stripMd = String, esc = String;
const meshDn = (name, context) => context.users[name]?.display || name;
const Event = class {};
const factory = new Function("deps", `with (deps) { ${__PRODUCTION__};
  return {startReply, startEdit}; }`);
const {startReply, startEdit} = factory({$, Mesh, ICONS, meshDraft, saveDraft,
  stripMd, esc, meshDn, Event});
const presentation = {user: "aryan", users: {bot: {display: "Build Bot"}}};
startReply("room", {id: "m1", from: "bot", body: "answer"}, presentation);
assert.equal(draft.reply.id, "m1");
assert(area.html.includes("Build Bot"));
startEdit("room", {id: "m2", from: "aryan", body: "correction"}, presentation);
assert.equal(draft.editing.id, "m2");
assert(area.html.includes("Edit message"));
assert.equal(Mesh.state, null, "actions must not require broad hydration");
'''
