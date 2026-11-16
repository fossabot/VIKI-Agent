"""Synthetic current-user DPAPI and Win32 ACL probes; no provider or vault access."""

from __future__ import annotations

import ctypes
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection
from viki_agent.protection import (
    ProtectionError,
    create_private_directory,
    protect,
    require_private_directory,
    require_private_file,
    unprotect,
)

if sys.platform == "win32":
    from ctypes import wintypes

    _native = ctypes.WinDLL("advapi32", use_last_error=True)
    _ConvertDescriptor = protection._fn(
        _native,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    _GetOwner = protection._fn(
        _native,
        "GetSecurityDescriptorOwner",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    _GetDacl = protection._fn(
        _native,
        "GetSecurityDescriptorDacl",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    _SetFileSecurity = protection._fn(
        _native,
        "SetFileSecurityW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI and ACLs are Windows only")


@pytest.fixture(scope="module")
def user_sid() -> str:
    assert sys.platform == "win32"
    return protection._current_user_sid()


@contextmanager
def _descriptor(sddl: str) -> Iterator[int]:
    assert sys.platform == "win32"
    descriptor = ctypes.c_void_p()
    assert _ConvertDescriptor(sddl, 1, ctypes.byref(descriptor), None)
    assert descriptor.value is not None
    try:
        yield descriptor.value
    finally:
        protection._LocalFree(descriptor.value)


def _check_sddl(sddl: str, user_sid: str) -> None:
    assert sys.platform == "win32"
    with _descriptor(sddl) as descriptor:
        owner, acl = ctypes.c_void_p(), ctypes.c_void_p()
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        assert _GetOwner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted))
        assert _GetDacl(
            descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
        )
        protection._require_private_acl(
            owner.value, acl.value, {user_sid, "S-1-5-18", "S-1-5-32-544"}
        )


def _set_acl(path: Path, sddl: str) -> None:
    """Change only a test-created scratch path, preserving the file's current owner."""
    assert sys.platform == "win32"
    with _descriptor(sddl) as descriptor:
        assert _SetFileSecurity(str(path), 0x80000004, descriptor)


def _acl_snapshot(path: Path) -> tuple[str, bytes]:
    assert sys.platform == "win32"
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    assert (
        protection._GetNamedSecurityInfoW(
            str(path),
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        )
        == 0
    )
    try:
        info = protection._AclSize()
        assert protection._GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2)
        return protection._sid_text(owner.value), ctypes.string_at(acl, info.used)
    finally:
        protection._LocalFree(descriptor)


@pytest.mark.parametrize(
    "data",
    [b"", b"synthetic draft", "draft café\n🙂\x00".encode(), b"x" * 1048576],
    ids=["empty", "ascii", "unicode", "one_mib"],
)
def test_real_dpapi_round_trip(data: bytes) -> None:
    ciphertext = protect(data, context=b"synthetic scope/key/revision 1")
    assert ciphertext != data
    assert len(ciphertext) <= len(data) + 65536
    assert unprotect(ciphertext, context=b"synthetic scope/key/revision 1") == data


def test_dpapi_randomizes_and_binds_context() -> None:
    clear = b"synthetic private editor text"
    first = protect(clear, context=b"scope-a/key-a/1")
    assert first != protect(clear, context=b"scope-a/key-a/1")
    assert clear not in first
    for context in (b"scope-b/key-a/1", b"scope-a/key-b/1", b"scope-a/key-a/2", b""):
        with pytest.raises(ProtectionError):
            unprotect(first, context=context)


