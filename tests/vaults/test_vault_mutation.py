from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from viki_agent.domain import CanonicalRoot, RootIdentity, Workspace
from viki_agent.vaults import vault_mutation as native
from viki_agent.vaults.vault import EXCLUDED_DIRS, PRIVATE_NAMES, STAGE_PREFIX, WRITE_ONLY_NAMES
from viki_agent.vaults.vault_mutation import (
    BACKUP_PREFIX,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    StagedPublication,
    VaultPublication,
)
from viki_agent.winproc import child_env, run, spawn
from viki_agent.workspaces import root_key

_WIN32_HOLD = pytest.mark.skipif(sys.platform != "win32", reason="needs a Win32 sharing-mode hold")
_WIN32_SHORT_NAME = pytest.mark.skipif(
    sys.platform != "win32", reason="needs a Windows 8.3 short name"
)
_WIN32_JUNCTION = pytest.mark.skipif(
    sys.platform != "win32",
    reason="needs an NTFS junction; the POSIX symlink twin is "
    "test_vault_mutation_posix.py::test_a_symlinked_parent_or_leaf_is_refused",
)

# A guard on work that ends in milliseconds or within one child process's life; only a hang
# reaches it.
HANG_GUARD_S = 30


def _workspace(root: Path, *, policy: str = "ordinary", common: Path | None = None) -> Workspace:
    info = root.stat()
    canonical = CanonicalRoot(
        key=root_key(root.resolve()),
        display=str(root.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    identity = RootIdentity(
        git_root_oids=(),
        git_common_dir=os.path.normcase(str(common.resolve())) if common else None,
        instruction_hashes={},
        established=common is not None,
        inherits_from=None,
        probed_at="2026-09-19T00:00:00.000Z",
    )
    return Workspace(
        workspace_id=str(uuid.uuid4()),
        kind="vault",
        root=canonical,
        name="scratch",
        policy=policy,  # type: ignore[arg-type]
        policy_revision=4,
        revision=1,
        source="opened",
        layout="unknown",
        portable_id=None,
        identity=identity,
        face_dir=None,
        bootstrap="complete",
        persistent=True,
        created_at="2026-09-19T00:00:00.000Z",
        updated_at="2026-09-19T00:00:00.000Z",
    )


def _root(path: Path, *, common: Path | None = None) -> OrdinaryMutationRoot:
    return OrdinaryMutationRoot.from_admitted_workspace(_workspace(path, common=common))


def _stage(
    publication: VaultPublication,
    lease: MutationLease,
    root: OrdinaryMutationRoot,
    ref: str,
    content: bytes,
) -> StagedPublication:
    reservation = publication.reserve()
    return publication.stage(lease, root, ref, reservation, content)


def _fingerprint(path: Path, content: bytes) -> FileFingerprint:
    """The identity and hash a verified primitive expects of `path`, holding `content`."""
    info = path.stat()
    return FileFingerprint(
        f"{info.st_dev}:{info.st_ino}", hashlib.sha256(content).hexdigest(), len(content)
    )


async def _finish(child: object) -> tuple[bytes, bytes]:
    process = child.process  # type: ignore[attr-defined]
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=HANG_GUARD_S)
    child.release()  # type: ignore[attr-defined]
    return stdout, stderr


def test_context_requires_admitted_ordinary_workspace(tmp_path: Path) -> None:
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        OrdinaryMutationRoot.from_admitted_workspace(_workspace(tmp_path, policy="protected"))


@pytest.mark.parametrize(
    "ref,code",
    [
        ("../outside.md", "invalid_ref"),
        ("notes\\outside.md", "invalid_ref"),
        (".git/config", "excluded_path"),
        ("notes/AGENTS.md", "excluded_path"),
        ("notes/.viki-stage-fake.tmp", "excluded_path"),
        ("notes/.viki-backup-fake.tmp", "excluded_path"),
    ],
)
async def test_refs_are_relative_and_generic_write_exclusions_apply(
    tmp_path: Path, ref: str, code: str
) -> None:
    with pytest.raises(MutationError, match=code):
        async with MutationCoordinator().lease(_root(tmp_path), (ref,)):
            pass


def _mixed(name: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(name))


# Every excluded name, at depth 1 and deeper, as a folder and as the leaf, in mixed case.
WRITE_SET = (
    *EXCLUDED_DIRS,
    *PRIVATE_NAMES,
    ".env.synthetic",
    *WRITE_ONLY_NAMES,
    f"{STAGE_PREFIX}x.md",
    f"{BACKUP_PREFIX}x.md",
)
EXCLUDED_REFS = sorted(
    f"{parent}{_mixed(name)}{leaf}"
    for name in WRITE_SET
    for parent in ("", "sub/", "a/b/")
    for leaf in ("/n.md", "")
)


async def test_every_excluded_name_is_refused_at_any_depth(tmp_path: Path) -> None:
    coordinator = MutationCoordinator()
    for ref in EXCLUDED_REFS:
        with pytest.raises(MutationError) as caught:
            async with coordinator.lease(_root(tmp_path), (ref,)):
                pass
        assert caught.value.code == "excluded_path", ref
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "ref",
    [
        "what?.md",
        'say "hi".md',
        "a*b.md",
        "notes/<x>.md",
        "a|b/n.md",
        "x" * 253 + ".md",
        "notes/" + "\U0001f600" * 126 + "x.md",
        ".md",
        "..md",
        "notes/.MD",
        "x.markdown",
        "notes/note",
        "COM1.md",
        "nul.md",
        "a./b.md",
        "a /b.md",
        "a//b.md",
        "a/./b.md",
        "a/\x01.md",
    ],
    ids=lambda ref: ref if len(ref) <= 32 else f"long-{len(ref)}",
)
async def test_names_ntfs_or_the_reader_cannot_hold_are_invalid(tmp_path: Path, ref: str) -> None:
    with pytest.raises(MutationError) as caught:
        async with MutationCoordinator().lease(_root(tmp_path), (ref,)):
            pass
    assert caught.value.code == "invalid_ref"


async def test_a_255_unit_markdown_leaf_is_admitted(tmp_path: Path) -> None:
    leaf = "x" * 252 + ".md"
    async with MutationCoordinator().lease(_root(tmp_path), (f"notes/{leaf}",)) as lease:
        assert lease.require_target(lease.root, f"notes/{leaf}") == ("notes", leaf)


