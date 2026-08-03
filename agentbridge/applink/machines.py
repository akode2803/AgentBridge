"""Signed, per-identity machine announcements for AppLink discovery."""

from __future__ import annotations

import platform
import time

from .. import crypto
from ..core.models import UserKind
from ..core.jsonkit import canonical_json_bytes
from ..core.timekit import utcnow_iso
from ..transport.base import Transport

__all__ = ["MachineRegistry", "MachineRegistryError", "STALE_S"]

STALE_S = 3600.0
_FUTURE_SKEW_NS = int(5 * 60 * 1e9)
_FIELDS = {
    "v", "machine", "user", "app_version", "platform", "capabilities",
    "last_seen", "last_seen_ns",
}


class MachineRegistryError(ValueError):
    """A machine announcement is malformed, forged, stale, or misrouted."""


def _version_key(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in value.strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


class MachineRegistry:
    def __init__(self, tx: Transport, machine: str, *, user: str = "",
                 app_version: str = "", directory=None, keystore=None) -> None:
        self.tx = tx
        self.machine = machine
        self.user = user
        self.app_version = app_version
        self.directory = directory
        self.keystore = keystore

    @staticmethod
    def _path(machine: str, user: str) -> str:
        return f"machines/{machine}/{user}.json"

    def _validate(self, path: str, value: object) -> dict:
        if not isinstance(value, dict) or set(value) != {"record", "sig"}:
            raise MachineRegistryError("invalid machine envelope")
        rec = value["record"]
        if not isinstance(rec, dict) or set(rec) != _FIELDS or rec["v"] != 1:
            raise MachineRegistryError("invalid machine record")
        for name in ("machine", "user", "app_version", "platform", "last_seen"):
            if not isinstance(rec[name], str) or not rec[name]:
                raise MachineRegistryError(f"invalid machine {name}")
        if path != self._path(rec["machine"], rec["user"]):
            raise MachineRegistryError("machine path mismatch")
        if (not isinstance(rec["capabilities"], list)
                or any(not isinstance(v, str) or not v for v in rec["capabilities"])
                or rec["capabilities"] != sorted(set(rec["capabilities"]))):
            raise MachineRegistryError("invalid machine capabilities")
        ns = rec["last_seen_ns"]
        if isinstance(ns, bool) or not isinstance(ns, int) \
                or ns > time.time_ns() + _FUTURE_SKEW_NS:
            raise MachineRegistryError("invalid machine timestamp")
        acc = self.directory.get(rec["user"]) if self.directory else None
        if not acc or not acc.active or not acc.keys.sign_pub:
            raise MachineRegistryError("machine announcer is unavailable")
        if (acc.kind is UserKind.AGENT
                and (not acc.agent or acc.agent.machine != rec["machine"])):
            raise MachineRegistryError("agent is not hosted on this machine")
        if not crypto.verify(acc.keys.sign_pub, str(value["sig"]),
                             canonical_json_bytes(rec)):
            raise MachineRegistryError("invalid machine signature")
        return rec

    def announce(self, *, capabilities: list[str] | None = None) -> dict:
        if not self.user or self.directory is None or self.keystore is None:
            raise MachineRegistryError("signed machine identity is unavailable")
        bundle = self.keystore.load(self.user)
        if not bundle:
            raise MachineRegistryError(
                f"identity key for @{self.user} is locked or unavailable")
        rec = {
            "v": 1, "machine": self.machine, "user": self.user,
            "app_version": self.app_version or "unknown",
            "platform": platform.system() or "unknown",
            "capabilities": sorted(set(capabilities or [])),
            "last_seen": utcnow_iso(), "last_seen_ns": time.time_ns(),
        }
        doc = {"record": rec,
               "sig": crypto.sign(bundle, canonical_json_bytes(rec))}
        self.tx.put_doc(self._path(self.machine, self.user), doc)
        return rec

    def records(self, machine: str | None = None, *, active_only: bool = True) -> list[dict]:
        floor = time.time_ns() - int(STALE_S * 1e9)
        prefix = f"machines/{machine}" if machine else "machines"
        out = []
        for path in self.tx.list_docs(prefix):
            try:
                rec = self._validate(path, self.tx.get_doc(path))
                if machine and rec["machine"] != machine:
                    continue
                if active_only and rec["last_seen_ns"] < floor:
                    continue
                out.append(rec)
            except (MachineRegistryError, OSError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda r: (r["machine"], r["user"]))

    @staticmethod
    def _aggregate(records: list[dict]) -> dict | None:
        if not records:
            return None
        latest = max(records, key=lambda r: (r["last_seen_ns"], r["user"]))
        return {
            **latest,
            "users": sorted({r["user"] for r in records}),
            "capabilities": sorted({c for r in records for c in r["capabilities"]}),
            "app_version": max(records, key=lambda r: (
                _version_key(r["app_version"]), r["last_seen_ns"], r["user"],
            ))["app_version"],
            "last_seen_ns": max(r["last_seen_ns"] for r in records),
        }

    def get(self, machine: str) -> dict | None:
        return self._aggregate(self.records(machine))

    def has_identity(self, machine: str, user: str) -> bool:
        return any(r["user"] == user for r in self.records(machine))

    def peers(self, *, include_self: bool = False,
              active_only: bool = True) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for rec in self.records(active_only=active_only):
            if not include_self and rec["machine"] == self.machine:
                continue
            grouped.setdefault(rec["machine"], []).append(rec)
        return [self._aggregate(grouped[name]) for name in sorted(grouped)]
