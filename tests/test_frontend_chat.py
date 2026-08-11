"""Focused source-contract checks for chat renderer identity decisions."""

from pathlib import Path


CHAT_JS = (Path(__file__).resolve().parents[1] / "gui" / "static" / "js" /
           "chat.js")
STYLE = Path(__file__).resolve().parents[1] / "gui" / "static" / "style.css"


def test_group_agent_badge_uses_sender_account_kind():
    chat = CHAT_JS.read_text(encoding="utf-8")

    assert 'ms.users?.[msg.from]?.kind === "agent"' in chat
    assert 'msg.kind === "agent"' not in chat


def test_runtime_contributor_rows_are_polled_keyed_and_content_free():
    chat = CHAT_JS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert "/api/mesh/runtime_tasks?id=" in chat
    assert 'runtimeTasks.map((t) => [t.id, t.state, t.updated_ns])' in chat
    assert '["runtime:" + task.id' in chat
    assert 'consumed: "Contribution used"' in chat
    assert "renderSeq !== chatRenderSeq" in chat
    assert 'role="status" aria-live="polite"' in chat
    assert "data.messages.length === 0 && runtimeTasks.length === 0" in chat
    assert "task.objective" not in chat
    assert ".runtime-task" in style
    assert "@media (max-width: 620px)" in style
