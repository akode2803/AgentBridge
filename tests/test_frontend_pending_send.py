"""Browser intent must settle by correlation, preserve recovery, and fence identity."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_pending_send_lifecycle(tmp_path):
    source = (ROOT / 'gui/static/js/pending-send.js').read_text(encoding='utf-8')
    for name in ('state', 'util', 'icons', 'markdown'):
        source = source.replace(f'./{name}.js', f'./{name}.mjs')
    (tmp_path / 'pending.mjs').write_text(source, encoding='utf-8')
    (tmp_path / 'state.mjs').write_text('''
export const App = {page:'chats'}, Mesh = {chatId:'room'};
export const owner = {epoch:1, lockEpoch:0, locked:false, viewer:'alice'};
export const captureSessionEpoch = () => ({epoch:owner.epoch});
export const sessionMayApply = s => s.epoch === owner.epoch;
export const meshStateSnapshot = () => ({...owner});
export const currentDraftViewer = () => owner.viewer;
''', encoding='utf-8')
    (tmp_path / 'util.mjs').write_text('export const esc = x => String(x), timeOnly = x => x;', encoding='utf-8')
    (tmp_path / 'icons.mjs').write_text('export const ICONS = {clock:"clock",info:"info",file:"file"};', encoding='utf-8')
    (tmp_path / 'markdown.mjs').write_text('export const md = x => x;', encoding='utf-8')
    (tmp_path / 'run.mjs').write_text('''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
Object.defineProperty(globalThis, "crypto", {value:webcrypto});
const listeners = new Map(), storage = new Map();
globalThis.document = {addEventListener:(e,f)=>listeners.set(e,f),querySelectorAll:()=>[]};
globalThis.localStorage = {getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
const {Mesh,owner} = await import('./state.mjs');
const P = await import('./pending.mjs');
const first = P.beginSend('room','first',[],null);
assert.match([...storage.values()][0], /first/);
assert.equal(P.pendingSendRows('room').length,1);
// Canonical response can beat POST response. One row, and no later resurrection.
P.reconcileSends('room',[{id:'m1',mine:true,receipt:{transport:{client_ref:first.ref}}}],0);
assert.equal(P.pendingSendRows('room').length,0);
P.acknowledgeSend(first,'m1'); assert.equal(P.pendingSendRows('room').length,0);
assert.equal(storage.size,0);
const second = P.beginSend('room','second',[],null);
P.acknowledgeSend(second,'m2');
P.reconcileSends('room',[], -1); assert.equal(P.pendingSendRows('room').length,1);
P.reconcileSends('room',[],Infinity); assert.equal(P.pendingSendRows('room').length,0);
const uncertain = P.beginSend('room','recover me',[{name:'doc.txt',token:'secret-stage'}],null);
P.failSend(uncertain,'offline');
assert.match(P.pendingSendRows('room')[0][1], /Restore draft/);
assert.ok(![...storage.values()].join('').includes('secret-stage'));
listeners.get('ab:session-reset')(); owner.epoch++;
assert.equal(P.sendMayApply(uncertain),false);
owner.viewer='bob'; P.reconcileSends('room',[],0);
assert.equal(P.pendingSendRows('room').length,0);
listeners.get('ab:session-reset')(); owner.viewer='alice';
P.reconcileSends('room',[],0);
assert.equal(P.pendingSendRows('room').length,1);
assert.equal(P.removeSend(uncertain.ref,'other'),null);
assert.equal(P.removeSend(uncertain.ref,'room').body,'recover me');
assert.equal(storage.size,0);
const locked = P.beginSend('room','lock',[],null);
owner.lockEpoch++; owner.locked=true; listeners.get('ab:lock-epoch')();
assert.equal(P.sendMayApply(locked),false); assert.equal(P.pendingSendRows('room').length,0);
assert.equal(P.beginSend('room','blocked',[],null),null);
''', encoding='utf-8')
    result = subprocess.run(['node', str(tmp_path/'run.mjs')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_restore_preserves_new_draft_and_never_reuses_attempted_uploads(tmp_path):
    import json
    source = (ROOT / 'gui/static/js/composer.js').read_text(encoding='utf-8')
    restore = source[source.index('export function restoreSendDraft'):].replace('export ', '', 1)
    script = '''
import assert from 'node:assert/strict';
const draft={body:'newer draft',atts:[{token:'fresh'}],reply:null};
const field={value:'',dispatchEvent:()=>{},focus:()=>{}};
const notices=[];
const restore=new Function('meshDraft','$','Mesh','toast','renderMeshPending','renderReplyArea', %s + ';return restoreSendDraft;')(
  ()=>draft,()=>field,{chatId:'room'},s=>notices.push(s),()=>{},()=>{});
restore('room',{body:'interrupted text',attachments:[{name:'a.txt',token:'consumed'}],reply:{id:'quote'}},{});
assert.equal(draft.body,'newer draft\\n\\ninterrupted text');
assert.deepEqual(draft.atts,[{token:'fresh'}]);
assert.equal(draft.reply.id,'quote');
assert.match(notices.join(''),/reattach/i);
''' % json.dumps(restore)
    path = tmp_path/'restore.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
