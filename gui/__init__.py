"""AgentBridge GUI — local web app (stdlib only) rendered in an Edge app window.

Serves the analyst-facing interface for the bridge: setup wizard, channel
dashboard, transcript/composer, and (later) the CoCo livestream pane.
Zero third-party dependencies by design: analyst machines cannot be assumed
to have pip access, and the setup wizard must run before anything is installed.
"""

__version__ = "0.24.33"
