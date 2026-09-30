"""One anticipatory older page per upward gesture; programmatic scrolls stay quiet."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_older_prefetch_direction_budget_owner_and_loader_region(tmp_path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    predicate = source[source.index("function shouldReadOlderPage("):
                       source.index("function samePageBinding(")]
    bind = source[source.index("    if (tr._pageScrollOwner !== owner) {"):
                  source.index('    let controls = $("#page-history-controls");')]
    loading = source[source.index("  const loadingHost ="):
                     source.index("  try {", source.index("  const loadingHost ="))]
    script = r'''
import assert from 'node:assert/strict';
const shouldReadOlderPage=new Function(__PREDICATE__+';return shouldReadOlderPage;')();
const tr={_pageHasMore:true,scrollTop:450,clientHeight:600};
const owner={busy:false,suppressOlderScroll:false};
assert.equal(shouldReadOlderPage(tr,700,owner),true,'one-viewport advance');
assert.equal(shouldReadOlderPage(tr,450,owner),false,'stationary does not drain');
assert.equal(shouldReadOlderPage(tr,400,owner),false,'downward does not drain');
tr.scrollTop=601; assert.equal(shouldReadOlderPage(tr,700,owner),false);
tr.clientHeight=2000;tr.scrollTop=800;
assert.equal(shouldReadOlderPage(tr,1000,owner),true,'threshold capped at 800px');
tr.scrollTop=801; assert.equal(shouldReadOlderPage(tr,1000,owner),false);
tr.scrollTop=40;owner.busy=true;assert.equal(shouldReadOlderPage(tr,100,owner),false);
owner.busy=false;owner.suppressOlderScroll=true;
assert.equal(shouldReadOlderPage(tr,100,owner),false,'anchor restoration suppressed');
owner.suppressOlderScroll=false;tr._pageHasMore=false;
assert.equal(shouldReadOlderPage(tr,100,owner),false,'exhausted history');

let older=0, marks=0, previous=null, removed=0;
tr._pageHasMore=true;tr.clientHeight=600;tr.scrollTop=450;
tr.addEventListener=(_type,fn)=>{tr.listener=fn};
tr.removeEventListener=(_type,fn)=>{assert.equal(fn,previous);removed++};
const Mesh={pendingRead:null},chatId='room',document={hasFocus:()=>true};
let active=owner; const pageOwner=owner;
const renderPagedChat=()=>{older++; owner.busy=true};
const markReadNow=()=>marks++;
owner.current=()=>active===owner;owner.lastScrollTop=700;
new Function('tr','owner','Mesh','chatId','document','pageOwner',
  'markReadNow','shouldReadOlderPage','renderPagedChat',__BIND__)
  (tr,owner,Mesh,chatId,document,pageOwner,markReadNow,shouldReadOlderPage,renderPagedChat);
tr.listener(); assert.equal(older,1); assert.equal(owner.lastScrollTop,450);
owner.busy=false;tr.listener(); assert.equal(older,1,'same position no second fetch');
previous=tr.listener;tr._pageScrollOwner={};
new Function('tr','owner','Mesh','chatId','document','pageOwner',
  'markReadNow','shouldReadOlderPage','renderPagedChat',__BIND__)
  (tr,owner,Mesh,chatId,document,pageOwner,markReadNow,shouldReadOlderPage,renderPagedChat);
assert.equal(removed,1,'new owner replaces old listener on reused DOM');
tr.scrollTop=300;active={};tr.listener();assert.equal(older,1,'retired owner cannot fetch');

const controls={id:'history'}, content={id:'content',dataset:{}};
const $=selector=>selector==='#page-history-controls'?controls:content;
let host;
const beginLoading=(node)=>{host=node;return ()=>{}};
const choose=new Function('mode','visibleRefresh','$','owner','beginLoading',
  __LOADING__+';return finishLoading;');
choose('older',false,$,{identity:'id',current:()=>true},beginLoading);
assert.equal(host,controls,'older cue belongs to history controls');
choose('first',false,$,{identity:'id',current:()=>true},beginLoading);
assert.equal(host,content);
'''
    for marker, snippet in (("__PREDICATE__", predicate), ("__BIND__", bind),
                            ("__LOADING__", loading)):
        script = script.replace(marker, json.dumps(snippet))
    runner = tmp_path / "older_prefetch.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(runner)], text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "    finishLoading();\n  }\n}" in source, "all older exits must settle the cue"


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_verified_bootstrap_home_paints_before_sidebar_await(tmp_path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    home = source[source.index("  const bootstrap = BrowserSession.snapshot();"):
                  source.index("  // very first boot", source.index("  const bootstrap = BrowserSession.snapshot();"))]
    script = r'''
import assert from 'node:assert/strict';
function probe({viewer='me',user='me',ready=true,mode='bound',restoring=false,
                locked=false,restart=false,chatId=null}={}) {
  let paints=0;
  const Mesh={state:null,chatId};const App={page:'chats',state:{user,restoring,app_lock:{locked}}};
  const BrowserSession={snapshot:()=>({mode,ready,binding:{viewer}})};
  const stateSnapshot={locked};const restartIntent=()=>restart;
  const $=()=>null;const renderEmptyChat=()=>paints++;
  new Function('Mesh','App','BrowserSession','stateSnapshot','restartIntent','$',
    'renderEmptyChat',__HOME__)(Mesh,App,BrowserSession,stateSnapshot,
      restartIntent,$,renderEmptyChat);
  return paints;
}
assert.equal(probe(),1);
for(const bad of [{viewer:'other'},{user:null},{ready:false},{mode:'legacy'},
                  {restoring:true},{locked:true},{restart:true},{chatId:'room'}])
  assert.equal(probe(bad),0,JSON.stringify(bad));
'''.replace("__HOME__", json.dumps(home))
    runner = tmp_path / "bootstrap_home.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(runner)], text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
