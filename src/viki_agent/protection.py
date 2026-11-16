"""At-rest protection for app-owned data and a read-only private-path check.

Windows: current-user DPAPI and a conservative directory and file ACL check. The ACL check is a
snapshot of the path, not its existing children or later ACL changes. Same-user processes,
Administrators and the operating system are outside this boundary.

macOS and Linux: the private-path check reads the owner and mode bits (and, on macOS, extended
ACL entries). Sealing uses AES-256-GCM under a 32-byte key from the installed key source; with no
source, or a failing one, it refuses, so nothing is stored in the clear.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import hmac
import logging
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from viki_agent import osfs, winproc

_log = logging.getLogger(__name__)


def _account_hint(platform: str) -> str:
    if platform == "win32":
        return "your Windows user account"
    if platform == "darwin":
        return "your macOS user account and Keychain"
    return (
        "your Linux user account and keyring"
        " (the secret-tool program, from the libsecret-tools package, must be installed)"
    )


ACCOUNT_HINT = _account_hint(sys.platform)
_MESSAGE = (
    "Couldn't protect the draft. Check the save location and the settings of " + ACCOUNT_HINT + "."
)
_DESCRIPTION = "VIKI Agent draft v1"
_MAGIC = b"VIKI-DRAFT\x00\x01"
_SEAL_MAGIC = b"VIKI-SEAL\x00\x01"
_SEAL_HEADER = len(_SEAL_MAGIC) + 8 + 12  # magic, key id, nonce
_UI_FORBIDDEN = 0x01
_MAX_DWORD = (1 << 32) - 1


class ProtectionError(Exception):
    """No native error, path, context or draft body is exposed to callers."""

    def __init__(self) -> None:
        super().__init__(_MESSAGE)


def _digest(data: bytes, context: bytes) -> bytes:
    return hashlib.sha256(len(context).to_bytes(8, "big") + context + data).digest()


_key_source: Callable[[], bytes] | None = None


def set_key_source(source: Callable[[], bytes] | None) -> None:
    """Install the sealing key's source; Windows ignores it, since DPAPI needs no key."""
    global _key_source
    _key_source = source


def visit_key_source() -> None:
    """Visit the installed key source once, so no later seal's first key lookup ever runs
    inside a store write transaction: a locked or slow keychain would otherwise hold that
    transaction open, and every other writer would see `busy`. Windows has no source to
    visit (DPAPI needs none). Never raises: the launch goes on either way. A failing keychain
    source records "unavailable" (keyring_state()), exactly as a first-use failure would, and a
    later seal answers ProtectionError at once, with no fresh keychain attempt; with no source
    installed nothing is recorded and every seal refuses."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(ProtectionError):
        _key()


def _key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:8]


def _seal(key: bytes, data: bytes, context: bytes) -> bytes:
    """AES-256-GCM with a random nonce; the context is associated data, as DPAPI's entropy is."""
    # Imported here: the Windows server imports this module but never seals this way.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    try:
        return _SEAL_MAGIC + _key_id(key) + nonce + AESGCM(key).encrypt(nonce, data, context)
    except ValueError, TypeError, OverflowError:
        raise ProtectionError() from None


def _unseal(key: bytes, blob: bytes, context: bytes) -> bytes:
    """Refuse a short blob, another magic or key, and any failed tag, all alike."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if (
            len(blob) < _SEAL_HEADER + 16  # the GCM tag
            or not blob.startswith(_SEAL_MAGIC)
            or blob[len(_SEAL_MAGIC) : len(_SEAL_MAGIC) + 8] != _key_id(key)
        ):
            raise ProtectionError()
        nonce = blob[_SEAL_HEADER - 12 : _SEAL_HEADER]
        return AESGCM(key).decrypt(nonce, blob[_SEAL_HEADER:], context)
    except InvalidTag, ValueError, TypeError, OverflowError:
        raise ProtectionError() from None


KeyringState = Literal["not_applicable", "unchecked", "ready", "unavailable"]
_keyring: KeyringState = "unchecked"
_KEYCHAIN_SERVICE = "viki-agent"  # persisted: every sealing key is filed under this name
_KEYCHAIN_TIMEOUT_S = 60.0  # room for the user to answer an unlock prompt
_SECURITY = "/usr/bin/security"
_ITEM_NOT_FOUND = 44  # errSecItemNotFound, as `security` reports it
_KEY_RECORD = "sealing-key-id"  # the key id, which every blob carries, as 16 hex characters


def keyring_state() -> KeyringState:
    """The last keychain outcome on macOS and Linux; Windows seals with DPAPI instead."""
    return "not_applicable" if sys.platform == "win32" else _keyring


def keychain_source(data_dir: Path) -> Callable[[], bytes]:
    """This data directory's sealing key, kept in the login Keychain or the Secret Service."""
    return _keychain_source(data_dir, sys.platform)


