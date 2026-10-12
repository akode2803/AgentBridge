"""Overlay access: per-message chat overlays (edits / redactions / pins),
per-user reaction files, and the per-user state overlay.

Rules enforced here (FORMAT2):
- per-user state MERGES, never overwrites (the v1 star-wipe burn);
- chat overlays are one FILE PER MESSAGE, so concurrent actors on different
  messages can never clobber each other;
- all reads go through the transport's tolerant get_doc (half-synced -> None).
"""

from __future__ import annotations

import json
import threading
from typing import Any

from ..core.timekit import new_id, next_ns, utcnow_iso
from ..transport.base import Transport
from .paths import P
from .reaction_documents import (
    MAX_DOCUMENT_BYTES, MAX_LATEST_OPERATIONS, MAX_MAPPING_ENTRIES, MAX_NS,
    ReactionOperation, is_v2_candidate, operation_signature, parse_v2,
)

__all__ = ["ChatOverlays", "UserState", "reaction_map", "fold_reactions"]


def _doc_id(path: str) -> str:
    """'chats/c/overlays/edits/m-1-ab.json' -> 'm-1-ab'."""
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def reaction_map(doc: Any) -> dict[str, str]:
    """The {msg_id: emoji} mapping from v2, signed v1, or legacy bare docs."""
    if not isinstance(doc, dict):
        return {}
    if is_v2_candidate(doc):
        try:
            mapping, _operations = parse_v2(doc)
        except ValueError:
            return {}
        return mapping
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


