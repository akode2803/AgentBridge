"""API timeout and external cancellation share one fetch signal without leaks."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='requires Node.js')
def test_api_external_abort_timeout_and_listener_cleanup(tmp_path):
    (tmp_path / 'api.mjs').write_text(
        (ROOT / 'gui/static/js/api.js').read_text(encoding='utf-8').replace(
            'from "./util.js"', 'from "./util.mjs"'), encoding='utf-8')
    (tmp_path / 'util.mjs').write_text('export const toast = () => {};\n', encoding='utf-8')
    script = tmp_path / 'check.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
globalThis.window = {};
globalThis.document = {dispatchEvent() { throw Error('unexpected DOM side effect'); }};
const {api} = await import('./api.mjs');

function instrument(controller) {
  const signal = controller.signal;
  const added = [], removed = [];
  const add = signal.addEventListener.bind(signal);
  const remove = signal.removeEventListener.bind(signal);
  signal.addEventListener = (...args) => { added.push(args[0]); return add(...args); };
  signal.removeEventListener = (...args) => { removed.push(args[0]); return remove(...args); };
  return {added, removed};
}
function abortableFetch() {
  let observed;
  globalThis.fetch = (_path, options) => {
    observed = options.signal;
    return new Promise((resolve, reject) => {
      const fail = () => reject(Object.assign(new Error('aborted'), {name:'AbortError'}));
      if (options.signal?.aborted) { fail(); return; }
      options.signal?.addEventListener('abort', fail, {once:true});
    });
  };
  return () => observed;
}

// Active external cancellation reaches the actual fetch even with a timeout.
let external = new AbortController();
let calls = instrument(external);
let fetchSignal = abortableFetch();
let pending = api('/slow', undefined, {timeoutMs:1000, signal:external.signal,
                                       sideEffects:false});
assert.ok(fetchSignal());
assert.notEqual(fetchSignal(), external.signal);
external.abort();
await assert.rejects(pending, {name:'AbortError'});
assert.equal(fetchSignal().aborted, true);
assert.deepEqual(calls.added, ['abort']);
assert.deepEqual(calls.removed, ['abort']);

// Already-aborted requests never start a live fetch; cleanup remains safe.
external = new AbortController();
external.abort();
calls = instrument(external);
fetchSignal = abortableFetch();
await assert.rejects(api('/already', undefined, {timeoutMs:1000,
  signal:external.signal, sideEffects:false}), {name:'AbortError'});
assert.equal(fetchSignal().aborted, true);
assert.deepEqual(calls.added, []);

// Timeout alone aborts the linked fetch, not the caller's external signal.
external = new AbortController();
calls = instrument(external);
fetchSignal = abortableFetch();
await assert.rejects(api('/timeout', undefined, {timeoutMs:5,
  signal:external.signal, sideEffects:false}), {name:'AbortError'});
assert.equal(fetchSignal().aborted, true);
assert.equal(external.signal.aborted, false);
assert.deepEqual(calls.added, ['abort']);
assert.deepEqual(calls.removed, ['abort']);

// Success removes listener and timer; later caller abort must not touch the
// completed request's signal. Without timeout, the original signal is used.
external = new AbortController();
calls = instrument(external);
let successfulSignal;
globalThis.fetch = async (_path, options) => {
  successfulSignal = options.signal;
  return {json:async () => ({ok:true})};
};
assert.deepEqual(await api('/fast', {hello:'world'}, {timeoutMs:10,
  signal:external.signal, sideEffects:false}), {ok:true});
assert.equal(successfulSignal.aborted, false);
assert.deepEqual(calls.removed, ['abort']);
external.abort();
await new Promise(resolve => setTimeout(resolve, 20));
assert.equal(successfulSignal.aborted, false);

globalThis.fetch = async (_path, options) => {
  assert.equal(options.signal, undefined);
  return {json:async () => ({plain:true})};
};
assert.deepEqual(await api('/plain', undefined, {sideEffects:false}), {plain:true});
''', encoding='utf-8')
    run = subprocess.run([shutil.which('node'), str(script)], cwd=tmp_path,
                         text=True, capture_output=True, timeout=15)
    assert run.returncode == 0, run.stderr
