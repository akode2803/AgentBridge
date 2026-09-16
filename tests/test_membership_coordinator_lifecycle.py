"""R214 lifecycle publication and final-fence coordinator regressions."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from agentbridge import crypto
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.mesh import authority_source, events
from agentbridge.mesh.membership_coordinator import run_membership_round
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, lifecycle_inputs
from agentbridge.transport import authority_observation
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "lifecycle-room"
SUBJECT = "aryan"
OUTER = "claude"


@pytest.fixture
def lifecycle_world(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    provider.cache_key = "r214-cache"
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror._mirror_root_identity = "r214-root"
    mirror._mirror_cache_identity = "r214-cache"
    mesh = Mesh(mirror, "aryan", "r214-box", home=tmp_path / "home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_agent(OUTER)
        snapshot = ChatSnapshot(
            id=CHAT, kind=ChatKind.GROUP, name="Lifecycle",
            members={
                "aryan": Member(Role.ADMIN, 1),
                OUTER: Member(Role.MEMBER, 1),
            },
            materialized_ns=1,
            tenure={"aryan": [[1, 0]], OUTER: [[1, 0]]},
        )
        mirror.put_doc(P.meta(CHAT), snapshot.to_dict())
        event = {
            "id": "remove-agent", "ns": 2, "from": OUTER, "kind": "info",
            "event": {"type": events.EV_MEMBER_REMOVED, "who": OUTER},
        }
        event["sig"] = crypto.sign(
            mesh.keystore.load(OUTER), events.signing_bytes(CHAT, event),
        )
        mesh.store.upsert_messages(CHAT, [event])
        # Force genuine recursive lifecycle evaluation to propose the inner
        # owner before the outer agent. Account creation had retained Aryan's
        # bootstrap through the canonical path, so retire only that cache row.
        with mesh.store._conn() as conn:
            conn.execute(
                "DELETE FROM docs WHERE path=?",
                (lifecycle_heads.PREFIX + SUBJECT,),
            )
        mesh.store.prepare_membership_suffix_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        mirror.refresh()
        target = f"{CHAT}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"
        mesh.store.refresh_terminal_observation(target)
        receipt = authority_source.publish_authority_source(mirror, mesh.store, CHAT)
        yield mesh, mirror, receipt
    finally:
        mesh.close()


def _assert_write_lock_available(store):
    probe = sqlite3.connect(store.path, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
    finally:
        probe.close()


def test_signed_recursive_lifecycle_publishes_first_postorder_without_sql_lock(
        lifecycle_world, monkeypatch):
    mesh, _mirror, receipt = lifecycle_world
    original_resolve = mesh.key_pins.resolve_observed
    original_verify = crypto.verify
    original_subject = authority_source.capture_authority_subject
    observed = {"pins": 0, "crypto": 0}
    captured_subjects = []

    def resolve_without_sql(*args, **kwargs):
        _assert_write_lock_available(mesh.store)
        observed["pins"] += 1
        return original_resolve(*args, **kwargs)

    def verify_without_sql(*args, **kwargs):
        _assert_write_lock_available(mesh.store)
        observed["crypto"] += 1
        return original_verify(*args, **kwargs)

    def capture_subject(*args, **kwargs):
        captured_subjects.append(args[3])
        return original_subject(*args, **kwargs)

    monkeypatch.setattr(mesh.key_pins, "resolve_observed", resolve_without_sql)
    monkeypatch.setattr(crypto, "verify", verify_without_sql)
    monkeypatch.setattr(authority_source, "capture_authority_subject", capture_subject)
    result = run_membership_round(mesh, receipt)

    assert (result.status, result.reason) == ("restart", "retained_head_progress")
    assert observed["pins"] >= 1 and observed["crypto"] >= 1
    assert captured_subjects[:2] == [OUTER, SUBJECT]
    assert mesh.store.observe_lifecycle_head(SUBJECT).payload_json is not None
    assert mesh.store.observe_lifecycle_head(OUTER).payload_json is None


def test_coordinator_holds_mirror_until_real_head_commit(lifecycle_world, monkeypatch):
    mesh, mirror, receipt = lifecycle_world
    original_publish = lifecycle_inputs.publish_head_in_transaction
    entered = threading.Event()
    finished = threading.Event()
    errors = []
    threads = []
    before_commit = []

    def change_policy():
        try:
            entered.set()
            mirror._record_failure(ConnectionError("offline"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def publish_then_race(conn, path, expected, proposed_json, fetched_ns):
        assert not mirror._lock.acquire(blocking=False)
        assert original_publish(conn, path, expected, proposed_json, fetched_ns)
        reader = sqlite3.connect(mesh.store.path)
        try:
            before_commit.append(reader.execute(
                "SELECT payload FROM docs WHERE path=?",
                (lifecycle_heads.PREFIX + SUBJECT,),
            ).fetchone())
        finally:
            reader.close()
        thread = threading.Thread(target=change_policy)
        threads.append(thread)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.1)
        return True

    monkeypatch.setattr(lifecycle_inputs, "publish_head_in_transaction", publish_then_race)
    result = run_membership_round(mesh, receipt)
    for thread in threads:
        thread.join(5)

    assert (result.status, result.reason) == ("restart", "retained_head_progress")
    assert before_commit == [None]
    assert finished.is_set() and not errors and all(not t.is_alive() for t in threads)
    assert mesh.store.observe_lifecycle_head(SUBJECT).payload_json is not None
    assert authority_observation.matches_lookup_policy(mirror,
        authority_observation.capture_lookup_policy(mirror, CHAT, ()))


def _install_trigger(mesh, receipt, mutation):
    head = lifecycle_heads.PREFIX + SUBJECT
    outer = lifecycle_heads.PREFIX + OUTER
    source = receipt.position.source_id
    target = f"{CHAT}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"
    actions = {
        "payload": f"UPDATE docs SET payload='corrupt' WHERE path='{head}';",
        "head": (
            "INSERT INTO docs(path,payload,fetched_ns) VALUES"
            f"('{outer}','triggered',1) ON CONFLICT(path) DO UPDATE SET payload='triggered';"
        ),
        "source": (
            "UPDATE document_observation_sources SET generation=generation+1 "
            f"WHERE source_id='{source}';"
        ),
        "message": (
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES"
            f"('{CHAT}','triggered',3,'aryan','info','{{}}');"
        ),
        "terminal": (
            "INSERT INTO outbox(kind,target,payload,created_ns,state) VALUES"
            f"('doc','{target}','{{}}',1,'pending');"
        ),
    }
    with mesh.store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER r214_after_head AFTER INSERT ON docs "
            f"WHEN NEW.path='{head}' BEGIN {actions[mutation]} END"
        )


@pytest.mark.parametrize("mutation", ["payload", "source", "message", "terminal"])
def test_post_cas_trigger_mutations_fail_final_fences_and_rollback(
        lifecycle_world, mutation):
    mesh, _mirror, receipt = lifecycle_world
    _install_trigger(mesh, receipt, mutation)
    source_before = mesh.store.capture_document_position(receipt.position.source_id)
    membership_before = mesh.store.capture_membership_input_position(CHAT)

    result = run_membership_round(mesh, receipt)

    assert result.status == "unavailable", result
    assert mesh.store.observe_lifecycle_head(SUBJECT).payload_json is None
    assert mesh.store.observe_lifecycle_head(OUTER).payload_json is None
    assert mesh.store.capture_document_position(receipt.position.source_id) == source_before
    assert mesh.store.capture_membership_input_position(CHAT) == membership_before


def test_post_cas_trigger_mutating_consumed_sibling_head_rolls_back(
        lifecycle_world):
    mesh, mirror, receipt = lifecycle_world

    # The first genuine postorder step retains the owner's head.  Recapture all
    # receipts, then the outer agent proposal consumes both that retained owner
    # head and its own absent head.
    first = run_membership_round(mesh, receipt)
    assert (first.status, first.reason) == ("restart", "retained_head_progress")
    mirror.refresh()
    receipt = authority_source.publish_authority_source(mirror, mesh.store, CHAT)
    owner_before = mesh.store.observe_lifecycle_head(SUBJECT)
    outer_before = mesh.store.observe_lifecycle_head(OUTER)
    assert owner_before.payload_json is not None
    assert outer_before.payload_json is None

    with mesh.store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER r214_mutate_consumed_sibling AFTER INSERT ON docs "
            f"WHEN NEW.path='{lifecycle_heads.PREFIX + OUTER}' BEGIN "
            "UPDATE docs SET payload='triggered' "
            f"WHERE path='{lifecycle_heads.PREFIX + SUBJECT}'; END"
        )

    result = run_membership_round(mesh, receipt)

    assert result.status == "unavailable", result
    assert mesh.store.observe_lifecycle_head(SUBJECT) == owner_before
    assert mesh.store.observe_lifecycle_head(OUTER) == outer_before


def test_baseexception_after_head_cas_rolls_back_and_releases_all_locks(
        lifecycle_world, monkeypatch):
    mesh, mirror, receipt = lifecycle_world
    original_publish = lifecycle_inputs.publish_head_in_transaction
    original_capture = lifecycle_inputs.capture_heads
    published = False

    def publish_then_mark(*args, **kwargs):
        nonlocal published
        result = original_publish(*args, **kwargs)
        published = True
        return result

    def interrupt_postwrite_capture(*args, **kwargs):
        if published:
            raise KeyboardInterrupt("post-CAS interruption")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(lifecycle_inputs, "publish_head_in_transaction", publish_then_mark)
    monkeypatch.setattr(lifecycle_inputs, "capture_heads", interrupt_postwrite_capture)

    with pytest.raises(KeyboardInterrupt, match="post-CAS interruption"):
        run_membership_round(mesh, receipt)

    assert published
    assert mesh.store.observe_lifecycle_head(SUBJECT).payload_json is None
    assert mirror._lock.acquire(blocking=False)
    mirror._lock.release()
    _assert_write_lock_available(mesh.store)


def test_policy_rejection_before_cas_rolls_back_and_releases_locks(
        lifecycle_world, monkeypatch):
    mesh, mirror, receipt = lifecycle_world
    reached_final = False
    original_heads = lifecycle_inputs.matches_heads
    original_policy = authority_observation._locked_matching_lookup_policy

    def mark_final(*args, **kwargs):
        nonlocal reached_final
        matched = original_heads(*args, **kwargs)
        reached_final = True
        return matched

    @contextmanager
    def changed_policy(transport, expected):
        if reached_final:
            yield False
        else:
            with original_policy(transport, expected) as matched:
                yield matched

    monkeypatch.setattr(lifecycle_inputs, "matches_heads", mark_final)
    monkeypatch.setattr(
        authority_observation, "_locked_matching_lookup_policy", changed_policy,
    )
    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("unavailable", "lookup_policy_changed")
    assert mesh.store.observe_lifecycle_head(SUBJECT).payload_json is None
    assert mirror._lock.acquire(blocking=False)
    mirror._lock.release()
    _assert_write_lock_available(mesh.store)
