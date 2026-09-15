"""R179 pure lifecycle evaluation over signed, captured observations."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.mesh import lifecycle
from agentbridge.mesh import lifecycle_evaluation as evaluation
from agentbridge.mesh.lifecycle import LifecycleUnavailable, publish_change, resolve_lifecycle
from agentbridge.mesh.lifecycle_evaluation import (
    AccountAuthorityFact,
    CapturedLifecycleInputs,
    LifecycleInputsIncomplete,
    LifecycleLimits,
    SubjectEvidence,
    evaluate_lifecycle,
)
from agentbridge.mesh.service import Mesh
from agentbridge.core.jsonkit import canonical_json_bytes
from agentbridge.transport.folder import FolderTransport


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@pytest.fixture
def mesh(tmp_path):
    instance = Mesh(FolderTransport(tmp_path / "mesh"), "aryan", "workstation",
                    home=tmp_path / "home")
    instance.accounts.create_human("aryan", "aryan-pass")
    instance.accounts.create_human("fable", "fable-pass")
    instance.accounts.create_agent("claude")
    yield instance
    instance.close()


def _inputs(mesh, *, now_ns=None, accounts=None, subjects=None, retained=None):
    now = now_ns if now_ns is not None else 2**63 - 1
    names = ("aryan", "fable", "claude") if accounts is None else accounts
    account_rows = []
    for name in names:
        raw = mesh.directory._raw_get(name)
        account_rows.append((name, None if raw is None else AccountAuthorityFact(
            raw.kind.value, raw.keys.sign_pub, raw.active,
        )))
    names = ("aryan", "fable", "claude") if subjects is None else subjects
    subject_rows = []
    for name in names:
        paths = mesh.tx.list_docs(f"lifecycle/{name}")
        envelope_rows = tuple((path, _json(mesh.tx.get_doc(path))) for path in paths)
        retained_json = None if retained is None else retained.get(name)
        subject_rows.append((name, SubjectEvidence(True, envelope_rows, retained_json)))
    return CapturedLifecycleInputs(now, tuple(account_rows), tuple(subject_rows))


def _effective(result):
    return None if result.effective_json is None else json.loads(result.effective_json)


def test_signed_live_pure_parity_through_all_action_kinds(mesh):
    """Use real signatures so the evaluator cannot pass by accepting raw records."""
    expected = []
    expected.append(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))
    mesh.accounts.set_machine_agents_active(False)
    expected.append(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))
    publish_change(mesh.directory, mesh.keystore, "claude", actor="aryan",
                   action="host", machine="other-machine")
    expected.append(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))
    publish_change(mesh.directory, mesh.keystore, "claude", actor="fable",
                   action="transfer", owner="fable", machine="fable-machine")
    expected.append(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))
    publish_change(mesh.directory, mesh.keystore, "claude", actor="fable",
                   action="deactivate", active=False, deactivated="retired")
    expected.append(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))

    result = evaluate_lifecycle(_inputs(mesh), "claude")
    assert _effective(result) == expected[-1]
    assert [row["action"] for row in expected] == [
        "bootstrap", "state", "host", "transfer", "deactivate",
    ]


def test_unknown_subject_differs_from_known_empty_and_observed_absent_account(mesh):
    inputs = _inputs(mesh, subjects=("aryan", "fable"))
    with pytest.raises(LifecycleInputsIncomplete):
        evaluate_lifecycle(inputs, "claude")

    empty = CapturedLifecycleInputs(
        10, (), (("nobody", SubjectEvidence(True, (), None)),)
    )
    assert evaluate_lifecycle(empty, "nobody").effective_json is None

    captured = _inputs(mesh)
    absent_accounts = tuple(
        (name, None if name == "aryan" else fact) for name, fact in captured.accounts
    )
    observed_absent = replace(captured, accounts=absent_accounts)
    result = evaluate_lifecycle(observed_absent, "claude")
    assert result.effective_json is None
    assert "aryan" in result.consumed_accounts

    unknown_account = replace(captured, accounts=tuple(
        row for row in captured.accounts if row[0] != "aryan"
    ))
    with pytest.raises(LifecycleInputsIncomplete):
        evaluate_lifecycle(unknown_account, "claude")

    unavailable_no_retained = replace(captured, subjects=tuple(
        (name, SubjectEvidence(False, (), None) if name == "claude" else evidence)
        for name, evidence in captured.subjects
    ))
    with pytest.raises(LifecycleInputsIncomplete):
        evaluate_lifecycle(unavailable_no_retained, "claude")


def test_invalid_remote_evidence_skips_but_malformed_retained_is_unavailable(mesh):
    captured = _inputs(mesh)
    name, evidence = next(row for row in captured.subjects if row[0] == "claude")
    bad = SubjectEvidence(True, evidence.envelopes + (("lifecycle/claude/bad.json", "{"),), None)
    inputs = replace(captured, subjects=tuple(
        (entry, bad if entry == name else value) for entry, value in captured.subjects
    ))
    assert _effective(evaluate_lifecycle(inputs, "claude")) == resolve_lifecycle(
        mesh.directory, "claude", store=mesh.store
    )

    unavailable = replace(inputs, subjects=tuple(
        (entry, SubjectEvidence(False, (), "{") if entry == "claude" else value)
        for entry, value in inputs.subjects
    ))
    with pytest.raises(LifecycleUnavailable):
        evaluate_lifecycle(unavailable, "claude")

    retained = _json(resolve_lifecycle(mesh.directory, "claude", store=mesh.store))
    rollback = replace(inputs, subjects=tuple(
        (entry, SubjectEvidence(False, (), retained) if entry == "claude" else value)
        for entry, value in inputs.subjects
    ))
    result = evaluate_lifecycle(rollback, "claude")
    assert _effective(result)["id"] == json.loads(retained)["id"]
    assert not result.proposals


def test_bounded_deep_remote_json_skips_but_retained_json_is_unavailable(mesh):
    captured = _inputs(mesh)
    deep = "[" * 2_000 + "]" * 2_000
    rows = tuple(
        (name, SubjectEvidence(True, evidence.envelopes + (("lifecycle/claude/deep.json", deep),), None)
         if name == "claude" else evidence)
        for name, evidence in captured.subjects
    )
    assert _effective(evaluate_lifecycle(replace(captured, subjects=rows), "claude"))["id"] == \
        resolve_lifecycle(mesh.directory, "claude", store=mesh.store)["id"]
    retained = replace(captured, subjects=tuple(
        (name, SubjectEvidence(False, (), deep) if name == "claude" else evidence)
        for name, evidence in captured.subjects
    ))
    with pytest.raises(LifecycleUnavailable):
        evaluate_lifecycle(retained, "claude")


@pytest.mark.parametrize("action", ["bootstrap", "transfer"])
def test_recursive_missing_owner_propagates_instead_of_being_skipped(mesh, action):
    if action == "transfer":
        publish_change(mesh.directory, mesh.keystore, "claude", actor="fable",
                       action="transfer", owner="fable", machine="fable-machine")
        missing = "fable"
    else:
        missing = "aryan"
    captured = _inputs(mesh, subjects=tuple(name for name in ("aryan", "fable", "claude")
                                              if name != missing))
    with pytest.raises(LifecycleInputsIncomplete):
        evaluate_lifecycle(captured, "claude")


def test_real_owner_edge_exhausts_depth_zero(mesh):
    with pytest.raises(LifecycleInputsIncomplete):
        evaluate_lifecycle(_inputs(mesh), "claude", limits=LifecycleLimits(max_depth=0))


def test_injected_clock_boundary_and_retained_clock_rollback(mesh):
    record = mesh.tx.get_doc(mesh.tx.list_docs("lifecycle/claude")[0])["record"]
    before_admission = _inputs(mesh, now_ns=record["ns"] - lifecycle._FUTURE_SKEW_NS - 1)
    assert evaluate_lifecycle(before_admission, "claude").effective_json is None
    at_boundary = _inputs(mesh, now_ns=record["ns"] - lifecycle._FUTURE_SKEW_NS)
    assert _effective(evaluate_lifecycle(at_boundary, "claude"))["id"] == record["id"]

    retained = {"claude": _json(record)}
    retained_only = _inputs(mesh, now_ns=0, retained=retained)
    rows = tuple(
        (name, SubjectEvidence(False, (), evidence.retained_json) if name == "claude" else evidence)
        for name, evidence in retained_only.subjects
    )
    assert _effective(evaluate_lifecycle(replace(retained_only, subjects=rows), "claude")) == record


def test_evaluation_is_deterministic_proposes_serialized_heads_and_does_no_clock_io(mesh, monkeypatch):
    captured = _inputs(mesh)
    reordered = replace(captured, accounts=tuple(reversed(captured.accounts)),
                        subjects=tuple(reversed(captured.subjects)))
    monkeypatch.setattr(lifecycle.time, "time_ns", lambda: pytest.fail("live clock read"))
    monkeypatch.setattr(mesh.tx, "get_doc", lambda *_args: pytest.fail("transport read"))
    monkeypatch.setattr(mesh.tx, "list_docs", lambda *_args: pytest.fail("transport list"))
    monkeypatch.setattr(mesh.store, "observe_lifecycle_head",
                        lambda *_args: pytest.fail("Store read"))
    first = evaluate_lifecycle(captured, "claude")
    second = evaluate_lifecycle(reordered, "claude")
    assert first == second
    assert first.now_ns == captured.now_ns
    assert first.consumed_accounts == tuple(sorted(first.consumed_accounts))
    assert first.consumed_subjects == tuple(sorted(first.consumed_subjects))
    assert [proposal.subject for proposal in first.proposals] == sorted(
        proposal.subject for proposal in first.proposals
    )
    assert any(proposal.subject == "claude" and proposal.expected_retained_json is None
               for proposal in first.proposals)


def test_exact_input_limits_and_tampering_fail_before_remote_json_decode(mesh):
    malformed = CapturedLifecycleInputs(
        0,
        (),
        (("subject", SubjectEvidence(True, (("path", "{"),), None)),),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(evaluation.json, "loads", lambda *_args: pytest.fail("decoded"))
        monkeypatch.setattr(lifecycle.crypto, "verify", lambda *_args: pytest.fail("verified"))
        with pytest.raises(OverflowError):
            evaluate_lifecycle(malformed, "subject", limits=LifecycleLimits(max_bytes=0))
    finally:
        monkeypatch.undo()

    tampered = _inputs(mesh)
    object.__setattr__(tampered, "accounts", [])
    with pytest.raises((TypeError, ValueError)):
        evaluate_lifecycle(tampered, "claude")
    with pytest.raises(ValueError):
        evaluate_lifecycle(_inputs(mesh), "claude", limits=LifecycleLimits(max_accounts=True))
    duplicates = CapturedLifecycleInputs(
        0, (("a", None), ("a", None)), (("subject", SubjectEvidence(True, (), None)),)
    )
    with pytest.raises(ValueError):
        evaluate_lifecycle(duplicates, "subject")
    duplicate_envelopes = CapturedLifecycleInputs(
        0, (), (("subject", SubjectEvidence(True, (("p", "{}"), ("p", "{}")), None)),)
    )
    with pytest.raises(ValueError, match="duplicate.*envelope"):
        evaluate_lifecycle(duplicate_envelopes, "subject")

    utf8 = CapturedLifecycleInputs(0, (), (("é", SubjectEvidence(True, (), None)),))
    exact = 2 * len("é".encode("utf-8"))  # root plus the one supplied subject name
    assert evaluate_lifecycle(utf8, "é", limits=LifecycleLimits(max_bytes=exact)).effective_json is None
    with pytest.raises(OverflowError):
        evaluate_lifecycle(utf8, "é", limits=LifecycleLimits(max_bytes=exact - 1))


def test_equal_ns_signed_branches_sort_by_id_independent_of_envelope_order(mesh):
    path = mesh.tx.list_docs("lifecycle/claude")[0]
    bootstrap = mesh.tx.get_doc(path)["record"]
    bundle = mesh.keystore.load("aryan")

    def branch(record_id, active):
        record = {**bootstrap, "id": record_id, "ns": bootstrap["ns"] + 100,
                  "actor": "aryan", "action": "state", "previous_id": bootstrap["id"],
                  "active": active}
        return (f"lifecycle/claude/{record_id}.json", _json({
            "record": record,
            "sig": crypto.sign(bundle, canonical_json_bytes(record)),
            "subject_sig": "",
        }))

    low, high = branch("a-branch", False), branch("z-branch", True)
    captured = _inputs(mesh)
    rows = tuple(
        (name, SubjectEvidence(True, evidence.envelopes + (high, low), None)
         if name == "claude" else evidence)
        for name, evidence in captured.subjects
    )
    forward = evaluate_lifecycle(replace(captured, subjects=rows), "claude")
    reversed_rows = tuple(
        (name, SubjectEvidence(True, tuple(reversed(evidence.envelopes)), None)
         if name == "claude" else evidence)
        for name, evidence in rows
    )
    backward = evaluate_lifecycle(replace(captured, subjects=reversed_rows), "claude")
    assert _effective(forward)["id"] == "a-branch"
    assert forward == backward
