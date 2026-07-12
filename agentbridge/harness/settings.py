"""Owner-set harness settings — parsed fresh from the agent's account each
scan, so a change in Settings → My agents applies without a restart (the v1
``rate_ok`` lesson). The store is the free-form ``agent.harness`` dict
(``accounts.set_agent_harness``); R16 formalizes the adapter/model half of
the schema — this module owns the runner half.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import Account

__all__ = ["HarnessSettings", "RULES", "CATCHUP_POLICIES"]

RULES = ("all", "tagged", "humans")
CATCHUP_POLICIES = ("recent", "none", "all")


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class HarnessSettings:
    default_rule: str = "tagged"
    rules: dict[str, str] = field(default_factory=dict)  # chat_id -> rule
    concurrency: int = 2            # parallel runs (across AND within chats)
    max_replies_per_hour: int = 30  # per chat — the runaway-conversation brake
    catchup: str = "recent"         # after downtime: recent | none | all
    catchup_window_h: float = 48.0  # "recent" = triggers younger than this
    error_notices: bool = True      # post a short notice when a run fails
    timeout_s: float = 3300.0       # per-run budget (adapters enforce, R16)

    @classmethod
    def from_account(cls, acc: Account | None) -> "HarnessSettings":
        h = dict(acc.agent.harness) if (acc and acc.agent) else {}
        rule = str(h.get("default_rule") or "tagged").lower()
        rules = {
            str(k): str(v).lower()
            for k, v in (h.get("rules") or {}).items()
            if str(v).lower() in RULES
        }
        catchup = str(h.get("catchup") or "recent").lower()
        return cls(
            default_rule=rule if rule in RULES else "tagged",
            rules=rules,
            concurrency=_int(h.get("concurrency"), 2, 1, 8),
            max_replies_per_hour=_int(h.get("max_replies_per_hour"), 30, 1, 1000),
            catchup=catchup if catchup in CATCHUP_POLICIES else "recent",
            catchup_window_h=float(_int(h.get("catchup_window_h"), 48, 1, 24 * 30)),
            error_notices=bool(h.get("error_notices", True)),
            timeout_s=float(_int(h.get("timeout_s"), 3300, 30, 6 * 3600)),
        )

    def rule_for(self, chat_id: str) -> str:
        return self.rules.get(chat_id, self.default_rule)
