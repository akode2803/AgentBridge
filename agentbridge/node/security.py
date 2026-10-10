"""Owner-only local node files on POSIX and Windows."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class LocalSecurityError(OSError):
    """The local node could not prove its private filesystem boundary."""


def _windows_restrict(path: Path, *, directory: bool) -> None:
    """Install and verify a protected DACL containing only the current user."""
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    ERROR_INSUFFICIENT_BUFFER = 122
    ACL_REVISION = 2
    SE_FILE_OBJECT = 1
    OWNER_SECURITY_INFORMATION = 0x00000001
    DACL_SECURITY_INFORMATION = 0x00000004
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SE_DACL_PROTECTED = 0x1000
    FILE_ALL_ACCESS = 0x001F01FF
    OBJECT_INHERIT_ACE = 0x01
    CONTAINER_INHERIT_ACE = 0x02
    ACCESS_ALLOWED_ACE_TYPE = 0x00

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER_VALUE(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    class ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD), ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [wintypes.LPVOID]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.InitializeAcl.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD]
    advapi.InitializeAcl.restype = wintypes.BOOL
    advapi.AddAccessAllowedAceEx.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID,
    ]
    advapi.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetAce.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY,
                                   ctypes.byref(token)):
        raise LocalSecurityError("could not inspect local owner")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, TOKEN_USER, None, 0,
                                   ctypes.byref(needed))
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not needed.value:
            raise LocalSecurityError("could not inspect local owner")
        token_data = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
                token, TOKEN_USER, token_data, needed.value, ctypes.byref(needed)):
            raise LocalSecurityError("could not inspect local owner")
        sid = ctypes.cast(token_data, ctypes.POINTER(TOKEN_USER_VALUE)).contents.User.Sid
        sid_size = advapi.GetLengthSid(sid)
        if not sid_size:
            raise LocalSecurityError("could not inspect local owner")
        # ACL + ACCESS_ALLOWED_ACE, excluding the ACE's placeholder SidStart.
        acl_data = ctypes.create_string_buffer(ctypes.sizeof(ACL) + 8 + sid_size)
        if not advapi.InitializeAcl(acl_data, len(acl_data), ACL_REVISION):
            raise LocalSecurityError("could not protect local node files")
        flags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE if directory else 0
        if not advapi.AddAccessAllowedAceEx(
                acl_data, ACL_REVISION, flags, FILE_ALL_ACCESS, sid):
            raise LocalSecurityError("could not protect local node files")
        result = advapi.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl_data, None,
        )
        if result:
            raise LocalSecurityError("could not protect local node files")

        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        result = advapi.GetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None,
            ctypes.byref(descriptor),
        )
        if result or not descriptor or not dacl:
            raise LocalSecurityError("could not verify local node files")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if (not advapi.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision))
                    or not control.value & SE_DACL_PROTECTED
                    or not advapi.EqualSid(owner, sid)):
                raise LocalSecurityError("local node files are not owner-only")
            acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
            if acl.AceCount != 1:
                raise LocalSecurityError("local node files are not owner-only")
            ace = wintypes.LPVOID()
            if not advapi.GetAce(dacl, 0, ctypes.byref(ace)):
                raise LocalSecurityError("could not verify local node files")
            address = ctypes.cast(ace, ctypes.c_void_p).value
            if address is None or ctypes.c_ubyte.from_address(address).value != ACCESS_ALLOWED_ACE_TYPE:
                raise LocalSecurityError("local node files are not owner-only")
            ace_sid = ctypes.c_void_p(address + 8)
            if not advapi.EqualSid(ace_sid, sid):
                raise LocalSecurityError("local node files are not owner-only")
        finally:
            kernel.LocalFree(descriptor)
    finally:
        kernel.CloseHandle(token)


def protect_path(path: Path, *, directory: bool) -> None:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise LocalSecurityError("local node path cannot be a symlink")
        if os.name == "nt":
            _windows_restrict(path, directory=directory)
            return
        os.chmod(path, 0o700 if directory else 0o600)
        after = path.stat()
        mode = stat.S_IMODE(after.st_mode)
        if mode & 0o077 or (hasattr(os, "geteuid")
                           and after.st_uid != os.geteuid()):
            raise LocalSecurityError("local node files are not owner-only")
    except LocalSecurityError:
        raise
    except OSError as exc:
        raise LocalSecurityError("could not protect local node files") from exc


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    protect_path(path, directory=True)
    if not path.is_dir():
        raise LocalSecurityError("invalid local node directory")
    return path


def atomic_private_bytes(path: Path, payload: bytes) -> None:
    private_directory(path.parent)
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp = Path(tmp_name)
        protect_path(tmp, directory=False)
        os.replace(tmp, path)
        protect_path(path, directory=False)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def atomic_private_json(path: Path, value: Any) -> None:
    atomic_private_bytes(path, json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
