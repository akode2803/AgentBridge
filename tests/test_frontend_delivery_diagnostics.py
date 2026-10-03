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
const fetch=async(p,o)=>{requests.push(JSON.parse(o.body));return {ok:true}};
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