def test_tampered_or_non_dpapi_payloads_never_return_text() -> None:
    encrypted = protect(b"synthetic tamper target", context=b"tamper")
    candidates = [b"", b"plain editor text", encrypted[:-1]]
    for offset in (0, len(encrypted) // 2, len(encrypted) - 1):
        changed = bytearray(encrypted)
        changed[offset] ^= 1
        candidates.append(bytes(changed))
    for damaged in candidates:
        with pytest.raises(ProtectionError):
            unprotect(damaged, context=b"tamper")


@pytest.mark.parametrize("damage", ["magic", "length", "digest", "context"])
def test_inner_validation_rejects_invalid_native_success(damage: str) -> None:
    assert sys.platform == "win32"
    context, clear = b"synthetic context", b"synthetic validated body"
    payload = bytearray(
        protection._MAGIC
        + len(clear).to_bytes(8, "big")
        + protection._digest(clear, context)
        + clear
    )
    offset = {"magic": 0, "length": len(protection._MAGIC), "digest": len(payload) - 1}
    if damage in offset:
        payload[offset[damage]] ^= 1
    else:
        context = b"different native entropy"
    ciphertext = protection._crypt(bytes(payload), context, decrypt=False)
    with pytest.raises(ProtectionError):
        unprotect(ciphertext, context=context)


def test_native_calls_are_current_user_noninteractive_and_free_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "win32"
    native_protect = protection._CryptProtectData
    native_unprotect = protection._CryptUnprotectData
    native_free = protection._LocalFree
    calls: list[tuple[str, Any, int]] = []
    freed: list[int] = []

    def encrypt(*args: Any) -> Any:
        assert args[3] is None and args[4] is None
        calls.append(("protect", args[1], args[5]))
        return native_protect(*args)

    def decrypt(*args: Any) -> Any:
        assert args[1] is None and args[3] is None and args[4] is None
        calls.append(("unprotect", None, args[5]))
        return native_unprotect(*args)

    def free(pointer: int) -> Any:
        freed.append(pointer)
        assert ctypes.string_at(pointer, 8) == b"\x00" * 8
        return native_free(pointer)

    monkeypatch.setattr(protection, "_CryptProtectData", encrypt)
    monkeypatch.setattr(protection, "_CryptUnprotectData", decrypt)
    monkeypatch.setattr(protection, "_LocalFree", free)
    assert unprotect(protect(b"synthetic", context=b"fixed"), context=b"fixed") == b"synthetic"
    assert calls == [("protect", "VIKI Agent draft v1", 1), ("unprotect", None, 1)]
    assert len(freed) == 2


@pytest.mark.parametrize("native_name", ["_CryptProtectData", "_CryptUnprotectData"])
@pytest.mark.parametrize("failure", ["false", "exception", "allocated_false"])
def test_native_failures_are_safe_and_release_allocations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    native_name: str,
    failure: str,
) -> None:
    assert sys.platform == "win32"
    clear, context = b"PRIVATE-SYNTHETIC-CONTENT", b"PRIVATE-CONTEXT"
    ciphertext = protect(clear, context=context)
    original, native_free = getattr(protection, native_name), protection._LocalFree
    freed: list[int] = []

    def fail(*args: Any) -> int:
        if failure == "exception":
            raise OSError("PRIVATE-SYNTHETIC-CONTENT native detail")
        if failure == "allocated_false":
            assert original(*args)
        return 0

    def free(pointer: int) -> Any:
        freed.append(pointer)
        return native_free(pointer)

    monkeypatch.setattr(protection, native_name, fail)
    monkeypatch.setattr(protection, "_LocalFree", free)
    with pytest.raises(ProtectionError) as caught:
        if native_name == "_CryptProtectData":
            protect(clear, context=context)
        else:
            unprotect(ciphertext, context=context)
    assert str(caught.value) == protection._MESSAGE
    rendered = "".join(traceback.format_exception(caught.value))
    assert "native detail" not in rendered
    assert len(freed) == (1 if failure == "allocated_false" else 0)
    assert capsys.readouterr() == ("", "")


def test_private_synthetic_directory_and_file_accept_normal_inherited_acl(tmp_path: Path) -> None:
    require_private_directory(tmp_path)
    child = tmp_path / "synthetic.db"
    child.write_bytes(b"synthetic metadata")
    require_private_file(child)
    with pytest.raises(ProtectionError):
        require_private_file(tmp_path)
    with pytest.raises(ProtectionError):
        require_private_directory(child)
    with pytest.raises(ProtectionError):
        require_private_file(tmp_path / "absent.db")


def test_explicit_private_and_broad_scratch_directory(tmp_path: Path, user_sid: str) -> None:
    private = f"D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    _set_acl(tmp_path, private)
    require_private_directory(tmp_path)
    _set_acl(tmp_path, private + "(A;OICI;GR;;;WD)")
    with pytest.raises(ProtectionError):
        require_private_directory(tmp_path)


def test_private_parent_does_not_admit_broad_existing_file(tmp_path: Path, user_sid: str) -> None:
    child = tmp_path / "synthetic.db"
    child.write_bytes(b"synthetic metadata")
    _set_acl(child, f"D:P(A;;FA;;;{user_sid})(A;;GR;;;WD)")
    require_private_directory(tmp_path)
    with pytest.raises(ProtectionError):
        require_private_file(child)


@pytest.mark.parametrize("owner", ["user", "SY", "BA"])
def test_trusted_owners_and_empty_or_standard_private_dacl(user_sid: str, owner: str) -> None:
    owner = user_sid if owner == "user" else owner
    _check_sddl(f"O:{owner}D:P", user_sid)
    _check_sddl(f"O:{owner}D:P(A;OICI;FA;;;{user_sid})(A;ID;FA;;;SY)(A;;FA;;;BA)", user_sid)
    _check_sddl(f"O:{owner}D:P(D;;GR;;;WD)(A;;FA;;;{user_sid})", user_sid)


@pytest.mark.parametrize(
    "unsafe_ace",
    [
        "(A;;GR;;;WD)",
        "(A;ID;GR;;;WD)",
        "(A;OICIIO;GR;;;WD)",
        "(A;;WD;;;WD)",
        "(A;;WO;;;BU)",
        "(A;OICIIO;FA;;;CO)",
        "(D;;GR;;;WD)(A;;GR;;;WD)",
        "(OA;;FA;00112233-4455-6677-8899-aabbccddeeff;;SY)",
    ],
)
def test_broad_and_unsupported_acl_entries_fail_closed(user_sid: str, unsafe_ace: str) -> None:
    with pytest.raises(ProtectionError):
        _check_sddl(f"O:{user_sid}D:P(A;;FA;;;{user_sid}){unsafe_ace}", user_sid)


