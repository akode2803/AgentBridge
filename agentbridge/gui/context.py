"""Session + Mesh lifecycle for the local GUI server.

One GuiApp = one machine's GUI = at most ONE signed-in human (v1 semantics,
kept). The Mesh facade instance exists only while someone is signed in; the
session survives restarts via ``gui_session.json`` in the LOCAL home dir
(never the synced folder) — the E2EE identity bundle already lives in the
local keystore, so restoring a session never needs the password again.
"""

from __future__ import annotations

import platform
import threading
from pathlib import Path

from ..core.config import DEFAULT_HOME, atomic_write_json, read_json
from ..core.errors import ValidationError
from ..core.timekit import utcnow_iso
from ..mesh.directory import Directory
from ..mesh.eventbus import Subscription
from ..mesh.keyring import KeyStore
from ..mesh.service import Mesh
from ..transport.folder import FolderTransport

__all__ = ["GuiApp"]

_SESSION_FILE = "gui_session.json"


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
        self.root = Path(root)
        self.home = Path(home) if home else DEFAULT_HOME
        self.machine = machine or platform.node() or "gui"
        self.encrypt = encrypt
        self.app_version = app_version
        self.poll_s = poll_s
        self.sse_ping_s = sse_ping_s
        # repo layout default: agentbridge/gui/context.py -> <repo>/gui/static
        self.static_dir = (
            Path(static_dir)
            if static_dir
            else Path(__file__).resolve().parents[2] / "gui" / "static"
        )
        self.mesh: Mesh | None = None
        # pre-auth reads (login screen, name checks) — directory only, no store
        self._tx0 = FolderTransport(self.root)
        self.directory0 = Directory(self._tx0)
        self._lock = threading.RLock()
        self._sync_thread: threading.Thread | None = None
        self._subs: set[Subscription] = set()

    # ------------------------------------------------------------- session
    @property
    def user(self) -> str | None:
        return self.mesh.user if self.mesh else None

    @property
    def _session_path(self) -> Path:
        return self.home / _SESSION_FILE

    def restore(self) -> None:
        """Re-attach the signed-in user from the session file, if the account
        still exists and (under E2EE) this machine still holds its keys."""
        doc = read_json(self._session_path, default=None)
        name = (doc or {}).get("user")
        if not name:
            return
        acc = self.directory0.get(name)
        if acc is None or not acc.active or acc.auth is None:
            self._session_path.unlink(missing_ok=True)
            return
        if self.encrypt and (
            KeyStore(self.home).load(name) is None  # local private bundle gone
            or not acc.keys.sign_pub                 # account not yet key-published
        ):
            # A migrated account starts keyless: it must go through the
            # upgrading LOGIN (which publishes its identity + shows the
            # recovery code) — never silently restore into a half-state where
            # it can read plaintext history but can't seal a new message. A
            # fresh machine (no local bundle) likewise re-logs in.
            self._session_path.unlink(missing_ok=True)
            return
        with self._lock:
            self._adopt(self._build(name))

    def signup(self, name: str, display: str, password: str) -> dict:
        with self._lock:
            mesh = self._build(name)
            try:
                _, code = mesh.accounts.create_human(
                    name, password, display=display
                )
            except Exception:
                mesh.close()  # a failed signup never drops the old session
                raise
            self._detach()
            self._adopt(mesh)
        return {"ok": True, "user": name, "recovery_code": code}

    def login(self, name: str, password: str) -> dict:
        acc = self.directory0.get(name)
        if acc is None or not acc.active or acc.auth is None:
            # agents and deleted accounts fail exactly like a bad password
            raise ValidationError("Wrong username or password")
        with self._lock:
            mesh = self._build(name)
            if not mesh.verify_password(name, password):
                mesh.close()  # a failed login never drops the old session
                raise ValidationError("Wrong username or password")
            # first v2 sign-in of a migrated account: scrypt + identity keys
            code = mesh.accounts.upgrade_login(name, password)
            if mesh.keystore.load(name) is None:
                mesh.accounts.unlock(password)  # new machine: unwrap + cache
            try:
                mesh.accounts.claim_machine_agents()  # D19: login claims
            except Exception:  # noqa: BLE001 — claiming must not block login
                pass
            self._detach()
            self._adopt(mesh)
        out = {"ok": True, "user": name}
        if code:
            out["recovery_code"] = code
        return out

    def logout(self) -> dict:
        with self._lock:
            self._detach()
            self._session_path.unlink(missing_ok=True)
        return {"ok": True}

    def close(self) -> None:
        with self._lock:
            self._detach()

    # ------------------------------------------------------------ internals
    def _build(self, name: str) -> Mesh:
        return Mesh(
            self.root,
            name,
            self.machine,
            encrypt=self.encrypt,
            home=self.home,
            app_version=self.app_version,
        )

    def _adopt(self, mesh: Mesh) -> None:
        self.mesh = mesh
        mesh.start()
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
