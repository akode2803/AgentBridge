"""Double-click launcher for the AgentBridge GUI (no console window).

R14 cutover: this now launches the v2 server (`agentbridge.gui`) in the
project's virtualenv, which has the backend's dependencies. The mesh root is
remembered in ``~/.agentbridge/config.json`` (the migration set it to the v2
``mesh2`` folder), so no path is hard-coded here — a bare launch reuses it and
opens the app window itself.

Kept as a .pyw so it can be pinned to the taskbar / Start menu. The pre-v2
entry point was ``runpy.run_module("gui")`` (the v1 stdlib server); the v1
code stays in the repo for reference until R26 retires it.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
# the backend needs cryptography etc. — run it in the project venv, not the
# system python a double-click might use
venv_pyw = REPO / ".venv" / "Scripts" / "pythonw.exe"
python = str(venv_pyw) if venv_pyw.is_file() else sys.executable
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

subprocess.Popen(
    [python, "-m", "agentbridge.gui"],   # root read from config.json
    cwd=str(REPO),
    stdin=subprocess.DEVNULL,
    creationflags=NO_WINDOW,
)
