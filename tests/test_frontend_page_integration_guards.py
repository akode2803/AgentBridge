"""Exercise the active chat.js paging gates without importing the whole UI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_paged_read_eyes_latest_scroll_and_pending_receipt_guards(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    mark = source[source.index('function markReadNow(chatId)'):
                  source.index('// reading needs eyes:', source.index('function markReadNow(chatId)'))]
    ticks = source[source.index('function syncReceiptTicks('):
                   source.index('function receiptTicks(', source.index('function syncReceiptTicks('))]
    preserve = source[source.index('const preserve = kind !== "first"'):
                      source.index(';', source.index('const preserve = kind !== "first"')) + 1]
    script = r'''
import assert from 'node:assert/strict';
let paged = true, requests = [], sidebarPaints = 0;
let tr = {scrollHeight:1000, clientHeight:200, scrollTop:250};
let pageOwner = {ready:true, chatId:'room', browsing:false, current:()=>true,
                 visibleReadNs:'100'};
let Mesh = {pendingRead:null, readTail:{}, state:{chats:[
  {id:'room', last:{ns:150}, unread:5, forced_unread:true},
]}};
const meshCaps = () => ({chat_page_v1:paged});
const $ = selector => selector === '#transcript' ? tr : null;
const api = (path, body) => {requests.push({path, body}); return Promise.resolve({ok:true});};
const renderSidebar = () => {sidebarPaints += 1;};
__MARK__
__TICKS__
function receiptTicks() {return '<span>Sent</span>';}

markReadNow('room');
assert.equal(Mesh.pendingRead,'room');
assert.deepEqual(requests,[]); // not at tail, even within first page
assert.equal(sidebarPaints,0);
tr.scrollTop=800;
markReadNow('room');
assert.equal(Mesh.pendingRead,null);
assert.equal(requests.length,1);
assert.deepEqual(requests[0].body,{chat_id:'room',up_to_ns:'100'});
assert.equal(Mesh.readTail.room,undefined);
assert.equal(Mesh.state.chats[0].unread,5); // a newer sidebar tail was not seen
Mesh.state.chats[0].last.ns=90;
markReadNow('room');
assert.equal(Mesh.state.chats[0].unread,5); // JS Number cannot prove exact cutoff
assert.equal(sidebarPaints,0);
pageOwner.browsing=true;
markReadNow('room');
assert.equal(requests.length,2); // historical window never advances read
pageOwner.browsing=false;
markReadNow('different');
assert.equal(requests.length,2);
paged=false;
markReadNow('room');
assert.equal(Mesh.readTail.room,90);
assert.equal(Mesh.state.chats[0].unread,0);
assert.equal(sidebarPaints,1);

const slot = {innerHTML:'<span>Sent</span>', _receiptHtml:'<span>Sent</span>'};
const row = {querySelector:()=>slot};
const transcript = {_rows:new Map([['m:own',{el:row}]])};
syncReceiptTicks(transcript,[{id:'own',mine:true}],false,false);
assert.equal(slot.innerHTML,''); // pending receipt is not fabricated Sent
syncReceiptTicks(transcript,[{id:'own',mine:true}],false,true);
assert.equal(slot.innerHTML,'<span>Sent</span>');

const decidePreserve = new Function('kind','mode','owner','before',
  `__PRESERVE__ return preserve;`);
const far = {scrollHeight:1000,scrollTop:100,clientHeight:200};
assert.equal(decidePreserve('first','first',{browsing:true},far),false);
assert.equal(decidePreserve(null,'first',{browsing:false},far),true);
assert.equal(decidePreserve('older','older',{browsing:true},far),true);
'''
    script = script.replace('__MARK__', mark).replace('__TICKS__', ticks).replace(
        '__PRESERVE__', preserve)
    path = tmp_path / 'check.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run(['node', str(path)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
