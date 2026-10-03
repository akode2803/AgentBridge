"""Transport-aware connection payloads without a live HTTP socket."""

from types import SimpleNamespace

from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request


def test_cloud_connection_reports_actual_registered_root_and_mirror_health(tmp_path, clouds):
    root = clouds.root(tmp_path / "cloud-mesh")
    tx = clouds.cached(root)
    app = SimpleNamespace(root=root, transport=tx)
    conn = api_chats._connection(app)
    assert conn["scheme"] == "supabase" and conn["root"] == root
    assert conn["host"] == "offline.invalid"
    assert conn["state"] == "online" and conn["mirror"]["warm"] is True
    assert not {"mode", "provider", "shared_ok", "writable", "sync_client"} & conn.keys()


def test_cloud_missing_mirror_state_stays_loading():
    tx = SimpleNamespace(scheme="supabase", host="offline.invalid",
                         mirror_status=lambda: {"warm": False, "cached": True})
    app = SimpleNamespace(root="supabase://mesh", transport=tx)
    conn = api_chats._connection(app)
    assert conn["state"] == "loading"
    assert conn["mirror"] == {"warm": False, "cached": True}


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


def test_activity_endpoint_only_sets_the_local_transport_lease():
    calls = []
    tx = SimpleNamespace(set_interactive=lambda active: calls.append(active))
    mesh = SimpleNamespace(tx=tx)
    app = SimpleNamespace(mesh=mesh, lock=None)
    assert api_chats.activity(app, Request(data={"active": True})) == {
        "ok": True, "active": True,
    }
    assert api_chats.activity(app, Request(data={"active": False}))["ok"]
    assert calls == [True, False]
