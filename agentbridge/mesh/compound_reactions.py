"""Build one conditional signed checkpoint/reaction pair without side effects."""
from __future__ import annotations

from .events import state_signing_bytes
from .overlays import build_reaction_document


def build_reaction_pair(chat, user, message, emoji, state, reactions, *, signer, verifier):
    fields = {key: value for key, value in state.items() if key not in ('ns', 'sig')}
    checkpoint = None

    def capture(ns):
        nonlocal checkpoint
        checkpoint = {**fields, 'ns': ns,
                      'sig': signer(state_signing_bytes(chat, user, ns, fields))}
        if verifier(checkpoint) is not True:
            raise ValueError('reaction checkpoint signature did not verify')
        return checkpoint

    previous = state.get('ns')
    minimum = previous + 1 if type(previous) is int and previous > 0 else 1
    document, operation = build_reaction_document(
        chat, user, message, emoji, reactions, signer=signer,
        state_checkpoint=capture if emoji else None, minimum_ns=minimum)
    if checkpoint is None:
        capture(operation.ns)
    if not document.get('sig') or not document.get('ops_sig'):
        raise ValueError('reaction signing key is unavailable')
    return checkpoint, document
