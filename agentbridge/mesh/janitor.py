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
  by the fold itself) with the deletion older than the grace window is
  purged via ``tx.delete_chat``. Info events are plaintext by design, so
  this leg works even for chats this identity was folded out of.

Scope: naming a redacted message's file ids requires unsealing its body,
so each member's janitor sweeps only chats it can read — every chat has
members, so every chat has a janitor. Every delete is idempotent; two
janitors racing is fine.
"""

from __future__ import annotations

import time

from ..core.errors import TransportError, ValidationError
from ..core.models import Envelope
from . import events
from .attachments import attachment_path
from .overlays import ChatOverlays
from .readmodel import member_at

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
    def _purge_deleted_chat(self, chat_id: str, horizon: int) -> bool:
        """Purge a chat whose (plaintext, signed) event fold says deleted,
        with the deletion older than the grace window."""
        records: list[dict] = []
        info_records: list[dict] = []
        deleted_ns = 0
        for log_name, _size in self.mesh.tx.list_logs(chat_id):
            recs, _ = self.mesh.tx.read_log(chat_id, log_name, 0)
            for r in recs:
                records.append(r)
                if r.get("kind") != "info":
                    continue
                info_records.append(r)
                if (r.get("event") or {}).get("type") == events.EV_DELETED:
                    deleted_ns = max(deleted_ns, int(r.get("ns", 0)))
        if not deleted_ns or deleted_ns > horizon:
            return False
        # the FOLD is the judge — it verifies signatures and admin authority,
        # so a forged chat_deleted from a non-admin purges nothing
        snap = events.fold(chat_id, info_records, self.mesh.directory)
        if not snap.deleted:
            return False
        for path in self._owned_attachment_paths(chat_id, records, snap.tenure):
            self.mesh.tx.delete_blob(path)
        self.mesh.tx.delete_chat(chat_id)
        return True

    def reclaim_deleted_chat_attachments(self, chat_id: str) -> int:
        """Delete exact signed references before terminal ACL materialization."""
        records: list[dict] = []
        info_records: list[dict] = []
        for log_name, _size in self.mesh.tx.list_logs(chat_id):
            recs, _ = self.mesh.tx.read_log(chat_id, log_name, 0)
            records.extend(recs)
            info_records.extend(r for r in recs if r.get("kind") == "info")
        snap = events.fold(chat_id, info_records, self.mesh.directory)
        if not snap.deleted:
            raise TransportError("terminal deletion event is not yet readable")
        # A provider read immediately after append can briefly omit an older
        # row even though this client already durably ingested it. Merge that
        # signed local evidence before deriving exact paths; IDs deduplicate
        # the normal case where the remote scan is already complete.
        seen = {r.get("id") for r in records if r.get("id")}
        records.extend(
            r for r in self.mesh.store.messages(chat_id)
            if r.get("id") not in seen
        )
        paths = self._owned_attachment_paths(chat_id, records, snap.tenure)
        for path in paths:
            self.mesh.tx.delete_blob(path)
        return len(paths)

    def _owned_attachment_paths(
        self, chat_id: str, records: list[dict], tenure: dict[str, list[list[int]]]
    ) -> list[str]:
        """Exact blob paths proven by signed, decryptable message bodies."""
        paths: set[str] = set()
        for rec in records:
            if rec.get("kind", "message") != "message":
                continue
            env = Envelope.from_dict(rec)
            if (not isinstance(env.id, str) or not env.id
                    or not isinstance(env.from_, str) or not env.from_
                    or not isinstance(env.ns, int) or isinstance(env.ns, bool)
                    or not member_at(
                        tenure.get(env.from_), env.ns)):
                continue
            body = self.mesh.sealer.unseal(chat_id, env)
            for item in (body.files if body else None) or []:
                try:
                    paths.add(attachment_path(chat_id, item.get("id")))
                except (AttributeError, ValidationError):
                    continue  # malformed signed body: skip, never guess
        return sorted(paths)

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
                    path = attachment_path(chat_id, blob_id)
                except ValidationError:  # malformed signed body: skip, never guess
                    continue
                got = self.mesh.tx.blob_size(path)
                if got is None:
                    continue                   # already reclaimed
                self.mesh.tx.delete_blob(path)
                if self.mesh.tx.blob_size(path) is None:
                    n += 1
                    size += int(got or 0)
        return n, size
