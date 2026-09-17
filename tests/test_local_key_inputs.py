"""Bounded local key-wrap capture and final-comparison contracts."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge.mesh import local_key_inputs, local_page_source
from agentbridge.store import document_observation, local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.key_observation import (
    AuthorityObservationUnavailable,
    capture_key_wrap,
)


CHAT = "room"
EPOCH = 7
VIEWER = "alice"
PATH = f"chats/{CHAT}/keys/{EPOCH}.json"
VALID = {"eph": "ephemeral-secret", "nonce": "nonce-secret", "ct": "cipher-secret"}


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    root = MutationCoordinator(tmp_path / "owner", "local-key-root")
    root.register_store(store)
    reader = local_page_source.LocalPageSource(root, store, CHAT)
    publisher = SourcePublisher(root, store, reader.definition)
    try:
        yield store, root, reader, publisher
    finally:
        store.close()


def _publish(rig, value_marker=...):
    _store, _root, reader, publisher = rig
    documents = {} if value_marker is ... else {PATH: value_marker}
    publisher.publish(publisher.capture(), documents, observed_ns=1)
    return reader.capture()


def _capture(rig, value_marker=..., **kwargs):
    _store, _root, reader, _publisher = rig
    receipt = _publish(rig, value_marker)
    charges = []
    value = local_key_inputs.capture(
        reader, receipt, EPOCH, VIEWER, charge_source=charges.append, **kwargs,
    )
    return receipt, value, charges


def _mirror(tmp_path, value_marker=...):
    provider = FolderTransport(tmp_path / "provider")
    if value_marker is not ...:
        provider.put_doc(PATH, value_marker)
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    if value_marker is ...:
        with mirror._lock:
            mirror._neg.add(PATH)
    return mirror


@pytest.mark.parametrize(
    ("document", "shape"),
    [
        (None, "document_not_dict"),
        ([], "document_not_dict"),
        ({}, "wrapped_not_dict"),
        ({"wrapped": []}, "wrapped_not_dict"),
        ({"wrapped": {}}, "viewer_not_dict"),
        ({"wrapped": {VIEWER: []}}, "viewer_not_dict"),
        ({"wrapped": {VIEWER: {"eph": "e", "nonce": "n"}}}, "invalid_fields"),
        ({"wrapped": {VIEWER: VALID}}, "valid"),
    ],
)
def test_local_and_mirror_shape_field_and_selected_byte_parity(
        rig, tmp_path, document, shape):
    _receipt, local, charges = _capture(rig, document)
    mirror = _mirror(tmp_path, document)
    try:
        observed = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    finally:
        mirror.close()

    assert (local.shape, local.fields, local.captured_bytes) == (
        observed.shape, observed.fields, observed.captured_bytes,
    )
    assert local.shape == shape
    assert charges == [local.source_bytes]


def test_local_and_mirror_absence_parity(rig, tmp_path):
    _receipt, local, charges = _capture(rig)
    mirror = _mirror(tmp_path)
    try:
        observed = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    finally:
        mirror.close()
    assert (local.mode, local.shape, local.fields, local.captured_bytes) == (
        "known_negative", observed.shape, observed.fields, observed.captured_bytes,
    )
    assert observed.mode == "known_negative"
    assert charges == [local.source_bytes]


def test_large_document_charges_full_source_but_selects_only_viewer_wrap(rig):
    wrapped = {f"other-{i}": {"eph": "x" * 80, "nonce": "n", "ct": "c"}
               for i in range(900)}
    wrapped[VIEWER] = dict(VALID)
    _receipt, value, charges = _capture(rig, {"wrapped": wrapped})

    assert value.source_bytes > 64 * 1024
    assert value.captured_bytes < 1024
    assert value.fields == tuple(VALID[name] for name in ("eph", "nonce", "ct"))
    assert charges == [value.source_bytes]


def test_full_source_charge_precedes_json_parse_and_propagates_exhaustion(
        rig, monkeypatch):
    _store, _root, reader, _publisher = rig
    receipt = _publish(rig, {"wrapped": {VIEWER: VALID}})
    parsed = False

    def forbidden_parse(*_args, **_kwargs):
        nonlocal parsed
        parsed = True
        raise AssertionError("parse ran before operation charge")

    monkeypatch.setattr(local_key_inputs, "_parse", forbidden_parse)

    def exhausted(_size):
        raise OverflowError("operation budget")

    with pytest.raises(OverflowError, match="operation budget"):
        local_key_inputs.capture(
            reader, receipt, EPOCH, VIEWER, charge_source=exhausted,
        )
    assert not parsed


def test_raw_size_preflight_rejects_before_payload_copy(rig, monkeypatch):
    _store, _root, reader, _publisher = rig
    receipt = _publish(rig, {"wrapped": {VIEWER: VALID}, "padding": "x" * 5000})
    original = document_observation._open_reader
    payload_reads = []

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(
            lambda sql: payload_reads.append(sql)
            if "SELECT payload,deleted" in sql else None
        )
        return conn

    monkeypatch.setattr(document_observation, "_open_reader", traced)
    with pytest.raises(OverflowError, match="byte budget"):
        local_key_inputs.capture(
            reader, receipt, EPOCH, VIEWER,
            charge_source=lambda _size: None, max_source_bytes=128,
        )
    assert payload_reads == []


def test_malformed_json_is_not_absence_and_charge_still_precedes_parse(rig):
    _store, _root, reader, _publisher = rig
    receipt = _publish(rig)
    absent = local_key_inputs.capture(
        reader, receipt, EPOCH, VIEWER, charge_source=lambda _size: None,
    )
    malformed = replace(
        absent,
        record=document_observation.SerializedDocumentRecord(PATH, "{", False),
        mode="present", shape="document_not_dict",
        source_bytes=len(PATH.encode()) + 1,
    )
    charges = []
    with pytest.raises(json.JSONDecodeError):
        local_key_inputs.prepare(reader, malformed, charge_source=charges.append)
    assert charges == [malformed.source_bytes]


def test_nonfinite_json_is_rejected_after_full_source_charge(rig):
    _store, _root, reader, _publisher = rig
    receipt = _publish(rig)
    absent = local_key_inputs.capture(
        reader, receipt, EPOCH, VIEWER, charge_source=lambda _size: None,
    )
    raw = '{"wrapped":NaN}'
    nonfinite = replace(
        absent,
        record=document_observation.SerializedDocumentRecord(PATH, raw, False),
        mode="present", shape="wrapped_not_dict",
        source_bytes=len(PATH.encode()) + len(raw.encode()),
    )
    charges = []
    with pytest.raises(ValueError, match="JSON constant"):
        local_key_inputs.prepare(reader, nonfinite, charge_source=charges.append)
    assert charges == [nonfinite.source_bytes]


@pytest.mark.parametrize(
    ("mutation", "pattern"),
    [
        (lambda value: replace(value, chat_id="other"), "another chat"),
        (lambda value: replace(value, epoch=0), "invalid epoch"),
        (lambda value: replace(value, viewer="../bad"), "invalid"),
        (lambda value: replace(value, mode="known_negative"), "mode"),
        (lambda value: replace(value, shape="absent"), "shape"),
        (lambda value: replace(value, fields=tuple(v[::-1] for v in value.fields)), "match raw"),
        (lambda value: replace(value, captured_bytes=value.captured_bytes + 1), "byte"),
        (lambda value: replace(value, source_bytes=value.source_bytes + 1), "source bytes"),
    ],
)
def test_prepare_rejects_forged_identity_shape_fields_and_sizes(
        rig, mutation, pattern):
    _receipt, observed, _charges = _capture(rig, {"wrapped": {VIEWER: VALID}})
    with pytest.raises((ValueError, AuthorityObservationUnavailable), match=pattern):
        local_key_inputs.prepare(
            rig[2], mutation(observed), charge_source=lambda _size: None,
        )


def test_receipt_root_epoch_and_source_forgery_rejected(rig, tmp_path):
    _receipt, observed, _charges = _capture(rig, {"wrapped": {VIEWER: VALID}})
    for forged in (
        replace(observed.receipt, coordinator_path=str(tmp_path / "other.sqlite")),
        replace(observed.receipt, coordinator_epoch="0" * 32),
        replace(observed.receipt, source=replace(
            observed.receipt.source,
            raw=replace(observed.receipt.source.raw, source_id="wrong"),
        )),
    ):
        with pytest.raises((ValueError, local_source.SourceChanged)):
            local_key_inputs.prepare(
                rig[2], replace(observed, receipt=forged),
                charge_source=lambda _size: None,
            )


def test_prepare_then_transaction_match_and_source_mutation_rejection(rig):
    store, _root, reader, publisher = rig
    receipt, observed, _charges = _capture(rig, {"wrapped": {VIEWER: VALID}})
    prepared = local_key_inputs.prepare(
        reader, observed, charge_source=lambda _size: None,
    )
    with reader.finalization(receipt) as conn:
        with pytest.raises(ValueError, match="semantically prepared"):
            local_key_inputs._matches_prepared_in_transaction(
                reader, conn, observed,
            )
        assert local_key_inputs._matches_prepared_in_transaction(
            reader, conn, prepared,
        )

    publisher.publish(
        publisher.capture(), {PATH: {"wrapped": {VIEWER: dict(VALID, ct="changed")}}},
        observed_ns=2,
    )
    with pytest.raises(local_source.SourceChanged):
        with reader.finalization(receipt):
            pytest.fail("stale local key receipt reached final interval")
    assert local_source.capture(store, reader.definition.source).ready


def test_secret_record_and_selected_fields_are_omitted_from_repr(rig):
    _receipt, observed, _charges = _capture(rig, {"wrapped": {VIEWER: VALID}})
    rendered = repr(observed)
    for secret in VALID.values():
        assert secret not in rendered
    assert "record=" not in rendered and "fields=" not in rendered
    prepared = local_key_inputs.prepare(
        rig[2], observed, charge_source=lambda _size: None,
    )
    assert all(secret not in repr(prepared) for secret in VALID.values())
