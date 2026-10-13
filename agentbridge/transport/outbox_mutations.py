"""Explicit outbox consumer binding for lifecycle-ticket mutation writers."""
from __future__ import annotations

import json
import re

from ..store.db import OutboxItem
from .typed_mutations import prepare_mutation
from .application_mutations import stable_append_id


def _payload(item):
    payload = json.loads(item.payload_json)
    if type(payload) is not dict or payload != item.payload:
        raise ValueError('outbox payload changed')
    if 'envelope' in payload:
        manifests = payload.get('attachments')
        if (set(payload) != {'v', 'envelope', 'attachments'}
                or type(payload.get('v')) is not int or payload['v'] != 1
                or type(payload['envelope']) is not dict
                or type(manifests) is not list or not 1 <= len(manifests) <= 1024):
            raise ValueError('invalid attachment outbox wrapper')
        seen = set()
        for manifest in manifests:
            blob = manifest.get('blob_id') if type(manifest) is dict else None
            if (type(blob) is not str
                    or re.fullmatch(r'f2-[A-Za-z0-9._-]{1,177}', blob) is None
                    or manifest.get('v') != 2 or blob in seen):
                raise ValueError('invalid attachment outbox manifest identity')
            seen.add(blob)
    return payload


class OutboxMutationOwner:
    def __init__(self, transport, store, dispatch):
        writer = transport._bound_writer()
        if writer is None or not writer.driver.journal.lifecycle_enabled():
            raise ValueError('outbox requires a lifecycle-bound mutation writer')
        self.transport, self.store, self.dispatch_callback = transport, store, dispatch
        self.writer = writer

    def _check(self, item, *, state):
        if self.transport._bound_writer() is not self.writer:
            raise ValueError('outbox mutation writer changed')
        if type(item) is not OutboxItem or item.kind != 'append_log':
            raise ValueError('unsupported typed outbox owner')
        payload = _payload(item)
        if state == 'pending':
            chat, separator, log = item.target.partition('|')
            if not separator:
                raise ValueError('invalid outbox target')
            envelope = payload.get('envelope', payload)
            legacy = self.writer.driver.journal.get_operation(
                stable_append_id(chat, log, envelope.get('id')))
            if legacy is not None:
                # Pre-consumer operations cannot be replaced by a fresh ticket.
                # Drain them using the old owner before enabling this owner.
                raise ValueError('drain pre-consumer append before typed outbox activation')
        row = self.store.outbox_owner_row(item.seq)
        if row != (state, item.owner_digest, item.kind, item.target):
            raise ValueError('outbox owner is no longer current')

    def dispatch(self, item):
        self._check(item, state='pending')
        self.dispatch_callback(item.target, item.payload,
                               _context=OutboxAppendContext(self, item))
        if self.transport._bound_writer() is not self.writer:
            raise ValueError('outbox mutation writer changed')

    def release(self, item):
        self._check(item, state='sent')
        journal = self.writer.driver.journal
        payload = _payload(item)
        manifests = payload.get('attachments', []) if isinstance(payload.get('envelope'), dict) else []
        # The append receipt owns completion of all preceding manifests. Root
        # cleanup independently validates every leg before forgetting any slot.
        for manifest in manifests:
            journal.consume_outbox_leg(self.store, item.seq,
                                       'manifest:' + manifest['blob_id'], item.owner_digest)
        journal.consume_outbox_leg(self.store, item.seq, 'append', item.owner_digest)
        journal.finish_outbox(self.store, item.seq, item.owner_digest)


class OutboxAppendContext:
    def __init__(self, owner, item):
        self.owner, self.item = owner, item

    def _arguments(self, chat, log, envelope):
        self.owner._check(self.item, state='pending')
        payload = _payload(self.item)
        original = payload.get('envelope', payload)
        if self.item.target != chat + '|' + log or original != envelope:
            raise ValueError('outbox append target or payload changed')
        return {'p_chat': chat, 'p_log': log, 'p_record': envelope}

    def recover_append_log(self, chat, log, envelope):
        arguments = self._arguments(chat, log, envelope)
        writer = self.owner.writer
        journal = writer.driver.journal
        ticket = journal.get_outbox_leg(self.owner.store, self.item.seq,
                                        'append', self.item.owner_digest)
        if ticket is None:
            return False
        retained = journal.get_ticket(ticket).request
        expected = prepare_mutation(writer.root, ticket.protocol_epoch,
                                    ticket.operation_id, writer.actor_id,
                                    'log_append', arguments)
        if retained.request_json != expected.request_json:
            raise ValueError('outbox append replay changed')
        writer.driver.resume(ticket.protocol_epoch, ticket.operation_id)
        return True

    def append_log(self, chat, log, envelope):
        if self.recover_append_log(chat, log, envelope):
            return
        arguments = self._arguments(chat, log, envelope)
        writer = self.owner.writer
        ticket = writer.driver.journal.allocate_outbox_ticket(
            self.owner.store, self.item.seq, 'append', self.item.owner_digest,
            'log_append', arguments)
        writer.driver.resume(ticket.protocol_epoch, ticket.operation_id)
