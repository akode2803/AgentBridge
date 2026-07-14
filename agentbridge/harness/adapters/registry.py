"""The model registry (R16) — a model/CLI is DATA, never a branch (D8).

Presets are JSON files (``presets/*.json``, plus any the machine drops into
``<home>/adapters/``): command, argv templates, parser format, safety
defaults. The registry loads them, probes which families are actually
installed on THIS machine, and resolves an agent's owner-set harness config
into one concrete ``Invocation`` per run.

Model resolution order (most specific wins): the chat's own model → the
override-all ``model`` → the per-purpose route's model → the preset default.
Families with one fixed install (or none worth choosing between) simply
resolve without a model flag — the picker degrades to enable/disable per
audience.

Shaped for swarms: everything resolves from (account config, category) — a
future instance carries its own config dict and rides the same path.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ...core.config import DEFAULT_HOME
from ...core.errors import ValidationError
from ..settings import HarnessSettings

__all__ = ["Preset", "Invocation", "ModelRegistry"]

PRESET_DIR = Path(__file__).resolve().parent / "presets"
FORMATS = ("claude-stream", "codex-jsonl", "text")


@dataclass
class Preset:
    id: str = ""
    label: str = ""
    command: str = ""                 # executable name or absolute path
    args: list[str] = field(default_factory=list)          # {prompt}/{reply_file}/{workdir}
    args_minimal: list[str] = field(default_factory=list)  # usage-error fallback
    safety_args: list[str] = field(default_factory=list)   # NEVER dropped
    model_args: list[str] = field(default_factory=list)    # {model}
    effort_args: list[str] = field(default_factory=list)   # {effort}
    efforts: list[str] = field(default_factory=list)       # allowed values
    # per-MODEL effort sets (Q13): a model listed here narrows (or widens)
    # the family's efforts; absent models use the family list. Data, not
    # code — an owner can refine it via a <home>/adapters overlay preset.
    model_efforts: dict[str, list[str]] = field(default_factory=dict)
    blocklist_args: list[str] = field(default_factory=list)  # {tool}, repeated
    blocklist: list[str] = field(default_factory=list)     # default tool blocks
    reply_file_arg: list[str] = field(default_factory=list)  # {reply_file}
    # R18 broker plumbing — {mcp_config} rides argv when a bridge is active;
    # safety-class: applied in BOTH full and minimal argv, never dropped
    permission_args: list[str] = field(default_factory=list)
    auto_allow: list[str] = field(default_factory=list)    # read-class tools
    format: str = "text"              # claude-stream | codex-jsonl | text
    default_model: str = ""
    models: list[str] = field(default_factory=list)        # picker suggestions
    requires_model: bool = False      # e.g. `ollama run <model>` is mandatory
    verified: bool = False            # ran against the real CLI at least once

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        p = cls(**known)
        if not p.id or not p.command:
            raise ValidationError("a preset needs at least id and command")
        if p.format not in FORMATS:
            raise ValidationError(f"unknown preset format {p.format!r}")
        return p

    def efforts_for(self, model: str) -> list[str]:
        """The effort levels THIS model accepts (family list when the model
        has no entry of its own)."""
        return self.model_efforts.get(model or "", None) or self.efforts

    def build_argv(
        self,
        *,
        prompt: str,
        workdir: str,
        reply_file: str,
        model: str = "",
        effort: str = "",
        blocklist: list[str] | None = None,
        minimal: bool = False,
        mcp_config: str = "",
    ) -> list[str]:
        """The run's argv — a LIST, never a shell string (v1 quoted prompts
        into a shell; argv removes that whole class). The minimal variant
        drops conveniences only — safety args, the blocklist and the
        permission plumbing are kept."""
        fill = {"prompt": prompt, "workdir": workdir, "reply_file": reply_file,
                "mcp_config": mcp_config}
        base = self.args_minimal if (minimal and self.args_minimal) else self.args
        argv = [self.command]
        argv += [a.format(**fill) for a in base]
        argv += [a.format(**fill) for a in self.safety_args]
        if mcp_config and self.permission_args:
            argv += [a.format(**fill) for a in self.permission_args]
        if not minimal and reply_file and self.reply_file_arg:
            argv += [a.format(**fill) for a in self.reply_file_arg]
        if model and self.model_args:
            argv += [a.format(model=model) for a in self.model_args]
        if effort and self.effort_args and effort in self.efforts_for(model):
            argv += [a.format(effort=effort) for a in self.effort_args]
        for tool in blocklist if blocklist is not None else self.blocklist:
            argv += [a.format(tool=tool) for a in self.blocklist_args]
        return argv


@dataclass
class Invocation:
    preset: Preset
    model: str = ""
    effort: str = ""


class ModelRegistry:
    def __init__(self, presets: dict[str, Preset]) -> None:
        self.presets = presets
        self._which: dict[str, bool] = {}

    @classmethod
    def load(cls, home: Path | None = None) -> "ModelRegistry":
        """Shipped presets, overlaid by any in ``<home>/adapters/`` (an owner
        can adjust a family's flags or add one without touching code)."""
        presets: dict[str, Preset] = {}
        dirs = [PRESET_DIR, (home or DEFAULT_HOME) / "adapters"]
        for d in dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    p = Preset.from_dict(json.loads(f.read_text(encoding="utf-8")))
                    presets[p.id] = p
                except (OSError, ValueError, ValidationError):
                    continue  # one bad preset never blocks the rest
        return cls(presets)

    # ------------------------------------------------------------- probing
    def available(self, preset: Preset) -> bool:
        """Is this family runnable on THIS machine? Re-probed per process
        (installs change; a stale verdict shouldn't outlive them)."""
        cached = self._which.get(preset.id)
        if cached is not None:
            return cached
        cmd = preset.command
        ok = bool(shutil.which(cmd)) or Path(cmd).is_file()
        self._which[preset.id] = ok
        return ok

    def installed(self) -> list[Preset]:
        return [p for p in self.presets.values() if self.available(p)]

    # ----------------------------------------------------------- resolution
    def resolve(self, settings: HarnessSettings, category: str,
                chat_id: str = "") -> Invocation:
        """The owner's config + the audience (+ the chat) -> one concrete
        invocation. Raises ValidationError with a showable reason."""
        if not settings.route(category).enabled:
            raise ValidationError(f"replies to {category} are turned off")
        if settings.adapter == "none":
            # MCP-only (Q21): the runner stands down for these agents; this
            # guard catches a stale runner mid-transition
            raise ValidationError(
                "this agent is MCP-only — it runs no local CLI")
        if settings.adapter:
            preset = self.presets.get(settings.adapter)
            if preset is None:
                raise ValidationError(f"unknown adapter {settings.adapter!r}")
            if not self.available(preset):
                raise ValidationError(
                    f"{preset.label or preset.id} is not installed on this machine")
        else:
            installed = self.installed()
            if len(installed) == 1:
                preset = installed[0]  # single-install degrade: no picking
            elif not installed:
                raise ValidationError("no agent CLI is installed on this machine")
            else:
                raise ValidationError(
                    "several agent CLIs are installed — pick one in the "
                    "agent's settings")
        model = settings.model_for(category, chat_id) or preset.default_model
        if preset.requires_model and not model:
            raise ValidationError(
                f"{preset.label or preset.id} needs a model picked in the "
                f"agent's settings")
        effort = (settings.reasoning
                  if settings.reasoning in preset.efforts_for(model) else "")
        return Invocation(preset=preset, model=model, effort=effort)
