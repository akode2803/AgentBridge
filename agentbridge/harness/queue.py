"""The durable work queue + the answered-guard.

Everything here persists in the agent's own store (SQLite, crash-safe), so a
harness restart resumes exactly where it stopped: pending items survive,
claimed-but-unfinished items return to pending when their lease expires, and
the ANSWERED LEDGER guarantees a trigger is answered at most once.

The answered-guard is two-legged (this is what kills v1's duplicate-reply bug
for good):
1. the local ledger, keyed ``msg_id@edit_ns`` — covers replies AND deliberate
   silences (a silent run leaves no trace in the chat);
2. the transcript itself — any of MY messages whose ``reply_to`` names the
   trigger proves it was answered, even if the local ledger was lost.

Dispatch groups pending items per (chat, sender): one run answers a sender's
burst of messages in one reply (threaded to their last message), while
different senders' requests — in the same chat or across chats — run in
parallel up to the owner-set concurrency.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from ..core.models import Message
from ..core.latency import clock_id, sink_for_store
from ..store.db import Store

__all__ = ["WorkItem", "WorkGroup", "WorkQueue"]

PENDING_DOC = "harness/pending"
ANSWERED_DOC = "harness/answered/{chat}"
RATE_DOC = "harness/rate"
ANSWERED_KEEP = 500          # per chat; the transcript leg covers older ones
LEASE_S = 3900.0             # a crashed run frees its claim after this


@dataclass
class WorkItem:
    key: str            # "<chat>|<msg_id>@<edit_ns>" or "<chat>|timer:<id>"
    chat_id: str
    kind: str           # "message" | "timer"
    msg_id: str = ""
    edit_ns: int = 0
    sender: str = ""    # timer items: the agent itself
    ns: int = 0         # trigger ordinal (ordering + reply threading)
    reason: str = ""
    note: str = ""      # timer note
    status: str = "pending"   # pending | running
    next_ns: int = 0    # earliest dispatch time (rate-cap backoff)
    lease_ns: int = 0
    observed_ns: int = 0
    observed_mono: int = 0
    observed_clock: str = ""
    enqueued_ns: int = 0
    enqueued_mono: int = 0
    enqueued_clock: str = ""
    claimed_ns: int = 0
    claimed_mono: int = 0
    claimed_clock: str = ""
    provider_started: bool = False
    attempts: int = 0   # FAILURE releases only (retry_or_fail); never legit defers

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "WorkItem":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


@dataclass
class WorkGroup:
    """What one run answers: a sender's pending burst in one chat."""

    chat_id: str
    sender: str
    items: list[WorkItem] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.items[0].kind if self.items else "message"

    @property
    def last(self) -> WorkItem:
        return max(self.items, key=lambda i: i.ns)


