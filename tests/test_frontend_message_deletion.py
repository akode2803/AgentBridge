"""Deletion feedback must finish cleanly and remain in its originating session."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_deletion_feedback_lifetime_and_session_races(tmp_path):
    source = (ROOT / "gui/static/js/chat.js").read_text(encoding="utf-8")
    start = source.index("async function requestMessageDeletion(")
    source = source[start:source.index("// ---- clear chat", start)]
    runner = r'''
import assert from 'node:assert/strict';
const source = __SOURCE__;
function setup() {
  let generation=1, seq=0, refreshes=0, exits=0, live=null;
  const timers=new Map(), requests=[], toasts=[];
  let respond=()=>Promise.resolve({ok:true});
  const api=(path,body)=>{requests.push({path,body});return respond(path,body);};
  const toast=(message,opts)=>{
    const cue={message,opts};live=cue;toasts.push(cue);
    return ()=>{if(live===cue)live=null;};
  };
  const setTimeout=(fn,delay)=>{timers.set(++seq,{fn,delay});return seq;};
  const clearTimeout=id=>timers.delete(id);
  const Mesh={chatId:'room'};
  const factory=new Function('api','toast','setTimeout','clearTimeout','captureSessionEpoch','sessionMayApply',
    'Mesh','exitSelect','refreshChat','ICONS',`${source};return {deleteForMe,deleteForEveryone,hideSilently};`);
  const actions=factory(api,toast,setTimeout,clearTimeout,()=>generation,t=>t===generation,Mesh,
    ()=>exits++,()=>refreshes++,{trash:'trash'});
  return {...actions,requests,toasts,timers,toast,
    response(fn){respond=fn;},retire(){generation++;},
    tick(){const entries=[...timers];timers.clear();entries.forEach(([,t])=>{assert.equal(t.delay,500);t.fn();});},
    get live(){return live;},get refreshes(){return refreshes;},get exits(){return exits;}};
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
// A fast private delete preserves its canonical Undo; no timer can flash later.
const fast=setup();await fast.deleteForMe('room',['one']);
assert.equal(fast.toasts.length,1);assert.equal(fast.timers.size,0);
assert.equal(fast.live.opts.action,'Undo');await fast.live.opts.onAction();
assert.deepEqual(fast.requests.map(r=>r.path),['/api/mesh/delete_messages','/api/mesh/undelete_messages']);
assert.equal(fast.refreshes,2);
// Slow delete replaces progress with the correct result for all variants.
for (const [method,scope,undo] of [['deleteForMe','me',true],['deleteForEveryone','everyone',false],['hideSilently','me',false]]) {
  const s=setup(), pending=deferred();s.response(()=>pending.promise);
  const done=s[method]('room',['one','two']);assert.equal(s.live,null);s.tick();
  assert.equal(s.live.message,'Deleting…');assert.equal(s.live.opts.spinner,true);
  pending.resolve({ok:true});await done;
  assert.equal(s.live.message,`2 messages deleted for ${scope==='everyone'?'everyone':'me'}`);
  assert.equal(s.live.opts.action==='Undo',undo);assert.equal(s.timers.size,0);
}
// Both synchronous failure and async rejection end progress and its reveal timer.
for (const failure of ['throw','reject','api-error']) {
  const s=setup(), pending=deferred();
  s.response(()=>{if(failure==='throw')throw new Error('offline');return pending.promise;});
  const done=s.deleteForEveryone('room',['one']);
  if(failure!=='throw'){s.tick();if(failure==='reject')pending.reject(new Error('offline'));else pending.resolve({error:'Denied'});}
  await done;assert.equal(s.timers.size,0);assert.equal(s.refreshes,0);
  assert.equal(s.live.opts,true);assert.equal(s.live.message,failure==='api-error'?'Denied':'Could not delete messages. Please try again.');
}
// A request settling after session retirement cannot show completion or Undo.
for (const method of ['deleteForMe','deleteForEveryone','hideSilently']) {
  const s=setup(), pending=deferred();s.response(()=>pending.promise);
  const done=s[method]('room',['one']);s.retire();s.tick();pending.resolve({ok:true});await done;
  assert.equal(s.toasts.length,0);assert.equal(s.refreshes,0);assert.equal(s.timers.size,0);
}
// Session retirement queued between helper settlement and caller continuation.
for (const method of ['deleteForMe','deleteForEveryone','hideSilently']) {
  const s=setup(), pending=deferred();s.response(()=>pending.promise);
  const done=s[method]('room',['one']);pending.promise.then(()=>s.retire());pending.resolve({ok:true});await done;
  assert.equal(s.toasts.length,0,`${method} painted in its replacement session`);
  assert.equal(s.refreshes,0);assert.equal(s.timers.size,0);
}
// A previously offered private Undo cannot issue a request in a later session.
const oldUndo=setup();await oldUndo.deleteForMe('room',['one']);const undo=oldUndo.live.opts.onAction;
oldUndo.retire();await undo();assert.equal(oldUndo.requests.length,1);assert.equal(oldUndo.refreshes,1);
// Retiring after a visible slow cue dismisses only that operation's cue.
for(const unrelated of [false,true]) {
  const s=setup(),pending=deferred();s.response(()=>pending.promise);
  const done=s.deleteForMe('room',['one']);s.tick();assert.equal(s.live.message,'Deleting…');
  if(unrelated)s.toast('New session feedback',{});
  s.retire();pending.resolve({ok:true});await done;
  assert.equal(s.live?.message,unrelated?'New session feedback':undefined);
  assert.equal(s.refreshes,0);assert.equal(s.toasts.filter(t=>t.opts?.action==='Undo').length,0);
}
'''.replace("__SOURCE__", json.dumps(source))
    script = tmp_path / "delete-feedback.mjs"
    script.write_text(runner, encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_toast_dismiss_and_swap_are_owned_by_the_latest_cue(tmp_path):
    source = (ROOT / "gui/static/js/util.js").read_text(encoding="utf-8")
    source = source[source.index("let toastTimer = null;"):source.index("// the ≤1100px breakpoint")]
    source = source.replace("export function toast", "function toast")
    program = r'''
import assert from 'node:assert/strict';
let seq=0;const timers=new Map(),classes=new Set();
const t={hidden:true,innerHTML:'',className:'',style:{},offsetWidth:200,
  querySelector:()=>null,classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)}};
const $=selector=>{assert.equal(selector,'#toast');return t;};
const document={querySelector:()=>null,body:{classList:{contains:()=>false}}};
const esc=text=>text;
const setTimeout=(fn,delay)=>{timers.set(++seq,{fn,delay});return seq;};
const clearTimeout=id=>timers.delete(id);
function tickSwap(){const swap=[...timers].find(([,timer])=>timer.delay===170);assert.ok(swap);timers.delete(swap[0]);swap[1].fn();}
__SOURCE__
const dismissOld=toast('Deleting…',{spinner:true});assert.equal(t.hidden,false);
const dismissNew=toast('Complete',{check:true});dismissOld();assert.equal(t.hidden,false);
assert.match(t.innerHTML,/Complete/);dismissNew();assert.equal(t.hidden,true);assert.equal(timers.size,0);
toast('First',{spinner:true});const dismissSwap=toast('Delayed result',{swap:true});
dismissSwap();tickSwap();assert.equal(t.hidden,true);assert.doesNotMatch(t.innerHTML,/Delayed result/);
toast('Starting',{spinner:true});toast('Old swap',{swap:true});toast('Current',{check:true});
tickSwap();assert.match(t.innerHTML,/Current/);assert.doesNotMatch(t.innerHTML,/Old swap/);
// An older swap callback cannot remove animation state owned by a later swap.
toast('Base',{spinner:true});toast('Earlier swap',{swap:true});toast('Latest swap',{swap:true});
assert.equal(classes.has('toast-out'),true);tickSwap();assert.equal(classes.has('toast-out'),true);
tickSwap();assert.equal(classes.has('toast-out'),false);assert.match(t.innerHTML,/Latest swap/);
'''.replace("__SOURCE__", source)
    script = tmp_path / "toast-ownership.mjs"
    script.write_text(program, encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
