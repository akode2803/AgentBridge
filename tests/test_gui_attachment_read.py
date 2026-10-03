"""Exact finalized attachment reads never authorize from a blob ID alone."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest

from agentbridge.gui import api_files
from agentbridge.gui import attachment_read
from agentbridge.gui.routing import Request, Response
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh

from test_gui_chat_pages import _ready, page_app as _shared_page_app


@pytest.fixture(name='page_app')
def _page_fixture(tmp_path, monkeypatch):
    yield from _shared_page_app.__wrapped__(tmp_path, monkeypatch)


def _posted(app, chat, raw=b'attachment-data', name='test.txt'):
    prepared = app.mesh.prepare_attachment(chat, name, raw)
    message = app.mesh.post(chat, 'attached', attachments=[prepared])
    _ready(app, chat)
    return message, prepared.record


def _request(app, chat, message_id, blob_id):
    req = Request(params={'chat': chat, 'message_id': message_id, 'id': blob_id})
    for _ in range(12):
        result = api_files.file(app, req)
        if not isinstance(result, dict) or result.get('status') != 'pending':
            return result
        app.mesh.local_inputs.prepare_one()
    pytest.fail(f'attachment preparation did not settle: {result}')


def test_exact_file_roundtrip_and_no_store(page_app):
    app, chat = page_app
    message, rec = _posted(app, chat)
    response = _request(app, chat, message.id, rec['id'])
    assert isinstance(response, Response), response
    assert response.body == b'attachment-data'
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('transport_cap,expected', [
    (0, 512 * 1024 * 1024),
    (32, 32),
    (512 * 1024 * 1024 + 1, 512 * 1024 * 1024),
])
def test_sealed_attachment_limit_uses_smallest_nonzero_cap(
        transport_cap, expected):
    class Tx:
        max_upload_bytes = transport_cap

    assert attachment_read._max_sealed_attachment_bytes(Tx()) == expected


def test_sealed_attachment_limit_defaults_when_transport_has_no_cap():
    class Tx:
        pass

    assert attachment_read._max_sealed_attachment_bytes(Tx()) == 512 * 1024 * 1024


def test_transport_attachment_limit_accepts_exact_boundary_and_rejects_next_byte(
        page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    path = P.file(chat, rec['id'])
    sealed = app.mesh.tx.get_blob(path)
    assert sealed
    opened = []
    open_blob = attachment_read.open_blob_observed

    def observed_open(*args):
        opened.append(True)
        return open_blob(*args)

    monkeypatch.setattr(attachment_read, 'open_blob_observed', observed_open)
    limits = [len(sealed)]
    monkeypatch.setattr(type(app.mesh.tx), 'max_upload_bytes',
                        property(lambda _tx: limits[0]))
    accepted = _request(app, chat, message.id, rec['id'])
    assert isinstance(accepted, Response) and accepted.body == b'attachment-data'
    assert opened

    opened.clear()
    limits[0] = len(sealed) - 1
    rejected = _request(app, chat, message.id, rec['id'])
    assert isinstance(rejected, dict) and rejected['status'] == 'unavailable'
    assert opened == []


def test_missing_wrong_hidden_and_mismatched_hints_never_fetch(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    other = app.mesh.post(chat, 'other')
    _ready(app, chat)
    original = app.mesh.tx.get_blob
    touched = []

    def observed(path):
        if path == P.file(chat, rec['id']):
            touched.append(path)
        return original(path)

    monkeypatch.setattr(app.mesh.tx, 'get_blob', observed)
    for hint in ('', 'missing', other.id):
        assert isinstance(_request(app, chat, hint, rec['id']), dict)
    assert _request(app, chat, message.id, 'wrong-blob')['error']
    app.mesh.hide(chat, [message.id])
    app.mesh.local_inputs.ingest(chat)
    assert _request(app, chat, message.id, rec['id'])['error']
    assert touched == []


@pytest.mark.parametrize('mutation', ('clear', 'redact'))
def test_cleared_or_redacted_target_never_fetches_blob(page_app, monkeypatch, mutation):
    app, chat = page_app
    message, rec = _posted(app, chat)
    if mutation == 'clear':
        app.mesh.clear_chat(chat)
    else:
        app.mesh.redact(chat, [message.id])
    app.mesh.local_inputs.ingest(chat)
    original = app.mesh.tx.get_blob
    fetched = []

    def observed(path):
        if path == P.file(chat, rec['id']):
            fetched.append(path)
        return original(path)

    monkeypatch.setattr(app.mesh.tx, 'get_blob', observed)
    assert _request(app, chat, message.id, rec['id'])['error']
    assert fetched == []


@pytest.mark.parametrize('mutation', ('hide', 'clear', 'redact'))
def test_mutation_during_blob_fetch_withholds_bytes(page_app, monkeypatch, mutation):
    app, chat = page_app
    message, rec = _posted(app, chat)
    original = app.mesh.tx.get_blob
    fetched = []

    def changed(path):
        data = original(path)
        if path == P.file(chat, rec['id']):
            fetched.append(path)
            if mutation == 'hide':
                app.mesh.hide(chat, [message.id])
            elif mutation == 'clear':
                app.mesh.clear_chat(chat)
            else:
                app.mesh.redact(chat, [message.id])
            app.mesh.local_inputs.ingest(chat)
        return data

    monkeypatch.setattr(app.mesh.tx, 'get_blob', changed)
    result = _request(app, chat, message.id, rec['id'])
    assert fetched and isinstance(result, dict) and result['error']


def test_tampered_sealed_blob_and_changed_file_record_fail(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    original = app.mesh.tx.get_blob

    def tampered(path):
        data = original(path)
        return data[:-1] + bytes([data[-1] ^ 1]) if path == P.file(chat, rec['id']) else data

    monkeypatch.setattr(app.mesh.tx, 'get_blob', tampered)
    result = _request(app, chat, message.id, rec['id'])
    assert result['status'] == 'unavailable'
    monkeypatch.setattr(app.mesh.tx, 'get_blob', original)
    assert _request(app, chat, message.id, rec['id']).body == b'attachment-data'


def test_unrelated_arrival_during_fetch_uses_fresh_final_authority(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    original = app.mesh.tx.get_blob
    fired = []

    def arrival(path):
        data = original(path)
        if path == P.file(chat, rec['id']) and not fired:
            fired.append(True)
            app.mesh.post(chat, 'unrelated')
            _ready(app, chat)
        return data

    monkeypatch.setattr(app.mesh.tx, 'get_blob', arrival)
    result = _request(app, chat, message.id, rec['id'])
    assert fired and isinstance(result, Response), result


def test_removed_member_cannot_fetch_or_probe_missing_message(page_app, monkeypatch):
    app, chat = page_app
    owner = app.mesh
    owner.accounts.create_human('peer', 'peer-password')
    owner.membership.add_members(chat, ['peer'])
    owner.membership.grant_admin(chat, 'peer')
    message, rec = _posted(app, chat)
    peer = Mesh(app.root, 'peer', 'peerbox', encrypt=True, home=app.home,
                store_path=app.home / 'peer-file.sqlite')
    try:
        peer.sync.sync_once([chat])
        peer.remove_member(chat, 'viewer')
        peer.outbox.flush_once()
        owner.sync.sync_once([chat])
        _ready(app, chat)
        fetched = []
        original = owner.tx.get_blob

        def observed(path):
            if path == P.file(chat, rec['id']):
                fetched.append(path)
            return original(path)

        monkeypatch.setattr(owner.tx, 'get_blob', observed)
        existing = _request(app, chat, message.id, rec['id'])
        absent = _request(app, chat, 'missing-message', rec['id'])
        assert existing['status'] == absent['status'] == 'forbidden'
        assert fetched == []
    finally:
        peer.close()


def test_member_removed_while_transport_fetch_waits(page_app, monkeypatch):
    app, chat = page_app
    owner = app.mesh
    owner.accounts.create_human('peer', 'peer-password')
    owner.membership.add_members(chat, ['peer'])
    owner.membership.grant_admin(chat, 'peer')
    message, rec = _posted(app, chat)
    peer = Mesh(app.root, 'peer', 'peerbox', encrypt=True, home=app.home,
                store_path=app.home / 'peer-file.sqlite')
    try:
        peer.sync.sync_once([chat])
        original = owner.tx.get_blob
        fetched = []

        def revoked(path):
            data = original(path)
            if path == P.file(chat, rec['id']):
                fetched.append(path)
                peer.remove_member(chat, 'viewer')
                peer.outbox.flush_once()
                owner.sync.sync_once([chat])
                _ready(app, chat)
            return data

        monkeypatch.setattr(owner.tx, 'get_blob', revoked)
        result = _request(app, chat, message.id, rec['id'])
        assert fetched and result['status'] == 'forbidden', result
    finally:
        peer.close()


def test_session_change_during_fetch_withholds_response(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    original = app.mesh.tx.get_blob
    fetched = []

    def signed_out(path):
        data = original(path)
        if path == P.file(chat, rec['id']):
            fetched.append(path)
            app.logout('secret')
        return data

    monkeypatch.setattr(app.mesh.tx, 'get_blob', signed_out)
    result = _request(app, chat, message.id, rec['id'])
    assert fetched and isinstance(result, dict) and result['error']


def test_sender_pending_spool_requires_canonical_admission(page_app):
    app, chat = page_app
    prepared = app.mesh.prepare_attachment(chat, 'pending.txt', b'pending')
    message = app.mesh.post(chat, 'pending', attachments=[prepared])
    assert app.mesh.attachments.local_sealed(prepared.record['id'])
    runtime = app.mesh.local_inputs
    runtime.prepare_one()
    runtime.ingest(chat)
    result = _request(app, chat, message.id, prepared.record['id'])
    assert isinstance(result, Response), result
    assert result.body == b'pending'


def test_blob_epoch_can_precede_hinted_message_epoch(page_app):
    app, chat = page_app
    first, rec = _posted(app, chat)
    app.mesh.accounts.create_human('peer', 'peer-password')
    app.mesh.membership.add_members(chat, ['peer'])
    later = app.mesh.post(chat, 'reference old sealed file', files=[rec])
    _ready(app, chat)
    result = _request(app, chat, later.id, rec['id'])
    assert isinstance(result, Response), result
    assert result.body == b'attachment-data'
    assert first.id != later.id


def test_open_replaces_same_length_stale_cache(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat, b'good')
    target = app.home / 'files_cache' / chat / api_files.cache_filename(rec['name'], rec['id'])
    target.parent.mkdir(parents=True)
    target.write_bytes(b'evil')
    opened = []
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    result = api_files.open_file(app, Request(data={'chat_id': chat, 'id': rec['id'],
                                                   'message_id': message.id}))
    assert result == {'ok': True}, result
    assert opened == [target] and target.read_bytes() == b'good'


@pytest.mark.parametrize('cached', (b'good', b'evil'))
def test_open_locked_cache_reuses_only_verified_private_bytes(page_app, monkeypatch, cached):
    app, chat = page_app
    message, rec = _posted(app, chat, b'good', name='report.pdf')
    target = app.home / 'files_cache' / chat / api_files.cache_filename(rec['name'], rec['id'])
    target.parent.mkdir(parents=True)
    target.write_bytes(cached)
    target.chmod(0o600)
    opened = []
    replace = api_files.os.replace

    def locked_target(source, dest):
        if dest == target:
            raise PermissionError(errno.EACCES, 'file held by Windows handler')
        return replace(source, dest)

    monkeypatch.setattr(api_files.os, 'replace', locked_target)
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    result = api_files.open_file(app, Request(data={'chat_id': chat, 'id': rec['id'],
                                                  'message_id': message.id}))
    assert result == {'ok': True}, result
    assert len(opened) == 1
    assert (opened[0] == target) == (cached == b'good')
    fresh = opened[0]
    assert fresh.parent == target.parent and fresh.suffix == '.pdf'
    assert fresh.read_bytes() == b'good' and target.read_bytes() == cached
    assert set(target.parent.iterdir()) == {target, fresh}
    if os.name != 'nt':
        assert fresh.stat().st_mode & 0o777 == 0o600
    for _ in range(4):
        assert api_files.open_file(app, Request(data={
            'chat_id': chat, 'id': rec['id'], 'message_id': message.id,
        })) == {'ok': True}
    assert opened == [fresh] * 5
    assert set(target.parent.iterdir()) == {target, fresh}


@pytest.mark.parametrize('failure', ('lock', 'session', 'handler'))
def test_open_locked_cache_cleans_unhanded_copy(page_app, monkeypatch, failure):
    app, chat = page_app
    message, rec = _posted(app, chat, b'good')
    target = app.home / 'files_cache' / chat / api_files.cache_filename(rec['name'], rec['id'])
    target.parent.mkdir(parents=True)
    target.write_bytes(b'evil')
    opened = []
    replace = api_files.os.replace

    def locked_target(source, dest):
        if dest != target:
            return replace(source, dest)
        if failure == 'lock':
            app.lock.path.write_text('{}', encoding='utf-8')
            app.lock.lock()
        elif failure == 'session':
            monkeypatch.setattr(app, 'validate_session_read', lambda _token: False)
        raise PermissionError(errno.EACCES, 'file held by Windows handler')

    def open_path(path):
        opened.append(path)
        raise OSError('handler unavailable')

    monkeypatch.setattr(api_files.os, 'replace', locked_target)
    monkeypatch.setattr(api_files.desktop, 'open_path', open_path)
    req = Request(data={'chat_id': chat, 'id': rec['id'], 'message_id': message.id})
    if failure == 'handler':
        with pytest.raises(OSError, match='handler unavailable'):
            api_files.open_file(app, req)
        assert len(opened) == 1
    else:
        assert api_files.open_file(app, req)['error']
        assert opened == []
    assert set(target.parent.iterdir()) == {target}
    assert target.read_bytes() == b'evil'


@pytest.mark.skipif(os.name != 'nt', reason='Windows denies replacing an open file')
def test_reopen_attachment_while_windows_reader_holds_cache(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    opened = []
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    req = Request(data={'chat_id': chat, 'id': rec['id'], 'message_id': message.id})
    assert api_files.open_file(app, req) == {'ok': True}
    target = opened[0]
    with target.open('rb') as held:
        for _ in range(5):
            assert api_files.open_file(app, req) == {'ok': True}
        assert held.read() == b'attachment-data'
    assert opened == [target] * 6
    assert list(target.parent.iterdir()) == [target]


def test_open_never_removes_a_copy_it_did_not_create(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    occupied = []
    opened = []
    write_private = api_files._write_private

    def collision(path, data):
        path.write_bytes(b'another request owns this file')
        occupied.append(path)
        write_private(path, data)

    monkeypatch.setattr(api_files, '_write_private', collision)
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    with pytest.raises(FileExistsError):
        api_files.open_file(app, Request(data={'chat_id': chat, 'id': rec['id'],
                                              'message_id': message.id}))
    assert opened == [] and len(occupied) == 1
    assert occupied[0].read_bytes() == b'another request owns this file'


def test_concurrent_open_requests_share_one_fallback(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat, b'good')
    target = app.home / 'files_cache' / chat / api_files.cache_filename(rec['name'], rec['id'])
    target.parent.mkdir(parents=True)
    target.write_bytes(b'evil')
    ready = threading.Barrier(4)
    opened = []
    replace = api_files.os.replace

    def read_attachment(*_args):
        # Isolate the post-verification disk handoff from unrelated page workers.
        ready.wait(timeout=5)
        return None, (rec, b'good')

    def locked_target(source, dest):
        if dest == target:
            raise PermissionError(errno.EACCES, 'file held by handler')
        return replace(source, dest)

    monkeypatch.setattr(api_files, 'read_attachment', read_attachment)
    monkeypatch.setattr(api_files.os, 'replace', locked_target)
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    req = Request(data={'chat_id': chat, 'id': rec['id'], 'message_id': message.id})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: api_files.open_file(app, req), range(4)))
    assert results == [{'ok': True}] * 4
    assert len(opened) == 4 and len(set(opened)) == 1
    assert opened[0] != target and opened[0].read_bytes() == b'good'
    assert set(target.parent.iterdir()) == {target, opened[0]}


@pytest.mark.timeout(20)
def test_another_process_cache_handoff_is_busy_until_released(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    code = (
        'import sys; from agentbridge.core.lock import SingleInstance; '
        'guard = SingleInstance(sys.argv[1], fail_open=False); '
        'print(guard.acquire(), flush=True); sys.stdin.read(1); guard.release()'
    )
    opened = []
    monkeypatch.setattr(api_files.desktop, 'open_path', opened.append)
    req = Request(data={'chat_id': chat, 'id': rec['id'], 'message_id': message.id})
    child = subprocess.Popen([sys.executable, '-c', code, str(app.home / 'files_cache.lock')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'True'
        assert api_files.open_file(app, req) == {'error': 'Attachment cache is busy; try again'}
        assert opened == []
        assert list((app.home / 'files_cache' / chat).iterdir()) == []
    finally:
        try:
            child.communicate('x', timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
    assert child.returncode == 0
    assert api_files.open_file(app, req) == {'ok': True}
    assert len(opened) == 1 and opened[0].read_bytes() == b'attachment-data'


def test_save_dialog_before_authority_and_partial_batch(page_app, monkeypatch, tmp_path):
    app, chat = page_app
    first, rec = _posted(app, chat)
    dest = tmp_path / 'saved'
    dest.mkdir()
    monkeypatch.setattr(api_files.desktop, 'pick_folder', lambda: str(dest))
    result = api_files.save(app, Request(data={'chat_id': chat, 'files': [
        {'message_id': first.id, 'id': rec['id']},
        {'message_id': 'absent', 'id': rec['id']},
    ]}))
    assert result['error'] and result['saved'] == 1
    assert (dest / rec['name']).read_bytes() == b'attachment-data'
    (dest / rec['name']).unlink()

    def mutating_picker():
        app.mesh.hide(chat, [first.id])
        app.mesh.local_inputs.ingest(chat)
        return str(dest)

    monkeypatch.setattr(api_files.desktop, 'pick_folder', mutating_picker)
    denied = api_files.save(app, Request(data={'chat_id': chat, 'files': [
        {'message_id': first.id, 'id': rec['id']},
    ]}))
    assert denied['error'] and denied['saved'] == 0
    assert list(dest.iterdir()) == []


def test_failed_save_write_leaves_no_partial_visible_file(page_app, monkeypatch, tmp_path):
    app, chat = page_app
    message, rec = _posted(app, chat)
    dest = tmp_path / 'saved'
    dest.mkdir()
    monkeypatch.setattr(api_files.desktop, 'pick_folder', lambda: str(dest))
    original_write = api_files._write_private
    original_fdopen = api_files.os.fdopen
    observed_modes = []

    class PartialFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, data):
            observed_modes.append(os.fstat(self.handle.fileno()).st_mode & 0o777)
            self.handle.write(data[:3])
            raise OSError('disk full')

    def failing_write(path, data):
        if path.parent == dest and path.name.startswith('.agentbridge-'):
            with monkeypatch.context() as context:
                context.setattr(api_files.os, 'fdopen',
                                lambda fd, mode: PartialFile(original_fdopen(fd, mode)))
                return original_write(path, data)
        return original_write(path, data)

    monkeypatch.setattr(api_files, '_write_private', failing_write)
    result = api_files.save(app, Request(data={'chat_id': chat, 'files': [
        {'message_id': message.id, 'id': rec['id']},
    ]}))
    assert result['error'] and result['saved'] == 0
    assert list(dest.iterdir()) == []
    if os.name != 'nt':
        assert observed_modes == [0o600]


@pytest.mark.parametrize('fail', (False, True))
def test_save_without_hardlink_support_keeps_exclusive_names_and_cleans_failure(
        page_app, monkeypatch, tmp_path, fail):
    app, chat = page_app
    message, rec = _posted(app, chat)
    dest = tmp_path / 'external'
    dest.mkdir()
    monkeypatch.setattr(api_files.desktop, 'pick_folder', lambda: str(dest))
    monkeypatch.setattr(api_files.os, 'link', lambda *_a: (_ for _ in ()).throw(
        OSError(errno.EOPNOTSUPP, 'hardlinks unsupported')))
    if fail:
        original_write = api_files._write_private
        original_fdopen = api_files.os.fdopen

        class PartialFile:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def write(self, data):
                self.handle.write(data[:3])
                raise OSError('storage failed')

        def partial_write(path, data):
            if path.parent == dest and path.name == rec['name']:
                with monkeypatch.context() as context:
                    context.setattr(api_files.os, 'fdopen',
                                    lambda fd, mode: PartialFile(original_fdopen(fd, mode)))
                    return original_write(path, data)
            return original_write(path, data)

        monkeypatch.setattr(api_files, '_write_private', partial_write)
    result = api_files.save(app, Request(data={'chat_id': chat, 'files': [
        {'message_id': message.id, 'id': rec['id']},
    ]}))
    if fail:
        assert result['error'] and result['saved'] == 0
        assert list(dest.iterdir()) == []
    else:
        assert result['saved'] == 1, result
        assert (dest / rec['name']).read_bytes() == b'attachment-data'


def test_lock_during_fetch_withholds_bytes_and_reports_lock(page_app, monkeypatch):
    app, chat = page_app
    message, rec = _posted(app, chat)
    original = app.mesh.tx.get_blob
    app.lock.path.write_text('{}', encoding='utf-8')

    def locked(path):
        data = original(path)
        if path == P.file(chat, rec['id']):
            app.lock.lock()
        return data

    monkeypatch.setattr(app.mesh.tx, 'get_blob', locked)
    result = _request(app, chat, message.id, rec['id'])
    assert isinstance(result, dict) and result.get('locked') is True, result
    assert result['status'] == 'locked'
