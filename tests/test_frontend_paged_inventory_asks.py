"""Incomplete bounded sidebar/ask lanes retain same-session display rows."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_incomplete_sidebar_inventory_retains_only_same_session_rows(tmp_path):
    source = (ROOT / 'gui/static/js/state.js').read_text(encoding='utf-8')
    body = source[source.index('export function applyMeshState('):
                  source.index('// agent reply-rule vocabulary', source.index('export function applyMeshState('))]
    body = body.replace('export function ', 'function ')
    script = r'''
import assert from 'node:assert/strict';
const binding=(viewer='alice', generation='1')=>({instance_id:'app',session_generation:generation,viewer});
const Mesh={state:null};
const build=new Function('Mesh','viewReadMayApply','advanceWarmCounter','monotonicNow',
  `let appliedMeshReadSequence=0,meshStateGeneration=0,meshStateAcceptedAt=null;
   ${__BODY__};return applyMeshState;`);
const apply=build(Mesh,()=>true,n=>n+1,()=>0);
const ticket={epoch:1};
let sequence=0;
const receive=(response)=>apply(ticket,response,{session:{epoch:1},readSequence:++sequence});
const initial={user:'alice',session_binding:binding(),users:{bob:{display:'Bob'}},
  chats:[{id:'room',name:'Room'}],users_complete:true,chats_complete:true};
assert.equal(receive(initial),true);
let next={user:'alice',session_binding:binding(),users:{},chats:[],
  users_complete:false,chats_complete:false};
assert.equal(receive(next),true);
assert.deepEqual(Mesh.state.users,{bob:{display:'Bob'}});
assert.deepEqual(Mesh.state.chats,[{id:'room',name:'Room'}]);
assert.equal(Mesh.state.chats_complete,false); // Explicit incomplete UI hint.
next={...next,users:{bob:{display:'Updated'},carol:{display:'Carol'}},
  chats:[{id:'room',name:'Updated room'},{id:'other',name:'Other'}]};
receive(next);
assert.deepEqual(Mesh.state.users,{bob:{display:'Updated'},carol:{display:'Carol'}});
assert.deepEqual(Mesh.state.chats,[{id:'room',name:'Updated room'},
  {id:'other',name:'Other'}]);
receive({...next,users:{},chats:[],users_complete:true,chats_complete:true});
assert.deepEqual(Mesh.state.users,{});
assert.deepEqual(Mesh.state.chats,[]); // Complete absence is authoritative.
receive(initial);
receive({...next,user:'other',session_binding:binding('other','2'),users:{},chats:[]});
assert.deepEqual(Mesh.state.users,{});
assert.deepEqual(Mesh.state.chats,[]); // Never carry display across sessions.
'''.replace('__BODY__', json.dumps(body))
    path = tmp_path / 'inventory.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_ask_poll_serializes_and_holds_incomplete_verified_lanes(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    body = source[source.index('let askPollRequest = null;'):
                  source.index('function renderAskBar(', source.index('let askPollRequest = null;'))]
    script = r'''
import assert from 'node:assert/strict';
const binding={instance_id:'app',session_generation:'1',viewer:'alice'};
const Mesh={chatId:'room',askPollId:null,askKey:'',askCounts:{}};
const App={page:'chats'};
const events={},requests=[],dots=[],bars=[],notified=[];
let tick;
const document={hidden:false,hasFocus:()=>true,
  addEventListener:(name,fn)=>{events[name]=fn;}};
const api=(_path,_body,options)=>new Promise(resolve=>requests.push({resolve,options}));
const deps={Mesh,App,document,api,$:()=>({innerHTML:''}),
  captureSessionEpoch:()=>({epoch:1}),meshStateSnapshot:()=>({lockEpoch:0}),
  meshCaps:()=>({chat_page_v1:true}),BrowserSession:{snapshot:()=>({binding})},
  sessionMayApply:()=>true,samePageBinding:(a,b)=>JSON.stringify(a)===JSON.stringify(b),
  syncAskDots:asks=>dots.push(asks.map(a=>a.id)),
  renderAskBar:(chat,asks,timers)=>bars.push({chat,asks:asks.map(a=>a.id),
                                           timers:timers.map(t=>t.id)}),
  notifyAsk:a=>notified.push(a.id),
  setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{},
};
const build=new Function(...Object.keys(deps),
  `${__BODY__};return {startAskPoll,resetAskPollState};`);
const {startAskPoll,resetAskPollState}=build(...Object.values(deps));
const response=(extra={})=>({ok:true,asks:[
  {id:'r1',chat_id:'room',kind:'tool'},
  {id:'p1',chat_id:'',kind:'peer'}],
  timers:[{id:'t1',chat_id:'room'}],
  asks_complete:true,rooms_complete:true,peer_complete:true,
  timers_complete:true,resolved_room_ids:['room'],forbidden:false,
  session_binding:binding,...extra});
startAskPoll();
assert.equal(requests.length,1);
tick();assert.equal(requests.length,1); // One in-flight request, no overlap.
requests.shift().resolve(response());
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(bars.at(-1),{chat:'room',asks:['r1','p1'],timers:['t1']});
assert.deepEqual(notified,['r1','p1']);

tick();
requests.shift().resolve(response({asks:[
  {id:'r2',chat_id:'room',kind:'tool'},
  {id:'p2',chat_id:'',kind:'peer'}],
  timers:[{id:'t2',chat_id:'room'}],
  asks_complete:false,rooms_complete:false,peer_complete:true,
  timers_complete:false}));
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(bars.at(-1),{chat:'room',asks:['r2','p2'],timers:['t1','t2']});
assert.deepEqual(dots.at(-1),['r2','p2']);

// Another global room pending leaves r2 retained, but an exact scoped
// forbidden result for the already-known selected room removes it now.
tick();
requests.shift().resolve(response({asks:[{id:'p2',chat_id:'',kind:'peer'}],
  timers:[],rooms_complete:false,asks_complete:false,resolved_room_ids:[],
  timers_complete:false}));
await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.length,1); // One scoped known-room check, not another global scan.
tick();assert.equal(requests.length,1); // Both phases share the in-flight owner.
requests.shift().resolve(response({forbidden:true,asks:[],timers:[],
  resolved_room_ids:[]}));
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(dots.at(-1),['p2']);
assert.deepEqual(bars.at(-1).timers,[]);

tick();
requests.shift().resolve(response({forbidden:true,asks:[],timers:[],
  resolved_room_ids:[]}));
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(dots.at(-1),[]);
assert.equal(Mesh.askKey,'');

tick();const stale=requests.shift();
events['ab:session-reset']();
assert.equal(stale.options.signal.aborted,true);
stale.resolve(response());
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(dots.at(-1),[]);
'''.replace('__BODY__', json.dumps(body))
    path = tmp_path / 'asks.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
