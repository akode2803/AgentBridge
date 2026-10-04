"""Guarded API side-effect regressions for bounded chat reads."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "gui" / "static" / "js" / "api.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _node(tmp_path: Path, program: str, *, api: bool = False) -> None:
    if api:
        source = API.read_text(encoding="utf-8").replace(
            'import { toast } from "./util.js";', "export const toast = () => {};",
        )
        (tmp_path / "diagnostics.js").write_text(
            "export const diagnostic = () => {};\n"
            "export const beginDiagnosticRequest = () => null;\n"
            "export const endDiagnosticRequest = () => false;\n", encoding="utf-8")
        source = source.replace('from "./files.js"', 'from "./files.mjs"')
        (tmp_path / "files.mjs").write_text(
            (ROOT / "gui/static/js/files.js").read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "api.mjs").write_text(source, encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    runner.write_text(textwrap.dedent(program), encoding="utf-8")
    done = subprocess.run(
        [shutil.which("node") or "node", str(runner)], cwd=tmp_path,
        text=True, capture_output=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@requires_node
def test_side_effect_free_api_defers_locked_event_until_owner_accepts(tmp_path):
    _node(tmp_path, """
        import assert from 'node:assert/strict';
        globalThis.window = globalThis;
        globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
        globalThis.document = { events: [], dispatchEvent(event) { this.events.push(event.type); } };
        globalThis.fetch = async () => ({json: async () => ({error: 'App is locked', locked: true})});
        const { api } = await import('./api.mjs');
        const deferred = await api('/api/mesh/chat_page', undefined, {sideEffects: false});
        assert.equal(deferred.locked, true);
        assert.deepEqual(document.events, []);
        await api('/api/mesh/chat_page');
        assert.deepEqual(document.events, ['ab:locked']);
    """, api=True)
