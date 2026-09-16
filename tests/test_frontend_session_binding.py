"""R188 browser session-adoption protocol, executed against the real ES module."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSION_JS = ROOT / "gui" / "static" / "js" / "session.js"
MAIN_JS = ROOT / "gui" / "static" / "js" / "main.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _run_node(tmp_path: Path, program: str) -> None:
    """Import a temporary .mjs copy so Node executes the tracked source as ESM."""
    module = tmp_path / "session.mjs"
    module.write_text(SESSION_JS.read_text(encoding="utf-8"), encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    runner.write_text(textwrap.dedent(program), encoding="utf-8")
    completed = subprocess.run(
        [shutil.which("node") or "node", str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@requires_node
def test_binding_parser_is_exact_decimal_and_refuses_legacy_after_adoption(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const bound = (instance_id, session_generation, viewer = 'aryan') =>
          ({v: 2, instance_id, user: viewer, caps: {session_binding_v1: true},
            session_binding: {instance_id, session_generation, viewer}});
        const accept = (payload) => b.acceptBootstrap(b.beginBootstrap(), payload);

        for (const generation of ['0', '9007199254740993', '9223372036854775807']) {
          const probe = createSessionBoundary();
          const out = probe.acceptBootstrap(probe.beginBootstrap(), bound('p' + generation, generation));
          assert.equal(out.accepted, true, generation);
        }
        for (const bad of ['01', '+1', ' 1', '1 ', '1.0', '1e3', '-1',
                           '9223372036854775808', 1, -1, null, undefined]) {
          const probe = createSessionBoundary();
          const out = probe.acceptBootstrap(probe.beginBootstrap(), bound('bad', bad));
          assert.equal(out.accepted, false, String(bad));
        }
        assert.equal(accept({configured: true, gui_version: 'legacy', bridge_version: 'legacy'}).accepted, true);
        assert.equal(accept({v: 3, caps: {session_binding_v1: true}}).accepted, false);
        assert.equal(accept(bound('one', '7')).accepted, true);
        // Binding mode is sticky: an old bridge payload must not downgrade it.
        assert.equal(accept({configured: true, gui_version: 'legacy', bridge_version: 'legacy'}).accepted, false);
        assert.equal(accept({v: 2, caps: {}}).accepted, false);
    """)


@requires_node
def test_legacy_bootstrap_refuses_malformed_capability_containers(tmp_path):
    """Untrusted legacy payload caps must be object-shaped before capability probing."""
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const oldV2 = (caps) => ({v: 2, configured: false, gui_version: '0.24.272',
          instance_id: 'old', user: null, ...(caps === undefined ? {} : {caps})});
        const accept = (payload) => {
          const boundary = createSessionBoundary();
          return boundary.acceptBootstrap(boundary.beginBootstrap(), payload).accepted;
        };
        assert.equal(accept(oldV2(undefined)), true, 'recognized capability-absent old v2');
        for (const caps of [null, [], 1, true, 'session_binding_v1', {session_binding_v1: false}]) {
          assert.equal(accept(oldV2(caps)), false, JSON.stringify(caps));
        }
    """)


def test_sidebar_imports_every_session_helper_it_uses():
    """Module imports are checked explicitly because Node syntax checking misses this runtime error."""
    sidebar = (ROOT / "gui" / "static" / "js" / "sidebar.js").read_text(encoding="utf-8")
    state_import = sidebar.split('from "./state.js";', 1)[0]
    assert "sessionMayApply" in state_import
    assert sidebar.count("sessionMayApply(") >= 1


@requires_node
def test_same_instance_aba_floor_and_bootstrap_sequence_cannot_roll_back(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const payload = (generation, viewer = 'aryan') => ({v: 2, instance_id: 'same-process', user: viewer, caps: {session_binding_v1: true},
          session_binding: {instance_id: 'same-process', session_generation: generation, viewer}});
        const accept = (p) => b.acceptBootstrap(b.beginBootstrap(), p);
        assert.equal(accept(payload('7')).accepted, true);
        const old = b.capture();
        assert.equal(b.invalidate().transition, true);
        assert.equal(accept(payload('9')).accepted, true);
        assert.equal(b.mayApply(old, payload('7')), false); // same-user logout/login ABA
        const snapshot = b.snapshot();
        assert.equal(snapshot.binding.session_generation, '9');
        assert.equal(accept(payload('8')).accepted, false); // floor survives invalidate
        assert.equal(accept(payload('9', 'fable')).accepted, false);

        // Earlier issued bootstrap cannot replace a newer accepted one.
        const late = b.beginBootstrap();
        const fresh = b.beginBootstrap();
        assert.equal(b.acceptBootstrap(fresh, payload('10')).accepted, true);
        assert.equal(b.acceptBootstrap(late, payload('11')).accepted, false);
        assert.equal(b.snapshot().binding.session_generation, '10');
    """)


