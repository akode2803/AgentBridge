"""R185 selective immutable mirror observations."""

from __future__ import annotations

import pytest
import threading
from agentbridge.transport import mirror_observation as observation

from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.mirror_observation import (
    MirrorCaptureUnavailable, MirrorExpectedPosition, MirrorSelection,
    MirrorSelectionRequest,
)
from tests.test_mirror_observation import MemoryProvider


@pytest.fixture
def mirror():
    provider = MemoryProvider()
    cached = CachingTransport(provider, auto_refresh=False)
    provider.docs = {
        "chats/one/meta.json": {"name": "one"},
        "lifecycle/a/one.json": {"record": 1},
        "lifecycle/b/two.json": {"record": 2},
        "users/a.json": None,
    }
    cached.refresh()
    return provider, cached


def _request(expected=None, exact=("chats/one/meta.json",), prefixes=(), **limits):
    return MirrorSelectionRequest(expected, exact, prefixes,
                                  limits.get("max_records", 16),
                                  limits.get("max_bytes", 100_000),
                                  limits.get("max_examined_paths", 100_000))


def test_exact_present_absent_null_and_complete_prefix_are_sorted_detached(mirror):
    _provider, cached = mirror
    selection = cached.capture_mirror_selection(_request(
        exact=("users/a.json", "users/missing.json", "chats/one/meta.json"),
        prefixes=("lifecycle/",),
    ))
    assert type(selection) is MirrorSelection
    assert selection.exact_records == (
        ("chats/one/meta.json", '{"name":"one"}'),
        ("users/a.json", "null"), ("users/missing.json", None),
    )
    assert tuple(record.path for record in selection.complete_prefixes[0].records) == (
        "lifecycle/a/one.json", "lifecycle/b/two.json",
    )
    with pytest.raises(AttributeError):
        selection.exact_records.append(None)


def test_expected_mismatch_and_base_transport_fail_closed_without_records(mirror):
    provider, cached = mirror
    current = cached.capture_mirror_selection(_request())
    assert type(current) is MirrorSelection
    forged = MirrorExpectedPosition(current.position.root_identity,
                                    current.position.cache_identity,
                                    current.position.instance_nonce,
                                    current.position.revision + 1)
    changed = cached.capture_mirror_selection(_request(expected=forged))
    assert isinstance(changed, MirrorCaptureUnavailable) and changed.reason == "changed"
    unsupported = provider.capture_mirror_selection(_request())
    assert isinstance(unsupported, MirrorCaptureUnavailable) and unsupported.reason == "unsupported"


def test_selector_limits_and_overlap_reject_before_capture(mirror):
    _provider, cached = mirror
    for request in (
        _request(exact=("users/a.json", "users/a.json")),
        _request(prefixes=("lifecycle/", "lifecycle/a/")),
        _request(exact=("lifecycle/a/one.json",), prefixes=("lifecycle/",)),
    ):
        with pytest.raises(ValueError):
            cached.capture_mirror_selection(request)
    limited = cached.capture_mirror_selection(_request(prefixes=("lifecycle/",),
                                                        max_examined_paths=1))
    assert isinstance(limited, MirrorCaptureUnavailable) and limited.reason == "budget_exceeded"


def test_selection_serialization_holds_one_revision_against_writer(mirror, monkeypatch):
    _provider, cached = mirror
    entered, release = threading.Event(), threading.Event()
    attempted, acquired = threading.Event(), threading.Event()
    original = observation._serialized_document
    original_lock = cached._lock
    captured, errors = [], []

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    def collect():
        try:
            captured.append(cached.capture_mirror_selection(_request(prefixes=("lifecycle/",))))
        except BaseException as exc:
            errors.append(exc)

    def mutate():
        try:
            cached.put_doc("chats/one/meta.json", {"name": "changed"})
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=collect)
    writer = threading.Thread(target=mutate)

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is writer:
                attempted.set()
            original_lock.acquire()
            if threading.current_thread() is writer:
                acquired.set()

        def __exit__(self, *args):
            original_lock.release()

    monkeypatch.setattr(cached, "_lock", ObservedLock())
    monkeypatch.setattr(observation, "_serialized_document", paused)
    reader.start()
    try:
        assert entered.wait(5)
        writer.start()
        # Writer has completed JSON normalization and reached the actual lock.
        assert attempted.wait(5)
        assert not acquired.is_set()
    finally:
        release.set()
        reader.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not reader.is_alive() and not writer.is_alive() and not errors
    assert acquired.is_set()
    assert type(captured[0]) is MirrorSelection
    assert dict(captured[0].exact_records)["chats/one/meta.json"] == '{"name":"one"}'
    assert cached.validate_mirror_position(captured[0].position).status == "changed"
    assert cached.get_doc("chats/one/meta.json") == {"name": "changed"}
