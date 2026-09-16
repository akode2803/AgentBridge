"""Privacy and bounds for display-only selected-chat presentation."""

from __future__ import annotations

from dataclasses import dataclass

from agentbridge.gui.api_chats import _chat_presentation
from agentbridge.mesh.service import Mesh


@dataclass
class _Account:
    avatar: dict | None = None


class _Directory:
    def __init__(self, accounts):
        self.accounts = accounts
        self.reads = []

    def get(self, name):
        self.reads.append(name)
        return self.accounts.get(name)


class _Mesh:
    user = "viewer"

    def __init__(self, accounts, profiles):
        self.directory = _Directory(accounts)
        self.profiles = profiles

    def visible_profile(self, name):
        return dict(self.profiles.get(name, {}))


def test_selected_presentation_is_relevant_privacy_filtered_and_detached():
    accounts = {
        "viewer": _Account(),
        "member": _Account({"sha256": "a" * 64, "updated": "now", "raw": "secret"}),
        "sender": _Account({"sha256": "b" * 64}),
        "unrelated": _Account({"sha256": "c" * 64}),
    }
    profiles = {
        "viewer": {"display": "Viewer", "kind": "human", "owner": "hidden"},
        "member": {"display": "Member", "kind": "agent", "photo_visible": True,
                   "machine": "hidden", "active": True},
        "sender": {"display": "Sender", "kind": "human", "photo_visible": False,
                   "sign_pub": "hidden"},
        "unrelated": {"display": "Nope", "kind": "agent", "photo_visible": True},
    }
    mesh = _Mesh(accounts, profiles)
    result = _chat_presentation(
        mesh, ["viewer", "member"], [{"from": "sender"}, {"from": "member"}],
    )

    assert result == {
        "user": "viewer",
        "users": {
            "viewer": {"display": "Viewer", "display_kind": "human"},
            "member": {"display": "Member", "display_kind": "agent",
                       "avatar": {"sha256": "a" * 64, "updated": "now"}},
            "sender": {"display": "Sender", "display_kind": "human"},
        },
    }
    assert "unrelated" not in mesh.directory.reads
    profiles["member"]["display"] = "changed later"
    accounts["member"].avatar["sha256"] = "changed later"
    assert result["users"]["member"]["display"] == "Member"
    assert result["users"]["member"]["avatar"]["sha256"] == "a" * 64
    wire = repr(result)
    for forbidden in ("owner", "machine", "active", "sign_pub", "raw", "photo_visible"):
        assert forbidden not in wire


def test_selected_presentation_enforces_count_and_utf8_budget():
    names = [f"u{i:03d}" for i in range(300)]
    accounts = {name: _Account() for name in ["viewer", *names]}
    profiles = {
        name: {"display": "\U0001f642" * 256, "kind": "human"}
        for name in accounts
    }
    mesh = _Mesh(accounts, profiles)
    result = _chat_presentation(mesh, names, [])

    assert len(result["users"]) <= 256
    assert list(result["users"])[0] == "viewer"
    # The production owner charges canonical per-entry UTF-8 JSON before
    # retaining each record and stops before crossing its 64 KiB budget.
    import json

    charged = sum(
        len(json.dumps({name: value}, ensure_ascii=False).encode("utf-8"))
        for name, value in result["users"].items()
    )
    assert charged <= 64 * 1024
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 64 * 1024
    assert len(result["users"]) < len(accounts)


def test_selected_presentation_retains_exactly_256_short_profiles():
    names = [f"u{i:03d}" for i in range(300)]
    accounts = {name: _Account() for name in ["viewer", *names]}
    profiles = {name: {"display": name, "kind": "human"} for name in accounts}
    result = _chat_presentation(_Mesh(accounts, profiles), names, [])
    assert len(result["users"]) == 256
    assert list(result["users"])[0] == "viewer"
    assert "u254" in result["users"]
    assert "u255" not in result["users"]


def test_missing_profiles_still_bound_authority_lookups_to_256_names():
    names = [f"missing{i:03d}" for i in range(300)]
    mesh = _Mesh({"viewer": _Account()}, {"viewer": {"display": "Viewer"}})
    result = _chat_presentation(mesh, names, [])
    assert result["users"] == {"viewer": {"display": "Viewer"}}
    assert len(mesh.directory.reads) == 256
    assert mesh.directory.reads[0] == "viewer"
    assert mesh.directory.reads[-1] == "missing254"


def test_outsider_chat_denial_never_returns_presentation(rig):
    rig.signup()
    rig.peer_account("fable")
    outsider = Mesh(
        rig.root, "fable", "peerbox", home=rig.home,
        store_path=rig.home / "fable-presentation.sqlite",
    )
    try:
        private = outsider.create_chat("Private", [])
    finally:
        outsider.close()
    denied = rig.get("/api/mesh/chat", id=private.id)
    assert "error" in denied
    assert "presentation" not in denied


def test_hidden_or_missing_profile_fields_fall_back_without_authority_data():
    mesh = _Mesh(
        {"viewer": _Account(), "missing": _Account(), "bad": _Account({"sha256": 7})},
        {"viewer": {}, "missing": {"display": "", "kind": "machine"},
         "bad": {"display": "B", "kind": "agent", "photo_visible": True}},
    )
    result = _chat_presentation(mesh, ["missing", "bad", "absent"], [])
    assert result["users"]["viewer"] == {"display": "viewer"}
    assert result["users"]["missing"] == {"display": "missing"}
    assert result["users"]["bad"] == {"display": "B", "display_kind": "agent"}
    assert "absent" not in result["users"]
