"""Inactive complete raw-document collection for local SQLite ingestion.

These bounded background reads do not verify permission or establish a remote
atomic snapshot. Exact absence is distinct from unreadable/malformed input.
"""
from __future__ import annotations

import json

from ..store import document_observation as docs, source_selectors as scopes
from .authority_observation import AuthorityObservationUnavailable, _JSONBudget, _position_locked
from .cache import CachingTransport
from .folder import FolderTransport


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
    if type(transport) is not FolderTransport:
        raise RawCollectionUnavailable('unsupported_transport')

    from .folder_raw import collect, FolderReadUnavailable

    def include_bytes(logical, payload):
        try:
            value = json.loads(payload.decode('utf-8-sig'), parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RawCollectionUnavailable('malformed_document') from exc
        include(logical, value)

    try:
        collect(transport.root, exact, prefixes, max_bytes=max_bytes,
                max_paths=max_examined_paths, include=include_bytes)
    except FolderReadUnavailable as exc:
        raise RawCollectionUnavailable(str(exc)) from exc
    except OSError as exc:
        raise RawCollectionUnavailable('io') from exc
    return result


def _invalid_constant(_value):
    raise ValueError('non-finite JSON')