def _keychain_source(data_dir: Path, platform: str) -> Callable[[], bytes]:
    # One item per data directory, named by a digest so the item shows no path.
    account = hashlib.sha256(os.fsencode(os.path.realpath(data_dir))).hexdigest()[:32]
    record = data_dir / _KEY_RECORD
    darwin = platform == "darwin"
    read = _darwin_read if darwin else _linux_read
    store = _darwin_store if darwin else _linux_store
    lock = threading.Lock()
    key: bytes | None = None
    visited = False

    def source() -> bytes:
        nonlocal key, visited
        global _keyring
        with lock:
            # One keychain visit per process, failed or not: a locked keychain must not prompt
            # again on every save. A restart tries again.
            if not visited:
                visited = True
                try:
                    found = read(account)
                    if found is None:
                        # A record means a key may have sealed data here. A lookup that finds
                        # none is then a locked keyring, a lost item or a moved directory, and a
                        # new key would replace or strand the one those blobs need.
                        try:
                            os.lstat(record)
                        except FileNotFoundError:
                            # Only a missing record makes a new key; any other error fails closed.
                            found = os.urandom(32)
                        else:
                            raise ProtectionError()
                        store(account, found.hex())
                        # A store is trusted only once the item reads back the same.
                        if read(account) != found:
                            raise ProtectionError()
                    _match_key_record(record, found)
                    key = found  # cached only once the record holds its id
                    _keyring = "ready"
                except Exception:  # a missing tool, a timeout, a locked or failing keychain
                    _keyring = "unavailable"
            if key is None:
                raise ProtectionError()
            return key

    return source


def _match_key_record(record: Path, key: bytes) -> None:
    """Record the key's id before its first use, or require the id already recorded."""
    key_id = _key_id(key).hex()
    try:
        file = osfs.open_new_private(record)
    except FileExistsError:  # recorded by an earlier launch, or by another writer first
        if sys.platform == "win32":
            # Windows seals with DPAPI and never installs this source; its fsync needs a
            # writable handle.
            recorded = record.read_bytes()
        else:
            # Synced before it is trusted: a killed earlier launch may have left it unwritten.
            # A link fails at this open; a directory, FIFO or device fails at the fstat below.
            fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ProtectionError()
                os.fsync(fd)
                recorded = os.read(fd, len(key_id) + 1)  # a longer record then differs
            finally:
                os.close(fd)
        if recorded != key_id.encode("ascii"):
            raise ProtectionError() from None
        osfs.fsync_dir(record.parent)
        return
    # A crash between the create and the write leaves an empty record, which is safe to delete.
    try:
        with file:
            file.write(key_id.encode("ascii"))
            file.flush()
            os.fsync(file.fileno())
        osfs.fsync_dir(record.parent)
    except Exception:
        # No key was handed out and no record existed before this call, so nothing was sealed.
        with contextlib.suppress(FileNotFoundError):
            record.unlink()
        raise


def _hex_key(text: str) -> bytes:
    key = bytes.fromhex(text.strip())
    if len(key) != 32:
        raise ValueError("not a 32-byte key")
    return key


