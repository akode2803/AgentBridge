"""R184 one-shot local membership admission, still unwired from runtime."""

from __future__ import annotations

import json
import time

import pytest

from agentbridge import crypto
from agentbridge.mesh import events
from agentbridge.mesh import membership_read as admission
from agentbridge.mesh.lifecycle import ensure_bootstrap, publish_change
from agentbridge.mesh.membership_read import (
    MembershipReadLimits,
    read_membership_snapshot,
    register_membership_admission_scope,
)
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def admitted_mesh(tmp_path):
    provider = FolderTransport(tmp_path / "mesh")
    provider.cache_key = "admission-cache"
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror._mirror_root_identity = "admission-root"
    mirror._mirror_cache_identity = "admission-cache"
    mesh = Mesh(mirror, "aryan", "admission-box", home=tmp_path / "home")
    try:
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_human("fable", "fable-pass")
        mirror.refresh()
        chat = mesh.membership.create_chat("Admission", members=["fable"])
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        yield mesh, mirror, chat.id
    finally:
        mesh.close()


def _read(mesh, chat_id, **kwargs):
    return read_membership_snapshot(
        mesh, chat_id, register_membership_admission_scope(mesh), **kwargs,
    )


def _canonical(mesh, chat_id):
    return mesh.messaging.snapshot(chat_id).to_dict()


def _newer_suffix(mesh, mirror, chat_id, name="After"):
    old_meta = mirror.get_doc(P.meta(chat_id))
    mesh.membership.rename(chat_id, name)
    mirror.refresh()
    mesh.sync.sync_once([chat_id])
    with mirror._lock:
        mirror._docs[P.meta(chat_id)] = old_meta
        mirror._mirror_revision += 1


def _signed_rename(mesh, chat_id, actor, bundle, name):
    event = {
        "id": f"admission-{actor}-{time.time_ns()}",
        "ns": time.time_ns(),
        "from": actor,
        "kind": "info",
        "event": {"type": events.EV_RENAMED, "name": name, "by": actor},
    }
    event["sig"] = crypto.sign(bundle, events.signing_bytes(chat_id, event))
    mesh.store.upsert_messages(chat_id, [event])


def test_genuine_signed_suffix_is_admitted_and_matches_explicit_canonical_snapshot(
        admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    _newer_suffix(mesh, mirror, chat_id)

    result = _read(mesh, chat_id)
    assert result.source == "admitted_local", result.rejection_reason
    assert result.rejection_reason is None and result.admission is not None
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)
    assert json.loads(result.snapshot_json)["name"] == "After"


