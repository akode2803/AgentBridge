"""Independent bounded provider reads for inactive recovery comparison.

These pages deliberately do not carry authority.  A reference collector binds
the complete sequence to equal opening and closing recovery fences; any source
movement makes the private run unusable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .recovery_sources import RecoveryStream, validate_recovery_after
from .scoped_sources import (
    ScopedDocumentRow, ScopedLogRow, validate_source_cursor,
)


def _page(rows: object, expected: type, *, limit: int, more: object) -> tuple:
    if (type(limit) is not int or limit < 1 or type(rows) is not tuple
            or len(rows) > limit
            or any(type(row) is not expected for row in rows)
            or type(more) is not bool or more and not rows):
        raise ValueError("invalid reference source page")
    return rows


@dataclass(frozen=True)
class ReferenceDocumentPage:
    after: str
    rows: tuple[ScopedDocumentRow, ...]
    has_more: bool
    limit: int

    def __post_init__(self) -> None:
        rows = _page(self.rows, ScopedDocumentRow,
                     limit=self.limit, more=self.has_more)
        keys = tuple(row.path for row in rows)
        if (type(self.after) is not str or keys != tuple(sorted(set(keys)))
                or any(key <= self.after for key in keys)):
            raise ValueError("invalid reference document keyset")

    @property
    def cursor(self) -> str:
        return self.rows[-1].path if self.rows else self.after


@dataclass(frozen=True)
class ReferenceStreamPage:
    after: tuple[str, str]
    rows: tuple[RecoveryStream, ...]
    has_more: bool
    limit: int

    def __post_init__(self) -> None:
        validate_recovery_after(self.after, "streams")
        rows = _page(self.rows, RecoveryStream,
                     limit=self.limit, more=self.has_more)
        keys = tuple((row.chat_id, row.log_name) for row in rows)
        if (keys != tuple(sorted(set(keys)))
                or any(key <= self.after for key in keys)):
            raise ValueError("invalid reference stream order")

    @property
    def cursor(self) -> tuple[str, str]:
        if not self.rows:
            return self.after
        row = self.rows[-1]
        return (row.chat_id, row.log_name)


@dataclass(frozen=True)
class ReferenceLogPage:
    after_cursor: int
    through_cursor: int
    rows: tuple[ScopedLogRow, ...]
    has_more: bool
    limit: int

    def __post_init__(self) -> None:
        after = validate_source_cursor(
            self.after_cursor, "reference log cursor")
        through = validate_source_cursor(
            self.through_cursor, "reference log head")
        rows = _page(self.rows, ScopedLogRow,
                     limit=self.limit, more=self.has_more)
        ids = tuple(row.row_id for row in rows)
        if (after > through or ids != tuple(sorted(set(ids)))
                or any(row_id <= after or row_id > through for row_id in ids)):
            raise ValueError("invalid reference log keyset")

    @property
    def cursor(self) -> int:
        return self.rows[-1].row_id if self.rows else self.after_cursor


__all__ = [
    "ReferenceDocumentPage", "ReferenceLogPage", "ReferenceStreamPage",
]
