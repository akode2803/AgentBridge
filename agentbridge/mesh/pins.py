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
import threading
from pathlib import Path

from .. import crypto
from ..core.config import DEFAULT_HOME, atomic_write_json, read_json
from ..core.timekit import utcnow_iso

__all__ = ["KeyPinStore", "rekey_signing_bytes", "key_fingerprint"]


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


class KeyPinStore:
    """One pin file per (machine, mesh root); every identity on the machine
    shares it (the trusted keys are the same truth for all of them). Writes
    are read-merge-write so concurrent processes (GUI + harness runners)
    never clobber each other's pins."""

    def __init__(self, home: Path | None, root_key: str) -> None:
        tag = hashlib.sha1(str(root_key).encode()).hexdigest()[:12]
        self.path = (home or DEFAULT_HOME) / "pins" / f"{tag}.json"
        self._lock = threading.Lock()
        doc = read_json(self.path, {}) or {}
        self._pins: dict[str, dict] = dict(doc.get("pins") or {})
        self._alerts: list[dict] = list(doc.get("alerts") or [])

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
        with self._lock:
            pin = self._pins.get(name)
            if not isinstance(pin, dict) or not pin.get("sign_pub"):
                if not sign_pub:
                    return sign_pub, agree_pub
                # first published keys seen by THIS process — the write merges
                # with the file, where another process may have pinned already
                self._write_pin(name, sign_pub, agree_pub)
                pin = self._pins.get(name) or {}
                if pin.get("sign_pub", sign_pub) == sign_pub:
                    return sign_pub, agree_pub
                self._write_alert(name, sign_pub, agree_pub, pin)
                return pin.get("sign_pub", ""), pin.get("agree_pub", "")
            pinned_sign = pin.get("sign_pub", "")
            pinned_agree = pin.get("agree_pub", "")
            if pinned_sign == sign_pub and pinned_agree == agree_pub:
                return sign_pub, agree_pub
            if sign_pub and self._chain_ok(
                name, pinned_sign, sign_pub, agree_pub, history
            ):
                self._write_pin(name, sign_pub, agree_pub, replace=True)
                return sign_pub, agree_pub
            self._write_alert(name, sign_pub, agree_pub, pin)
            return pinned_sign, pinned_agree

    def _chain_ok(
        self, name: str, pinned_sign: str, sign_pub: str, agree_pub: str,
        history: list | None,
    ) -> bool:
        """True when ``history`` carries signed transitions from the pinned
        key to the published pair, each signed by the key it retires."""
        entries = sorted(
            (e for e in (history or []) if isinstance(e, dict)),
            key=lambda e: int(e.get("ns", 0)),
        )
        current = pinned_sign
        for e in entries:
            if e.get("old_sign_pub") != current:
                continue
            data = rekey_signing_bytes(
                name, current, e.get("sign_pub", ""), e.get("agree_pub", ""),
                int(e.get("ns", 0)),
            )
            if not crypto.verify(current, e.get("sig", ""), data):
                return False  # a bad link never advances trust
            current = e.get("sign_pub", "")
            if current == sign_pub and e.get("agree_pub", "") == agree_pub:
                return True
        return False

    # ------------------------------------------------------------- mutations
    def pin(self, name: str, sign_pub: str, agree_pub: str) -> None:
        """Pin explicitly at provisioning time (signup / key mint) so the
        machine that created the keys trusts them before any read races a
        concurrent doc write. Never moves an existing pin."""
        with self._lock:
            if name not in self._pins and sign_pub:
                self._write_pin(name, sign_pub, agree_pub)

    def fingerprint(self, name: str, sign_pub: str = "", agree_pub: str = "") -> str:
        """The fingerprint of the keys THIS MACHINE trusts for ``name`` — the
        pin when one exists, else the published pair the caller has in hand.
        What you read aloud is what you actually verify against."""
        with self._lock:
            pin = self._pins.get(name) or {}
        sign = pin.get("sign_pub") or sign_pub
        agree = pin.get("agree_pub") or agree_pub
        return key_fingerprint(name, sign, agree)

    def verified(self, name: str) -> str:
        """ISO timestamp of the out-of-band verification, or '' (R31). Cleared
        automatically if the pin ever moves (a signed-history advance mints a
        fresh entry without the flag)."""
        with self._lock:
            pin = self._pins.get(name) or {}
        return str(pin.get("verified") or "")

    def projection_facts(self, names) -> dict:
        """Bounded local trust inputs for projection-version hashing.

        These facts are never returned to clients directly. The projection
        collector digests them immediately so cache keys change when local
        trust, verification, or a key-change alert changes.
        """
        selected = sorted({str(name) for name in names if name})
        with self._lock:
            return {
                "pins": {
                    name: copy.deepcopy(self._pins.get(name) or {})
                    for name in selected
                },
                "alerts": [
                    copy.deepcopy(alert) for alert in self._alerts
                    if str(alert.get("name") or "") in selected
                ],
            }

    def mark_verified(self, name: str) -> None:
        """Record that the signed-in human compared fingerprints out-of-band.
        Machine-local trust metadata, like the pin itself."""
        with self._lock:
            if name not in self._pins:
                return

            def apply(doc: dict) -> None:
                pin = doc.setdefault("pins", {}).get(name)
                if isinstance(pin, dict):
                    pin["verified"] = utcnow_iso()

            self._mutate(apply)

    def forget(self, name: str) -> None:
        """Remove trust state for an identity that was never published.

        Account creation uses this only while rolling back a failed transport
        write. Published or deleted identities keep their pins permanently.
        """
        with self._lock:
            def apply(doc: dict) -> None:
                doc.setdefault("pins", {}).pop(name, None)
                doc["alerts"] = [
                    a for a in doc.setdefault("alerts", [])
                    if a.get("name") != name
                ]

            self._mutate(apply)

    def auto_verify_local(self, load_bundle, pubs_of) -> list[str]:
        """R54 (V31): pins whose PRIVATE bundle lives on this machine verify
        themselves. The out-of-band ceremony guards against a substituted
        directory record — but a box holding the identity's own bundle
        minted (or adopted) those keys, so there is nothing to compare by
        hand; an owner's agents show Verified without the manual step.
        Marks ONLY when the bundle's public halves match the pin exactly;
        a stale bundle after a key change marks nothing (the key-change
        alert path owns that story). Returns the names marked."""
        with self._lock:
            names = [n for n, p in self._pins.items() if not p.get("verified")]
        marked = []
        for name in names:
            bundle = load_bundle(name)
            if not bundle:
                continue
            try:
                sign_pub, agree_pub = pubs_of(bundle)
            except Exception:  # noqa: BLE001 — an unreadable bundle proves nothing
                continue
            with self._lock:
                pin = dict(self._pins.get(name) or {})
            if pin and pin.get("sign_pub") == sign_pub \
                    and pin.get("agree_pub") == agree_pub:
                self.mark_verified(name)
                marked.append(name)
        return marked

    def alerts(self, *, unacked_only: bool = False) -> list[dict]:
        with self._lock:
            return [
                dict(a) for a in self._alerts
                if not (unacked_only and a.get("ack"))
            ]

    def ack(self, name: str, seen_sign_pub: str = "") -> None:
        """Acknowledge alerts for ``name`` (optionally one specific seen key)
        so the GUI banner clears; the pin itself is untouched."""
        with self._lock:
            def apply(doc: dict) -> None:
                for a in doc.setdefault("alerts", []):
                    if a.get("name") == name and (
                        not seen_sign_pub or a.get("seen_sign_pub") == seen_sign_pub
                    ):
                        a["ack"] = True

            self._mutate(apply)

    # --------------------------------------------------------------- storage
    def _write_pin(
        self, name: str, sign_pub: str, agree_pub: str, *, replace: bool = False
    ) -> None:
        entry = {"sign_pub": sign_pub, "agree_pub": agree_pub,
                 "pinned": utcnow_iso()}

        def apply(doc: dict) -> None:
            pins = doc.setdefault("pins", {})
            if replace:  # only a validly signed history transition gets here
                pins[name] = entry
            else:
                pins.setdefault(name, entry)

        self._mutate(apply)

    def _write_alert(
        self, name: str, sign_pub: str, agree_pub: str, pin: dict
    ) -> None:
        for a in self._alerts:  # one record per (name, seen key)
            if a.get("name") == name and a.get("seen_sign_pub") == sign_pub:
                return

        def apply(doc: dict) -> None:
            alerts = doc.setdefault("alerts", [])
            for a in alerts:
                if a.get("name") == name and a.get("seen_sign_pub") == sign_pub:
                    return
            alerts.append({
                "name": name,
                "seen_sign_pub": sign_pub,
                "seen_agree_pub": agree_pub,
                "pinned_sign_pub": pin.get("sign_pub", ""),
                "first_seen": utcnow_iso(),
                "ack": False,
            })

        self._mutate(apply)

    def _mutate(self, apply) -> None:
        """Read-merge-write: re-read the file, fold in what other processes
        pinned meanwhile (their existing entries win), apply the change, write
        atomically, refresh memory. Caller holds ``self._lock``. A failed disk
        write keeps the in-memory state consistent — trust decisions in this
        process never depend on the write landing."""
        doc = read_json(self.path, {}) or {}
        doc.setdefault("pins", {})
        doc.setdefault("alerts", [])
        for name, pin in self._pins.items():  # carry my pins into a fresh file
            doc["pins"].setdefault(name, pin)
        apply(doc)
        try:
            atomic_write_json(self.path, doc)
        except Exception:  # noqa: BLE001 — memory stays authoritative this run
            pass
        self._pins = dict(doc["pins"])
        self._alerts = list(doc["alerts"])
