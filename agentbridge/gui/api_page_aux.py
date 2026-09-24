"""Separately painted, bounded selected-chat companions at a canonical final cut."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from ..core.models import UserKind
from ..harness.runtime.controls import read_pause
from ..mesh import page_fence, profile_presentation
from ..mesh.page_operation import PageOperation
from ..mesh.runtime_page_view import RuntimePageView, effective_account, _Directory
from ..mesh.sealer import E2EESealer
from ..mesh.paths import P
from ..store import aux_inputs, document_observation, local_source, overlay_index, presence_index
from ..transport.authority_observation import _part
from .api_pages import _pending
from .context import session_read_binding
from .routing import authed_read_token
from .serialize import user_json


_PENDING_INPUTS = (local_source.SourceChanged, aux_inputs.AuxInputsUnavailable,
                   OSError, sqlite3.Error, OverflowError)


class AuxiliaryPageOperation(PageOperation):
    def __init__(self, app, mesh, chat, **kwargs):
        super().__init__(mesh, chat, limit=1, scan_budget=1, summary_only=True, **kwargs)
        self.app = app

    def _decorate(self, round_, snapshot, receipt, index, expected, sealer):
        from .api_runtime import contributor_rows, authority_rows
        runtime = self.mesh.local_inputs
        status = {'live': 'pending', 'runtime': 'pending', 'pause': 'pending',
                  'profiles': 'pending', 'presence': 'pending'}
        data = {'feeds': [], 'tasks': [], 'runs': [], 'users': {}, 'metadata_status': status}
        fences = []
        auxiliary = runtime.auxiliary
        for scope, chat in (('status', ''), ('runtime', self.chat)):
            auxiliary.request(scope, chat)
            try:
                _reader, observed, inputs = auxiliary.inputs(scope, chat,
                    max_bytes=min(8 * 1024 * 1024, round_.ledger.remaining()))
            except _PENDING_INPUTS:
                continue
            round_.ledger.charge(inputs.captured_bytes)
            raw = inputs.documents.documents()
            fences.append(observed)
            if scope == 'status':
                data['feeds'] = self._feeds(round_, snapshot, raw)
                status['live'] = 'ready'
                continue
            # Every ledger dependency comes from captured chat/raw inputs; the
            # legacy validators receive no provider, Store or snapshot fallback.
            with self.source_reader._read(receipt) as (conn, _):
                state = document_observation._capture_selected(conn, self.mesh.store.path,
                    receipt.source.raw, (P.state(self.chat, self.viewer),),
                    max_documents=1, max_bytes=min(4 * 1024 * 1024, round_.ledger.remaining()))
                keys = aux_inputs.capture_prefix(conn, self.mesh.store.path, receipt.source.raw,
                    f'chats/{self.chat}/keys', max_documents=64,
                    max_bytes=min(4 * 1024 * 1024, round_.ledger.remaining()))
            round_.ledger.charge(keys.captured_bytes)
            round_.ledger.charge(sum(len(row.payload_json.encode()) for row in state.records if row.payload_json))
            epochs = [doc for doc in keys.documents.documents().values()
                      if isinstance(doc, dict) and isinstance(doc.get('epoch'), int)]
            latest = max(epochs, key=lambda doc: doc['epoch']) if epochs else None
            adapter = RuntimePageView(round_, snapshot, raw,
                latest_key=(latest['epoch'], latest) if latest is not None else None,
                key_lookup=lambda epoch: sealer.key(self.chat, epoch),
                state_document=state.documents().get(P.state(self.chat, self.viewer)),
                encrypted=type(self.mesh.sealer) is E2EESealer)
            data['agents_paused'] = read_pause(adapter.directory, adapter.tx,
                                               chat_id=self.chat, snapshot=snapshot, source='cached')
            status['pause'] = 'ready'
            data['tasks'] = contributor_rows(adapter, self.chat)
            data['runs'] = authority_rows(adapter, self.chat)
            status['runtime'] = 'ready'
        display = self._profiles(round_, snapshot, receipt, data)
        # Live controls use wall-clock expiration. They are presentation only;
        # time never establishes membership, and long computation must retry.
        deadline = round_.now + 1_000_000_000
        round_.deadline = deadline if round_.deadline is None else min(round_.deadline, deadline)
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        round_.ledger.charge(len(encoded.encode()))
        if len(encoded.encode()) > 4 * 1024 * 1024:
            raise OverflowError('auxiliary response budget')
        return encoded, tuple(fences), display

    def _feeds(self, round_, snapshot, documents):
        from .api_messages import _age_s
        from .api_agents import runner_state
        from .livefeed import expand_runs, suppress_superseded_preparing
        feeds = []
        owned = SimpleNamespace(directory=_Directory(round_))
        for path, doc in documents.items():
            round_.ledger.step()
            if not isinstance(doc, dict):
                continue
            runs = expand_runs(path, doc)
            if runs:
                for run in runs:
                    round_.ledger.step()
                    if run.get('state') != 'running' or run.get('chat_id') != self.chat:
                        continue
                    age = _age_s(run.get('updated', ''))
                    if age is not None and age > 7200:
                        continue
                    if runner_state(self.app, owned, run.get('agent') or '') is False:
                        continue
                    feeds.append({**run, 'age_s': age})
            elif path.rsplit('/', 1)[-1].startswith('typing_'):
                age = _age_s(doc.get('updated', ''))
                if (doc.get('chat_id') == self.chat and doc.get('user')
                        and doc['user'] != self.viewer and age is not None and age <= 12):
                    feeds.append({'agent': doc['user'], 'human': True, 'typing': True, 'age_s': age})
            if len(feeds) > 64:
                raise OverflowError('selected live feed budget')
        feeds = suppress_superseded_preparing(feeds)
        feeds.sort(key=lambda item: (not item.get('human'), item.get('agent', ''), item.get('run_id', '')))
        return feeds

    def _profiles(self, round_, snapshot, receipt, data):
        names = tuple(dict.fromkeys((self.viewer, *sorted(snapshot.members))))[:64]
        accounts = {name: effective_account(round_, name) for name in names}
        viewer = accounts.get(self.viewer)
        if viewer is None:
            return None
        owns = any(a and a.kind is UserKind.AGENT and a.agent and a.agent.owner == self.viewer
                   for a in accounts.values()) or None
        if owns is not True and viewer.kind is not UserKind.AGENT:
            # Inventory rows select names only. Current ownership is resolved
            # again from this request's captured account/lifecycle inputs.
            from .sidebar_users import public_users, UsersUnavailable
            auxiliary = self.mesh.local_inputs.auxiliary
            auxiliary.request('users')
            try:
                _reader, _observed, candidates = auxiliary.inputs('users',
                    max_documents=2048, max_bytes=min(2 * 1024 * 1024, round_.ledger.remaining()))
                round_.ledger.charge(candidates.captured_bytes)
                names_to_check = [name for name, item in public_users(candidates).items()
                                  if item.get('kind') == 'agent' and name not in accounts]
                for name in names_to_check[:max(0, 64 - len(round_.accounts) - 2)]:
                    candidate = effective_account(round_, name)
                    if candidate and candidate.agent and candidate.agent.owner == self.viewer:
                        owns = True
                        break
            except _PENDING_INPUTS + (UsersUnavailable,):
                pass  # An incomplete inventory never proves no ownership.
        status = data['metadata_status']
        display = None
        runtime = self.mesh.local_inputs.presence
        runtime.request()
        try:
            observed = runtime.reader.capture()
            captured = runtime.reader.capture_display_members(observed, names)
            display = page_fence.PresenceDisplayFence(observed, captured)
        except (local_source.SourceChanged, presence_index.PresenceIndexUnavailable, OSError, sqlite3.Error):
            captured = None
        profile_pending = len(snapshot.members) > len(names)
        presence_pending = captured is None
        for name, account in accounts.items():
            if account is None:
                continue
            projected = profile_presentation.project_profile(account, self.viewer,
                viewer_kind=viewer.kind, same_selected_room=name in snapshot.members,
                viewer_owns_agent=owns)
            profile_pending |= bool(projected.pending_fields)
            presence = None
            if captured is not None:
                visible = profile_presentation.project_presence(captured, name, projected,
                    now_ns=round_.now, stale_s=self.mesh.presence.stale_s)
                presence, presence_pending = visible.values, presence_pending or visible.pending
                if not visible.pending:
                    deadline = captured.observed_ns + 30_000_000_000
                    row = next(row for row in captured.subjects if row[0] == name)
                    if presence.get('online'):
                        deadline = min(deadline, row[3] + int(self.mesh.presence.stale_s * 1_000_000_000) + 1)
                    round_.deadline = deadline if round_.deadline is None else min(round_.deadline, deadline)
            data['users'][name] = user_json(account, projected.profile, presence, me=self.viewer)
        status['profiles'] = 'pending' if profile_pending else 'ready'
        status['presence'] = 'pending' if presence_pending else 'ready'
        return display


@authed_read_token
def chat_aux(app, req, mesh, token):
    chat = _part(req.params.get('id', ''))
    runtime = mesh.local_inputs
    if runtime is None:
        return _pending(token, 'local_paging_disabled', status='unavailable')
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    operation = None
    for _ in range(4):
        try:
            reader, receipt, index = runtime.inputs(chat)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable, OSError, sqlite3.Error):
            return _pending(token, 'local_inputs_pending')
        if operation is None:
            operation = AuxiliaryPageOperation(app, mesh, chat, source_reader=reader)
        work = operation.prepare(receipt, receipt, index)
        if work.status == 'restart':
            continue
        if work.status == 'work':
            if work.reason == 'overlay_proofs':
                runtime.request_page(chat, index=index, proofs=work.work)
            return _pending(token, work.reason)
        if work.status == 'forbidden':
            return _pending(token, 'viewer_not_member', status='forbidden')
        if work.status != 'prepared':
            return _pending(token, work.reason or 'auxiliary_pending')
        final = app.finalize_page_read(token, work.prepared)
        if final.status == 'restart':
            continue
        if final.status != 'page' or final.result is None:
            return _pending(token, final.reason or 'auxiliary_changed', status=final.status)
        payload = json.loads(final.result.presentation.decoration_json)
        return {'status': 'ready', 'chat_id': chat, 'session_binding': session_read_binding(token),
                'page_version': app.page_cursors.version(token, chat, final.result.page,
                    local_trust_version=final.result.local_trust_version), **payload}
    return _pending(token, 'auxiliary_progress')


GET = {'/api/mesh/chat_aux': chat_aux}
POST = {}
