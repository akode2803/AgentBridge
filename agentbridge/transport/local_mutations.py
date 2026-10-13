"""Inactive outer transport owner for durable local-source invalidation.

Install only with registered source readers and publication gates. Delegation is
explicit: a new mutating driver API must not silently bypass this owner.
"""
from __future__ import annotations

import threading

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
        self._provider_transport = transport
        self._provider_identity_owner = None
        self._coordinator = coordinator
        self._provider_guard = threading.RLock()
        self._provider_principal = None
        self._provider_revision = None
        self._journal_writer = None
        self._journal_binding = None
        self._legacy_writes = 0

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
    def supports_change_ledger(self):
        return self._transport.supports_change_ledger

    @property
    def supports_source_ledger(self):
        return self._transport.supports_source_ledger

    @property
    def supports_scoped_source_reads(self):
        return self._transport.supports_scoped_source_reads

    @property
    def supports_recovery_source(self):
        return self._transport.supports_recovery_source

    @property
    def supports_reference_source(self):
        return self._transport.supports_reference_source

    @property
    def supports_mutation_protocol(self):
        return self._provider_read(
            lambda provider: provider.supports_mutation_protocol,
        )

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
                    ):
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

    def mirror_changes_since(self, request):
        return self._transport.mirror_changes_since(request)

    def _legacy_write(self, operation):
        """Admit a legacy write atomically against one-way journal binding."""
        with self._provider_guard:
            if self._journal_writer is not None:
                raise ValueError('typed mutation journal is bound')
            self._legacy_writes += 1
        try:
            return operation()
        finally:
            with self._provider_guard:
                self._legacy_writes -= 1

    def _mutate(self, changes, operation):
        def admitted():
            intent = self._coordinator.begin(changes)
            # Exceptions intentionally retain the durable intent. A later
            # success cannot clear this older operation's ambiguous outcome.
            result = operation()
            self._coordinator.complete(intent)
            return result
        return self._legacy_write(admitted)

    def _bound_writer(self):
        writer = self._journal_writer
        if writer is None:
            return None
        binding = self._journal_binding
        if binding is None:
            raise ValueError('local mutation journal binding changed')
        driver, journal, rpc, root, actor, endpoint, epoch = binding
        if (writer.driver is not driver or driver.journal is not journal
                or driver.rpc is not rpc or journal.coordinator is not self._coordinator
                or (writer.root, writer.actor_id, writer.endpoint,
                    writer.protocol_epoch) != (root, actor, endpoint, epoch)
                or (journal.root, journal.actor_id, journal.endpoint)
                != (root, actor, endpoint)
                or (rpc.root, rpc.actor_id, rpc.endpoint)
                != (root, actor, endpoint)):
            raise ValueError('local mutation journal binding changed')
        driver._check_owner()
        self._assert_provider_identity()
        provider = self._identity_owner()
        if rpc._provider is not provider:
            raise ValueError('local mutation journal provider changed')
        rpc._check_identity()
        return writer

    def _typed_success(self):
        # Typed receipts establish the write, but never establish a fresh
        # local mirror value. Ask the normal bounded mirror path to refresh.
        provider = self._identity_owner()
        for method in (provider.wake_local, provider.hint_now):
            try:
                method()
            except Exception:
                pass  # Best-effort hints cannot downgrade a durable receipt.

    def _typed_submit(self, kind, arguments, operation_id):
        writer = self._bound_writer()
        if operation_id is None:
            raise ValueError('typed mutation requires an operation id')
        writer.submit(kind, arguments, operation_id=operation_id)
        self._bound_writer()
        self._typed_success()
        return None

    @staticmethod
    def _reject_unbound_key(operation_id):
        if operation_id is not None:
            raise ValueError('typed mutation journal is not bound')

    def write_state_action(self, chat, user, kind, arguments, *, signer, verifier):
        """Persist a user-state action before observing or signing its attempt."""
        from ..store.user_state_actions import UserStateActionQueue

        writer = self._bound_writer()
        if writer is None:
            raise ValueError('typed mutation journal is not bound')
        result = UserStateActionQueue(writer.driver).write(
            chat, user, kind, arguments, signer=signer, verifier=verifier)
        self._bound_writer()
        if result[1]:
            self._typed_success()
        return result

    def recover_state_actions(self, user, owner, *, limit=64, after_seq=0):
        """Recover only a previously bound writer; startup never opts it in."""
        from ..store.user_state_actions import UserStateActionQueue, StateActionRecovery

        writer = self._bound_writer()
        if writer is None:
            return StateActionRecovery(0, (), 0)
        result = UserStateActionQueue(writer.driver).recover_pending(
            user, owner, limit=limit, after_seq=after_seq)
        if self._bound_writer() is not writer:
            raise ValueError('state action recovery writer changed')
        if result.completed:
            self._typed_success()
        return result

    def _identity_owners(self, writer):
        from ..store.provider_admission_owner import ProviderAdmissionOwnerJournal
        from .provider_admission import ProviderAdmission

        return ProviderAdmissionOwnerJournal(
            writer.driver.journal, ProviderAdmission(writer.driver.rpc))

    def create_agent_identity(self, name, prepare, *, keystore, directory):
        """Resume or mint one agent identity through the bound typed owner."""
        from ..mesh.identity_dispatch import CREATED, create_agent_identity

        writer = self._bound_writer()
        if writer is None:
            raise ValueError('typed mutation journal is not bound')
        outcome = create_agent_identity(
            self._identity_owners(writer), writer.driver, keystore, directory,
            name, prepare)
        if self._bound_writer() is not writer:
            raise ValueError('agent identity writer changed')
        if outcome.state == CREATED:
            self._typed_success()
        return outcome

    def create_human_identity(self, name, prepare, *, resume_allowed, keystore,
                              directory):
        """Resume or mint one human sign-up through the bound typed owner."""
        from ..mesh.identity_dispatch import CREATED, create_human_identity

        writer = self._bound_writer()
        if writer is None:
            raise ValueError('typed mutation journal is not bound')
        outcome = create_human_identity(
            self._identity_owners(writer), writer.driver, keystore, directory,
            name, prepare, resume_allowed=resume_allowed)
        if self._bound_writer() is not writer:
            raise ValueError('human identity writer changed')
        if outcome.state == CREATED:
            self._typed_success()
        return outcome

    def recover_agent_identities(self, *, keystore, directory):
        """Resume durable identity owners of a bound writer; never opts in."""
        from ..mesh.identity_dispatch import AgentIdentityDispatcher
        from ..mesh.identity_publication import OWNER_KIND

        writer = self._bound_writer()
        if writer is None:
            return ()
        owners = self._identity_owners(writer)
        dispatcher = AgentIdentityDispatcher(owners, writer.driver, keystore, directory)
        outcomes = []
        for owner_id in owners.owner_ids(OWNER_KIND):
            try:
                outcomes.append(dispatcher.run(owner_id))
            except KeyError:
                continue  # settled and reaped by a concurrent creation
        outcomes = tuple(outcomes)
        if self._bound_writer() is not writer:
            raise ValueError('agent identity writer changed')
        if outcomes:
            self._typed_success()
        return outcomes

    def update_doc(self, path, build):
        """Compare-and-swap a freshly observed document via the bound writer.

        ``build(current)`` must be a repeatable calculation over raw current
        data; it may run again after a conflict. Returns (document, changed).
        """
        writer = self._bound_writer()
        if writer is None:
            raise ValueError('typed mutation journal is not bound')
        document, changed = writer.update_document(path, build)
        self._bound_writer()
        if changed:
            self._typed_success()
        return document, changed

    def write_latest_doc(self, path, build):
        """Replace a per-device snapshot after its durable retry has settled."""
        writer = self._bound_writer()
        if writer is None:
            document = build()
            self.put_doc(path, document)
            return document
        document = writer.replace_document(path, build)
        self._bound_writer()
        self._typed_success()
        return document

    def put_doc(self, path, data, *, operation_id=None):
        if self._journal_writer is not None:
            return self._typed_submit('doc_put', {'p_path': path, 'p_data': data}, operation_id)
        self._reject_unbound_key(operation_id)
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.put_doc(path, data))

    def put_reserved_doc(self, path, data, reservation):
        """Finish an exact document write reserved in a canonical local cut."""
        if self._journal_writer is not None:
            raise NotImplementedError('reserved document writes need typed journal admission')
        from ..store.mutation_reservation import FinalizationMutation
        if (type(reservation) is not FinalizationMutation
                or reservation.coordinator is not self._coordinator
                or reservation.changes != (Selector('doc_exact', path),)):
            raise ValueError('foreign document reservation')
        return self._legacy_write(
            lambda: reservation.execute(lambda: self._transport.put_doc(path, data)))

    def create_doc(self, path, data, *, operation_id=None):
        if self._journal_writer is not None:
            return self._typed_submit('doc_create', {'p_path': path, 'p_data': data}, operation_id)
        self._reject_unbound_key(operation_id)
        # Delegate directly; base create_doc may call its own put_doc, but it
        # must not create a second outer intent.
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.create_doc(path, data))

    def create_exact_doc(self, path, data):
        """Create an immutable, uniquely named record.

        Under a bound writer the operation id derives from the path, so an
        exact retry of the same record replays its retained attempt instead of
        issuing a second write. A changed payload for that path fails closed.
        """
        writer = self._bound_writer()
        if writer is None:
            return self.create_doc(path, data)
        from uuid import NAMESPACE_URL, uuid5

        writer.create_exact(path, data, operation_id=str(
            uuid5(NAMESPACE_URL, 'agentbridge-exact-create:' + path)))
        self._bound_writer()
        self._typed_success()
        return None

    def settle_exact_creates(self, prefix):
        """Resume a bound writer's unfinished exact creates under ``prefix``."""
        writer = self._bound_writer()
        if writer is None:
            return 0
        settled = writer.settle_exact_creates(prefix)
        self._bound_writer()
        if settled:
            self._typed_success()
        return settled

    def delete_doc(self, path, *, operation_id=None):
        if self._journal_writer is not None:
            return self._typed_submit('doc_delete', {'p_path': path}, operation_id)
        self._reject_unbound_key(operation_id)
        return self._mutate((Selector('doc_exact', path),),
                            lambda: self._transport.delete_doc(path))

    def create_effect_doc(self, path, data, *, ask_envelope=None, decision_envelope=None,
                          operation_id=None):
        if self._journal_writer is not None:
            return self._typed_submit('effect_transition', {
                'p_path': path, 'p_data': data, 'p_grant_ask': ask_envelope,
                'p_grant_decision': decision_envelope,
            }, operation_id)
        self._reject_unbound_key(operation_id)
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
        writer = self._bound_writer()
        if writer is not None:
            writer.append_log(chat_id, log_name, record)
            self._bound_writer()
            self._typed_success()
            return None
        return self._mutate((Selector('log_chat', chat_id),),
                            lambda: self._transport.append_log(chat_id, log_name, record))

    def recover_append_log(self, chat_id, log_name, record):
        writer = self._bound_writer()
        if writer is None:
            return False
        found = writer.recover_append_log(chat_id, log_name, record)
        self._bound_writer()
        if found:
            self._typed_success()
        return found

    def delete_chat(self, chat_id, *, operation_id=None):
        if self._journal_writer is not None:
            return self._typed_submit('chat_delete', {'p_chat': chat_id}, operation_id)
        self._reject_unbound_key(operation_id)
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

    def _assert_provider_identity(self):
        """Fail before a node-source read if endpoint/root ownership moved."""
        try:
            current = root_identity(self._transport)
        except ValueError as exc:
            raise ValueError('local mutation owner identity changed') from exc
        if current != self._coordinator.identity:
            raise ValueError('local mutation owner identity changed')

    def _identity_revision(self):
        transport = self._identity_owner()
        revision = transport.node_identity_revision()
        if type(revision) is not int or revision < 0:
            raise ValueError('local mutation owner identity changed')
        return revision

    def _identity_owner(self):
        transport = self._transport
        if transport is not self._provider_transport:
            raise ValueError('local mutation owner identity changed')
        from .cache import CachingTransport
        from .supabase import SupabaseTransport
        if type(transport) is CachingTransport:
            transport = transport.inner
        if type(transport) is not SupabaseTransport:
            raise ValueError('unsupported transport owner')
        pinned = self._provider_identity_owner
        if pinned is None:
            self._provider_identity_owner = transport
        elif transport is not pinned:
            raise ValueError('local mutation owner identity changed')
        return transport

    def _principal_fence(self, expected):
        cut = self._identity_owner().recovery_fence()
        if (type(expected) is not str or not expected
                or getattr(cut, 'account_id', None) != expected):
            raise ValueError('local mutation owner principal changed')
        return cut

    def bind_provider_principal(self, principal):
        """Pin node-source reads to one authenticated provider principal."""
        if (type(principal) is not str or not principal
                or len(principal.encode('utf-8')) > 4096):
            raise ValueError('invalid provider principal')
        with self._provider_guard:
            self._assert_provider_identity()
            before = self._identity_revision()
            self._principal_fence(principal)
            self._assert_provider_identity()
            after = self._identity_revision()
            if after != before:
                raise ValueError('local mutation owner identity changed')
            if (self._provider_principal is not None
                    and self._provider_principal != principal):
                raise ValueError('local mutation owner principal changed')
            self._provider_principal = principal
            self._provider_revision = after

    def bind_journal(self, writer):
        """Opt into one exact authenticated typed writer, never implicitly."""
        from .application_mutations import ApplicationMutationWriter
        if type(writer) is not ApplicationMutationWriter:
            raise TypeError('invalid application mutation writer')
        with self._provider_guard:
            if self._journal_writer is not None:
                raise ValueError('typed mutation journal is already bound')
            if self._legacy_writes:
                raise ValueError('legacy mutation write is in flight')
            self._assert_provider_identity()
            provider = self._identity_owner()
            endpoint, root = provider_location(self._transport)
            rpc = writer.driver.rpc
            if (writer.driver.journal.coordinator is not self._coordinator
                    or rpc._provider is not provider
                    or (writer.endpoint, writer.root, writer.actor_id)
                    != (endpoint, root, rpc.actor_id)
                    or (rpc.endpoint, rpc.root) != (endpoint, root)
                    or (self._provider_principal is not None
                        and self._provider_principal != writer.actor_id)):
                raise ValueError('typed mutation journal owner mismatch')
            rpc._check_identity()
            # The RPC has authenticated this actor and pinned this revision.
            # Source reads must inherit the same binding even if node startup
            # has not called bind_provider_principal() separately.
            self._provider_principal = writer.actor_id
            self._provider_revision = rpc._revision
            self._journal_binding = (writer.driver, writer.driver.journal, rpc,
                                     writer.root, writer.actor_id, writer.endpoint,
                                     writer.protocol_epoch)
            self._journal_writer = writer

    def _provider_read(self, operation):
        """Return provider data only when one pinned location bounds the call."""
        with self._provider_guard:
            provider = self._identity_owner()
            self._assert_provider_identity()
            principal = self._provider_principal
            if principal is None:
                # Cold client creation publishes the chosen authenticated
                # session. Do that before the revision-bounded source cut so
                # its legitimate initialization is not confused with an ABA.
                provider.prepare_node_identity()
            before = self._identity_revision()
            if principal is not None:
                if self._provider_revision != before:
                    raise ValueError('local mutation owner identity changed')
                self._principal_fence(principal)
                if self._identity_revision() != before:
                    raise ValueError('local mutation owner identity changed')
            # Execute through the exact provider object pinned above. A mutable
            # cache wrapper is not an authenticated source owner and cannot
            # retarget this cut by swapping its public ``inner`` reference.
            # Do not hold the provider's identity lock across network I/O:
            # the cold-client path acquires its client lock before publishing
            # identity fields. The monotonic revision detects every change.
            result = operation(provider)
            if principal is not None:
                self._principal_fence(principal)
            self._identity_owner()
            self._assert_provider_identity()
            after = self._identity_revision()
            if after != before:
                raise ValueError('local mutation owner identity changed')
            return result

    def source_documents(self, paths, *, max_bytes):
        return self._provider_read(
            lambda provider: provider.source_documents(
                paths, max_bytes=max_bytes,
            ),
        )

    def source_log_page(
            self, chat_id, log_name, *, after_cursor, through_cursor, limit,
            max_bytes):
        return self._provider_read(
            lambda provider: provider.source_log_page(
                chat_id, log_name, after_cursor=after_cursor,
                through_cursor=through_cursor, limit=limit,
                max_bytes=max_bytes,
            ),
        )

    def source_ledger_fence(self):
        return self._provider_read(lambda provider: provider.source_ledger_fence())

    def source_ledger_events(self, after_cursor, *, limit):
        return self._provider_read(
            lambda provider: provider.source_ledger_events(
                after_cursor, limit=limit,
            ),
        )

    def recovery_fence(self):
        return self._provider_read(lambda provider: provider.recovery_fence())

    def mutation_protocol_state(self):
        def read(provider):
            state = provider.mutation_protocol_state()
            principal = self._provider_principal
            if (principal is not None
                    and getattr(state, 'account_id', None) != principal):
                raise ValueError('local mutation owner principal changed')
            return state
        return self._provider_read(read)

    def recovery_page(self, family, after, *, limit, max_bytes):
        return self._provider_read(
            lambda provider: provider.recovery_page(
                family, after, limit=limit, max_bytes=max_bytes,
            ),
        )

    def reference_documents(self, after, *, limit, max_bytes):
        return self._provider_read(
            lambda provider: provider.reference_documents(
                after, limit=limit, max_bytes=max_bytes,
            ),
        )

    def reference_streams(self, after, *, limit):
        return self._provider_read(
            lambda provider: provider.reference_streams(after, limit=limit),
        )

    def reference_log_page(
            self, chat_id, log_name, *, after_cursor, through_cursor, limit,
            max_bytes):
        return self._provider_read(
            lambda provider: provider.reference_log_page(
                chat_id, log_name, after_cursor=after_cursor,
                through_cursor=through_cursor, limit=limit,
                max_bytes=max_bytes,
            ),
        )

    def change_ledger_capability(self):
        return self._provider_read(
            lambda provider: provider.change_ledger_capability(),
        )

    def change_ledger_epoch(self):
        return self._provider_read(lambda provider: provider.change_ledger_epoch())

    def change_ledger_events(self, after_cursor, *, limit):
        return self._provider_read(
            lambda provider: provider.change_ledger_events(
                after_cursor, limit=limit,
            ),
        )

    def subscribe_change_ledger(self, callback):
        self._assert_provider_identity()
        return self._transport.subscribe_change_ledger(callback)

    def change_ledger_realtime_status(self):
        return self._transport.change_ledger_realtime_status()

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
        if self._journal_writer is not None:
            raise NotImplementedError('blob writes need typed journal admission')
        return self._mutate(self._blob_changes(path), lambda: self._transport.put_blob(path, data))

    def put_blob_from(self, local_src, path):
        if self._journal_writer is not None:
            raise NotImplementedError('blob writes need typed journal admission')
        return self._mutate(self._blob_changes(path), lambda: self._transport.put_blob_from(local_src, path))

    def get_blob(self, path):
        return self._transport.get_blob(path)

    def blob_size(self, path):
        return self._transport.blob_size(path)

    def delete_blob(self, path):
        if self._journal_writer is not None:
            raise NotImplementedError('blob writes need typed journal admission')
        return self._mutate(self._blob_changes(path), lambda: self._transport.delete_blob(path))

    def purge_deleted_docs(self, older_than_days=30.0):
        if self._journal_writer is not None:
            raise NotImplementedError('purge needs typed journal admission')
        return self._legacy_write(
            lambda: self._transport.purge_deleted_docs(older_than_days))

    def local_path(self, path):
        return self._transport.local_path(path)

    def watch(self):
        return self._transport.watch()


