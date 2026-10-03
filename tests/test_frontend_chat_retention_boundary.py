"""Real page/read modules at their 600-message safety ceiling, without a server."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_600_boundary_atomic_refresh_eviction_and_request_owner(tmp_path):
    (tmp_path / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    for name in ('chat-pages.js', 'chat-page-read.js'):
        shutil.copyfile(ROOT / 'gui/static/js' / name, tmp_path / name)
    script = tmp_path / 'retention.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {createChatPageRead} from './chat-page-read.js';
const binding = {instance_id:'local',session_generation:'1',viewer:'alice'};
const ids = (start, count) => Array.from({length:count}, (_, i) => `m${start+i}`);
const cutoff = '9007199254740993123';
const page = (rows, cursor, version='v1', extras={}) => ({
  status:'page',chat_id:'room',session_binding:binding,page_version:version,
  messages:rows.map(id=>({id,body:'x'})),meta:{id:'room'},me:'alice',
  starred:rows.length ? [rows.at(-1)] : [],metadata_status:{receipts:'ready'},
  read_ns:0,read_cutoff_ns:cutoff,read_ack_token:'a'.repeat(64),
  window_anchor:`anchor-${cursor}`,frozen_window_anchor:`frozen-${rows.at(-1)}`,
  continuation:cursor,has_more:cursor!==null,history_exhausted:cursor===null,
  scan_budget_exhausted:false,...extras,
});
const queue=[], calls=[];
const reader=createChatPageRead({pageSize:200,fetchPage:request=>{
  calls.push(request);assert.ok(queue.length,'unscripted page request');
  return queue.shift();
}});
reader.reset(binding,'room',1);
async function read(kind, response) {queue.push(response);return reader.read(kind);}
let result=await read('first',page(ids(401,200),'c401'));
assert.equal(result.messageCount,200);
result=await read('older',page(ids(201,200),'c201'));
result=await read('older',page(ids(2,199),'c2'));
assert.equal(result.messageCount,599);assert.equal(result.pageCount,3);
assert.deepEqual(result.messages.map(m=>m.id),ids(2,599));
result=await read('older',page(['m1'],'c1'));
assert.equal(result.messageCount,600);assert.equal(result.pageCount,4);
assert.deepEqual(result.evictedIds,[]);
assert.deepEqual(reader.refreshPlan(),{windowAnchor:'frozen-m600',requestedMessages:600});
result=await read('older',page(['m0'],'c0'));
// 601 attempted messages drop a whole newest page, preserving the older seek.
assert.equal(result.messageCount,401);assert.equal(result.pageCount,4);
assert.deepEqual(result.messages.map(m=>m.id),ids(0,401));
assert.deepEqual(result.evictedIds,ids(401,200));
assert.equal(result.continuation,'c0');assert.equal(result.pageVersion,'v1');
assert.deepEqual(result.pageData.starred,['m0','m1','m200','m400']);
assert.deepEqual(reader.refreshPlan(),{windowAnchor:'frozen-m400',requestedMessages:401});

// Replace with a 600-row window then hold its refresh between responses.
await read('first',page(ids(400,200),'d400'));
await read('older',page(ids(200,200),'d200'));
await read('older',page(ids(0,200),'d0'));
let release, requestedSecond;
const secondStarted=new Promise(resolve=>{requestedSecond=resolve});
const original=queue.push.bind(queue);
const second=new Promise(resolve=>{release=resolve});
original(page(ids(400,200),'r400','v2',{
  read_cutoff_ns:'9007199254740993999',read_ack_token:'b'.repeat(64)}));
original({then(resolve,reject){requestedSecond();second.then(resolve,reject);}});
original(page(['replacement',...ids(1,199)],null,'v2',{
  read_cutoff_ns:'9007199254740993000',read_ack_token:'d'.repeat(64)}));
const refreshing=reader.read('refresh');
await secondStarted;
assert.deepEqual(reader.snapshot().messages.map(m=>m.id),ids(0,600));
assert.equal(reader.snapshot().pageVersion,'v1');
assert.equal(reader.snapshot().pageData.read_ack_token,'a'.repeat(64));
assert.equal((await reader.read('older')).status,'busy');
assert.equal(calls.at(-2).anchor,'frozen-m599');
assert.equal(calls.at(-2).limit,200);
release(page(ids(200,200),'r200','v2',{
  read_cutoff_ns:'9007199254740993500',read_ack_token:'c'.repeat(64)}));
result=await refreshing;
assert.equal(result.messageCount,600);assert.equal(result.pageCount,3);
assert.deepEqual(result.evictedIds,['m0']);
assert.deepEqual(result.messages.map(m=>m.id),['replacement',...ids(1,599)]);
assert.equal(result.pageVersion,'v2');
assert.equal(result.pageData.read_cutoff_ns,'9007199254740993999');
assert.equal(result.pageData.read_ack_token,'b'.repeat(64));
assert.deepEqual(result.pageData.starred,['m199','m399','m599']);
assert.equal(calls.at(-1).limit,200);
assert.equal(result.hasMore,false);
assert.ok(JSON.stringify(result.messages).length<20000,'fixture bodies stay small');

// A delayed response from a retired request owner cannot replace the window.
let releaseOld;
queue.push(new Promise(resolve=>{releaseOld=resolve}));
const retired=reader.read('first');
reader.reset({...binding,session_generation:'2'},'room',2);
result=await read('first',page(['current'],null,'v3',{
  session_binding:{...binding,session_generation:'2'}}));
releaseOld(page(['retired'],null));
assert.equal((await retired).status,'stale');
assert.deepEqual(reader.snapshot().messages.map(m=>m.id),['current']);
assert.equal(result.pageVersion,'v3');
''', encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(script)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
