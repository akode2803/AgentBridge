"""Store contracts for generation-bound overlay candidates and pure proofs."""

from __future__ import annotations

import hashlib
import json

import pytest

from agentbridge import crypto
from agentbridge.store import overlay_index
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "mirror:room"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _payload(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reaction_path(actor):
    return f"chats/{CHAT}/overlays/reactions/{actor}.json"


def _state_path(viewer):
    return f"chats/{CHAT}/overlays/state/{viewer}.json"


def _publish_source(store, documents, *, source=SOURCE, expected=None, cursor=1):
    if expected is None:
        expected = store.capture_document_position(source)
    return store.publish_document_batch(
        expected, documents, cursor=cursor, full=True, retain_tombstones=False,
    )


def _indexed_document(path, actor, payload, signing=b"signed-input", bundle=None):
    signature = crypto.sign(bundle, signing) if bundle is not None else ""
    kind = "reactions" if "/reactions/" in path else "state"
    return overlay_index.IndexedDocument(
        path=path,
        kind=kind,
        actor=actor,
        source_digest=hashlib.sha256(payload.encode()).hexdigest(),
        source_bytes=len(payload.encode()),
        signature=signature,
        signing_bytes=signing,
        empty=False,
        shape_error="",
        scalars_json="{}",
    )


def _build(store, position, documents, candidates):
    prepared = overlay_index.PreparedOverlayIndex(
        position, CHAT, tuple(documents), tuple(candidates),
    )
    return store.publish_overlay_index(prepared)


def test_publish_capture_and_key_specific_proof_lifecycle(store):
    path = _reaction_path("alice")
    raw = _payload({"ns": 7, "v": {"m1": "👍"}})
    source = _publish_source(store, {path: json.loads(raw)})
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    other_public, _other_agree = crypto.identity_pubs(crypto.generate_identity())
    doc = _indexed_document(path, "alice", raw, bundle=bundle)
    indexed = _build(store, source, [doc], [
        overlay_index.OverlayCandidate(path, "reaction", "m1", "👍"),
    ])

    selected = store.capture_overlay_index(
        indexed, ("m1",), (_state_path("viewer"),),
    )
    assert selected.position == indexed
    assert selected.documents[0].path == path
    assert selected.candidates == (
        overlay_index.OverlayCandidate(path, "reaction", "m1", "👍"),
    )
    assert selected.absent_states == (_state_path("viewer"),)
    assert store.capture_overlay_proofs(indexed, ((path, public),)) == (None,)

    assert store.verify_overlay_signature(indexed, path, public) is True
    assert store.capture_overlay_proofs(indexed, ((path, public),)) == (True,)
    assert store.verify_overlay_signature(indexed, path, other_public) is False
    assert store.capture_overlay_proofs(
        indexed, ((path, public), (path, other_public)),
    ) == (None, False)
    assert store._conn().execute(
        "SELECT count(*) FROM overlay_index_proofs WHERE source=? AND path=?",
        (SOURCE, path),
    ).fetchone() == (1,)
    prior_keys = [other_public]
    for _ in range(4):
        rotated, _agree = crypto.identity_pubs(crypto.generate_identity())
        assert store.verify_overlay_signature(indexed, path, rotated) is False
        prior_keys.append(rotated)
        assert store._conn().execute(
            "SELECT count(*) FROM overlay_index_proofs "
            "WHERE source=? AND path=?",
            (SOURCE, path),
        ).fetchone() == (1,)
    assert store.capture_overlay_proofs(
        indexed, tuple((path, key) for key in prior_keys),
    ) == (None, None, None, None, False)


def test_source_invalidation_and_reset_reject_ready_index(store):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    indexed = _build(
        store, source, [_indexed_document(path, "alice", raw)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )

    invalid = store.invalidate_document_observation(source)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.capture_overlay_index(indexed, ("m1",))
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.capture_overlay_proofs(indexed, ())

    reset = store.reset_document_observation(invalid)
    assert not reset.initialized
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.capture_overlay_index(indexed, ("m1",))
    rebuilt_source = _publish_source(
        store, {path: json.loads(raw)}, expected=reset, cursor=1,
    )
    rebuilt = _build(
        store, rebuilt_source, [_indexed_document(path, "alice", raw)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )
    assert rebuilt.source.generation > indexed.source.generation
    assert rebuilt.build != indexed.build
    assert store.capture_overlay_index(rebuilt, ("m1",)).candidates[0].value == "x"


def test_same_source_rebuild_nonce_rejects_late_old_proof(store):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    doc = _indexed_document(path, "alice", raw, bundle=bundle)
    candidates = [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")]
    old = _build(store, source, [doc], candidates)
    winner = _build(store, source, [doc], candidates)
    assert winner.source == old.source and winner.build != old.build

    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.capture_overlay_index(old, ("m1",))
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.verify_overlay_signature(old, path, public)
    assert store.capture_overlay_proofs(winner, ((path, public),)) == (None,)
    assert store.verify_overlay_signature(winner, path, public) is True
    assert store.capture_overlay_proofs(winner, ((path, public),)) == (True,)


def test_direct_candidate_mutation_invalidates_ready_build(store):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    indexed = _build(
        store, source, [_indexed_document(path, "alice", raw)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )
    with store._conn() as conn:
        conn.execute(
            "UPDATE overlay_index_candidates SET value='forged' "
            "WHERE source=? AND kind='reaction' AND target='m1'",
            (SOURCE,),
        )
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.capture_overlay_index(indexed, ("m1",))
    assert store._conn().execute(
        "SELECT value FROM overlay_index_candidates WHERE source=?", (SOURCE,),
    ).fetchone() == ("forged",)


def test_publication_requires_complete_supported_source_coverage(store):
    first = _reaction_path("alice")
    second = _state_path("viewer")
    raw_first = _payload({"v": {"m1": "x"}})
    raw_second = _payload({"hidden": ["m1"]})
    source = _publish_source(store, {
        first: json.loads(raw_first), second: json.loads(raw_second),
    })
    with pytest.raises(
        overlay_index.OverlayIndexUnavailable,
        match="incomplete_prepared_documents",
    ):
        _build(store, source, [_indexed_document(first, "alice", raw_first)], [
            overlay_index.OverlayCandidate(first, "reaction", "m1", "x"),
        ])
    assert store._conn().execute(
        "SELECT count(*) FROM overlay_index_ready WHERE source=?", (SOURCE,),
    ).fetchone() == (0,)

    foreign = "accounts/alice.json"
    foreign_source = _publish_source(
        store, {foreign: {"name": "Alice"}}, source="foreign-source",
    )
    prepared = overlay_index.PreparedOverlayIndex(
        foreign_source, CHAT, (), (),
    )
    with pytest.raises(ValueError, match="unsupported overlay path"):
        store.publish_overlay_index(prepared)


def test_fan_in_row_and_byte_budgets_fail_whole_capture(store):
    documents = {}
    indexed_docs = []
    candidates = []
    for index in range(3):
        actor = f"agent{index}"
        path = _reaction_path(actor)
        raw = _payload({"v": {"target": "emoji-" + "x" * 20 + str(index)}})
        documents[path] = json.loads(raw)
        indexed_docs.append(_indexed_document(path, actor, raw))
        candidates.append(overlay_index.OverlayCandidate(
            path, "reaction", "target", "emoji-" + "x" * 20 + str(index),
        ))
    source = _publish_source(store, documents)
    indexed = _build(store, source, indexed_docs, candidates)

    complete = store.capture_overlay_index(indexed, ("target",), max_rows=3)
    assert len(complete.documents) == 3 and len(complete.candidates) == 3
    with pytest.raises(OverflowError, match="selection exceeds budget"):
        store.capture_overlay_index(indexed, ("target",), max_rows=2)
    with pytest.raises(OverflowError, match="byte budget|selection exceeds budget"):
        store.capture_overlay_index(indexed, ("target",), max_bytes=80)
    # Failed bounded reads never alter or consume the ready build.
    assert store.capture_overlay_index(indexed, ("target",)) == complete


def test_rebuild_during_verification_rejects_late_proof(store, monkeypatch):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    doc = _indexed_document(path, "alice", raw, bundle=bundle)
    candidates = [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")]
    old = _build(store, source, [doc], candidates)
    replacement = []
    original_verify = overlay_index.crypto.verify

    def rebuild_then_verify(key, signature, signing):
        replacement.append(_build(store, source, [doc], candidates))
        return original_verify(key, signature, signing)

    monkeypatch.setattr(overlay_index.crypto, "verify", rebuild_then_verify)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.verify_overlay_signature(old, path, public)
    assert len(replacement) == 1 and replacement[0].build != old.build
    assert store.capture_overlay_proofs(replacement[0], ((path, public),)) == (None,)


def test_source_invalidation_during_verification_rejects_proof(store, monkeypatch):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    indexed = _build(
        store, source, [_indexed_document(path, "alice", raw, bundle=bundle)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )
    invalidated = []
    original_verify = overlay_index.crypto.verify

    def invalidate_then_verify(key, signature, signing):
        invalidated.append(store.invalidate_document_observation(source))
        return original_verify(key, signature, signing)

    monkeypatch.setattr(overlay_index.crypto, "verify", invalidate_then_verify)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.verify_overlay_signature(indexed, path, public)
    assert len(invalidated) == 1 and not invalidated[0].initialized
    assert store._conn().execute(
        "SELECT count(*) FROM overlay_index_proofs WHERE source=?", (SOURCE,),
    ).fetchone() == (0,)


def test_candidate_base_exception_rolls_back_to_complete_old_index(store):
    path = _reaction_path("alice")
    raw = _payload({"v": {"old": "x", "new": "y"}})
    source = _publish_source(store, {path: json.loads(raw)})
    doc = _indexed_document(path, "alice", raw)
    old = _build(store, source, [doc], [
        overlay_index.OverlayCandidate(path, "reaction", "old", "x"),
    ])
    before = store.capture_overlay_index(old, ("old", "new"))
    prepared = overlay_index.PreparedOverlayIndex(source, CHAT, (doc,), (
        overlay_index.OverlayCandidate(path, "reaction", "old", "changed"),
        overlay_index.OverlayCandidate(path, "reaction", "new", "y"),
    ))
    real = store._conn()

    class InterruptingConnection:
        def __init__(self, conn):
            self.conn = conn

        @property
        def in_transaction(self):
            return self.conn.in_transaction

        def execute(self, sql, parameters=()):
            return self.conn.execute(sql, parameters)

        def executemany(self, sql, rows):
            rows = list(rows)
            if sql.startswith("INSERT INTO overlay_index_candidates"):
                self.conn.execute(sql, rows[0])
                raise KeyboardInterrupt("candidate insertion interrupted")
            return self.conn.executemany(sql, rows)

        def commit(self):
            return self.conn.commit()

        def rollback(self):
            return self.conn.rollback()

    store._local.conn = InterruptingConnection(real)
    try:
        with pytest.raises(KeyboardInterrupt, match="candidate insertion interrupted"):
            store.publish_overlay_index(prepared)
        assert not real.in_transaction
    finally:
        store._local.conn = real
    assert store.capture_overlay_index(old, ("old", "new")) == before


def test_verifier_base_exception_writes_no_false_proof(store, monkeypatch):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    indexed = _build(
        store, source, [_indexed_document(path, "alice", raw, bundle=bundle)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )

    def interrupted(*_args):
        raise KeyboardInterrupt("verification interrupted")

    monkeypatch.setattr(overlay_index.crypto, "verify", interrupted)
    with pytest.raises(KeyboardInterrupt, match="verification interrupted"):
        store.verify_overlay_signature(indexed, path, public)
    assert store.capture_overlay_proofs(indexed, ((path, public),)) == (None,)


@pytest.mark.parametrize("damage", ["trigger", "table"])
def test_owned_index_schema_tamper_is_unavailable(store, damage):
    path = _reaction_path("alice")
    raw = _payload({"v": {"m1": "x"}})
    source = _publish_source(store, {path: json.loads(raw)})
    indexed = _build(
        store, source, [_indexed_document(path, "alice", raw)],
        [overlay_index.OverlayCandidate(path, "reaction", "m1", "x")],
    )
    with store._conn() as conn:
        if damage == "trigger":
            conn.execute("DROP TRIGGER overlay_index_dirty_candidates_update")
        else:
            conn.execute("ALTER TABLE overlay_index_candidates ADD COLUMN forged TEXT")
    expected = "index_triggers_changed" if damage == "trigger" else "index_schema_changed"
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match=expected):
        store.capture_overlay_index(indexed, ("m1",))
