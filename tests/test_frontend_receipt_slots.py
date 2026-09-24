"""Receipt-only updates preserve canonical message DOM and patch one slot."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _function(source: str, name: str) -> str:
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


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_receipt_sync_patches_only_stable_slot(tmp_path: Path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    production = "\n".join([
        _function(source, "syncReceiptTicks"),
        _function(source, "receiptTicks"),
    ])
    runner = tmp_path / "receipt-slots.mjs"
    runner.write_text(
        _RUNNER.replace("__PRODUCTION__", json.dumps(production)),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(runner)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_RUNNER = r'''
import assert from "node:assert/strict";
let writes = 0;
const slot = { _receiptHtml: undefined, html: "",
  set innerHTML(value) { writes++; this.html = value; },
  get innerHTML() { return this.html; } };
const sentinel = {listeners: 3, text: "message body"};
const row = {sentinel, querySelector(selector) {
  assert.equal(selector, ".bubble > .meta > .receipt-slot"); return slot;
}};
const tr = {_rows: new Map([["m:m1", {el: row}]])};
const ICONS = {clock: "CLOCK", info: "INFO", tick: "TICK", ticks: "TICKS"};
const esc = String;
const factory = new Function("deps", `with (deps) { ${__PRODUCTION__};
  return {syncReceiptTicks}; }`);
const {syncReceiptTicks} = factory({ICONS, esc});
const message = receipt => ({id: "m1", mine: true, deleted: false,
  kind: "message", receipt});

syncReceiptTicks(tr, [message({state: "sent", transport: {state: "queued"}})], true);
assert.equal(writes, 1); assert.match(slot.html, /send-pending/);
assert.match(slot.html, /title="Waiting for transport"/); assert.match(slot.html, /CLOCK/);
assert.strictEqual(row.sentinel, sentinel);

// Identical receipt state performs no DOM write at all.
syncReceiptTicks(tr, [message({state: "sent", transport: {state: "queued"}})], true);
assert.equal(writes, 1); assert.equal(sentinel.listeners, 3);

syncReceiptTicks(tr, [message({state: "sent", transport: {state: "sent"}})], true);
assert.equal(writes, 2); assert.doesNotMatch(slot.html, /send-pending| read/);
assert.match(slot.html, /title="Sent"/); assert.match(slot.html, /TICK/);

syncReceiptTicks(tr, [message({state: "delivered"})], true);
assert.equal(writes, 3); assert.match(slot.html, /title="Delivered"/);
assert.match(slot.html, /TICKS/); assert.doesNotMatch(slot.html, /class="ticks read"/);

syncReceiptTicks(tr, [message({state: "read"})], true);
assert.equal(writes, 4); assert.match(slot.html, /class="ticks read"/);
assert.match(slot.html, /title="Read"/); assert.match(slot.html, /TICKS/);
assert.strictEqual(row.sentinel, sentinel);

// Rows without a canonical seeded slot are ignored rather than rebuilt.
tr._rows.set("m:m2", {el: {querySelector: () => null}});
syncReceiptTicks(tr, [{id: "m2", mine: true, kind: "message",
  receipt: {state: "read"}}], true);
assert.equal(writes, 4);
// A cold paged receipt companion cannot fabricate Sent from absence.
syncReceiptTicks(tr, [message(undefined)], true, false);
assert.equal(slot.html, ""); assert.equal(writes, 5);
syncReceiptTicks(tr, [message(undefined)], true, false);
assert.equal(writes, 5);
syncReceiptTicks(tr, [message({state:"delivered"})], true, true);
assert.match(slot.html, /Delivered/); assert.equal(writes, 6);
'''
