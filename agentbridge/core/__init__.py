"""Core layer: models, time/ordering, config, errors.

Depends on nothing else in the package; every other layer depends on it.
"""

from .config import DEFAULT_HOME, atomic_write_json, load_app_config, read_json, save_app_config
from .errors import (
    AgentBridgeError,
    ConfigError,
    CryptoError,
    NotAMember,
    PermissionDenied,
    StoreError,
    TransportError,
    ValidationError,
)
from .models import (
    Account,
    AccountKeys,
    AgentInfo,
    Audience,
    BodyRecord,
    ChatKind,
    ChatPermissions,
    ChatSnapshot,
    Envelope,
    Member,
    Message,
    MsgKind,
    PermLevel,
    PresenceRecord,
    Privacy,
    ReceiptState,
    Role,
    Status,
    UserKind,
    WrappedKey,
)
from .timekit import new_id, next_ns, utcnow, utcnow_iso

__all__ = [
    # config
    "DEFAULT_HOME", "read_json", "atomic_write_json", "load_app_config", "save_app_config",
    # errors
    "AgentBridgeError", "ConfigError", "TransportError", "StoreError", "CryptoError",
    "ValidationError", "PermissionDenied", "NotAMember",
    # models
    "UserKind", "ChatKind", "MsgKind", "Audience", "Role", "PermLevel", "ReceiptState",
    "WrappedKey", "AccountKeys", "Privacy", "Status", "AgentInfo", "Account",
    "PresenceRecord", "Member", "ChatPermissions", "ChatSnapshot", "Envelope",
    "BodyRecord", "Message",
    # timekit
    "utcnow", "utcnow_iso", "next_ns", "new_id",
]
