"""Inactive outer transport owner for durable local-source invalidation.

Install only with registered source readers and publication gates. Delegation is
explicit: a new mutating driver API must not silently bypass this owner.
"""
from __future__ import annotations

from .base import Transport
from ..store.source_selectors import Selector
from ..store.mutation_coordinator import MutationCoordinator


class LocalMutationTransport(Transport):
    def __init__(self, transport, coordinator):
        if not isinstance(transport, Transport) or type(coordinator) is not MutationCoordinator:
            raise TypeError('expected transport and local mutation coordinator')
        if isinstance(transport, LocalMutationTransport):
            raise ValueError('nested local mutation owners are unsupported')
        self._transport = transport
        self._coordinator = coordinator

    @property
    def scheme(self):
        return self._transport.scheme

    @property
    def profile(self):
        return self._transport.profile

    @property
    def max_upload_bytes(self):
        return self._transport.max_upload_bytes

    @property
    def has_change_feed(self):
        return self._transport.has_change_feed

    @property
    def supports_exclusive_create(self):
        return self._transport.supports_exclusive_create

    def __getattr__(self, name):
        # Reviewed optional identity/lifecycle/read APIs, never arbitrary methods.
        # purge_deleted_docs only removes already-deleted provider tombstones;
        # it cannot remove a live logical document, but does erase delta history.
        # Admission must use a complete selection with explicit absence, never a
        # delta cursor alone. Changes to this contract need mutation mediation.
        if name in ('root', 'cache_key', 'subscribe_changes', 'warm', 'warm_async',
                    'refresh_now', 'set_interactive', 'mirror_status', 'latency_lane',
                    'realtime_status', 'transfer_stats', 'hint_now', 'wake_local',
                    'purge_deleted_docs'):
            return getattr(self._transport, name)
        raise AttributeError(name)

    def close(self):
        close = getattr(self._transport, 'close', None)
        if callable(close):
            return close()
        return None

    def capture_mirror(self, *, max_documents=100_000, max_chat_ids=100_000,
                       max_bytes=64 * 1024 * 1024):
        return self._transport.capture_mirror(
            max_documents=max_documents, max_chat_ids=max_chat_ids, max_bytes=max_bytes)

    def validate_mirror_position(self, expected):
        return self._transport.validate_mirror_position(expected)

    def capture_mirror_selection(self, request):
        return self._transport.capture_mirror_selection(request)

    def _mutate(self, changes, operation):
        intent = self._coordinator.begin(changes)
        # Exceptions intentionally retain the durable intent. A later retry's
        # success cannot clear this older operation's ambiguous outcome.
        result = operation()
        self._coordinator.complete(intent)
        return result

    def put_doc(self, path, data):
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.put_doc(path, data))

    def create_doc(self, path, data):
        # Delegate directly; base create_doc may call its own put_doc, but it
        # must not create a second outer intent.
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.create_doc(path, data))

    def delete_doc(self, path):
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.delete_doc(path))

    def create_effect_doc(self, path, data, *, ask_envelope=None, decision_envelope=None):
        if type(path) is not str:
            raise ValueError('invalid effect path')
        changes = [Selector('doc_exact', path)]
        parent = path.rsplit('/', 1)[0]
        if ask_envelope is not None:
            changes.append(Selector('doc_exact', parent + '/grant-ask.json'))
        if decision_envelope is not None:
            changes.append(Selector('doc_exact', parent + '/grant-decision.json'))
        return self._mutate(tuple(changes), lambda: self._transport.create_effect_doc(
            path, data, ask_envelope=ask_envelope, decision_envelope=decision_envelope))

    def append_log(self, chat_id, log_name, record):
        return self._mutate((Selector('log_chat', chat_id),),
                            lambda: self._transport.append_log(chat_id, log_name, record))

    def delete_chat(self, chat_id):
        # The log selector validates the chat identity before any external call.
        if type(chat_id) is not str:
            raise ValueError('invalid chat identity')
        return self._mutate((Selector('doc_prefix', f'chats/{chat_id}'), Selector('log_chat', chat_id)),
                            lambda: self._transport.delete_chat(chat_id))

    def get_doc(self, path, default=None):
        return self._transport.get_doc(path, default)

    def list_docs(self, prefix):
        return self._transport.list_docs(prefix)

    def list_cached_docs(self, prefix):
        return self._transport.list_cached_docs(prefix)

    def list_cached_docs_bounded(self, prefix, limit):
        return self._transport.list_cached_docs_bounded(prefix, limit)

    def cached_docs_bounded(self, prefix, limit):
        return self._transport.cached_docs_bounded(prefix, limit)

    def get_docs(self, prefix=''):
        return self._transport.get_docs(prefix)

    def snapshot_docs(self):
        return self._transport.snapshot_docs()

    def get_docs_delta(self, cursor):
        return self._transport.get_docs_delta(cursor)

    def list_chat_ids(self):
        return self._transport.list_chat_ids()

    def list_logs(self, chat_id):
        return self._transport.list_logs(chat_id)

    def read_log(self, chat_id, log_name, offset=0):
        return self._transport.read_log(chat_id, log_name, offset)

    def changed_logs(self, cursor):
        return self._transport.changed_logs(cursor)

    def suggest_poll_s(self, default):
        return self._transport.suggest_poll_s(default)

    def note_log_poll(self, *, changed, hinted):
        return self._transport.note_log_poll(changed=changed, hinted=hinted)

    def effect_claims_ready(self):
        return self._transport.effect_claims_ready()

    @staticmethod
    def _blob_changes(path):
        # Drivers expose byte writes to arbitrary logical paths. Even though
        # normal attachments live outside raw-document scope, this API must not
        # bypass invalidation when used to replace a document or a writer log.
        changes = [Selector('doc_exact', path)]
        if type(path) is str:
            parts = path.split('/')
            if len(parts) == 4 and parts[0] == 'chats' and parts[2] == 'msgs':
                changes.append(Selector('log_chat', parts[1]))
        return tuple(changes)

    def put_blob(self, path, data):
        return self._mutate(self._blob_changes(path), lambda: self._transport.put_blob(path, data))

    def put_blob_from(self, local_src, path):
        return self._mutate(self._blob_changes(path), lambda: self._transport.put_blob_from(local_src, path))

    def get_blob(self, path):
        return self._transport.get_blob(path)

    def blob_size(self, path):
        return self._transport.blob_size(path)

    def delete_blob(self, path):
        return self._mutate(self._blob_changes(path), lambda: self._transport.delete_blob(path))

    def local_path(self, path):
        return self._transport.local_path(path)

    def watch(self):
        return self._transport.watch()


