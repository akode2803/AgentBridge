"""R193 display-only warm-context and guarded API side-effect regressions."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WARM = ROOT / "gui" / "static" / "js" / "warm-context.js"
API = ROOT / "gui" / "static" / "js" / "api.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _node(tmp_path: Path, program: str, *, api: bool = False) -> None:
    if api:
        source = API.read_text(encoding="utf-8").replace(
            'import { toast } from "./util.js";', "export const toast = () => {};",
        )
        (tmp_path / "api.mjs").write_text(source, encoding="utf-8")
    else:
        (tmp_path / "warm-context.mjs").write_text(WARM.read_text(encoding="utf-8"), encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    runner.write_text(textwrap.dedent(program), encoding="utf-8")
    done = subprocess.run(
        [shutil.which("node") or "node", str(runner)], cwd=tmp_path,
        text=True, capture_output=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@requires_node
def test_warm_context_copies_only_detached_display_allowlist(tmp_path):
    _node(tmp_path, """
        import assert from 'node:assert/strict';
        import { presentationFromState, warmContext } from './warm-context.mjs';
        const state = {available: true, user: 'aryan', chats: [{id: 'c1'}], users: {
          aryan: {display: 'Aryan', avatar: {sha256: 'a'.repeat(64), updated: '2026-09-16', secret: 'no'}, color: '#123',
                  kind: 'human', departed: true, owners: ['fable'],
                  keys: {sign_pub: 'secret'}, presence: {online: true}, mute: true},
          fable: {display: '', avatar: {sha256: 'b'.repeat(64), updated: 'today', secret: 'no'}, color: '#456', key_verified: true},
        }};
        const presentation = presentationFromState(state);
        assert.deepEqual(JSON.parse(JSON.stringify(presentation)), {user: 'aryan', users: {
          aryan: {username: 'aryan', display: 'Aryan', avatar: {sha256: 'a'.repeat(64), updated: '2026-09-16'}, color: '#123'},
          fable: {username: 'fable', display: 'fable', avatar: {sha256: 'b'.repeat(64), updated: 'today'}, color: '#456'},
        }});
        state.users.aryan.avatar.sha256 = 'c'.repeat(64);
        assert.equal(presentation.users.aryan.avatar.sha256, 'a'.repeat(64));
        for (const field of ['kind', 'departed', 'owners', 'keys', 'presence', 'mute']) {
          assert.equal(field in presentation.users.aryan, false, field);
        }
        const context = warmContext(
          {sessionEpoch: 4, lockEpoch: 9, stateGeneration: 11, viewer: 'aryan', ageMs: 4, locked: false, bound: true},
          state, 'c1', 1000,
        );
        assert.equal(context.sessionEpoch, 4);
        assert.equal(context.lockEpoch, 9);
        assert.equal(context.stateGeneration, 11);
        assert.equal(context.presentation.users.aryan.avatar.sha256, 'c'.repeat(64));
        state.users.aryan.avatar.sha256 = 'd'.repeat(64);
        assert.equal(context.presentation.users.aryan.avatar.sha256, 'c'.repeat(64));
        assert.equal(Object.isFrozen(context), true);
    """)


@requires_node
def test_warm_context_requires_recent_unlocked_matching_viewer_and_known_room(tmp_path):
    _node(tmp_path, """
        import assert from 'node:assert/strict';
        import { warmContext, sameWarmOperation, WARM_CONTEXT_MAX_AGE_MS } from './warm-context.mjs';
        const state = {available: true, user: 'aryan', chats: [{id: 'c1'}], users: {aryan: {display: 'Aryan'}}};
        const valid = {sessionEpoch: 1, lockEpoch: 2, stateGeneration: 3, viewer: 'aryan', ageMs: 0, locked: false, bound: true};
        assert.ok(warmContext(valid, state, 'c1'));
        for (const snapshot of [
          {...valid, locked: true}, {...valid, exhausted: true}, {...valid, bound: false}, {...valid, viewer: 'fable'},
          {...valid, ageMs: WARM_CONTEXT_MAX_AGE_MS + 1}, {...valid, ageMs: -1},
        ]) assert.equal(warmContext(snapshot, state, 'c1'), null);
        for (const unavailable of [
          {...state, available: false}, {...state, restoring: true},
          {...state, connection: {state: 'offline'}},
          {...state, connection: {state: 'restricted'}},
        ]) assert.equal(warmContext(valid, unavailable, 'c1'), null);
        assert.equal(warmContext(valid, state, 'gone'), null);
        const operation = {sessionEpoch: 1, lockEpoch: 2, stateGeneration: 3, routeSeq: 4, chatId: 'c1', operationId: 5, chatRenderSeq: 6};
        assert.equal(sameWarmOperation(operation, {...operation}), true);
        for (const key of Object.keys(operation)) {
          assert.equal(sameWarmOperation(operation, {...operation, [key]: typeof operation[key] === 'number' ? 99 : 'gone'}), false, key);
        }
    """)


@requires_node
def test_side_effect_free_api_defers_locked_event_until_owner_accepts(tmp_path):
    _node(tmp_path, """
        import assert from 'node:assert/strict';
        globalThis.window = globalThis;
        globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
        globalThis.document = { events: [], dispatchEvent(event) { this.events.push(event.type); } };
        globalThis.fetch = async () => ({json: async () => ({error: 'App is locked', locked: true})});
        const { api } = await import('./api.mjs');
        const deferred = await api('/api/mesh/chat', undefined, {sideEffects: false});
        assert.equal(deferred.locked, true);
        assert.deepEqual(document.events, []);
        await api('/api/mesh/chat');
        assert.deepEqual(document.events, ['ab:locked']);
    """, api=True)
