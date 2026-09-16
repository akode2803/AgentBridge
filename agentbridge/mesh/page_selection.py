"""Bounded canonical prefix selection over one already-captured local window.

This is not an endpoint or access gate. The caller still owns current authority,
coherent verified overlay inputs, session binding and final position validation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..core.models import Message
from ..store.page_inputs import MessageKey, PageInputPosition, RawPageInputs, SerializedMessage, _key
from ..store.membership_input_position import _copy_expected
from ..store.overlay_index import _wanted, _text
from .readmodel import _blank_reply_quotes, _build_messages_with_redactions, transcript_visible

MAX_VISIBLE = 200
MAX_SCAN = 2000
MAX_PARENTS = 64


class PageDependencyPending(RuntimeError):
    """Exact direct-parent inputs must be captured at the same positions."""

    def __init__(self, ids):
        self.ids = tuple(sorted(ids))
        super().__init__('direct reply parents are not captured')


@dataclass(frozen=True)
class PageSelection:
    position: PageInputPosition
    messages: tuple[Message, ...]
    oldest_examined: MessageKey | None
    raw_examined: int
    parents_examined: int
    parents_required: int
    has_more: bool
    history_exhausted: bool
    scan_budget_exhausted: bool
    needs_more_input: bool
    parent_ids: frozenset[str] = frozenset()


def _snapshot(inputs):
    """Detach keys and positions before projection callbacks can run."""
    if type(inputs) is not RawPageInputs or type(inputs.position) is not PageInputPosition:
        raise ValueError('expected raw page inputs')
    pos = inputs.position
    msg_pos, overlay_pos = pos.messages, pos.overlays
    owned_messages = _copy_expected(msg_pos, msg_pos.database_path)
    position = PageInputPosition(owned_messages,
                                 _wanted(overlay_pos, Path(owned_messages.database_path)))
    rows, exact, absent, lookahead = inputs.rows, inputs.exact_rows, inputs.absent_ids, inputs.lookahead
    if (type(rows) is not tuple or type(exact) is not tuple or type(absent) is not tuple
            or len(rows) > MAX_SCAN or len(exact) > MAX_PARENTS or len(absent) > MAX_PARENTS):
        raise ValueError('invalid bounded input rows')
    copied = []
    unique = {}
    used = 0
    for group in (rows, exact):
        out = []
        for row in group:
            if type(row) is not SerializedMessage:
                raise ValueError('invalid serialized message')
            key, kind, payload = _key(row.key), row.kind, row.payload_json
            _text(kind, 'message kind', 256)
            if type(payload) is not str:
                raise ValueError('invalid message payload')
            owned = SerializedMessage(key, kind, payload)
            if key.id in unique:
                if unique[key.id] != owned:
                    raise ValueError('inconsistent repeated message input')
                owned = unique[key.id]
            else:
                used += len(payload.encode())
                if used > 4 * 1024 * 1024:
                    raise OverflowError('selection payload budget exceeded')
                unique[key.id] = owned
            out.append(owned)
        copied.append(tuple(out))
    for ident in absent:
        _text(ident, 'absent message ID')
    size = inputs.captured_bytes
    if type(size) is not int or size < used or size > 4 * 1024 * 1024:
        raise OverflowError('invalid captured input byte accounting')
    return replace(inputs, captured_bytes=size, position=position, rows=copied[0], exact_rows=copied[1],
                   absent_ids=tuple(i for i in absent),
                   lookahead=None if lookahead is None else _key(lookahead))


def select_message_page(
    inputs: RawPageInputs, viewer: str, sealer, *, limit: int = 50,
    scan_budget: int = 1000, parent_limit: int = MAX_PARENTS,
    _prior_parent_ids: frozenset[str] = frozenset(),
    **fold_inputs,
) -> PageSelection:
    """Select only an examined newest-first prefix; leave prefetch unconsumed.

    `needs_more_input` is an intermediate condition, not a completed visible
    page. Its caller must continue bounded capture/selection with the remaining
    request budgets. Direct parents use the same input cut and separate budget;
    reading them never advances the ordered-scan continuation.
    """
    inputs = _snapshot(inputs)
    if type(viewer) is not str or not viewer:
        raise ValueError('expected viewer')
    if inputs.raw_window_captured is not True:
        raise ValueError('exact-only capture has no history exhaustion evidence')
    if type(limit) is not int or not 1 <= limit <= MAX_VISIBLE:
        raise ValueError('invalid visible limit')
    if type(scan_budget) is not int or not 1 <= scan_budget <= MAX_SCAN:
        raise ValueError('invalid raw scan budget')
    if type(parent_limit) is not int or not 0 <= parent_limit <= MAX_PARENTS:
        raise ValueError('invalid parent budget')
    if len(inputs.rows) > MAX_SCAN or len(inputs.exact_rows) > MAX_PARENTS:
        raise OverflowError('raw input window exceeds selection budget')
    keys = [r.key for r in inputs.rows]
    if keys != sorted(keys, reverse=True) or len({k.id for k in keys}) != len(keys):
        raise ValueError('raw window must have unique IDs in reverse composite order')
    chat = inputs.position.messages.chat_id
    if chat != inputs.position.overlays.chat_id:
        raise ValueError('inconsistent input chat binding')
    # Parent reads can use prefetched rows, but cannot consume their stream keys.
    available = {r.key.id: r for r in inputs.rows + inputs.exact_rows}
    absent = set(inputs.absent_ids)
    if absent.intersection(available):
        raise ValueError('inconsistent exact absence')
    selected, honored, examined_ids = [], set(), set()
    consumed, oldest = 0, None
    for row in inputs.rows:
        if consumed >= scan_budget or len(selected) >= limit:
            break
        projected, redacted = _build_messages_with_redactions(
            chat, viewer, [row.decoded()], sealer, **fold_inputs,
        )
        consumed += 1
        oldest = row.key
        examined_ids.add(row.key.id)
        honored.update(redacted)
        if projected and transcript_visible(projected[0], viewer):
            selected.append(projected[0])
    parents = {}
    for msg in selected:
        if msg.reply_to:
            ident = msg.reply_to.get('id')
            if type(ident) not in (str, int) or not str(ident):
                raise ValueError('invalid direct reply parent ID')
            parents[str(ident)] = ident
    required = set(parents).difference(examined_ids)
    if len(required.union(_prior_parent_ids)) > parent_limit:
        raise OverflowError('direct parent dependency budget exceeded')
    unknown = required.difference(available, absent)
    if unknown:
        raise PageDependencyPending(unknown)
    dependency_ids = sorted(required.intersection(available))
    for ident in dependency_ids:
        _unused, redacted = _build_messages_with_redactions(
            chat, viewer, [available[ident].decoded()], sealer, **fold_inputs,
        )
        honored.update(redacted)
    _blank_reply_quotes(selected, honored)
    more = consumed < len(inputs.rows) or inputs.lookahead is not None
    budget_exhausted = consumed == scan_budget and len(selected) < limit and more
    return PageSelection(
        inputs.position, tuple(reversed(selected)), oldest, consumed,
        len(dependency_ids), len(required), more, not more, budget_exhausted,
        len(selected) < limit and more and not budget_exhausted, frozenset(required),
    )


class MorePageInputRequired(RuntimeError):
    """The request has remaining budgets and needs another contiguous window."""


class CanonicalPageAccumulator:
    """Request-local assembly; never retain across HTTP requests or sessions.

    Callers capture/verify each input window at the same source/message positions
    and still perform final current-authority validation before handout.
    """

    def __init__(self, viewer, sealer, *, limit=50, scan_budget=1000, parent_limit=MAX_PARENTS):
        if type(limit) is not int or not 1 <= limit <= MAX_VISIBLE:
            raise ValueError('invalid visible limit')
        if type(scan_budget) is not int or not 1 <= scan_budget <= MAX_SCAN:
            raise ValueError('invalid raw scan budget')
        if type(parent_limit) is not int or not 0 <= parent_limit <= MAX_PARENTS:
            raise ValueError('invalid parent budget')
        self.viewer, self.sealer = viewer, sealer
        self.limit, self.scan_budget, self.parent_limit = limit, scan_budget, parent_limit
        self._selection = None
        self._next_key = None
        self._bytes = 0

    def feed(self, inputs, **fold_inputs):
        inputs = _snapshot(inputs)
        previous = self._selection
        if previous is not None:
            if not previous.needs_more_input:
                raise ValueError('page selection is already complete')
            if inputs.position != previous.position:
                raise ValueError('page inputs changed between windows')
            if not inputs.rows or inputs.rows[0].key != self._next_key:
                raise ValueError('page windows are not contiguous')
        size = inputs.captured_bytes
        if type(size) is not int or size < 0 or self._bytes + size > 4 * 1024 * 1024:
            raise OverflowError('request input byte budget exceeded')
        prior_visible = len(previous.messages) if previous else 0
        prior_raw = previous.raw_examined if previous else 0
        prior_parent_ids = previous.parent_ids if previous else frozenset()
        current = select_message_page(
            inputs, self.viewer, self.sealer, limit=self.limit - prior_visible,
            scan_budget=self.scan_budget - prior_raw,
            parent_limit=self.parent_limit, _prior_parent_ids=prior_parent_ids, **fold_inputs,
        )
        if previous:
            current = replace(current, messages=current.messages + previous.messages,
                              raw_examined=prior_raw + current.raw_examined,
                              parents_examined=previous.parents_examined + current.parents_examined,
                              parents_required=len(prior_parent_ids | current.parent_ids),
                              parent_ids=prior_parent_ids | current.parent_ids)
        self._selection = current
        self._next_key = inputs.lookahead
        self._bytes += size
        return current.needs_more_input

    def finish(self):
        if self._selection is None or self._selection.needs_more_input:
            raise MorePageInputRequired('a visible page is not complete')
        return self._selection
