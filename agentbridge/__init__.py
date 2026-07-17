"""AgentBridge backend.

Layout: core -> transport/store/crypto -> mesh services -> harness / cli /
gui connectors.

``__version__`` is the APP version source of truth (moved here from
``gui/__init__.py`` in R26). Bump it with the Edit tool every round — never
PowerShell (it re-encodes to UTF-16+BOM and mangles em-dashes).
"""

__version__ = "0.24.192"

__all__ = ["__version__"]
