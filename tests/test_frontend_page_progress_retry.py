"""Pending canonical work retries promptly without claiming read success."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')


def _run(tmp_path, script):
    (tmp_path / 'package.json').write_text('{"type":"module"}')
    for name in ('chat-pages.js', 'chat-page-read.js'):
        (tmp_path / name).write_text((ROOT / 'gui/static/js' / name).read_text())
    (tmp_path / 'chat-source.txt').write_text((ROOT / 'gui/static/js/chat.js').read_text())
    check = tmp_path / 'check.mjs'
    check.write_text(script)
    subprocess.run(['node', str(check)], check=True, capture_output=True, text=True)


def test_pending_page_retry_policy_honors_bounded_progress_without_io_backoff(tmp_path):
    _run(tmp_path, r''' 
import assert from 'node:assert/strict';
import {pageRetryDelay} from './chat-page-read.js';
for (const status of ['pending','reset_required']) {
  for (const ms of [undefined,null,-1,NaN,Infinity,1.5,'350',{},30001])
    assert.equal(pageRetryDelay({status,retry_after_ms:ms},1,true),350);
  for (const [ms,delay] of [[0,350],[350,350],[1000,1000],[2000,2000],[30000,2000]])
    assert.equal(pageRetryDelay({status,retry_after_ms:ms},5,true),delay);
  assert.equal(pageRetryDelay({status,retry_after_ms:350},6,true),2000);
  assert.equal(pageRetryDelay({status},300,true),2000);
}
for(const attempts of [undefined,NaN,Infinity,-1,0,1.5,'5'])
  assert.equal(pageRetryDelay({status:'pending'},attempts,true),350);
assert.deepEqual([1,2,3,4,5,30].map(n=>pageRetryDelay({status:'unavailable'},n,true)),
  [500,1000,2000,4000,8000,8000]);
assert.equal(pageRetryDelay({status:'unavailable'},30,false),350);
assert.equal(pageRetryDelay({status:'unavailable',retry_after_ms:30000},30,false),2000);
''')


def test_native_ack_refreshes_and_paints_new_token_before_retry_with_ownership_fences(tmp_path):
    _run(tmp_path, r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {pageRetryDelay} from './chat-page-read.js';
const src=fs.readFileSync(new URL('./chat-source.txt',import.meta.url),'utf8');
const ackSource=src.slice(src.indexOf('let legacyReadAck = null;'),src.indexOf('// reading needs eyes:'));
const resetSource=src.slice(src.indexOf('function resetPagedView()'),src.indexOf('document.addEventListener("ab:session-reset"'));
const tick=async()=>{for(let n=0;n<8;n++)await Promise.resolve();};
const busyStart=src.indexOf('  if (owner.busy) {',src.indexOf('async function renderPagedChat('));
const busyGuard=new Function('owner','kind','options',
  src.slice(busyStart,src.indexOf('  if (!Mesh.state) renderSideLoading();',busyStart))+'return false;');
const dirtyStart=src.indexOf('    if (owner.refreshDirty && pageOwner === owner && owner.current())');
const finishDirty=new Function('owner','pageOwner','options','setTimeout','renderPagedChat',
  src.slice(dirtyStart,src.indexOf('\n  }\n}\n\nlet chatRenderSeq',dirtyStart)));

function rig() {
  let now=0,current=true,focused=true,serial=0;
  const timers=new Map(),events=new Map(),calls=[],log=[],queue=[];
  const transcript={scrollHeight:1000,scrollTop:900,clientHeight:100};
  const owner={chatId:'room',ready:true,current:()=>current,browsing:false,
    visibleReadNs:'1790000000000000030',visibleReadToken:'old-token',visibleReadVersion:'old-version'};
  const Mesh={chatId:'room',pendingRead:null,state:{chats:[{id:'room',unread:5,forced_unread:true}]}};
  let h;
  const deps={pageOwner:owner,Mesh,pageRetryTimer:null,pageRead:{invalidate:()=>log.push('invalidate')},
    pageRetryDelay,meshCaps:()=>({chat_page_v1:true}),captureViewRead:()=>{throw Error('legacy');},
    viewReadMayApply:()=>false,App:{page:'chats'},renderSidebar:()=>log.push('sidebar'),
    $:q=>q==='#transcript'?transcript:null,abortPagedAux:()=>{},Date:{now:()=>now},
    document:{hasFocus:()=>focused,addEventListener:(name,fn)=>events.set(name,fn)},
    setTimeout:(fn,ms)=>{timers.set(++serial,{fn,ms});return serial;},clearTimeout:id=>timers.delete(id),
    api:(path,data)=>{calls.push({path,data});log.push('post:'+data.read_ack_token);
      const value=queue.shift();return value instanceof Error?Promise.reject(value):Promise.resolve(value);},
    renderPagedChat:(...args)=>{if(busyGuard(owner,args[1],args[2])!==false)return;log.push('get');assert.deepEqual(args,[false,null,{realtime:true,sidebar:false}]);
      owner.visibleReadToken='fresh-token';owner.visibleReadVersion='fresh-version';log.push('paint');
      if(focused)h.markReadNow('room');},
  };
  const names=Object.keys(deps);
  h=new Function('deps','let {'+names.join(',')+'}=deps;'+resetSource+ackSource+
    ';return {markReadNow,resetPagedView,setOwner:value=>pageOwner=value};')(deps);
  return {...h,owner,Mesh,transcript,timers,events,calls,log,queue,
    advance:ms=>now+=ms,setCurrent:v=>current=v,setFocused:v=>focused=v,
    finishBusy:()=>{owner.busy=false;finishDirty(owner,owner,{},deps.setTimeout,deps.renderPagedChat);},
    fire:()=>{const [id,t]=timers.entries().next().value;timers.delete(id);now+=t.ms;t.fn();}};
}
for(const status of ['pending','reset_required']) {
 const h=rig();h.queue.push({status,retry_after_ms:350},{ok:true,status:'acknowledged'});
 h.markReadNow('room');await tick();
 assert.equal(h.calls.length,1);assert.equal(h.timers.size,1);
 assert.equal([...h.timers.values()][0].ms,350);
 assert.equal(h.owner.readAck.failures,0);assert.equal(h.owner.readAck.progressRetries,1);
 assert.equal(h.owner.readAck.lastSuccess,undefined);assert.equal(h.Mesh.pendingRead,'room');
 assert.equal(h.Mesh.state.chats[0].unread,5);assert.equal(h.Mesh.state.chats[0].forced_unread,true);
 h.markReadNow('room');assert.equal(h.timers.size,1);assert.equal(h.calls.length,1);
 h.fire();await tick();
 assert.deepEqual(h.log.slice(0,4),['post:old-token','get','paint','post:fresh-token']);
 assert.equal(h.calls[1].data.page_version,'fresh-version');
 assert.equal(h.owner.readAck.lastSuccess,h.owner.visibleReadNs);
 assert.equal(h.owner.readAck.progressRetries,0);assert.equal(h.timers.size,0);
}
{
 const h=rig();h.queue.push({status:'pending',retry_after_ms:350},{ok:true,status:'acknowledged'});
 h.markReadNow('room');await tick();h.owner.busy=true;h.fire();await tick();
 assert.equal(h.calls.length,1);assert.equal(h.owner.refreshDirty,undefined);
 assert.equal(h.timers.size,1);assert.equal([...h.timers.values()][0].ms,350);
 h.finishBusy();assert.equal(h.timers.size,1);
 h.fire();await tick();assert.deepEqual(h.log.slice(0,4),['post:old-token','get','paint','post:fresh-token']);
 assert.equal(h.owner.readAck.lastSuccess,h.owner.visibleReadNs);
}
for(const gate of ['manual','reset','route','session','focus']) {
 const h=rig();h.queue.push({status:'pending',retry_after_ms:350});
 h.markReadNow('room');await tick();h.owner.busy=true;h.fire();await tick();
 assert.equal(h.timers.size,1);assert.equal(h.owner.refreshDirty,undefined);
 if(gate==='manual')h.events.get('ab:manual-mark-unread')({detail:{chatId:'room'}});
 else if(gate==='reset')h.resetPagedView();
 else if(gate==='route')h.setOwner({...h.owner});
 else if(gate==='session')h.setCurrent(false);
 else h.setFocused(false);
 h.finishBusy();if(h.timers.size)h.fire();await tick();
 assert.equal(h.calls.length,1);assert(!h.log.includes('get'));assert.equal(h.timers.size,0);
}
{
 const h=rig();h.queue.push({status:'pending',retry_after_ms:350});
 h.markReadNow('room');await tick();h.owner.busy=true;
 h.owner.refreshDirty=true;h.owner.refreshOptions={realtime:true,sidebar:true};
 const options=h.owner.refreshOptions;
 h.fire();await tick();
 assert.equal(h.owner.refreshDirty,true);assert.equal(h.owner.refreshOptions,options);
 h.events.get('ab:manual-mark-unread')({detail:{chatId:'room'}});
 assert.equal(h.timers.size,0);assert.equal(h.owner.refreshDirty,true);
 assert.equal(h.owner.refreshOptions,options);assert.equal(h.calls.length,1);
}
for(const gate of ['route','session','lock','manual','reset']) {
 const h=rig();h.queue.push({status:'pending',retry_after_ms:350});h.markReadNow('room');await tick();
 if(gate==='route')h.setOwner({...h.owner});
 else if(gate==='manual')h.events.get('ab:manual-mark-unread')({detail:{chatId:'room'}});
 else if(gate==='reset')h.resetPagedView();
 else h.setCurrent(false);
 if(h.timers.size)h.fire();await tick();
 assert.equal(h.calls.length,1);assert(!h.log.includes('get'));
 if(gate==='manual'||gate==='reset')assert.equal(h.timers.size,0);
}
for(const gate of ['focus','scroll','browsing']) {
 const h=rig();h.queue.push({status:'pending',retry_after_ms:350});h.markReadNow('room');await tick();
 if(gate==='focus')h.setFocused(false);else if(gate==='scroll')h.transcript.scrollTop=0;else h.owner.browsing=true;
 h.fire();await tick();assert.equal(h.calls.length,1);assert.equal(h.Mesh.pendingRead,'room');
}
{
 const h=rig();h.queue.push(new Error('offline'));h.markReadNow('room');await tick();
 assert.equal(h.owner.readAck.failures,1);assert.equal(h.owner.readAck.nextAt,2000);assert.equal(h.timers.size,0);
}
{
 const h=rig();for(let n=0;n<7;n++)h.queue.push({status:'pending',retry_after_ms:350});
 h.markReadNow('room');await tick();
 for(let n=1;n<=6;n++) {
   assert.equal([...h.timers.values()][0].ms,n<=5?350:2000);
   h.fire();await tick();
 }
 assert.equal(h.calls.length,7);assert.equal(h.owner.readAck.failures,0);
 assert.equal([...h.timers.values()][0].ms,2000);
 assert.equal(h.Mesh.pendingRead,'room');
}
''')
