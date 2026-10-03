"""Read receipts settle only after acknowledgement, with bounded retry pressure."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_paged_and_legacy_read_ack_failure_backoff_and_stale_owner(tmp_path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    read = source[source.index("let legacyReadAck = null;"):
                  source.index("// reading needs eyes:", source.index("let legacyReadAck = null;"))]
    script = r'''
import assert from 'node:assert/strict';
let now=1000, paged=true, refreshes=0, sidebar=0, calls=[];
const Date={now:()=>now};
const document={addEventListener(){}};
const window={addEventListener(){}};
const tr={scrollHeight:1000,scrollTop:800,clientHeight:200};
const $=()=>tr;
const Mesh={chatId:'room',pendingRead:null,readTail:{},state:{chats:[
  {id:'room',last:{ns:9},unread:2,forced_unread:true}]}};
const App={page:'chats'};
const meshCaps=()=>({chat_page_v1:paged});
const api=(path,body,options)=>new Promise((resolve,reject)=>{
  calls.push({path,body,options,resolve,reject});
});
const refreshPagedSidebar=()=>{refreshes++;return Promise.resolve()};
const renderSidebar=()=>sidebar++;
let legacyOwner={}, routeCurrent=true;
const captureViewRead=()=>legacyOwner;
const viewReadMayApply=owner=>routeCurrent && owner===legacyOwner;
const factory=new Function('Mesh','App','meshCaps','$','api','Date',
  'captureViewRead','viewReadMayApply','refreshPagedSidebar','renderSidebar','document',
  'window','setTimeout','clearTimeout',
  `let pageOwner={ready:true,chatId:'room',browsing:false,visibleReadNs:'9',current:()=>true};
   ${__READ__};return {markReadNow,getOwner:()=>pageOwner,setOwner:o=>pageOwner=o};`);
const h=factory(Mesh,App,meshCaps,$,api,Date,captureViewRead,viewReadMayApply,
  refreshPagedSidebar,renderSidebar,document,window,()=>1,()=>{});
const settle=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()};
h.markReadNow('room');h.markReadNow('room');
assert.equal(calls.length,1,'at most one request while held');
assert.equal(calls[0].body.up_to_ns,'9');
assert.equal(calls[0].options.timeoutMs,8000);
assert.equal(Mesh.pendingRead,'room');
calls[0].reject(Error('network'));await settle();
assert.equal(Mesh.pendingRead,'room');
assert.equal(refreshes,0);
now=2999;h.markReadNow('room');assert.equal(calls.length,1,'bounded cooldown');
now=3000;h.markReadNow('room');assert.equal(calls.length,2);
calls[1].resolve({status:'pending'});await settle();
now=6999;h.markReadNow('room');assert.equal(calls.length,2,'second backoff doubles');
now=7000;h.markReadNow('room');assert.equal(calls.length,3);
calls[2].resolve({ok:true});await settle();
assert.equal(Mesh.pendingRead,null);assert.equal(refreshes,1);
h.markReadNow('room');assert.equal(calls.length,3,'same cutoff not resent');

// A new visible cutoff arriving during a held acknowledgement remains pending.
h.getOwner().visibleReadNs='10'; h.markReadNow('room');
assert.equal(calls.length,4);
h.getOwner().visibleReadNs='11';calls[3].resolve({ok:true});await settle();
assert.equal(Mesh.pendingRead,'room');
h.markReadNow('room');assert.equal(calls.length,5);
const retired=h.getOwner();
h.setOwner({ready:true,chatId:'room',browsing:false,visibleReadNs:'11',current:()=>true});
calls[4].resolve({ok:true});await settle();
assert.equal(Mesh.pendingRead,'room','retired response cannot settle new owner');
assert.equal(retired.readAck.inflight,false);

// Legacy badge and optimistic readTail are not updated on failure.
paged=false; now=10000;
h.markReadNow('room'); assert.equal(calls.length,6);
calls[5].reject(Error('network'));await settle();
assert.equal(Mesh.state.chats[0].unread,2);
assert.equal(Mesh.readTail.room,undefined);
now=12000;h.markReadNow('room');assert.equal(calls.length,7);
calls[6].resolve({ok:true});await settle();
assert.equal(Mesh.state.chats[0].unread,0);
assert.equal(Mesh.state.chats[0].forced_unread,false);
assert.equal(Mesh.readTail.room,9);assert.equal(sidebar,1);
'''.replace("__READ__", json.dumps(read))
    runner = tmp_path / "read_ack.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(runner)], text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_manual_unread_rearms_same_cutoff_only_after_successful_sidebar_write(tmp_path):
    chat = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    read = chat[chat.index("let legacyReadAck = null;"):
                chat.index("// reading needs eyes:", chat.index("let legacyReadAck = null;"))]
    sidebar = (ROOT / "gui/static/js/sidebar.js").read_text(encoding="utf-8")
    action = sidebar[sidebar.index("async function runChatAction("):
                     sidebar.index("async function refreshList(", sidebar.index("async function runChatAction("))]
    script = r'''
import assert from 'node:assert/strict';
let event, reads=[];
const document={addEventListener:(name,fn)=>{if(name==='ab:manual-mark-unread')event=fn},
  dispatchEvent:e=>event(e)};
const window={addEventListener(){}};
class CustomEvent {constructor(type,options){this.type=type;this.detail=options.detail}}
const Mesh={chatId:'room',pendingRead:null,state:{user:'me',chats:[
  {id:'room',last:{ns:9},unread:0,forced_unread:false}]}};
const App={page:'chats'}, tr={scrollHeight:100,scrollTop:0,clientHeight:100};
const owner={ready:true,chatId:'room',current:()=>true,browsing:false,visibleReadNs:'9'};
const $=()=>tr, meshCaps=()=>({chat_page_v1:true});
const api=(path,body)=>{if(path==='/api/mesh/read'){
  let resolve;const promise=new Promise(done=>resolve=done);
  reads.push({resolve,body});return promise;
}return Promise.resolve({ok:true})};
const build=new Function('document','Mesh','App','$','meshCaps','api',
  'refreshPagedSidebar','renderSidebar','Date','captureViewRead','viewReadMayApply','owner','window',
  `let pageOwner=owner;${__READ__};return {markReadNow,getOwner:()=>pageOwner};`);
const h=build(document,Mesh,App,$,meshCaps,api,()=>Promise.resolve(),()=>{},Date,
  ()=>({}),()=>true,owner,window);
const settle=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()};
h.markReadNow('room');reads[0].resolve({ok:true});await settle();
assert.equal(h.getOwner().readAck.lastSuccess,'9');
let marked=0, failManual=false, events=0;
const originalDispatch=document.dispatchEvent.bind(document);
document.dispatchEvent=e=>{events++;originalDispatch(e)};
const sidebarApi=async(path)=>{assert.equal(path,'/api/mesh/mark_unread');
  return failManual?{error:'denied'}:{ok:true}};
const actionFactory=new Function('Mesh','api','document','CustomEvent',
  'captureSessionEpoch','sessionMayApply','toast','refreshList','chatDisplay','V',
  __ACTION__+';return runChatAction;');
const run=actionFactory(Mesh,sidebarApi,document,CustomEvent,()=>({}),()=>true,
  ()=>{},async()=>{marked++;Mesh.state.chats[0].forced_unread=true;return true},
  ()=>'',{});
failManual=true;await run('unread',Mesh.state.chats[0]);
assert.equal(events,0,'failed manual write cannot invalidate ack');
h.markReadNow('room');assert.equal(reads.length,1);
failManual=false;await run('unread',Mesh.state.chats[0]);
assert.equal(events,1);assert.equal(marked,1);
assert.equal(h.getOwner().readAck.manualUnreadArmed,true);
assert.equal(Mesh.pendingRead,null,'menu write does not itself trigger a read');
h.markReadNow('room');assert.equal(reads.length,2,'same cutoff can read after gesture');
reads[1].resolve({ok:true});await settle();
assert.equal(h.getOwner().readAck.manualUnreadArmed,false);
assert.equal(h.getOwner().readAck.lastSuccess,'9');
Mesh.state.chats[0].forced_unread=false;
await run('unread',Mesh.state.chats[0]);
h.markReadNow('room');assert.equal(reads.length,3);
Mesh.state.chats[0].forced_unread=false;
await run('unread',Mesh.state.chats[0]);
reads[2].resolve({ok:true});await settle();
assert.equal(h.getOwner().readAck.manualUnreadArmed,true,
  'older acknowledgement cannot erase a newer manual unread intent');
assert.equal(h.getOwner().readAck.lastSuccess,undefined);
h.markReadNow('room');assert.equal(reads.length,4);
'''.replace("__READ__", json.dumps(read)).replace("__ACTION__", json.dumps(action))
    runner = tmp_path / "manual_unread.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(runner)], text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_legacy_read_retries_without_news_and_cancels_for_owner_or_manual_unread(tmp_path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    read = source[source.index("let legacyReadAck = null;"):
                  source.index("async function renderChats(force)",
                               source.index("let legacyReadAck = null;"))]
    script = r'''
import assert from 'node:assert/strict';
const settle=async()=>{for(let i=0;i<6;i++)await Promise.resolve()};
function setup() {
  let now=1000, focused=true, paged=false, nextTimer=0, sidebar=0;
  const timers=new Map(), listeners=new Map(), reads=[];
  const on=(name,fn)=>listeners.set(name,fn);
  const document={addEventListener:on,hasFocus:()=>focused};
  const window={addEventListener:on};
  const Mesh={chatId:'room',pendingRead:null,readTail:{},state:{user:'me',chats:[
    {id:'room',last:{ns:9},unread:2,forced_unread:true}]}};
  const App={page:'chats'};
  let identity={routeSeq:1,selectedViewGeneration:1,sessionEpoch:1,lockEpoch:1};
  const captureViewRead=()=>({...identity,chatId:Mesh.chatId,page:App.page});
  const viewReadMayApply=owner=>owner.chatId===Mesh.chatId && owner.page===App.page
    && Object.keys(identity).every(key=>identity[key]===owner[key]);
  const api=(path,body,options)=>new Promise((resolve,reject)=>{
    reads.push({path,body,options,resolve,reject});
  });
  const setTimeout=(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,at:now+delay});return id};
  const clearTimeout=id=>timers.delete(id);
  const build=new Function('Mesh','App','meshCaps','api','Date','captureViewRead',
    'viewReadMayApply','renderSidebar','document','window','setTimeout','clearTimeout',
    `let pageOwner=null;${__READ__};return {markReadNow,getAck:()=>legacyReadAck};`);
  const h=build(Mesh,App,()=>({chat_page_v1:paged}),api,{now:()=>now},captureViewRead,
    viewReadMayApply,()=>sidebar++,document,window,setTimeout,clearTimeout);
  return {...h,Mesh,App,reads,timers,identity,sidebar:()=>sidebar,
    emit:(name,detail)=>listeners.get(name)?.({detail}),
    focus:value=>focused=value, paged:value=>paged=value,
    advance:async(ms)=>{now+=ms;
      for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}
      await settle();
    }};
}

// No incoming message or focus transition is needed; both transport errors and
// non-ok responses retain unread until success. Pressure doubles to the cap.
{
  const h=setup();h.markReadNow('room');
  const delays=[2000,4000,8000,16000,32000,60000,60000];
  for(let i=0;i<delays.length;i++){
    if(i%2)h.reads[i].resolve({ok:false});else h.reads[i].reject(Error('offline'));
    await settle();
    assert.equal(h.timers.size,1,'one owned retry');
    assert.equal(h.Mesh.pendingRead,'room');
    assert.equal(h.Mesh.state.chats[0].unread,2);
    assert.equal(h.Mesh.readTail.room,undefined);
    await h.advance(delays[i]-1);assert.equal(h.reads.length,i+1);
    await h.advance(1);assert.equal(h.reads.length,i+2,'timer retries without a poll');
    assert.equal(h.timers.size,0,'no overlapping timer during request');
    h.markReadNow('room');assert.equal(h.reads.length,i+2,'inflight deduplication');
  }
  h.reads.at(-1).resolve({ok:true});await settle();
  assert.equal(h.getAck().failures,0);assert.equal(h.getAck().nextAt,0);
  assert.equal(h.Mesh.pendingRead,null);assert.equal(h.Mesh.readTail.room,9);
  assert.equal(h.Mesh.state.chats[0].unread,0);assert.equal(h.sidebar(),1);
  assert.equal(h.timers.size,0);await h.advance(120000);assert.equal(h.reads.length,8);
  h.markReadNow('room');assert.equal(h.reads.length,8,'success suppresses same cutoff');
}

// Losing focus leaves pending work for the existing focus listener. It does
// not acknowledge unseen messages or install a recurring background timer.
{
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  h.focus(false);await h.advance(2000);
  assert.equal(h.reads.length,1);assert.equal(h.timers.size,0);
  assert.equal(h.Mesh.pendingRead,'room');
  h.focus(true);h.emit('focus');assert.equal(h.reads.length,2);
  h.reads[1].resolve({ok:true});await settle();assert.equal(h.Mesh.pendingRead,null);
}

// Each part of selected-view ownership retires the retry. Returning to the
// same room still has a newer route ticket; the old closure cannot reclaim it.
for(const key of ['routeSeq','selectedViewGeneration','sessionEpoch','lockEpoch']){
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  h.identity[key]++;await h.advance(2000);
  assert.equal(h.reads.length,1,key);assert.equal(h.timers.size,0,key);
  h.markReadNow('room');assert.equal(h.reads.length,2,'new owner reads normally');
  h.reads[1].resolve({ok:true});await settle();
}
for(const event of ['hashchange','ab:session-reset','ab:lock-epoch']){
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  h.emit(event);assert.equal(h.timers.size,0,event);assert.equal(h.getAck(),null,event);
  await h.advance(60000);assert.equal(h.reads.length,1,event);
  // An already inflight old-owner failure must not schedule after cleanup.
  h.markReadNow('room');h.emit(event);h.reads[1].reject(Error('late'));await settle();
  assert.equal(h.timers.size,0,event);
  h.markReadNow('room');h.emit(event);h.reads[2].resolve({ok:true});await settle();
  assert.equal(h.Mesh.state.chats[0].unread,2,'retired success cannot settle badge');
  assert.equal(h.Mesh.readTail.room,undefined,event);
}
{
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  h.Mesh.chatId='other';h.identity.routeSeq++;h.emit('hashchange');
  h.Mesh.chatId='room';h.identity.routeSeq++;h.emit('hashchange');
  await h.advance(2000);assert.equal(h.reads.length,1,'away and back retires timer');
}
for(const change of [h=>h.App.page='settings',h=>h.Mesh.chatId='other',
                     h=>h.paged(true),h=>h.Mesh.pendingRead=null]){
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  change(h);await h.advance(2000);assert.equal(h.reads.length,1);
  assert.equal(h.timers.size,0);
}

// Manual unread cancels the old timer and invalidates any held response. Only
// a later existing read gesture may rearm the same cutoff.
{
  const h=setup();h.markReadNow('room');h.reads[0].reject(Error('offline'));await settle();
  h.emit('ab:manual-mark-unread',{chatId:'room'});
  assert.equal(h.timers.size,0);assert.equal(h.getAck().manualUnreadArmed,true);
  await h.advance(60000);assert.equal(h.reads.length,1,'manual unread is preserved');
  h.markReadNow('room');assert.equal(h.reads.length,2);
  h.emit('ab:manual-mark-unread',{chatId:'room'});
  h.reads[1].reject(Error('late'));await settle();
  assert.equal(h.timers.size,0,'retired version cannot schedule a retry');
  assert.equal(h.getAck().manualUnreadArmed,true);assert.equal(h.sidebar(),0);
  h.markReadNow('room');h.emit('ab:manual-mark-unread',{chatId:'room'});
  h.reads[2].resolve({ok:true});await settle();
  assert.equal(h.getAck().manualUnreadArmed,true,'retired success preserves unread intent');
  assert.equal(h.Mesh.state.chats[0].unread,2);assert.equal(h.sidebar(),0);
  h.markReadNow('room');h.reads[3].resolve({ok:true});await settle();
  assert.equal(h.Mesh.pendingRead,null);assert.equal(h.getAck().manualUnreadArmed,false);
}
'''.replace("__READ__", json.dumps(read))
    runner = tmp_path / "legacy_read_retry.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(runner)], text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
