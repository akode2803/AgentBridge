"""Pure semantic deltas and signing for durable per-user state actions."""
from __future__ import annotations

from typing import Any

from ..core.timekit import next_ns
from .events import state_signing_bytes

_MAX_NS = 2**63 - 1


def _fields(document: dict) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in ('ns', 'sig')}


def _cursor(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(result, 0)


def reduce_state_action(current: dict, kind: str, args: dict) -> dict | None:
    """Apply one validated immutable logical delta to a verified source doc.

    None is a true semantic no-op, never an authority claim. The caller must
    already have verified `current` against the state owner's signing key.
    """
    state = _fields(current)
    if kind == 'mark_read':
        old = _cursor(state.get('read_ns'))
        if old >= args['up_to_ns'] and not state.get('forced_unread'):
            return None
        state.update(read_ns=max(old, args['up_to_ns']), read_ts=args['at'])
        if state.get('forced_unread'):
            state['forced_unread'] = False
    elif kind == 'mark_delivered':
        old = _cursor(state.get('delivered_ns'))
        if old >= args['up_to_ns']:
            return None
        state.update(delivered_ns=args['up_to_ns'], delivered_ts=args['at'])
    elif kind in {'star', 'hide'}:
        field = 'starred' if kind == 'star' else 'hidden'
        prior = state.get(field, [])
        prior = prior if type(prior) is list else []
        state[field] = list(dict.fromkeys([*prior, *args['msg_ids']]))
    elif kind in {'unstar', 'unhide'}:
        field = 'starred' if kind == 'unstar' else 'hidden'
        prior = state.get(field, [])
        prior = prior if type(prior) is list else []
        drop = set(args['msg_ids'])
        state[field] = [item for item in prior if item not in drop]
    elif kind == 'clear':
        prior = state.get('hidden_runtime', [])
        prior = prior if type(prior) is list else []
        state['hidden_runtime'] = list(dict.fromkeys([*prior, *args['runtime_ids']]))
        if args['up_to_ns']:
            state['cleared'] = {'ns': args['up_to_ns'],
                                'keep_starred': args['keep_starred'],
                                'at': args['at']}
    elif kind == 'set_flag':
        state[args['name']] = args['value']
    else:
        raise ValueError('invalid state action family')
    return state


def sign_state_action(chat: str, user: str, current: dict, fields: dict,
                      kind: str, args: dict, signer, verifier) -> dict:
    """Sign a fresh attempt; the journal retains its bytes before dispatch."""
    previous = current.get('ns')
    ns = max(next_ns(), previous + 1 if type(previous) is int and previous > 0 else 1)
    if ns > _MAX_NS:
        raise OverflowError('state action namespace exhausted')
    data = state_signing_bytes(chat, user, ns, fields)
    signature = signer(data)
    if type(signature) is not str or not signature:
        raise ValueError('state action signing failed')
    document = {**fields, 'ns': ns, 'sig': signature}
    if verifier(document) is not True:
        raise ValueError('state action owner signature did not verify')
    return document


__all__ = ['reduce_state_action', 'sign_state_action']
