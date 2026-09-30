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
const fetch=async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return {ok:true}};
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
