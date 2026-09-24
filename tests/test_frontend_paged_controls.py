"""Selected-page mute and owner-entry UI decisions stay bounded and neutral."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_agent_entry_only_uses_current_members_and_public_sidebar_owner_facts(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    helper = source[source.index('function agentPermissionEntry('):
                    source.index('async function renderMeshChat(', source.index('function agentPermissionEntry('))]
    script = r'''
import assert from 'node:assert/strict';
const build = new Function(__HELPER__ + ';return agentPermissionEntry;');
const decide = build();
const meta = {members:['alice','bot','peer']};
const viewer = {user:'alice',users:{
  alice:{kind:'human'},bot:{kind:'agent',owners:['alice']},
  peer:{kind:'human'},outside:{kind:'agent',owners:['alice']},
}};
assert.equal(decide(meta,viewer),'ready');
assert.equal(decide({members:['alice','peer']},viewer),'absent');
assert.equal(decide(meta,{...viewer,users:{...viewer.users,
  bot:{kind:'agent',owners:['other']}}}),'absent');
assert.equal(decide(meta,{...viewer,users:{...viewer.users,
  bot:{kind:'agent'}}}),'pending');
assert.equal(decide(meta,{user:'alice',users:{}}),'absent');
// A known agent whose owner relation is unavailable gets a neutral disabled
// entry, never a fabricated authorization or an inferred "not owned" claim.
'''.replace('__HELPER__', json.dumps(helper))
    path = tmp_path / 'controls.mjs'
    path.write_text(script, encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(path)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'data.metadata_status?.mute === "ready"' in source
    assert 'Mute status loading…' in source
    assert 'permissionEntry === "ready"' in source
    assert 'permissionEntry === "pending"' in source
    assert 'data._paged ? {mute:meta.mute}' in source
