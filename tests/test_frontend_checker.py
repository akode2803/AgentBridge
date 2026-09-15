"""The dev frontend checker must parse modules, validate imports, and clean up."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from devtools import check_frontend as checker


requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


@requires_node
def test_current_frontend_modules_pass_node_syntax_gate(capsys):
    assert checker.main() == 0
    count = len(list(checker.JS_DIR.glob("*.js")))
    assert f"all {count} modules pass" in capsys.readouterr().out


@requires_node
def test_malformed_module_fails_node_check_and_unlinks_temp_copy(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "broken.js").write_text("export const = ;\n", encoding="utf-8")
    created = []
    original = checker.tempfile.TemporaryDirectory

    def tracked_tempdir(*args, **kwargs):
        directory = original(*args, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(checker, "JS_DIR", tmp_path)
    monkeypatch.setattr(checker.tempfile, "TemporaryDirectory", tracked_tempdir)
    assert checker.main() == 1
    assert "FAIL  broken.js" in capsys.readouterr().out
    assert created and all(not path.exists() for path in created)


@requires_node
@pytest.mark.parametrize("source", [
    "import './missing.js';\n",
    "import { value } from './missing.js';\n",
    "export { value } from './missing.js';\n",
])
def test_missing_static_relative_import_fails_before_browser_runtime(
        tmp_path, monkeypatch, capsys, source):
    (tmp_path / "entry.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(checker, "JS_DIR", tmp_path)
    assert checker.main() == 1
    assert "relative import does not exist: ./missing.js" in capsys.readouterr().out


def test_missing_node_reports_a_named_checker_failure(monkeypatch, capsys):
    monkeypatch.setattr(checker.shutil, "which", lambda _name: None)
    assert checker.main() == 1
    assert "Node.js executable was not found" in capsys.readouterr().err
