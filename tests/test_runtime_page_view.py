"""Captured encrypted runtime ledger inputs match legacy, without fallback."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agentbridge.gui.api_runtime import contributor_rows
from agentbridge.harness.runtime.authority import AuthorityError
from agentbridge.harness.runtime.handoffs import HandoffLedger
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.mesh.paths import P
from agentbridge.mesh.runtime_page_view import RuntimePageView
from agentbridge.mesh.service import Mesh


class Budget:
    def __init__(self, *, steps=10000, bytes_=8 * 1024 * 1024, signatures=10000):
        self.steps, self.bytes, self.signatures = steps, bytes_, signatures

    def step(self):
        self.steps -= 1
        if self.steps < 0:
            raise RuntimeError('runtime step budget')

    def charge(self, size):
        self.bytes -= size
        if self.bytes < 0:
            raise RuntimeError('runtime byte budget')

    def signature(self, _data):
        self.signatures -= 1
        if self.signatures < 0:
            raise RuntimeError('runtime signature budget')


class Round:
    def __init__(self, viewer, accounts, owners, *, ledger=None):
        self.mesh = SimpleNamespace(messaging=SimpleNamespace(user=viewer), keystore=None)
        self.states = {}
        self.accounts, self.owners = accounts, owners
        self.ledger = ledger or Budget()

    def get(self, name):
        self.ledger.step()
        return self.accounts.get(name)

    def owner_of(self, name):
        self.ledger.step()
        return self.owners.get(name)

    def sign_pub(self, name):
        account = self.get(name)
        return account.keys.sign_pub if account else None


@pytest.fixture
def world(tmp_path):
    root, home = tmp_path / 'mesh', tmp_path / 'home'
    root.mkdir()
    owner = Mesh(root, 'owner', 'box', encrypt=True, home=home,
                 store_path=tmp_path / 'owner.sqlite')
    owner.accounts.create_human('owner', 'owner-pass')
    owner.accounts.create_agent('manager', harness={'agent_tools_enabled':True})
    owner.accounts.create_agent('specialist', harness={'agent_tools_enabled':True})
    manager = Mesh(root, 'manager', 'box', encrypt=True, home=home,
                   store_path=tmp_path / 'manager.sqlite')
    specialist = Mesh(root, 'specialist', 'box', encrypt=True, home=home,
                      store_path=tmp_path / 'specialist.sqlite')
    chat = owner.create_chat('Runtime captured', members=['manager','specialist']).id
    owner.outbox.flush_once()
    manager.sync.sync_once([chat])
    specialist.sync.sync_once([chat])
    try:
        runs = RunLedger(manager)
        tasks = TaskLedger(manager, runs)
        handoffs = HandoffLedger(manager, tasks)
        tasks.start_with_run(run_id='run-1', task_id='task-1', chat_id=chat,
                             trigger_id='message-1', provider='codex', model='test')
        offered = handoffs.offer(chat_id=chat, run_id='run-1',
                                 parent_task_id='task-1', destination_agent='specialist',
                                 objective='Review', reason='Review',
                                 success_criteria=('Return a finding',))
        manager.outbox.flush_once()
        documents = owner.tx.cached_docs_bounded(f'chats/{chat}/runtime/', 10000)
        accounts = {name:owner.directory.get(name)
                    for name in ('owner','manager','specialist')}
        owners = {'manager':'owner','specialist':'owner'}
        latest = owner.keys.latest(chat)
        assert latest is not None
        keys = {latest[0]:owner.keys.my_key(chat, latest[0])}
        snapshot = owner.snapshot(chat)
        yield owner, manager, specialist, chat, offered, documents, accounts, owners, latest, keys, snapshot
    finally:
        specialist.close()
        manager.close()
        owner.close()


def _view(world, *, snapshot=None, accounts=None, owners=None, latest=None,
          keys=None, state=None, ledger=None):
    owner, _manager, _specialist, chat, _offered, docs, raw_accounts, raw_owners, raw_latest, raw_keys, snap = world
    return RuntimePageView(
        Round('owner', accounts or raw_accounts, owners or raw_owners, ledger=ledger),
        snapshot or snap, docs, latest_key=raw_latest if latest is None else latest,
        key_lookup=(keys or raw_keys).get,
        state_document=(owner.tx.get_doc(P.state(chat,'owner')) if state is None else state),
        encrypted=True,
    )


def _ledgers(mesh, documents):
    runs = RunLedger(mesh, fresh_reads=False, register_outbox=False,
                     read_snapshot=documents)
    tasks = TaskLedger(mesh, runs, fresh_reads=False, register_outbox=False,
                       read_snapshot=documents)
    handoffs = HandoffLedger(mesh, tasks, fresh_reads=False, register_outbox=False,
                             read_snapshot=documents)
    return runs, tasks, handoffs


def test_captured_run_task_handoff_equal_legacy_without_provider_fallback(world, monkeypatch):
    owner, _manager, _specialist, chat, offered, docs, *_ = world
    expected = _ledgers(owner, docs)
    run_rows = expected[0].read(chat)
    task_rows = expected[1].read(chat)
    handoff_rows = expected[2].read(chat, historical=True)
    assert run_rows and task_rows and handoff_rows
    expected_contributors = contributor_rows(owner, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('captured adapter consulted provider or Store')

    view = _view(world)
    for method in ('get_doc','list_docs','cached_docs_bounded'):
        monkeypatch.setattr(owner.tx, method, forbidden)
    monkeypatch.setattr(owner.store, 'cached_doc', forbidden)
    actual = _ledgers(view, view.tx.cached_docs_bounded(f'chats/{chat}/runtime/',10000))
    assert actual[0].read(chat) == run_rows
    assert actual[1].read(chat) == task_rows
    assert actual[2].read(chat, historical=True) == handoff_rows
    assert contributor_rows(view, chat) == expected_contributors
    assert offered.events[0].meta.call_id in [row['id'] for row in expected_contributors]


def test_captured_revocation_key_change_and_hidden_state(world):
    owner, _manager, _specialist, chat, offered, docs, accounts, owners, latest, keys, snapshot = world
    hidden_id = offered.events[0].meta.call_id
    owner.messaging._state(chat)._merge(hidden_runtime=[hidden_id, 'run-1'])
    state = owner.tx.get_doc(P.state(chat,'owner'))
    hidden = _view(world, state=state)
    assert hidden.my_state(chat)['hidden_runtime'] == [hidden_id, 'run-1']
    assert contributor_rows(hidden, chat) == contributor_rows(owner, chat) == []
    forged = dict(state, sig='wrong')
    assert _view(world, state=forged).my_state(chat)['hidden_runtime'] == []

    removed = deepcopy(snapshot)
    removed.members.pop('owner')
    with pytest.raises(AuthorityError):
        RunLedger(_view(world, snapshot=removed), fresh_reads=False,
                  register_outbox=False, read_snapshot=docs).read(chat)
    assert _ledgers(_view(world, owners={'manager':None,'specialist':'owner'}), docs)[0].read(chat) == []
    changed_accounts = {**accounts, 'manager':deepcopy(accounts['manager'])}
    changed_accounts['manager'].keys.sign_pub = 'replaced-signing-key'
    assert _ledgers(_view(world, accounts=changed_accounts), docs)[0].read(chat) == []
    changed_keys = {**keys, latest[0]:b'0'*32}
    assert _ledgers(_view(world, keys=changed_keys), docs)[0].read(chat) == []
    assert _ledgers(_view(world, latest=(latest[0]+1,{})), docs)[0].read(chat) == []


@pytest.mark.parametrize('budget', [Budget(steps=0), Budget(bytes_=0), Budget(signatures=0)])
def test_work_budget_errors_propagate_not_silently_filtered(world, budget):
    _owner, _manager, _specialist, chat, _offered, docs, *_ = world
    view = _view(world, ledger=budget, state={})
    with pytest.raises(RuntimeError, match='runtime .* budget'):
        captured = view.tx.cached_docs_bounded(f'chats/{chat}/runtime/',10000)
        _ledgers(view, captured)[0].read(chat)


def test_effective_account_applies_lifecycle_without_mutating_raw_fact(world):
    from agentbridge.mesh.runtime_page_view import effective_account
    owner, _manager, _specialist, _chat, *_rest = world
    account = owner.directory.get('manager')
    round_ = Round('owner', {'manager': account}, {'manager': 'new-owner'})
    round_.states['manager'] = {'active': False, 'deactivated': 'retired',
                               'owner': 'new-owner', 'machine': 'new-machine'}
    effective = effective_account(round_, 'manager')
    assert effective.active is False and effective.agent.owner == 'new-owner'
    assert effective.agent.machine == 'new-machine'
    assert account.active is True and account.agent.owner == 'owner'
