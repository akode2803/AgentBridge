"""Handle-relative bounded folder reads; inactive local-input ingestion helper."""
from __future__ import annotations

import os
import stat


class FolderReadUnavailable(RuntimeError):
    pass


def collect(root, exact, prefixes, *, max_bytes, max_paths, include):
    # Never substitute pathname check-then-open on platforms lacking this owner.
    # A Windows handle-relative backend is required before paging activation there.
    if (os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd
            or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')):
        raise FolderReadUnavailable('handle_relative_reads_unsupported')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    examined, remaining = 0, max_bytes
    seen = set()

    def charge(parts):
        nonlocal examined
        examined += 1
        if examined > max_paths or len(parts) > 32:
            raise FolderReadUnavailable('path_budget')

    def open_directory(parent, name):
        fd = os.open(name, directory_flags, dir_fd=parent)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise FolderReadUnavailable('non_directory_selection')
        return fd

    # Walk from the filesystem anchor rather than following replaceable ancestors
    # of the configured root. Every subsequent lookup is relative to held handles.
    if not root.is_absolute():
        raise ValueError('folder root must be absolute')
    root_fd = os.open(root.anchor, directory_flags)
    try:
        for part in root.parts[1:]:
            child = open_directory(root_fd, part)
            os.close(root_fd)
            root_fd = child

        def selected_directory(parts):
            fd = os.dup(root_fd)
            try:
                for n, part in enumerate(parts, 1):
                    charge(parts[:n])
                    child = open_directory(fd, part)
                    os.close(fd)
                    fd = child
                return fd
            except FileNotFoundError:
                os.close(fd)
                return None
            except BaseException:
                os.close(fd)
                raise

        def read(parent, name, parts, *, missing_ok):
            nonlocal remaining
            logical = '/'.join(parts)
            if logical in seen:
                return
            charge(parts)
            try:
                fd = os.open(name, file_flags, dir_fd=parent)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise FolderReadUnavailable('selection_changed') from None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise FolderReadUnavailable('non_regular_document')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    payload = stream.read(remaining + 1)
                remaining -= len(payload)
                if remaining < 0:
                    raise FolderReadUnavailable('byte_budget')
                include(logical, payload)
                seen.add(logical)
            finally:
                os.close(fd)

        def walk(fd, parts):
            with os.scandir(fd) as entries:
                for entry in entries:
                    child_parts = (*parts, entry.name)
                    charge(child_parts)
                    mode = os.stat(entry.name, dir_fd=fd, follow_symlinks=False).st_mode
                    if stat.S_ISLNK(mode):
                        raise FolderReadUnavailable('symlink_in_selection')
                    if stat.S_ISDIR(mode):
                        child = open_directory(fd, entry.name)
                        try:
                            walk(child, child_parts)
                        finally:
                            os.close(child)
                    elif entry.name.endswith('.json'):
                        read(fd, entry.name, child_parts, missing_ok=False)

        for path in sorted(exact):
            parts = tuple(path.split('/'))
            fd = selected_directory(parts[:-1])
            if fd is not None:
                try:
                    read(fd, parts[-1], parts, missing_ok=True)
                finally:
                    os.close(fd)
        for prefix in sorted(prefixes):
            parts = tuple(prefix.split('/'))
            fd = selected_directory(parts)
            if fd is not None:
                try:
                    walk(fd, parts)
                finally:
                    os.close(fd)
    finally:
        os.close(root_fd)