def test_signed_suffix_captures_each_lazy_dependency_once(admitted_mesh, monkeypatch):
    mesh, mirror, chat_id = admitted_mesh
    _newer_suffix(mesh, mirror, chat_id)
    original = mirror.capture_mirror_selection
    requests = []

    def capture(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(mirror, "capture_mirror_selection", capture)
    result = _read(mesh, chat_id)
    assert result.source == "admitted_local", result.rejection_reason
    assert json.loads(result.snapshot_json)["name"] == "After"
    assert sum(request.complete_prefixes == ("lifecycle/",) for request in requests) == 1
    assert sum(request.exact_paths == (P.user("aryan"),) for request in requests) == 1
    assert result.admission.consumed_accounts.count("aryan") == 1


@pytest.mark.parametrize("phase", ("lifecycle", "account", "final"))
@pytest.mark.parametrize("persistent", (False, True), ids=("once", "persistent"))
def test_lazy_selection_mirror_change_retries_whole_read_once(
        admitted_mesh, monkeypatch, phase, persistent):
    mesh, mirror, chat_id = admitted_mesh
    _newer_suffix(mesh, mirror, chat_id)
    original = mirror.capture_mirror_selection
    initial_captures = 0
    lazy_changes = 0
    canonical_calls = 0

    def mutate():
        nonlocal lazy_changes
        lazy_changes += 1
        mirror.put_doc("fixture/lazy-change.json", {"change": lazy_changes})

    def capture(request):
        nonlocal initial_captures, lazy_changes
        if request.expected is None:
            initial_captures += 1
        result = original(request)
        selected_phase = request.complete_prefixes if phase == "lifecycle" else (
            request.exact_paths == (P.user("aryan"),) if phase == "account" else False
        )
        if selected_phase and (persistent or lazy_changes == 0):
            mutate()
        return result

    monkeypatch.setattr(mirror, "capture_mirror_selection", capture)
    if phase == "final":
        original_final = admission._validate_final

        def validate(*args, **kwargs):
            if persistent or lazy_changes == 0:
                mutate()
            return original_final(*args, **kwargs)

        monkeypatch.setattr(admission, "_validate_final", validate)
    original_canonical = mesh.messaging.snapshot

    def canonical(chat):
        nonlocal canonical_calls
        canonical_calls += 1
        return original_canonical(chat)

    monkeypatch.setattr(mesh.messaging, "snapshot", canonical)
    result = _read(mesh, chat_id)
    assert initial_captures == 2
    if persistent:
        assert (result.source, result.rejection_reason) == ("canonical", "mirror_changed")
        assert canonical_calls == 1
        assert json.loads(result.snapshot_json)["name"] == "After"
    else:
        assert result.source == "admitted_local", result.rejection_reason
        assert canonical_calls == 0
        assert json.loads(result.snapshot_json)["name"] == "After"


def test_aggregate_selection_pin_and_sqlite_byte_boundary(admitted_mesh, monkeypatch):
    mesh, mirror, chat_id = admitted_mesh
    _newer_suffix(mesh, mirror, chat_id)
    original_selection = mirror.capture_mirror_selection
    original_cut = admission.membership_read_inputs.capture_cut
    selections = []
    cut_bytes = []

    def capture(request):
        result = original_selection(request)
        if hasattr(result, "serialized_bytes"):
            selections.append(result)
        return result

    def capture_cut(*args, **kwargs):
        result = original_cut(*args, **kwargs)
        cut_bytes.append(result.serialized_bytes)
        return result

    monkeypatch.setattr(mirror, "capture_mirror_selection", capture)
    monkeypatch.setattr(admission.membership_read_inputs, "capture_cut", capture_cut)
    baseline = _read(mesh, chat_id)
    assert baseline.source == "admitted_local", baseline.rejection_reason
    pin_bytes = len(admission.canonical_json({"pins": mesh.key_pins._pins,
                                              "alerts": mesh.key_pins._alerts}).encode("utf-8"))
    exact_limit = sum(item.serialized_bytes for item in selections) + cut_bytes[-1] + pin_bytes
    exact_records = sum(len(item.exact_records) + sum(
        len(group.records) for group in item.complete_prefixes
    ) for item in selections)

    selections.clear()
    cut_bytes.clear()
    at_limit = _read(mesh, chat_id, limits=MembershipReadLimits(max_bytes=exact_limit))
    assert at_limit.source == "admitted_local", at_limit.rejection_reason
    below = _read(mesh, chat_id, limits=MembershipReadLimits(max_bytes=exact_limit - 1))
    assert below.source == "canonical" and below.rejection_reason == "mirror_budget_exceeded"
    at_count = _read(mesh, chat_id, limits=MembershipReadLimits(max_documents=exact_records))
    assert at_count.source == "admitted_local", at_count.rejection_reason
    below_count = _read(
        mesh, chat_id, limits=MembershipReadLimits(max_documents=exact_records - 1),
    )
    assert below_count.source == "canonical"
    assert below_count.rejection_reason == "mirror_budget_exceeded"


def test_no_newer_suffix_admits_without_lifecycle_resolution(admitted_mesh, monkeypatch):
    mesh, _mirror, chat_id = admitted_mesh
    import agentbridge.mesh.membership_read as admission

    monkeypatch.setattr(
        admission, "_CapturedResolver",
        lambda *_args, **_kwargs: pytest.fail("no newer suffix resolved lifecycle"),
    )
    result = _read(mesh, chat_id)
    assert result.source == "admitted_local", result.rejection_reason
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)


