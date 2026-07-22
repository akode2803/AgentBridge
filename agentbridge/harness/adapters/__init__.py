"""Model adapters (R16): presets are data, the engine is one.

``ModelRegistry`` loads the preset catalog (shipped JSON + the machine's
``<home>/adapters/`` overlay), probes what's installed HERE, and resolves an
agent's owner-set config into a concrete invocation per audience.
``CliResponder`` is the one subprocess engine every CLI family runs through.
"""

from .cli import CliResponder, extract_step, provider_env, reply_from_output
from .registry import Invocation, ModelRegistry, Preset

__all__ = [
    "CliResponder", "Invocation", "ModelRegistry", "Preset",
    "extract_step", "provider_env", "reply_from_output",
]
