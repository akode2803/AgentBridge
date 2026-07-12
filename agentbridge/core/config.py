"""Config + resilient JSON file primitives.

Successor to the load-bearing utilities in ``legacy/bridge.py`` (DEFAULT_HOME,
read_json, atomic_write_json, ...). Two v1 lessons are baked in as defaults:

- **Atomic writes retry on PermissionError** — OneDrive locks files mid-sync;
  a one-shot ``os.replace`` surfaced raw PermissionErrors to users (the 8D
  incident). Every JSON write in v2 goes through the retrying primitive.
- **Reads are tolerant** — a half-synced or corrupt JSON file returns the
  default instead of raising; the sync layer heals it on the next pass.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .errors import ConfigError, TransportError

__all__ = [
    "DEFAULT_HOME",
    "read_json",
    "atomic_write_json",
    "load_app_config",
    "save_app_config",
]

DEFAULT_HOME = Path(os.environ.get("AGENTBRIDGE_HOME", "")) if os.environ.get(
    "AGENTBRIDGE_HOME"
) else Path.home() / ".agentbridge"

_CONFIG_NAME = "config.json"
# read-side lock tolerance: short, since a real writer's os.replace window is
# sub-millisecond — a few backoffs cover it without stalling a poll tick
_READ_RETRIES = 5
_READ_DELAY = 0.03
_TMP_SEQ = itertools.count()  # unique-per-write tmp suffix (thread-safe)

# In-process striped I/O locks: on Windows, os.replace fails ACCESS_DENIED
# while ANY handle is open on the destination (CPython opens files without
# FILE_SHARE_DELETE) — and our own threads are those handles (a request
# handler and the sync thread both touching meta.json). Serializing same-path
# reads and writes in-process removes that whole collision class; the retry
# loops still cover the CROSS-process cases (another app instance, OneDrive).
_IO_LOCKS = [threading.Lock() for _ in range(64)]


def _io_lock(p: Path) -> threading.Lock:
    return _IO_LOCKS[hash(str(p)) % len(_IO_LOCKS)]


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read a JSON file; missing or corrupt -> ``default`` (sync tolerance).

    A transient lock is NOT missing data: on Windows, opening a file during
    another thread's ``os.replace`` raises PermissionError, and a synced
    folder (OneDrive) locks briefly mid-sync. Retry those a few times before
    giving up — otherwise a concurrent write makes a membership read spuriously
    look like "no such chat". A genuinely absent or corrupt file still returns
    ``default`` immediately (no wasted spin)."""
    p = Path(path)
    for attempt in range(_READ_RETRIES):
        try:
            with _io_lock(p), p.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except (PermissionError, OSError):  # transient lock (Windows / sync)
            if attempt == _READ_RETRIES - 1:
                return default
            time.sleep(_READ_DELAY * (2**attempt))
    return default


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    retries: int = 6,
    base_delay: float = 0.15,
) -> None:
    """Write JSON atomically (tmp + os.replace), retrying transient locks.

    Raises TransportError only after every retry is exhausted.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # the tmp name must be unique PER WRITE, not per process: two THREADS
    # writing the same doc (a request handler + the sync thread both
    # refolding meta.json) would otherwise share one tmp path and collide —
    # open-while-replacing raises until the retries exhaust (a real CI burn)
    tmp = p.with_suffix(
        p.suffix + f".tmp{os.getpid()}-{threading.get_ident()}-{next(_TMP_SEQ)}"
    )
    payload = json.dumps(data, ensure_ascii=False, indent=1)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with _io_lock(p):
                with tmp.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                os.replace(tmp, p)
            return
        except (PermissionError, OSError) as e:  # OneDrive mid-sync lock etc.
            last_err = e
            time.sleep(base_delay * (2**attempt))  # outside the lock
    tmp.unlink(missing_ok=True)
    raise TransportError(f"atomic write failed after {retries} attempts: {p}") from last_err


def load_app_config(home: Path | None = None) -> dict[str, Any]:
    """Load ``~/.agentbridge/config.json`` (empty dict if absent)."""
    cfg = read_json((home or DEFAULT_HOME) / _CONFIG_NAME, default={})
    if not isinstance(cfg, dict):
        raise ConfigError("config.json is not a JSON object")
    return cfg


def save_app_config(cfg: dict[str, Any], home: Path | None = None) -> None:
    atomic_write_json((home or DEFAULT_HOME) / _CONFIG_NAME, cfg)
