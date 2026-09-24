"""Auxiliary paint is separately fenced and never invokes legacy hot reads."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_aux_response_stale_guards_and_paged_captured_inputs(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    start = source.index('function samePageBinding(')
    end = source.index('async function refreshPagedSidebar(', start)
    abort = source[source.index('function abortPagedAux(owner)'):
                   source.index('function resetPagedView()', source.index('function abortPagedAux(owner)'))]
    helper = abort + '\n' + source[start:end]
    agent = source[source.index('function agentPermissionEntry('):
                   source.index('async function renderMeshChat(', source.index('function agentPermissionEntry('))]
    reads_start = source.index('const [feedData, runtimeData] =')
    reads = source[reads_start:source.index('if (prepared?.guard', reads_start)]
    authority_start = source.index('const authorityRuns =', reads_start)
    authority = source[authority_start:source.index(';', authority_start) + 1]
    pause_start = source.index('const down = data._paged ? !b._paused')
    pause_choice = source[pause_start:source.index(';', pause_start) + 1]
    script = r'''
import assert from 'node:assert/strict';
const source = __HELPER__ + '\n' + __AGENT__;
const binding={instance_id:'app',session_generation:'7',viewer:'alice'};
const owner={chatId:'room',pageRevision:1,pageVersion:'v1',auxAbort:null,
  current:()=>true};
const pageData={me:'alice',meta:{id:'room',members:['alice','bot']},
  messages:[{id:'m',body:'canonical'}],session_binding:binding,
  metadata_status:{pause:'deferred',receipts:'ready'}};
const Mesh={state:{user:'alice',users:{alice:{kind:'human'}}},
  agentsView:false,agentsFromComposer:false};
const ICONS={pause:'P',hand:'H'};
const calls=[],paints=[],restores=[];
let current=owner;
let resets=0,forceReset=()=>{};
const content={innerHTML:'canonical'};
const location={hash:''};
const pause={disabled:true,innerHTML:'pending'};
const title={badge:null,querySelector(){return this.badge;},
  appendChild(node){this.badge=node;}};
const pill={button:null,firstElementChild:null,
  insertBefore(node){this.button=node;}};
const tr={id:'transcript'};
const $=selector=>({"#chat-menu [data-act='pause']":pause,
  '#chat-top .chat-head-name':title,'#composer-pill':pill,
  '#transcript':tr,'#agents-perm-btn':pill.button,
  '#content':content,
  '#chat-top .chat-head-name .agent-pause-tag':title.badge})[selector] || null;
const document={createElement(tag){return {tag,id:'',disabled:false,
  innerHTML:'',className:'',textContent:'',
  addEventListener(){},remove(){if(tag==='span') title.badge=null;
    else pill.button=null;}};}};
const api=(_path,_body,options)=>new Promise(resolve=>{
  calls.push({resolve,options});
});
const deps={Mesh,ICONS,$,document,api,location,
  resetPagedView:()=>{resets++;forceReset();},
  captureTranscriptAnchor:()=>({candidates:[{id:'m',offset:1}]}),
  restoreTranscriptAnchor:()=>restores.push('restored'),
  renderMeshChat:async(_force,_trace,prepared)=>{
    paints.push(prepared);return true;},
};
const build=new Function(...Object.keys(deps),
  `let pageOwner=arguments[arguments.length-1];${source};
   return {refreshPagedAux,abortPagedAux,syncPagedAuxControls,pagedAuxDisplay,
           setOwner:o=>pageOwner=o};`);
const {refreshPagedAux,abortPagedAux,syncPagedAuxControls,pagedAuxDisplay,setOwner}=
  build(...Object.values(deps),owner);
forceReset=()=>{abortPagedAux(owner);setOwner(null);};
const response=(extra={})=>({status:'ready',chat_id:'room',page_version:'v1',
  session_binding:{...binding},metadata_status:{live:'ready',runtime:'ready',
    pause:'ready',profiles:'ready',presence:'pending'},
  agents_paused:true,feeds:[{agent:'bot'}],tasks:[{id:'task'}],runs:[{run_id:'run'}],
  users:{bot:{kind:'agent',owners:['alice']}},...extra});

// An old response cannot repaint after another canonical page revision.
let pending=refreshPagedAux(owner,1,'v1',pageData);
assert.equal(calls.length,1);
owner.pageRevision=2;
calls.shift().resolve(response());await pending;
assert.equal(paints.length,0);

// Exact session and raw/trust version both fence the companion handout.
for(const altered of [
  response({page_version:'v2'}),
  response({session_binding:{...binding,viewer:'other'}}),
  response({chat_id:'elsewhere'}),
  {status:'pending'},
]) {
  pending=refreshPagedAux(owner,2,'v1',pageData);
  calls.shift().resolve(altered);await pending;
  assert.equal(paints.length,0);
}

pending=refreshPagedAux(owner,2,'v1',pageData);
calls.shift().resolve(response({status:'forbidden'}));await pending;
assert.equal(resets,1);
assert.equal(location.hash,'#/chats');
assert.equal(content.innerHTML,'');
assert.equal(paints.length,0);
setOwner(owner);

// Route/session reset aborts in-flight work and ignores its later response.
pending=refreshPagedAux(owner,2,'v1',pageData);
const stale=calls.shift();
abortPagedAux(owner);
assert.equal(stale.options.signal.aborted,true);
stale.resolve(response());await pending;
assert.equal(paints.length,0);
setOwner(null);
pending=refreshPagedAux(owner,2,'v1',pageData);
calls.shift().resolve(response());await pending;
assert.equal(paints.length,0);
setOwner(owner);

// A same-owner completed companion overlays selected users, pause and compact
// captured cards on the canonical rows, preserving viewport by row anchor.
pending=refreshPagedAux(owner,2,'v1',pageData);
calls.shift().resolve(response());await pending;
assert.equal(paints.length,1);
const supplied=paints[0];
assert.equal(supplied.warmBase,true);
assert.equal(supplied.paged,true);
assert.equal(supplied.historyRead,true); // never mark-read from aux paint
assert.deepEqual(supplied.aux,{feeds:[{agent:'bot'}],tasks:[{id:'task'}],
  runs:[{run_id:'run'}]});
assert.equal(supplied.data.messages,pageData.messages);
assert.equal(supplied.presentation.users.bot.kind,'agent');
assert.equal(supplied.data.meta.agents_paused,true);
assert.equal(pageData.meta.agents_paused,true);
assert.equal(pause.disabled,false);
assert.ok(title.badge);
assert.equal(pill.button.disabled,false);
assert.deepEqual(restores,['restored']);
const retained=pagedAuxDisplay(pageData,response());
assert.deepEqual(retained.aux,{feeds:[{agent:'bot'}],tasks:[{id:'task'}],
  runs:[{run_id:'run'}]});
assert.equal(retained.presentation.users.bot.owners[0],'alice');
assert.equal(retained.data.meta.agents_paused,true);
const neutral=pagedAuxDisplay(pageData,null);
assert.equal(neutral.aux,null);
assert.equal(neutral.data.metadata_status.pause,pageData.metadata_status.pause);

// Two unchanged canonical page polls can replace the pageData object without
// rebuilding the menu. The live button property, not an old meta closure,
// chooses Resume and then Stand down; pending disables it again.
const pageData2={...pageData,meta:{id:'room',members:['alice','bot'],
  agents_paused:false},metadata_status:{pause:'pending'}};
syncPagedAuxControls(pageData2,supplied.presentation,
  {pause:'ready'},response());
const decidePause=new Function('data','b','meta',
  __PAUSE__+';return down;');
assert.equal(decidePause({_paged:true},pause,{agents_paused:false}),false);
pause._paused=false;
assert.equal(decidePause({_paged:true},pause,{agents_paused:true}),true);
syncPagedAuxControls(pageData2,supplied.presentation,
  {pause:'pending'},{});
assert.equal(pause.disabled,true);
assert.equal(pause._paused,null);
assert.equal(title.badge,null); // Pending never leaves a false ready badge.

// Paged render consumes supplied arrays even if warmBase is false; it must
// never invoke livefeed/runtime_tasks/currentRunAuthority fallbacks.
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const extract=new AsyncFunction('prepared','chatId','api','currentRunAuthority',
  __READS__ + ';const feeds=feedData.feeds||[];' + __AUTHORITY__ +
  ';return {feedData,runtimeData,authorityRuns};');
const noLegacy=()=>{throw Error('legacy runtime read');};
const compact=await extract({paged:true,warmBase:false,
  aux:{feeds:[{agent:'bot'}],tasks:[{id:'task'}],runs:[{run_id:'run'}]}},
  'room',noLegacy,noLegacy);
assert.deepEqual(compact.feedData,{feeds:[{agent:'bot'}]});
assert.deepEqual(compact.runtimeData,{tasks:[{id:'task'}]});
assert.deepEqual(compact.authorityRuns,[{run_id:'run'}]);
'''
    script = (script.replace('__HELPER__', json.dumps(helper))
             .replace('__AGENT__', json.dumps(agent))
             .replace('__READS__', json.dumps(reads))
             .replace('__AUTHORITY__', json.dumps(authority))
             .replace('__PAUSE__', json.dumps(pause_choice)))
    path = tmp_path / 'aux.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
