"""Diagnostics stay bounded, content-free and inert while disabled."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_diagnostics_redaction_bounds_and_disable(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'gui/static/js/diagnostics.js').read_text(encoding='utf-8')
    source = source.replace('export function ', 'function ')
    script = r'''
import assert from 'node:assert/strict';
const timers=new Map();let next=0, disconnects=0, requests=[];
const setTimeout=(fn,ms)=>{timers.set(++next,{fn,ms});return next};
const clearTimeout=id=>timers.delete(id);
const tick=async()=>{const [id,t]=[...timers].find(([,v])=>v.ms===1000)||[];
  if(t){timers.delete(id);await t.fn()}};
const listeners={};
const window={addEventListener:(name,fn)=>listeners[name]=fn};
const document={body:{},addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]};
const MutationObserver=class {observe(){} disconnect(){disconnects++}};
const fetch=async(path,options)=>{const body=JSON.parse(options.body);requests.push({path,body});
  return new Response(JSON.stringify({ok:true,accepted:body.events.length,dropped:0}))};
const build=new Function('window','document','MutationObserver','fetch','setTimeout',
  'clearTimeout','requestAnimationFrame','cancelAnimationFrame',__SOURCE__+
  ';return {diagnostic,configureDiagnostics};');
const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,()=>1,()=>{});
d.diagnostic('client_request',{route:'/api/mesh/chat_page?cursor=SECRET'});
assert.equal(timers.size,0,'disabled has no queue or timer');
d.configureDiagnostics(true);
for(let i=0;i<350;i++) d.diagnostic('client_request',{
  route:'/api/mesh/chat_page?cursor=SECRET',status:'password',body:'PRIVATE',
  error_type:'my_password',rows:i,reason:'secret_value',duration_ms:Infinity});
for(let i=0;i<6;i++) await tick();
const events=requests.flatMap(r=>r.body.events);
assert.equal(events.length,200,'client retains at most 200 records');
assert.equal(requests.length,4,'at most 50 records per batch');
assert.equal(events[0].rows,150);
assert.equal(events[0].route,'/api/mesh/chat_page');
assert.equal(events[0].status,'unknown');
assert.equal(events[0].error_type,'unknown');
assert.equal(events[0].reason,'unknown');
assert.equal(events[0].duration_ms,undefined);
assert.doesNotMatch(JSON.stringify(requests),/SECRET|PRIVATE|password|secret_value/);
d.diagnostic('client_error',{error_type:'Error'});
d.configureDiagnostics(false);
await tick();assert.equal(requests.length,4,'disable clears queued events');
listeners.error({message:'PRIVATE',filename:'SECRET'});
assert.equal(timers.size,0);assert.equal(disconnects,1);
'''.replace('__SOURCE__', json.dumps(source))
    runner = tmp_path / 'diagnostics.mjs'
    runner.write_text(script, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(runner)], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_upload_receipts_loss_deadline_and_generation(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'gui/static/js/diagnostics.js').read_text(encoding='utf-8')
    source = source.replace('export function ', 'function ')
    script = r'''
import assert from 'node:assert/strict';
const secret='PRIVATE_RECEIPT_ERROR';
const receipt=(accepted,dropped=0)=>new Response(JSON.stringify({ok:true,accepted,dropped}));
function setup(upload) {
  let next=0;const timers=new Map(),requests=[];
  const setTimeout=(fn,ms)=>{timers.set(++next,{fn,ms});return next};
  const clearTimeout=id=>timers.delete(id);
  const tick=async(ms=1000)=>{const [id,t]=[...timers].find(([,v])=>v.ms===ms)||[];
    if(t){timers.delete(id);await t.fn()}};
  const window={addEventListener(){}};
  const document={body:{},addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]};
  const MutationObserver=class {observe(){}disconnect(){}};
  const fetch=(path,opts)=>{requests.push({events:JSON.parse(opts.body).events,signal:opts.signal});
    return upload(requests.length,requests.at(-1))};
  const build=new Function('window','document','MutationObserver','fetch','setTimeout','clearTimeout',
    'requestAnimationFrame','cancelAnimationFrame',__SOURCE__+
    ';return {diagnostic,configureDiagnostics};');
  const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,()=>1,()=>{});
  return {d,tick,timers,requests};
}
const cases=[
  ['accepted',()=>receipt(2),0],
  ['partial',()=>receipt(1,1),1],
  ['rejected collector',()=>receipt(0,2),2],
  ['oversize application rejection',()=>new Response(JSON.stringify({ok:true,accepted:0,dropped:true})),2],
  ['HTTP 403',()=>new Response(secret,{status:403}),2],
  ['HTTP 500',()=>new Response(secret,{status:500}),2],
  ['application error',()=>new Response(JSON.stringify({error:secret})),2],
  ['error with accepted count',()=>new Response(JSON.stringify({ok:true,accepted:2,dropped:0,error:secret})),2],
  ['malformed JSON',()=>new Response(secret),2],
  ['null JSON',()=>new Response('null'),2],
  ['missing acknowledgment',()=>new Response('{}'),2],
  ['out of range',()=>receipt(3,-1),2],
  ['fractional',()=>receipt(1.5,0.5),2],
  ['contradictory count',()=>receipt(2,1),2],
  ['string count',()=>receipt('2'),2],
  ['fetch rejection',()=>Promise.reject(new Error(secret)),2],
];
for (const [name,response,lost] of cases) {
  const h=setup(async n=>n===1 ? response() : receipt(1));
  h.d.configureDiagnostics(true);h.d.diagnostic('client_request',{route:'/api/state'});
  await h.tick();await h.tick();assert.equal(h.requests.length,1,name+': no replay');
  h.d.diagnostic('client_request',{route:'/api/state'});await h.tick();
  assert.equal(h.requests[1].events[0].client_dropped,lost,name);
  assert.doesNotMatch(JSON.stringify(h.requests),/PRIVATE_RECEIPT_ERROR/,name+': no raw receipt');
  assert.equal(h.timers.size,0,name+': deadline cleared');
}
let cancelled=0;
const oversized=setup(async n=>n===1 ? new Response(new ReadableStream({
  start(c){c.enqueue(new TextEncoder().encode(' '.repeat(4097)))},cancel(){cancelled++}
})) : receipt(1));
oversized.d.configureDiagnostics(true);await oversized.tick();
assert.equal(cancelled,1,'oversized streaming body cancelled');
oversized.d.diagnostic('route');await oversized.tick();
assert.equal(oversized.requests[1].events[0].client_dropped,1);

let bodyCancelled=0;
const stalled=setup(async n=>n===1 ? new Response(new ReadableStream({
  pull(){return new Promise(()=>{})},cancel(){bodyCancelled++}
})) : receipt(1));
stalled.d.configureDiagnostics(true);
const stalledFlush=stalled.tick();
await new Promise(resolve=>setImmediate(resolve));await stalled.tick(4000);await stalledFlush;
assert.equal(bodyCancelled,1,'deadline cancels stalled body');
assert.equal(stalled.requests[0].signal.aborted,true);
stalled.d.diagnostic('route');await stalled.tick();
assert.equal(stalled.requests[1].events[0].client_dropped,1,'deadline loss counted once');

let settleOld,settleNew;
const stale=setup(n=>n===1 ? new Promise(resolve=>{settleOld=resolve})
  : n===2 ? new Promise(resolve=>{settleNew=resolve}) : Promise.resolve(receipt(1)));
stale.d.configureDiagnostics(true);const oldFlush=stale.tick();
stale.d.configureDiagnostics(false);stale.d.configureDiagnostics(true);
assert.equal(stale.requests[0].signal.aborted,true);
await stale.tick();assert.equal(stale.requests.length,1,'one upload owner during stale completion');
settleOld(new Response(JSON.stringify({error:secret})));await oldFlush;
const newFlush=stale.tick();assert.equal(stale.requests.length,2);
assert.equal(stale.requests[1].events[0].client_dropped,0,'stale failure cannot contaminate new epoch');
stale.d.diagnostic('route');await stale.tick();
assert.equal(stale.requests.length,2,'new inflight upload retains ownership');
settleNew(new Response(secret,{status:503}));await newFlush;
stale.d.diagnostic('route');await stale.tick();
assert.equal(stale.requests.length,3,'new queue resumes without replay');
assert.equal(stale.requests[2].events.at(-1).client_dropped,1,'current failure counted exactly once');
assert.doesNotMatch(JSON.stringify(stale.requests),/PRIVATE_RECEIPT_ERROR/);
'''.replace('__SOURCE__', json.dumps(source))
    runner = tmp_path / 'upload-receipts.mjs'
    runner.write_text(script, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(runner)], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_canceled_transcript_frame_cannot_observe_or_clear_new_epoch(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'gui/static/js/diagnostics.js').read_text(encoding='utf-8')
    source = source.replace('export function ', 'function ')
    script = r'''
import assert from 'node:assert/strict';
let serial=0,rows=1;const timers=new Map(),frames=new Map(),listeners={},events=[];
const window={addEventListener:(name,fn)=>listeners[name]=fn};
const document={body:{},addEventListener:(name,fn)=>listeners[name]=fn,
 querySelector:()=>({children:{length:rows}}),querySelectorAll:()=>[]};
const MutationObserver=class {observe(){}disconnect(){}};
const setTimeout=(fn,ms)=>{timers.set(++serial,{fn,ms});return serial};
const clearTimeout=id=>timers.delete(id);
const raf=fn=>{frames.set(++serial,fn);return serial};
const fetch=async(p,o)=>{const batch=JSON.parse(o.body).events;events.push(...batch);
 return new Response(JSON.stringify({ok:true,accepted:batch.length,dropped:0}))};
const build=new Function('window','document','MutationObserver','fetch','setTimeout','clearTimeout',
 'requestAnimationFrame','cancelAnimationFrame',__SOURCE__+
 ';return {configureDiagnostics,observeTranscript,captureDiagnosticObservation,diagnosticObservationMayApply};');
const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,raf,id=>frames.delete(id));
const flush=async()=>{const found=[...timers].find(([,t])=>t.ms===1000);
 if(found){timers.delete(found[0]);await found[1].fn()}};
const disabledToken=d.captureDiagnosticObservation();assert.equal(disabledToken,null);
for(const edge of ['off_on','ab:session-reset','ab:lock-epoch','hashchange']) {
 d.configureDiagnostics(false);d.configureDiagnostics(true);
 const oldFrame=[...frames.values()][0],oldOwner=d.captureDiagnosticObservation();
 if(edge==='off_on'){d.configureDiagnostics(false);d.configureDiagnostics(true)}else listeners[edge]();
 assert.equal(d.diagnosticObservationMayApply(oldOwner),false);
 assert.equal(d.diagnosticObservationMayApply(disabledToken),false);
 rows++;d.observeTranscript();
 assert.equal(frames.size,1,'new context owns one frame');
 const fresh=[...frames.values()][0];events.length=0;
 oldFrame();await flush();
 assert.equal(events.filter(e=>e.event==='transcript_state').length,0,'canceled callback cannot log into new context');
 d.observeTranscript();assert.equal(frames.size,1,'old callback cannot clear new frame ownership');
 frames.clear();fresh();await flush();
 assert.equal(events.filter(e=>e.event==='transcript_state').length,1);
 assert.equal(events.find(e=>e.event==='transcript_state').rows,rows);
}
d.configureDiagnostics(false);assert.equal(timers.size,0);assert.equal(frames.size,0);
'''.replace('__SOURCE__', json.dumps(source))
    runner = tmp_path / 'transcript-frame-owner.mjs'
    runner.write_text(script, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(runner)], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
