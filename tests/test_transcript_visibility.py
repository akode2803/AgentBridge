"""Python/GUI parity for deciding whether an info item renders a row."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentbridge.core.models import Envelope, Message, MsgKind
from agentbridge.mesh.readmodel import build_messages, transcript_visible
from agentbridge.mesh.sealer import PlainSealer


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "gui/static/js/state.js"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _mesh_info_source() -> str:
    source = STATE.read_text(encoding="utf-8")
    start = source.index("export function meshInfoText(")
    end = source.index("// NOTE: the server mirrors", start)
    return source[start:end].replace("export ", "", 1)


def _js_visibility(tmp_path: Path, cases: list[dict]) -> list[bool]:
    runner = tmp_path / "mesh-info.mjs"
    runner.write_text(
        "const Mesh = {state: {users: {}}};\n"
        "const meshDn = (user) => user || '';\n"
        + _mesh_info_source()
        + "\nconst cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(({msg, me}) => "
          "Boolean(meshInfoText(msg, me)))));\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [shutil.which("node") or "node", str(runner)],
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@requires_node
def test_transcript_visibility_matches_real_mesh_info_text_matrix(tmp_path):
    visible_events = (
        "created", "member_added", "member_removed", "member_left", "renamed",
        "description", "permissions_changed", "avatar", "chat_deleted",
    )
    cases = [
        {"name": kind, "event": {"type": kind}, "body": ""}
        for kind in visible_events
    ] + [
        {"name": "admin_granted_self", "event": {"type": "admin_granted", "who": "me"}},
        {"name": "admin_granted_other", "event": {"type": "admin_granted", "who": "other"}},
        {"name": "admin_revoked_self", "event": {"type": "admin_revoked", "who": "me"}},
        {"name": "admin_revoked_other", "event": {"type": "admin_revoked", "who": "other"}},
        {"name": "key_rotated", "event": {"type": "key_rotated"}, "body": "fallback"},
        {"name": "reaction", "event": {"type": "reaction"}, "body": "fallback"},
        {"name": "unknown_empty", "event": {"type": "future_event"}, "body": ""},
        {"name": "unknown_body", "event": {"type": "future_event"}, "body": "fallback"},
        {"name": "noevent_empty", "event": None, "body": ""},
        {"name": "noevent_body", "event": None, "body": "fallback"},
    ]
    js_cases = [
        {"me": "me", "msg": {
            "from": "actor", "event": case.get("event"), "body": case.get("body", ""),
        }}
        for case in cases
    ]
    js_visible = _js_visibility(tmp_path, js_cases)
    python_visible = [
        transcript_visible(Message(
            id=case["name"], kind=MsgKind.INFO, from_="actor",
            event=case.get("event"), body=case.get("body", ""),
        ), "me")
        for case in cases
    ]
    assert python_visible == js_visible, [
        (case["name"], py, js)
        for case, py, js in zip(cases, python_visible, js_visible)
        if py != js
    ]


def test_build_messages_keeps_info_items_independent_of_transcript_visibility():
    events = [
        {"type": "created"},
        {"type": "admin_granted", "who": "other"},
        {"type": "admin_revoked", "who": "me"},
        {"type": "key_rotated"},
        {"type": "future_event"},
        None,
    ]
    envelopes = [
        Envelope(
            id=f"i{index}", ns=index, ts="t", from_="actor",
            kind=MsgKind.INFO, event=event,
        ).to_dict()
        for index, event in enumerate(events, 1)
    ]
    folded = build_messages("room", "me", envelopes, PlainSealer())
    assert [message.id for message in folded] == [f"i{i}" for i in range(1, 7)]
    assert [message.event for message in folded] == events
    assert [transcript_visible(message, "me") for message in folded] == [
        True, False, True, False, False, False,
    ]

    reaction = Envelope(
        id="reaction", ns=7, ts="t", from_="actor", kind=MsgKind.INFO,
        event={"type": "reaction"},
    ).to_dict()
    # Existing default breadcrumb suppression is unchanged; the harness opt-in
    # still exposes the complete fold, while transcript rendering suppresses it.
    assert build_messages("room", "me", [reaction], PlainSealer()) == []
    breadcrumb = build_messages(
        "room", "me", [reaction], PlainSealer(), breadcrumbs=True,
    )
    assert [message.id for message in breadcrumb] == ["reaction"]
    assert transcript_visible(breadcrumb[0], "me") is False
