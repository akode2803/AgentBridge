"""Owner-set harness settings — parsed fresh from the agent's account each
scan, so a change in Settings → My agents applies without a restart (the v1
``rate_ok`` lesson). The store is the free-form ``agent.harness`` dict
(``accounts.set_agent_harness``); R16 formalizes the adapter/model half of
the schema — this module owns the runner half.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import Account

__all__ = ["HarnessSettings", "Route", "RULES", "CATCHUP_POLICIES",
           "CATEGORIES"]

RULES = ("all", "tagged", "humans")
CATCHUP_POLICIES = ("recent", "none", "all")
# per-purpose routing (R16): who the agent is replying TO decides the model
# and whether that audience is served at all. Timer runs bill as "owner"
# (self-scheduled work serves the responsible member).
CATEGORIES = ("owner", "humans", "agents")

_MODEL_RE_MAX = 64  # model ids ride argv; keep them short and sane


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _model(value) -> str:
    s = str(value or "").strip()
    return s[:_MODEL_RE_MAX]


@dataclass
class Route:
    enabled: bool = True
    model: str = ""

    @classmethod
    def from_dict(cls, d) -> "Route":
        d = d if isinstance(d, dict) else {}
        return cls(enabled=bool(d.get("enabled", True)),
                   model=_model(d.get("model")))


@dataclass
class HarnessSettings:
    default_rule: str = "tagged"
    rules: dict[str, str] = field(default_factory=dict)   # chat_id -> rule
    models: dict[str, str] = field(default_factory=dict)  # chat_id -> model
    concurrency: int = 2            # parallel runs (across AND within chats)
    max_replies_per_hour: int = 30  # per chat — the runaway-conversation brake
    catchup: str = "recent"         # after downtime: recent | none | all
    catchup_window_h: float = 48.0  # "recent" = triggers younger than this
    error_notices: bool = True      # post a short notice when a run fails
    timeout_s: float = 3300.0       # per-run budget (adapters enforce)
    ask_timeout_s: float = 120.0    # owner-answer window; silence = deny
    # owner-granted standing permissions: [{tool, chat}] (chat "*" = all)
    approvals: list[dict] = field(default_factory=list)
    # cross-chat memory policy (R20): where may the agent touch its GLOBAL
    # memory — "dm" (default: only one-on-one with a member), "everywhere",
    # or "off" (chat-scoped memory only)
    global_memory: str = "dm"
    # peer harness access (R22): "off" (default: unreachable) or "ask" (each
    # peer session surfaces an owner popup); peer_auto = agents pre-approved
    # (DIAGNOSTICS only). peer_repair (R22.5) is a SEPARATE, stricter gate:
    # repair mutations are refused unless it is on, and ALWAYS pop up per
    # session — a diagnostics auto-grant never covers a mutation.
    peer_access: str = "off"
    peer_auto: list[str] = field(default_factory=list)
    peer_repair: bool = False
    # ----- model selection (R16): the owner's picker writes these
    adapter: str = ""               # preset family id; "" = the sole install
    model: str = ""                 # override-all "current model"
    reasoning: str = ""             # effort knob, where the family supports it
    routing: dict[str, Route] = field(default_factory=dict)

    @classmethod
    def from_account(cls, acc: Account | None) -> "HarnessSettings":
        h = dict(acc.agent.harness) if (acc and acc.agent) else {}
        rule = str(h.get("default_rule") or "tagged").lower()
        rules = {
            str(k): str(v).lower()
            for k, v in (h.get("rules") or {}).items()
            if str(v).lower() in RULES
        }
        models = {
            str(k): _model(v)
            for k, v in (h.get("models") or {}).items()
            if _model(v)
        }
        catchup = str(h.get("catchup") or "recent").lower()
        routing = {
            cat: Route.from_dict((h.get("routing") or {}).get(cat))
            for cat in CATEGORIES
        }
        return cls(
            default_rule=rule if rule in RULES else "tagged",
            rules=rules,
            models=models,
            concurrency=_int(h.get("concurrency"), 2, 1, 8),
            max_replies_per_hour=_int(h.get("max_replies_per_hour"), 30, 1, 1000),
            catchup=catchup if catchup in CATCHUP_POLICIES else "recent",
            catchup_window_h=float(_int(h.get("catchup_window_h"), 48, 1, 24 * 30)),
            error_notices=bool(h.get("error_notices", True)),
            timeout_s=float(_int(h.get("timeout_s"), 3300, 30, 6 * 3600)),
            ask_timeout_s=float(_int(h.get("ask_timeout_s"), 120, 15, 900)),
            approvals=[
                {"tool": str(r.get("tool") or ""),
                 "chat": str(r.get("chat") or "*")}
                for r in (h.get("approvals") or [])
                if isinstance(r, dict) and r.get("tool")
            ],
            global_memory=(str(h.get("global_memory") or "dm").lower()
                           if str(h.get("global_memory") or "dm").lower()
                           in ("dm", "everywhere", "off") else "dm"),
            peer_access=("ask" if str(h.get("peer_access") or "off").lower()
                         == "ask" else "off"),
            peer_auto=[str(n) for n in (h.get("peer_auto") or []) if n],
            peer_repair=bool(h.get("peer_repair", False)),
            adapter=str(h.get("adapter") or "").strip().lower(),
            model=_model(h.get("model")),
            reasoning=str(h.get("reasoning") or "").strip().lower(),
            routing=routing,
        )

    def rule_for(self, chat_id: str, *, dm: bool = False) -> str:
        """The reply rule in ONE chat: an explicit per-chat rule wins; a DM
        defaults to answering every message (talking to an agent one-on-one
        IS addressing it — v1 semantics, and what the GUI advertises)."""
        explicit = self.rules.get(chat_id)
        if explicit:
            return explicit
        return "all" if dm else self.default_rule

    def route(self, category: str) -> Route:
        return self.routing.get(category) or Route()

    def model_for(self, category: str, chat_id: str = "") -> str:
        """Most specific wins: this chat's model → the override-all "current
        model" → the audience route's model — else empty, and the registry
        falls back to the preset default."""
        return (self.models.get(chat_id, "") or self.model
                or self.route(category).model)

    @staticmethod
    def category(sender_kind: str, sender: str, owner: str | None) -> str:
        if sender and owner and sender == owner:
            return "owner"
        return "humans" if sender_kind == "human" else "agents"
