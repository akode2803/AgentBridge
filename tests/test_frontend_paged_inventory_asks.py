"""Incomplete bounded sidebar/ask lanes retain same-session display rows."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_incomplete_sidebar_inventory_retains_only_same_session_rows(tmp_path):
    source = (ROOT / 'gui/static/js/state.js').read_text(encoding='utf-8')
    body = source[source.index('export function applyMeshState('):
                  source.index('// agent reply-rule vocabulary', source.index('export function applyMeshState('))]
    body = body.replace('export function ', 'function ')
    script = r'''
import assert from 'node:assert/strict';
const binding=(viewer='alice', generation='1')=>({instance_id:'app',session_generation:generation,viewer});
const Mesh={state:null};
const build=new Function('Mesh','viewReadMayApply','advanceViewCounter',
  `let appliedMeshReadSequence=0,meshStateGeneration=0;
   ${__BODY__};return applyMeshState;`);
const apply=build(Mesh,()=>true,n=>n+1);
const ticket={epoch:1};
let sequence=0;
const receive=(response)=>apply(ticket,response,{session:{epoch:1},readSequence:++sequence});
const initial={user:'alice',session_binding:binding(),users:{bob:{display:'Bob'}},
  chats:[{id:'room',name:'Room'}],users_complete:true,chats_complete:true};
assert.equal(receive(initial),true);
let next={user:'alice',session_binding:binding(),users:{},chats:[],
  users_complete:false,chats_complete:false};
assert.equal(receive(next),true);
assert.deepEqual(Mesh.state.users,{bob:{display:'Bob'}});
assert.deepEqual(Mesh.state.chats,[{id:'room',name:'Room'}]);
assert.equal(Mesh.state.chats_complete,false); // Explicit incomplete UI hint.
next={...next,users:{bob:{display:'Updated'},carol:{display:'Carol'}},
  chats:[{id:'room',name:'Updated room'},{id:'other',name:'Other'}]};
receive(next);
assert.deepEqual(Mesh.state.users,{bob:{display:'Updated'},carol:{display:'Carol'}});
assert.deepEqual(Mesh.state.chats,[{id:'room',name:'Updated room'},
  {id:'other',name:'Other'}]);
receive({...next,users:{},chats:[],users_complete:true,chats_complete:true});
assert.deepEqual(Mesh.state.users,{});
assert.deepEqual(Mesh.state.chats,[]); // Complete absence is authoritative.
receive(initial);
receive({...next,user:'other',session_binding:binding('other','2'),users:{},chats:[]});
assert.deepEqual(Mesh.state.users,{});
assert.deepEqual(Mesh.state.chats,[]); // Never carry display across sessions.
'''.replace('__BODY__', json.dumps(body))
    path = tmp_path / 'inventory.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
@pytest.mark.parametrize('scenario', [
    'independent', 'global_first', 'denial', 'partial', 'route', 'reset',
    'invalid', 'done', 'standdown', 'global_partial', 'bounds', 'failure', 'repeated_denial',
])
def test_ask_poll_independent_owned_lanes(tmp_path, scenario):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    body = source[source.index('let askPollRequest = null;'):
                  source.index('function renderAskBar(', source.index('let askPollRequest = null;'))]
    script = (ROOT / 'tests/fixtures/ask_poll_scenarios.mjs').read_text(encoding='utf-8')
    script = script.replace('__BODY__', json.dumps(body)).replace('__SCENARIO__', json.dumps(scenario))
    path = tmp_path / 'asks.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
