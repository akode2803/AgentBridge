"""The storage janitor (V63 / R65) — reclaims server space that tombstones
alone never free.

Delete-for-everyone writes a signed redaction: readers can never see the
file again and the server refuses to serve it — but the sealed blob stayed
on the transport forever (real Supabase free-tier pressure, and the bytes
are unreadable to everyone anyway). Likewise a group's ``chat_deleted`` is
terminal in the fold, yet its subtree stayed. The janitor closes both,
conservatively:

- **Blobs** — only for messages whose redaction VERIFIES (the exact
  verifier the read fold uses, so a forged doc reclaims nothing and a
  validly VOIDED redaction — R44's Undo delete — is skipped) AND is older
  than the grace window. Within the grace, Undo restores everything; after
  it, Undo still restores the text (the sealed body lives in the
  append-only log) while the attachment is gone for good — documented
  behaviour, the price of actually freeing space.
- **Chats** — a group whose event fold says deleted (signed + admin-gated
  by the fold itself) with the deletion older than the grace window loses
  every Storage object it owns (its own folders, listed exactly and
  completely, so files nobody can decrypt and the group photo go too), and
  only once a fresh listing is empty is it purged via ``tx.delete_chat``.
  Under a bound writer the provider meta must also be terminal, and each
  sweep first settles any purge whose reply was lost. Its final members
  (older deletions: everyone in its tenure) are the ones who can still read
  it, so their janitors do this (D-018).

Scope: naming a redacted message's file ids requires unsealing its body,
so each member's janitor sweeps only chats it can read — every chat has
members, so every chat has a janitor. A live chat's staged attachment can be
removed only by its uploader, so each janitor reclaims its own. Every delete
is idempotent; two janitors racing is fine.
"""

from __future__ import annotations

import time

from ..core.errors import TransportError, ValidationError
from ..core.models import Envelope
from . import events
from .attachments import attachment_path
from .overlays import ChatOverlays
from .paths import P

__all__ = ["Janitor", "GRACE_DAYS"]

GRACE_DAYS = 7.0
LAST_DOC = "janitor/last"     # local store — when this machine last swept


