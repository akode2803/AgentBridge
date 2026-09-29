"""Executable ownership checks for chat-info and collection subviews."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_details_ticket_fences_same_route_subviews_reordered_reads_and_session(tmp_path):
    source = (ROOT / "gui/static/js/details_read.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    source = source.replace("export function ", "function ")
    script = r'''
import assert from 'node:assert/strict';
const Mesh = {chatId:'room', detailsView:true, searchView:false, searchQ:'',
  mediaView:false, mediaTab:'media', starredPane:false, agentsView:false,
  permsView:false, memberInfo:null};
let session=1, route=1;
const captureViewRead=()=>({session,route,chat:Mesh.chatId});
const viewReadMayApply=(owner,response)=>!!owner && owner.session===session
  && owner.route===route && owner.chat===Mesh.chatId
  && (!response?.session_binding || response.session_binding===session);
const factory=new Function('Mesh','captureViewRead','viewReadMayApply',
  __SOURCE__+'; return {beginDetailsRead,detailsReadCurrent,finishDetailsRead,invalidateDetailsRead};');
const t=factory(Mesh,captureViewRead,viewReadMayApply);
let a=t.beginDetailsRead();
assert.ok(a); assert.equal(t.beginDetailsRead(),null,'same request coalesces while busy');
Mesh.mediaView=true;
assert.equal(t.detailsReadCurrent(a),false,'same-route media entry retires summary');
let b=t.beginDetailsRead(); assert.ok(b);
Mesh.mediaTab='docs';
assert.equal(t.detailsReadCurrent(b),false,'same-route tab switch retires media');
let c=t.beginDetailsRead(); assert.ok(c);
Mesh.mediaTab='links'; let d=t.beginDetailsRead();
Mesh.mediaTab='docs'; let e=t.beginDetailsRead();
assert.equal(t.detailsReadCurrent(c),false,'A-B-A transition cannot revive A');
assert.equal(t.detailsReadCurrent(d),false);
assert.equal(t.detailsReadCurrent(e),true);
t.finishDetailsRead(e);
let newer=t.beginDetailsRead();
assert.ok(newer);
assert.equal(t.detailsReadCurrent(e),false,'later same-pane request wins');
assert.equal(t.detailsReadCurrent(newer),true);
Mesh.mediaView=false; Mesh.searchView=true; Mesh.searchQ='one';
let search=t.beginDetailsRead();
Mesh.searchQ='two';
assert.equal(t.detailsReadCurrent(search),false,'query edit retires old results');
let changed=t.beginDetailsRead();
assert.equal(t.detailsReadCurrent(changed,{session_binding:99}),false);
session++;
assert.equal(t.detailsReadCurrent(changed),false,'session retire after await');
let current=t.beginDetailsRead(); route++;
assert.equal(t.detailsReadCurrent(current),false,'route retire after await');
let next=t.beginDetailsRead(); t.invalidateDetailsRead();
assert.equal(t.detailsReadCurrent(next),false,'explicit invalidation');
Mesh.detailsView=false; assert.equal(t.beginDetailsRead(),null);
'''.replace("__SOURCE__", json.dumps(source))
    path = tmp_path / "details_read.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
