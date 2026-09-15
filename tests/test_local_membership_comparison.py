"""R181 comparisons through real signed membership events and captured trust."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.devtools import local_membership_comparison as comparison
from agentbridge.mesh import events
from agentbridge.mesh import local_membership_capture as membership
from agentbridge.mesh.lifecycle import LifecycleUnavailable, ensure_bootstrap
from agentbridge.mesh.lifecycle_evaluation import LifecycleInputsIncomplete
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import local_membership_inputs, shadow_slot
from agentbridge.transport.folder import FolderTransport


CHAT = "captured-membership"
SOURCE = shadow_slot.ShadowSource("fixture-root", "fixture-cache", "fixture-nonce")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def fixture_mesh(tmp_path):
    mesh = Mesh(FolderTransport(tmp_path / "mesh"), "aryan", "machine",
                home=tmp_path / "home")
    mesh.accounts.create_human("aryan", "aryan-pass")
    # Seed the accepted retained head before any test mutates published trust.
    ensure_bootstrap(mesh.directory, mesh.keystore, "aryan", actor="aryan")
    mesh.directory.get("aryan")
    yield mesh
    mesh.close()


def _base(name="Before", *, boundary=1):
    return ChatSnapshot(
        id=CHAT, kind=ChatKind.GROUP, name=name,
        members={"aryan": Member(Role.ADMIN, 1)}, materialized_ns=boundary,
    )


def _event(mesh, *, event_id="rename", ns=2, name="After", actor="aryan", bundle=None):
    envelope = {
        "id": event_id, "ns": ns, "from": actor, "kind": "info",
        "event": {"type": events.EV_RENAMED, "name": name, "by": actor},
    }
    envelope["sig"] = crypto.sign(
        bundle or mesh.keystore.load(actor), events.signing_bytes(CHAT, envelope),
    )
    return envelope


def _publish(mesh, meta, accounts):
    mesh.tx.put_doc(P.meta(CHAT), meta)
    records = [(P.meta(CHAT), _json(meta))]
    for name, doc in accounts.items():
        mesh.tx.put_doc(P.user(name), doc)
        records.append((P.user(name), _json(doc)))
        for path in mesh.tx.list_docs(f"lifecycle/{name}/"):
            records.append((path, _json(mesh.tx.get_doc(path))))
    position = mesh.store.acquire_shadow(
        mesh.store.inspect_shadow_position(), "fixture-publisher", SOURCE,
    )
    return mesh.store.publish_shadow(position, shadow_slot.ShadowSnapshot(
        SOURCE, 1, 0, "provider_observed", (CHAT,), tuple(sorted(records)),
    ))


def _compare(mesh, position, *, present=("aryan",), absent=(), subjects=("aryan",)):
    captured = membership.capture(
        mesh, position, CHAT, known_accounts=present,
        known_absent_accounts=absent, complete_lifecycle_subjects=subjects,
        now_ns=2**63 - 1,
    )
    return captured, comparison.compare_fixture(
        mesh, captured, comparison.register_disposable_fixture(mesh),
    )


def test_real_signed_suffix_matches_production_snapshot(fixture_mesh):
    mesh = fixture_mesh
    meta = _base().to_dict()
    account = mesh.tx.get_doc(P.user("aryan"))
    position = _publish(mesh, meta, {"aryan": account})
    mesh.store.upsert_messages(CHAT, [_event(mesh)])

    _captured, result = _compare(mesh, position)

    assert result.equal is True
    assert result.unavailable_reason is None
    assert result.captured_digest == result.canonical_digest
    assert dict(result.dependencies)["shadow_generation"] == str(position.generation)
    assert mesh.messaging.snapshot(CHAT).name == "After"


def test_controlled_materialized_divergence_is_reported(fixture_mesh):
    mesh = fixture_mesh
    canonical_meta = _base("Canonical").to_dict()
    position = _publish(mesh, canonical_meta, {"aryan": mesh.tx.get_doc(P.user("aryan"))})
    captured = membership.capture(
        mesh, position, CHAT, known_accounts=("aryan",),
        complete_lifecycle_subjects=("aryan",), now_ns=2**63 - 1,
    )
    raw = captured.raw
    revision, cursor, provenance, chats, records = raw.shadow
    changed = tuple(
        (path, _json(_base("Captured").to_dict())) if path == P.meta(CHAT)
        else (path, payload) for path, payload in records
    )
    divergent = replace(captured, raw=local_membership_inputs.RawMembershipInputs(
        raw.position, (revision, cursor, provenance, chats, changed),
        raw.events, raw.heads,
    ))

    result = comparison.compare_fixture(
        mesh, divergent, comparison.register_disposable_fixture(mesh),
    )
    assert result.equal is False
    assert result.captured_digest != result.canonical_digest
    assert "name" in result.differing_fields


@pytest.mark.parametrize("published", ["different", "keyless"])
def test_nonkeep_trust_is_unavailable_but_canonical_records_alert(
        fixture_mesh, published):
    mesh = fixture_mesh
    original_bundle = mesh.keystore.load("aryan")
    account = mesh.tx.get_doc(P.user("aryan"))
    if published == "different":
        _bundle, sign_pub, agree_pub = (
            lambda bundle: (bundle, *crypto.identity_pubs(bundle))
        )(crypto.generate_identity())
        account["keys"].update(sign_pub=sign_pub, agree_pub=agree_pub)
    else:
        account["keys"].update(sign_pub="", agree_pub="")
    position = _publish(mesh, _base().to_dict(), {"aryan": account})
    mesh.store.upsert_messages(CHAT, [_event(mesh, bundle=original_bundle)])

    _captured, result = _compare(mesh, position)

    assert result.equal is None
    assert result.unavailable_reason == "pin_action_required"
    alerts = mesh.key_pins.alerts()
    assert alerts and alerts[-1]["name"] == "aryan"


def test_unpinned_published_key_and_keyless_without_pin_take_distinct_paths(tmp_path):
    mesh = Mesh(FolderTransport(tmp_path / "mesh"), "viewer", "machine",
                home=tmp_path / "home")
    try:
        mesh.accounts.create_human("viewer", "viewer-pass")
        bundle = crypto.generate_identity()
        sign_pub, agree_pub = crypto.identity_pubs(bundle)
        guest = {"name": "guest", "kind": "human", "active": True,
                 "keys": {"sign_pub": sign_pub, "agree_pub": agree_pub}}
        meta = _base().to_dict()
        meta["members"] = {"viewer": {"role": "admin", "joined_ns": 1}}
        position = _publish(mesh, meta, {
            "viewer": mesh.tx.get_doc(P.user("viewer")), "guest": guest,
        })
        mesh.store.upsert_messages(CHAT, [
            _event(mesh, actor="guest", bundle=bundle),
        ])
        _captured, result = _compare(
            mesh, position, present=("viewer", "guest"),
            subjects=("viewer", "guest"),
        )
        assert result.equal is None and result.unavailable_reason == "pin_action_required"
        persisted = json.loads(mesh.key_pins.path.read_text())
        assert persisted["pins"]["guest"]["sign_pub"] == sign_pub
        assert persisted["pins"]["guest"]["agree_pub"] == agree_pub

        # A separately asserted keyless account with no durable pin is a keep.
        mesh.store.forget_chat(CHAT)
        keyless = {"name": "nobody", "kind": "human", "active": True,
                   "keys": {"sign_pub": "", "agree_pub": ""}}
        position = mesh.store.publish_shadow(position, shadow_slot.ShadowSnapshot(
            SOURCE, 2, 0, "provider_observed", (CHAT,), tuple(sorted((
                (P.meta(CHAT), _json(meta)),
                (P.user("viewer"), _json(mesh.tx.get_doc(P.user("viewer")))),
                (P.user("nobody"), _json(keyless)),
            ))),
        ))
        mesh.store.upsert_messages(CHAT, [{
            "id": "unsigned", "ns": 2, "from": "nobody", "kind": "info",
            "event": {"type": events.EV_RENAMED, "name": "Ignored", "by": "nobody"},
            "sig": "",
        }])
        _captured, fast = _compare(
            mesh, position, present=("viewer", "nobody"),
            subjects=("viewer", "nobody"),
        )
        assert fast.equal is True
    finally:
        mesh.close()


@pytest.mark.parametrize("boundary", [None, True])
def test_materialized_boundary_missing_defaults_zero_but_bool_is_invalid(
        fixture_mesh, boundary):
    mesh = fixture_mesh
    meta = _base(boundary=0).to_dict()
    if boundary is None:
        meta.pop("materialized_ns")
    else:
        meta["materialized_ns"] = boundary
    position = _publish(mesh, meta, {"aryan": mesh.tx.get_doc(P.user("aryan"))})
    _captured, result = _compare(mesh, position)
    if boundary is None:
        assert result.equal is True
    else:
        assert result.equal is None
        assert result.unavailable_reason == "invalid_captured_membership"


def test_resolver_distinguishes_unknown_from_explicitly_absent_owner(fixture_mesh):
    mesh = fixture_mesh
    position = _publish(
        mesh, _base().to_dict(), {"aryan": mesh.tx.get_doc(P.user("aryan"))},
    )
    captured = membership.capture(
        mesh, position, CHAT, known_accounts=("aryan",),
        known_absent_accounts=("absent",), complete_lifecycle_subjects=("aryan",),
        now_ns=2**63 - 1,
    )
    records = dict(comparison._shadow(captured).records)
    resolver = comparison._resolver(captured, records)
    assert resolver.owner_of("absent") is None
    with pytest.raises(LifecycleInputsIncomplete, match="unasserted"):
        resolver.owner_of("unknown")


@pytest.mark.parametrize("lookup", ["sign_pub", "kind"])
def test_malformed_retained_is_unavailable_for_identity_lookup(fixture_mesh, lookup):
    mesh = fixture_mesh
    position = _publish(
        mesh, _base().to_dict(), {"aryan": mesh.tx.get_doc(P.user("aryan"))},
    )
    captured = membership.capture(
        mesh, position, CHAT, known_accounts=("aryan",), now_ns=2**63 - 1,
    )
    raw = captured.raw
    malformed = tuple(
        (subject, generation, "{") if subject == "aryan" else row
        for row in raw.heads for subject, generation, _payload in (row,)
    )
    captured = replace(captured, raw=replace(raw, heads=malformed))
    resolver = comparison._resolver(captured, dict(comparison._shadow(captured).records))
    with pytest.raises(LifecycleUnavailable):
        getattr(resolver, lookup)("aryan")


def test_valid_retained_fallback_when_local_lifecycle_enumeration_is_incomplete(
        fixture_mesh):
    mesh = fixture_mesh
    position = _publish(
        mesh, _base().to_dict(), {"aryan": mesh.tx.get_doc(P.user("aryan"))},
    )
    captured = membership.capture(
        mesh, position, CHAT, known_accounts=("aryan",),
        complete_lifecycle_subjects=(), now_ns=0,
    )
    assert any(subject == "aryan" and payload is not None
               for subject, _generation, payload in captured.raw.heads)
    resolver = comparison._resolver(captured, dict(comparison._shadow(captured).records))
    assert resolver.sign_pub("aryan") == mesh.key_pins.projection_facts(["aryan"])[
        "pins"
    ]["aryan"]["sign_pub"]
