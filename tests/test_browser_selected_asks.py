"""Real Chromium polling/DOM with disposable controlled responses, no live transport."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api', reason='requires optional Playwright')
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('global_first', [False, True])
def test_selected_asks_render_without_waiting_for_global_inventory(global_first):
    executable = os.environ.get('AGENTBRIDGE_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
    if not executable:
        pytest.skip('requires installed Chromium')
    source = (ROOT / 'gui/static/js/chat.js').read_text(encoding='utf-8')
    body = source[source.index('let askPollRequest = null;'):
                  source.index('function renderAskBar(', source.index('let askPollRequest = null;'))]
    harness = r'''
const Mesh={chatId:'room',askPollId:null}, App={page:'chats',routeSeq:1};
const binding={instance_id:'app',session_generation:'1',viewer:'alice'};
const $=selector=>document.querySelector(selector);
Object.defineProperty(document,'hasFocus',{value:()=>true});
const BrowserSession={snapshot:()=>({binding})};
const captureSessionEpoch=()=>({epoch:1}), meshStateSnapshot=()=>({lockEpoch:0});
const sessionMayApply=()=>true, samePageBinding=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const syncAskDots=asks=>$('#dots').textContent=asks.map(a=>a.id).join(',');
const renderAskBar=(chat,asks,timers)=>$('#ask-bar').textContent=asks.map(a=>a.id).join(',');
const notifyAsk=()=>{};
const pending=[];
const api=(path,_body,options)=>new Promise(resolve=>pending.push({path,resolve,options}));
const response=asks=>({ok:true,asks,timers:[],rooms_complete:true,peer_complete:true,
  timers_complete:true,resolved_room_ids:['room'],session_binding:binding});
__BODY__
window.poll={start:startAskPoll,selected:()=>{
  pending.find(r=>r.path.includes('?chat=')).resolve(response([{id:'selected',chat_id:'room'}]));
},global:()=>{
  pending.find(r=>!r.path.includes('?')).resolve(response([
    {id:'stale-global',chat_id:'room'}, {id:'peer',kind:'peer',chat_id:''}]));
},count:()=>pending.length};
'''.replace('__BODY__', body)
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable, headless=True,
                                         args=['--no-sandbox'])
        try:
            page = browser.new_page()
            page.set_content('<div id="ask-bar"></div><div id="dots"></div>')
            page.add_script_tag(content=harness)
            page.evaluate('poll.start()')
            assert page.evaluate('poll.count()') == 2
            if global_first:
                page.evaluate('poll.global()')
                page.wait_for_function("document.querySelector('#ask-bar').textContent==='stale-global,peer'")
            page.evaluate('poll.selected()')
            expected = 'selected,peer' if global_first else 'selected'
            page.wait_for_function('expected=>document.querySelector("#ask-bar").textContent===expected', arg=expected)
            if not global_first:
                # The broad promise remains unresolved when selected cards paint.
                page.evaluate('poll.global()')
                page.wait_for_function("document.querySelector('#ask-bar').textContent==='selected,peer'")
            assert page.locator('#dots').inner_text() == 'selected,peer'
        finally:
            browser.close()
