"""Session + Mesh lifecycle for the local GUI server.

One GuiApp = one machine's GUI = at most ONE signed-in human (v1 semantics,
kept). The Mesh facade instance exists only while someone is signed in; the
session survives restarts via ``gui_session.json`` in the LOCAL home dir
(never the synced folder) — the E2EE identity bundle already lives in the
local keystore, so restoring a session never needs the password again.
"""

from __future__ import annotations

import platform
import secrets
import threading
from pathlib import Path

from ..core.config import DEFAULT_HOME, atomic_write_json, read_json
from ..core.errors import ValidationError
from ..core.timekit import utcnow_iso
from ..mesh.directory import Directory
from ..mesh.eventbus import Subscription
from ..mesh.keyring import KeyStore
from ..mesh.service import Mesh
from ..transport import make_transport
from .applock import AppLock

__all__ = ["GuiApp"]

_SESSION_FILE = "gui_session.json"


def _breadcrumb(line: str) -> None:
    """V125: restore's timeline lands in the SAME file as the restart
    helper's (%TEMP%/agentbridge_restart.log), so a "the app signed out
    after restart" report is one file to read. Best-effort — a log line
    must never break auth."""
    try:
        import os
        import tempfile
        import time as _t

        with open(Path(tempfile.gettempdir()) / "agentbridge_restart.log",
                  "a", encoding="utf-8") as f:
            f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[{os.getpid()}] {line}\n")
    except Exception:  # noqa: BLE001
        pass


