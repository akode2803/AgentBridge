"""Current page diagnostics and retained core projection observations stay private."""

from __future__ import annotations

import json
from itertools import count
from types import SimpleNamespace

from agentbridge.gui import diagnostics
from agentbridge.gui.projection_perf import ProjectionObservation


def _records(recorder):
    recorder.flush()
    paths = [recorder.directory / f"events.{n}.jsonl" for n in (2, 1)] + [recorder.path]
    return [json.loads(line) for path in paths if path.exists()
            for line in path.read_text().splitlines()]


def test_chat_and_sidebar_diagnostics_are_content_free_and_detect_bounded_work(rig, monkeypatch):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Observed", members=[])["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=cid, body="projection-private-body")
    # Prepare an initial cut; later reads may still request background work.
    assert rig.sidebar()["chats_complete"]
    assert rig.page(cid)["status"] == "page"
    # Model sparse observations for retained-stage coverage. Separate diagnostic
    # tests cover burst dropping under the unchanged production rate limit.
    real_clock = diagnostics.time
    sparse_clock = SimpleNamespace(
        monotonic=count(real_clock.monotonic(), 0.025).__next__,
        perf_counter=real_clock.perf_counter,
        sleep=real_clock.sleep,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(diagnostics, "time", sparse_clock)
        assert rig.app.diagnostics.set_enabled(True, sample_rate=1)
        # Intervening reads may request background work and invalidate the
        # prepared cut. Wait for real canonical readiness within fixture bounds.
        sidebar = rig.sidebar()
        chat = rig.page(cid)
    assert chat["status"] == "page"
    assert chat["session_binding"] == sidebar["session_binding"]
    assert chat["messages"][-1]["body"] == "projection-private-body"
    assert chat["starred_scope"] == "page"
    assert next(item for item in sidebar["chats"] if item["id"] == cid)["last"]
    rows = _records(rig.app.diagnostics)
    assert rig.app.diagnostics.configuration()["rate_dropped"] == 0
    assert any(row.get("route") == "/api/mesh/chat_page" and row.get("phase") == "finalize"
               and row.get("status") == "page" and row["raw_examined"] <= 1000 for row in rows)
    assert any(row.get("route") == "/api/mesh/state" and row.get("event") == "server_request"
               and row["chats_complete"] for row in rows)
    encoded = json.dumps(rows)
    for private in ("projection-private-body", cid, "aryan", "Observed"):
        assert private not in encoded


def test_denied_chat_diagnostics_have_no_room_plaintext_or_payload(rig):
    rig.signup()
    rig.peer_account("fable")
    with rig.peer_mesh("fable") as outsider:
        private = outsider.create_chat("Private", []).id
    assert rig.page(private)["status"] == "forbidden"
    assert rig.app.diagnostics.set_enabled(True, sample_rate=1)
    denied = rig.get("/api/mesh/chat_page", id=private)
    assert denied["status"] == "forbidden"
    assert "messages" not in denied and "users" not in denied
    rows = _records(rig.app.diagnostics)
    assert any(row.get("route") == "/api/mesh/chat_page" and row.get("status") == "forbidden" for row in rows)
    assert private not in json.dumps(rows)


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
