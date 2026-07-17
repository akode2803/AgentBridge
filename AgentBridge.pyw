"""Cross-platform double-click launcher for the AgentBridge GUI.

R14 cutover: this now launches the v2 server (`agentbridge.gui`) in the
project's virtualenv, which has the backend's dependencies. The mesh root is
remembered in ``~/.agentbridge/config.json`` (the migration set it to the v2
``mesh2`` folder), so no path is hard-coded here — a bare launch reuses it and
opens the app window itself.

The shared launcher selects this checkout's virtualenv on Windows, macOS, and
POSIX, detaches without a console, and records startup failures in
``~/.agentbridge/launcher.log``. Kept as a .pyw for direct OS integration.
"""

from pathlib import Path

from agentbridge.core.launcher import run_launcher

REPO = Path(__file__).resolve().parent
run_launcher(REPO, "agentbridge.gui")  # root is remembered in config.json