@pytest.mark.parametrize("answer", ["FAT32", "exFAT", "ReFS", None])
@pytest.mark.skipif(
    sys.platform != "win32",
    reason="patches the Win32 volume-information API; the POSIX unsupported-filesystem twin is "
    "test_vault_mutation_posix.py::test_an_unsupported_filesystem_type_refuses_every_primitive",
)
async def test_a_root_on_a_volume_other_than_ntfs_is_refused_before_any_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    def query(*args: object) -> bool:
        if answer is not None:
            args[6].value = answer  # type: ignore[attr-defined]  # the filesystem name buffer
        return answer is not None

    root = _root(tmp_path)
    monkeypatch.setattr(native, "_GetVolumeInformationW", query)
    with pytest.raises(MutationError) as caught:
        async with MutationCoordinator().lease(root, ("note.md",)):
            pass
    assert caught.value.code == "unsupported_filesystem"
    assert list(tmp_path.iterdir()) == []


def _short_name(path: Path) -> str:
    """The 8.3 name Windows gave the last component of `path`, or the long name if it gave none."""
    assert sys.platform == "win32"
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    assert 0 < get_short(str(path), buffer, len(buffer)) < len(buffer), ctypes.get_last_error()
    return Path(buffer.value).name


def _tree(root: Path) -> list[tuple[str, bytes | None]]:
    return sorted(
        (str(path.relative_to(root)), None if path.is_dir() else path.read_bytes())
        for path in root.rglob("*")
    )


@pytest.mark.parametrize("folder", [".claude", "long directory name"])
@_WIN32_SHORT_NAME
async def test_a_save_through_a_short_name_parent_is_refused(tmp_path: Path, folder: str) -> None:
    (tmp_path / folder / "commands").mkdir(parents=True)
    (tmp_path / folder / "commands" / "n.md").write_bytes(b"old")
    alias = _short_name(tmp_path / folder)
    if alias == folder:
        pytest.skip("this volume makes no 8.3 short names")
    before = _tree(tmp_path)
    root, ref = _root(tmp_path), f"{alias}/commands/n.md"
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        with pytest.raises(MutationError) as caught:
            _stage(VaultPublication(), lease, root, ref, b"new")
    assert caught.value.code == "excluded_path"
    assert not caught.value.stage_retained
    assert _tree(tmp_path) == before


@_WIN32_SHORT_NAME
async def test_a_replace_through_a_short_name_leaf_is_refused(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    target = tmp_path / "notes" / "a long note name.md"
    target.write_bytes(b"old")
    alias = _short_name(target)
    if alias == target.name:
        pytest.skip("this volume makes no 8.3 short names")
    root, ref = _root(tmp_path), f"notes/{alias}"
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        staged = _stage(publication, lease, root, ref, b"new")
        with (
            pytest.raises(MutationError) as caught,
            publication.prepare_replace(
                lease, staged, hashlib.sha256(b"old").hexdigest(), staged.backup_name
            ),
        ):
            pass
        assert caught.value.code == "excluded_path"
        assert caught.value.stage_retained
        assert publication.discard(lease, staged)
    assert target.read_bytes() == b"old"
    assert sorted(path.name for path in (tmp_path / "notes").iterdir()) == [target.name]


async def test_alias_and_ancestor_leases_serialize(tmp_path: Path) -> None:
    root = _root(tmp_path)
    alias = OrdinaryMutationRoot(
        workspace_id=str(uuid.uuid4()),
        path=root.path,
        canonical_key=root.canonical_key,
        physical_id=root.physical_id,
        policy_revision=root.policy_revision,
        git_common_dir=None,
    )
    coordinator = MutationCoordinator()
    entered = asyncio.Event()

    async def nested() -> None:
        async with coordinator.lease(alias, ("notes/topic.md/item.md",)):
            entered.set()

    async with coordinator.lease(root, ("notes/topic.md",)):
        task = asyncio.create_task(nested())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=HANG_GUARD_S)
    assert entered.is_set()


async def test_lease_capability_covers_only_its_target_or_descendants(tmp_path: Path) -> None:
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("notes/one.md",)) as lease:
        with pytest.raises(MutationError, match="lease_required"):
            _stage(VaultPublication(), lease, root, "notes/two.md", b"two")


async def test_shared_git_common_directory_serializes_separate_roots(tmp_path: Path) -> None:
    first, second, common = tmp_path / "first", tmp_path / "second", tmp_path / "common.git"
    first.mkdir()
    second.mkdir()
    common.mkdir()
    coordinator = MutationCoordinator()
    entered = asyncio.Event()

    async def second_lease() -> None:
        async with coordinator.lease(_root(second, common=common), (), include_git_common=True):
            entered.set()

    async with coordinator.lease(_root(first, common=common), (), include_git_common=True):
        task = asyncio.create_task(second_lease())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=HANG_GUARD_S)


async def test_create_only_publication_is_handle_relative_and_verified(tmp_path: Path) -> None:
    root = _root(tmp_path)
    coordinator, publication = MutationCoordinator(), VaultPublication()
    async with coordinator.lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"first")
        assert uuid.UUID(staged.operation_id).version == 4
        assert staged.stage_name == f"{native.STAGE_PREFIX}{staged.operation_id}.tmp"
        assert staged.backup_name == f"{BACKUP_PREFIX}{staged.operation_id}.tmp"
        published = publication.publish_create(lease, staged)
    assert published.fingerprint == staged.fingerprint
    assert (tmp_path / "note.md").read_bytes() == b"first"
    assert not (tmp_path / staged.stage_name).exists()


def test_reservation_allocates_deterministic_names_without_writing(tmp_path: Path) -> None:
    reservation = VaultPublication().reserve()
    assert uuid.UUID(reservation.operation_id).version == 4
    assert reservation.stage_name == f"{native.STAGE_PREFIX}{reservation.operation_id}.tmp"
    assert reservation.backup_name == f"{BACKUP_PREFIX}{reservation.operation_id}.tmp"
    assert list(tmp_path.iterdir()) == []


