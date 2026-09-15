"""Independent boundary checks for selective mirror capture."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from agentbridge.transport import mirror_observation as observation
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.mirror_observation import (
    MirrorCaptureUnavailable, MirrorSelection, MirrorSelectionRequest,
)
from tests.test_mirror_observation import MemoryProvider


@pytest.fixture
def selected_cache():
    provider = MemoryProvider()
    provider.docs = {
        "users/α.json": {"z": 2, "a": "é"},
        "users/null.json": None,
        "lifecycle/α/one.json": {"v": 1},
    }
    cache = CachingTransport(provider, auto_refresh=False)
    cache.refresh()
    try:
        yield provider, cache
    finally:
        cache.close()


def request(**changes):
    return replace(MirrorSelectionRequest(
        None, ("users/α.json",), (), 20, 100_000,
    ), **changes)


def test_protocol_accounting_exact_limits_and_canonical_unicode(selected_cache):
    _, cache = selected_cache
    query = request(exact_paths=("users/α.json", "users/null.json", "users/missing.json"),
                    complete_prefixes=("lifecycle/",))
    result = cache.capture_mirror_selection(query)
    assert type(result) is MirrorSelection
    assert dict(result.exact_records)["users/α.json"] == '{"a":"é","z":2}'
    # Compute the published protocol formula independently of the implementation.
    def size(s):
        return len(s.encode("utf-8"))
    expected = 16 + sum(8 + size(s) for s in (
        result.position.root_identity, result.position.cache_identity,
        result.position.instance_nonce, result.provenance,
    ))
    expected += sum(8 + size(s) for s in query.exact_paths + query.complete_prefixes)
    expected += sum(9 + size(path) + (size(raw) if raw is not None else 0)
                    for path, raw in result.exact_records)
    for group in result.complete_prefixes:
        expected += 8 + size(group.prefix)
        expected += sum(8 + size(row.path) + size(row.payload_json) for row in group.records)
    assert result.serialized_bytes == expected
    assert cache.capture_mirror_selection(replace(query, max_bytes=expected, max_records=4)) == result
    assert cache.capture_mirror_selection(replace(query, max_bytes=expected - 1)).reason == "budget_exceeded"
    assert cache.capture_mirror_selection(replace(query, max_records=3)).reason == "budget_exceeded"


@pytest.mark.parametrize("limits", [{"max_records": 0}, {"max_bytes": 0}])
def test_budget_preflight_never_touches_payload(selected_cache, monkeypatch, limits):
    _, cache = selected_cache
    monkeypatch.setattr(observation, "_serialized_document",
                        lambda *args: pytest.fail("budget refusal serialized payload"))
    assert cache.capture_mirror_selection(request(**limits)).reason == "budget_exceeded"


def test_byte_overflow_stops_before_next_payload(selected_cache, monkeypatch):
    _, cache = selected_cache
    query = request(exact_paths=("users/null.json", "users/α.json"))
    baseline = cache.capture_mirror_selection(query)
    original = observation._serialized_document
    called = []

    def serial(path, value):
        called.append(path)
        return "x" * baseline.serialized_bytes if len(called) == 1 else original(path, value)

    monkeypatch.setattr(observation, "_serialized_document", serial)
    assert cache.capture_mirror_selection(replace(query, max_bytes=baseline.serialized_bytes)).reason == "budget_exceeded"
    assert called == ["users/null.json"]


@pytest.mark.parametrize("bad", [
    request(exact_paths=["users/α.json"]),
    request(exact_paths=("../secrets",)),
    request(exact_paths=("x" * 4097,)),
    request(exact_paths=("\ud800",)),
    request(exact_paths=tuple(f"users/{i}" for i in range(129))),
    request(complete_prefixes=tuple(f"p{i}/" for i in range(9))),
    request(exact_paths=tuple(f"{i:02d}" + "x" * 4094 for i in range(17))),
    request(max_examined_paths=100_001),
    request(max_records=True),
])
def test_malformed_requests_rejected_before_mutex(selected_cache, monkeypatch, bad):
    _, cache = selected_cache

    @contextmanager
    def forbidden_lock():
        pytest.fail("invalid request entered mirror mutex")
        yield

    monkeypatch.setattr(cache, "_lock", forbidden_lock())
    with pytest.raises(ValueError):
        cache.capture_mirror_selection(bad)


def test_request_and_position_are_detached_before_mutex(selected_cache, monkeypatch):
    _, cache = selected_cache
    first = cache.capture_mirror_selection(request())
    query = request(expected=first.position)
    original_lock = cache._lock

    @contextmanager
    def changed_caller():
        object.__setattr__(query, "exact_paths", ("users/missing.json",))
        object.__setattr__(query.expected, "revision", query.expected.revision + 1)
        with original_lock:
            yield

    monkeypatch.setattr(cache, "_lock", changed_caller())
    result = cache.capture_mirror_selection(query)
    assert type(result) is MirrorSelection
    assert result.exact_records == first.exact_records
    assert result.position.revision + 1 == query.expected.revision


def test_unsupported_cold_and_warm_selection_do_not_read_provider(tmp_path, selected_cache):
    provider, cache = selected_cache
    calls = provider.calls
    assert type(cache.capture_mirror_selection(request())) is MirrorSelection
    assert provider.calls == calls
    cold_provider = MemoryProvider()
    cold = CachingTransport(cold_provider, auto_refresh=False)
    try:
        assert cold.capture_mirror_selection(request()).reason == "cold"
        assert cold_provider.calls == 0
        assert provider.capture_mirror_selection(request()).reason == "unsupported"
        assert FolderTransport(tmp_path).capture_mirror_selection(request()).reason == "unsupported"
    finally:
        cold.close()


@pytest.mark.parametrize("ingress", ["refresh", "delta", "put", "readthrough"])
def test_hostile_stored_key_disables_selection_before_lookup(ingress):
    lookups = []

    class HostileKey(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            lookups.append(other)
            return str.__eq__(self, other)

    provider = MemoryProvider()
    cache = CachingTransport(provider, auto_refresh=False)
    key = HostileKey("users/target.json")
    try:
        if ingress == "refresh":
            provider.docs[key] = {"v": 1}
            cache.refresh()
        else:
            cache.refresh()
            if ingress == "delta":
                provider.delta = ({key: {"v": 1}}, set(), 1)
                cache._refresh_delta()
            elif ingress == "put":
                cache.put_doc(key, {"v": 1})
            else:
                provider.docs[key] = {"v": 1}
                cache.get_doc(key)
        lookups.clear()
        assert cache.capture_mirror_selection(request(exact_paths=(str(key),))).reason == "invalid_payload"
        assert lookups == []
    finally:
        cache.close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_serialization_failure_returns_no_partial_and_does_not_poison(
        selected_cache, monkeypatch, interrupt):
    _, cache = selected_cache
    original = observation._serialized_document

    def fail(*args):
        if interrupt:
            raise KeyboardInterrupt("selection interrupted")
        raise ValueError("bad payload")

    monkeypatch.setattr(observation, "_serialized_document", fail)
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            cache.capture_mirror_selection(request())
    else:
        result = cache.capture_mirror_selection(request())
        assert type(result) is MirrorCaptureUnavailable and result.reason == "invalid_payload"
    monkeypatch.setattr(observation, "_serialized_document", original)
    assert type(cache.capture_mirror_selection(request())) is MirrorSelection


def test_absence_and_same_bytes_delete_reinsert_invalidate_position(selected_cache):
    _, cache = selected_cache
    query = request(exact_paths=("users/new.json",), complete_prefixes=("lifecycle/",))
    before = cache.capture_mirror_selection(query)
    assert before.exact_records == (("users/new.json", None),)
    cache.put_doc("users/new.json", None)
    assert cache.capture_mirror_selection(replace(query, expected=before.position)).reason == "changed"
    captured = cache.capture_mirror_selection(query)
    cache.delete_doc("lifecycle/α/one.json")
    cache.put_doc("lifecycle/α/one.json", {"v": 1})
    after = cache.capture_mirror_selection(query)
    assert after.complete_prefixes == captured.complete_prefixes
    assert cache.capture_mirror_selection(replace(query, expected=captured.position)).reason == "changed"
