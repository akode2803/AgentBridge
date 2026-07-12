"""Accounts v2 (R7) — the user file IS the account.

Model (docs/FORMAT2.md + memory `agentbridge-account-model`):
- ``name`` = immutable identity; ``handle`` = the mutable @-username
  (Telegram split — renames never churn logs, cursors, or memberships).
- Machine-login ownership: a human's machine creates that machine's agents;
  one human -> N agents, an agent has exactly ONE responsible member.
- Auth = scrypt (humans only; agents never authenticate — machine identity).
- Deletion is SOFT and falls out of the invariants: leave every group (the
  fold then cascades the ex-owner's agents out of every room), flip
  ``active=false`` on the account and all owned agents. Sender names stay
  resolvable forever; the GUI greys them (product decision).
"""

from __future__ import annotations

import base64
import os
import re
import secrets

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..core.errors import PermissionDenied, ValidationError
from ..core.models import Account, ChatKind, UserKind
from ..core.timekit import utcnow_iso
from ..transport.base import Transport
from .directory import Directory
from .membership import MembershipService
from .messaging import MessagingService
from .paths import P

__all__ = ["AccountsService", "RESERVED_NAMES"]

RESERVED_NAMES = {"all", "everyone", "here", "admin", "system", "info", "mesh"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _scrypt(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and name not in RESERVED_NAMES


class AccountsService:
    def __init__(
        self,
        tx: Transport,
        directory: Directory,
        messaging: MessagingService,
        membership: MembershipService,
        user: str,
        machine: str,
    ) -> None:
        self.tx = tx
        self.directory = directory
        self.messaging = messaging
        self.membership = membership
        self.user = user
        self.machine = machine

    # ------------------------------------------------------------- creation
    def create_human(self, name: str, password: str, *, display: str = "") -> Account:
        self._require_free(name)
        if len(password or "") < 6:
            raise ValidationError("password must be at least 6 characters")
        salt = os.urandom(16)
        doc = {
            "name": name,
            "kind": UserKind.HUMAN.value,
            "display": display or name.title(),
            "created": utcnow_iso(),
            "active": True,
            "auth": {"algo": "scrypt", "salt": _b64(salt),
                     "hash": _b64(_scrypt(password, salt))},
        }
        self.tx.put_doc(P.user(name), doc)
        return Account.from_dict(doc)

    def create_agent(
        self,
        name: str,
        *,
        display: str = "",
        harness: dict | None = None,
    ) -> Account:
        """Create an agent ON THIS MACHINE, owned by the signed-in human
        (machine-login ownership). Agents never get an auth record."""
        owner = self.user
        acc = self.directory.get(owner)
        if acc is None or acc.kind is not UserKind.HUMAN:
            raise PermissionDenied("only a signed-in member can create agents")
        self._require_free(name)
        display = display or name.title()
        doc = {
            "name": name,
            "kind": UserKind.AGENT.value,
            "display": display,
            # default about, overridable later: "<Owner>'s <Agent> on <machine>"
            "about": f"{acc.display or owner}'s {display} on {self.machine}",
            "created": utcnow_iso(),
            "active": True,
            "agent": {"owner": owner, "machine": self.machine,
                      "harness": harness or {}},
        }
        self.tx.put_doc(P.user(name), doc)
        return Account.from_dict(doc)

    def _require_free(self, name: str) -> None:
        if not valid_name(name):
            raise ValidationError(
                "names are 2-32 chars: lowercase letters, digits, _ or -, "
                "starting with a letter (reserved words excluded)"
            )
        if self.directory.handle_taken(name):
            raise ValidationError(f"@{name} is taken")

    # ----------------------------------------------------------------- auth
    def verify_password(self, name: str, password: str) -> bool:
        doc = self.tx.get_doc(P.user(name))
        auth = (doc or {}).get("auth") or {}
        if auth.get("algo") != "scrypt":
            return False
        try:
            salt = base64.b64decode(auth["salt"])
            expected = base64.b64decode(auth["hash"])
        except (KeyError, ValueError):
            return False
        return secrets.compare_digest(_scrypt(password, salt), expected)

    def change_password(self, old: str, new: str) -> None:
        """Re-hash with a fresh salt. R9 hooks in here to re-wrap the
        password-wrapped account key (D5) in the same operation."""
        if not self.verify_password(self.user, old):
            raise PermissionDenied("current password is incorrect")
        if len(new or "") < 6:
            raise ValidationError("password must be at least 6 characters")
        salt = os.urandom(16)
        self.directory.patch(
            self.user,
            lambda doc: doc.update(auth={"algo": "scrypt", "salt": _b64(salt),
                                         "hash": _b64(_scrypt(new, salt))}),
        )

    # -------------------------------------------------------------- profile
    def set_handle(self, handle: str, *, agent: str | None = None) -> Account:
        """Username change, the Telegram way: the @handle moves, the identity
        (and every log/cursor/membership) stays put."""
        target = self._writable_target(agent)
        handle = (handle or "").lower()
        if not valid_name(handle):
            raise ValidationError("that username isn't allowed")
        current = self.directory.get(target)
        if handle != target and handle != (current.handle or ""):
            if self.directory.handle_taken(handle):
                raise ValidationError(f"@{handle} is taken")
        return self.directory.patch(target, lambda doc: doc.update(handle=handle))

    def set_display(self, display: str, *, agent: str | None = None) -> Account:
        target = self._writable_target(agent)
        if not (display or "").strip():
            raise ValidationError("display name can't be empty")
        return self.directory.patch(
            target, lambda doc: doc.update(display=display.strip())
        )

    def set_about(self, about: str, *, agent: str | None = None) -> Account:
        target = self._writable_target(agent)
        return self.directory.patch(target, lambda doc: doc.update(about=about or ""))

    # ------------------------------------------------------------- lifecycle
    def set_machine_agents_active(self, active: bool) -> list[str]:
        """Sign-out/in on THIS machine: flip only this machine's agents.
        Membership is untouched — a re-login restores service."""
        changed = []
        for name in self._owned_agents():
            acc = self.directory.get(name)
            if acc and acc.agent and acc.agent.machine == self.machine:
                self.directory.patch(name, lambda doc: doc.update(active=active))
                changed.append(name)
        return changed

    def delete_account(self, password: str) -> None:
        """Soft deletion (product spec): messages stay under the name (GUI
        greys them), everything else stops. Leaves every group — the fold's
        heal cascades this account's agents out of every room for free —
        then deactivates the account and all its agents everywhere."""
        if not self.verify_password(self.user, password):
            raise PermissionDenied("password is incorrect")
        for snap in self.membership.chats_for():
            if snap.kind is ChatKind.GROUP:
                try:
                    self.membership.leave(snap.id)
                except Exception:  # noqa: BLE001 — deletion must not wedge
                    continue
        for agent in self._owned_agents():
            self.directory.patch(agent, lambda doc: doc.update(
                active=False, deactivated=utcnow_iso()))
        self.directory.patch(self.user, lambda doc: doc.update(
            active=False, deactivated=utcnow_iso()))

    # ---------------------------------------------------------------- helpers
    def _owned_agents(self) -> list[str]:
        out = []
        for path in self.tx.list_docs("users"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and (doc.get("agent") or {}).get("owner") == self.user:
                out.append(doc["name"])
        return sorted(out)

    def _writable_target(self, agent: str | None) -> str:
        if agent is None:
            return self.user
        if self.directory.kind(agent) is not UserKind.AGENT:
            raise ValidationError(f"@{agent} is not an agent")
        if self.directory.owner_of(agent) != self.user:
            raise PermissionDenied(
                f"only @{agent}'s responsible member can change its profile"
            )
        return agent
