"""C4.1 bridge catalog completeness and public-report contracts."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from agentbridge.harness.capabilities import (
    BRIDGE_CAPABILITIES, BRIDGE_CAPABILITY_SCHEMA, bridge_capability_report,
)


ROOT = Path(__file__).resolve().parents[1]


def _registered_bridge_tools() -> set[str]:
    tree = ast.parse((ROOT / "agentbridge/harness/bridge.py").read_text(
        encoding="utf-8"))
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    }


def test_every_bridge_tool_has_exactly_one_reviewed_catalog_entry():
    assert _registered_bridge_tools() == set(BRIDGE_CAPABILITIES)
    assert set(BRIDGE_CAPABILITIES) == {
        spec.id for spec in BRIDGE_CAPABILITIES.values()
    }
    assert all(spec.failure_mode == "deny"
               for spec in BRIDGE_CAPABILITIES.values())
    assert {name for name, spec in BRIDGE_CAPABILITIES.items()
            if spec.surface == "model-capability"} == {"delegate_agent"}


def test_every_agentbridge_tool_is_documented():
    tools = json.loads((ROOT / "agentbridge/harness/prompts/tooldocs.json")
                       .read_text(encoding="utf-8"))["tools"]
    # ``approve`` is provider control-plane plumbing, not a model-facing tool.
    assert _registered_bridge_tools() - {"approve"} <= set(tools)
    assert BRIDGE_CAPABILITIES["approve"].surface == "control-plane"


def test_public_report_is_versioned_complete_and_secret_free():
    report = bridge_capability_report()
    assert report["schema_version"] == BRIDGE_CAPABILITY_SCHEMA
    assert {item["id"] for item in report["tools"]} == set(BRIDGE_CAPABILITIES)
    encoded = json.dumps(report)
    assert "token" not in encoded.lower()
    assert "credential" not in encoded.lower()


def test_catalog_records_are_immutable():
    with pytest.raises(FrozenInstanceError):
        BRIDGE_CAPABILITIES["delegate_agent"].risk = "low"
    with pytest.raises(TypeError):
        BRIDGE_CAPABILITIES["future_tool"] = BRIDGE_CAPABILITIES["read_docs"]
