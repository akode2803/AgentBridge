"""Inactive complete raw-document collection for local SQLite ingestion.

These bounded background reads do not verify permission or establish a remote
atomic snapshot. Exact absence is distinct from unreadable/malformed input.
"""
from __future__ import annotations

import json

from ..store import document_observation as docs, source_selectors as scopes
from .authority_observation import AuthorityObservationUnavailable, _JSONBudget, _position_locked
from .cache import CachingTransport


class RawCollectionUnavailable(RuntimeError):
    pass


def collect_documents(transport, definition, *, max_documents=20_000,
                      max_bytes=16 * 1024 * 1024, max_examined_paths=100_000):
    """Capture complete declared document scope, excluding separately ingested logs.

    Register/capture the SourcePublisher position BEFORE calling this function.
    Publication and all canonical verification are separate responsibilities.
    No provider call or filesystem read may occur under the root/Store gate.
    """
    definition = scopes._definition(definition)
    from .local_mutations import root_identity
    if json.loads(definition.serialized)[0] != root_identity(transport):
        raise ValueError('collection belongs to another transport root')
    for value, ceiling in ((max_documents, 20_000), (max_bytes, 16 * 1024 * 1024),
                           (max_examined_paths, 100_000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError('invalid collection budget')
    exact = {s.value for s in definition.selectors if s.kind == 'doc_exact'}
    prefixes = {s.value for s in definition.selectors if s.kind == 'doc_prefix'}
    # Avoid overlapping walks; the immutable definition itself remains unchanged.
    prefixes = {p for p in prefixes if not any(p.startswith(q + '/') for q in prefixes if q != p)}
    budget = _JSONBudget(max_bytes)
    result = {}

    def selected(path):
        return path in exact or any(path == p or path.startswith(p + '/') for p in prefixes)

    def include(path, value):
        if path in result:
            return
        if len(result) >= max_documents:
            raise RawCollectionUnavailable('document_budget')
        try:
            docs._validate_document_path(path)
            budget.string(path)
            result[path] = budget.copy(value)
        except (AuthorityObservationUnavailable, ValueError, UnicodeError, RecursionError) as exc:
            raise RawCollectionUnavailable('invalid_or_oversize_payload') from exc

    if type(transport) is CachingTransport:
        # Cache ownership marks unsafe externally mutable values; refs admitted
        # here are immutable owned values, so expensive copying runs off-lock.
        with transport._lock:
            try:
                _position_locked(transport)
            except AuthorityObservationUnavailable as exc:
                raise RawCollectionUnavailable('mirror_pending') from exc
            if len(transport._docs) > max_examined_paths:
                raise RawCollectionUnavailable('path_budget')
            refs = []
            for path, value in transport._docs.items():
                if selected(path):
                    if path in transport._authority_unsafe:
                        raise RawCollectionUnavailable('unsafe_cached_value')
                    if len(refs) >= max_documents:
                        raise RawCollectionUnavailable('document_budget')
                    refs.append((path, value))
        for path, value in refs:
            include(path, value)
        return result
    raise RawCollectionUnavailable('unsupported_transport')


def collect_document_batches(transport, definition, *, consume,
                             batch_documents=128, batch_bytes=1024 * 1024,
                             max_document_bytes=4 * 1024 * 1024,
                             max_total_bytes=512 * 1024 * 1024,
                             max_documents=1_000_000,
                             max_examined_paths=2_000_000, stats=None):
    """Deliver bounded dictionaries of raw documents; return only after full enumeration.

    A callback may have received batches when an exception occurs. Its caller must
    keep those provisional until this function returns normally. The mirror's
    revision is checked on each batch boundary, including the final boundary.
    """
    from .local_mutations import root_identity
    definition = scopes._definition(definition)
    if json.loads(definition.serialized)[0] != root_identity(transport):
        raise ValueError('collection belongs to another transport root')
    if not callable(consume):
        raise ValueError('consume must be callable')
    if stats is not None and type(stats) is not dict:
        raise ValueError('stats must be a dict')
    for value, ceiling in ((batch_documents, 1024), (batch_bytes, 16 * 1024 * 1024),
                           (max_document_bytes, 16 * 1024 * 1024),
                           (max_total_bytes, 1024 * 1024 * 1024),
                           (max_documents, 1_000_000),
                           (max_examined_paths, 2_000_000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError('invalid collection budget')
    exact = {s.value for s in definition.selectors if s.kind == 'doc_exact'}
    prefixes = {s.value for s in definition.selectors if s.kind == 'doc_prefix'}
    prefixes = {p for p in prefixes if not any(p.startswith(q + '/') for q in prefixes if q != p)}
    # Prefix walks include exact selections beneath them, so no global seen set.
    exact = {p for p in exact if not any(p == q or p.startswith(q + '/') for q in prefixes)}
    batch, size, total, count = {}, 0, 0, 0
    examined = batches = 0

    def report():
        if stats is not None:
            try:
                stats.update(documents_examined=examined,
                             documents_selected=count,
                             document_bytes=total,
                             document_batches=batches)
            except Exception:
                pass  # Optional work evidence cannot replace collection results.
    def verify():
        pass

    def flush():
        nonlocal batch, size, batches
        if batch:
            consume(batch)
            if stats is not None:
                batches += 1
            verify()
            batch, size = {}, 0

    def include(path, value):
        nonlocal batch, size, total, count
        if count >= max_documents:
            raise RawCollectionUnavailable('document_budget')
        try:
            docs._validate_document_path(path)
            budget = _JSONBudget(max_document_bytes)
            budget.string(path)
            detached = budget.copy(value)
            used = max_document_bytes - budget.remaining
        except (AuthorityObservationUnavailable, ValueError, UnicodeError,
                RecursionError, MemoryError) as exc:
            raise RawCollectionUnavailable('invalid_or_oversize_payload') from exc
        if total + used > max_total_bytes:
            raise RawCollectionUnavailable('byte_budget')
        if batch and (len(batch) >= batch_documents or size + used > batch_bytes):
            flush()
        # A single document may exceed the batch byte target, but never its
        # separately enforced per-document limit.
        batch[path] = detached
        size += used
        total += used
        count += 1
        if len(batch) >= batch_documents or size >= batch_bytes:
            flush()

    if type(transport) is CachingTransport:
        with transport._lock:
            try:
                position = _position_locked(transport)
            except AuthorityObservationUnavailable as exc:
                raise RawCollectionUnavailable('mirror_pending') from exc
            if len(transport._docs) > max_examined_paths:
                raise RawCollectionUnavailable('path_budget')
            iterator = iter(transport._docs.items())

        def check_position():
            with transport._lock:
                try:
                    if _position_locked(transport) != position:
                        raise RawCollectionUnavailable('mirror_changed')
                except AuthorityObservationUnavailable as exc:
                    raise RawCollectionUnavailable('mirror_changed') from exc

        verify = check_position

        try:
            while True:
                refs = []
                with transport._lock:
                    try:
                        if _position_locked(transport) != position:
                            raise RawCollectionUnavailable('mirror_changed')
                        finished = False
                        for _ in range(batch_documents):
                            try:
                                path, value = next(iterator)
                            except StopIteration:
                                finished = True
                                break
                            if stats is not None:
                                examined += 1
                            if path in exact or any(path == p or path.startswith(p + '/') for p in prefixes):
                                if path in transport._authority_unsafe:
                                    raise RawCollectionUnavailable('unsafe_cached_value')
                                refs.append((path, value))
                    except AuthorityObservationUnavailable as exc:
                        raise RawCollectionUnavailable('mirror_changed') from exc
                    except RawCollectionUnavailable:
                        raise
                    except RuntimeError as exc:
                        raise RawCollectionUnavailable('mirror_changed') from exc
                for path, value in refs:
                    include(path, value)
                check_position()
                if finished:
                    break
            flush()
            check_position()
        finally:
            report()
        return
    raise RawCollectionUnavailable('unsupported_transport')
