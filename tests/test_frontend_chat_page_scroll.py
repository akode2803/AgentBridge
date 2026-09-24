"""Geometry and resource ownership of bounded transcript scroll windows."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_scroll_anchor_and_evicted_row_resources(tmp_path):
    (tmp_path / 'chat-page-scroll.mjs').write_text(
        (ROOT / 'gui/static/js/chat-page-scroll.js').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    script = tmp_path / 'check.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {captureTranscriptAnchor, restoreTranscriptAnchor, pruneTranscriptResources}
  from './chat-page-scroll.mjs';

class Transcript {
  constructor(entries = [], scrollTop = 0) {
    this.entries = entries;
    this.scrollTop = scrollTop;
    Object.defineProperty(this, 'scrollHeight', {get() {
      throw Error('height-delta restoration is invalid under opposite-end eviction');
    }});
  }
  getBoundingClientRect() { return {top:100, bottom:400}; }
  querySelectorAll(selector) {
    assert.equal(selector, '.msg[data-mid]');
    return this.entries.map(entry => entry.node);
  }
  setRows(items) {
    this.entries = items.map(([id, y, height = 40, reuse]) => ({
      id, node: reuse || {
        dataset:{mid:id},
        getBoundingClientRect: () => ({
          top:100 + y - this.scrollTop,
          bottom:100 + y - this.scrollTop + height,
        }),
      },
    }));
  }
}
const make = (items, top) => {
  const tr = new Transcript([], top);
  tr.setRows(items);
  return tr;
};
const offset = (tr, id) => tr.entries.find(entry => entry.id === id)
  .node.getBoundingClientRect().top - tr.getBoundingClientRect().top;

// Prepend shifts all content; first visible mid stays at the same viewport Y.
let tr = make([['a',0], ['b',40], ['c',80], ['d',120]], 65);
let anchor = captureTranscriptAnchor(tr);
assert.equal(anchor.candidates[0].id, 'b');
assert.equal(anchor.candidates[0].offset, -25);
tr.setRows([['older',0], ['a',40], ['b',80], ['c',120], ['d',160]]);
assert.equal(restoreTranscriptAnchor(tr, anchor), 'anchor');
assert.equal(tr.scrollTop, 105);
assert.equal(offset(tr, 'b'), -25);

// A top prepend and opposite-end eviction cancel in total height, but moving
// the surviving anchor still requires a row-based correction.
tr = make([['a',0], ['b',40], ['c',80], ['d',120]], 65);
anchor = captureTranscriptAnchor(tr);
tr.setRows([['older',0], ['a',40], ['b',80], ['c',120]]); // d evicted
assert.equal(restoreTranscriptAnchor(tr, anchor), 'anchor');
assert.equal(tr.scrollTop, 105);

// The first visible node disappears: prefer the next surviving neighbor,
// using that neighbor's old position rather than old scrollTop.
tr = make([['a',0], ['b',40], ['c',80], ['d',120]], 65);
anchor = captureTranscriptAnchor(tr);
tr.setRows([['older',0], ['a',40], ['c',80], ['d',120]]); // b removed
assert.equal(restoreTranscriptAnchor(tr, anchor), 'adjacent');
assert.equal(offset(tr, 'c'), 15); // original c: 80-65

// Reused DOM node may move under keyed reconcile; geometry is sampled fresh.
tr = make([['a',0], ['b',40], ['c',80]], 65);
anchor = captureTranscriptAnchor(tr);
const old = tr.entries.find(entry => entry.id === 'b').node;
tr.setRows([['earlier',0], ['a',40], ['b',80,40,old], ['c',120]]);
// Real DOM reads live layout from a reused node. Simulate its new layout.
old.getBoundingClientRect = () => ({top:100 + 80 - tr.scrollTop,
                                     bottom:100 + 120 - tr.scrollTop});
assert.equal(restoreTranscriptAnchor(tr, anchor), 'anchor');
assert.equal(offset(tr, 'b'), -25);

// Empty transcript and a vanished anchor without neighbors are bounded.
tr = make([['a',0]], 19);
anchor = captureTranscriptAnchor(tr);
tr.setRows([]);
assert.equal(restoreTranscriptAnchor(tr, anchor), 'empty');
assert.equal(tr.scrollTop, 0);
tr = make([], 44);
anchor = captureTranscriptAnchor(tr);
assert.deepEqual(anchor.candidates, []);
tr.setRows([['new', 0]]);
assert.equal(restoreTranscriptAnchor(tr, anchor), 'position');
assert.equal(tr.scrollTop, 44);

// No unbounded geometry walk if a malformed caller retained more than cap.
tr = make([['one',0], ['two',40], ['three',80], ['four',120]], 30);
tr.entries[3].node.getBoundingClientRect = () => { throw Error('past cap'); };
assert.equal(captureTranscriptAnchor(tr, {maxRows:3}).candidates[0].id, 'one');
assert.throws(() => captureTranscriptAnchor(tr, {maxRows:601}), RangeError);

// Evicted canonical IDs release only their state; a deduplicated ID still in
// retained DOM stays selected, expanded and present in row/message maps.
tr = make([['keep',0], ['live',40]], 0);
tr._msgs = new Map([['keep',{id:'keep'}], ['old',{id:'old'}]]);
tr._rows = new Map([['m:keep',{html:'keep'}], ['m:old',{html:'old'}],
                    ['d:day',{html:'day'}]]);
const msgExpand = {keep:true, old:true};
const selectedIds = new Set(['keep','old']);
const icons = new Map([['keep','icon'], ['old','icon']]);
assert.deepEqual(pruneTranscriptResources(tr, ['old','keep','old'],
  {msgExpand,selectedIds,maps:[icons]}), ['old']);
assert.deepEqual(msgExpand, {keep:true});
assert.deepEqual([...selectedIds], ['keep']);
assert.equal(tr._msgs.has('old'), false);
assert.equal(tr._msgs.has('keep'), true);
assert.equal(tr._rows.has('m:old'), false);
assert.equal(tr._rows.has('d:day'), true);
assert.equal(icons.has('old'), false);
assert.equal(icons.has('keep'), true);
assert.throws(() => pruneTranscriptResources(tr, Array(601).fill('x')), RangeError);
''', encoding='utf-8')
    subprocess.run([shutil.which('node'), str(script)], check=True,
                   cwd=tmp_path, capture_output=True, text=True, timeout=15)