async def test_stage_create_new_collision_preserves_existing_entry(tmp_path: Path) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()
    reservation = publication.reserve()
    stage_path = tmp_path / reservation.stage_name
    stage_path.write_bytes(b"external")
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        with pytest.raises(MutationError, match="stage_collision"):
            publication.stage(lease, root, "note.md", reservation, b"new")
    assert stage_path.read_bytes() == b"external"
    assert not (tmp_path / "note.md").exists()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("flush", "stage_failed"),
        ("write", "stage_failed"),
        ("short_write", "short_write"),
        ("hash", "unsafe_stage"),
    ],
)
async def test_a_post_create_stage_failure_deletes_its_own_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, code: str
) -> None:
    """No journal row records the stage yet, so a failed write, flush or verification
    deletes it through the handle that created it, and reports nothing retained."""
    root = _root(tmp_path)
    publication = VaultPublication()
    reservation = publication.reserve()
    real_write = native._write_handle

    def failing_write(handle: native.FileHandle, content: bytes) -> None:
        if failure == "hash":
            real_write(handle, b"other bytes")
            return
        real_write(handle, content[:1])
        if failure == "short_write":
            raise MutationError("short_write", stage_retained=True)
        raise OSError(errno.ENOSPC, "WriteFile")

    def failing_flush(_handle: native.FileHandle) -> None:
        raise OSError("synthetic")

    if failure == "flush":
        monkeypatch.setattr(native, "_flush", failing_flush)
    else:
        monkeypatch.setattr(native, "_write_handle", failing_write)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            publication.stage(lease, root, "note.md", reservation, b"new")
    assert (caught.value.code, caught.value.stage_retained) == (code, False)
    assert list(tmp_path.iterdir()) == []


async def test_a_stage_whose_delete_fails_is_reported_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a failed delete leaves the unrecorded stage, and says so."""
    root = _root(tmp_path)
    publication = VaultPublication()
    reservation = publication.reserve()

    def failing_flush(_handle: native.FileHandle) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(native, "_flush", failing_flush)

    def delete_fails(_handle: native.FileHandle) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(native, "_delete_handle", delete_fails)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            publication.stage(lease, root, "note.md", reservation, b"new")
    assert (caught.value.code, caught.value.stage_retained) == ("stage_failed", True)
    assert (tmp_path / reservation.stage_name).read_bytes() == b"new"


async def test_one_character_stems_cover_create_replace_and_recovery(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_bytes(b"old-b")
    (tmp_path / "c.md").write_bytes(b"old-c")
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("a.md", "b.md", "c.md")) as lease:
        created = _stage(publication, lease, root, "a.md", b"new-a")
        publication.publish_create(lease, created)

        replaced = _stage(publication, lease, root, "b.md", b"new-b")
        with publication.prepare_replace(
            lease, replaced, hashlib.sha256(b"old-b").hexdigest(), replaced.backup_name
        ) as prepared:
            prepared.move_target_to_backup()
            prepared.publish_stage_no_replace()
            prepared.verify_published()
        publication.discard_backup_after_publish(
            lease,
            root,
            "b.md",
            replaced.backup_name,
            replaced.fingerprint,
            prepared.old,
        )

        recovering = _stage(publication, lease, root, "c.md", b"new-c")
        with publication.prepare_replace(
            lease, recovering, hashlib.sha256(b"old-c").hexdigest(), recovering.backup_name
        ) as interrupted:
            interrupted.move_target_to_backup()
        publication.move_verified_no_replace(
            lease, root, "c.md", recovering.backup_name, interrupted.old
        )
    assert (tmp_path / "a.md").read_bytes() == b"new-a"
    assert (tmp_path / "b.md").read_bytes() == b"new-b"
    assert (tmp_path / "c.md").read_bytes() == b"old-c"
    assert (tmp_path / recovering.stage_name).read_bytes() == b"new-c"


async def test_create_only_collision_preserves_target_and_stage(tmp_path: Path) -> None:
    root = _root(tmp_path)
    coordinator, publication = MutationCoordinator(), VaultPublication()
    async with coordinator.lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"new")
        (tmp_path / "note.md").write_bytes(b"racer")
        with pytest.raises(MutationError) as caught:
            publication.publish_create(lease, staged)
        assert caught.value.code == "conflict"
    assert (tmp_path / "note.md").read_bytes() == b"racer"
    assert (tmp_path / staged.stage_name).read_bytes() == b"new"


async def test_expected_hash_conflict_and_recoverable_replace_steps(
    tmp_path: Path,
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    old_hash = hashlib.sha256(b"old").hexdigest()
    root = _root(tmp_path)
    coordinator, publication = MutationCoordinator(), VaultPublication()
    async with coordinator.lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"new")
        backup_name = staged.backup_name
        with (
            pytest.raises(MutationError) as stale,
            publication.prepare_replace(lease, staged, "0" * 64, backup_name),
        ):
            pass
        assert stale.value.code == "conflict"
        with publication.prepare_replace(lease, staged, old_hash, backup_name) as prepared:
            assert prepared.old.sha256 == old_hash
            prepared.move_target_to_backup()
            assert not target.exists()
            assert (tmp_path / backup_name).exists()
            prepared.publish_stage_no_replace()
            assert prepared.verify_published().fingerprint == staged.fingerprint
        assert (tmp_path / backup_name).read_bytes() == b"old"
        publication.discard_backup_after_publish(
            lease,
            root,
            "note.md",
            backup_name,
            staged.fingerprint,
            prepared.old,
        )
    assert target.read_bytes() == b"new"
    assert not (tmp_path / backup_name).exists()


async def test_gap_racer_is_preserved_with_old_backup_and_new_stage(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    root = _root(tmp_path)
    coordinator, publication = MutationCoordinator(), VaultPublication()
    async with coordinator.lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"new")
        backup_name = staged.backup_name
        with publication.prepare_replace(
            lease, staged, hashlib.sha256(b"old").hexdigest(), backup_name
        ) as prepared:
            old = prepared.old
            prepared.move_target_to_backup()
            target.write_bytes(b"external")
            with pytest.raises(MutationError, match="conflict"):
                prepared.publish_stage_no_replace()
    assert target.read_bytes() == b"external"
    assert (tmp_path / backup_name).read_bytes() == b"old"
    assert (tmp_path / staged.stage_name).read_bytes() == b"new"

    async with coordinator.lease(root, ("note.md",)) as lease:
        with pytest.raises(MutationError, match="conflict"):
            publication.move_verified_no_replace(lease, root, "note.md", backup_name, old)
    assert target.read_bytes() == b"external"
    assert (tmp_path / backup_name).read_bytes() == b"old"
    assert (tmp_path / staged.stage_name).read_bytes() == b"new"


async def test_exact_backup_recovery_restores_only_an_absent_target(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    root = _root(tmp_path)
    coordinator, publication = MutationCoordinator(), VaultPublication()
    async with coordinator.lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"new")
        backup_name = staged.backup_name
        with publication.prepare_replace(
            lease, staged, hashlib.sha256(b"old").hexdigest(), backup_name
        ) as prepared:
            old = prepared.old
            prepared.move_target_to_backup()
    assert not target.exists()

    async with coordinator.lease(root, ("note.md",)) as lease:
        recovered = publication.move_verified_no_replace(lease, root, "note.md", backup_name, old)
    assert recovered.fingerprint == old
    assert target.read_bytes() == b"old"
    assert (tmp_path / staged.stage_name).read_bytes() == b"new"


async def test_same_hash_different_identity_is_not_recovery_material(tmp_path: Path) -> None:
    source = tmp_path / f"{BACKUP_PREFIX}{uuid.uuid4().hex}.tmp"
    source.write_bytes(b"old")
    stat_result = source.stat()
    wrong = FileFingerprint(
        f"{stat_result.st_dev}:{stat_result.st_ino + 1}",
        hashlib.sha256(b"old").hexdigest(),
        3,
    )
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        publication = VaultPublication()
        assert publication.probe_verified(lease, root, "note.md", None, wrong) == "absent"
        assert (
            publication.probe_verified(lease, root, "note.md", source.name, wrong) == "unexpected"
        )
        with pytest.raises(MutationError, match="stale_stage"):
            publication.move_verified_no_replace(lease, root, "note.md", source.name, wrong)
    assert source.read_bytes() == b"old"
    assert not (tmp_path / "note.md").exists()


async def test_probe_matches_only_the_recorded_physical_identity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = _stage(publication, lease, root, "note.md", b"new")
        assert (
            publication.probe_verified(
                lease, root, "note.md", staged.stage_name, staged.fingerprint
            )
            == "match"
        )


@_WIN32_HOLD
async def test_existing_external_writer_blocks_expected_hash_probe(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = _stage(VaultPublication(), lease, root, "note.md", b"new")
    code = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
f = k.CreateFileW
f.restype = wintypes.HANDLE
f.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
h = f(sys.argv[1], 0x40000000, 7, None, 3, 0, None)
print('ready' if h != ctypes.c_void_p(-1).value else ctypes.get_last_error(), flush=True)
sys.stdin.buffer.read(1)
"""
    child = await spawn(
        [sys.executable, "-I", "-c", code, str(target)],
        cwd=Path.cwd(),
        env=child_env("vault-mutation-test", os.environ),
    )
    assert child.process.stdout is not None
    line = await asyncio.wait_for(child.process.stdout.readline(), timeout=HANG_GUARD_S)
    assert line == b"ready\r\n"
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            with VaultPublication().prepare_replace(
                lease,
                staged,
                hashlib.sha256(b"old").hexdigest(),
                staged.backup_name,
            ):
                pass
    assert caught.value.code == "target_busy"
    assert child.process.stdin is not None
    child.process.stdin.write(b"x")
    await child.process.stdin.drain()
    _, stderr = await _finish(child)
    assert stderr == b""
    assert target.read_bytes() == b"old"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="the parent guard blocks an external rename only under a Win32 directory share mode; "
    "on POSIX this rename is not guarded",
)
async def test_parent_guard_blocks_external_directory_rename(tmp_path: Path) -> None:
    parent = tmp_path / "notes"
    parent.mkdir()
    target = parent / "note.md"
    target.write_bytes(b"old")
    destination = tmp_path / "notes-moved"
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("notes/note.md",)) as lease:
        staged = _stage(publication, lease, root, "notes/note.md", b"new")
        with publication.prepare_replace(
            lease,
            staged,
            hashlib.sha256(b"old").hexdigest(),
            staged.backup_name,
        ):
            code = r"""
import os, sys
try:
    os.rename(sys.argv[1], sys.argv[2])
except OSError as exc:
    print(exc.winerror, flush=True)
else:
    print(0, flush=True)
"""
            child = await spawn(
                [sys.executable, "-I", "-c", code, str(parent), str(destination)],
                cwd=Path.cwd(),
                env=child_env("vault-parent-guard-test", os.environ),
            )
            stdout, stderr = await _finish(child)
            assert stdout.strip() in {b"5", b"32"}
            assert stderr == b""
        assert publication.discard(lease, staged)
    assert parent.is_dir()
    assert not destination.exists()