def provider_location(transport):
    """Return the credential-free endpoint and root for one built-in owner."""
    from urllib.parse import urlsplit, urlunsplit
    from .cache import CachingTransport
    from .supabase import SupabaseTransport

    if type(transport) is CachingTransport:
        if type(transport.inner) is not SupabaseTransport:
            raise ValueError('unsupported nested transport owner')
        from .mirror_observation import transport_identities
        if (transport._mirror_root_identity, transport._mirror_cache_identity) != transport_identities(transport.inner):
            raise ValueError('cache identity changed')
        return provider_location(transport.inner)
    if type(transport) is SupabaseTransport:
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
        return canonical, root
    else:
        raise ValueError('unsupported transport owner')


def root_identity(transport):
    """Trusted built-in transport identity, excluding member credentials.

    Production composition must use owned_transport, before writable references
    escape. Unknown drivers need an explicit identity contract before activation.
    """
    import json

    canonical, root = provider_location(transport)
    fields = ['local-root-v1', 'supabase', canonical, root]
    identity = json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
    if len(identity.encode()) > 4096:
        raise ValueError('transport identity budget')
    return identity


def owned_transport(transport, home):
    """Inactive composition factory. Register Stores before serving any pages."""
    coordinator = MutationCoordinator(home, root_identity(transport))
    return LocalMutationTransport(transport, coordinator)
