"""timekit: the ns-ordering primitive (regression home of the v1 ns-tie bug)."""

import threading

from agentbridge.core import timekit


def test_next_ns_strictly_increases_in_burst():
    # v1 bug class: same-second ties skipped messages. ns must NEVER tie.
    seen = [timekit.next_ns() for _ in range(10_000)]
    assert all(b > a for a, b in zip(seen, seen[1:]))


def test_next_ns_thread_safe_no_duplicates():
    out: list[int] = []
    lock = threading.Lock()

    def burst():
        vals = [timekit.next_ns() for _ in range(2_000)]
        with lock:
            out.extend(vals)

    threads = [threading.Thread(target=burst) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == len(set(out)), "duplicate ns issued across threads"


def test_new_id_shape_and_uniqueness():
    a, b = timekit.new_id("m"), timekit.new_id("m")
    assert a != b
    prefix, ns, rand = a.split("-")
    assert prefix == "m" and int(ns) > 0 and len(rand) == 8


def test_ts_is_display_only_second_resolution():
    ts = timekit.utcnow_iso()
    # documents the contract: ISO seconds, Z-suffixed — never used for ordering
    assert ts.endswith("Z") and len(ts) == 20
