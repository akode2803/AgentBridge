"""Overlay access: per-message chat overlays (edits / redactions / pins),
per-user reaction files, and the per-user state overlay.

Rules enforced here (FORMAT2):
- per-user state MERGES, never overwrites (the v1 star-wipe burn);
- chat overlays are one FILE PER MESSAGE, so concurrent actors on different
  messages can never clobber each other;
- all reads go through the transport's tolerant get_doc (half-synced -> None).
"""

from __future__ import annotations

import threading
from typing import Any

from ..core.timekit import next_ns, utcnow_iso
from ..transport.base import Transport
from .paths import P

__all__ = ["ChatOverlays", "UserState", "reaction_map", "fold_reactions"]


def _doc_id(path: str) -> str:
    """'chats/c/overlays/edits/m-1-ab.json' -> 'm-1-ab'."""
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def reaction_map(doc: Any) -> dict[str, str]:
    """The {msg_id: emoji} mapping out of a reaction doc — signed shape
    ({"v": mapping, "ns", "sig"}) or the legacy bare mapping."""
    if not isinstance(doc, dict):
        return {}
    mapping = doc.get("v") if "v" in doc else doc
    if not isinstance(mapping, dict):
        return {}
    return {m: e for m, e in mapping.items() if isinstance(e, str) and e}


def fold_reactions(per_user: dict[str, dict[str, str]]) -> dict[str, dict[str, list[str]]]:
    """{msg_id: {emoji: [users...]}} out of per-user {msg_id: emoji} maps."""
    out: dict[str, dict[str, list[str]]] = {}
    for user, mapping in per_user.items():
        for msg_id, emoji in mapping.items():
            out.setdefault(msg_id, {}).setdefault(emoji, []).append(user)
    for per_msg in out.values():
        for users in per_msg.values():
            users.sort()
    return out