@_WIN32_JUNCTION
async def test_parent_walk_refuses_a_reparse_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    created = await asyncio.to_thread(
        run,
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(real)],
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stderr
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("linked/note.md",)) as lease:
        with pytest.raises(MutationError, match="unsafe_parent"):
            _stage(VaultPublication(), lease, root, "linked/note.md", b"blocked")
    assert not (real / "note.md").exists()


async def test_a_missing_folder_holds_no_stage_but_a_file_at_its_name_refuses(
    tmp_path: Path,
) -> None:
    """Discard and probe read a deleted folder as an absent entry, nothing else."""
    (tmp_path / "sub").mkdir()
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("sub/note.md",)) as lease:
        staged = _stage(publication, lease, root, "sub/note.md", b"new")
        (tmp_path / "sub" / staged.stage_name).unlink()
        (tmp_path / "sub").rmdir()
        assert publication.discard(lease, staged) is False
        for name in (staged.stage_name, None):
            probed = publication.probe_verified(
                lease, root, "sub/note.md", name, staged.fingerprint
            )
            assert probed == "absent"

        (tmp_path / "sub").write_bytes(b"a file, not a folder")
        with pytest.raises(MutationError, match="unsafe_parent"):
            publication.discard(lease, staged)
        with pytest.raises(MutationError, match="unsafe_parent"):
            publication.probe_verified(lease, root, "sub/note.md", None, staged.fingerprint)


@_WIN32_HOLD
async def test_guard_denies_external_writer_and_also_prevents_atomic_replace(
    tmp_path: Path,
) -> None:
    assert sys.platform == "win32"
    target, stage = tmp_path / "note.md", tmp_path / ".viki-stage-probe.tmp"
    target.write_bytes(b"old")
    stage.write_bytes(b"new")
    target_handle = native._open_file(target, native._GENERIC_READ | native._DELETE)
    code = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
