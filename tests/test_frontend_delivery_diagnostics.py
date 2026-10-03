import json
import shutil
import subprocess
from pathlib import Path
import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_exact_dom_ack_cancellation_and_privacy(tmp_path):
    source = (
        (Path(__file__).resolve().parents[1] / "gui/static/js/diagnostics.js")
        .read_text()
        .replace("export function ", "function ")
    )
    script = r"""
import assert from 'node:assert/strict';
let now=1,next=0;const timers=new Map(),listeners={},requests=[];
const window={addEventListener:(n,f)=>listeners[n]=f};
const document={body:{},addEventListener:(n,f)=>listeners[n]=f,querySelector:()=>null,querySelectorAll:()=>[]};
const MutationObserver=class {observe(){}disconnect(){}};
const setTimeout=(f,ms)=>{timers.set(++next,{f,ms});return next};
const clearTimeout=id=>timers.delete(id);
const fetch=async(p,o)=>{const body=JSON.parse(o.body);requests.push(body);
  return new Response(JSON.stringify({ok:true,accepted:body.events.length,dropped:0}))};
const build=new Function('window','document','MutationObserver','fetch','setTimeout','clearTimeout',
 'requestAnimationFrame','cancelAnimationFrame','performance',SOURCE+
 ';return {configureDiagnostics,beginDiagnosticRequest,endDiagnosticRequest,canonicalDeliveryDom,acknowledgedDelivery,receivedDelivery};');
const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,()=>1,()=>{}, {now:()=>now});
const flush=async()=>{for(let i=0;i<15;i++){const [id,t]=[...timers].find(([,v])=>v.ms===1000)||[];if(!t)break;timers.delete(id);await t.f()}};
assert.equal(d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'}),null);
d.configureDiagnostics(true);
const ref=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});assert.match(ref,/^[0-9a-f]{16}$/);
now=10;d.endDiagnosticRequest(ref,'/api/mesh/post',{}, {id:'m-SECRET',ns:'9007199254740993123',_diagnostics:{trace_ref:'a'.repeat(16),chat_ref:'b'.repeat(16)}});
let pending=true;const row={dataset:{mid:'m-SECRET'},classList:{contains:name=>pending&&name==='pending-send'}};
const transcript={querySelectorAll:()=>[row]};
now=100;d.canonicalDeliveryDom('PRIVATE',[{id:'m-SECRET',body:'PASSWORD'}],transcript);
d.acknowledgedDelivery('PRIVATE','9007199254740993123');
await flush();let events=requests.flatMap(x=>x.events);
assert.equal(events.find(x=>x.phase==='browser_response'&&x.request_ref===ref).duration_ms,9);
assert.equal(events.filter(x=>x.phase==='canonical_dom').length,0,'optimistic echo excluded');
assert.equal(events.filter(x=>x.phase==='native_ack').length,0,'ack requires exact canonical DOM');
pending=false;now=1200;d.canonicalDeliveryDom('PRIVATE',[{id:'m-SECRET'}],transcript);
d.acknowledgedDelivery('PRIVATE','9007199254740993122');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='canonical_dom').length,1);
assert.equal(events.filter(x=>x.phase==='native_ack').length,0,'integer exact cutoff below message');
now=1300;d.acknowledgedDelivery('PRIVATE','9007199254740993123');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack').length,1);
const cancelled=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});listeners['ab:session-reset']();
d.endDiagnosticRequest(cancelled,'/api/mesh/post',{}, {id:'later-SECRET',ns:'100',_diagnostics:{trace_ref:'c'.repeat(16)}});
d.canonicalDeliveryDom('PRIVATE',[{id:'later-SECRET'}],transcript);await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='canonical_dom').length,1);
assert.ok(events.some(x=>x.phase==='abandoned'));
d.receivedDelivery({type:'message',id:'received-SECRET',ns:'123',chat_id:'PRIVATE',diagnostic_ref:'e'.repeat(16)});
const incoming={dataset:{mid:'received-SECRET'},classList:{contains:()=>false}};
d.canonicalDeliveryDom('PRIVATE',[{id:'received-SECRET'}],{querySelectorAll:()=>[incoming]});
d.acknowledgedDelivery('PRIVATE','123');await flush();
events=requests.flatMap(x=>x.events);assert.ok(events.some(x=>x.phase==='native_ack'&&x.flow==='received'));
d.receivedDelivery({type:'message',id:'m-9007199254740993123-SECRET',ns:Number('9007199254740993123'),chat_id:'PRIVATE',diagnostic_ref:'f'.repeat(16)});
const precise={dataset:{mid:'m-9007199254740993123-SECRET'},classList:{contains:()=>false}};
d.canonicalDeliveryDom('PRIVATE',[{id:'m-9007199254740993123-SECRET'}],{querySelectorAll:()=>[precise]});
d.acknowledgedDelivery('PRIVATE','9007199254740993122');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack'&&x.trace_ref==='f'.repeat(16)).length,0);
d.acknowledgedDelivery('PRIVATE','9007199254740993123');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack'&&x.trace_ref==='f'.repeat(16)).length,1);
d.receivedDelivery({type:'message',id:'m-9007199254740996-SECRET',ns:'9007199254740996',chat_id:'PRIVATE',diagnostic_ref:'2'.repeat(16)});
const unsafeRow={dataset:{mid:'m-9007199254740996-SECRET'},classList:{contains:()=>false}};
d.canonicalDeliveryDom('PRIVATE',[{id:unsafeRow.dataset.mid}],{querySelectorAll:()=>[unsafeRow]});
d.acknowledgedDelivery('PRIVATE',Number('9007199254740995'));await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack'&&x.trace_ref==='2'.repeat(16)).length,0,'unsafe rounded cutoff cannot prove coverage');
d.acknowledgedDelivery('PRIVATE','9007199254740995');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack'&&x.trace_ref==='2'.repeat(16)).length,0);
d.acknowledgedDelivery('PRIVATE','9007199254740996');await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.phase==='native_ack'&&x.trace_ref==='2'.repeat(16)).length,1);
now=3000;const early=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});
now=3100;d.receivedDelivery({type:'message',id:'m-333-SECRET',ns:333,chat_id:'PRIVATE',diagnostic_ref:'1'.repeat(16)});
const earlyRow={dataset:{mid:'m-333-SECRET'},classList:{contains:()=>false}};
now=3200;d.canonicalDeliveryDom('PRIVATE',[{id:'m-333-SECRET'}],{querySelectorAll:()=>[earlyRow]});
now=3300;d.acknowledgedDelivery('PRIVATE','333');
now=3400;d.endDiagnosticRequest(early,'/api/mesh/post',{}, {id:'m-333-SECRET',ns:333,_diagnostics:{trace_ref:'1'.repeat(16)}});
await flush();events=requests.flatMap(x=>x.events);
assert.equal(events.filter(x=>x.trace_ref==='1'.repeat(16)&&x.phase==='canonical_dom').length,1);
assert.equal(events.filter(x=>x.trace_ref==='1'.repeat(16)&&x.phase==='native_ack').length,1);
const reconciled=events.find(x=>x.trace_ref==='1'.repeat(16)&&x.phase==='send_reconciled');
assert.equal(reconciled.dom_delay_ms,200);assert.equal(reconciled.ack_delay_ms,300);
const late=d.beginDiagnosticRequest('/api/state');d.configureDiagnostics(false);d.configureDiagnostics(true);
d.endDiagnosticRequest(late,'/api/state',{},{});await flush();
events=requests.flatMap(x=>x.events);assert.equal(events.filter(x=>x.request_ref===late&&x.phase==='browser_response').length,0);
const disabled=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});d.configureDiagnostics(false);
d.endDiagnosticRequest(disabled,'/api/mesh/post',{}, {id:'late',ns:1,_diagnostics:{trace_ref:'d'.repeat(16)}});
await flush();assert.doesNotMatch(JSON.stringify(requests),/PRIVATE|SECRET|PASSWORD/);
""".replace("SOURCE", json.dumps(source))
    path = tmp_path / "delivery.mjs"
    path.write_text(script)
    result = subprocess.run(
        [shutil.which("node"), str(path)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_elapsed_request_completion_selects_correlated_slow_context(tmp_path):
    from agentbridge.gui.diagnostics import Diagnostics

    source = (
        (Path(__file__).resolve().parents[1] / "gui/static/js/diagnostics.js")
        .read_text()
        .replace("export function ", "function ")
    )
    script = r"""
import assert from 'node:assert/strict';
let now=0,next=0;const timers=new Map(),events=[];
const window={addEventListener(){}};
const document={body:{},addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]};
const MutationObserver=class {observe(){}disconnect(){}};
const setTimeout=(f,ms)=>{timers.set(++next,{f,ms});return next};
const clearTimeout=id=>timers.delete(id);
const fetch=async(p,o)=>{const batch=JSON.parse(o.body).events;events.push(...batch);
  return new Response(JSON.stringify({ok:true,accepted:batch.length,dropped:0}))};
const build=new Function('window','document','MutationObserver','fetch','setTimeout','clearTimeout',
 'requestAnimationFrame','cancelAnimationFrame','performance',SOURCE+
 ';return {configureDiagnostics,beginDiagnosticRequest,endDiagnosticRequest};');
const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,()=>1,()=>{}, {now:()=>now});
d.configureDiagnostics(true);
const slow=d.beginDiagnosticRequest('/api/state');
now=10;const fast=d.beginDiagnosticRequest('/api/mesh/chat_page?id=PRIVATE');
now=40;d.endDiagnosticRequest(fast,'/api/mesh/chat_page?id=PRIVATE',{},{});
now=1500;d.endDiagnosticRequest(slow,'/api/state',{},{});
now=2000;const failure=d.beginDiagnosticRequest('/api/mesh/chat_page');
now=3200;d.endDiagnosticRequest(failure,'/api/mesh/chat_page',{},null,true);
const [id,t]=[...timers].find(([,v])=>v.ms===1000);timers.delete(id);await t.f();
assert.equal(events.find(x=>x.request_ref===slow&&x.phase==='browser_response').duration_ms,1500);
assert.equal(events.find(x=>x.request_ref===fast&&x.phase==='browser_response').duration_ms,30);
assert.equal(events.find(x=>x.request_ref===failure&&x.phase==='browser_request_failed').duration_ms,1200);
console.log(JSON.stringify({slow,fast,failure,events}));
""".replace("SOURCE", json.dumps(source))
    runner = tmp_path / "correlated-duration.mjs"
    runner.write_text(script)
    result = subprocess.run(
        [shutil.which("node"), str(runner)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    sink = Diagnostics(tmp_path / "home")
    try:
        assert sink.set_enabled(True, slow_ms=1000, sample_rate=0)
        assert sink.flight_record({
            "event": "server_request", "phase": "request_started",
            "route": "/api/state", "request_ref": observed["slow"],
        })
        accepted, dropped = sink.collect(observed["events"])
        assert accepted == len(observed["events"]) and dropped == 0
        assert sink.flush(timeout=1)
        rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
        for ref in (observed["slow"], observed["failure"]):
            matching = [row for row in rows if row.get("request_ref") == ref]
            assert any(row.get("phase") == "browser_request_started" for row in matching)
            assert any(row.get("duration_ms", 0) >= 1000 for row in matching)
        assert any(row.get("phase") == "request_started" and
                   row.get("request_ref") == observed["slow"] for row in rows)
        assert not any(row.get("request_ref") == observed["fast"] for row in rows)
        assert "PRIVATE" not in sink.path.read_text()
    finally:
        sink.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_api_completion_ownership_after_optout_reset_and_disabled_start(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "diagnostics.mjs").write_text(
        (root / "gui/static/js/diagnostics.js").read_text()
    )
    (tmp_path / "api.mjs").write_text(
        (root / "gui/static/js/api.js").read_text()
        .replace('"./util.js"', '"./util.mjs"')
        .replace('"./files.js"', '"./files.mjs"')
        .replace('"./diagnostics.js"', '"./diagnostics.mjs"')
    )
    (tmp_path / "util.mjs").write_text("export const toast=()=>{};\n")
    (tmp_path / "files.mjs").write_text("export const bindFilePreview=()=>{};\n")
    script = r"""
import assert from 'node:assert/strict';
let now=0,next=0,settle;const timers=new Map(),events=[],listeners=new Map(),native=[];
globalThis.performance={now:()=>now};
globalThis.window={addEventListener(){}};
globalThis.document={body:{},addEventListener:(name,f)=>listeners.set(name,f),
  querySelector:()=>null,querySelectorAll:()=>[],dispatchEvent(){}};
globalThis.MutationObserver=class {observe(){}disconnect(){}};
globalThis.requestAnimationFrame=()=>1;globalThis.cancelAnimationFrame=()=>{};
globalThis.setTimeout=(f,ms)=>{timers.set(++next,{f,ms});return next};
globalThis.clearTimeout=id=>timers.delete(id);
globalThis.fetch=(path,opts)=>{
  if(path==='/api/diagnostics/events') {
    const batch=JSON.parse(opts.body).events;events.push(...batch);
    return Promise.resolve(new Response(JSON.stringify({ok:true,accepted:batch.length,dropped:0})));
  }
  native.push({path,opts});
  return new Promise((resolve,reject)=>{settle={resolve,reject}});
};
const d=await import('./diagnostics.mjs');const {api}=await import('./api.mjs');
const flush=async()=>{for(let i=0;i<5;i++){
  const [id,t]=[...timers].find(([,v])=>v.ms===1000)||[];
  if(!t)break;timers.delete(id);await t.f();
}};
for(const mode of ['off_on','session_reset','disabled_start','owned']) {
  for(const fail of [false,true]) {
    d.configureDiagnostics(false);events.length=0;native.length=0;
    if(mode!=='disabled_start')d.configureDiagnostics(true);
    now=100;const pending=api('/api/state?secret=PRIVATE_QUERY');
    const ref=native[0].opts.headers?.['X-AgentBridge-Diagnostic'];
    if(mode==='disabled_start')assert.equal(ref,undefined);else assert.match(ref,/^[0-9a-f]{16}$/);
    if(mode==='off_on'){d.configureDiagnostics(false);d.configureDiagnostics(true)}
    if(mode==='session_reset')listeners.get('ab:session-reset')();
    if(mode==='disabled_start')d.configureDiagnostics(true);
    now=1800;
    if(fail) {
      const error=Object.assign(new Error('PRIVATE_REJECTION'),{name:'AbortError'});
      const rejected=assert.rejects(pending,e=>e===error);settle.reject(error);await rejected;
    } else {
      const out={status:'ok',messages:['PRIVATE_MESSAGE']};
      settle.resolve({json:async()=>{now=1900;return out}});
      assert.equal(await pending,out,'diagnostics preserves application response');
    }
    await flush();
    const completed=events.filter(e=>e.phase==='browser_response'||e.phase==='browser_request_failed');
    const legacy=events.filter(e=>e.event==='client_request');
    if(mode==='owned') {
      assert.equal(completed.length,1);assert.equal(legacy.length,1);
      assert.equal(completed[0].request_ref,ref);assert.equal(legacy[0].request_ref,ref);
      assert.equal(completed[0].duration_ms,fail?1700:1800);
      assert.equal(legacy[0].duration_ms,fail?1700:1800);
      if(fail){assert.equal(legacy[0].error_type,'AbortError');assert.equal(legacy[0].status,'error')}
      else assert.equal(legacy[0].rows,1);
    } else {
      assert.equal(completed.length,0,mode+': stale correlated completion excluded');
      assert.equal(legacy.length,0,mode+': stale generic completion excluded');
    }
    assert.doesNotMatch(JSON.stringify(events),/PRIVATE_QUERY|PRIVATE_REJECTION|PRIVATE_MESSAGE/);
  }
}
"""
    runner = tmp_path / "api-completion-ownership.mjs"
    runner.write_text(script)
    result = subprocess.run(
        [shutil.which("node"), str(runner)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_quiet_expiry_retirement_capacity_and_stale_timers(tmp_path):
    source = (
        (Path(__file__).resolve().parents[1] / "gui/static/js/diagnostics.js")
        .read_text()
        .replace("export function ", "function ")
    )
    script = r"""
import assert from 'node:assert/strict';
let now=0,next=0;const timers=new Map(),listeners={},events=[];
const window={addEventListener:(n,f)=>listeners[n]=f};
const document={body:{},addEventListener:(n,f)=>listeners[n]=f,
 querySelector:()=>null,querySelectorAll:()=>[]};
const MutationObserver=class {observe(){}disconnect(){}};
const setTimeout=(fn,ms)=>{timers.set(++next,{fn,ms,at:now+ms});return next};
const clearTimeout=id=>timers.delete(id);
const fetch=async(p,o)=>{const batch=JSON.parse(o.body).events;events.push(...batch);
 return new Response(JSON.stringify({ok:true,accepted:batch.length,dropped:0}))};
const build=new Function('window','document','MutationObserver','fetch','setTimeout','clearTimeout',
 'requestAnimationFrame','cancelAnimationFrame','performance',SOURCE+
 ';return {configureDiagnostics,beginDiagnosticRequest,endDiagnosticRequest,receivedDelivery,'+
 'canonicalDeliveryDom,acknowledgedDelivery,captureDiagnosticObservation,diagnosticObservationMayApply,'+
 'sizes:()=>[requests.size,attempts.size,deliveries.size,completedDeliveries.size]};');
const d=build(window,document,MutationObserver,fetch,setTimeout,clearTimeout,()=>1,()=>{}, {now:()=>now});
const flush=async()=>{for(let i=0;i<10;i++){
 const found=[...timers].find(([,t])=>t.ms===1000);if(!found)break;
 timers.delete(found[0]);await found[1].fn();
}};
const expire=async()=>{
 const found=[...timers].find(([,t])=>t.at===now);assert.ok(found,'expiry timer due');
 timers.delete(found[0]);await found[1].fn();await flush();
};
const abandoned=ref=>events.filter(e=>e.phase==='abandoned'&&(e.request_ref===ref||e.trace_ref===ref));
const incoming=(id,ref)=>({type:'message',id,ns:'1',chat_id:'PRIVATE',diagnostic_ref:ref});
const response={id:'m-1-PRIVATE',ns:'1',_diagnostics:{trace_ref:'a'.repeat(16)}};
assert.equal(timers.size,0);
d.configureDiagnostics(true);await flush();assert.equal(timers.size,0,'enabled empty has no timer');
const token=d.captureDiagnosticObservation();
const generic=d.beginDiagnosticRequest('/api/state');
now=100;const post=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});
now=200;d.receivedDelivery(incoming('m-1-INCOMING','b'.repeat(16)));
await flush();assert.equal(timers.size,1,'one earliest expiration timer');
now=300000;await expire();assert.equal(abandoned(generic).length,1);
assert.equal(abandoned(post).length,0);assert.equal(d.endDiagnosticRequest(generic,'/api/state',{},{}),false);
assert.equal(timers.size,1);
now=300100;await expire();assert.equal(abandoned(post).length,1,'POST retired exactly once');
assert.equal(d.endDiagnosticRequest(post,'/api/mesh/post',{},response),false,'late POST cannot create delivery');
assert.deepEqual(d.sizes(),[0,0,1,0]);
now=300200;await expire();assert.equal(abandoned('b'.repeat(16)).length,1);
assert.deepEqual(d.sizes(),[0,0,0,0]);assert.equal(timers.size,0,'quiet maps emptied');
assert.ok(d.diagnosticObservationMayApply(token),'expiry does not replace observation epoch');
// Successful requests and acknowledged deliveries have no expiration timer.
const completed=d.beginDiagnosticRequest('/api/state');now+=10;
assert.equal(d.endDiagnosticRequest(completed,'/api/state',{},{}),true);
d.receivedDelivery(incoming('m-1-ACKED','c'.repeat(16)));
const row={dataset:{mid:'m-1-ACKED'},classList:{contains:()=>false}};
d.canonicalDeliveryDom('PRIVATE',[{id:'m-1-ACKED'}],{querySelectorAll:()=>[row]});
d.acknowledgedDelivery('PRIVATE','1');await flush();assert.equal(timers.size,0);
d.receivedDelivery(incoming('m-1-ACKED','c'.repeat(16)));await flush();
assert.deepEqual(d.sizes(),[0,0,0,1],'acked duplicate cannot resurrect');
now+=300000;await flush();assert.equal(abandoned(completed).length,0);assert.equal(abandoned('c'.repeat(16)).length,0);
// Opt-out and every context retirement fence retained canceled callbacks.
for(const edge of ['off_on','ab:session-reset','ab:lock-epoch','hashchange']) {
 const old=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});
 await flush();const stale=[...timers.values()][0].fn;
 const owner=d.captureDiagnosticObservation();
 if(edge==='off_on'){d.configureDiagnostics(false);d.configureDiagnostics(true)}else listeners[edge]();
 assert.equal(d.diagnosticObservationMayApply(owner),false,edge);
 const fresh=d.beginDiagnosticRequest('/api/state');await flush();
 const snapshot=JSON.stringify(d.sizes());stale();await flush();
 assert.equal(JSON.stringify(d.sizes()),snapshot,edge+': stale callback cannot alter new observations');
 assert.equal(d.endDiagnosticRequest(old,'/api/mesh/post',{},response),false);
 assert.equal(abandoned(old).length,edge==='off_on'?0:1,edge+': retirement exactly once');
 assert.equal(d.endDiagnosticRequest(fresh,'/api/state',{},{}),true);await flush();
 assert.equal(timers.size,0);
}
// A canceled timer in the same observation epoch cannot replace the current timer.
const settled=d.beginDiagnosticRequest('/api/state');await flush();
const sameEpochStale=[...timers.values()][0].fn;
d.endDiagnosticRequest(settled,'/api/state',{},{});await flush();
const owned=d.beginDiagnosticRequest('/api/state');await flush();
const timerIds=[...timers.keys()];sameEpochStale();await flush();
assert.deepEqual([...timers.keys()],timerIds,'timer identity fences canceled callbacks within same epoch');
d.endDiagnosticRequest(owned,'/api/state',{},{});await flush();
// Throttled timer dispatch cannot admit a completion after its deadline.
const overdue=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});
now+=300000;assert.equal(d.endDiagnosticRequest(overdue,'/api/mesh/post',{},response),false);
await flush();assert.equal(abandoned(overdue).length,1);assert.deepEqual(d.sizes().slice(0,3),[0,0,0]);
assert.equal(timers.size,0);
// Oldest request capacity eviction also retires its matching POST attempt.
const oldest=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});await flush();
for(let i=0;i<128;i++){d.beginDiagnosticRequest('/api/state');if(i%20===0)await flush()}
await flush();assert.deepEqual(d.sizes().slice(0,2),[128,0]);
assert.equal(abandoned(oldest).length,1);assert.equal(d.endDiagnosticRequest(oldest,'/api/mesh/post',{},response),false);
listeners['ab:session-reset']();await flush();
const first=d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});
for(let i=0;i<32;i++){d.beginDiagnosticRequest('/api/mesh/post',{chat_id:'PRIVATE'});await flush()}
assert.deepEqual(d.sizes().slice(0,2),[32,32]);assert.equal(abandoned(first).length,1);
assert.equal(d.endDiagnosticRequest(first,'/api/mesh/post',{},response),false);
listeners['ab:session-reset']();await flush();
for(let i=0;i<33;i++){d.receivedDelivery(incoming('m-1-'+i,i.toString(16).padStart(16,'0')));await flush()}
assert.equal(d.sizes()[2],32);assert.equal(abandoned('0'.repeat(16)).length,1);
assert.equal(timers.size,1,'capacity remains one timer');
d.configureDiagnostics(false);assert.equal(timers.size,0);assert.deepEqual(d.sizes(),[0,0,0,0]);
assert.doesNotMatch(JSON.stringify(events),/PRIVATE|INCOMING|ACKED/);
""".replace("SOURCE", json.dumps(source))
    runner = tmp_path / "quiet-expiry.mjs"
    runner.write_text(script)
    result = subprocess.run(
        [shutil.which("node"), str(runner)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
