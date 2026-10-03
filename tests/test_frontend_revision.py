"""Frontend identity changes with served code even at an unchanged release version."""
from contextlib import contextmanager
from pathlib import Path

import pytest

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


@pytest.mark.parametrize('entry_count', [512, 513])
def test_frontend_revision_entry_limit_includes_ignored_files(tmp_path, entry_count):
    (tmp_path / 'main.js').write_bytes(b'asset')
    expected = frontend_revision(tmp_path)
    assert len(expected) == 64
    for index in range(entry_count - 1):
        (tmp_path / f'ignored-{index:03}.log').touch()

    assert frontend_revision(tmp_path) == (expected if entry_count == 512 else '')


def test_frontend_revision_stops_traversal_at_first_excess_entry(tmp_path, monkeypatch):
    script = tmp_path / 'main.js'
    script.write_bytes(b'asset')
    visited = []

    def entries(directory, pattern):
        assert directory == tmp_path and pattern == '*'
        for index in range(513):
            visited.append(index)
            yield script if index == 0 else tmp_path / f'ignored-{index}.log'
        pytest.fail('Traversal continued beyond the rejection boundary')

    def unexpected_open(*args, **kwargs):
        pytest.fail('An over-budget directory must be rejected before reading assets')

    monkeypatch.setattr(Path, 'rglob', entries)
    monkeypatch.setattr(Path, 'open', unexpected_open)
    assert frontend_revision(tmp_path) == ''
    assert len(visited) == 513


def _record_asset_reads(monkeypatch):
    original_open = Path.open
    reads = []

    @contextmanager
    def tracked_open(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            class Reader:
                def read(self, size):
                    reads.append((path.name, size))
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(Path, 'open', tracked_open)
    return reads


@pytest.mark.parametrize('excess', [0, 1])
def test_frontend_revision_per_asset_byte_boundary(tmp_path, monkeypatch, excess):
    limit = 4 * 1024 * 1024
    (tmp_path / 'main.js').write_bytes(b'x' * (limit + excess))
    reads = _record_asset_reads(monkeypatch)

    revision = frontend_revision(tmp_path)

    assert (len(revision) == 64) if excess == 0 else (revision == '')
    assert reads == [('main.js', limit + 1)]


@pytest.mark.parametrize('excess', [0, 1])
def test_frontend_revision_aggregate_byte_boundary(tmp_path, monkeypatch, excess):
    asset_limit = 4 * 1024 * 1024
    for index in range(4):
        (tmp_path / f'{index}.js').write_bytes(b'x' * asset_limit)
    # An empty asset is still valid once the exact 16 MiB allowance is spent.
    (tmp_path / '4.js').write_bytes(b'x' * excess)
    reads = _record_asset_reads(monkeypatch)

    revision = frontend_revision(tmp_path)

    assert (len(revision) == 64) if excess == 0 else (revision == '')
    assert reads == [(f'{index}.js', asset_limit + 1) for index in range(4)] + [('4.js', 1)]


def test_frontend_revision_digest_is_independent_of_traversal_order(tmp_path, monkeypatch):
    (tmp_path / 'js').mkdir()
    for name, content in [('index.html', b'page'), ('style.css', b'css'),
                          ('js/main.js', b'script'), ('ignored.log', b'ignored')]:
        (tmp_path / name).write_bytes(content)
    entries = list(tmp_path.rglob('*'))
    expected = frontend_revision(tmp_path)
    assert len(expected) == 64

    for ordered in (entries, list(reversed(entries)), entries[2:] + entries[:2]):
        monkeypatch.setattr(Path, 'rglob', lambda directory, pattern: iter(ordered))
        assert frontend_revision(tmp_path) == expected


@pytest.mark.parametrize('yield_asset_first', [False, True])
def test_frontend_revision_traversal_failure_discards_partial_inputs(
    tmp_path, monkeypatch, yield_asset_first,
):
    script = tmp_path / 'main.js'
    script.write_bytes(b'asset')

    def unavailable(directory, pattern):
        if yield_asset_first:
            yield script
        raise OSError('directory changed during traversal')

    monkeypatch.setattr(Path, 'rglob', unavailable)
    assert frontend_revision(tmp_path) == ''


@pytest.mark.parametrize('failure_stage', ['stat', 'open', 'read'])
def test_frontend_revision_asset_failure_discards_partial_digest(
    tmp_path, monkeypatch, failure_stage,
):
    good = tmp_path / '0.js'
    failing = tmp_path / '1.js'
    good.write_bytes(b'already hashed')
    failing.write_bytes(b'unavailable')
    method_name = 'is_file' if failure_stage == 'stat' else 'open'
    original = getattr(Path, method_name)
    reads = []

    @contextmanager
    def tracked_stream(path, *args, **kwargs):
        with original(path, *args, **kwargs) as stream:
            class Reader:
                def read(self, size):
                    reads.append(path.name)
                    if path == failing:
                        raise OSError('asset changed during read')
                    return stream.read(size)

            yield Reader()

    def unavailable(path, *args, **kwargs):
        if failure_stage == 'read':
            return tracked_stream(path, *args, **kwargs)
        if path == failing:
            raise OSError('asset became unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method_name, unavailable)
    assert frontend_revision(tmp_path) == ''
    if failure_stage == 'read':
        assert reads == ['0.js', '1.js']
