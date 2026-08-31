"""Resume R0: secret-free atomic development checkpoint foundations."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from devtools.task_checkpoint import (
    ZERO_DIGEST, CheckpointError, StaleCheckpointError, audit_checkpoint,
    repair_journal, validate_active, validate_payload, write_checkpoint,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    tracked = root / "tracked.txt"
    tracked.write_text("baseline\n", encoding="ascii")
    agreement = root / "WORKING_AGREEMENT.md"
    agreement.write_text("rules\n", encoding="ascii")
    _git(root, "add", "tracked.txt", "WORKING_AGREEMENT.md")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _payload(root: Path, *, expected_digest: str = "", objective: str = "Continue work"):
    head = _git(root, "rev-parse", "HEAD")
    common = _git(root, "rev-parse", "--git-common-dir")
    common_path = str((root / common).resolve())
    agreement = root / "WORKING_AGREEMENT.md"
    return {
        "schema_version": 1,
        "identity": {
            "task_id": str(uuid.uuid4()),
            "workspace": str(root.resolve()),
            "created_at": "2026-08-31T00:00:00Z",
        },
        "request_ref": {
            "source_task_id": "task-local-1",
            "objective": objective,
            "request_sha256": hashlib.sha256(b"request").hexdigest(),
            "observed_at": "2026-08-31T00:00:00Z",
        },
        "agreement": {
            "path": str(agreement.resolve()),
            "sha256": _sha(agreement),
            "observed_at": "2026-08-31T00:00:00Z",
        },
        "git_baseline": {
            "worktree": str(root.resolve()),
            "common_dir": common_path,
            "branch": _git(root, "branch", "--show-current"),
            "detached": False,
            "base_head": head,
            "expected_head": head,
            "upstream_ref": "",
            "upstream_oid": "",
            "remote_digest": "",
        },
        "workspace_entries": [{
            "path": "tracked.txt",
            "ownership": "task",
            "tracked": True,
            "mode": "100644",
            "baseline_digest": _sha(root / "tracked.txt"),
            "expected_digest": expected_digest,
        }],
        "plan": [{
            "id": "P0", "state": "in_progress",
            "dependencies": [], "evidence_refs": [],
        }],
        "evidence": [],
        "process_observations": [],
        "live_resources": [],
        "approvals": [],
        "recovery": {
            "interrupted": False,
            "next_action": {
                "kind": "run_test", "target": "focused checkpoint tests",
                "prerequisites": ["validate working tree"],
            },
            "blocked_reason": "",
        },
    }


def test_atomic_write_digest_chain_and_journal_repair(workspace, tmp_path):
    state = tmp_path / "state"
    first = write_checkpoint(
        _payload(workspace), state, expect_seq=0, expect_digest=ZERO_DIGEST)
    assert validate_active(first) is first
    assert first["integrity"]["checkpoint_seq"] == 1
    assert first["integrity"]["previous_checkpoint_digest"] == ZERO_DIGEST
    assert json.loads((state / "journal.jsonl").read_text().splitlines()[0])[
        "checkpoint_digest"] == first["integrity"]["checkpoint_digest"]

    journal = state / "journal.jsonl"
    journal.unlink()
    assert repair_journal(state) is True
    assert repair_journal(state) is False

    second = write_checkpoint(
        _payload(workspace), state,
        expect_seq=1, expect_digest=first["integrity"]["checkpoint_digest"])
    assert second["integrity"]["checkpoint_seq"] == 2
    assert second["integrity"]["previous_checkpoint_digest"] == \
        first["integrity"]["checkpoint_digest"]


def test_stale_compare_and_swap_allows_exactly_one_writer(workspace, tmp_path):
    state = tmp_path / "state"
    first = write_checkpoint(
        _payload(workspace), state, expect_seq=0, expect_digest=ZERO_DIGEST)

    def update(label):
        payload = _payload(workspace, objective=f"Continue {label}")
        return write_checkpoint(
            payload, state, expect_seq=1,
            expect_digest=first["integrity"]["checkpoint_digest"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = []
        for future in (pool.submit(update, "one"), pool.submit(update, "two")):
            try:
                outcomes.append(("ok", future.result()))
            except StaleCheckpointError:
                outcomes.append(("stale", None))
    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("stale") == 1
    assert validate_active(json.loads((state / "active.json").read_text()))


def test_validation_rejects_unknown_and_secret_material(workspace):
    payload = _payload(workspace)
    payload["unknown"] = True
    with pytest.raises(CheckpointError, match="unknown fields"):
        validate_payload(payload)

    payload = _payload(workspace, objective="use token=do-not-store-this")
    with pytest.raises(CheckpointError, match="secret material"):
        validate_payload(payload)

    payload = _payload(workspace)
    payload["process_observations"] = [{
        "role": "gui", "pid": 1, "start_token": "start",
        "executable_digest": "", "endpoint": "127.0.0.1:7787",
        "instance_id": "instance", "observed_at": "now", "argv": "forbidden",
    }]
    with pytest.raises(CheckpointError, match="unknown fields"):
        validate_payload(payload)


def test_corrupt_active_and_symlink_destination_fail_closed(workspace, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "active.json").write_text("{bad", encoding="ascii")
    (state / "active.json").chmod(0o600)
    with pytest.raises(CheckpointError, match="JSON is corrupt"):
        write_checkpoint(
            _payload(workspace), state, expect_seq=0, expect_digest=ZERO_DIGEST)

    other = tmp_path / "other"
    other.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(other, target_is_directory=True)
    with pytest.raises(CheckpointError, match="symlink"):
        write_checkpoint(
            _payload(workspace), linked, expect_seq=0, expect_digest=ZERO_DIGEST)


def test_audit_classifies_expected_conflicting_and_unknown_drift(workspace, tmp_path):
    state = tmp_path / "state"
    changed = b"expected\n"
    expected = hashlib.sha256(changed).hexdigest()
    active = write_checkpoint(
        _payload(workspace, expected_digest=expected), state,
        expect_seq=0, expect_digest=ZERO_DIGEST)
    clean = audit_checkpoint(active, offline=True)
    assert next(row for row in clean["observations"]
                if row.get("path") == "tracked.txt")["state"] == "MATCH"
    assert clean["overall"] == "UNAVAILABLE"

    (workspace / "tracked.txt").write_bytes(changed)
    expected_report = audit_checkpoint(active, offline=True)
    assert next(row for row in expected_report["observations"]
                if row.get("path") == "tracked.txt")["state"] == "EXPECTED_DRIFT"

    (workspace / "tracked.txt").write_text("unexpected\n", encoding="ascii")
    (workspace / "new.txt").write_text("new\n", encoding="ascii")
    conflict = audit_checkpoint(active, offline=True)
    assert conflict["overall"] == "CONFLICT"
    assert any(row.get("path") == "tracked.txt"
               for row in conflict["observations"])
    assert not any(row.get("path") == "racked.txt"
                   for row in conflict["observations"])
    assert any(row["state"] == "UNKNOWN" and row.get("path") == "new.txt"
               for row in conflict["observations"])


def test_checkpoint_files_are_private(workspace, tmp_path):
    state = tmp_path / "state"
    write_checkpoint(
        _payload(workspace), state, expect_seq=0, expect_digest=ZERO_DIGEST)
    assert state.stat().st_mode & 0o077 == 0
    for name in ("active.json", "journal.jsonl", ".lock"):
        assert (state / name).stat().st_mode & 0o077 == 0


def test_existing_broad_permissions_and_credential_url_are_rejected(
        workspace, tmp_path):
    payload = _payload(workspace)
    payload["process_observations"] = [{
        "role": "gui", "pid": 1, "start_token": "start",
        "executable_digest": "",
        "endpoint": "https://member:credential@example.invalid/",
        "instance_id": "generation", "observed_at": "now",
    }]
    with pytest.raises(CheckpointError, match="secret material"):
        validate_payload(payload)

    state = tmp_path / "state"
    first = write_checkpoint(
        _payload(workspace), state, expect_seq=0, expect_digest=ZERO_DIGEST)
    (state / "active.json").chmod(0o644)
    with pytest.raises(CheckpointError, match="permissions are too broad"):
        write_checkpoint(
            _payload(workspace), state, expect_seq=1,
            expect_digest=first["integrity"]["checkpoint_digest"])
