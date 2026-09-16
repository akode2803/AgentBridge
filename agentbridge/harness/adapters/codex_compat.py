"""Bounded, package-owned Codex CLI version compatibility policy."""

from __future__ import annotations

import re

from ...core.errors import ValidationError

__all__ = [
    "codex_version_requirement", "codex_version_supported",
    "parse_codex_version", "validate_codex_version_series",
]

_VERSION = re.compile(r"codex-cli ((?:0|[1-9][0-9]{0,5}))\.((?:0|[1-9][0-9]{0,5}))\.((?:0|[1-9][0-9]{0,5}))")
_SERIES = re.compile(r"((?:0|[1-9][0-9]{0,5}))\.((?:0|[1-9][0-9]{0,5}))")


def parse_codex_version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def validate_codex_version_series(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError("Codex supported_versions must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or _SERIES.fullmatch(item) is None:
            raise ValidationError("invalid Codex supported version series")
        if item in result:
            raise ValidationError("duplicate Codex supported version series")
        result.append(item)
    return tuple(result)


def codex_version_supported(version: str, series: tuple[str, ...]) -> bool:
    parsed = parse_codex_version(version)
    return parsed is not None and f"{parsed[0]}.{parsed[1]}" in series


def codex_version_requirement(series: tuple[str, ...]) -> str:
    return ", ".join(f"codex-cli {item}.x" for item in series)


class BridgeCompatibilityError(ValidationError):
    """Deterministic, safe public preparation refusal; never echoes raw output."""

    def __init__(self, version: object, series: tuple[str, ...]) -> None:
        parsed = parse_codex_version(version)
        detected = ("codex-cli " + ".".join(map(str, parsed))
                    if parsed is not None else "unrecognized version output")
        reviewed = validate_codex_version_series(list(series))
        super().__init__(
            f"Unsupported Codex CLI: {detected}. Supported: "
            f"{codex_version_requirement(reviewed)}. Update AgentBridge or review "
            "adapters/presets/codex.json compatibility settings.")
