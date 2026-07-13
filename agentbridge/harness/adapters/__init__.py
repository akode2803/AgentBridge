"""Model adapters (R16): presets are data, the engine is one.

``ModelRegistry`` loads the preset catalog (shipped JSON + the machine's
``<home>/adapters/`` overlay), probes what's installed HERE, and resolves an
agent's owner-set config into a concrete invocation per audience.
``CliResponder`` is the one subprocess engine every CLI family runs through.
"""

from .cli import CliResponder, reply_from_output, summarize_stream_event
from .registry import Invocation, ModelRegistry, Preset

__all__ = [
    "CliResponder", "Invocation", "ModelRegistry", "Preset",
    "reply_from_output", "summarize_stream_event",
]
