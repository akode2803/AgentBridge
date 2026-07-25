"""Provider-independent failure classification and retry policy."""

from agentbridge.transport.health import (
    classify_transport_error,
    retry_delay,
    retry_inline,
)


class StatusError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status


def test_terminal_provider_failures_are_not_retried_inline():
    cases = [
        (StatusError(402, "Payment Required"), "restricted"),
        (StatusError(429, "Too Many Requests"), "rate_limited"),
        (RuntimeError("PGRST301: JWT expired"), "auth_error"),
        (RuntimeError("row-level security policy (42501)"), "permission_error"),
        (RuntimeError("column missing (42703)"), "configuration_error"),
    ]
    for exc, expected in cases:
        kind = classify_transport_error(exc)
        assert kind == expected
        assert retry_inline(kind) is False


def test_network_and_service_failures_get_bounded_recovery_delays():
    assert classify_transport_error(TimeoutError("timed out")) == "offline"
    assert classify_transport_error(OSError("temporary failure in name resolution")) \
        == "offline"
    assert classify_transport_error(StatusError(503)) == "service_error"
    assert retry_inline("offline") is True
    assert retry_inline("service_error") is True
    assert retry_delay("restricted", 10) == 300
    assert retry_delay("rate_limited", 10) == 60