def test_null_dacl_and_untrusted_owner_are_rejected(user_sid: str) -> None:
    for descriptor in (
        f"O:{user_sid}D:NO_ACCESS_CONTROL",
        f"O:BUD:P(A;;FA;;;{user_sid})",
        "O:BUD:P(A;OICI;FA;;;S-1-3-4)",
    ):
        with pytest.raises(ProtectionError):
            _check_sddl(descriptor, user_sid)


def test_owner_rights_resolves_only_to_the_already_trusted_owner(user_sid: str) -> None:
    for owner in (user_sid, "SY", "BA"):
        _check_sddl(f"O:{owner}D:P(A;OICI;FA;;;S-1-3-4)", user_sid)


@pytest.mark.parametrize(
    "native_name",
    [
        "_OpenProcessToken",
        "_GetTokenInformation",
        "_ConvertSidToStringSidW",
        "_IsValidSid",
        "_IsValidAcl",
        "_GetAclInformation",
        "_GetAce",
    ],
)
def test_acl_native_failure_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_name: str,
) -> None:
    monkeypatch.setattr(protection, native_name, lambda *args: 0)
    with pytest.raises(ProtectionError, match=protection._MESSAGE):
        require_private_directory(tmp_path)


def test_security_descriptor_is_freed_on_native_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "win32"
    original, native_free = protection._GetNamedSecurityInfoW, protection._LocalFree
    allocated: list[int] = []
    freed: list[int] = []

    def fail(*args: Any) -> int:
        assert original(*args) == 0
        pointer = ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p)).contents.value
        assert pointer is not None
        allocated.append(pointer)
        return 5

    def free(pointer: int) -> Any:
        freed.append(pointer)
        return native_free(pointer)

    monkeypatch.setattr(protection, "_GetNamedSecurityInfoW", fail)
    monkeypatch.setattr(protection, "_LocalFree", free)
    with pytest.raises(ProtectionError):
        require_private_directory(tmp_path)
    assert len(allocated) == 1 and freed.count(allocated[0]) == 1


def test_token_is_closed_when_sizing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sys.platform == "win32"
    original = protection._CloseHandle
    closed: list[int] = []

    def close(handle: Any) -> Any:
        closed.append(handle.value)
        return original(handle)

    monkeypatch.setattr(protection, "_GetTokenInformation", lambda *args: 0)
    monkeypatch.setattr(protection, "_CloseHandle", close)
    with pytest.raises(ProtectionError):
        protection._current_user_sid()
    assert len(closed) == 1


def test_new_private_leaf_under_broad_parent_preserves_parent_acl(
    tmp_path: Path, user_sid: str
) -> None:
    _set_acl(tmp_path, f"D:P(A;OICI;FA;;;{user_sid})(A;OICI;GR;;;WD)")
    before = _acl_snapshot(tmp_path)
    leaf = tmp_path / "new-app-data"
    assert create_private_directory(leaf) is True
    require_private_directory(leaf)
    file = leaf / "synthetic.db"
    file.write_bytes(b"synthetic metadata")
    require_private_file(file)
    assert _acl_snapshot(tmp_path) == before
    leaf_before = _acl_snapshot(leaf)
    assert create_private_directory(leaf) is False
    assert _acl_snapshot(leaf) == leaf_before


def test_existing_broad_leaf_is_never_repaired(tmp_path: Path, user_sid: str) -> None:
    leaf = tmp_path / "existing-app-data"
    leaf.mkdir()
    _set_acl(leaf, f"D:P(A;OICI;FA;;;{user_sid})(A;OICI;GR;;;BU)")
    before = _acl_snapshot(leaf)
    assert create_private_directory(leaf) is False
    assert _acl_snapshot(leaf) == before
    with pytest.raises(ProtectionError):
        require_private_directory(leaf)


def test_private_creation_does_not_create_missing_parents(tmp_path: Path) -> None:
    parent = tmp_path / "missing"
    with pytest.raises(ProtectionError):
        create_private_directory(parent / "leaf")
    assert not parent.exists()


def test_private_creation_failure_frees_descriptor_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "win32"
    original, native_free = (
        protection._ConvertStringSecurityDescriptorToSecurityDescriptorW,
        protection._LocalFree,
    )
    allocated: list[int] = []
    freed: list[int] = []

    def convert(*args: Any) -> Any:
        result = original(*args)
        pointer = ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p)).contents.value
        assert result and pointer is not None
        allocated.append(pointer)
        return result

    def create(*args: Any) -> int:
        ctypes.set_last_error(5)
        return 0

    def free(pointer: int) -> Any:
        freed.append(pointer)
        return native_free(pointer)

    monkeypatch.setattr(
        protection, "_ConvertStringSecurityDescriptorToSecurityDescriptorW", convert
    )
    monkeypatch.setattr(protection, "_CreateDirectoryW", create)
    monkeypatch.setattr(protection, "_LocalFree", free)
    leaf = tmp_path / "no-fallback"
    with pytest.raises(ProtectionError):
        create_private_directory(leaf)
    assert not leaf.exists()
    assert len(allocated) == 1 and freed.count(allocated[0]) == 1
