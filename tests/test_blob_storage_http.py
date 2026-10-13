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
