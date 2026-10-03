"""Execute the production refresh policy and SSE owner with deterministic JS clocks."""
from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1] / 'gui' / 'static' / 'js'
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')


def _run(tmp_path, script):
    path = tmp_path / 'test.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(path)], capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_healthy_stream_has_no_broad_timer_and_fallback_rechecks_health(tmp_path):
    source = (ROOT / 'refresh-policy.js').read_text(encoding='utf-8')
    _run(tmp_path, "import assert from 'node:assert/strict';\n" + source + r'''
let live=false, oldLive=false, hidden=false, reads=0, serial=0;
const timers=new Map();
const p=createRefreshPolicy({refresh:async()=>{reads++},healthy:()=>live,
 connected:()=>oldLive,background:()=>hidden,
 schedule:(fn,ms)=>{timers.set(++serial,{fn,ms});return serial},cancel:id=>timers.delete(id)});
p.start(); assert.equal(timers.size,1); assert.equal([...timers.values()][0].ms,2500);
const stale=[...timers.values()][0].fn;
live=true; p.changed(); assert.equal(timers.size,0);
await stale(); assert.equal(reads,0); assert.equal(timers.size,0);
live=false; oldLive=true; p.changed(); assert.equal([...timers.values()][0].ms,20000);
oldLive=false; p.changed();
const [id,timer]=[...timers][0];timers.delete(id);await timer.fn();
assert.equal(reads,1);assert.equal(timers.size,1);
p.stop();assert.equal(timers.size,0);
await [...timers.values()][0]?.fn();assert.equal(reads,1);
''')


_JS_ENV = r'''
import assert from 'node:assert/strict';
let now=10000, epoch=1, locked=false, lockEpoch=0, serial=0;
const timers=new Map(), events=new Map(), windowEvents=new Map();
const setTimeout=(fn,ms)=>{timers.set(++serial,{fn,ms});return serial};
const clearTimeout=id=>timers.delete(id);
const document={hidden:false,hasFocus:()=>true,
 addEventListener:(name,fn)=>{const list=events.get(name)||[];list.push(fn);events.set(name,list)},
 dispatchEvent:event=>{for(const fn of events.get(event.type)||[])fn(event)}};
const window={addEventListener:(name,fn)=>windowEvents.set(name,fn)};
class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail}}
const performance={now:()=>now};
const Date={now:()=>now};
const Mesh={state:{user:'viewer'}},caps={sse:true,sse_refresh_v1:true};
const isV2=()=>true,meshCaps=()=>caps,captureSessionEpoch=()=>({epoch});
const sessionMayApply=t=>t.epoch===epoch;
const meshStateSnapshot=()=>({locked,lockEpoch});
let activity=0,broad=0,scoped=[],notifications=[];
const api=async()=>{activity++;return {}},diagnostic=()=>{};
const handleNotifyFrame=frame=>notifications.push(frame);
const requestAnimationFrame=fn=>fn();
const V={refresh:async()=>{broad++},refreshRealtime:async frames=>{scoped.push(frames)}};
class EventSource{
 static CLOSED=2;static instances=[];
 constructor(){this.readyState=1;this.closed=false;EventSource.instances.push(this)}
 close(){this.closed=true}
}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const tick=async ms=>{
 const found=[...timers].find(([,t])=>t.ms===ms);assert.ok(found,`no timer ${ms}`);
 timers.delete(found[0]);found[1].fn();await flush();
};
'''


def _realtime_source():
    source = (ROOT / 'realtime.js').read_text(encoding='utf-8')
    source = re.sub(r'^import[\s\S]*?;\n', '', source, flags=re.MULTILINE)
    return source.replace('export ', '')


def test_reconnect_activation_and_stale_callbacks_have_owned_catchup(tmp_path):
    _run(tmp_path, _JS_ENV + _realtime_source() + r'''
startRealtime();const first=EventSource.instances[0];first.onopen();
assert.equal(realtimeActive(),true);await tick(0);assert.equal(broad,1);
first.onopen();await tick(0);assert.equal(broad,2);
windowEvents.get('focus')();document.dispatchEvent(new CustomEvent('visibilitychange'));
await tick(0);assert.equal(broad,3);
first.onmessage({data:JSON.stringify({type:'heartbeat'})});await flush();
assert.equal(broad,3);assert.equal(scoped.length,0);assert.equal(notifications.length,0);
const oldMessage=first.onmessage,oldOpen=first.onopen,oldError=first.onerror;
epoch++;document.dispatchEvent(new CustomEvent('ab:session-reset'));
assert.equal(first.closed,true);assert.equal(realtimeActive(),false);
oldOpen();oldMessage({data:JSON.stringify({type:'message',notify:{preview:'old secret'}})});oldError();
await flush();assert.equal(broad,3);assert.equal(notifications.length,0);
assert.ok(![...timers.values()].some(t=>t.ms===4000||t.ms===45000));
''')


def test_burst_scoped_work_does_not_serialize_selected_behind_sidebar(tmp_path):
    _run(tmp_path, _JS_ENV + _realtime_source() + r'''
let release;const held=new Promise(resolve=>{release=resolve});
V.refreshRealtime=frames=>{scoped.push(frames);return scoped.length===1?held:Promise.resolve()};
startRealtime();const src=EventSource.instances[0];src.onopen();await tick(0);
for(let i=0;i<50;i++)src.onmessage({data:JSON.stringify({type:'message',chat_id:'other',id:String(i)})});
await tick(0);assert.equal(scoped.length,1);assert.equal(scoped[0].length,50);
src.onmessage({data:JSON.stringify({type:'message',chat_id:'selected',id:'latest'})});
await tick(0);assert.equal(scoped.length,2);assert.equal(scoped[1][0].id,'latest');
release();await flush();assert.equal(broad,1);
stopRealtime();
''')


