"""Machine registry (R11) — each machine announces itself so peers can see
what versions and capabilities are on the mesh.

``machines/<machine>.json`` is single-writer (the machine owns its own file).
This is metadata, not chat content — readable by folder members, never E2EE
(it carries no message bodies). Staleness mirrors presence: a machine that
stopped announcing ages out of "active".
"""

from __future__ import annotations

import platform
import time

from ..core.timekit import utcnow_iso
from ..transport.base import Transport

__all__ = ["MachineRegistry", "STALE_S"]

STALE_S = 3600.0  # a machine unseen for an hour no longer counts as active


class MachineRegistry:
    def __init__(
        self, tx: Transport, machine: str, *, user: str = "", app_version: str = ""
    ) -> None:
        self.tx = tx
        self.machine = machine
        self.user = user
        self.app_version = app_version

    def _path(self, machine: str) -> str:
        return f"machines/{machine}.json"

    def announce(self, *, capabilities: list[str] | None = None) -> dict:
        rec = {
            "machine": self.machine,
            "user": self.user,
            "app_version": self.app_version,
            "platform": platform.system(),
            "capabilities": sorted(capabilities or []),
            "last_seen": utcnow_iso(),
            "last_seen_ns": time.time_ns(),
        }
        self.tx.put_doc(self._path(self.machine), rec)
        return rec

    def get(self, machine: str) -> dict | None:
        doc = self.tx.get_doc(self._path(machine))
        return doc if isinstance(doc, dict) else None

    def peers(self, *, include_self: bool = False, active_only: bool = True) -> list[dict]:
        floor = time.time_ns() - int(STALE_S * 1e9)
        out = []
        for path in self.tx.list_docs("machines"):
            doc = self.tx.get_doc(path)
            if not isinstance(doc, dict):
                continue
            if not include_self and doc.get("machine") == self.machine:
                continue
            if active_only and int(doc.get("last_seen_ns", 0)) < floor:
                continue
            out.append(doc)
        return sorted(out, key=lambda d: d.get("machine", ""))
