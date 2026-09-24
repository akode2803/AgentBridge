"""Bounded chatless approval/timer presentation from admitted raw inputs."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from ..harness.runtime.authority import AuthorityError
from ..harness.runtime.peer_control import open_owner_ask, PeerControlError
from ..mesh.account_round import AccountRound
from ..mesh.membership_coordinator import _Proposal, _Stop
from ..mesh.page_operation import _Ledger, PageOperationLimits, _ERRORS
from ..mesh.pairwise import EnvelopeError
from ..mesh.runtime_page_view import _Records, _Documents, _Directory, effective_account
from ..store import aux_inputs, local_source
from .api_agents import runner_state


_EMPTY = {'peer_asks': [], 'timers': [], 'peer_complete': False, 'timers_complete': False}


def _finalize(app, token, round_, companions, proposal=None):
    # Same screen -> session -> trust -> root -> Store lock order as pages.
    with app.lock._mx:
        if app.lock._expire_if_idle_locked():
            return False
        with app._lock:
            if not app.validate_session_read(token) or token.mesh is not round_.mesh:
                return False
            round_.finalize(companions, proposal)
            return not app.lock._expire_if_idle_locked()


def _timer(name, value):
    """Only the bounded UI timer shape crosses the raw-status boundary."""
    if type(value) is not dict:
        raise ValueError('invalid timer')
    result = {'agent': name}
    for key, limit in (('id', 256), ('chat_id', 256), ('note', 16000)):
        text = value.get(key, '')
        if type(text) is not str or len(text.encode()) > limit:
            raise ValueError('invalid timer text')
        result[key] = text
    stamp = value.get('at_ns')
    if type(stamp) is not int or not 0 <= stamp <= 2**63 - 1:
        raise ValueError('invalid timer time')
    result['at_ns'] = stamp
    repeat = value.get('repeat')
    if repeat:
        if type(repeat) is not dict or repeat.get('kind') not in ('daily', 'weekly', 'monthly'):
            raise ValueError('invalid timer repeat')
        result['repeat'] = {'kind': repeat['kind']}
        if repeat['kind'] == 'weekly':
            days = repeat.get('days', [])
            if type(days) is not list or len(days) > 7 or any(type(d) is not int or not 0 <= d <= 6 for d in days):
                raise ValueError('invalid timer days')
            result['repeat']['days'] = days
        elif repeat['kind'] == 'monthly':
            day = repeat.get('day')
            if type(day) is not int or not 1 <= day <= 31:
                raise ValueError('invalid timer day')
            result['repeat']['day'] = day
    return result


def _project(app, round_, status, peer, users, *, chat_id):
    ledger = round_.ledger
    view = SimpleNamespace(user=round_.viewer, directory=_Directory(round_),
                           keystore=round_.mesh.keystore)
    output = {**_EMPTY, 'peer_asks': [], 'timers': []}
    if status is not None and users is not None:
        timers_complete = True
        for path, doc in users.items():
            ledger.step()
            parts = path.split('/')
            if len(parts) != 2 or not parts[1].endswith('.json'):
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get('agent'), dict):
                continue
            name = parts[1][:-5]
            if view.directory.owner_of(name) != round_.viewer:
                continue
            harness = status.get(f'status/{name}_harness.json')
            if harness is not None and type(harness) is not dict:
                timers_complete = False
                continue
            timers = (harness.get('timers') if isinstance(harness, dict) else None) or []
            if type(timers) is not list or len(timers) > 512:
                timers_complete = False
                continue
            for timer in timers:
                ledger.step()
                try:
                    projected = _timer(name, timer)
                except ValueError:
                    timers_complete = False
                    continue
                if chat_id and projected['chat_id'] != chat_id:
                    continue
                output['timers'].append(projected)
                if len(output['timers']) > 512:
                    raise OverflowError('timer response budget')
        output['timers_complete'] = timers_complete
    if chat_id:
        output['peer_complete'] = True
    elif peer is not None:
        view.tx = _Documents(_Records(peer, ledger), ledger)
        for path in peer:
            ledger.step()
            parts = path.split('/')
            if (len(parts) != 6 or parts[:2] != ['runtime', 'owner-control']
                    or parts[3:5] != ['peer', 'asks'] or not parts[5].endswith('.json')):
                continue
            target = parts[2]
            try:
                ask = open_owner_ask(view, target=target, ask_id=parts[5][:-5]).record
            except (EnvelopeError, PeerControlError, AuthorityError, OSError):
                continue
            if runner_state(app, view, target) is False:
                continue
            round_.deadline = min(round_.deadline, ask['expires_ns'])
            repair, command, requester = ask['repair'], ask['command'], ask['requester']
            output['peer_asks'].append({'id': ask['id'], 'agent': target, 'kind': 'peer',
                'tool': command, 'chat_id': '', 'repair': repair, 'peer': requester,
                'detail': (f'@{requester} wants to {command} {target}\'s harness' if repair else
                           f'@{requester} wants a diagnostic session ({command})')})
            if len(output['peer_asks']) > 512:
                raise OverflowError('peer response budget')
        output['peer_complete'] = True
    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False).encode()
    ledger.charge(len(encoded))
    if len(encoded) > 1024 * 1024:
        raise OverflowError('approval companion response budget')
    return output


def capture_companions(app, mesh, token, *, chat_id=''):
    auxiliary = mesh.local_inputs.auxiliary
    auxiliary.request('identities')
    auxiliary.request('status')
    if not chat_id:
        auxiliary.request('peer')
    ledger = _Ledger(PageOperationLimits())
    for _ in range(8):
        round_ = None
        companions = []
        try:
            reader = auxiliary.reader('identities')
            receipt = reader.capture()
            round_ = AccountRound(mesh, reader, receipt, ledger)
            viewer = effective_account(round_, mesh.user)
            if viewer is None or not viewer.active:
                return dict(_EMPTY)
            with reader._read(receipt) as (conn, _):
                users = aux_inputs.capture_prefix(conn, mesh.store.path, receipt.source.raw,
                    'users', max_documents=2048, max_bytes=min(2 * 1024 * 1024, ledger.remaining()))
            ledger.charge(users.captured_bytes)
            raw = {}
            for scope in ('status',) if chat_id else ('status', 'peer'):
                try:
                    owner, observed, inputs = auxiliary.inputs(scope, max_bytes=ledger.remaining())
                except (local_source.SourceChanged, aux_inputs.AuxInputsUnavailable):
                    raw[scope] = None
                    continue
                ledger.charge(inputs.captured_bytes)
                raw[scope] = inputs.documents.documents()
                companions.append((owner, observed))
            result = _project(app, round_, raw['status'], raw.get('peer'),
                              users.documents.documents(), chat_id=chat_id)
            if _finalize(app, token, round_, tuple(companions)):
                return result
            return dict(_EMPTY)
        except _Proposal as exc:
            try:
                if round_ is None or not _finalize(app, token, round_, tuple(companions), exc.value):
                    return dict(_EMPTY)
            except _ERRORS:
                return dict(_EMPTY)
        except _Stop as exc:
            if exc.status == 'restart':
                continue
            return dict(_EMPTY)
        except _ERRORS + (sqlite3.Error,):
            return dict(_EMPTY)
    return dict(_EMPTY)
