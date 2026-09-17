"""Inactive single authority work round; never a viewer/page admission cache."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from ..core.models import Account, ChatSnapshot, UserKind
from ..store import document_observation, lifecycle_inputs, local_source, membership_suffix, terminal_observation
from ..store.membership_input_position import MembershipInputUnavailable
from ..transport import authority_observation
from . import authority_source, events, local_page_source, page_fence
from .lifecycle import LifecycleUnavailable
from .pin_storage import PinStoreUnavailable
from .lifecycle_evaluation import (
    AccountAuthorityFact, CapturedLifecycleInputs, LifecycleInputsIncomplete,
    LifecycleLimits, SubjectEvidence, evaluate_lifecycle_work,
)
from .paths import P
from .pins import EffectivePinView, evaluate_observed_view


@dataclass(frozen=True)
class CoordinatorLimits:
    max_steps: int = 64
    max_accounts: int = 64
    max_subjects: int = 64
    max_bytes: int = 16 * 1024 * 1024
    max_suffix_events: int = 128
    max_subject_records: int = 256


@dataclass(frozen=True)
class MembershipCandidate:
    chat_id: str
    viewer: str
    machine: str
    snapshot_json: str
    receipt: authority_source.AuthoritySourceReceipt | local_page_source.LocalSourceReceipt
    authority_inputs: tuple[authority_source.AuthorityInputs | local_page_source.LocalAuthorityInputs, ...]
    policy: authority_observation.LookupPolicy | None
    suffix: membership_suffix.MembershipSuffix
    terminal: terminal_observation.TerminalObservation
    subjects: tuple[lifecycle_inputs.SubjectSelection, ...]
    heads: tuple[lifecycle_inputs.HeadSelection, ...]
    pin_view: EffectivePinView
    captured_now_ns: int
    validated_now_ns: int
    next_recheck_ns: int | None


@dataclass(frozen=True)
class CoordinatorResult:
    status: str
    candidate: MembershipCandidate | None = None
    work_path: str | None = None
    reason: str | None = None
    page: page_fence.PageSelection | None = None


class _Stop(RuntimeError):
    def __init__(self, status, reason, path=None):
        self.status, self.reason, self.path = status, reason, path


class _Proposal(RuntimeError):
    def __init__(self, value):
        self.value = value


class _Ledger:
    def __init__(self, limits):
        if type(limits) is not CoordinatorLimits:
            raise TypeError('expected CoordinatorLimits')
        self.limits = CoordinatorLimits(**{name: getattr(limits, name) for name in limits.__dataclass_fields__})
        defaults = CoordinatorLimits()
        for name in defaults.__dataclass_fields__:
            value = getattr(self.limits, name)
            if type(value) is not int or not 0 <= value <= getattr(defaults, name):
                raise ValueError('invalid coordinator limit')
        self.bytes = self.steps = 0

    def charge(self, size):
        if size > self.limits.max_bytes - self.bytes:
            raise _Stop('unavailable', 'budget_exhausted')
        self.bytes += size

    def step(self):
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise _Stop('unavailable', 'step_budget_exhausted')

    def remaining(self):
        return min(4 * 1024 * 1024, self.limits.max_bytes - self.bytes)


def _raw_size(captured):
    return sum(len(r.path.encode()) + (len(r.payload_json.encode()) if r.payload_json is not None else 0)
               for r in captured.documents.records)


class _Round:
    def __init__(self, mesh, receipt, ledger, *, source_reader=None):
        self.mesh, self.ledger = mesh, ledger
        self.source_reader = source_reader
        self.transport_owner = mesh.tx
        if source_reader is None:
            self.receipt = authority_source._copy_receipt(mesh.tx, mesh.store, receipt)
        else:
            from ..transport.local_mutations import LocalMutationTransport, root_identity
            if type(source_reader) is not local_page_source.LocalPageSource or source_reader.store is not mesh.store:
                raise ValueError('invalid local source owner')
            transport = mesh.tx
            if type(transport) is LocalMutationTransport:
                if transport._coordinator is not source_reader.coordinator:
                    raise ValueError('local mutation owner mismatch')
                transport = transport._transport
            if root_identity(transport) != source_reader.coordinator.identity:
                raise ValueError('local source transport mismatch')
            self.receipt = source_reader._receipt(receipt)
        self.chat = self.receipt.chat_id
        self.viewer, self.machine = mesh.messaging.user, mesh.messaging.machine
        authority_observation._part(self.viewer)
        authority_observation._part(self.machine)
        self.now = time.time_ns()
        if type(self.now) is not int or not 0 <= self.now <= 2**63 - 1:
            raise _Stop('unavailable', 'invalid_clock')
        self.deadline = None
        self.batches, self.accounts, self.published = [], {}, {}
        self.subjects, self.heads, self.states = {}, {}, {}
        self.facts, self.evidence = {}, {}
        self.meta_input = self.capture(())
        record = self.meta_input.documents.records[0]
        meta = None if record.deleted else json.loads(record.payload_json)
        if type(meta) is not dict:
            raise _Stop('unavailable', 'missing_meta')
        boundary = meta.get('materialized_ns', 0)
        if type(boundary) is not int or not 0 <= boundary <= 2**63 - 1:
            raise _Stop('unavailable', 'invalid_meta_boundary')
        self.snapshot = ChatSnapshot.from_dict(meta)
        self.suffix = membership_suffix.capture(mesh.store.path, self.chat, boundary,
            max_events=ledger.limits.max_suffix_events, max_bytes=ledger.remaining())
        ledger.charge(self.suffix.captured_bytes)
        target = f'{self.chat}|{P.log_name(self.viewer, self.machine)}'
        self.terminal = terminal_observation.capture(mesh.store.path, target,
            wrapped=mesh.messaging.attachments is not None)
        if self.terminal.pending:
            raise _Stop('unavailable', 'pending_terminal')

    def capture(self, names):
        self.ledger.step()
        if self.source_reader is None:
            value = authority_source.capture_authority_inputs(self.mesh.tx, self.mesh.store,
                self.receipt, names, max_bytes=self.ledger.remaining())
        else:
            value = self.source_reader.capture_authority(self.receipt, names,
                                                        max_bytes=self.ledger.remaining())
        self.ledger.charge(_raw_size(value))
        self.batches.append(value)
        return value

    def raw(self, name):
        if name in self.accounts:
            return self.accounts[name]
        self.ledger.step()
        if len(self.accounts) >= self.ledger.limits.max_accounts:
            raise _Stop('unavailable', 'account_budget_exhausted')
        value = self.capture((name,)).documents.records[1]
        doc = None if value.deleted else json.loads(value.payload_json)
        if type(doc) is not dict:
            self.accounts[name], self.facts[name] = None, None
            return None
        account = Account.from_dict(doc)
        keys = doc.get('keys') or {}
        history = keys.get('history') if type(keys) is dict else None
        sign, agree = account.keys.sign_pub, account.keys.agree_pub
        # Canonical owner calls are independently bounded by max_steps and the
        # pin owner's file/history ceilings. They never run in a SQL transaction.
        resolution = self.mesh.key_pins.resolve_observed(name, sign, agree, history)
        if resolution.changed:
            raise _Stop('restart', 'pin_progress')
        account.keys.sign_pub, account.keys.agree_pub = resolution.sign_pub, resolution.agree_pub
        self.published[name] = (sign, agree, history, resolution.sign_pub, resolution.agree_pub)
        self.accounts[name] = account
        self.facts[name] = AccountAuthorityFact(account.kind.value, account.keys.sign_pub, bool(account.active))
        return account

    def subject(self, name):
        if name in self.evidence:
            return
        if len(self.subjects) >= self.ledger.limits.max_subjects:
            raise _Stop('unavailable', 'subject_budget_exhausted')
        self.ledger.step()
        if self.source_reader is None:
            selected = authority_source.capture_authority_subject(self.mesh.tx, self.mesh.store,
                self.receipt, name, max_records=self.ledger.limits.max_subject_records,
                max_bytes=self.ledger.remaining())
        else:
            selected = self.source_reader.capture_subject(self.receipt, name,
                max_records=self.ledger.limits.max_subject_records, max_bytes=self.ledger.remaining())
        self.ledger.charge(selected.serialized_bytes)
        conn = document_observation._open_reader(self.mesh.store.path)
        try:
            conn.execute('BEGIN')
            heads = lifecycle_inputs.capture_heads(conn, self.mesh.store.path, (name,),
                                                   max_bytes=self.ledger.remaining())
        finally:
            conn.close()
        self.ledger.charge(heads.serialized_bytes)
        self.subjects[name], self.heads[name] = selected, heads
        self.evidence[name] = SubjectEvidence(True,
            tuple((r.path, r.payload_json) for r in selected.records if not r.deleted),
            heads.entries[0].payload_json)

    def state(self, name):
        if name in self.states:
            return self.states[name]
        self.subject(name)
        while True:
            self.ledger.step()
            # Repeated pure evaluations consume work/byte budget as well as
            # captures; requesting a missing dependency never resets the ledger.
            size = sum(len(n.encode()) + len(f.trusted_sign_pub.encode()) if f else len(n.encode())
                       for n, f in self.facts.items())
            size += sum(len(n.encode()) + sum(len(p.encode()) + len(r.encode()) for p, r in e.envelopes)
                        + (len(e.retained_json.encode()) if e.retained_json else 0)
                        for n, e in self.evidence.items())
            self.ledger.charge(size)
            try:
                work = evaluate_lifecycle_work(CapturedLifecycleInputs(self.now,
                    tuple(self.facts.items()), tuple(self.evidence.items())), name,
                    limits=LifecycleLimits(max_accounts=self.ledger.limits.max_accounts,
                        max_subjects=self.ledger.limits.max_subjects,
                        max_envelopes=min(10_000, self.ledger.limits.max_subjects * self.ledger.limits.max_subject_records),
                        max_bytes=self.ledger.limits.max_bytes))
            except LifecycleInputsIncomplete as exc:
                if exc.dependency_kind == 'account' and exc.dependency_name is not None:
                    self.raw(exc.dependency_name)
                elif exc.dependency_kind == 'subject' and exc.dependency_name is not None:
                    self.subject(exc.dependency_name)
                else:
                    raise _Stop('unavailable', 'lifecycle_incomplete') from exc
                continue
            deadline = work.result.next_recheck_ns
            if deadline is not None:
                self.deadline = deadline if self.deadline is None else min(self.deadline, deadline)
            if not work.complete:
                raise _Proposal(work.result.next_proposal)
            self.states[name] = None if work.result.effective_json is None else json.loads(work.result.effective_json)
            return self.states[name]

    def get(self, name):
        account = self.raw(name)
        if account is not None:
            self.state(name)
        return account

    def kind(self, name):
        account = self.get(name)
        return account.kind if account else None

    def owner_of(self, name):
        account = self.get(name)
        if account and account.kind is UserKind.AGENT and account.agent:
            state = self.states[name]
            return (state['owner'] if state is not None else account.agent.owner) or None
        return None

    def sign_pub(self, name):
        account = self.get(name)
        return (account.keys.sign_pub or None) if account else None

    def clock(self, previous):
        value = time.time_ns()
        if type(value) is not int or not self.now <= previous <= value <= 2**63 - 1:
            raise _Stop('unavailable', 'clock_rollback')
        if self.deadline is not None and value >= self.deadline:
            raise _Stop('unavailable', 'clock_expired')
        return value

    @contextmanager
    def _final_store(self):
        if self.source_reader is not None:
            if self.mesh.tx is not self.transport_owner or self.mesh.store is not self.source_reader.store:
                raise _Stop('unavailable', 'local_source_owner_changed')
            with self.source_reader.finalization(self.receipt) as conn:
                yield conn
            return
        conn = sqlite3.connect(self.mesh.store.path, timeout=1.0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
        finally:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()

    def _matches_batch(self, conn, batch):
        if self.source_reader is None:
            return authority_source.matches_inputs_in_transaction(
                conn, self.mesh.store, batch, max_bytes=_raw_size(batch))
        captured = batch.documents
        return (self.source_reader.matches_in_transaction(conn, self.receipt)
                and document_observation._capture_selected(conn, self.mesh.store.path,
                    self.receipt.source.raw, tuple(r.path for r in captured.records),
                    max_documents=129, max_bytes=_raw_size(batch)) == captured)

    def _policy_scope(self, policy):
        return (nullcontext(True) if self.source_reader is not None else
                authority_observation._locked_matching_lookup_policy(self.mesh.tx, policy))

    def final(self, snapshot, proposal, *, page=None):
        if self.source_reader is not None and page is not None:
            raise _Stop('unavailable', 'local_page_fence_pending')
        prepared_page = None if page is None else page_fence.prepare(
            self.mesh, page, self.suffix.position, self.receipt.mirror)
        if prepared_page is not None:
            self.ledger.charge(prepared_page.comparison_bytes * (2 if proposal is not None else 1))
        view = self.mesh.key_pins.capture_effective_view()
        view_size = len(view.effective_json.encode()) + len(view.durable_json.encode())
        self.ledger.charge(view_size)
        for name, (sign, agree, history, used_sign, used_agree) in self.published.items():
            self.ledger.step()
            self.ledger.charge(view_size)
            replay = evaluate_observed_view(view, name, sign, agree, history)
            if not replay.satisfied or (replay.sign_pub, replay.agree_pub) != (used_sign, used_agree):
                raise _Stop('unavailable', 'pin_inputs_changed')
        policy = None
        if self.source_reader is None:
            policy = authority_observation.LookupPolicy(self.chat, self.receipt.mirror,
                tuple(entry for batch in self.batches for entry in batch.policy.accounts),
                self.meta_input.policy.meta)
            policy = authority_observation._copy_lookup_policy(policy)
        # Own SQL writes can fire triggers that mutate other authority inputs.
        # Budget both prewrite and postwrite checks, including the new head bytes.
        comparison_size = (sum(_raw_size(v) for v in self.batches)
            + sum(v.serialized_bytes for v in self.subjects.values())
            + sum(v.serialized_bytes for v in self.heads.values()))
        self.ledger.charge(comparison_size * (2 if proposal is not None else 1))
        if proposal is not None:
            self.ledger.charge(len(proposal.proposed_json.encode()))
        # These are locally owned DTOs from this round's owner captures. Bind
        # namespace/policy with the owner before locking; the postwrite read
        # compares their exact raw rows without recomputing a JSON source hash.
        raw_checks = tuple((batch.documents,
                            tuple(r.path for r in batch.documents.records), _raw_size(batch))
                           for batch in self.batches)
        serialized = None
        if proposal is None:
            if prepared_page is None:
                serialized = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(',', ':'))
                self.ledger.charge(len(serialized.encode()))
        elif proposal.subject not in self.heads:
            raise _Stop('unavailable', 'invalid_proposal')
        local = nullcontext() if prepared_page is None else page_fence.local_scope(self.mesh, prepared_page)
        with local:
            with self.mesh.key_pins.locked_matching_view(view) as pins_match:
                if not pins_match:
                    raise _Stop('unavailable', 'pin_inputs_changed')
                with self._final_store() as conn:
                    first = self.clock(self.now)
                    for batch in self.batches:
                        if not self._matches_batch(conn, batch):
                            raise _Stop('unavailable', 'authority_inputs_changed')
                    if not membership_suffix.matches_position(conn, self.mesh.store.path, self.suffix.position):
                        raise _Stop('unavailable', 'membership_inputs_changed')
                    for name in self.subjects:
                        if (not lifecycle_inputs.matches_subject(conn, self.mesh.store.path, self.subjects[name])
                                or not lifecycle_inputs.matches_heads(conn, self.mesh.store.path, self.heads[name])):
                            raise _Stop('unavailable', 'lifecycle_inputs_changed')
                    if not terminal_observation.matches(conn, self.mesh.store.path, self.terminal.position):
                        raise _Stop('unavailable', 'terminal_inputs_changed')
                    if prepared_page is not None and not page_fence.matches_store(conn, self.mesh, prepared_page):
                        raise _Stop('unavailable', 'page_inputs_changed')
                    if (self.mesh.messaging.user, self.mesh.messaging.machine) != (self.viewer, self.machine):
                        raise _Stop('unavailable', 'identity_changed')
                    if proposal is not None:
                        # No JSON, crypto, provider or caller callbacks below. All
                        # mirror writers are excluded until the SQL commit finishes.
                        with self._policy_scope(policy) as matched:
                            if not matched:
                                raise _Stop('unavailable', 'lookup_policy_changed')
                            if prepared_page is not None and not page_fence.matches_mirror_locked(self.mesh, prepared_page):
                                raise _Stop('unavailable', 'page_mirror_changed')
                            wanted = self.heads[proposal.subject].entries[0]
                            if wanted.payload_json != proposal.expected_retained_json:
                                raise _Stop('unavailable', 'proposal_head_mismatch')
                            if not lifecycle_inputs.publish_head_in_transaction(conn, self.mesh.store.path,
                                    wanted, proposal.proposed_json, first):
                                raise _Stop('unavailable', 'retained_head_changed')
                            for wanted_rows, paths, size in raw_checks:
                                current_rows = document_observation._capture_selected(conn,
                                    self.mesh.store.path, wanted_rows.position, paths,
                                    max_documents=129, max_bytes=size)
                                if current_rows != wanted_rows:
                                    raise _Stop('unavailable', 'authority_inputs_changed')
                            if not membership_suffix.matches_position(conn, self.mesh.store.path, self.suffix.position):
                                raise _Stop('unavailable', 'membership_inputs_changed')
                            if not terminal_observation.matches(conn, self.mesh.store.path, self.terminal.position):
                                raise _Stop('unavailable', 'terminal_inputs_changed')
                            for name in self.subjects:
                                if not lifecycle_inputs.matches_subject(conn, self.mesh.store.path, self.subjects[name]):
                                    raise _Stop('unavailable', 'lifecycle_inputs_changed')
                                if name != proposal.subject:
                                    matched_head = lifecycle_inputs.matches_heads(conn, self.mesh.store.path, self.heads[name])
                                else:
                                    head_size = (self.heads[name].serialized_bytes
                                        - (len(wanted.payload_json.encode()) if wanted.payload_json is not None else 0)
                                        + len(proposal.proposed_json.encode()))
                                    current = lifecycle_inputs.capture_heads(conn, self.mesh.store.path, (name,),
                                        max_bytes=head_size).entries[0]
                                    matched_head = (current.database_path == wanted.database_path
                                        and current.incarnation == wanted.incarnation
                                        and current.subject == wanted.subject
                                        and current.generation == wanted.generation + 1
                                        and current.payload_json == proposal.proposed_json)
                                if not matched_head:
                                    raise _Stop('unavailable', 'lifecycle_inputs_changed')
                            if prepared_page is not None and not page_fence.matches_store(conn, self.mesh, prepared_page):
                                raise _Stop('unavailable', 'page_inputs_changed')
                            self.clock(first)
                            if self.source_reader is None:
                                conn.commit()
                        return CoordinatorResult('restart', reason='retained_head_progress')
                    with self._policy_scope(policy) as matched:
                        if not matched:
                            raise _Stop('unavailable', 'lookup_policy_changed')
                        if prepared_page is not None and not page_fence.matches_mirror_locked(self.mesh, prepared_page):
                            raise _Stop('unavailable', 'page_mirror_changed')
                        last = self.clock(first)
                        if prepared_page is not None:
                            return CoordinatorResult('page', page=prepared_page.fence.selection)
                        candidate = MembershipCandidate(self.chat, self.viewer, self.machine, serialized,
                            self.receipt, tuple(self.batches), policy, self.suffix, self.terminal,
                            tuple(self.subjects.values()), tuple(self.heads.values()), view,
                            self.now, last, self.deadline)
                        return CoordinatorResult('candidate', candidate=candidate)


def run_membership_round(mesh, receipt, *, limits=CoordinatorLimits(), source_reader=None):
    """Perform at most one side effect; caller must recapture after any work.

    No retry, source rebuild, provider read-through or canonical full-fold fallback
    occurs here. A future outer operation must cap rounds/conflicts/readthroughs
    across calls. Candidates are internal raw dependency receipts, not authority
    that can be reused after this round or handed straight to the GUI.
    """
    ledger = _Ledger(limits)
    try:
        attempt = _Round(mesh, receipt, ledger, source_reader=source_reader)
        proposal = None
        try:
            snapshot = events.advance(attempt.snapshot,
                [json.loads(r.payload_json) for r in attempt.suffix.rows], attempt)
        except _Proposal as ready:
            proposal = ready.value
            snapshot = None
            if proposal is None:
                raise _Stop('unavailable', 'invalid_proposal')
        return attempt.final(snapshot, proposal)
    except authority_source.AuthorityReadThroughRequired as exc:
        return CoordinatorResult('readthrough', work_path=exc.paths[0], reason='account_readthrough_required')
    except _Stop as exc:
        return CoordinatorResult(exc.status, work_path=exc.path, reason=exc.reason)
    except (local_source.SourceChanged, authority_source.AuthoritySourceUnavailable,
            authority_observation.AuthorityObservationUnavailable,
            document_observation.DocumentObservationConflict,
            lifecycle_inputs.LifecycleInputsUnavailable,
            membership_suffix.MembershipSuffixUnavailable,
            terminal_observation.TerminalObservationUnavailable,
            MembershipInputUnavailable):
        return CoordinatorResult('unavailable', reason='inputs_unavailable')
    except PinStoreUnavailable:
        return CoordinatorResult('unavailable', reason='pins_unavailable')
    except LifecycleUnavailable:
        return CoordinatorResult('unavailable', reason='lifecycle_unavailable')
    except (sqlite3.Error, OSError):
        return CoordinatorResult('unavailable', reason='storage_unavailable')
    except MemoryError:
        return CoordinatorResult('unavailable', reason='resource_unavailable')
    except OverflowError:
        return CoordinatorResult('unavailable', reason='budget_exhausted')
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        return CoordinatorResult('unavailable', reason='invalid_inputs')
