"""A full raw window leaves headroom for a far older reply/pin target."""
from __future__ import annotations

import json

from agentbridge.gui import api_chats, api_pages
from agentbridge.gui.context import GuiApp
from agentbridge.gui.routing import Request
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.service import Mesh


def test_full_raw_window_plus_outside_reply_and_pin_uses_bounded_targets(tmp_path, monkeypatch):
    # The actual folder provider and mutation owner are used; background work
    # is driven explicitly so the target-cap regression is deterministic.
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kw: None)
    app = GuiApp(tmp_path / 'provider', home=tmp_path / 'home', machine='capacity',
                 encrypt=False, local_inputs=True)
    try:
        assert app.signup('viewer', '', 'fixture-pass')['ok']
        chat = api_chats.create_chat(app, Request(data={
            'name': 'Large target window', 'members': [],
        }))['chat']['id']
        anchor = app.mesh.post(chat, 'seed')
        app.mesh.outbox.flush_once()
        rows = []
        for n in range(260):
            record = {'body': f'row-{n:03d}'}
            if n == 259:
                record['reply_to'] = {'id': 'row-000', 'from': 'viewer', 'body': 'row-000'}
            rows.append({
                'id': f'row-{n:03d}', 'ns': anchor.ns + 1 + n,
                'ts': '2026-09-24T10:00:00Z', 'from': 'viewer',
                'kind': 'message', 'epoch': 0, 'nonce': '',
                'ct': json.dumps(record), 'sig': '',
            })
        app.mesh.store.upsert_messages(chat, rows)
        app.mesh.pin(chat, 'row-000')
        seen = []
        original = LocalPageSource.capture_page

        def capture(self, *args, **kwargs):
            seen.append((kwargs.get('raw_limit'), kwargs.get('exact_ids', ())))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(LocalPageSource, 'capture_page', capture)
        runtime = app.mesh.local_inputs
        runtime.request_page(chat)
        for _ in range(12):
            runtime.prepare_one()
            runtime.ingest(chat)
            result = api_pages.chat_page(app, Request(params={'id': chat, 'limit': '50'}))
            if result.get('status') == 'page':
                break
        assert result['status'] == 'page', result
        assert len(result['messages']) == 50 and result['has_more']
        assert any(item['id'] == 'row-000' for item in result['meta']['pins'])
        newest = next(item for item in result['messages'] if item['id'] == 'row-259')
        assert newest['reply_to']['id'] == 'row-000'
        assert any('row-000' in exact for _limit, exact in seen)
        assert all(limit <= 200 for limit, _exact in seen)
    finally:
        app.close()
