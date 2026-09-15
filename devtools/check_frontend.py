"""Check GUI ES-module syntax and relative static imports."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


JS_DIR = Path(__file__).resolve().parent.parent / "gui" / "static" / "js"
STATIC_IMPORT_RE = re.compile(
    r'''^\s*(?:import\s*|(?:import|export)\b[^"']*?\bfrom\s*)["'](\.{1,2}/[^"']+)["']''',
    re.MULTILINE,
)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("FAIL  Node.js executable was not found on PATH", file=sys.stderr)
        return 1
    files = sorted(JS_DIR.glob("*.js"))
    if not files:
        print(f"FAIL  no JavaScript modules found under {JS_DIR}", file=sys.stderr)
        return 1

    failures = 0
    try:
        with tempfile.TemporaryDirectory(prefix="agentbridge-frontend-") as temporary:
            temp_dir = Path(temporary)
            for source_path in files:
                try:
                    source = source_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    print(f"FAIL  {source_path.name}: cannot read UTF-8 source: {exc}")
                    failures += 1
                    continue

                for relative in STATIC_IMPORT_RE.findall(source):
                    imported = source_path.parent / relative
                    if not imported.is_file():
                        print(
                            f"FAIL  {source_path.name}: relative import does not exist: "
                            f"{relative}"
                        )
                        failures += 1

                temporary_path = temp_dir / f"{source_path.stem}.mjs"
                try:
                    temporary_path.write_text(source, encoding="utf-8")
                    result = subprocess.run(
                        [node, "--check", str(temporary_path)],
                        capture_output=True,
                        check=False,
                        text=True,
                        shell=False,
                    )
                except OSError as exc:
                    print(f"FAIL  {source_path.name}: could not execute Node.js: {exc}")
                    failures += 1
                    continue
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "syntax check failed").replace(
                        str(temporary_path), source_path.name,
                    ).rstrip()
                    print(f"FAIL  {source_path.name}\n{detail}")
                    failures += 1
                else:
                    print(f"ok    {source_path.name}")
    except OSError as exc:
        print(f"FAIL  could not create or clean temporary files: {exc}", file=sys.stderr)
        return 1

    if failures:
        print(f"\n{failures} problem(s)")
        return 1
    print(f"\nall {len(files)} modules pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
