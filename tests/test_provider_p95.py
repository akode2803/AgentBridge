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
        {"run_id": "r-prefix", "transition_id": "chat|m-current-longer@0"},
        {"run_id": "r-current", "transition_id": f"chat|{message_id}@0"},
    ]
    matching = benchmark.matching_feeds(feeds, message_id)
    assert [item["run_id"] for item in matching] == ["r-current"]


def test_summary_omits_incomplete_metric_instead_of_fabricating_p95():
    rows = [{"reply_ms": i, "access_seen": True} for i in range(20)]
    rows[0]["reply_ms"] = None
    out = benchmark.summary({"target": 20, "completed": rows})
    assert "reply_ms" not in out["metrics"]


@pytest.fixture
def sample_driver(tmp_path, monkeypatch):
    import copy

    calls = []
    tick = [0.0]
    binding = {"instance_id": "i", "viewer": "owner", "session_generation": "1"}
    aux = {"status": "ready", "chat_id": "c", "session_binding": binding,
           "page_version": "v", "metadata_status": {"live": "ready", "runtime": "ready"},
           "feeds": [{"run_id": "r-current", "transition_id": "c|m-current@0"}],
           "runs": [{"run_id": "r-current"}]}
    page = {"status": "page", "chat_id": "c", "session_binding": binding,
            "page_version": "v", "messages": [
                {"id": "reply", "from": "agent", "body": "benchmark-ok-1-1"}]}
    sequence = []
    current = [None]

    def request(_base, path, body=None):
        calls.append(path)
        assert not any(retired in path for retired in ("livefeed", "runtime_authority", "/chat?"))
        if path == "/api/mesh/post":
            return {"id": "m-current"}
        if path.startswith("/api/mesh/chat_aux?"):
            current[0] = sequence.pop(0) if sequence else (copy.deepcopy(aux), copy.deepcopy(page))
            return current[0][0]
        if path.startswith("/api/mesh/chat_page?"):
            assert path.endswith("&limit=50")
            return current[0][1]
        if path == "/api/mesh/latency?limit=1000":
            return {"agents": {}}
        raise AssertionError(path)

    def sleep(seconds):
        tick[0] += seconds

    monkeypatch.setattr(benchmark, "request_json", request)
    monkeypatch.setattr(benchmark.time, "sleep", sleep)
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(benchmark.time, "perf_counter", lambda: tick[0])

    def run():
        return benchmark.run_sample("http://offline", "agent", 1, 1,
                                    {"attempt_seq": 0}, tmp_path / "state.json",
                                    room={"id": "c"}, delete_room=False)
    return run, aux, page, sequence, calls


def test_sample_waits_for_complete_matching_canonical_projections(sample_driver):
    import copy
    run, aux, page, sequence, calls = sample_driver
    pending = copy.deepcopy(aux)
    pending["status"] = "pending"
    wrong_version = copy.deepcopy(page)
    wrong_version["page_version"] = "other"
    sequence.extend([(pending, page), (aux, wrong_version), (aux, page)])
    result, error = run()
    assert error is None and result["reply_id"] == "reply" and result["access_seen"]
    assert sum("chat_page?" in path for path in calls) == 3


@pytest.mark.parametrize("status", ["forbidden", "unavailable"])
def test_sample_rejects_terminal_projection_status(sample_driver, status):
    run, aux, page, sequence, _ = sample_driver
    aux["status"] = status
    sequence.append((aux, page))
    result, error = run()
    assert result is None and "unavailable" in error


def test_sample_never_completes_without_current_run_authority(sample_driver):
    run, aux, _, _, _ = sample_driver
    aux["runs"] = [{"run_id": "r-old"}]
    result, error = run()
    assert result is None and "without observed access" in error


def test_sample_never_completes_from_old_message_feed(sample_driver):
    run, aux, _, _, _ = sample_driver
    aux["feeds"][0]["transition_id"] = "c|m-old@0"
    result, error = run()
    assert result is None and "without observed feed" in error


def test_sample_rejects_session_change_after_first_admitted_projection(sample_driver):
    import copy
    run, aux, page, sequence, _ = sample_driver
    incomplete = copy.deepcopy(page)
    incomplete["messages"] = []
    new_aux, new_page = copy.deepcopy(aux), copy.deepcopy(page)
    new_aux["session_binding"]["session_generation"] = "2"
    new_page["session_binding"]["session_generation"] = "2"
    sequence.extend([(aux, incomplete), (new_aux, new_page)])
    result, error = run()
    assert result is None and "session changed" in error


def test_sample_rejects_nonobject_response(sample_driver):
    run, _, page, sequence, _ = sample_driver
    sequence.append(([], page))
    result, error = run()
    assert result is None and "malformed" in error
