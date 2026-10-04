"""Source-level contracts for Settings interactions without a JS DOM runner."""

from pathlib import Path


ROOT = Path(__file__).parents[1] / "gui" / "static" / "js"
STYLE = Path(__file__).parents[1] / "gui" / "static" / "style.css"


def test_agent_timer_rows_have_accessible_signed_dismiss_flow():
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert 'class="ag-timer-x"' in settings
    assert 'aria-label="Dismiss this wake-up"' in settings
    assert 'api("/api/mesh/timer_cancel"' in settings
    assert "Mesh.timerDone" in settings
    assert "btn.disabled = true" in settings
    assert "Unable to dismiss the wake-up while offline" in settings
    assert "dd.insertBefore(row" in settings
    assert ".ag-timer-x" in style and "width: 28px" in style


def test_mobile_agent_routes_can_shrink_inside_settings_grid():
    style = STYLE.read_text(encoding="utf-8")

    assert "@media (max-width: 760px)" in style
    assert ".settings-body .ag-route-name" in style
    assert ".settings-body .ag-route .csel" in style
    assert "grid-template-columns: auto minmax(0, 1fr)" in style
    assert ".settings-body .ag-route-model { grid-column: 1 / -1;" in style
    assert "overflow: visible; text-overflow: clip; white-space: normal;" in style
    assert ".settings-body .ag-route .csel { width: 100%; }" in style
    assert ".settings-body .ag-head h2 { min-width: 0; }" in style


def test_remounted_agent_model_controls_still_autosave():
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")
    remount = settings[settings.index("const remount ="):
                       settings.index("const refreshModels =")]

    assert 'slot.classList.contains("ag-model")' in remount
    assert "refreshEfforts(slot.dataset.agent)" in remount
    assert "agConfigSave(slot.dataset.agent)" in remount


def test_cloud_connection_and_account_surfaces_have_no_folder_transport_controls():
    chat = (ROOT / "chat.js").read_text(encoding="utf-8")
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")
    state = (ROOT / "state.js").read_text(encoding="utf-8")
    connection = chat[chat.index("function connectionRows(s)"):
                      chat.index("V.connectionRows = connectionRows")]
    about = settings[settings.index("function connKvRows(s)"):
                     settings.index("async function renderSettings()")]

    assert '<dt>Cloud mesh</dt>' in connection
    assert 'm.age_s > 120' in connection
    assert 'm.retry_in_s' in connection
    assert 'm.hints_suspect' in connection
    assert 'm.transfer' in connection
    assert 'm.auth.startsWith("member:")' in connection
    assert '<dt>Cloud root</dt>' in about
    assert 's.connection?.root' in about
    assert 'shared_dir' not in connection + about
    assert 'scheme === "folder"' not in connection
    assert "openTarget('shared')" not in settings
    assert "machine that syncs it" not in settings
    assert "folder_unavailable" not in state
    assert "folder_read_only" not in state
    assert 'title="Save to a folder"' in chat
    assert "Open config folder" in settings
