"""Account directory — read-side lookups over ``users/<name>.json``.

R5 needs kinds/owners/displays for the fold and owner-pull-in; R7 (accounts)
grows the write side. Satisfies ``events.Resolver``.
"""

from __future__ import annotations

from ..core.errors import ValidationError
from ..core.models import Account, UserKind
from ..transport.base import Transport
from .paths import P

__all__ = ["Directory"]


class Directory:
    def __init__(self, tx: Transport) -> None:
        self.tx = tx

    def get(self, name: str) -> Account | None:
        doc = self.tx.get_doc(P.user(name))
        return Account.from_dict(doc) if isinstance(doc, dict) else None

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def kind(self, name: str) -> UserKind | None:
        acc = self.get(name)
        return acc.kind if acc else None

    def owner_of(self, name: str) -> str | None:
        """The ONE responsible member of an agent (account model v2)."""
        acc = self.get(name)
        if acc and acc.kind is UserKind.AGENT and acc.agent:
            return acc.agent.owner or None
        return None

    def display(self, name: str) -> str:
        acc = self.get(name)
        return (acc.display or name) if acc else name

    def resolve(self, ref: str) -> str | None:
        """Handle-or-id -> immutable id (R7 Telegram model). Ids win; then a
        handle scan (small mesh — cache later if it ever matters)."""
        ref = (ref or "").lower()
        if not ref:
            return None
        if self.exists(ref):
            return ref
        for path in self.tx.list_docs("users"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and doc.get("handle", "").lower() == ref:
                return doc.get("name")
        return None

    def handle_taken(self, handle: str) -> bool:
        """True if ``handle`` collides with ANY existing name or handle."""
        handle = (handle or "").lower()
        if self.exists(handle):
            return True
        for path in self.tx.list_docs("users"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and doc.get("handle", "").lower() == handle:
                return True
        return False

    def patch(self, name: str, apply) -> Account:
        """Read-merge-write on an account doc (single writer in practice:
        the account's own machine, or its owner's)."""
        doc = self.tx.get_doc(P.user(name))
        if not isinstance(doc, dict):
            raise ValidationError(f"unknown user @{name}")
        apply(doc)
        self.tx.put_doc(P.user(name), doc)
        return Account.from_dict(doc)

    def missing_owners(self, members: list[str]) -> dict[str, str]:
        """FREE-CHATTING invariant (ported from v1 ``_missing_owners``): for
        every agent in ``members`` whose responsible member isn't present,
        the owner that must be pulled in. Returns {owner: agent-that-needs-it}."""
        present = set(members)
        pulled: dict[str, str] = {}
        for m in members:
            if self.kind(m) is not UserKind.AGENT:
                continue
            owner = self.owner_of(m)
            if owner and owner not in present and self.exists(owner):
                pulled[owner] = m
                present.add(owner)
        return pulled
