"""Chromium retention composition with synthetic pages and bubble markup.

Page/read/scroll modules and chat.js orchestration/keyed reconciliation are real.
The bubble painter and authority responses are disposable seams, so this is not
full-app, authenticated transport, or independent-peer acceptance.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


playwright = pytest.importorskip('playwright.sync_api', reason='requires optional Playwright')
ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / 'gui/static/js'
BINDING = {'instance_id': 'local', 'session_generation': '1', 'viewer': 'alice'}
CUTOFF = '9007199254740993123'


def _ids(start, count):
    return [f'm{i}' for i in range(start, start + count)]


def _page(ids, cursor, version='v1', **extras):
    return {
        'status': 'page', 'chat_id': 'room', 'session_binding': BINDING,
        'page_version': version, 'messages': [{'id': mid, 'body': 'x'} for mid in ids],
        'meta': {'id': 'room'}, 'me': 'alice', 'starred': [],
        'metadata_status': {'receipts': 'ready'}, 'read_ns': 0,
        'read_cutoff_ns': CUTOFF, 'read_ack_token': 'a' * 64,
        'window_anchor': f'anchor-{cursor}',
        'frozen_window_anchor': f'frozen-{ids[-1] if ids else "empty"}',
        'continuation': cursor, 'has_more': cursor is not None,
        'history_exhausted': cursor is None, 'scan_budget_exhausted': False,
        'raw_examined': len(ids), **extras,
    }


def _harness():
    source = (JS / 'chat.js').read_text(encoding='utf-8')
    render_start = source.index('async function renderPagedChat(')
    render = source[render_start:source.index('let chatRenderSeq =', render_start)]
    mark_start = source.index('function markReadNow(chatId)')
    mark = source[mark_start:source.index('// reading needs eyes:', mark_start)]
    older = source[source.index('function shouldReadOlderPage('):
                   source.index('function samePageBinding(')]
    reconcile = source[source.index('function reconcileRows('):
                       source.index('// element; partial renders',
                                    source.index('function reconcileRows('))]
    return r'''
import {createChatPageRead,pageRetryDelay} from '/chat-page-read.js';
import {captureTranscriptAnchor,restoreTranscriptAnchor,pruneTranscriptResources}
  from '/chat-page-scroll.js';
const App={page:'chats',routeSeq:1};
const Mesh={chatId:'room',state:{user:'alice',chats:[
  {id:'room',last:{ns:1},unread:7,forced_unread:true}]},
  msgExpand:{},select:{ids:new Set()},pendingRead:null};
const binding={instance_id:'local',session_generation:'1',viewer:'alice'};
const $=selector=>document.querySelector(selector);
Object.defineProperty(document,'hasFocus',{value:()=>true});
const BrowserSession={snapshot:()=>({binding})};
const captureSessionEpoch=()=>({route:App.routeSeq});
const sessionMayApply=ticket=>ticket.route===App.routeSeq;
const meshStateSnapshot=()=>({lockEpoch:1});
const meshCaps=()=>({chat_page_v1:true});
const beginLoading=()=>()=>{},endLoading=()=>{},diagnostic=()=>{};
const canonicalDeliveryDom=()=>{},acknowledgedDelivery=()=>{};
const renderSideLoading=()=>{},renderSidebar=()=>{},recordChatOpen=()=>{};
const abortPagedAux=()=>{},refreshPagedAux=async()=>{},refreshPagedSidebar=async()=>{};
const syncPagedAuxControls=()=>{},syncDmHeaderPresence=()=>{};
const observeLockState=()=>{};
const V={renderChatDetails:async()=>{}};
const pagedAuxDisplay=data=>({data,presentation:Mesh.state,aux:null});
let pageOwner=null,pageRetryTimer=null;
let fetchCount=0,pauseAt=null,releasePage=null,held=false,paintAllowed=true;
const ackBodies=[];
async function api(path,body) {
  ackBodies.push(body);
  return (await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})).json();
}
const pageRead=createChatPageRead({pageSize:200,fetchPage:async request=>{
  fetchCount++;
  const response=await (await fetch('/synthetic/page?limit='+request.limit)).json();
  if(fetchCount===pauseAt){held=true;await new Promise(resolve=>{releasePage=resolve});held=false;}
  return response;
}});
function resetPagedView(){pageOwner=null;pageRead.invalidate('route_changed');}
__RECONCILE__
// Explicit synthetic bubble seam: actual keyed reconcile operates on real DOM.
async function paintMeshChat(force,trace,prepared){
  if(!paintAllowed || !prepared.guard())return false;
  const tr=$('#transcript');
  const rows=prepared.data.messages.map(message=>{
    const n=Number(message.id.slice(1));
    const height=32+(Number.isFinite(n)?Math.abs(n)%4:2)*4;
    return ['m:'+message.id,`<div class="msg" data-mid="${message.id}" style="height:${height}px">x</div>`];
  });
  reconcileRows(tr,[['d:fixture','<div class="day-sep">Fixture</div>'],...rows]);
  tr._msgs=new Map(prepared.data.messages.map(message=>[message.id,message]));
  Mesh.renderedChat=Mesh.chatId;
  return true;
}
__OLDER__
__RENDER__
__MARK__
const frames=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
window.fixture={
  async render(kind){await renderPagedChat(false,kind,{sidebar:false});await frames();},
  snapshot(){
    const tr=$('#transcript'),snapshot=pageRead.snapshot();
    return {ids:[...tr.querySelectorAll('.msg[data-mid]')].map(node=>node.dataset.mid),
      messages:[...tr._msgs.keys()],rows:[...tr._rows.keys()].filter(key=>key.startsWith('m:')),
      count:snapshot.messageCount,snapshotIds:snapshot.messages.map(message=>message.id),
      selected:[...Mesh.select.ids],expanded:Object.keys(Mesh.msgExpand),
      token:pageOwner.visibleReadToken,version:pageOwner.visibleReadVersion,
      cutoff:pageOwner.visibleReadNs,browsing:pageOwner.browsing,
      continuation:snapshot.continuation,ackBodies:[...ackBodies],
      unread:Mesh.state.chats[0].unread,forced:Mesh.state.chats[0].forced_unread,
      scrollTop:tr.scrollTop,held,ackInflight:!!pageOwner.readAck?.inflight};
  },
  position(id,offset=-7){
    const tr=$('#transcript'),node=tr.querySelector(`[data-mid="${id}"]`);
    tr.scrollTop+=node.getBoundingClientRect().top-tr.getBoundingClientRect().top-offset;
    return captureTranscriptAnchor(tr);
  },
  offset(id){return $('#transcript').querySelector(`[data-mid="${id}"]`).getBoundingClientRect().top
    -$('#transcript').getBoundingClientRect().top;},
  remember(id){this.node=$('#transcript').querySelector(`[data-mid="${id}"]`);},
  sameNode(id){return this.node===$('#transcript').querySelector(`[data-mid="${id}"]`);},
  seed(ids=['m100','m599']){
    Mesh.msgExpand=Object.fromEntries(ids.map(id=>[id,true]));Mesh.select.ids=new Set(ids);
  },
  clearAcks(){ackBodies.length=0;},
  markAtBottom(){const tr=$('#transcript');tr.scrollTop=tr.scrollHeight;markReadNow('room');},
  holdSecond(){pauseAt=fetchCount+2;},
  startRefresh(){window.refreshOp=renderPagedChat(false,'refresh',{sidebar:false});},
  async release(){releasePage();await window.refreshOp;await frames();},
  rejectPaint(){paintAllowed=false;},
  retire(){App.routeSeq++;},
};
'''.replace('__RECONCILE__', reconcile).replace('__OLDER__', older).replace(
        '__RENDER__', render).replace('__MARK__', mark)


@pytest.fixture
def retention_browser():
    responses, acknowledgments = [], []
    modules = {f'/{name}': (JS / name).read_bytes() for name in (
        'chat-pages.js', 'chat-page-read.js', 'chat-page-scroll.js')}
    modules['/fixture.js'] = _harness().encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, content_type='application/json'):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/':
                self.reply(b'<style>#transcript{height:240px;overflow:auto;overflow-anchor:none}'
                           b'.msg{margin:0;box-sizing:border-box}.day-sep{height:20px}</style>'
                           b'<div id="content"><div id="transcript"></div></div>'
                           b'<div id="details-pane"></div><script type="module" src="/fixture.js"></script>',
                           'text/html')
            elif self.path in modules:
                self.reply(modules[self.path], 'text/javascript')
            elif self.path.startswith('/synthetic/page?') and responses:
                self.reply(responses.pop(0))
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != '/api/mesh/chat_page_read':
                self.send_error(404)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                self.send_error(413)
                return
            acknowledgments.append(json.loads(self.rfile.read(length)))
            self.reply({'ok': True, 'status': 'acknowledged', 'read_ns': CUTOFF})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    browser = None
    try:
        with playwright.sync_playwright() as driver:
            configured = os.environ.get('AGENTBRIDGE_CHROMIUM_EXECUTABLE')
            executable = configured or next((found for name in (
                'chromium', 'chromium-browser', 'google-chrome')
                if (found := shutil.which(name))), driver.chromium.executable_path)
            if not Path(executable).is_file():
                if configured:
                    pytest.fail('AGENTBRIDGE_CHROMIUM_EXECUTABLE is not a local executable')
                pytest.skip('no local Chromium executable available')
            browser = driver.chromium.launch(executable_path=executable, headless=True,
                                             args=['--no-zygote'], timeout=8000)
            context = browser.new_context(service_workers='block')
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith(base + '/') else route.abort())
            page = context.new_page()
            page.set_default_timeout(5000)
            page.goto(base)
            page.wait_for_function('!!window.fixture')
            yield SimpleNamespace(page=page, responses=responses, acknowledgments=acknowledgments)
            context.close()
            browser.close()
            browser = None
    finally:
        if browser is not None and browser.is_connected():
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        assert not thread.is_alive(), 'loopback HTTP server was not reaped'


def _render(rig, kind, response):
    rig.responses.append(response)
    rig.page.evaluate('(kind)=>fixture.render(kind)', kind)
    state = rig.page.evaluate('fixture.snapshot()')
    assert state['ids'] == state['messages'] == state['snapshotIds']
    assert state['rows'] == [f'm:{mid}' for mid in state['ids']]
    assert len(state['ids']) == state['count'] <= 600
    return state


@pytest.mark.timeout(30)
def test_real_chromium_600_boundary_anchor_resources_refresh_and_painted_ack(retention_browser):
    rig = retention_browser
    page = rig.page
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    with page.expect_response('**/api/mesh/chat_page_read'):
        _render(rig, 'first', _page(_ids(400, 200), 'c400'))
    page.wait_for_function('!fixture.snapshot().ackInflight')
    _render(rig, 'older', _page(_ids(200, 200), 'c200'))
    state = _render(rig, 'older', _page(_ids(1, 199), 'c1'))
    assert state['count'] == 599
    state = _render(rig, 'older', _page(['m0'], 'c0'))
    assert state['count'] == 600
    page.evaluate('fixture.clearAcks();fixture.seed();fixture.remember("m100")')
    before = page.evaluate('fixture.position("m100")')
    state = _render(rig, 'older', _page(['m-1'], 'c-1'))
    assert state['count'] == 401 and state['ids'] == _ids(-1, 401)
    assert state['continuation'] == 'c-1'
    assert state['selected'] == state['expanded'] == ['m100']
    assert page.evaluate('fixture.sameNode("m100")')
    assert abs(page.evaluate('fixture.offset("m100")') - before['candidates'][0]['offset']) <= 1
    page.evaluate('fixture.markAtBottom()')
    assert page.evaluate('fixture.snapshot().ackBodies') == []

    # Refill 600 rows and pause a three-page refresh after its second fetch.
    _render(rig, 'first', _page(_ids(400, 200), 'd400'))
    _render(rig, 'older', _page(_ids(200, 200), 'd200'))
    _render(rig, 'older', _page(_ids(0, 200), 'd0'))
    page.evaluate('fixture.clearAcks();fixture.seed(["m150","m151"]);fixture.remember("m151")')
    anchor = page.evaluate('fixture.position("m150")')
    next_candidate = next(row for row in anchor['candidates'] if row['id'] == 'm151')
    rig.responses.extend([
        _page(_ids(400, 200), 'r400', 'v2', read_cutoff_ns='9007199254740993999',
              read_ack_token='b' * 64),
        _page(_ids(200, 200), 'r200', 'v2', read_cutoff_ns='9007199254740993500',
              read_ack_token='c' * 64),
        _page(['replacement', *_ids(0, 150), *_ids(151, 49)], None, 'v2',
              read_cutoff_ns='9007199254740993000', read_ack_token='d' * 64),
    ])
    page.evaluate('fixture.holdSecond();fixture.startRefresh()')
    page.wait_for_function('fixture.snapshot().held')
    staged = page.evaluate('fixture.snapshot()')
    assert staged['ids'] == _ids(0, 600) and staged['count'] == 600
    assert staged['token'] == 'a' * 64 and staged['version'] == 'v1'
    page.evaluate('fixture.release()')
    state = page.evaluate('fixture.snapshot()')
    assert state['ids'] == state['messages'] == state['snapshotIds']
    assert state['count'] == 600 and 'm150' not in state['ids']
    assert state['rows'] == [f'm:{mid}' for mid in state['ids']]
    assert state['selected'] == state['expanded'] == ['m151']
    assert state['token'] == 'b' * 64 and state['version'] == 'v2'
    assert state['cutoff'] == '9007199254740993999'
    assert page.evaluate('fixture.sameNode("m151")')
    assert abs(page.evaluate('fixture.offset("m151")') - next_candidate['offset']) <= 1
    assert state['ackBodies'] == []

    # Only a painted latest window may supply the exact token/version body.
    latest = _page(['latest'], None, 'v3', read_ack_token='e' * 64,
                   read_cutoff_ns='9007199254740994111')
    with page.expect_response('**/api/mesh/chat_page_read'):
        state = _render(rig, 'first', latest)
    assert state['ackBodies'] == [{
        'chat_id': 'room', 'page_version': 'v3', 'read_ack_token': 'e' * 64}]
    assert rig.acknowledgments[-1] == state['ackBodies'][0]
    assert state['unread'] == 7 and state['forced'] is True

    # An impossible completed zero-raw replacement keeps the admitted DOM.
    # A later canonical page replaces it; a true visibility cut with examined
    # raw rows is still allowed to empty it.
    rig.responses.append(_page([], None, 'v4', raw_examined=0))
    page.evaluate('fixture.render("first")')
    retained = page.evaluate('fixture.snapshot()')
    assert retained['ids'] == ['latest']
    assert retained['snapshotIds'] == []
    state = _render(rig, 'first', _page(['latest', 'new'], None, 'v5'))
    assert state['ids'] == ['latest', 'new']
    state = _render(rig, 'first', _page([], None, 'v6', raw_examined=2))
    assert state['ids'] == []

    # Refill once before checking that an unpainted response cannot publish
    # its read token or replace the retained transcript.
    state = _render(rig, 'first', _page(['latest'], None, 'v7',
                    read_ack_token='e' * 64,
                    read_cutoff_ns='9007199254740994111'))
    page.evaluate('fixture.rejectPaint()')
    rig.responses.append(_page(['unpainted'], None, 'v8', read_ack_token='f' * 64))
    page.evaluate('fixture.render("first")')
    state = page.evaluate('fixture.snapshot()')
    assert state['ids'] == ['latest']
    assert state['token'] == 'e' * 64 and state['version'] == 'v7'
    page.evaluate('fixture.retire();fixture.markAtBottom()')
    assert page.evaluate('fixture.snapshot().ackBodies') == state['ackBodies']
    assert rig.responses == [] and errors == []
