"""AgentBridge GUI static frontend package (native ES modules under
``static/js/``, served by the v2 stdlib server in ``agentbridge/gui/``).

Zero third-party dependencies by design: analyst machines cannot be assumed
to have pip access, and the frontend must run in a bare Edge app window.

The app ``__version__`` moved to ``agentbridge/__init__.py`` in R26 — this
package no longer carries it (nothing should import a version from here).
"""
