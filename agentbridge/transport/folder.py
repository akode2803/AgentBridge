"""Synced-folder transport (OneDrive / Google Drive / SharePoint desktop sync).

Battle-tested v1 behaviours carried forward:
- every write retries transient PermissionError (OneDrive locks mid-sync);
- reads tolerate half-synced files: BOM strip, partial trailing JSONL lines
  are NOT consumed (the offset waits for the line to complete);
- the change watcher is a wake-up HINT only (ReadDirectoryChangesW misses
  files OneDrive syncs down from other machines) — polling stays the truth.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..core.config import atomic_write_json, read_json
from ..core.errors import TransportError
from .base import Transport, Watcher

__all__ = ["FolderTransport"]

_RETRIES = 6
_BASE_DELAY = 0.15


def _unextend(s: str) -> str:
    """Strip a Windows extended-length prefix so two spellings of one path
    compare equal (``\\\\?\\C:\\x`` == ``C:\\x``; ``\\\\?\\UNC\\srv`` == ``\\\\srv``)."""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


class FolderTransport(Transport):
    scheme = "folder"

    def __init__(self, root: Path | str, *, max_upload_mb: int | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if max_upload_mb is not None:
            self.max_upload_bytes = max_upload_mb * 1024 * 1024

    # ------------------------------------------------------------ path guard
    def _abs(self, rel: str) -> Path:
        """Resolve a relative POSIX path, refusing anything escaping root.

        The containment check compares NORMALIZED spellings, not Path
        equality: when another thread/process holds the target mid-write,
        Windows ``resolve()`` returns the extended-length form
        (``\\\\?\\C:\\...``) of the SAME path, and a naive comparison read
        that as an escape (a real flake the R15 parallel tests caught —
        OneDrive locks trigger the identical misfire live)."""
        p = (self.root / rel).resolve()
        target = os.path.normcase(_unextend(str(p)))
        root = os.path.normcase(_unextend(str(self.root)))
        if target != root and not target.startswith(root + os.sep):
            raise TransportError(f"path escapes transport root: {rel!r}")
        return Path(_unextend(str(p)))

    @staticmethod
    def _retrying(fn, what: str):
        last: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                return fn()
            except (PermissionError, OSError) as e:  # OneDrive mid-sync lock
                last = e
                time.sleep(_BASE_DELAY * (2**attempt))
        raise TransportError(f"{what} failed after {_RETRIES} attempts") from last

    # ------------------------------------------------------------------ docs
    def get_doc(self, path: str, default: Any = None) -> Any:
        return read_json(self._abs(path), default=default)

    def put_doc(self, path: str, data: Any) -> None:
        atomic_write_json(self._abs(path), data)

    def delete_doc(self, path: str) -> None:
        try:
            self._abs(path).unlink(missing_ok=True)
        except OSError:
            self._retrying(lambda: self._abs(path).unlink(missing_ok=True), f"delete {path}")

    def list_docs(self, prefix: str) -> list[str]:
        base = self._abs(prefix)
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*.json")
            if p.is_file()
        )

    # ----------------------------------------------------------- chats / logs
    def list_chat_ids(self) -> list[str]:
        base = self.root / "chats"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def _log_path(self, chat_id: str, log_name: str) -> Path:
        if not log_name.endswith(".jsonl"):
            log_name += ".jsonl"
        return self._abs(f"chats/{chat_id}/msgs/{log_name}")

    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        base = self.root / "chats" / chat_id / "msgs"
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.glob("*.jsonl")):
            try:
                out.append((p.stem, p.stat().st_size))
            except OSError:
                continue  # vanished mid-scan (sync); next pass sees it
        return out

    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        p = self._log_path(chat_id, log_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"

        def _write():
            with p.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)

        self._retrying(_write, f"append {chat_id}/{log_name}")

    def read_log(self, chat_id: str, log_name: str, offset: int = 0) -> tuple[list[dict], int]:
        p = self._log_path(chat_id, log_name)
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            return [], 0
        except OSError:
            return [], offset  # locked mid-sync; retry next pass

        if len(raw) < offset:
            offset = 0  # file shrank (sync conflict) — re-read; cache dedups by id
        chunk = raw[offset:]
        if offset == 0 and chunk.startswith(b"\xef\xbb\xbf"):
            chunk = chunk[3:]
            offset = 3

        records: list[dict] = []
        consumed = 0
        for line in chunk.split(b"\n")[:-1]:  # anything after the last \n is partial
            consumed += len(line) + 1
            text = line.strip().decode("utf-8", errors="replace")
            if not text:
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                continue  # complete-but-garbage line: skip, never comes back
            if isinstance(rec, dict):
                records.append(rec)
        return records, offset + consumed

    def delete_chat(self, chat_id: str) -> None:
        base = self._abs(f"chats/{chat_id}")
        if base.exists():
            self._retrying(lambda: shutil.rmtree(base), f"delete chat {chat_id}")

    # ----------------------------------------------------------------- blobs
    def put_blob(self, path: str, data: bytes) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")

        def _write():
            tmp.write_bytes(data)
            os.replace(tmp, p)

        try:
            self._retrying(_write, f"put blob {path}")
        finally:
            tmp.unlink(missing_ok=True)

    def put_blob_from(self, local_src: Path, path: str) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")

        def _copy():
            shutil.copyfile(local_src, tmp)
            os.replace(tmp, p)

        try:
            self._retrying(_copy, f"put blob {path}")
        finally:
            tmp.unlink(missing_ok=True)

    def get_blob(self, path: str) -> bytes | None:
        try:
            return self._abs(path).read_bytes()
        except (FileNotFoundError, OSError):
            return None

    def blob_size(self, path: str) -> int | None:
        try:
            return self._abs(path).stat().st_size
        except (FileNotFoundError, OSError):
            return None

    def local_path(self, path: str) -> Path | None:
        p = self._abs(path)
        return p if p.exists() else None

    # ---------------------------------------------------------------- events
    def watch(self) -> Watcher:
        if os.name == "nt":
            try:
                return _WinDirWatcher(self.root)
            except Exception:  # noqa: BLE001 — degrade to polling, never fail
                pass
        return Watcher()


class _WinDirWatcher(Watcher):
    """ReadDirectoryChangesW wake-hint (ported from v1 agent_worker.DirWatcher).

    Best-effort BY DESIGN: OneDrive doesn't reliably notify for files synced
    DOWN from another machine, so this only shortens the poll latency — the
    caller's rescan is the source of truth.
    """

    def __init__(self, root: Path) -> None:
        import ctypes
        from ctypes import wintypes

        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILTER = 0x1 | 0x2 | 0x8 | 0x10  # FILE_NAME | DIR_NAME | SIZE | LAST_WRITE
        THREAD_TERMINATE = 0x0001
        INVALID = ctypes.c_void_p(-1).value

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        # explicit argtypes on every handle-taking call: default int coercion
        # can truncate 64-bit HANDLEs
        k32.OpenThread.restype = wintypes.HANDLE
        k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CancelSynchronousIo.restype = wintypes.BOOL
        k32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        self._event = threading.Event()
        self._k32 = k32
        self._closed = False
        self._handle = k32.CreateFileW(
            str(root), FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if not self._handle or self._handle == INVALID:
            raise TransportError("CreateFileW failed for dir watch")

        buf = ctypes.create_string_buffer(8192)
        nbytes = wintypes.DWORD()

        def loop() -> None:
            while not self._closed:
                ok = k32.ReadDirectoryChangesW(
                    self._handle, buf, len(buf), True, FILTER,
                    ctypes.byref(nbytes), None, None,
                )
                if not ok or self._closed:  # cancelled / handle closed
                    break
                self._event.set()

        self._thread = threading.Thread(target=loop, daemon=True, name="ab-dirwatch")
        self._thread.start()
        self._hthread = k32.OpenThread(THREAD_TERMINATE, False, self._thread.native_id)

    def wait(self, timeout: float) -> bool:
        hinted = self._event.wait(timeout)
        self._event.clear()
        return hinted

    def close(self) -> None:
        """Shutting this down safely is subtle (both failure modes were found
        with py-spy on real hangs):
        - CloseHandle while the thread is blocked INSIDE ReadDirectoryChangesW
          hangs; CancelIoEx does NOT abort a synchronous RDCW.
        - The documented cancel for sync I/O is CancelSynchronousIo(thread) —
          retried, because it no-ops if the thread isn't in the syscall at
          that instant (it re-enters and blocks again).
        If the thread still won't exit, we LEAK the handle (v1 lived like
        that for its whole life) — close() must never hang."""
        if self._closed:
            return
        self._closed = True
        try:
            for _ in range(40):
                if self._hthread:
                    self._k32.CancelSynchronousIo(self._hthread)
                self._thread.join(0.05)
                if not self._thread.is_alive():
                    break
            if not self._thread.is_alive():
                self._k32.CloseHandle(self._handle)
            if self._hthread:
                self._k32.CloseHandle(self._hthread)
        except Exception:  # noqa: BLE001 — closing is best-effort
            pass
