"""Route feedback follows the visible surface without skipping fresh reads."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_route_and_paged_feedback_keep_fresh_reads(tmp_path):
    main = (ROOT / "gui/static/js/main.js").read_text(encoding="utf-8")
    chat = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    route = main[main.index("function route() {"):main.index('\nwindow.addEventListener("hashchange", route);')]
    home_start = chat.index('  if (!Mesh.chatId && App.page === "chats" && !$("#content > .empty-state")')
    home = chat[home_start:chat.index("  // very first boot", home_start)]
    loading_start = chat.index("  const visibleRefresh =")
    loading = chat[loading_start:chat.index("  try {", loading_start)]
    program = r'''
import assert from 'node:assert/strict';
const routeSource = __ROUTE__, homeSource = __HOME__, loadingSource = __LOADING__;
function setup({chatId=null, rendered=null, transcript=false, state={available:true,user:'me'}, details=false}={}) {
  const classes = new Map(), content = {innerHTML:transcript?'transcript':'settings',dataset:{}};
  function node() {return {hidden:true,innerHTML:'',classList:{toggle:(key,on)=>classes.set(key,on)}};}
  const nodes = new Map(['#content','#side-chats','#details-pane','#side-new','#rail-chats','#rail-account'].map(id=>[id,node()]));
  content.classList=node().classList; nodes.set('#content',content);
  const $ = id => id === '#transcript' ? (content.innerHTML==='transcript'?{}:null)
    : id === '#content > .empty-state' ? (content.innerHTML==='home'?{}:null) : nodes.get(id);
  const App={page:'settings',routeSeq:3}, Mesh={chatId,renderedChat:rendered,detailsView:details,state,listKey:'empty'};
  const Settings={}, document={body:node()}, location={hash:''};
  let cues=0, reads=0, paints=0;
  const held=new Promise(()=>{}), beginLoading=()=>{cues++;return ()=>{};};
  const renderEmptyChat=()=>{content.innerHTML='home';paints++;};
  const paintHome=new Function('$','App','Mesh','renderEmptyChat',homeSource);
  const PAGES={chats:()=>{reads++;paintHome($,App,Mesh,renderEmptyChat);return held;},settings:()=>held,new:()=>held};
  const factory=new Function('$','App','Mesh','Settings','document','location','PAGES','beginLoading','endLoading',
    'resetSubviews','renderChrome','captureViewRead','viewReadMayApply','renderSidebar','syncSidebarSelection',
    `let sessionRoutePending=false; ${routeSource}; return route;`);
  const noop=()=>{};
  const route=factory($,App,Mesh,Settings,document,location,PAGES,beginLoading,noop,noop,noop,noop,()=>true,noop,noop);
  return {go(hash){location.hash=hash;route();},App,Mesh,content,classes,get cues(){return cues;},get reads(){return reads;},get paints(){return paints;}};
}
// Settings replaced an earlier home, but the render signature still says empty.
const home=setup();home.go('#/chats');
assert.equal(home.content.innerHTML,'home');assert.equal(home.paints,1);
assert.equal(home.cues,0);assert.equal(home.reads,1);assert.equal(home.App.routeSeq,4);
assert.equal(home.classes.get('chat-active'),false);
// Closing info retains the transcript but still advances the route/read owner.
const close=setup({chatId:'room',rendered:'room',transcript:true,details:true});
close.go('#/chats/room');assert.equal(close.cues,0);assert.equal(close.reads,1);
assert.equal(close.content.innerHTML,'transcript');assert.equal(close.classes.get('chat-active'),true);
// New rooms retain a content cue; empty-list startup gives ownership to sidebar.
const change=setup({chatId:'old',rendered:'old',transcript:true});change.go('#/chats/new');
assert.equal(change.cues,1);assert.equal(change.content.innerHTML,'<div class="chat-loading"></div>');
assert.equal(change.reads,1);
const cold=setup({state:null});cold.go('#/chats');assert.equal(cold.cues,0);assert.equal(cold.paints,0);
const info=setup({chatId:'room',rendered:'room',transcript:true});info.go('#/chats/room/details');
assert.equal(info.cues,1);assert.equal(info.reads,1);
// Paged feedback distinguishes an unchanged visible read from explicit history work.
let cues=0;
const choose=new Function('before','Mesh','chatId','mode','kind','$','owner','beginLoading',
  `${loadingSource}; return finishLoading;`);
function chooseCue({before={},rendered='room',mode='first',kind=null,pending=false}={}) {
  cues=0;choose(before,{renderedChat:rendered},'room',mode,kind,()=>({dataset:{pagePending:pending?'owner':null}}),
    {identity:'owner',current:()=>true},()=>{cues++;return ()=>{};});return cues;
}
assert.equal(chooseCue(),0);assert.equal(chooseCue({mode:'refresh'}),0);
assert.equal(chooseCue({before:null}),1);assert.equal(chooseCue({rendered:'other'}),1);
assert.equal(chooseCue({mode:'older',kind:'older'}),1);assert.equal(chooseCue({kind:'first'}),1);
assert.equal(chooseCue({before:null,pending:true}),0); // reset already owns a cue
'''
    for name, source in [("ROUTE", route), ("HOME", home), ("LOADING", loading)]:
        program = program.replace(f"__{name}__", json.dumps(source))
    script = tmp_path / "route-loading.mjs"
    script.write_text(program, encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
