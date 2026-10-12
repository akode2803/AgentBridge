"""R187 — final handout fence for GUI session-bound reads."""

from __future__ import annotations

import json
import threading

import pytest

from agentbridge.applink import AppLink
from agentbridge.core.config import read_json
from agentbridge.gui import api_chats, api_pages
from agentbridge.gui.context import GuiApp, SessionReadToken
from agentbridge.gui.routing import Request, authed, authed_read
from agentbridge.mesh.presence import PresenceService
from agentbridge.mesh.service import Mesh


def _join(thread: threading.Thread) -> None:
    thread.join(10)
    assert not thread.is_alive()


@pytest.mark.parametrize("route,next_user,next_password", [
    ("chat", "aryan", "hexagon"),
    ("chat", "fable", "fablepass"),
    ("state", "aryan", "hexagon"),
    ("state", "fable", "fablepass"),
])
def test_completed_real_read_is_discarded_across_logout_login_aba(
    rig, monkeypatch, route, next_user, next_password,
):
    """Pause at the final observation: all payload work already completed."""
    rig.signup()
    chat_id = ""
    if route == "chat":
        chat_id = rig.post("/api/mesh/create_chat", name="Private", members=[])["chat"]["id"]
        rig.post("/api/mesh/post", chat_id=chat_id, body="R187 old plaintext")
    if next_user == "fable":
        rig.peer_account("fable")
    entered, release = threading.Event(), threading.Event()
    if route == "chat":
        rig.page(chat_id)
    else:
        rig.sidebar()
    original = api_pages.chat_page.__wrapped__ if route == "chat" else api_chats._state_captured

    def block_after_payload(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return value

    if route == "chat":
        # Wrap only payload construction, leaving the public read fence intact.
        from agentbridge.gui.routing import authed_read_token
        monkeypatch.setattr(api_pages, "chat_page", authed_read_token(block_after_payload))
    else:
        monkeypatch.setattr(api_chats, "_state_captured", block_after_payload)
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def read() -> None:
        try:
            handler = api_pages.chat_page if route == "chat" else api_chats.state
            req = Request(params={"id": chat_id}) if route == "chat" else Request()
            result["out"] = handler(rig.app, req)
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(10)
        assert rig.app.logout("hexagon") == {"ok": True}
        assert rig.app.login(next_user, next_password)["ok"] is True
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert not errors
    assert result["out"] == {"error": "Sign in first"}
    assert "R187 old plaintext" not in repr(result["out"])


@pytest.mark.parametrize("next_user,next_password", [("aryan", "hexagon"), ("fable", "fablepass")])
def test_old_token_rejected_across_same_and_other_user_aba(rig, next_user, next_password):
    rig.signup()
    if next_user == "fable":
        rig.peer_account("fable")
    old = rig.app.capture_session_read()
    assert old is not None and rig.app.validate_session_read(old)
    assert rig.app.logout("hexagon") == {"ok": True}
    assert rig.app.login(next_user, next_password)["ok"] is True
    assert not rig.app.validate_session_read(old)


def test_anonymous_state_is_stable_before_auth_and_fenced_against_login(rig, monkeypatch):
    """Pre-auth shape remains useful, but an in-flight login invalidates it."""
    initial = rig.get("/api/mesh/state")
    assert initial["user"] is None and initial["v"] == 2
    entered, release = threading.Event(), threading.Event()
    original = rig.app.directory0.names

    def blocked_names():
        entered.set()
        assert release.wait(10)
        return original()

    monkeypatch.setattr(rig.app.directory0, "names", blocked_names)
    out: dict[str, object] = {}
    reader = threading.Thread(
        target=lambda: out.setdefault("value", api_chats.state(rig.app, Request())), daemon=True)
    reader.start()
    try:
        assert entered.wait(10)
        # Create/login through the public owner while the anonymous response is pending.
        rig.app.signup("aryan", "", "hexagon")
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert out["value"] == {"error": "Sign in first"}


def test_valid_reads_do_not_hold_session_lock_during_handler_work(rig):
    rig.signup()
    entered, release = threading.Event(), threading.Event()
    count = {"n": 0}
    count_lock = threading.Lock()

    @authed_read
    def slow_read(app, req, mesh):
        del app, req, mesh
        with count_lock:
            count["n"] += 1
            if count["n"] == 2:
                entered.set()
        assert release.wait(10)
        return {"ok": True}

    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(slow_read(rig.app, Request())), daemon=True)
               for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(5), "second read was serialized behind session lock"
        release.set()
        for thread in threads:
            _join(thread)
    finally:
        release.set()
        for thread in threads:
            _join(thread)
    assert results == [{"ok": True}, {"ok": True}]


