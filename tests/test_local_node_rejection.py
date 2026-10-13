"""Small rejected bodies have a bounded discard; hostile lengths never do."""
import io
from types import SimpleNamespace

import pytest
from agentbridge.node.server import _discard_rejected_body


@pytest.mark.parametrize('length', ['8', '-1', '65537', 'invalid'])
def test_rejection_discard_is_bounded_and_restores_timeout(length):
    timeouts = []
    stream = io.BufferedReader(io.BytesIO(b'12345678'))
    handler = SimpleNamespace(headers={'Content-Length': length}, rfile=stream,
                              connection=SimpleNamespace(gettimeout=lambda: 2,
                                                         settimeout=timeouts.append))
    _discard_rejected_body(handler)
    if length == '8':
        assert stream.read() == b''
        assert 0 < timeouts[0] <= 0.1
        assert timeouts[-1] == 2
    else:
        assert stream.read() == b'12345678'
        assert timeouts == []


def test_consumed_or_chunked_body_is_not_read_twice():
    for extra, headers in [({'_body_read_started': True}, {}),
                           ({}, {'Transfer-Encoding': 'chunked'})]:
        handler = SimpleNamespace(headers={'Content-Length': '8', **headers}, **extra)
        _discard_rejected_body(handler)  # No stream/socket access permitted.
