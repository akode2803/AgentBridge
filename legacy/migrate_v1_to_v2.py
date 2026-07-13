"""RETIRED (R16.5). This ran the one R14 cutover as ``agentbridge.migrate``;
the migrated legacy chats were then exported to plain text and removed, and
v2 no longer reads the epoch-0 plaintext this tool writes. Kept for
reference only — it does not run from this location.

v1 -> v2 migration (R9.5). READ-ONLY on the source tree, writes only to a
fresh destination root (plan D3: parallel-root, never in-place), and a
--dry-run that touches nothing.

    python -m agentbridge.migrate --src <v1 mesh dir> --dest <mesh2 dir> [--dry-run]

What maps where (v1 shapes read from mesh.py, 2026-07-13):
  users/<u>.json          username->name; humans keep their PBKDF2 auth record
                          (tagged algo="pbkdf2"; v2 verifies it and upgrades to
                          scrypt + identity keys at first login); agents get
                          agent{owner=owners[0], machine="migrated",
                          harness=<v1 settings>} — keys bootstrap on their own
                          machine later (seal-forward: old chats stay epoch-0).
  chats/<id>/meta.json    synthesized GENESIS info event (ns just before the
                          oldest message; v1 owner -> admin) + meta rewritten
                          as the FOLD of the migrated events — self-healing
                          from day one.
  msgs/<u>.jsonl          line-for-line into msgs/<u>@migrated.jsonl as
                          epoch-0 envelopes (ids/ns PRESERVED — cursors and
                          receipts keep working); v1 kind human/agent ->
                          "message"; v1 kind info -> inert legacy_note pill.
  redactions.json         -> overlays/redactions/<id>.json (tombstones hold)
  edits.json              -> overlays/edits/<id>.json (epoch-0 sealed; edit ns
                          derived from its v1 timestamp so nothing shows as
                          freshly-unread)
  meta["pins"]            -> overlays/pins/<id>.json (active pins only)
  state/<u>.json          -> overlays/state/<u>.json (cursors/hidden/cleared/
                          flags copied; v1 starred SNAPSHOTS -> id list, the
                          v2 model)
  files/, avatars, tasks  copied byte-for-byte.
Runtime artifacts (status/, outbox/, control.json) are deliberately skipped.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .core.models import BodyRecord, ChatKind, Role
from .mesh import events
from .mesh.directory import Directory
from .mesh.paths import P
from .mesh.sealer import PlainSealer
from .transport.folder import FolderTransport

__all__ = ["migrate", "MigrationReport"]

_KIND_MAP = {"group": ChatKind.GROUP, "dm": ChatKind.DM, "self": ChatKind.SELF}


@dataclass
class MigrationReport:
    users: int = 0
    chats: int = 0
    messages: int = 0
    info_events: int = 0
    overlays: int = 0
    blobs: int = 0
    warnings: list[str] = field(default_factory=list)
    verified: bool = False

    def summary(self) -> str:
        lines = [
            f"users={self.users} chats={self.chats} messages={self.messages}",
            f"info_events={self.info_events} overlays={self.overlays} blobs={self.blobs}",
            f"verification: {'PASS' if self.verified else 'FAILED/SKIPPED'}",
        ]
        lines += [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def _iso_to_ns(iso: str, fallback: int) -> int:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except (ValueError, TypeError):
        return fallback


def _msg_ns(rec: dict) -> int:
    if isinstance(rec.get("ns"), int):
        return rec["ns"]
    try:  # v1 id format: "<ns hex>-<suffix>"
        return int(str(rec.get("id", "0-")).split("-")[0], 16)
    except ValueError:
        return 0


def migrate(src_root: Path | str, dest_root: Path | str, *, dry_run: bool = False,
            verify: bool = True) -> MigrationReport:
    src = FolderTransport(src_root)
    report = MigrationReport()
    sealer = PlainSealer()

    dest_path = Path(dest_root)
    if not dry_run:
        if dest_path.exists() and any(dest_path.iterdir()):
            raise SystemExit(f"destination {dest_path} is not empty — refusing")
    dest = None if dry_run else FolderTransport(dest_path)

    def put(path: str, doc: dict) -> None:
        if dest is not None:
            dest.put_doc(path, doc)

    # ------------------------------------------------------------- accounts
    for doc_path in src.list_docs("users"):
        u = src.get_doc(doc_path)
        if not isinstance(u, dict) or not u.get("username"):
            report.warnings.append(f"skipped malformed user doc {doc_path}")
            continue
        name = u["username"]
        rec: dict = {
            "name": name, "kind": u.get("kind", "human"),
            "display": u.get("display", name.title()),
            "created": u.get("created", ""), "active": True,
        }
        if u.get("kind") == "agent":
            owners = u.get("owners") or []
            if not owners:
                report.warnings.append(f"agent @{name} has no owner — kept inactive")
                rec["active"] = False
            rec["agent"] = {
                "owner": owners[0] if owners else "",
                "machine": "migrated",              # real machine set at setup
                "harness": u.get("settings") or {},  # model/rules -> R15 maps
            }
        elif isinstance(u.get("auth"), dict):
            rec["auth"] = {"algo": "pbkdf2", **u["auth"]}
        if isinstance(u.get("avatar"), dict):
            rec["avatar"] = u["avatar"]
        put(P.user(name), rec)
        report.users += 1

    # copy profile photos
    for jpg in _iter_files(src, "avatars"):
        _copy_blob(src, dest, jpg, report)

    # ---------------------------------------------------------------- chats
    directory = Directory(dest) if dest is not None else Directory(src)
    for chat_id in src.list_chat_ids():
        meta = src.get_doc(f"chats/{chat_id}/meta.json")
        if not isinstance(meta, dict):
            report.warnings.append(f"chat {chat_id}: unreadable meta — skipped")
            continue
        members = [m for m in (meta.get("members") or []) if m]
        if not members:
            report.warnings.append(f"chat {chat_id}: no members — skipped")
            continue

        # read every v1 sender log once
        logs: dict[str, list[dict]] = {}
        for log_name, _size in src.list_logs(chat_id):
            recs, _ = src.read_log(chat_id, log_name)
            logs[log_name] = recs
        all_recs = [r for recs in logs.values() for r in recs]
        oldest = min((_msg_ns(r) for r in all_recs), default=time.time_ns())
        # genesis sits strictly before the oldest message and stays POSITIVE
        # (ns is always positive; the store filters ns>0). Real ns are huge,
        # so `oldest - 1` is safe and distinct; the max() guards toy data.
        genesis_ns = max(1, oldest - 1)

        # synthesized genesis: the v1 owner becomes the (only) initial admin
        owner = meta.get("owner") or members[0]
        author = meta.get("created_by") or owner
        kind = _KIND_MAP.get(meta.get("kind", "group"), ChatKind.GROUP)
        genesis = {
            "id": f"{genesis_ns:x}-genesis", "ns": genesis_ns,
            "ts": meta.get("created", ""), "from": author, "kind": "info",
            "event": {
                "type": events.EV_CREATED, "kind": kind.value,
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "auto_dm": bool(meta.get("auto_dm")),
                "members": {
                    m: (Role.ADMIN.value if m == owner and kind is ChatKind.GROUP
                        else Role.MEMBER.value)
                    for m in members
                },
            },
        }
        out_events: list[dict] = [genesis]
        if dest is not None:
            dest.append_log(chat_id, f"{author}@migrated", genesis)
        report.info_events += 1

        # messages, sender by sender (per-device single-writer holds)
        for log_name, recs in logs.items():
            sender = log_name  # v1 log name == sender username
            for rec in recs:
                env = _convert_message(chat_id, rec, sealer, report)
                if env is None:
                    continue
                out_events.append(env)
                if dest is not None:
                    dest.append_log(chat_id, f"{sender}@migrated", env)
                if env["kind"] == "info":
                    report.info_events += 1
                else:
                    report.messages += 1

        # meta = the FOLD of what we just wrote (self-healing from day one)
        snap = events.fold(chat_id, out_events, directory)
        if not snap.members:
            report.warnings.append(f"chat {chat_id}: fold produced no members")
        put(P.meta(chat_id), snap.to_dict())
        report.chats += 1

        # ------------------------------------------------------- overlays
        redactions = src.get_doc(f"chats/{chat_id}/redactions.json", default={}) or {}
        for mid, r in redactions.items():
            if isinstance(r, dict):
                put(P.redaction(chat_id, mid),
                    {"by": r.get("by", ""), "at": r.get("at", ""),
                     "ns": _iso_to_ns(r.get("at", ""), 0)})
                report.overlays += 1

        edits = src.get_doc(f"chats/{chat_id}/edits.json", default={}) or {}
        by_id = {r.get("id"): r for r in out_events}
        for mid, e in edits.items():
            if not isinstance(e, dict):
                continue
            base_ns = _msg_ns(by_id.get(mid) or {})
            edit_ns = _iso_to_ns(e.get("at", ""), base_ns + 1)
            sealed = sealer.seal(chat_id, mid, edit_ns,
                                 BodyRecord(body=e.get("body", ""),
                                            tags=e.get("tags") or []))
            put(P.edit(chat_id, mid),
                {**sealed, "by": e.get("by", ""), "at": e.get("at", ""), "ns": edit_ns})
            report.overlays += 1

        now_ns = time.time_ns()
        for pin in (meta.get("pins") or []):
            if not isinstance(pin, dict) or not pin.get("id"):
                continue
            until = _iso_to_ns(pin.get("until", ""), 0)
            if until and until < now_ns:
                continue  # lazily-expired pin: let it go
            put(P.pin(chat_id, pin["id"]),
                {"by": pin.get("by", ""), "at": pin.get("at", ""),
                 "ns": _iso_to_ns(pin.get("at", ""), 0)})
            report.overlays += 1

        for state_path in src.list_docs(f"chats/{chat_id}/state"):
            user = state_path.rsplit("/", 1)[-1].removesuffix(".json")
            state = src.get_doc(state_path)
            if not isinstance(state, dict):
                continue
            out = {k: state[k] for k in
                   ("read_ts", "read_ns", "hidden", "cleared", "pinned",
                    "deleted", "forced_unread") if k in state}
            starred = state.get("starred")
            if isinstance(starred, dict):   # v1 snapshots -> id list (v2 model)
                out["starred"] = list(starred.keys())
            elif isinstance(starred, list):
                out["starred"] = starred
            put(P.state(chat_id, user), out)
            report.overlays += 1

        # attachments + group photo + agent task history: byte-for-byte
        for blob in _iter_files(src, f"chats/{chat_id}/files"):
            _copy_blob(src, dest, blob, report)
        if src.blob_size(f"chats/{chat_id}/avatar.jpg"):
            _copy_blob(src, dest, f"chats/{chat_id}/avatar.jpg", report)
        for task_doc in src.list_docs(f"chats/{chat_id}/tasks"):
            doc = src.get_doc(task_doc)
            if doc is not None:
                put(task_doc, doc)

    # ---------------------------------------------------------- verification
    if verify and dest is not None:
        report.verified = _verify(src, dest, directory, report)
    elif dry_run:
        report.verified = True  # nothing written, nothing to contradict
    return report


def _convert_message(chat_id: str, rec: dict, sealer: PlainSealer,
                     report: MigrationReport) -> dict | None:
    mid, ns = rec.get("id"), _msg_ns(rec)
    if not mid or not ns:
        report.warnings.append(f"chat {chat_id}: skipped malformed line")
        return None
    base = {"id": mid, "ns": ns, "ts": rec.get("ts", ""), "from": rec.get("from", "")}
    if rec.get("kind") == "info":
        # v1 events are display pills with a STRING event tag; keep them as
        # inert legacy notes (the v2 fold ignores unknown types by design)
        return {**base, "kind": "info",
                "event": {"type": "legacy_note",
                          "v1_event": str(rec.get("event", "")),
                          "who": rec.get("target"),
                          "text": rec.get("body", "")}}
    body = BodyRecord(
        body=rec.get("body", ""), tags=rec.get("tags") or [],
        reply_to=rec.get("reply_to"), files=rec.get("files") or [],
        fwd=rec.get("fwd"),
    )
    return {**base, "kind": "message", **sealer.seal(chat_id, mid, ns, body)}


def _iter_files(src: FolderTransport, prefix: str):
    base = src.local_path(prefix)
    if base is None or not base.is_dir():
        return
    for p in sorted(base.rglob("*")):
        if p.is_file():
            yield (Path(prefix) / p.relative_to(base)).as_posix()


def _copy_blob(src: FolderTransport, dest: FolderTransport | None,
               path: str, report: MigrationReport) -> None:
    if dest is None:
        report.blobs += 1
        return
    local = src.local_path(path)
    if local is not None:
        dest.put_blob_from(local, path)
        report.blobs += 1


def _verify(src: FolderTransport, dest: FolderTransport,
            directory: Directory, report: MigrationReport) -> bool:
    ok = True
    for chat_id in dest.list_chat_ids():
        # 1) the written meta must equal a fresh fold (self-heal holds)
        recs = []
        for log_name, _ in dest.list_logs(chat_id):
            recs += dest.read_log(chat_id, log_name)[0]
        folded = events.fold(chat_id, recs, directory)
        meta = dest.get_doc(P.meta(chat_id)) or {}
        if sorted(folded.members) != sorted((meta.get("members") or {})):
            report.warnings.append(f"verify {chat_id}: fold/meta member mismatch")
            ok = False
        # 2) v1 line count == v2 line count (genesis accounts for the +1)
        v1 = sum(len(src.read_log(chat_id, n)[0]) for n, _ in src.list_logs(chat_id))
        v2 = len(recs)
        if v2 != v1 + 1:
            report.warnings.append(f"verify {chat_id}: line count {v2} != {v1}+1")
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge.migrate", description=__doc__)
    ap.add_argument("--src", required=True, help="v1 mesh directory (read-only)")
    ap.add_argument("--dest", required=True, help="fresh mesh2 directory")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    report = migrate(args.src, args.dest, dry_run=args.dry_run)
    print(report.summary())
    return 0 if report.verified and not report.warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())