def test_half_open_stream_watchdog_reconnects_without_steady_broad_poll(tmp_path):
    _run(tmp_path, _JS_ENV + _realtime_source() + r'''
startRealtime();const src=EventSource.instances[0];src.onopen();await tick(0);
now+=30000;src.onmessage({data:JSON.stringify({type:'heartbeat'})});
await flush();assert.equal(broad,1);
now+=45001;await tick(45000);
assert.equal(src.closed,true);assert.equal(EventSource.instances.length,2);
assert.equal(realtimeActive(),false);assert.equal(broad,2);
const fresh=EventSource.instances[1];fresh.onopen();await tick(0);
assert.equal(broad,3);assert.equal(realtimeActive(),true);
stopRealtime();
''')


def test_lock_control_suppresses_late_notifications_and_retry_resurrection(tmp_path):
    _run(tmp_path, _JS_ENV + _realtime_source() + r'''
startRealtime();const src=EventSource.instances[0];src.onopen();await tick(0);
const message=src.onmessage;
src.onmessage({data:JSON.stringify({type:'control',reason:'locked'})});
assert.equal(src.closed,true);assert.equal(realtimeActive(),false);
message({data:JSON.stringify({type:'message',notify:{preview:'private'}})});
await flush();assert.equal(notifications.length,0);
locked=true;lockEpoch++;startRealtime();assert.equal(EventSource.instances.length,1);
''')


def test_scoped_router_avoids_off_room_transcript_and_keeps_new_directory(tmp_path):
    source = (ROOT / 'chat.js').read_text(encoding='utf-8')
    router = re.search(r'V\.refreshRealtime = async frames => \{[\s\S]*?\n\};', source).group()
    _run(tmp_path, r'''
import assert from 'node:assert/strict';
const App={page:'chats'},Mesh={state:{user:'me'},chatId:'selected'};
const meshStateSnapshot=()=>({locked:false}),meshCaps=()=>({chat_page_v1:true});
let pages=0,sidebars=0,aux=0,broad=0;
const V={refresh:async()=>{broad++}};
const renderPagedChat=async(_force,_kind,options)=>{
 assert.equal(options.sidebar,false);assert.equal(options.realtime,true);pages++;
};
const refreshRealtimeSidebar=async()=>{sidebars++};
const refreshRealtimeAux=async()=>{aux++};
''' + router + r'''
await V.refreshRealtime([{type:'message',chat_id:'other'}]);
assert.deepEqual([pages,sidebars,aux,broad],[0,1,0,0]);
await V.refreshRealtime([{type:'message',chat_id:'selected'}]);
assert.deepEqual([pages,sidebars,aux,broad],[1,2,0,0]);
await V.refreshRealtime([{type:'read_model',scope:'aux',chat_id:''}]);
assert.deepEqual([pages,sidebars,aux,broad],[1,2,1,0]);
await V.refreshRealtime([{type:'read_model',scope:'sidebar',chat_id:'other'}]);
assert.deepEqual([pages,sidebars,aux,broad],[1,3,1,0]);
App.page='new';await V.refreshRealtime([{type:'read_model',scope:'sidebar'}]);
assert.equal(broad,1);
App.page='settings';await V.refreshRealtime([{type:'mirror_update'}]);
assert.equal(broad,1);
''')


def test_last_sidebar_failure_retries_with_backoff_and_session_fence(tmp_path):
    source = (ROOT / 'chat.js').read_text(encoding='utf-8')
    section = source[source.index('let realtimeSidebarSerial = '):
                     source.index('async function refreshRealtimeAux()')]
    _run(tmp_path, _JS_ENV + r'''
const App={page:'chats',routeSeq:1};
let attempts=0,renders=0,fail=true,incomplete=false;
const deps={document,App,captureSessionEpoch,sessionMayApply,meshStateSnapshot,
 setTimeout,clearTimeout,captureMeshStateRead:()=>({}),
 sidebarRead:{request:async(read,current)=>current()?read().catch(()=>null):null},
 api:async()=>{attempts++;if(fail)throw Error('offline');return {chats_complete:!incomplete}},
 applyMeshState:()=>true,renderSidebar:()=>{renders++}};
const build=new Function(...Object.keys(deps),__SOURCE__+';return refreshRealtimeSidebar;');
const refresh=build(...Object.values(deps));
await refresh();assert.equal(attempts,1);
for(const delay of [500,1000,2000,4000,8000,8000]){await tick(delay)}
assert.equal(attempts,7);
fail=false;incomplete=true;await tick(8000);assert.equal(renders,1);
incomplete=false;await tick(8000);assert.equal(renders,2);
assert.equal(timers.size,0);
fail=true;await refresh();const callback=[...timers.values()][0].fn;
epoch++;document.dispatchEvent(new CustomEvent('ab:session-reset'));
assert.equal(timers.size,0);const before=attempts;callback();await flush();
assert.equal(attempts,before);
'''.replace('__SOURCE__', json.dumps(section)))


def test_lock_epoch_retires_stream_before_locked_boolean_is_assigned(tmp_path):
    _run(tmp_path, _JS_ENV + _realtime_source() + r'''
startRealtime();const src=EventSource.instances[0];src.onopen();await tick(0);
assert.equal(locked,false);
lockEpoch++;document.dispatchEvent(new CustomEvent('ab:lock-epoch'));
assert.equal(src.closed,true);assert.equal(realtimeActive(),false);
locked=true;src.onopen();assert.equal(realtimeActive(),false);
''')
