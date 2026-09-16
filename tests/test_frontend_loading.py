"""Delayed regional progress must not flash, shift content, or outlive its owner."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_delayed_loading_lifetime_and_replacement(tmp_path):
    (tmp_path / 'loading.mjs').write_text(
        (ROOT / 'gui/static/js/loading.js').read_text(encoding='utf-8'), encoding='utf-8')
    script = tmp_path / 'test.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {beginLoading, endLoading} from './loading.mjs';
let next = 0;
const timers = new Map();
globalThis.setTimeout = (fn, delay) => {
  assert.equal(delay, 500); timers.set(++next, fn); return next;
};
globalThis.clearTimeout = id => timers.delete(id);
function node() {
  const attrs = new Map(), classes = new Set();
  return {isConnected: true, children: [],
    classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)},
    getAttribute: k => attrs.get(k) ?? null,
    setAttribute: (k, v) => attrs.set(k, v), removeAttribute: k => attrs.delete(k),
    appendChild(child) {this.children.push(child); child.parent = this;},
    remove() {if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this);},
  };
}
globalThis.document = {createElement: node};
function tick() {const calls = [...timers.values()]; timers.clear(); calls.forEach(f => f());}
const host = node();
let finish = beginLoading(host);
assert.equal(host.children.length, 0); assert.equal(host.getAttribute('aria-busy'), null);
finish(); tick(); assert.equal(host.children.length, 0); // fast result never flashes
finish = beginLoading(host, {label: 'Loading chat info…', placement: 'center'});
tick(); assert.equal(host.children.length, 1);
assert.equal(host.children[0].getAttribute('role'), 'status');
assert.equal(host.children[0].className, 'loading-status loading-centered');
assert.equal(host.children[0].children[0].textContent, 'Loading chat info…');
assert.equal(host.getAttribute('aria-busy'), 'true');
finish(); finish(); assert.equal(host.children.length, 0);
assert.equal(host.getAttribute('aria-busy'), null);
assert.equal(host.classList.contains('loading-host'), false);
beginLoading(host, {current: () => false}); tick(); assert.equal(host.children.length, 0);
host.isConnected = false; beginLoading(host); tick(); assert.equal(host.children.length, 0);
host.isConnected = true;
const old = beginLoading(host); tick();
const newer = beginLoading(host); tick();
old(); assert.equal(host.children.length, 1); assert.equal(host.getAttribute('aria-busy'), 'true');
newer(); assert.equal(host.children.length, 0);
host.setAttribute('aria-busy', 'false'); beginLoading(host); tick(); endLoading(host);
assert.equal(host.getAttribute('aria-busy'), 'false');
beginLoading(host); endLoading(host); tick(); assert.equal(host.children.length, 0);
assert.doesNotThrow(() => {beginLoading(null)(); endLoading(null);});
''', encoding='utf-8')
    run = subprocess.run(['node', str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
