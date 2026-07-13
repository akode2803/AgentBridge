"""Double-click launcher for the v2 agent harness (no console window).

Starts ``python -m agentbridge.harness --all`` in the project venv: one
supervised runner per agent HOSTED ON THIS MACHINE (the directory says which
— an agent's account names its machine; ``adopt_agent`` re-homes one here).
The mesh root is remembered in ``~/.agentbridge/config.json``, same as the
GUI launcher. Successor to v1's ``AgentWorker.pyw``.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
venv_pyw = REPO / ".venv" / "Scripts" / "pythonw.exe"
python = str(venv_pyw) if venv_pyw.is_file() else sys.executable
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

subprocess.Popen(
    [python, "-m", "agentbridge.harness", "--all"],
    cwd=str(REPO),
    stdin=subprocess.DEVNULL,
    creationflags=NO_WINDOW,
)
