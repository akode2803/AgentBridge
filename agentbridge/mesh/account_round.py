"""Bounded request-only account authority for chatless owner controls.

Shares the canonical lifecycle evaluator with membership reads. Inputs are an
admitted users+lifecycle raw source, exact retained heads and local trust pins;
no room membership, provider read or reusable authority result is invented.
"""
from __future__ import annotations

import time

from ..store import document_observation as docs, lifecycle_inputs, local_source
from .membership_coordinator import _Round, _Stop
from .lifecycle_evaluation import SubjectEvidence
from .pins import evaluate_observed_view
from .paths import P


class AccountRound:
    raw = _Round.raw
    state = _Round.state
    get = _Round.get
    kind = _Round.kind
    owner_of = _Round.owner_of
    sign_pub = _Round.sign_pub
    clock = _Round.clock

    def __init__(self, mesh, reader, receipt, ledger):
        self.mesh, self.reader, self.receipt, self.ledger = mesh, reader, receipt, ledger
        if (reader.scope != 'identities' or reader.store is not mesh.store
                or reader.coordinator is not mesh.tx._coordinator):
            raise ValueError('invalid account source')
        reader._receipt(receipt)
        self.viewer, self.machine = mesh.messaging.user, mesh.messaging.machine
        self.now = time.time_ns()
        if type(self.now) is not int or not 0 <= self.now <= 2**63 - 1:
            raise _Stop('unavailable', 'invalid_clock')
        self.deadline = self.now + 1_000_000_000
        self.accounts, self.facts, self.published = {}, {}, {}
        self.subjects, self.heads, self.evidence, self.states = {}, {}, {}, {}
        self.documents = []

    def _account_record(self, name):
        with self.reader._read(self.receipt) as (conn, receipt):
            captured = docs._capture_selected(conn, self.mesh.store.path, receipt.source.raw,
                (P.user(name),), max_documents=1, max_bytes=self.ledger.remaining())
        self.ledger.charge(sum(len(r.path.encode()) + len((r.payload_json or '').encode())
                               for r in captured.records))
        self.documents.append(captured)
        return captured.records[0]

    def subject(self, name):
        if name in self.evidence:
            return
        if len(self.subjects) >= self.ledger.limits.max_subjects:
            raise _Stop('unavailable', 'subject_budget_exhausted')
        self.ledger.step()
        with self.reader._read(self.receipt) as (conn, receipt):
            selected = lifecycle_inputs.capture_subject(conn, self.mesh.store.path,
                receipt.source.raw, name, max_records=self.ledger.limits.max_subject_records,
                max_bytes=self.ledger.remaining())
            self.ledger.charge(selected.serialized_bytes)
            heads = lifecycle_inputs.capture_heads(conn, self.mesh.store.path, (name,),
                                                   max_bytes=self.ledger.remaining())
        self.ledger.charge(heads.serialized_bytes)
        self.subjects[name], self.heads[name] = selected, heads
        self.evidence[name] = SubjectEvidence(True,
            tuple((r.path, r.payload_json) for r in selected.records if not r.deleted),
            heads.entries[0].payload_json)

    def _matches(self, conn, companions, changed_head=None):
        if not self.reader.matches_in_transaction(conn, self.receipt):
            raise local_source.SourceChanged('account_source_changed')
        for reader, receipt in companions:
            if not reader.matches_in_transaction(conn, receipt):
                raise local_source.SourceChanged('account_companion_changed')
        for selected in self.documents:
            size = sum(len(r.path.encode()) + len((r.payload_json or '').encode()) for r in selected.records)
            if docs._capture_selected(conn, self.mesh.store.path, selected.position,
                    tuple(r.path for r in selected.records), max_documents=1,
                    max_bytes=max(1, size)) != selected:
                raise local_source.SourceChanged('account_document_changed')
        for name, selected in self.subjects.items():
            if not lifecycle_inputs.matches_subject(conn, self.mesh.store.path, selected):
                raise local_source.SourceChanged('account_lifecycle_changed')
            if name != changed_head and not lifecycle_inputs.matches_heads(conn, self.mesh.store.path, self.heads[name]):
                raise local_source.SourceChanged('account_head_changed')

    def finalize(self, companions=(), proposal=None):
        """Caller holds screen/session gate. No computation under the final cut."""
        view = self.mesh.key_pins.capture_effective_view()
        size = len(view.effective_json.encode()) + len(view.durable_json.encode())
        for name, (sign, agree, history, used_sign, used_agree) in self.published.items():
            self.ledger.step()
            self.ledger.charge(size)
            replay = evaluate_observed_view(view, name, sign, agree, history)
            if not replay.satisfied or (replay.sign_pub, replay.agree_pub) != (used_sign, used_agree):
                raise _Stop('unavailable', 'pin_inputs_changed')
        compare = sum(sum(len(r.path.encode()) + len((r.payload_json or '').encode())
                          for r in selected.records) for selected in self.documents)
        compare += sum(v.serialized_bytes for v in (*self.subjects.values(), *self.heads.values()))
        self.ledger.charge(compare * (2 if proposal is not None else 1))
        if proposal is not None:
            if proposal.subject not in self.heads:
                raise ValueError('unobserved account proposal')
            wanted = self.heads[proposal.subject].entries[0]
            if wanted.payload_json != proposal.expected_retained_json:
                raise local_source.SourceChanged('account_proposal_changed')
            self.ledger.charge(len(proposal.proposed_json.encode()))
        definitions = tuple(reader.definition for reader, _receipt in companions)
        with self.mesh.key_pins.locked_matching_view(view) as matches:
            if not matches:
                raise _Stop('unavailable', 'pin_inputs_changed')
            with self.reader.coordinator.finalization_cut(self.mesh.store, self.reader.definition,
                                                          companions=definitions) as (conn, _):
                first = self.clock(self.now)
                if (self.mesh.messaging.user, self.mesh.messaging.machine) != (self.viewer, self.machine):
                    raise _Stop('unavailable', 'identity_changed')
                self._matches(conn, companions)
                if proposal is not None:
                    if not lifecycle_inputs.publish_head_in_transaction(conn, self.mesh.store.path,
                            wanted, proposal.proposed_json, first):
                        raise local_source.SourceChanged('account_head_changed')
                    self._matches(conn, companions, changed_head=proposal.subject)
                    head_size = self.heads[proposal.subject].serialized_bytes + len(proposal.proposed_json.encode())
                    current = lifecycle_inputs.capture_heads(conn, self.mesh.store.path,
                        (proposal.subject,), max_bytes=head_size).entries[0]
                    if (current.database_path != wanted.database_path or current.incarnation != wanted.incarnation
                            or current.generation != wanted.generation + 1
                            or current.payload_json != proposal.proposed_json):
                        raise local_source.SourceChanged('account_proposal_postcheck')
                self.clock(first)