class WorkQueue:
    def __init__(self, store: Store, agent: str) -> None:
        self.store = store
        self.agent = agent
        self._lock = threading.RLock()
        self.latency = sink_for_store(store)

    # ------------------------------------------------------------- pending
    def _pending(self) -> dict[str, dict]:
        return self.store.cached_doc(PENDING_DOC, default={}) or {}

    def _save_pending(self, items: dict[str, dict]) -> None:
        self.store.cache_doc(PENDING_DOC, items)

    def offer(self, item: WorkItem) -> bool:
        """Enqueue unless already pending/running or already answered."""
        with self._lock:
            if self.answered(item.chat_id, item.msg_id, item.edit_ns) and \
                    item.kind == "message":
                return False
            items = self._pending()
            if item.key in items:
                return False
            now = time.time_ns()
            if not item.observed_ns:
                item.observed_ns = now
                item.observed_clock = clock_id()
            item.enqueued_ns = now
            item.enqueued_mono = time.perf_counter_ns()
            item.enqueued_clock = clock_id()
            items[item.key] = item.to_dict()
            self._save_pending(items)
            self.latency.observe(
                "queue_enqueued", item.msg_id or item.key, lane="local")
            return True

    def claim_groups(
        self, *, limit: int, exclude: set[tuple[str, str]] = frozenset()
    ) -> list[WorkGroup]:
        """Claim up to ``limit`` due (chat, sender) groups under a lease.
        ``exclude`` holds the groups currently in flight — a sender's next
        burst waits for their current run (its reply may already cover it)."""
        now = time.time_ns()
        lease = now + int(LEASE_S * 1e9)
        with self._lock:
            items = self._pending()
            groups: dict[tuple[str, str], WorkGroup] = {}
            transitioned_unknown = False
            for d in sorted(items.values(), key=lambda d: d.get("ns", 0)):
                it = WorkItem.from_dict(d)
                if it.status == "unknown":
                    continue  # provider effects may have happened; manual only
                if it.status == "running" and it.provider_started:
                    # Expiry is not proof that provider/tool effects did not
                    # happen. Settle ambiguity durably; never auto-dispatch.
                    if it.lease_ns <= now:
                        it.status, it.lease_ns = "unknown", 0
                        items[it.key] = it.to_dict()
                        transitioned_unknown = True
                    continue
                if it.status == "running" and it.lease_ns > now:
                    continue  # genuinely in flight before provider start
                if it.next_ns > now:
                    continue  # backing off (rate cap)
                gkey = (it.chat_id, it.sender if it.kind == "message" else it.key)
                if gkey in exclude:
                    continue
                if gkey not in groups and len(groups) >= limit:
                    continue
                groups.setdefault(gkey, WorkGroup(it.chat_id, it.sender)).items.append(it)
            for g in groups.values():
                for it in g.items:
                    it.status, it.lease_ns = "running", lease
                    it.claimed_ns = now
                    it.claimed_mono = time.perf_counter_ns()
                    it.claimed_clock = clock_id()
                    items[it.key] = it.to_dict()
                    self.latency.observe(
                        "queue_claimed", it.msg_id or it.key, lane="local")
            if groups or transitioned_unknown:
                self._save_pending(items)
            return list(groups.values())

    def release(self, group: WorkGroup, *, retry_in_s: float | None = None) -> None:
        """Return a claimed group to pending (pause/rate-cap/still-not-due)."""
        with self._lock:
            items = self._pending()
            for it in group.items:
                d = items.get(it.key)
                if d is None:
                    continue
                d["status"] = "pending"
                d["lease_ns"] = 0
                if retry_in_s is not None:
                    d["next_ns"] = time.time_ns() + int(retry_in_s * 1e9)
            self._save_pending(items)

    def recover_claims(self) -> int:
        """Return this runner's crash-left claims immediately to pending."""
        with self._lock:
            items = self._pending()
            recovered = 0
            changed = False
            for data in items.values():
                if data.get("status") != "running":
                    continue
                if data.get("provider_started"):
                    data["status"] = "unknown"
                    data["lease_ns"] = 0
                    changed = True
                    continue
                data["status"] = "pending"
                data["lease_ns"] = 0
                data["claimed_ns"] = 0
                recovered += 1
                changed = True
            if changed:
                self._save_pending(items)
            return recovered

    def mark_provider_started(self, group: WorkGroup) -> None:
        """Persist the no-auto-retry boundary before provider tool effects."""
        with self._lock:
            items = self._pending()
            changed = False
            for item in group.items:
                data = items.get(item.key)
                if data is not None and not data.get("provider_started"):
                    data["provider_started"] = True
                    changed = True
            if changed:
                self._save_pending(items)

    def retry_or_fail(self, group: WorkGroup, *, max_attempts: int = 3,
                      retry_in_s: float = 20.0) -> bool:
        """A FAILURE release (R55): bump each item's attempt count and put the
        group back for one more try — until any item has failed
        ``max_attempts`` times, when the whole group resolves as an error
        (ledger written, so it never re-fires). Legit deferrals (stand-down,
        rate cap, sync barrier) use plain ``release`` and never count.
        Returns True when released for retry, False when given up."""
        with self._lock:
            items = self._pending()
            ambiguous = any(
                bool((items.get(item.key) or {}).get("provider_started"))
                for item in group.items)
            if ambiguous:
                for item in group.items:
                    data = items.get(item.key)
                    if data is not None:
                        data["status"] = "unknown"
                        data["lease_ns"] = 0
                self._save_pending(items)
                return False
            give_up = False
            for it in group.items:
                d = items.get(it.key)
                if d is None:
                    continue
                d["attempts"] = int(d.get("attempts", 0)) + 1
                if d["attempts"] >= max_attempts:
                    give_up = True
            self._save_pending(items)
            if give_up:
                self.finish(group, "error:gave-up")
                return False
            self.release(group, retry_in_s=retry_in_s)
            return True

    def group_unknown(self, group: WorkGroup) -> bool:
        items = self._pending()
        return any((items.get(item.key) or {}).get("status") == "unknown"
                   for item in group.items)

    def finish(self, group: WorkGroup, result: str) -> None:
        """Resolve a claimed group: drop from pending, write the ledger."""
        with self._lock:
            items = self._pending()
            for it in group.items:
                items.pop(it.key, None)
                if it.kind == "message":
                    self._record(it.chat_id, it.msg_id, it.edit_ns, result)
                else:
                    self._record(it.chat_id, it.key, 0, result)
            self._save_pending(items)

    def clear_pending(self) -> int:
        """Drop every pending trigger — the peer-repair escape hatch for a
        harness stuck on a poisoned item (R22.5). The answered ledger and the
        scan cursors are untouched, so nothing already handled re-fires and a
        genuinely-new trigger is still picked up on the next scan."""
        with self._lock:
            n = len(self._pending())
            self._save_pending({})
            return n

    def snapshot(self) -> list[dict]:
        """Metadata-only view for the owner-visible status doc (no bodies)."""
        items = sorted(self._pending().values(), key=lambda d: d.get("ns", 0))
        return [
            {"chat_id": d.get("chat_id"), "kind": d.get("kind"),
             "from": d.get("sender"), "ns": d.get("ns"),
             "reason": d.get("reason"), "status": d.get("status")}
            for d in items
        ]

    # ------------------------------------------------------ answered ledger
    def _ledger(self, chat_id: str) -> dict[str, str]:
        return self.store.cached_doc(ANSWERED_DOC.format(chat=chat_id), default={}) or {}

    def _record(self, chat_id: str, msg_id: str, edit_ns: int, result: str) -> None:
        led = self._ledger(chat_id)
        led[f"{msg_id}@{edit_ns}"] = result
        while len(led) > ANSWERED_KEEP:  # dicts keep insertion order
            led.pop(next(iter(led)))
        self.store.cache_doc(ANSWERED_DOC.format(chat=chat_id), led)

    def record_skip(self, chat_id: str, msg_id: str, edit_ns: int, why: str) -> None:
        """Resolve a candidate WITHOUT running (catch-up policy etc.) so it
        can never fire later."""
        with self._lock:
            self._record(chat_id, msg_id, edit_ns, f"skipped:{why}")

    def answered(self, chat_id: str, msg_id: str, edit_ns: int = 0) -> bool:
        return f"{msg_id}@{edit_ns}" in self._ledger(chat_id)

    def answered_in_transcript(
        self, msgs: Iterable[Message], msg_id: str
    ) -> bool:
        """The second guard leg: one of MY visible messages already replies to
        this trigger — answered even if the local ledger was lost."""
        return any(
            m.from_ == self.agent and (m.reply_to or {}).get("id") == msg_id
            for m in msgs
        )

    # ------------------------------------------------------------ rate cap
    def rate_acquire(self, chat_id: str, cap: int) -> bool:
        """Atomically claim a reply slot in this chat's rolling hour —
        check-then-record as one step, so parallel runs can't both pass a
        cap of one. Refund the slot if the run ends without posting."""
        with self._lock:
            doc = self.store.cached_doc(RATE_DOC, default={}) or {}
            now = time.time()
            recent = [t for t in doc.get(chat_id, []) if now - t < 3600]
            if len(recent) >= cap:
                doc[chat_id] = recent
                self.store.cache_doc(RATE_DOC, doc)
                return False
            doc[chat_id] = recent + [now]
            self.store.cache_doc(RATE_DOC, doc)
            return True

    def rate_refund(self, chat_id: str) -> None:
        with self._lock:
            doc = self.store.cached_doc(RATE_DOC, default={}) or {}
            if doc.get(chat_id):
                doc[chat_id].pop()
                self.store.cache_doc(RATE_DOC, doc)

    # ------------------------------------------------------------- cursors
    def scan_cursor(self, chat_id: str) -> tuple[int, int]:
        return (
            self.store.get_cursor("hscan", chat_id),
            self.store.get_cursor("hedit", chat_id),
        )

    def set_scan_cursor(self, chat_id: str, ns: int, edit_ns: int) -> None:
        self.store.set_cursor("hscan", chat_id, ns)
        self.store.set_cursor("hedit", chat_id, edit_ns)