class GuiApp:
    """Owns the session, the Mesh, its sync thread, and live SSE subs."""

    def __init__(
        self,
        root: Path | str,
        *,
        home: Path | str | None = None,
        machine: str = "",
        encrypt: bool = True,
        static_dir: Path | str | None = None,
        app_version: str = "",
        poll_s: float = 4.0,
        sse_ping_s: float = 15.0,
    ) -> None:
        # a scheme spec (supabase://…) MUST stay a string — Path() collapses the
        # double slash into `supabase:\…` and mkdir fails (R23; folder roots
        # stay Paths). Mirrors app.main()'s as_root — the GuiApp was the one
        # cloud-wiring site R23 missed.
        self.root = root if (isinstance(root, str) and "://" in root) else Path(root)
        self.home = Path(home) if home else DEFAULT_HOME
        self.machine = machine or platform.node() or "gui"
        self.encrypt = encrypt
        self.app_version = app_version
        # One process generation, exposed to clients so a restart cover cannot
        # mistake the still-draining old server for the relaunched instance.
        self.instance_id = secrets.token_hex(8)
        self.poll_s = poll_s
        self.sse_ping_s = sse_ping_s
        # repo layout default: agentbridge/gui/context.py -> <repo>/gui/static
        self.static_dir = (
            Path(static_dir)
            if static_dir
            else Path(__file__).resolve().parents[2] / "gui" / "static"
        )
        self.mesh: Mesh | None = None
        # pre-auth reads (login screen, name checks) — directory only, no store.
        # make_transport so the login screen works on a cloud root too, not just
        # a folder (the FolderTransport hard-wire here broke supabase:// roots).
        self._tx0 = make_transport(self.root, home=self.home)
        self.directory0 = Directory(self._tx0)
        # cloud roots: start the mirror's first bulk load NOW so the first
        # /api/mesh/state finds it hot instead of paying the warm-up (R29)
        warm = getattr(self._tx0, "warm_async", None)
        if callable(warm):
            warm()
        self._lock = threading.RLock()
        self._sync_thread: threading.Thread | None = None
        self._subs: set[Subscription] = set()
        # V111 app lock: an optional local passphrase gate — starts LOCKED
        # whenever configured (launch always asks); routing.authed enforces it
        self.lock = AppLock(self.home)

    # ------------------------------------------------------------- session
    @property
    def user(self) -> str | None:
        return self.mesh.user if self.mesh else None

    @property
    def transport(self):
        """The ONE shared transport — the Mesh rides the pre-auth instance
        (R29), so this is valid signed in or out."""
        return self._tx0

    @property
    def _session_path(self) -> Path:
        return self.home / _SESSION_FILE

    @property
    def restoring(self) -> bool:
        """V125: a session exists but its restore is still blind (cold cloud
        transport at boot). The frontend shows the boot/connecting surface
        instead of the sign-in page — which read as "the app signed me out"
        during every post-restart warm-up."""
        return (self.mesh is None
                and getattr(self, "_restore_retrying", False)
                and self._session_path.exists())

    def restore(self) -> None:
        """Re-attach the signed-in user from the session file, if the account
        still exists and (under E2EE) this machine still holds its keys.

        V122: never raises, and only AUTHORITATIVE evidence may delete the
        session file. A directory that can't be read at boot (cold cloud
        transport, network blip) is not evidence the account is gone — it
        used to hit the unlink anyway, turning a transient hiccup into a
        hard sign-out ("the app signs out", three live reports). Blind or
        failed restores keep the session and retry in the background; the
        frontend already flips to chats the moment a user appears."""
        doc = read_json(self._session_path, default=None)
        name = (doc or {}).get("user")
        if not name:
            return
        try:
            names = self.directory0.names()
        except Exception:  # noqa: BLE001 — transport not ready
            names = []
        if not names:
            # can't SEE the directory (a mesh always has members) — transient
            _breadcrumb("restore: directory unreadable (cold transport?) — "
                        "session kept, retrying in background")
            self._schedule_restore_retry()
            return
        acc = self.directory0.get(name)
        if acc is None or not acc.active or acc.auth is None:
            _breadcrumb(f"restore: account {name!r} gone — session cleared")
            self._session_path.unlink(missing_ok=True)   # authoritative: gone
            return
        if self.encrypt and (
            KeyStore(self.home).load(name) is None  # local private bundle gone
            or not acc.keys.sign_pub                 # account not yet key-published
        ):
            # A migrated account starts keyless: it must go through the
            # upgrading LOGIN (which publishes its identity + shows the
            # recovery code) — never silently restore into a half-state where
            # it can read plaintext history but can't seal a new message. A
            # fresh machine (no local bundle) likewise re-logs in. (The
            # keystore is a local, deterministic read, and the directory was
            # just proven healthy — this branch stays authoritative.)
            _breadcrumb(f"restore: {name!r} must log in again (identity "
                        "keys) — session cleared")
            self._session_path.unlink(missing_ok=True)
            return
        try:
            with self._lock:
                self._adopt(self._build(name))
            _breadcrumb(f"restore: attached {name!r}")
        except Exception as e:  # noqa: BLE001 — a failed attach is transient too
            _breadcrumb(f"restore: attach failed ({type(e).__name__}) — "
                        "retrying in background")
            self._schedule_restore_retry()

    def _schedule_restore_retry(self, *, every_s: float = 5.0,
                                cap_s: float = 600.0) -> None:
        """Single-flight background re-restore (V122). Runs until a user is
        attached, the session file disappears (real sign-out), or the cap —
        then the auth page simply stands (login always works)."""
        import threading
        import time as _time

        with self._lock:
            if getattr(self, "_restore_retrying", False):
                return
            self._restore_retrying = True

        def run() -> None:
            deadline = _time.time() + cap_s
            try:
                while _time.time() < deadline:
                    _time.sleep(every_s)
                    if self.user or not self._session_path.exists():
                        return
                    with self._lock:
                        self._restore_retrying = False
                    self.restore()   # reschedules itself if still blind
                    return
            finally:
                with self._lock:
                    self._restore_retrying = False

        threading.Thread(target=run, daemon=True,
                         name="ab-restore-retry").start()

    def signup(self, name: str, display: str, password: str) -> dict:
        """V124: refuses while someone is signed in. V68 password-gated
        logout precisely because the next sign-in claims this machine's
        agents — an ungated signup was a credential-free bypass around that
        gate (sign up, session swaps, agents follow). The UI never offers
        signup while signed in, so only a script or a passer-by hits this;
        the legitimate swap is logout (password) → signup."""
        with self._lock:
            if self.mesh is not None:
                raise ValidationError("Already signed in — sign out first")
            mesh = self._build(name)
            try:
                _, code = mesh.accounts.create_human(
                    name, password, display=display
                )
            except Exception:
                mesh.close()  # release the half-built facade
                raise
            self._adopt(mesh)
        return {"ok": True, "user": name, "recovery_code": code}

    def login(self, name: str, password: str) -> dict:
        with self._lock:
            # V130 (V124's twin): login was a session swap needing only the
            # CALLER's credentials — a passer-by with their own account
            # could adopt this machine (and claim its agents) without the
            # signed-in user's password. The UI never offers login while
            # signed in; the legitimate swap is logout (password) → login.
            if self.mesh is not None:
                raise ValidationError("Already signed in — sign out first")
            return self._login_locked(name, password)

    def _login_locked(self, name: str, password: str) -> dict:
        """The login body — the caller holds ``self._lock`` (V130 keeps the
        signed-out guard and the adopt in ONE lock span, so no session can
        appear between the check and the swap)."""
        acc = self.directory0.get(name)
        if acc is None:
            # V125: a cold transport (boot warm-up, network blip) makes the
            # directory unreadable — "Wrong username or password" would be a
            # lie that reads as a broken account (live report: correct
            # credentials refused for minutes after a restart). Say what is
            # actually happening; the same "no members visible" rule as
            # restore() keeps this from masking a real bad username.
            try:
                blind = not self.directory0.names()
            except Exception:  # noqa: BLE001 — unreadable = blind
                blind = True
            if blind:
                raise ValidationError(
                    "Still connecting to your mesh — try again in a moment")
        if acc is None or not acc.active or acc.auth is None:
            # agents and deleted accounts fail exactly like a bad password
            raise ValidationError("Wrong username or password")
        mesh = self._build(name)
        if not mesh.verify_password(name, password):
            mesh.close()  # a failed login never drops anything
            raise ValidationError("Wrong username or password")
        # first v2 sign-in of a migrated account: scrypt + identity keys
        code = mesh.accounts.upgrade_login(name, password)
        if mesh.keystore.load(name) is None:
            mesh.accounts.unlock(password)  # new machine: unwrap + cache
        try:
            # D19: login claims; the facade posts the V69 owner-changed
            # departure pills before ownership moves
            mesh.claim_machine_agents()
        except Exception:  # noqa: BLE001 — claiming must not block login
            pass
        self._adopt(mesh)
        out = {"ok": True, "user": name}
        if code:
            out["recovery_code"] = code
        return out

    def logout(self, password: str = "") -> dict:
        """V68: signing out requires the member's password. The machine-claim
        on the NEXT sign-in transfers this machine's agents to whoever signs
        in, so a passer-by at an unlocked device must not be able to swap the
        session. This gates the only in-app path to switch users (one signed-in
        human per GuiApp). Session RESTORE across restarts stays password-free
        — the keystore already holds the unlocked identity, and no user swap
        happens there."""
        with self._lock:
            mesh = self.mesh
            if mesh is not None:
                if not mesh.accounts.verify_password(mesh.user, password):
                    raise ValidationError("Password is incorrect")
            self._detach()
            self._session_path.unlink(missing_ok=True)
        return {"ok": True}

    def close(self) -> None:
        with self._lock:
            self._detach()

    # ------------------------------------------------------------ internals
    def _build(self, name: str) -> Mesh:
        # the Mesh RIDES the pre-auth transport instead of building its own:
        # on a cloud root that shares ONE warm mirror + realtime channel per
        # GUI process (R29). Mesh.close() never closes its transport, so a
        # logout/login cycle keeps _tx0 valid.
        return Mesh(
            self._tx0,
            name,
            self.machine,
            encrypt=self.encrypt,
            home=self.home,
            app_version=self.app_version,
        )

    def _adopt(self, mesh: Mesh) -> None:
        self.mesh = mesh
        mesh.start()
        try:  # R25: populate tenure + re-sign any legacy redactions (idempotent)
            mesh.harden_startup()
        except Exception:  # noqa: BLE001 — hardening must not block login
            pass
        try:
            mesh.applink.announce(["gui"])
        except Exception:  # noqa: BLE001 — presence lane must not block login
            pass
        self._sync_thread = threading.Thread(
            target=mesh.sync.run,
            kwargs={"poll_s": self.poll_s},
            daemon=True,
            name="gui-sync",
        )
        self._sync_thread.start()
        atomic_write_json(
            self._session_path, {"user": mesh.user, "ts": utcnow_iso()}
        )

    def _detach(self) -> None:
        mesh, self.mesh = self.mesh, None
        if mesh is None:
            return
        for sub in list(self._subs):
            sub.close()
        self._subs.clear()
        # stop sync FIRST and wait for its loop to leave the store, THEN close
        mesh.sync.stop()
        t, self._sync_thread = self._sync_thread, None
        if t is not None:
            t.join(timeout=self.poll_s + 10)
        mesh.close()

    # ----------------------------------------------------------------- SSE
    def subscribe(self) -> Subscription | None:
        with self._lock:
            if self.mesh is None:
                return None
            sub = self.mesh.bus.subscribe()
            self._subs.add(sub)
            return sub

    def release(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)
            if self.mesh is not None:
                self.mesh.bus.unsubscribe(sub)
