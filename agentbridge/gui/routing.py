"""Request/response primitives shared by the endpoint modules."""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass, field

from ..core.errors import AgentBridgeError

log = logging.getLogger("agentbridge.gui")

__all__ = ["Request", "Response", "authed", "dispatch"]


@dataclass
class Request:
    method: str = "GET"
    path: str = ""
    params: dict = field(default_factory=dict)  # query string (GET)
    data: dict = field(default_factory=dict)    # JSON body (POST)

    def int_param(self, name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(self.params.get(name, default))))
        except (TypeError, ValueError):
            return default


@dataclass
class Response:
    """Non-JSON replies (files, avatars). JSON handlers just return a dict."""

    body: bytes = b""
    status: int = 200
    ctype: str = "application/octet-stream"
    headers: dict = field(default_factory=dict)


def authed(fn):
    """Endpoints that need a signed-in session. The handler receives the
    live Mesh as a third argument so it can't forget the check. Raw-body
    handlers get their extra ``raw`` argument passed through.

    V111: the app lock gates HERE, so it covers every data endpoint in one
    place — a lock that only covered the window would be cosmetic (the
    localhost API would still answer). The ``locked`` flag lets the client
    tell this apart from a sign-out."""

    @functools.wraps(fn)
    def wrapper(app, req, *args):
        lock = getattr(app, "lock", None)
        if lock is not None and lock.locked:
            return {"error": "App is locked", "locked": True}
        mesh = app.mesh
        if mesh is None:
            return {"error": "Sign in first"}
        return fn(app, req, mesh, *args)

    return wrapper


def dispatch(handler, app, req, *args):
    """Run one endpoint with the v1 error contract: domain errors come back
    as ``{"error": ...}`` JSON (HTTP 200), never as an HTML error page."""
    try:
        return handler(app, req, *args)
    except AgentBridgeError as e:
        return {"error": str(e)}
    except json.JSONDecodeError:
        return {"error": "malformed JSON body"}
    except Exception as e:  # noqa: BLE001 — a bug must never kill the socket
        log.exception("endpoint %s failed", req.path)
        return {"error": f"internal error: {e}"}
