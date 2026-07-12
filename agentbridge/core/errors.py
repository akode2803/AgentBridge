"""Error taxonomy for the v2 backend.

Every layer raises from this tree so callers can catch at the granularity they
need (`except AgentBridgeError` at API boundaries; specific types inside).
"""

from __future__ import annotations

__all__ = [
    "AgentBridgeError",
    "ConfigError",
    "TransportError",
    "StoreError",
    "CryptoError",
    "ValidationError",
    "PermissionDenied",
    "NotAMember",
]


class AgentBridgeError(Exception):
    """Base for every error the backend raises deliberately."""


class ConfigError(AgentBridgeError):
    """Malformed or missing configuration."""


class TransportError(AgentBridgeError):
    """The storage transport failed (sync folder IO, cloud request, ...)."""


class StoreError(AgentBridgeError):
    """The local SQLite cache/outbox failed."""


class CryptoError(AgentBridgeError):
    """Key handling or envelope encryption/decryption failed."""


class ValidationError(AgentBridgeError):
    """A record or request failed shape/content validation."""


class PermissionDenied(AgentBridgeError):
    """The permission layer refused the action (R6 matrix, group perms, ...)."""


class NotAMember(PermissionDenied):
    """Visibility = membership: the caller is not a member of the chat."""
