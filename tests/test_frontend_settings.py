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
