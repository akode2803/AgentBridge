"""Request-owned browser pagination state, before GUI activation."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_chat_pages_route_version_dedup_and_memory_caps(tmp_path):
    (tmp_path / 'chat-pages.mjs').write_text(
        (ROOT / 'gui/static/js/chat-pages.js').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    script = tmp_path / 'check.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {createChatPages} from './chat-pages.mjs';

const binding = (generation = '1', viewer = 'alice') => ({
  instance_id: 'app', session_generation: generation, viewer,
});
const page = (ids, version = 'v1', continuation = 'raw-older', extra = {}) => ({
  status: 'page', chat_id: 'room', session_binding: binding(), page_version: version,
  messages: ids.map(id => ({id, body: id})), continuation,
  window_anchor: `anchor-${continuation ?? 'tail'}`,
  has_more: continuation !== null, history_exhausted: continuation === null,
  scan_budget_exhausted: false, ...extra,
});
const ids = result => result.messages.map(m => m.id);

const state = createChatPages({maxPages: 2, maxMessages: 4, maxBytes: 1000});
assert.equal(state.begin('first'), null);
state.reset(binding(), 'room', 1);
const first = state.begin('first');
assert.equal(state.begin('first'), null);
assert.equal(state.begin('older'), null); // one request at a time
let rendered = state.accept(first, page(['m4', 'm5']));
assert.deepEqual(ids(rendered), ['m4', 'm5']);
assert.equal(rendered.continuation, 'raw-older');
let older = state.begin('older');
assert.equal(older.pageVersion, 'v1');
assert.equal(older.continuation, 'raw-older');
assert.equal(state.begin('first'), null);
rendered = state.accept(older, page(['m2', 'm3', 'm4'], 'v1', 'raw-earlier'));
assert.deepEqual(ids(rendered), ['m2', 'm3', 'm4', 'm5']);
assert.equal(rendered.messageCount, 4);
assert.equal(rendered.pageCount, 2);
assert.equal(rendered.continuation, 'raw-earlier');

older = state.begin('older');
rendered = state.accept(older, page(['m0', 'm1'], 'v1', 'raw-oldest'));
assert.deepEqual(ids(rendered), ['m0', 'm1', 'm2', 'm3']);
assert.deepEqual(rendered.evictedIds, ['m4', 'm5']);
assert.ok(rendered.bytes <= 1000);
assert.equal(rendered.continuation, 'raw-oldest'); // raw cursor, never visible ID
rendered.messages[0].body = 'tampered';
assert.equal(state.snapshot().messages[0].body, 'm0');

// The newest retained page was once an older page. Refresh from its original
// raw request anchor, not the tail, staging every replacement off-DOM.
assert.deepEqual(state.refreshPlan(), {
  windowAnchor:'anchor-raw-earlier', requestedMessages:4,
});
let refreshing = state.begin('refresh');
assert.equal(refreshing.windowAnchor, 'anchor-raw-earlier');
assert.equal(refreshing.requestedMessages, 4);
assert.equal(refreshing.limit, 4);
assert.equal(state.begin('first'), null);
let progress = state.accept(refreshing, page(['m2', 'm3'], 'v1', 'ref-next'));
assert.equal(progress.status, 'refreshing');
assert.deepEqual(ids(state.snapshot()), ['m0', 'm1', 'm2', 'm3']);
refreshing = state.begin('refresh');
assert.equal(refreshing.windowAnchor, null);
assert.equal(refreshing.continuation, 'ref-next');
assert.equal(refreshing.limit, 2);
rendered = state.accept(refreshing, page(['m0', 'm1-new'], 'v1', 'ref-oldest'));
assert.equal(rendered.status, 'page');
assert.deepEqual(ids(rendered), ['m0', 'm1-new', 'm2', 'm3']);
assert.deepEqual(rendered.evictedIds, ['m1']);
assert.equal(state.refreshPlan().windowAnchor, 'anchor-ref-next');

// Even the same version cannot authorize keeping old visible windows after a
// first-page refresh: overlays, keys and membership can change independently.
const refreshed = state.begin('first');
rendered = state.accept(refreshed, page(['m2', 'm6'], 'v1', 'newest-raw'));
assert.deepEqual(ids(rendered), ['m2', 'm6']);
assert.deepEqual(rendered.evictedIds, ['m0', 'm1-new', 'm3']); // m2 remains visible
assert.equal(rendered.pageCount, 1);

older = state.begin('older');
rendered = state.accept(older, page(['m2'], 'v2', 'stale-raw'));
assert.equal(rendered.status, 'reset_required');
assert.deepEqual(rendered.evictedIds, ['m2', 'm6']);
assert.deepEqual(ids(state.snapshot()), []);
assert.equal(state.begin('older'), null);

let ticket = state.begin('first');
rendered = state.accept(ticket, page(['fresh-generation'], 'v4', 'raw-v4'));
assert.deepEqual(ids(rendered), ['fresh-generation']);
assert.equal(rendered.pageVersion, 'v4');
assert.equal(rendered.pageCount, 1);
assert.deepEqual(rendered.evictedIds, []);

// An empty visible page still advances the server's oldest examined raw seek.
ticket = state.begin('first');
rendered = state.accept(ticket, page([], 'v3', 'raw-1', {scan_budget_exhausted:true}));
assert.equal(rendered.scanBudgetExhausted, true);
assert.deepEqual(rendered.evictedIds, ['fresh-generation']);
assert.equal(rendered.pageCount, 1);
for (let n = 0; n < 50; n++) {
  ticket = state.begin('older');
  assert.equal(ticket.continuation, n ? `raw-${n + 1}` : 'raw-1');
  rendered = state.accept(ticket, page([], 'v3', `raw-${n + 2}`, {scan_budget_exhausted:true}));
  assert.equal(rendered.pageCount, 1);
  assert.equal(rendered.messageCount, 0);
  assert.ok(rendered.bytes <= 1000);
}
ticket = state.begin('older');
rendered = state.accept(ticket, page([], 'v3', ticket.continuation, {scan_budget_exhausted:true}));
assert.equal(rendered.status, 'reset_required');
assert.equal(rendered.reason, 'continuation_stalled');
assert.equal(state.begin('older'), null);

// A contradictory completion flag or missing progress cursor fails closed.
for (const contradiction of [
  {has_more:true, continuation:null, history_exhausted:false},
  {has_more:false, continuation:'raw', history_exhausted:true},
  {has_more:true, continuation:'raw', history_exhausted:true},
  {has_more:false, continuation:null, history_exhausted:false},
  {has_more:'yes'},
  {has_more:false, continuation:null, history_exhausted:true, scan_budget_exhausted:true},
]) {
  ticket = state.begin('first');
  rendered = state.accept(ticket, page(['retained'], 'v3'));
  ticket = state.begin('first');
  rendered = state.accept(ticket, page(['incoming'], 'v3', 'raw', contradiction));
  assert.equal(rendered.status, 'invalidated');
  assert.deepEqual(rendered.evictedIds, ['retained']);
  assert.deepEqual(ids(state.snapshot()), []);
}

// A pending or forbidden response clears even a previously visible snapshot.
ticket = state.begin('first');
state.accept(ticket, page(['visible'], 'v3'));
ticket = state.begin('older');
rendered = state.accept(ticket, {status:'pending', reason:'local_inputs_pending', session_binding:binding()});
assert.deepEqual(rendered.evictedIds, ['visible']);
assert.deepEqual(ids(state.snapshot()), []);
ticket = state.begin('first');
state.accept(ticket, page(['again'], 'v3'));
ticket = state.begin('first');
rendered = state.accept(ticket, {status:'forbidden', session_binding:binding()});
assert.deepEqual(rendered.evictedIds, ['again']);

// Route and session replacement invalidate in-flight tickets even if a late
// network response bears the prior page version and chat ID.
ticket = state.begin('first');
rendered = state.reset(binding('2'), 'room', 2);
assert.deepEqual(ids(rendered), []);
assert.equal(state.accept(ticket, page(['late'])).status, 'stale');
const current = state.begin('first');
assert.notEqual(current, ticket);
assert.equal(state.accept(current, page(['wrong-binding'])).status, 'invalidated');
state.reset(binding(), 'room', 3);
ticket = state.begin('first');
rendered = state.accept(ticket, {...page(['wrong-chat']), chat_id:'different'});
assert.equal(rendered.status, 'invalidated');
assert.deepEqual(ids(rendered), []);
state.reset(binding(), 'another', 4);
assert.equal(state.accept(ticket, page(['late'])).status, 'stale');

const invalidated = state.invalidate('user_left');
assert.equal(invalidated.status, 'invalidated');
assert.deepEqual(ids(invalidated), []);

// An oversized single page is rejected before any retained data can exceed
// the hard count or byte budget.
const small = createChatPages({maxPages:2, maxMessages:2, maxBytes:100});
small.reset(binding(), 'room', 1);
ticket = small.begin('first');
rendered = small.accept(ticket, page(['a', 'b', 'c']));
assert.equal(rendered.status, 'invalidated');
assert.equal(rendered.messageCount, 0);
ticket = small.begin('first');
rendered = small.accept(ticket, page(['a'], 'v1', 'raw', {
  messages:[{id:'a', body:'x'.repeat(1000)}],
}));
assert.equal(rendered.status, 'invalidated');
assert.equal(rendered.bytes, 0);
assert.throws(() => createChatPages({maxPages:0}), RangeError);

// Only serialized copies are held; caller-owned response objects and returned
// arrays cannot mutate a later snapshot. Many older pages stay inside caps.
const detached = createChatPages({maxPages:2, maxMessages:3, maxBytes:160});
detached.reset(binding(), 'room', 1);
ticket = detached.begin('first');
const original = page(['new']);
detached.accept(ticket, original);
original.messages[0].body = 'changed externally';
assert.equal(detached.snapshot().messages[0].body, 'new');
for (let n = 0; n < 100; n++) {
  ticket = detached.begin('older');
  rendered = detached.accept(ticket, page([`old-${n}`], 'v1', `raw-${n}`));
  assert.equal(rendered.status, 'page');
  assert.ok(rendered.pageCount <= 2 && rendered.messageCount <= 3 && rendered.bytes <= 160);
}
assert.deepEqual(ids(detached.snapshot()), ['old-99', 'old-98']);

const refreshState = createChatPages({maxPages:2, maxMessages:4, maxBytes:1000});
refreshState.reset(binding(), 'room', 1);
ticket = refreshState.begin('first');
refreshState.accept(ticket, page(['new-1', 'new-2'], 'v1', 'raw-2'));
ticket = refreshState.begin('older');
refreshState.accept(ticket, page(['old-1'], 'v1', 'raw-1'));
refreshing = refreshState.begin('refresh');
progress = refreshState.accept(refreshing, page(['new-v2'], 'v2', 'next'));
assert.equal(progress.status, 'refreshing');
assert.deepEqual(ids(refreshState.snapshot()), ['old-1', 'new-1', 'new-2']);
refreshing = refreshState.begin('refresh');
assert.equal(refreshing.pageVersion, 'v1'); // previous display version is not authority
rendered = refreshState.accept(refreshing, page(['old-v2', 'mid-v2'], 'v2', null));
assert.equal(rendered.status, 'page');
assert.deepEqual(ids(rendered), ['old-v2', 'mid-v2', 'new-v2']);
assert.equal(rendered.pageVersion, 'v2');
assert.deepEqual(rendered.evictedIds, ['old-1', 'new-1', 'new-2']);
assert.equal(refreshState.refreshPlan().windowAnchor, 'anchor-next');

refreshing = refreshState.begin('refresh');
progress = refreshState.accept(refreshing, page([], 'v3', 'empty-1', {scan_budget_exhausted:true}));
assert.equal(progress.status, 'refreshing');
refreshing = refreshState.begin('refresh');
rendered = refreshState.accept(refreshing, page([], 'v3', 'empty-2', {scan_budget_exhausted:true}));
assert.equal(rendered.status, 'unavailable');
assert.equal(rendered.reason, 'refresh_request_budget');
assert.equal(refreshState.refreshPlan(), null);
assert.deepEqual(ids(refreshState.snapshot()), []);

const emptyFirst = createChatPages({maxPages:2, maxMessages:4, maxBytes:1000});
emptyFirst.reset(binding(), 'room', 1);
ticket = emptyFirst.begin('first');
emptyFirst.accept(ticket, page(['a', 'b'], 'v1', 'older'));
const {begin: beginDetached} = emptyFirst;
refreshing = beginDetached('refresh'); // no method receiver required
progress = emptyFirst.accept(refreshing, page([], 'v2', 'empty-raw',
  {scan_budget_exhausted:true, window_anchor:'first-boundary'}));
assert.equal(progress.status, 'refreshing');
refreshing = emptyFirst.begin('refresh');
rendered = emptyFirst.accept(refreshing, page(['a2', 'b2'], 'v2', null,
  {window_anchor:'second-boundary'}));
assert.equal(rendered.status, 'page');
assert.deepEqual(ids(rendered), ['a2', 'b2']);
assert.equal(emptyFirst.refreshPlan().windowAnchor, 'first-boundary');

const emptyRetained = createChatPages({maxPages:2, maxMessages:4, maxBytes:1000});
emptyRetained.reset(binding(), 'room', 1);
ticket = emptyRetained.begin('first');
emptyRetained.accept(ticket, page([], 'v1', 'raw-empty', {scan_budget_exhausted:true}));
assert.equal(emptyRetained.refreshPlan().requestedMessages, 0);
refreshing = emptyRetained.begin('refresh');
assert.equal(refreshing.limit, 1);
rendered = emptyRetained.accept(refreshing, page([], 'v2', 'raw-next', {scan_budget_exhausted:true}));
assert.equal(rendered.status, 'page');
assert.equal(rendered.pageCount, 1);
assert.equal(emptyRetained.refreshPlan().windowAnchor, 'anchor-raw-next');

const refreshCap = createChatPages({maxPages:2, maxMessages:2, maxBytes:100});
refreshCap.reset(binding(), 'room', 1);
ticket = refreshCap.begin('first');
refreshCap.accept(ticket, page(['small'], 'v1'));
refreshing = refreshCap.begin('refresh');
rendered = refreshCap.accept(refreshing, page(['small'], 'v2', 'next', {
  messages:[{id:'small', body:'x'.repeat(1000)}],
}));
assert.equal(rendered.status, 'invalidated');
assert.deepEqual(rendered.evictedIds, ['small']);
assert.equal(refreshCap.refreshPlan(), null);

ticket = refreshState.begin('first');
refreshState.accept(ticket, page(['retained'], 'v3'));
refreshing = refreshState.begin('refresh');
progress = refreshState.accept(refreshing, page([], 'v4', 'next', {scan_budget_exhausted:true}));
assert.equal(progress.status, 'refreshing');
refreshing = refreshState.begin('refresh');
rendered = refreshState.accept(refreshing, page(['changed'], 'v5', null));
assert.equal(rendered.status, 'reset_required');
assert.deepEqual(rendered.evictedIds, ['retained']);

ticket = refreshState.begin('first');
refreshState.accept(ticket, page(['before-pending'], 'v6'));
refreshing = refreshState.begin('refresh');
progress = refreshState.accept(refreshing, page([], 'v7', 'pending-cursor',
  {scan_budget_exhausted:true}));
assert.equal(progress.status, 'refreshing');
assert.deepEqual(ids(refreshState.snapshot()), ['before-pending']);
refreshing = refreshState.begin('refresh');
rendered = refreshState.accept(refreshing, {status:'pending', session_binding:binding()});
assert.equal(rendered.status, 'pending');
assert.deepEqual(rendered.evictedIds, ['before-pending']);

ticket = refreshState.begin('first');
refreshState.accept(ticket, page(['before-route'], 'v6'));
refreshing = refreshState.begin('refresh');
refreshState.reset(binding('2'), 'room', 2);
assert.equal(refreshState.accept(refreshing, page(['late'], 'v7')).status, 'stale');
assert.equal(refreshState.refreshPlan(), null);
''', encoding='utf-8')
    run = subprocess.run(['node', str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
