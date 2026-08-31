"""P0 content-free projection observations and endpoint characterization."""

from __future__ import annotations

import json

from agentbridge.gui.projection_perf import ProjectionObservation
from agentbridge.mesh.service import Mesh


def _records(home):
    path = home / "gui" / "perf" / "projections.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def test_chat_and_sidebar_observations_are_content_free_and_detect_two_folds(rig):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Observed", members=[])["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=cid, body="projection-private-body")

    sidebar = rig.get("/api/mesh/state")
    chat = rig.get("/api/mesh/chat", id=cid)
    assert set(chat) == {"meta", "messages", "me", "starred", "read_ns", "total"}
    assert chat["messages"][-1]["body"] == "projection-private-body"
    assert next(item for item in sidebar["chats"] if item["id"] == cid)["last"]

    rows = _records(rig.home)
    sidebar_row = next(row for row in reversed(rows) if row["scope"] == "sidebar")
    chat_row = next(row for row in reversed(rows) if row["scope"] == "chat")
    assert sidebar_row["outcome"] == chat_row["outcome"] == "ok"
    assert sidebar_row["counts"]["room_count"] >= 1
    assert chat_row["counts"]["fold_calls"] == 2
    assert chat_row["counts"]["raw_messages"] >= chat["total"] * 2
    assert chat_row["stages_s"]["transcript_fold"] >= 0
    assert chat_row["stages_s"]["receipts_fold"] >= 0
    encoded = json.dumps(rows)
    for private in ("projection-private-body", cid, "aryan", "Observed"):
        assert private not in encoded


def test_denied_chat_observation_has_no_room_correlation_or_counts(rig):
    rig.signup()
    rig.peer_account("fable")
    outsider = Mesh(
        rig.root, "fable", "peerbox", home=rig.home,
        store_path=rig.home / "fable-observation.sqlite",
    )
    try:
        private = outsider.create_chat("Private", []).id
    finally:
        outsider.close()
    assert "error" in rig.get("/api/mesh/chat", id=private)
    row = _records(rig.home)[-1]
    assert row["scope"] == "chat" and row["outcome"] == "denied_or_error"
    assert row["counts"] == {}
    assert private not in json.dumps(row)


def test_projection_observer_failure_never_changes_filtered_messages(rig):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Observer", members=[])["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=cid, body="same result")
    mesh = rig.app.mesh
    expected = mesh.messages_for(cid)

    class BrokenObserver:
        def stage(self, _name, _seconds):
            raise RuntimeError("diagnostics unavailable")

        def count(self, _name, _value=1):
            raise RuntimeError("diagnostics unavailable")

    actual = mesh.messages_for(cid, observer=BrokenObserver())
    assert actual == expected


def test_projection_observation_ignores_unowned_fields(tmp_path):
    observation = ProjectionObservation("chat")
    observation.stage("not-a-stage", 10)
    observation.count("body", 100)
    row = observation.record("ok")
    assert row["stages_s"] == {} and row["counts"] == {}
    assert set(row) == {
        "v", "ts", "request_ref", "scope", "outcome", "total_s",
        "stages_s", "counts",
    }
