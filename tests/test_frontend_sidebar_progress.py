"""One delayed chat-shaped sidebar cue with stable ownership and quiet background work."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_sidebar_progress_lifetime_policy_and_reversal(tmp_path):
    helper = (ROOT / "gui/static/js/sidebar_progress.js").read_text()
    helper = helper.replace("export function ", "function ")
    sidebar = (ROOT / "gui/static/js/sidebar.js").read_text()
    start = sidebar.index("function renderChatListSidebar()")
    end = sidebar.index("  // the mutable pieces of a row", start)
    policy = sidebar[start:end] + "  return {waiting, progress};\n}\n"
    script = r'''
import assert from 'node:assert/strict';
let timers=new Map(), next=1, cover=false, listeners=new Map();
const setTimeout=(fn,ms)=>{const id=next++; timers.set(id,{fn,ms}); return id};
const clearTimeout=id=>timers.delete(id);
const tick=ms=>{const due=[...timers].filter(([,t])=>t.ms===ms);
  due.forEach(([id,t])=>{timers.delete(id);t.fn()})};
const classes=()=>{const values=new Set(); return {
  add:x=>values.add(x),remove:x=>values.delete(x),contains:x=>values.has(x)}};
const host={isConnected:true,children:[],classList:classes(),
  prepend(node){node.parentNode=this;this.children.unshift(node)}};
const document={querySelector:()=>cover?{}:null,
  addEventListener:(name,fn)=>listeners.set(name,fn),
  createElement:()=>({classList:classes(),parentNode:null,innerHTML:'',
    setAttribute(){},remove(){if(this.parentNode)
      this.parentNode.children=this.parentNode.children.filter(x=>x!==this);
      this.parentNode=null}})};
const funcs=new Function('document','setTimeout','clearTimeout',
  __HELPER__+'; return {syncSidebarProgress,clearSidebarProgress};')
  (document,setTimeout,clearTimeout);
const {syncSidebarProgress:sync,clearSidebarProgress:clear}=funcs;
let owned=true;
const pending=()=>sync(host,{pending:true,current:()=>owned});
pending(); pending(); assert.equal(timers.size,1,'poll does not reset 500ms delay');
tick(500); assert.equal(host.children.length,1);
assert.match(host.children[0].innerHTML,/Updating chats/);
assert.equal(host.children[0].parentNode,host);
sync(host,{pending:false,current:()=>owned});
assert.equal(host.children[0].classList.contains('is-leaving'),true);
assert.equal(host.classList.contains('sidebar-results-arrive'),true);
pending(); tick(200); assert.equal(host.children.length,1,'pending reversal cancels exit');
assert.equal(host.children[0].classList.contains('is-leaving'),false);
sync(host,{pending:false,current:()=>owned}); tick(200);
assert.equal(host.children.length,0); assert.equal(host.classList.contains('sidebar-results-arrive'),false);
sync(host,{limited:true,current:()=>owned});
assert.equal(host.children.length,1,'room limit is immediate');
assert.match(host.children[0].innerHTML,/Chat list limit reached/);
owned=false; sync(host,{limited:true,current:()=>owned});
assert.equal(host.children.length,0,'retired session clears even persistent limit');
owned=true; pending(); host.isConnected=false; tick(500);
assert.equal(host.children.length,0,'detached sidebar never paints');
host.isConnected=true; pending(); tick(500);
assert.equal(host.children.length,1); listeners.get('ab:session-reset')();
assert.equal(host.children.length,0,'session reset clears cue');
pending(); tick(500); listeners.get('ab:lock-epoch')();
assert.equal(host.children.length,0,'lock clears cue');
cover=true; pending(); tick(500); assert.equal(host.children.length,0);
clear(); cover=false;

const Mesh={state:null,chatId:null,showArchived:false};
const App={page:'chats'};
const calls=[];
const captureSessionEpoch=()=>({});
const sessionMayApply=()=>true;
const updateTitleBadge=()=>{};
const $=()=>host;
const syncSidebarProgress=(...args)=>calls.push(args[1]);
const policy=new Function('Mesh','App','$','updateTitleBadge','captureSessionEpoch',
  'sessionMayApply','syncSidebarProgress',__POLICY__+';return renderChatListSidebar;')
  (Mesh,App,$,updateTitleBadge,captureSessionEpoch,sessionMayApply,syncSidebarProgress);
const base={available:true,user:'me',chats_complete:false,chats:[{id:'a'}]};
Mesh.state=base; policy().progress();
assert.equal(calls.at(-1).pending,false,'usable list quiet during background paging');
Mesh.state={...base,chats:[]}; policy().progress();
assert.equal(calls.at(-1).pending,true,'empty incomplete list receives one cue');
Mesh.chatId='selected'; policy().progress();
assert.equal(calls.at(-1).pending,false,'foreground chat owns feedback');
Mesh.chatId=null; Mesh.state={...base,sidebar_status:'room_limit'};
policy().progress(); assert.equal(calls.at(-1).limited,true);
Mesh.state={...base,chats:[],chats_complete:true}; policy().progress();
assert.equal(calls.at(-1).pending,false,'complete empty state is not loading');
'''.replace("__HELPER__", json.dumps(helper)).replace("__POLICY__", json.dumps(policy))
    runner = tmp_path / "sidebar_progress.mjs"
    runner.write_text(script)
    run = subprocess.run([shutil.which("node"), str(runner)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_full_sidebar_rebuild_retains_departing_row_for_animation(tmp_path):
    source = (ROOT / "gui/static/js/sidebar.js").read_text()
    start = source.index('  box.classList.remove("ng-host");', source.index("function renderChatListSidebar()"))
    end = source.index('  box.dataset.mode = "list";', start)
    rebuilt = source[start:end]
    program = r'''
import assert from 'node:assert/strict';
const row={classList:{},parentNode:null}; let progressCalled=0;
const box={children:[row],style:{},classList:{remove(){}},
  querySelector:selector=>selector==='.sidebar-progress'?box.children.find(x=>x===row)||null:null,
  prepend(value){this.children.unshift(value);value.parentNode=this},
  set innerHTML(value){this.html=value;this.children=[]}};
const progress=()=>{progressCalled++}; const html='<div class="chat-row">new room</div>';
new Function('box','progress','html',__REBUILD__)(box,progress,html);
assert.equal(box.children[0],row,'same progress node survives list HTML replacement');
assert.equal(progressCalled,1);
'''.replace("__REBUILD__", json.dumps(rebuilt))
    runner = tmp_path / "sidebar_rebuild.mjs"
    runner.write_text(program)
    run = subprocess.run([shutil.which("node"), str(runner)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr

    css = (ROOT / "gui/static/style.css").read_text()
    motion = css[css.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert ".sidebar-progress.is-leaving { display:none; }" in motion
    assert (".sidebar-progress, .sidebar-progress.is-leaving, "
            ".sidebar-results-arrive > .chat-row { animation:none; }") in motion
