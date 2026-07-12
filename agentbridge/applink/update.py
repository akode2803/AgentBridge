"""Auto-update (R11) — detection + integrity verification ONLY. This module
never downloads-and-runs anything on its own; the actual fetch and install
are INJECTED by the caller (the GUI wires the real, user-confirmed OS
machinery). Safety rails that cannot be bypassed:

  * apply() refuses unless ``confirm()`` returns True (explicit user consent);
  * apply() refuses unless the fetched bytes' SHA-256 matches the digest that
    ``check()`` obtained from the TRUSTED release source (GitHub over HTTPS),
    NOT from a mesh peer — a peer version-advert is only a hint to go look.

So a compromised mesh peer can, at worst, prompt the user to check GitHub; it
can neither choose the artifact nor skip the digest match nor auto-apply.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from .machines import MachineRegistry

__all__ = ["UpdatePlan", "UpdateService"]


def _parse_version(v: str) -> tuple:
    parts = []
    for chunk in (v or "").strip().lstrip("v").split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


@dataclass
class UpdatePlan:
    version: str
    url: str
    sha256: str  # from the trusted release source — the ONLY digest apply trusts


# release_info(current_version) -> {"version","url","sha256"} | None
ReleaseInfo = Callable[[str], dict | None]


class UpdateService:
    def __init__(
        self,
        registry: MachineRegistry,
        current_version: str,
        *,
        release_info: ReleaseInfo,
    ) -> None:
        self.registry = registry
        self.current_version = current_version
        self._release_info = release_info

    def peer_hint(self) -> str | None:
        """The highest app version any active peer advertises, if it beats
        ours — a nudge to consult the release source, nothing more."""
        best = self.current_version
        found = None
        for peer in self.registry.peers():
            pv = peer.get("app_version", "")
            if _parse_version(pv) > _parse_version(best):
                best, found = pv, pv
        return found

    def check(self) -> UpdatePlan | None:
        """Ask the TRUSTED release source about updates. Returns a plan only
        if it names a strictly newer version with a url + digest."""
        info = self._release_info(self.current_version)
        if not isinstance(info, dict):
            return None
        version, url, sha = info.get("version", ""), info.get("url", ""), info.get("sha256", "")
        if not (version and url and sha):
            return None
        if _parse_version(version) <= _parse_version(self.current_version):
            return None
        return UpdatePlan(version=version, url=url, sha256=sha.lower())

    def apply(
        self,
        plan: UpdatePlan,
        *,
        confirm: Callable[[UpdatePlan], bool],
        fetch: Callable[[str], bytes],
        install: Callable[[bytes, UpdatePlan], None],
    ) -> bool:
        """Run an update ONLY with explicit consent AND a verified digest.
        Returns True if installed; raises ValueError on a digest mismatch so
        a tampered artifact is a loud failure, never a silent install."""
        if not confirm(plan):
            return False
        artifact = fetch(plan.url)
        digest = hashlib.sha256(artifact).hexdigest()
        if digest != plan.sha256:
            raise ValueError(
                f"update integrity check FAILED: expected {plan.sha256}, got {digest}"
            )
        install(artifact, plan)
        return True
