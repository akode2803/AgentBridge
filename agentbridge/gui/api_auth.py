"""Auth endpoints: signup / login / logout / check_name + the V111 app lock.

Signup returns the ONE-TIME recovery code (D5) — the frontend must show it
before moving on. Login on a migrated v1 account upgrades it in place
(pbkdf2 -> scrypt, identity keys provisioned) and, when keys were just
minted, returns that same one-time code.

check_name (R53/V24) is the pre-auth username probe behind the sign-in
page's live checking: format + reserved words via the accounts rules,
existence via ``app.directory0`` — the sessionless directory reader built
for exactly this (context.py) but never exposed over HTTP until now. The
directory is public inside a mesh (every member sees the roster), so
existence here leaks nothing the app doesn't already show.

App lock (V111, gui/applock.py): ``unlock`` is public (it IS the door) and
backed off against brute force; ``lock`` and ``set`` are authed. Changing
or removing the passphrase needs the current one — or the account password,
which also unlocks (it already grants a full sign-out/sign-in swap, so
accepting it here loses nothing and saves the forgot-it file surgery).
"""

from __future__ import annotations

from ..mesh.accounts import valid_name
from .context import GuiApp
from .routing import authed

__all__ = ["GET", "POST"]


def _locked(app) -> dict | None:
    """V111: while locked, the session-mutating auth endpoints refuse too —
    a signup/login would swap the session UNDER the lock. Recovery still
    works: unlock accepts the account password, then sign out normally."""
    lock = getattr(app, "lock", None)
    if lock is not None and lock.locked:
        return {"error": "App is locked", "locked": True}
    return None


def signup(app: GuiApp, req) -> dict:
    if (err := _locked(app)) is not None:
        return err
    data = req.data
    return app.signup(
        (data.get("username") or data.get("name") or "").strip().lower(),
        (data.get("display") or "").strip(),
        data.get("password") or "",
    )


def login(app: GuiApp, req) -> dict:
    if (err := _locked(app)) is not None:
        return err
    data = req.data
    return app.login(
        (data.get("username") or data.get("name") or "").strip().lower(),
        data.get("password") or "",
    )


def logout(app: GuiApp, req) -> dict:
    # V68: sign-out is password-gated (the next sign-in claims this machine's
    # agents — a passer-by must not be able to swap the session)
    if (err := _locked(app)) is not None:
        return err
    return app.logout((req.data or {}).get("password") or "")


def check_name(app: GuiApp, req) -> dict:
    """Facts only — the client phrases per mode (signup wants "taken",
    sign-in wants "doesn't exist"). ``hint`` carries the format rule."""
    name = (req.data.get("username") or req.data.get("name") or "").strip().lower()
    if not name:
        return {"ok": True, "name": name, "valid": False, "taken": False, "hint": ""}
    if not valid_name(name):
        return {"ok": True, "name": name, "valid": False, "taken": False,
                "hint": "2-32 characters: lowercase letters, digits, _ or -, "
                        "starting with a letter (reserved words excluded)"}
    try:
        taken = bool(app.directory0.handle_taken(name))
    except Exception:  # noqa: BLE001 — transport hiccup: stay quiet, not wrong
        return {"ok": False, "error": "directory unavailable"}
    return {"ok": True, "name": name, "valid": True, "taken": taken, "hint": ""}


# ------------------------------------------------------------- app lock (V111)
def _account_pw_ok(app: GuiApp, pw: str) -> bool:
    """The signed-in account's password as a lock fallback (see module doc)."""
    try:
        return bool(pw and app.mesh
                    and app.mesh.accounts.verify_password(app.user, pw))
    except Exception:  # noqa: BLE001 — a transport blip reads as "no"
        return False


def applock_unlock(app: GuiApp, req) -> dict:
    """Public — this endpoint IS the lock screen's door. Cooldown first so
    a scripted caller can't outpace the backoff by ignoring errors."""
    lock = app.lock
    if not lock.enabled or not lock.locked:
        lock.unlock()          # not configured / already open: stay open
        return {"ok": True}
    wait = lock.retry_in()
    if wait > 0:
        return {"error": f"Too many tries — wait {int(wait) + 1}s",
                "retry_in_s": round(wait, 1)}
    pw = str(req.data.get("passphrase") or req.data.get("password") or "")
    if lock.verify(pw) or _account_pw_ok(app, pw):
        lock.note_success()
        lock.unlock()
        return {"ok": True}
    wait = lock.note_failure()
    return {"error": "Wrong passphrase"
            + (f" — next try in {int(wait)}s" if wait else ""),
            "retry_in_s": round(wait, 1)}


@authed
def applock_lock(app: GuiApp, req, mesh) -> dict:
    if not app.lock.enabled:
        return {"error": "App lock is not set up"}
    app.lock.lock()
    return {"ok": True, "locked": True}


@authed
def applock_set(app: GuiApp, req, mesh) -> dict:
    """Enable ({passphrase, autolock_min}), change ({passphrase, current}),
    disable ({passphrase: "", current}), or retime ({autolock_min})."""
    lock = app.lock
    data = req.data or {}
    try:
        autolock = max(0, int(data.get("autolock_min")))
    except (TypeError, ValueError):
        autolock = None
    if "passphrase" not in data:
        if autolock is None:
            return {"error": "nothing to change"}
        if not lock.enabled:
            return {"error": "App lock is not set up"}
        lock.set_autolock(autolock)
        return {"ok": True, **lock.status()}
    new = str(data.get("passphrase") or "")
    if lock.enabled:
        current = str(data.get("current") or "")
        if not (lock.verify(current) or _account_pw_ok(app, current)):
            return {"error": "The current passphrase is wrong"}
    if not new:
        lock.remove()
        return {"ok": True, **lock.status()}
    if len(new) < 4:
        return {"error": "Use at least 4 characters"}
    lock.configure(new, lock.autolock_min if autolock is None else autolock)
    return {"ok": True, **lock.status()}


GET: dict = {}
POST = {
    "/api/mesh/signup": signup,
    "/api/mesh/login": login,
    "/api/mesh/logout": logout,
    "/api/mesh/check_name": check_name,
    "/api/applock/unlock": applock_unlock,
    "/api/applock/lock": applock_lock,
    "/api/applock/set": applock_set,
}
