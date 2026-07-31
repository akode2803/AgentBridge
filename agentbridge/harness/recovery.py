"""Bounded local retention for files left by incomplete harness runs.

The active ``outbox/`` is one run's explicit share lane. Files that did not
reach a message move to the same chat workspace's ``recovery/`` directory;
they are never resent implicitly. A later run sees their paths in its prompt
and may deliberately copy one back into the active outbox.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RECOVERY_MAX_AGE_S", "RECOVERY_MAX_BYTES", "RECOVERY_MAX_FILES",
    "RecoveryReport", "archive_outbox", "discard_delivered",
    "finish_outbox", "prepare_outbox", "retain_paths",
]

RECOVERY_MAX_AGE_S = 7 * 86400.0
RECOVERY_MAX_BYTES = 1024 * 1024 * 1024
RECOVERY_MAX_FILES = 50
AGENT_RECOVERY_MAX_BYTES = 2 * 1024 * 1024 * 1024
AGENT_RECOVERY_MAX_FILES = 200


@dataclass(frozen=True)
class RecoveryReport:
    retained: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
    pruned_elsewhere: int = 0

    def prompt_text(self) -> str:
        if not self.retained and not self.pruned and not self.pruned_elsewhere:
            return ""
        parts = []
        if self.retained:
            shown = ", ".join(self.retained[:12])
            if len(self.retained) > 12:
                shown += f", and {len(self.retained) - 12} more"
            parts.append(
                "Files from incomplete or over-limit earlier runs are retained "
                f"locally at: {shown}. They are NOT resent automatically. "
                "Inspect them only if relevant and copy an intended file into "
                "this run's outbox to share it."
            )
        if self.pruned:
            shown = ", ".join(self.pruned[:8])
            if len(self.pruned) > 8:
                shown += f", and {len(self.pruned) - 8} more"
            parts.append(
                "The bounded recovery policy removed older/excess artifacts: "
                f"{shown}. Recreate them only if the conversation still needs them."
            )
        if self.pruned_elsewhere:
            parts.append(
                f"The agent-wide cap also removed {self.pruned_elsewhere} "
                "older artifact(s) from other chat workspaces; their names are "
                "not exposed in this chat."
            )
        return " ".join(parts)


def _safe_reason(reason: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-")[:32] \
        or "incomplete"


def _workspace_from_parent(path: Path) -> Path | None:
    try:
        parent = path.parent
        if parent.name != "outbox":
            return None
        run_dir = parent.parent
        if run_dir.parent.name != "runs":
            return None
        return run_dir.parent.parent
    except OSError:
        return None


def _managed_workspace(path: Path) -> Path | None:
    """Return the workspace only for a safe child of a run-scoped outbox."""
    if path.is_symlink():
        return None
    return _workspace_from_parent(path)


def _cleanup_run_dir(outbox: Path) -> None:
    run_dir = outbox.parent
    try:
        if outbox.is_dir():
            if any(outbox.iterdir()):
                return
            outbox.rmdir()
        marker = run_dir / ".active.json"
        marker.unlink(missing_ok=True)
        if run_dir.is_dir() and not any(run_dir.iterdir()):
            run_dir.rmdir()
        runs = run_dir.parent
        if runs.is_dir() and not any(runs.iterdir()):
            runs.rmdir()
    except OSError:
        pass


def _event_dir(workdir: Path, reason: str) -> Path:
    base = workdir / "recovery" / f"{time.time_ns()}-{_safe_reason(reason)}"
    base.mkdir(parents=True, exist_ok=False)
    return base


def _move_entries(entries: list[Path], workdir: Path, reason: str) -> list[Path]:
    candidates = [p for p in entries if p.exists() or p.is_symlink()]
    if not candidates:
        return []
    target_dir = _event_dir(workdir, reason)
    retained = []
    for source in candidates:
        try:
            if source.is_symlink():
                source.unlink(missing_ok=True)
                continue
            target = target_dir / source.name
            suffix = 2
            while target.exists():
                target = target_dir / f"{source.stem}-{suffix}{source.suffix}"
                suffix += 1
            shutil.move(str(source), str(target))
            now = time.time()
            if target.is_dir():
                for child in target.rglob("*"):
                    if child.is_file() and not child.is_symlink():
                        os.utime(child, (now, now))
            elif target.is_file():
                os.utime(target, (now, now))
            retained.append(target)
        except OSError:
            continue
    if not retained:
        with contextlib.suppress(OSError):
            target_dir.rmdir()
    return retained


def archive_outbox(outbox: Path, reason: str) -> list[Path]:
    """Move one failed/interrupted run's active artifacts into recovery."""
    outbox = Path(outbox)
    workdir = _managed_workspace(outbox / "placeholder")
    if workdir is None:
        return []
    if not outbox.is_dir():
        _cleanup_run_dir(outbox)
        return []
    retained = _move_entries(list(outbox.iterdir()), workdir, reason)
    _cleanup_run_dir(outbox)
    return retained


