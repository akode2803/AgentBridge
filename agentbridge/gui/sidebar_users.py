"""Capped public names from one admitted users source; no privacy verdicts."""
from __future__ import annotations

import json

from ..core.models import Account, UserKind
from ..store.aux_inputs import AuxInputs
from ..transport.authority_observation import _part


MAX_USERS = 2048
MAX_BYTES = 2 * 1024 * 1024


class UsersUnavailable(RuntimeError):
    pass


def _text(value, limit, label):
    if type(value) is not str:
        raise UsersUnavailable('invalid_' + label)
    try:
        if len(value.encode()) > limit:
            raise UsersUnavailable(label + '_byte_budget')
    except UnicodeError as exc:
        raise UsersUnavailable('invalid_' + label) from exc
    return value


def _accounts(inputs):
    if (type(inputs) is not AuxInputs or inputs.prefix != 'users'
            or len(inputs.documents.records) > MAX_USERS or inputs.captured_bytes > MAX_BYTES):
        raise UsersUnavailable('users_input_budget')
    seen = set()
    for record in inputs.documents.records:
        path = record.path
        # Nested JSON under users/ is not an account; Directory.get resolves
        # only exact users/<name>.json documents.
        if type(path) is not str or not path.startswith('users/'):
            raise UsersUnavailable('users_path_changed')
        tail = path[6:]
        if '/' in tail or not tail.endswith('.json'):
            continue
        name = tail[:-5]
        try:
            _part(name)
            raw = json.loads(record.payload_json)
            if type(raw) is not dict:
                raise UsersUnavailable('invalid_user_document')
            account = Account.from_dict(raw)
        except (ValueError, TypeError, UnicodeError, AttributeError) as exc:
            raise UsersUnavailable('invalid_user_document') from exc
        if account.name != name or name in seen:
            raise UsersUnavailable('invalid_user_identity')
        seen.add(name)
        yield account


def public_users(inputs):
    """Public fallback only; private profile/presence remains pending."""
    users = {}
    used = 0
    for account in _accounts(inputs):
        name = account.name
        entry = {
            'name': name, 'username': name,
            'handle': _text(account.handle or name, 256, 'handle'),
            'kind': account.kind.value,
            'display': _text(account.display or name, 256, 'display'),
            'active': bool(account.active),
        }
        if account.deactivated:
            entry['departed'] = True
        if account.kind is UserKind.AGENT and account.agent:
            entry['owners'] = [_text(account.agent.owner, 256, 'owner')]
        used += len(json.dumps({name: entry}, ensure_ascii=False).encode())
        if used > MAX_BYTES:
            raise UsersUnavailable('user_response_byte_budget')
        users[name] = entry
    return users


def owner_candidates(inputs, viewer, *, max_candidates=64):
    """Names only; caller must re-read/verify each current account in its round."""
    _part(viewer)
    if type(max_candidates) is not int or not 1 <= max_candidates <= 64:
        raise ValueError('invalid owner candidate budget')
    names = []
    for account in _accounts(inputs):
        if account.kind is UserKind.AGENT and account.agent and account.agent.owner == viewer:
            names.append(account.name)
            if len(names) > max_candidates:
                raise UsersUnavailable('owner_candidate_budget')
    return tuple(names)
