"""Windows DPAPI wrap for locally stored secrets (R31.5).

The unlocked identity bundle in ``~/.agentbridge/keys`` used to be plain
base64 — safe only as far as the OS user boundary. On Windows we now wrap it
with the Data Protection API (per-user scope, ``CryptProtectData``): the file
can be opened only by the same OS user on the same machine, so a copied file
(backup leak, another local account, a lifted disk) is unreadable.

Deliberately stdlib-only (ctypes), best-effort by design:

- ``protect``/``unprotect`` return ``None`` when DPAPI is unavailable or the
  call fails — callers fall back to the previous plain handling, so a key is
  never lost to a wrap failure (the wrap is hardening, not a new trust root).
- Non-Windows platforms simply report unavailable; the keystore keeps its
  plain format there (Keychain/keyutils are possible later behind the same
  seam).
"""

from __future__ import annotations

import sys

__all__ = ["available", "protect", "unprotect"]

# Bound into every blob; not a secret (it's in the source) — it just keeps
# unrelated DPAPI users from decrypting our blobs by accident.
_ENTROPY = b"agentbridge.keystore.v1"


def available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _call(fn, data: bytes) -> bytes | None:
        src, entropy, out = _blob(data), _blob(_ENTROPY), _BLOB()
        ok = fn(
            ctypes.byref(src), None, ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def protect(data: bytes) -> bytes | None:
        try:
            return _call(ctypes.windll.crypt32.CryptProtectData, data)
        except OSError:
            return None

    def unprotect(blob: bytes) -> bytes | None:
        try:
            return _call(ctypes.windll.crypt32.CryptUnprotectData, blob)
        except OSError:
            return None

else:  # non-Windows: unavailable, callers keep the plain format

    def protect(data: bytes) -> bytes | None:
        return None

    def unprotect(blob: bytes) -> bytes | None:
        return None
