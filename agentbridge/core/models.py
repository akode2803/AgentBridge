"""Core data model for format v2 (docs/FORMAT2.md).

Design rules:
- Enums are ``str`` subclasses so records JSON-serialize without adapters.
- ``from_dict`` is TOLERANT: unknown keys are ignored (mesh peers may run
  newer app versions), and unknown enum values **fail closed** — they coerce
  to the most restrictive option, never the most permissive.
- Models are transport- and crypto-agnostic: ``Envelope`` is the at-rest view,
  ``Message`` is the decrypted read-model the services hand out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

__all__ = [
    "UserKind", "ChatKind", "MsgKind", "Audience", "Role", "PermLevel",
    "ReceiptState", "WrappedKey", "AccountKeys", "Privacy", "Status",
    "AgentInfo", "AgentRules", "Account", "PresenceRecord", "Member",
    "ChatPermissions", "ChatSnapshot", "Envelope", "BodyRecord", "Message",
]


# --------------------------------------------------------------------------- enums

class UserKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class ChatKind(str, Enum):
    DM = "dm"
    GROUP = "group"
    SELF = "self"
    CHANNEL = "channel"  # reserved (R5 keeps permissions config-driven for it)


class MsgKind(str, Enum):
    MESSAGE = "message"
    INFO = "info"


class Audience(str, Enum):
    """Who may see a privacy-gated surface / pass a permission gate (R6)."""

    EVERYONE = "everyone"
    MEMBERS = "members"   # shares >=1 chat with me (D13 — no contact book)
    AGENTS = "agents"     # agents (+ their owner members where the matrix says so)
    NOBODY = "nobody"


class Role(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"


class PermLevel(str, Enum):
    ALL = "all"
    ADMINS = "admins"


class ReceiptState(str, Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"


def _coerce(enum_cls: type[Enum], value: Any, fail_closed: Enum) -> Enum:
    """Parse an enum value; UNKNOWN values fail closed, never open."""
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return fail_closed


def _known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys this dataclass declares (tolerant forward-compat)."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


# --------------------------------------------------------------------------- keys

@dataclass
class WrappedKey:
    """A secret wrapped by a derived key (password / recovery code): D5."""

    salt: str = ""
    nonce: str = ""
    ct: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "WrappedKey | None":
        return cls(**_known(cls, d)) if isinstance(d, dict) else None


@dataclass
class AccountKeys:
    sign_pub: str = ""    # b64 Ed25519 public
    agree_pub: str = ""   # b64 X25519 public
    wrapped_priv: WrappedKey | None = None  # humans only; agents keep keys local
    recovery: WrappedKey | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AccountKeys":
        d = d or {}
        return cls(
            sign_pub=d.get("sign_pub", ""),
            agree_pub=d.get("agree_pub", ""),
            wrapped_priv=WrappedKey.from_dict(d.get("wrapped_priv")),
            recovery=WrappedKey.from_dict(d.get("recovery")),
        )


# --------------------------------------------------------------------------- privacy / profile

@dataclass
class Privacy:
    """The R6 matrix. ``messaging``/``add_to_group`` are PUBLIC by design so an
    agent can check the gate before messaging instead of being silently
    blocked; every other field is private to the permission layer."""

    last_seen: Audience = Audience.EVERYONE
    online: Audience = Audience.EVERYONE
    photo: Audience = Audience.EVERYONE
    about: Audience = Audience.EVERYONE
    status: Audience = Audience.EVERYONE
    read_receipts: bool = True
    view_read_receipts: bool = True
    messaging: Audience = Audience.EVERYONE
    add_to_group: Audience = Audience.EVERYONE

    def __post_init__(self) -> None:
        for name in ("last_seen", "online", "photo", "about", "status",
                     "messaging", "add_to_group"):
            setattr(self, name, _coerce(Audience, getattr(self, name), Audience.NOBODY))

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Privacy":
        return cls(**_known(cls, d or {}))


@dataclass
class Status:
    state: str = "available"  # available | busy | dnd | ... (free-form vocab)
    text: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Status":
        return cls(**_known(cls, d or {}))


@dataclass
class AgentInfo:
    owner: str = ""     # exactly ONE responsible human (account model v2)
    machine: str = ""   # agent identity = name + machine + owner
    harness: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AgentInfo | None":
        return cls(**_known(cls, d)) if isinstance(d, dict) else None


@dataclass
class AgentRules:
    """Owner-set OUTBOUND rules for an agent — the one asymmetric piece of the
    R6 model: who may the agent message / add to groups. (The agent's own
    ``privacy`` block covers the symmetric inbound side.)"""

    messaging: Audience = Audience.EVERYONE
    add_to_group: Audience = Audience.EVERYONE

    def __post_init__(self) -> None:
        for name in ("messaging", "add_to_group"):
            setattr(self, name, _coerce(Audience, getattr(self, name), Audience.NOBODY))

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AgentRules":
        return cls(**_known(cls, d or {}))


@dataclass
class Account:
    """One record in ``users/<name>.json`` — the user file IS the account."""

    name: str = ""
    kind: UserKind = UserKind.HUMAN
    display: str = ""
    about: str = ""
    created: str = ""
    active: bool = True
    keys: AccountKeys = field(default_factory=AccountKeys)
    auth: dict[str, Any] | None = None  # humans only (scrypt record, R7)
    privacy: Privacy = field(default_factory=Privacy)
    blocked: list[str] = field(default_factory=list)
    status: Status = field(default_factory=Status)
    agent: AgentInfo | None = None
    agent_rules: AgentRules | None = None  # owner-set outbound rules (agents)

    def __post_init__(self) -> None:
        self.kind = _coerce(UserKind, self.kind, UserKind.AGENT)  # closed = fewer rights

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.auth is None:
            d.pop("auth")
        if self.agent is None:
            d.pop("agent")
        if self.agent_rules is None:
            d.pop("agent_rules")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        base = _known(cls, d)
        base["keys"] = AccountKeys.from_dict(d.get("keys"))
        base["privacy"] = Privacy.from_dict(d.get("privacy"))
        base["status"] = Status.from_dict(d.get("status"))
        base["agent"] = AgentInfo.from_dict(d.get("agent"))
        base["agent_rules"] = (
            AgentRules.from_dict(d["agent_rules"]) if isinstance(d.get("agent_rules"), dict)
            else None
        )
        return cls(**base)

    def rules(self) -> AgentRules:
        """Outbound rules with the everyone-default applied."""
        return self.agent_rules or AgentRules()


@dataclass
class PresenceRecord:
    """``presence/<user>@<machine>.json`` — per-device, merged by readers."""

    user: str = ""
    machine: str = ""
    online: bool = False
    last_seen: str = ""
    last_seen_ns: int = 0
    app: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PresenceRecord":
        return cls(**_known(cls, d))


# --------------------------------------------------------------------------- chats

@dataclass
class Member:
    role: Role = Role.MEMBER
    joined_ns: int = 0

    def __post_init__(self) -> None:
        self.role = _coerce(Role, self.role, Role.MEMBER)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Member":
        return cls(**_known(cls, d or {}))


@dataclass
class ChatPermissions:
    """Group permission toggles (R5) — config-driven so channels reuse them.
    Unknown values fail closed to ADMINS."""

    edit_settings: PermLevel = PermLevel.ALL   # name/icon/description/timer/pins
    send_messages: PermLevel = PermLevel.ALL
    add_members: PermLevel = PermLevel.ALL
    # history-on-join. Default True — deliberate divergence from WhatsApp's
    # off-default: agents joining a room usually NEED the context (memory
    # open-reminder #5); admins can switch it off per group.
    send_history: bool = True
    approve_members: bool = False               # admins approve joins
    # Agent adds are governed EXCLUSIVELY by these two (Aryan 2026-07-12,
    # "agents are tied to their owners"); agents can never remove members.
    agents_add_if_owner_admin: bool = True      # add allowed while owner is admin
    agents_add_if_members_can: bool = False     # add allowed when add_members=all

    def __post_init__(self) -> None:
        for name in ("edit_settings", "send_messages", "add_members"):
            setattr(self, name, _coerce(PermLevel, getattr(self, name), PermLevel.ADMINS))

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ChatPermissions":
        return cls(**_known(cls, d or {}))


@dataclass
class ChatSnapshot:
    """``meta.json`` — a REBUILDABLE cache materialized from info events
    (FORMAT2 tenet 3). Never treat it as the source of truth."""

    id: str = ""
    kind: ChatKind = ChatKind.GROUP
    name: str = ""
    description: str = ""
    members: dict[str, Member] = field(default_factory=dict)
    permissions: ChatPermissions = field(default_factory=ChatPermissions)
    auto_dm: bool = False  # agent-DM born as a small group (dedup marker, v1)
    key_epoch: int = 1
    materialized_ns: int = 0

    def __post_init__(self) -> None:
        self.kind = _coerce(ChatKind, self.kind, ChatKind.GROUP)

    def admins(self) -> list[str]:
        return [n for n, m in self.members.items() if m.role is Role.ADMIN]

    def is_member(self, user: str) -> bool:
        return user in self.members

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatSnapshot":
        base = _known(cls, d)
        base["members"] = {
            n: Member.from_dict(m) for n, m in (d.get("members") or {}).items()
        }
        base["permissions"] = ChatPermissions.from_dict(d.get("permissions"))
        return cls(**base)


# --------------------------------------------------------------------------- messages

@dataclass
class Envelope:
    """One line of ``msgs/<sender>.jsonl`` — the AT-REST record.

    ``kind=message``: body is in ``ct`` (encrypted BodyRecord, epoch/nonce/sig).
    ``kind=info``: plaintext ``event`` — info events ARE the chat-state log.
    """

    id: str = ""
    ns: int = 0
    ts: str = ""
    from_: str = ""
    kind: MsgKind = MsgKind.MESSAGE
    epoch: int = 0
    nonce: str = ""
    ct: str = ""
    sig: str = ""
    event: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.kind = _coerce(MsgKind, self.kind, MsgKind.MESSAGE)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["from"] = d.pop("from_")  # canonical JSON field name
        if self.event is None:
            d.pop("event")
        if self.kind is MsgKind.INFO:
            for k in ("epoch", "nonce", "ct", "sig"):
                d.pop(k)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Envelope":
        d = dict(d)
        if "from" in d:
            d["from_"] = d.pop("from")
        return cls(**_known(cls, d))


@dataclass
class BodyRecord:
    """The DECRYPTED payload of a message envelope (inside ``ct``)."""

    body: str = ""
    tags: list[str] = field(default_factory=list)
    reply_to: dict[str, Any] | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    fwd: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {})}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BodyRecord":
        return cls(**_known(cls, d))


@dataclass
class Message:
    """The decrypted READ-MODEL handed out by the mesh services — always
    already filtered through membership + overlays (edits/redactions/hidden/
    cleared), so no caller ever sees a body it shouldn't."""

    id: str = ""
    chat_id: str = ""
    from_: str = ""
    ns: int = 0
    ts: str = ""
    kind: MsgKind = MsgKind.MESSAGE
    body: str = ""
    tags: list[str] = field(default_factory=list)
    reply_to: dict[str, Any] | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    fwd: dict[str, Any] | None = None
    edited: dict[str, Any] | None = None
    deleted: bool = False
    event: dict[str, Any] | None = None
    reactions: dict[str, list[str]] = field(default_factory=dict)  # {emoji: [users]}