f = k.CreateFileW
f.restype = wintypes.HANDLE
f.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
h = f(sys.argv[1], 0x40000000, 7, None, 3, 0, None)
print(ctypes.get_last_error() if h == ctypes.c_void_p(-1).value else 0, flush=True)
"""
    child = await spawn(
        [sys.executable, "-I", "-c", code, str(target)],
        cwd=Path.cwd(),
        env=child_env("vault-mutation-test", os.environ),
    )
    stdout, stderr = await _finish(child)
    assert stdout.strip() == b"32"  # ERROR_SHARING_VIOLATION
    assert stderr == b""
    stage_handle = native._open_file(stage, native._GENERIC_READ | native._DELETE)
    try:
        with pytest.raises(OSError) as caught:
            native._rename_handle(stage_handle, target, replace=True)
        assert caught.value.winerror == 32
    finally:
        native._CloseHandle(stage_handle)
        native._CloseHandle(target_handle)
    assert target.read_bytes() == b"old"
    assert stage.read_bytes() == b"new"


async def test_child_crash_after_stage_preserves_target_and_stage(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    info = tmp_path.stat()
    code = r"""
import asyncio, os, sys
from pathlib import Path
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator, OrdinaryMutationRoot, VaultPublication,
)
from viki_agent.workspaces import root_key
async def main():
    root = OrdinaryMutationRoot('scratch', Path(sys.argv[1]), root_key(sys.argv[1]),
                                sys.argv[2], 1, None)
    async with MutationCoordinator().lease(root, ('note.md',)) as lease:
        publication = VaultPublication()
        staged = publication.stage(lease, root, 'note.md', publication.reserve(), b'new')
        print(staged.stage_name, flush=True)
        os._exit(79)
asyncio.run(main())
"""
    child = await spawn(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(tmp_path.resolve()),
            f"{info.st_dev}:{info.st_ino}",
        ],
        cwd=Path.cwd(),
        env=child_env("vault-mutation-crash-test", os.environ),
    )
    stdout, stderr = await _finish(child)
    assert child.process.returncode == 79, stderr.decode("utf-8", "replace")
    stage_name = stdout.decode("utf-8").strip()
    assert stage_name.startswith(native.STAGE_PREFIX)
    assert target.read_bytes() == b"old"
    assert (tmp_path / stage_name).read_bytes() == b"new"


async def test_child_crash_after_backup_rename_recovers_old_without_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    info = tmp_path.stat()
    code = r"""
import asyncio, hashlib, json, os, sys, uuid
from pathlib import Path
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator, OrdinaryMutationRoot, VaultPublication,
)
from viki_agent.workspaces import root_key
async def main():
    root = OrdinaryMutationRoot('scratch', Path(sys.argv[1]), root_key(sys.argv[1]),
                                sys.argv[2], 1, None)
    async with MutationCoordinator().lease(root, ('note.md',)) as lease:
        publication = VaultPublication()
        staged = publication.stage(lease, root, 'note.md', publication.reserve(), b'new')
        backup = staged.backup_name
        with publication.prepare_replace(
            lease, staged, hashlib.sha256(b'old').hexdigest(), backup
        ) as prepared:
            record = {
                'stage': staged.stage_name,
                'backup': backup,
                'physical_id': prepared.old.physical_id,
                'sha256': prepared.old.sha256,
                'byte_size': prepared.old.byte_size,
            }
            print(json.dumps(record), flush=True)
            prepared.move_target_to_backup()
            os._exit(79)
asyncio.run(main())
"""
    child = await spawn(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(tmp_path.resolve()),
            f"{info.st_dev}:{info.st_ino}",
        ],
        cwd=Path.cwd(),
        env=child_env("vault-mutation-backup-crash-test", os.environ),
    )
    stdout, stderr = await _finish(child)
    assert child.process.returncode == 79, stderr.decode("utf-8", "replace")
    record = json.loads(stdout)
    assert not target.exists()
    assert (tmp_path / record["backup"]).read_bytes() == b"old"
    assert (tmp_path / record["stage"]).read_bytes() == b"new"

    root = _root(tmp_path)
    old = FileFingerprint(record["physical_id"], record["sha256"], record["byte_size"])
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        VaultPublication().move_verified_no_replace(lease, root, "note.md", record["backup"], old)
    assert target.read_bytes() == b"old"
    assert (tmp_path / record["stage"]).read_bytes() == b"new"


async def test_a_cross_parent_move_is_no_replace_and_verified(tmp_path: Path) -> None:
    """One no-replace rename between parents, verified through the handle it held."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    source = tmp_path / "a" / "n.md"
    source.write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"note")
    async with MutationCoordinator().lease(root, ("a/n.md", "b/n.md")) as lease:
        moved = VaultPublication().move_verified(lease, root, "a/n.md", "b/n.md", expected)
    assert moved == native.PublishedPublication("b/n.md", expected)
    assert (tmp_path / "b" / "n.md").read_bytes() == b"note"
    assert list((tmp_path / "a").iterdir()) == []


async def test_a_case_only_rename_needs_no_special_path(tmp_path: Path) -> None:
    source = tmp_path / "n.md"
    source.write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"note")
    async with MutationCoordinator().lease(root, ("n.md", "N.md")) as lease:
        moved = VaultPublication().move_verified(lease, root, "n.md", "N.md", expected)
    assert moved.ref == "N.md"
    assert [path.name for path in tmp_path.iterdir()] == ["N.md"]
    assert (tmp_path / "N.md").read_bytes() == b"note"


async def test_a_move_onto_a_taken_name_across_parents_is_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    source = tmp_path / "a" / "n.md"
    source.write_bytes(b"mine")
    (tmp_path / "b" / "n.md").write_bytes(b"theirs")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"mine")
    async with MutationCoordinator().lease(root, ("a/n.md", "b/n.md")) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, "a/n.md", "b/n.md", expected)
    assert caught.value.code == "conflict"
    assert source.read_bytes() == b"mine"
    assert (tmp_path / "b" / "n.md").read_bytes() == b"theirs"


@_WIN32_SHORT_NAME
async def test_a_move_onto_an_8_3_alias_of_a_taken_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    taken = tmp_path / "b" / "a long note name.md"
    taken.write_bytes(b"theirs")
    alias = _short_name(taken)
    if alias == taken.name:
        pytest.skip("this volume makes no 8.3 short names")
    source = tmp_path / "n.md"
    source.write_bytes(b"mine")
    root, dest_ref = _root(tmp_path), f"b/{alias}"
    expected = _fingerprint(source, b"mine")
    async with MutationCoordinator().lease(root, ("n.md", dest_ref)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, "n.md", dest_ref, expected)
    assert caught.value.code == "conflict"
    assert taken.read_bytes() == b"theirs"
    assert source.read_bytes() == b"mine"
    assert sorted(path.name for path in (tmp_path / "b").iterdir()) == [taken.name]


