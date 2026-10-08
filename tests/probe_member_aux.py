"""Opt-in disposable benchmark: uv run pytest -q -s tests/probe_member_aux.py.

Compare identical canonical reads with/without raw-account batching. Wall-time
samples exclude cProfile; the separately instrumented sample exposes CPU work.
This file is deliberately outside pytest's default test_* collection pattern.
"""
import cProfile
import pstats
import time
import pytest

from conftest import seed_account
from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request
from test_gui_chat_pages import page_app as page_app
from test_gui_page_aux import _ready, _settled_aux, _settled_lane


@pytest.mark.parametrize('batched', [False, True])
@pytest.mark.parametrize('members', [8, 32, 64])
def test_profile(page_app, monkeypatch, members, batched):
    from agentbridge.mesh.membership_coordinator import _Round

    if not batched:
        monkeypatch.setattr(_Round, 'prefetch_accounts', lambda self, names: None)
    app, _ = page_app
    names = [f'p{i:03d}' for i in range(members - 1)]
    for name in names:
        seed_account(app.mesh.tx, name)
    chat = api_chats.create_chat(app, Request(data={'name': 'Profile', 'members': names}))['chat']['id']
    app.mesh.post(chat, 'profile')
    _ready(app, chat)
    app.mesh.local_inputs.auxiliary.ingest('users')
    app.mesh.local_inputs.presence.ingest()
    for lane in ('controls', 'members', 'all'):
        for n in range(3):
            start = time.perf_counter()
            cpu = time.process_time()
            result = (_settled_aux(app, chat) if lane == 'all'
                      else _settled_lane(app, chat, lane))
            print('AUX', members, batched, lane, n, result.get('status'),
                  result.get('reason'), 'wall', time.perf_counter()-start,
                  'cpu', time.process_time()-cpu)
            if result['status'] == 'ready':
                assert len(result['users']) == (0 if lane == 'controls' else members)
            else:
                assert (result['status'], result['reason']) == ('unavailable', 'clock_expired')
                assert 'users' not in result
    if members == 64 and batched:
        profile = cProfile.Profile()
        profile.enable()
        result = _settled_lane(app, chat, 'members')
        profile.disable()
        assert result['status'] == 'ready', result.get('reason')
        pstats.Stats(profile).strip_dirs().sort_stats('cumtime').print_stats(30)
