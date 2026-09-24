"""Network owner composes bounded canonical windows without DOM or legacy reads."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_chat_page_read_network_owner(tmp_path):
    js = ROOT / 'gui/static/js'
    (tmp_path / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    for name in ('chat-pages.js', 'chat-page-read.js'):
        (tmp_path / name).write_text((js / name).read_text(encoding='utf-8'), encoding='utf-8')
    script = tmp_path / 'check.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {createChatPageRead} from './chat-page-read.js';

const binding = (generation = '1') => ({instance_id:'app',session_generation:generation,viewer:'alice'});
const page = (ids, version = 'v1', cursor = 'next', starred = [], extras = {}) => ({
  status:'page', chat_id:'room', session_binding:binding(), page_version:version,
  window_anchor:`anchor-${cursor ?? 'tail'}`, continuation:cursor,
  has_more:cursor !== null, history_exhausted:cursor === null,
  scan_budget_exhausted:false, messages:ids.map(id => ({id, body:id, receipt:{status:'read'}})),
  starred, starred_scope:'page', meta:{id:'room',pins:[{id:'p1'}]}, me:'alice',
  read_ns:17, read_cutoff_ns:'1790000000000000001', metadata_status:{pins:'ready',receipts:'ready'}, ...extras,
});
const ids = result => result.messages.map(m => m.id);
const queue = [], calls = [];
const fetchPage = request => {calls.push(request); const next=queue.shift();
  if (next === undefined) throw new Error('unscripted fetch');
  return typeof next === 'function' ? next(request) : next;
};
const reader = createChatPageRead({fetchPage,maxPages:2,maxMessages:4,maxBytes:2000});
assert.equal((await reader.read('first')).status, 'unavailable');
reader.reset(binding(), 'room', 1);
queue.push(page(['m4','m5'],'v1','c4',['m5']));
let result = await reader.read('first');
assert.equal(result.status,'page');
assert.deepEqual(result.pageData.session_binding,binding());
assert.deepEqual(ids(result),['m4','m5']);
assert.deepEqual(result.pageData.starred,['m5']);
assert.equal(result.messages[1].receipt.status,'read');
assert.equal(result.pageData.meta.pins[0].id,'p1');
assert.equal(result.pageData.read_ns,17);
assert.equal(result.pageData.read_cutoff_ns,'1790000000000000001');
assert.equal(calls[0].cursor,null);
assert.equal(calls[0].anchor,null);

queue.push(page(['m2','m3'],'v1','c2',['m3']));
result = await reader.read('older');
assert.deepEqual(ids(result),['m2','m3','m4','m5']);
assert.deepEqual(result.pageData.starred,['m3','m5']);
queue.push(page(['m0','m1'],'v1','c0',['m0'], {meta:{id:'room',pins:[{id:'p2'}]}}));
result = await reader.read('older');
assert.deepEqual(ids(result),['m0','m1','m2','m3']);
assert.deepEqual(result.evictedIds,['m4','m5']);
assert.deepEqual(result.pageData.starred,['m0','m3']);
assert.equal(result.pageData.meta.pins[0].id,'p2');
assert.equal(reader.refreshPlan().windowAnchor,'anchor-c2');

let resolveSecond;
const second = new Promise(resolve => {resolveSecond=resolve;});
queue.push(page(['m2','m3'],'v2','rc2',['m3']));
queue.push(second);
const refresh = reader.read('refresh');
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(ids(reader.snapshot()),['m0','m1','m2','m3']); // draft not handed out
assert.equal(calls.at(-2).anchor,'anchor-c2');
assert.equal(calls.at(-2).cursor,null);
assert.equal(calls.at(-1).anchor,null);
assert.equal(calls.at(-1).cursor,'rc2');
resolveSecond(page(['m0','m1-new'],'v2','rc0',['m1-new'],
  {meta:{id:'room',pins:[{id:'fresh'}]}, read_ns:19}));
result = await refresh;
assert.deepEqual(ids(result),['m0','m1-new','m2','m3']);
assert.deepEqual(result.evictedIds,['m1']);
assert.deepEqual(result.pageData.starred,['m1-new','m3']);
assert.equal(result.pageData.meta.pins[0].id,'fresh');
assert.equal(result.pageData.read_ns,19);
assert.equal(result.pageVersion,'v2');

queue.push(page([], 'v3', 'empty1', [], {scan_budget_exhausted:true}));
queue.push(page([], 'v3', 'empty2', [], {scan_budget_exhausted:true}));
result = await reader.read('refresh');
assert.equal(result.status,'unavailable');
assert.equal(result.reason,'refresh_request_budget');
assert.deepEqual(ids(result),[]);
assert.equal(result.pageData,null);
assert.deepEqual(ids(reader.snapshot()),[]);

queue.push(page(['visible'],'v4','next',['visible']));
await reader.read('first');
queue.push({status:'pending',reason:'local_inputs_pending',retry_after_ms:350,
            session_binding:binding()});
result = await reader.read('older');
assert.equal(result.status,'pending');
assert.equal(result.retry_after_ms,350);
assert.deepEqual(result.evictedIds,['visible']);
assert.equal(result.pageData,null);
assert.deepEqual(ids(result),[]);

queue.push(page(['another'],'v5','next'));
await reader.read('first');
queue.push({error:'App is locked',locked:true});
result = await reader.read('first');
assert.equal(result.status,'locked');
assert.deepEqual(result.evictedIds,['another']);
assert.equal(result.pageData,null);

queue.push(page(['retry'],'v6','next'));
await reader.read('first');
queue.push(() => Promise.reject(new Error('network down')));
result = await reader.read('first');
assert.equal(result.status,'unavailable');
assert.equal(result.reason,'page_fetch_failed');
assert.deepEqual(result.evictedIds,['retry']);

let resolveOld;
const old = new Promise(resolve => {resolveOld=resolve;});
queue.push(old);
const stale = reader.read('first');
assert.equal((await reader.read('first')).status,'busy');
const reset = reader.reset(binding('2'), 'room', 2);
assert.equal(reset.pageData,null);
queue.push(page(['fresh'],'v7',null,[],{session_binding:binding('2')}));
result = await reader.read('first');
assert.deepEqual(ids(result),['fresh']);
resolveOld(page(['old'],'v6','old'));
assert.equal((await stale).status,'stale');
assert.deepEqual(ids(reader.snapshot()),['fresh']);

queue.push(page(['wrong'],'v8','next',[], {meta:{id:'other'}}));
result = await reader.read('first');
assert.equal(result.status,'unavailable');
assert.equal(result.reason,'page_metadata_invalid');
assert.deepEqual(result.evictedIds,['fresh']);
assert.deepEqual(ids(reader.snapshot()),[]);

const filteredQueue = [];
const filtered = createChatPageRead({fetchPage: () => filteredQueue.shift(),
  maxPages:3,maxMessages:4,maxBytes:2000});
filtered.reset(binding(), 'room', 1);
filteredQueue.push(page(['a','b'],'v1','next',['b']));
await filtered.read('first');
filteredQueue.push(page([], 'v2', 'filtered', [], {scan_budget_exhausted:true,
  window_anchor:'upper-boundary'}));
filteredQueue.push(page(['a2','b2'], 'v2', null, ['a2'],
  {window_anchor:'lower-boundary'}));
result = await filtered.read('refresh');
assert.equal(result.status,'page');
assert.deepEqual(ids(result),['a2','b2']);
assert.deepEqual(result.pageData.starred,['a2']);
assert.equal(filtered.refreshPlan().windowAnchor,'upper-boundary');

let finishSecond;
const delayedSecond = new Promise(resolve => {finishSecond=resolve;});
filteredQueue.push(page([], 'v3', 'waiting', [], {scan_budget_exhausted:true}));
filteredQueue.push(delayedSecond);
const staleRefresh = filtered.read('refresh');
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(ids(filtered.snapshot()),['a2','b2']);
filtered.reset(binding('2'), 'room', 2);
finishSecond(page(['late'], 'v3', null));
assert.equal((await staleRefresh).status,'stale');
assert.deepEqual(ids(filtered.snapshot()),[]);

const recoveryQueue = [];
const recoveryCalls = [];
const recovery = createChatPageRead({fetchPage: request => {
  recoveryCalls.push(request);
  const next = recoveryQueue.shift();
  return typeof next === 'function' ? next() : next;
}, maxPages:2,maxMessages:4,maxBytes:2000});
recovery.reset(binding(),'room',1);
recoveryQueue.push(page(['r4','r5'],'v1','r4'));
await recovery.read('first');
recoveryQueue.push(page(['r2','r3'],'v1','r2'));
await recovery.read('older');
const oldPosition = recovery.refreshPlan();
recoveryQueue.push({status:'reset_required',reason:'continuation_changed',
  session_binding:binding()});
result = await recovery.read('older');
assert.equal(result.status,'reset_required');
assert.equal(result.pageData,null);
assert.deepEqual(ids(result),[]);
assert.deepEqual(recovery.refreshPlan(),oldPosition);
recoveryQueue.push(() => Promise.reject(new Error('temporarily offline')));
result = await recovery.read('refresh');
assert.equal(result.status,'unavailable');
assert.deepEqual(recovery.refreshPlan(),oldPosition);
recoveryQueue.push(page(['r4-new','r5-new'],'v2','newer'));
recoveryQueue.push(page(['r2-new','r3-new'],'v2','older'));
result = await recovery.read('refresh');
assert.deepEqual(ids(result),['r2-new','r3-new','r4-new','r5-new']);
assert.equal(recoveryCalls.at(-2).anchor,oldPosition.windowAnchor);
assert.equal(recoveryCalls.at(-1).cursor,'newer');
assert.notEqual(recovery.refreshPlan(),null);
recovery.reset(binding('2'),'room',2);
assert.equal(recovery.refreshPlan(),null);
''', encoding='utf-8')
    run = subprocess.run(['node', str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
