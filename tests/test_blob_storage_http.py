"""Pure checks for the real Storage HTTP acceptance classifier."""

import pytest
from storage3.exceptions import StorageApiError, StorageException

from devtools.validate_blob_storage_http import (
    GateFailure,
    OBJECT_ALREADY_EXISTS,
    OBJECT_NOT_VISIBLE,
    OBJECT_WRITE_FORBIDDEN,
    _expect_storage_denial,
)


def _raise(exc: Exception) -> None:
    raise exc


@pytest.mark.parametrize(
    ("status", "code", "allowed"),
    (
        (404, "not_found", OBJECT_NOT_VISIBLE),
        (409, "Duplicate", OBJECT_ALREADY_EXISTS),
        (403, "Unauthorized", OBJECT_WRITE_FORBIDDEN),
        ("404", "not_found", OBJECT_NOT_VISIBLE),
    ),
)
def test_expected_storage_denial_requires_exact_status_and_code(
    status, code, allowed,
):
    observed = _expect_storage_denial(
        lambda: _raise(StorageApiError("redacted", code, status)),
        "probe",
        allowed,
    )
    assert observed == {"status": int(status), "code": code}


@pytest.mark.parametrize(
    ("status", "code"),
    (
        (500, "InternalError"),
        (429, "TooManyRequests"),
        (400, "InvalidRequest"),
        (404, "Unauthorized"),
    ),
)
def test_unrelated_or_transient_storage_failure_cannot_satisfy_denial(
    status, code,
):
    with pytest.raises(GateFailure, match="unexpected storage status/code"):
        _expect_storage_denial(
            lambda: _raise(StorageApiError("redacted", code, status)),
            "probe",
            OBJECT_NOT_VISIBLE,
        )


def test_generic_storage_exception_cannot_satisfy_denial():
    with pytest.raises(GateFailure, match="unclassified storage exception"):
        _expect_storage_denial(
            lambda: _raise(StorageException("redacted")),
            "probe",
            OBJECT_NOT_VISIBLE,
        )


def test_success_cannot_satisfy_denial():
    with pytest.raises(GateFailure, match="unexpectedly succeeded"):
        _expect_storage_denial(lambda: None, "probe", OBJECT_NOT_VISIBLE)


class _FakeBucket:
    """Storage's list contract: immediate children, folders without an id,
    name order, at most ``limit`` rows from ``offset``, ``search`` a prefix."""

    def __init__(self, keys):
        self.keys = set(keys)
        self.list_calls = []
        self.remove_calls = []

    def list(self, path, options):
        self.list_calls.append(path)
        prefix = path.rstrip("/") + "/"
        children = {}
        for key in self.keys:
            if key.startswith(prefix):
                head, slash, _rest = key[len(prefix):].partition("/")
                children[head] = None if slash else "id-" + head
        rows = [{"name": name, "id": children[name]} for name in sorted(children)
                if name.startswith(options.get("search", ""))]
        offset = options.get("offset", 0)
        return rows[offset:offset + options["limit"]]

    def remove(self, keys):
        self.remove_calls.append(list(keys))
        gone = [key for key in keys if key in self.keys]
        self.keys -= set(gone)
        return [{"name": key} for key in gone]


def test_listing_walk_pages_past_100_and_recurses_into_folders():
    from devtools.validate_blob_storage_http import _janitor_listing

    room = [f"r/chats/c/files/f{i:03d}" for i in range(150)]
    nested = ["r/chats/c/avatar.png", "r/chats/c/odd/deep/x"]
    staged = ["r/blob-stage/c/b1"]
    bucket = _FakeBucket(room + nested + staged + ["r/chats/other/files/z"])
    assert _janitor_listing(bucket, "r", "c") == sorted(
        key[len("r/"):] for key in room + nested + staged)
    assert bucket.list_calls.count("r/chats/c/files") >= 4  # two pages, twice


def test_listing_remove_batches_100_keys_and_counts_rows():
    from devtools.validate_blob_storage_http import _remove_batched

    paths = [f"chats/c/files/f{i:03d}" for i in range(152)]
    bucket = _FakeBucket(f"r/{path}" for path in paths[:-1])
    assert _remove_batched(bucket, "r", paths) == [100, 51]
    assert [len(call) for call in bucket.remove_calls] == [100, 52]
    assert not bucket.keys
