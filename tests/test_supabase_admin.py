"""R84 v2.2 membership tooling: the join flow (self-signup + self-claim +
local credential install) against a faked supabase client. The contract
under test is the IDEMPOTENT one that came out of the first live failure:
credential installed BEFORE the claim, reruns resume, clear errors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge.transport import supabase_admin as admin


def _policy(sql: str, name: str) -> str:
    return sql.split(f"create policy {name}", 1)[1].split(";", 1)[0]


def test_deleted_chat_rls_is_read_delete_only_for_former_members():
    sql = (Path(__file__).parents[1] / "docs" / "supabase_schema.sql").read_text(
        encoding="utf-8",
    )
    assert "m.data->'tenure' ? public.ab_member(p_root)" in sql
    for lane in ("docs", "logs", "blobs"):
        assert "ab_can_read_chat" in _policy(
            sql, f"ab_{lane}_member_select",
        )
        assert "ab_can_read_chat" in _policy(
            sql, f"ab_{lane}_member_delete",
        )
        assert "ab_is_member" in _policy(
            sql, f"ab_{lane}_member_insert",
        )
    assert "ab_is_member" in _policy(sql, "ab_docs_member_update")
    assert "ab_is_member" in _policy(sql, "ab_blobs_member_update")
    effect_path = "path not like 'chats/%/runtime/effects/%'"
    assert effect_path in _policy(sql, "ab_docs_member_update")
    assert effect_path in _policy(sql, "ab_docs_member_delete")
    assert effect_path in _policy(sql, "ab_docs_member_insert")
    assert "ab_effect_transition" in sql
    assert "security definer set search_path = public" in sql
    assert "grant-ask.json" in sql and "grant-decision.json" in sql
    assert "drop function if exists public.ab_effect_transition" in sql


def test_runtime_prefix_and_chat_discovery_avoid_full_rls_table_scans():
    sql = (Path(__file__).parents[1] / "docs" / "supabase_schema.sql").read_text(
        encoding="utf-8",
    )
    assert "ab_docs_live_path_prefix" in sql
    assert "(root, path text_pattern_ops)" in sql
    assert "where not deleted" in sql

    start = sql.rindex("create or replace function public.ab_chat_ids")
    end = sql.index("grant execute on function public.ab_chat_ids", start)
    body = sql[start:end]
    assert "security definer set search_path = pg_catalog" in body
    assert "public.ab_root_ok(p_root)" in body
    assert "public.ab_can_read_chat(p_root, c.chat_id)" in body
    assert "auth.role() = 'service_role'" in body
    assert "from public.ab_logs l" in body
    assert "from public.ab_docs d" in body
    assert "from public, anon, authenticated" in body
    grant = sql[end:sql.index(";", end) + 1]
    assert "to authenticated, service_role" in grant


def test_member_self_service_auth_lookup_uses_one_init_plan_per_statement():
    sql = (Path(__file__).parents[1] / "docs" / "supabase_schema.sql").read_text(
        encoding="utf-8",
    )
    assert "with check (uid = (select auth.uid()))" in _policy(
        sql, "ab_members_claim",
    )
    assert "using (uid = (select auth.uid()))" in _policy(
        sql, "ab_members_leave",
    )


class _Table:
    def __init__(self, client):
        self.client = client
        self._select = False
        self._filters = {}

    def insert(self, row, returning="representation"):
        # returning="minimal" is load-bearing on the claim (RLS: the
        # representation return can't see the row it just made)
        self.client.last_returning = returning
        self._row = row
        self._select = False
        return self

    def select(self, cols):
        self._select = True
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self._select:
            rows = [r for r in self.client.rows
                    if all(str(r.get(k)) == str(v)
                           for k, v in self._filters.items())]
            return SimpleNamespace(data=rows)
        if self.client.claim_error:
            raise RuntimeError(self.client.claim_error)
        self.client.rows.append(self._row)
        return SimpleNamespace(data=[self._row])


class _Client:
    def __init__(self, *, signup_user=True, signup_session=True,
                 claim_error="", signin_ok=False, uid="uid-123"):
        self.uid = uid
        self.rows: list[dict] = []
        self.signups: list[dict] = []
        self.signins: list[dict] = []
        self._signup_user = signup_user
        self._signup_session = signup_session
        self._signin_ok = signin_ok
        self.claim_error = claim_error
        self.auth = SimpleNamespace(sign_up=self._sign_up,
                                    sign_in_with_password=self._sign_in)

    def _sign_up(self, creds):
        self.signups.append(creds)
        user = SimpleNamespace(id=self.uid) if self._signup_user else None
        session = (SimpleNamespace(user=user)
                   if self._signup_session and user else None)
        return SimpleNamespace(user=user, session=session)

    def _sign_in(self, creds):
        self.signins.append(creds)
        if not self._signin_ok:
            raise RuntimeError("invalid login credentials")
        return SimpleNamespace(user=SimpleNamespace(id=self.uid))

    def table(self, name):
        assert name == "ab_members"
        return _Table(self)


def _env(**extra):
    return {"SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "pub-key", **extra}


def _patch(monkeypatch, client):
    import supabase as sb_mod

    monkeypatch.setattr(sb_mod, "create_client", lambda url, key: client)


@pytest.fixture(autouse=True)
def unsealed_root(monkeypatch):
    """The legacy tests run against an unsealed root; typed tests opt out."""
    monkeypatch.setattr(admin, "_sealed_claim", lambda *a, **k: "unsealed")


def test_join_signs_up_claims_and_installs(tmp_path, monkeypatch):
    client = _Client()
    _patch(monkeypatch, client)
    env_path = tmp_path / "supabase.env"
    env_path.write_text("SUPABASE_URL=https://x.supabase.co\n",
                        encoding="utf-8")

    admin.join_mesh(_env(), "ben", "mesh2", env_path)

    # self-signup with a synthetic address, never echoed anywhere
    assert client.signups[0]["email"] == "ben@mesh2.agentbridge.local"
    pw = client.signups[0]["password"]
    assert len(pw) >= 24
    # self-claim: own uid, the requested name — with the load-bearing
    # minimal return (representation trips RLS on its own SELECT)
    assert client.rows == [{"root": "mesh2", "username": "ben",
                            "uid": "uid-123"}]
    assert client.last_returning == "minimal"
    # the credential landed in the LOCAL env file (and only there)
    text = env_path.read_text(encoding="utf-8")
    assert "SUPABASE_MEMBER_EMAIL=ben@mesh2.agentbridge.local" in text
    assert f"SUPABASE_MEMBER_PASSWORD={pw}" in text
    assert "SUPABASE_URL=" in text            # existing lines survive


def test_failed_claim_keeps_the_working_credential(tmp_path, monkeypatch):
    """The first live failure, encoded: the claim dies (table missing) but
    the credential MUST already be installed so a rerun resumes instead of
    orphaning an auth user with a lost password."""
    client = _Client(claim_error='relation "public.ab_members" does not exist')
    _patch(monkeypatch, client)
    env_path = tmp_path / "supabase.env"

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", env_path)
    assert "paste docs/supabase_schema.sql" in str(e.value)
    text = env_path.read_text(encoding="utf-8")
    assert "SUPABASE_MEMBER_PASSWORD=" in text   # creds survived the failure


def test_rerun_reuses_the_local_credential_and_is_idempotent(tmp_path, monkeypatch):
    """A rerun with installed creds signs IN (no second auth user), and an
    already-ours claim row is success, not a conflict."""
    client = _Client(signin_ok=True,
                     claim_error="23505: duplicate key value")
    client.rows.append({"root": "mesh2", "username": "ben", "uid": "uid-123"})
    _patch(monkeypatch, client)
    env_path = tmp_path / "supabase.env"

    admin.join_mesh(_env(SUPABASE_MEMBER_EMAIL="ben@mesh2.agentbridge.local",
                         SUPABASE_MEMBER_PASSWORD="kept-pw"),
                    "ben", "mesh2", env_path)
    assert client.signups == []               # no second auth user
    assert client.signins[0]["password"] == "kept-pw"


def test_join_surfaces_a_name_taken_by_someone_else(tmp_path, monkeypatch):
    client = _Client(claim_error="23505: duplicate key value")
    client.rows.append({"root": "mesh2", "username": "ben", "uid": "uid-OTHER"})
    _patch(monkeypatch, client)

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", tmp_path / "supabase.env")
    assert "someone else" in str(e.value)


def test_join_explains_an_orphaned_auth_user(tmp_path, monkeypatch):
    """sign_up returns a user but NO session = the email already exists
    (anti-enumeration stub) or confirmations are ON — say exactly what to
    do instead of failing cryptically at the claim as anon."""
    client = _Client(signup_session=False)
    _patch(monkeypatch, client)

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", tmp_path / "supabase.env")
    assert "revoke" in str(e.value)


def test_join_explains_a_refused_signup(tmp_path, monkeypatch):
    client = _Client(signup_user=False)
    _patch(monkeypatch, client)

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", tmp_path / "supabase.env")
    assert "email signup enabled" in str(e.value)


# ------------------------------------------------------------ sealed roots
_TYPED_CLAIM = admin._sealed_claim


@pytest.fixture
def sealed(monkeypatch):
    """Route the claim through a real typed RPC over a fake member session."""
    from test_member_claim import ACTOR, URL, _Client as _RpcClient

    from agentbridge.transport import member_claim
    from agentbridge.transport.supabase import SupabaseTransport
    from agentbridge.transport.typed_mutations import TypedMutationRPC

    monkeypatch.setattr(admin, "_sealed_claim", _TYPED_CLAIM)
    rpc_client = _RpcClient()
    opened = []

    def open_member_rpc(env, root, actor_id):
        opened.append((dict(env), root, actor_id))
        provider = SupabaseTransport(root, env={"SUPABASE_URL": URL}, client=rpc_client)
        provider.auth_mode = "member:" + env["SUPABASE_MEMBER_EMAIL"].split("@")[0]
        return TypedMutationRPC(provider, actor_id=actor_id, root=root, endpoint=URL)

    monkeypatch.setattr(member_claim, "open_member_rpc", open_member_rpc)
    monkeypatch.setattr(member_claim.time, "sleep", lambda _s: None)
    return SimpleNamespace(actor=ACTOR, rpc=rpc_client, opened=opened)


def test_sealed_join_claims_typed_after_installing_the_credential(
        tmp_path, monkeypatch, sealed, capsys):
    client = _Client(uid=sealed.actor)
    _patch(monkeypatch, client)
    env_path = tmp_path / "supabase.env"
    installed = []
    real_open = admin._sealed_claim

    def spy(*args, **kwargs):
        installed.append(env_path.read_text(encoding="utf-8"))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(admin, "_sealed_claim", spy)
    admin.join_mesh(_env(SUPABASE_SECRET_KEY="service"), "ben", "mesh2", env_path)

    pw = client.signups[0]["password"]
    assert f"SUPABASE_MEMBER_PASSWORD={pw}" in installed[0]
    env, root, actor = sealed.opened[0]
    assert (root, actor) == ("mesh2", sealed.actor)
    assert "SUPABASE_SECRET_KEY" not in env          # the member acts alone
    assert env["SUPABASE_MEMBER_PASSWORD"] == pw
    assert sealed.rpc.members == {("mesh2", "ben"): sealed.actor}
    assert len(sealed.rpc.acked) == 1
    assert client.rows == []                          # no legacy insert
    assert "@ben joined mesh2" in capsys.readouterr().out


def test_sealed_join_rerun_after_success_is_idempotent(tmp_path, monkeypatch, sealed):
    client = _Client(uid=sealed.actor, signin_ok=True)
    _patch(monkeypatch, client)
    env_path = tmp_path / "supabase.env"
    creds = dict(SUPABASE_MEMBER_EMAIL="ben@mesh2.agentbridge.local",
                 SUPABASE_MEMBER_PASSWORD="kept-pw")
    admin.join_mesh(_env(**creds), "ben", "mesh2", env_path)
    admin.join_mesh(_env(**creds), "ben", "mesh2", env_path)
    assert client.signups == []
    assert sealed.rpc.effects == 1
    claims = [a for n, a in sealed.rpc.calls if n == "ab_mutation_member_claim"]
    assert len({a["p_operation_id"] for a in claims}) == 1


def test_sealed_join_reports_a_name_taken_and_keeps_the_credential(
        tmp_path, monkeypatch, sealed):
    client = _Client(uid=sealed.actor)
    _patch(monkeypatch, client)
    sealed.rpc.members[("mesh2", "ben")] = "someone-else"
    env_path = tmp_path / "supabase.env"
    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", env_path)
    assert "someone else" in str(e.value)
    assert "SUPABASE_MEMBER_PASSWORD=" in env_path.read_text(encoding="utf-8")


def test_sealed_join_unknown_outcome_says_rerun(tmp_path, monkeypatch, sealed):
    client = _Client(uid=sealed.actor)
    _patch(monkeypatch, client)
    sealed.rpc.faults["ab_mutation_member_claim"] = [ConnectionError("x")] * 4
    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", tmp_path / "supabase.env")
    assert "rerun join" in str(e.value)


class _AdminTable:
    def __init__(self, error=""):
        self.error = error
        self.calls = []

    def upsert(self, row):
        self.calls.append(("upsert", row))
        return self

    def delete(self):
        self.calls.append(("delete",))
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        if self.error:
            raise RuntimeError(self.error)
        return SimpleNamespace(data=[])


def _admin_client(table, uid):
    deleted = []
    auth_admin = SimpleNamespace(
        list_users=lambda page, per_page: [],
        create_user=lambda data: SimpleNamespace(user=SimpleNamespace(id=uid)),
        delete_user=deleted.append)
    return SimpleNamespace(auth=SimpleNamespace(admin=auth_admin),
                           table=lambda name: table, deleted=deleted)


def test_sealed_seed_claims_as_the_minted_member(monkeypatch, sealed):
    table = _AdminTable()
    _patch(monkeypatch, _admin_client(table, sealed.actor))
    email, password = admin.seed(_env(SUPABASE_SECRET_KEY="service"), "ben", "mesh2")
    env, root, actor = sealed.opened[0]
    assert (env["SUPABASE_MEMBER_EMAIL"], env["SUPABASE_MEMBER_PASSWORD"]) == (email, password)
    assert "SUPABASE_SECRET_KEY" not in env
    assert sealed.rpc.members == {("mesh2", "ben"): sealed.actor}
    assert table.calls == []                          # no service-key write


def test_sealed_seed_without_publishable_key_explains(monkeypatch):
    table = _AdminTable("legacy provider writes are sealed")
    _patch(monkeypatch, _admin_client(table, "uid-1"))
    with pytest.raises(SystemExit) as e:
        admin.seed({"SUPABASE_URL": "https://x.supabase.co",
                    "SUPABASE_SECRET_KEY": "service"}, "ben", "mesh2")
    assert "SUPABASE_PUBLISHABLE_KEY" in str(e.value)


def test_sealed_revoke_refuses_before_deleting_the_auth_user(monkeypatch):
    table = _AdminTable("legacy provider writes are sealed")
    client = _admin_client(table, "uid-1")
    _patch(monkeypatch, client)
    with pytest.raises(SystemExit) as e:
        admin.revoke({"SUPABASE_URL": "https://x.supabase.co",
                      "SUPABASE_SECRET_KEY": "service"}, "ben", "mesh2")
    assert "sealed" in str(e.value)
    assert client.deleted == []