def test_no_newer_suffix_never_requests_lifecycle_prefix(admitted_mesh, monkeypatch):
    mesh, _mirror, chat_id = admitted_mesh
    original = mesh.tx.capture_mirror_selection
    requests = []

    def selected(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(mesh.tx, "capture_mirror_selection", selected)
    assert _read(mesh, chat_id).source == "admitted_local"
    assert all(not request.complete_prefixes for request in requests)


def test_no_newer_small_chat_ignores_large_unrelated_mirror_payload(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    mirror.put_doc("users/unrelated-large.json", {"payload": "x" * (17 * 1024 * 1024)})
    result = _read(mesh, chat_id)
    assert result.source == "admitted_local", result.rejection_reason
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)


def test_missing_detached_account_uses_canonical_readthrough_fallback(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    _newer_suffix(mesh, mirror, chat_id)
    with mirror._lock:
        assert mirror._docs.pop(P.user("aryan")) is not None
        mirror._mirror_revision += 1
    result = _read(mesh, chat_id)
    assert result.source == "canonical"
    assert result.rejection_reason == "account_unknown"
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)


def test_unpinned_actor_falls_back_and_canonical_persists_first_seen_pin(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    mesh.key_pins.forget("fable")
    _signed_rename(mesh, chat_id, "fable", mesh.keystore.load("fable"), "Needs first sight")
    result = _read(mesh, chat_id)
    assert result.source == "canonical"
    assert result.rejection_reason == "pin_action_required"
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)
    assert mesh.key_pins.fingerprint("fable")


def test_changed_actor_key_falls_back_and_canonical_records_alert(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    changed = crypto.generate_identity()
    sign_pub, agree_pub = crypto.identity_pubs(changed)
    account = mirror.get_doc(P.user("fable"))
    account["keys"] = {"sign_pub": sign_pub, "agree_pub": agree_pub}
    mirror.put_doc(P.user("fable"), account)
    _signed_rename(mesh, chat_id, "fable", changed, "Needs alert")

    result = _read(mesh, chat_id)
    assert result.source == "canonical"
    assert result.rejection_reason == "pin_action_required"
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)
    assert any(alert["name"] == "fable" for alert in mesh.key_pins.alerts())


def test_unrelated_malformed_account_stays_lazy_for_signed_suffix(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    mirror.put_doc(P.user("unrelated"), {"not": "an account"})
    _newer_suffix(mesh, mirror, chat_id)

    result = _read(mesh, chat_id)
    assert result.source == "admitted_local", result.rejection_reason
    assert json.loads(result.snapshot_json) == _canonical(mesh, chat_id)


def test_retained_head_proposal_falls_back_to_canonical_owner(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    ensure_bootstrap(mesh.directory, mesh.keystore, "aryan", actor="aryan")
    mesh.directory.get("aryan")
    mirror.refresh()
    _newer_suffix(mesh, mirror, chat_id)
    with mesh.store._conn() as conn:
        conn.execute("DELETE FROM docs WHERE path=?", ("lifecycle/head/aryan",))

    result = _read(mesh, chat_id)
    assert result.source == "canonical"
    assert result.rejection_reason == "retained_write_required"
    assert mesh.store.observe_lifecycle_head("aryan").payload_json is not None


def test_actual_recursive_lifecycle_closure_is_lazy_and_dependency_bounded(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    mesh.accounts.create_agent("helper")
    for subject in ("aryan", "fable", "helper"):
        ensure_bootstrap(mesh.directory, mesh.keystore, subject, actor=subject)
    publish_change(mesh.directory, mesh.keystore, "helper", actor="fable",
                   action="transfer", owner="fable", machine="fable-machine")
    mesh.directory.get("helper")
    mirror.refresh()
    observed, pins_json, _present, cut = admission._capture(
        mesh, chat_id, MembershipReadLimits(), 2**63 - 1,
    )
    records = dict(observed.exact_records)
    resolver = admission._CapturedResolver(
        records, pins_json, cut.heads, 2**63 - 1, MembershipReadLimits(),
        observed.position, observed.serialized_bytes + len(pins_json.encode("utf-8"))
        + cut.serialized_bytes,
    ).bind_transport(mesh.tx)
    assert resolver._state("helper")["owner"] == "fable"
    assert {"helper", "fable"} <= resolver.consumed_subjects

    # The bound is global to the read. A completed subject is memoized, but a
    # different subject cannot start another evaluation after it consumes the
    # remaining operation budget.
    bounded = admission._CapturedResolver(
        records, pins_json, cut.heads, 2**63 - 1,
        MembershipReadLimits(max_dependency_steps=resolver.dependency_steps),
        observed.position, observed.serialized_bytes + len(pins_json.encode("utf-8"))
        + cut.serialized_bytes,
    ).bind_transport(mesh.tx)
    assert bounded._state("helper")["owner"] == "fable"
    used = bounded.dependency_steps
    assert bounded._state("helper")["owner"] == "fable"
    assert bounded.dependency_steps == used
    with pytest.raises(admission._Reject, match="dependency_limit"):
        bounded._state("aryan")


def test_folder_transport_is_unsupported_and_continues_canonical(tmp_path):
    provider = FolderTransport(tmp_path / "folder")
    writer = Mesh(provider, "aryan", "writer", home=tmp_path / "writer-home")
    try:
        writer.accounts.create_human("aryan", "aryan-pass")
        chat = writer.membership.create_chat("Folder admission")
    finally:
        writer.close()
    reader = Mesh(provider, "aryan", "reader", home=tmp_path / "reader-home")
    try:
        result = _read(reader, chat.id)
        assert (result.source, result.rejection_reason) == ("canonical", "unsupported")
        assert json.loads(result.snapshot_json)["id"] == chat.id
    finally:
        reader.close()


def test_cold_cache_falls_back_without_candidate_provider_warmup(tmp_path, monkeypatch):
    provider = FolderTransport(tmp_path / "cold")
    writer = Mesh(provider, "aryan", "writer", home=tmp_path / "writer-home")
    try:
        writer.accounts.create_human("aryan", "aryan-pass")
        chat = writer.membership.create_chat("Cold admission")
    finally:
        writer.close()
    cold = CachingTransport(provider, auto_refresh=False)
    cold._mirror_root_identity = "cold-root"
    cold._mirror_cache_identity = "cold-cache"
    reader = Mesh(cold, "aryan", "reader", home=tmp_path / "reader-home")
    try:
        snapshot_docs = provider.snapshot_docs
        warmed = []
        monkeypatch.setattr(
            provider, "snapshot_docs",
            lambda: warmed.append("provider") or snapshot_docs(),
        )
        canonical = reader.messaging.snapshot

        def after_candidate(chat_id):
            assert warmed == []
            return canonical(chat_id)

        monkeypatch.setattr(reader.messaging, "snapshot", after_candidate)
        result = _read(reader, chat.id)
        assert (result.source, result.rejection_reason) == ("canonical", "mirror_cold")
        assert json.loads(result.snapshot_json)["id"] == chat.id
        assert warmed == ["provider"]
    finally:
        reader.close()


def test_bootstrap_unverified_admission_is_explicitly_only_a_local_point(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    with mirror._lock:
        mirror._mirror_provenance = "bootstrap_unverified"
    observation = mirror.capture_mirror()
    assert observation.provenance == "bootstrap_unverified"

    result = _read(mesh, chat_id)
    assert result.source == "admitted_local"
    assert result.admission is not None
    # Admission carries a same-instance position, never a provider cursor or
    # freshness assertion; bootstrap provenance remains observation metadata.
    assert not hasattr(result.admission, "provider_cursor")


def test_unrefreshed_provider_and_local_write_are_only_local_mirror_evidence(admitted_mesh):
    mesh, mirror, chat_id = admitted_mesh
    stale_name = json.loads(_read(mesh, chat_id).snapshot_json)["name"]
    provider = mirror.inner
    remote_meta = provider.get_doc(P.meta(chat_id))
    remote_meta["name"] = "Provider-only"
    provider.put_doc(P.meta(chat_id), remote_meta)

    provider_only = _read(mesh, chat_id)
    assert provider_only.source == "admitted_local"
    assert json.loads(provider_only.snapshot_json)["name"] == stale_name

    local_meta = mirror.get_doc(P.meta(chat_id))
    local_meta["name"] = "Local write"
    mirror.put_doc(P.meta(chat_id), local_meta)
    local = _read(mesh, chat_id)
    assert local.source == "admitted_local"
    assert json.loads(local.snapshot_json)["name"] == "Local write"


def test_canonical_exception_identity_propagates_after_candidate_disposal(
        admitted_mesh, monkeypatch):
    mesh, _mirror, chat_id = admitted_mesh
    import agentbridge.mesh.membership_read as admission

    expected = RuntimeError("canonical sentinel")
    monkeypatch.setattr(
        admission, "_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(admission._Reject("capture_unavailable")),
    )
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda *_args: (_ for _ in ()).throw(expected))
    with pytest.raises(RuntimeError) as raised:
        _read(mesh, chat_id, limits=MembershipReadLimits())
    assert raised.value is expected


def test_scope_mismatch_continues_to_canonical_exactly_once(admitted_mesh, monkeypatch):
    mesh, _mirror, chat_id = admitted_mesh
    scope = register_membership_admission_scope(mesh)
    object.__setattr__(scope, "database_path", "wrong.sqlite")
    calls = []
    canonical = mesh.messaging.snapshot
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda value: calls.append(value) or canonical(value))
    result = read_membership_snapshot(mesh, chat_id, scope)
    assert (result.source, result.rejection_reason, calls) == ("canonical", "scope_mismatch", [chat_id])
