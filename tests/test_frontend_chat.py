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


def test_current_run_authority_is_exact_matched_minimized_and_responsive():
    chat = CHAT_JS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert 'api("/api/mesh/runtime_authority", {' in chat
    assert "run_ids: ids" in chat
    assert "i += 20" in chat
    assert "runIds.slice(i, i + 20)" in chat
    assert "timeoutMs: 4000" in chat
    assert "Promise.all(batches.map" in chat
    assert "currentRunAuthority(chatId, feeds)" in chat
    assert "renderSeq !== chatRenderSeq" in chat
    assert "run.run_id === f.run_id && run.manager === f.agent" in chat
    assert 'run.state === "running"' in chat
    assert "Mesh.authorityPoll?.[chatId] !== request" in chat
    assert "const valid = outputs.filter" in chat
    assert "const verified = valid.flatMap" in chat
    assert "valid.length === outputs.length && verified.length" in chat
    assert "const merged = new Map" in chat
    assert "verified.forEach((run) => merged.set(run.run_id, run))" in chat
    assert "cached && cached.key === key ? cached.runs : []" in chat
    assert "now - cached.at < 5000" not in chat
    assert "(!poll.pending && now - poll.at > 2000)" in chat
    authority_fn = chat[
        chat.index("function currentRunAuthority"):
        chat.index("function runAccessDetails")
    ]
    assert "delete Mesh.authorityCache[chatId]" in authority_fn
    assert authority_fn.count("delete Mesh.authorityCache[chatId]") == 1
    assert "await" not in authority_fn
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