def _darwin_read(account: str) -> bytes | None:
    done = winproc.run(
        [_SECURITY, "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        cwd=Path.home(),
        timeout=_KEYCHAIN_TIMEOUT_S,
    )
    if done.returncode == _ITEM_NOT_FOUND:
        return None
    if done.returncode != 0:
        raise OSError(done.returncode, "keychain read failed")
    return _hex_key(done.stdout)


def _darwin_store(account: str, hex_key: str) -> None:
    # `security -i` reads the command from stdin, so the key never reaches an argv.
    command = (
        f"add-generic-password -s {_KEYCHAIN_SERVICE} -a {account}"
        f" -l {_KEYCHAIN_SERVICE} -w {hex_key}\n"
    )
    done = winproc.run(
        [_SECURITY, "-i"], cwd=Path.home(), timeout=_KEYCHAIN_TIMEOUT_S, input=command
    )
    if done.returncode != 0:
        raise OSError(done.returncode, "keychain store failed")


@contextlib.contextmanager
def _logging_a_missing_secret_tool() -> Iterator[None]:
    """The failure still propagates (the key source fails closed); only a code-only line, which
    names the program and nothing else, tells a missing package from a locked keyring."""
    try:
        yield
    except FileNotFoundError:
        _log.warning("secret-tool was not found; install libsecret-tools")
        raise


def _linux_read(account: str) -> bytes | None:
    with _logging_a_missing_secret_tool():
        done = winproc.run(
            ["secret-tool", "lookup", "service", _KEYCHAIN_SERVICE, "account", account],
            cwd=Path.home(),
            timeout=_KEYCHAIN_TIMEOUT_S,
        )
    # secret-tool exits 1 for a missing item and for a dismissed unlock prompt alike, so None
    # only means that no key was read; the key-id record decides whether a new key may be made.
    if done.returncode != 0 or not done.stdout:
        return None
    return _hex_key(done.stdout)


def _linux_store(account: str, hex_key: str) -> None:
    with _logging_a_missing_secret_tool():
        done = winproc.run(
            [
                "secret-tool",
                "store",
                "--label=VIKI Agent",
                "service",
                _KEYCHAIN_SERVICE,
                "account",
                account,
            ],
            cwd=Path.home(),
            timeout=_KEYCHAIN_TIMEOUT_S,
            input=hex_key,
        )
    if done.returncode != 0:
        raise OSError(done.returncode, "keyring store failed")


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _AclSize(ctypes.Structure):
        _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]

    class _AceHeader(ctypes.Structure):
        _fields_ = [("kind", wintypes.BYTE), ("flags", wintypes.BYTE), ("size", wintypes.WORD)]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _fn(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _CryptProtectData = _fn(
        _crypt32,
        "CryptProtectData",
        wintypes.BOOL,
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    )
    _CryptUnprotectData = _fn(
        _crypt32,
        "CryptUnprotectData",
        wintypes.BOOL,
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    )
    _LocalFree = _fn(_kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    _CloseHandle = _fn(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _GetCurrentProcess = _fn(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
    _CreateDirectoryW = _fn(
        _kernel32,
        "CreateDirectoryW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    )
    _ConvertStringSecurityDescriptorToSecurityDescriptorW = _fn(
        _advapi32,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    _OpenProcessToken = _fn(
        _advapi32,
        "OpenProcessToken",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    _GetTokenInformation = _fn(
        _advapi32,
        "GetTokenInformation",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _ConvertSidToStringSidW = _fn(
        _advapi32,
        "ConvertSidToStringSidW",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    _IsValidSid = _fn(_advapi32, "IsValidSid", wintypes.BOOL, ctypes.c_void_p)
    _GetNamedSecurityInfoW = _fn(
        _advapi32,
        "GetNamedSecurityInfoW",
        wintypes.DWORD,
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    _IsValidAcl = _fn(_advapi32, "IsValidAcl", wintypes.BOOL, ctypes.c_void_p)
    _GetAclInformation = _fn(
        _advapi32,
        "GetAclInformation",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    )
    _GetAce = _fn(
        _advapi32,
        "GetAce",
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )

    def _crypt(data: bytes, context: bytes, *, decrypt: bool) -> bytes:
        """Native allocations have one owner; no native errors escape this module."""
        if not isinstance(data, bytes) or not isinstance(context, bytes):
            raise ProtectionError()
        if len(data) > _MAX_DWORD or len(context) > _MAX_DWORD:
            raise ProtectionError()
        data_buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        entropy_buffer = ctypes.create_string_buffer(context, max(1, len(context)))
        incoming = _Blob(len(data), ctypes.addressof(data_buffer))
        entropy = _Blob(len(context), ctypes.addressof(entropy_buffer))
        outgoing = _Blob()
        try:
            if decrypt:
                ok = _CryptUnprotectData(
                    ctypes.byref(incoming),
                    None,
                    ctypes.byref(entropy),
                    None,
                    None,
                    _UI_FORBIDDEN,
                    ctypes.byref(outgoing),
                )
            else:
                ok = _CryptProtectData(
                    ctypes.byref(incoming),
                    _DESCRIPTION,
                    ctypes.byref(entropy),
                    None,
                    None,
                    _UI_FORBIDDEN,
                    ctypes.byref(outgoing),
                )
            if not ok or not outgoing.data or not outgoing.size:
                raise ProtectionError()
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            # This erases native temporary buffers, not Python's immutable bytes or all memory.
            ctypes.memset(data_buffer, 0, len(data_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
            if outgoing.data:
                ctypes.memset(outgoing.data, 0, outgoing.size)
                _LocalFree(outgoing.data)

    def protect(data: bytes, *, context: bytes) -> bytes:
        """Encrypt bytes for the current Windows user, bound to the caller's context."""
        try:
            if not isinstance(data, bytes) or not isinstance(context, bytes):
                raise ProtectionError()
            # DPAPI protects the payload. This inner format validates recovered bytes, including
            # rare malformed-blob cases where Windows reports success with corrupted output.
            payload = _MAGIC + len(data).to_bytes(8, "big") + _digest(data, context) + data
            return _crypt(payload, context, decrypt=False)
        except OSError, ValueError, TypeError, OverflowError, ctypes.ArgumentError:
            raise ProtectionError() from None

    def unprotect(data: bytes, *, context: bytes) -> bytes:
        """Recover a validated payload, or fail without a plaintext fallback."""
        try:
            payload = _crypt(data, context, decrypt=True)
            header = len(_MAGIC) + 8 + 32
            if len(payload) < header or not payload.startswith(_MAGIC):
                raise ProtectionError()
            length = int.from_bytes(payload[len(_MAGIC) : len(_MAGIC) + 8], "big")
            digest, clear = payload[header - 32 : header], payload[header:]
            if len(clear) != length or not hmac.compare_digest(digest, _digest(clear, context)):
                raise ProtectionError()
            return clear
        except OSError, ValueError, TypeError, OverflowError, ctypes.ArgumentError:
            raise ProtectionError() from None

    def _sid_text(sid: int | None) -> str:
        if not sid or not _IsValidSid(sid):
            raise ProtectionError()
        text = ctypes.c_void_p()
        try:
            if not _ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
                raise ProtectionError()
            return ctypes.wstring_at(text.value)
        finally:
            if text.value:
                _LocalFree(text.value)

    def _current_user_sid() -> str:
        # The app does not impersonate other users; its primary process token owns DPAPI data.
        token = wintypes.HANDLE()
        try:
            if not _OpenProcessToken(_GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                raise ProtectionError()
            size = wintypes.DWORD()
            ok = _GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if (
                ok
                or ctypes.get_last_error() != 122
                or not ctypes.sizeof(_TokenUser) <= size.value <= 4096
            ):
                raise ProtectionError()
            buffer = ctypes.create_string_buffer(size.value)
            if not _GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)):
                raise ProtectionError()
            user = _TokenUser.from_buffer(buffer)
            return _sid_text(user.sid)
        finally:
            if token.value:
                _CloseHandle(token)

    def _require_private_acl(owner: int | None, acl: int | None, trusted: set[str]) -> None:
        owner_sid = _sid_text(owner)
        if owner_sid not in trusted or not acl or not _IsValidAcl(acl):
            raise ProtectionError()
        info = _AclSize()
        if not _GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ProtectionError()
        if info.used < 8 or info.used > 65535 or info.count > (info.used - 8) // 16:
            raise ProtectionError()
        for index in range(info.count):
            ace = ctypes.c_void_p()
            if not _GetAce(acl, index, ctypes.byref(ace)) or ace.value is None:
                raise ProtectionError()
            address = ace.value
            if address < acl + 8 or address + 16 > acl + info.used:
                raise ProtectionError()
            header = _AceHeader.from_address(address)
            if (
                header.kind not in {0, 1}
                or header.size < 16
                or address + header.size > acl + info.used
            ):
                raise ProtectionError()
            # SID is embedded at offset eight, with an eight-byte header followed by DWORDs.
            subauthorities = ctypes.c_ubyte.from_address(address + 9).value
            if 16 + 4 * subauthorities > header.size:
                raise ProtectionError()
            sid = _sid_text(address + 8)
            if sid == "S-1-3-4":
                # OWNER RIGHTS means this object's owner, already required to be trusted.
                # It is not CREATOR OWNER's inheritance placeholder or an additional user.
                sid = owner_sid
            mask = wintypes.DWORD.from_address(address + 4).value
            if header.kind == 0 and mask and sid not in trusted:
                # Also reject inherited/inherit-only allows: new database files inherit them.
                # CREATOR OWNER and unusual ACE types intentionally require a future policy.
                raise ProtectionError()

    def _require_private_path(path: Path, *, directory: bool) -> None:
        descriptor = ctypes.c_void_p()
        try:
            kind_ok = path.is_dir() if directory else path.is_file()
            if not kind_ok or path.is_symlink() or path.is_junction():
                raise ProtectionError()
            trusted = {_current_user_sid(), "S-1-5-18", "S-1-5-32-544"}
            owner, acl = ctypes.c_void_p(), ctypes.c_void_p()
            status = _GetNamedSecurityInfoW(
                str(path),
                1,
                0x00000005,
                ctypes.byref(owner),
                None,
                ctypes.byref(acl),
                None,
                ctypes.byref(descriptor),
            )
            if status != 0 or not descriptor.value:
                raise ProtectionError()
            _require_private_acl(owner.value, acl.value, trusted)
        except OSError, ValueError, TypeError, OverflowError, ctypes.ArgumentError:
            raise ProtectionError() from None
        finally:
            if descriptor.value:
                _LocalFree(descriptor.value)

    def require_private_directory(path: Path) -> None:
        """Require trusted ownership and no allow grants beyond user/SYSTEM/Administrators.

        This never changes permissions. Null/unsupported ACLs fail closed; an empty DACL may
        pass confidentiality validation but still refuse subsequent filesystem operations.
        """
        _require_private_path(path, directory=True)

    def require_private_file(path: Path) -> None:
        """Check an existing file's owner/DACL; this is not a path-race or hardlink guard."""
        _require_private_path(path, directory=False)

    def create_private_directory(path: Path) -> bool:
        """Atomically create only a new app-owned leaf with a private inheritable DACL.

        The caller validates the path and creates any parents. Existing paths return False
        without permission changes; their suitability must still be checked by the caller.
        """
        descriptor = ctypes.c_void_p()
        try:
            sid = _current_user_sid()
            sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
            if (
                not _ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(descriptor), None
                )
                or not descriptor.value
            ):
                raise ProtectionError()
            attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes), descriptor.value, False
            )
            if not _CreateDirectoryW(str(path), ctypes.byref(attributes)):
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS; never change its ACL.
                    return False
                raise ProtectionError()
            # Filesystems without persistent ACLs may ignore SECURITY_ATTRIBUTES. Do not
            # report a private result until the actual directory descriptor has been checked.
            require_private_directory(path)
            return True
        except OSError, ValueError, TypeError, OverflowError, ctypes.ArgumentError:
            raise ProtectionError() from None
        finally:
            if descriptor.value:
                _LocalFree(descriptor.value)

else:
    if sys.platform == "darwin":
        _libc = ctypes.CDLL(None, use_errno=True)
        _acl_get_link_np = _libc.acl_get_link_np
        _acl_get_link_np.restype = ctypes.c_void_p
        _acl_get_link_np.argtypes = (ctypes.c_char_p, ctypes.c_int)
        _acl_get_entry = _libc.acl_get_entry
        _acl_get_entry.restype = ctypes.c_int
        _acl_get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        _acl_free = _libc.acl_free
        _acl_free.restype = ctypes.c_int
        _acl_free.argtypes = (ctypes.c_void_p,)
        _ACL_TYPE_EXTENDED = 0x100
        _ACL_FIRST_ENTRY = 0

        def _require_no_extended_acl(path: Path) -> None:
            """An extended ACL can grant access the mode bits do not show."""
            ctypes.set_errno(0)
            acl = _acl_get_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED)
            if not acl:
                if ctypes.get_errno() == errno.ENOENT:  # The path has no extended ACL.
                    return
                raise ProtectionError()
            try:
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                found = _acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry))
                # Only "no entry" (-1 with EINVAL) passes; an entry or any other failure refuses.
                if found != -1 or ctypes.get_errno() != errno.EINVAL:
                    raise ProtectionError()
            finally:
                _acl_free(acl)

    def _key() -> bytes:
        """The source's key; no source, a failing one or anything but 32 bytes refuses."""
        source = _key_source
        if source is None:
            raise ProtectionError()
        try:
            key = source()
        except Exception:  # a keychain failure of any kind; never logged, never a fallback
            raise ProtectionError() from None
        if not isinstance(key, bytes) or len(key) != 32:
            raise ProtectionError()
        return key

    def protect(data: bytes, *, context: bytes) -> bytes:
        """Seal bytes under the installed key, bound to the caller's context."""
        if not isinstance(data, bytes) or not isinstance(context, bytes):
            raise ProtectionError()
        # The same inner format as on Windows, so recovered bytes are validated alike.
        payload = _MAGIC + len(data).to_bytes(8, "big") + _digest(data, context) + data
        return _seal(_key(), payload, context)

    def unprotect(data: bytes, *, context: bytes) -> bytes:
        """Recover a validated payload, or fail without a plaintext fallback."""
        if not isinstance(data, bytes) or not isinstance(context, bytes):
            raise ProtectionError()
        payload = _unseal(_key(), data, context)
        header = len(_MAGIC) + 8 + 32
        if len(payload) < header or not payload.startswith(_MAGIC):
            raise ProtectionError()
        length = int.from_bytes(payload[len(_MAGIC) : len(_MAGIC) + 8], "big")
        digest, clear = payload[header - 32 : header], payload[header:]
        if len(clear) != length or not hmac.compare_digest(digest, _digest(clear, context)):
            raise ProtectionError()
        return clear

    def _require_private_path(path: Path, *, directory: bool) -> None:
        try:
            info = os.lstat(path)
            # lstat never follows a final symlink, so a link fails the type test.
            kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not kind_ok or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ProtectionError()
            if sys.platform == "darwin":
                _require_no_extended_acl(path)
        except OSError, ValueError, TypeError, ctypes.ArgumentError:
            raise ProtectionError() from None

    def require_private_directory(path: Path) -> None:
        """Require a real directory owned by the effective user with no group or other bits.

        This never changes permissions. It does not guard against same-user processes, and on
        Linux a POSIX ACL's mask is the group bits, so the mode check covers it.
        """
        _require_private_path(path, directory=True)

    def require_private_file(path: Path) -> None:
        """Check an existing file's owner and mode; this is not a path-race or hardlink guard."""
        _require_private_path(path, directory=False)

    def create_private_directory(path: Path) -> bool:
        """Atomically create only a new app-owned 0700 leaf.

        The caller validates the path and creates any parents. Existing paths return False
        without permission changes; their suitability must still be checked by the caller.
        """
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            return False
        except OSError, ValueError, TypeError:
            raise ProtectionError() from None
        # A umask can only clear bits, but the check still reads what the filesystem kept.
        require_private_directory(path)
        return True