class Janitor:
    def __init__(self, mesh) -> None:
        self.mesh = mesh

    def sweep(self, *, grace_days: float = GRACE_DAYS) -> dict:
        """One full pass. Returns {"chats": n, "blobs": n, "bytes": n}."""
        horizon = time.time_ns() - int(grace_days * 86_400 * 1e9)
        out = {"chats": 0, "blobs": 0, "bytes": 0}
        if self._typed():
            try:  # a purge whose reply was lost is acknowledged here, or never
                self.mesh.tx.settle_chat_purges()
            except Exception:  # noqa: BLE001 — the next sweep retries
                pass
        for chat_id in list(self.mesh.tx.list_chat_ids()):
            try:
                if self._purge_deleted_chat(chat_id, horizon):
                    out["chats"] += 1
                    continue
                n, size = self._purge_redacted_blobs(chat_id, horizon)
                out["blobs"] += n
                out["bytes"] += size
            except Exception:  # noqa: BLE001 — one chat never blocks the sweep
                continue
        # R76: hard-drop doc tombstones old enough that every mirror has
        # long seen them ride the delta feed (stragglers heal via reconcile)
        purge = getattr(self.mesh.tx, "purge_deleted_docs", None)
        if callable(purge):
            try:
                purge(30.0)
            except Exception:  # noqa: BLE001 — next sweep retries
                pass
        try:
            self.mesh.store.cache_doc(LAST_DOC, {
                "at_ns": time.time_ns(), **out})
        except Exception:  # noqa: BLE001 — the receipt is cosmetic
            pass
        return out

    # ------------------------------------------------------------- chat leg
    def _deletion(self, chat_id: str) -> int:
        """The ns of a signed, fold-verified deletion of this chat, else 0.
        The FOLD is the judge — it verifies signatures and admin authority,
        so a forged chat_deleted from a non-admin reclaims nothing. (Under a
        bound writer a delete valid in swap order that the ns order rejects
        is left alone: fail closed.)"""
        info_records: list[dict] = []
        deleted_ns = 0
        for log_name, _size in self.mesh.tx.list_logs(chat_id):
            recs, _ = self.mesh.tx.read_log(chat_id, log_name, 0)
            for r in recs:
                if r.get("kind") != "info":
                    continue
                info_records.append(r)
                if (r.get("event") or {}).get("type") == events.EV_DELETED:
                    deleted_ns = max(deleted_ns, int(r.get("ns", 0)))
        if not deleted_ns:
            return 0
        snap = events.fold(chat_id, info_records, self.mesh.directory)
        return deleted_ns if snap.deleted else 0

    def _chat_objects(self, chat_id: str) -> list[str]:
        """Every Storage object this chat owns: its group photo, its files
        and its staged attachments, every page. The three folders are listed
        by name, so nothing depends on Storage reporting sub-folders (it
        still recurses into any it does report)."""
        if not chat_id or chat_id in {".", ".."} or "/" in chat_id or "\x00" in chat_id:
            raise ValidationError("malformed chat id")
        found: set[str] = set()
        for folder in (f"chats/{chat_id}", f"chats/{chat_id}/files",
                       f"blob-stage/{chat_id}"):
            found.update(self.mesh.tx.list_blobs(folder))
        return sorted(found)

    def _purge_deleted_chat(self, chat_id: str, horizon: int) -> bool:
        """Purge a chat whose (plaintext, signed) event fold says deleted,
        with the deletion older than the grace window: its Storage objects
        first, then (once a fresh listing is empty) its records. The purge
        ends every read, so nothing it missed could be reclaimed later; a
        transport that cannot list never purges."""
        deleted_ns = self._deletion(chat_id)
        if not deleted_ns or deleted_ns > horizon:
            return False
        if self._typed() and not self._provider_terminal(chat_id):
            return False  # the provider would refuse; its mirror may lag
        self.mesh.tx.delete_blobs(self._chat_objects(chat_id))
        if self._chat_objects(chat_id):
            return False  # new or undeletable objects: the next sweep retries
        self.mesh.tx.delete_chat(chat_id)
        return True

    def reclaim_deleted_chat_attachments(self, chat_id: str) -> int:
        """Delete a just-deleted chat's Storage objects (no grace: deleted
        files are unreadable to everyone but its final members anyway).
        Best effort: the janitor lists again before it purges."""
        if not self._deletion(chat_id):
            raise TransportError("terminal deletion event is not yet readable")
        try:
            paths = self._chat_objects(chat_id)
        except NotImplementedError:
            return 0  # nothing to reclaim on a transport without listing
        self.mesh.tx.delete_blobs(paths)
        return len(paths)

    def _typed(self) -> bool:
        from ..transport.local_mutations import LocalMutationTransport

        tx = self.mesh.tx
        return isinstance(tx, LocalMutationTransport) and tx._bound_writer() is not None

    def _provider_terminal(self, chat_id: str) -> bool:
        meta = self.mesh.tx.get_doc(P.meta(chat_id))
        return (isinstance(meta, dict) and meta.get("deleted") is True
                and meta.get("members") == {})

    # ------------------------------------------------------------- blob leg
    def _purge_redacted_blobs(self, chat_id: str, horizon: int) -> tuple[int, int]:
        """Reclaim the attachments of verified-redacted messages, past the
        undo grace. Needs membership (the file ids live in the sealed body)."""
        try:
            snap = self.mesh.messaging.snapshot(chat_id)
        except Exception:  # noqa: BLE001 — unreadable meta = not mine to sweep
            return 0, 0
        if not snap.is_member(self.mesh.user):
            return 0, 0
        reds = ChatOverlays(self.mesh.tx, chat_id).redactions()
        if not reds:
            return 0, 0
        verify = self.mesh.messaging._redaction_verifier(chat_id)
        by_id = {r.get("id"): r for r in self.mesh.store.messages(chat_id)}
        n = size = 0
        for msg_id, red in reds.items():
            if int(red.get("ns", 0)) > horizon:
                continue                       # still inside the undo grace
            rec = by_id.get(msg_id)
            if rec is None:
                continue                       # not in my cache — skip, not guess
            env = Envelope.from_dict(rec)
            if verify is not None:
                if not verify(msg_id, red, env.from_):
                    continue                   # forged, or validly voided (undo)
            elif isinstance(red.get("void"), dict):
                continue  # plaintext/dev mesh: honor undo presence-based too
            body = self.mesh.sealer.unseal(chat_id, env)
            for f in (body.files if body else None) or []:
                blob_id = f.get("id")
                if not blob_id:
                    continue
                try:
                    path = attachment_path(chat_id, blob_id, f)
                except ValidationError:  # malformed signed body: skip, never guess
                    continue
                if path.startswith("blob-stage/") and env.from_ != self.mesh.user:
                    continue  # only its uploader may remove a live staged object
                got = self.mesh.tx.blob_size(path)
                if got is None:
                    continue                   # already reclaimed
                self.mesh.tx.delete_blob(path)
                if self.mesh.tx.blob_size(path) is None:
                    n += 1
                    size += int(got or 0)
        return n, size
