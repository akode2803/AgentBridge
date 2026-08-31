"""Behavior-neutral observation contract for the current message projection.

This is deliberately not a cache revision or alternate read model. P0 observes
the existing membership-filtered ``messages_for`` choke point; revision
authority remains deferred until every mutation owner has been inventoried.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol, TypeVar

__all__ = ["ProjectionObserver", "observed", "observed_count"]

T = TypeVar("T")


class ProjectionObserver(Protocol):
    def stage(self, name: str, seconds: float) -> None: ...

    def count(self, name: str, value: int = 1) -> None: ...


def observed(observer: ProjectionObserver | None, name: str,
             fn: Callable[[], T]) -> T:
    """Measure one real ownership boundary; diagnostics never change output."""
    started = time.perf_counter()
    value = fn()
    if observer is not None:
        try:
            observer.stage(name, max(0.0, time.perf_counter() - started))
        except Exception:
            pass
    return value


def observed_count(observer: ProjectionObserver | None, name: str,
                   value: int = 1) -> None:
    if observer is not None:
        try:
            observer.count(name, int(value))
        except Exception:
            pass
