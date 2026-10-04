"""Bounded unread aggregates and raw dependency fingerprints, never authority.

No documents, messages, keys, epoch observations or evaluated permission results
survive a quantum. Every reuse re-evaluates the dependency closure and finishes
through the ordinary canonical page fences.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ..store.page_inputs import MessageKey, PageInputPosition
from . import membership_coordinator as membership
from .readmodel import unread_info

MAX_JOBS = 128
VISIBLE_LIMIT = 64
RAW_LIMIT = 256
MAX_DEPENDENCIES = 64
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_COUNT = 2**53 - 1


@dataclass(frozen=True)
class UnreadSession:
    app_identity: str
    generation: int
    viewer: str

    def __post_init__(self):
        if (type(self.app_identity) is not str or not 1 <= len(self.app_identity) <= 128
                or '\x00' in self.app_identity
                or type(self.generation) is not int or not 0 <= self.generation < 2**63
                or type(self.viewer) is not str or not 1 <= len(self.viewer) <= 256
                or any(c in self.viewer for c in ('/', '\\', '\x00'))):
            raise ValueError('invalid unread session')


@dataclass(frozen=True)
class UnreadEvidence:
    position: PageInputPosition
    owner: tuple[str, str, str, str]
    accounts: tuple[str, ...]
    heads: tuple[tuple[str, str], ...]
    epochs: tuple[tuple[int, str], ...]
    trust_version: str
    observed_ns: int
    deadline_ns: int | None
    validated_ns: int


@dataclass(frozen=True)
class UnreadCandidate:
    session: UnreadSession
    chat_id: str
    count: int
    first_unread_ns: int
    mention: bool
    evidence: UnreadEvidence
    complete: bool
    before: MessageKey | None

    def summary(self):
        if not self.complete:
            raise ValueError('incomplete unread count')
        return dict(unread=self.count, unread_lower_bound=self.count,
                    unread_complete=True, first_unread_ns=self.first_unread_ns,
                    mention=self.mention)


def _digest(value, ledger):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()
    ledger.charge(len(encoded))
    return hashlib.sha256(encoded).hexdigest()


def _bytes_digest(value):
    return None if value is None else hashlib.sha256(value).hexdigest()


def _epoch_digest(view, ledger):
    # Do not retain the secret-bearing view or its serialized raw documents.
    wrap = view.wrap
    raw = None if wrap is None else wrap.record
    if raw is not None:
        ledger.charge(len((raw.payload_json or '').encode()))
    identity = view.identity
    return _digest([view.owner, view.chat_id, view.epoch, view.viewer,
        _bytes_digest(view.resident),
        None if identity is None else [identity.path, _bytes_digest(identity.raw)],
        None if wrap is None else [wrap.mode, wrap.shape, raw.path, raw.deleted,
            _bytes_digest(None if raw.payload_json is None else raw.payload_json.encode())]], ledger)


def _owner(receipt):
    # Collection claims can advance revision without changing admitted inputs.
    # Never retain that revision as authority or require a quiet transport poll.
    return (receipt.coordinator_path, receipt.coordinator_epoch,
            receipt.source.epoch, receipt.source.source_id)


def _bounded(evidence):
    if (type(evidence) is not UnreadEvidence
            or type(evidence.accounts) is not tuple
            or type(evidence.heads) is not tuple
            or type(evidence.epochs) is not tuple
            or len(evidence.accounts) > MAX_DEPENDENCIES
            or len(evidence.heads) > MAX_DEPENDENCIES
            or len(evidence.epochs) > MAX_DEPENDENCIES
            or len(json.dumps(asdict(evidence), ensure_ascii=False).encode()) > MAX_EVIDENCE_BYTES):
        raise membership._Stop('unavailable', 'unread_dependency_budget')
    if (type(evidence.trust_version) is not str or len(evidence.trust_version) != 64
            or any(c not in '0123456789abcdef' for c in evidence.trust_version)
            or type(evidence.observed_ns) is not int
            or type(evidence.validated_ns) is not int
            or not 0 <= evidence.observed_ns <= evidence.validated_ns <= 2**63 - 1
            or evidence.deadline_ns is not None and (
                type(evidence.deadline_ns) is not int
                or not evidence.validated_ns < evidence.deadline_ns <= 2**63 - 1)):
        raise membership._Stop('unavailable', 'unread_invalid_evidence')


def validate_inputs(round_, index, sealer, candidate):
    """Expand THIS request's raw dependency closure before canonical finalization."""
    if type(candidate) is not UnreadCandidate:
        raise ValueError('invalid unread candidate')
    if (type(candidate.session) is not UnreadSession
            or type(candidate.count) is not int or not 0 <= candidate.count <= MAX_COUNT
            or type(candidate.first_unread_ns) is not int
            or not 0 <= candidate.first_unread_ns <= 2**63 - 1
            or type(candidate.mention) is not bool or type(candidate.complete) is not bool):
        raise ValueError('invalid unread aggregate')
    evidence = candidate.evidence
    _bounded(evidence)
    if (candidate.chat_id != round_.chat or candidate.session.viewer != round_.viewer
            or round_.source_reader is None
            or _owner(round_.receipt) != evidence.owner
            or round_.suffix.position != evidence.position.messages
            or index != evidence.position.overlays
            or not evidence.observed_ns <= evidence.validated_ns <= round_.now
            or evidence.deadline_ns is not None and round_.now >= evidence.deadline_ns):
        raise membership._Stop('restart', 'unread_inputs_changed')
    for name in evidence.accounts:
        round_.get(name)  # Fresh keys, trust and lifecycle, including owner chains.
    for name, expected in evidence.heads:
        round_.state(name)
        if _digest(asdict(round_.heads[name]), round_.ledger) != expected:
            raise membership._Stop('restart', 'unread_inputs_changed')
    for epoch, expected in evidence.epochs:
        sealer.key(round_.chat, epoch)  # New request-local observation + final epoch fence.
        if _epoch_digest(sealer.epochs[epoch], round_.ledger) != expected:
            raise membership._Stop('restart', 'unread_inputs_changed')
    if evidence.deadline_ns is not None:
        round_.deadline = (evidence.deadline_ns if round_.deadline is None
                           else min(evidence.deadline_ns, round_.deadline))


