"""A finalized paged transcript can dismiss boot before sidebar hydration."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_paged_boot_does_not_wait_for_sidebar_and_rejects_stale_view(tmp_path):
    root = Path(__file__).resolve().parents[1] / 'gui/static/js'
    chat = (root / 'chat.js').read_text(encoding='utf-8')
    ready = chat[chat.index('export function isPagedChatViewReady()'):chat.index('function abortPagedAux(')].replace('export ', '')
    main = (root / 'main.js').read_text(encoding='utf-8')
    boot = main[main.index('  (function bootDone()'):main.index('  // ---- V111 app lock:')]
    script = r'''
const assert = require('node:assert/strict');
let timers=[], done=false, removed=false, current=true, transcript=true, sideLoading=0;
let pageOwner={ready:false, chatId:'room',current:()=>current};
const Mesh={chatId:'room', renderedChat:'room',state:null};
const App={page:'chats',state:{user:'alice'}};
const $=s=>s==='#transcript' ? transcript : null;
const BrowserSession={snapshot:()=>({binding:{viewer:'alice'}})};
const restartIntent=()=>false, isInitialSelectedViewReady=()=>false;
const document={getElementById:id=>id==='boot'?{
 classList:{add:value=>{assert.equal(value,'done');done=true;}},remove:()=>{removed=true;}
}:null};
const setTimeout=(fn,ms)=>{timers.push({fn,ms});};
const diagnostic=()=>{};
const renderSideLoading=()=>{assert.equal(done,true);sideLoading++;};
eval(__READY__);
eval(__BOOT__);
assert.equal(done,false); assert.equal(timers[0].ms,80);
pageOwner.ready=true; current=false;
timers.shift().fn(); assert.equal(done,false);
current=true; transcript=false;
timers.shift().fn(); assert.equal(done,false);
transcript=true;
timers.shift().fn(); assert.equal(done,true); assert.equal(Mesh.state,null);
assert.equal(sideLoading,1,"boot completion rearms the delayed sidebar cue");
assert.equal(timers[0].ms,350); timers.shift().fn(); assert.equal(removed,true);
'''.replace('__READY__', json.dumps(ready)).replace('__BOOT__', json.dumps(boot))
    runner = tmp_path / 'boot.cjs'
    runner.write_text(script, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(runner)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
