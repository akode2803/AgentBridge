"""Bounded request-local timing buffers; no recorder work in transactions."""
from __future__ import annotations

import time
from contextlib import nullcontext
from contextvars import ContextVar

PHASES = frozenset({'root_open', 'root_setup', 'root_begin', 'root_checks', 'root_commit',
    'root_rollback', 'root_close', 'store_open', 'store_setup', 'store_begin', 'source_checks',
    'source_rechecks', 'index_checks', 'store_commit', 'store_rollback', 'store_close'})
MAX_PHASE_ROWS = 32
_current = ContextVar('input_phase_timings', default=None)
_disabled = nullcontext()


def _fault(capture):
    try:
        capture.fault()
    except Exception:
        pass


class InputPhaseTimings:
    def __init__(self):
        self.rows = []
        self.dropped = self.faults = 0

    def fault(self):
        self.faults = min(1_000_000, self.faults + 1)


class _Span:
    def __init__(self, capture, phase):
        self.capture, self.phase, self.started = capture, phase, None

    def __enter__(self):
        try:
            self.started = time.perf_counter()
        except Exception:
            _fault(self.capture)
        return None

    def __exit__(self, kind, value, traceback):
        try:
            if self.started is not None:
                duration = (time.perf_counter() - self.started) * 1000
                if len(self.capture.rows) < MAX_PHASE_ROWS:
                    self.capture.rows.append((self.phase, duration, kind is not None))
                else:
                    self.capture.dropped = min(1_000_000, self.capture.dropped + 1)
        except Exception:
            _fault(self.capture)
        return False


def span(phase):
    """Disabled calls allocate nothing and never read a clock."""
    try:
        capture = _current.get()
        if capture is not None and phase in PHASES:
            return _Span(capture, phase)
    except Exception:
        pass
    return _disabled


def begin():
    try:
        capture = InputPhaseTimings()
        return capture, _current.set(capture)
    except Exception:
        return None, None


def end(token):
    if token is not None:
        try:
            _current.reset(token)
        except Exception:
            # A reset failure must not leave an active collector on this thread.
            try:
                _current.set(None)
            except Exception:
                pass


def clear():
    try:
        _current.set(None)
    except Exception:
        pass
