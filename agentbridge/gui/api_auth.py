"""Auth endpoints: signup / login / logout / check_name.

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
"""

from __future__ import annotations

from ..mesh.accounts import valid_name
from .context import GuiApp

__all__ = ["GET", "POST"]


def signup(app: GuiApp, req) -> dict:
    data = req.data
    return app.signup(
        (data.get("username") or data.get("name") or "").strip().lower(),
        (data.get("display") or "").strip(),
        data.get("password") or "",
    )


def login(app: GuiApp, req) -> dict:
    data = req.data
    return app.login(
        (data.get("username") or data.get("name") or "").strip().lower(),
        data.get("password") or "",
    )


def logout(app: GuiApp, req) -> dict:
    # V68: sign-out is password-gated (the next sign-in claims this machine's
    # agents — a passer-by must not be able to swap the session)
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


GET: dict = {}
POST = {
    "/api/mesh/signup": signup,
    "/api/mesh/login": login,
    "/api/mesh/logout": logout,
    "/api/mesh/check_name": check_name,
}
