"""Account directory — read-side lookups over ``users/<name>.json``.

R5 needs kinds/owners/displays for the fold and owner-pull-in; R7 (accounts)
grows the write side. Satisfies ``events.Resolver``.

R27: when a ``KeyPinStore`` is wired in, ``get`` resolves the account's
published keys THROUGH the pins — every consumer of directory keys (the fold's
signature checks, the sealer, redaction verify, epoch-key wraps, peer requests)
therefore trusts the keys this machine already knows, not whatever the
transport currently says. This is the single choke point for key trust.
"""

from __future__ import annotations

from ..core.errors import ValidationError
from ..core.models import Account, UserKind
from ..transport.base import Transport
from .paths import P
from .pins import KeyPinStore

__all__ = ["Directory"]


class Directory:
    def __init__(self, tx: Transport, pins: KeyPinStore | None = None,
                 store=None) -> None:
        self.tx = tx
        self.pins = pins
        self.store = store

    def _raw_get(self, name: str) -> Account | None:
        doc = self.tx.get_doc(P.user(name))
        if not isinstance(doc, dict):
            return None
        acc = Account.from_dict(doc)
        if self.pins is not None:
            keys = doc.get("keys") or {}
            acc.keys.sign_pub, acc.keys.agree_pub = self.pins.trusted(
                name, acc.keys.sign_pub, acc.keys.agree_pub,
                keys.get("history") if isinstance(keys, dict) else None,
            )
        return acc

    def get(self, name: str) -> Account | None:
        acc = self._raw_get(name)
        if acc is None:
            return None
        try:
            from .lifecycle import resolve_lifecycle

            state = resolve_lifecycle(self, name, store=self.store)
        except Exception:  # noqa: BLE001 — unreadable evidence retains raw compatibility
            state = None
        if state is not None:
            acc.active = bool(state["active"])
            acc.deactivated = str(state["deactivated"])
            if acc.agent is not None:
                acc.agent.owner = str(state["owner"])
                acc.agent.machine = str(state["machine"])
        return acc

    def pin_keys(self, name: str, sign_pub: str, agree_pub: str) -> None:
        """Pin at provisioning time (accounts service calls this right after
        minting keys) so trust is established before any read."""
        if self.pins is not None:
            self.pins.pin(name, sign_pub, agree_pub)

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

    def sign_pub(self, name: str) -> str | None:
        """The account's published Ed25519 verify key (R13.5 fold checks info-
        event signatures against it); None for a keyless/legacy account."""
        acc = self.get(name)
        return (acc.keys.sign_pub or None) if acc else None

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

    def names(self) -> list[str]:
        """Every account name on the mesh (the GUI's people picker reads
        this; per-viewer field filtering stays PrivacyService's job)."""
        out = []
        for path in self.tx.list_docs("users"):
            leaf = path.rsplit("/", 1)[-1]
            if leaf.endswith(".json"):
                out.append(leaf[:-5])
        return sorted(out)

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
        return self.get(name)

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
