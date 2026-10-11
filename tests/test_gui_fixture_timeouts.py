"""HTTP fixture timeouts remain terminal and expose only bounded code frames."""
from __future__ import annotations

import urllib.request

import pytest

import conftest
from conftest import _fixture_response


@pytest.mark.parametrize('phase', ['open', 'read'])
def test_timeout_preserves_identity_deadline_and_request_without_retry(monkeypatch, phase):
    marker = TimeoutError('disposable timeout')
    private = 'private-request-marker'
    request = urllib.request.Request(
        'http://127.0.0.1/disposable?token=' + private, data=private.encode())
    calls = []
    closed = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)

        def read(self):
            raise marker

    def opening(value, *, timeout):
        calls.append((value, timeout))
        if phase == 'open':
            raise marker
        return Response()

    monkeypatch.setattr(urllib.request, 'urlopen', opening)
    with pytest.raises(TimeoutError) as raised:
        with _fixture_response(request) as response:
            response.read()
    assert raised.value is marker
    assert calls == [(request, 10)]
    assert closed == ([True] if phase == 'read' else [])
    assert len(marker.__notes__) == 1
    note = marker.__notes__[0]
    assert 'GUI fixture HTTP timeout (10s)' in note
    assert 'conftest.py:' in note
    assert len(note) <= 16000
    assert private not in note and request.full_url not in note


@pytest.mark.parametrize('diagnostic_failure', ['capture', 'attachment'])
def test_diagnostic_failure_does_not_replace_http_timeout(monkeypatch, diagnostic_failure):
    class UnannotatableTimeout(TimeoutError):
        def add_note(self, _note):
            raise RuntimeError('private attachment detail')

    marker = (UnannotatableTimeout if diagnostic_failure == 'attachment'
              else TimeoutError)('disposable timeout')
    calls = []

    def opening(*_args, **_kwargs):
        calls.append(True)
        raise marker

    def unavailable():
        raise RuntimeError('private capture detail')

    monkeypatch.setattr(urllib.request, 'urlopen', opening)
    monkeypatch.setattr(conftest, '_http_timeout_frames', unavailable
                        if diagnostic_failure == 'capture' else lambda: 'code frames')
    with pytest.raises(TimeoutError) as raised:
        with _fixture_response('http://127.0.0.1/disposable'):
            pytest.fail('timed-out request entered a response')
    assert raised.value is marker
    assert calls == [True]
    if diagnostic_failure == 'capture':
        assert marker.__notes__ == [
            'GUI fixture HTTP timeout (10s); code frame capture unavailable']
    else:
        assert not hasattr(marker, '__notes__')
