"""Offline PostgREST/Storage backing for the real Supabase transport.

This models protocol operations, not provider authentication or RLS. Each
registry owns isolated backing state; peers share clients, never transport
instances. Raw ``db`` access is retained for existing single-threaded fault
injection tests. Concurrent mutations should use the locked helper methods.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from agentbridge.transport import make_transport as _production_factory
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import LocalMutationTransport
from agentbridge.transport.supabase import SupabaseTransport, _check


def _wire_json(value):
    """Model the client's JSON request boundary, including enum/tuple encoding."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def refresh_transport(transport):
    """Explicitly observe the actual cloud mirror behind a mutation owner.

    This helper never changes admission flags or forwards a new production API.
    Source-cut tests call it before admitting fixtures, not during projections.
    """
    if type(transport) is LocalMutationTransport:
        transport = transport._transport
    if type(transport) is not CachingTransport or type(transport.inner) is not SupabaseTransport:
        raise TypeError("expected exact offline cloud mirror or its mutation owner")
    transport.refresh()
    return transport


def _touch_document(db, row):
    if not db.get("_legacy"):
        db["_docseq"] = db.get("_docseq", 0) + 1
        row["seq"] = db["_docseq"]
        row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row.setdefault("deleted", False)


class FakeQuery:
    """The driver's query surface, including legacy-schema failure controls."""

    def __init__(self, db, table, *, lock=None):
        self.db, self.table = db, table
        self._lock = lock if lock is not None else threading.RLock()
        self.filters = []
        self._like = self._gt = self._lt = self._order = None
        self._desc = False
        self._limit = self._range = None
        self._cols = ""
        self._op = ("select", None)

    def select(self, cols="*"):
        self._cols = str(cols)
        return self

    def insert(self, row, **_kw):
        self._op = ("insert", _wire_json(row))
        return self

    def upsert(self, row, **_kw):
        self._op = ("upsert", _wire_json(row))
        return self

    def update(self, patch, **_kw):
        self._op = ("update", _wire_json(patch))
        return self

    def delete(self):
        self._op = ("delete", None)
        return self

    def eq(self, col, val):
        self.filters.append((col, copy.deepcopy(val)))
        return self

    def like(self, col, pat):
        self._like = (col, pat.rstrip("%"))
        return self

    def gt(self, col, val):
        self._gt = (col, val)
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def order(self, col="id", desc=False):
        self._order, self._desc = col, bool(desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def _match(self, row):
        if any(row.get(col) != val for col, val in self.filters):
            return False
        if self._like and not str(row.get(self._like[0], "")).startswith(self._like[1]):
            return False
        if self._gt and not row.get(self._gt[0], 0) > self._gt[1]:
            return False
        if self._lt and not str(row.get(self._lt[0], "")) < str(self._lt[1]):
            return False
        return True

    def _legacy_guard(self, payload):
        if not self.db.get("_legacy") or self.table != "ab_docs":
            return
        mentioned = set(self._cols.replace(" ", "").split(","))
        mentioned |= {c for c, _ in self.filters}
        mentioned |= set(payload or ())
        if self._order:
            mentioned.add(self._order)
        for comparison in (self._like, self._gt, self._lt):
            if comparison:
                mentioned.add(comparison[0])
        if {"seq", "deleted"} & mentioned:
            raise RuntimeError('column ab_docs.seq does not exist (42703)')

    def _touch(self, row):
        if self.table == "ab_docs":
            _touch_document(self.db, row)

    def execute(self):
        with self._lock:
            return self._execute_locked()

    def _execute_locked(self):
        rows = self.db.setdefault(self.table, [])
        op, source = self._op
        payload = copy.deepcopy(source)
        self._legacy_guard(payload if op in ("insert", "upsert", "update") else None)
        if op == "insert":
            if self.table == "ab_docs" and any(
                r.get("root") == payload.get("root")
                and r.get("path") == payload.get("path") for r in rows
            ):
                raise RuntimeError("duplicate key value violates unique constraint (23505)")
            payload["id"] = self.db["_seq"] = self.db.get("_seq", 0) + 1
            self._touch(payload)
            rows.append(payload)
            return FakeResult([payload])
        if op == "upsert":
            def matching(row):
                return all(row.get(key) == payload.get(key) for key in ("root", "path"))

            if (self.db.get("_deny_genesis_upsert")
                    and self.table == "ab_docs"
                    and str(payload.get("path", "")).startswith("chats/")
                    and str(payload.get("path", "")).endswith("/meta.json")
                    and not any(matching(r) for r in rows)):
                raise RuntimeError(
                    "new row violates row-level security policy for table ab_docs (42501)")
            rows[:] = [r for r in rows if not matching(r)]
            self._touch(payload)
            rows.append(payload)
            return FakeResult([payload])
        if op == "update":
            hit = []
            for row in rows:
                if self._match(row):
                    row.update(copy.deepcopy(payload))
                    self._touch(row)
                    hit.append(row)
            return FakeResult(hit)
        if op == "delete":
            keep = [r for r in rows if not self._match(r)]
            gone = len(rows) - len(keep)
            rows[:] = keep
            return FakeResult([{"deleted": gone}])
        out = [r for r in rows if self._match(r)]
        if self._order:
            out.sort(key=lambda r: r.get(self._order) or 0, reverse=self._desc)
        if self._limit is not None:
            out = out[:self._limit]
        if self._range:
            lo, hi = self._range
            out = out[lo:hi + 1]
        return FakeResult(out)


class FakeResult:
    def __init__(self, data):
        self.data = copy.deepcopy(data)


class FakeBucketApi:
    def __init__(self, objects, *, lock=None):
        self.objects = objects
        self._lock = lock if lock is not None else threading.RLock()

    def upload(self, key, data, file_options=None):
        with self._lock:
            self.objects[key] = bytes(data)

    def download(self, key):
        with self._lock:
            return self.objects[key]

    def list(self, parent):
        with self._lock:
            return [{"name": key.rpartition("/")[2], "metadata": {"size": len(data)}}
                    for key, data in self.objects.items() if key.rpartition("/")[0] == parent]

    def remove(self, keys):
        with self._lock:
            removed = []
            for key in keys:
                if key in self.objects:
                    self.objects.pop(key)
                    removed.append({"name": key})
            return removed


class FakeStorage:
    def __init__(self, *, lock=None):
        self._lock = lock if lock is not None else threading.RLock()
        self.objects = {}
        self.buckets = []

    def list_buckets(self):
        with self._lock:
            return [type("B", (), {"name": name}) for name in self.buckets]

    def create_bucket(self, name):
        with self._lock:
            if name not in self.buckets:
                self.buckets.append(name)

    def from_(self, _bucket):
        return FakeBucketApi(self.objects, lock=self._lock)


class FakeExec:
    """RPC evaluation occurs at execute(), under its backing client's gate."""

    def __init__(self, rows=None, *, evaluate=None, lock=None):
        self._rows = copy.deepcopy(rows)
        self._evaluate = evaluate
        self._lock = lock if lock is not None else threading.RLock()

    def execute(self):
        with self._lock:
            return FakeResult(self._evaluate() if self._evaluate is not None else self._rows)


class FakeClient:
    def __init__(self, legacy: bool = False, *, effects_ready: bool = True):
        self.lock = threading.RLock()
        self.db = {"_legacy": legacy, "_effects_ready": effects_ready,
                   "_change_ledger_version": None,
                   "_source_ledger_version": None,
                   "_scoped_source_version": None,
                   "_recovery_version": None}
        self.storage = FakeStorage(lock=self.lock)

    def migrate(self):
        with self.lock:
            self.db["_legacy"] = False
            for row in self.db.get("ab_docs", []):
                if "seq" not in row:
                    _touch_document(self.db, row)

    def table(self, name):
        return FakeQuery(self.db, name, lock=self.lock)

    def rpc(self, fn, params=None):
        params = _wire_json(params or {})
        return FakeExec(evaluate=lambda: self._rpc_locked(fn, params), lock=self.lock)

    def _rpc_locked(self, fn, params):
        if fn == "ab_list_logs":
            heads = {}
            for row in self.db.get("ab_logs", []):
                if row["root"] == params["p_root"] and row["chat_id"] == params["p_chat"]:
                    heads[row["log_name"]] = max(heads.get(row["log_name"], 0), row["id"])
            return [{"log_name": key, "head": value} for key, value in heads.items()]
        if fn == "ab_chat_ids":
            ids = {row["chat_id"] for row in self.db.get("ab_logs", [])
                   if row["root"] == params["p_root"]}
            for row in self.db.get("ab_docs", []):
                if (row["root"] == params["p_root"] and row["path"].startswith("chats/")
                        and not row.get("deleted")):
                    ids.add(row["path"].split("/")[1])
            return [{"chat_id": chat} for chat in sorted(ids)]
        if fn == "ab_effects_ready":
            return int(bool(self.db["_effects_ready"]))
        if fn == "ab_change_ledger_ready":
            return self.db.get("_change_ledger_version")
        if fn == "ab_node_recovery_ready":
            return (self.db.get("_recovery_version")
                    if self.db.get("_recovery_role", "authenticated")
                    == "authenticated"
                    and self.db.get("_recovery_timeout_ok", True) else 0)
        if fn in ("ab_node_recovery_fence", "ab_node_recovery_page"):
            return self._recovery_rpc(fn, params)
        if fn == "ab_change_events_page":
            root = params.get("p_root")
            after = params.get("p_after")
            limit = params.get("p_limit")
            rows = [copy.deepcopy(row) for row in self.db.get("ab_change_events", [])
                    if row.get("root") == root and row.get("id", 0) > after]
            rows.sort(key=lambda row: row["id"])
            return [{key: row.get(key) for key in (
                "id", "stream_kind", "stream_id", "domain", "doc_head", "log_head",
            )} for row in rows[:limit]]
        if fn == "ab_node_source_ledger_ready":
            return self.db.get("_source_ledger_version")
        if fn == "ab_node_source_ledger_fence":
            root = params.get("p_root")
            epochs = [row for row in self.db.get("ab_change_epochs", [])
                      if row.get("root") == root]
            if len(epochs) != 1:
                return []
            events = [row for row in self.db.get("ab_change_events", [])
                      if row.get("root") == root]
            minimum = epochs[0].get("minimum_cursor")
            cursor = max([minimum] + [row.get("id", 0) for row in events])
            return [{"epoch": epochs[0].get("epoch"),
                     "minimum_cursor": minimum, "cursor": cursor,
                     "schema_version": 2}]
        if fn == "ab_node_source_events_page":
            root = params.get("p_root")
            after = params.get("p_after")
            limit = params.get("p_limit")
            rows = [copy.deepcopy(row) for row in self.db.get("ab_change_events", [])
                    if row.get("root") == root and row.get("id", 0) > after]
            rows.sort(key=lambda row: row["id"])
            return [{key: row.get(key) for key in (
                "id", "stream_kind", "stream_id", "domain", "source_key",
                "doc_head", "log_head",
            )} for row in rows[:limit]]
        if fn == "ab_node_scoped_source_ready":
            return self.db.get("_scoped_source_version")
        if fn == "ab_node_docs_exact":
            root = params.get("p_root")
            paths = params.get("p_paths")
            budget = params.get("p_max_bytes")
            by_path = {row.get("path"): copy.deepcopy(row)
                       for row in self.db.get("ab_docs", [])
                       if row.get("root") == root}
            rows = []
            total = 0
            for path in paths:
                row = by_path.get(path)
                if row is None:
                    continue
                deleted = bool(row.get("deleted"))
                data = None if deleted else row.get("data")
                payload_size = 0 if deleted else len(json.dumps(
                    data, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8"))
                total += len(json.dumps(
                    path, ensure_ascii=False,
                ).encode("utf-8")) + payload_size + 256
                rows.append({
                    "path": path, "seq": row.get("seq"), "data": data,
                    "deleted": deleted, "batch_overflow": False,
                })
            if total > budget:
                return [{"path": None, "seq": None, "data": None,
                         "deleted": None, "batch_overflow": True}]
            return rows
        if fn == "ab_node_log_exact_page":
            rows = [copy.deepcopy(row) for row in self.db.get("ab_logs", [])
                    if row.get("root") == params.get("p_root")
                    and row.get("chat_id") == params.get("p_chat")
                    and row.get("log_name") == params.get("p_log")
                    and params.get("p_after") < row.get("id", 0)
                    <= params.get("p_through")]
            rows.sort(key=lambda row: row["id"])
            candidates = rows[:params.get("p_limit") + 1]
            selected = []
            used = 0
            for row in candidates:
                size = len(json.dumps(
                    row.get("line", ""), ensure_ascii=False,
                ).encode("utf-8")) + 256
                if (len(selected) >= params.get("p_limit")
                        or used + size > params.get("p_max_bytes")):
                    break
                selected.append(row)
                used += size
            if candidates and not selected:
                return [{"id": None, "line": None, "page_has_more": True,
                         "page_overflow": True}]
            more = len(selected) < len(candidates)
            return [{"id": row.get("id"), "line": row.get("line"),
                     "page_has_more": more, "page_overflow": False}
                    for row in selected]
        if fn == "ab_effect_transition":
            return self._effect_transition_locked(params)
        return []

    def _recovery_rpc(self, fn, params):
        """Offline response shape; SQL locking/RLS require provider tests."""
        root = params["p_root"]
        if (self.db.get("_recovery_version") != 1
                or not self.db.get("_recovery_timeout_ok", True)):
            raise ValueError("recovery source schema is unavailable")
        if (self.db.get("_recovery_denied")
                or self.db.get("_recovery_role", "authenticated") != "authenticated"):
            raise ValueError("recovery root is unavailable")
        epoch = next((row for row in self.db.get("ab_change_epochs", [])
                      if row["root"] == root), None)
        if epoch is None or epoch.get("source_schema_version") != 2:
            raise ValueError("recovery source schema is unavailable")
        minimum = epoch["minimum_cursor"]
        tail = max([minimum] + [row["id"] for row in
                   self.db.get("ab_change_events", []) if row["root"] == root])
        cut = {"schema_version": 1, "index_contract": "root-manifest-v1",
               "source_schema_version": 2, "source_epoch": epoch["epoch"],
               "account_id": self.db.get("_recovery_account",
                                         "12345678-1234-5678-9234-567812345679"),
               "role": "authenticated", "minimum_cursor": minimum,
               "cursor": tail}
        if fn == "ab_node_recovery_fence":
            return cut
        family = params["p_family"]
        after, after_log, after_id = (params[key] for key in (
            "p_after", "p_after_log", "p_after_id"))
        visible = self.db.get("_recovery_visible_chats")

        def can_read(path):
            if not path.startswith("chats/") or visible is None:
                return True
            return path.split("/")[1] in visible

        if family == "documents":
            rows = [{"path": row["path"], "seq": row["seq"],
                     "deleted": bool(row.get("deleted")),
                     "data": None if row.get("deleted") else copy.deepcopy(row["data"])}
                    for row in self.db.get("ab_docs", [])
                    if row["root"] == root and row["path"] > after
                    and not row["path"].startswith("presence/")
                    and can_read(row["path"])]
            rows.sort(key=lambda row: row["path"])
        elif family == "chats":
            rows = [row["path"].split("/")[1]
                    for row in self.db.get("ab_docs", [])
                    if row["root"] == root and row["path"].startswith("chats/")
                    and row["path"] == f"chats/{row['path'].split('/')[1]}/meta.json"
                    and row["path"].split("/")[1] > after and can_read(row["path"])]
            rows.sort()
        elif family == "streams":
            heads = {}
            for row in self.db.get("ab_logs", []):
                pair = (row["chat_id"], row["log_name"])
                if (row["root"] == root and pair > (after, after_log)
                        and can_read(f"chats/{row['chat_id']}/meta.json")):
                    heads[pair] = max(heads.get(pair, 0), row["id"])
            rows = [{"chat_id": chat, "log_name": log, "head": heads[chat, log]}
                    for chat, log in sorted(heads)]
        else:
            if after_id < minimum or after_id > tail:
                raise ValueError("recovery replay cursor unavailable")
            rows = [{field: copy.deepcopy(row.get(field)) for field in (
                "id", "stream_kind", "stream_id", "domain", "source_key",
                "doc_head", "log_head")}
                    for row in self.db.get("ab_change_events", [])
                    if row["root"] == root and after_id < row["id"] <= tail
                    and (row["stream_kind"] == "root"
                         or visible is None or row["stream_id"] in visible)]
            rows.sort(key=lambda row: row["id"])
        selected = []
        used = 0
        budget = params["p_max_bytes"] - 1024
        for row in rows[:params["p_limit"] + 1]:
            if len(selected) >= params["p_limit"]:
                break
            size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 256
            next_continuation = (
                [row["chat_id"], row["log_name"]] if family == "streams" else
                row["path"] if family == "documents" else
                row if family == "chats" else row["id"]
            )
            continuation_size = len(json.dumps(
                next_continuation, ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"))
            if used + size + continuation_size > budget:
                break
            used += size
            selected.append(row)
        overflow = bool(rows) and not selected
        more = len(selected) < len(rows)
        outcome = ("overflow" if overflow else "empty_terminal" if not rows
                   else "page")
        continuation = ((selected[-1]["path"] if family == "documents" else
                         selected[-1] if family == "chats" else
                         [selected[-1]["chat_id"], selected[-1]["log_name"]]
                         if family == "streams" else selected[-1]["id"])
                        if selected else
            [after, after_log] if family == "streams" else
            after_id if family == "events" else after)
        return {"cut": cut, "rows": selected, "has_more": more,
                "empty_terminal": not rows, "overflow": overflow,
                "outcome": outcome, "continuation": continuation}

    def _effect_transition_locked(self, params):
        """Model the schema's ordered atomic lane, without authenticating a caller.

        Routing and current fixture documents are checked; real signatures and
        member authentication remain the application's/provider's responsibility.
        """
        if not self.db["_effects_ready"]:
            return False
        root, path, data = (params.get(key) for key in ("p_root", "p_path", "p_data"))
        if not isinstance(path, str) or not isinstance(data, dict):
            return False
        parts = path.split("/")
        if (len(parts) != 7 or parts[0] != "chats" or parts[2:4] != ["runtime", "effects"]
                or any(not part for part in parts)
                or parts[-1] not in ("claim.json", "state-2.json", "state-3.json")):
            return False
        _chats, chat, _runtime, _effects, run, call, filename = parts
        meta = data.get("meta")
        if not isinstance(meta, dict):
            return False
        actor = meta.get("actor")
        expected = {"kind": "effect", "signer": actor, "chat_id": chat,
                    "run_id": run, "root_run_id": run, "call_id": call}
        if (not isinstance(actor, str) or not actor
                or any(meta.get(key) != value for key, value in expected.items())):
            return False
        rows = self.db.setdefault("ab_docs", [])
        indexed = {row["path"]: row for row in rows if row["root"] == root}

        def live(selected):
            row = indexed.get(selected)
            return row.get("data") if row is not None and not row.get("deleted") else None

        account, room = live(f"users/{actor}.json"), live(f"chats/{chat}/meta.json")
        if (not isinstance(account, dict) or account.get("kind") != "agent"
                or account.get("active") is not True or not isinstance(account.get("agent"), dict)
                or not isinstance(room, dict)
                or not isinstance(room.get("members"), (dict, list))):
            return False
        owner = account["agent"].get("owner")
        if (not isinstance(owner, str) or not owner
                or owner not in room["members"] or actor not in room["members"]):
            return False
        base = path.rsplit("/", 1)[0]
        proposed = []
        if filename == "claim.json":
            for key, kind, sender, recipient, target in (
                ("p_grant_ask", "permission_ask", actor, owner, "grant-ask.json"),
                ("p_grant_decision", "permission_decision", owner, actor, "grant-decision.json"),
            ):
                envelope = params.get(key)
                header = envelope.get("header") if isinstance(envelope, dict) else None
                expected_header = {"kind": kind, "sender": sender, "recipient": recipient,
                                   "agent": actor, "chat_id": chat}
                if (not isinstance(header, dict)
                        or any(header.get(key) != value
                               for key, value in expected_header.items())):
                    return False
                proposed.append((f"{base}/{target}", envelope))
        else:
            previous_file = "claim.json" if filename == "state-2.json" else "state-2.json"
            predecessor = live(f"{base}/{previous_file}")
            previous_meta = predecessor.get("meta") if isinstance(predecessor, dict) else None
            if not isinstance(previous_meta, dict) or any(
                previous_meta.get(key) != meta.get(key)
                for key in ("actor", "chat_id", "run_id", "call_id")
            ):
                return False
        proposed.append((path, data))
        if any(selected in indexed for selected, _value in proposed):
            return all(live(selected) == value for selected, value in proposed)
        # Validate every companion/predecessor/conflict before changing any row.
        additions = [{"root": root, "path": selected,
                      "data": copy.deepcopy(value), "deleted": False}
                     for selected, value in proposed]
        for row in additions:
            _touch_document(self.db, row)
            rows.append(row)
        return True

    def seed_documents(self, root, documents):
        """Merge fixture documents in one linear pass, advancing real delta cursors.

        This is an explicit setup hook, not a replacement for testing transport
        writes. Nothing changes if payload detachment or path validation fails.
        """
        detached = {_check(path): _wire_json(value) for path, value in documents.items()}
        with self.lock:
            rows = self.db.setdefault("ab_docs", [])
            keep = [row for row in rows
                    if row["root"] != root or row["path"] not in detached]
            for path, value in detached.items():
                row = {"root": root, "path": path, "data": value}
                _touch_document(self.db, row)
                keep.append(row)
            rows[:] = keep

    def replace_log(self, root, chat, log, records):
        """Explicit provider tampering hook; replacements get new row high-waters."""
        chat, log = _check(chat), _check(log)
        lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                 for record in records]
        with self.lock:
            rows = self.db.setdefault("ab_logs", [])
            keep = [row for row in rows
                    if (row["root"], row["chat_id"], row["log_name"]) != (root, chat, log)]
            for line in lines:
                self.db["_seq"] = self.db.get("_seq", 0) + 1
                keep.append({"id": self.db["_seq"], "root": root,
                             "chat_id": chat, "log_name": log, "line": line})
            rows[:] = keep


class CloudRegistry:
    """Test-owned roots and real cloud drivers with isolated offline backing."""

    def __init__(self, *, project_url="https://offline.invalid", effects_ready=False,
                 factory_auto_refresh=True, factory_refresh_s=0.05):
        self.project_url = project_url
        self.effects_ready = effects_ready
        self.factory_auto_refresh = factory_auto_refresh
        self.factory_refresh_s = factory_refresh_s
        self._clients = {}
        self._names = {}
        self._resources = []
        self._lock = threading.RLock()
        self._closed = False

    def _open(self):
        if self._closed:
            raise RuntimeError("cloud registry is closed")

    def root(self, key):
        """Register a temporary-root key and return its valid Supabase URI."""
        with self._lock:
            self._open()
            text = str(key)
            if text in self._clients:
                return text
            if text in self._names:
                return self._names[text]
            if "://" in text:
                raise ValueError("unregistered cloud root")
            resolved = str(Path(key).resolve())
            name = "test-" + hashlib.sha256(resolved.encode()).hexdigest()[:24]
            uri = "supabase://" + name
            if uri not in self._clients:
                self._clients[uri] = FakeClient(effects_ready=self.effects_ready)
                self._names[name] = uri
            return uri

    def client(self, key):
        with self._lock:
            return self._clients[self.root(key)]

    def bare(self, key, *, unmetered=True):
        with self._lock:
            uri = self.root(key)
            tx = SupabaseTransport(uri.removeprefix("supabase://"),
                                   env={"SUPABASE_URL": self.project_url},
                                   client=self._clients[uri])
            # The real Realtime path creates its own client independently of
            # client= injection. Disable it before any write, hint or watch.
            tx._ensure_rt = lambda: None
            if unmetered:
                tx.profile = replace(tx.profile, metered=False)
            self._resources.append(tx)
            return tx

    def cached(self, key, *, warm=True, auto_refresh=False, unmetered=True,
               snapshot_path=None, nonblocking_cold=False, refresh_s=None):
        with self._lock:
            cache = CachingTransport(self.bare(key, unmetered=unmetered),
                                     auto_refresh=auto_refresh, snapshot_path=snapshot_path,
                                     nonblocking_cold=nonblocking_cold, refresh_s=refresh_s)
            self._resources.append(cache)
        if warm:
            cache.refresh()
        return cache

    def seed_documents(self, key, documents):
        with self._lock:
            uri = self.root(key)
            self._clients[uri].seed_documents(uri.removeprefix("supabase://"), documents)

    def replace_log(self, key, chat, log, records):
        with self._lock:
            uri = self.root(key)
            self._clients[uri].replace_log(uri.removeprefix("supabase://"), chat, log, records)

    def make_transport(self, spec, home=None, *, offline_cache=False):
        """Inject registered URIs with live polling, preserving other factory calls.

        Configure factory_auto_refresh=False for tests that explicitly drive
        source preparation. cached() itself defaults to that manual mode.
        """
        with self._lock:
            self._open()
            registered = type(spec) is str and spec in self._clients
        if registered:
            return self.cached(spec, auto_refresh=self.factory_auto_refresh,
                               refresh_s=self.factory_refresh_s)
        return _production_factory(spec, home=home, offline_cache=offline_cache)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            resources, self._resources = self._resources, []
        errors = []
        caches = [resource for resource in resources if type(resource) is CachingTransport]
        for cache in caches:
            cache._stop.set()
            cache.inner.wake_local()
        for resource in reversed(resources):
            try:
                resource.close()
            except Exception as exc:
                errors.append(exc)
        # Production close methods already signal these workers. Join our
        # owned warm-load/hint threads too, so a fixture cannot outlive teardown.
        threads = {getattr(resource, "_warm_thread", None) for resource in resources}
        threads |= {getattr(getattr(resource, "_hints", None), "_thread", None)
                    for resource in resources}
        for thread in threads - {None, threading.current_thread()}:
            thread.join(timeout=2.0)
            if thread.is_alive():
                errors.append(RuntimeError("offline cloud worker did not stop"))
        # A warm loader may have started a refresher just after the first close.
        for cache in caches:
            cache._stop.set()
            cache.inner.wake_local()
            if cache._thread is not None and cache._thread is not threading.current_thread():
                cache._thread.join(timeout=2.0)
                if cache._thread.is_alive():
                    errors.append(RuntimeError("offline cloud refresher did not stop"))
        with self._lock:
            self._clients.clear()
            self._names.clear()
        if errors:
            raise errors[0]

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *_exc):
        self.close()
