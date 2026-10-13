"""Execute the inactive mutation protocol against a disposable PostgreSQL DB.

The target database must be named ``agentbridge_mutation_test`` or start with
``agentbridge_mutation_test_``.  The URL is read from
``AGENTBRIDGE_TEST_DATABASE_URL`` by default so credentials never appear in the
process list.  This gate deliberately uses two independent psql sessions for
the legacy-write/seal schedules; static SQL inspection cannot establish the
transaction-lock behavior.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
from threading import Thread
import time
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID, uuid4


REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMA_PATH: Final = REPO_ROOT / "docs" / "supabase_schema.sql"
PROTOCOL_MARKER: Final = (
    "-- ---------------------------------------------------------------------------\n"
    "-- Inactive receipt-bearing mutation protocol (v1)."
)
SAFE_DATABASE = re.compile(
    r"^agentbridge_mutation_test(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?$")

BOOTSTRAP_SQL: Final = r"""
create extension if not exists pgcrypto;

create schema if not exists auth;
create schema if not exists private;

do $$ begin
  create role anon nologin;
exception when duplicate_object then null;
end $$;
do $$ begin
  create role authenticated nologin;
exception when duplicate_object then null;
end $$;
do $$ begin
  create role service_role nologin;
exception when duplicate_object then null;
end $$;

do $$ begin
  if pg_catalog.to_regprocedure('auth.uid()') is null then
    execute $function$
      create function auth.uid() returns uuid language sql stable as $body$
        select nullif(pg_catalog.current_setting(
          'request.jwt.claim.sub', true), '')::uuid
      $body$
    $function$;
  end if;
  if pg_catalog.to_regprocedure('auth.role()') is null then
    execute $function$
      create function auth.role() returns text language sql stable as $body$
        select nullif(pg_catalog.current_setting(
          'request.jwt.claim.role', true), '')
      $body$
    $function$;
  end if;
end $$;

create table public.ab_docs (
  root text not null,
  path text not null,
  data jsonb not null,
  primary key (root, path)
);
create table public.ab_logs (
  id bigint generated always as identity primary key,
  root text not null,
  chat_id text not null,
  log_name text not null,
  line text not null
);
create table public.ab_members (
  root text not null,
  username text not null,
  uid uuid not null,
  primary key (root, username),
  unique (root, uid)
);
create table public.ab_change_epochs (
  root text primary key,
  epoch uuid not null default gen_random_uuid(),
  minimum_cursor bigint not null default 0,
  source_schema_version integer not null default 2
);
create table public.ab_change_events (
  id bigint generated always as identity primary key,
  root text not null
);

alter table public.ab_members enable row level security;
create or replace function public.ab_root_ok(p_root text) returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1 from public.ab_members m
    where m.root = p_root and m.uid = auth.uid()
  )
