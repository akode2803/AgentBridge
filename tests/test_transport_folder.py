"""FolderTransport: docs/logs/blobs roundtrips + the OneDrive-reality cases."""

import json
import os
import sys

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def tx(tmp_path):
    return FolderTransport(tmp_path / "mesh2")


def test_docs_roundtrip_and_listing(tx):
    assert tx.get_doc("users/aryan.json") is None
    assert tx.get_doc("users/aryan.json", default={}) == {}
    tx.put_doc("users/aryan.json", {"name": "aryan", "kind": "human"})
    tx.put_doc("users/claude.json", {"name": "claude", "kind": "agent"})
    assert tx.get_doc("users/aryan.json")["name"] == "aryan"
    assert tx.list_docs("users") == ["users/aryan.json", "users/claude.json"]
    tx.delete_doc("users/claude.json")
    tx.delete_doc("users/claude.json")  # missing is not an error
    assert tx.list_docs("users") == ["users/aryan.json"]


def test_path_traversal_refused(tx):
    with pytest.raises(TransportError):
        tx.get_doc("../outside.json")
    with pytest.raises(TransportError):
        tx.put_doc("users/../../evil.json", {})


@pytest.mark.skipif(os.name != "nt", reason="extended-length paths are Windows")
def test_extended_length_spelling_is_the_same_root(tx):
    """Windows resolve() returns the \\\\?\\ form of a path while another
    handle holds it mid-write; the guard must treat that as the SAME root,
    not an escape (a real flake the R15 parallel harness tests caught)."""
    tx.put_doc("chats/c1/overlays/state/helper.json", {"read_ns": 1})
    ext = FolderTransport(f"\\\\?\\{tx.root}")
    assert ext.get_doc("chats/c1/overlays/state/helper.json") == {"read_ns": 1}
    ext.put_doc("chats/c1/overlays/state/helper.json", {"read_ns": 2})
    assert tx.get_doc("chats/c1/overlays/state/helper.json") == {"read_ns": 2}
    with pytest.raises(TransportError):
        ext.get_doc("../outside.json")  # the guard itself still guards


def test_log_append_read_incremental(tx):
    chat = "c1"
    for i in range(3):
        tx.append_log(chat, "aryan@lenovo", {"id": f"m{i}", "ns": i + 1})
    recs, off = tx.read_log(chat, "aryan@lenovo")
    assert [r["id"] for r in recs] == ["m0", "m1", "m2"] and off > 0

    tx.append_log(chat, "aryan@lenovo", {"id": "m3", "ns": 4})
    recs2, off2 = tx.read_log(chat, "aryan@lenovo", offset=off)
    assert [r["id"] for r in recs2] == ["m3"] and off2 > off

    assert tx.list_chat_ids() == [chat]
    logs = tx.list_logs(chat)
    assert logs == [("aryan@lenovo", off2)]


def test_partial_trailing_line_not_consumed(tx, tmp_path):
    """A half-synced final line must wait — the offset may not pass it."""
    chat = "c2"
    tx.append_log(chat, "coco@avd", {"id": "m1", "ns": 1})
    p = tmp_path / "mesh2" / "chats" / chat / "msgs" / "coco@avd.jsonl"
    with p.open("ab") as fh:  # simulate OneDrive mid-sync: no trailing newline
        fh.write(b'{"id": "m2", "ns"')

    recs, off = tx.read_log(chat, "coco@avd")
    assert [r["id"] for r in recs] == ["m1"]
    with p.open("ab") as fh:  # sync completes the line
        fh.write(b': 2}\n')
    recs2, off2 = tx.read_log(chat, "coco@avd", offset=off)
    assert [r["id"] for r in recs2] == ["m2"] and off2 > off


def test_shrunken_file_resets_offset(tx, tmp_path):
    """Sync conflict rewrote the file smaller -> re-read all; cache dedups."""
    chat = "c3"
    for i in range(5):
        tx.append_log(chat, "a@m", {"id": f"m{i}", "ns": i + 1})
    _, off = tx.read_log(chat, "a@m")
    p = tmp_path / "mesh2" / "chats" / chat / "msgs" / "a@m.jsonl"
    p.write_bytes(b'{"id": "m0", "ns": 1}\n')  # shrunk
    recs, off2 = tx.read_log(chat, "a@m", offset=off)
    assert [r["id"] for r in recs] == ["m0"] and off2 < off


