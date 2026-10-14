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
EFFECT_MARKER: Final = (
    "-- R142: versioned, authenticated append-only effect transitions."
)
EFFECT_END_MARKER: Final = "-- ab_logs: members only, both ways;"
SAFE_DATABASE = re.compile(
    r"^agentbridge_mutation_test(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?$")

BOOTSTRAP_SQL: Final = r"""
create schema if not exists auth;
create schema if not exists private;
create schema if not exists storage;

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
  deleted boolean not null default false,
  primary key (root, path)
);
create sequence public.ab_docs_ver;
alter table public.ab_docs add column seq bigint not null default 0;
create function public.ab_docs_touch() returns trigger language plpgsql as $$
begin
  new.seq := nextval('public.ab_docs_ver');
  return new;
end $$;
create trigger ab_docs_touch before insert or update on public.ab_docs
  for each row execute function public.ab_docs_touch();
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
create table storage.objects (
  id uuid primary key default gen_random_uuid(),
  bucket_id text not null,
  name text not null,
  owner_id text,
  metadata jsonb,
  version text,
  archived_at timestamptz,
  is_delete_marker boolean not null default false,
  is_versioned boolean not null default false,
  unique (bucket_id, name)
);

alter table public.ab_members enable row level security;
alter table storage.objects enable row level security;
grant usage on schema storage to authenticated, service_role;
grant select, insert, update, delete on table storage.objects
  to authenticated, service_role;
create or replace function public.ab_source_json_bytes(p_value jsonb)
returns integer language sql immutable strict set search_path = pg_catalog as $$
  select pg_catalog.octet_length(
    pg_catalog.convert_to(p_value::text, 'UTF8')
  )::integer
$$;
create or replace function public.ab_source_text_bytes(p_value text)
returns integer language sql immutable strict set search_path = pg_catalog as $$
  select pg_catalog.octet_length(
    pg_catalog.convert_to(pg_catalog.to_json(p_value)::text, 'UTF8')
  )::integer
$$;
alter table public.ab_docs add column source_bytes integer
  generated always as (public.ab_source_json_bytes(data)) stored;
alter table public.ab_logs add column source_bytes integer
  generated always as (public.ab_source_text_bytes(line)) stored;
alter table public.ab_docs add constraint ab_docs_node_source_size check (
  source_bytes between 0 and 8355840
);
alter table public.ab_logs add constraint ab_logs_node_source_size check (
  source_bytes between 0 and 8355840
);

create or replace function public.ab_member(p_root text) returns text
language sql stable security definer set search_path = pg_catalog as $$
  select coalesce((
    select m.username from public.ab_members m
    where m.root = p_root and m.uid = auth.uid()
  ), '')
$$;
create or replace function public.ab_root_ok(p_root text) returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1 from public.ab_members m
    where m.root = p_root and m.uid = auth.uid()
  )
$$;
revoke all on function public.ab_root_ok(text) from public;
grant execute on function public.ab_root_ok(text) to authenticated;

create or replace function public.ab_chat_of(p_path text) returns text
language sql immutable set search_path = pg_catalog as $$
  select case when p_path like 'chats/%'
              then pg_catalog.split_part(p_path, '/', 2) else '' end
$$;
create or replace function public.ab_is_member(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and m.data->'members' ? public.ab_member(p_root)
  )
$$;
create or replace function public.ab_can_read_chat(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and (
        m.data->'members' ? public.ab_member(p_root)
        or (
          m.data @> '{"deleted": true}'::jsonb
          and m.data->'tenure' ? public.ab_member(p_root)
        )
      )
  )
$$;
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


def _jsonb(value: object) -> str:
    return _quote(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )) + "::jsonb"


def _protocol_sql() -> str:
    source = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        start = source.index(PROTOCOL_MARKER)
    except ValueError as exc:
        raise GateFailure("mutation protocol marker is missing") from exc
    return source[start:]


def _effect_sql() -> str:
    source = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        start = source.index(EFFECT_MARKER)
        end = source.index(EFFECT_END_MARKER, start)
    except ValueError as exc:
        raise GateFailure("R142 effect transition source is missing") from exc
    return source[start:end]


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
    lock_key_sql: str | None = None,
) -> None:
    deadline = time.monotonic() + 5
    root_key = lock_key_sql or (
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


def _blob_manifest_sql(
    root: str,
    epoch: UUID,
    operation_id: UUID,
    payload_digest: str,
    chat: str,
    blob: str,
    object_id: UUID,
    sealed_bytes: int,
    sealed_sha256: str,
    manifest_digest: str,
) -> str:
    return (
        "select public.ab_mutation_blob_manifest("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(operation_id))}::uuid, {_quote(payload_digest)}, "
        f"{_quote(chat)}, {_quote(blob)}, {_quote(str(object_id))}::uuid, "
        f"{sealed_bytes}, {_quote(sealed_sha256)}, "
        f"{_quote(manifest_digest)})"
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


def _validate_p1b_doc_log_rpcs(database_url: str) -> None:
    root = "typed-doc-log"
    actor = uuid4()
    outsider = uuid4()
    before = _prepare_root(database_url, root, actor)
    actor_name = root + "-member"
    _run(
        database_url,
        "insert into public.ab_members(root, username, uid) values ("
        f"{_quote(root)}, 'typed-doc-log-outsider', "
        f"{_quote(str(outsider))}::uuid); "
        "insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'chats/room/meta.json', "
        f"pg_catalog.jsonb_build_object('members', "
        f"pg_catalog.jsonb_build_object({_quote(actor_name)}, "
        "'{}'::jsonb)), false);",
    )
    preseal_id = uuid4()
    preseal = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(before))}::uuid, "
        f"{_quote(str(preseal_id))}::uuid, {_quote('9' * 64)}, "
        "'preseal.json', '{}'::jsonb); commit;",
        expect_ok=False,
    )
    if "typed mutation root is not sealed" not in preseal.stderr:
        raise GateFailure("public typed RPC admitted an operation before sealing")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = {_quote(str(preseal_id))}::uuid;",
    )):
        raise GateFailure("pre-seal typed RPC retained a receipt")
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])

    put_id = uuid4()
    put_digest = "a" * 64
    put_call = (
        "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(put_id))}::uuid, {_quote(put_digest)}, "
        "'global.json', '{\"value\":1}'::jsonb)"
    )
    put_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + put_call + "; commit;",
    ))
    put_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + put_call + "; commit;",
    ))
    if put_receipt != put_replay or put_receipt["result"] != {
            "path": "global.json", "updated": True}:
        raise GateFailure("typed document put replay changed its receipt")
    put_collision = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(put_id))}::uuid, {_quote(put_digest)}, "
        "'global.json', '{\"value\":2}'::jsonb); commit;",
        expect_ok=False,
    )
    if "collides with existing receipt" not in put_collision.stderr:
        raise GateFailure("provider-computed document digest did not bind data")
    stored = _scalar(
        database_url,
        "select data::text from public.ab_docs where root = "
        f"{_quote(root)} and path = 'global.json';",
    )
    if json.loads(stored) != {"value": 1}:
        raise GateFailure("colliding document retry changed source state")

    create_id = uuid4()
    create_digest = "b" * 64
    create_call = (
        "select public.ab_mutation_doc_create("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(create_id))}::uuid, {_quote(create_digest)}, "
        "'chats/room/note.json', '{\"note\":true}'::jsonb)"
    )
    created = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + create_call + "; commit;",
    ))
    if created["result"].get("created") is not True:
        raise GateFailure("typed document create did not report its effect")
    conflict_id = uuid4()
    conflict = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_create("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(conflict_id))}::uuid, {_quote('c' * 64)}, "
        "'chats/room/note.json', '{\"note\":true}'::jsonb); commit;",
        expect_ok=False,
    )
    if "typed document already exists" not in conflict.stderr:
        raise GateFailure("new document-create operation reused an existing row")
    leaked_conflict = int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(conflict_id))}::uuid;",
    ))
    if leaked_conflict:
        raise GateFailure("failed document create retained an applying receipt")

    delete_id = uuid4()
    deleted = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_delete("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(delete_id))}::uuid, {_quote('d' * 64)}, "
        "'chats/room/note.json'); commit;",
    ))
    if deleted["result"].get("deleted") is not True:
        raise GateFailure("typed document delete did not report its tombstone")
    tombstone = _scalar(
        database_url,
        "select (deleted and data = '{}'::jsonb)::text from public.ab_docs "
        f"where root = {_quote(root)} and path = 'chats/room/note.json';",
    )
    if tombstone != "true":
        raise GateFailure("typed document delete did not preserve delta semantics")

    log_id = uuid4()
    log_digest = "e" * 64
    log_call = (
        "select public.ab_mutation_log_append("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(log_id))}::uuid, {_quote(log_digest)}, "
        "'room', 'writer', '{\"message\":\"hello\"}'::jsonb)"
    )
    log_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + log_call + "; commit;",
    ))
    log_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + log_call + "; commit;",
    ))
    log_count = int(_scalar(
        database_url,
        "select count(*) from public.ab_logs where root = "
        f"{_quote(root)} and chat_id = 'room' and log_name = 'writer';",
    ))
    if log_receipt != log_replay or log_count != 1:
        raise GateFailure("typed log replay appended more than one source row")

    denied_id = uuid4()
    denied = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(outsider)
        + "select public.ab_mutation_log_append("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(denied_id))}::uuid, {_quote('f' * 64)}, "
        "'room', 'outsider', '{\"message\":\"no\"}'::jsonb); commit;",
        expect_ok=False,
    )
    if "typed log append is unauthorized" not in denied.stderr:
        raise GateFailure("non-room member appended through the typed RPC")
    denied_count = int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = {_quote(str(denied_id))}::uuid;",
    ))
    if denied_count:
        raise GateFailure("unauthorized typed append retained a receipt")

    effect_denied = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(uuid4()))}::uuid, {_quote('1' * 64)}, "
        "'chats/room/runtime/effects/run/call/state-2.json', "
        "'{}'::jsonb); commit;",
        expect_ok=False,
    )
    if "typed document put is unauthorized" not in effect_denied.stderr:
        raise GateFailure("generic typed document RPC entered the effect lane")

    genesis_root = "typed-doc-genesis"
    genesis_actor = uuid4()
    genesis_before = _prepare_root(database_url, genesis_root, genesis_actor)
    genesis_seal = json.loads(_scalar(
        database_url, _seal_sql(genesis_root, genesis_before, uuid4())))
    genesis_epoch = UUID(genesis_seal["protocol_epoch"])
    genesis_name = genesis_root + "-member"
    for invalid_data in ("'{}'::jsonb", "'null'::jsonb"):
        invalid_id = uuid4()
        invalid_genesis = _run(
            database_url,
            "begin; set local role authenticated; " + _claims(genesis_actor)
            + "select public.ab_mutation_doc_create("
            f"{_quote(genesis_root)}, {_quote(str(genesis_epoch))}::uuid, "
            f"{_quote(str(invalid_id))}::uuid, {_quote('3' * 64)}, "
            f"'chats/new-room/meta.json', {invalid_data}); commit;",
            expect_ok=False,
        )
        if "typed document create is unauthorized" not in invalid_genesis.stderr:
            raise GateFailure(
                "typed chat genesis accepted data without creator membership")
        if int(_scalar(
            database_url,
            "select count(*) from public.ab_mutation_receipts where root = "
            f"{_quote(genesis_root)} and operation_id = "
            f"{_quote(str(invalid_id))}::uuid;",
        )):
            raise GateFailure("rejected chat genesis retained a receipt")
    genesis = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(genesis_actor)
        + "select public.ab_mutation_doc_create("
        f"{_quote(genesis_root)}, {_quote(str(genesis_epoch))}::uuid, "
        f"{_quote(str(uuid4()))}::uuid, {_quote('2' * 64)}, "
        "'chats/new-room/meta.json', "
        "pg_catalog.jsonb_build_object('members', "
        f"pg_catalog.jsonb_build_object({_quote(genesis_name)}, "
        "'{}'::jsonb))); commit;",
    ))
    if genesis["result"].get("created") is not True:
        raise GateFailure("typed chat genesis lost its insert-only exception")


def _validate_p1b_lifecycle_rpcs(database_url: str) -> None:
    root = "typed-lifecycle"
    owner = uuid4()
    outsider = uuid4()
    claimant = uuid4()
    before = _prepare_root(database_url, root, owner)
    owner_name = root + "-member"
    outsider_name = root + "-outsider"
    _run(
        database_url,
        "insert into public.ab_members(root, username, uid) values ("
        f"{_quote(root)}, {_quote(outsider_name)}, "
        f"{_quote(str(outsider))}::uuid);",
    )

    preseal_id = uuid4()
    preseal = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + "select public.ab_mutation_member_claim("
        f"{_quote(root)}, {_quote(str(before))}::uuid, "
        f"{_quote(str(preseal_id))}::uuid, {_quote('4' * 64)}, "
        "'preseal-claimant'); commit;",
        expect_ok=False,
    )
    if "typed mutation root is not sealed" not in preseal.stderr:
        raise GateFailure("member claim admitted before root seal")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = {_quote(str(preseal_id))}::uuid;",
    )):
        raise GateFailure("pre-seal member claim retained a receipt")

    chat = "room%_x"
    sibling = "roomAAx"
    denied_chat = "outsider-terminal"
    live_chat = "not-terminal"
    malformed_chats = (
        "terminal-missing-members",
        "terminal-null-members",
        "terminal-list-members",
        "terminal-null-tenure",
    )
    _run(
        database_url,
        "insert into public.ab_docs(root,path,data,deleted) values "
        f"({_quote(root)}, {_quote(f'chats/{chat}/meta.json')}, "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '{}'::jsonb, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, {_quote(f'chats/{chat}/note.json')}, "
        "'{\"note\":true}'::jsonb, false), "
        f"({_quote(root)}, {_quote(f'chats/{sibling}/meta.json')}, "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '{}'::jsonb, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, {_quote(f'chats/{sibling}/note.json')}, "
        "'{\"note\":\"sibling\"}'::jsonb, false), "
        f"({_quote(root)}, {_quote(f'chats/{denied_chat}/meta.json')}, "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '{}'::jsonb, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, {_quote(f'chats/{live_chat}/meta.json')}, "
        "pg_catalog.jsonb_build_object("
        "'deleted', false, 'members', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '{{}}'::jsonb), "
        "'tenure', pg_catalog.jsonb_build_object("
        f"{_quote(owner_name)}, '[]'::jsonb)), false), "
        f"({_quote(root)}, 'chats/terminal-missing-members/meta.json', "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, 'chats/terminal-null-members/meta.json', "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', null, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, 'chats/terminal-list-members/meta.json', "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '[]'::jsonb, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(owner_name)}, '[]'::jsonb)), "
        "false), "
        f"({_quote(root)}, 'chats/terminal-null-tenure/meta.json', "
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '{}'::jsonb, 'tenure', null), false); "
        "insert into public.ab_logs(root,chat_id,log_name,line) values "
        f"({_quote(root)}, {_quote(chat)}, 'writer', '{{}}'), "
        f"({_quote(root)}, {_quote(chat)}, 'writer', '{{}}'), "
        f"({_quote(root)}, {_quote(sibling)}, 'writer', '{{}}');",
    )
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])

    claim_id = uuid4()
    claim_digest = "5" * 64
    claim_call = (
        "select public.ab_mutation_member_claim("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(claim_id))}::uuid, {_quote(claim_digest)}, "
        "'typed-new-member')"
    )
    claim_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + claim_call + "; commit;",
    ))
    claim_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + claim_call + "; commit;",
    ))
    if claim_receipt != claim_replay or claim_receipt["result"] != {
            "claimed": True, "username": "typed-new-member"}:
        raise GateFailure("typed member claim replay changed its receipt")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_members where root = "
        f"{_quote(root)} and uid = {_quote(str(claimant))}::uuid;",
    )) != 1:
        raise GateFailure("typed member claim did not create one self row")

    collision = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + "select public.ab_mutation_member_claim("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(claim_id))}::uuid, {_quote(claim_digest)}, "
        "'changed-member'); commit;",
        expect_ok=False,
    )
    if "collides with existing receipt" not in collision.stderr:
        raise GateFailure("member claim provider digest did not bind username")
    duplicate_id = uuid4()
    duplicate = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + "select public.ab_mutation_member_claim("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(duplicate_id))}::uuid, {_quote('6' * 64)}, "
        "'typed-new-member'); commit;",
        expect_ok=False,
    )
    if "conflicts with existing identity" not in duplicate.stderr:
        raise GateFailure("fresh member operation reused existing identity")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(duplicate_id))}::uuid;",
    )):
        raise GateFailure("conflicting member claim retained a receipt")

    leave_id = uuid4()
    leave_digest = "7" * 64
    leave_call = (
        "select public.ab_mutation_member_leave("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(leave_id))}::uuid, {_quote(leave_digest)})"
    )
    leave_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + leave_call + "; commit;",
    ))
    leave_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + leave_call + "; commit;",
    ))
    if leave_receipt != leave_replay or leave_receipt["result"] != {
            "left": True, "username": "typed-new-member"}:
        raise GateFailure("member leave did not replay after membership loss")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_members where root = "
        f"{_quote(root)} and uid = {_quote(str(claimant))}::uuid;",
    )):
        raise GateFailure("typed member leave did not remove exact self row")
    second_leave_id = uuid4()
    second_leave = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(claimant)
        + "select public.ab_mutation_member_leave("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(second_leave_id))}::uuid, {_quote('8' * 64)}); commit;",
        expect_ok=False,
    )
    if "typed member leave is unauthorized" not in second_leave.stderr:
        raise GateFailure("nonmember started a fresh member leave")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(second_leave_id))}::uuid;",
    )):
        raise GateFailure("unauthorized member leave retained a receipt")

    delete_id = uuid4()
    delete_digest = "9" * 64
    delete_call = (
        "select public.ab_mutation_chat_delete("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(delete_id))}::uuid, {_quote(delete_digest)}, "
        f"{_quote(chat)})"
    )
    delete_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(owner)
        + delete_call + "; commit;",
    ))
    delete_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(owner)
        + delete_call + "; commit;",
    ))
    expected_delete = {
        "chat_id": chat, "documents_tombstoned": 2, "logs_deleted": 2,
    }
    if delete_receipt != delete_replay \
            or delete_receipt["result"] != expected_delete:
        raise GateFailure("terminal chat delete replay changed its receipt")
    target_state = _scalar(
        database_url,
        "select (count(*) = 2 and bool_and(deleted and data = '{}'::jsonb))"
        "::text from public.ab_docs where root = "
        f"{_quote(root)} and pg_catalog.left(path, "
        f"pg_catalog.length({_quote(f'chats/{chat}/')})) = "
        f"{_quote(f'chats/{chat}/')};",
    )
    sibling_state = _scalar(
        database_url,
        "select (count(*) = 2 and bool_and(not deleted))::text "
        "from public.ab_docs where root = "
        f"{_quote(root)} and pg_catalog.left(path, "
        f"pg_catalog.length({_quote(f'chats/{sibling}/')})) = "
        f"{_quote(f'chats/{sibling}/')};",
    )
    log_state = _scalar(
        database_url,
        "select ((count(*) filter (where chat_id = " + _quote(chat)
        + ")) = 0 and (count(*) filter (where chat_id = "
        + _quote(sibling) + ")) = 1)::text from public.ab_logs where root = "
        + _quote(root) + ";",
    )
    if target_state != "true" or sibling_state != "true" or log_state != "true":
        raise GateFailure("chat delete crossed its exact room subtree")

    denied_delete_id = uuid4()
    denied_delete = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(outsider)
        + "select public.ab_mutation_chat_delete("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(denied_delete_id))}::uuid, {_quote('a' * 64)}, "
        f"{_quote(denied_chat)}); commit;",
        expect_ok=False,
    )
    if "typed chat delete is unauthorized" not in denied_delete.stderr:
        raise GateFailure("chat deletion ignored exact terminal tenure")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(denied_delete_id))}::uuid;",
    )):
        raise GateFailure("unauthorized chat delete retained a receipt")

    live_delete_id = uuid4()
    live_delete = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(owner)
        + "select public.ab_mutation_chat_delete("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(live_delete_id))}::uuid, {_quote('b' * 64)}, "
        f"{_quote(live_chat)}); commit;",
        expect_ok=False,
    )
    if "typed chat delete is unauthorized" not in live_delete.stderr:
        raise GateFailure("chat delete accepted a nonterminal room")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(live_delete_id))}::uuid;",
    )):
        raise GateFailure("nonterminal chat delete retained a receipt")

    for malformed_chat in malformed_chats:
        malformed_id = uuid4()
        malformed = _run(
            database_url,
            "begin; set local role authenticated; " + _claims(owner)
            + "select public.ab_mutation_chat_delete("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(malformed_id))}::uuid, {_quote('c' * 64)}, "
            f"{_quote(malformed_chat)}); commit;",
            expect_ok=False,
        )
        if "typed chat delete is unauthorized" not in malformed.stderr:
            raise GateFailure("chat delete accepted malformed terminal meta")
        if int(_scalar(
            database_url,
            "select count(*) from public.ab_mutation_receipts where root = "
            f"{_quote(root)} and operation_id = "
            f"{_quote(str(malformed_id))}::uuid;",
        )):
            raise GateFailure("malformed terminal delete retained a receipt")


def _validate_p1b_room_terminal_lock(database_url: str) -> None:
    root = "typed-room-terminal-lock"
    chat = "room"
    actor = uuid4()
    actor_name = root + "-member"
    before = _prepare_root(database_url, root, actor)
    _run(
        database_url,
        "insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'chats/room/meta.json', "
        "pg_catalog.jsonb_build_object("
        "'deleted', false, 'members', "
        f"pg_catalog.jsonb_build_object({_quote(actor_name)}, '{{}}'::jsonb), "
        "'tenure', pg_catalog.jsonb_build_object("
        f"{_quote(actor_name)}, '[]'::jsonb)), false), ("
        f"{_quote(root)}, 'chats/room/note.json', '{{}}'::jsonb, false);",
    )
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])

    writer_id = uuid4()
    terminal_id = uuid4()
    suffix = uuid4().hex[:8]
    writer_name = f"ab-room-writer-{suffix}"
    terminal_name = f"ab-room-terminal-{suffix}"
    writer_ready = f"room-writer-ready-{suffix}"
    writer_done = f"room-writer-done-{suffix}"
    terminal_done = f"room-terminal-done-{suffix}"
    provider_expr = (
        "private.ab_mutation_provider_digest("
        "pg_catalog.jsonb_build_array("
        "'log_append', 'room', 'writer', '{}'::jsonb))"
    )
    terminal_data = (
        "pg_catalog.jsonb_build_object("
        "'deleted', true, 'members', '{}'::jsonb, 'tenure', "
        f"pg_catalog.jsonb_build_object({_quote(actor_name)}, '[]'::jsonb))"
    )
    writer = _PsqlSession(database_url)
    terminal = _PsqlSession(database_url)
    try:
        writer.send(
            f"set application_name = {_quote(writer_name)}; begin; "
            + _claims(actor)
            + "select private.ab_mutation_begin("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(writer_id))}::uuid, 'log_append', "
            f"{_quote('d' * 64)}, {provider_expr}); "
            f"select private.ab_mutation_lock_actor({_quote(root)}); "
            "select private.ab_mutation_lock_chat("
            f"{_quote(root)}, {_quote(chat)}, false); "
            "do $ab$ begin if not public.ab_is_member("
            f"{_quote(root)}, {_quote(chat)}) then "
            "raise exception 'writer authority missing'; end if; end $ab$; "
            f"select {_quote(writer_ready)};"
        )
        writer.wait_marker(writer_ready)
        terminal.send(
            f"set application_name = {_quote(terminal_name)}; begin; "
            "set local role authenticated; " + _claims(actor)
            + "select public.ab_mutation_doc_put("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(terminal_id))}::uuid, {_quote('e' * 64)}, "
            "'chats/room/meta.json', " + terminal_data + "); "
            f"commit; select {_quote(terminal_done)};"
        )
        room_key = (
            "pg_catalog.hashtextextended("
            "'agentbridge-chat-mutation:' || "
            f"pg_catalog.jsonb_build_array({_quote(root)}, {_quote(chat)})"
            "::text, 0)"
        )
        _wait_for_lock_order(
            database_url,
            root=root,
            holder=writer,
            holder_name=writer_name,
            holder_mode="ShareLock",
            waiter=terminal,
            waiter_name=terminal_name,
            waiter_mode="ExclusiveLock",
            lock_key_sql=room_key,
        )
        writer.send(
            "insert into public.ab_logs(root,chat_id,log_name,line) values ("
            f"{_quote(root)}, {_quote(chat)}, 'writer', '{{}}'); "
            "select private.ab_mutation_finish("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(writer_id))}::uuid, 'log_append', "
            f"{_quote('d' * 64)}, {provider_expr}, "
            "pg_catalog.jsonb_build_object('row_id', 1)); "
            f"commit; select {_quote(writer_done)};"
        )
        writer.wait_marker(writer_done)
        terminal.wait_marker(terminal_done)
        writer.finish()
        terminal.finish()
    finally:
        writer.abort()
        terminal.abort()

    terminal_state = _scalar(
        database_url,
        "select (data @> '{\"deleted\":true,\"members\":{}}'::jsonb)"
        "::text from public.ab_docs where root = "
        f"{_quote(root)} and path = 'chats/room/meta.json';",
    )
    if terminal_state != "true":
        raise GateFailure("terminal meta did not publish after room writer")
    cleanup = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_chat_delete("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(uuid4()))}::uuid, {_quote('f' * 64)}, 'room'); commit;",
    ))
    if cleanup["result"].get("logs_deleted") != 1:
        raise GateFailure("terminal cleanup missed the admitted room writer")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_logs where root = "
        f"{_quote(root)} and chat_id = 'room';",
    )):
        raise GateFailure("room writer survived terminal cleanup")


def _validate_p1b_effect_rpc(database_url: str) -> None:
    root = "typed-effect"
    actor = uuid4()
    outsider = uuid4()
    before = _prepare_root(database_url, root, actor)
    member = root + "-member"
    effect_actor = "helper"
    chat = "room"
    ask = {"header": {
        "kind": "permission_ask", "sender": effect_actor,
        "recipient": member, "agent": effect_actor, "chat_id": chat,
    }}
    decision = {"header": {
        "kind": "permission_decision", "sender": member,
        "recipient": effect_actor, "agent": effect_actor, "chat_id": chat,
    }}

    def effect_path(call: str, filename: str) -> str:
        return f"chats/{chat}/runtime/effects/run/{call}/{filename}"

    def effect_doc(call: str, state: str) -> dict[str, object]:
        return {
            "meta": {
                "kind": "effect", "actor": effect_actor,
                "signer": effect_actor, "chat_id": chat,
                "run_id": "run", "root_run_id": "run", "call_id": call,
            },
            "state": state,
        }

    conflict_base = effect_path("conflict", "claim.json").rsplit("/", 1)[0]
    _run(
        database_url,
        "insert into public.ab_members(root,username,uid) values ("
        f"{_quote(root)}, 'typed-effect-outsider', "
        f"{_quote(str(outsider))}::uuid); "
        "insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'users/{effect_actor}.json', "
        "pg_catalog.jsonb_build_object("
        "'kind','agent','active',true,'agent',"
        f"pg_catalog.jsonb_build_object('owner',{_quote(member)})), false), ("
        f"{_quote(root)}, 'chats/{chat}/meta.json', "
        "pg_catalog.jsonb_build_object('members',"
        f"pg_catalog.jsonb_build_object({_quote(member)},'{{}}'::jsonb,"
        f"{_quote(effect_actor)},'{{}}'::jsonb)), false), ("
        f"{_quote(root)}, {_quote(conflict_base + '/grant-ask.json')}, "
        f"{_jsonb(ask)}, false), ("
        f"{_quote(root)}, {_quote(conflict_base + '/grant-decision.json')}, "
        f"{_jsonb(decision)}, false);",
    )
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])

    def call_sql(
        operation_id: UUID,
        digest: str,
        path: str,
        data: object,
        *,
        grant_ask: object | None = None,
        grant_decision: object | None = None,
    ) -> str:
        return (
            "select public.ab_mutation_effect_transition("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(operation_id))}::uuid, {_quote(digest)}, "
            f"{_quote(path)}, {_jsonb(data)}, "
            f"{_jsonb(grant_ask) if grant_ask is not None else 'null'}, "
            f"{_jsonb(grant_decision) if grant_decision is not None else 'null'})"
        )

    claim_path = effect_path("call", "claim.json")
    claim_doc = effect_doc("call", "prepared")
    claim_id = uuid4()
    claim_digest = "1" * 64
    claim_call = call_sql(
        claim_id, claim_digest, claim_path, claim_doc,
        grant_ask=ask, grant_decision=decision,
    )
    claim_receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + claim_call + "; commit;",
    ))
    claim_replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + claim_call + "; commit;",
    ))
    if claim_receipt != claim_replay or claim_receipt["result"] != {
            "path": claim_path, "transitioned": True}:
        raise GateFailure("typed effect claim replay changed its receipt")
    base = claim_path.rsplit("/", 1)[0]
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_docs where root = "
        f"{_quote(root)} and path in ("
        f"{_quote(claim_path)}, {_quote(base + '/grant-ask.json')}, "
        f"{_quote(base + '/grant-decision.json')}) and not deleted;",
    )) != 3:
        raise GateFailure("typed effect claim lost its atomic grant evidence")

    collision = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(
            claim_id, claim_digest, claim_path,
            effect_doc("call", "changed"),
            grant_ask=ask, grant_decision=decision,
        ) + "; commit;",
        expect_ok=False,
    )
    if "collides with existing receipt" not in collision.stderr:
        raise GateFailure("typed effect digest did not bind the exact request")

    duplicate_id = uuid4()
    duplicate = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(
            duplicate_id, "2" * 64, claim_path, claim_doc,
            grant_ask=ask, grant_decision=decision,
        ) + "; commit;",
        expect_ok=False,
    )
    if "typed effect transition already exists" not in duplicate.stderr:
        raise GateFailure("fresh effect operation accepted an existing path")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id = "
        f"{_quote(str(duplicate_id))}::uuid;",
    )):
        raise GateFailure("fresh duplicate effect retained a receipt")

    missing_id = uuid4()
    missing = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(
            missing_id, "3" * 64,
            effect_path("missing", "state-3.json"),
            effect_doc("missing", "committed"),
        ) + "; commit;",
        expect_ok=False,
    )
    if "predecessor is unavailable" not in missing.stderr:
        raise GateFailure("typed effect skipped its exact predecessor")

    state2_id = uuid4()
    state2_path = effect_path("call", "state-2.json")
    state2_doc = effect_doc("call", "executing")
    state2 = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(state2_id, "4" * 64, state2_path, state2_doc)
        + "; commit;",
    ))
    state3_id = uuid4()
    state3_path = effect_path("call", "state-3.json")
    state3_doc = effect_doc("call", "committed")
    state3_call = call_sql(state3_id, "5" * 64, state3_path, state3_doc)
    state3 = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + state3_call + "; commit;",
    ))
    if state2["result"].get("path") != state2_path \
            or state3["result"].get("path") != state3_path:
        raise GateFailure("typed effect state chain returned the wrong path")

    conflict_id = uuid4()
    conflict = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(
            conflict_id, "6" * 64,
            effect_path("conflict", "claim.json"),
            effect_doc("conflict", "prepared"),
            grant_ask=ask, grant_decision=decision,
        ) + "; commit;",
        expect_ok=False,
    )
    if "did not commit exact effect" not in conflict.stderr:
        raise GateFailure("pre-existing grant evidence fabricated an effect")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_docs where root = "
        f"{_quote(root)} and path = "
        f"{_quote(effect_path('conflict', 'claim.json'))};",
    )):
        raise GateFailure("conflicting grant evidence left a partial effect")

    direct = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_effect_transition("
        f"{_quote(root)}, {_quote(effect_path('direct', 'claim.json'))}, "
        f"{_jsonb(effect_doc('direct', 'prepared'))}, "
        f"{_jsonb(ask)}, {_jsonb(decision)}); commit;",
        expect_ok=False,
    )
    if "legacy provider writes are sealed" not in direct.stderr:
        raise GateFailure("legacy effect endpoint bypassed the sealed root")

    meta_id = uuid4()
    meta = {"members": {effect_actor: {}}}
    _scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(meta_id))}::uuid, {_quote('7' * 64)}, "
        f"'chats/{chat}/meta.json', {_jsonb(meta)}); commit;",
    )
    replay_after_removal = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + state3_call + "; commit;",
    ))
    if replay_after_removal != state3:
        raise GateFailure("exact effect replay failed after room removal")
    denied_id = uuid4()
    denied = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call_sql(
            denied_id, "8" * 64,
            effect_path("denied", "claim.json"),
            effect_doc("denied", "prepared"),
            grant_ask=ask, grant_decision=decision,
        ) + "; commit;",
        expect_ok=False,
    )
    if "typed effect transition is unauthorized" not in denied.stderr:
        raise GateFailure("removed room member started a fresh effect")
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id in ("
        f"{_quote(str(missing_id))}::uuid, "
        f"{_quote(str(conflict_id))}::uuid, "
        f"{_quote(str(denied_id))}::uuid);",
    )):
        raise GateFailure("rejected typed effect retained a receipt")


def _validate_p1b_effect_path_lock(database_url: str) -> None:
    root = "typed-effect-lock"
    actor = uuid4()
    before = _prepare_root(database_url, root, actor)
    member = root + "-member"
    path = "chats/room/runtime/effects/run/call/claim.json"
    data = {"meta": {
        "kind": "effect", "actor": "helper", "signer": "helper",
        "chat_id": "room", "run_id": "run", "root_run_id": "run",
        "call_id": "call",
    }, "state": "prepared"}
    ask = {"header": {
        "kind": "permission_ask", "sender": "helper",
        "recipient": member, "agent": "helper", "chat_id": "room",
    }}
    decision = {"header": {
        "kind": "permission_decision", "sender": member,
        "recipient": "helper", "agent": "helper", "chat_id": "room",
    }}
    _run(
        database_url,
        "insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'users/helper.json', "
        "pg_catalog.jsonb_build_object("
        "'kind','agent','active',true,'agent',"
        f"pg_catalog.jsonb_build_object('owner',{_quote(member)})), false), ("
        f"{_quote(root)}, 'chats/room/meta.json', "
        "pg_catalog.jsonb_build_object('members',"
        f"pg_catalog.jsonb_build_object({_quote(member)},'{{}}'::jsonb,"
        "'helper','{}'::jsonb)), false);",
    )
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])
    holder_id = uuid4()
    waiter_id = uuid4()
    holder_client = "9" * 64
    holder_provider = "a" * 64
    suffix = uuid4().hex[:8]
    holder_name = f"ab-effect-holder-{suffix}"
    waiter_name = f"ab-effect-waiter-{suffix}"
    holder_ready = f"effect-holder-ready-{suffix}"
    holder_done = f"effect-holder-done-{suffix}"
    lock_key = (
        "pg_catalog.hashtextextended("
        "'agentbridge-effect-transition:' || "
        "pg_catalog.jsonb_build_array("
        f"{_quote(root)}, {_quote(path)})::text, 0)"
    )
    holder = _PsqlSession(database_url)
    waiter = _PsqlSession(database_url)
    try:
        holder.send(
            f"set application_name = {_quote(holder_name)}; begin; "
            + _claims(actor)
            + _begin_sql(
                root, epoch, holder_id, "effect_transition",
                holder_client, holder_provider,
            )
            + f"; select private.ab_mutation_lock_actor({_quote(root)}); "
            + "select private.ab_mutation_lock_chat("
            + f"{_quote(root)}, 'room', false); "
            + f"select pg_catalog.pg_advisory_xact_lock({lock_key}); "
            + f"select {_quote(holder_ready)};"
        )
        holder.wait_marker(holder_ready)
        waiter.send(
            f"set application_name = {_quote(waiter_name)}; begin; "
            + _claims(actor)
            + "select public.ab_mutation_effect_transition("
            + f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            + f"{_quote(str(waiter_id))}::uuid, {_quote('b' * 64)}, "
            + f"{_quote(path)}, {_jsonb(data)}, {_jsonb(ask)}, "
            + f"{_jsonb(decision)}); commit;"
        )
        _wait_for_lock_order(
            database_url,
            root=root,
            holder=holder,
            holder_name=holder_name,
            holder_mode="ExclusiveLock",
            waiter=waiter,
            waiter_name=waiter_name,
            waiter_mode="ExclusiveLock",
            lock_key_sql=lock_key,
        )
        holder.send(
            "select public.ab_effect_transition("
            f"{_quote(root)}, {_quote(path)}, {_jsonb(data)}, "
            f"{_jsonb(ask)}, {_jsonb(decision)}); "
            "select private.ab_mutation_finish("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(holder_id))}::uuid, 'effect_transition', "
            f"{_quote(holder_client)}, {_quote(holder_provider)}, "
            "pg_catalog.jsonb_build_object("
            f"'path',{_quote(path)},'transitioned',true)); "
            f"commit; select {_quote(holder_done)};"
        )
        holder.wait_marker(holder_done)
        holder.finish()
        waiter_error = waiter.finish(expect_ok=False)
        if "typed effect transition already exists" not in waiter_error:
            raise GateFailure(
                "effect path waiter did not reject the committed winner")
    finally:
        holder.abort()
        waiter.abort()

    counts = _scalar(
        database_url,
        "select ((select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)})::text || ':' || "
        "(select count(*) from public.ab_docs where root = "
        f"{_quote(root)} and path = {_quote(path)})::text);",
    )
    if counts != "1:1":
        raise GateFailure("effect path contention produced duplicate evidence")


def _validate_p1b_blob_manifest_rpc(database_url: str) -> None:
    # Exercise casts on stored values, not just constants: SQL inlining and
    # predicate reordering must not turn malformed metadata into an exception.
    size_checks = _scalar(database_url, """
        create temporary table ab_size_cases(metadata jsonb, expected bigint);
        insert into ab_size_cases values
          (null, null), ('null', null), ('{}', null), ('[]', null),
          ('{"size":null}', null), ('{"size":true}', null),
          ('{"size":{}}', null), ('{"size":[]}', null),
          ('{"size":"garbage"}', null), ('{"size":"1e3"}', null),
          ('{"size":"1.0"}', null), ('{"size":"-1"}', null),
          ('{"size":"99999999999999999999999999999"}', null),
          ('{"size":"0"}', null), ('{"size":52428801}', null),
          ('{"size":1}', 1), ('{"size":"4096"}', 4096),
          ('{"size":52428800}', 52428800);
        select bool_and(private.ab_storage_object_size(metadata)
                        is not distinct from expected) from ab_size_cases;
    """)
    if size_checks != "t":
        raise GateFailure("Storage size metadata did not fail closed")
    root = "typed-blob"
    chat = "room"
    blob = "blob-primary"
    object_id = uuid4()
    size = 37
    actor = uuid4()
    reader = uuid4()
    outsider = uuid4()
    before = _prepare_root(database_url, root, actor)
    actor_name = root + "-member"
    reader_name = root + "-reader"
    outsider_name = root + "-outsider"
    _run(
        database_url,
        "insert into public.ab_members(root,username,uid) values ("
        f"{_quote(root)}, {_quote(reader_name)}, {_quote(str(reader))}::uuid), ("
        f"{_quote(root)}, {_quote(outsider_name)}, {_quote(str(outsider))}::uuid);"
        " insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'chats/{chat}/meta.json', "
        "pg_catalog.jsonb_build_object('members',"
        f"pg_catalog.jsonb_build_object({_quote(actor_name)},'{{}}'::jsonb,"
        f"{_quote(reader_name)},'{{}}'::jsonb)), false);",
    )

    object_name = f"{root}/blob-stage/{chat}/{blob}"
    _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "insert into storage.objects("
        "id,bucket_id,name,owner_id,metadata,version) values ("
        f"{_quote(str(object_id))}::uuid, 'ab-mesh', {_quote(object_name)}, "
        f"{_quote(str(actor))}, '{{\"size\":{size}}}'::jsonb, 'fixture'); "
        "commit;",
    )
    denied_upload = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(outsider)
        + "insert into storage.objects("
        "id,bucket_id,name,owner_id,metadata,version) values ("
        f"{_quote(str(uuid4()))}::uuid, 'ab-mesh', "
        f"{_quote(root + '/blob-stage/' + chat + '/outsider')}, "
        f"{_quote(str(outsider))}, '{{\"size\":9}}'::jsonb, 'v1'); commit;",
        expect_ok=False,
    )
    if "row-level security" not in denied_upload.stderr.lower():
        raise GateFailure("non-room member uploaded a staged object")

    def visible(uid: UUID, name: str = object_name) -> int:
        return int(_scalar(
            database_url,
            "begin; set local role authenticated; " + _claims(uid)
            + "select count(*) from storage.objects where bucket_id='ab-mesh' "
            f"and name={_quote(name)}; commit;",
        ))

    if visible(actor) != 1 or visible(reader) != 0 or visible(outsider) != 0:
        raise GateFailure("unpublished staged-object visibility is not owner-only")

    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])
    operation_id = uuid4()
    client_digest = "1" * 64
    sealed_sha256 = "2" * 64
    manifest_digest = "3" * 64
    call = _blob_manifest_sql(
        root, epoch, operation_id, client_digest, chat, blob, object_id, size,
        sealed_sha256, manifest_digest,
    )
    receipt = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call + "; commit;",
    ))
    replay = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call + "; commit;",
    ))
    if replay != receipt or receipt["result"] != {
            "blob_id": blob, "chat_id": chat,
            "object_id": str(object_id), "sealed_bytes": size}:
        raise GateFailure("typed blob manifest exact replay changed")

    collision = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _blob_manifest_sql(
            root, epoch, operation_id, client_digest, chat, blob, object_id,
            size, "4" * 64, manifest_digest,
        ) + "; commit;",
        expect_ok=False,
    )
    if "collides with existing receipt" not in collision.stderr:
        raise GateFailure("blob operation digest collision was accepted")

    duplicate_id = uuid4()
    duplicate = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _blob_manifest_sql(
            root, epoch, duplicate_id, "5" * 64, chat, blob, object_id,
            size, sealed_sha256, manifest_digest,
        ) + "; commit;",
        expect_ok=False,
    )
    if "typed blob manifest already exists" not in duplicate.stderr:
        raise GateFailure("fresh duplicate blob manifest was accepted")

    wrong_owner_id = uuid4()
    right_id = uuid4()
    wrong_size_id = uuid4()
    wrong_cases = (
        ("wrong-owner", reader, wrong_owner_id, 11, wrong_owner_id, 11),
        ("wrong-id", actor, right_id, 12, uuid4(), 12),
        ("wrong-size", actor, wrong_size_id, 13, wrong_size_id, 14),
    )
    rejected_ids: list[UUID] = [duplicate_id]
    for index, (candidate, owner, stored_id, stored_size,
                requested_id, requested_size) in enumerate(wrong_cases):
        _run(
            database_url,
            "insert into storage.objects("
            "id,bucket_id,name,owner_id,metadata,version) values ("
            f"{_quote(str(stored_id))}::uuid, 'ab-mesh', "
            f"{_quote(root + '/blob-stage/' + chat + '/' + candidate)}, "
            f"{_quote(str(owner))}, '{{\"size\":{stored_size}}}'::jsonb, "
            "'fixture');",
        )
        rejected_id = uuid4()
        rejected_ids.append(rejected_id)
        rejected = _run(
            database_url,
            "begin; set local role authenticated; " + _claims(actor)
            + _blob_manifest_sql(
                root, epoch, rejected_id, str(6 + index) * 64,
                chat, candidate, requested_id, requested_size,
                sealed_sha256, manifest_digest,
            ) + "; commit;",
            expect_ok=False,
        )
        if "typed blob manifest object is unavailable" not in rejected.stderr:
            raise GateFailure(f"{candidate} Storage mismatch was accepted")

    if visible(actor) != 1 or visible(reader) != 1 or visible(outsider) != 0:
        raise GateFailure("published staged-object visibility is not room-bound")
    updated = int(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "with changed as (update storage.objects set version='forbidden' "
        f"where bucket_id='ab-mesh' and name={_quote(object_name)} returning 1) "
        "select count(*) from changed; commit;",
    ))
    if updated:
        raise GateFailure("authenticated staged-object update was admitted")

    replacement_id = uuid4()
    _run(
        database_url,
        "delete from storage.objects where bucket_id='ab-mesh' and name="
        f"{_quote(object_name)}; insert into storage.objects("
        "id,bucket_id,name,owner_id,metadata,version) values ("
        f"{_quote(str(replacement_id))}::uuid, 'ab-mesh', "
        f"{_quote(object_name)}, {_quote(str(actor))}, "
        f"'{{\"size\":{size}}}'::jsonb, 'replacement');",
    )
    if visible(actor) != 0 or visible(reader) != 0:
        raise GateFailure("replacement object matched an older manifest")

    pending_blob = "after-removal"
    pending_object_id = uuid4()
    _run(
        database_url,
        "insert into storage.objects("
        "id,bucket_id,name,owner_id,metadata,version) values ("
        f"{_quote(str(pending_object_id))}::uuid, 'ab-mesh', "
        f"{_quote(root + '/blob-stage/' + chat + '/' + pending_blob)}, "
        f"{_quote(str(actor))}, '{{\"size\":15}}'::jsonb, 'v-pending');",
    )
    remove_id = uuid4()
    removed_meta = {"members": {reader_name: {}}}
    _scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + "select public.ab_mutation_doc_put("
        f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
        f"{_quote(str(remove_id))}::uuid, {_quote('9' * 64)}, "
        f"'chats/{chat}/meta.json', {_jsonb(removed_meta)}); commit;",
    )
    replay_after_removal = json.loads(_scalar(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + call + "; commit;",
    ))
    if replay_after_removal != receipt:
        raise GateFailure("blob receipt replay failed after room removal")
    denied_id = uuid4()
    rejected_ids.append(denied_id)
    denied = _run(
        database_url,
        "begin; set local role authenticated; " + _claims(actor)
        + _blob_manifest_sql(
            root, epoch, denied_id, "a" * 64, chat, pending_blob,
            pending_object_id, 15, sealed_sha256, manifest_digest,
        ) + "; commit;",
        expect_ok=False,
    )
    if "typed blob manifest is unauthorized" not in denied.stderr:
        raise GateFailure("removed room member published a fresh blob manifest")
    rejected_values = ",".join(
        f"{_quote(str(value))}::uuid" for value in rejected_ids)
    if int(_scalar(
        database_url,
        "select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)} and operation_id in ({rejected_values});",
    )):
        raise GateFailure("rejected blob operations retained receipts")


def _validate_p1b_blob_manifest_lock(database_url: str) -> None:
    root = "typed-blob-lock"
    chat = "room"
    blob = "same-blob"
    object_id = uuid4()
    size = 23
    actor = uuid4()
    before = _prepare_root(database_url, root, actor)
    member = root + "-member"
    _run(
        database_url,
        "insert into public.ab_docs(root,path,data,deleted) values ("
        f"{_quote(root)}, 'chats/{chat}/meta.json', "
        "pg_catalog.jsonb_build_object('members',"
        f"pg_catalog.jsonb_build_object({_quote(member)},'{{}}'::jsonb)), false);"
        " insert into storage.objects("
        "id,bucket_id,name,owner_id,metadata,version) values ("
        f"{_quote(str(object_id))}::uuid, 'ab-mesh', "
        f"{_quote(root + '/blob-stage/' + chat + '/' + blob)}, "
        f"{_quote(str(actor))}, '{{\"size\":{size}}}'::jsonb, "
        "'fixture');",
    )
    sealed = json.loads(_scalar(
        database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed["protocol_epoch"])
    holder_id = uuid4()
    waiter_id = uuid4()
    holder_client = "b" * 64
    sealed_sha256 = "c" * 64
    manifest_digest = "d" * 64
    provider_digest = _scalar(
        database_url,
        "select private.ab_mutation_provider_digest("
        "pg_catalog.jsonb_build_array('blob_manifest',"
        f"{_quote(chat)}, {_quote(blob)}, {_quote(str(object_id))}::uuid, {size}, "
        f"{_quote(sealed_sha256)}, {_quote(manifest_digest)}));",
    )
    suffix = uuid4().hex[:8]
    holder_name = f"ab-blob-holder-{suffix}"
    waiter_name = f"ab-blob-waiter-{suffix}"
    holder_ready = f"blob-holder-ready-{suffix}"
    holder_done = f"blob-holder-done-{suffix}"
    lock_key = (
        "pg_catalog.hashtextextended("
        "'agentbridge-blob-manifest:' || "
        "pg_catalog.jsonb_build_array("
        f"{_quote(root)}, {_quote(chat)}, {_quote(blob)})::text, 0)"
    )
    holder = _PsqlSession(database_url)
    waiter = _PsqlSession(database_url)
    try:
        holder.send(
            f"set application_name = {_quote(holder_name)}; begin; "
            + _claims(actor)
            + _begin_sql(
                root, epoch, holder_id, "blob_manifest",
                holder_client, provider_digest,
            )
            + f"; select private.ab_mutation_lock_actor({_quote(root)}); "
            + "select private.ab_mutation_lock_chat("
            + f"{_quote(root)}, {_quote(chat)}, false); "
            + f"select pg_catalog.pg_advisory_xact_lock({lock_key}); "
            + f"select {_quote(holder_ready)};"
        )
        holder.wait_marker(holder_ready)
        waiter.send(
            f"set application_name = {_quote(waiter_name)}; begin; "
            + _claims(actor)
            + _blob_manifest_sql(
                root, epoch, waiter_id, "e" * 64, chat, blob, object_id,
                size, sealed_sha256, manifest_digest,
            ) + "; commit;"
        )
        _wait_for_lock_order(
            database_url,
            root=root,
            holder=holder,
            holder_name=holder_name,
            holder_mode="ExclusiveLock",
            waiter=waiter,
            waiter_name=waiter_name,
            waiter_mode="ExclusiveLock",
            lock_key_sql=lock_key,
        )
        result = json.dumps({
            "blob_id": blob, "chat_id": chat,
            "object_id": str(object_id), "sealed_bytes": size,
        }, separators=(",", ":"), sort_keys=True)
        holder.send(
            "insert into public.ab_blob_manifests("
            "root,protocol_epoch,operation_id,chat_id,blob_id,actor_uid,"
            "object_id,sealed_bytes,sealed_sha256,manifest_digest) values ("
            f"{_quote(root)}, {_quote(str(epoch))}::uuid, "
            f"{_quote(str(holder_id))}::uuid, {_quote(chat)}, {_quote(blob)}, "
            f"{_quote(str(actor))}::uuid, {_quote(str(object_id))}::uuid, {size}, "
            f"{_quote(sealed_sha256)}, {_quote(manifest_digest)}); "
            + _finish_sql(
                root, epoch, holder_id, "blob_manifest",
                holder_client, provider_digest, result,
            )
            + f"; commit; select {_quote(holder_done)};"
        )
        holder.wait_marker(holder_done)
        holder.finish()
        waiter_error = waiter.finish(expect_ok=False)
        if "typed blob manifest already exists" not in waiter_error:
            raise GateFailure("blob-key waiter did not reject committed winner")
    finally:
        holder.abort()
        waiter.abort()

    counts = _scalar(
        database_url,
        "select ((select count(*) from public.ab_mutation_receipts where root = "
        f"{_quote(root)})::text || ':' || "
        "(select count(*) from public.ab_blob_manifests where root = "
        f"{_quote(root)} and chat_id={_quote(chat)} and blob_id={_quote(blob)})"
        "::text);",
    )
    if counts != "1:1":
        raise GateFailure("blob-key contention produced duplicate evidence")


def _validate_conditional_documents(database_url: str) -> None:
    root = 'typed-conditional-document'
    actor = uuid4()
    initial = _prepare_root(database_url, root, actor)
    sealed = json.loads(_scalar(database_url, _seal_sql(root, initial, uuid4())))
    epoch = UUID(sealed['protocol_epoch'])
    source = UUID(sealed['source_epoch'])

    def call(path, seq, *, deleted=False, source_epoch=source, operation=None):
        return ('select public.ab_mutation_doc_compare_put('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(operation or uuid4()))}::uuid, {_quote("c" * 64)}, '
                f'{_quote(path)}, \'{{"cas":true}}\'::jsonb, '
                f'{_quote(str(source_epoch))}::uuid, {seq}, '
                f'{"true" if deleted else "false"})')

    def run(sql):
        return json.loads(_scalar(database_url, 'begin; set local role authenticated; '
                                 + _claims(actor) + sql + '; commit;'))

    def sequence(path):
        return int(_scalar(database_url, 'select seq from public.ab_docs where root='
                           f'{_quote(root)} and path={_quote(path)};'))

    absent = call('doc.json', 0)
    receipt = run(absent)
    if receipt['result'] != {'path': 'doc.json', 'applied': True}:
        raise GateFailure('conditional absent insert failed')
    seq = sequence('doc.json')
    conflict_id = uuid4()
    conflict = call('doc.json', 0, operation=conflict_id)
    conflict_receipt = run(conflict)
    if conflict_receipt['result']['applied'] is not False or run(conflict) != conflict_receipt:
        raise GateFailure('conditional conflict is not an exact terminal replay')
    collision = _run(database_url, 'begin; set local role authenticated; '
                     + _claims(actor) + call('doc.json', seq, operation=conflict_id)
                     + '; commit;', expect_ok=False)
    if 'collides with existing receipt' not in collision.stderr:
        raise GateFailure('conditional provider digest did not bind observed version')
    if run(call('doc.json', seq))['result']['applied'] is not True:
        raise GateFailure('conditional existing replacement failed')
    current = sequence('doc.json')
    if current == seq or run(absent) != receipt or sequence('doc.json') != current:
        raise GateFailure('old conditional receipt did not replay exactly')
    if run(call('doc.json', seq))['result']['applied'] is not False:
        raise GateFailure('conditional stale sequence overwrote newer value')
    if run(call('doc.json', current, source_epoch=uuid4()))['result']['applied'] is not False:
        raise GateFailure('conditional obsolete source epoch was applied')
    run('select public.ab_mutation_doc_delete('
        f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(str(uuid4()))}::uuid, '
        f'{_quote("d" * 64)}, \'doc.json\')')
    tombstone = sequence('doc.json')
    if run(call('doc.json', tombstone))['result']['applied'] is not False:
        raise GateFailure('conditional replacement ignored tombstone state')
    if run(call('doc.json', tombstone, deleted=True))['result']['applied'] is not True:
        raise GateFailure('conditional tombstone replacement failed')

    # A held unconditional write and a separate READ COMMITTED CAS backend.
    # The blocked CAS must recheck after commit, for both a row and an absent key.
    for mode in ('put', 'create', 'delete'):
        path = mode + '.json'
        seq = 0
        if mode != 'create':
            run(call(path, 0))
            seq = sequence(path)
        suffix = uuid4().hex[:10]
        holder_name, waiter_name = 'cas-holder-' + suffix, 'cas-waiter-' + suffix
        ready, done = 'cas-ready-' + suffix, 'cas-done-' + suffix
        holder, waiter = _PsqlSession(database_url), _PsqlSession(database_url)
        operation = uuid4()
        cas_sql = call(path, seq, operation=operation)
        old_sql = (f'select public.ab_mutation_doc_{mode}('
                   f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                   f'{_quote(str(uuid4()))}::uuid, {_quote("e" * 64)}, {_quote(path)}'
                   + ('' if mode == 'delete' else ', \'{"newer":true}\'::jsonb') + ')')
        try:
            holder.send(f'set application_name={_quote(holder_name)}; begin; '
                        'set local role authenticated; ' + _claims(actor)
                        + old_sql + f'; select {_quote(ready)};')
            holder.wait_marker(ready)
            waiter.send(f'set application_name={_quote(waiter_name)}; begin; '
                        'set local role authenticated; ' + _claims(actor)
                        + cas_sql + f'; commit; select {_quote(done)};')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                blocked = _scalar(database_url,
                    'select exists(select 1 from pg_catalog.pg_stat_activity h '
                    'join pg_catalog.pg_stat_activity w on '
                    'h.pid = any(pg_catalog.pg_blocking_pids(w.pid)) '
                    f'where h.application_name={_quote(holder_name)} '
                    f'and w.application_name={_quote(waiter_name)});')
                if blocked == 't':
                    break
                time.sleep(0.025)
            else:
                raise GateFailure('conditional writer did not wait for existing ' + mode)
            holder.send('commit;')
            waiter.wait_marker(done)
            holder.finish()
            waiter.finish()
        finally:
            holder.abort()
            waiter.abort()
        if run(cas_sql)['result']['applied'] is not False:
            raise GateFailure('conditional writer overwrote concurrent ' + mode)

    # Receipt replay survives current membership removal; new work does not.
    run('select public.ab_mutation_member_leave('
        f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(str(uuid4()))}::uuid, '
        f'{_quote("f" * 64)})')
    if run(absent) != receipt or run(conflict) != conflict_receipt:
        raise GateFailure('conditional replay incorrectly required fresh membership')
    denied = _run(database_url, 'begin; set local role authenticated; '
                  + _claims(actor) + call('denied.json', 0) + '; commit;', expect_ok=False)
    if 'conditional document put is unauthorized' not in denied.stderr:
        raise GateFailure('conditional fresh write ignored membership removal')


def _validate_meta_append(database_url: str) -> None:
    """A membership swap and its events commit together, under pre-swap authority."""
    root, actor, chat = 'typed-meta-append', uuid4(), 'room'
    member, log = root + '-member', root + '-member|m1'
    meta_path = f'chats/{chat}/meta.json'
    initial = _prepare_root(database_url, root, actor)
    stranger = uuid4()  # a root member who was never in the chat
    _run(database_url, 'insert into public.ab_members(root, username, uid) values ('
         f'{_quote(root)}, {_quote(root + "-stranger")}, {_quote(str(stranger))});')
    _run(database_url, 'insert into public.ab_docs(root,path,data) values ('
         f'{_quote(root)}, {_quote(meta_path)}, '
         f'{_jsonb({"members": {member: {}, "peer": {}}})});')
    sealed = json.loads(_scalar(database_url, _seal_sql(root, initial, uuid4())))
    epoch, source = UUID(sealed['protocol_epoch']), UUID(sealed['source_epoch'])

    def record(n):
        return {'id': f'e{n}', 'ns': n, 'kind': 'info', 'event': {'type': 'member_left'}}

    def call(seq, members, records, *, operation=None, source_epoch=source,
             deleted=False, extra=None):
        return ('select public.ab_mutation_meta_append('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(operation or uuid4()))}::uuid, {_quote("d" * 64)}, '
                f'{_quote(chat)}, {_jsonb({"members": members, **(extra or {})})}, '
                f'{_quote(str(source_epoch))}::uuid, {seq}, '
                f'{"true" if deleted else "false"}, {_quote(log)}, '
                f'{_quote(json.dumps(records))}::jsonb)')

    def run(sql, uid=actor):
        return json.loads(_scalar(database_url, 'begin; set local role authenticated; '
                                 + _claims(uid) + sql + '; commit;'))

    def refused(sql, uid=actor):
        return _run(database_url, 'begin; set local role authenticated; '
                    + _claims(uid) + sql + '; commit;', expect_ok=False).stderr

    def state():
        return json.loads(_scalar(database_url,
            "select jsonb_build_object('seq', d.seq, 'data', d.data, 'lines', "
            "coalesce((select jsonb_agg(l.line::jsonb order by l.id) from public.ab_logs l "
            f"where l.root={_quote(root)} and l.chat_id={_quote(chat)}), '[]'::jsonb)) "
            f"from public.ab_docs d where d.root={_quote(root)} and d.path={_quote(meta_path)};"))

    start = state()
    seq = start['seq']
    for bad in (call(0, {}, [record(1)]), call(seq, {}, []),
                call(seq, {}, [record(1)], deleted=True),
                call(seq, {}, [record(1), 'not-an-object']),
                call(seq, {}, [record(n) for n in range(257)])):
        if 'invalid meta append' not in refused(bad) or state() != start:
            raise GateFailure('meta append accepted an invalid request')
    # A stale version or source epoch changes nothing and replays exactly.
    stale = call(seq + 1, {}, [record(1)])
    stale_receipt = run(stale)
    if (stale_receipt['result'] != {'chat_id': chat, 'log_name': log, 'applied': False}
            or run(stale) != stale_receipt or state() != start):
        raise GateFailure('stale meta append wrote or did not replay')
    if run(call(seq, {}, [record(1)], source_epoch=uuid4()))['result']['applied'] \
            or state() != start:
        raise GateFailure('meta append crossed the observed source epoch')
    # An aborted transaction leaves neither half nor a receipt.
    aborted = call(seq, {'peer': {}}, [record(1)])
    _run(database_url, 'begin; set local role authenticated; ' + _claims(actor)
         + aborted + '; rollback;')
    if state() != start:
        raise GateFailure('aborted meta append leaked a write')
    # A root member who was never in the chat gets no answer, even for a
    # version that moved on.
    if 'meta append is unauthorized' not in refused(call(seq + 1, {}, [record(1)]),
                                                    uid=stranger):
        raise GateFailure('meta append answered a principal outside the chat')
    # The member leaves: meta and both records land together, in order. The
    # fold keeps the leaver in tenure.
    operation = uuid4()
    tenure = {member: [[1, 3]], 'peer': [[1]]}
    leave = call(seq, {'peer': {}}, [record(2), record(3)], operation=operation,
                 extra={'tenure': tenure})
    receipt = run(leave)
    after = state()
    if (receipt['result'] != {'chat_id': chat, 'log_name': log, 'applied': True}
            or after['data'] != {'members': {'peer': {}}, 'tenure': tenure}
            or after['seq'] == seq or after['lines'] != [record(2), record(3)]):
        raise GateFailure('meta append did not swap and append atomically')
    collision = refused(call(seq, {'peer': {}}, [record(2)], operation=operation))
    if 'collides with existing receipt' not in collision:
        raise GateFailure('meta append digest omitted its records')
    # Replay precedes authority: the former member recovers the exact receipt
    # without a second append; fresh work is refused.
    if run(leave) != receipt or run(stale) != stale_receipt or state() != after:
        raise GateFailure('meta append replay required membership or re-appended')
    if 'meta append is unauthorized' not in refused(
            call(after['seq'], {'peer': {}, member: {}}, [record(4)])):
        raise GateFailure('a former member could still swap the meta')
    # An attempt that lost a race to the leaver's own departure settles: the
    # version moved on and the leaver is in tenure, so applied=false.
    lost = run(call(seq, {'peer': {}, member: {}}, [record(5)]))
    if (lost['result'] != {'chat_id': chat, 'log_name': log, 'applied': False}
            or state() != after):
        raise GateFailure('a race lost to removal did not settle as applied=false')

    # A concurrent swap holding the exclusive chat lock: the waiting append
    # rechecks the version after the holder commits and appends nothing.
    root2, actor2 = 'typed-meta-append-race', uuid4()
    member2 = root2 + '-member'
    initial2 = _prepare_root(database_url, root2, actor2)
    _run(database_url, 'insert into public.ab_docs(root,path,data) values ('
         f'{_quote(root2)}, {_quote(meta_path)}, {_jsonb({"members": {member2: {}}})});')
    sealed2 = json.loads(_scalar(database_url, _seal_sql(root2, initial2, uuid4())))
    epoch2, source2 = UUID(sealed2['protocol_epoch']), UUID(sealed2['source_epoch'])
    seq2 = int(_scalar(database_url, 'select seq from public.ab_docs where root='
                       f'{_quote(root2)} and path={_quote(meta_path)};'))
    holder_sql = ('select public.ab_mutation_doc_compare_put('
                  f'{_quote(root2)}, {_quote(str(epoch2))}::uuid, '
                  f'{_quote(str(uuid4()))}::uuid, {_quote("a" * 64)}, {_quote(meta_path)}, '
                  f'{_jsonb({"members": {member2: {}, "late": {}}})}, '
                  f'{_quote(str(source2))}::uuid, {seq2}, false)')
    waiter_sql = ('select public.ab_mutation_meta_append('
                  f'{_quote(root2)}, {_quote(str(epoch2))}::uuid, '
                  f'{_quote(str(uuid4()))}::uuid, {_quote("b" * 64)}, {_quote(chat)}, '
                  f'{_jsonb({"members": {}})}, {_quote(str(source2))}::uuid, {seq2}, false, '
                  f'{_quote(member2 + "|m1")}, {_quote(json.dumps([record(9)]))}::jsonb)')
    suffix = uuid4().hex[:10]
    holder_name, waiter_name = 'meta-holder-' + suffix, 'meta-waiter-' + suffix
    ready, done = 'meta-ready-' + suffix, 'meta-done-' + suffix
    holder, waiter = _PsqlSession(database_url), _PsqlSession(database_url)
    try:
        holder.send(f'set application_name={_quote(holder_name)}; begin; '
                    'set local role authenticated; ' + _claims(actor2)
                    + holder_sql + f'; select {_quote(ready)};')
        holder.wait_marker(ready)
        waiter.send(f'set application_name={_quote(waiter_name)}; begin; '
                    'set local role authenticated; ' + _claims(actor2)
                    + waiter_sql + f'; commit; select {_quote(done)};')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blocked = _scalar(database_url,
                'select exists(select 1 from pg_catalog.pg_stat_activity h '
                'join pg_catalog.pg_stat_activity w on '
                'h.pid = any(pg_catalog.pg_blocking_pids(w.pid)) '
                f'where h.application_name={_quote(holder_name)} '
                f'and w.application_name={_quote(waiter_name)});')
            if blocked == 't':
                break
            time.sleep(0.025)
        else:
            raise GateFailure('meta append did not wait for a concurrent meta swap')
        holder.send('commit;')
        waiter.wait_marker(done)
        holder.finish()
        waiter.finish()
    finally:
        holder.abort()
        waiter.abort()
    lines = _scalar(database_url, 'select count(*) from public.ab_logs where root='
                    f'{_quote(root2)};')
    members = _scalar(database_url, "select data->'members' ? 'late' from public.ab_docs "
                      f'where root={_quote(root2)} and path={_quote(meta_path)};')
    if lines != '0' or members != 't':
        raise GateFailure('meta append overwrote a concurrent swap or appended anyway')


def _validate_reaction_pairs(database_url: str) -> None:
    """Real row/unique-key races must never expose half a checkpoint pair."""
    root, actor, chat = 'typed-reaction-pair', uuid4(), 'room'
    initial = _prepare_root(database_url, root, actor)
    member = root + '-member'
    _run(database_url, 'insert into public.ab_docs(root,path,data) values ('
         f'{_quote(root)}, {_quote("chats/" + chat + "/meta.json")}, '
         f'{_quote(json.dumps({"members": {member: {}}}))}::jsonb);')
    sealed = json.loads(_scalar(database_url, _seal_sql(root, initial, uuid4())))
    epoch, source = UUID(sealed['protocol_epoch']), UUID(sealed['source_epoch'])

    def path(user, document):
        return f'chats/{chat}/overlays/{document}/{user}.json'

    def call(user, state_seq=0, reaction_seq=0, *, state_deleted=False,
             reaction_deleted=False, source_epoch=source, operation=None, value=1):
        return ('select public.ab_mutation_reaction_pair('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(operation or uuid4()))}::uuid, {_quote("a" * 64)}, '
                f'{_quote(chat)}, {_quote(user)}, '
                f'{_quote(json.dumps({"state": value}))}::jsonb, '
                f'{_quote(json.dumps({"reaction": value}))}::jsonb, '
                f'{_quote(str(source_epoch))}::uuid, {state_seq}, '
                f'{"true" if state_deleted else "false"}, {reaction_seq}, '
                f'{"true" if reaction_deleted else "false"})')

    def run(sql):
        return json.loads(_scalar(database_url, 'begin; set local role authenticated; '
                                 + _claims(actor) + sql + '; commit;'))

    def snapshot(user):
        return json.loads(_scalar(database_url,
            "select coalesce(jsonb_object_agg(path,jsonb_build_object("
            "'seq',seq,'deleted',deleted,'data',data)), '{}'::jsonb) "
            f'from public.ab_docs where root={_quote(root)} and path in ('
            f'{_quote(path(user, "state"))},{_quote(path(user, "reactions"))});'))

    def versions(user):
        rows = snapshot(user)
        return tuple(rows.get(path(user, document), {}).get('seq', 0)
                     for document in ('state', 'reactions'))

    def ordinary(mode, document_path):
        return (f'select public.ab_mutation_doc_{mode}('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(uuid4()))}::uuid, {_quote("b" * 64)}, '
                + _quote(document_path)
                + ('' if mode == 'delete' else ', \'{"newer":true}\'::jsonb') + ')')

    exact = call('alice')
    first = run(exact)
    if first['result'] != {'chat_id': chat, 'username': 'alice', 'applied': True}:
        raise GateFailure('reaction pair absent creation failed')
    original = snapshot('alice')
    if len(original) != 2 or run(exact) != first or snapshot('alice') != original:
        raise GateFailure('reaction pair replay mutated either document')
    state_seq, reaction_seq = versions('alice')
    conflict = call('alice', state_seq + 1, reaction_seq, value=2)
    conflict_receipt = run(conflict)
    if (conflict_receipt['result']['applied'] is not False
            or run(conflict) != conflict_receipt or snapshot('alice') != original):
        raise GateFailure('second comparison did not roll back first pair update')
    # The first insert, rather than only an update, must roll back as well.
    run(ordinary('put', path('insert-rollback', 'state')))
    before = snapshot('insert-rollback')
    if (run(call('insert-rollback'))['result']['applied'] is not False
            or snapshot('insert-rollback') != before):
        raise GateFailure('second comparison left a newly inserted reaction row')
    if (run(call('alice', state_seq, reaction_seq, source_epoch=uuid4()))['result']['applied']
            or snapshot('alice') != original):
        raise GateFailure('reaction pair crossed the observed source epoch')
    collision = _run(database_url, 'begin; set local role authenticated; '
                     + _claims(actor) + exact.replace('{"state": 1}', '{"state": 2}')
                     + '; commit;', expect_ok=False)
    if 'collides with existing receipt' not in collision.stderr:
        raise GateFailure('reaction pair digest omitted checkpoint content')
    # An outer abort removes both writes and the receipt, allowing exact retry.
    abort_sql = call('abort')
    _run(database_url, 'begin; set local role authenticated; ' + _claims(actor)
         + abort_sql + '; rollback;')
    if snapshot('abort') or not run(abort_sql)['result']['applied']:
        raise GateFailure('aborted pair leaked writes or a terminal receipt')
    # Tombstone versions cannot be mistaken for live rows.
    for document in ('state', 'reactions'):
        run(ordinary('delete', path('alice', document)))
    state_seq, reaction_seq = versions('alice')
    before = snapshot('alice')
    if (run(call('alice', state_seq, reaction_seq))['result']['applied']
            or snapshot('alice') != before):
        raise GateFailure('reaction pair ignored observed deletion state')
    if not run(call('alice', state_seq, reaction_seq,
                    state_deleted=True, reaction_deleted=True))['result']['applied']:
        raise GateFailure('reaction pair could not replace exact tombstones')

    def contend(holder_sql, waiter_sql):
        suffix = uuid4().hex[:10]
        holder_name, waiter_name = 'pair-holder-' + suffix, 'pair-waiter-' + suffix
        ready, done = 'pair-ready-' + suffix, 'pair-done-' + suffix
        holder, waiter = _PsqlSession(database_url), _PsqlSession(database_url)
        try:
            holder.send(f'set application_name={_quote(holder_name)}; begin; '
                        'set local role authenticated; ' + _claims(actor)
                        + holder_sql + f'; select {_quote(ready)};')
            holder.wait_marker(ready)
            waiter.send(f'set application_name={_quote(waiter_name)}; begin; '
                        'set local role authenticated; ' + _claims(actor)
                        + waiter_sql + f'; commit; select {_quote(done)};')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                blocked = _scalar(database_url,
                    'select exists(select 1 from pg_catalog.pg_stat_activity h '
                    'join pg_catalog.pg_stat_activity w on '
                    'h.pid = any(pg_catalog.pg_blocking_pids(w.pid)) '
                    f'where h.application_name={_quote(holder_name)} '
                    f'and w.application_name={_quote(waiter_name)});')
                if blocked == 't':
                    break
                time.sleep(0.025)
            else:
                raise GateFailure('reaction pair failed to reach the held row/key barrier')
            holder.send('commit;')
            waiter.wait_marker(done)
            holder.finish()
            waiter.finish()
        finally:
            holder.abort()
            waiter.abort()
        if run(waiter_sql)['result']['applied'] is not False:
            raise GateFailure('reaction pair overwrote a concurrently replaced version')

    for document in ('state', 'reactions'):
        other = 'reactions' if document == 'state' else 'state'
        for mode in ('put', 'create', 'delete'):
            user = document + '-' + mode
            if mode == 'create':
                run(ordinary('put', path(user, other)))
            else:
                run(call(user))
            before = snapshot(user)
            state_seq, reaction_seq = versions(user)
            waiter_sql = call(user, state_seq, reaction_seq, value=9)
            contend(ordinary(mode, path(user, document)), waiter_sql)
            after = snapshot(user)
            if after[path(user, other)] != before[path(user, other)]:
                raise GateFailure('losing pair changed its uncontended document')
            if mode == 'delete' and not after[path(user, document)]['deleted']:
                raise GateFailure('losing pair resurrected a concurrently deleted document')
            if after[path(user, document)]['data'] != {'newer': True} and mode != 'delete':
                raise GateFailure('losing pair changed the winning ordinary document')
    run(call('pair-race'))
    state_seq, reaction_seq = versions('pair-race')
    winning = call('pair-race', state_seq, reaction_seq, value=7)
    contend(winning, call('pair-race', state_seq, reaction_seq, value=9))
    after = snapshot('pair-race')
    if (after[path('pair-race', 'state')]['data'] != {'state': 7}
            or after[path('pair-race', 'reactions')]['data'] != {'reaction': 7}):
        raise GateFailure('pair/pair race exposed a mixed checkpoint')

    run('select public.ab_mutation_member_leave('
        f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(str(uuid4()))}::uuid, '
        f'{_quote("f" * 64)})')
    if run(exact) != first or run(conflict) != conflict_receipt:
        raise GateFailure('reaction pair replay incorrectly required fresh membership')
    denied = _run(database_url, 'begin; set local role authenticated; '
                  + _claims(actor) + call('denied') + '; commit;', expect_ok=False)
    if 'reaction pair is unauthorized' not in denied.stderr:
        raise GateFailure('fresh reaction pair ignored membership removal')


def _validate_receipt_retirement(database_url: str) -> None:
    """Closure metadata survives removal, rotation replay, and skipped epochs."""
    root, actor = 'receipt-retirement', uuid4()
    legacy = _prepare_root(database_url, root, actor)
    observation_sql = ("begin; set local role authenticated; " + _claims(actor)
                       + f"select public.ab_mutation_epoch_state({_quote(root)}); commit;")
    unavailable = _run(database_url, observation_sql, expect_ok=False)
    if 'sealed mutation lifecycle is unavailable' not in unavailable.stderr:
        raise GateFailure('unsealed lifecycle observation was accepted')
    seal = json.loads(_scalar(database_url, _seal_sql(root, legacy, uuid4())))
    epoch = UUID(seal['protocol_epoch'])
    before = json.loads(_scalar(database_url, observation_sql))
    if (before['generation'] != 0 or before['protocol_epoch'] != str(epoch)
            or before['actor_id'] != str(actor) or before['sealed'] is not True
            or set(before) != {'schema_version', 'root', 'actor_id', 'role',
                               'lifecycle_id', 'protocol_epoch', 'generation', 'sealed'}):
        raise GateFailure('initial lifecycle observation changed its bounded contract')
    anonymous = _run(database_url, "begin; set local role anon; "
                     + f"select public.ab_mutation_epoch_state({_quote(root)}); commit;", expect_ok=False)
    if 'permission denied' not in anonymous.stderr:
        raise GateFailure('anonymous caller read epoch metadata')
    operation, client, provider = uuid4(), 'a' * 64, 'b' * 64
    _run(database_url, 'begin; ' + _claims(actor)
         + _begin_sql(root, epoch, operation, 'member_leave', client, provider)
         + f"; delete from public.ab_members where root={_quote(root)}; "
         + _finish_sql(root, epoch, operation, 'member_leave', client, provider, '{"left":true}')
         + '; commit;')
    rotation_id = uuid4()
    rotation_sql = ("select public.ab_admin_rotate_mutation_epoch("
                    f"{_quote(root)},{_quote(str(epoch))}::uuid,{_quote(str(rotation_id))}::uuid);")
    refused = _run(database_url, rotation_sql, expect_ok=False)
    if 'not fully acknowledged' not in refused.stderr:
        raise GateFailure('rotation deleted an unacknowledged receipt')
    _run(database_url, 'begin; set local role authenticated; ' + _claims(actor)
         + _receipt_sql('ab_ack_mutation_receipt', root, epoch, operation, client, provider)
         + '; commit;')
    rotated = json.loads(_scalar(database_url, rotation_sql))
    if json.loads(_scalar(database_url, rotation_sql)) != rotated:
        raise GateFailure('rotation response-loss replay changed')
    after = json.loads(_scalar(database_url, observation_sql))
    if (after['generation'] != 1 or after['lifecycle_id'] != before['lifecycle_id']
            or after['protocol_epoch'] != rotated['protocol_epoch']):
        raise GateFailure('removed actor could not observe exact closed lineage')
    # A second closure is enough; the client need not retain every transition.
    second_rotation = ("select public.ab_admin_rotate_mutation_epoch("
                       f"{_quote(root)},{_quote(after['protocol_epoch'])}::uuid,{_quote(str(uuid4()))}::uuid);")
    _run(database_url, second_rotation)
    later = json.loads(_scalar(database_url, observation_sql))
    if later['generation'] != 2 or later['lifecycle_id'] != before['lifecycle_id']:
        raise GateFailure('multiple closures lost monotonic lineage')
    stale = _run(database_url, 'begin; ' + _claims(actor)
                 + _begin_sql(root, epoch, operation, 'member_leave', client, provider)
                 + '; commit;', expect_ok=False)
    if 'epoch' not in stale.stderr:
        raise GateFailure('retired operation could become a new provider effect')
    _run(database_url, "update public.ab_mutation_roots set receipt_generation=9223372036854775807 "
         f"where root={_quote(root)};")
    exhausted = _run(database_url, "select public.ab_admin_rotate_mutation_epoch("
                     f"{_quote(root)},{_quote(later['protocol_epoch'])}::uuid,{_quote(str(uuid4()))}::uuid);", expect_ok=False)
    if 'generation exhausted' not in exhausted.stderr:
        raise GateFailure('epoch counter wrapped at exhaustion')
    final = json.loads(_scalar(database_url, observation_sql))
    if final['protocol_epoch'] != later['protocol_epoch']:
        raise GateFailure('failed exhausted rotation changed the current epoch')


def _validate_provider_admission(database_url: str) -> None:
    """Prospective admission reserves capacity and pins outcomes through closure."""
    root, actor = 'provider-admission', uuid4()
    before = _prepare_root(database_url, root, actor)
    sealed = json.loads(_scalar(database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed['protocol_epoch'])
    _run(database_url, f"select public.ab_admin_enable_mutation_admission({_quote(root)}, {_quote(str(epoch))}::uuid);")

    def authenticated(sql, *, ok=True):
        statement = 'begin; set local role authenticated; ' + _claims(actor) + sql + '; commit;'
        return _scalar(database_url, statement) if ok else _run(database_url, statement, expect_ok=False)

    def admit(owner, plan):
        return ('select public.ab_admit_mutation_owner('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(str(owner))}::uuid, '
                f'{_quote(json.dumps(plan))}::jsonb)')

    def leg(kind='doc_put'):
        return {'operation_id': str(uuid4()), 'kind': kind, 'client_digest': 'a' * 64}

    def cancel(owner, item, *, ack=False):
        name = 'ab_ack_mutation_admission_cancel' if ack else 'ab_cancel_mutation_admission'
        return (f'select public.{name}({_quote(root)}, {_quote(str(epoch))}::uuid, '
                f'{_quote(str(owner))}::uuid, {_quote(item["operation_id"])}::uuid, '
                f'{_quote(item["kind"])}, {_quote(item["client_digest"])})')

    def put(item, path):
        return ('select public.ab_mutation_doc_put('
                f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(item["operation_id"])}::uuid, '
                f'{_quote(item["client_digest"])}, {_quote(path)}, \'{{}}\'::jsonb)')

    def ack(receipt):
        return _receipt_sql('ab_ack_mutation_receipt', root, epoch,
                            UUID(receipt['operation_id']), receipt['client_digest'], receipt['provider_digest'])

    owner = uuid4()
    plan = [leg(), leg(), leg('member_leave')]
    admitted = json.loads(authenticated(admit(owner, plan)))
    expected = dict(schema_version=1, root=root, actor_id=str(actor), protocol_epoch=str(epoch),
                    owner_id=str(owner), status='admitted', plan=plan)
    if admitted != expected or json.loads(authenticated(admit(owner, plan))) != expected:
        raise GateFailure('admission lost-response replay changed immutable plan')
    altered = [dict(item) for item in plan]
    altered[0]['client_digest'] = 'b' * 64
    authenticated(admit(owner, altered), ok=False)
    # Option B: admission is optional per owner; a single-leg write takes a
    # free unreserved slot directly and must be acknowledged before closure.
    unadmitted = json.loads(authenticated(put(leg(), 'unadmitted.json')))
    first = json.loads(authenticated(put(plan[0], 'admitted.json')))
    if json.loads(authenticated(cancel(owner, plan[0])))['status'] != 'executed':
        raise GateFailure('cancellation fabricated non-effect after committed write')
    cancelled = json.loads(authenticated(cancel(owner, plan[1])))
    if cancelled['status'] != 'cancelled' or json.loads(authenticated(cancel(owner, plan[1]))) != cancelled:
        raise GateFailure('cancellation replay lost exact no-effect outcome')
    authenticated(put(plan[1], 'cancelled.json'), ok=False)
    rotation = (f'select public.ab_admin_rotate_mutation_epoch({_quote(root)}, '
                f'{_quote(str(epoch))}::uuid, {_quote(str(uuid4()))}::uuid);')
    _run(database_url, rotation, expect_ok=False)
    authenticated(ack(first))
    authenticated(ack(unadmitted))
    authenticated(cancel(owner, plan[1], ack=True))
    _run(database_url, rotation, expect_ok=False)  # Unused membership leg pins epoch.
    member = plan[2]
    left = json.loads(authenticated('select public.ab_mutation_member_leave('
                      f'{_quote(root)}, {_quote(str(epoch))}::uuid, '
                      f'{_quote(member["operation_id"])}::uuid, {_quote(member["client_digest"])})'))
    if json.loads(authenticated(admit(owner, plan))) != expected:
        raise GateFailure('admission replay incorrectly required current membership')
    authenticated(admit(uuid4(), [leg()]), ok=False)
    authenticated(ack(left))
    _run(database_url, rotation)
    authenticated(put(plan[0], 'admitted.json'), ok=False)
    if _scalar(database_url, f"select count(*) from public.ab_docs where root={_quote(root)} and path='cancelled.json';") != '0':
        raise GateFailure('cancelled admitted mutation changed source')
    if _scalar(database_url, f"select count(*) from public.ab_docs where root={_quote(root)} and path='unadmitted.json';") != '1':
        raise GateFailure('optional admission refused an unadmitted single-leg write')

    # A new authenticated principal can reserve its one claim, but not source
    # writes before membership exists.
    actor = uuid4()
    epoch = UUID(json.loads(_scalar(database_url,
        'begin; set local role authenticated; ' + _claims(actor)
        + f"select public.ab_mutation_epoch_state({_quote(root)}); commit;"))['protocol_epoch'])
    claim_owner, claim_plan = uuid4(), [leg('member_claim')]
    authenticated(admit(uuid4(), [leg()]), ok=False)
    authenticated(admit(claim_owner, claim_plan))
    claim = claim_plan[0]
    claimed = json.loads(authenticated('select public.ab_mutation_member_claim('
        f'{_quote(root)}, {_quote(str(epoch))}::uuid, {_quote(claim["operation_id"])}::uuid, '
        f"{_quote(claim['client_digest'])}, 'admission-new-member')"))
    if claimed['result'] != {'username': 'admission-new-member', 'claimed': True}:
        raise GateFailure('admission broke authenticated new-member claim')

    # An unaffiliated principal holds one reservation not yet settled for
    # closure and four claims per epoch, never the shared slot pool.
    actor = uuid4()
    pending_owner, pending_plan = uuid4(), [leg('member_claim')]
    pending = json.loads(authenticated(admit(pending_owner, pending_plan)))
    if json.loads(authenticated(admit(pending_owner, pending_plan))) != pending:
        raise GateFailure('unaffiliated admission bound blocked exact owner replay')
    authenticated(admit(uuid4(), [leg('member_claim')]), ok=False)
    authenticated(cancel(pending_owner, pending_plan[0]))
    authenticated(admit(uuid4(), [leg('member_claim')]), ok=False)  # unacknowledged
    authenticated(cancel(pending_owner, pending_plan[0], ack=True))
    for _ in range(3):
        claim_owner, claim_plan = uuid4(), [leg('member_claim')]
        if json.loads(authenticated(admit(claim_owner, claim_plan)))['status'] != 'admitted':
            raise GateFailure('settled unaffiliated reservation blocked a new claim')
        authenticated(cancel(claim_owner, claim_plan[0]))
        authenticated(cancel(claim_owner, claim_plan[0], ack=True))
    authenticated(admit(uuid4(), [leg('member_claim')]), ok=False)
    if _scalar(database_url, 'select count(*) from public.ab_mutation_admission_groups '
               f'where root={_quote(root)} and actor_uid={_quote(str(actor))}::uuid;') != '4':
        raise GateFailure('unaffiliated admission exceeded its per-epoch claim budget')

    # Whole owner capacity survives unrelated clients filling every other slot.
    root = 'provider-admission-full'
    actor, other_actor = uuid4(), uuid4()
    before = _prepare_root(database_url, root, actor)
    _run(database_url, 'insert into public.ab_members(root,username,uid) values ('
         f"{_quote(root)}, 'capacity-peer', {_quote(str(other_actor))}::uuid);")
    sealed = json.loads(_scalar(database_url, _seal_sql(root, before, uuid4())))
    epoch = UUID(sealed['protocol_epoch'])
    _run(database_url, f"select public.ab_admin_enable_mutation_admission({_quote(root)}, {_quote(str(epoch))}::uuid);")
    owner, plan = uuid4(), [leg(), leg()]
    authenticated(admit(owner, plan))
    primary_actor, actor = actor, other_actor
    for size in (1024, 1024, 1024, 1020):
        result = json.loads(authenticated(admit(uuid4(), [leg() for _ in range(size)])))
        if result['status'] != 'admitted':
            raise GateFailure('provider rejected available owner reservation capacity')
    # Unadmitted writes take only the two unreserved slots, then see a full
    # epoch (reservations count), never contention and never a reserved slot.
    direct = [leg(), leg()]
    for item in direct:
        authenticated(put(item, 'direct-' + item['operation_id'] + '.json'))
    overflow = authenticated(put(leg(), 'overflow.json'), ok=False)
    if 'epoch is full' not in overflow.stderr.lower():
        raise GateFailure('unadmitted write did not count reserved slots as occupied')
    if _scalar(database_url, 'select count(*) from public.ab_mutation_receipts r '
               'join public.ab_mutation_admission_legs l on l.root=r.root '
               'and l.protocol_epoch=r.protocol_epoch and l.receipt_slot=r.receipt_slot '
               f'and l.operation_id<>r.operation_id where r.root={_quote(root)};') != '0':
        raise GateFailure('unadmitted write took a reserved admission slot')
    # An operation that already executed unadmitted can never gain a leg.
    authenticated(admit(uuid4(), [direct[0]]), ok=False)
    actor = primary_actor
    waiting_owner, waiting_plan = uuid4(), [leg()]
    full = json.loads(authenticated(admit(waiting_owner, waiting_plan)))
    if full['status'] != 'full' or json.loads(authenticated(admit(waiting_owner, waiting_plan))) != full:
        raise GateFailure('full admission was not an effect-free repeatable result')
    receipt = json.loads(authenticated(put(plan[0], 'reserved-last-slot.json')))
    authenticated(ack(receipt))
    if json.loads(authenticated(admit(owner, plan)))['status'] != 'admitted':
        raise GateFailure('full ledger lost prior owner reservation')
    authenticated(cancel(owner, plan[1]))
    authenticated(cancel(owner, plan[1], ack=True))
    if json.loads(authenticated(admit(waiting_owner, waiting_plan)))['status'] != 'full':
        raise GateFailure('cancelled slot reused before epoch closure')


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
    _run(database_url, _effect_sql(), timeout=30)
    _run(database_url, _protocol_sql(), timeout=30)
    _validate_state_and_grants(database_url)
    _validate_seal_replay(database_url)
    _validate_old_write_before_seal(database_url)
    _validate_old_write_after_seal(database_url)
    _validate_p1b_core(database_url)
    _validate_p1b_concurrency_and_bounds(database_url)
    _validate_p1b_doc_log_rpcs(database_url)
    _validate_p1b_lifecycle_rpcs(database_url)
    _validate_p1b_room_terminal_lock(database_url)
    _validate_p1b_effect_rpc(database_url)
    _validate_p1b_effect_path_lock(database_url)
    _validate_p1b_blob_manifest_rpc(database_url)
    _validate_p1b_blob_manifest_lock(database_url)
    _validate_conditional_documents(database_url)
    _validate_reaction_pairs(database_url)
    _validate_meta_append(database_url)
    _validate_receipt_retirement(database_url)
    _validate_provider_admission(database_url)
    if __package__:
        from devtools.provider_admission_race_gate import _validate_provider_admission_races
    else:
        from provider_admission_race_gate import _validate_provider_admission_races
    _validate_provider_admission_races(database_url)
    _run(database_url, _protocol_sql(), timeout=30)
    print(json.dumps({
        "database": database_name,
        "gate": "inactive-mutation-protocol-p1a-p1b-blob-manifest",
        "result": "passed",
        "server_version": server_version,
        "server_version_num": server_version_num,
        "schedules": [
            "legacy-before-seal", "legacy-after-seal",
            "typed-replay", "typed-member-leave", "typed-epoch-rotation",
            "typed-before-seal", "typed-duplicate", "typed-near-full",
            "typed-doc-put", "typed-doc-create", "typed-doc-delete",
            "typed-log-append", "typed-chat-genesis",
            "typed-member-claim", "typed-member-leave",
            "typed-terminal-chat-delete", "typed-chat-delete-exact-prefix",
            "typed-room-terminal-lock",
            "typed-effect-claim", "typed-effect-state-chain",
            "typed-effect-fresh-duplicate", "typed-effect-post-removal-replay",
            "typed-effect-path-lock",
            "typed-blob-owner-upload", "typed-blob-owner-only-unpublished",
            "typed-blob-manifest", "typed-blob-replay",
            "typed-blob-digest-collision", "typed-blob-fresh-duplicate",
            "typed-blob-owner-object-id-size", "typed-blob-member-read",
            "typed-blob-update-denied", "typed-blob-object-replacement",
            "typed-blob-post-removal-replay", "typed-blob-key-lock",
            "conditional-doc-applied-conflict-replay",
            "conditional-doc-concurrent-put-create-delete",
            "conditional-doc-tombstone-sourceepoch-membership",
            "reaction-pair-exact-replay-and-atomic-rollback",
            "reaction-pair-two-document-put-create-delete-races",
            "reaction-pair-pair-race-and-membership-replay",
            "meta-append-atomic-swap-replay-and-race",
            "receipt-retirement-generation-ack-closure",
            "provider-admission-full-owner-reservation",
            "provider-admission-replay-cancellation-closure",
            "provider-admission-execute-cancel-lock-order",
            "provider-admission-rotation-lock-order",
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
