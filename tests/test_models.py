"""models: round-trips, tolerance to unknown input, fail-closed enum coercion."""

import json

from agentbridge.core.models import (
    Account,
    Audience,
    ChatKind,
    ChatPermissions,
    ChatSnapshot,
    Envelope,
    Member,
    MsgKind,
    PermLevel,
    Privacy,
    Role,
    UserKind,
)


def test_account_roundtrip_and_json():
    a = Account(name="claude", kind=UserKind.AGENT, display="Claude")
    a.agent = None  # agents normally carry AgentInfo; keep minimal here
    d = a.to_dict()
    assert "auth" not in d and "agent" not in d  # None sections omitted
    assert json.dumps(d)  # str-enums serialize natively
    back = Account.from_dict(json.loads(json.dumps(d)))
    assert back.name == "claude" and back.kind is UserKind.AGENT


def test_from_dict_ignores_unknown_keys():
    # a NEWER peer may write fields we don't know — must not crash
    acc = Account.from_dict({"name": "x", "kind": "human", "hologram": {"v": 3}})
    assert acc.name == "x" and acc.kind is UserKind.HUMAN


def test_unknown_enum_values_fail_closed():
    p = Privacy.from_dict({"last_seen": "quantum-visibility"})
    assert p.last_seen is Audience.NOBODY  # privacy: closed = hidden
    cp = ChatPermissions.from_dict({"send_messages": "vips-only"})
    assert cp.send_messages is PermLevel.ADMINS  # perms: closed = admins
    acc = Account.from_dict({"name": "y", "kind": "cyborg"})
    assert acc.kind is UserKind.AGENT  # kind: closed = fewer rights
    m = Member.from_dict({"role": "superadmin"})
    assert m.role is Role.MEMBER


def test_chat_snapshot_helpers_and_roundtrip():
    snap = ChatSnapshot(
        id="c1",
        kind=ChatKind.GROUP,
        name="QA",
        members={
            "aryan": Member(role=Role.ADMIN, joined_ns=1),
            "claude": Member(role=Role.MEMBER, joined_ns=2),
        },
    )
    assert snap.admins() == ["aryan"]
    assert snap.is_member("claude") and not snap.is_member("eve")
    back = ChatSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert back.members["aryan"].role is Role.ADMIN
    assert back.permissions.send_history is False


def test_envelope_message_vs_info_shapes():
    msg = Envelope(id="m1", ns=10, from_="aryan", kind=MsgKind.MESSAGE,
                   epoch=2, nonce="nn", ct="cc", sig="ss")
    d = msg.to_dict()
    assert d["from"] == "aryan" and d["epoch"] == 2 and "event" not in d

    info = Envelope(id="m2", ns=11, from_="aryan", kind=MsgKind.INFO,
                    event={"type": "member_added", "who": "coco"})
    di = info.to_dict()
    # info events are PLAINTEXT chat-state log lines: no crypto fields at all
    assert di["event"]["who"] == "coco"
    assert all(k not in di for k in ("epoch", "nonce", "ct", "sig"))

    back = Envelope.from_dict(d)
    assert back.from_ == "aryan" and back.kind is MsgKind.MESSAGE
