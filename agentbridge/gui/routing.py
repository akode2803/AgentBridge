"""Request/response primitives shared by the endpoint modules."""

from __future__ import annotations

import functools
import json
import logging
import time
from dataclasses import dataclass, field

from ..core.errors import AgentBridgeError
from ..core import delivery_trace
from .diagnostics import assign_request_sequence

log = logging.getLogger("agentbridge.gui")

__all__ = ["Request", "Response", "authed", "authed_read",
           "authed_read_token", "dispatch"]


@dataclass
class Request:
    method: str = "GET"
    path: str = ""
    params: dict = field(default_factory=dict)  # query string (GET)
    data: dict = field(default_factory=dict)    # JSON body (POST)
    diagnostic_sequence: int | None = None
    diagnostic_ref: str | None = None

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
        if lock is not None and lock.expire_if_idle():
            return {"error": "App is locked", "locked": True}
        mesh = app.mesh
        if mesh is None:
            return {"error": "Sign in first"}
        return fn(app, req, mesh, *args)

    return wrapper


def authed_read(fn):
    """Fence an explicitly selected read response against session changes."""
    @functools.wraps(fn)
    def wrapper(app, req, *args):
        lock = getattr(app, "lock", None)
        if lock is not None and lock.expire_if_idle():
            return {"error": "App is locked", "locked": True}
        token = app.capture_session_read()
        if token is None or token.mesh is None:
            return {"error": "Sign in first"}
        try:
            result = fn(app, req, token.mesh, *args)
        except Exception:
            if not app.validate_session_read(token):
                return {"error": "Sign in first"}
            raise
        if not app.validate_session_read(token):
            return {"error": "Sign in first"}
        return result

    return wrapper


def authed_read_token(fn):
    """Fence a read and pass its exact R187 token to the assembler."""
    @functools.wraps(fn)
    def wrapper(app, req, *args):
        lock = getattr(app, "lock", None)
        if lock is not None and lock.expire_if_idle():
            return {"error": "App is locked", "locked": True}
        token = app.capture_session_read()
        if token is None or token.mesh is None:
            return {"error": "Sign in first"}
        try:
            result = fn(app, req, token.mesh, token, *args)
        except Exception:
            if not app.validate_session_read(token):
                return {"error": "Sign in first"}
            raise
        if not app.validate_session_read(token):
            return {"error": "Sign in first"}
        return result

    return wrapper


def dispatch(handler, app, req, *args):
    """Run one endpoint with the v1 error contract: domain errors come back
    as ``{"error": ...}`` JSON (HTTP 200), never as an HTML error page."""
    started = time.perf_counter()
    diagnostics = None
    try:
        diagnostics = getattr(app, 'diagnostics', None)
        if diagnostics is not None and diagnostics.enabled:
            assign_request_sequence(diagnostics, req)
        else:
            req.diagnostic_sequence = None
    except Exception:
        pass
    diagnostic_generation = getattr(diagnostics, "generation", None)
    failure = None
    with delivery_trace.request_context(req.diagnostic_ref, req.diagnostic_sequence):
        if not req.path.startswith('/api/diagnostics'):
            data = req.params if req.method == 'GET' else req.data
            chat = (data.get('chat_id') or data.get('chat') or data.get('id')) if type(data) is dict else ''
            delivery_trace.emit('request_started', request_ref=req.diagnostic_ref, route=req.path, chat=chat)
        try:
            result = handler(app, req, *args)
        except AgentBridgeError as e:
            failure = e
            result = {"error": str(e)}
        except json.JSONDecodeError:
            result = {"error": "malformed JSON body"}
        except Exception as e:  # noqa: BLE001 — a bug must never kill the socket
            failure = e
            log.exception("endpoint %s failed", req.path)
            result = {"error": f"internal error: {e}"}
    if not req.path.startswith('/api/diagnostics'):
        try:
            enabled = (diagnostics is not None and diagnostics.enabled
                       and delivery_trace.recorder() is diagnostics
                       and getattr(diagnostics, "generation", None) == diagnostic_generation)
        except Exception:
            enabled = False
        if enabled:
            try:
                status = ('bytes' if isinstance(result, Response) else
                          result.get('status', 'error' if result.get('error') else
                                     'ok' if result.get('ok') else 'ready')
                          if type(result) is dict else 'other')
                event = {'event': 'server_request', 'route': req.path,
                         'duration_ms': (time.perf_counter() - started) * 1000,
                         'status': status, 'request_seq': req.diagnostic_sequence,
                         'request_ref': req.diagnostic_ref, 'phase': 'request_finished'}
                message_ref = None
                if (req.method == 'POST' and req.path == '/api/mesh/post'
                        and type(result) is dict and result.get('ok') is True
                        and not result.get('error') and type(result.get('id')) is str):
                    message_ref = diagnostics.chat_ref(result['id'])
                    if message_ref is not None:
                        event['trace_ref'] = message_ref
                if type(result) is dict:
                    event['reason'] = (str(failure) if failure is not None else
                                       result.get('reason') or result.get('sidebar_status') or 'none')
                    for key in ('messages', 'items', 'chats'):
                        if type(result.get(key)) is list:
                            event[key] = len(result[key])
                    if type(result.get('chats_complete')) is bool:
                        event['chats_complete'] = result['chats_complete']
                if failure is not None:
                    event['error_type'] = type(failure).__name__
                elif type(result) is dict and result.get('error'):
                    event['error_type'] = 'DomainError'
                data = req.params if req.method == 'GET' else req.data
                if type(data) is dict:
                    chat = data.get('chat_id') or data.get('chat') or data.get('id')
                    ref = diagnostics.chat_ref(chat)
                    if ref is not None:
                        event['chat_ref'] = ref
                # Ref calculation can race opt-out. Recheck and serialize both
                # admission and response enrichment with generation/owner changes.
                with delivery_trace.recorder_fence(diagnostics, diagnostic_generation) as active:
                    if active:
                        diagnostics.flight_record(event)
                        if message_ref is not None:
                            result = {**result, '_diagnostics': {'trace_ref': message_ref,
                                 'chat_ref': diagnostics.chat_ref(chat)}}
            except Exception:  # noqa: BLE001 — telemetry must not affect replies
                pass
    return result
