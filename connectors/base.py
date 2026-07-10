"""Connector base — the storage interface the mesh speaks.

A connector maps RELATIVE keys (POSIX-style, e.g. "chats/x/meta.json") onto
some storage that at least two machines share. Today that is a locally-synced
cloud folder (folder.py — OneDrive, SharePoint and Google Drive desktop sync
all look identical from here). Tomorrow it can be an API-backed store for
devices without filesystem sync (phones): MS Graph, Google Drive API, or
anything else — one new module in this package per backend.

Contract every connector must honor (they are what make a shared-store
transport reliable — see mesh.py's design rules):
  * write_text/write_json are ATOMIC — readers never see half a file.
  * append_line is append-only.
  * read_* tolerate concurrent syncs: missing files return None/[], partial
    trailing lines in JSONL are skipped, BOMs are stripped.
  * Keys use forward slashes; connectors translate to their native form.

`local_path(rel)` returns a real filesystem Path when the backend is a local
folder, else None. Folder-only consumers (open-with-OS, path-validated file
serving) must check for None and degrade — that is the seam where phone
support plugs in later.
"""

import json


class ConnectorError(Exception):
    """Storage-level failure; message is safe to show to the user."""


class Connector:
    """Abstract storage interface. Subclasses implement the primitives;
    the JSON/JSONL conveniences below are shared."""

    scheme = "abstract"

    # Largest attachment (bytes) an upload may be, PER CONNECTOR — the ceiling
    # is a property of the transport, not the app: a locally-synced folder must
    # push every attachment down to each member's machine (a huge file chokes
    # OneDrive/Drive sync for everyone), while an API-backed store has its own
    # service limits. The GUI reads this to gate uploads and to name the limit
    # in the "file too large" dialog. Subclasses override the default.
    max_upload_bytes = 512 * 1024 * 1024   # 512 MB

    # ------------------------------------------------------------ primitives

    def read_text(self, rel):
        """Return the file's text, or None if missing/unreadable."""
        raise NotImplementedError

    def write_text(self, rel, text):
        """Atomically replace the file's content, creating parents."""
        raise NotImplementedError

    def append_line(self, rel, line):
        """Append one line (no trailing newline needed), creating parents."""
        raise NotImplementedError

    def listdir(self, rel):
        """Names (not paths) inside a directory key; [] if missing."""
        raise NotImplementedError

    def exists(self, rel):
        raise NotImplementedError

    def isdir(self, rel):
        raise NotImplementedError

    def mkdir(self, rel):
        """Ensure a directory key exists (parents included)."""
        raise NotImplementedError

    def put_file(self, local_src, rel):
        """Copy a LOCAL file into the store at rel (attachments inbound)."""
        raise NotImplementedError

    def size(self, rel):
        """File size in bytes, or None."""
        raise NotImplementedError

    def delete_tree(self, rel):
        """Remove a directory key and everything under it. The mesh uses
        this ONLY for owner-initiated chat deletion."""
        raise NotImplementedError

    def sha256(self, rel):
        """Hex digest of the file's content, or None."""
        raise NotImplementedError

    def local_path(self, rel):
        """Real filesystem Path for folder-backed stores, else None."""
        return None

    # ---------------------------------------------------------- convenience

    def read_json(self, rel):
        text = self.read_text(rel)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def write_json(self, rel, obj):
        self.write_text(rel, json.dumps(obj, ensure_ascii=False, indent=2))

    def append_jsonl(self, rel, obj):
        self.append_line(rel, json.dumps(obj, ensure_ascii=False))

    def read_jsonl(self, rel):
        out = []
        text = self.read_text(rel)
        if text is None:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # mid-sync partial line
        return out