class ChatOverlays:
    """Chat-level overlay reader/writer for ONE chat."""

    def __init__(self, tx: Transport, chat_id: str) -> None:
        self.tx = tx
        self.chat_id = chat_id

    # ------------------------------------------------------------------ edits
    def put_edit(self, msg_id: str, sealed: dict, by: str, ns: int | None = None) -> None:
        self.tx.put_doc(
            P.edit(self.chat_id, msg_id),
            {**sealed, "by": by, "at": utcnow_iso(), "ns": ns if ns is not None else next_ns()},
        )

    def edits(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for path in self.tx.list_docs(P.edits_prefix(self.chat_id)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict):
                out[_doc_id(path)] = doc
        return out

    # ------------------------------------------------------------- redactions
    def put_redaction(self, msg_id: str, by: str, sig: str = "", ns: int | None = None) -> None:
        self.tx.put_doc(
            P.redaction(self.chat_id, msg_id),
            {"by": by, "at": utcnow_iso(),
             "ns": ns if ns is not None else next_ns(), "sig": sig},
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
    def put_pin(
        self, msg_id: str, by: str, *,
        ns: int | None = None, until_ns: int = 0, sig: str = "",
    ) -> None:
        doc = {"by": by, "at": utcnow_iso(),
               "ns": ns if ns is not None else next_ns(), "sig": sig}
        if until_ns:
            doc["until_ns"] = int(until_ns)
        self.tx.put_doc(P.pin(self.chat_id, msg_id), doc)

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
    def set_reaction(
        self, user: str, msg_id: str, emoji: str | None, signer=None,
    ) -> None:
        """One reaction per user per message (WhatsApp); None removes it.
        The per-USER file is single-writer, so this is a safe read-modify-write.
        The whole mapping is signed on every write (R31) — ``signer`` is the
        identity's ``(bytes) -> sig`` callable; readers on an E2EE mesh ignore
        files whose signature doesn't verify."""
        from .events import reaction_signing_bytes

        path = P.reactions(self.chat_id, user)
        mapping = reaction_map(self.tx.get_doc(path, default={}))
        if emoji:
            mapping[msg_id] = emoji
        else:
            mapping.pop(msg_id, None)
        ns = next_ns()
        sig = signer(reaction_signing_bytes(self.chat_id, user, ns, mapping)) \
            if signer else ""
        self.tx.put_doc(path, {"v": mapping, "ns": ns, "at": utcnow_iso(),
                               "sig": sig})

    def reaction_docs(self) -> dict[str, dict]:
        """Raw per-user reaction docs — {user: doc}. Verification policy
        (signature + membership) lives in the messaging service, like the
        redaction verifier."""
        out: dict[str, dict] = {}
        for path in self.tx.list_docs(P.reactions_prefix(self.chat_id)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict):
                out[_doc_id(path)] = doc
        return out

    def reactions(self) -> dict[str, dict[str, list[str]]]:
        """{msg_id: {emoji: [users...]}} folded across every member's file —
        UNVERIFIED (the plaintext/dev-mesh path; E2EE reads go through the
        messaging service's verified fold)."""
        return fold_reactions(
            {u: reaction_map(d) for u, d in self.reaction_docs().items()}
        )


# in-process serialization for the per-user state files: read-modify-write
# on the SAME (chat, user) file must not interleave — R30 moved mark_read to
# a background thread, so a post's read-cursor write now runs concurrently
# with the user's own star/flag writes and a stale read would clobber them
_state_locks: dict[tuple[str, str], threading.RLock] = {}
_state_locks_guard = threading.Lock()


def _state_lock(chat_id: str, user: str) -> threading.RLock:
    with _state_locks_guard:
        return _state_locks.setdefault((chat_id, user), threading.RLock())


class UserState:
    """The per-user overlay of ONE chat for ONE user. Field semantics are v1's
    (read_ns/read_ts, starred, hidden, cleared, pinned, deleted, forced_unread,
    mute) — every mutation is read-MERGE-write on the user's own file, held
    under a per-(chat, user) lock so concurrent in-process writers merge
    instead of clobbering.

    R31.5 signs the doc like the reaction file: with a ``signer`` every write
    re-signs the full field set, and with a ``verifier`` every read treats an
    unsigned/mis-signed doc as ABSENT. That closes the forged-state surface
    (dropped-in ``hidden``/``cleared`` blanking the owner's own view, a fake
    ``read_ns`` faking read receipts, a fake ``mute`` silencing pings) — and
    because ``_merge`` starts from the VERIFIED read, a forged field is never
    laundered into the next genuine write."""

    def __init__(
        self,
        tx: Transport,
        chat_id: str,
        user: str,
        *,
        signer=None,     # (bytes) -> sig_b64, the owner's identity signer
        verifier=None,   # (doc) -> bool, None on plaintext/dev meshes
    ) -> None:
        self.tx = tx
        self.chat_id = chat_id
        self.user = user
        self._signer = signer
        self._verifier = verifier
        self._path = P.state(chat_id, user)
        self._lock = _state_lock(chat_id, user)

    @staticmethod
    def signed_fields(doc: dict) -> dict[str, Any]:
        """The field payload the signature covers — everything but the
        signature envelope itself."""
        return {k: v for k, v in doc.items() if k not in ("ns", "sig")}

    def get(self) -> dict[str, Any]:
        doc = self.tx.get_doc(self._path, default={})
        if not isinstance(doc, dict):
            return {}
        if self._verifier is not None and not self._verifier(doc):
            return {}  # fail-safe: a doc this user never signed doesn't exist
        return doc

    def _merge(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self.signed_fields(self.get())
            state.update(changes)
            if self._signer is not None:
                from .events import state_signing_bytes

                ns = next_ns()
                state["ns"] = ns
                state["sig"] = self._signer(
                    state_signing_bytes(self.chat_id, self.user, ns,
                                        self.signed_fields(state)))
            self.tx.put_doc(self._path, state)
            return state

    # ------------------------------------------------------------ read cursor
    def mark_read(self, up_to_ns: int) -> None:
        with self._lock:
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
        with self._lock:
            state = self.get()
            starred = dict.fromkeys(list(state.get("starred", [])) + msg_ids)
            self._merge(starred=list(starred))

    def unstar(self, msg_ids: list[str]) -> None:
        with self._lock:
            drop = set(msg_ids)
            self._merge(starred=[m for m in self.get().get("starred", [])
                                 if m not in drop])

    def starred(self) -> list[str]:
        return list(self.get().get("starred", []))

    # ------------------------------------------------------- hide / clear
    def hide(self, msg_ids: list[str]) -> None:
        with self._lock:
            state = self.get()
            hidden = dict.fromkeys(list(state.get("hidden", [])) + msg_ids)
            self._merge(hidden=list(hidden))

    def unhide(self, msg_ids: list[str]) -> None:
        with self._lock:
            drop = set(msg_ids)
            self._merge(hidden=[m for m in self.get().get("hidden", [])
                                if m not in drop])

    def clear(self, up_to_ns: int, *, keep_starred: bool = False) -> None:
        self._merge(cleared={"ns": up_to_ns, "keep_starred": keep_starred, "at": utcnow_iso()})

    # -------------------------------------------------- chat-list overlays
    def set_flag(self, name: str, value: Any) -> None:
        """pinned / archived / deleted / forced_unread / mute — the
        sidebar overlays (all per-user, all merged)."""
        if name not in ("pinned", "archived", "deleted", "forced_unread", "mute"):
            raise ValueError(f"unknown state flag {name!r}")
        self._merge(**{name: value})
