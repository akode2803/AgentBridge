"""Secret-free, atomic checkpoints for resumable AgentBridge development.

The active snapshot is authoritative. The append-only journal is repairable
diagnostic history and never decides whether work is complete.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
STATES = {"pending", "in_progress", "completed", "blocked"}
OWNERSHIP = {"task", "user", "generated"}
EVIDENCE_KINDS = {"test", "live", "commit", "push", "review", "measurement"}
AUDIT_STATES = {
    "MATCH", "EXPECTED_DRIFT", "STALE_OBSERVATION", "CONFLICT",
    "UNKNOWN", "UNAVAILABLE",
}
ZERO_DIGEST = "0" * 64
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|recovery[_-]?code)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|"
    r"\bsb_[A-Za-z0-9_-]{12,}|://[^/\s]+@|"
    r"(?:password|secret|token|api[_-]?key)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_SAFE_SECRET_LIKE_KEYS = {"start_token"}


class CheckpointError(ValueError):
    """The checkpoint is malformed, unsafe, stale, or conflicts with reality."""


class StaleCheckpointError(CheckpointError):
    """A compare-and-swap writer observed a newer active snapshot."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _expect_keys(value: Any, required: set[str], optional: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise CheckpointError(f"{where} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise CheckpointError(f"{where} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise CheckpointError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, where: str, *, limit: int = 500, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise CheckpointError(f"{where} must be a non-empty string")
    if value != value.strip() or len(value) > limit or "\x00" in value:
        raise CheckpointError(f"{where} is not canonical or exceeds {limit} characters")
    if _SECRET_VALUE.search(value):
        raise CheckpointError(f"{where} appears to contain secret material")
    return value


def _sha(value: Any, where: str, *, oid: bool = False, empty: bool = False) -> str:
    if empty and value == "":
        return ""
    pattern = _OID if oid else _HEX64
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CheckpointError(f"{where} must be a lowercase digest")
    return value


def _absolute(value: Any, where: str) -> str:
    text = _text(value, where, limit=2000)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise CheckpointError(f"{where} must be an absolute normalized path")
    return str(path)


def _relative(value: Any, where: str) -> str:
    text = _text(value, where, limit=1000)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text in {".", ""}:
        raise CheckpointError(f"{where} must be a safe workspace-relative path")
    return path.as_posix()


def _scan_secret_keys(value: Any, where: str = "checkpoint") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (str(key) not in _SAFE_SECRET_LIKE_KEYS
                    and _SECRET_KEY.search(str(key))):
                raise CheckpointError(f"{where} contains prohibited field {key!r}")
            _scan_secret_keys(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_secret_keys(item, f"{where}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise CheckpointError(f"{where} appears to contain secret material")


def _validate_payload(doc: dict, *, active: bool) -> dict:
    required = {
        "schema_version", "identity", "request_ref", "agreement",
        "git_baseline", "workspace_entries", "plan", "evidence",
        "process_observations", "live_resources", "approvals", "recovery",
    }
    optional = {"integrity"} if active else set()
    _expect_keys(doc, required, optional, "checkpoint")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise CheckpointError("unsupported checkpoint schema version")
    _scan_secret_keys(doc)

    identity = _expect_keys(
        doc["identity"], {"task_id", "workspace", "created_at"}, set(), "identity")
    try:
        uuid.UUID(_text(identity["task_id"], "identity.task_id", limit=64))
    except ValueError as exc:
        raise CheckpointError("identity.task_id must be a UUID") from exc
    _absolute(identity["workspace"], "identity.workspace")
    _text(identity["created_at"], "identity.created_at", limit=64)

    request = _expect_keys(
        doc["request_ref"],
        {"source_task_id", "objective", "request_sha256", "observed_at"},
        set(), "request_ref")
    _text(request["source_task_id"], "request_ref.source_task_id", limit=160)
    _text(request["objective"], "request_ref.objective", limit=500)
    _sha(request["request_sha256"], "request_ref.request_sha256")
    _text(request["observed_at"], "request_ref.observed_at", limit=64)

    agreement = _expect_keys(
        doc["agreement"], {"path", "sha256", "observed_at"}, set(), "agreement")
    _absolute(agreement["path"], "agreement.path")
    _sha(agreement["sha256"], "agreement.sha256")
    _text(agreement["observed_at"], "agreement.observed_at", limit=64)

    git = _expect_keys(
        doc["git_baseline"],
        {"worktree", "common_dir", "branch", "detached", "base_head",
         "expected_head", "upstream_ref", "upstream_oid", "remote_digest"},
        set(), "git_baseline")
    _absolute(git["worktree"], "git_baseline.worktree")
    _absolute(git["common_dir"], "git_baseline.common_dir")
    _text(git["branch"], "git_baseline.branch", limit=300, empty=bool(git["detached"]))
    if not isinstance(git["detached"], bool):
        raise CheckpointError("git_baseline.detached must be boolean")
    for name in ("base_head", "expected_head"):
        _sha(git[name], f"git_baseline.{name}", oid=True)
    _text(git["upstream_ref"], "git_baseline.upstream_ref", limit=300, empty=True)
    _sha(git["upstream_oid"], "git_baseline.upstream_oid", oid=True, empty=True)
    _sha(git["remote_digest"], "git_baseline.remote_digest", empty=True)

    entries = doc["workspace_entries"]
    if not isinstance(entries, list) or len(entries) > 2000:
        raise CheckpointError("workspace_entries must be a bounded list")
    seen_paths = set()
    for index, raw in enumerate(entries):
        entry = _expect_keys(
            raw,
            {"path", "ownership", "tracked", "mode", "baseline_digest",
             "expected_digest"},
            set(), f"workspace_entries[{index}]")
        path = _relative(entry["path"], f"workspace_entries[{index}].path")
        if path in seen_paths:
            raise CheckpointError(f"duplicate workspace entry {path}")
        seen_paths.add(path)
        if entry["ownership"] not in OWNERSHIP:
            raise CheckpointError(f"invalid ownership for {path}")
        if not isinstance(entry["tracked"], bool):
            raise CheckpointError(f"tracked flag for {path} must be boolean")
        _text(entry["mode"], f"workspace_entries[{index}].mode", limit=20)
        _sha(entry["baseline_digest"], f"workspace_entries[{index}].baseline_digest",
             empty=True)
        _sha(entry["expected_digest"], f"workspace_entries[{index}].expected_digest",
             empty=True)

    plan = doc["plan"]
    if not isinstance(plan, list) or not plan or len(plan) > 500:
        raise CheckpointError("plan must be a non-empty bounded list")
    plan_ids = set()
    for index, raw in enumerate(plan):
        item = _expect_keys(
            raw, {"id", "state", "dependencies", "evidence_refs"}, set(),
            f"plan[{index}]")
        item_id = _text(item["id"], f"plan[{index}].id", limit=100)
        if item_id in plan_ids:
            raise CheckpointError(f"duplicate plan id {item_id}")
        plan_ids.add(item_id)
        if item["state"] not in STATES:
            raise CheckpointError(f"invalid plan state for {item_id}")
        for field in ("dependencies", "evidence_refs"):
            if not isinstance(item[field], list) or len(item[field]) > 200:
                raise CheckpointError(f"plan {field} must be a bounded list")
            for value in item[field]:
                _text(value, f"plan[{index}].{field}", limit=160)

    evidence = doc["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 1000:
        raise CheckpointError("evidence must be a bounded list")
    evidence_ids = set()
    for index, raw in enumerate(evidence):
        item = _expect_keys(
            raw,
            {"id", "kind", "command_digest", "started_at", "finished_at",
             "exit_status", "artifact_ref"},
            set(), f"evidence[{index}]")
        evidence_id = _text(item["id"], f"evidence[{index}].id", limit=160)
        if evidence_id in evidence_ids:
            raise CheckpointError(f"duplicate evidence id {evidence_id}")
        evidence_ids.add(evidence_id)
        if item["kind"] not in EVIDENCE_KINDS:
            raise CheckpointError(f"invalid evidence kind for {evidence_id}")
        _sha(item["command_digest"], f"evidence[{index}].command_digest", empty=True)
        _text(item["started_at"], f"evidence[{index}].started_at", limit=64)
        _text(item["finished_at"], f"evidence[{index}].finished_at", limit=64,
              empty=True)
        if item["exit_status"] is not None and not isinstance(item["exit_status"], int):
            raise CheckpointError(f"invalid exit status for {evidence_id}")
        _text(item["artifact_ref"], f"evidence[{index}].artifact_ref", limit=500,
              empty=True)

    processes = doc["process_observations"]
    if not isinstance(processes, list) or len(processes) > 100:
        raise CheckpointError("process_observations must be a bounded list")
    for index, raw in enumerate(processes):
        item = _expect_keys(
            raw,
            {"role", "pid", "start_token", "executable_digest", "endpoint",
             "instance_id", "observed_at"},
            set(), f"process_observations[{index}]")
        _text(item["role"], f"process_observations[{index}].role", limit=80)
        if not isinstance(item["pid"], int) or item["pid"] < 0:
            raise CheckpointError("process pid must be a non-negative integer")
        _text(item["start_token"], f"process_observations[{index}].start_token",
              limit=160)
        _sha(item["executable_digest"],
             f"process_observations[{index}].executable_digest", empty=True)
        _text(item["endpoint"], f"process_observations[{index}].endpoint", limit=300,
              empty=True)
        _text(item["instance_id"], f"process_observations[{index}].instance_id",
              limit=160, empty=True)
        _text(item["observed_at"], f"process_observations[{index}].observed_at",
              limit=64)

    resources = doc["live_resources"]
    if not isinstance(resources, list) or len(resources) > 500:
        raise CheckpointError("live_resources must be a bounded list")
    for index, raw in enumerate(resources):
        item = _expect_keys(
            raw,
            {"adapter", "kind", "opaque_id", "creator_checkpoint",
             "expected_state", "cleanup_required", "last_observed_at",
             "cleanup_evidence_ref"},
            set(), f"live_resources[{index}]")
        for field in ("adapter", "kind", "opaque_id", "creator_checkpoint",
                      "expected_state", "last_observed_at"):
            _text(item[field], f"live_resources[{index}].{field}", limit=200)
        if not isinstance(item["cleanup_required"], bool):
            raise CheckpointError("cleanup_required must be boolean")
        _text(item["cleanup_evidence_ref"],
              f"live_resources[{index}].cleanup_evidence_ref", limit=160, empty=True)

    approvals = doc["approvals"]
    if not isinstance(approvals, list) or len(approvals) > 200:
        raise CheckpointError("approvals must be a bounded list")
    for index, raw in enumerate(approvals):
        item = _expect_keys(
            raw,
            {"scope_digest", "granting_context_digest", "expires_at",
             "observed_state"},
            set(), f"approvals[{index}]")
        _sha(item["scope_digest"], f"approvals[{index}].scope_digest")
        _sha(item["granting_context_digest"],
             f"approvals[{index}].granting_context_digest")
        _text(item["expires_at"], f"approvals[{index}].expires_at", limit=64,
              empty=True)
        _text(item["observed_state"], f"approvals[{index}].observed_state",
              limit=80)

    recovery = _expect_keys(
        doc["recovery"], {"interrupted", "next_action", "blocked_reason"}, set(),
        "recovery")
    if not isinstance(recovery["interrupted"], bool):
        raise CheckpointError("recovery.interrupted must be boolean")
    action = _expect_keys(
        recovery["next_action"], {"kind", "target", "prerequisites"}, set(),
        "recovery.next_action")
    _text(action["kind"], "recovery.next_action.kind", limit=80)
    _text(action["target"], "recovery.next_action.target", limit=500)
    if not isinstance(action["prerequisites"], list) or len(action["prerequisites"]) > 100:
        raise CheckpointError("next action prerequisites must be a bounded list")
    for value in action["prerequisites"]:
        _text(value, "recovery.next_action.prerequisites", limit=300)
    _text(recovery["blocked_reason"], "recovery.blocked_reason", limit=500, empty=True)

    if active:
        integrity = _expect_keys(
            doc["integrity"],
            {"checkpoint_seq", "checkpoint_id", "previous_checkpoint_digest",
             "checkpoint_digest", "journal_through_seq"},
            set(), "integrity")
        if not isinstance(integrity["checkpoint_seq"], int) \
                or integrity["checkpoint_seq"] < 1:
            raise CheckpointError("checkpoint sequence must be positive")
        try:
            uuid.UUID(_text(integrity["checkpoint_id"], "integrity.checkpoint_id",
                            limit=64))
        except ValueError as exc:
            raise CheckpointError("checkpoint id must be a UUID") from exc
        _sha(integrity["previous_checkpoint_digest"],
             "integrity.previous_checkpoint_digest")
        _sha(integrity["checkpoint_digest"], "integrity.checkpoint_digest")
        if integrity["journal_through_seq"] != integrity["checkpoint_seq"]:
            raise CheckpointError("journal sequence must match checkpoint sequence")
        unsigned = {**doc, "integrity": {**integrity, "checkpoint_digest": ""}}
        if _digest(unsigned) != integrity["checkpoint_digest"]:
            raise CheckpointError("checkpoint digest does not match content")
    return doc


def validate_payload(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise CheckpointError("checkpoint payload must be an object")
    return _validate_payload(doc, active=False)


def validate_active(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise CheckpointError("active checkpoint must be an object")
    return _validate_payload(doc, active=True)


def workspace_state_dir(workspace: Path) -> Path:
    key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".agentbridge" / "dev-tasks" / key


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CheckpointError("checkpoint lock cannot be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise CheckpointError("checkpoint lock permissions are too broad")
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI later
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":  # pragma: no cover
            import msvcrt
            with contextlib.suppress(OSError):
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _prepare_dir(state_dir: Path) -> None:
    existed = state_dir.exists()
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if state_dir.is_symlink():
        raise CheckpointError("checkpoint directory cannot be a symlink")
    if existed and state_dir.stat().st_mode & 0o077:
        raise CheckpointError("checkpoint directory permissions are too broad")
    if not existed:
        state_dir.chmod(0o700)


def _durable_replace(path: Path, doc: dict) -> None:
    if path.is_symlink():
        raise CheckpointError("checkpoint destination cannot be a symlink")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(doc, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _append_journal(path: Path, doc: dict) -> None:
    event = {
        "v": 1,
        "checkpoint_seq": doc["integrity"]["checkpoint_seq"],
        "checkpoint_id": doc["integrity"]["checkpoint_id"],
        "checkpoint_digest": doc["integrity"]["checkpoint_digest"],
        "at": time.time_ns(),
    }
    encoded = _canonical(event) + b"\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def _journal_sequences(path: Path) -> set[int]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="ascii").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("checkpoint_seq"), int):
                out.add(row["checkpoint_seq"])
        except json.JSONDecodeError:
            continue
    return out


def write_checkpoint(
    payload: dict,
    state_dir: Path,
    *,
    expect_seq: int,
    expect_digest: str,
) -> dict:
    validate_payload(payload)
    _prepare_dir(state_dir)
    active_path = state_dir / "active.json"
    journal_path = state_dir / "journal.jsonl"
    with _exclusive_lock(state_dir / ".lock"):
        current = None
        if active_path.is_symlink():
            raise CheckpointError("active checkpoint cannot be a symlink")
        if active_path.exists():
            if active_path.stat().st_mode & 0o077:
                raise CheckpointError("active checkpoint permissions are too broad")
            try:
                current = validate_active(
                    json.loads(active_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                raise CheckpointError("active checkpoint JSON is corrupt") from exc
        if journal_path.exists() and journal_path.stat().st_mode & 0o077:
            raise CheckpointError("checkpoint journal permissions are too broad")
        current_seq = current["integrity"]["checkpoint_seq"] if current else 0
        current_digest = current["integrity"]["checkpoint_digest"] if current else ZERO_DIGEST
        if current_seq != expect_seq or current_digest != expect_digest:
            raise StaleCheckpointError(
                f"active checkpoint is seq {current_seq} digest {current_digest}")
        seq = current_seq + 1
        integrity = {
            "checkpoint_seq": seq,
            "checkpoint_id": str(uuid.uuid4()),
            "previous_checkpoint_digest": current_digest,
            "checkpoint_digest": "",
            "journal_through_seq": seq,
        }
        candidate = {**payload, "integrity": integrity}
        integrity["checkpoint_digest"] = _digest(candidate)
        validate_active(candidate)
        _durable_replace(active_path, candidate)
        if seq not in _journal_sequences(journal_path):
            _append_journal(journal_path, candidate)
        readback = validate_active(json.loads(active_path.read_text(encoding="utf-8")))
        if readback["integrity"]["checkpoint_digest"] != integrity["checkpoint_digest"]:
            raise CheckpointError("checkpoint readback verification failed")
        return readback


def repair_journal(state_dir: Path) -> bool:
    active_path = state_dir / "active.json"
    if not active_path.exists():
        return False
    with _exclusive_lock(state_dir / ".lock"):
        doc = validate_active(json.loads(active_path.read_text(encoding="utf-8")))
        journal = state_dir / "journal.jsonl"
        seq = doc["integrity"]["checkpoint_seq"]
        if seq in _journal_sequences(journal):
            return False
        _append_journal(journal, doc)
        return True


def _git(workspace: Path, *args: str, allow_fail: bool = False,
         preserve: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], cwd=workspace, text=True, capture_output=True,
        check=False,
    )
    if result.returncode and not allow_fail:
        raise CheckpointError(result.stderr.strip() or "git observation failed")
    if result.returncode:
        return ""
    return result.stdout if preserve else result.stdout.strip()


def _file_digest(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return ""
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
    if not path.is_file():
        return "other"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_checkpoint(doc: dict, *, offline: bool = False) -> dict:
    validate_active(doc)
    workspace = Path(doc["identity"]["workspace"])
    results = []
    if not workspace.is_dir():
        return {"overall": "UNAVAILABLE", "observations": [
            {"kind": "workspace", "state": "UNAVAILABLE", "detail": "workspace missing"}
        ]}
    current_root = _git(workspace, "rev-parse", "--show-toplevel")
    expected_root = doc["git_baseline"]["worktree"]
    results.append({
        "kind": "worktree", "state": "MATCH" if current_root == expected_root else "CONFLICT",
        "detail": current_root,
    })
    current_common = _git(workspace, "rev-parse", "--git-common-dir")
    current_common = str((workspace / current_common).resolve()) \
        if not Path(current_common).is_absolute() else str(Path(current_common).resolve())
    results.append({
        "kind": "common_dir",
        "state": ("MATCH" if current_common == doc["git_baseline"]["common_dir"]
                  else "CONFLICT"),
        "detail": current_common,
    })
    branch = _git(workspace, "symbolic-ref", "--short", "-q", "HEAD", allow_fail=True)
    expected_branch = doc["git_baseline"]["branch"]
    results.append({
        "kind": "branch", "state": "MATCH" if branch == expected_branch else "CONFLICT",
        "detail": branch or "DETACHED",
    })
    head = _git(workspace, "rev-parse", "HEAD")
    expected_head = doc["git_baseline"]["expected_head"]
    head_state = "MATCH" if head == expected_head else "STALE_OBSERVATION"
    results.append({"kind": "head", "state": head_state, "detail": head})

    listed = {entry["path"]: entry for entry in doc["workspace_entries"]}
    for rel, entry in listed.items():
        current = _file_digest(workspace / rel)
        baseline = entry["baseline_digest"]
        expected = entry["expected_digest"]
        state = ("MATCH" if current == baseline else
                 "EXPECTED_DRIFT" if expected and current == expected else
                 "CONFLICT")
        results.append({"kind": "path", "path": rel, "state": state,
                        "ownership": entry["ownership"]})
    porcelain = _git(
        workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        preserve=True)
    dirty = set()
    fields = porcelain.split("\x00") if porcelain else []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        status = field[:2]
        dirty.add(field[3:])
        if "R" in status or "C" in status:
            index += 1  # the following NUL field is the original path
    for rel in sorted(dirty - set(listed)):
        results.append({"kind": "path", "path": rel, "state": "UNKNOWN",
                        "ownership": "unclassified"})

    upstream_ref = doc["git_baseline"]["upstream_ref"]
    upstream_expected = doc["git_baseline"]["upstream_oid"]
    upstream_now = (_git(workspace, "rev-parse", upstream_ref, allow_fail=True)
                    if upstream_ref else "")
    upstream_state = ("MATCH" if upstream_now == upstream_expected else
                      "STALE_OBSERVATION")
    results.append({"kind": "upstream", "state": upstream_state,
                    "detail": upstream_now})
    results.append({"kind": "remote", "state": "UNAVAILABLE",
                    "detail": "offline audit" if offline else
                    "network remote not contacted by R0"})
    for item in doc["process_observations"]:
        results.append({"kind": "process", "state": "UNAVAILABLE",
                        "detail": str(item.get("role") or "untyped")[:80]})
    for item in doc["live_resources"]:
        results.append({"kind": "live_resource", "state": "UNAVAILABLE",
                        "detail": str(item.get("opaque_id") or "untyped")[:160]})

    order = {"CONFLICT": 5, "UNKNOWN": 4, "STALE_OBSERVATION": 3,
             "UNAVAILABLE": 2, "EXPECTED_DRIFT": 1, "MATCH": 0}
    overall = max((row["state"] for row in results), key=order.get, default="MATCH")
    return {"overall": overall, "observations": results}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read checkpoint JSON: {path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbridge-checkpoint")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("file", type=Path)
    write_cmd = sub.add_parser("write")
    write_cmd.add_argument("--input", type=Path, required=True)
    write_cmd.add_argument("--expect-seq", type=int, required=True)
    write_cmd.add_argument("--expect-digest", required=True)
    audit_cmd = sub.add_parser("audit")
    audit_cmd.add_argument("--offline", action="store_true")
    audit_cmd.add_argument("--json", action="store_true", dest="json_output")
    sub.add_parser("repair-journal")
    args = parser.parse_args(argv)
    state_dir = args.state_dir or workspace_state_dir(args.workspace)
    try:
        if args.command == "validate":
            doc = _load(args.file)
            (validate_active(doc) if "integrity" in doc else validate_payload(doc))
            print("valid")
        elif args.command == "write":
            doc = write_checkpoint(
                _load(args.input), state_dir,
                expect_seq=args.expect_seq,
                expect_digest=args.expect_digest,
            )
            print(json.dumps(doc["integrity"], sort_keys=True))
        elif args.command == "audit":
            doc = validate_active(_load(state_dir / "active.json"))
            report = audit_checkpoint(doc, offline=args.offline)
            print(json.dumps(report, indent=2, sort_keys=True)
                  if args.json_output else report["overall"])
            return 3 if report["overall"] in {"CONFLICT", "UNKNOWN"} else 0
        elif args.command == "repair-journal":
            print("repaired" if repair_journal(state_dir) else "current")
        return 0
    except StaleCheckpointError as exc:
        print(f"stale checkpoint: {exc}", file=sys.stderr)
        return 4
    except CheckpointError as exc:
        print(f"invalid checkpoint: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"checkpoint storage failure: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
