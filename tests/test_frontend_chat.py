"""Focused source-contract checks for chat renderer identity decisions."""

from pathlib import Path


CHAT_JS = (Path(__file__).resolve().parents[1] / "gui" / "static" / "js" /
           "chat.js")
STYLE = Path(__file__).resolve().parents[1] / "gui" / "static" / "style.css"


def test_group_agent_badge_uses_sender_account_kind():
    chat = CHAT_JS.read_text(encoding="utf-8")

    assert 'displayKind(msg.from) === "agent"' in chat
    assert 'ms.users?.[name]?.display_kind' in chat
    assert '|| ms.users?.[name]?.kind' in chat
    assert 'msg.kind === "agent"' not in chat


def test_runtime_contributor_rows_use_bounded_aux_and_stay_content_free():
    chat = CHAT_JS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert "/api/mesh/chat_aux?id=" in chat
    assert "/api/mesh/runtime_tasks?id=" not in chat
    assert 'runtimeTasks.map((t) => [t.id, t.state, t.updated_ns])' in chat
    assert '["runtime:" + task.id' in chat
    assert 'consumed: "Contribution used"' in chat
    assert "renderSeq !== chatRenderSeq" in chat
    assert 'role="status" aria-live="polite"' in chat
    assert "data.messages.length === 0 && runtimeTasks.length === 0" in chat
    assert "task.objective" not in chat
    assert ".runtime-task" in style
    assert "@media (max-width: 620px)" in style


def test_current_run_authority_is_exact_matched_minimized_and_responsive():
    chat = CHAT_JS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert 'const authorityRuns = prepared.aux?.runs || [];' in chat
    assert "/api/mesh/runtime_authority" not in chat
    assert "currentRunAuthority" not in chat
    assert "renderSeq !== chatRenderSeq" in chat
    assert "run.run_id === f.run_id && run.manager === f.agent" in chat
    assert 'run.state === "running"' in chat
    assert 'Mesh.authorityExpand[runId]' in chat
    assert 'aria-label="${open ? "Hide" : "Show"} access for this run"' in chat
    assert 'aria-controls="${esc(panelId)}"' in chat
    assert 'role="region"' in chat
    assert "Access for this run" in chat
    assert '[["enabled", "Allowed"]' not in chat
    assert '["enabled", "Allowed"]' in chat
    assert '["approval_gated", "Controlled"]' in chat
    assert '["blocked", "Blocked"]' in chat
    for private_fact in (
            "run.native_policy_digest", "run.authority_digest",
            "run.workspace", "run.executable", "run.environment",
            "RUN_ACCESS_LABELS"):
        assert private_fact not in chat
    assert ".feed-access-toggle" in style
    assert ".feed-access-counts" in style
    assert "grid-column: 2" in style
    assert "min-height: 32px" in style
    assert "white-space: normal" in style
    assert "#content.chat-mode { min-width: 0; width: 100%; }" in style
