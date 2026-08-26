import pytest

from agentbridge.benchmarks import realtime as MODULE


def sample(at, instance="same", queries=0, rx=0):
    return {
        "at": at, "instance_id": instance,
        **{name: 0 for name in MODULE.COUNTERS},
        "queries": queries, "rx_bytes": rx,
    }


def test_report_differences_and_labels_short_extrapolation():
    out = MODULE.report(
        sample(100, queries=10, rx=1000),
        sample(150, queries=15, rx=5353),
    )
    assert out["elapsed_s"] == 50
    assert out["delta"]["queries"] == 5
    assert out["delta"]["rx_bytes"] == 4353
    assert out["hourly_extrapolation"]["rx_bytes"] == pytest.approx(313416)
    assert any("not p95" in caveat for caveat in out["caveats"])


def test_report_refuses_cross_restart_counter_math():
    with pytest.raises(ValueError, match="process changed"):
        MODULE.report(sample(1, "old"), sample(2, "new"))
