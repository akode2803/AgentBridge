"""Two-prefix identity source is background-only and exact-read-only."""
from __future__ import annotations

import pytest

from agentbridge.mesh.aux_input_runtime import AuxInputRuntime
from agentbridge.store import document_observation as docs, local_source
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import owned_transport


def test_users_peer_and_two_prefix_identity_scopes_are_distinct(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc('users/alice.json', {'name': 'alice', 'kind': 'human'})
    folder.put_doc('lifecycle/alice/one.json', {'event': 'active'})
    folder.put_doc('runtime/owner-control/alice/commands/one.json', {'id': 'one'})
    owned = owned_transport(folder, tmp_path / 'owner')
    store = Store(tmp_path / 'cache.sqlite')
    runtime = AuxInputRuntime(owned, store)
    try:
        users = runtime.ingest('users')
        peer = runtime.ingest('peer')
        identities = runtime.ingest('identities')
        assert users.ready and peer.ready and identities.ready
        reader = runtime.reader('identities')
        assert tuple(s.value for s in reader.definition.selectors) == ('lifecycle', 'users')
        receipt = reader.capture()
        with pytest.raises(ValueError, match='exact selected'):
            reader.capture_documents(receipt)
        with reader._read(receipt) as (conn, current):
            exact = docs._capture_selected(conn, store.path, current.source.raw,
                ('users/alice.json', 'lifecycle/alice/one.json'),
                max_documents=2, max_bytes=1024)
        assert exact.documents()['users/alice.json']['name'] == 'alice'
        assert exact.documents()['lifecycle/alice/one.json']['event'] == 'active'
        assert runtime.inputs('users')[2].documents.document('lifecycle/alice/one.json') is None
        assert runtime.inputs('peer')[2].documents.document(
            'runtime/owner-control/alice/commands/one.json')['id'] == 'one'
        owned.put_doc('lifecycle/alice/two.json', {'event': 'removed'})
        with pytest.raises(local_source.SourceChanged):
            reader.capture()
        assert runtime.inputs('users')[1].source.ready
        assert runtime.inputs('peer')[1].source.ready
    finally:
        runtime.stop()
        store.close()
        owned.close()
