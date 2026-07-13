"""The plain-text exporter (R16c): a member's view on file — edits applied,
tombstones blank, membership enforced — plus the legacy-only selector and
the harness's hosted-agent discovery that AgentHarness.pyw rides.
"""

from __future__ import annotations

import pytest

from agentbridge.export import export_chat
from agentbridge.harness.runner import hosted_agents
from agentbridge.core.errors import NotAMember
from agentbridge.mesh.events import is_legacy_chat_id
from agentbridge.mesh.service import Mesh


@pytest.fixture
def xrig(tmp_path):
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    m = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    m.accounts.create_human("aryan", "hunter2x")
    yield m, root, home, tmp_path
    m.close()


def test_export_reflects_the_read_model(xrig):
    m, root, home, tmp_path = xrig
    snap = m.create_chat("Notes")
    m.post(snap.id, "first line")
    edited = m.post(snap.id, "typo here")
    gone = m.post(snap.id, "delete me")
    m.edit(snap.id, edited.id, "typo fixed")
    m.redact(snap.id, [gone.id])
    m.outbox.flush_once()
    m.sync.sync_once([snap.id])

    out = export_chat(m, snap.id, tmp_path / "exports")
    text = out.read_text(encoding="utf-8")
    assert "# Notes" in text and "@aryan" in text
    assert "first line" in text
    assert "typo fixed (edited)" not in text          # marker precedes body
    assert "(edited) typo fixed" in text
    assert "typo here" not in text                    # the old body is gone
    assert "delete me" not in text                    # tombstoned
    assert "a message was deleted" in text


def test_export_is_membership_gated(xrig):
    m, root, home, tmp_path = xrig
    other_home = tmp_path / "other-home"
    fable = Mesh(root, "fable", "devbox", encrypt=True, home=other_home)
    fable.accounts.create_human("fable", "fablepass")
    private = fable.create_chat("Private")
    fable.post(private.id, "not for aryan")
    fable.outbox.flush_once()
    try:
        with pytest.raises(NotAMember):
            export_chat(m, private.id, tmp_path / "exports")
    finally:
        fable.close()


def test_legacy_selector_and_hosted_discovery(xrig):
    m, root, home, tmp_path = xrig
    v2 = m.create_chat("New room")
    assert not is_legacy_chat_id(v2.id)
    assert is_legacy_chat_id("aryan-s-hub")            # migrated v1 shape

    m.accounts.create_agent("helper")
    m.accounts.create_agent("scout")
    assert hosted_agents(root, "devbox") == ["helper", "scout"]
    assert hosted_agents(root, "elsewhere") == []
