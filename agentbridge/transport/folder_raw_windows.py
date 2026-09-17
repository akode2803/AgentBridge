"""Windows held-parent reads for the inactive local-input collector.

NtCreateFile resolves one component against an open directory. Reparse points
are refused before enumeration or reads; there is no pathname-open fallback.
"""
from __future__ import annotations

import ctypes as C
import errno
import os
import re
import struct
from dataclasses import dataclass
from pathlib import PureWindowsPath

from .folder_raw import FolderReadUnavailable

BUFFER_BYTES = 64 * 1024
_HEADER = struct.Struct('<IIqqqqqqIII')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'}


def component(name):
    if (type(name) is not str or not name or name in ('.', '..')
            or name.endswith((' ', '.'))
            or any(ord(c) < 32 or c in '\\/:*?"<>|\x00' for c in name)):
        raise FolderReadUnavailable('invalid_windows_component')
    try:
        raw = name.encode('utf-16-le')
    except UnicodeError as exc:
        raise FolderReadUnavailable('invalid_windows_component') from exc
    base = name.split('.', 1)[0].upper()
    if len(raw) > 510 or base in _RESERVED or re.fullmatch(r'(COM|LPT)[1-9¹²³]', base):
        raise FolderReadUnavailable('invalid_windows_component')
    return name


def directory_records(buffer):
    """Parse one fixed, freshly zeroed FILE_FULL_DIR_INFO output buffer.

    GetFileInformationByHandleEx returns no used-byte count. All variable tails
    and advancing offsets must therefore fit the supplied fixed buffer.
    """
    if type(buffer) is not bytes or not _HEADER.size <= len(buffer) <= BUFFER_BYTES:
        raise FolderReadUnavailable('invalid_directory_buffer')
    offset = 0
    while True:
        if offset + _HEADER.size > len(buffer):
            raise FolderReadUnavailable('invalid_directory_record')
        fields = _HEADER.unpack_from(buffer, offset)
        next_offset, name_size = fields[0], fields[9]
        end = offset + _HEADER.size + name_size
        if not name_size or name_size % 2 or end > len(buffer):
            raise FolderReadUnavailable('invalid_directory_record')
        if next_offset and (next_offset % 8 or next_offset < _HEADER.size + name_size
                            or offset + next_offset + _HEADER.size > len(buffer)):
            raise FolderReadUnavailable('invalid_directory_record')
        try:
            name = buffer[offset + _HEADER.size:end].decode('utf-16-le')
        except UnicodeError as exc:
            raise FolderReadUnavailable('invalid_directory_record') from exc
        if name not in ('.', '..'):
            component(name)
        yield name
        if not next_offset:
            return
        offset += next_offset


class _UnicodeString(C.Structure):
    _fields_ = [('Length', C.c_uint16), ('MaximumLength', C.c_uint16), ('Buffer', C.c_void_p)]


class _ObjectAttributes(C.Structure):
    _fields_ = [('Length', C.c_uint32), ('RootDirectory', C.c_void_p),
                ('ObjectName', C.POINTER(_UnicodeString)), ('Attributes', C.c_uint32),
                ('SecurityDescriptor', C.c_void_p), ('SecurityQualityOfService', C.c_void_p)]


class _IoStatus(C.Structure):
    _fields_ = [('Status', C.c_void_p), ('Information', C.c_size_t)]


@dataclass(frozen=True)
class Handle:
    value: int
    directory: bool