@_WIN32_JUNCTION
async def test_a_junction_destination_parent_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    created = await asyncio.to_thread(
        run,
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(tmp_path / "linked"), str(real)],
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stderr
    source = tmp_path / "n.md"
    source.write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"note")
    async with MutationCoordinator().lease(root, ("n.md", "linked/n.md")) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, "n.md", "linked/n.md", expected)
    assert caught.value.code == "unsafe_parent"
    assert source.read_bytes() == b"note"
    assert list(real.iterdir()) == []


@_WIN32_HOLD
async def test_a_move_refuses_a_source_an_external_writer_holds(tmp_path: Path) -> None:
    source = tmp_path / "n.md"
    source.write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"note")
    code = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
f = k.CreateFileW
f.restype = wintypes.HANDLE
f.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
h = f(sys.argv[1], 0x40000000, 7, None, 3, 0, None)
print('ready' if h != ctypes.c_void_p(-1).value else ctypes.get_last_error(), flush=True)
sys.stdin.buffer.read(1)
"""
    child = await spawn(
        [sys.executable, "-I", "-c", code, str(source)],
        cwd=Path.cwd(),
        env=child_env("vault-move-holder-test", os.environ),
    )
    assert child.process.stdout is not None
    line = await asyncio.wait_for(child.process.stdout.readline(), timeout=HANG_GUARD_S)
    assert line == b"ready\r\n"
    async with MutationCoordinator().lease(root, ("n.md", "moved.md")) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, "n.md", "moved.md", expected)
    assert caught.value.code == "target_busy"
    assert child.process.stdin is not None
    child.process.stdin.write(b"x")
    await child.process.stdin.drain()
    _, stderr = await _finish(child)
    assert stderr == b""
    assert source.read_bytes() == b"note"
    assert not (tmp_path / "moved.md").exists()


async def test_a_move_refuses_a_source_a_writer_changed(tmp_path: Path) -> None:
    source = tmp_path / "n.md"
    source.write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(source, b"note")
    source.write_bytes(b"gone")
    async with MutationCoordinator().lease(root, ("n.md", "moved.md")) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, "n.md", "moved.md", expected)
    assert caught.value.code == "conflict"
    assert source.read_bytes() == b"gone"
    assert not (tmp_path / "moved.md").exists()


@pytest.mark.parametrize(
    ("source_ref", "dest_ref"),
    [
        ("n.md", ".git/n.md"),
        (".claude/n.md", "n.md"),
        ("n.md", "sub/CLAUDE.md"),
        ("n.md", f"{STAGE_PREFIX}x.md"),
    ],
)
async def test_a_move_refuses_an_excluded_name_at_either_end(
    tmp_path: Path, source_ref: str, dest_ref: str
) -> None:
    (tmp_path / "n.md").write_bytes(b"note")
    root = _root(tmp_path)
    expected = _fingerprint(tmp_path / "n.md", b"note")
    before = _tree(tmp_path)
    async with MutationCoordinator().lease(root, ()) as lease:  # the whole root
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, root, source_ref, dest_ref, expected)
    assert caught.value.code == "excluded_path"
    assert _tree(tmp_path) == before


async def test_the_bytes_removed_are_the_bytes_read(tmp_path: Path) -> None:
    """One identity and hash spans the read and the remove."""
    note = tmp_path / "sub" / "n.md"
    note.parent.mkdir()
    note.write_bytes(b"keep me")
    root = _root(tmp_path)
    expected = _fingerprint(note, b"keep me")
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("sub/n.md",)) as lease:
        assert publication.read_verified(lease, root, "sub/n.md", expected) == b"keep me"
        assert note.read_bytes() == b"keep me"
        publication.remove_verified(lease, root, "sub/n.md", expected)
    assert not note.exists()
    assert (tmp_path / "sub").is_dir()


@pytest.mark.parametrize("size", [0, 7, 64 * 1024 + 7])
async def test_a_read_returns_every_byte_of_the_note(tmp_path: Path, size: int) -> None:
    """A read crosses the 64 KiB chunk the handle reads at a time, and an empty note is no bytes."""
    note = tmp_path / "n.md"
    content = bytes(index % 251 for index in range(size))
    note.write_bytes(content)
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("n.md",)) as lease:
        read = VaultPublication().read_verified(lease, root, "n.md", _fingerprint(note, content))
    assert read == content


async def test_a_changed_note_is_neither_read_nor_removed(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    note.write_bytes(b"old")
    root = _root(tmp_path)
    expected = _fingerprint(note, b"old")
    note.write_bytes(b"new")
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("n.md",)) as lease:
        for call in (publication.read_verified, publication.remove_verified):
            with pytest.raises(MutationError) as caught:
                call(lease, root, "n.md", expected)
            assert caught.value.code == "conflict"
    assert note.read_bytes() == b"new"


async def test_a_note_over_the_cap_is_not_read_removed_or_moved(tmp_path: Path) -> None:
    """The note cap refuses every new primitive before it changes anything."""
    note = tmp_path / "n.md"
    content = b"x" * (native.MAX_PUBLICATION_BYTES + 1)
    note.write_bytes(content)
    root = _root(tmp_path)
    expected = _fingerprint(note, content)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("n.md", "moved.md")) as lease:
        calls = (
            lambda: publication.read_verified(lease, root, "n.md", expected),
            lambda: publication.remove_verified(lease, root, "n.md", expected),
            lambda: publication.move_verified(lease, root, "n.md", "moved.md", expected),
        )
        for call in calls:
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "unsafe_target"
        # The save path's stage keeps the note cap too, and refuses before it writes.
        with pytest.raises(MutationError) as caught:
            _stage(publication, lease, root, "moved.md", content)
        assert caught.value.code == "invalid_content"
    assert note.read_bytes() == content
    assert _tree(tmp_path) == [("n.md", content)]


async def test_folders_are_created_only_when_a_write_needs_them(tmp_path: Path) -> None:
    """Create-only, verified, reported, and never removed again."""
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("a/b/n.md",)) as lease:
        assert publication.ensure_folders(lease, root, ("a", "b")) == ("a", "a/b")
        assert (tmp_path / "a" / "b").is_dir()
        # Already there: nothing created, nothing reported, nothing removed.
        assert publication.ensure_folders(lease, root, ("a", "b")) == ()
        assert publication.ensure_folders(lease, root, ()) == ()
    assert (tmp_path / "a" / "b").is_dir()
    assert _tree(tmp_path) == [("a", None), (str(Path("a") / "b"), None)]


async def test_a_file_or_an_excluded_name_at_a_folder_component_refuses(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"a file, not a folder")
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("a/b/n.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            publication.ensure_folders(lease, root, ("a", "b"))
        assert caught.value.code == "unsafe_parent"
    async with MutationCoordinator().lease(root, ()) as lease:
        with pytest.raises(MutationError) as caught:
            publication.ensure_folders(lease, root, (".git", "hooks"))
        assert caught.value.code == "excluded_path"
    assert _tree(tmp_path) == [("a", b"a file, not a folder")]


async def test_folder_creation_needs_a_lease_on_that_line_of_the_tree(tmp_path: Path) -> None:
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("x/n.md",)) as lease:
        with pytest.raises(MutationError, match="lease_required"):
            VaultPublication().ensure_folders(lease, root, ("a", "b"))
    assert list(tmp_path.iterdir()) == []


async def test_a_lease_deadline_refuses_instead_of_waiting_for_ever(tmp_path: Path) -> None:
    """A wait past the injected deadline refuses, and names no path."""
    root = _root(tmp_path)
    coordinator = MutationCoordinator()
    async with coordinator.lease(root, ("note.md",)):
        with pytest.raises(MutationError) as caught:
            async with coordinator.lease(root, ("note.md",), wait_s=0.01):
                pass
    assert caught.value.code == "vault_locked"
    assert str(tmp_path) not in str(caught.value)
    # The deadline refuses one wait, not the resource: it is free again at once.
    async with coordinator.lease(root, ("note.md",), wait_s=0.05) as lease:
        assert lease.require_target(root, "note.md") == ("note.md",)


# `ensure_folders` repeats `_safe_ref`'s per-part rules inline, so the two are driven over
# one table: a rule added to one and not the other fails here. Long admitted names stay out, so a
# folder this test creates never needs a path over MAX_PATH.
PER_PART_CASES = (
    *((_mixed(name), "excluded_path") for name in WRITE_SET),
    ("plain", None),
    ("two words", None),
    ("x" * 256, "invalid_ref"),
    ("\U0001f600" * 128, "invalid_ref"),
    ("COM1", "invalid_ref"),
    ("nul", "invalid_ref"),
    ("CON.md", "invalid_ref"),
    ("a.", "invalid_ref"),
    ("a ", "invalid_ref"),
    (".git.", "invalid_ref"),
    ("what?", "invalid_ref"),
    ("a|b", "invalid_ref"),
    ("<x>", "invalid_ref"),
    ('"q"', "invalid_ref"),
    ("a*b", "invalid_ref"),
    ("\x01", "invalid_ref"),
)


async def test_safe_ref_and_ensure_folders_apply_the_same_per_part_rules(tmp_path: Path) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()

    def as_ref_part(part: str) -> str | None:
        try:
            native._safe_ref(f"{part}/n.md")
        except MutationError as exc:
            return exc.code
        return None

    def as_folder(lease: MutationLease, part: str) -> str | None:
        try:
            publication.ensure_folders(lease, root, (part,))
        except MutationError as exc:
            return exc.code
        return None

    async with MutationCoordinator().lease(root, ()) as lease:
        for part, code in PER_PART_CASES:
            assert (as_ref_part(part), as_folder(lease, part)) == (code, code), part
    assert sorted(path.name for path in tmp_path.iterdir()) == ["plain", "two words"]


@pytest.mark.parametrize(
    "ref", ["attachments/a.png", "attachments/a.jpg", "attachments/a.JPEG", "attachments/a.webp"]
)
def test_an_attachment_ref_is_one_image_directly_under_attachments(ref: str) -> None:
    assert native.attachment_ref(ref) == tuple(ref.split("/"))
    # The note grammar is unchanged: an image is no note.
    with pytest.raises(MutationError, match="invalid_ref"):
        native._safe_ref(ref)


@pytest.mark.parametrize(
    "ref,code",
    [
        ("a.png", "invalid_ref"),
        ("attachments/a/b.png", "invalid_ref"),
        ("x/attachments/a.png", "invalid_ref"),
        ("Attachments/a.png", "invalid_ref"),
        ("attachment/a.png", "invalid_ref"),
        ("attachments", "invalid_ref"),
        ("attachments/", "invalid_ref"),
        ("attachments/.a.png", "invalid_ref"),
        ("attachments/.png", "invalid_ref"),
        ("attachments/a.gif", "invalid_ref"),
        ("attachments/a.svg", "invalid_ref"),
        ("attachments/a.md", "invalid_ref"),
        ("attachments/a.png.exe", "invalid_ref"),
        ("attachments/a", "invalid_ref"),
        ("../attachments/a.png", "invalid_ref"),
        ("attachments\\a.png", "invalid_ref"),
        ("attachments/a.png:stream", "invalid_ref"),
        ("attachments/COM1.png", "invalid_ref"),
        ("attachments/nul.jpg", "invalid_ref"),
        ("attachments/a.png.", "invalid_ref"),
        ("attachments/a?.png", "invalid_ref"),
        ("attachments/" + "x" * 252 + ".png", "invalid_ref"),
        ("attachments/.viki-stage-x.png", "excluded_path"),
        ("attachments/.viki-backup-x.png", "excluded_path"),
        ("attachments/.env.png", "excluded_path"),
        ("attachments/.GIT", "excluded_path"),
        (".git/a.png", "excluded_path"),
        (".viki/attachments/a.png", "excluded_path"),
    ],
    ids=lambda value: value if len(value) <= 40 else f"long-{len(value)}",
)
def test_an_attachment_ref_refuses_every_other_shape(ref: str, code: str) -> None:
    with pytest.raises(MutationError) as caught:
        native.attachment_ref(ref)
    assert caught.value.code == code
    assert not caught.value.stage_retained


async def test_the_ref_decides_the_cap_so_a_note_never_stages_over_two_mib(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()
    over_note_cap = b"x" * (native.MAX_PUBLICATION_BYTES + 1)
    refs = ("a.md", "attachments/a.md", "attachments/a.png", "attachments/b.webp")
    async with MutationCoordinator().lease(root, refs) as lease:
        assert publication.ensure_folders(lease, root, ("attachments",)) == ("attachments",)
        # A note keeps its cap wherever it sits, though the attachment cap is ten times larger.
        for note in ("a.md", "attachments/a.md"):
            with pytest.raises(MutationError) as caught:
                _stage(publication, lease, root, note, over_note_cap)
            assert caught.value.code == "invalid_content"
            assert not caught.value.stage_retained
        staged = _stage(publication, lease, root, "attachments/a.png", over_note_cap)
        published = publication.publish_create(lease, staged)
        assert published.ref == "attachments/a.png"
        ceiling = b"y" * native.MAX_STAGE_BYTES
        largest = _stage(publication, lease, root, "attachments/b.webp", ceiling)
        assert largest.byte_size == native.MAX_STAGE_BYTES
        assert publication.discard(lease, largest)
        with pytest.raises(MutationError) as caught:
            _stage(publication, lease, root, "attachments/b.webp", ceiling + b"y")
        assert caught.value.code == "invalid_content"
    assert _tree(tmp_path) == [
        ("attachments", None),
        (str(Path("attachments") / "a.png"), over_note_cap),
    ]


async def test_every_other_primitive_refuses_an_attachment(tmp_path: Path) -> None:
    """Rename, move, archive and delete stay note-only: only a create's steps take an image."""
    root = _root(tmp_path)
    publication = VaultPublication()
    ref = "attachments/a.png"
    async with MutationCoordinator().lease(root, (ref, "attachments/b.png")) as lease:
        publication.ensure_folders(lease, root, ("attachments",))
        staged = _stage(publication, lease, root, ref, b"image")
        publication.publish_create(lease, staged)
        kept = _fingerprint(tmp_path / "attachments" / "a.png", b"image")
        other = _stage(publication, lease, root, "attachments/b.png", b"other")
        calls = {
            "require_target": lambda: lease.require_target(root, ref),
            "read_verified": lambda: publication.read_verified(lease, root, ref, kept),
            "remove_verified": lambda: publication.remove_verified(lease, root, ref, kept),
            "move_verified": lambda: publication.move_verified(
                lease, root, ref, "attachments/b.png", kept
            ),
            "move_verified_no_replace": lambda: publication.move_verified_no_replace(
                lease, root, ref, other.stage_name, kept
            ),
            "probe_verified": lambda: publication.probe_verified(lease, root, ref, None, kept),
            "discard_backup_after_publish": lambda: publication.discard_backup_after_publish(
                lease, root, ref, other.backup_name, kept, kept
            ),
        }
        for name, call in calls.items():
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "invalid_ref", name
        with (
            pytest.raises(MutationError) as caught,
            publication.prepare_replace(lease, other, "0" * 64, other.backup_name),
        ):
            pass
        assert caught.value.code == "invalid_ref"
        assert publication.discard(lease, other)
    assert (tmp_path / "attachments" / "a.png").read_bytes() == b"image"
    assert sorted(path.name for path in (tmp_path / "attachments").iterdir()) == ["a.png"]


