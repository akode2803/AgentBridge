"""Logical path conventions (docs/FORMAT2.md) — one place, so the services
and every transport driver agree on where records live."""

from __future__ import annotations

__all__ = ["P"]


class P:
    MANIFEST = "manifest.json"

    @staticmethod
    def user(name: str) -> str:
        return f"users/{name}.json"

    @staticmethod
    def presence(user: str, machine: str) -> str:
        return f"presence/{user}@{machine}.json"

    @staticmethod
    def avatar(user: str) -> str:
        return f"avatars/{user}.jpg"

    # ------------------------------------------------------------------ chats
    @staticmethod
    def meta(chat_id: str) -> str:
        return f"chats/{chat_id}/meta.json"

    @staticmethod
    def log_name(sender: str, machine: str) -> str:
        """Per-device message log (FORMAT2: single-writer means single DEVICE)."""
        return f"{sender}@{machine}"

    @staticmethod
    def edit(chat_id: str, msg_id: str) -> str:
        return f"chats/{chat_id}/overlays/edits/{msg_id}.json"

    @staticmethod
    def edits_prefix(chat_id: str) -> str:
        return f"chats/{chat_id}/overlays/edits"

    @staticmethod
    def redaction(chat_id: str, msg_id: str) -> str:
        return f"chats/{chat_id}/overlays/redactions/{msg_id}.json"

    @staticmethod
    def redactions_prefix(chat_id: str) -> str:
        return f"chats/{chat_id}/overlays/redactions"

    @staticmethod
    def pin(chat_id: str, msg_id: str) -> str:
        return f"chats/{chat_id}/overlays/pins/{msg_id}.json"

    @staticmethod
    def pins_prefix(chat_id: str) -> str:
        return f"chats/{chat_id}/overlays/pins"

    @staticmethod
    def reactions(chat_id: str, user: str) -> str:
        return f"chats/{chat_id}/overlays/reactions/{user}.json"

    @staticmethod
    def reactions_prefix(chat_id: str) -> str:
        return f"chats/{chat_id}/overlays/reactions"

    @staticmethod
    def state(chat_id: str, user: str) -> str:
        return f"chats/{chat_id}/overlays/state/{user}.json"

    @staticmethod
    def file(chat_id: str, file_id: str) -> str:
        return f"chats/{chat_id}/files/{file_id}"

    @staticmethod
    def keys(chat_id: str, epoch: int) -> str:
        return f"chats/{chat_id}/keys/{epoch}.json"