def accumulate(prepared, final, session, previous=None):
    """Called only after a successful fresh page finalization; retain scalars only."""
    round_ = prepared._round
    page, presentation = final.page, final.presentation
    if page is None or presentation is None or round_.source_reader is None:
        raise ValueError('unread accumulation requires a local canonical page')
    accounts = tuple(sorted(round_.accounts))
    heads = tuple((name, _digest(asdict(head), round_.ledger))
                  for name, head in sorted(round_.heads.items()))
    epochs = tuple((view.epoch, _epoch_digest(view, round_.ledger))
                   for view in sorted(prepared._fence.epochs, key=lambda view: view.epoch))
    observed_ns, deadline = round_.now, round_.deadline
    validated_ns = final.validated_now_ns
    if type(validated_ns) is not int or validated_ns < observed_ns:
        raise membership._Stop('unavailable', 'unread_clock_unvalidated')
    if previous is not None:
        old = previous.evidence
        # validate_inputs must have recaptured the entire earlier closure, even
        # dependencies used by invisible rows. Never union stale observations
        # into evidence without passing the new round's final gates.
        if (not set(old.accounts).issubset(accounts)
                or not set(old.heads).issubset(heads)
                or not set(old.epochs).issubset(epochs)
                or prepared._operation.unread_candidate is not previous
                or previous.session != session
                or old.validated_ns > observed_ns):
            raise membership._Stop('restart', 'unread_closure_changed')
        observed_ns = min(observed_ns, old.observed_ns)
        if old.deadline_ns is not None:
            deadline = old.deadline_ns if deadline is None else min(deadline, old.deadline_ns)
    evidence = UnreadEvidence(page.position, _owner(round_.receipt), accounts, heads, epochs,
        final.local_trust_version, observed_ns, deadline, validated_ns)
    _bounded(evidence)
    state = json.loads(presentation.viewer_state_json)
    observed = unread_info(list(page.messages), session.viewer, state)
    count = observed['unread'] + (previous.count if previous else 0)
    if count > MAX_COUNT:
        raise membership._Stop('unavailable', 'unread_count_budget')
    first = min((n for n in (observed['first_unread_ns'],
                             previous.first_unread_ns if previous else 0) if n), default=0)
    if not page.history_exhausted and (page.oldest_examined is None or page.raw_examined < 1):
        raise membership._Stop('unavailable', 'unread_no_progress')
    candidate = UnreadCandidate(session, round_.chat, count, first,
        observed['mention'] or bool(previous and previous.mention), evidence,
        page.history_exhausted, page.oldest_examined)
    if len(json.dumps(asdict(candidate), ensure_ascii=False).encode()) > MAX_EVIDENCE_BYTES:
        raise membership._Stop('unavailable', 'unread_dependency_budget')
    return candidate
