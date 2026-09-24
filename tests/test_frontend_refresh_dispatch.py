"""Normal refresh must reach the real latest-read scheduler before repainting."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_normal_refresh_dispatches_and_rejects_stale_queued_owners(tmp_path):
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    start = source.index('async function renderChats(force)')
    source = source[start:source.index('V.renderChats = renderChats;', start)]
    (tmp_path / 'latest-read.mjs').write_text(
        (ROOT / 'gui/static/js/latest-read.js').read_text(encoding='utf-8'),
        encoding='utf-8')
    script = _RUNNER.replace('__SOURCE__', json.dumps(source))
    (tmp_path / 'run.mjs').write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(tmp_path / 'run.mjs')],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


_RUNNER = r'''
import assert from 'node:assert/strict';
import {createLatestRead} from './latest-read.mjs';
const source = __SOURCE__;
function fixture() {
  const App={page:'chats',routeSeq:1}, Mesh={chatId:'room',detailsView:false,
    state:{available:true,user:'alice',chats:[]}};
  let lockEpoch=0, session=0, calls=0, sidebar=0, transcript=0;
  const next={available:true,user:'alice',chats:[{id:'room',preview:'new message'}]};
  const pane={hidden:true};
  const env={App,Mesh,window:{},BrowserSession:{snapshot:()=>({})},
    meshCaps:()=>({chat_page_v1:false}),pageOwner:null,
    sidebarRead:createLatestRead(10), SIDEBAR_TIMEOUT_MS:100,
    isInitialSelectedViewPending:()=>false, captureSessionEpoch:()=>session,
    sessionMayApply:t=>t===session, meshStateSnapshot:()=>({lockEpoch}),
    selectedChatContext:()=>null, warmContext:()=>null, restartIntent:()=>null,
    captureWarmStateRequest:()=>({lockEpoch}),
    api:async()=>{calls++;return next;},
    applyMeshState:(ticket,fresh,request)=>{
      assert.equal(ticket,session);assert.equal(request.lockEpoch,lockEpoch);
      Mesh.state=fresh;return true;
    },
    $:s=>s==='#details-pane'?pane:{},
    renderSidebar:()=>{sidebar++;assert.equal(Mesh.state,next);},
    startAskPoll:()=>{},renderMeshChat:async()=>{transcript++;},
    V:{closeAuthPage:()=>{},closeConnectingPage:()=>{}},
  };
  const render=new Function(...Object.keys(env),
    `let warmOperationSeq=0,warmOperationsExhausted=false,chatsFetchSeq=0,skipWarmChatId=null;
     ${source};return renderChats;`)(...Object.values(env));
  return {render,App,Mesh,counts:()=>({calls,sidebar,transcript}),
    lock:()=>{lockEpoch++;},session:()=>{session++;}};
}
// Already-visible chat uses the normal safety/event/mutation refresh path.
{
 const f=fixture();await f.render(false);
 assert.deepEqual(f.counts(),{calls:1,sidebar:1,transcript:1},
   'normal refresh must dispatch its state read and repaint both surfaces');
 await f.render(false);
 assert.deepEqual(f.counts(),{calls:2,sidebar:2,transcript:2});
}
// Queue admission uses the owner captured before enqueue, never a request
// initialized only by the not-yet-dispatched read callback.
for (const change of [f=>f.lock(),f=>f.session(),f=>{f.App.routeSeq++;}]) {
 const f=fixture();const pending=f.render(false);change(f);await pending;
 assert.deepEqual(f.counts(),{calls:0,sidebar:0,transcript:0});
}
'''
