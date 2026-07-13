"""Double-click launcher for AgentBridge agent workers (no console window).

Supervises every agent configured on THIS machine: one restart-protected
worker per `worker_<agent>.json` found in ~/.agentbridge. Deliberately the
SAME app for every agent — claude, coco, codex, ollama, anything — with no
agent name in the code; only the JSON config differs (set at setup or in
Settings). Mirrors AgentBridge.pyw, which does the same for the GUI server.

Each child runs `agent_worker.py <agent> --supervise`, which relaunches its
worker on a crash and holds a single-instance lock so an agent can never be
double-served. Kept as a .pyw so it can be pinned to the taskbar / Start menu
and started at login.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home() / ".agentbridge"


def main():
    agents = sorted(p.stem[len("worker_"):] for p in HOME.glob("worker_*.json"))
    if not agents:
        return  # nothing configured on this machine yet
    procs = [subprocess.Popen([sys.executable, str(REPO / "agent_worker.py"),
                               a, "--supervise"]) for a in agents]
    for p in procs:
        try:
            p.wait()
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