@requires_node
def test_changed_instance_requires_second_fresh_bootstrap_and_retires_old(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary({maxRetiredInstances: 2});
        const p = (instance_id, generation, viewer = 'aryan') => ({v: 2, instance_id, user: viewer, caps: {session_binding_v1: true},
          session_binding: {instance_id, session_generation: generation, viewer}});
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('old', '19')).accepted, true);
        const oldTicket = b.capture();
        const firstNew = b.acceptBootstrap(b.beginBootstrap(), p('new', '0'));
        assert.equal(firstNew.accepted, false);
        assert.equal(firstNew.transition, true);
        assert.equal(firstNew.retryInstance, 'new');
        assert.equal(b.mayApply(oldTicket, p('old', '19')), false);
        assert.equal(b.snapshot().binding, null);
        // A fresh request is mandatory; the candidate response cannot self-adopt.
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('new', '0')).accepted, true);
        assert.equal(b.snapshot().binding.instance_id, 'new');
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('old', '20')).accepted, false);
    """)


@requires_node
def test_retired_instance_capacity_exhausts_and_refuses_all_late_application(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary({maxRetiredInstances: 1});
        const p = (instance_id) => ({v: 2, instance_id, user: 'aryan',
          caps: {session_binding_v1: true},
          session_binding: {instance_id, session_generation: '0', viewer: 'aryan'}});
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('a')).accepted, true);
        const a = b.capture();
        const bFirst = b.acceptBootstrap(b.beginBootstrap(), p('b'));
        assert.equal(bFirst.transition, true);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('b')).accepted, true);
        const current = b.capture();
        const exhausted = b.acceptBootstrap(b.beginBootstrap(), p('c'));
        assert.equal(exhausted.accepted, false);
        assert.equal(b.snapshot().exhausted, true);
        assert.equal(b.mayApply(a, p('a')), false);
        assert.equal(b.mayApply(current, p('b')), false);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('c')).accepted, false);
    """)


