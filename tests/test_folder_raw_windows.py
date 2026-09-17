"""Platform-neutral parser and native Windows held-handle folder tests."""
from __future__ import annotations

import os
import struct
import subprocess
from pathlib import PureWindowsPath

import pytest

from agentbridge.transport import folder_raw_windows as windows
from agentbridge.transport.folder_raw import FolderReadUnavailable


_HEADER = struct.Struct("<IIqqqqqqIII")
_WINDOWS = pytest.mark.skipif(os.name != "nt", reason="native Windows handles")


def _record(name, *, next_offset=0, name_size=None):
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    size = len(encoded) if name_size is None else name_size
    return _HEADER.pack(next_offset, 0, 0, 0, 0, 0, 0, 0, 0, size, 0) + encoded


def _records(*names):
    chunks = []
    for index, name in enumerate(names):
        raw = _record(name)
        if index + 1 < len(names):
            offset = (len(raw) + 7) & ~7
            raw = _record(name, next_offset=offset).ljust(offset, b"\0")
        chunks.append(raw)
    return b"".join(chunks)


def test_directory_records_parse_multiple_unicode_names_and_dot_entries():
    payload = _records(".", "..", "alice.json", "\u03b4elta.json")
    assert list(windows.directory_records(payload)) == [
        ".", "..", "alice.json", "\u03b4elta.json",
    ]


@pytest.mark.parametrize("payload", [
    None,
    bytearray(_record("a.json")),
    b"",
    b"\0" * (_HEADER.size - 1),
    b"\0" * (windows.BUFFER_BYTES + 1),
    _HEADER.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    _record("a", name_size=1),
    _record("\ud800"),
    _record("bad/name"),
    _record("a", next_offset=_HEADER.size + 2),
    _record("a", next_offset=72),
])
def test_directory_records_reject_malformed_or_unsafe_records(payload):
    with pytest.raises(FolderReadUnavailable):
        list(windows.directory_records(payload))


@pytest.mark.parametrize("name", [
    "", ".", "..", "bad/name", "bad\\name", "bad:name", "trailing.",
    "trailing ", "NUL", "con.txt", "COM1", "LPT9.log", "control\x01",
    "x" * 256,
])
def test_component_rejects_unsafe_windows_names(name):
    with pytest.raises(FolderReadUnavailable, match="invalid_windows_component"):
        windows.component(name)


def test_component_accepts_bounded_unicode_name():
    assert windows.component("\u03b4elta-\u732b.json") == "\u03b4elta-\u732b.json"


def test_root_close_failure_still_closes_newly_opened_child(monkeypatch):
    reader = object.__new__(windows.Reader)
    root = windows.Handle(1, True)
    child = windows.Handle(2, True)
    opened = iter((root, child))
    closed = []

    monkeypatch.setattr(reader, "open", lambda _parent, _name: next(opened))

    def close(handle):
        closed.append(handle)
        if handle is root:
            raise OSError("root close failed")

    monkeypatch.setattr(reader, "close", close)
    with pytest.raises(OSError, match="root close failed"):
        reader.root(PureWindowsPath("C:/scope"))
    assert closed == [root, child]


def test_selected_path_close_failure_still_closes_new_child_and_root(monkeypatch):
    root = windows.Handle(1, True)
    directory = windows.Handle(2, True)
    document = windows.Handle(3, False)

    class FakeReader:
        def __init__(self):
            self.closed = []

        def root(self, _path):
            return root

        def open(self, parent, name):
            if parent is root and name == "scope":
                return directory
            if parent is directory and name == "alice.json":
                return document
            raise AssertionError((parent, name))

        def close(self, handle):
            self.closed.append(handle)
            if handle is directory:
                raise OSError("directory close failed")

        def read(self, _handle, _limit):
            raise AssertionError("read must not run after close failure")

    reader = FakeReader()
    monkeypatch.setattr(windows, "Reader", lambda: reader)
    with pytest.raises(OSError, match="directory close failed"):
        windows.collect(
            PureWindowsPath("C:/provider"), {"scope/alice.json"}, set(),
            max_bytes=1024, max_paths=100,
            include=lambda _path, _payload: None,
        )
    assert reader.closed == [directory, document, root]


@_WINDOWS
def test_native_reader_exact_prefix_absence_unicode_and_byte_budget(tmp_path):
    root = tmp_path / "provider"
    (root / "scope" / "\u03b4elta").mkdir(parents=True)
    (root / "scope" / "\u03b4elta" / "one.json").write_bytes(b'{"one":1}')
    (root / "scope" / "two.json").write_bytes(b'{"two":2}')
    captured = {}

    windows.collect(
        root,
        {"scope/\u03b4elta/one.json", "scope/missing.json"},
        {"scope"},
        max_bytes=1024,
        max_paths=100,
        include=lambda path, payload: captured.setdefault(path, payload),
    )
    assert captured == {
        "scope/\u03b4elta/one.json": b'{"one":1}',
        "scope/two.json": b'{"two":2}',
    }
    with pytest.raises(FolderReadUnavailable, match="byte_budget"):
        windows.collect(
            root, set(), {"scope"}, max_bytes=4, max_paths=100,
            include=lambda _path, _payload: None,
        )


@_WINDOWS
def test_native_reader_rejects_file_replaced_before_open(tmp_path, monkeypatch):
    root = tmp_path / "provider"
    scope = root / "scope"
    scope.mkdir(parents=True)
    target = scope / "inside.json"
    target.write_bytes(b'{"inside":true}')
    original = windows.Reader.open
    removed = False

    def remove_then_open(self, parent, name):
        nonlocal removed
        if name == "inside.json" and not removed:
            removed = True
            target.unlink()
        return original(self, parent, name)

    monkeypatch.setattr(windows.Reader, "open", remove_then_open)
    with pytest.raises(FolderReadUnavailable, match="selection_changed"):
        windows.collect(
            root, set(), {"scope"}, max_bytes=1024, max_paths=100,
            include=lambda _path, _payload: None,
        )
    assert removed


@_WINDOWS
def test_native_reader_rejects_symlink_swap_when_supported(tmp_path, monkeypatch):
    root = tmp_path / "provider"
    scope = root / "scope"
    scope.mkdir(parents=True)
    target = scope / "inside.json"
    target.write_bytes(b'{"inside":true}')
    external = tmp_path / "outside.json"
    external.write_bytes(b'{"outside":true}')
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(external)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"Windows symlink creation unavailable: {exc}")
    original = windows.Reader.open
    swapped = False

    def swap_then_open(self, parent, name):
        nonlocal swapped
        if name == "inside.json" and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(external)
        return original(self, parent, name)

    monkeypatch.setattr(windows.Reader, "open", swap_then_open)
    with pytest.raises(FolderReadUnavailable, match="symlink_in_selection"):
        windows.collect(
            root, set(), {"scope"}, max_bytes=1024, max_paths=100,
            include=lambda _path, _payload: None,
        )
    assert swapped


@_WINDOWS
def test_native_reader_rejects_directory_junction_when_supported(tmp_path):
    root = tmp_path / "provider"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "outside.json").write_bytes(b'{"outside":true}')
    junction = root / "linked"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        check=False,
        text=True,
    )
    if made.returncode:
        pytest.skip(f"Windows junction creation unavailable: {made.stderr.strip()}")
    try:
        with pytest.raises(FolderReadUnavailable, match="symlink_in_selection"):
            windows.collect(
                root, set(), {"linked"}, max_bytes=1024, max_paths=100,
                include=lambda _path, _payload: None,
            )
    finally:
        junction.rmdir()
