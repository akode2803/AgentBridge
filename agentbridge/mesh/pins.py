"""Key pinning (R27) — the local trust record for published account keys.

The directory doc (``users/<name>.json``) publishes the ``sign_pub`` /
``agree_pub`` that every signature check and epoch-key wrap relies on, but the
doc itself sits on a transport any member can write. This module gives each
machine its own durable record of the keys it has already seen (trust on
first use):

- **First sight pins.** The first published keypair seen for a name is stored
  under ``<home>/pins/<root>.json`` — beside the keystore, NOT in the SQLite
  cache, because the cache is rebuildable from the transport and trust state
  must never be.
- **The pin takes precedence.** When the published keys later differ from the
  pin, ``Directory.get`` returns the PINNED pair — so info events keep
  verifying against the key this machine already trusts, and new epoch keys
  keep being wrapped to it. A doc rewrite changes nothing for any machine
  that saw the original keys.
- **Changes raise an alert.** A mismatch is recorded once per (name, seen
  key) and surfaced to the signed-in human (GUI banner) until acknowledged.
- **A signed history advances the pin.** ``keys.history`` entries let a
  future key-rotation flow prove a transition: each entry is signed by the
  OLD key over ``rekey_signing_bytes``. A valid chain from the pinned key to
  the published one moves the pin forward silently. Nothing emits history
  yet — v2 has no key-change flow, so today every pin change alerts.

Residual (docs/THREAT_MODEL.md): a machine that never saw the original keys
pins whatever it reads first. Out-of-band fingerprint comparison is the
eventual answer; pinning protects every established relationship.
"""

from __future__ import annotations

import hashlib
import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import crypto
from ..core.config import DEFAULT_HOME
from ..core.timekit import utcnow_iso
from .pin_storage import (
    PendingAck,
    PendingAlert,
    PendingForget,
    PendingPin,
    PendingVerify,
    PinFileCoordinator,
    PinStoreUnavailable,
    canonical_json,
    serialize_history,
    validate_output,
)

__all__ = [
    "KeyPinStore", "PinStoreUnavailable", "rekey_signing_bytes", "key_fingerprint",
]


@dataclass(frozen=True)
class PinDecision:
    """Pure selection result; publication remains owned by ``KeyPinStore``."""

    action: Literal["keep", "first_seen", "rotate", "alert"]
    sign_pub: str
    agree_pub: str


def rekey_signing_bytes(
    name: str, old_sign_pub: str, sign_pub: str, agree_pub: str, ns: int
) -> bytes:
    """Canonical bytes a key-history entry's author signs WITH THE OLD KEY.
    Binds the account name (no cross-account replay), both keypairs of the
    transition, and an ns for ordering."""
    return f"{name}|rekey|{old_sign_pub}|{sign_pub}|{agree_pub}|{ns}".encode()


def key_fingerprint(name: str, sign_pub: str, agree_pub: str) -> str:
    """The short human-comparable digest of an account's keypair (R31 — the
    out-of-band answer to the first-contact residual). Both devices derive it
    from the same (name, keys) triple, so reading it aloud over a call — or
    comparing in person — proves they pinned the same identity. Rendered as
    8 groups of 4 hex chars: 'A1B2 C3D4 …'."""
    if not sign_pub:
        return ""
    digest = hashlib.sha256(f"{name}|{sign_pub}|{agree_pub}".encode()).hexdigest()
    hexs = digest[:32].upper()
    return " ".join(hexs[i:i + 4] for i in range(0, 32, 4))


def _history_advances(
    name: str,
    pinned_sign: str,
    sign_pub: str,
    agree_pub: str,
    history: list | None,
) -> bool:
    """Whether signed history advances ``pinned_sign`` to the published pair."""
    entries = sorted(
        (e for e in (history or []) if isinstance(e, dict)),
        key=lambda e: int(e.get("ns", 0)),
    )
    current = pinned_sign
    for entry in entries:
        if entry.get("old_sign_pub") != current:
            continue
        data = rekey_signing_bytes(
            name,
            current,
            entry.get("sign_pub", ""),
            entry.get("agree_pub", ""),
            int(entry.get("ns", 0)),
        )
        if not crypto.verify(current, entry.get("sig", ""), data):
            return False
        current = entry.get("sign_pub", "")
        if current == sign_pub and entry.get("agree_pub", "") == agree_pub:
            return True
    return False


