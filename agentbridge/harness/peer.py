"""Peer harness access (R22) — with its owner's grant, another agent may
reach THIS agent's harness to diagnose it ("remote access, almost").

Everything crosses the synced folder as SIGNED docs (Ed25519, the info-event
model): a forged request — a folder writer impersonating another agent —
fails verification and is dropped. One writer per doc:
- ``peer/<target>/req/<requester>.json``   — the requester writes; the
  target lists the dir and reads them;
- ``peer/<target>/resp/<requester>.json``  — the target writes; the
  requester polls it.

The target gate, owner-controlled (never the agent's own choice):
- ``peer_access`` = "off" (default): every request denied, silently but
  AUDITED — an off agent is simply unreachable;
- ``peer_access`` = "ask": each session surfaces an owner popup (the R18
  ask surface, kind "peer"); "Always" adds the requester to ``peer_auto``,
  which runs without asking thereafter.

``serve_once`` runs in the target harness's tick — NON-BLOCKING: a request
awaiting the owner is parked in a pending doc (the GUI raises it) and
resolved on a later tick when the verdict lands, or denied on timeout.

Two command classes:
- READ diagnostics (ping / status / run_feed) — gated by ``peer_access``;
  a ``peer_auto`` entry may auto-run them.
- REPAIR mutations (pause / resume / clear_queue / clear_timers, R22.5) —
  a SECOND, stricter gate: refused entirely unless ``peer_repair`` is on,
  and ALWAYS surface a per-session owner popup (a diagnostics auto-grant
  never covers a mutation). They act only on the target harness's OWN
  runtime state (its pause hold, pending queue, scheduled timers) — never
  on chats, messages, accounts, or keys — and the actions are injected by
  the runner, so this module can't reach anything it wasn't handed.

Every outcome — served, denied, timed out — is appended to an owner-
visible audit log.
"""

from __future__ import annotations

import json
import threading
import time

from .. import crypto
from ..core.timekit import new_id, utcnow_iso

__all__ = ["PeerService", "PEER_COMMANDS", "signing_bytes"]

REQ_DIR = "peer/{target}/req/"
REQ_DOC = "peer/{target}/req/{requester}.json"
RESP_DOC = "peer/{target}/resp/{requester}.json"
PENDING_DOC = "status/peer_pending/{target}.json"
VERDICT_DOC = "status/peer_pending/{target}_verdicts.json"
AUDIT_DOC = "status/peer_audit/{target}.json"
STATE_KEY = "harness/peer_state"          # per-requester resolve cursor + awaiting

READ_COMMANDS = ("ping", "status", "run_feed")
REPAIR_COMMANDS = ("pause", "resume", "clear_queue", "clear_timers")
PEER_COMMANDS = READ_COMMANDS + REPAIR_COMMANDS
AWAIT_TIMEOUT_S = 180.0
AUDIT_KEEP = 100


def signing_bytes(env: dict) -> bytes:
    """Canonical bytes signed over a peer request/response — binds the pair
    (to|from), the linked request id, and the payload so neither can be
    replayed, retargeted, or re-linked to a different request."""
    payload = json.dumps(env.get("payload") or {}, sort_keys=True,
                         separators=(",", ":"))
    return (f"{env.get('to', '')}|{env.get('from', '')}|{env.get('id', '')}|"
            f"{env.get('kind', '')}|{env.get('command', '')}|"
            f"{env.get('req_id', '')}|{env.get('ns', 0)}|{payload}").encode()


