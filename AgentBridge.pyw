"""Double-click launcher for the AgentBridge GUI (no console window).

Equivalent to `python -m gui` run from this folder — kept as a .pyw so
analysts can pin it to the taskbar or Start menu.
"""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv = [sys.argv[0]]
runpy.run_module("gui", run_name="__main__")
