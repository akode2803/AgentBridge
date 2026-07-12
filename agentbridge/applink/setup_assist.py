"""Setup-assist (R11) — a permitted agent helps another machine write its
agent/harness config during install. Rides the control lane.

Trust model:
  * ANYONE may ASK for help (the requester is a machine being set up).
  * An agent only ANSWERS if its owner granted ``AgentRules.setup_assist``
    (default off, R6/R11) — otherwise the request is auto-declined.
  * The reply is a PROPOSED config; the requesting side ALWAYS reviews it
    before applying (apply is out of scope here — the returned proposal is
    handed to the setup UI). A proposal never auto-writes anything.
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
        return self.lane.send(
            to_machine, KIND, {"agent": agent, "context": context or {}}
        )

    # -------------------------------------------------------- responder side
    def _on_request(self, msg: ControlMessage) -> dict | None:
        if msg.reply_to:
            return None  # this is a reply arriving at the requester — not ours
        agent = msg.payload.get("agent", "")
        acc = self.directory.get(agent)
        if acc is None or acc.kind is not UserKind.AGENT:
            return {"ok": False, "reason": "unknown agent"}
        # the gate: the agent's OWNER must have opted this agent into helping
        if not acc.rules().setup_assist:
            return {"ok": False, "reason": "setup-assist not permitted for this agent"}
        proposal = {}
        if self._proposer is not None:
            try:
                proposal = self._proposer(agent, msg.payload.get("context") or {})
            except Exception:  # noqa: BLE001 — a broken proposer just declines
                return {"ok": False, "reason": "proposer error"}
        return {"ok": True, "agent": agent, "proposal": proposal}