def test_token_tampering_and_cross_app_fail_closed(rig, tmp_path, clouds):
    rig.signup()
    token = rig.app.capture_session_read()
    assert token is not None
    other = GuiApp(clouds.root(tmp_path / "other-root"), home=tmp_path / "other-home", machine="other")
    try:
        assert not other.validate_session_read(token)
        object.__setattr__(token, "generation", token.generation + 1)
        assert not rig.app.validate_session_read(token)
        forged = SessionReadToken(rig.app.instance_id, 0, None)
        assert not rig.app.validate_session_read(forged)
    finally:
        other.close()


class _SensitiveReadError(Exception):
    pass


def test_stale_read_exception_is_replaced_but_current_exception_identity_survives(rig):
    rig.signup()
    started, release = threading.Event(), threading.Event()
    marker = _SensitiveReadError("R187 old viewer detail")

    @authed_read
    def failing_read(app, req, mesh):
        del app, req, mesh
        started.set()
        assert release.wait(10)
        raise marker

    out: dict[str, object] = {}
    thread = threading.Thread(target=lambda: out.setdefault("value", failing_read(rig.app, Request())), daemon=True)
    thread.start()
    try:
        assert started.wait(10)
        assert rig.app.logout("hexagon") == {"ok": True}
        release.set()
        _join(thread)
    finally:
        release.set()
        _join(thread)
    assert out["value"] == {"error": "Sign in first"}
    assert "old viewer" not in repr(out["value"])

    assert rig.app.login("aryan", "hexagon")["ok"] is True
    current = _SensitiveReadError("current failure")

    @authed_read
    def current_failure(app, req, mesh):
        del app, req, mesh
        raise current

    with pytest.raises(_SensitiveReadError) as raised:
        current_failure(rig.app, Request())
    assert raised.value is current


def test_deleted_token_field_fails_closed_without_owner_state_access(rig):
    rig.signup()
    token = rig.app.capture_session_read()
    assert token is not None
    object.__delattr__(token, "mesh")
    assert not rig.app.validate_session_read(token)


def test_generation_exhaustion_and_detach_failure_still_clear_session(rig, monkeypatch):
    rig.signup()
    old = rig.app.capture_session_read()
    assert old is not None
    mesh = rig.app.mesh
    assert mesh is not None
    rig.app._session_generation = 2**63 - 1
    original_close = mesh.close

    def broken_close():
        original_close()
        raise RuntimeError("close interrupted")

    monkeypatch.setattr(mesh, "close", broken_close)
    with pytest.raises(RuntimeError, match="close interrupted"):
        rig.app.logout("hexagon")
    assert rig.app.mesh is None
    assert rig.app._teardown_mesh is mesh
    assert not rig.app.validate_session_read(old)
    assert rig.app.capture_session_read() is None
    monkeypatch.setattr(mesh, "close", original_close)
    assert rig.app._finish_teardown()
    assert rig.app._teardown_mesh is None


def test_stale_restore_cannot_replace_a_concurrent_login(rig, monkeypatch):
    rig.signup()
    with rig.app._lock:
        rig.app._detach()
    entered = threading.Event()
    release = threading.Event()
    original_names = rig.app.directory0.names

    def blocked_names():
        entered.set()
        assert release.wait(10)
        return original_names()

    monkeypatch.setattr(rig.app.directory0, "names", blocked_names)
    restoring = threading.Thread(target=rig.app.restore, daemon=True)
    restoring.start()
    assert entered.wait(10)

    assert rig.app.login("aryan", "hexagon")["ok"] is True
    current = rig.app.mesh
    stale_builds = []
    monkeypatch.setattr(
        rig.app, "_build",
        lambda name: stale_builds.append(name) or pytest.fail("stale restore built a Mesh"),
    )
    release.set()
    _join(restoring)

    assert rig.app.mesh is current
    assert not stale_builds


