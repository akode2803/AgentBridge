"""C1.0's behavior-preserving runtime contract baseline."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from agentbridge.harness.runtime import (
    ContinuationRecord,
    ContinuationState,
    ControlRecord,
    ControlState,
    ControlType,
    EffectRecord,
    EffectState,
    HandoffRecord,
    HandoffState,
    HandoffType,
    RecordKind,
    RecordMeta,
    RunRecord,
    RunState,
    RuntimeContractError,
    RuntimeEnvelope,
    TaskRecord,
    TaskState,
    canonical_json_bytes,
    record_from_dict,
)


def meta(kind: RecordKind, *, task: bool = False, call: bool = False) -> RecordMeta:
    return RecordMeta(
        schema_version=1, kind=kind, id=f"rec-{kind.value}", ns=100,
        actor="planner", chat_id="room-a", signer="owner-a", root_run_id="run-1",
        run_id="run-1", task_id="task-1" if task else None,
        call_id="call-1" if call else None, key_epoch=2, policy_revision=3,
        membership_epoch=4, ownership_epoch=5, expires_ns=200,
    )


def records():
    yield RunRecord(
        meta(RecordKind.RUN), RunState.RUNNING, "message-1", "planner", "owner-a",
        "brokered", "openai", "gpt", ("filesystem.read",), ("task-1",),
        "Working", None,
    )
    yield TaskRecord(
        meta(RecordKind.TASK, task=True), TaskState.ACTIVE, "Inspect the repository",
        "worker", "planner", "owner-b", None, ("Report findings",), "sha256:ctx",
        ("grant-1",), (), "Reading", None, "planner",
    )
    yield HandoffRecord(
        meta(RecordKind.HANDOFF, task=True, call=True), HandoffState.ACCEPTED,
        HandoffType.HANDOFF, "planner", "worker", "owner-a", "owner-b", "owner-a",
        "Specialist review", "sha256:handoff", ("filesystem.read",), ("grant-1",),
        "planner", None,
    )
    yield EffectRecord(
        meta(RecordKind.EFFECT, task=True, call=True), EffectState.PREPARED,
        "filesystem.write", "sha256:args", "effect-key", "grant-1", "worker", 1,
        None, "not_requested",
    )
    yield ContinuationRecord(
        meta(RecordKind.CONTINUATION, task=True, call=True), ContinuationState.PAUSED,
        "task-1", "task-2", "provider-state", "sandbox-state", ("grant-1",),
        "sha256:context", 300,
    )
    yield ControlRecord(
        meta(RecordKind.CONTROL, task=True, call=True), ControlType.ASK,
        ControlState.REQUESTED, "worker", None, "sha256:input", None, None, 1, True,
    )


@pytest.mark.parametrize("record", list(records()))
def test_runtime_records_are_immutable_strict_canonical_and_round_trip(record):
    encoded = record.to_dict()

    parsed = record_from_dict(encoded)
    assert parsed == record
    assert parsed.canonical_bytes() == canonical_json_bytes(encoded)
    assert parsed.canonical_bytes() == record.canonical_bytes()
    with pytest.raises(FrozenInstanceError):
        record.meta = meta(RecordKind.RUN)  # type: ignore[misc]

    with pytest.raises(RuntimeContractError, match="extra"):
        record_from_dict({**encoded, "unexpected": True})


def test_pre_r134_run_record_defaults_to_no_native_authority():
    encoded = next(records()).to_dict()
    for name in ("native_policy_digest", "provider_policy_digest",
                 "native_provider_version",
                 "native_enabled", "native_approval_gated", "native_blocked"):
        encoded.pop(name)
    parsed = record_from_dict(encoded)
    assert isinstance(parsed, RunRecord)
    assert parsed.native_policy_digest == ""
    assert parsed.native_provider_version == ""
    assert parsed.native_enabled == ()


def test_r134_digest_without_effective_facts_migrates_fail_closed():
    encoded = next(records()).to_dict()
    encoded["native_policy_digest"] = "a" * 64
    for name in ("provider_policy_digest", "native_provider_version", "native_enabled",
                 "native_approval_gated", "native_blocked"):
        encoded.pop(name)
    parsed = record_from_dict(encoded)
    assert isinstance(parsed, RunRecord)
    assert parsed.native_policy_digest == ""
    assert parsed.native_enabled == parsed.native_blocked == ()


def test_runtime_envelope_binds_metadata_ciphertext_and_signature_spelling():
    envelope = RuntimeEnvelope(meta(RecordKind.CONTROL, task=True, call=True),
                               "base64-nonce", "base64-ciphertext", "base64-signature")
    assert RuntimeEnvelope.from_dict(envelope.to_dict()) == envelope
    assert envelope.meta.chat_id.encode() in envelope.aad_bytes()
    assert b"base64-ciphertext" in envelope.signing_bytes()
    assert b"base64-signature" not in envelope.signing_bytes()
    assert envelope.signing_bytes() == RuntimeEnvelope.from_dict(
        envelope.to_dict()).signing_bytes()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "unsupported schema_version"),
        ("kind", "future_kind", "unknown record kind"),
        ("ns", 0, "ns must be"),
        ("expires_ns", 99, "expires_ns must be greater"),
    ],
)
def test_record_metadata_fails_closed(field, value, message):
    encoded = meta(RecordKind.RUN).to_dict()
    encoded[field] = value
    with pytest.raises(RuntimeContractError, match=message):
        RecordMeta.from_dict(encoded)


def test_unknown_state_and_cross_kind_substitution_fail_closed():
    run = next(records()).to_dict()
    run["state"] = "future_state"
    with pytest.raises(RuntimeContractError, match="unknown state"):
        record_from_dict(run)

    run = next(records()).to_dict()
    run["meta"]["kind"] = "control"
    with pytest.raises(RuntimeContractError, match="ControlRecord record"):
        record_from_dict(run)

    direct = next(records())
    with pytest.raises(RuntimeContractError, match="state must be RunState"):
        replace(direct, state="future_state")
    with pytest.raises(RuntimeContractError, match="immutable string tuple"):
        replace(direct, capability_ceiling=["filesystem.read"])


def test_required_lineage_and_control_subtype_bindings_fail_closed():
    task = list(records())[1]
    with pytest.raises(RuntimeContractError, match="task_id"):
        replace(task, meta=replace(task.meta, task_id=None))

    control = list(records())[-1]
    with pytest.raises(RuntimeContractError, match="call_id"):
        replace(control, meta=replace(control.meta, call_id=None))

    with pytest.raises(RuntimeContractError, match="target_machine"):
        replace(
            control, control_type=ControlType.APPLINK,
            target_agent=None, target_machine=None,
        )


def test_canonical_json_rejects_non_json_numbers():
    with pytest.raises(RuntimeContractError, match="canonical JSON"):
        canonical_json_bytes({"value": float("nan")})


def test_legacy_controls_do_not_import_generic_runtime_records():
    """C1.1 uses a dedicated secure lane, never generic partial records."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "agentbridge"
    production = [
        root / "harness" / "broker.py", root / "harness" / "peer.py",
        root / "harness" / "runner.py", root / "gui" / "api_agents.py",
        root / "applink" / "control.py",
    ]
    for path in production:
        source = path.read_text(encoding="utf-8")
        assert "runtime.models" not in source