def evaluate_pin(
    name: str,
    pin: object,
    sign_pub: str,
    agree_pub: str,
    history: list | None = None,
) -> PinDecision:
    """Select trusted keys from caller-provided observations without I/O."""
    if not isinstance(pin, dict) or not pin.get("sign_pub"):
        if not sign_pub:
            return PinDecision("keep", sign_pub, agree_pub)
        return PinDecision("first_seen", sign_pub, agree_pub)
    pinned_sign = pin.get("sign_pub", "")
    pinned_agree = pin.get("agree_pub", "")
    if pinned_sign == sign_pub and pinned_agree == agree_pub:
        return PinDecision("keep", sign_pub, agree_pub)
    if sign_pub and _history_advances(
        name, pinned_sign, sign_pub, agree_pub, history
    ):
        return PinDecision("rotate", sign_pub, agree_pub)
    return PinDecision("alert", pinned_sign, pinned_agree)


class KeyPinStore:
    """One pin file per (machine, mesh root); every identity on the machine
    shares it (the trusted keys are the same truth for all of them). Writes
    are read-merge-write so concurrent processes (GUI + harness runners)
    never clobber each other's pins."""

    def __init__(self, home: Path | None, root_key: str) -> None:
        tag = hashlib.sha1(str(root_key).encode()).hexdigest()[:12]
        self.path = (home or DEFAULT_HOME) / "pins" / f"{tag}.json"
        self._lock = threading.Lock()
        self._storage = PinFileCoordinator(self.path)
        with self._lock, self._view():
            pass

    # ------------------------------------------------------------- resolution
    def trusted(
        self,
        name: str,
        sign_pub: str,
        agree_pub: str,
        history: list | None = None,
    ) -> tuple[str, str]:
        """The (sign_pub, agree_pub) this machine should trust for ``name``,
        given the currently PUBLISHED pair. Pins on first sight; advances the
        pin along a validly signed history; otherwise the pin wins and a
        mismatch is recorded."""
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name)
            history_json = None
            if pin and sign_pub and (
                pin.get("sign_pub"), pin.get("agree_pub")
            ) != (sign_pub, agree_pub):
                history_json = serialize_history(history)
                decision = evaluate_pin(
                    name, pin, sign_pub, agree_pub, json.loads(history_json),
                )
            else:
                decision = evaluate_pin(name, pin, sign_pub, agree_pub, history)
            if decision.action == "first_seen":
                operation = PendingPin(
                    "first_seen", name, None, None, sign_pub, agree_pub,
                    utcnow_iso(), "[]",
                )
                candidate = copy.deepcopy(doc)
                self._apply(operation, candidate)
                self._commit(candidate, operation)
                return sign_pub, agree_pub
            if decision.action == "rotate":
                operation = PendingPin(
                    "rotate", name, pin.get("sign_pub", ""),
                    pin.get("agree_pub", ""), sign_pub, agree_pub,
                    utcnow_iso(), history_json or "[]",
                )
                candidate = copy.deepcopy(doc)
                self._apply(operation, candidate)
                self._commit(candidate, operation)
            elif decision.action == "alert":
                if any(
                    alert["name"] == name
                    and alert["seen_sign_pub"] == sign_pub
                    for alert in doc["alerts"]
                ):
                    return decision.sign_pub, decision.agree_pub
                operation = PendingAlert(
                    name, pin.get("sign_pub", ""), pin.get("agree_pub", ""),
                    sign_pub, agree_pub, utcnow_iso(),
                )
                candidate = copy.deepcopy(doc)
                if self._apply(operation, candidate):
                    self._commit(candidate, operation)
            return decision.sign_pub, decision.agree_pub

    def _chain_ok(
        self, name: str, pinned_sign: str, sign_pub: str, agree_pub: str,
        history: list | None,
    ) -> bool:
        """True when ``history`` carries signed transitions from the pinned
        key to the published pair, each signed by the key it retires."""
        return _history_advances(name, pinned_sign, sign_pub, agree_pub, history)

    # ------------------------------------------------------------- mutations
    def pin(self, name: str, sign_pub: str, agree_pub: str) -> None:
        """Pin explicitly at provisioning time (signup / key mint) so the
        machine that created the keys trusts them before any read races a
        concurrent doc write. Never moves an existing pin."""
        with self._lock, self._view() as doc:
            if name not in doc["pins"] and sign_pub:
                operation = PendingPin(
                    "first_seen", name, None, None, sign_pub, agree_pub,
                    utcnow_iso(), "[]",
                )
                candidate = copy.deepcopy(doc)
                self._apply(operation, candidate)
                self._commit(candidate, operation)

    def fingerprint(self, name: str, sign_pub: str = "", agree_pub: str = "") -> str:
        """The fingerprint of the keys THIS MACHINE trusts for ``name`` — the
        pin when one exists, else the published pair the caller has in hand.
        What you read aloud is what you actually verify against."""
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name) or {}
        sign = pin.get("sign_pub") or sign_pub
        agree = pin.get("agree_pub") or agree_pub
        return key_fingerprint(name, sign, agree)

    def verified(self, name: str) -> str:
        """ISO timestamp of the out-of-band verification, or '' (R31). Cleared
        automatically if the pin ever moves (a signed-history advance mints a
        fresh entry without the flag)."""
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name) or {}
        return str(pin.get("verified") or "")

    def projection_facts(self, names) -> dict:
        """Bounded local trust inputs for projection-version hashing.

        These facts are never returned to clients directly. The projection
        collector digests them immediately so cache keys change when local
        trust, verification, or a key-change alert changes.
        """
        selected = sorted({str(name) for name in names if name})
        with self._lock, self._view() as doc:
            return {
                "pins": {
                    name: copy.deepcopy(doc["pins"].get(name) or {})
                    for name in selected
                },
                "alerts": [
                    copy.deepcopy(alert) for alert in doc["alerts"]
                    if str(alert.get("name") or "") in selected
                ],
            }

    def mark_verified(self, name: str) -> None:
        """Record that the signed-in human compared fingerprints out-of-band.
        Machine-local trust metadata, like the pin itself."""
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name)
            if pin is None:
                return
            operation = PendingVerify(
                name, pin["sign_pub"], pin["agree_pub"],
                str(pin.get("verified") or ""), utcnow_iso(),
            )
            candidate = copy.deepcopy(doc)
            self._apply(operation, candidate)
            self._commit(candidate, operation)

    def forget(self, name: str) -> None:
        """Remove trust state for an identity that was never published.

        Account creation uses this only while rolling back a failed transport
        write. Published or deleted identities keep their pins permanently.
        """
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name)
            alerts = [a for a in doc["alerts"] if a.get("name") == name]
            if pin is None and not alerts:
                return
            operation = PendingForget(
                name, None if pin is None else canonical_json(pin),
                canonical_json(alerts),
            )
            candidate = copy.deepcopy(doc)
            self._apply(operation, candidate)
            self._commit(candidate, operation)

    def auto_verify_local(self, load_bundle, pubs_of) -> list[str]:
        """R54 (V31): pins whose PRIVATE bundle lives on this machine verify
        themselves. The out-of-band ceremony guards against a substituted
        directory record — but a box holding the identity's own bundle
        minted (or adopted) those keys, so there is nothing to compare by
        hand; an owner's agents show Verified without the manual step.
        Marks ONLY when the bundle's public halves match the pin exactly;
        a stale bundle after a key change marks nothing (the key-change
        alert path owns that story). Returns the names marked."""
        with self._lock, self._view() as doc:
            names = [n for n, p in doc["pins"].items() if not p.get("verified")]
        marked = []
        for name in names:
            bundle = load_bundle(name)
            if not bundle:
                continue
            try:
                sign_pub, agree_pub = pubs_of(bundle)
            except Exception:  # noqa: BLE001 — an unreadable bundle proves nothing
                continue
            with self._lock, self._view() as doc:
                pin = dict(doc["pins"].get(name) or {})
            if pin and pin.get("sign_pub") == sign_pub \
                    and pin.get("agree_pub") == agree_pub:
                if self._mark_verified_if(name, sign_pub, agree_pub):
                    marked.append(name)
        return marked

    def alerts(self, *, unacked_only: bool = False) -> list[dict]:
        with self._lock, self._view() as doc:
            return [
                copy.deepcopy(a) for a in doc["alerts"]
                if not (unacked_only and a.get("ack"))
            ]

    def ack(self, name: str, seen_sign_pub: str = "") -> None:
        """Acknowledge alerts for ``name`` (optionally one specific seen key)
        so the GUI banner clears; the pin itself is untouched."""
        with self._lock, self._view() as doc:
            targets = tuple(
                (a["name"], a["seen_sign_pub"], a["first_seen"])
                for a in doc["alerts"]
                if a["name"] == name
                and (not seen_sign_pub or a["seen_sign_pub"] == seen_sign_pub)
                and not a["ack"]
            )
            if not targets:
                return
            operation = PendingAck(targets)
            candidate = copy.deepcopy(doc)
            self._apply(operation, candidate)
            self._commit(candidate, operation)

    # --------------------------------------------------------------- storage
    @contextmanager
    def _view(self):
        with self._storage.locked() as (durable, _present):
            view = copy.deepcopy(durable)
            changed = False
            remaining = []
            for operation in self._storage.pending:
                try:
                    applied = self._apply(operation, view)
                except Exception as exc:
                    self._storage.conflicted = True
                    raise PinStoreUnavailable("pin_conflict") from exc
                if applied:
                    changed = True
                    remaining.append(operation)
                elif changed:
                    # This no-op may depend on an earlier unpersisted operation.
                    remaining.append(operation)
            if changed:
                try:
                    self._storage.write(view)
                except PinStoreUnavailable as exc:
                    if exc.reason != "write_failed":
                        raise
                    self._storage.pending = tuple(remaining)
                else:
                    self._storage.pending = ()
            else:
                self._storage.pending = ()
            self._set_view(view)
            yield view

    def _commit(self, candidate: dict, operation) -> None:
        staged = self._storage.staged(operation)
        validate_output(candidate)
        try:
            self._storage.write(candidate)
        except PinStoreUnavailable as exc:
            if exc.reason != "write_failed":
                raise
            self._storage.pending = staged
        else:
            self._storage.pending = ()
        self._set_view(candidate)

    def _set_view(self, doc: dict) -> None:
        self._pins = copy.deepcopy(doc["pins"])
        self._alerts = copy.deepcopy(doc["alerts"])

    def _mark_verified_if(self, name: str, sign_pub: str, agree_pub: str) -> bool:
        with self._lock, self._view() as doc:
            pin = doc["pins"].get(name)
            if not pin or pin["sign_pub"] != sign_pub \
                    or pin["agree_pub"] != agree_pub or pin.get("verified"):
                return False
            operation = PendingVerify(name, sign_pub, agree_pub, "", utcnow_iso())
            candidate = copy.deepcopy(doc)
            self._apply(operation, candidate)
            self._commit(candidate, operation)
            return True

    @staticmethod
    def _apply(operation, doc: dict) -> bool:
        pins, alerts = doc["pins"], doc["alerts"]
        if type(operation) is PendingPin:
            current = pins.get(operation.name)
            if current and (current["sign_pub"], current["agree_pub"]) == (
                operation.target_sign, operation.target_agree,
            ):
                return False
            if operation.mode == "first_seen":
                if current is not None:
                    raise PinStoreUnavailable("pin_conflict")
            else:
                if current is None or (current["sign_pub"], current["agree_pub"]) != (
                    operation.expected_sign, operation.expected_agree,
                ):
                    raise PinStoreUnavailable("pin_conflict")
                history = json.loads(operation.history_json)
                if not _history_advances(
                    operation.name, current["sign_pub"], operation.target_sign,
                    operation.target_agree, history,
                ):
                    raise PinStoreUnavailable("pin_conflict")
            entry = {} if current is None else copy.deepcopy(current)
            entry.update(sign_pub=operation.target_sign,
                         agree_pub=operation.target_agree,
                         pinned=operation.pinned)
            entry.pop("verified", None)
            pins[operation.name] = entry
            return True
        if type(operation) is PendingAlert:
            pin = pins.get(operation.name)
            if pin is None or (pin["sign_pub"], pin["agree_pub"]) != (
                operation.expected_sign, operation.expected_agree,
            ):
                raise PinStoreUnavailable("pin_conflict")
            for alert in alerts:
                if (alert["name"], alert["seen_sign_pub"]) == (
                    operation.name, operation.seen_sign,
                ):
                    immutable = (
                        alert["seen_agree_pub"], alert["pinned_sign_pub"],
                        alert["first_seen"],
                    )
                    expected = (
                        operation.seen_agree, operation.expected_sign,
                        operation.first_seen,
                    )
                    if immutable != expected:
                        raise PinStoreUnavailable("pin_conflict")
                    return False
            alerts.append({
                "name": operation.name, "seen_sign_pub": operation.seen_sign,
                "seen_agree_pub": operation.seen_agree,
                "pinned_sign_pub": operation.expected_sign,
                "first_seen": operation.first_seen, "ack": False,
            })
            return True
        if type(operation) is PendingVerify:
            pin = pins.get(operation.name)
            if pin is None or (pin["sign_pub"], pin["agree_pub"]) != (
                operation.expected_sign, operation.expected_agree,
            ):
                raise PinStoreUnavailable("pin_conflict")
            current = str(pin.get("verified") or "")
            if current == operation.target_verified:
                return False
            if current != operation.expected_verified:
                raise PinStoreUnavailable("pin_conflict")
            pin["verified"] = operation.target_verified
            return True
        if type(operation) is PendingAck:
            changed = False
            for target in operation.targets:
                found = next((a for a in alerts if (
                    a["name"], a["seen_sign_pub"], a["first_seen"]
                ) == target), None)
                if found is None:
                    raise PinStoreUnavailable("pin_conflict")
                if not found["ack"]:
                    found["ack"] = True
                    changed = True
            return changed
        if type(operation) is PendingForget:
            current = pins.get(operation.name)
            selected = [a for a in alerts if a["name"] == operation.name]
            if current is None and not selected:
                return False
            if (None if current is None else canonical_json(current)) != operation.pin_json \
                    or canonical_json(selected) != operation.alerts_json:
                raise PinStoreUnavailable("pin_conflict")
            pins.pop(operation.name, None)
            doc["alerts"] = [a for a in alerts if a["name"] != operation.name]
            return True
        raise PinStoreUnavailable("invalid_pending")
