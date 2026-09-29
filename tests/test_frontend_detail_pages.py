"""Collection windows and async details reads against the actual browser helper."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_collection_bounds_versions_sparse_pages_and_async_ownership(tmp_path):
    source = (ROOT / "gui/static/js/detail_pages.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    source = source.replace("export const ", "const ").replace("export function ", "function ")
    source = source.replace("export async function ", "async function ")
    script = r'''
import assert from 'node:assert/strict';
const Mesh={chatId:'room', searchQ:'query'};
let owner=1, current=true, loading=0, finished=0, requests=[];
const pane={}; const $=selector=>pane;
const viewReadMayApply=token=>token?.epoch===owner;
const beginDetailsRead=()=>({owner:{epoch:owner}});
const detailsReadCurrent=(ticket,response)=>current && ticket.owner.epoch===owner
  && (!response?.session_binding || response.session_binding===owner);
const finishDetailsRead=()=>{finished++};
const beginLoading=(_host,options)=>{loading++; assert.equal(options.placement,'center');
  return ()=>{finished++};};
let responses=[];
const api=path=>{requests.push(path); const response=responses.shift();
  return response instanceof Error ? Promise.reject(response) : response;};
const factory=new Function('$','api','Mesh','viewReadMayApply','beginDetailsRead',
  'detailsReadCurrent','finishDetailsRead','beginLoading','TextEncoder','ICONS','esc',
  __SOURCE__+'; return {mergeCollection,readCollection,collectionControls,COLLECTION_MAX_ITEMS,COLLECTION_MAX_BYTES};');
const h=factory($,api,Mesh,viewReadMayApply,beginDetailsRead,detailsReadCurrent,
  finishDetailsRead,beginLoading,TextEncoder,{back:''},String);
const page=(v,items,continuation='next',more=true)=>({status:'page',page_version:v,
  items,continuation,has_more:more,history_exhausted:!more,meta:{name:v}});
const item=(key,size=1)=>({item_key:key,value:'x'.repeat(size)});
let merged=h.mergeCollection(null,page('v1',[item('m:0'),item('m:1')]),false);
merged=h.mergeCollection(merged,page('v1',[item('m:1'),item('m:2')]),true);
assert.deepEqual(merged.items.map(x=>x.item_key),['m:0','m:1','m:2'],
  'overlap deduplicates only the stable item key');
assert.equal(h.mergeCollection(merged,page('v2',[item('fresh')]),true).status,'reset_required');
assert.deepEqual(h.mergeCollection(merged,page('v2',[item('fresh')]),false).items.map(x=>x.item_key),['fresh']);
assert.deepEqual(h.mergeCollection(merged,{status:'pending'},true).items,[],
  'pending and failed pages never retain old rows');
let window=h.mergeCollection(null,page('v3',Array.from({length:300},(_,i)=>item('k:'+i))),false);
window=h.mergeCollection(window,page('v3',[item('last')]),true);
assert.equal(window.windowed,true); assert.deepEqual(window.items.map(x=>x.item_key),['last']);
assert.equal(h.mergeCollection(null,page('v3',[item('huge',h.COLLECTION_MAX_BYTES)]),false).status,'unavailable');
const sparse=page('v3',[],'cursor',true);
assert.match(h.collectionControls(sparse),/Load older results/);
assert.match(h.collectionControls(sparse),/Continue to older results/);
assert.doesNotMatch(h.collectionControls(sparse),/No results in this window/);
assert.match(h.collectionControls(page('v3',[],null,false)),/No results in this window/);

responses=[page('v1',[item('a')])];
let first=await h.readCollection('media');
assert.equal(first.status,'page'); assert.equal(loading,1);
assert.ok(requests[0].includes('kind=media'));
responses=[page('v1',[item('b')])];
let older=await h.readCollection('media','older');
assert.deepEqual(older.items.map(x=>x.item_key),['a','b']);
assert.ok(requests[1].includes('cursor=next'));
responses=[page('v2',[item('new')])];
assert.equal((await h.readCollection('media','older')).status,'reset_required');
responses=[page('v2',[item('new')])];
assert.deepEqual((await h.readCollection('media','latest')).items.map(x=>x.item_key),['new']);
assert.ok(!requests.at(-1).includes('cursor='),'latest resets continuation');
responses=[new Error('pending')];
assert.deepEqual((await h.readCollection('media','older')).items,[],
  'request failure clears previously rendered rows');
responses=[page('v2',[])];
let empty=await h.readCollection('media');
assert.equal(empty.status,'page'); assert.equal(empty.items.length,0);
assert.ok(empty.continuation);

// Held responses cannot become the next tab's or next session's retained data.
let resolve; responses=[new Promise(r=>resolve=r)];
const held=h.readCollection('docs'); owner++;
resolve(page('v2',[item('stale')]));
assert.equal(await held,null);
responses=[page('v2',[item('valid')])];
assert.deepEqual((await h.readCollection('docs')).items.map(x=>x.item_key),['valid']);
current=false; responses=[new Error('stale')];
assert.equal(await h.readCollection('docs','older'),null);
assert.ok(finished>=loading,'loaders are always settled');
'''.replace("__SOURCE__", json.dumps(source))
    path = tmp_path / "detail_pages.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_search_query_return_repaints_identical_results_after_clearing(tmp_path):
    source = (ROOT / "gui/static/js/search.js").read_text()
    start = source.index("async function renderChatSearch(")
    end = source.index("\nV.renderChatSearch", start)
    function = source[start:end]
    script = r'''
import assert from 'node:assert/strict';
const Mesh={chatId:'room',searchView:true,searchQ:'query',detailsKey:''};
const pane={dataset:{},innerHTML:'',querySelector:()=>({})};
const results={dataset:{},innerHTML:'',querySelectorAll:()=>[]};
const input={value:'',focus(){},addEventListener(_event,fn){this.onInput=fn}};
const back={addEventListener(){}};
const $=selector=>({ '#details-pane':pane,'#cs-back':back,'#cs-input':input,
  '#cs-results':results})[selector];
const readCollection=async()=>({status:'page',items:[{id:'m1',from:'a',body:'query body',ts:'2026'}],
  current:()=>true,continuation:null,has_more:false,history_exhausted:true});
const factory=new Function('Mesh','$','ICONS','readCollection','collectionControls',
  'invalidateDetailsRead','V','clearTimeout','setTimeout','esc','dayLabel','timeOnly','meshDn',
  'let queryTimer=null;'+__SOURCE__+'; return renderChatSearch;');
const render=factory(Mesh,$,{back:'',search:''},readCollection,()=>'',()=>{},
  {},()=>{},()=>0,String,()=>'',()=>'',String);
await render(); assert.match(results.innerHTML,/search-hit/);
input.value='q'; input.onInput();
await render(); assert.match(results.innerHTML,/Enter at least two characters/);
input.value='query'; input.onInput();
await render('latest');
assert.match(results.innerHTML,/search-hit/,
  'identical latest results must repaint after the input handler cleared them');
'''.replace("__SOURCE__", json.dumps(function))
    path = tmp_path / "search_repaint.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_details_loader_is_delayed_and_stale_or_finished_reads_leave_no_cue(tmp_path):
    source = (ROOT / "gui/static/js/loading.js").read_text()
    source = source.replace("export function ", "function ")
    script = r'''
import assert from 'node:assert/strict';
let timers=new Map(), next=1, current=true;
const setTimeout=(fn,delay)=>{assert.equal(delay,500); const id=next++; timers.set(id,fn); return id};
const clearTimeout=id=>timers.delete(id);
const tick=()=>{for(const [id,fn] of [...timers]){timers.delete(id);fn()}};
const host={isConnected:true,attrs:new Map(),children:[],classList:{add(){},remove(){}},
  getAttribute(name){return this.attrs.get(name)??null},
  setAttribute(name,value){this.attrs.set(name,value)},
  removeAttribute(name){this.attrs.delete(name)},
  querySelector(){return null},appendChild(node){this.children.push(node)}};
const document={querySelector:()=>null,createElement:()=>({children:[],className:'',
  setAttribute(){},appendChild(node){this.children.push(node)},remove(){this.removed=true}})};
const factory=new Function('document','setTimeout','clearTimeout',
  __SOURCE__+'; return {beginLoading,endLoading};');
const h=factory(document,setTimeout,clearTimeout);
let finish=h.beginLoading(host,{current:()=>current});
assert.equal(host.children.length,0,'short read has no spinner');
finish(); tick(); assert.equal(host.children.length,0);
finish=h.beginLoading(host,{current:()=>current}); tick();
assert.equal(host.children.length,1,'slow read shows the cue');
assert.equal(host.attrs.get('aria-busy'),'true');
finish(); assert.equal(host.children[0].removed,true); assert.equal(host.attrs.has('aria-busy'),false);
current=false; finish=h.beginLoading(host,{current:()=>current}); tick();
assert.equal(host.children.length,1,'retired read never shows the cue'); finish();
'''.replace("__SOURCE__", json.dumps(source))
    path = tmp_path / "details_loader.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
