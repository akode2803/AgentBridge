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
from .native import (
    NATIVE_CAPABILITIES, NATIVE_DENY_ARG_TEMPLATES, EffectiveNativePolicy,
    NativeProfile,
)
from .policy import BridgeProfile

__all__ = [
    "Preset", "Invocation", "ModelRegistry", "effective_gates",
    "effective_native_policy",
]

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
    # H2/R43: the blocklist entries the owner's "web access" toggle governs —
    # flipped on, they leave the blocklist and route through the ask gate
    # instead. A family without this key simply has no web toggle.
    aux_web: list[str] = field(default_factory=list)
    reply_file_arg: list[str] = field(default_factory=list)  # {reply_file}
    # Package-trusted only. Owner overlays may configure a CLI, but cannot
    # self-certify bridge authority.
    bridge_profile: BridgeProfile | None = None
    bridge_unavailable_reason: str = ""
    # Package-trusted classification of provider-native tools. Raw legacy
    # strings remain readable for owner overlays, but are never reported as a
    # canonical inventory.
    native_profile: NativeProfile | None = None
    native_unavailable_reason: str = ""
    auto_allow: list[str] = field(default_factory=list)    # read-class tools
    # Explicit host variables this provider process may inherit. The adapter
    # supplies a small cross-platform process baseline separately; credentials
    # and provider endpoints must be named here instead of inheriting the host.
    env_allow: list[str] = field(default_factory=list)
    format: str = "text"              # claude-stream | codex-jsonl | text
    default_model: str = ""
    models: list[str] = field(default_factory=list)        # picker suggestions
    requires_model: bool = False      # e.g. `ollama run <model>` is mandatory
    verified: bool = False            # ran against the real CLI at least once
    # V157: explicit opt-in for the isolated specialist sidecar. False is the
    # security default; this declaration is accepted only for structurally
    # plain-text presets with no bridge/tool or reply-file plumbing.
    child_text_only: bool = False

    @classmethod
    def from_dict(cls, d: dict, *, trusted: bool = False) -> "Preset":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d
                 and f not in ("bridge_profile", "native_profile")}
        raw_bridge = d.get("bridge_profile")
        if trusted and raw_bridge is not None:
            known["bridge_profile"] = BridgeProfile.from_dict(raw_bridge)
        elif raw_bridge is not None:
            known["bridge_unavailable_reason"] = (
                "owner adapter declarations cannot attach the trusted bridge")
        raw_native = d.get("native_profile")
        if trusted and raw_native is not None:
            if any(name in d for name in ("auto_allow", "blocklist", "aux_web")):
                raise ValidationError(
                    "canonical native profiles cannot mix raw tool lists")
            provider = str(d.get("id") or "")
            native = NativeProfile.from_dict(
                raw_native, expected_provider=provider)
            blocklist_args = d.get("blocklist_args")
            expected_deny = NATIVE_DENY_ARG_TEMPLATES.get(provider)
            if (not isinstance(blocklist_args, list)
                    or tuple(blocklist_args) != expected_deny):
                raise ValidationError(
                    "native deny flags do not match the reviewed provider template")
            compiled = native.compile(
                allow_read=True, allow_web=False, permission_callback=False)
            known["native_profile"] = native
            known["auto_allow"] = list(
                native.compile(
                    allow_read=True, allow_web=False,
                    permission_callback=True,
                ).auto_allow_tools)
            known["blocklist"] = list(compiled.blocked_tools)
            known["aux_web"] = [
                tool for capability_id in native.aux_web
                for tool in NATIVE_CAPABILITIES[capability_id].tools
            ]
        elif raw_native is not None:
            known["native_unavailable_reason"] = (
                "owner adapter declarations cannot certify native authority")
        # JSON booleans only. In particular, the string "true" must not turn
        # an owner overlay into an executable child preset.
        known["child_text_only"] = d.get("child_text_only") is True
        p = cls(**known)
        if not p.id or not p.command:
            raise ValidationError("a preset needs at least id and command")
        if p.format not in FORMATS:
            raise ValidationError(f"unknown preset format {p.format!r}")
        if p.child_text_only and (raw_bridge is not None
                                  or raw_native is not None
                                  or d.get("permission_args") is not None
                                  or not p.is_child_text_only_safe()):
            raise ValidationError(
                f"preset {p.id!r} declares child_text_only but exposes "
                "non-text invocation features")
        return p

    def is_child_text_only_safe(self) -> bool:
        """Whether this data declaration describes the bounded child ABI.

        The declaration remains the source of truth, while these structural
        checks stop contradictory overlay data from weakening it.
        """
        reserved = ("{mcp_config}", "{reply_file}")
        argv_templates = (*self.args, *self.args_minimal)
        return bool(
            self.child_text_only is True
            and self.format == "text"
            and self.bridge_profile is None
            and self.native_profile is None
            and not self.auto_allow
            and not self.aux_web
            and not self.blocklist_args
            and not self.blocklist
            and not self.reply_file_arg
            and not any(token in arg for arg in argv_templates
                        for token in reserved)
        )

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
        bridge_args: tuple[str, ...] | list[str] = (),
        command: str = "",
        include_safety: bool = True,
    ) -> list[str]:
        """The run's argv — a LIST, never a shell string (v1 quoted prompts
        into a shell; argv removes that whole class). The minimal variant
        drops conveniences only — safety args, the blocklist and the
        compiled bridge policy are kept."""
        fill = {"prompt": prompt, "workdir": workdir, "reply_file": reply_file,
                "model": model, "effort": effort}
        base = self.args_minimal if (minimal and self.args_minimal) else self.args
        argv = [command or self.command]
        argv += [a.format(**fill) for a in base]
        if include_safety:
            argv += [a.format(**fill) for a in self.safety_args]
        argv += list(bridge_args)
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


