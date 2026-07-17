"""Compatibility entry point for ``python -m gui``.

The supported GUI server lives in ``agentbridge.gui``. Keep this tiny shim so
older local launch habits still land on the current app.
"""

from agentbridge.gui.fastboot import main


if __name__ == "__main__":
    raise SystemExit(main())
