"""Local transport acceptance, independent of recipient receipts and telemetry.

Rows contain no plaintext. Failed status survives bounded outbox cleanup; absence
means legacy or transport-observed history, never a newly queued local send.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable


def initialize(conn: sqlite3.Connection) -> None:
    with conn:
        # Serialize backfill with worker completion, including other processes.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS local_send_status(
            chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
            outbox_seq INTEGER NOT NULL, state TEXT NOT NULL,
            accepted_ns INTEGER NOT NULL DEFAULT 0,
            client_ref TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(chat_id,message_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_outbox "
                     "ON local_send_status(outbox_seq)")
        # Existing pending/dead sends must not become ticks on upgrade. Repeat
        # safely for old clients that may have queued work since the last open.
        for seq, target, payload, state in conn.execute(
                "SELECT seq,target,payload,state FROM outbox WHERE kind='append_log'"):
            try:
                record = json.loads(payload)
                record = record.get("envelope", record)
                if not isinstance(record, dict) or record.get("kind") != "message":
                    continue
                mid = record.get("id")
                if not isinstance(mid, str) or not mid:
                    continue
            except (ValueError, AttributeError, TypeError):
                continue
            conn.execute("INSERT OR IGNORE INTO local_send_status "
                         "(chat_id,message_id,outbox_seq,state) VALUES(?,?,?,?)",
                         (target.partition('|')[0], mid, seq,
                          "failed" if state == "dead" else "queued"))


def capture(conn: sqlite3.Connection, chat_id: str, ids: Iterable[str]) -> dict:
    result = {}
    unique = list(dict.fromkeys(ids))
    # Bound SQL variable count; callers supply already-canonical own IDs.
    for offset in range(0, len(unique), 400):
        batch = unique[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        for mid, state, accepted, ref in conn.execute(
                "SELECT message_id,state,accepted_ns,client_ref "
                f"FROM local_send_status WHERE chat_id=? AND message_id IN ({marks})",
                [chat_id, *batch]):
            result[mid] = dict(state=state, accepted_ns=accepted, client_ref=ref)
    return result
