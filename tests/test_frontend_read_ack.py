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
const cutoff='1790238834318311101',nextCutoff='1790238834318311102';
const latestCutoff='1790238834318311103';
const makeOwner=(ns,token,version)=>({ready:true,chatId:'room',browsing:false,
  visibleReadNs:ns,visibleReadToken:token,visibleReadVersion:version,current:()=>true});
const factory=new Function('Mesh','App','meshCaps','$','api','Date',
  'captureViewRead','viewReadMayApply','refreshPagedSidebar','renderSidebar','document','owner',
  `let pageOwner=owner;
   ${__READ__};return {markReadNow,getOwner:()=>pageOwner,setOwner:o=>pageOwner=o};`);
const h=factory(Mesh,App,meshCaps,$,api,Date,captureViewRead,viewReadMayApply,
  refreshPagedSidebar,renderSidebar,document,makeOwner(cutoff,'a'.repeat(64),'v1'));
const settle=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()};
const assertPagedRequest=(index,token,version)=>{
  assert.equal(calls[index].path,'/api/mesh/chat_page_read');
  assert.deepEqual(calls[index].body,{
    chat_id:'room',page_version:version,read_ack_token:token});
};
h.markReadNow('room');h.markReadNow('room');
assert.equal(calls.length,1,'at most one request while held');
assertPagedRequest(0,'a'.repeat(64),'v1');
assert.equal(calls[0].options.timeoutMs,8000);
assert.equal(Mesh.pendingRead,'room');
assert.equal(Mesh.state.chats[0].unread,2);
assert.equal(Mesh.state.chats[0].forced_unread,true);
assert.equal(Mesh.readTail.room,undefined);
calls[0].reject(Error('network'));await settle();
assert.equal(Mesh.pendingRead,'room');
assert.equal(refreshes,0);
now=2999;h.markReadNow('room');assert.equal(calls.length,1,'bounded cooldown');
now=3000;h.markReadNow('room');assert.equal(calls.length,2);
calls[1].resolve({status:'pending'});await settle();
assert.equal(Mesh.pendingRead,'room');assert.equal(refreshes,0);
assert.equal(Mesh.state.chats[0].unread,2);
now=6999;h.markReadNow('room');assert.equal(calls.length,2,'second backoff doubles');
now=7000;h.markReadNow('room');assert.equal(calls.length,3);
assertPagedRequest(1,'a'.repeat(64),'v1');
assertPagedRequest(2,'a'.repeat(64),'v1');
calls[2].resolve({ok:true});await settle();
assert.equal(Mesh.pendingRead,null);assert.equal(refreshes,1);
assert.equal(h.getOwner().readAck.lastSuccess,cutoff);
assert.equal(Mesh.state.chats[0].unread,2,'paged badges require canonical sidebar data');
assert.equal(Mesh.state.chats[0].forced_unread,true);
assert.equal(Mesh.readTail.room,undefined);assert.equal(sidebar,0);
h.markReadNow('room');assert.equal(calls.length,3,'same cutoff not resent');

// Adjacent decimal cuts beyond Number precision must not be conflated. A new
// painted token/cutoff arriving during a held acknowledgement remains pending.
Object.assign(h.getOwner(),makeOwner(nextCutoff,'b'.repeat(64),'v2'));
h.markReadNow('room');
assert.equal(calls.length,4);
assertPagedRequest(3,'b'.repeat(64),'v2');
Object.assign(h.getOwner(),makeOwner(latestCutoff,'c'.repeat(64),'v3'));
calls[3].resolve({ok:true});await settle();
assert.equal(h.getOwner().readAck.lastSuccess,nextCutoff);
assert.equal(Mesh.pendingRead,'room');
h.markReadNow('room');assert.equal(calls.length,5);
assertPagedRequest(4,'c'.repeat(64),'v3');
const retired=h.getOwner();
const priorRefreshes=refreshes;
h.setOwner(makeOwner(latestCutoff,'d'.repeat(64),'v4'));
calls[4].resolve({ok:true});await settle();
assert.equal(Mesh.pendingRead,'room','retired response cannot settle new owner');
assert.equal(retired.readAck.inflight,false);
assert.equal(refreshes,priorRefreshes,'retired response cannot refresh the new sidebar');

// Legacy badge and optimistic readTail are not updated on failure.
paged=false; now=10000;
h.markReadNow('room'); assert.equal(calls.length,6);
assert.equal(calls[5].path,'/api/mesh/read');
assert.deepEqual(calls[5].body,{chat_id:'room'});
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
class CustomEvent {constructor(type,options){this.type=type;this.detail=options.detail}}
const Mesh={chatId:'room',pendingRead:null,state:{user:'me',chats:[
  {id:'room',last:{ns:9},unread:0,forced_unread:false}]}};
const App={page:'chats'}, tr={scrollHeight:100,scrollTop:0,clientHeight:100};
const owner={ready:true,chatId:'room',current:()=>true,browsing:false,visibleReadNs:'9',
  visibleReadToken:'a'.repeat(64),visibleReadVersion:'v1'};
const $=()=>tr, meshCaps=()=>({chat_page_v1:true});
const api=(path,body)=>{
  assert.equal(path,'/api/mesh/chat_page_read');
  assert.deepEqual(body,{chat_id:'room',page_version:'v1',read_ack_token:'a'.repeat(64)});
  let resolve;const promise=new Promise(done=>resolve=done);
  reads.push({resolve,body});return promise;
};
const build=new Function('document','Mesh','App','$','meshCaps','api',
  'refreshPagedSidebar','renderSidebar','Date','captureViewRead','viewReadMayApply','owner',
  `let pageOwner=owner;${__READ__};return {markReadNow,getOwner:()=>pageOwner};`);
const h=build(document,Mesh,App,$,meshCaps,api,()=>Promise.resolve(),()=>{},Date,
  ()=>({}),()=>true,owner);
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
