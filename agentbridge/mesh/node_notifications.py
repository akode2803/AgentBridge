"""One fresh exact-ID notification projection, never a queued policy verdict.

This is the read primitive. Consumers must finalize through their session or
shutdown gate, then immediately derive/deliver the note. Pending preparation
retains only the wake identity; it cannot fall back to legacy full-history reads.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass

from ..core.models import MsgKind
from . import eventbus, page_fence
from .eventbus import Event
from .notify import Notification, Notifier, PREVIEW_CHARS
from .page_operation import ExactMessageProjectionOperation, PageOperationLimits, _PreparedPage


@dataclass(frozen=True)
class MessageNotificationWake:
    chat_id: str
    message_id: str
    sender: str
    ns: int

    @classmethod
    def capture(cls, event: Event):
        if (type(event) is not Event or event.type != eventbus.MESSAGE
                or type(event.data) is not dict or set(event.data) != {'id', 'from'}
                or any(type(value) is not str or not value or len(value.encode()) > 4096
                       or '\x00' in value for value in
                       (event.chat_id, event.data['id'], event.data['from']))
                or type(event.ns) is not int or not 0 <= event.ns <= 2**63 - 1):
            raise ValueError('invalid message notification wake')
        return cls(event.chat_id, event.data['id'], event.data['from'], event.ns)


@dataclass(frozen=True)
class NotificationReadResult:
    status: str
    notification: Notification | None = None
    reason: str = ''


class MessageNotificationOperation(ExactMessageProjectionOperation):
    """Recompute current visibility, content and signed viewer policy at drain."""

    def __init__(self, mesh, wake: MessageNotificationWake, *, source_reader,
                 limits=PageOperationLimits()):
        if type(wake) is not MessageNotificationWake:
            raise ValueError('invalid notification identity')
        # Validate direct construction as strictly as an EventBus capture.
        wake = MessageNotificationWake.capture(Event(eventbus.MESSAGE, wake.chat_id,
            {'id': wake.message_id, 'from': wake.sender}, wake.ns))
        self.__wake = wake
        super().__init__(mesh, wake.chat_id, (wake.message_id,),
                         source_reader=source_reader, limits=limits)
        from .local_input_runtime import LocalInputRuntime
        if (type(mesh.local_inputs) is not LocalInputRuntime
                or mesh.local_inputs.store is not self._store
                or mesh.local_inputs.transport is not self._transport):
            raise ValueError('invalid notification source owner')
        self.__owner = mesh.local_inputs

    def _bound(self):
        return (super()._bound() and self.mesh.local_inputs is self.__owner
                and not self.__owner._closed)

    def _exact_presentation(self, snapshot, viewer_state, context=None):
        mute = viewer_state.get('mute', False)
        state = {
            'read_ns': int(viewer_state.get('read_ns') or 0),
            'archived': bool(viewer_state.get('archived')),
            'mute': mute if (type(mute) is bool
                            or type(mute) is int and 0 <= mute <= 2**63 - 1) else None,
        }
        snapshot_json = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(',', ':'))
        state_json = json.dumps(state, sort_keys=True, separators=(',', ':'))
        self.ledger.charge(len(snapshot_json.encode()) + len(state_json.encode()))
        return page_fence.PagePresentation(snapshot_json, state_json, ())

    def finalize_notification(self, prepared, *, app=None, session=None,
                              notifier=None, notifier_generation=None):
        """Finalize and derive immediately under an exact consumer gate.

        GUI callers pass their captured session and app; background callers
        join the exact local input owner's shutdown gate. This API cannot turn
        an arbitrary previously finalized result into a new notification.
        """
        if type(prepared) is not _PreparedPage or prepared._operation is not self:
            raise ValueError('foreign notification preparation')
        if app is not None:
            if notifier is not None or notifier_generation is not None:
                raise ValueError('conflicting notification consumers')
            from ..gui.context import GuiApp, SessionReadToken
            if type(app) is not GuiApp or type(session) is not SessionReadToken:
                raise ValueError('invalid notification session gate')
            # The canonical GUI finalizer owns screen -> session ordering.
            # AppLock is deliberately non-reentrant; never reacquire it here.
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
        from .local_input_runtime import LocalInputRuntime
        owner = self.__owner
        if (type(owner) is not LocalInputRuntime or owner.store is not self._store
                or owner.transport is not self._transport
                or self.mesh.local_inputs is not owner):
            return NotificationReadResult('unavailable', reason='notification_owner_changed')
        # Consumer gate precedes source gates. Application callbacks run only
        # after SQL commit and release of ingestion/scheduling locks.
        with notifier._delivery_gate if notifier is not None else nullcontext():
            if notifier is not None and (notifier._stop.is_set()
                    or notifier._delivery_generation != notifier_generation):
                return NotificationReadResult('unavailable', reason='notification_consumer_changed')
            with owner._notification_gate:
                with owner._worker_lock:
                    with owner._lock:
                        if owner._closed or not self._bound():
                            return NotificationReadResult('unavailable', reason='notification_owner_changed')
                        result = self._notification(prepared.finalize())
                if notifier is not None:
                    notifier._deliver_note(result.notification, owner=owner)
                return result

    def _notification(self, finalized):
        if finalized.status != 'page' or finalized.result is None:
            return NotificationReadResult(finalized.status, reason=finalized.reason)
        result = finalized.result
        if result.page is None or result.presentation is None:
            return NotificationReadResult('unavailable', reason='notification_presentation_missing')
        wake = self.__wake
        message = next((m for m in result.page.messages if m.id == wake.message_id), None)
        if (message is None or message.kind is not MsgKind.MESSAGE or message.deleted
                or message.undecrypted or (message.chat_id, message.from_, message.ns)
                != (wake.chat_id, wake.sender, wake.ns) or message.from_ == self.viewer):
            return NotificationReadResult('ready')
        state = json.loads(result.presentation.viewer_state_json)
        if state['mute'] is None or Notifier._muted(state) or wake.ns <= state['read_ns']:
            return NotificationReadResult('ready')
        snapshot = json.loads(result.presentation.snapshot_json)
        note = Notification('message', wake.chat_id, snapshot.get('name', ''),
            message.from_, (message.body or '')[:PREVIEW_CHARS], wake.ns,
            chat_kind=snapshot.get('kind', ''))
        return NotificationReadResult('ready', note)


__all__ = ['MessageNotificationWake', 'MessageNotificationOperation', 'NotificationReadResult']
