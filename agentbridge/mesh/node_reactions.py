"""Fresh semantic reaction projection from identity-only breadcrumbs.

Breadcrumb hints are never trusted as notification content.  Every attempt
re-reads the exact info envelope, current native reaction operation, canonical
target message and viewer policy under one final local-source fence.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass

from .. import crypto
from ..core.models import MsgKind, UserKind
from ..store import node_event_journal, overlay_index
from ..transport.authority_observation import _part
from . import eventbus, events, page_fence, readmodel, reaction_documents
from .eventbus import Event
from .notify import Notification, Notifier
from .page_operation import (
    PageOperation, PageOperationLimits, _PreparedPage, _Work,
)
from .paths import P


def _text(value, maximum, *, empty=False):
    if type(value) is not str or '\x00' in value or not empty and not value:
        return False
    try:
        return len(value.encode('utf-8')) <= maximum
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True)
class ReactionNotificationWake:
    chat_id: str
    event_id: str
    sender: str
    ns: int

    @classmethod
    def capture(cls, event: Event):
        if (type(event) is not Event or event.type != eventbus.REACTION
                or type(event.data) is not dict or set(event.data) != {'id', 'from'}
                or any(not _text(value, 4096) for value in
                       (event.chat_id, event.data['id'], event.data['from']))
                or type(event.ns) is not int or not 1 <= event.ns <= 2**63 - 1):
            raise ValueError('invalid reaction notification wake')
        return cls(event.chat_id, event.data['id'], event.data['from'], event.ns)


@dataclass(frozen=True)
class _Candidate:
    event_id: str
    sender: str
    event_ns: int
    target_id: str
    emoji: str

    def to_dict(self):
        return {'id': self.event_id, 'from': self.sender, 'ns': self.event_ns,
                'target': self.target_id, 'emoji': self.emoji}


@dataclass(frozen=True)
class ReactionNotificationResult:
    status: str
    notification: Notification | None = None
    reason: str = ''


class ReactionProjectionOperation(PageOperation):
    """Project up to 32 reaction breadcrumbs without trusting their hints."""

    MAX_EVENTS = node_event_journal.MAX_PROJECT_EVENTS

    def __init__(self, mesh, chat_id, event_ids, *, source_reader,
                 limits=PageOperationLimits(), event_identities=()):
        if (type(event_ids) is not tuple or not event_ids
                or len(event_ids) > self.MAX_EVENTS
                or len(set(event_ids)) != len(event_ids)):
            raise ValueError('invalid exact reaction event IDs')
        for ident in event_ids:
            overlay_index._text(ident, 'message id')
        if (type(event_identities) is not tuple
                or len(event_identities) > self.MAX_EVENTS
                or any(type(item) is not node_event_journal.NodeEventIdentity
                       for item in event_identities)
                or event_identities and (
                    tuple(item.message_id for item in event_identities) != event_ids
                    or any(item.chat_id != chat_id or item.kind != 'info'
                           for item in event_identities))):
            raise ValueError('invalid reaction journal identities')
        super().__init__(
            mesh, chat_id, source_reader=source_reader, limit=len(event_ids),
            scan_budget=1, limits=limits, summary_only=True,
            defer_receipts=True, defer_group_receipts=True,
        )
        self.__event_ids = event_ids
        self.__event_identities = tuple(event_identities)
        from .local_input_runtime import LocalInputRuntime
        if (type(mesh.local_inputs) is not LocalInputRuntime
                or mesh.local_inputs.store is not self._store
                or mesh.local_inputs.transport is not self._transport):
            raise ValueError('invalid reaction source owner')
        self.__owner = mesh.local_inputs

    def _bound(self):
        return (super()._bound() and self.mesh.local_inputs is self.__owner
                and not self.__owner._closed)

    def _exact_projection_request(self):
        return self.__event_ids, self.__event_identities

    def _exact_include_reactions(self):
        # Semantic proof uses each actor's exact raw document. Target
        # visibility needs viewer state, edits and redactions, not the room's
        # complete reaction manifest.
        return False

    def _breadcrumb(self, row, round_, snapshot):
        if row.kind != 'info':
            return None
        try:
            env = row.decoded()
        except Exception:  # malformed admitted payload is terminally nonsemantic
            return None
        if (set(env) != {'id', 'ns', 'ts', 'from', 'kind', 'sig', 'event'}
                or env.get('kind') != 'info' or not _text(env.get('ts'), 256, empty=True)
                or not _text(env.get('sig'), 4096)
                or type(env.get('event')) is not dict):
            return None
        event = env['event']
        if (set(event) != {'type', 'msg_id', 'emoji', 'to',
                          'reaction_id', 'reaction_ns'}
                or event.get('type') != events.EV_REACTION
                or not _text(event.get('msg_id'), 4096)
                or not _text(event.get('emoji'), 128)
                or not _text(event.get('to'), 4096, empty=True)
                or not _text(event.get('reaction_id'), 128)
                or type(event.get('reaction_ns')) is not int
                or not 1 <= event['reaction_ns'] < row.key.ns):
            return None
        actor = row.key.sender
        try:
            _part(actor)
        except ValueError:
            return None
        # Historical responsible-owner bindings are not yet a captured input.
        # Restrict semantic notification proof to humans; agent reactions stay
        # generic read-model invalidations until that witness exists.
        if round_.kind(actor) is not UserKind.HUMAN:
            return None
        public = round_.sign_pub(actor)
        signed = events.signing_bytes(self.chat, env)
        self.ledger.signature(signed)
        spans = snapshot.tenure.get(actor)
        if (not spans or not public or not crypto.verify(public, env['sig'], signed)
                or not readmodel.member_at(spans, row.key.ns)
                or not readmodel.member_at(
                    spans, event['reaction_ns'])):
            return None
        return env, event, public

    def _resolve_exact_projection(
            self, round_, snapshot, source_binding, inputs, requested_targets):
        rows = {row.key.id: row for row in inputs.exact_rows
                if row.key.id in requested_targets}
        prelim = []
        for ident in requested_targets:
            row = rows.get(ident)
            if row is None:
                continue
            value = self._breadcrumb(row, round_, snapshot)
            if value is not None:
                prelim.append((ident, *value))
        if not prelim:
            return (), ()
        actors = tuple(dict.fromkeys(env['from'] for _, env, _, _ in prelim))
        observed = self.source_reader.capture_reaction_documents(
            source_binding, actors, max_bytes=self.ledger.remaining())
        by_path = {record.path: record for record in observed.records}
        self.ledger.charge(sum(
            len(record.path.encode()) +
            (0 if record.payload_json is None else len(record.payload_json.encode()))
            for record in observed.records
        ))
        candidates = []
        for ident, env, hint, public in prelim:
            record = by_path.get(P.reactions(self.chat, env['from']))
            if record is None or record.deleted or record.payload_json is None:
                # An authentic event may arrive before its overlay document.
                raise _Work('reaction_document')
            try:
                document = record.decoded()
                _mapping, operations = reaction_documents.parse_native_v2(document)
                signed = reaction_documents.signing_bytes(
                    self.chat, env['from'], document)
                self.ledger.signature(signed)
                signature_valid = crypto.verify(public, document['sig'], signed)
            except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
                continue
            if not signature_valid:
                continue
            operation = operations.get(hint['msg_id'])
            if operation is None:
                if document['ns'] < hint['reaction_ns']:
                    raise _Work('reaction_document')
                continue
            if operation.ns < hint['reaction_ns']:
                raise _Work('reaction_document')
            if (operation.ns != hint['reaction_ns']
                    or operation.id != hint['reaction_id']
                    or operation.emoji != hint['emoji']
                    or not operation.emoji):
                continue
            candidates.append(_Candidate(
                ident, env['from'], env['ns'], hint['msg_id'], operation.emoji,
            ))
        absent = set(inputs.absent_ids)
        if any(item.target_id in absent for item in candidates):
            # A valid breadcrumb/document pair can precede its target log row.
            # Hidden, cleared and redacted targets are present and are handled
            # terminally by canonical selection below; absence is unresolved.
            raise _Work('reaction_target')
        return tuple(dict.fromkeys(item.target_id for item in candidates)), tuple(candidates)

    def _exact_presentation(self, snapshot, viewer_state, context=None):
        mute = viewer_state.get('mute', False)
        state = {
            'read_ns': int(viewer_state.get('read_ns') or 0),
            'archived': bool(viewer_state.get('archived')),
            'mute': mute if (type(mute) is bool or type(mute) is int
                             and 0 <= mute <= 2**63 - 1) else None,
        }
        snapshot_json = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(',', ':'))
        state_json = json.dumps(state, sort_keys=True, separators=(',', ':'))
        decoration = json.dumps(
            {'reaction_events': [item.to_dict() for item in context or ()]},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        )
        self.ledger.charge(sum(len(value.encode()) for value in
                               (snapshot_json, state_json, decoration)))
        return page_fence.PagePresentation(
            snapshot_json, state_json, (), decoration_json=decoration,
        )

    @staticmethod
    def events(finalized, viewer):
        if (finalized.status != 'page' or finalized.result is None
                or finalized.result.page is None
                or finalized.result.presentation is None):
            return ()
        messages = {message.id: message for message in finalized.result.page.messages}
        data = json.loads(finalized.result.presentation.decoration_json)
        result = []
        for item in data['reaction_events']:
            message = messages.get(item['target'])
            if (message is not None and message.kind is MsgKind.MESSAGE
                    and not message.deleted and not message.undecrypted
                    and message.from_ == viewer and item['from'] != viewer):
                result.append(Event(
                    eventbus.REACTION, message.chat_id,
                    {'id': item['id'], 'from': item['from']}, item['ns'],
                ))
        return tuple(result)


class ReactionNotificationOperation(ReactionProjectionOperation):
    def __init__(self, mesh, wake: ReactionNotificationWake, *, source_reader,
                 limits=PageOperationLimits()):
        if type(wake) is not ReactionNotificationWake:
            raise ValueError('invalid reaction notification identity')
        wake = ReactionNotificationWake.capture(Event(
            eventbus.REACTION, wake.chat_id,
            {'id': wake.event_id, 'from': wake.sender}, wake.ns,
        ))
        self.__wake = wake
        super().__init__(mesh, wake.chat_id, (wake.event_id,),
                         source_reader=source_reader, limits=limits)

    def finalize_notification(self, prepared, *, app=None, session=None,
                              notifier=None, notifier_generation=None):
        if type(prepared) is not _PreparedPage or prepared._operation is not self:
            raise ValueError('foreign reaction notification preparation')
        if app is not None:
            if notifier is not None or notifier_generation is not None:
                raise ValueError('conflicting notification consumers')
            from ..gui.context import GuiApp, SessionReadToken
            if type(app) is not GuiApp or type(session) is not SessionReadToken:
                raise ValueError('invalid notification session gate')
            return self._notification(app.finalize_page_read(session, prepared))
        if session is not None:
            raise ValueError('notification session requires an app gate')
        if notifier is not None:
            if (type(notifier) is not Notifier or notifier._node_mesh is not self.mesh
                    or notifier.messaging is not self.mesh.messaging
                    or notifier.sealer is not self.mesh.sealer or notifier.user != self.viewer
                    or type(notifier_generation) is not int or notifier_generation < 0):
                raise ValueError('invalid background notification gate')
        elif notifier_generation is not None:
            raise ValueError('notification generation requires a consumer')
        owner = self.mesh.local_inputs
        with notifier._delivery_gate if notifier is not None else nullcontext():
            if notifier is not None and (notifier._stop.is_set()
                    or notifier._delivery_generation != notifier_generation):
                return ReactionNotificationResult(
                    'unavailable', reason='notification_consumer_changed')
            with owner._notification_gate:
                with owner._worker_lock:
                    with owner._lock:
                        if owner._closed or not self._bound():
                            return ReactionNotificationResult(
                                'unavailable', reason='notification_owner_changed')
                        result = self._notification(prepared.finalize())
                if notifier is not None:
                    notifier._deliver_note(result.notification, owner=owner)
                return result

    def _notification(self, finalized):
        if finalized.status != 'page' or finalized.result is None:
            return ReactionNotificationResult(finalized.status, reason=finalized.reason)
        result = finalized.result
        if result.page is None or result.presentation is None:
            return ReactionNotificationResult(
                'unavailable', reason='notification_presentation_missing')
        wake = self.__wake
        data = json.loads(result.presentation.decoration_json)
        candidate = next((item for item in data['reaction_events']
                          if (item['id'], item['from'], item['ns'])
                          == (wake.event_id, wake.sender, wake.ns)), None)
        if candidate is None or wake.sender == self.viewer:
            return ReactionNotificationResult('ready')
        message = next((message for message in result.page.messages
                        if message.id == candidate['target']), None)
        if (message is None or message.kind is not MsgKind.MESSAGE
                or message.deleted or message.undecrypted or message.from_ != self.viewer):
            return ReactionNotificationResult('ready')
        state = json.loads(result.presentation.viewer_state_json)
        if state['mute'] is None or Notifier._muted(state) or wake.ns <= state['read_ns']:
            return ReactionNotificationResult('ready')
        snapshot = json.loads(result.presentation.snapshot_json)
        emoji = candidate['emoji']
        note = Notification(
            'reaction', wake.chat_id, snapshot.get('name', ''), wake.sender,
            f'Reacted {emoji} to your message'.strip(), wake.ns,
            chat_kind=snapshot.get('kind', ''), emoji=emoji,
        )
        return ReactionNotificationResult('ready', note)


__all__ = [
    'ReactionNotificationOperation', 'ReactionNotificationResult',
    'ReactionNotificationWake', 'ReactionProjectionOperation',
]
