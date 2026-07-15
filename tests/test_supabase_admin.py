"""R84 v2.2 membership tooling: the join flow (self-signup + self-claim +
local credential install) against a faked supabase client."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentbridge.transport import supabase_admin as admin


class _Table:
    def __init__(self, sink, fail_dup=False):
        self.sink = sink
        self.fail_dup = fail_dup

    def insert(self, row):
        self.row = row
        return self

    def execute(self):
        if self.fail_dup:
            raise RuntimeError('23505: duplicate key value violates unique '
                               'constraint "ab_members_pkey"')
        self.sink.append(self.row)
        return SimpleNamespace(data=[self.row])


class _Client:
    def __init__(self, *, signup_user=True, fail_dup=False):
        self.rows: list[dict] = []
        self.signups: list[dict] = []
        self._signup_user = signup_user
        self._fail_dup = fail_dup
        self.auth = SimpleNamespace(sign_up=self._sign_up)

    def _sign_up(self, creds):
        self.signups.append(creds)
        user = SimpleNamespace(id="uid-123") if self._signup_user else None
        return SimpleNamespace(user=user)

    def table(self, name):
        assert name == "ab_members"
        return _Table(self.rows, fail_dup=self._fail_dup)


def _env():
    return {"SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "pub-key"}


def test_join_signs_up_claims_and_installs(tmp_path, monkeypatch):
    import supabase as sb_mod

    client = _Client()
    monkeypatch.setattr(sb_mod, "create_client", lambda url, key: client)
    env_path = tmp_path / "supabase.env"
    env_path.write_text("SUPABASE_URL=https://x.supabase.co\n",
                        encoding="utf-8")

    admin.join_mesh(_env(), "ben", "mesh2", env_path)

    # self-signup with a synthetic address, never echoed anywhere
    assert client.signups[0]["email"] == "ben@mesh2.agentbridge.local"
    pw = client.signups[0]["password"]
    assert len(pw) >= 24
    # self-claim: own uid, the requested name
    assert client.rows == [{"root": "mesh2", "username": "ben",
                            "uid": "uid-123"}]
    # the credential landed in the LOCAL env file (and only there)
    text = env_path.read_text(encoding="utf-8")
    assert "SUPABASE_MEMBER_EMAIL=ben@mesh2.agentbridge.local" in text
    assert f"SUPABASE_MEMBER_PASSWORD={pw}" in text
    assert "SUPABASE_URL=" in text            # existing lines survive


def test_join_surfaces_a_taken_username(tmp_path, monkeypatch):
    import supabase as sb_mod

    client = _Client(fail_dup=True)
    monkeypatch.setattr(sb_mod, "create_client", lambda url, key: client)
    env_path = tmp_path / "supabase.env"

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "aryan", "mesh2", env_path)
    assert "already claimed" in str(e.value)
    assert not env_path.exists()              # no credential for a failed join


def test_join_explains_a_refused_signup(tmp_path, monkeypatch):
    import supabase as sb_mod

    client = _Client(signup_user=False)
    monkeypatch.setattr(sb_mod, "create_client", lambda url, key: client)

    with pytest.raises(SystemExit) as e:
        admin.join_mesh(_env(), "ben", "mesh2", tmp_path / "supabase.env")
    assert "email signup enabled" in str(e.value)
