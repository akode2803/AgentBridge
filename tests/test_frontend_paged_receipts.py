"""Deferred page receipts update only existing tick slots after canonical paint."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_page_bound_receipts_retry_merge_and_stale_guards(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    start = source.index('function receiptMap(')
    end = source.index('document.addEventListener("ab:session-reset"', start)
    functions = source[start:end]
    binding_start = source.index('function samePageBinding(')
    binding_end = source.index('function openAgentPermissionEntry(', binding_start)
    same_binding = source[binding_start:binding_end]
    script = r'''
import assert from 'node:assert/strict';
const code = __FUNCTIONS__ + '\n' + __BINDING__;
const binding={instance_id:'app',session_generation:'1',viewer:'alice'};
const owner={chatId:'room',pageVersion:'v1',current:()=>true,busy:false};
let pageOwner=owner;
const calls=[],ticks=[],refreshes=[];
const queue=[];
const transcript={};
const content={innerHTML:'painted'};
const $=selector=>selector==='#transcript'?transcript:selector==='#content'?content:null;
const BrowserSession={snapshot:()=>({binding})};
const pageRead={snapshot:()=>({pageData:{meta:{kind:'group'}}})};
const isDmLike=meta=>meta.kind==='dm';
const pageRetryDelay=()=>0;
const syncReceiptTicks=(tr,messages,isDm,ready)=>ticks.push({tr,messages,isDm,ready});
const api=async(path,body,options)=>{calls.push({path,body,options});return queue.shift();};
const setTimeout=fn=>{fn();return 1;};
const retireDeniedMeshChat=()=>{};
const renderSidebar=()=>{};
const resetPagedView=()=>{pageOwner=null;};
const renderPagedChat=async(...args)=>refreshes.push(args);
const location={hash:''};
const build=new Function('pageOwner','owner','BrowserSession','pageRead','isDmLike',
  'syncReceiptTicks','api','setTimeout','retireDeniedMeshChat','renderSidebar',
  'resetPagedView','renderPagedChat','$','location','pageRetryDelay',
  `${code};return {refreshPagedReceipts,syncRetainedReceipts,receiptMap};`);
const apiFns=build(pageOwner,owner,BrowserSession,pageRead,isDmLike,syncReceiptTicks,
  api,setTimeout,retireDeniedMeshChat,renderSidebar,resetPagedView,renderPagedChat,$,location,
  pageRetryDelay);
const receipt={state:'read',read_by:['bob'],delivered_to:[],pending:[],total:1};
queue.push({status:'pending',retry_after_ms:350,session_binding:binding});
queue.push({status:'ready',chat_id:'room',page_version:'v1',session_binding:binding,
  receipts:{m1:receipt}});
await apiFns.refreshPagedReceipts(owner,[{chat_id:'room',page_version:'v1',
  read_ack_token:'a'.repeat(64)}]);
assert.equal(calls.length,2);
assert.equal(calls[0].path,'/api/mesh/chat_page_receipts');
assert.equal(owner.receiptSnapshot.get('m1'),receipt);
assert.equal(ticks.length,1);
assert.deepEqual(ticks[0].messages,[{id:'m1',mine:true,receipt}]);
assert.equal(ticks[0].ready,true);

// A later canonical paint reuses only the in-memory decoration for matching ids.
apiFns.syncRetainedReceipts(owner,[{id:'m1',mine:true},{id:'m2',mine:true}],{kind:'group'});
assert.equal(ticks.length,2);
assert.equal(ticks[1].messages.length,1);
assert.equal(ticks[1].messages[0].id,'m1');

// A companion from another page version cannot decorate or trigger a repaint.
queue.push({status:'ready',chat_id:'room',page_version:'old',session_binding:binding,
  receipts:{m2:receipt}});
await apiFns.refreshPagedReceipts(owner,[{chat_id:'room',page_version:'v1',
  read_ack_token:'b'.repeat(64)}]);
assert.equal(owner.receiptSnapshot.has('m2'),false);
assert.equal(ticks.length,2);
assert.deepEqual(refreshes,[]);

// A changed exact cut asks for a fresh canonical page even though the compact
// reset response deliberately carries no chat payload.
queue.push({status:'reset_required',reason:'receipt_page_inputs_changed',
  session_binding:binding});
await apiFns.refreshPagedReceipts(owner,[{chat_id:'room',page_version:'v1',
  read_ack_token:'c'.repeat(64)}]);
assert.equal(refreshes.length,1);
'''.replace('__FUNCTIONS__', json.dumps(functions)).replace('__BINDING__', json.dumps(same_binding))
    path = tmp_path / 'paged-receipts.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
