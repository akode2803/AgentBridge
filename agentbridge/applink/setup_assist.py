"""Setup-assist (R11) — a permitted agent helps another machine write its
agent/harness config during install. Rides the control lane.

R124 authenticates and encrypts the request path, validates exact target
hosting, and then declines every proposal because the legacy owner permission
still lives in an unsigned profile projection. The proposer API remains as a
compatibility seam but cannot run until authenticated policy evidence lands.
"""

from __future__ import annotations

from typing import Callable

from ..core.models import UserKind
from ..mesh.directory import Directory
from .control import ControlLane, ControlMessage

__all__ = ["SetupAssist", "KIND"]

KIND = "setup_assist"


class SetupAssist:
    def __init__(self, lane: ControlLane, directory: Directory) -> None:
        self.lane = lane
        self.directory = directory
        # the owner-supplied config proposer: (agent, context) -> proposal dict.
        # Wired by the harness (R15+); until then a declared agent that has the
        # capability but no proposer simply returns an empty proposal.
        self._proposer: Callable[[str, dict], dict] | None = None
        lane.register(KIND, self._on_request)

    def set_proposer(self, proposer: Callable[[str, dict], dict]) -> None:
        self._proposer = proposer

    # -------------------------------------------------------- requester side
    def request(self, to_machine: str, agent: str, context: dict | None = None) -> str:
        """Ask ``agent`` (hosted on ``to_machine``) to propose a config."""
        acc = self.directory.get(agent)
        if not acc or not acc.agent or acc.agent.machine != to_machine:
            raise ValueError("target agent is not active on that machine")
        return self.lane.send(
            to_machine, KIND, {"agent": agent, "context": context or {}},
            to_user=acc.agent.owner,
        )

    # -------------------------------------------------------- responder side
    def _on_request(self, msg: ControlMessage) -> dict | None:
        if msg.reply_to:
            return None  # this is a reply arriving at the requester — not ours
        agent = msg.payload.get("agent", "")
        acc = self.directory.get(agent)
        if acc is None or acc.kind is not UserKind.AGENT:
            return {"ok": False, "reason": "unknown agent"}
        if (not acc.agent or acc.agent.machine != self.lane.machine
                or acc.agent.owner != self.lane.user):
            return {"ok": False, "reason": "agent is not hosted by this responder"}
        # the gate: the agent's OWNER must have opted this agent into helping
        if not acc.rules().setup_assist:
            return {"ok": False, "reason": "setup-assist not permitted for this agent"}
        return {"ok": False,
                "reason": "setup-assist permission is not yet authenticated"}