def effective_native_policy(
    preset: Preset,
    settings: HarnessSettings,
    *,
    permission_callback: bool,
) -> EffectiveNativePolicy | None:
    """Compile package-declared native authority for one invocation.

    ``None`` is an honest legacy/unclassified preset, not an empty tool set.
    """
    if preset.native_profile is None:
        return None
    return preset.native_profile.compile(
        allow_read=settings.aux.get("read", True),
        allow_web=settings.aux.get("web", False),
        permission_callback=permission_callback,
    )


def effective_gates(preset: Preset, settings: HarnessSettings,
                    *, permission_callback: bool = False) \
        -> tuple[list[str], list[str]]:
    """The run's (auto_allow, blocklist) after the owner's aux flags (H2/R43).
    ``read`` off empties auto_allow — even reads outside the workspace ask.
    ``web`` on releases the preset's aux_web tools from the blocklist INTO
    the ask gate — and only for presets that HAVE a trusted bridge profile;
    a family without the ask plumbing keeps its full blocklist regardless,
    so the toggle can never trade a hard block for nothing."""
    native = effective_native_policy(
        preset, settings, permission_callback=permission_callback)
    if native is not None:
        return list(native.auto_allow_tools), list(native.blocked_tools)
    auto = list(preset.auto_allow) if settings.aux.get("read", True) else []
    block = list(preset.blocklist)
    if settings.aux.get("web") and preset.bridge_profile and preset.aux_web:
        block = [t for t in block if t not in preset.aux_web]
    return auto, block


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
            shipped = d.resolve() == PRESET_DIR.resolve()
            for f in sorted(d.glob("*.json")):
                try:
                    p = Preset.from_dict(
                        json.loads(f.read_text(encoding="utf-8")),
                        trusted=shipped,
                    )
                    # Owner overlays may configure ordinary providers, but
                    # cannot self-certify an arbitrary host command as the
                    # zero-capability child ABI. That trust bit ships only
                    # with reviewed package presets.
                    if not shipped:
                        p.child_text_only = False
                        existing = presets.get(p.id)
                        if existing is not None and (
                                existing.bridge_profile is not None
                                or existing.native_profile is not None):
                            # A same-id overlay can otherwise replace a reviewed
                            # command/profile with ordinary untrusted CLI data.
                            # Keep the package preset; custom providers use a
                            # distinct id and remain honestly unclassified.
                            continue
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
        return [p for p in self.presets.values() if self.runnable(p)]

    def runnable(self, preset: Preset) -> bool:
        """Whether selection may lead to a provider invocation right now."""
        return bool(
            self.available(preset)
            and (preset.native_profile is None
                 or preset.native_profile.inventory_complete)
        )

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
        if (preset.native_profile is not None
                and not preset.native_profile.inventory_complete):
            raise ValidationError(
                f"{preset.label or preset.id} is quarantined: its native tool "
                "inventory is not version-bound and exhaustive")
        model = settings.model_for(category, chat_id) or preset.default_model
        if preset.requires_model and not model:
            raise ValidationError(
                f"{preset.label or preset.id} needs a model picked in the "
                f"agent's settings")
        if preset.bridge_profile is not None and not model:
            raise ValidationError(
                f"{preset.label or preset.id} exact profile has no reviewed "
                "default model")
        if preset.bridge_profile is not None and model and model not in preset.models:
            raise ValidationError(
                f"{preset.label or preset.id} does not support configured model "
                f"{model!r} in its exact reviewed profile — pick a current model "
                "in the agent's settings")
        if (preset.bridge_profile is not None and settings.reasoning
                and settings.reasoning not in preset.efforts_for(model)):
            raise ValidationError(
                f"{preset.label or preset.id} does not support configured reasoning "
                f"level {settings.reasoning!r} for {model or 'the default model'}")
        effort = (settings.reasoning
                  if settings.reasoning in preset.efforts_for(model) else "")
        return Invocation(preset=preset, model=model, effort=effort)
