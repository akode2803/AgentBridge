"""Strict cross-process storage coordination for machine-local trust pins."""

from __future__ import annotations

import copy
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.config import atomic_write_json
from . import pin_lock_gate

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PENDING_BYTES = 16 * 1024 * 1024
MAX_PENDING_OPS = 1024
MAX_HISTORY_ENTRIES = 10_000
MAX_JSON_DEPTH = 128
MAX_JSON_NODES = 200_000


class PinStoreUnavailable(RuntimeError):
    """The durable pin authority cannot be safely selected or changed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PendingPin:
    mode: Literal["first_seen", "rotate"]
    name: str
    expected_sign: str | None
    expected_agree: str | None
    target_sign: str
    target_agree: str
    pinned: str
    history_json: str


@dataclass(frozen=True)
class PendingAlert:
    name: str
    expected_sign: str
    expected_agree: str
    seen_sign: str
    seen_agree: str
    first_seen: str


@dataclass(frozen=True)
class PendingVerify:
    name: str
    expected_sign: str
    expected_agree: str
    expected_verified: str
    target_verified: str


@dataclass(frozen=True)
class PendingAck:
    targets: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class PendingForget:
    name: str
    pin_json: str | None
    alerts_json: str


PendingOperation = PendingPin | PendingAlert | PendingVerify | PendingAck | PendingForget


class PinFileCoordinator:
    def __init__(self, path: Path, *, lock_timeout_s: float = 1.0) -> None:
        if type(lock_timeout_s) not in {int, float} \
                or not math.isfinite(lock_timeout_s) \
                or not 0 <= lock_timeout_s <= 1.0:
            raise ValueError("lock_timeout_s must be finite and between 0 and 1")
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.lock_timeout_s = float(lock_timeout_s)
        self.pending: tuple[PendingOperation, ...] = ()
        self.seen_present = False
        self.conflicted = False

    @contextmanager
    def locked(self):
        if self.conflicted:
            raise PinStoreUnavailable("pin_conflict")
        held = self._acquire()
        try:
            doc, present = strict_read_pin_file(self.path)
            if self.seen_present and not present:
                raise PinStoreUnavailable("file_disappeared")
            if present:
                self.seen_present = True
            try:
                yield copy.deepcopy(doc), present
            except (MemoryError, RecursionError) as exc:
                raise PinStoreUnavailable("invalid_file") from exc
        finally:
            try:
                _unlock(held.fh)
            finally:
                held.local.release()

    def write(self, doc: dict) -> None:
        validate_output(doc)
        try:
            atomic_write_json(self.path, doc, retries=3, base_delay=0.05)
        except Exception as exc:
            raise PinStoreUnavailable("write_failed") from exc
        self.seen_present = True

    def staged(self, operation: PendingOperation) -> tuple[PendingOperation, ...]:
        try:
            operation = _validate_operation(operation)
            existing = tuple(_validate_operation(item) for item in self.pending)
        except PinStoreUnavailable as exc:
            if exc.reason == "file_too_large":
                raise PinStoreUnavailable("pending_exhausted") from exc
            raise
        operations = _coalesce(existing, operation)
        if len(operations) > MAX_PENDING_OPS:
            raise PinStoreUnavailable("pending_exhausted")
        try:
            raw = json.dumps(
                [_operation_dict(item) for item in operations],
                ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, MemoryError,
                RecursionError) as exc:
            raise PinStoreUnavailable("invalid_pending") from exc
        if len(raw) > MAX_PENDING_BYTES:
            raise PinStoreUnavailable("pending_exhausted")
        return operations

    def _acquire(self):
        deadline = time.monotonic() + self.lock_timeout_s
        try:
            local = pin_lock_gate.acquire(self.lock_path, deadline)
        except pin_lock_gate.LocalGateTimeout as exc:
            raise PinStoreUnavailable("lock_timeout") from exc
        except OSError as exc:
            raise PinStoreUnavailable("lock_open") from exc
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+b")
        except BaseException as exc:
            try:
                if isinstance(exc, OSError):
                    raise PinStoreUnavailable("lock_open") from exc
                raise
            finally:
                local.release()
        try:
            delay = 0.01
            while True:
                state = _try_lock(fh)
                if state is True:
                    return _HeldPinLock(fh, local)
                if state is None:
                    raise PinStoreUnavailable("lock_failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PinStoreUnavailable("lock_timeout")
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)
        except BaseException:
            try:
                fh.close()
            finally:
                local.release()
            raise


@dataclass(frozen=True)
class _HeldPinLock:
    fh: object
    local: pin_lock_gate.LocalGateLease


def strict_read_pin_file(path: Path) -> tuple[dict, bool]:
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return {"pins": {}, "alerts": []}, False
    except OSError as exc:
        raise PinStoreUnavailable("read_failed") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise PinStoreUnavailable("file_too_large")
    try:
        text = raw.decode("utf-8")
        doc = json.loads(
            text, object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_raise_json(value)),
        )
        bounded = _bounded_copy(doc, MAX_FILE_BYTES)
        return _validate_document(bounded), True
    except PinStoreUnavailable:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError,
            MemoryError, RecursionError) as exc:
        raise PinStoreUnavailable("invalid_file") from exc


def validate_output(doc: dict) -> None:
    try:
        bounded = _bounded_copy(doc, MAX_FILE_BYTES)
    except PinStoreUnavailable as exc:
        if exc.reason == "file_too_large":
            raise PinStoreUnavailable("output_too_large") from exc
        raise
    _validate_document(bounded)
    try:
        raw = json.dumps(
            doc, ensure_ascii=False, allow_nan=False, indent=1,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, MemoryError,
            RecursionError) as exc:
        raise PinStoreUnavailable("invalid_output") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise PinStoreUnavailable("output_too_large")


def serialize_history(history: list | None) -> str:
    value = [] if history is None else history
    if type(value) is not list or len(value) > MAX_HISTORY_ENTRIES:
        raise PinStoreUnavailable("invalid_history")
    try:
        value = _bounded_copy(value, MAX_PENDING_BYTES)
    except PinStoreUnavailable as exc:
        if exc.reason == "file_too_large":
            raise PinStoreUnavailable("pending_exhausted") from exc
        raise PinStoreUnavailable("invalid_history") from exc
    except (MemoryError, RecursionError, RuntimeError) as exc:
        raise PinStoreUnavailable("invalid_history") from exc
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError, MemoryError,
            RecursionError) as exc:
        raise PinStoreUnavailable("invalid_history") from exc
    if len(raw.encode("utf-8")) > MAX_PENDING_BYTES:
        raise PinStoreUnavailable("pending_exhausted")
    return raw


def canonical_json(value) -> str:
    try:
        value = _bounded_copy(value, MAX_PENDING_BYTES)
    except PinStoreUnavailable as exc:
        raise PinStoreUnavailable("invalid_pending") from exc
    except (MemoryError, RecursionError, RuntimeError) as exc:
        raise PinStoreUnavailable("invalid_pending") from exc
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError, MemoryError,
            RecursionError) as exc:
        raise PinStoreUnavailable("invalid_pending") from exc


def _validate_document(value) -> dict:
    if type(value) is not dict:
        raise PinStoreUnavailable("invalid_file")
    pins = value.get("pins", {})
    alerts = value.get("alerts", [])
    if type(pins) is not dict or type(alerts) is not list:
        raise PinStoreUnavailable("invalid_file")
    for name, pin in pins.items():
        if type(name) is not str or not name or type(pin) is not dict:
            raise PinStoreUnavailable("invalid_file")
        for field in ("sign_pub", "agree_pub", "pinned"):
            if type(pin.get(field)) is not str:
                raise PinStoreUnavailable("invalid_file")
        if not pin["sign_pub"]:
            raise PinStoreUnavailable("invalid_file")
        if "verified" in pin and type(pin["verified"]) is not str:
            raise PinStoreUnavailable("invalid_file")
    identities = set()
    for alert in alerts:
        if type(alert) is not dict:
            raise PinStoreUnavailable("invalid_file")
        for field in (
            "name", "seen_sign_pub", "seen_agree_pub", "pinned_sign_pub",
            "first_seen",
        ):
            if type(alert.get(field)) is not str:
                raise PinStoreUnavailable("invalid_file")
        if type(alert.get("ack")) is not bool:
            raise PinStoreUnavailable("invalid_file")
        identity = (alert["name"], alert["seen_sign_pub"])
        if identity in identities:
            raise PinStoreUnavailable("invalid_file")
        identities.add(identity)
    value.setdefault("pins", {})
    value.setdefault("alerts", [])
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PinStoreUnavailable("invalid_file")
        result[key] = value
    return result


def _raise_json(_value):
    raise PinStoreUnavailable("invalid_file")


def _operation_dict(operation: PendingOperation) -> dict:
    if type(operation) is PendingPin:
        fields = (
            operation.mode, operation.name, operation.expected_sign,
            operation.expected_agree, operation.target_sign,
            operation.target_agree, operation.pinned, operation.history_json,
        )
        return dict(zip((
            "mode", "name", "expected_sign", "expected_agree", "target_sign",
            "target_agree", "pinned", "history_json",
        ), fields), type="PendingPin")
    if type(operation) is PendingAlert:
        fields = (
            operation.name, operation.expected_sign, operation.expected_agree,
            operation.seen_sign, operation.seen_agree, operation.first_seen,
        )
        return dict(zip((
            "name", "expected_sign", "expected_agree", "seen_sign",
            "seen_agree", "first_seen",
        ), fields), type="PendingAlert")
    if type(operation) is PendingVerify:
        fields = (
            operation.name, operation.expected_sign, operation.expected_agree,
            operation.expected_verified, operation.target_verified,
        )
        return dict(zip((
            "name", "expected_sign", "expected_agree", "expected_verified",
            "target_verified",
        ), fields), type="PendingVerify")
    if type(operation) is PendingAck:
        return {"type": "PendingAck", "targets": operation.targets}
    if type(operation) is PendingForget:
        return {
            "type": "PendingForget", "name": operation.name,
            "pin_json": operation.pin_json, "alerts_json": operation.alerts_json,
        }
    raise PinStoreUnavailable("invalid_pending")


def _validate_operation(operation: PendingOperation) -> PendingOperation:
    if type(operation) not in {
        PendingPin, PendingAlert, PendingVerify, PendingAck, PendingForget,
    }:
        raise PinStoreUnavailable("invalid_pending")
    operation_type = type(operation)
    try:
        snapshot = _bounded_copy(_operation_dict(operation), MAX_PENDING_BYTES)
    except PinStoreUnavailable as exc:
        raise PinStoreUnavailable("invalid_pending") from exc
    except (MemoryError, RecursionError, RuntimeError) as exc:
        raise PinStoreUnavailable("invalid_pending") from exc
    if operation_type is PendingPin:
        fields = tuple(snapshot[name] for name in (
            "mode", "name", "expected_sign", "expected_agree", "target_sign",
            "target_agree", "pinned", "history_json",
        ))
        mode, name, expected_sign, expected_agree, target_sign, target_agree, \
            pinned, history_json = fields
        if type(mode) is not str or mode not in {"first_seen", "rotate"}:
            raise PinStoreUnavailable("invalid_pending")
        strings = (name, target_sign, target_agree, pinned, history_json)
        if any(type(value) is not str for value in strings):
            raise PinStoreUnavailable("invalid_pending")
        optional = (expected_sign, expected_agree)
        if mode == "first_seen" and optional != (None, None):
            raise PinStoreUnavailable("invalid_pending")
        if mode == "rotate" and any(type(value) is not str for value in optional):
            raise PinStoreUnavailable("invalid_pending")
        try:
            history = json.loads(
                history_json, object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_raise_json(value)),
            )
        except (PinStoreUnavailable, json.JSONDecodeError, RecursionError) as exc:
            raise PinStoreUnavailable("invalid_pending") from exc
        if type(history) is not list or len(history) > MAX_HISTORY_ENTRIES \
                or canonical_json(history) != history_json:
            raise PinStoreUnavailable("invalid_pending")
        if mode == "first_seen" and history_json != "[]":
            raise PinStoreUnavailable("invalid_pending")
        result = PendingPin(*fields)
    elif operation_type is PendingAlert:
        fields = tuple(snapshot[name] for name in (
            "name", "expected_sign", "expected_agree", "seen_sign",
            "seen_agree", "first_seen",
        ))
        strings = fields
        result = PendingAlert(*fields)
    elif operation_type is PendingVerify:
        fields = tuple(snapshot[name] for name in (
            "name", "expected_sign", "expected_agree", "expected_verified",
            "target_verified",
        ))
        strings = fields
        result = PendingVerify(*fields)
    elif operation_type is PendingAck:
        targets = snapshot["targets"]
        if type(targets) is not tuple or any(
            type(target) is not tuple or len(target) != 3
            or any(type(value) is not str for value in target)
            for target in targets
        ):
            raise PinStoreUnavailable("invalid_pending")
        strings = ()
        result = PendingAck(tuple(tuple(value for value in target)
                                  for target in targets))
    else:
        name = snapshot["name"]
        pin_json = snapshot["pin_json"]
        alerts_json = snapshot["alerts_json"]
        if type(name) is not str \
                or pin_json is not None and type(pin_json) is not str \
                or type(alerts_json) is not str:
            raise PinStoreUnavailable("invalid_pending")
        strings = (name, alerts_json) + (
            () if pin_json is None else (pin_json,)
        )
        result = PendingForget(name, pin_json, alerts_json)
    if any(type(value) is not str for value in strings):
        raise PinStoreUnavailable("invalid_pending")
    canonical_json(snapshot)
    return result


def _bounded_copy(value, maximum: int, *, max_depth: int = MAX_JSON_DEPTH,
                  max_nodes: int = MAX_JSON_NODES):
    remaining = maximum
    nodes = 0

    def visit(item, depth: int):
        nonlocal remaining, nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise PinStoreUnavailable("invalid_file")
        if item is None or type(item) is bool or type(item) is int:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise PinStoreUnavailable("invalid_file")
            return item
        if type(item) is str:
            if len(item) > remaining:
                raise PinStoreUnavailable("file_too_large")
            try:
                size = len(item.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise PinStoreUnavailable("invalid_file") from exc
            remaining -= size
            if remaining < 0:
                raise PinStoreUnavailable("file_too_large")
            return item
        if type(item) is list:
            return [visit(child, depth + 1) for child in item]
        if type(item) is tuple:
            return tuple(visit(child, depth + 1) for child in item)
        if type(item) is dict:
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise PinStoreUnavailable("invalid_file")
                copied_key = visit(key, depth + 1)
                result[copied_key] = visit(child, depth + 1)
            return result
        raise PinStoreUnavailable("invalid_file")

    return visit(value, 0)


def _coalesce(
    pending: tuple[PendingOperation, ...], operation: PendingOperation,
) -> tuple[PendingOperation, ...]:
    if pending and operation == pending[-1]:
        return pending
    if pending and type(operation) is PendingAck and type(pending[-1]) is PendingAck:
        targets = set(pending[-1].targets)
        targets.update(operation.targets)
        return (*pending[:-1], PendingAck(tuple(sorted(targets))))
    return (*pending, operation)


def _try_lock(fh) -> bool | None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        import errno

        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) \
                or getattr(exc, "winerror", None) in (33, 36):
            return False
        return None


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


__all__ = [
    "MAX_FILE_BYTES", "MAX_HISTORY_ENTRIES", "MAX_JSON_DEPTH",
    "MAX_JSON_NODES", "MAX_PENDING_BYTES", "MAX_PENDING_OPS",
    "PendingAck", "PendingAlert", "PendingForget",
    "PendingPin", "PendingVerify", "PinFileCoordinator",
    "PinStoreUnavailable", "canonical_json", "serialize_history",
    "strict_read_pin_file", "validate_output",
]
