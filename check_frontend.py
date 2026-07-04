"""Dev-time front-end gate: syntax-check every ES module in gui/static/js.

Run after any front-end edit (requires Node on the DEV machine only —
analyst machines never need this):

    python check_frontend.py

Each module is copied to a temp .mjs so `node --check` parses it as an ES
module, catching typos, bad imports and stray braces before the browser
ever sees them. Also fails on imports of files that don't exist.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent / "gui" / "static" / "js"
IMPORT_RE = re.compile(r'''^\s*import\b[^"']*["'](\.\/[^"']+)["']''', re.M)


def main() -> int:
    files = sorted(JS_DIR.glob("*.js"))
    if not files:
        print(f"no modules found under {JS_DIR}")
        return 1
    bad = 0
    for f in files:
        src = f.read_text(encoding="utf-8")
        # imports must point at real files (node --check doesn't resolve them)
        for rel in IMPORT_RE.findall(src):
            if not (JS_DIR / rel).is_file():
                print(f"FAIL  {f.name}: import of missing file {rel}")
                bad += 1
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".mjs", delete=False, encoding="utf-8") as tmp:
            tmp.write(src)
            tmp_path = Path(tmp.name)
        try:
            r = subprocess.run(["node", "--check", str(tmp_path)],
                               capture_output=True, text=True, shell=True)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout).replace(str(tmp_path), f.name)
                print(f"FAIL  {f.name}\n{detail}")
                bad += 1
            else:
                print(f"ok    {f.name}")
        finally:
            tmp_path.unlink(missing_ok=True)
    if bad:
        print(f"\n{bad} problem(s)")
        return 1
    print(f"\nall {len(files)} modules pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