def test_stale_authoritative_restore_cannot_unlink_a_new_session(rig, monkeypatch):
    rig.signup()
    with rig.app._lock:
        rig.app._detach()
    entered = threading.Event()
    release = threading.Event()

    def blocked_names():
        entered.set()
        assert release.wait(10)
        return ["aryan"]

    monkeypatch.setattr(rig.app.directory0, "names", blocked_names)
    monkeypatch.setattr(rig.app.directory0, "get", lambda _name: None)
    restoring = threading.Thread(target=rig.app.restore, daemon=True)
    restoring.start()
    assert entered.wait(10)

    assert rig.app.signup("fable", "", "fablepass")["ok"] is True
    release.set()
    _join(restoring)

    assert rig.app.user == "fable"
    assert rig.app._session_path.exists()
    assert read_json(rig.app._session_path)["user"] == "fable"


def test_pending_restore_retry_hands_off_after_stale_owner_exit(
        tmp_path, clouds, monkeypatch):
    app = GuiApp(
        clouds.root(tmp_path / 'retry-root'), home=tmp_path / 'retry-home',
        machine='gui', encrypt=False,
    )
    app._session_path.parent.mkdir(parents=True, exist_ok=True)
    app._session_path.write_text(json.dumps({'user': 'aryan'}), encoding='utf-8')
    started = []

    class DeferredThread:
        def __init__(self, *, target, daemon, name):
            self.target, self.daemon, self.name = target, daemon, name

        def start(self):
            started.append(self)

    monkeypatch.setattr(threading, 'Thread', DeferredThread)
    old_token = object()
    with app._lock:
        app._restore_retrying = True
        app._restore_retry_token = old_token
        app._restore_retry_pending = False

    # A fatal activation that arrives before the old retry's finally block
    # must leave a durable handoff instead of being discarded as a duplicate.
    app._schedule_restore_retry(every_s=.01, cap_s=1)
    assert app._restore_retry_pending
    assert started == []

    assert app._release_restore_retry(old_token)
    app._schedule_restore_retry(every_s=.01, cap_s=1)

    assert len(started) == 1
    assert app._restore_retrying
    assert app._restore_retry_token is not None
    assert app._restore_retry_token is not old_token
    app.close()


def test_failed_adopt_is_transient_then_cleanup_and_re_adopt_recovers(rig, monkeypatch):
    rig.signup()
    assert rig.app.logout("hexagon") == {"ok": True}
    original_start = Mesh.start

    def broken_start(self, **_kwargs):
        raise RuntimeError("start interrupted")

    monkeypatch.setattr(Mesh, "start", broken_start)
    with pytest.raises(RuntimeError, match="start interrupted"):
        rig.app.login("aryan", "hexagon")
    assert rig.app.capture_session_read() is None
    monkeypatch.setattr(Mesh, "start", original_start)
    assert rig.app.logout("hexagon") == {"ok": True}
    assert rig.app.login("aryan", "hexagon")["ok"] is True
    token = rig.app.capture_session_read()
    assert token is not None and rig.app.validate_session_read(token)


def test_adopt_announces_machine_before_starting_presence(rig, monkeypatch):
    events = []
    original_start = Mesh.start

    def start(self, *, heartbeat=True):
        events.append(("mesh_start", heartbeat))
        return original_start(self, heartbeat=heartbeat)

    monkeypatch.setattr(Mesh, "start", start)
    announce = AppLink.announce
    presence_start = PresenceService.start

    def record_announce(self, capabilities=None):
        events.append(("announce", tuple(capabilities or ())))
        return announce(self, capabilities)

    def record_presence(self, interval=None):
        events.append(("presence_start", interval))
        return presence_start(self, interval)

    monkeypatch.setattr(AppLink, "announce", record_announce)
    monkeypatch.setattr(PresenceService, "start", record_presence)
    rig.signup()
    assert events[:3] == [
        ("mesh_start", False), ("announce", ("gui",)), ("presence_start", None),
    ]


