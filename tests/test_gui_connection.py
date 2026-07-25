"""Transport-aware connection payloads without a live HTTP socket."""

from types import SimpleNamespace

from agentbridge.gui import api_chats, desktop
from agentbridge.transport import FolderTransport


def test_local_folder_is_authoritative_without_a_sync_client(tmp_path):
    root = tmp_path / "mesh"
    tx = FolderTransport(root)
    app = SimpleNamespace(root=root, transport=tx)
    conn = api_chats._connection(app)
    assert conn["scheme"] == "folder"
    assert conn["mode"] == "local"
    assert conn["state"] == "online"
    assert conn["writable"] is True
    assert conn["sync_client"] is None


def test_synced_folder_stays_usable_when_client_is_paused(tmp_path, monkeypatch):
    root = tmp_path / "OneDrive - Team" / "AgentBridge"
    tx = FolderTransport(root)
    monkeypatch.setattr(desktop, "sync_client_running", lambda: False)
    app = SimpleNamespace(root=root, transport=tx)
    conn = api_chats._connection(app)
    assert conn["mode"] == "synced" and conn["provider"] == "OneDrive"
    assert conn["state"] == "sync_paused"
    assert conn["shared_ok"] is True and conn["writable"] is True


def test_cloud_state_promotes_normalized_mirror_failure():
    class Cloud:
        scheme = "supabase"
        host = "example.supabase.co"

        @staticmethod
        def mirror_status():
            return {"state": "restricted", "warm": True, "cached": True}

    app = SimpleNamespace(root="supabase://mesh", transport=Cloud())
    conn = api_chats._connection(app)
    assert conn["state"] == "restricted"
    assert conn["mirror"]["cached"] is True
