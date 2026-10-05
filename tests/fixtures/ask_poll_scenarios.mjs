import assert from 'node:assert/strict';
const binding={instance_id:'app',session_generation:'1',viewer:'alice'};
const Mesh={chatId:'room',askPollId:null,askKey:'',askCounts:{}};
const App={page:'chats',routeSeq:1};
const events={},requests=[],dots=[],bars=[],notified=[];
let tick, intervalCount=0, epoch=1, lockEpoch=0, focused=true;
const document={hidden:false,hasFocus:()=>focused,
  addEventListener:(name,fn)=>{events[name]=fn;}};
const api=(path,_body,options)=>new Promise((resolve,reject)=>requests.push({path,resolve,reject,options}));
const deps={Mesh,App,document,api,$:()=>({innerHTML:''}),
  captureSessionEpoch:()=>({epoch}),meshStateSnapshot:()=>({lockEpoch}),
  BrowserSession:{snapshot:()=>({binding})},
  sessionMayApply:t=>t.epoch===epoch,samePageBinding:(a,b)=>JSON.stringify(a)===JSON.stringify(b),
  syncAskDots:asks=>dots.push(asks.map(a=>a.id)),
  renderAskBar:(chat,asks,timers)=>bars.push({chat,asks:asks.map(a=>a.id),timers:timers.map(t=>t.id)}),
  notifyAsk:a=>notified.push(a.id),
  setInterval:(fn,ms)=>{assert.equal(ms,2000);tick=fn;intervalCount++;return 1;},clearInterval:()=>{},
};
const build=new Function(...Object.keys(deps), `${__BODY__};return {startAskPoll,resetAskPollState};`);
const {startAskPoll}=build(...Object.values(deps));
const row=(id,chat='room',kind='tool')=>({id,chat_id:chat,kind});
const response=(extra={})=>({ok:true,asks:[row('r1')],timers:[row('t1')],
  asks_complete:true,rooms_complete:true,peer_complete:true,timers_complete:true,
  resolved_room_ids:['room'],forbidden:false,session_binding:binding,...extra});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const take=path=>{const i=requests.findIndex(r=>r.path===path);assert.notEqual(i,-1,path);return requests.splice(i,1)[0];};
