"""Executable sidebar ownership regressions for R197 navigation recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SIDEBAR = ROOT / "gui" / "static" / "js" / "sidebar.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"export function {name}")
    end = source.index(f"export function {next_name}", start)
    return source[start:end].replace("export ", "")


def _run(tmp_path: Path, program: str) -> None:
    runner = tmp_path / "runner.mjs"
    runner.write_text(program, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner)], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_node
def test_session_reset_invalidates_sidebar_cache_and_repaints_same_markup(tmp_path: Path):
    source = SIDEBAR.read_text(encoding="utf-8")
    clear = _function(source, "clearSidebar", "syncSidebarSelection")
    set_start = source.index("function setSide(")
    set_end = source.index("\n\nfunction renderSettingsSidebar", set_start)
    set_side = source[set_start:set_end]
    _run(tmp_path, _CACHE_RUNNER.replace("__SOURCE__", json.dumps(clear + set_side)))


@requires_node
def test_route_selection_changes_in_same_task_while_network_is_held(tmp_path: Path):
    source = SIDEBAR.read_text(encoding="utf-8")
    sync = _function(source, "syncSidebarSelection", "renderSideLoading")
    _run(tmp_path, _SELECTION_RUNNER.replace("__SOURCE__", json.dumps(sync)))


_CACHE_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__;
const classes = new Set(["ng-host", "slide"]);
const box = {
  dataset: {key: "<nav>About</nav>", mode: "settings", struct: "old"},
  innerHTML: "<nav>About</nav>", style: {padding: "12px"}, offsetWidth: 200,
  replaceChildren() { this.innerHTML = ""; },
  classList: {
    remove(...names) { names.forEach(name => classes.delete(name)); },
    add(name) { classes.add(name); },
  },
};
const App = {page: "settings", _sidePage: "settings"};
const $ = selector => { assert.equal(selector, "#side-chats"); return box; };
const factory = new Function("$", "App", `${source}; return {clearSidebar, setSide};`);
const api = factory($, App);
api.clearSidebar();
assert.deepEqual(box.dataset, {}); assert.equal(box.innerHTML, "");
assert.equal(box.style.padding, ""); assert.equal(App._sidePage, null);
assert.equal(api.setSide("<nav>About</nav>", "12px"), true);
assert.equal(box.innerHTML, "<nav>About</nav>");
assert.equal(box.dataset.key, "<nav>About</nav>");
'''


_SELECTION_RUNNER = r'''
import assert from "node:assert/strict";
const source = __SOURCE__;
function row(id, active) {
  const classes = new Set(active ? ["chat-row", "active"] : ["chat-row"]);
  return {dataset: {chat: id, sig: `sig-${id}`}, classList: {
    contains(name) { return classes.has(name); },
    toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
  }};
}
const rows = [row("old", true), row("new", false)];
const document = {querySelectorAll: selector => {
  assert.equal(selector, "#side-chats .chat-row"); return rows;
}};
const App = {page: "chats"}; const Mesh = {chatId: "new"};
const heldRequest = new Promise(() => {});
const factory = new Function("document", "App", "Mesh",
  `${source}; return syncSidebarSelection;`);
factory(document, App, Mesh)();
assert.equal(rows[0].classList.contains("active"), false);
assert.equal(rows[1].classList.contains("active"), true);
assert.equal("sig" in rows[0].dataset, false);
assert.equal("sig" in rows[1].dataset, false);
assert.ok(heldRequest instanceof Promise); // no network completion was involved
'''