def root_identity(transport):
    """Trusted built-in transport identity, excluding member credentials.

    Production composition must use owned_transport, before writable references
    escape. Unknown drivers need an explicit identity contract before activation.
    """
    import json
    import os
    from urllib.parse import urlsplit, urlunsplit
    from .cache import CachingTransport
    from .folder import FolderTransport, _unextend
    from .supabase import SupabaseTransport

    if type(transport) is CachingTransport:
        if type(transport.inner) not in (FolderTransport, SupabaseTransport):
            raise ValueError('unsupported nested transport owner')
        from .mirror_observation import validate_identity
        if (transport._mirror_root_identity != validate_identity(transport.inner.root)
                or transport._mirror_cache_identity != validate_identity(getattr(transport.inner, 'cache_key', None))):
            raise ValueError('cache identity changed')
        return root_identity(transport.inner)
    if type(transport) is FolderTransport:
        fields = ['local-root-v1', 'folder', os.path.normcase(_unextend(str(transport.root.resolve())))]
    elif type(transport) is SupabaseTransport:
        url, root = transport._env.get('SUPABASE_URL'), transport.root
        if type(url) is not str or not url or len(url) > 4096 or type(root) is not str or not root:
            raise ValueError('invalid provider identity')
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.hostname.endswith('.')
                or not parsed.hostname.isascii()
                or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
            raise ValueError('invalid provider endpoint')
        host, port = parsed.hostname.lower(), parsed.port
        if ':' in host:
            host = '[' + host + ']'
        if port is not None and port != (443 if parsed.scheme == 'https' else 80):
            host += ':' + str(port)
        canonical = urlunsplit((parsed.scheme, host, parsed.path.rstrip('/'), '', ''))
        if transport.cache_key != f'supabase:{url}:{root}':
            raise ValueError('provider identity changed')
        fields = ['local-root-v1', 'supabase', canonical, root]
    else:
        raise ValueError('unsupported transport owner')
    identity = json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
    if len(identity.encode()) > 4096:
        raise ValueError('transport identity budget')
    return identity


def owned_transport(transport, home):
    """Inactive composition factory. Register Stores before serving any pages."""
    coordinator = MutationCoordinator(home, root_identity(transport))
    return LocalMutationTransport(transport, coordinator)
