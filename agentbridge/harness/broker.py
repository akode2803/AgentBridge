"""The permission broker (R18) — every tool use an inner CLI cannot decide
for itself lands here, and the OWNER decides.

Policy, in resolution order (first hit wins):
1. a tool whose path target resolves INSIDE the run's workspace is allowed —
   the workspace is the agent's own desk (D7 "workspace" level);
2. a target inside a DENY ROOT (the harness home, the mesh root) is refused
   outright, no ask: the keystore and the local cache hold plaintext keys
   and other members' chat bodies — reading them would break visibility =
   membership, and no owner click should be able to grant that;
3. read-class tools the preset marks ``auto_allow`` run anywhere else — the
   workspace sandbox is about WRITES and side effects, not curiosity;
4. an owner-granted always-allow rule (``agent.harness["approvals"]``:
   ``[{tool, chat}]``, chat ``"*"`` = every chat) allows without asking;
5. everything else becomes an ASK: an owner-visible doc the GUI surfaces as
   a popup (approve / always-allow / deny). No answer inside the timeout
   means **deny** — unattended agents never get the benefit of the doubt.

Asks and answers ride two transport docs with ONE writer each (the v1
overlay lesson — merge, never fight over a doc):
- ``status/asks/<agent>.json``      — pending asks; written by the harness;
- ``status/asks/<agent>_answers.json`` — verdicts; written by the owner's GUI.

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

from ..core.timekit import new_id, utcnow_iso

__all__ = ["PermissionBroker", "Ask", "path_of"]

ASK_DOC = "status/asks/{agent}.json"
ANSWER_DOC = "status/asks/{agent}_answers.json"
PATH_KEYS = ("file_path", "path", "notebook_path")
MAX_DETAIL = 200
POLL_S = 1.0


def path_of(tool_input: dict) -> str:
    """The filesystem target of a tool call, if it names one."""
    for k in PATH_KEYS:
        v = (tool_input or {}).get(k)
        if v:
            return str(v)
    return ""


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
                 label: str = "", options: list[str] | None = None) -> None:
        self.id = new_id("ask")
        self.agent = agent
        self.chat_id = chat_id
        self.kind = kind          # "permission" | "question"
        self.tool = tool
        self.detail = detail
        self.label = label        # friendly verb phrase ("write a file", R43)
        self.options = options or []  # a question's offered choices (R43/Q28)
        self.input_hash = input_hash
        self.created = utcnow_iso()
        self.expires_at = time.time() + timeout_s

    def to_doc(self) -> dict:
        doc = {"id": self.id, "chat_id": self.chat_id, "kind": self.kind,
               "tool": self.tool, "detail": self.detail,
               "created": self.created,
               "expires_in_s": max(0, int(self.expires_at - time.time()))}
        if self.label:
            doc["label"] = self.label
        if self.options:
            doc["options"] = self.options
        return doc


class PermissionBroker:
    """One broker per runner; runs bind it to their (chat, workspace)."""

    def __init__(self, tx, agent: str, docs=None) -> None:
        self.tx = tx
        self.agent = agent
        self.docs = docs   # ToolDocs (R43): friendly popup phrases; optional
        self._lock = threading.Lock()
        self._pending: dict[str, Ask] = {}
        self._denied: dict[str, str] = {}   # input_hash -> deny message (per process)

    # -------------------------------------------------------------- policy
    def decide(self, *, chat_id: str, workspace: Path, tool: str,
               tool_input: dict, auto_allow: tuple[str, ...] | list[str],
               approvals: list[dict], timeout_s: float,
               deny_roots: list[Path] | None = None) -> tuple[bool, str]:
        """Returns ``(allowed, message)``; blocks while the owner decides."""
        target = path_of(tool_input)
        if target and _inside(target, workspace):
            return True, ""
        if target and any(_inside(target, root) for root in deny_roots or []):
            return False, ("that path is the platform's own storage "
                           "(keys, caches, the shared mesh) — off limits")
        if tool in (auto_allow or ()):
            return True, ""
        for rule in approvals or []:
            if rule.get("tool") == tool and \
                    rule.get("chat") in ("*", chat_id):
                return True, ""
        digest = hashlib.sha256(
            f"{chat_id}|{tool}|{json.dumps(tool_input, sort_keys=True, default=str)}"
            .encode()).hexdigest()[:16]
        with self._lock:
            if digest in self._denied:   # a retry of a denied intent
                return False, self._denied[digest]
        detail = target or " ".join(json.dumps(
            tool_input, default=str).split())[:MAX_DETAIL]
        verdict, note = self.ask(chat_id=chat_id, kind="permission",
                                 tool=tool, detail=detail[:MAX_DETAIL],
                                 input_hash=digest, timeout_s=timeout_s)
        if verdict in ("allow", "always"):
            return True, ""
        with self._lock:
            self._denied[digest] = note
        return False, note

    # ----------------------------------------------------------- the pipe
    def ask(self, *, chat_id: str, kind: str, tool: str, detail: str,
            input_hash: str = "", timeout_s: float = 120.0,
            options: list[str] | None = None) -> tuple[str, str]:
        """Publish one ask and wait for the owner. Returns
        ``(verdict, text)`` — verdict allow|always|deny|timeout for
        permissions, answer|timeout for questions (text = the reply/reason).
        ``options``: a question's offered choices — the popup renders them
        as one-tap buttons with free text as the escape (R43/Q28).
        """
        label = (self.docs.ask_phrase(tool)
                 if self.docs is not None and kind == "permission" else "")
        a = Ask(self.agent, chat_id, kind, tool, detail, input_hash,
                timeout_s, label=label, options=options)
        with self._lock:
            self._pending[a.id] = a
            self._publish()
        try:
            deadline = a.expires_at
            while time.time() < deadline:
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
            return [a.to_doc() for a in self._pending.values()]

    # ------------------------------------------------------------- plumbing
    def _publish(self) -> None:
        """The asks doc (harness = its only writer). Best-effort."""
        try:
            self.tx.put_doc(ASK_DOC.format(agent=self.agent), {
                "agent": self.agent, "updated": utcnow_iso(),
                "asks": [a.to_doc() for a in self._pending.values()],
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
