"""Chatless timers/peer asks use exact account lifecycle and source cuts."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agentbridge.gui import api_ask_companions
from agentbridge.harness import PeerService
from agentbridge.harness.settings import HarnessSettings
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.lifecycle import publish_change
from agentbridge.mesh.membership_coordinator import _Stop
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


class _Lock:
    def __init__(self):
        self._mx = threading.RLock()

    @staticmethod
    def _expire_if_idle_locked():
        return False


class _App:
    def __init__(self, mesh, home):
        self.mesh, self.home = mesh, home
        self.machine = 'different-machine'  # remote runner has no local PID claim
        self.lock = _Lock()
        self._lock = threading.RLock()
        self.valid = True

    def validate_session_read(self, token):
        return self.valid and token.mesh is self.mesh


def _settings():
    return HarnessSettings.from_account(SimpleNamespace(agent=SimpleNamespace(
        harness={'peer_access': 'ask', 'peer_auto': [], 'peer_repair': False})))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / 'provider'
    owner = Mesh(FolderTransport(root), 'aryan', 'owner-box',
                 home=tmp_path / 'owner-home', local_inputs=True)
    owner.accounts.create_human('aryan', 'owner-pass')
    owner.accounts.create_human('fable', 'fable-pass')
    owner.accounts.create_agent('claude')
    fable_home = tmp_path / 'fable-home'
    KeyStore(fable_home).save('fable', owner.keystore.load('fable'))
    fable = Mesh(FolderTransport(root), 'fable', 'fable-box', home=fable_home)
    fable.accounts.create_agent('ops')
    target_home, requester_home = tmp_path / 'target-home', tmp_path / 'requester-home'
    KeyStore(target_home).save('claude', owner.keystore.load('claude'))
    KeyStore(requester_home).save('ops', fable.keystore.load('ops'))
    target = Mesh(FolderTransport(root), 'claude', 'remote-box', home=target_home)
    requester = Mesh(FolderTransport(root), 'ops', 'remote-box', home=requester_home)
    app = _App(owner, tmp_path)
    token = SimpleNamespace(mesh=owner)
    try:
        yield owner, fable, target, requester, app, token
    finally:
        for mesh in (requester, target, fable, owner):
            mesh.close()


def _ingest(owner):
    auxiliary = owner.local_inputs.auxiliary
    for scope in ('identities', 'status', 'peer'):
        assert auxiliary.ingest(scope).ready


def test_chatless_timer_and_signed_peer_ask_without_room(world):
    owner, _fable, target, requester, app, token = world
    PeerService(requester).request('claude', 'status')
    assert PeerService(target).serve_once(_settings()) == 1
    owner.tx.put_doc('status/claude_harness.json', {'timers': [
        {'id': 'wake-one', 'chat_id': '', 'at_ns': time.time_ns() + 60_000_000_000,
         'note': 'check status'},
    ]})
    _ingest(owner)
    observed = api_ask_companions.capture_companions(app, owner, token)
    assert observed['peer_complete'] and observed['timers_complete']
    assert len(observed['peer_asks']) == 1
    assert observed['peer_asks'][0]['agent'] == 'claude'
    assert observed['peer_asks'][0]['peer'] == 'ops'
    assert observed['timers'] == [{
        'agent': 'claude', 'id': 'wake-one', 'chat_id': '',
        'at_ns': observed['timers'][0]['at_ns'], 'note': 'check status',
    }]


def test_late_identity_source_and_session_change_rejects_chatless_handoff(world, monkeypatch):
    owner, _fable, _target, _requester, app, token = world
    owner.tx.put_doc('status/claude_harness.json', {'timers': [
        {'id': 'wake-one', 'chat_id': '', 'at_ns': 123, 'note': 'old'},
    ]})
    _ingest(owner)
    assert api_ask_companions.capture_companions(app, owner, token)['timers_complete']
    original = api_ask_companions._project

    def mutate(*args, **kwargs):
        out = original(*args, **kwargs)
        doc = owner.tx.get_doc('users/claude.json')
        owner.tx.put_doc('users/claude.json', {**doc, 'display': 'Changed late'})
        return out

    monkeypatch.setattr(api_ask_companions, '_project', mutate)
    denied = api_ask_companions.capture_companions(app, owner, token)
    assert denied['timers'] == [] and not denied['timers_complete']
    monkeypatch.setattr(api_ask_companions, '_project', original)
    owner.local_inputs.auxiliary.ingest('identities')
    assert api_ask_companions.capture_companions(app, owner, token)['timers_complete']
    app.valid = False
    denied = api_ask_companions.capture_companions(app, owner, token)
    assert denied['timers'] == [] and not denied['timers_complete']


def test_signed_lifecycle_deactivation_hides_old_owner_prompt(world):
    owner, fable, target, requester, app, token = world
    PeerService(requester).request('claude', 'status')
    assert PeerService(target).serve_once(_settings()) == 1
    _ingest(owner)
    assert api_ask_companions.capture_companions(app, owner, token)['peer_asks']
    publish_change(owner.directory, owner.keystore, 'claude', actor='aryan',
                   action='deactivate', active=False, deactivated='retired')
    owner.local_inputs.auxiliary.ingest('identities')
    deactivated = api_ask_companions.capture_companions(app, owner, token)
    assert deactivated['peer_asks'] == []


def test_signed_transfer_requires_new_complete_identity_admission(world):
    owner, fable, target, requester, app, token = world
    PeerService(requester).request('claude', 'status')
    assert PeerService(target).serve_once(_settings()) == 1
    _ingest(owner)
    assert api_ask_companions.capture_companions(app, owner, token)['peer_asks']
    fable.keystore.save('claude', owner.keystore.load('claude'))
    publish_change(fable.directory, fable.keystore, 'claude', actor='fable',
                   action='transfer', owner='fable', machine='fable-box')
    # Another process's write has not entered this machine's admitted raw
    # input yet. It is neither a synchronous local mutation nor remote proof.
    assert api_ask_companions.capture_companions(app, owner, token)['peer_asks']
    owner.local_inputs.auxiliary.ingest('identities')
    settled = api_ask_companions.capture_companions(app, owner, token)
    assert settled['peer_asks'] == []
    assert settled['timers'] == []


def test_local_signed_lifecycle_change_between_projection_and_final_rejects(world, monkeypatch):
    owner, _fable, target, requester, app, token = world
    PeerService(requester).request('claude', 'status')
    assert PeerService(target).serve_once(_settings()) == 1
    _ingest(owner)
    assert api_ask_companions.capture_companions(app, owner, token)['peer_asks']
    original = api_ask_companions._project

    def deactivate_between_projection_and_final(*args, **kwargs):
        result = original(*args, **kwargs)
        publish_change(owner.directory, owner.keystore, 'claude', actor='aryan',
                       action='deactivate', active=False, deactivated='retired')
        return result

    monkeypatch.setattr(api_ask_companions, '_project', deactivate_between_projection_and_final)
    denied = api_ask_companions.capture_companions(app, owner, token)
    assert denied['peer_asks'] == [] and not denied['peer_complete']


def test_pinned_key_change_fails_closed_before_chatless_handoff(world, monkeypatch):
    owner, _fable, _target, _requester, app, token = world
    owner.tx.put_doc('status/claude_harness.json', {'timers': [
        {'id': 'wake-one', 'chat_id': '', 'at_ns': 123, 'note': 'old'},
    ]})
    _ingest(owner)
    assert api_ask_companions.capture_companions(app, owner, token)['timers_complete']
    original = api_ask_companions._project

    def change_pin(*args, **kwargs):
        out = original(*args, **kwargs)
        pins = owner.key_pins
        with pins._lock, pins._storage.locked() as (document, _present):
            assert document['pins']['claude']['sign_pub']
            document['pins']['claude']['sign_pub'] = fable_sign
            pins._storage.write(document)
        return out

    fable_sign = owner.directory.get('fable').keys.sign_pub
    monkeypatch.setattr(api_ask_companions, '_project', change_pin)
    denied = api_ask_companions.capture_companions(app, owner, token)
    assert denied['timers'] == [] and not denied['timers_complete']


def test_repeated_cold_restart_has_eight_attempt_ceiling(world, monkeypatch):
    owner, _fable, _target, _requester, app, token = world
    _ingest(owner)
    from agentbridge.mesh.account_round import AccountRound
    attempts = []

    def never_settles(self, name):
        attempts.append(name)
        raise _Stop('restart', 'pin_progress')

    monkeypatch.setattr(AccountRound, 'raw', never_settles)
    result = api_ask_companions.capture_companions(app, owner, token)
    assert result['peer_asks'] == [] and not result['peer_complete']
    assert len(attempts) == 8
