"""App-to-app link (R11): machine registry, control lane, auto-update
detection/verification, and setup-assist. Machine-scoped (not per-chat), so
it lives beside the mesh services rather than inside them.
"""

from ..mesh.directory import Directory
from ..store.db import Store
from ..transport.base import Transport
from .control import ControlLane, ControlMessage
from .machines import MachineRegistry
from .setup_assist import SetupAssist
from .update import UpdatePlan, UpdateService

__all__ = [
    "AppLink", "MachineRegistry", "ControlLane", "ControlMessage",
    "UpdateService", "UpdatePlan", "SetupAssist",
]


class AppLink:
    """The machine-scoped façade tying the pieces together for one machine."""

    def __init__(
        self,
        tx: Transport,
        store: Store,
        directory: Directory,
        machine: str,
        *,
        user: str = "",
        app_version: str = "",
        release_info=None,
    ) -> None:
        self.registry = MachineRegistry(tx, machine, user=user, app_version=app_version)
        self.control = ControlLane(tx, store, machine, user=user)
        self.setup_assist = SetupAssist(self.control, directory)
        self.update = (
            UpdateService(self.registry, app_version, release_info=release_info)
            if release_info is not None
            else None
        )

    def announce(self, capabilities: list[str] | None = None) -> dict:
        return self.registry.announce(capabilities=capabilities)

    def poll(self):
        return self.control.poll()