def test_garbage_line_skipped_but_consumed(tx):
    chat = "c4"
    tx.append_log(chat, "x@y", {"id": "m1", "ns": 1})
    p = tx.local_path(f"chats/{chat}/msgs/x@y.jsonl")
    with p.open("ab") as fh:
        fh.write(b"not json at all\n")
    tx.append_log(chat, "x@y", {"id": "m2", "ns": 2})
    recs, off = tx.read_log(chat, "x@y")
    assert [r["id"] for r in recs] == ["m1", "m2"]
    # consumed: a re-read from the offset returns nothing, not the garbage
    assert tx.read_log(chat, "x@y", offset=off) == ([], off)


def test_bom_stripped_at_start(tx, tmp_path):
    chat = "c5"
    p = tmp_path / "mesh2" / "chats" / chat / "msgs" / "w@z.jsonl"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"id": "m1", "ns": 1}).encode() + b"\n")
    recs, _ = tx.read_log(chat, "w@z")
    assert recs and recs[0]["id"] == "m1"


def test_append_retries_transient_lock(tx, monkeypatch):
    import agentbridge.transport.folder as mod

    monkeypatch.setattr(mod, "_BASE_DELAY", 0.001)
    fails = {"left": 2}
    real_open = mod.Path.open

    def flaky(self, *a, **kw):
        if str(self).endswith(".jsonl") and "a" in (a[0] if a else kw.get("mode", "")):
            if fails["left"] > 0:
                fails["left"] -= 1
                raise PermissionError("OneDrive lock")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(mod.Path, "open", flaky)
    tx.append_log("c6", "a@m", {"id": "m1", "ns": 1})
    assert fails["left"] == 0
    recs, _ = tx.read_log("c6", "a@m")
    assert recs[0]["id"] == "m1"


def test_blobs_roundtrip_and_cap(tx, tmp_path):
    tx.put_blob("chats/c7/files/f1.bin", b"\x00\x01payload")
    assert tx.get_blob("chats/c7/files/f1.bin") == b"\x00\x01payload"
    assert tx.blob_size("chats/c7/files/f1.bin") == 9
    assert tx.get_blob("chats/c7/files/nope.bin") is None

    src = tmp_path / "local.bin"
    src.write_bytes(b"upload me")
    tx.put_blob_from(src, "chats/c7/files/f2.bin")
    assert tx.get_blob("chats/c7/files/f2.bin") == b"upload me"

    assert tx.local_path("chats/c7/files/f2.bin") is not None
    assert tx.local_path("chats/c7/files/nope.bin") is None

    assert FolderTransport(tmp_path / "m3", max_upload_mb=64).max_upload_bytes == 64 * 1024 * 1024


def test_delete_chat(tx):
    tx.append_log("gone", "a@m", {"id": "m1", "ns": 1})
    tx.put_doc("chats/gone/meta.json", {"id": "gone"})
    tx.delete_chat("gone")
    assert tx.list_chat_ids() == []
    tx.delete_chat("never-existed")  # not an error


@pytest.mark.skipif(os.name != "nt", reason="ReadDirectoryChangesW is Windows-only")
def test_windows_watcher_hints_on_change(tx):
    w = tx.watch()
    try:
        assert type(w).__name__ == "_WinDirWatcher"
        w.wait(0.1)  # drain any startup noise
        tx.append_log("cw", "a@m", {"id": "m1", "ns": 1})
        assert w.wait(3.0), "no hint after a local write"
    finally:
        w.close()


@pytest.mark.skipif(os.name == "nt", reason="posix fallback path")
def test_posix_watcher_is_pure_polling(tx):
    w = tx.watch()
    assert w.wait(0.01) is False


def test_watcher_never_breaks_transport(tx, monkeypatch):
    """If the watcher can't start, watch() degrades to polling, not an error."""
    if os.name == "nt":
        import agentbridge.transport.folder as mod

        monkeypatch.setattr(
            mod, "_WinDirWatcher",
            lambda root: (_ for _ in ()).throw(RuntimeError("no handles")),
        )
        w = tx.watch()
        assert type(w).__name__ == "Watcher"
    else:
        assert sys.platform != "win32"
