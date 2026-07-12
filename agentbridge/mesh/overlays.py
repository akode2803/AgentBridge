"""Overlay access: per-message chat overlays (edits / redactions / pins),
per-user reaction files, and the per-user state overlay.

Rules enforced here (FORMAT2):
- per-user state MERGES, never overwrites (the v1 star-wipe burn);
- chat overlays are one FILE PER MESSAGE, so concurrent actors on different
  messages can never clobber each other;
- all reads go through the transport's tolerant get_doc (half-synced -> None).
"""

from __future__ import annotations

from typing import Any

from ..core.timekit import next_ns, utcnow_iso
from ..transport.base import Transport
from .paths import P

__all__ = ["ChatOverlays", "UserState"]


def _doc_id(path: str) -> str:
    """'chats/c/overlays/edits/m-1-ab.json' -> 'm-1-ab'."""
    return path.rsplit("/", 1)[-1].removesuffix(".json")


class ChatOverlays:
    """Chat-level overlay reader/writer for ONE chat."""

    def __init__(self, tx: Transport, chat_id: str) -> None:
        self.tx = tx
        self.chat_id = chat_id

    # ------------------------------------------------------------------ edits
    def put_edit(self, msg_id: str, sealed: dict, by: str) -> None:
        self.tx.put_doc(
            P.edit(self.chat_id, msg_id),
            {**sealed, "by": by, "at": utcnow_iso(), "ns": next_ns()},
        )

    def edits(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for path in self.tx.list_docs(P.edits_prefix(self.chat_id)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict):
                out[_doc_id(path)] = doc
        return out

    # ------------------------------------------------------------- redactions
    def put_redaction(self, msg_id: str, by: str) -> None:
        self.tx.put_doc(
            P.redaction(self.chat_id, msg_id),
            {"by": by, "at": utcnow_iso(), "ns": next_ns()},
        )
        # a redacted message can't stay pinned (v1 rule)
        self.tx.delete_doc(P.pin(self.chat_id, msg_id))

    def redactions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for path in self.tx.list_docs(P.redactions_prefix(self.chat_id)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict):
                out[_doc_id(path)] = doc
        return out

    # ------------------------------------------------------------------- pins
    def put_pin(self, msg_id: str, by: str) -> None:
        self.tx.put_doc(
            P.pin(self.chat_id, msg_id),
            {"by": by, "at": utcnow_iso(), "ns": next_ns()},
        )

    def remove_pin(self, msg_id: str) -> None:
        self.tx.delete_doc(P.pin(self.chat_id, msg_id))

    def pins(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for path in self.tx.list_docs(P.pins_prefix(self.chat_id)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict):
                out[_doc_id(path)] = doc
        return out

    # -------------------------------------------------------------- reactions
    def set_reaction(self, user: str, msg_id: str, emoji: str | None) -> None:
        """One reaction per user per message (WhatsApp); None removes it.
        The per-USER file is single-writer, so this is a safe read-modify-write."""
        path = P.reactions(self.chat_id, user)
        current = self.tx.get_doc(path, default={})
        if not isinstance(current, dict):
            current = {}
        if emoji:
            current[msg_id] = emoji
        else:
            current.pop(msg_id, None)
        self.tx.put_doc(path, current)

    def reactions(self) -> dict[str, dict[str, list[str]]]:
        """{msg_id: {emoji: [users...]}} folded across every member's file."""
        out: dict[str, dict[str, list[str]]] = {}
        for path in self.tx.list_docs(P.reactions_prefix(self.chat_id)):
            user = _doc_id(path)
            doc = self.tx.get_doc(path)
            if not isinstance(doc, dict):
                continue
            for msg_id, emoji in doc.items():
                if isinstance(emoji, str) and emoji:
                    out.setdefault(msg_id, {}).setdefault(emoji, []).append(user)
        for per_msg in out.values():
            for users in per_msg.values():
                users.sort()
        return out


class UserState:
    """The per-user overlay of ONE chat for ONE user. Field semantics are v1's
    (read_ns/read_ts, starred, hidden, cleared, pinned, deleted, forced_unread,
    mute) — every mutation is read-MERGE-write on the user's own file."""

    def __init__(self, tx: Transport, chat_id: str, user: str) -> None:
        self.tx = tx
        self.chat_id = chat_id
        self.user = user
        self._path = P.state(chat_id, user)

    def get(self) -> dict[str, Any]:
        doc = self.tx.get_doc(self._path, default={})
        return doc if isinstance(doc, dict) else {}

    def _merge(self, **changes: Any) -> dict[str, Any]:
        state = self.get()
        state.update(changes)
        self.tx.put_doc(self._path, state)
        return state

    # ------------------------------------------------------------ read cursor
    def mark_read(self, up_to_ns: int) -> None:
        state = self.get()
        merged: dict[str, Any] = {
            "read_ns": max(int(state.get("read_ns", 0)), up_to_ns),
            "read_ts": utcnow_iso(),
        }
        if state.get("forced_unread"):
            merged["forced_unread"] = False
        self._merge(**merged)

    def read_ns(self) -> int:
        return int(self.get().get("read_ns", 0))

    # ------------------------------------------------------------------ stars
    def star(self, msg_ids: list[str]) -> None:
        state = self.get()
        starred = dict.fromkeys(list(state.get("starred", [])) + msg_ids)
        self._merge(starred=list(starred))

    def unstar(self, msg_ids: list[str]) -> None:
        drop = set(msg_ids)
        self._merge(starred=[m for m in self.get().get("starred", []) if m not in drop])

    def starred(self) -> list[str]:
        return list(self.get().get("starred", []))

    # ------------------------------------------------------- hide / clear
    def hide(self, msg_ids: list[str]) -> None:
        state = self.get()
        hidden = dict.fromkeys(list(state.get("hidden", [])) + msg_ids)
        self._merge(hidden=list(hidden))

    def unhide(self, msg_ids: list[str]) -> None:
        drop = set(msg_ids)
        self._merge(hidden=[m for m in self.get().get("hidden", []) if m not in drop])

    def clear(self, up_to_ns: int, *, keep_starred: bool = False) -> None:
        self._merge(cleared={"ns": up_to_ns, "keep_starred": keep_starred, "at": utcnow_iso()})

    # -------------------------------------------------- chat-list overlays
    def set_flag(self, name: str, value: Any) -> None:
        """pinned / deleted / forced_unread / mute — the sidebar overlays."""
        if name not in ("pinned", "deleted", "forced_unread", "mute"):
            raise ValueError(f"unknown state flag {name!r}")
        self._merge(**{name: value})
