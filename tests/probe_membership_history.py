"""Opt-in disposable benchmark for current-roster vs membership-history cost.

Run with::

    uv run pytest -q -s tests/probe_membership_history.py

The room always has 64 current members. Repeated remove/add cycles increase the
canonical membership history while preserving the same final roster, allowing
page and auxiliary costs to be compared without changing current member count.
This file is deliberately outside pytest's default test_* collection pattern.
"""
import json
import sqlite3
import statistics
import time

from conftest import seed_account
from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request
from test_gui_chat_pages import page_app as page_app, _ready, _settled_page
from test_gui_page_aux import _settled_lane


def _sample(label, read):
    wall, cpu = [], []
    for _ in range(3):
        started, cpu_started = time.perf_counter(), time.process_time()
        result = read()
        wall.append(time.perf_counter() - started)
        cpu.append(time.process_time() - cpu_started)
        assert result['status'] in {'page', 'ready'}, result
    print('HISTORY', label, 'wall', wall, 'median', statistics.median(wall),
          'cpu', cpu, 'median', statistics.median(cpu))


def test_membership_history_profile(page_app):
    app, _ = page_app
    names = [f'h{i:03d}' for i in range(63)]
    for name in names:
        seed_account(app.mesh.tx, name)
    chat = api_chats.create_chat(
        app, Request(data={'name': 'History profile', 'members': names}))['chat']['id']
    app.mesh.post(chat, 'profile')
    target = names[-1]
    cycles = 0

    for checkpoint in (0, 16, 64, 256):
        for _ in range(checkpoint - cycles):
            app.mesh.membership.remove_member(chat, target)
            app.mesh.membership.add_members(chat, [target])
        cycles = checkpoint
        _ready(app, chat)
        app.mesh.local_inputs.auxiliary.ingest('users')
        app.mesh.local_inputs.presence.ingest()
        with sqlite3.connect(app.mesh.store.path) as conn:
            events = conn.execute(
                "SELECT count(*) FROM messages WHERE chat_id=? AND kind='info'", (chat,),
            ).fetchone()[0]
        meta = app.mesh.tx.get_doc(f'chats/{chat}/meta.json')
        assert len(meta['members']) == 64
        print('INPUT', checkpoint, 'events', events,
              'meta_bytes', len(json.dumps(meta, separators=(',', ':')).encode()))
        _sample(f'{checkpoint}:page', lambda: _settled_page(app, chat, limit='50'))
        _sample(f'{checkpoint}:controls', lambda: _settled_lane(app, chat, 'controls'))
        _sample(f'{checkpoint}:members', lambda: _settled_lane(app, chat, 'members'))
