"""Bounded round-robin hints from locally ingested message-bearing chats.

The message cache is neither a room registry nor membership authority. Empty
rooms and rooms with no ingested messages need explicit foreground hints.
"""
from __future__ import annotations


class SourceDiscovery:
    def __init__(self, store):
        self.store = store
        self.cursor = ""

    def next_batch(self, *, max_rooms=32) -> list[str]:
        """Seek up to 32 distinct chat IDs; wrap once without scanning messages.

        Each seek uses the existing messages(chat_id, id) primary-key index.
        The exclusive keyset advances past all messages in a chat in one seek,
        regardless of the number of messages that chat contains.
        """
        if type(max_rooms) is not int or not 1 <= max_rooms <= 32:
            raise ValueError('invalid discovery batch size')
        conn = self.store._conn()
        batch = []
        cursor = self.cursor
        while len(batch) < max_rooms:
            row = conn.execute(
                'SELECT chat_id FROM messages WHERE chat_id>? '
                'ORDER BY chat_id LIMIT 1', (cursor,)).fetchone()
            if row is None:
                if cursor == '':
                    break
                cursor = ''
                continue
            chat = row[0]
            if batch and chat == batch[0]:
                break
            batch.append(chat)
            cursor = chat
        self.cursor = batch[-1] if batch else cursor
        return batch