@requires_node
def test_actual_epoch_guard_model_drops_stale_cache_dom_and_post_followup(tmp_path):
    """The real boundary gates modeled main-state/cache/DOM continuations once."""
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const response = (viewer, generation) => ({v: 2, instance_id: 'p', caps: {session_binding_v1: true},
          user: viewer, session_binding: {instance_id: 'p', session_generation: generation, viewer}});
        const app = {state: null};
        const mesh = {state: null, chatId: null, authorityCache: {private: true}};
        let paints = 0, posts = 0, followups = 0;
        const clear = () => { app.state = null; mesh.state = null; mesh.chatId = null; mesh.authorityCache = {}; };
        const bootstrap = (payload) => {
          const ticket = b.beginBootstrap();
          const decision = b.acceptBootstrap(ticket, payload);
          if (!decision.accepted) return decision;
          if (decision.transition) clear();
          app.state = payload; mesh.state = payload; paints += 1;
          return decision;
        };
        assert.equal(bootstrap(response('aryan', '7')).accepted, true);
        const delayedState = b.capture();
        const delayedPost = b.capture();
        assert.equal(b.invalidate().transition, true); clear();
        assert.equal(bootstrap(response('fable', '9')).accepted, true);
        // Representative global cache + DOM application is gated by the real machine.
        if (b.mayApply(delayedState, response('aryan', '7'))) {
          app.state = response('aryan', '7'); mesh.state = app.state; paints += 1;
        }
        // A successful POST is never repeated; only its stale UI continuation drops.
        posts += 1;
        if (b.mayApply(delayedPost)) { followups += 1; mesh.authorityCache.old = true; }
        assert.equal(posts, 1); assert.equal(followups, 0); assert.equal(paints, 2);
        assert.equal(app.state.user, 'fable'); assert.equal(mesh.state.user, 'fable');
        assert.deepEqual(mesh.authorityCache, {});
        const current = b.capture();
        assert.equal(b.mayApply(current, response('fable', '9')), true);
    """)


@requires_node
def test_real_bootstrap_orchestration_discards_stale_and_clears_on_exhaustion(tmp_path):
    """Execute the tracked main bootstrap body, not a lookalike callback model."""
    source = json.dumps(str(MAIN_JS))
    _run_node(tmp_path, f"""
        import assert from 'node:assert/strict';
        import fs from 'node:fs';
        import vm from 'node:vm';
        import {{ createSessionBoundary }} from './session.mjs';
        const source = fs.readFileSync({source}, 'utf8');
        const start = source.indexOf('export async function bootstrapSession()');
        const end = source.indexOf('V.bootstrapSession = bootstrapSession;', start);
        assert.ok(start >= 0 && end > start, 'main bootstrap seam missing');
        const body = ['let bootstrapPromise = null;',
          source.slice(start, end).replace('export async function', 'async function'),
          'globalThis.runBootstrap = bootstrapSession;'].join('\\n');
        const boundary = createSessionBoundary({{maxRetiredInstances: 1}});
        const App = {{state: null}};
        const Mesh = {{state: {{user: 'old'}}, chatId: 'old', authorityCache: {{old: true}}}};
        let clears = 0;
        const clearSessionCaches = () => {{
          clears += 1; App.state = null; Mesh.state = null; Mesh.chatId = null; Mesh.authorityCache = {{}};
        }};
        const p = (instance_id, generation, viewer = 'aryan') => ({{v: 2, instance_id, user: viewer,
          caps: {{session_binding_v1: true}},
          session_binding: {{instance_id, session_generation: generation, viewer}}}});
        let replies = [];
        const context = {{BrowserSession: boundary, App, Mesh, clearSessionCaches,
          V: {{}}, api: async () => replies.shift(), Promise, console}};
        vm.runInNewContext(body, context, {{filename: 'main.bootstrap.extracted.js'}});
        // First authoritative state establishes the binding and cache.
        replies = [p('a', '1')];
        assert.equal((await context.runBootstrap()).state.user, 'aryan');
        assert.equal(App.state.instance_id, 'a');
        // Changed process needs two fresh replies: first clears, second adopts.
        replies = [p('b', '0'), p('b', '0')];
        assert.equal((await context.runBootstrap()).state.instance_id, 'b');
        assert.equal(App.state.instance_id, 'b');
        // Capacity exhaustion is a session boundary too: stale b caches cannot remain.
        replies = [p('c', '0')];
        assert.equal(await context.runBootstrap(), null);
        assert.equal(boundary.snapshot().exhausted, true);
        assert.ok(clears >= 2, 'changed instance and exhausted transition each clear caches');
        assert.equal(App.state, null);
        assert.equal(Mesh.state, null);
        assert.equal(Mesh.chatId, null);
        assert.deepEqual(Mesh.authorityCache, {{}});
    """)

@requires_node
def test_real_bootstrap_starts_fresh_request_after_auth_epoch_invalidation(tmp_path):
    """A pending old bootstrap must not strand the newly adopted session until polling."""
    source = json.dumps(str(MAIN_JS))
    _run_node(tmp_path, f"""
        import assert from 'node:assert/strict';
        import fs from 'node:fs';
        import vm from 'node:vm';
        import {{ createSessionBoundary }} from './session.mjs';
        const source = fs.readFileSync({source}, 'utf8');
        const start = source.indexOf('export async function bootstrapSession()');
        const end = source.indexOf('V.bootstrapSession = bootstrapSession;', start);
        const body = ['let bootstrapPromise = null;',
          source.slice(start, end).replace('export async function', 'async function'),
          'globalThis.runBootstrap = bootstrapSession;'].join('\\n');
        const boundary = createSessionBoundary();
        const App = {{state: null}};
        let calls = 0, resolveOld;
        const old = new Promise((resolve) => {{ resolveOld = resolve; }});
        const p = (generation) => ({{v: 2, instance_id: 'same', user: 'aryan',
          caps: {{session_binding_v1: true}},
          session_binding: {{instance_id: 'same', session_generation: generation, viewer: 'aryan'}}}});
        const replies = [p('1'), old, p('3')];
        const context = {{BrowserSession: boundary, App, V: {{}}, clearSessionCaches: () => {{ App.state = null; }},
          api: async () => {{ calls += 1; return replies.shift(); }}, Promise, console}};
        vm.runInNewContext(body, context, {{filename: 'main.bootstrap.extracted.js'}});
        assert.equal((await context.runBootstrap()).state.session_binding.session_generation, '1');
        const oldBootstrap = context.runBootstrap();
        boundary.invalidate(); context.clearSessionCaches();
        const freshBootstrap = context.runBootstrap();
        resolveOld(p('1'));
        assert.equal((await oldBootstrap), null);
        const fresh = await freshBootstrap;
        assert.equal(calls, 3, 'new auth epoch issued its own authoritative bootstrap');
        assert.equal(fresh.state.session_binding.session_generation, '3');
        assert.equal(App.state.session_binding.session_generation, '3');
    """)


@requires_node
def test_pending_transition_is_not_read_ready_until_fresh_bootstrap_commits(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const p = (instance_id, generation) => ({v: 2, instance_id, user: 'aryan',
          caps: {session_binding_v1: true},
          session_binding: {instance_id, session_generation: generation, viewer: 'aryan'}});
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('a', '1')).accepted, true);
        assert.equal(b.mayApply(b.capture()), true);
        b.invalidate();
        // A new epoch ticket is not authority while logout/login confirmation is pending.
        assert.equal(b.mayApply(b.capture()), false);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('a', '3')).accepted, true);
        assert.equal(b.mayApply(b.capture()), true);
        // Process replacement clears read readiness until its required second bootstrap.
        const first = b.acceptBootstrap(b.beginBootstrap(), p('b', '0'));
        assert.equal(first.transition, true);
        assert.equal(b.mayApply(b.capture()), false);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('b', '0')).accepted, true);
        assert.equal(b.mayApply(b.capture()), true);
    """)

