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
                 claim_error="", signin_ok=False):
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
        user = SimpleNamespace(id="uid-123") if self._signup_user else None
        session = (SimpleNamespace(user=user)
                   if self._signup_session and user else None)
        return SimpleNamespace(user=user, session=session)

    def _sign_in(self, creds):
        self.signins.append(creds)
        if not self._signin_ok:
            raise RuntimeError("invalid login credentials")
        return SimpleNamespace(user=SimpleNamespace(id="uid-123"))

    def table(self, name):
        assert name == "ab_members"
        return _Table(self)


def _env(**extra):
    return {"SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "pub-key", **extra}


def _patch(monkeypatch, client):
    import supabase as sb_mod

    monkeypatch.setattr(sb_mod, "create_client", lambda url, key: client)


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
