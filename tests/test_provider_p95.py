import json

import pytest

from agentbridge.benchmarks import provider_p95 as benchmark


def test_nearest_rank_percentile_and_twenty_sample_summary():
    rows = []
    for i in range(1, 21):
        rows.append({
            "post_return_ms": i, "first_feed_ms": i * 2, "reply_ms": i * 3,
            "claim_to_preparation_ms": i * 4,
            "preparation_to_feed_ms": i * 5, "provider_ms": i * 6,
            "access_seen": i != 1,
        })
    out = benchmark.summary({"target": 20, "completed": rows})
    assert out["metrics"]["reply_ms"]["p50"] == 30
    assert out["metrics"]["reply_ms"]["p95"] == 57
    assert out["access_seen"] == 19


def test_summary_refuses_to_call_partial_run_p95():
    out = benchmark.summary({"target": 20, "completed": [{}] * 19})
    assert "p95 requires" in out["note"] and "metrics" not in out


def test_saved_state_must_match_requested_benchmark(tmp_path):
    path = tmp_path / "state.json"
    state = benchmark.load_state(
        path, base="http://local", agent="codex", samples=20, mode="shared")
    benchmark.save_state(path, state)
    assert json.loads(path.read_text())["target"] == 20
    with pytest.raises(ValueError, match="does not match"):
        benchmark.load_state(
            path, base="http://other", agent="codex", samples=20,
            mode="shared")


def test_fresh_and_shared_state_cannot_be_mixed(tmp_path):
    path = tmp_path / "state.json"
    state = benchmark.load_state(
        path, base="http://local", agent="codex", samples=20, mode="fresh")
    benchmark.save_state(path, state)
    with pytest.raises(ValueError, match="does not match"):
        benchmark.load_state(
            path, base="http://local", agent="codex", samples=20,
            mode="shared")


def test_feed_match_requires_current_message_transition_id():
    message_id = "m-current"
    feeds = [
        {"run_id": "r-old", "transition_id": "chat|m-old@0"},
        {"run_id": "r-current", "transition_id": f"chat|{message_id}@0"},
    ]
    matching = benchmark.matching_feeds(feeds, message_id)
    assert [item["run_id"] for item in matching] == ["r-current"]


def test_summary_omits_incomplete_metric_instead_of_fabricating_p95():
    rows = [{"reply_ms": i, "access_seen": True} for i in range(20)]
    rows[0]["reply_ms"] = None
    out = benchmark.summary({"target": 20, "completed": rows})
    assert "reply_ms" not in out["metrics"]
