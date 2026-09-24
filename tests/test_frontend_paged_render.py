"""Execute the active paged chat orchestrator with a disposable DOM seam."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_explicit_latest_scroll_anchor_and_exact_read_after_paint(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    render = source[source.index('async function renderPagedChat(force, kind = null)'):
                    source.index('let chatRenderSeq =', source.index('async function renderPagedChat(force, kind = null)'))]
    mark = source[source.index('function markReadNow(chatId)'):
                  source.index('// reading needs eyes:', source.index('function markReadNow(chatId)'))]
    runner = r'''
import assert from 'node:assert/strict';
const source = __RENDER__ + '\n' + __MARK__;
const binding = {instance_id:'app', session_generation:'1', viewer:'alice'};
const cutoff = '1790238834318311101';
const App = {page:'chats',routeSeq:3};
const Mesh = {chatId:'room',state:{user:'alice',chats:[
  {id:'room',last:{ns:60},unread:2,forced_unread:true},
]},select:{ids:new Set()},msgExpand:{},readTail:{},pendingRead:null};
let anchorCaptures=0,anchorRestores=0,prunes=0,paints=0,sidebar=0;
const calls=[], modes=[];
let paintAllowed=true;
let pageVersion='v1';
const auxDisplayCalls=[];
let pendingOlder=false, savedPlan=false;
let timerSeq=0;
const timers=[];
const schedule=(fn,delay)=>{const item={id:++timerSeq,fn,delay,cancelled:false};
  timers.push(item);return item.id;};
const cancel=id=>{for(const timer of timers)if(timer.id===id)timer.cancelled=true;};
const flushTimer=async()=>{const item=timers.find(timer=>!timer.cancelled&&!timer.ran);
  assert.ok(item,'expected a scheduled retry');item.ran=true;item.fn();
  await new Promise(resolve=>setImmediate(resolve));};
const elements = {};
const transcript = {scrollHeight:1000,scrollTop:100,clientHeight:200,
  addEventListener(type,fn) {this['on'+type]=fn;},
  before(el) {elements['#'+el.id]=el;},
};
const content = {dataset:{},innerHTML:''};
const pane = {hidden:true,innerHTML:''};
elements['#transcript']=transcript;
elements['#content']=content;
elements['#details-pane']=pane;
const $ = selector => elements[selector] || null;
const document = {
  hasFocus:()=>true,dispatchEvent(){},
  createElement:()=>({id:'',className:'',innerHTML:'',onclick:null}),
};
const pageRead = {
  reset(){},invalidate(){},refreshPlan:()=>savedPlan ?
    {windowAnchor:'frozen-prior',requestedMessages:1}:null,
  async read(mode) {
    modes.push(mode);
    if (mode==='older' && pendingOlder) {
      pendingOlder=false;savedPlan=true;
      return {status:'pending',reason:'local_inputs_pending',retry_after_ms:350};
    }
    if (mode==='refresh') savedPlan=false;
    return {status:'page', pageData:{me:'alice',read_cutoff_ns:cutoff,
      meta:{id:'room'},metadata_status:{}}, messages:[{id:'m',ns:60}],
      evictedIds:[],hasMore:true,pageVersion};
  },
};
const deps={App,Mesh,BrowserSession:{snapshot:()=>({binding})},
  captureSessionEpoch:()=>({id:'session'}),
  sessionMayApply:()=>true,meshStateSnapshot:()=>({lockEpoch:1}),
  pageRead,$,document,performance:{now:()=>1},
  beginLoading:()=>()=>{},endLoading:()=>{},
  captureTranscriptAnchor:()=>{anchorCaptures++;return {candidates:[]};},
  restoreTranscriptAnchor:()=>{anchorRestores++;},
  pruneTranscriptResources:()=>{prunes++;},
  abortPagedAux:()=>{},refreshPagedAux:async()=>{},
  pagedAuxDisplay:(pageData,response)=>{
    auxDisplayCalls.push(response);
    return {data:{...pageData,_paged:true},presentation:Mesh.state,
      aux:response ? response.aux : null};
  },
  syncPagedAuxControls:()=>{},syncDmHeaderPresence:()=>{},
  renderMeshChat:async()=>{paints++;return paintAllowed;},
  api:(path,body)=>{calls.push([path,body]);return Promise.resolve({ok:true});},
  renderSidebar:()=>{sidebar++;},meshCaps:()=>({chat_page_v1:true}),
  observeLockState:()=>{},CustomEvent:class{},location:{hash:''},
  V:{renderChatDetails:async()=>{}},
  refreshPagedSidebar:async()=>{},
  requestAnimationFrame:fn=>fn(),recordChatOpen:()=>{},
  setTimeout:schedule,clearTimeout:cancel,
};
const build = new Function(...Object.keys(deps),
  `let pageOwner=null,pageRetryTimer=null;
   function resetPagedView(){pageOwner=null;pageRead.invalidate();}
   ${source}
   return {renderPagedChat,markReadNow,getOwner:()=>pageOwner};`);
const {renderPagedChat,getOwner} = build(...Object.values(deps));

// Automatic first page while scrolled away from bottom preserves geometry
// and defers read even though the selected message was painted.
await renderPagedChat(false);
assert.deepEqual(modes,['first']);
assert.equal(anchorCaptures,1);
assert.equal(anchorRestores,1);
assert.equal(Mesh.pendingRead,'room');
assert.deepEqual(calls,[]);
assert.equal(prunes,1);

// Explicit Jump to latest ignores the old scroll anchor, moves to bottom,
// and drains pendingRead with the exact decimal cutoff after a paint.
transcript.scrollTop=100;
await renderPagedChat(false,'first');
assert.deepEqual(modes,['first','first']);
assert.equal(anchorCaptures,1);
assert.equal(anchorRestores,1);
assert.equal(transcript.scrollTop,transcript.scrollHeight);
assert.deepEqual(calls,[['/api/mesh/read',{chat_id:'room',up_to_ns:cutoff}]]);
assert.equal(Mesh.pendingRead,null);
assert.equal(sidebar,0); // Decimal cutoff never does optimistic Number math.

// A historical page preserves its position, does not advance the read cursor,
// and its scroll listener fetches older only at the top while idle.
transcript.scrollTop=300;
await renderPagedChat(false,'older');
assert.equal(getOwner().browsing,true);
assert.equal(anchorCaptures,2);
assert.equal(anchorRestores,2);
assert.equal(calls.length,1);
transcript.scrollTop=0;
transcript.onscroll();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(modes.at(-1),'older');
assert.equal(calls.length,1);

// A first-ever older fetch may be pending. Its visible data is cleared for
// freshness, but the saved opaque positioning plan makes retry a refresh,
// then the original older intent resumes rather than silently showing tail.
await renderPagedChat(false,'first');
pendingOlder=true;
await renderPagedChat(false,'older');
assert.equal(getOwner().browsing,true);
assert.equal(getOwner().wantOlder,true);
await flushTimer();
assert.deepEqual(modes.slice(-3),['first','older','refresh']);
assert.equal(getOwner().wantOlder,true);
await flushTimer();
assert.deepEqual(modes.slice(-3),['older','refresh','older']);
assert.equal(getOwner().wantOlder,false);
assert.equal(calls.length,1); // Historical recovery never advances the read.

// Explicit latest overrides a pending older intent and its recovery anchor.
pendingOlder=true;
await renderPagedChat(false,'older');
assert.equal(getOwner().wantOlder,true);
await renderPagedChat(false,'first');
assert.deepEqual(modes.slice(-2),['older','first']);
assert.equal(getOwner().wantOlder,false);
assert.equal(getOwner().recoveryAnchor,null);
assert.equal(timers.filter(timer=>!timer.cancelled&&!timer.ran).length,0);

// Same-version canonical polls reuse the last bounded decoration as display
// only. A new raw/trust version neutralizes it until another aux final cut.
getOwner().auxSnapshot={pageVersion:'v1',response:{aux:{feeds:[{agent:'bot'}]}}};
await renderPagedChat(false,'first');
assert.deepEqual(auxDisplayCalls.at(-1),{aux:{feeds:[{agent:'bot'}]}});
pageVersion='v2';
await renderPagedChat(false,'first');
assert.equal(getOwner().auxSnapshot,null);
assert.equal(auxDisplayCalls.at(-1),null);

// A superseded same-route paint must not prune, restore or mark anything.
paintAllowed=false;
const prior={paints,prunes,anchorRestores,calls:calls.length};
await renderPagedChat(false,'first');
assert.equal(paints,prior.paints+1);
assert.equal(prunes,prior.prunes);
assert.equal(anchorRestores,prior.anchorRestores);
assert.equal(calls.length,prior.calls);
'''.replace('__RENDER__', json.dumps(render)).replace('__MARK__', json.dumps(mark))
    path = tmp_path / 'paged-render.mjs'
    path.write_text(runner, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
