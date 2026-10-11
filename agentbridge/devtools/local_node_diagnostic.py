"""One explicitly gated, resumable local-node diagnostic step.

This operator command is intentionally separate from GUI and harness startup.
Each invocation performs at most one provider data page and prints bounded,
content-free progress JSON. Re-running the command resumes from the node Store.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from ..core.config import (
    DEFAULT_HOME, configured_machine, load_app_config, validate_root_spec,
)
from ..node.diagnostic_runtime import (
    LocalNodeDiagnosticRuntime, diagnostic_runtime_enabled,
)
from ..node.protocol import ReplicaIdentity
from ..node.recovery import _provider_cut
from ..node.store import NodeStore
from ..transport.supabase import SupabaseTransport, load_supabase_env


def _paths(home: Path, identity: ReplicaIdentity) -> tuple[Path, Path]:
    base = home / "local-node" / identity.digest[:24]
    return base / "node.sqlite3", base / "evidence"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbridge-local-node-diagnostic")
    parser.add_argument("--home", default="")
    parser.add_argument("--root", default="", help="default: remembered cloud root")
    parser.add_argument("--machine", default="", help="default: configured machine")
    return parser


def main(
    argv: list[str] | None = None, *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    source_environment = os.environ if environment is None else environment
    if not diagnostic_runtime_enabled(source_environment):
        parser.error(
            "local-node diagnostics are disabled; set "
            "AGENTBRIDGE_LOCAL_NODE_DIAGNOSTIC=1 explicitly"
        )

    transport = None
    try:
        home = Path(args.home).expanduser() if args.home else DEFAULT_HOME
        config = load_app_config(home)
        root = validate_root_spec(args.root or config.get("mesh_root"))
        machine = args.machine or configured_machine(
            config, platform.node() or "local-node-diagnostic")
        provider_environment = load_supabase_env(home)
        transport = SupabaseTransport(
            root[len("supabase://"):], env=provider_environment, home=home)
        authenticated = _provider_cut(transport.recovery_fence())
        identity = ReplicaIdentity(
            provider_environment.get("SUPABASE_URL", ""), root,
            authenticated.account_id, machine,
        )
        store_path, evidence_path = _paths(home, identity)
        result = LocalNodeDiagnosticRuntime(
            NodeStore(store_path, identity), transport, evidence_path,
            enabled=True,
        ).step(attempted_ns=time.time_ns())
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    except Exception as exc:  # bounded local diagnostics must not print provider payloads
        print(json.dumps({
            "state": "error",
            "error": type(exc).__name__,
        }, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