const scoped=()=>take(`/api/mesh/asks?chat=${encodeURIComponent(Mesh.chatId)}`);
const broad=()=>take('/api/mesh/asks');
const settle=async(req,extra={})=>{req.resolve(response(extra));await flush();};
const last=()=>bars.at(-1);
const scenario=__SCENARIO__;
startAskPoll();
assert.equal(requests.length,2); // Selected and broad start in the same tick.
const firstSelected=scoped(), firstGlobal=broad();
tick();tick();assert.equal(requests.length,0); // At most one active owner per lane.
assert.equal(firstSelected.options.timeoutMs,15000);
assert.equal(firstGlobal.options.timeoutMs,15000);
assert.equal(firstSelected.options.sideEffects,false);
if(scenario==='independent') {
  await settle(firstSelected);
  assert.deepEqual(last(),{chat:'room',asks:['r1'],timers:['t1']});
  tick();assert.equal(requests.length,1); // Broad request is still held.
  await settle(scoped(),{asks:[row('r2')],timers:[]});
  assert.deepEqual(last().asks,['r2']);
  await settle(firstGlobal,{asks:[row('stale'),row('other','other'),row('peer','','peer')],resolved_room_ids:['room','other']});
  assert.deepEqual(last(),{chat:'room',asks:['r2','peer'],timers:[]});
  assert.deepEqual(dots.at(-1),['other','r2','peer']);
} else if(scenario==='global_first') {
  await settle(firstGlobal,{asks:[row('old'),row('peer','','peer')]});
  assert.deepEqual(last().asks,['old','peer']);
  await settle(firstSelected,{asks:[row('new'),row('scoped-peer','','peer')]});
  assert.deepEqual(last().asks,['new','peer']); // Scoped lane cannot replace companions.
  const count=requests.length;startAskPoll();assert.equal(requests.length,count);
  assert.equal(intervalCount,1);
} else if(scenario==='denial') {
  await settle(firstGlobal,{asks:[row('revoked'),row('peer','','peer')]});
  tick();const staleGlobal=broad();
  await settle(firstSelected,{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
  assert.equal(staleGlobal.options.signal.aborted,false);
  await settle(staleGlobal,{asks:[row('revoked'),row('peer','','peer'),row('other-ask','other')],timers:[row('other-timer','other')],resolved_room_ids:['room','other']});
  Mesh.chatId='other';App.routeSeq++;startAskPoll();
  assert.deepEqual(dots.at(-1),['other-ask','peer']);
  assert.deepEqual(last().timers,['other-timer']);
  const other=scoped();Mesh.chatId='room';App.routeSeq++;startAskPoll();
  assert.equal(other.options.signal.aborted,true);
  const revisit=scoped();await settle(revisit,{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
  assert.deepEqual(last(),{chat:'room',asks:['peer'],timers:[]});
  assert.deepEqual(dots.at(-1),['other-ask','peer']);
  tick();const pending=scoped();broad().reject(new Error('offline'));
  await settle(pending,{asks:[row('unresolved')],rooms_complete:false,timers_complete:false,resolved_room_ids:[]});
  assert.deepEqual(last().asks,['peer']);
  tick();const recovered=scoped();broad().reject(new Error('offline'));
  await settle(recovered,{asks:[row('recovered')]});
  assert.deepEqual(last().asks,['recovered','peer']);
} else if(scenario==='partial') {
  // An unresolved initial scoped response does not mask verified global rows.
  await settle(firstSelected,{asks:[],timers:[],rooms_complete:false,timers_complete:false,resolved_room_ids:[]});
  await settle(firstGlobal);
  assert.deepEqual(last().asks,['r1']);
  tick();await settle(scoped(),{asks:[row('r2')],timers:[row('t2')],rooms_complete:false,timers_complete:false});
  assert.deepEqual(last(),{chat:'room',asks:['r1','r2'],timers:['t1','t2']});
  await settle(broad(),{asks:[],timers:[],resolved_room_ids:[]});
  assert.deepEqual(last().asks,['r1','r2']);
  tick();await settle(scoped(),{asks:[],timers:[]});
  assert.deepEqual(last(),{chat:'room',asks:[],timers:[]});
} else if(scenario==='route') {
  Mesh.chatId='other';App.routeSeq++;startAskPoll();
  assert.equal(firstSelected.options.signal.aborted,true);
  const second=scoped();
  Mesh.chatId='room';App.routeSeq++;startAskPoll();
  assert.equal(second.options.signal.aborted,true);
  const third=scoped();
  await settle(firstSelected,{asks:[row('ABA-stale')]});
  await settle(second,{asks:[row('wrong','other')],resolved_room_ids:['other']});
  assert.equal(notified.length,0);
  await settle(third,{asks:[row('current')]});
  assert.deepEqual(last().asks,['current']);
  await settle(firstGlobal,{asks:[row('global-stale')]});
  assert.deepEqual(last().asks,['current']);assert.equal(intervalCount,1);
} else if(scenario==='reset') {
  for(const event of ['ab:session-reset','ab:lock-epoch']) {
    let s=firstSelected,g=firstGlobal;
    if(event==='ab:lock-epoch') {tick();s=scoped();g=broad();lockEpoch++;}
    else epoch++;
    events[event]();
    assert.equal(s.options.signal.aborted,true);assert.equal(g.options.signal.aborted,true);
    await settle(s);await settle(g);
    assert.deepEqual(dots.at(-1),[]);assert.equal(notified.length,0);
  }
} else if(scenario==='invalid') {
  await settle(firstSelected);await settle(firstGlobal);
  for(const invalid of [
    {session_binding:undefined},{rooms_complete:undefined},{peer_complete:undefined},
    {timers_complete:undefined},{resolved_room_ids:undefined},{rooms_complete:'true'},
    {resolved_room_ids:['foreign']},{resolved_room_ids:['room','room']},
    {asks:Array.from({length:1025},()=>row('large'))},
  ]) {
    tick();const s=scoped(),g=broad();g.reject(new Error('offline'));await flush();
    const before={bars:bars.length,dots:dots.length,notified:notified.length};
    await settle(s,{...invalid,asks:invalid.asks||[row('unverified')]});
    assert.deepEqual({bars:bars.length,dots:dots.length,notified:notified.length},before);
  }
} else if(scenario==='done') {
  await settle(firstSelected);await settle(firstGlobal);
  assert.deepEqual(notified,['r1']);
  Mesh.askDone.set('r1',Date.now());Mesh.timerDone.set('t1',Date.now());
  tick();await settle(scoped());await settle(broad());
  assert.deepEqual(last(),{chat:'room',asks:[],timers:[]});assert.deepEqual(notified,['r1']);
} else if(scenario==='standdown') {
  await settle(firstSelected);await settle(firstGlobal);
  document.hidden=true;tick();assert.equal(requests.length,0);
  document.hidden=false;focused=false;tick();assert.equal(requests.length,0);
  focused=true;tick();const s=scoped(),g=broad();
  Mesh.chatId='';App.routeSeq++;startAskPoll();assert.equal(s.options.signal.aborted,true);
  assert.equal(requests.length,0);
  App.page='settings';tick();assert.equal(g.options.signal.aborted,true);assert.equal(Mesh.askPollId,null);
  await settle(s);await settle(g);assert.deepEqual(dots.at(-1),[]);
} else if(scenario==='repeated_denial') {
  await settle(firstSelected,{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
  await settle(firstGlobal,{asks:[row('private'),row('peer1','','peer')]});
  for(let i=2;i<=4;i++) {
    tick();const s=scoped(),g=broad();
    await settle(s,{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
    assert.equal(g.options.signal.aborted,false);
    await settle(g,{asks:[row('private'),row('peer'+i,'','peer')]});
    assert.deepEqual(last().asks,['peer'+i]);
    Mesh.chatId='other';App.routeSeq++;startAskPoll();
    assert.deepEqual(dots.at(-1),['peer'+i]);
    const other=scoped();Mesh.chatId='room';App.routeSeq++;startAskPoll();
    assert.equal(other.options.signal.aborted,true);
    await settle(scoped(),{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
  }
} else if(scenario==='bounds') {
  await settle(firstSelected,{asks:Array.from({length:700},(_,i)=>row('selected'+i)),
    timers:Array.from({length:300},(_,i)=>row('selected-timer'+i))});
  await settle(firstGlobal,{asks:Array.from({length:1024},(_,i)=>row('other'+i,'other')),
    timers:Array.from({length:512},(_,i)=>row('other-timer'+i,'other')),resolved_room_ids:['other']});
  assert.equal(dots.at(-1).length,1024);assert.equal(last().asks.length,700);
  assert.equal(last().timers.length,300);
} else if(scenario==='failure') {
  firstSelected.reject(new Error('timeout'));firstGlobal.reject(new Error('timeout'));await flush();
  tick();assert.equal(requests.length,2);const s=scoped(),g=broad();
  lockEpoch++;await settle(s);await settle(g);
  assert.equal(notified.length,0); // Completion fence also works without the reset event.
  tick();assert.equal(requests.length,2);
} else if(scenario==='global_partial') {
  await settle(firstSelected);await settle(firstGlobal,{asks:[row('r1'),row('o1','other'),row('p1','','peer')],resolved_room_ids:['room','other']});
  tick();scoped().reject(new Error('offline'));
  await settle(broad(),{asks:[row('p2','','peer')],timers:[row('t2','other')],resolved_room_ids:[],rooms_complete:false,timers_complete:false});
  assert.deepEqual(dots.at(-1),['o1','r1','p2']);
  assert.deepEqual(last().timers,['t1']);
  tick();scoped().reject(new Error('offline'));
  await settle(broad(),{forbidden:true,asks:[],timers:[],resolved_room_ids:[]});
  assert.deepEqual(dots.at(-1),[]);assert.equal(Mesh.askKey,'');
}