def retain_paths(paths: list[str], reason: str) -> list[Path]:
    """Retain managed outbox files; ignore arbitrary responder-owned paths."""
    grouped: dict[tuple[Path, Path], list[Path]] = {}
    for raw in paths or []:
        path = Path(raw)
        if path.is_symlink():
            workdir = _workspace_from_parent(path)
            if workdir is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                _cleanup_run_dir(path.parent)
            continue
        workdir = _managed_workspace(path)
        if workdir is not None:
            grouped.setdefault((workdir, path.parent), []).append(path)
    retained = []
    for (workdir, outbox), entries in grouped.items():
        retained.extend(_move_entries(entries, workdir, reason))
        _cleanup_run_dir(outbox)
    return retained


def discard_delivered(paths: list[str]) -> None:
    """Delete only managed active-outbox sources after durable message enqueue."""
    outboxes = set()
    for raw in paths or []:
        path = Path(raw)
        if _managed_workspace(path) is None:
            continue
        outboxes.add(path.parent)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
    for outbox in outboxes:
        _cleanup_run_dir(outbox)


def finish_outbox(outbox: str | Path) -> None:
    """Remove an empty run lane after all of its files were resolved."""
    if outbox:
        _cleanup_run_dir(Path(outbox))


def _prune_recovery(
    workdir: Path,
    *,
    max_age_s: float = RECOVERY_MAX_AGE_S,
    max_bytes: int = RECOVERY_MAX_BYTES,
    max_files: int = RECOVERY_MAX_FILES,
) -> list[str]:
    root = Path(workdir) / "recovery"
    if not root.is_dir():
        return []
    now = time.time()
    files: list[tuple[Path, int, float]] = []
    pruned = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink():
                path.unlink(missing_ok=True)
                pruned.append(str(path.relative_to(workdir)))
            elif path.is_file():
                stat = path.stat()
                files.append((path, stat.st_size, stat.st_mtime))
        except OSError:
            continue

    kept_bytes = 0
    kept_files = 0
    for path, size, mtime in sorted(files, key=lambda item: item[2], reverse=True):
        expired = max_age_s >= 0 and mtime < now - max_age_s
        over = kept_files >= max_files or kept_bytes + size > max_bytes
        if expired or over:
            try:
                path.unlink()
                pruned.append(str(path.relative_to(workdir)))
            except OSError:
                pass
            continue
        kept_files += 1
        kept_bytes += size

    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    return pruned


def _prune_agent_recovery(
    workdir: Path,
    *,
    max_bytes: int = AGENT_RECOVERY_MAX_BYTES,
    max_files: int = AGENT_RECOVERY_MAX_FILES,
) -> tuple[list[str], int]:
    """Bound all chat recoveries without leaking other-chat names here."""
    workdir = Path(workdir)
    files: list[tuple[Path, int, float]] = []
    for root in workdir.parent.glob("*/recovery"):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    files.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
    kept_bytes = 0
    kept_files = 0
    current_pruned = []
    other_pruned = 0
    for path, size, mtime in sorted(files, key=lambda item: item[2], reverse=True):
        if kept_files < max_files and kept_bytes + size <= max_bytes:
            kept_files += 1
            kept_bytes += size
            continue
        try:
            path.unlink()
            if path.is_relative_to(workdir):
                current_pruned.append(str(path.relative_to(workdir)))
            else:
                other_pruned += 1
        except OSError:
            continue
    return current_pruned, other_pruned


def _recover_abandoned_runs(workdir: Path) -> None:
    runs = workdir / "runs"
    if not runs.is_dir():
        return
    for run_dir in list(runs.iterdir()):
        if not run_dir.is_dir():
            continue
        marker = run_dir / ".active.json"
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
            if int(meta.get("pid", -1)) == os.getpid():
                continue  # another live run in this process owns it
        except (OSError, ValueError, TypeError):
            pass
        archive_outbox(run_dir / "outbox", "interrupted")


def _recover_legacy_outbox(workdir: Path) -> None:
    legacy = workdir / "outbox"
    if not legacy.is_dir():
        return
    _move_entries(list(legacy.iterdir()), workdir, "legacy-interrupted")
    with contextlib.suppress(OSError):
        legacy.rmdir()


def prepare_outbox(workdir: Path, run_id: str) -> tuple[Path, RecoveryReport]:
    """Create one isolated run outbox and recover only abandoned predecessors."""
    workdir = Path(workdir)
    (workdir / "recovery").mkdir(parents=True, exist_ok=True)
    _recover_legacy_outbox(workdir)
    _recover_abandoned_runs(workdir)
    pruned = _prune_recovery(workdir)
    global_pruned, pruned_elsewhere = _prune_agent_recovery(workdir)
    pruned.extend(global_pruned)
    retained = []
    for path in (workdir / "recovery").rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                retained.append(str(path.relative_to(workdir)))
        except OSError:
            continue
    run_dir = workdir / "runs" / _safe_reason(run_id)
    outbox = run_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=False)
    (run_dir / ".active.json").write_text(
        json.dumps({"pid": os.getpid(), "created_ns": time.time_ns()}),
        encoding="utf-8", newline="\n")
    return outbox, RecoveryReport(
        retained=tuple(sorted(retained)), pruned=tuple(sorted(set(pruned))),
        pruned_elsewhere=pruned_elsewhere)