def _bounded_reaction_state(
    mapping: dict, operations: dict[str, ReactionOperation], priority: str, ns: int,
) -> tuple[dict[str, str], dict[str, ReactionOperation]]:
    """Normalize state into the v2 count/byte budgets, preserving this action."""
    valid = {}
    for target, emoji in mapping.items():
        try:
            accepted = (type(target) is str and 0 < len(target.encode('utf-8')) <= 4096
                        and type(emoji) is str and 0 < len(emoji.encode('utf-8')) <= 128)
        except UnicodeEncodeError:
            accepted = False
        if accepted:
            valid[target] = emoji
    ordered = sorted(valid)
    minimum = 0
    if priority in valid:
        ordered.remove(priority)
        ordered.insert(0, priority)
        minimum = 1

    def candidate(count):
        kept_mapping = {target: valid[target] for target in ordered[:count]}
        kept_operations = {
            target: operation for target, operation in operations.items()
            if not operation.emoji or target in kept_mapping
        }
        # The current removal has no mapping entry but is still the namespace
        # head and must survive any migration cut.
        if priority in operations:
            kept_operations[priority] = operations[priority]
        document = {
            'schema': 2, 'v': kept_mapping, 'ops_origin': 'native',
            'ops': {target: value.to_dict()
                    for target, value in kept_operations.items()},
            'ns': ns, 'sig': '', 'ops_sig': '',
        }
        try:
            parse_v2(document)
        except ValueError:
            return None
        body = json.dumps(
            {key: document[key] for key in
             ('schema', 'v', 'ops', 'ops_origin', 'ns')},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        )
        if len(body.encode('utf-8')) > MAX_DOCUMENT_BYTES:
            return None
        return kept_mapping, kept_operations

    low, high, best = minimum, min(len(ordered), MAX_MAPPING_ENTRIES), None
    while low <= high:
        middle = (low + high) // 2
        value = candidate(middle)
        if value is None:
            high = middle - 1
        else:
            best = value
            low = middle + 1
    if best is None:
        raise OverflowError('reaction v2 document exceeds budget')
    return best


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

    def void_redaction(self, msg_id: str, by: str, sig: str = "",
                       ns: int | None = None) -> None:
        """Undo a delete-for-everyone (R44): the redaction doc KEEPS its
        original signed record and gains a signed ``void`` — presence-based
        and verifiable, unlike deleting the doc (absence can't be
        authenticated). A later re-delete overwrites the whole doc fresh."""
        doc = self.tx.get_doc(P.redaction(self.chat_id, msg_id))
        if not isinstance(doc, dict):
            return  # nothing to void — the caller validated; races are benign
        doc["void"] = {"by": by, "at": utcnow_iso(),
                       "ns": ns if ns is not None else next_ns(), "sig": sig}
        self.tx.put_doc(P.redaction(self.chat_id, msg_id), doc)

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
    ) -> ReactionOperation:
        """One reaction per user per message (WhatsApp); None removes it.
        The per-USER file is single-writer, so this is a safe read-modify-write.
        The v2 mapping and latest operation records are signed on every write.
        ``signer`` is the
        identity's ``(bytes) -> sig`` callable; readers on an E2EE mesh ignore
        files whose signature doesn't verify."""
        from .events import reaction_signing_bytes, reaction_v2_signing_bytes

        path = P.reactions(self.chat_id, user)
        old = self.tx.get_doc(path, default={})
        authenticated_old = signer is None
        if is_v2_candidate(old):
            try:
                mapping, operations = parse_v2(old)
                existing = operation_signature(old)
            except ValueError:
                mapping, operations, existing = {}, {}, ''
                authenticated_old = False
            if signer is not None:
                try:
                    data = reaction_v2_signing_bytes(self.chat_id, user, old)
                except ValueError:
                    expected = None
                else:
                    expected = signer(data)
                if expected and (not existing or existing != expected):
                    # A shared transport can replace this user's document.
                    # Never make a forged or unsigned v2 value a persistent
                    # write blocker, or launder its mapping/operation IDs.
                    mapping, operations = {}, {}
                    authenticated_old = False
                elif expected:
                    authenticated_old = True
                else:
                    # Keyless/plaintext operation preserves display mapping,
                    # but cannot authenticate timing or operation identities.
                    operations = {}
                    authenticated_old = False
            if not existing or old.get('ops_origin') == 'resealed':
                # A fresh local action must not promote unproven historical
                # operation IDs into a native document.
                operations = {}
        else:
            mapping, operations = reaction_map(old), {}
            if type(old) is not dict:
                authenticated_old = False
            if signer is not None:
                legacy = old if type(old) is dict else {}
                legacy_sig = legacy.get('sig')
                try:
                    legacy_data = reaction_signing_bytes(
                        self.chat_id, user, int(legacy.get('ns', 0)), mapping)
                except (TypeError, ValueError, OverflowError):
                    expected = None
                else:
                    expected = signer(legacy_data)
                if expected and (type(legacy_sig) is not str
                                 or not legacy_sig or legacy_sig != expected):
                    mapping = {}
                    authenticated_old = False
                elif expected:
                    authenticated_old = True
                else:
                    authenticated_old = False
        previous = operations.get(msg_id)
        ns = next_ns()
        if authenticated_old and type(old) is dict and type(old.get('ns')) is int:
            ns = max(ns, old['ns'] + 1)
        if ns > MAX_NS:
            raise OverflowError('reaction operation namespace exhausted')
        operation = ReactionOperation(new_id('rx', ns), ns, emoji or '',
                                      previous.id if previous is not None else None)
        if emoji:
            mapping[msg_id] = emoji
        else:
            mapping.pop(msg_id, None)
        operations[msg_id] = operation
        while len(operations) > MAX_LATEST_OPERATIONS:
            oldest = min(operations, key=lambda target: (operations[target].ns, target))
            del operations[oldest]
        mapping, operations = _bounded_reaction_state(
            mapping, operations, msg_id, ns,
        )
        doc = {'schema': 2, 'v': mapping, 'ops_origin': 'native', 'ops': {
            target: value.to_dict() for target, value in operations.items()
        }, 'ns': ns, 'sig': '', 'ops_sig': ''}
        if signer:
            doc['sig'] = signer(reaction_signing_bytes(
                self.chat_id, user, ns, mapping))
            doc['ops_sig'] = signer(reaction_v2_signing_bytes(
                self.chat_id, user, doc))
            # Do not publish half a compatibility pair if key availability
            # changes between the two bounded signing calls. Retry the whole
            # pair once: the v2 proof binds the compatibility signature, so a
            # proof minted across two key-availability states is not a coherent
            # document. A second transition leaves the prior overlay intact.
            if bool(doc['sig']) != bool(doc['ops_sig']):
                doc['sig'] = signer(reaction_signing_bytes(
                    self.chat_id, user, ns, mapping))
                doc['ops_sig'] = signer(reaction_v2_signing_bytes(
                    self.chat_id, user, doc))
                if bool(doc['sig']) != bool(doc['ops_sig']):
                    raise RuntimeError('reaction signing key changed during write')
        self.tx.put_doc(path, doc)
        return operation

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
            # Reopening an already-read room must not publish a new signed
            # state document: that write retires the local canonical source
            # (which also carries hidden/cleared state) until reingestion.
            # Clearing an explicit unread flag is still a real mutation.
            if int(state.get("read_ns", 0)) >= up_to_ns and not state.get("forced_unread"):
                return
            merged: dict[str, Any] = {
                "read_ns": max(int(state.get("read_ns", 0)), up_to_ns),
                "read_ts": utcnow_iso(),
            }
            if state.get("forced_unread"):
                merged["forced_unread"] = False
            self._merge(**merged)

    def read_ns(self) -> int:
        return int(self.get().get("read_ns", 0))

    def read_ts(self) -> str:
        return str(self.get().get("read_ts", ""))

    # -------------------------------------------------------- delivered cursor
    def mark_delivered(self, up_to_ns: int) -> bool:
        """Advance the delivered high-water — "my client/harness has fetched
        messages up to this ns" (R33). The Delivered receipt tier reads it, so
        it's a real per-recipient receipt (not just presence). Returns whether
        the cursor actually moved (callers skip the write otherwise)."""
        with self._lock:
            cur = int(self.get().get("delivered_ns", 0))
            if up_to_ns <= cur:
                return False
            self._merge(delivered_ns=up_to_ns, delivered_ts=utcnow_iso())
            return True

    def delivered_ns(self) -> int:
        return int(self.get().get("delivered_ns", 0))

    def delivered_ts(self) -> str:
        return str(self.get().get("delivered_ts", ""))

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

    def clear(self, up_to_ns: int, *, keep_starred: bool = False,
              runtime_ids: tuple[str, ...] = ()) -> None:
        with self._lock:
            state = self.get()
            hidden_runtime = list(dict.fromkeys([
                *state.get("hidden_runtime", []), *runtime_ids,
            ]))
            changes: dict[str, Any] = {"hidden_runtime": hidden_runtime}
            if up_to_ns:
                changes["cleared"] = {
                    "ns": up_to_ns, "keep_starred": keep_starred,
                    "at": utcnow_iso(),
                }
            self._merge(**changes)

    # -------------------------------------------------- chat-list overlays
    def set_flag(self, name: str, value: Any) -> None:
        """pinned / archived / deleted / forced_unread / mute — the
        sidebar overlays (all per-user, all merged)."""
        if name not in ("pinned", "archived", "deleted", "forced_unread", "mute"):
            raise ValueError(f"unknown state flag {name!r}")
        self._merge(**{name: value})