$$;
revoke all on function public.ab_root_ok(text) from public;
grant execute on function public.ab_root_ok(text) to authenticated;
"""


class GateFailure(RuntimeError):
    """One executable protocol invariant failed."""


def _psql_command() -> list[str]:
    return [
        "psql",
        "-X",
        "--no-psqlrc",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
    ]


def _environment(database_url: str) -> dict[str, str]:
    try:
        parsed = urlsplit(database_url)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        raise GateFailure("database URL is malformed") from None
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise GateFailure("database URL must use postgres or postgresql")
    if (hostname is None or username is None
            or not parsed.path.lstrip("/")):
        raise GateFailure(
            "database URL must include a host, user and database name")
    query = parse_qs(parsed.query, keep_blank_values=True)
    unsupported = set(query) - {"connect_timeout", "sslmode"}
    if unsupported or any(len(values) != 1 for values in query.values()):
        raise GateFailure("database URL contains unsupported connection options")
    env = os.environ.copy()
    for name in (
            "PGDATABASE", "PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER",
            "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE"):
        env.pop(name, None)
    env["PGDATABASE"] = unquote(parsed.path.lstrip("/"))
    env["PGHOST"] = hostname
    env["PGPORT"] = str(port or 5432)
    env["PGUSER"] = unquote(username)
    if password is not None:
        env["PGPASSWORD"] = unquote(password)
    env["PGCONNECT_TIMEOUT"] = query.get("connect_timeout", ["5"])[0]
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"][0]
    return env


def _run(
    database_url: str,
    sql: str,
    *,
    timeout: float = 20,
    expect_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        _psql_command(),
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=_environment(database_url),
        check=False,
    )
    if expect_ok and result.returncode:
        raise GateFailure(result.stderr.strip() or "psql command failed")
    if not expect_ok and not result.returncode:
        raise GateFailure("psql command unexpectedly succeeded")
    return result


class _PsqlSession:
    """One controllable psql backend with asynchronously drained output."""

    def __init__(self, database_url: str) -> None:
        self.process = subprocess.Popen(
            _psql_command(),
            text=True,
            bufsize=1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(database_url),
        )
        self.stdout: Queue[str | None] = Queue()
        self.stderr: list[str] = []
        self._stdout_thread = Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout.put(line.strip())
        self.stdout.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr.extend(self.process.stderr)

    def send(self, sql: str) -> None:
        if self.process.poll() is not None:
            raise GateFailure(self._failure("psql session exited before command"))
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(sql + "\n")
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise GateFailure(self._failure(
                "psql session exited while accepting command")) from exc

    def wait_marker(self, marker: str, *, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateFailure(f"timed out waiting for psql marker {marker}")
            try:
                line = self.stdout.get(timeout=remaining)
            except Empty as exc:
                raise GateFailure(
                    f"timed out waiting for psql marker {marker}") from exc
            if line == marker:
                return
            if line is None:
                raise GateFailure(self._failure(
                    f"psql session exited before marker {marker}"))

    def finish(self, *, expect_ok: bool = True) -> str:
        if self.process.poll() is None and expect_ok:
            assert self.process.stdin is not None
            try:
                self.process.stdin.write("\\q\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            returncode = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.abort()
            raise GateFailure("psql session did not exit") from None
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        stderr = "".join(self.stderr)
        if expect_ok and returncode:
            raise GateFailure(stderr.strip() or "psql session failed")
        if not expect_ok and not returncode:
            raise GateFailure("psql session unexpectedly succeeded")
        return stderr

    def abort(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def _failure(self, prefix: str) -> str:
        detail = "".join(self.stderr).strip()
        return f"{prefix}: {detail}" if detail else prefix


def _scalar(database_url: str, sql: str) -> str:
    lines = [line.strip() for line in _run(database_url, sql).stdout.splitlines()
             if line.strip()]
    if len(lines) != 1:
        raise GateFailure(f"expected one scalar row, received {lines!r}")
    return lines[0]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _protocol_sql() -> str:
    source = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        start = source.index(PROTOCOL_MARKER)
    except ValueError as exc:
        raise GateFailure("mutation protocol marker is missing") from exc
    return source[start:]


def _prepare_root(database_url: str, root: str, uid: UUID) -> UUID:
    _run(
        database_url,
        f"""
        insert into public.ab_members(root, username, uid)
        values ({_quote(root)}, {_quote(root + '-member')}, {_quote(str(uid))});
        insert into public.ab_change_epochs(root)
        values ({_quote(root)});
        """,
    )
    return UUID(_scalar(
        database_url,
        "select protocol_epoch::text from public.ab_mutation_roots "
        f"where root = {_quote(root)};",
    ))


def _seal_sql(root: str, epoch: UUID, seal_id: UUID) -> str:
    digest = "a" * 64
    return (
        "select public.ab_admin_seal_legacy_database_mutations("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(seal_id))}::uuid, {_quote(digest)});"
    )


def _wait_for_lock_order(
    database_url: str,
    *,
    root: str,
    holder: _PsqlSession,
    holder_name: str,
    holder_mode: str,
    waiter: _PsqlSession,
    waiter_name: str,
    waiter_mode: str,
    lock_prefix: str = "agentbridge-change-ledger:",
) -> None:
    deadline = time.monotonic() + 5
    root_key = (
        "pg_catalog.hashtextextended("
        f"{_quote(lock_prefix)} || {_quote(root)}, 0)")
    query = f"""
      select count(*)
      from pg_catalog.pg_locks held
      join pg_catalog.pg_stat_activity holder
        on holder.pid = held.pid
      join pg_catalog.pg_locks waiting
        on waiting.locktype = held.locktype
       and waiting.database is not distinct from held.database
       and waiting.classid is not distinct from held.classid
       and waiting.objid is not distinct from held.objid
       and waiting.objsubid is not distinct from held.objsubid
      join pg_catalog.pg_stat_activity waiter
        on waiter.pid = waiting.pid
      where held.locktype = 'advisory'
        and held.database = (
          select oid from pg_catalog.pg_database
          where datname = pg_catalog.current_database()
        )
        and holder.application_name = {_quote(holder_name)}
        and waiter.application_name = {_quote(waiter_name)}
        and held.classid = (({root_key} >> 32) & 4294967295)::oid
        and held.objid = ({root_key} & 4294967295)::oid
        and held.objsubid = 1
        and held.mode = {_quote(holder_mode)} and held.granted
        and waiting.mode = {_quote(waiter_mode)} and not waiting.granted;
    """
    while time.monotonic() < deadline:
        if holder.process.poll() is not None:
            raise GateFailure(holder._failure("lock holder exited early"))
        if waiter.process.poll() is not None:
            raise GateFailure(waiter._failure("lock waiter exited early"))
        if int(_scalar(database_url, query)):
            return
        time.sleep(0.025)
    raise GateFailure(
        "timed out waiting for exact participant advisory-lock contention")


def _assert_permission_denied(result: subprocess.CompletedProcess[str]) -> None:
    if "permission denied" not in result.stderr.lower():
        raise GateFailure(
            "expected permission denial, received: " + result.stderr.strip())


def _validate_state_and_grants(database_url: str) -> None:
    root = "state-contract"
    uid = uuid4()
    epoch = _prepare_root(database_url, root, uid)
    state_text = _scalar(
        database_url,
        f"""
        begin;
        set local role authenticated;
        set local "request.jwt.claim.sub" = {_quote(str(uid))};
        set local "request.jwt.claim.role" = 'authenticated';
        select public.ab_mutation_protocol_state({_quote(root)})::text;
        commit;
        """,
    )
    state = json.loads(state_text)
    if state["root"] != root or state["protocol_epoch"] != str(epoch):
        raise GateFailure("state probe did not bind the requested root/epoch")
    if state["account_id"] != str(uid) or state["role"] != "authenticated":
        raise GateFailure("state probe did not bind the authenticated principal")
    if state["legacy_writes_enabled"] is not True:
        raise GateFailure("installation changed the legacy-write mode")
    if state["database_seal"] is not None:
        raise GateFailure("installation fabricated a database seal")
    if state["blob_writes_sealed"] is not False:
        raise GateFailure("database-v1 incorrectly covered blob writes")

    receipt = _run(
        database_url,
        "set role authenticated; insert into public.ab_mutation_receipts("
        "root, protocol_epoch, operation_id, actor_uid, operation_kind, "
        "payload_digest, result) values ("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, gen_random_uuid(), "
        f"{_quote(str(uid))}::uuid, 'doc_put', {_quote('b' * 64)}, "
        "'{}'::jsonb);",
        expect_ok=False,
    )
    _assert_permission_denied(receipt)
    seal = _run(
        database_url,
        "set role authenticated; " + _seal_sql(root, epoch, uuid4()),
        expect_ok=False,
    )
    _assert_permission_denied(seal)


def _validate_seal_replay(database_url: str) -> None:
    root = "seal-replay"
    epoch = _prepare_root(database_url, root, uuid4())
    _run(
        database_url,
        "begin; insert into public.ab_docs(root, path, data) values "
        f"({_quote(root)}, 'before.json', '{{}}'::jsonb); "
        "insert into public.ab_change_events(root) values "
        f"({_quote(root)}); commit;",
    )
    seal_id = uuid4()
    first = json.loads(_scalar(database_url, _seal_sql(root, epoch, seal_id)))
    replay = json.loads(_scalar(database_url, _seal_sql(root, epoch, seal_id)))
    if first != replay or first["seal_id"] != str(seal_id):
        raise GateFailure("exact seal replay did not return the same receipt")
    tail = int(_scalar(
        database_url,
        "select max(id) from public.ab_change_events where root = "
        f"{_quote(root)};",
    ))
    if first["cursor"] != tail:
        raise GateFailure("seal receipt omitted the completed source-event tail")
    rejected = _run(
        database_url,
        "insert into public.ab_docs(root, path, data) values "
        f"({_quote(root)}, 'after.json', '{{}}'::jsonb);",
        expect_ok=False,
    )
    if "legacy provider writes are sealed" not in rejected.stderr:
        raise GateFailure("post-seal legacy write was not rejected by the gate")


def _validate_old_write_before_seal(database_url: str) -> None:
    root = "old-write-before-seal"
    epoch = _prepare_root(database_url, root, uuid4())
    suffix = uuid4().hex[:8]
    writer_name = f"ab-gate-writer-before-{suffix}"
    sealer_name = f"ab-gate-sealer-after-{suffix}"
    writer_ready = f"writer-ready-{suffix}"
    writer_done = f"writer-done-{suffix}"
    seal_acquired = f"seal-acquired-{suffix}"
    seal_done = f"seal-done-{suffix}"
    writer = _PsqlSession(database_url)
    sealer = _PsqlSession(database_url)
    try:
        writer.send(
            f"set application_name = {_quote(writer_name)}; begin; "
            "insert into public.ab_docs(root, path, data) values "
            f"({_quote(root)}, 'old.json', '{{}}'::jsonb); "
            "insert into public.ab_change_events(root) values "
            f"({_quote(root)}); select {_quote(writer_ready)};")
        writer.wait_marker(writer_ready)
        sealer.send(
            f"set application_name = {_quote(sealer_name)}; begin; "
            + _seal_sql(root, epoch, uuid4())
            + f" select {_quote(seal_acquired)};")
        _wait_for_lock_order(
            database_url,
            root=root,
            holder=writer,
            holder_name=writer_name,
            holder_mode="ShareLock",
            waiter=sealer,
            waiter_name=sealer_name,
            waiter_mode="ExclusiveLock",
        )
        writer.send(f"commit; select {_quote(writer_done)};")
        writer.wait_marker(writer_done)
        sealer.wait_marker(seal_acquired)
        sealer.send(f"commit; select {_quote(seal_done)};")
        sealer.wait_marker(seal_done)
        writer.finish()
        sealer.finish()
        cursor_matches = _scalar(
            database_url,
            "select (s.cursor = max(e.id))::text "
            "from public.ab_database_mutation_seals s "
            "join public.ab_change_events e on e.root = s.root "
            f"where s.root = {_quote(root)} group by s.cursor;",
        )
        if cursor_matches != "true":
            raise GateFailure(
                "seal cut did not include the admitted legacy write's event")
    finally:
        writer.abort()
        sealer.abort()


def _validate_old_write_after_seal(database_url: str) -> None:
    root = "old-write-after-seal"
    epoch = _prepare_root(database_url, root, uuid4())
    suffix = uuid4().hex[:8]
    sealer_name = f"ab-gate-sealer-before-{suffix}"
    writer_name = f"ab-gate-writer-after-{suffix}"
    seal_acquired = f"seal-acquired-{suffix}"
    seal_done = f"seal-done-{suffix}"
    writer_escaped = f"writer-escaped-{suffix}"
    sealer = _PsqlSession(database_url)
    writer = _PsqlSession(database_url)
    try:
        sealer.send(
            f"set application_name = {_quote(sealer_name)}; begin; "
            + _seal_sql(root, epoch, uuid4())
            + f" select {_quote(seal_acquired)};")
        sealer.wait_marker(seal_acquired)
        writer.send(
            f"set application_name = {_quote(writer_name)}; begin; "
            "insert into public.ab_docs(root, path, data) values "
            f"({_quote(root)}, 'late.json', '{{}}'::jsonb); "
            f"select {_quote(writer_escaped)};")
        _wait_for_lock_order(
            database_url,
            root=root,
            holder=sealer,
            holder_name=sealer_name,
            holder_mode="ExclusiveLock",
            waiter=writer,
            waiter_name=writer_name,
            waiter_mode="ShareLock",
        )
        sealer.send(f"commit; select {_quote(seal_done)};")
        sealer.wait_marker(seal_done)
        sealer.finish()
        writer_error = writer.finish(expect_ok=False)
        if "legacy provider writes are sealed" not in writer_error:
            raise GateFailure(
                "post-lock legacy write escaped the irreversible seal")
        escaped = int(_scalar(
            database_url,
            "select count(*) from public.ab_docs where root = "
            f"{_quote(root)} and path = 'late.json';",
        ))
        if escaped:
            raise GateFailure("rejected legacy write left a source row behind")
    finally:
        sealer.abort()
        writer.abort()


def _claims(uid: UUID) -> str:
    return (
        f"set local \"request.jwt.claim.sub\" = {_quote(str(uid))}; "
        "set local \"request.jwt.claim.role\" = 'authenticated'; "
    )


def _begin_sql(
    root: str,
    epoch: UUID,
    operation_id: UUID,
    kind: str,
    client_digest: str,
    provider_digest: str,
) -> str:
    return (
        "select private.ab_mutation_begin("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(operation_id))}::uuid, {_quote(kind)}, "
        f"{_quote(client_digest)}, {_quote(provider_digest)})"
    )


def _perform_begin_sql(
    root: str,
    epoch: UUID,
    operation_id: UUID,
    kind: str,
    client_digest: str,
    provider_digest: str,
) -> str:
    call = _begin_sql(
        root, epoch, operation_id, kind, client_digest, provider_digest,
    ).removeprefix("select ")
    return f"do $ab$ begin perform {call}; end $ab$"


def _finish_sql(
    root: str,
    epoch: UUID,
    operation_id: UUID,
    kind: str,
    client_digest: str,
    provider_digest: str,
    result: str,
) -> str:
    return (
        "select private.ab_mutation_finish("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(operation_id))}::uuid, {_quote(kind)}, "
        f"{_quote(client_digest)}, {_quote(provider_digest)}, "
        f"{_quote(result)}::jsonb)"
    )


def _receipt_sql(
    function: str,
    root: str,
    epoch: UUID,
    operation_id: UUID,
    client_digest: str,
    provider_digest: str,
) -> str:
    return (
        f"select public.{function}("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(operation_id))}::uuid, "
        f"{_quote(client_digest)}, {_quote(provider_digest)})"
    )


def _validate_p1b_core(database_url: str) -> None:
    root = "typed-receipt-core"
    actor = uuid4()
    observer = uuid4()
    legacy_epoch = _prepare_root(database_url, root, actor)
    _run(
        database_url,
        "insert into public.ab_members(root, username, uid) values ("
        f"{_quote(root)}, 'typed-observer', {_quote(str(observer))}::uuid);",
    )
    seal_id = uuid4()
    seal = json.loads(_scalar(
        database_url, _seal_sql(root, legacy_epoch, seal_id)))
    epoch = UUID(seal["protocol_epoch"])

    operation_id = uuid4()
    client_digest = "b" * 64
    provider_digest = "c" * 64
    applied = json.loads(_scalar(
        database_url,
        "begin; " + _claims(actor)
        + _perform_begin_sql(
            root, epoch, operation_id, "doc_put",
            client_digest, provider_digest,
        )
        + "; insert into public.ab_docs(root, path, data) values ("
        f"{_quote(root)}, 'typed.json', '{{\"value\":1}}'::jsonb); "
        + _finish_sql(
            root, epoch, operation_id, "doc_put",
            client_digest, provider_digest, '{"path":"typed.json"}',
        )
        + "; commit;",
    ))
    if applied["operation_id"] != str(operation_id):
        raise GateFailure("typed mutation receipt lost its operation identity")

    direct = _run(
        database_url,
        "insert into public.ab_docs(root, path, data) values ("
        f"{_quote(root)}, 'direct.json', '{{}}'::jsonb);",
        expect_ok=False,
    )
    if "legacy provider writes are sealed" not in direct.stderr:
        raise GateFailure("sealed direct write bypassed the receipt-aware gate")

    replay = json.loads(_scalar(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, operation_id, "doc_put",
            client_digest, provider_digest,
        )
        + "; commit;",
    ))
    if replay.get("replay") is not True:
        raise GateFailure("exact typed retry did not resolve its receipt")
    collision = _run(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, operation_id, "doc_put",
            "d" * 64, provider_digest,
        )
        + "; commit;",
        expect_ok=False,
    )
    if "collides with existing receipt" not in collision.stderr:
        raise GateFailure("typed operation collision was not rejected")

    wrong_lane_id = uuid4()
    wrong_lane = _run(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, wrong_lane_id, "doc_put",
            client_digest, "e" * 64,
        )
        + "; insert into public.ab_logs(root, chat_id, log_name, line) values ("
        f"{_quote(root)}, 'chat', 'writer', '{{}}'); commit;",
        expect_ok=False,
    )
    if "legacy provider writes are sealed" not in wrong_lane.stderr:
        raise GateFailure("typed receipt authorized the wrong source lane")
    leaked = int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = {_quote(str(wrong_lane_id))}::uuid;",
    ))
    if leaked:
        raise GateFailure("failed typed mutation retained an applying receipt")

    receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _receipt_sql(
            "ab_mutation_receipt", root, epoch, operation_id,
            client_digest, provider_digest,
        )
        + "; commit;",
    ))
    if receipt["result"] != {"path": "typed.json"}:
        raise GateFailure("exact receipt query returned the wrong result")
    _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _receipt_sql(
            "ab_ack_mutation_receipt", root, epoch, operation_id,
            client_digest, provider_digest,
        )
        + "; commit;",
    )

    leave_id = uuid4()
    leave_client = "f" * 64
    leave_provider = "1" * 64
    _run(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, leave_id, "member_leave",
            leave_client, leave_provider,
        )
        + "; delete from public.ab_members where root = "
        f"{_quote(root)} and uid = {_quote(str(actor))}::uuid; "
        + _finish_sql(
            root, epoch, leave_id, "member_leave",
            leave_client, leave_provider, '{"left":true}',
        )
        + "; commit;",
    )
    post_leave = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _receipt_sql(
            "ab_mutation_receipt", root, epoch, leave_id,
            leave_client, leave_provider,
        )
        + "; commit;",
    ))
    if post_leave["result"] != {"left": True}:
        raise GateFailure("membership loss stranded the terminal receipt")
    post_leave_replay = json.loads(_scalar(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, leave_id, "member_leave",
            leave_client, leave_provider,
        )
        + "; commit;",
    ))
    if post_leave_replay.get("replay") is not True:
        raise GateFailure("membership loss stranded exact operation replay")
    _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _receipt_sql(
            "ab_ack_mutation_receipt", root, epoch, leave_id,
            leave_client, leave_provider,
        )
        + "; commit;",
    )

    rotation_id = uuid4()
    rotation = json.loads(_scalar(
        database_url,
        "select public.ab_admin_rotate_mutation_epoch("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(rotation_id))}::uuid);",
    ))
    rotated_epoch = UUID(rotation["protocol_epoch"])
    replay_rotation = json.loads(_scalar(
        database_url,
        "select public.ab_admin_rotate_mutation_epoch("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(rotation_id))}::uuid);",
    ))
    if rotation != replay_rotation:
        raise GateFailure("exact epoch rotation replay changed its receipt")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)};",
    )):
        raise GateFailure("epoch rotation retained acknowledged receipts")

    state = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(observer)
        + f"select public.ab_mutation_protocol_state({_quote(root)})::text; "
        "commit;",
    ))
    if (state["schema_version"] != 2
            or state["protocol_epoch"] != str(rotated_epoch)
            or state["database_seal"]["protocol_epoch"] != str(epoch)):
        raise GateFailure("rotated state rewrote or lost the historical seal")
    historical_seal = json.loads(_scalar(
        database_url, _seal_sql(root, legacy_epoch, seal_id)))
    if historical_seal != seal:
        raise GateFailure("rotation broke exact historical seal replay")
    stale = _run(
        database_url,
        "begin; " + _claims(actor)
        + _begin_sql(
            root, epoch, uuid4(), "doc_put", client_digest, provider_digest,
        )
        + "; commit;",
        expect_ok=False,
    )
    if "typed mutation epoch is stale" not in stale.stderr:
        raise GateFailure("old-epoch retry survived receipt rotation")


def _validate_p1b_concurrency_and_bounds(database_url: str) -> None:
    # A typed transaction owns the epoch's shared lock before it can reach the
    # source ledger.  Seal must wait there, without taking the later change
    # lock, and then proceed after a rollback leaves no receipt/effect.
    seal_root = "typed-before-seal"
    seal_actor = uuid4()
    legacy_epoch = _prepare_root(database_url, seal_root, seal_actor)
    staged_id = uuid4()
    suffix = uuid4().hex[:8]
    typed_name = f"ab-typed-before-seal-{suffix}"
    seal_name = f"ab-seal-after-typed-{suffix}"
    typed_ready = f"typed-ready-{suffix}"
    typed_done = f"typed-done-{suffix}"
    seal_done = f"seal-done-{suffix}"
    typed = _PsqlSession(database_url)
    sealer = _PsqlSession(database_url)
    try:
        typed.send(
            f"set application_name = {_quote(typed_name)}; begin; "
            + _claims(seal_actor)
            + _begin_sql(
                seal_root, legacy_epoch, staged_id, "doc_put",
                "2" * 64, "3" * 64,
            )
            + f"; select {_quote(typed_ready)};")
        typed.wait_marker(typed_ready)
        sealer.send(
            f"set application_name = {_quote(seal_name)}; begin; "
            + _seal_sql(seal_root, legacy_epoch, uuid4())
            + f" select {_quote(seal_done)};")
        _wait_for_lock_order(
            database_url,
            root=seal_root,
            holder=typed,
            holder_name=typed_name,
            holder_mode="ShareLock",
            waiter=sealer,
            waiter_name=seal_name,
            waiter_mode="ExclusiveLock",
            lock_prefix="agentbridge-mutation-epoch:",
        )
        typed.send(f"rollback; select {_quote(typed_done)};")
        typed.wait_marker(typed_done)
        sealer.wait_marker(seal_done)
        sealer.send("commit;")
        typed.finish()
        sealer.finish()
    finally:
        typed.abort()
        sealer.abort()

    # Two copies of one operation serialize on its exact operation lock.  The
    # waiter resolves the first terminal receipt and no second effect appears.
    root = "typed-duplicate"
    actor = uuid4()
    before = _prepare_root(database_url, root, actor)
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])
    operation_id = uuid4()
    suffix = uuid4().hex[:8]
    holder_name = f"ab-typed-holder-{suffix}"
    waiter_name = f"ab-typed-waiter-{suffix}"
    holder_ready = f"holder-ready-{suffix}"
    holder_done = f"holder-done-{suffix}"
    waiter_done = f"waiter-done-{suffix}"
    holder = _PsqlSession(database_url)
    waiter = _PsqlSession(database_url)
    try:
        holder.send(
            f"set application_name = {_quote(holder_name)}; begin; "
            + _claims(actor)
            + _begin_sql(
                root, epoch, operation_id, "doc_put", "4" * 64, "5" * 64,
            )
            + "; insert into public.ab_docs(root, path, data) values ("
            f"{_quote(root)}, 'duplicate.json', '{{}}'::jsonb); "
            f"select {_quote(holder_ready)};")
        holder.wait_marker(holder_ready)
        waiter.send(
            f"set application_name = {_quote(waiter_name)}; begin; "
            + _claims(actor)
            + _begin_sql(
                root, epoch, operation_id, "doc_put", "4" * 64, "5" * 64,
            )
            + f"; select {_quote(waiter_done)};")
        _wait_for_lock_order(
            database_url,
            root=f"{root}:{epoch}:{operation_id}",
            holder=holder,
            holder_name=holder_name,
            holder_mode="ExclusiveLock",
            waiter=waiter,
            waiter_name=waiter_name,
            waiter_mode="ExclusiveLock",
            lock_prefix="agentbridge-mutation-operation:",
        )
        holder.send(
            _finish_sql(
                root, epoch, operation_id, "doc_put", "4" * 64, "5" * 64,
                '{"path":"duplicate.json"}',
            )
            + f"; commit; select {_quote(holder_done)};")
        holder.wait_marker(holder_done)
        waiter.wait_marker(waiter_done)
        waiter.send("commit;")
        holder.finish()
        waiter.finish()
    finally:
        holder.abort()
        waiter.abort()
    counts = _scalar(
        database_url,
        "select ((select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = {_quote(str(operation_id))}::uuid)"
        "::text || ':' || (select count(*) from public.ab_docs where root = "
        f"{_quote(root)} and path = 'duplicate.json')::text);",
    )
    if counts != "1:1":
        raise GateFailure("concurrent duplicate produced extra receipt/effect")

    # A near-full epoch skips settled slots before locking.  One new operation
    # should hold only epoch, operation and successful-slot advisory locks.
    bounded_root = "typed-near-full"
    bounded_actor = uuid4()
    bounded_before = _prepare_root(database_url, bounded_root, bounded_actor)
    bounded_seal = json.loads(_scalar(
        database_url, _seal_sql(bounded_root, bounded_before, uuid4())))
    bounded_epoch = UUID(bounded_seal["protocol_epoch"])
    _run(
        database_url,
        "insert into public.ab_mutation_receipts("
        "root,protocol_epoch,operation_id,actor_uid,operation_kind,"
        "payload_digest,provider_digest,receipt_slot,receipt_state,"
        "applying_xid,acknowledged_at,result) "
        f"select {_quote(bounded_root)}, {_quote(str(bounded_epoch))}::uuid, "
        "pg_catalog.gen_random_uuid(), "
        f"{_quote(str(bounded_actor))}::uuid, 'doc_put', repeat('6',64), "
        "repeat('7',64), slot, 'terminal', null, now(), '{}'::jsonb "
        "from pg_catalog.generate_series(0,4094) slot;",
        timeout=30,
    )
    advisory_locks = int(_scalar(
        database_url,
        "begin; " + _claims(bounded_actor)
        + _perform_begin_sql(
            bounded_root, bounded_epoch, uuid4(), "doc_put",
            "8" * 64, "9" * 64,
        )
        + "; select count(*) from pg_catalog.pg_locks "
        "where pid = pg_catalog.pg_backend_pid() and locktype = 'advisory'; "
        "rollback;",
    ))
    if advisory_locks > 3:
        raise GateFailure(
            "near-full receipt allocation accumulated advisory locks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-env",
        default="AGENTBRIDGE_TEST_DATABASE_URL",
        help="environment variable holding the disposable PostgreSQL URL",
    )
    args = parser.parse_args()
    if shutil.which("psql") is None:
        parser.error("psql is required; no PostgreSQL client is available")
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        parser.error(f"{args.database_url_env} is not set")

    database_name = _scalar(database_url, "select current_database();")
    if not SAFE_DATABASE.fullmatch(database_name):
        parser.error(
            "refusing non-disposable database; name it "
            "agentbridge_mutation_test or agentbridge_mutation_test_<suffix>"
        )

    existing = int(_scalar(
        database_url,
        "select count(*) from ("
        "select c.relname from pg_catalog.pg_class c "
        "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname in ('public', 'private') "
        "and c.relname like 'ab\\_%' escape '\\' "
        "union all select p.proname from pg_catalog.pg_proc p "
        "join pg_catalog.pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname in ('public', 'private') "
        "and p.proname like 'ab\\_%' escape '\\') existing;",
    ))
    if existing:
        parser.error(
            "refusing database with existing public/private ab_* objects")

    auth_functions = _scalar(
        database_url,
        "select ((pg_catalog.to_regprocedure('auth.uid()') is not null)::int "
        "::text || ':' || "
        "(pg_catalog.to_regprocedure('auth.role()') is not null)::int::text);",
    )
    if auth_functions not in {"0:0", "1:1"}:
        parser.error("refusing incomplete existing auth.uid/auth.role contract")

    server_version = _scalar(database_url, "show server_version;")
    server_version_num = int(_scalar(
        database_url, "show server_version_num;"))

    _run(database_url, BOOTSTRAP_SQL, timeout=30)
    _run(database_url, _protocol_sql(), timeout=30)
    _validate_state_and_grants(database_url)
    _validate_seal_replay(database_url)
    _validate_old_write_before_seal(database_url)
    _validate_old_write_after_seal(database_url)
    _validate_p1b_core(database_url)
    _validate_p1b_concurrency_and_bounds(database_url)
    _run(database_url, _protocol_sql(), timeout=30)
    print(json.dumps({
        "database": database_name,
        "gate": "inactive-mutation-protocol-p1a-p1b-core",
        "result": "passed",
        "server_version": server_version,
        "server_version_num": server_version_num,
        "schedules": [
            "legacy-before-seal", "legacy-after-seal",
            "typed-replay", "typed-member-leave", "typed-epoch-rotation",
            "typed-before-seal", "typed-duplicate", "typed-near-full",
            "schema-rerun",
        ],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateFailure, subprocess.TimeoutExpired) as exc:
        print(f"mutation protocol PostgreSQL gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
