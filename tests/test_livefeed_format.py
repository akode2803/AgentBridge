from agentbridge.gui.livefeed import expand_runs, suppress_superseded_preparing


def test_preparing_lane_is_visible_until_matching_live_run_exists():
    preparing = expand_runs("status/helper_preparing.json", {
        "kind": "run-set", "agent": "helper", "runs": [{
            "run_id": "waiting-a", "transition_id": "message-a",
            "state": "running", "activity": "Preparing secure runtime",
        }],
    })
    assert preparing[0]["preparing"] is True
    assert suppress_superseded_preparing(preparing) == preparing

    live = expand_runs("status/helper_live.json", {
        "kind": "run-set", "agent": "helper", "runs": [{
            "run_id": "run-a", "transition_id": "message-a",
            "state": "running", "activity": "Working",
        }],
    })
    visible = suppress_superseded_preparing(preparing + live)
    assert [run["run_id"] for run in visible] == ["run-a"]
