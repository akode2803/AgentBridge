"""Operator entry point for the disabled local-node diagnostic runtime."""

import json
from types import SimpleNamespace

import pytest

from agentbridge.devtools import local_node_diagnostic as command
from agentbridge.node.diagnostic_runtime import (
    LOCAL_NODE_DIAGNOSTIC_ENV, NodeDiagnosticStepResult,
)
from agentbridge.transport.recovery_sources import RecoveryCut

ACCOUNT = "12345678-1234-5678-9234-567812345678"
EPOCH = "87654321-4321-5678-9234-567812345678"


def fence():
    return RecoveryCut(
        1, "root-manifest-v1", 2, EPOCH, ACCOUNT, "authenticated", 0, 0)


def test_command_fails_before_configuration_or_provider_when_gate_is_off(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        command, "load_app_config",
        lambda _home: (_ for _ in ()).throw(AssertionError("config touched")),
    )
    with pytest.raises(SystemExit) as stopped:
        command.main(["--home", str(tmp_path)], environment={})
    assert stopped.value.code == 2


def test_command_runs_one_enabled_step_with_deterministic_private_paths(
    tmp_path, monkeypatch, capsys,
):
    closed = []
    observed = {}
    source = SimpleNamespace(
        recovery_fence=fence, close=lambda: closed.append(True))
    monkeypatch.setattr(
        command, "load_app_config",
        lambda _home: {"mesh_root": "supabase://team", "machine_name": "desk"},
    )
    monkeypatch.setattr(
        command, "load_supabase_env",
        lambda _home: {"SUPABASE_URL": "https://example.supabase.co"},
    )
    monkeypatch.setattr(command, "SupabaseTransport", lambda *a, **k: source)
    monkeypatch.setattr(
        command, "NodeStore",
        lambda path, identity: observed.update(store=path, identity=identity) or object(),
    )

    class Runtime:
        def __init__(self, store, transport, evidence, *, enabled):
            observed.update(
                runtime_store=store, transport=transport,
                evidence=evidence, enabled=enabled,
            )

        def step(self, *, attempted_ns):
            observed["attempted_ns"] = attempted_ns
            return NodeDiagnosticStepResult(
                "running", "recovery", "started",
                recovery_generation=1, source_cursor=0,
            )

    monkeypatch.setattr(command, "LocalNodeDiagnosticRuntime", Runtime)
    assert command.main(
        ["--home", str(tmp_path)],
        environment={LOCAL_NODE_DIAGNOSTIC_ENV: "1"},
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "recovery"
    assert observed["store"].name == "node.sqlite3"
    assert observed["evidence"].name == "evidence"
    assert observed["identity"].principal == ACCOUNT
    assert observed["identity"].machine == "desk"
    assert observed["enabled"] is True
    assert observed["attempted_ns"] > 0
    assert closed == [True]


def test_command_reports_only_bounded_error_category_and_closes_transport(
    tmp_path, monkeypatch, capsys,
):
    closed = []
    source = SimpleNamespace(
        recovery_fence=fence, close=lambda: closed.append(True))
    monkeypatch.setattr(
        command, "load_app_config", lambda _home: {"mesh_root": "supabase://team"})
    monkeypatch.setattr(
        command, "load_supabase_env",
        lambda _home: {"SUPABASE_URL": "https://example.supabase.co"},
    )
    monkeypatch.setattr(command, "SupabaseTransport", lambda *a, **k: source)
    monkeypatch.setattr(command, "NodeStore", lambda *a, **k: object())

    class Runtime:
        def __init__(self, *args, **kwargs):
            pass

        def step(self, **kwargs):
            raise RuntimeError("PRIVATE_PROVIDER_PAYLOAD")

    monkeypatch.setattr(command, "LocalNodeDiagnosticRuntime", Runtime)
    assert command.main(
        ["--home", str(tmp_path)],
        environment={LOCAL_NODE_DIAGNOSTIC_ENV: "1"},
    ) == 1
    error = capsys.readouterr().err
    assert json.loads(error) == {"error": "RuntimeError", "state": "error"}
    assert "PRIVATE_PROVIDER_PAYLOAD" not in error
    assert closed == [True]


def test_command_sanitizes_configuration_failure_before_transport(tmp_path, capsys):
    assert command.main(
        ["--home", str(tmp_path), "--root", "PRIVATE_BAD_ROOT"],
        environment={LOCAL_NODE_DIAGNOSTIC_ENV: "1"},
    ) == 1
    error = capsys.readouterr().err
    assert json.loads(error) == {"error": "ConfigError", "state": "error"}
    assert "PRIVATE_BAD_ROOT" not in error
