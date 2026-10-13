"""Inactive authenticated epoch observation; no rotation or automatic pruning."""
from __future__ import annotations

from dataclasses import dataclass

from ..node.protocol import canonical_endpoint
from .mutation_protocol import _root, _uuid


@dataclass(frozen=True)
class ProviderEpoch:
    endpoint: str
    root: str
    actor_id: str
    lifecycle_id: str
    protocol_epoch: str
    generation: int

    def __post_init__(self):
        if canonical_endpoint(self.endpoint) != self.endpoint:
            raise ValueError('invalid epoch endpoint')
        _root(self.root)
        for value in (self.actor_id, self.lifecycle_id, self.protocol_epoch):
            _uuid(value, 'mutation epoch identity')
        if type(self.generation) is not int or not 0 <= self.generation <= 2**63 - 1:
            raise ValueError('invalid epoch generation')


@dataclass(frozen=True)
class OperationTicket:
    """A caller-retained original epoch; a missing ticket is never a new write."""
    protocol_epoch: str
    operation_id: str

    def __post_init__(self):
        _uuid(self.protocol_epoch, 'operation ticket epoch')
        _uuid(self.operation_id, 'operation ticket id')


def decode_provider_epoch(raw, *, endpoint, root, actor_id):
    expected = {'schema_version', 'root', 'actor_id', 'role', 'lifecycle_id',
                'protocol_epoch', 'generation', 'sealed'}
    if (type(raw) is not dict or set(raw) != expected
            or type(raw['schema_version']) is not int or raw['schema_version'] != 1
            or raw['root'] != root or raw['actor_id'] != actor_id
            or raw['role'] != 'authenticated' or raw['sealed'] is not True):
        raise ValueError('invalid provider epoch evidence')
    return ProviderEpoch(endpoint, root, actor_id, raw['lifecycle_id'],
                         raw['protocol_epoch'], raw['generation'])


def observe_provider_epoch(rpc):
    """Pinned authenticated read, with identity checks before and after I/O.

    This describes receipt closure only. It never supplies membership authority
    or node publication readiness, and never invokes administrative rotation.
    """
    from .typed_mutations import TypedMutationRPC, TypedMutationRPCError

    if type(rpc) is not TypedMutationRPC:
        raise TypeError('invalid mutation epoch observer')
    raw = rpc._call('ab_mutation_epoch_state', {'p_root': rpc.root})
    try:
        return decode_provider_epoch(raw, endpoint=rpc.endpoint, root=rpc.root,
                                     actor_id=rpc.actor_id)
    except (TypeError, ValueError):
        raise TypedMutationRPCError('invalid mutation epoch evidence') from None
