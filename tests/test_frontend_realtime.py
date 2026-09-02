from pathlib import Path


ROOT = Path(__file__).parents[1] / "gui" / "static" / "js"


def test_realtime_refresh_is_coalesced_and_visibility_scoped():
    main = (ROOT / "main.js").read_text(encoding="utf-8")
    realtime = (ROOT / "realtime.js").read_text(encoding="utf-8")
    chat = (ROOT / "chat.js").read_text(encoding="utf-8")
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")

    assert "if (refreshPromise)" in main
    assert "refreshDirty = true" in main
    assert "while (refreshDirty)" in main
    assert "queueMicrotask" not in main
    assert "await V.renderChats(false)" in main
    assert "document.hidden || !document.hasFocus()" in main
    assert 'api("/api/mesh/activity", { active })' in realtime
    assert 'setTimeout(reportActivity, 10000)' in realtime
    assert realtime.index("reportActivity();") < realtime.index("typeof EventSource")
    assert "V.renderMeshChat(false)" not in realtime
    assert "performance.now()" in realtime
    assert 'observe("browser_received"' in realtime
    assert 'observe("refetch_completed"' in realtime
    assert 'observe("render_completed"' in realtime
    assert "window.agentBridgeRealtimeMetrics = realtimeMetrics" in realtime
    assert "window.agentBridgeChatOpenMetrics = chatOpenMetrics" in chat
    assert "sidebar_fetch_ms" in chat
    assert "chat_fetch_ms" in chat
    assert "aux_fetch_ms" in chat
    assert "first_painted_ms" in chat
    assert "openTrace.chat_id" not in chat
    assert "openTrace.body" not in chat
    assert "Date.now() - Math.round(Number(frame.server_ns)" not in realtime
    assert "if (document.hidden || !document.hasFocus()) return;" in chat
    assert "fetchSeq !== chatsFetchSeq" in chat
    assert "routeSeq !== App.routeSeq" in chat
    assert 'App.page !== "new" || routeSeq !== App.routeSeq' in chat
    assert 'App.page !== "settings" || routeSeq !== App.routeSeq' in settings


def test_realtime_frames_remain_read_model_wakes_not_payloads():
    realtime = (ROOT / "realtime.js").read_text(encoding="utf-8")
    assert "V.refresh(false)" in realtime
    assert "frame.body" not in realtime
    assert "frame.activity" not in realtime


def test_settings_has_no_mesh_global_pause_control():
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")
    assert "st-pause" not in settings
    assert "/api/mesh/pause" not in settings
    assert "Stand down all agents" not in settings
