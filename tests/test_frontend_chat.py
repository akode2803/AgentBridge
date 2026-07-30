"""Focused source-contract checks for chat renderer identity decisions."""

from pathlib import Path


CHAT_JS = (Path(__file__).resolve().parents[1] / "gui" / "static" / "js" /
           "chat.js")


def test_group_agent_badge_uses_sender_account_kind():
    chat = CHAT_JS.read_text(encoding="utf-8")

    assert 'ms.users?.[msg.from]?.kind === "agent"' in chat
    assert 'msg.kind === "agent"' not in chat
