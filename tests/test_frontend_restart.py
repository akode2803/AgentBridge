"""V140 restart-state wiring across every open browser client."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "gui" / "static" / "js"


def test_restart_intent_is_shared_and_generation_gated():
    state = (ROOT / "state.js").read_text(encoding="utf-8")
    main = (ROOT / "main.js").read_text(encoding="utf-8")
    settings = (ROOT / "settings.js").read_text(encoding="utf-8")

    assert 'RESTART_KEY = "ab:restart"' in state
    assert 'localStorage.setItem(RESTART_KEY' in state
    assert 'window.addEventListener("storage"' in main
    assert "App.state.instance_id === restarting.instance" in main
    assert 'V.renderConnectingPage("Restarting…")' in main
    assert "watchRestartGeneration();" in main
    assert "setTimeout(tick, 750)" in main
    assert "beginRestartIntent(App.state?.instance_id)" in settings

    restart_block = settings[settings.index("const restartBtn"):
                             settings.index("// My-agents dropdowns")]
    assert "location.reload()" not in restart_block
    assert 'fetch("/api/state"' not in restart_block


def test_initial_state_failure_reaches_connecting_cover_and_poll_loop():
    main = (ROOT / "main.js").read_text(encoding="utf-8")
    boot = main[main.index('App.state = await api("/api/state")') - 80:
                main.index("if (!location.hash)")]
    assert "try {" in boot and "catch {" in boot
    assert 'V.renderConnectingPage(restartIntent() ? "Restarting…" : "Connecting…")' in boot
