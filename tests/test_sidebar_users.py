"""Public users inventory is source-bound and excludes private account fields."""
from __future__ import annotations

import pytest

from agentbridge.gui import sidebar_users
from agentbridge.mesh.aux_input_runtime import AuxInputRuntime
from agentbridge.store import local_source
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import owned_transport


def test_public_fallback_and_owner_hints_do_not_project_private_raw_fields(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc('users/alice.json', {
        'name': 'alice', 'kind': 'human', 'display': 'Alice',
        'auth': {'hash': 'secret'}, 'about': 'private bio',
        'privacy': {'about': 'nobody', 'photo': 'nobody'},
    })
    folder.put_doc('users/bob.json', {
        'name': 'bob', 'kind': 'agent', 'display': 'Bob',
        'agent': {'owner': 'alice', 'machine': 'laptop', 'harness': {'token': 'private'}},
    })
    folder.put_doc('users/carl.json', {
        'name': 'carl', 'kind': 'agent',
        'agent': {'owner': 'alice', 'machine': 'laptop'},
    })
    owned = owned_transport(folder, tmp_path / 'home')
    store = Store(tmp_path / 'cache.sqlite')
    runtime = AuxInputRuntime(owned, store)
    try:
        assert runtime.ingest('users').ready
        reader, receipt, inputs = runtime.inputs('users', max_documents=2048,
                                                 max_bytes=2 * 1024 * 1024)
        monkeypatch.setattr(folder, 'list_docs',
                            lambda *_args, **_kwargs: pytest.fail('foreground provider walk'))
        users = sidebar_users.public_users(inputs)
        assert set(users) == {'alice', 'bob', 'carl'}
        assert users['alice']['display'] == 'Alice'
        assert users['bob']['owners'] == ['alice']
        assert users['bob']['kind'] == 'agent'
        assert not any(word in str(users) for word in ('secret', 'private bio', 'token'))
        assert sidebar_users.owner_candidates(inputs, 'alice') == ('bob', 'carl')
        with pytest.raises(sidebar_users.UsersUnavailable, match='owner_candidate_budget'):
            sidebar_users.owner_candidates(inputs, 'alice', max_candidates=1)
        with reader.finalization(receipt):
            pass
        owned.put_doc('users/alice.json', {'name': 'alice', 'display': 'New'})
        with pytest.raises(local_source.SourceChanged):
            with reader.finalization(receipt):
                pass
    finally:
        runtime.stop()
        store.close()
        owned.close()
