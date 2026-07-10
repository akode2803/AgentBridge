"""Folder connector — a locally-synced cloud folder as the shared store.

This is the desktop path: OneDrive, SharePoint ("Add shortcut to My files")
and Google Drive for desktop all materialize the shared space as a plain
local folder, so one connector covers every provider. The sync client does
the transport; we only do careful local file I/O (atomic replaces, BOM
tolerance, partial-line skips) so readers on other machines never see a
torn write.
"""

import hashlib
import os
import shutil
from pathlib import Path

from .base import Connector

SCHEME = "folder"


class FolderConnector(Connector):
    scheme = SCHEME

    def __init__(self, root, max_upload_mb=None):
        self.root = Path(root)
        # OneDrive/SharePoint/Drive sync every attachment to each member's
        # machine, so the cap guards everyone's sync bandwidth, not just disk.
        # Overridable per deployment via a dict spec ({"connector":"folder",
        # "root":…, "max_upload_mb":1024}); defaults to the base 512 MB.
        if max_upload_mb:
            self.max_upload_bytes = int(max_upload_mb) * 1024 * 1024

    def __repr__(self):
        return f"FolderConnector({self.root})"

    def _p(self, rel):
        return self.root / rel if rel else self.root

    # ------------------------------------------------------------ primitives

    def read_text(self, rel):
        try:
            return self._p(rel).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None

    def write_text(self, rel, text):
        path = self._p(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def append_line(self, rel, line):
        path = self._p(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def listdir(self, rel):
        p = self._p(rel)
        try:
            return sorted(e.name for e in p.iterdir())
        except OSError:
            return []

    def exists(self, rel):
        return self._p(rel).exists()

    def isdir(self, rel):
        return self._p(rel).is_dir()

    def mkdir(self, rel):
        self._p(rel).mkdir(parents=True, exist_ok=True)

    def put_file(self, local_src, rel):
        dest = self._p(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_src, dest)

    def size(self, rel):
        try:
            return self._p(rel).stat().st_size
        except OSError:
            return None

    def delete_tree(self, rel):
        if not rel or rel in (".", "/"):
            raise ValueError("refusing to delete the store root")
        shutil.rmtree(self._p(rel), ignore_errors=True)

    def sha256(self, rel, chunk=1 << 20):
        h = hashlib.sha256()
        try:
            with open(self._p(rel), "rb") as f:
                while True:
                    b = f.read(chunk)
                    if not b:
                        break
                    h.update(b)
        except OSError:
            return None
        return h.hexdigest()

    def local_path(self, rel):
        return self._p(rel)