class PeerService:
    """One agent's peer surface — target side (serve_once) + requester side
    (request/read_response), bound to that agent's Mesh facade."""

    def __init__(self, mesh, repair_ops: dict | None = None) -> None:
        self.mesh = mesh
        self.tx = mesh.tx
        self.agent = mesh.user
        self.store = getattr(mesh, "store", None)
        # repair actions the RUNNER injects (pause/resume/clear_queue/
        # clear_timers -> callables). Absent (e.g. a bare requester) = repair
        # commands can't run here, only be sent.
        self.repair_ops = repair_ops or {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- signing
    def _bundle(self):
        return self.mesh.keystore.load(self.agent)

    def _sign(self, env: dict) -> dict:
        bundle = self._bundle()
        env["sig"] = crypto.sign(bundle, signing_bytes(env)) if bundle else ""
        return env

    def _authentic(self, env: dict, sender: str) -> bool:
        pub = self.mesh.directory.sign_pub(sender)
        sig = env.get("sig") or ""
        return bool(pub) and bool(sig) and crypto.verify(
            pub, sig, signing_bytes(env))

    # ----------------------------------------------------- requester side
    def request(self, target: str, command: str, payload: dict | None = None) -> str:
        """Send (or replace) this agent's diagnostic request to ``target``.
        Returns the request id."""
        if command not in PEER_COMMANDS:
            raise ValueError(f"unknown peer command {command!r}")
        env = self._sign({
            "id": new_id("peer"), "to": target, "from": self.agent,
            "kind": "request", "command": command,
            "payload": payload or {}, "ns": time.time_ns(),
        })
        self.tx.put_doc(REQ_DOC.format(target=target, requester=self.agent), env)
        return env["id"]

    def read_response(self, target: str, req_id: str = "") -> dict | None:
        """The target's signed reply to my request, once it lands (verified).
        ``req_id`` filters to a specific request."""
        env = self.tx.get_doc(RESP_DOC.format(target=target, requester=self.agent))
        if not isinstance(env, dict) or not self._authentic(env, target):
            return None
        if req_id and env.get("req_id") != req_id:
            return None
        return env

    # -------------------------------------------------------- target side
    def serve_once(self, settings) -> int:
        """Resolve verdicts, expire stale awaits, ingest new requests. Never
        raises into the tick loop; returns how many outcomes were written."""
        try:
            with self._lock:
                return self._serve(settings)
        except Exception:  # noqa: BLE001 — a peer glitch never stops the harness
            return 0

    def pending(self) -> list[dict]:
        doc = self.tx.get_doc(PENDING_DOC.format(target=self.agent))
        awaiting = (doc or {}).get("awaiting") if isinstance(doc, dict) else None
        return list(awaiting or [])

    # ------------------------------------------------------------- guts
    def _state(self) -> dict:
        if self.store is None:
            return {}
        return self.store.cached_doc(STATE_KEY, default={}) or {}

    def _save_state(self, state: dict) -> None:
        if self.store is not None:
            self.store.cache_doc(STATE_KEY, state)

    def _serve(self, settings) -> int:
        state = self._state()
        awaiting = dict(state.get("awaiting") or {})   # req_id -> req meta
        resolved = dict(state.get("resolved") or {})   # requester -> last id
        wrote = 0

        # 1) verdicts the owner has answered
        verdicts = self.tx.get_doc(VERDICT_DOC.format(target=self.agent)) or {}
        vmap = verdicts.get("verdicts") if isinstance(verdicts, dict) else {}
        for rid, meta in list(awaiting.items()):
            v = (vmap or {}).get(rid)
            if not isinstance(v, dict):
                continue
            verdict = str(v.get("verdict") or "deny")
            # "always" persistence is the OWNER's write (D19: only the
            # responsible member sets agent config) — the GUI does it when it
            # records the verdict; here always simply serves this session
            if verdict in ("allow", "always"):
                self._run_and_respond(meta)
                self._audit(meta, "allowed")
            else:
                self._respond(meta, ok=False,
                              error="the responsible member declined")
                self._audit(meta, "denied")
            resolved[meta["from"]] = rid
            awaiting.pop(rid, None)
            wrote += 1

        # 2) expire awaits nobody answered (fail closed)
        now = time.time()
        for rid, meta in list(awaiting.items()):
            if now - meta.get("at", now) > AWAIT_TIMEOUT_S:
                self._respond(meta, ok=False,
                              error="no answer from the responsible member")
                self._audit(meta, "timed-out")
                resolved[meta["from"]] = rid
                awaiting.pop(rid, None)
                wrote += 1

        # 3) new requests
        policy = getattr(settings, "peer_access", "off")
        auto = set(getattr(settings, "peer_auto", []) or [])
        repair_on = bool(getattr(settings, "peer_repair", False))
        for path in self.tx.list_docs(REQ_DIR.format(target=self.agent)):
            env = self.tx.get_doc(path)
            if not isinstance(env, dict):
                continue
            requester = env.get("from") or ""
            rid = env.get("id") or ""
            if not requester or not rid:
                continue
            if resolved.get(requester) == rid or rid in awaiting:
                continue  # already handled / already awaiting
            if not self._authentic(env, requester):
                continue  # forged or keyless — ignore, don't even audit
            command = env.get("command")
            if command not in PEER_COMMANDS:
                self._respond_env(env, ok=False, error="unknown command")
                resolved[requester] = rid
                self._audit(env, "bad-command")
                wrote += 1
                continue
            is_repair = command in REPAIR_COMMANDS
            meta = {"id": rid, "from": requester, "command": command,
                    "payload": env.get("payload") or {}, "at": now,
                    "repair": is_repair}
            if policy == "off":
                self._respond(meta, ok=False,
                              error=f"@{self.agent} is not accepting peer access")
                resolved[requester] = rid
                self._audit(meta, "denied-off")
            elif is_repair and not repair_on:
                self._respond(meta, ok=False,
                              error=f"@{self.agent} does not allow repair actions")
                resolved[requester] = rid
                self._audit(meta, "denied-no-repair")
            elif is_repair:
                # a mutation ALWAYS asks — peer_auto covers diagnostics only
                awaiting[rid] = meta
                self._audit(meta, "requested-repair")
            elif requester in auto:
                self._run_and_respond(meta)
                resolved[requester] = rid
                self._audit(meta, "allowed-auto")
            else:
                awaiting[rid] = meta        # park for the owner popup
                self._audit(meta, "requested")
            wrote += 1

        self._save_state({"awaiting": awaiting, "resolved": resolved})
        self._publish_pending(awaiting)
        return wrote

    # ------------------------------------------------------------ commands
    def _run(self, command: str) -> dict:
        if command == "ping":
            from ..gui import __version__
            return {"agent": self.agent, "machine": self.mesh.machine,
                    "version": __version__, "alive": True}
        if command == "status":
            doc = self.tx.get_doc(f"status/{self.agent}_harness.json") or {}
            return {"paused": doc.get("paused"),
                    "queue": len(doc.get("queue") or []),
                    "timers": len(doc.get("timers") or []),
                    "updated": doc.get("updated")}
        if command == "run_feed":
            doc = self.tx.get_doc(f"status/{self.agent}_run.json") or {}
            return {"state": doc.get("state"), "activity": doc.get("activity"),
                    "recent": doc.get("recent"), "updated": doc.get("updated")}
        if command in REPAIR_COMMANDS:
            op = self.repair_ops.get(command)
            if op is None:      # no runner wired this in (e.g. a bare service)
                return {"error": "repair is not available on this harness"}
            return {"command": command, "result": op()}
        return {"error": "unknown command"}

    def _run_and_respond(self, meta: dict) -> None:
        try:
            result = self._run(meta["command"])
            self._respond(meta, ok=True, result=result)
        except Exception as e:  # noqa: BLE001
            self._respond(meta, ok=False, error=f"{type(e).__name__}")

    def _respond(self, meta: dict, *, ok: bool, result: dict | None = None,
                 error: str = "") -> None:
        requester = meta["from"]
        env = self._sign({
            "id": new_id("presp"), "to": requester,
            "from": self.agent, "kind": "response", "command": meta["command"],
            "req_id": meta["id"], "ns": time.time_ns(),
            "payload": {"ok": ok, "result": result or {}, "error": error},
        })
        self.tx.put_doc(RESP_DOC.format(target=self.agent, requester=requester),
                        env)

    def _respond_env(self, req_env: dict, *, ok: bool, error: str = "") -> None:
        self._respond({"from": req_env.get("from"), "id": req_env.get("id"),
                       "command": req_env.get("command")}, ok=ok, error=error)

    # ------------------------------------------------------------ plumbing
    def _publish_pending(self, awaiting: dict) -> None:
        self.tx.put_doc(PENDING_DOC.format(target=self.agent), {
            "agent": self.agent, "updated": utcnow_iso(),
            "awaiting": [{"id": m["id"], "from": m["from"],
                          "command": m["command"],
                          "repair": bool(m.get("repair"))}
                         for m in awaiting.values()],
        })

    def _audit(self, meta: dict, outcome: str) -> None:
        path = AUDIT_DOC.format(target=self.agent)
        doc = self.tx.get_doc(path)
        entries = (doc.get("entries") if isinstance(doc, dict) else None) or []
        entries.append({"ts": utcnow_iso(), "from": meta.get("from"),
                        "command": meta.get("command"), "outcome": outcome,
                        "id": meta.get("id")})
        self.tx.put_doc(path, {"agent": self.agent,
                               "entries": entries[-AUDIT_KEEP:]})
