"""Rejected local diagnostic bodies fit a bounded TCP discard window."""
from __future__ import annotations

import io
from types import SimpleNamespace

from agentbridge.gui.app import Handler, MAX_REJECT_DRAIN


def _handler(body):
    stream = io.BufferedReader(io.BytesIO(body))
    timeouts = []
    connection = SimpleNamespace(
        gettimeout=lambda: 9,
        settimeout=timeouts.append,
    )
    return SimpleNamespace(rfile=stream, connection=connection), timeouts


def test_rejected_diagnostic_payload_is_drained_and_original_timeout_restored():
    size = 70 * 1024
    handler, timeouts = _handler(b'x' * size)

    Handler._prepare_body_rejection(handler, size)

    assert handler.close_connection
    assert handler.rfile.read() == b''
    assert timeouts and 0 < timeouts[0] <= 0.1
    assert timeouts[-1] == 9


def test_hostile_declared_body_never_expands_the_discard_cap():
    handler, timeouts = _handler(b'untouched')

    Handler._prepare_body_rejection(handler, MAX_REJECT_DRAIN + 1)

    assert handler.close_connection
    assert handler.rfile.read() == b'untouched'
    assert timeouts == []
