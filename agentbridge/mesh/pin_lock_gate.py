"""Process-local FIFO admission for one path-named pin OS lock."""

from __future__ import annotations

import os
import threading
import time
import weakref
from collections import deque
from pathlib import Path


class LocalGateTimeout(RuntimeError):
    pass


class _FairPathGate:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.waiters: deque[object] = deque()
        self.held = False
        self.owner_thread: int | None = None


class LocalGateLease:
    def __init__(self, gate: _FairPathGate) -> None:
        self._gate: _FairPathGate | None = gate

    def release(self) -> None:
        gate = self._gate
        if gate is None:
            return
        with gate.condition:
            if self._gate is not gate:
                return
            gate.held = False
            gate.owner_thread = None
            gate.condition.notify_all()
            self._gate = None


_registry_lock = threading.Lock()
_registry: weakref.WeakValueDictionary[str, _FairPathGate] = weakref.WeakValueDictionary()


def acquire(path: Path, deadline: float) -> LocalGateLease:
    """Enter the path FIFO before *deadline*, admitting a free zero-time gate."""
    key = _path_key(path)
    gate = _gate_for(key)
    token = object()
    owner = threading.get_ident()
    lease = LocalGateLease(gate)
    enqueued = False
    admitting = False
    try:
        with gate.condition:
            if gate.held and gate.owner_thread == owner:
                raise LocalGateTimeout("reentrant local pin lock")
            enqueued = True
            gate.waiters.append(token)
            immediate = gate.waiters[0] is token and not gate.held
        waited = not immediate
        _queue_arrived(key, token)
        with gate.condition:
            while True:
                eligible = gate.waiters and gate.waiters[0] is token and not gate.held
                if eligible and (not waited or time.monotonic() < deadline):
                    admitting = True
                    gate.waiters.popleft()
                    gate.held = True
                    gate.owner_thread = owner
                    return lease
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalGateTimeout("local pin lock timeout")
                gate.condition.wait(remaining)
                waited = True
    except BaseException:
        try:
            if enqueued:
                with gate.condition:
                    try:
                        gate.waiters.remove(token)
                    except ValueError:
                        pass
                    gate.condition.notify_all()
        finally:
            if admitting:
                lease.release()
        raise


def _path_key(path: Path) -> str:
    raw = os.fspath(path)
    if type(raw) is not str or not raw or "\x00" in raw:
        raise OSError("invalid pin lock path")
    return os.path.normcase(os.path.realpath(os.path.abspath(raw)))


def _gate_for(key: str) -> _FairPathGate:
    with _registry_lock:
        gate = _registry.get(key)
        if gate is None:
            gate = _FairPathGate()
            _registry[key] = gate
        return gate


def _queue_arrived(_key: str, _token: object) -> None:
    """Private test seam, called without the registry, condition, or OS lock."""


def _registry_size() -> int:
    with _registry_lock:
        return len(_registry)


def _reset_after_fork() -> None:
    global _registry_lock, _registry
    _registry_lock = threading.Lock()
    _registry = weakref.WeakValueDictionary()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


__all__ = ["LocalGateLease", "LocalGateTimeout", "acquire"]