@requires_node
def test_invalidation_preserves_identity_floor_and_requires_fresh_readiness(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const p = (instance_id, generation, viewer = 'aryan') => ({v: 2, instance_id, user: viewer,
          caps: {session_binding_v1: true},
          session_binding: {instance_id, session_generation: generation, viewer}});
        const b = createSessionBoundary();
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('same', '7')).accepted, true);
        const old = b.capture();
        b.invalidate();
        assert.equal(b.mayApply(b.capture()), false);
        // Same generation cannot turn a logout/login into a different-viewer adoption.
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('same', '7', 'fable')).accepted, false);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('same', '8', 'fable')).accepted, true);
        assert.equal(b.mayApply(old, p('same', '7')), false);

        const next = createSessionBoundary();
        assert.equal(next.acceptBootstrap(next.beginBootstrap(), p('old', '7')).accepted, true);
        next.invalidate();
        const firstNew = next.acceptBootstrap(next.beginBootstrap(), p('new', '0'));
        assert.equal(firstNew.accepted, false);
        assert.equal(firstNew.transition, true);
        assert.equal(firstNew.retryInstance, 'new');
        assert.equal(next.acceptBootstrap(next.beginBootstrap(), p('new', '0')).accepted, true);
    """)


@requires_node
def test_legacy_upgrade_advances_epoch_and_old_backend_versions_stay_compatible(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const legacy = (version) => ({v: 2, configured: false, gui_version: version, instance_id: 'legacy', user: null});
        const bound = {v: 2, instance_id: 'p', user: 'aryan', caps: {session_binding_v1: true},
          session_binding: {instance_id: 'p', session_generation: '4', viewer: 'aryan'}};
        for (const version of ['0.24.259', '0.24.270', '0.24.272']) {
          const probe = createSessionBoundary();
          assert.equal(probe.acceptBootstrap(probe.beginBootstrap(), legacy(version)).accepted, true, version);
        }
        const b = createSessionBoundary();
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), legacy('0.24.259')).accepted, true);
        const old = b.capture();
        b.invalidate();
        assert.equal(b.mayApply(b.capture()), false);
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), legacy('0.24.259')).accepted, true);
        const beforeUpgrade = b.capture();
        const upgraded = b.acceptBootstrap(b.beginBootstrap(), bound);
        assert.equal(upgraded.accepted, true);
        assert.equal(upgraded.transition, true);
        assert.equal(b.mayApply(beforeUpgrade), false);
        assert.equal(b.mayApply(old), false);
        assert.equal(b.mayApply(b.capture(), bound), true);
    """)


