"""Verified transcript-overlay assembly from one bounded page-input cut."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge import crypto
from agentbridge.core.models import BodyRecord, ChatKind, ChatSnapshot, Envelope, Member, MsgKind, Role
from agentbridge.mesh.events import reaction_signing_bytes, state_signing_bytes
from agentbridge.mesh.messaging import MessagingService
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.overlays import UserState
from agentbridge.mesh.page_overlays import (
    PageOverlayProofsPending,
    PageOverlaysUnavailable,
    assemble_page_overlays,
)
from agentbridge.mesh.page_selection import select_message_page
from agentbridge.mesh.paths import P
from agentbridge.mesh.readmodel import build_messages, transcript_visible
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.store.db import Store


CHAT = "room"
VIEWER = "viewer"


class Directory:
    def __init__(self, pubs, *, mutate=None):
        self.pubs = dict(pubs)
        self.calls = []
        self.mutate = mutate

    def sign_pub(self, actor):
        self.calls.append(actor)
        if self.mutate is not None:
            self.mutate(actor)
        value = self.pubs.get(actor)
        if isinstance(value, BaseException):
            raise value
        return value


class Documents:
    def __init__(self, docs):
        self.docs = docs

    def get_doc(self, path, default=None):
        return self.docs.get(path, default)


def _snapshot(*members, tenure=None):
    return ChatSnapshot(
        id=CHAT, kind=ChatKind.GROUP,
        members={name: Member(role=Role.MEMBER, joined_ns=1) for name in members},
        tenure=tenure or {},
    )


def _reaction(actor, bundle, mapping, ns=11):
    return {
        "v": mapping, "ns": ns,
        "sig": crypto.sign(bundle, reaction_signing_bytes(CHAT, actor, ns, mapping)),
    }


def _state(bundle, fields, ns=12):
    return {
        **fields, "ns": ns,
        "sig": crypto.sign(bundle, state_signing_bytes(CHAT, VIEWER, ns, fields)),
    }


def _message(ident, ns):
    return {"id": ident, "ns": ns, "from": "alice", "kind": "message"}


def _capture(tmp_path, documents, *, ids=("m1", "m2"), proof_keys=(), records=None):
    store = Store(tmp_path / "store.sqlite")
    store.prepare_page_input_index()
    source = store.publish_document_batch(
        store.capture_document_position("source"), documents,
        cursor=1, full=True, retain_tombstones=False,
    )
    index = store.publish_overlay_index(prepare_overlay_index(
        store.capture_document_observation(source.source_id), CHAT,
    ))
    store.upsert_messages(
        CHAT,
        records if records is not None else [
            _message(ident, n + 1) for n, ident in enumerate(ids)
        ],
    )
    inputs = store.capture_page_inputs(
        index, raw_limit=len(ids), include_reactions=True,
        state_paths=(P.state(CHAT, VIEWER),), proof_keys=proof_keys,
    )
    return store, index, inputs


def _prove_and_recapture(store, index, keys, count=2):
    for path, pub in keys:
        store.verify_overlay_signature(index, path, pub)
    return store.capture_page_inputs(
        index, raw_limit=count, include_reactions=True,
        state_paths=(P.state(CHAT, VIEWER),), proof_keys=tuple(keys),
    )


def test_crypto_reactions_and_state_request_exact_proofs_then_match_legacy_rules(tmp_path):
    alice, viewer, departed, outsider = [crypto.generate_identity() for _ in range(4)]
    pubs = {
        "alice": crypto.identity_pubs(alice)[0],
        VIEWER: crypto.identity_pubs(viewer)[0],
        "departed": crypto.identity_pubs(departed)[0],
        "outsider": crypto.identity_pubs(outsider)[0],
    }
    docs = {
        P.reactions(CHAT, "alice"): _reaction("alice", alice, {"m1": "👍"}),
        P.reactions(CHAT, "departed"): _reaction("departed", departed, {"m1": "👀"}),
        P.reactions(CHAT, "outsider"): _reaction("outsider", outsider, {"m1": "❌"}),
        P.state(CHAT, VIEWER): _state(viewer, {
            "hidden": ["m1"], "starred": ["m2"],
            "cleared": {"ns": 1, "keep_starred": True}, "deleted": 0,
        }),
    }
    store, index, inputs = _capture(tmp_path, docs)
    directory = Directory(pubs)
    snapshot = _snapshot("alice", VIEWER, tenure={"departed": [[1, 9]]})
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            assemble_page_overlays(inputs, VIEWER, snapshot, directory=directory)
        assert directory.calls == ["alice", "departed", "outsider", VIEWER]
        assert pending.value.keys == (
            (P.reactions(CHAT, "alice"), pubs["alice"]),
            (P.reactions(CHAT, "departed"), pubs["departed"]),
            (P.state(CHAT, VIEWER), pubs[VIEWER]),
        )

        captured = _prove_and_recapture(store, index, pending.value.keys)
        result = assemble_page_overlays(
            captured, VIEWER, snapshot, directory=Directory(pubs),
        )
        assert result.reactions == {"m1": {"👍": ["alice"], "👀": ["departed"]}}
        assert result.state == {
            "cleared": {"ns": 1, "keep_starred": True}, "deleted": 0,
            "hidden": ["m1"], "starred": ["m2"],
        }
        assert result.key_dependencies == (
            ("alice", pubs["alice"]), ("departed", pubs["departed"]),
            ("outsider", pubs["outsider"]), (VIEWER, pubs[VIEWER]),
        )
        legacy = MessagingService._verified_reactions(
            SimpleNamespace(
                _crypto_boundary=lambda: True,
                directory=Directory(pubs),
                _ever_member=lambda snap, user: (
                    snap.is_member(user) or user in snap.tenure
                ),
            ),
            CHAT, snapshot,
            SimpleNamespace(reaction_docs=lambda: {
                path.rsplit("/", 1)[-1][:-5]: doc
                for path, doc in docs.items() if "/reactions/" in path
            }),
        )
        assert result.reactions == legacy
        legacy_state = UserState(
            Documents(docs), CHAT, VIEWER,
            verifier=lambda doc: crypto.verify(
                pubs[VIEWER], doc.get("sig") or "",
                state_signing_bytes(
                    CHAT, VIEWER, int(doc.get("ns", 0)), UserState.signed_fields(doc),
                ),
            ),
        ).get()
        assert result.state == {
            key: legacy_state[key]
            for key in ("cleared", "deleted", "hidden", "starred")
        }
    finally:
        store.close()


def test_unsigned_never_member_nondict_and_directory_failure_order(tmp_path):
    member = crypto.generate_identity()
    never = crypto.generate_identity()
    viewer = crypto.generate_identity()
    pubs = {name: crypto.identity_pubs(bundle)[0] for name, bundle in (
        ("member", member), ("never", never), (VIEWER, viewer),
    )}
    docs = {
        P.reactions(CHAT, "member"): {"v": {"m1": "u"}, "ns": 1, "sig": ""},
        P.reactions(CHAT, "never"): _reaction("never", never, {"m1": "n"}),
        P.reactions(CHAT, "nondict"): ["not", "a", "document"],
        P.state(CHAT, VIEWER): {},
    }
    store, _index, inputs = _capture(tmp_path, docs)
    directory = Directory({**pubs, "member": RuntimeError("pin unavailable")})
    try:
        with pytest.raises(RuntimeError, match="pin unavailable"):
            assemble_page_overlays(
                inputs, VIEWER, _snapshot("member", VIEWER), directory=directory,
            )
        assert directory.calls == ["member"]

        unrelated = Directory({**pubs, "never": RuntimeError("unrelated unavailable")})
        with pytest.raises(RuntimeError, match="unrelated unavailable"):
            assemble_page_overlays(
                inputs, VIEWER, _snapshot("member", VIEWER), directory=unrelated,
            )
        assert unrelated.calls == ["member", "never"]

        directory = Directory(pubs)
        result = assemble_page_overlays(
            inputs, VIEWER, _snapshot("member", VIEWER), directory=directory,
        )
        assert directory.calls == ["member", "never"]
        assert result.reactions == {} and result.state == {}
    finally:
        store.close()


def test_malformed_reaction_signing_input_is_unavailable_after_legacy_gates(tmp_path):
    viewer = crypto.generate_identity()
    store, _index, inputs = _capture(tmp_path, {
        P.reactions(CHAT, "alice"): {
            "v": {"m1": "x"}, "ns": "bad", "sig": "truthy",
        },
        P.state(CHAT, VIEWER): {},
    }, ids=("m1",))
    try:
        with pytest.raises(PageOverlaysUnavailable, match="malformed_gated_signing_input"):
            assemble_page_overlays(
                inputs, VIEWER, _snapshot("alice", VIEWER),
                directory=Directory({"alice": crypto.identity_pubs(viewer)[0]}),
            )
    finally:
        store.close()


def test_falsey_malformed_signature_preserves_legacy_ignore_after_key_lookup(tmp_path):
    viewer = crypto.generate_identity()
    pub = crypto.identity_pubs(viewer)[0]
    store, _index, inputs = _capture(tmp_path, {
        P.reactions(CHAT, "alice"): {
            "v": {"m1": "x"}, "ns": 1, "sig": False,
        },
        P.state(CHAT, VIEWER): {},
    }, ids=("m1",))
    directory = Directory({"alice": pub})
    try:
        result = assemble_page_overlays(
            inputs, VIEWER, _snapshot("alice", VIEWER), directory=directory,
        )
        assert directory.calls == ["alice"]
        assert result.reactions == {}
    finally:
        store.close()


def test_unrepresentable_keys_are_ignored_until_legacy_signature_and_membership_gates(
        tmp_path):
    docs = {
        P.reactions(CHAT, "unsigned"): {
            "v": {"m1": "u"}, "ns": 1, "sig": "",
        },
        P.reactions(CHAT, "never"): {
            "v": {"m1": "n"}, "ns": 1, "sig": "truthy",
        },
        P.state(CHAT, VIEWER): {
            "hidden": ["m1"], "ns": 1, "sig": "",
        },
    }
    store, _index, inputs = _capture(tmp_path, docs, ids=("m1",))
    oversized = "x" * 1000
    directory = Directory({
        "unsigned": oversized, "never": object(), VIEWER: oversized,
    })
    try:
        result = assemble_page_overlays(
            inputs, VIEWER, _snapshot("unsigned", VIEWER), directory=directory,
        )
        assert directory.calls == ["never", "unsigned", VIEWER]
        assert result.reactions == {} and result.state == {}
        assert result.key_dependencies == (
            ("never", None), ("unsigned", None), (VIEWER, None),
        )
    finally:
        store.close()


def test_state_absent_empty_false_proof_rotation_and_mixed_ids(tmp_path):
    viewer, rotated = crypto.generate_identity(), crypto.generate_identity()
    pub, rotated_pub = crypto.identity_pubs(viewer)[0], crypto.identity_pubs(rotated)[0]
    snapshot = _snapshot(VIEWER)

    missing_store, _index, missing = _capture(tmp_path / "missing", {}, ids=("m1",))
    empty_store, _index, empty = _capture(
        tmp_path / "empty", {P.state(CHAT, VIEWER): {}}, ids=("m1",),
    )
    try:
        assert assemble_page_overlays(
            missing, VIEWER, snapshot, directory=Directory({VIEWER: pub}),
        ).state == {}
        assert assemble_page_overlays(
            empty, VIEWER, snapshot, directory=Directory({VIEWER: pub}),
        ).state == {}
    finally:
        missing_store.close()
        empty_store.close()

    bad_state = _state(viewer, {"hidden": ["m1"]})
    bad_state["sig"] = crypto.sign(rotated, b"wrong")
    store, index, inputs = _capture(
        tmp_path / "false", {P.state(CHAT, VIEWER): bad_state}, ids=("m1",),
    )
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            assemble_page_overlays(inputs, VIEWER, snapshot, directory=Directory({VIEWER: pub}))
        captured = _prove_and_recapture(store, index, pending.value.keys, count=1)
        assert assemble_page_overlays(
            captured, VIEWER, snapshot, directory=Directory({VIEWER: pub}),
        ).state == {}
        with pytest.raises(PageOverlayProofsPending) as rotated_pending:
            assemble_page_overlays(
                captured, VIEWER, snapshot, directory=Directory({VIEWER: rotated_pub}),
            )
        assert rotated_pending.value.keys == ((P.state(CHAT, VIEWER), rotated_pub),)
    finally:
        store.close()

    mixed = _state(viewer, {"hidden": ["m1", 7]})
    store, index, inputs = _capture(
        tmp_path / "mixed", {P.state(CHAT, VIEWER): mixed}, ids=("m1",),
    )
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            assemble_page_overlays(inputs, VIEWER, snapshot, directory=Directory({VIEWER: pub}))
        captured = _prove_and_recapture(store, index, pending.value.keys, count=1)
        with pytest.raises(PageOverlaysUnavailable, match="viewer_ids_not_representable"):
            assemble_page_overlays(
                captured, VIEWER, snapshot, directory=Directory({VIEWER: pub}),
            )
    finally:
        store.close()


def test_inputs_are_detached_before_key_callback_and_plaintext_matches_presence(tmp_path):
    alice, viewer = crypto.generate_identity(), crypto.generate_identity()
    pub_a, pub_v = crypto.identity_pubs(alice)[0], crypto.identity_pubs(viewer)[0]
    docs = {
        P.reactions(CHAT, "alice"): _reaction("alice", alice, {"m1": "👍"}),
        P.state(CHAT, VIEWER): _state(viewer, {"hidden": ["m1"]}),
    }
    store, index, inputs = _capture(tmp_path, docs, ids=("m1",))
    snapshot = _snapshot("alice", VIEWER)
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            assemble_page_overlays(inputs, VIEWER, snapshot, directory=Directory({
                "alice": pub_a, VIEWER: pub_v,
            }))
        captured = _prove_and_recapture(store, index, pending.value.keys, count=1)
        original = captured.indexed

        def mutate(_actor):
            object.__setattr__(captured, "indexed", replace(original, candidates=()))
            snapshot.members.clear()
            snapshot.tenure.clear()

        result = assemble_page_overlays(
            captured, VIEWER, snapshot,
            directory=Directory({"alice": pub_a, VIEWER: pub_v}, mutate=mutate),
        )
        assert result.reactions == {"m1": {"👍": ["alice"]}}
        assert result.state["hidden"] == ["m1"]
        assert snapshot.members == {}

        plaintext = assemble_page_overlays(
            inputs, VIEWER, _snapshot("alice", VIEWER), crypto_boundary=False,
        )
        assert plaintext.reactions == {"m1": {"👍": ["alice"]}}
        assert plaintext.state["hidden"] == ["m1"]
        assert plaintext.key_dependencies == ()
    finally:
        store.close()


def test_verified_capture_assemble_and_selection_matches_full_fold_tail(tmp_path):
    alice, viewer = crypto.generate_identity(), crypto.generate_identity()
    pub_a, pub_v = crypto.identity_pubs(alice)[0], crypto.identity_pubs(viewer)[0]
    sealer = PlainSealer()
    records = []
    for ns, ident in enumerate(("m1", "m2", "m3", "m4"), 1):
        sealed = sealer.seal(CHAT, ident, ns, BodyRecord(body=f"body {ident}"))
        records.append(Envelope(
            id=ident, ns=ns, ts="t", from_="alice", kind=MsgKind.MESSAGE,
            epoch=sealed["epoch"], nonce=sealed["nonce"], ct=sealed["ct"],
            sig=sealed["sig"],
        ).to_dict())
    state_fields = {
        "hidden": ["m1"], "starred": ["m2"],
        "cleared": {"ns": 2, "keep_starred": True},
    }
    docs = {
        P.reactions(CHAT, "alice"): _reaction("alice", alice, {"m3": "👍"}),
        P.state(CHAT, VIEWER): _state(viewer, state_fields),
    }
    store, index, inputs = _capture(
        tmp_path, docs, ids=("m1", "m2", "m3", "m4"), records=records,
    )
    snapshot = _snapshot("alice", VIEWER)
    directory = Directory({"alice": pub_a, VIEWER: pub_v})
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            assemble_page_overlays(
                inputs, VIEWER, snapshot, directory=directory,
            )
        captured = _prove_and_recapture(
            store, index, pending.value.keys, count=len(records),
        )
        overlays = assemble_page_overlays(
            captured, VIEWER, snapshot,
            directory=Directory({"alice": pub_a, VIEWER: pub_v}),
        )
        selected = select_message_page(
            captured, VIEWER, sealer, limit=len(records),
            reactions=overlays.reactions, state=overlays.state,
        )
        expected = [
            message for message in build_messages(
                CHAT, VIEWER, records, sealer,
                reactions=overlays.reactions, state=overlays.state,
            )
            if transcript_visible(message, VIEWER)
        ][-len(records):]
        assert selected.messages == tuple(expected)
        assert [message.id for message in selected.messages] == ["m2", "m3", "m4"]
        assert selected.messages[1].reactions == {"👍": ["alice"]}
    finally:
        store.close()
