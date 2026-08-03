"""Accounts v2 (R7) — the user file IS the account.

Model:
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
import hashlib
import os
import re
import secrets

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .. import crypto
from ..core.errors import PermissionDenied, ValidationError
from ..core.models import Account, ChatKind, UserKind
from ..core.timekit import utcnow_iso
from ..transport.base import Transport
from .directory import Directory
from .keyring import KeyStore
from .membership import MembershipService
from .messaging import MessagingService
from .paths import P
from .pins import key_fingerprint

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
        keystore: KeyStore | None = None,
    ) -> None:
        self.tx = tx
        self.directory = directory
        self.messaging = messaging
        self.membership = membership
        self.user = user
        self.machine = machine
        self.keystore = keystore or KeyStore()

    # ------------------------------------------------------------- creation
    def create_human(self, name: str, password: str, *, display: str = "") -> tuple[Account, str]:
        """Returns (account, recovery_code). The recovery code is shown ONCE:
        forgotten password + lost code = history unreadable (D5)."""
        self._require_free(name)
        if len(password or "") < 6:
            raise ValidationError("password must be at least 6 characters")
        salt = os.urandom(16)
        bundle = crypto.generate_identity()
        sign_pub, agree_pub = crypto.identity_pubs(bundle)
        recovery_code = crypto.new_recovery_code()
        doc = {
            "name": name,
            "kind": UserKind.HUMAN.value,
            "display": display or name.title(),
            "created": utcnow_iso(),
            "active": True,
            "auth": {"algo": "scrypt", "salt": _b64(salt),
                     "hash": _b64(_scrypt(password, salt))},
            "keys": {
                "sign_pub": sign_pub,
                "agree_pub": agree_pub,
                "wrapped_priv": crypto.wrap_bundle(bundle, password),
                "recovery": crypto.wrap_bundle(bundle, recovery_code),
            },
        }
        self._publish_identity(
            name, doc, bundle, sign_pub, agree_pub, lifecycle_actor=name,
        )
        return Account.from_dict(doc), recovery_code

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
        # agents get identity keys too, but no password wrap: the private
        # bundle lives ONLY on this machine's keystore (machine identity)
        bundle = crypto.generate_identity()
        sign_pub, agree_pub = crypto.identity_pubs(bundle)
        doc = {
            "name": name,
            "kind": UserKind.AGENT.value,
            "display": display,
            # default about, overridable later: "<Owner>'s <Agent> on <machine>"
            "about": f"{acc.display or owner}'s {display} on {self.machine}",
            "created": utcnow_iso(),
            "active": True,
            "keys": {"sign_pub": sign_pub, "agree_pub": agree_pub},
            "agent": {"owner": owner, "machine": self.machine,
                      "harness": harness or {}},
        }
        self._publish_identity(
            name, doc, bundle, sign_pub, agree_pub, verify_local=True,
            lifecycle_actor=owner,
        )
        return Account.from_dict(doc)

    def _publish_identity(
        self,
        name: str,
        doc: dict,
        bundle: bytes,
        sign_pub: str,
        agree_pub: str,
        *,
        verify_local: bool = False,
        lifecycle_actor: str = "",
    ) -> None:
        """Persist private identity state before publishing its account row.

        A visible agent without its private key cannot sign events or unwrap
        chat keys. Prepare the machine-local key and trust pin first; if the
        transport refuses the account row, remove only state minted by this
        attempt so retrying the same name starts cleanly.
        """
        pins = self.directory.pins
        prior_fp = pins.fingerprint(name) if pins is not None else ""
        self.keystore.save(name, bundle)
        try:
            self.directory.pin_keys(name, sign_pub, agree_pub)
            if pins is not None:
                expected = key_fingerprint(name, sign_pub, agree_pub)
                if pins.fingerprint(name) != expected:
                    raise ValidationError(
                        f"@{name} has conflicting local identity keys"
                    )
                # R54: an agent minted on this machine verifies itself. Human
                # fingerprints keep their existing out-of-band semantics.
                if verify_local:
                    pins.mark_verified(name)
            self.tx.put_doc(P.user(name), doc)
            if lifecycle_actor:
                from .lifecycle import ensure_bootstrap

                ensure_bootstrap(
                    self.directory, self.keystore, name, actor=lifecycle_actor,
                )
        except Exception:
            try:
                self.tx.delete_doc(P.user(name))
            except Exception:
                pass
            self.keystore.forget(name)
            if pins is not None and not prior_fp:
                pins.forget(name)
            raise

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
        algo = auth.get("algo")
        try:
            if algo == "scrypt":
                salt = base64.b64decode(auth["salt"])
                expected = base64.b64decode(auth["hash"])
                return secrets.compare_digest(_scrypt(password, salt), expected)
            if algo == "pbkdf2":
                # migrated v1 record (hex salt/hash, sha256, iteration count);
                # the login flow upgrades it to scrypt after a success
                import hashlib

                derived = hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"),
                    bytes.fromhex(auth["salt"]), int(auth["iterations"]),
                )
                return secrets.compare_digest(derived, bytes.fromhex(auth["hash"]))
        except (KeyError, ValueError, TypeError):
            return False
        return False

    def upgrade_login(self, name: str, password: str) -> str | None:
        """Bring a migrated v1 account up to v2 on its first successful
        sign-in (call ONLY after ``verify_password`` said yes): a pbkdf2 auth
        record is re-hashed as scrypt, and identity keys are provisioned when
        absent — in that case the ONE-TIME recovery code is returned so the
        GUI can show it (D5). Returns None when nothing needed doing."""
        doc = self.tx.get_doc(P.user(name))
        if not isinstance(doc, dict):
            return None
        changed = False
        code: str | None = None
        auth = doc.get("auth") or {}
        if auth.get("algo") == "pbkdf2":
            salt = os.urandom(16)
            doc["auth"] = {"algo": "scrypt", "salt": _b64(salt),
                           "hash": _b64(_scrypt(password, salt))}
            changed = True
        keys = doc.get("keys") or {}
        if not keys.get("sign_pub"):
            bundle = crypto.generate_identity()
            sign_pub, agree_pub = crypto.identity_pubs(bundle)
            code = crypto.new_recovery_code()
            doc["keys"] = {
                "sign_pub": sign_pub,
                "agree_pub": agree_pub,
                "wrapped_priv": crypto.wrap_bundle(bundle, password),
                "recovery": crypto.wrap_bundle(bundle, code),
            }
            self.keystore.save(name, bundle)
            changed = True
        if changed:
            self.tx.put_doc(P.user(name), doc)
            if code is not None:  # keys were minted just now: pin them (R27)
                keys = doc.get("keys") or {}
                self.directory.pin_keys(
                    name, keys.get("sign_pub", ""), keys.get("agree_pub", ""))
        return code

    def change_password(self, old: str, new: str) -> None:
        """Re-hash with a fresh salt AND re-wrap the identity bundle under
        the new password in the same operation (D5) — the recovery-code wrap
        is untouched, so the code keeps working."""
        if not self.verify_password(self.user, old):
            raise PermissionDenied("current password is incorrect")
        if len(new or "") < 6:
            raise ValidationError("password must be at least 6 characters")
        bundle = self.keystore.load(self.user)
        if bundle is None:  # locked machine: unwrap with the old password
            acc = self.directory.get(self.user)
            wrapped = acc.keys.wrapped_priv if acc else None
            if wrapped is not None:
                bundle = crypto.unwrap_bundle(
                    {"salt": wrapped.salt, "nonce": wrapped.nonce, "ct": wrapped.ct},
                    old,
                )
                self.keystore.save(self.user, bundle)
        salt = os.urandom(16)

        def apply(doc: dict) -> None:
            doc["auth"] = {"algo": "scrypt", "salt": _b64(salt),
                           "hash": _b64(_scrypt(new, salt))}
            if bundle is not None:
                doc.setdefault("keys", {})["wrapped_priv"] = crypto.wrap_bundle(bundle, new)

        self.directory.patch(self.user, apply)

    def unlock(self, password: str) -> bool:
        """Sign-in on a device: unwrap the identity bundle with the password
        and cache it in the local keystore. False = wrong password/no keys."""
        acc = self.directory.get(self.user)
        wrapped = acc.keys.wrapped_priv if acc else None
        if wrapped is None:
            return False
        try:
            bundle = crypto.unwrap_bundle(
                {"salt": wrapped.salt, "nonce": wrapped.nonce, "ct": wrapped.ct},
                password,
            )
        except crypto.CryptoFail:
            return False
        self.keystore.save(self.user, bundle)
        return True

    def unlock_with_recovery(self, code: str) -> bool:
        """The D5 escape hatch: the recovery code unwraps the identity when
        the password is gone. Follow with change_password to set a new one."""
        acc = self.directory.get(self.user)
        wrapped = acc.keys.recovery if acc else None
        if wrapped is None:
            return False
        try:
            bundle = crypto.unwrap_bundle(
                {"salt": wrapped.salt, "nonce": wrapped.nonce, "ct": wrapped.ct},
                (code or "").strip(),
            )
        except crypto.CryptoFail:
            return False
        self.keystore.save(self.user, bundle)
        return True

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
        # R38: an agent MAY write its own About (owner and agent both edit;
        # the account doc's last write wins) — see _writable_target
        target = self._writable_target(agent, allow_self=True)
        return self.directory.patch(target, lambda doc: doc.update(about=about or ""))

    def set_status(self, state: str, text: str = "", *, agent: str | None = None) -> Account:
        """ONE logical status per account across all devices (account-model
        v2) — it lives on the account file, not in per-device presence.
        Suggested vocabulary: available / busy / dnd / away; agents read it
        before deciding whether to disturb someone (matrix-gated, R6).
        R38: an agent keeps its OWN status current (state + what it's
        working on) — owner and agent both edit, most recent wins."""
        target = self._writable_target(agent, allow_self=True)
        state = (state or "").strip().lower()
        if not state or len(state) > 24:
            raise ValidationError("status state must be 1-24 characters")
        return self.directory.patch(
            target,
            lambda doc: doc.update(status={"state": state, "text": (text or "")[:140]}),
        )

    # ------------------------------------------------------------- lifecycle
    def set_avatar(self, data: bytes, *, agent: str | None = None) -> Account:
        """Profile photo: blob at ``avatars/<name>.jpg`` + a {sha256, updated}
        marker on the account doc (cache busting). Owner-gated for agents.
        Photos are directory metadata — plain at rest like the rest of the
        account doc; VIEW access is matrix-gated at every connector."""
        target = self._writable_target(agent)
        if not data:
            raise ValidationError("empty image")
        sha = hashlib.sha256(data).hexdigest()
        self.tx.put_blob(P.avatar(target), data)
        return self.directory.patch(
            target,
            lambda doc: doc.update(avatar={"sha256": sha, "updated": utcnow_iso()}),
        )

    def clear_avatar(self, *, agent: str | None = None) -> Account:
        target = self._writable_target(agent)
        return self.directory.patch(target, lambda doc: doc.pop("avatar", None))

    def set_agent_harness(self, agent: str, changes: dict) -> Account:
        """Owner-set harness config for ONE agent (model, reasoning effort,
        concurrency, …). A merge, never an overwrite: a None value drops the
        key, and a dict value merges one level deep (an inner None drops the
        inner key) — the per-chat dicts (``rules``, ``models``) are written
        one chat at a time and must never wipe the other chats' picks."""
        target = self._writable_target(agent)
        if target == self.user:
            raise ValidationError("harness settings apply to agents only")

        def apply(doc: dict) -> None:
            harness = (doc.setdefault("agent", {})).setdefault("harness", {})
            for k, v in (changes or {}).items():
                if v is None:
                    harness.pop(k, None)
                elif isinstance(v, dict):
                    inner = harness.get(k)
                    inner = dict(inner) if isinstance(inner, dict) else {}
                    for ik, iv in v.items():
                        if iv is None:
                            inner.pop(ik, None)
                        else:
                            inner[ik] = iv
                    harness[k] = inner
                else:
                    harness[k] = v

        return self.directory.patch(target, apply)

    def adopt_agent(self, agent: str) -> Account:
        """Re-home an agent to the signed-in member's CURRENT machine (owner-
        gated) — the path that brings a MIGRATED agent (machine="migrated",
        no identity keys) online under the R15 harness. A keyless agent gets
        its identity minted here (safe: migrated logs hold no signed events
        of its authorship to invalidate). A keyed agent whose private bundle
        lives on another machine is refused: minting a replacement key would
        orphan the signatures on its past info events — proper machine moves
        need published key history (later round)."""
        target = self._writable_target(agent)
        acc = self.directory.get(target)
        has_pubs = bool(acc and acc.keys.sign_pub)
        has_bundle = self.keystore.load(target) is not None
        if has_pubs and not has_bundle:
            raise ValidationError(
                f"@{target}'s identity keys live on its current machine — "
                f"recreate the agent here instead (key moves come later)"
            )
        if not has_pubs:
            bundle = crypto.generate_identity()
            sign_pub, agree_pub = crypto.identity_pubs(bundle)

            def mint(doc: dict) -> None:
                keys = doc.setdefault("keys", {})
                keys["sign_pub"], keys["agree_pub"] = sign_pub, agree_pub

            self.directory.patch(target, mint)
            self.keystore.save(target, bundle)
            self.directory.pin_keys(target, sign_pub, agree_pub)  # R27
            # R54 (V31): keys minted here — the pin is born Verified
            if self.directory.pins is not None:
                self.directory.pins.mark_verified(target)
        self.ensure_lifecycle(target)
        from .lifecycle import publish_change

        publish_change(
            self.directory, self.keystore, target, actor=self.user,
            action="host", machine=self.machine,
        )
        self.directory.patch(
            target, lambda doc: doc.setdefault("agent", {}).update(
                machine=self.machine)
        )
        return self.directory.get(target)

    def set_machine_agents_active(self, active: bool) -> list[str]:
        """The EXPLICIT stand-down/resume switch for this machine's agents.
        NOT wired to logout (D19, Aryan 2026-07-13): signing out leaves agents
        running — they belong to the account, not the login session.
        Membership is untouched either way."""
        changed = []
        for name in self._owned_agents():
            acc = self.directory.get(name)
            if acc and acc.agent and acc.agent.machine == self.machine:
                self.ensure_lifecycle(name)
                from .lifecycle import publish_change

                publish_change(
                    self.directory, self.keystore, name, actor=self.user,
                    action="state", active=active,
                )
                self.directory.patch(name, lambda doc: doc.update(active=active))
                changed.append(name)
        return changed

    def claimable_agents(self) -> list[str]:
        """Agents a sign-in on THIS machine would transfer (hosted here,
        owned by someone else). Split from the patch (V69) so the facade
        can post their owner-changed departures BEFORE ownership moves."""
        me = self.directory.get(self.user)
        if me is None or me.kind is not UserKind.HUMAN:
            raise PermissionDenied("only a signed-in member can claim agents")
        out = []
        for name in self.directory.names():
            acc = self.directory.get(name)
            if (acc and acc.agent and acc.agent.machine == self.machine
                    and acc.agent.owner not in ("", self.user)
                    and self.keystore.load(name) is not None):
                out.append(name)
        return sorted(out)

    def claim_machine_agents(self) -> list[str]:
        """Login-on-this-machine ownership transfer (D19): the signed-in
        member becomes the responsible member for agents hosted HERE. The
        invariant then self-enforces the fallout — in any room where the old
        owner sat but the new one doesn't, the agent cascades out on the next
        fold. Called by the sign-in flow (R13) through the FACADE's
        ``Mesh.claim_machine_agents`` (V69), which first posts each agent's
        owner-changed departure pills; this primitive only moves ownership."""
        claimed = []
        for name in self.claimable_agents():
            self.ensure_lifecycle(name)
            from .lifecycle import publish_change

            publish_change(
                self.directory, self.keystore, name, actor=self.user,
                action="transfer", owner=self.user, machine=self.machine,
            )
            self.directory.patch(
                name, lambda d: d.setdefault("agent", {}).update(owner=self.user)
            )
            claimed.append(name)
        return claimed

    def delete_agent(self, agent: str) -> None:
        """Owner-initiated agent deletion (soft, like every deletion): remove
        it from every room (the owner may ALWAYS remove their own agent —
        admin or not, D19), deactivate the account, drop the local keys.
        The name stays resolvable; transcripts grey it out."""
        self._writable_target(agent)  # owner gate
        for snap in self.membership.chats_for():  # agent rooms ⊆ owner rooms
            if snap.kind is ChatKind.GROUP and agent in snap.members:
                try:
                    self.membership.remove_member(snap.id, agent)
                except Exception:  # noqa: BLE001 — deletion must not wedge
                    continue
        at = utcnow_iso()
        self.ensure_lifecycle(agent)
        from .lifecycle import publish_change

        publish_change(
            self.directory, self.keystore, agent, actor=self.user,
            action="deactivate", active=False, deactivated=at,
        )
        self.directory.patch(agent, lambda doc: doc.update(
            active=False, deactivated=at))
        self.keystore.forget(agent)

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
        at = utcnow_iso()
        from .lifecycle import publish_change

        for agent in self._owned_agents():
            self.ensure_lifecycle(agent)
            publish_change(
                self.directory, self.keystore, agent, actor=self.user,
                action="deactivate", active=False, deactivated=at,
            )
            self.directory.patch(agent, lambda doc: doc.update(
                active=False, deactivated=at))
        self.ensure_lifecycle(self.user)
        publish_change(
            self.directory, self.keystore, self.user, actor=self.user,
            action="deactivate", active=False, deactivated=at,
        )
        self.directory.patch(self.user, lambda doc: doc.update(
            active=False, deactivated=at))

    # ---------------------------------------------------------------- helpers
    def _owned_agents(self) -> list[str]:
        out = []
        for name in self.directory.names():
            acc = self.directory.get(name)
            if acc and acc.agent and acc.agent.owner == self.user:
                out.append(name)
        return sorted(out)

    def ensure_lifecycle(self, subject: str | None = None) -> list[str]:
        """TOFU-migrate locally provable account lifecycle state."""
        from .lifecycle import ensure_bootstrap, resolve_lifecycle

        names = [subject] if subject else [self.user, *self._owned_agents()]
        done = []
        for name in dict.fromkeys(n for n in names if n):
            if resolve_lifecycle(
                self.directory, name, store=self.directory.store,
            ) is not None:
                done.append(name)
                continue
            acc = self.directory._raw_get(name)
            if acc is None:
                continue
            actor = name if acc.kind is UserKind.HUMAN else self.user
            if self.keystore.load(actor) is None:
                continue
            if acc.kind is UserKind.AGENT and self.keystore.load(name) is None:
                continue
            ensure_bootstrap(self.directory, self.keystore, name, actor=actor)
            done.append(name)
        return done

    def _writable_target(self, agent: str | None, *, allow_self: bool = False) -> str:
        if agent is None:
            # D19: an agent identity can't self-manage its account — profile,
            # handle, privacy belong to the responsible member. R38 carves out
            # exactly TWO surfaces (``allow_self``): status and about, which
            # the agent is expected to keep current itself (the brief's M7 —
            # status informs whether to disturb someone). Owner and agent
            # write the same account field; the most recent write wins.
            if self.directory.kind(self.user) is UserKind.AGENT and not allow_self:
                raise PermissionDenied(
                    "an agent's account settings are managed by its responsible member"
                )
            return self.user
        if self.directory.kind(agent) is not UserKind.AGENT:
            raise ValidationError(f"@{agent} is not an agent")
        if self.directory.owner_of(agent) != self.user:
            raise PermissionDenied(
                f"only @{agent}'s responsible member can change its profile"
            )
        return agent