def test_delete_agent_captures_before_post_and_checks_late_ui_followup():
    """The production continuation's guard is pre-POST, with a second post-result check."""
    settings = (ROOT / "gui" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    start = settings.index('document.querySelectorAll(".ag-delete")')
    end = settings.index('// owner stops the agent', start)
    delete_flow = settings[start:end]
    capture = delete_flow.index("sessionTicket")
    post = delete_flow.index('api("/api/mesh/delete_agent"')
    result_guard = delete_flow.index("if (!sessionMayApply(sessionTicket)) return;", post)
    request = delete_flow.index("captureMeshStateRead(sessionTicket)", result_guard)
    apply = delete_flow.index("applyMeshState(sessionTicket, fresh, request)", request)
    assert capture < post < result_guard < request < apply


def test_auth_receipt_is_checked_against_the_confirmed_bootstrap_before_ui():
    """The auth route has only the exact-receipt recovery exception to a stale request."""
    auth = (ROOT / "gui" / "static" / "js" / "auth.js").read_text(encoding="utf-8")
    start = auth.index("const go = async () =>")
    end = auth.index('$("#auth-go").addEventListener', start)
    flow = auth[start:end]
    request_ticket = flow.index("const requestTicket = captureSessionEpoch()")
    post = flow.index('const r = await api(', request_ticket)
    error_guard = flow.index("if (sessionMayApply(requestTicket)) setSubError(r.error);", post)
    transition = flow.index("beginSessionTransition();", error_guard)
    bootstrap = flow.index("const bootstrap = await V.bootstrapSession();", transition)
    receipt_guard = flow.index("sessionMayApply(bootstrap.ticket, r)", bootstrap)
    stale_branch = flow.index("const currentTicket = captureSessionEpoch();", receipt_guard)
    stale_receipt_guard = flow.index("if (!sessionMayApply(currentTicket, r)) return;", stale_branch)
    recovery = flow.index("await showRecoveryCode(r.recovery_code)", stale_receipt_guard)
    final_guard = flow.index("if (!sessionMayApply(acceptedTicket)) return;", recovery)
    assert request_ticket < post < error_guard < transition < bootstrap < receipt_guard
    assert receipt_guard < stale_branch < stale_receipt_guard < recovery < final_guard


@requires_node
def test_polled_exact_auth_receipt_can_finish_once_but_mismatched_receipt_cannot(tmp_path):
    """Execute the auth route's decision rule with the real session boundary."""
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const response = (viewer, generation) => ({v: 2, instance_id: 'p', user: viewer,
          caps: {session_binding_v1: true},
          session_binding: {instance_id: 'p', session_generation: generation, viewer}});
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), response('aryan', '1')).accepted, true);
        const prePost = b.capture();
        // Poll adopts the exact completed auth receipt before its POST promise settles.
        const receipt = response('aryan', '2');
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), receipt).accepted, true);
        assert.equal(b.mayApply(prePost), false);
        const current = b.capture();
        assert.equal(b.mayApply(current, receipt), true);
        let recovery = 0, close = 0;
        if (b.mayApply(current, receipt)) { recovery += 1; }
        if (b.mayApply(current)) { close += 1; }
        assert.deepEqual({recovery, close}, {recovery: 1, close: 1});

        // A late result for another viewer/generation remains a dropped continuation.
        const wrong = response('fable', '3');
        assert.equal(b.mayApply(b.capture(), wrong), false);
        assert.deepEqual({recovery, close}, {recovery: 1, close: 1});
    """)


@requires_node
def test_accepted_post_drops_its_actual_cache_and_dom_followup_after_transition(tmp_path):
    _run_node(tmp_path, """
        import assert from 'node:assert/strict';
        import { createSessionBoundary } from './session.mjs';
        const b = createSessionBoundary();
        const p = (viewer, generation) => ({v: 2, instance_id: 'p', user: viewer,
          caps: {session_binding_v1: true},
          session_binding: {instance_id: 'p', session_generation: generation, viewer}});
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('aryan', '1')).accepted, true);
        const cache = {state: {user: 'aryan'}, agent: 'old'};
        const dom = {toast: '', rendered: false};
        let posts = 0, release;
        const acceptedPost = new Promise((resolve) => { release = resolve; });
        async function productionShapedDelete() {
          const ticket = b.capture(); // same ordering as settings' delete listener
          posts += 1;
          const result = await acceptedPost;
          if (!b.mayApply(ticket)) return;
          if (result.error) { dom.toast = result.error; return; }
          dom.toast = '@agent deleted';
          cache.state = result.fresh;
          cache.agent = null;
          dom.rendered = true;
        }
        const pending = productionShapedDelete();
        b.invalidate();
        cache.state = {user: 'fable'}; cache.agent = 'fable-agent';
        assert.equal(b.acceptBootstrap(b.beginBootstrap(), p('fable', '3')).accepted, true);
        release({ok: true, fresh: {user: 'aryan'}});
        await pending;
        assert.equal(posts, 1); // accepted mutation has no automatic retry
        assert.equal(cache.state.user, 'fable'); // no stale follow-up write happened
        assert.equal(cache.agent, 'fable-agent');
        assert.equal(dom.toast, '');
        assert.equal(dom.rendered, false);
    """)
