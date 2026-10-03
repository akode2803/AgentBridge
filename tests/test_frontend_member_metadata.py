"""Execute the actual member modals across bounded-read and ownership transitions."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _run(tmp_path, script):
    source = (ROOT / "gui/static/js/members.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    program = _HARNESS.replace("__SOURCE__", json.dumps(source)) + script
    runner = tmp_path / "members.mjs"
    runner.write_text(program)
    result = subprocess.run([shutil.which("node"), str(runner)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


_HARNESS = r'''
import assert from 'node:assert/strict';
function deferred() { let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no});
  return {promise,resolve,reject}; }
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const binding={instance_id:'app',session_generation:'1',viewer:'viewer'};
function fixture({mode='bound',freshCaps={chat_page_v1:true},globalCaps={},bootstrapCaps={}}={}) {
  let identity={session:1,route:1,page:'chats',chat:'A',details:true,selection:1,lock:1};
  let locked=false,modalOwner={},active=null;
  const calls=[],opened=[],events=[],toasts=[],rows=[],picked=[];
  let queue=[];
  const Mesh={chatId:'A',state:{caps:globalCaps,marker:'global-original'},structKey:'old',detailsKey:'old'};
  const App={state:{caps:bootstrapCaps}};
  const meshCaps=()=>Mesh.state?.caps || App.state?.caps || {};
  const captureModalRead=()=>({owner:modalOwner,view:{...identity}});
  const modalReadMayApply=(ticket,response)=>ticket?.owner===modalOwner && !locked
    && Object.keys(identity).every(key=>ticket.view[key]===identity[key])
    && (response===undefined || mode==='legacy' || JSON.stringify(response?.session_binding)===JSON.stringify(binding));
  const closeModal=()=>{modalOwner={};if(active)active.isConnected=false;active=null;};
  const beginModalRead=()=>{closeModal();return captureModalRead();};
  const openModal=html=>{closeModal();const controls=new Map();
    const box={html,isConnected:true,querySelector(selector){
      if(!controls.has(selector))controls.set(selector,{disabled:false,addEventListener(_event,fn){this.click=fn;}});
      return controls.get(selector);}};
    opened.push(box);active=box;return box;};
  const api=(path,body,options)=>{calls.push({path,body,options});assert.ok(queue.length,'unexpected request '+path);
    const value=queue.shift();return value instanceof Error?Promise.reject(value):Promise.resolve(value?.promise||value);};
  const user=(username,kind='human',extra={})=>({username,display:username,kind,...extra});
  const state=()=>({user:'viewer',users:{viewer:user('viewer'),zed:user('zed'),alpha:user('alpha'),
    bot:user('bot','agent',{owners:['zed']}),gone:user('gone','human',{departed:true})},
    caps:freshCaps,session_binding:binding});
  const meta=()=>({id:'A',members:['alpha','viewer','zed'],admins:['viewer'],roles:{viewer:'admin'}});
  const summary=(changes={})=>({status:'ready',chat_id:'A',meta:meta(),session_binding:binding,...changes});
  const legacy=(changes={})=>({meta:meta(),session_binding:binding,...changes});
  const deps={api,Mesh,meshCaps,beginModalRead,captureModalRead,modalReadMayApply,openModal,closeModal,
    bindModalFilter(){},bindPicker(box,fn){box.pick=fn;picked.push(box);},
    pickerRow(value){rows.push(value);return `<row>${value.value}</row>`;},
    pickerSection(_title,html){return html;},pickerFooter(){return '<footer>add</footer>';},
    esc:String,toast:(...args)=>toasts.push(args),ICONS:{close:'',search:'',check:''},
    meshDn:(name,context)=>context.users[name]?.display||name,meshAvatarInner:()=>'',
    agentIdentityBadge:()=>'<agent/>',V:{renderChats(){events.push('render');}},
    document:{dispatchEvent(event){events.push(event.type);}},CustomEvent:class {constructor(type){this.type=type;}}};
  const build=new Function(...Object.keys(deps),__SOURCE__+`;
    return {showAddMembers,showSearchMembers,readMemberMetadata,memberSummarySupported,
      afterRead(fn){const original=readMemberMetadata;readMemberMetadata=async(...args)=>{
        const result=await original(...args);fn(result);return result;};}};`);
  const funcs=build(...Object.values(deps));
  return {funcs,calls,opened,events,toasts,rows,picked,Mesh,App,state,meta,summary,legacy,
    enqueue(...items){queue.push(...items);},get active(){return active;},get queued(){return queue.length;},
    close:closeModal,mutate(key){if(key==='modal')closeModal();else if(key==='locked')locked=true;
      else identity[key]=typeof identity[key]==='number'?identity[key]+1:String(identity[key])+'-new';},
    ticket:beginModalRead};
}
const invoke=(f,kind)=>f.funcs[kind==='add'?'showAddMembers':'showSearchMembers']('A');
const paths=f=>f.calls.map(call=>call.path);
'''


def test_member_routes_and_exact_rendered_roster(tmp_path):
    _run(tmp_path, r'''
for(const kind of ['add','search'])for(const config of [
  {freshCaps:{chat_page_v1:true},expected:'chat_summary'},
  {freshCaps:{chat_page_v1:'supported'},expected:'chat_summary'},
  {freshCaps:{},globalCaps:{chat_page_v1:true},expected:'chat_summary'},
  {freshCaps:{chat_page_v1:false},globalCaps:{chat_page_v1:true},expected:'chat_summary'},
  {freshCaps:null,globalCaps:null,bootstrapCaps:{chat_page_v1:true},expected:'chat_summary'},
  {freshCaps:{},globalCaps:{},bootstrapCaps:{chat_page_v1:true},expected:'chat'},
  {freshCaps:{chat_page_v1:false},expected:'chat'},
  {mode:'legacy',freshCaps:{},expected:'chat'},
]) {
  const f=fixture(config);const data=config.expected==='chat_summary'?f.summary():f.legacy();
  if(config.mode==='legacy'){delete data.session_binding;}
  const state=f.state();if(config.mode==='legacy'){delete state.session_binding;}
  f.enqueue(state,data);await invoke(f,kind);
  assert.deepEqual(paths(f),['/api/mesh/state',`/api/mesh/${config.expected}?id=A`]);
  assert.equal(f.opened.length,1);assert.equal(f.Mesh.state?.marker??'global-original','global-original');
  for(const call of f.calls){assert.equal(call.options.sideEffects,false);assert.equal(call.options.timeoutMs,15000);}
  if(kind==='add')assert.deepEqual(f.rows.map(row=>row.value),['bot'],'exclude all existing members, viewer, departed');
  else {const html=f.active.html;assert.ok(html.indexOf('@alpha')<html.indexOf('@viewer'));
    assert.ok(html.indexOf('@viewer')<html.indexOf('@zed'),'preserve server canonical order');
    assert.ok(!html.includes('owner-chip'),'admins never become an invented owner');}
}
// The optional single-owner field remains a legacy presentation input only.
const old=fixture({mode:'legacy',freshCaps:{}});const legacy=old.legacy();legacy.meta.owner='zed';
old.enqueue(old.state(),legacy);await invoke(old,'search');assert.ok(old.active.html.includes('owner-chip'));
''')


def test_member_nonready_malformed_and_foreign_responses_never_render_roster(tmp_path):
    _run(tmp_path, r'''
for(const kind of ['add','search'])for(const changes of [
  {status:'pending'},{status:'restart'},{status:'unavailable'},{status:'forbidden'},
  {status:'reset_required'},{status:'page'},{status:undefined},{error:'server failure'},
  {chat_id:'B'},{meta:null},{meta:{}},{meta:{id:'B',members:['secret-stale']}},
  {meta:{id:'A',members:null}},{meta:{id:'A',members:'secret-stale'}},
  {meta:{id:'A',members:['secret-stale',42]}},{meta:{id:'A',members:['x','x']}},
]) {
  const f=fixture();f.enqueue(f.state(),f.summary(changes));await invoke(f,kind);
  assert.equal(f.opened.length,1);assert.ok(f.active.html.includes('mr-retry'));
  assert.ok(!f.active.html.includes('mem-row')&&!f.active.html.includes('<row>'));
  assert.ok(!f.active.html.includes('secret-stale'));assert.equal(paths(f).length,2);
  assert.ok(!paths(f).includes('/api/mesh/chat?id=A'));
}
for(const kind of ['add','search'])for(const stage of ['state','metadata'])for(const reply of [
  {session_binding:{...binding,viewer:'other'}},
  {error:'foreign',session_binding:{...binding,session_generation:'2'}},
  {error:'locked',locked:true,session_binding:{...binding,viewer:'other'}},
]){
  const f=fixture();f.enqueue(...(stage==='state'?[reply]:[f.state(),reply]));await invoke(f,kind);
  assert.equal(f.opened.length,0);assert.deepEqual(f.events,[]);
  assert.equal(f.calls.length,stage==='state'?1:2);
}
for(const kind of ['add','search'])for(const stage of ['state','metadata'])for(const reply of [null,undefined,0,[]]){
  const f=fixture();f.enqueue(...(stage==='state'?[reply]:[f.state(),reply]));await invoke(f,kind);
  assert.equal(f.opened.length,1);assert.ok(f.active.html.includes('mr-retry'));assert.deepEqual(f.events,[]);
  assert.equal(f.calls.length,stage==='state'?1:2);
}
for(const kind of ['add','search'])for(const value of [new Error('offline'),{error:'server failed'}]){
  const f=fixture();f.enqueue(f.state(),value);await invoke(f,kind);
  assert.ok(f.active.html.includes('mr-retry'));assert.equal(f.calls.length,2);
}
for(const value of [{error:'locked',locked:true},{status:'locked',session_binding:binding}]){
  const f=fixture();f.enqueue(f.state(),value);await invoke(f,'search');
  assert.equal(f.opened.length,0);assert.deepEqual(f.events,['ab:locked']);
}
''')


def test_member_deferred_read_and_helper_handoff_ownership(tmp_path):
    _run(tmp_path, r'''
for(const kind of ['add','search'])for(const stage of ['state','metadata'])
for(const change of ['session','route','page','chat','details','selection','lock','locked','modal'])
for(const rejection of [false,true]){
  const f=fixture();const held=deferred();f.enqueue(...(stage==='state'?[held]:[f.state(),held]));
  const pending=invoke(f,kind);await tick();f.mutate(change);
  if(rejection)held.reject(new Error('old'));else held.resolve(stage==='state'?f.state():f.summary());
  await pending;assert.equal(f.opened.length,0,`${kind}/${stage}/${change}`);assert.deepEqual(f.events,[]);
  assert.equal(f.calls.length,stage==='state'?1:2);
}
// A helper is an extra await boundary: the consumer must also check ownership.
for(const kind of ['add','search'])for(const change of ['session','route','modal']){
  const f=fixture();f.funcs.afterRead(()=>f.mutate(change));f.enqueue(f.state(),f.summary());
  await invoke(f,kind);assert.equal(f.opened.length,0);
}
// A newer modal owns its result even if an older summary resolves afterward.
const f=fixture();const held=deferred();f.enqueue(f.state(),held);
const older=invoke(f,'search');await tick();f.enqueue(f.state(),f.summary());await invoke(f,'add');
const current=f.active;held.resolve(f.summary());await older;
assert.equal(f.active,current);assert.equal(f.opened.length,1);assert.ok(current.isConnected);
''')


def test_member_capability_upgrade_and_no_downgrade_after_failure(tmp_path):
    _run(tmp_path, r'''
for(const kind of ['add','search'])for(const outcome of ['ready','error','rejection']){
  const f=fixture({freshCaps:{},globalCaps:{}}),held=deferred();f.enqueue(f.state(),held,f.summary());
  const pending=invoke(f,kind);await tick();f.Mesh.state.caps={chat_page_v1:true};
  if(outcome==='rejection')held.reject(new Error('old failure'));
  else held.resolve(outcome==='error'?{error:'old failure'}:f.legacy());
  await pending;assert.deepEqual(paths(f),['/api/mesh/state','/api/mesh/chat?id=A','/api/mesh/chat_summary?id=A']);
  assert.equal(f.opened.length,1);assert.ok(!f.active.html.includes('mr-retry'));
}
// Capability can change after the loader resolves but before its consumer paints.
for(const kind of ['add','search']){
  const f=fixture({freshCaps:{},globalCaps:{}});let n=0;
  f.funcs.afterRead(()=>{f.Mesh.state.caps=n++===0?{chat_page_v1:true}:{};});
  f.enqueue(f.state(),f.legacy(),f.summary());await invoke(f,kind);
  assert.deepEqual(paths(f),['/api/mesh/state','/api/mesh/chat?id=A','/api/mesh/chat_summary?id=A']);
  assert.equal(f.opened.length,1);
}
for(const kind of ['add','search'])for(const outcome of ['pending','forbidden','unavailable','rejection']){
  const f=fixture({freshCaps:{},globalCaps:{chat_page_v1:true}}),held=deferred();
  f.enqueue(f.state(),held);const pending=invoke(f,kind);await tick();f.Mesh.state.caps={};
  if(outcome==='rejection')held.reject(new Error('summary failed'));else held.resolve(f.summary({status:outcome}));
  await pending;assert.deepEqual(paths(f),['/api/mesh/state','/api/mesh/chat_summary?id=A']);
  assert.ok(f.active.html.includes('mr-retry'));
}
const stale=fixture({freshCaps:{}}),held=deferred();stale.enqueue(stale.state(),held);
const pending=invoke(stale,'search');await tick();stale.Mesh.state.caps={chat_page_v1:true};stale.close();
held.resolve(stale.legacy());await pending;assert.equal(stale.calls.length,2);assert.equal(stale.opened.length,0);
''')


def test_member_retry_and_add_actions_keep_post_open_owner(tmp_path):
    _run(tmp_path, r'''
for(const kind of ['add','search']){
  const f=fixture();f.enqueue(f.state(),f.summary({status:'pending'}));await invoke(f,kind);
  const old=f.active;const retry=old.querySelector('#mr-retry').click;
  f.enqueue(f.state(),f.summary());retry();retry();await tick();await tick();
  assert.equal(f.calls.length,4,'second click is retired by the first retry');
  assert.ok(!old.isConnected);assert.ok(!f.active.html.includes('mr-retry'));
  f.close();retry();assert.equal(f.calls.length,4,'closed retry remains inert');
}
// Explicit retry retains a supported-summary obligation even if capabilities
// disappear while the failed request is in flight or while the panel is open.
for(const kind of ['add','search']){
  const f=fixture({freshCaps:{},globalCaps:{chat_page_v1:true}});
  f.enqueue(f.state(),f.summary({status:'pending'}));await invoke(f,kind);
  f.Mesh.state.caps={};f.enqueue(f.state(),f.summary());f.active.querySelector('#mr-retry').click();
  await tick();await tick();
  assert.deepEqual(paths(f),['/api/mesh/state','/api/mesh/chat_summary?id=A',
    '/api/mesh/state','/api/mesh/chat_summary?id=A']);
}
for(const change of ['session','route','modal']){
  const f=fixture();f.enqueue(f.state(),f.summary({status:'pending'}));await invoke(f,'search');
  const retry=f.active.querySelector('#mr-retry').click;f.mutate(change);retry();assert.equal(f.calls.length,2);
}
for(const change of ['session','route','modal']){
  const f=fixture();f.enqueue(f.state(),f.summary());await invoke(f,'add');const box=f.active;
  f.mutate(change);await box.pick(['bot']);assert.equal(f.calls.length,2);
}
const good=fixture();good.enqueue(good.state(),good.summary());await invoke(good,'add');
good.enqueue({ok:true},{ok:true});await good.active.pick(['bot','zed']);
assert.deepEqual(good.calls.slice(2).map(({path,body})=>({path,body})),[
  {path:'/api/mesh/add_member',body:{chat_id:'A',username:'bot'}},
  {path:'/api/mesh/add_member',body:{chat_id:'A',username:'zed'}}]);
assert.deepEqual(good.events,['render']);assert.equal(good.Mesh.structKey,'');assert.equal(good.Mesh.detailsKey,'');
const late=fixture();late.enqueue(late.state(),late.summary());await invoke(late,'add');
const held=deferred();late.enqueue(held);const action=late.active.pick(['bot','zed']);late.mutate('route');
held.resolve({ok:true});await action;assert.equal(late.calls.length,3);assert.deepEqual(late.events,[]);
const denied=fixture();denied.enqueue(denied.state(),denied.summary());await invoke(denied,'add');
denied.enqueue({error:'not permitted'});await denied.active.pick(['bot']);
assert.deepEqual(denied.toasts,[['not permitted',true]]);assert.equal(denied.active.querySelector('.pf-go').disabled,false);
''')
