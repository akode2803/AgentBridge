"""Frontend identity changes with served code even at an unchanged release version."""
from agentbridge.gui.frontend_revision import frontend_revision


def test_frontend_content_identity(tmp_path):
    assert frontend_revision(tmp_path) == ''
    script = tmp_path / 'js' / 'main.js'
    script.parent.mkdir()
    script.write_text('old', encoding='utf-8')
    old = frontend_revision(tmp_path)
    assert old == frontend_revision(tmp_path)
    script.write_text('new', encoding='utf-8')
    assert frontend_revision(tmp_path) != old
    current = frontend_revision(tmp_path)
    (tmp_path / 'unrelated.log').write_text('ignored', encoding='utf-8')
    assert frontend_revision(tmp_path) == current
    script.rename(script.with_name('other.js'))
    assert frontend_revision(tmp_path) != current


def test_changed_frontend_waits_for_draft_before_reloading(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = (Path(__file__).resolve().parents[1] / 'gui/static/js/main.js').read_text()
    body = source[source.index('  const v = App.state.frontend_revision'):source.index('  const restarting = restartIntent();')]
    program = '''
const assert = require('node:assert/strict');
let bootVersion='', reloadArmed=false, draft='', modal=false, reloads=0;
const App={state:{gui_version:'same',frontend_revision:'first'}};
const document={querySelector:()=>modal,getElementById:()=>({value:draft})};
const location={reload:()=>reloads++};
function check(){ BODY }
check(); assert.equal(reloads,0);
App.state.frontend_revision='second'; draft='unsent message';
check(); assert.equal(reloads,0);
draft=''; modal=true; check(); assert.equal(reloads,0);
modal=false; check(); assert.equal(reloads,1);
'''.replace('BODY', body)
    runner = tmp_path / 'revision.cjs'
    runner.write_text(program)
    result = subprocess.run([node, str(runner)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unreadable_or_oversized_asset_falls_back(tmp_path, monkeypatch):
    from pathlib import Path
    script = tmp_path / 'main.js'
    script.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
    assert frontend_revision(tmp_path) == ''
    script.write_text('small')
    def unavailable(*args, **kwargs):
        raise OSError('changed during startup')
    monkeypatch.setattr(Path, 'open', unavailable)
    assert frontend_revision(tmp_path) == ''
