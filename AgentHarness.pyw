"""Cross-platform double-click launcher for the agent harness.

Starts ``python -m agentbridge.harness --all`` in the project venv: one
supervised runner per agent HOSTED ON THIS MACHINE (the directory says which
— an agent's account names its machine; ``adopt_agent`` re-homes one here).
The mesh root is remembered in ``~/.agentbridge/config.json``, same as the
GUI launcher. The shared launcher selects the checkout virtualenv, detaches
without a console, and logs startup failures locally.
"""

from pathlib import Path

from agentbridge.core.launcher import run_launcher

REPO = Path(__file__).resolve().parent
run_launcher(REPO, "agentbridge.harness", ["--all"])
