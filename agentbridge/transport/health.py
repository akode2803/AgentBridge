"""Normalize transport failures into stable recovery classes.

Provider exception types are optional dependencies and have changed between
Supabase client releases.  This module therefore reads their common public
attributes and falls back to the exception text without importing a provider.
"""

from __future__ import annotations

import re

__all__ = [
    "classify_transport_error",
    "retry_inline",
    "retry_delay",
    "transport_error_message",
]

_STATUS_RE = re.compile(
    r"(?<!\d)(400|401|402|403|404|405|406|408|409|416|422|429|5\d\d)(?!\d)"
)


def _text(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    for name in ("code", "message", "details", "hint"):
        value = getattr(exc, name, None)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _status(exc: BaseException, text: str) -> int | None:
    response = getattr(exc, "response", None)
    for value in (
        getattr(response, "status_code", None),
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    match = _STATUS_RE.search(text)
    return int(match.group(1)) if match else None


def classify_transport_error(exc: BaseException) -> str:
    """Return a stable state used by retries and the GUI connection surface."""
    text = _text(exc)
    status = _status(exc, text)

    if status == 402 or "payment required" in text or "fair use" in text:
        return "restricted"
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    if (
        status == 401
        or any(code in text for code in ("pgrst301", "pgrst302", "pgrst303"))
        or "jwt expired" in text
        or "invalid jwt" in text
        or "invalid login credentials" in text
    ):
        return "auth_error"
    if status == 403 or "42501" in text or "row-level security policy" in text:
        return "permission_error"
    if status in (400, 404, 405, 406, 409, 416, 422) or any(
        mark in text for mark in (
            "23505", "42703", "pgrst100", "pgrst102", "pgrst105",
            "pgrst204", "validationerror", "bad transport path",
        )
    ):
        return "configuration_error"
    if status == 408 or "timeout" in text or "timed out" in text:
        return "offline"
    if any(
        mark in text for mark in (
            "connecterror", "connectionerror", "networkerror", "dns",
            "name or service not known", "nodename nor servname",
            "temporary failure in name resolution", "connection refused",
            "connection reset", "network is unreachable", "no route to host",
            "sslerror", "certificate verify failed", "tls",
        )
    ):
        return "offline"
    if status is not None and status >= 500:
        return "service_error"
    if any(code in text for code in (
        "pgrst000", "pgrst001", "pgrst002", "pgrst003",
        "realtimerestarting", "databaseconnectionissue",
        "unabletoconnecttoproject", "unabletoconnecttotenantdatabase",
    )):
        return "service_error"
    return "service_error"


def retry_inline(kind: str) -> bool:
    """Whether one foreground operation should make another bounded attempt."""
    return kind in {"offline", "service_error"}


def retry_delay(kind: str, fallback_s: float = 10.0) -> float:
    """Circuit-breaker floor after a failed background refresh."""
    if kind in {"restricted", "auth_error", "permission_error",
                "configuration_error"}:
        return 300.0
    if kind == "rate_limited":
        return 60.0
    if kind == "service_error":
        return max(30.0, fallback_s)
    return max(5.0, fallback_s)


def transport_error_message(kind: str) -> str:
    """Credential-free text suitable for the local GUI."""
    return {
        "restricted": "Cloud access is restricted until the provider resets usage.",
        "rate_limited": "Cloud requests are rate limited; retrying later.",
        "auth_error": "Cloud authentication failed; check this machine's member login.",
        "permission_error": "Cloud row security refused the request.",
        "configuration_error": "Cloud storage configuration needs attention.",
        "offline": "The network is unavailable or too slow.",
        "service_error": "The cloud service is unavailable; retrying in the background.",
    }.get(kind, "The transport is unavailable; retrying in the background.")