async def test_an_attachment_hash_is_verified_streamed_and_none_when_absent(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()
    ref = "attachments/a.png"
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        # No folder and no file are both absent, and neither is created.
        assert publication.attachment_sha256(lease, root, ref) is None
        (tmp_path / "attachments").mkdir()
        assert publication.attachment_sha256(lease, root, ref) is None
        # Over the note cap is still an attachment.
        content = b"z" * (native.MAX_PUBLICATION_BYTES + 1)
        (tmp_path / "attachments" / "a.png").write_bytes(content)
        assert publication.attachment_sha256(lease, root, ref) == (
            hashlib.sha256(content).hexdigest()
        )
    assert (tmp_path / "attachments" / "a.png").read_bytes() == content


@pytest.mark.parametrize("shape", ["hard_link", "symlink", "folder", "over_the_ceiling"])
async def test_an_attachment_hash_refuses_what_a_note_primitive_refuses(
    tmp_path: Path, shape: str
) -> None:
    (tmp_path / "attachments").mkdir()
    target = tmp_path / "attachments" / "a.png"
    if shape in {"hard_link", "symlink"}:
        (tmp_path / "elsewhere.png").write_bytes(b"image")
    if shape == "hard_link":
        os.link(tmp_path / "elsewhere.png", target)
    elif shape == "symlink":
        try:
            os.symlink(tmp_path / "elsewhere.png", target)
        except OSError:
            pytest.skip("this account may not create a symbolic link")
    elif shape == "folder":
        target.mkdir()
    else:
        target.write_bytes(b"z" * (native.MAX_STAGE_BYTES + 1))
    before = _tree(tmp_path)
    root = _root(tmp_path)
    async with MutationCoordinator().lease(root, ("attachments/a.png",)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().attachment_sha256(lease, root, "attachments/a.png")
    assert caught.value.code == "unsafe_target"
    assert not caught.value.stage_retained
    assert _tree(tmp_path) == before


async def test_an_attachment_hash_takes_only_a_held_attachment_ref(tmp_path: Path) -> None:
    root = _root(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(root, ("n.md", "attachments/a.png")) as lease:
        for ref in ("n.md", "attachments/a/b.png", "attachments/.a.png"):
            with pytest.raises(MutationError) as caught:
                publication.attachment_sha256(lease, root, ref)
            assert caught.value.code == "invalid_ref", ref
        with pytest.raises(MutationError, match="lease_required"):
            publication.attachment_sha256(lease, root, "attachments/b.png")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="drives the raw Win32 handle API directly; the cap itself is proven on every OS by "
    "the tests above",
)
def test_the_fingerprint_cap_is_the_note_cap_unless_a_larger_one_is_asked(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    assert native.MAX_PUBLICATION_BYTES == 2 * 1024 * 1024
    assert native.MAX_STAGE_BYTES == 20 * 1024 * 1024
    content = b"x" * (native.MAX_PUBLICATION_BYTES + 1)
    (tmp_path / "a.png").write_bytes(content)
    handle = native._open_file(
        tmp_path / "a.png", native._FILE_READ_DATA | native._FILE_READ_ATTRIBUTES
    )
    try:
        with pytest.raises(MutationError) as caught:
            native._fingerprint_handle(handle, unsafe_code="unsafe_target")
        assert caught.value.code == "unsafe_target"
        larger = native._fingerprint_handle(
            handle, unsafe_code="unsafe_target", max_bytes=native.MAX_STAGE_BYTES
        )
        assert (larger.sha256, larger.byte_size) == (
            hashlib.sha256(content).hexdigest(),
            len(content),
        )
        with pytest.raises(MutationError) as caught:
            native._fingerprint_handle(
                handle, unsafe_code="unsafe_target", max_bytes=native.MAX_STAGE_BYTES + 1
            )
        assert caught.value.code == "invalid_content"
        assert not caught.value.stage_retained
    finally:
        native._CloseHandle(handle)
