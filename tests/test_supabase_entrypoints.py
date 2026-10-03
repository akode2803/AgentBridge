"""Configured roots fail closed before startup resources or persistence."""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ConfigError
from agentbridge import export
from agentbridge.gui import api_auth, context, desktop, fastboot
from agentbridge.harness import runner
from agentbridge.harness.adapters.cli import CliResponder
from agentbridge.transport import supabase_admin


cli_main = import_module("agentbridge.cli.main")


def _unexpected(*_args, **_kwargs):
    raise AssertionError("startup resource reached before root validation")


@pytest.mark.parametrize("entrypoint", ["gui", "harness", "export", "cli", "admin"])
@pytest.mark.parametrize("remembered", [False, True])
@pytest.mark.parametrize("invalid", ["/tmp/former-mesh", "supabase://bad/name"])
def test_entrypoints_reject_invalid_roots_before_activation(
        tmp_path, monkeypatch, entrypoint, remembered, invalid):
    if entrypoint == "cli" and remembered:
        pytest.skip("CLI requires an explicit root")
    config = {"mesh_root": invalid}
    root_args = [] if remembered else ["--root", invalid]
    home_args = ["--home", str(tmp_path / "home")]
    if entrypoint == "gui":
        monkeypatch.setattr(fastboot, "load_app_config", lambda *_: config)
        monkeypatch.setattr(fastboot, "save_app_config", _unexpected)
        monkeypatch.setattr(fastboot.socket, "socket", _unexpected)
        monkeypatch.setattr(desktop, "launch_window", _unexpected)
        import agentbridge.core.lock as lock
        monkeypatch.setattr(lock, "SingleInstance", _unexpected)
        invoke = fastboot.main
        invoke_args = [*root_args, *home_args]
    elif entrypoint == "harness":
        monkeypatch.setattr(runner, "load_app_config", lambda *_: config)
        monkeypatch.setattr(runner, "FleetInstance", _unexpected)
        monkeypatch.setattr(runner, "SingleInstance", _unexpected)
        monkeypatch.setattr(runner, "AgentRunner", _unexpected)
        invoke = runner.main
        invoke_args = ["--all", *root_args, *home_args]
    elif entrypoint == "export":
        monkeypatch.setattr(export, "load_app_config", lambda *_: config)
        monkeypatch.setattr(export, "Mesh", _unexpected)
        invoke = export.main
        invoke_args = ["--user", "aryan", *root_args, *home_args]
    elif entrypoint == "cli":
        monkeypatch.setattr(cli_main, "_mesh", _unexpected)
        invoke = cli_main.main
        invoke_args = [*root_args, "--user", "helper", "mcp"]
    else:
        monkeypatch.setattr(supabase_admin, "load_app_config", lambda *_: config)
        monkeypatch.setattr(supabase_admin, "load_supabase_env", _unexpected)
        monkeypatch.setattr(supabase_admin, "join_mesh", _unexpected)
        invoke = supabase_admin.main
        invoke_args = [*root_args, *home_args, "join", "aryan"]
    with pytest.raises(SystemExit) as exc:
        invoke(invoke_args)
    assert exc.value.code == 2
    assert not (tmp_path / "home").exists()


def test_gui_constructor_rejects_folder_before_local_state(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "make_transport", _unexpected)
    with pytest.raises(ConfigError):
        context.GuiApp(tmp_path / "former-mesh", home=tmp_path / "home")
    assert not (tmp_path / "home").exists()


def test_runner_constructor_rejects_folder_before_mesh(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "Mesh", _unexpected)
    with pytest.raises(ConfigError):
        runner.AgentRunner(tmp_path / "former-mesh", "helper", home=tmp_path / "home")
    assert not (tmp_path / "home").exists()


def test_supervisor_rejects_folder_before_signal_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.signal, "signal", _unexpected)
    with pytest.raises(ConfigError):
        runner.supervise_all(tmp_path / "former-mesh", "devbox", [])


def test_fastboot_preserves_uri_and_merges_config_before_serving(tmp_path, monkeypatch):
    from agentbridge.gui import app as gui_app

    spec = "supabase://世界-room"
    seen = {}
    config = {"mesh_root": "supabase://old", "machine_name": "stable-machine"}
    monkeypatch.setattr(fastboot, "load_app_config", lambda *_: config)
    monkeypatch.setattr(
        fastboot, "save_app_config",
        lambda value, home: seen.update(config=value, home=home),
    )
    socket = SimpleNamespace(
        setsockopt=lambda *_: None, bind=lambda *_: None,
        listen=lambda *_: None, getsockname=lambda: ("127.0.0.1", 12345),
    )
    monkeypatch.setattr(fastboot.socket, "socket", lambda *_: socket)
    monkeypatch.setattr(
        gui_app, "serve", lambda **kwargs: seen.update(served=kwargs) or 0,
    )
    assert fastboot.main([
        "--root", spec, "--home", str(tmp_path), "--port", "0", "--no-browser",
    ]) == 0
    assert seen["config"] == {"mesh_root": spec, "machine_name": "stable-machine"}
    assert seen["served"]["root"] == spec and type(seen["served"]["root"]) is str
    assert seen["served"]["args"].machine == "stable-machine"


def test_export_preserves_supabase_root_and_closes_mesh(tmp_path, monkeypatch):
    seen = {}
    spec = "supabase://世界-room"

    def make_mesh(root, user, machine, **kwargs):
        seen.update(root=root, user=user, machine=machine, kwargs=kwargs)
        return SimpleNamespace(
            sync=SimpleNamespace(sync_once=lambda: None), chats_for=lambda: [],
            close=lambda: seen.update(closed=True),
        )

    monkeypatch.setattr(export, "Mesh", make_mesh)
    assert export.main(["--root", spec, "--user", "aryan", "--home", str(tmp_path)]) == 0
    assert seen["root"] == spec and type(seen["root"]) is str
    assert seen["kwargs"]["home"] == tmp_path
    assert seen["closed"] is True


def test_admin_preserves_exact_root_label(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(supabase_admin, "load_supabase_env", lambda *_: {})
    monkeypatch.setattr(
        supabase_admin, "join_mesh",
        lambda env, user, root, path: seen.update(root=root, user=user),
    )
    supabase_admin.main([
        "--root", "supabase://世界-room", "--home", str(tmp_path), "join", "aryan",
    ])
    assert seen == {"root": "世界-room", "user": "aryan"}


def test_absent_username_requires_mirror_authority_even_for_generic_probe():
    app = SimpleNamespace(
        directory0=SimpleNamespace(handle_taken=lambda _: False),
        transport=SimpleNamespace(scheme="folder"),
    )
    result = api_auth.check_name(app, SimpleNamespace(data={"username": "fresh-user"}))
    assert result["taken"] is False
    assert result["lookup_complete"] is False
    assert result["lookup_state"] == "loading"


def test_denied_provider_paths_use_local_path_without_existence_probe(tmp_path):
    responder = object.__new__(CliResponder)
    responder.home = tmp_path / "home"
    provider_root = tmp_path / "provider"
    responder.mesh = SimpleNamespace(tx=SimpleNamespace(
        root="cloud-label", local_path=lambda _: provider_root,
    ))
    assert responder._deny_roots() == [responder.home, provider_root]
    assert not provider_root.exists()
    responder.mesh.tx.local_path = lambda _: None
    assert responder._deny_roots() == [responder.home]
    assert all(isinstance(path, Path) for path in responder._deny_roots())
