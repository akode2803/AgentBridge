"""Exact message hints reach every attachment action without stacked opens."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_file_urls_media_items_open_and_bulk_save_use_message_hints(tmp_path):
    files = (ROOT / "gui/static/js/files.js").read_text()
    files = files[files.index("export const fileUrl"):files.index("export function monthLabel")]
    files = files.replace("export const ", "const ")
    api = (ROOT / "gui/static/js/api.js").read_text()
    binder = api[api.index("export function bindOpenFile("):].replace("export function ", "function ")
    media = (ROOT / "gui/static/js/media.js").read_text()
    render = media[media.index("  const render = {"):media.index("  const body =", media.index("  const render = {"))]
    chat = (ROOT / "gui/static/js/chat.js").read_text()
    bulk = chat[chat.index("async function bulkSave("):chat.index("// ---- delete", chat.index("async function bulkSave("))]
    script = r'''
import assert from 'node:assert/strict';
const fileUrl=new Function(__FILES__+'; return fileUrl;')();
const url=fileUrl('room & one','blob/1','message #1');
assert.equal(url,'/api/mesh/file?chat=room%20%26%20one&id=blob%2F1&message_id=message%20%231');
const esc=String, fmtSize=()=>'', fmtTime=()=>'', meshDn=String, extIcon=()=>'';
const render=new Function('chatId','fileUrl','esc','fmtSize','fmtTime','meshDn','extIcon',
  'const ICONS={};'+__MEDIA__+' return render;')('room',fileUrl,esc,fmtSize,fmtTime,meshDn,extIcon);
const photo={id:'same-blob',msg_id:'message one',name:'photo.png',ts:'2026',from:'a'};
const doc={...photo,msg_id:'message two',name:'note.txt'};
const photoHtml=render.media({items:[photo]}), docHtml=render.docs({items:[doc]});
assert.match(photoHtml,/data-message-id="message one"/);
assert.match(photoHtml,/message_id=message%20one/);
assert.match(docHtml,/data-message-id="message two"/);

let calls=[], resolve;
const api=async(path,body)=>{calls.push([path,body]); return await new Promise(done=>resolve=done)};
let notices=[]; const toast=(...args)=>notices.push(args);
const bindFilePreview=()=>{};
const classes=new Set();
const button={dataset:{id:'same-blob',messageId:'message one'},_openBound:false,
  classList:{contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x)},
  addEventListener(_type,fn){this.click=fn}};
const scope={querySelectorAll:()=>[button]};
const bind=new Function('api','toast','bindFilePreview',__BINDER__+'; return bindOpenFile;')
  (api,toast,bindFilePreview);
bind(scope,'room','.mesh-att'); const firstClick=button.click;
bind(scope,'room','.mesh-att'); assert.equal(button.click,firstClick,'rebind does not stack');
const held=button.click(); await button.click();
assert.equal(calls.length,1,'in-flight double click debounced');
assert.deepEqual(calls[0],['/api/mesh/open_file',
  {chat_id:'room',id:'same-blob',message_id:'message one'}]);
resolve({ok:true}); await held;
assert.equal(classes.has('att-loading'),false);
const next=button.click(); assert.equal(calls.length,2); resolve({status:'pending'}); await next;
assert.match(notices.at(-1)[0],/not ready yet/);

const Mesh={select:{ids:new Set(['m1','m2','empty'])}};
const messages=new Map([['m1',{files:[{id:'dup'},{id:'only-one'}]}],
  ['m2',{files:[{id:'dup'}]}],['empty',{files:[]}]]);
const $=()=>({_msgs:messages});
let saved, exits=0, saveResponse={cancelled:true};
const saveApi=async(path,body)=>{saved=[path,body];
  if (saveResponse instanceof Error) throw saveResponse;
  return saveResponse;};
const bulkSave=new Function('Mesh','$','api','toast','exitSelect',__BULK__+'; return bulkSave;')
  (Mesh,$,saveApi,toast,()=>exits++);
await bulkSave('room');
assert.deepEqual(saved,['/api/mesh/save',{chat_id:'room',files:[
  {message_id:'m1',id:'dup'},{message_id:'m1',id:'only-one'},
  {message_id:'m2',id:'dup'}]}]);
assert.equal(exits,0,'cancelled folder picker retains selection');
saveResponse={error:'third file unavailable',saved:2};
await bulkSave('room');
assert.match(notices.at(-1)[0],/2 files already saved\. third file unavailable/);
assert.equal(exits,0,'partial failure retains selection');
saveResponse=new Error('network ambiguity');
await bulkSave('room');
assert.match(notices.at(-1)[0],/Check the destination before retrying/);
assert.equal(exits,0);
'''
    for marker, snippet in (("__FILES__", files), ("__MEDIA__", render),
                            ("__BINDER__", binder), ("__BULK__", bulk)):
        script = script.replace(marker, json.dumps(snippet))
    path = tmp_path / "file_hints.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
def test_preview_finite_retries_detached_node_and_success_cleanup(tmp_path):
    source = (ROOT / "gui/static/js/files.js").read_text()
    helper = source[source.index("export function bindFilePreview("):].replace(
        "export function ", "function ")
    script = r'''
import assert from 'node:assert/strict';
let timers=new Map(), next=1, delays=[];
const setTimeout=(fn,ms)=>{const id=next++; timers.set(id,fn); delays.push(ms); return id};
const clearTimeout=id=>timers.delete(id);
const tick=()=>{const batch=[...timers]; timers.clear(); batch.forEach(([,fn])=>fn())};
const document={createElement:()=>({textContent:'',remove(){this.removed=true}})};
const helper=new Function('document','setTimeout','clearTimeout',
  __HELPER__+'; return bindFilePreview;')(document,setTimeout,clearTimeout);
function mock() {
  const events={}; let src='hinted-url', loads=0;
  const img={isConnected:true,complete:false,naturalWidth:0,hidden:false,loading:'lazy',
    addEventListener(name,fn){events[name]=fn},
    get src(){return src},set src(value){src=value;loads++}};
  const button={children:[],querySelector:()=>img,appendChild(x){this.children.push(x)}};
  return {img,button,events,get loads(){return loads}};
}
let m=mock(); helper(m.button); helper(m.button);
assert.deepEqual(delays,[500],'binding is once and cue is delayed');
tick(); assert.equal(m.button.children[0].textContent,'Loading preview…');
m.events.error(); assert.equal(m.img.hidden,true); assert.equal(timers.size,1);
m.events.error(); assert.equal(timers.size,1,'duplicate error cannot stack retries');
tick(); assert.equal(m.loads,1); assert.equal(m.img.loading,'eager');
m.events.error(); tick(); assert.equal(m.loads,2);
m.events.error(); tick(); assert.equal(m.loads,3);
m.events.error(); assert.equal(timers.size,0);
assert.equal(m.button.children[0].textContent,'Preview unavailable · click to open');
assert.deepEqual(delays,[500,500,1000,2000]);

m=mock(); helper(m.button); m.events.error();
m.img.isConnected=false; tick(); assert.equal(m.loads,0,'detached route has no retry');

m=mock(); helper(m.button); m.events.error();
assert.equal(timers.size,1); const label=m.button.children[0];
m.img.naturalWidth=100; m.events.load();
assert.equal(m.img.hidden,false); assert.equal(label.removed,true);
assert.equal(timers.size,0,'load cancels outstanding retry');
'''.replace("__HELPER__", json.dumps(helper))
    path = tmp_path / "file_preview.mjs"
    path.write_text(script)
    run = subprocess.run([shutil.which("node"), str(path)], text=True,
                         capture_output=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