def test_closed_app_rejects_signup_before_account_mutation(rig, monkeypatch):
    rig.app.close()
    builds = []
    monkeypatch.setattr(
        rig.app, '_build',
        lambda name: builds.append(name) or pytest.fail('closed app built a Mesh'),
    )

    with pytest.raises(RuntimeError, match='GUI app is closed'):
        rig.app.signup('late-user', '', 'late-password')

    assert builds == []


def test_closed_app_rejects_login_before_account_or_agent_mutation(rig, monkeypatch):
    rig.signup()
    assert rig.app.logout('hexagon') == {'ok': True}
    rig.app.close()
    builds = []
    monkeypatch.setattr(
        rig.app, '_build',
        lambda name: builds.append(name) or pytest.fail('closed app built a Mesh'),
    )

    with pytest.raises(RuntimeError, match='GUI app is closed'):
        rig.app.login('aryan', 'hexagon')

    assert builds == []


def test_close_marks_app_closed_before_diagnostics_flush(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_close = rig.app.diagnostics.close

    def delayed_diagnostics_close():
        entered.set()
        assert release.wait(10)
        return original_close()

    monkeypatch.setattr(rig.app.diagnostics, 'close', delayed_diagnostics_close)
    closer = threading.Thread(target=rig.app.close, daemon=True)
    closer.start()
    try:
        assert entered.wait(10)
        with pytest.raises(RuntimeError, match='GUI app is closed'):
            rig.app.signup('late-user', '', 'late-password')
    finally:
        release.set()
        _join(closer)


def test_authenticated_logout_clears_session_when_teardown_is_retained(
        rig, monkeypatch):
    rig.signup()
    mesh = rig.app.mesh
    assert mesh is not None and rig.app._session_path.exists()
    original_close = mesh.close
    monkeypatch.setattr(
        mesh, 'close',
        lambda: (_ for _ in ()).throw(TimeoutError('retirement pending')),
    )

    with pytest.raises(TimeoutError, match='retirement pending'):
        rig.app.logout('hexagon')

    assert rig.app.mesh is None
    assert rig.app._teardown_mesh is mesh
    assert not rig.app._session_path.exists()
    monkeypatch.setattr(mesh, 'close', original_close)
    with rig.app._lock:
        assert rig.app._finish_teardown()


def test_close_flushes_failed_shutdown_diagnostics(rig, monkeypatch):
    rig.signup()
    mesh = rig.app.mesh
    assert mesh is not None
    original_close = mesh.close
    assert rig.app.diagnostics.set_enabled(True, sample_rate=0)
    monkeypatch.setattr(
        mesh, 'close',
        lambda: (_ for _ in ()).throw(RuntimeError('close failed')),
    )

    with pytest.raises(RuntimeError, match='close failed'):
        rig.app.close()

    assert rig.app._closed and rig.app.mesh is None
    assert rig.app._teardown_mesh is mesh
    recorded = [
        json.loads(line)
        for path in sorted(rig.app.diagnostics.directory.glob('events*.jsonl'))
        for line in path.read_text().splitlines()
    ]
    assert any(
        row.get('event') == 'delivery'
        and row.get('phase') == 'shutdown'
        and row.get('status') == 'error'
        and row.get('outcome') == 'failed'
        and row.get('error_type') == 'OtherError'
        for row in recorded
    )
    monkeypatch.setattr(mesh, 'close', original_close)
    with rig.app._lock:
        assert rig.app._finish_teardown()


def test_writes_keep_entry_only_auth_and_lock_denials(rig):
    rig.signup()
    chat_id = rig.post("/api/mesh/create_chat", name="Writes", members=[])["chat"]["id"]
    rig.app.lock.configure("local-pass")
    rig.app.lock.lock()
    assert rig.post("/api/mesh/post", chat_id=chat_id, body="blocked") == {
        "error": "App is locked", "locked": True}
    rig.app.lock.unlock()
    assert rig.post("/api/mesh/post", chat_id=chat_id, body="still writes")["ok"] is True

    @authed
    def write(app, req, mesh):
        del app, req, mesh
        return {"written": True}

    # The mutation wrapper retains its entry-only contract: no final token fence.
    assert write(rig.app, Request()) == {"written": True}