class Reader:
    def __init__(self):
        if os.name != 'nt':
            raise FolderReadUnavailable('windows_reader_unavailable')
        pointer = C.sizeof(C.c_void_p)
        if (pointer not in (4, 8) or C.sizeof(_UnicodeString) != (16 if pointer == 8 else 8)
                or C.sizeof(_ObjectAttributes) != (48 if pointer == 8 else 24)
                or C.sizeof(_IoStatus) != 2 * pointer):
            raise FolderReadUnavailable('windows_abi_unsupported')
        self.nt = C.WinDLL('ntdll', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.nt.NtCreateFile.argtypes = [C.POINTER(C.c_void_p), C.c_uint32,
            C.POINTER(_ObjectAttributes), C.POINTER(_IoStatus), C.c_void_p,
            C.c_uint32, C.c_uint32, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32]
        self.nt.NtCreateFile.restype = C.c_int32
        self.nt.RtlNtStatusToDosError.argtypes = [C.c_int32]
        self.nt.RtlNtStatusToDosError.restype = C.c_uint32
        self.kernel.GetFileInformationByHandleEx.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32]
        self.kernel.GetFileInformationByHandleEx.restype = C.c_int
        self.kernel.GetFileType.argtypes = [C.c_void_p]
        self.kernel.GetFileType.restype = C.c_uint32
        self.kernel.ReadFile.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p]
        self.kernel.ReadFile.restype = C.c_int
        self.kernel.CloseHandle.argtypes = [C.c_void_p]
        self.kernel.CloseHandle.restype = C.c_int

    def open(self, parent, name):
        if parent is not None:
            if not parent.directory:
                raise FolderReadUnavailable('non_directory_selection')
            component(name)
        raw = name.encode('utf-16-le')
        if len(raw) > 65532:
            raise FolderReadUnavailable('invalid_windows_component')
        backing = C.create_string_buffer(raw + b'\x00\x00')
        unicode = _UnicodeString(len(raw), len(raw) + 2, C.cast(backing, C.c_void_p))
        attrs = _ObjectAttributes(C.sizeof(_ObjectAttributes), parent.value if parent else None,
                                   C.pointer(unicode), 0x40, None, None)
        result, ios = C.c_void_p(), _IoStatus()
        # read/list + attributes + synchronize, share all, open existing only,
        # synchronous I/O and OPEN_REPARSE_POINT; classify the actual handle.
        status = self.nt.NtCreateFile(C.byref(result), 0x100081, C.byref(attrs),
            C.byref(ios), None, 0, 7, 1, 0x200020, None, 0)
        if status < 0:
            error = self.nt.RtlNtStatusToDosError(status)
            if error in (2, 3):
                raise FileNotFoundError(errno.ENOENT, 'selected input missing')
            raise C.WinError(error)
        if not result.value:
            raise FolderReadUnavailable('invalid_windows_handle')
        try:
            info = (C.c_uint32 * 2)()
            if not self.kernel.GetFileInformationByHandleEx(result, 9, info, C.sizeof(info)):
                raise C.WinError(C.get_last_error())
            if info[0] & 0x400:
                raise FolderReadUnavailable('symlink_in_selection')
            if self.kernel.GetFileType(result) != 1:
                raise FolderReadUnavailable('non_regular_document')
            return Handle(result.value, bool(info[0] & 0x10))
        except BaseException:
            self.kernel.CloseHandle(result)
            raise

    def close(self, handle):
        if not self.kernel.CloseHandle(handle.value):
            raise C.WinError(C.get_last_error())

    def root(self, path):
        from .folder import _unextend
        path = PureWindowsPath(_unextend(str(path)))
        if not path.is_absolute():
            raise ValueError('folder root must be absolute')
        drive = path.drive
        if re.fullmatch(r'[A-Za-z]:', drive):
            anchor = '\\??\\' + drive + '\\'
        elif drive.startswith('\\\\'):
            parts = drive[2:].split('\\')
            if len(parts) != 2:
                raise FolderReadUnavailable('invalid_windows_root')
            for part in parts:
                component(part)
            anchor = '\\??\\UNC\\' + '\\'.join(parts) + '\\'
        else:
            raise FolderReadUnavailable('invalid_windows_root')
        handle = self.open(None, anchor)
        try:
            if not handle.directory:
                raise FolderReadUnavailable('non_directory_selection')
            for part in path.parts[1:]:
                child = self.open(handle, part)
                try:
                    if not child.directory:
                        raise FolderReadUnavailable('non_directory_selection')
                except BaseException:
                    self.close(child)
                    raise
                previous, handle = handle, child
                self.close(previous)
            return handle
        except BaseException:
            self.close(handle)
            raise

    def entries(self, handle):
        if not handle.directory:
            raise FolderReadUnavailable('non_directory_selection')
        first = True
        while True:
            buffer = C.create_string_buffer(BUFFER_BYTES)
            if not self.kernel.GetFileInformationByHandleEx(handle.value, 15 if first else 14,
                                                           buffer, BUFFER_BYTES):
                error = C.get_last_error()
                if error == 18:  # ERROR_NO_MORE_FILES
                    return
                raise C.WinError(error)
            first = False
            yield from directory_records(buffer.raw)

    def read(self, handle, limit):
        if handle.directory:
            raise FolderReadUnavailable('non_regular_document')
        chunks, used = [], 0
        while used < limit:
            buffer = C.create_string_buffer(min(64 * 1024, limit - used))
            count = C.c_uint32()
            if not self.kernel.ReadFile(handle.value, buffer, len(buffer), C.byref(count), None):
                error = C.get_last_error()
                if error == 38:  # ERROR_HANDLE_EOF
                    break
                raise C.WinError(error)
            if count.value > len(buffer):
                raise FolderReadUnavailable('invalid_windows_read')
            if not count.value:
                break
            chunks.append(buffer.raw[:count.value])
            used += count.value
        return b''.join(chunks)


def collect(root, exact, prefixes, *, max_bytes, max_paths, include):
    reader = Reader()
    root_handle = reader.root(root)
    examined, remaining = 0, max_bytes
    seen = set()

    def charge(parts):
        nonlocal examined
        examined += 1
        if examined > max_paths or len(parts) > 32:
            raise FolderReadUnavailable('path_budget')

    def read(handle, parts):
        nonlocal remaining
        logical = '/'.join(parts)
        if logical in seen:
            return
        payload = reader.read(handle, remaining + 1)
        remaining -= len(payload)
        if remaining < 0:
            raise FolderReadUnavailable('byte_budget')
        include(logical, payload)
        seen.add(logical)

    def walk(handle, parts):
        for name in reader.entries(handle):
            charge(parts if name in ('.', '..') else (*parts, name))
            if name in ('.', '..'):
                continue
            try:
                child = reader.open(handle, name)
            except FileNotFoundError:
                raise FolderReadUnavailable('selection_changed') from None
            try:
                if child.directory:
                    walk(child, (*parts, name))
                elif name.endswith('.json'):
                    read(child, (*parts, name))
            finally:
                reader.close(child)

    try:
        for logical, prefix in [(p, False) for p in sorted(exact)] + [(p, True) for p in sorted(prefixes)]:
            parts = tuple(logical.split('/'))
            handle, owned = root_handle, False
            try:
                for n, part in enumerate(parts, 1):
                    charge(parts[:n])
                    try:
                        child = reader.open(handle, part)
                    except FileNotFoundError:
                        handle_missing = True
                        break
                    previous, previous_owned = handle, owned
                    handle, owned = child, True
                    if previous_owned:
                        reader.close(previous)
                else:
                    handle_missing = False
                if not handle_missing:
                    if prefix:
                        walk(handle, parts)
                    else:
                        read(handle, parts)
            finally:
                if owned:
                    reader.close(handle)
    finally:
        reader.close(root_handle)
