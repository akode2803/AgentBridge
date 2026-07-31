"""V136 run-scoped outboxes and bounded failed-artifact recovery."""

from __future__ import annotations

import json
import os
import time

from agentbridge.harness.recovery import (
    RecoveryReport,
    _prune_agent_recovery,
    _prune_recovery,
    archive_outbox,
    prepare_outbox,
    retain_paths,
)


def test_concurrent_run_outboxes_are_isolated_and_failure_is_recoverable(tmp_path):
    workdir = tmp_path / "workspace"
    first, _ = prepare_outbox(workdir, "first")
    (first / "one.bin").write_bytes(b"one")

    second, _ = prepare_outbox(workdir, "second")
    (second / "two.bin").write_bytes(b"two")
    assert (first / "one.bin").is_file()  # same-process live run untouched

    archive_outbox(first, "failed")
    third, report = prepare_outbox(workdir, "third")
    assert third.is_dir() and (second / "two.bin").is_file()
    assert any(path.endswith("one.bin") for path in report.retained)
    assert "NOT resent automatically" in report.prompt_text()


def test_abandoned_process_run_is_recovered_but_other_chat_never_is(tmp_path):
    work_a = tmp_path / "chat-a"
    work_b = tmp_path / "chat-b"
    stale, _ = prepare_outbox(work_a, "stale")
    (stale / "a.txt").write_text("a", encoding="utf-8")
    marker = stale.parent / ".active.json"
    marker.write_text(json.dumps({"pid": os.getpid() + 100000}), encoding="utf-8")
    live_b, _ = prepare_outbox(work_b, "live")
    (live_b / "b.txt").write_text("b", encoding="utf-8")

    _, report = prepare_outbox(work_a, "next")
    assert any(path.endswith("a.txt") for path in report.retained)
    assert (live_b / "b.txt").read_text(encoding="utf-8") == "b"
    assert not list((work_a / "recovery").rglob("b.txt"))


def test_recovery_prunes_by_age_count_and_bytes_with_visible_names(tmp_path):
    workdir = tmp_path / "workspace"
    recovery = workdir / "recovery" / "event"
    recovery.mkdir(parents=True)
    old = recovery / "old.bin"
    middle = recovery / "middle.bin"
    newest = recovery / "newest.bin"
    for path in (old, middle, newest):
        path.write_bytes(b"xxxx")
    now = time.time()
    os.utime(old, (now - 1000, now - 1000))
    os.utime(middle, (now - 2, now - 2))
    os.utime(newest, (now - 1, now - 1))

    pruned = _prune_recovery(
        workdir, max_age_s=100, max_bytes=4, max_files=1)
    assert newest.is_file()
    assert not old.exists() and not middle.exists()
    assert any(name.endswith("old.bin") for name in pruned)
    assert any(name.endswith("middle.bin") for name in pruned)


def test_only_managed_run_paths_are_retained(tmp_path):
    workdir = tmp_path / "workspace"
    outbox, _ = prepare_outbox(workdir, "run")
    managed = outbox / "managed.bin"
    external = tmp_path / "external.bin"
    managed.write_bytes(b"managed")
    external.write_bytes(b"external")

    retained = retain_paths([str(managed), str(external)], "over-limit")
    assert any(path.name == "managed.bin" for path in retained)
    assert external.read_bytes() == b"external"


def test_agent_wide_prune_does_not_disclose_other_chat_names(tmp_path):
    work_a = tmp_path / "workspaces" / "chat-a"
    work_b = tmp_path / "workspaces" / "chat-b"
    file_a = work_a / "recovery" / "a" / "current.bin"
    file_b = work_b / "recovery" / "b" / "other-secret-name.bin"
    file_a.parent.mkdir(parents=True)
    file_b.parent.mkdir(parents=True)
    file_a.write_bytes(b"aaaa")
    file_b.write_bytes(b"bbbb")
    now = time.time()
    os.utime(file_b, (now - 10, now - 10))

    current, elsewhere = _prune_agent_recovery(
        work_a, max_bytes=4, max_files=1)
    assert current == [] and elsewhere == 1
    assert file_a.is_file() and not file_b.exists()
    notice = RecoveryReport(pruned_elsewhere=elsewhere).prompt_text()
    assert "other chat workspaces" in notice
    assert "other-secret-name" not in notice
