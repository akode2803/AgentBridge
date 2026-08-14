"""The permission broker (R18) — every tool use an inner CLI cannot decide
for itself lands here, and the OWNER decides.

Policy, in resolution order (first hit wins):
1. a tool whose path target resolves INSIDE the run's workspace is allowed —
   the workspace is the agent's own desk (D7 "workspace" level);
2. a target inside a DENY ROOT (the harness home, the mesh root) is refused
   outright, no ask: the keystore and the local cache hold plaintext keys
   and other members' chat bodies — reading them would break visibility =
   membership, and no owner click should be able to grant that;
3. any OTHER path target — outside the workspace, reads INCLUDED — goes
   straight to the ASK (rule 5) and is decided PER PATH, every time.
   Reading a member's personal files (their Downloads, documents, keys) IS
   the privacy breach, not mere "curiosity" (R67, V79: the live agent read
   a 44k-file Downloads tree and a personal PDF with no prompt). Neither
   ``auto_allow`` NOR a standing approval short-circuits an outside path
   (V83): a tool-wide "always allow Read in this chat" must never silently
   become "read ANY file on the host" — that WAS the residual hole (a
   sweep-era always-allow left @claude reading Downloads in a DM while it
   correctly asked in a fresh group). Blanket approvals are for no-path /
   in-workspace tools only;
4. ``auto_allow`` read-class / no-side-effect tools (Read/Glob/Grep scoped
   to the workspace cwd, TodoWrite) run without asking ONLY when they carry
   no path target outside the workspace — internal state and workspace reads,
   never a reach onto the host;
5. an owner-granted always-allow rule (``agent.harness["approvals"]``:
   ``[{tool, chat}]``, chat ``"*"`` = every chat) allows without asking —
   but, like auto_allow, ONLY for a call with no outside-workspace path;
6. everything else becomes an ASK: an owner-visible doc the GUI surfaces as
   a popup (approve / always-allow / deny). No answer inside the timeout
   means **deny** — unattended agents never get the benefit of the doubt.

Production asks and decisions ride the C1.1 secure permission lane: immutable
chat-scoped documents, pairwise encryption to the agent and responsible
member, signatures, current authority epochs, full intent digests, absolute
expiry, and a durable one-use claim. There is no production downgrade to the
old plaintext ``status/asks`` lane. A private legacy lane remains only for the
pre-C1 unit fixtures that isolate policy resolution from transport security.

A denied intent is cached per run: inner CLIs retry a denied tool call
(seen live in the R18 spike — three asks for one Write), and the owner
answers once, not once per retry. Questions (``ask_member``) share the same
pipe with ``kind="question"`` and a free-text answer.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from ..core.errors import ValidationError
from ..core.timekit import new_id, utcnow_iso
from .runtime.authority import (
    AuthorityError, capability_call_digest, validate_run_authority,
)

__all__ = ["PermissionBroker", "Ask", "path_of"]

ASK_DOC = "status/asks/{agent}.json"
ANSWER_DOC = "status/asks/{agent}_answers.json"
PATH_KEYS = ("file_path", "path", "notebook_path")
MAX_DETAIL = 200
POLL_S = 1.0


def path_of(tool_input: dict, keys=PATH_KEYS, *, required: bool = False) -> str:
    """The filesystem target of a tool call, if it names one."""
    values = [str((tool_input or {})[key]) for key in keys
              if (tool_input or {}).get(key)]
    distinct = tuple(dict.fromkeys(values))
    if len(distinct) > 1:
        raise ValidationError("conflicting provider-native path fields")
    if not distinct:
        if required:
            raise ValidationError("provider-native file target is missing")
        return ""
    return distinct[0]


def _inside(target: str, workspace: Path) -> bool:
    """Is ``target`` inside the workspace? Relative paths are the run's cwd
    (= the workspace). Compares normalized spellings, never Path equality
    (the v0.24.79 extended-length lesson)."""
    try:
        t = Path(target)
        if not t.is_absolute():
            t = workspace / t
        t = t.resolve()
        w = workspace.resolve()
        ts = str(t).replace("\\", "/").lstrip("/?").lower()
        ws = str(w).replace("\\", "/").lstrip("/?").lower()
        return ts == ws or ts.startswith(ws + "/")
    except (OSError, ValueError):
        return False


class Ask:
    def __init__(self, agent: str, chat_id: str, kind: str, tool: str,
                 detail: str, input_hash: str, timeout_s: float,
                 label: str = "", options: list | None = None,
                 scope: str = "") -> None:
        self.id = new_id("ask")
        self.agent = agent
        self.chat_id = chat_id
        self.kind = kind          # "permission" | "question"
        self.tool = tool
        self.detail = detail
        self.label = label        # friendly verb phrase ("write a file", R43)
        self.options = options or []  # a question's offered choices (R43/Q28)
        self.scope = scope        # "outside" = per-path ask; no standing grant
        self.input_hash = input_hash
        self.created = utcnow_iso()
        self.expires_at = time.time() + timeout_s
        # V85/V109: a run tearing down withdraws its asks — the blocked
        # ask() loops see the flag and return instead of waiting out the
        # timeout, and the doc stops advertising a prompt nobody can answer
        self.withdrawn = False

    def to_doc(self) -> dict:
        doc = {"id": self.id, "chat_id": self.chat_id, "kind": self.kind,
               "tool": self.tool, "detail": self.detail,
               "created": self.created,
               "expires_in_s": max(0, int(self.expires_at - time.time()))}
        if self.label:
            doc["label"] = self.label
        if self.options:
            doc["options"] = self.options
        if self.scope:
            doc["scope"] = self.scope  # the GUI hides "Always allow" for it
        return doc


class PermissionBroker:
    """One broker per runner; runs bind it to their (chat, workspace)."""

    def __init__(self, mesh_or_tx, agent: str, docs=None,
                 *, _legacy_test_lane: bool = False) -> None:
        self.mesh = mesh_or_tx if hasattr(mesh_or_tx, "directory") else None
        if self.mesh is None and not _legacy_test_lane:
            raise TypeError("PermissionBroker requires a Mesh secure lane")
        self.tx = self.mesh.tx if self.mesh is not None else mesh_or_tx
        self.agent = agent
        self.docs = docs   # ToolDocs (R43): friendly popup phrases; optional
        self._lock = threading.Lock()
        self._pending: dict[str, Ask] = {}
        self._denied: dict[str, str] = {}   # input_hash -> deny message (per process)
        # V85: an "always allow" takes effect NOW, not next run — the owner
        # said always and the very next call re-asking read as broken. The
        # persisted rule (agent.harness["approvals"]) covers future
        # processes; this covers the rest of this one. Same constraint as
        # approvals: never consulted for an outside-workspace path (V83).
        self._grants: set[tuple[str, str]] = set()   # (tool, chat_id)
        self._lane = None
        self._secure_pending = {}
        self._withdrawn: set[str] = set()
        if self.mesh is not None:
            from .runtime.permissions import PermissionLane
            self._lane = PermissionLane(self.mesh, self.agent)

    # -------------------------------------------------------------- policy
    def decide(self, *, chat_id: str, workspace: Path, tool: str,
               tool_input: dict, auto_allow: tuple[str, ...] | list[str],
               approvals: list[dict], timeout_s: float,
               deny_roots: list[Path] | None = None, run_id: str = "",
               call_id: str = "", native_policy=None,
               run_record=None) -> tuple[bool, str]:
        """Returns ``(allowed, message)``; blocks while the owner decides."""
        if native_policy is not None:
            try:
                capability_id = native_policy.capability_for_tool(tool)
            except (KeyError, ValidationError):
                return False, "unknown provider-native tool — denied"
            if capability_id in native_policy.blocked:
                return False, "this provider-native capability is blocked"
            if self.mesh is None:
                return False, "current provider-native authority is unavailable"
            try:
                validate_run_authority(
                    self.mesh, run_record, agent=self.agent, chat_id=chat_id,
                    run_id=run_id, provider=native_policy.provider,
                    native_policy=native_policy,
                )
                digest = capability_call_digest(
                    native_policy.provider, tool, tool_input)
            except (AuthorityError, TypeError, ValueError):
                return False, "current provider-native authority is unavailable"
            auto_allow = native_policy.auto_allow_tools
            try:
                target = path_of(
                    tool_input,
                    native_policy.path_keys_for_tool(tool) or PATH_KEYS,
                    required=native_policy.path_required_for_tool(tool),
                )
            except ValidationError:
                return False, "provider-native path input is invalid"
        else:
            digest = hashlib.sha256(
                f"{chat_id}|{tool}|{json.dumps(tool_input, sort_keys=True, default=str)}"
                .encode()).hexdigest()
            target = path_of(tool_input)
        outside = False
        if target:
            if _inside(target, workspace):
                if (native_policy is None
                        or capability_id in native_policy.enabled):
                    return True, ""
            if any(_inside(target, root) for root in deny_roots or []):
                return False, ("that path is the platform's own storage "
                               "(keys, caches, the shared mesh) — off limits")
            # a real filesystem path OUTSIDE the workspace: it must be gated,
            # reads included (V79). Neither auto_allow NOR a standing approval
            # short-circuits an outside path (V83): a tool-wide "always allow
            # Read in this chat" must never silently become "read ANY file on
            # the host" — the live @claude hole. Every outside access is a
            # fresh, per-path owner decision (approve / deny), never blanket.
            outside = True
        if not outside:
            if native_policy is not None:
                if capability_id in native_policy.enabled:
                    return True, ""
                if capability_id not in native_policy.approval_gated:
                    return False, "provider-native capability has no valid state"
            if tool in (auto_allow or ()):
                return True, ""
            with self._lock:
                granted = (tool, chat_id) in self._grants \
                    or (tool, "*") in self._grants
            if granted:
                return True, ""
            for rule in approvals or []:
                if rule.get("tool") == tool and \
                        rule.get("chat") in ("*", chat_id):
                    return True, ""
        with self._lock:
            if digest in self._denied:   # a retry of a denied intent
                return False, self._denied[digest]
        # the popup's detail line: the path for path tools; a config-phrased
        # summary for known non-path tools (V86 — "background work · up to
        # 5s" beats the raw Monitor JSON); the honest input JSON for the
        # rest; nothing at all for an input-less call (was a bare "{}")
        friendly = self.docs.detail_phrase(tool, tool_input) if self.docs \
            else ""
        detail = target or friendly or (" ".join(json.dumps(
            tool_input, default=str).split())[:MAX_DETAIL]
            if tool_input else "")
        verdict, note = self.ask(chat_id=chat_id, kind="permission",
                                 tool=tool, detail=detail[:MAX_DETAIL],
                                 input_hash=digest, timeout_s=timeout_s,
                                 scope="outside" if outside else "",
                                 run_id=run_id, call_id=call_id)
        if verdict == "always" and not outside:
            with self._lock:
                self._grants.add((tool, chat_id))
        if verdict in ("allow", "always"):
            return True, ""
        with self._lock:
            self._denied[digest] = note
        return False, note

    # ----------------------------------------------------------- the pipe
    def ask(self, *, chat_id: str, kind: str, tool: str, detail: str,
            input_hash: str = "", timeout_s: float = 120.0,
            options: list | None = None, scope: str = "", run_id: str = "",
            call_id: str = "") -> tuple[str, str]:
        """Publish one ask and wait for the owner. Returns
        ``(verdict, text)`` — verdict allow|always|deny|timeout for
        permissions, answer|timeout for questions (text = the reply/reason).
        ``options``: a question's offered choices — the popup renders them
        as one-tap buttons with free text as the escape (R43/Q28).
        """
        label = (self.docs.ask_phrase(tool)
                 if self.docs is not None and kind == "permission" else "")
        if self._lane is not None:
            from .runtime.permissions import digest
            full_digest = input_hash or digest({
                "chat_id": chat_id, "kind": kind, "tool": tool,
                "detail": detail, "options": options or [], "scope": scope,
            })
            try:
                secure = self._lane.publish_ask(
                    chat_id=chat_id, kind=kind, tool=tool, detail=detail,
                    input_digest=full_digest, timeout_s=timeout_s,
                    run_id=run_id, call_id=call_id, label=label,
                    options=options, scope=scope,
                )
            except Exception as exc:  # fail closed; never downgrade to plaintext
                return "timeout", f"secure owner approval unavailable: {exc}"
            with self._lock:
                self._secure_pending[secure.id] = secure
            try:
                deadline = secure.record["expires_ns"]
                while time.time_ns() < deadline:
                    with self._lock:
                        if secure.id in self._withdrawn:
                            return "timeout", "the run ended before an answer arrived"
                    ans = self._lane.read_decision(secure)
                    if ans is not None:
                        verdict = str(ans.get("verdict") or "deny")
                        text = str(ans.get("text") or "")
                        if kind == "question":
                            return ("answer" if verdict == "answer" else "timeout", text)
                        if verdict not in ("allow", "always", "deny"):
                            verdict = "deny"
                        return verdict, text or (
                            "the responsible member denied this" if verdict == "deny" else "")
                    time.sleep(POLL_S)
                return "timeout", ("no answer from the responsible member "
                                   f"within {int(timeout_s)}s — denied")
            finally:
                with self._lock:
                    self._secure_pending.pop(secure.id, None)
                    self._withdrawn.discard(secure.id)
                self._lane.withdraw(secure)

        a = Ask(self.agent, chat_id, kind, tool, detail, input_hash,
                timeout_s, label=label, options=options, scope=scope)
        with self._lock:
            self._pending[a.id] = a
            self._publish()
        try:
            deadline = a.expires_at
            while time.time() < deadline:
                if a.withdrawn:   # the run is gone — nobody needs this answer
                    return "timeout", ("the run ended before an answer "
                                       "arrived")
                ans = self._answer_for(a.id)
                if ans is not None:
                    verdict = str(ans.get("verdict") or "deny")
                    text = str(ans.get("text") or "")
                    if kind == "question":
                        return ("answer" if verdict != "timeout" else "timeout",
                                text)
                    if verdict not in ("allow", "always", "deny"):
                        verdict = "deny"
                    return verdict, text or (
                        "the responsible member denied this"
                        if verdict == "deny" else "")
                time.sleep(POLL_S)
            return "timeout", ("no answer from the responsible member "
                               f"within {int(timeout_s)}s — denied")
        finally:
            with self._lock:
                self._pending.pop(a.id, None)
                self._publish()

    def pending(self) -> list[dict]:
        with self._lock:
            if self._lane is not None:
                return [a.record.copy() for a in self._secure_pending.values()]
            return [a.to_doc() for a in self._pending.values()
                    if not a.withdrawn]

    def withdraw(self, chat_id: str) -> int:
        """V85/V109: a run tearing down takes its asks with it — the doc
        stops advertising them NOW (the GUI popup dies with the run, not
        two minutes later) and every blocked ``ask()`` returns on its next
        poll tick. Chat-scoped: parallel runs in other chats keep theirs."""
        with self._lock:
            secure = [a for a in self._secure_pending.values()
                      if a.record["chat_id"] == chat_id]
            for a in secure:
                self._withdrawn.add(a.id)
                self._lane.withdraw(a)
            if secure:
                return len(secure)
            hit = [a for a in self._pending.values()
                   if a.chat_id == chat_id and not a.withdrawn]
            for a in hit:
                a.withdrawn = True
            if hit:
                self._publish()
        return len(hit)

    @classmethod
    def clear_stale(cls, mesh_or_tx, agent: str) -> None:
        """Boot hygiene (V85: 'persists after a fleet restart'): a process
        that died mid-ask left its asks doc advertising prompts no one can
        answer. A starting runner has no pending asks by definition — reset
        the doc to empty. Best-effort."""
        if hasattr(mesh_or_tx, "directory"):
            return  # secure records expire and are validated individually
        tx = mesh_or_tx
        try:
            tx.put_doc(ASK_DOC.format(agent=agent), {
                "agent": agent, "updated": utcnow_iso(), "asks": [],
            })
        except Exception:  # noqa: BLE001 — hygiene never blocks a boot
            pass

    # ------------------------------------------------------------- plumbing
    def _publish(self) -> None:
        """The asks doc (harness = its only writer). Best-effort."""
        if self._lane is not None:
            return
        try:
            self.tx.put_doc(ASK_DOC.format(agent=self.agent), {
                "agent": self.agent, "updated": utcnow_iso(),
                "asks": [a.to_doc() for a in self._pending.values()
                         if not a.withdrawn],
            })
        except Exception:  # noqa: BLE001 — a status write never breaks a run
            pass

    def _answer_for(self, ask_id: str) -> dict | None:
        try:
            doc = self.tx.get_doc(ANSWER_DOC.format(agent=self.agent))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(doc, dict):
            return None
        ans = (doc.get("answers") or {}).get(ask_id)
        return ans if isinstance(ans, dict) else None
