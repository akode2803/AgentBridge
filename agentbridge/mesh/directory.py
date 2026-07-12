"""Account directory — read-side lookups over ``users/<name>.json``.

R5 needs kinds/owners/displays for the fold and owner-pull-in; R7 (accounts)
grows the write side. Satisfies ``events.Resolver``.
"""

from __future__ import annotations

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
