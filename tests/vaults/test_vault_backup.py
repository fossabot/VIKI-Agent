"""The vault backup service: a verified `notes` copy outside every root, and no vault write.

Every row runs against a real on-disk vault and a real destination, and asserts `snapshot` equality
on the source afterwards, so a write this module ever makes into a vault shows up as a changed
entry (hard rule 1). The destination rule is `exports.py`'s promoted one, called here rather than
re-implemented.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.app.test_data_api import make_junction
from viki_agent.domain import CanonicalRoot, Workspace
from viki_agent.vaults import vault_backup
from viki_agent.vaults.vault_backup import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    MANIFEST_NAME,
    MAX_BACKUP_BYTES,
    MAX_BACKUP_FILES,
    PARTIAL_SUFFIX,
    BackupError,
    VaultBackupService,
)
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

NOW = 1_700_000_000.123
STAMP = "20231114-221320"
VAULT_NAME = "VIKI-synthetic"
# The deadline the server injects into the real lease (its `wait_s`); the fake below refuses as
# that deadline would, so no test waits on one.
LEASE_WAIT_S = 0.05

# (relative path, is_dir, size, sha256); a link is recorded, never followed.
Entry = tuple[str, bool, int, str]


def snapshot(root: Path) -> list[Entry]:
    """Every entry under `root`, from a walk that never follows a symlink or junction."""
    entries: list[Entry] = []
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as listing:
            for item in listing:
                relative = os.path.relpath(item.path, root)
                if item.is_symlink() or item.is_junction():
                    entries.append((relative, False, -1, "link"))
                elif item.is_dir(follow_symlinks=False):
                    entries.append((relative, True, 0, ""))
                    pending.append(Path(item.path))
                else:
                    with open(item.path, "rb") as reader:
                        data = reader.read()
                    entries.append((relative, False, len(data), hashlib.sha256(data).hexdigest()))
    return sorted(entries)


class _Workspaces:
    """Only what the backup service reaches: the registry guard, `get` and `current_root`."""

    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}
        self.available = True
        self.registration_lock = threading.Lock()

    def add(self, root: Path, policy: str = "protected") -> str:
        info = os.stat(root)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            policy=policy,
            root=CanonicalRoot(
                key=root_key(root.resolve()),
                display=str(root.resolve()),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        return workspace_id

    def get(self, workspace_id: str) -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root if self.available else None


class _Lease:
    """The lease the server passes: held for the whole copy, refusing once its deadline passes."""

    def __init__(self) -> None:
        self.entered = 0
        self.locked = False
        self.wait_s = LEASE_WAIT_S

    @asynccontextmanager
    async def __call__(self, workspace: Workspace) -> AsyncIterator[None]:
        if self.locked:
            raise MutationError("vault_locked")
        self.entered += 1
        yield None


@dataclass
class Harness:
    service: VaultBackupService
    spaces: _Workspaces
    lease: _Lease
    root: Path
    dest: Path
    wid: str

    def note(self, ref: str, text: str) -> str:
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    async def run(self, destination: str | None = None) -> tuple[Any, bool]:
        return await self.service.run(str(uuid.uuid4()), self.wid, destination or str(self.dest))


def harness(tmp_path: Path, *, policy: str = "protected") -> Harness:
    root = tmp_path / VAULT_NAME
    root.mkdir()
    dest = tmp_path / "backups"
    dest.mkdir()
    spaces = _Workspaces()
    wid = spaces.add(root, policy)
    lease = _Lease()
    service = VaultBackupService(
        cast(WorkspaceService, spaces),
        lease=lease,
        # The registered root is in the deny set, exactly as `__main__` builds it for the export.
        denied_roots=lambda: [root, tmp_path / "appdata"],
        app_version="0.1.0-test",
        now=lambda: NOW,
    )
    return Harness(service, spaces, lease, root, dest, wid)


def manifest_of(folder: Path) -> dict[str, Any]:
    with open(folder / MANIFEST_NAME, encoding="utf-8") as handle:
        return json.load(handle)


def test_the_bound_is_two_thousand_files_and_two_hundred_fifty_six_mebibytes() -> None:
    assert MAX_BACKUP_FILES == 2_000
    assert MAX_BACKUP_BYTES == 256 * 1024 * 1024


async def test_backup_is_verified_and_the_source_is_untouched(tmp_path: Path) -> None:
    kit = harness(tmp_path)
    digests = {
        "note.md": kit.note("note.md", "# The first one\n"),
        "30-knowledge/深い.markdown": kit.note("30-knowledge/深い.markdown", "deep\n"),
        "AGENTS.md": kit.note("AGENTS.md", "instructions\n"),
    }
    kit.note("img.png", "not a note\n")  # not a note suffix
    kit.note(".git/config", "[core]\n")  # the reader's excluded directories
    kit.note(".obsidian/workspace.md", "a dot directory\n")
    kit.note(".env", "SECRET=1\n")
    kit.note("30-knowledge/.viki-stage-x.md", "the writer's own temporary file\n")
    before = snapshot(kit.root)

    preview = await kit.service.preview(kit.wid, str(kit.dest))
    assert preview.files == 3
    assert preview.bytes == sum((kit.root / ref).stat().st_size for ref in digests)
    assert preview.instruction_files == ("AGENTS.md",)
    assert preview.skipped == ()
    assert preview.local_recovery is True  # the destination is on the vault's own disk

    result, replayed = await kit.run()
    assert replayed is False
    assert kit.lease.entered == 1
    assert result.folder == f"{VAULT_NAME} backup {STAMP}"
    assert (result.files, result.bytes, result.skipped) == (3, preview.bytes, ())

    folder = kit.dest / result.folder
    assert sorted(item.name for item in kit.dest.iterdir()) == [result.folder]  # no .partial left
    document = manifest_of(folder)
    assert document["format"] == BACKUP_FORMAT
    assert document["version"] == BACKUP_VERSION
    assert document["scope"] == "notes"
    assert document["encrypted"] is False
    assert document["created_at"] == "2023-11-14T22:13:20.123Z"
    assert document["app_version"] == "0.1.0-test"
    assert document["source"] == {
        "name": VAULT_NAME,
        "policy": "protected",
        "workspace_id": kit.wid,
    }
    assert document["skipped"] == []
    assert document["totals"] == {"files": 3, "bytes": preview.bytes, "skipped": 0}
    for row in document["files"]:
        ref = row["path"]
        copy = folder / Path(ref)
        assert row["sha256"] == digests[ref] == hashlib.sha256(copy.read_bytes()).hexdigest()
        assert row["bytes"] == len(copy.read_bytes()) == (kit.root / ref).stat().st_size
    assert {row["path"] for row in document["files"]} == set(digests)
    assert not (folder / "img.png").exists()
    assert not (folder / ".git").exists()
    assert not (folder / ".obsidian").exists()
    assert not (folder / ".env").exists()
    assert not (folder / "30-knowledge/.viki-stage-x.md").exists()
    assert snapshot(kit.root) == before


async def test_a_repeated_command_id_answers_the_first_copy(tmp_path: Path) -> None:
    kit = harness(tmp_path, policy="ordinary")
    kit.note("note.md", "Only once\n")
    before = snapshot(kit.root)
    command_id = str(uuid.uuid4())

    first, replayed = await kit.service.run(command_id, kit.wid, str(kit.dest))
    assert replayed is False
    again, replayed = await kit.service.run(command_id, kit.wid, str(kit.dest))
    assert (again, replayed) == (first, True)

    assert [item.name for item in kit.dest.iterdir()] == [first.folder]
    assert kit.lease.entered == 1  # the replay never took the lease again
    assert snapshot(kit.root) == before


@pytest.mark.parametrize("kind", ["preview", "run"])
async def test_a_denied_or_device_destination_refuses(tmp_path: Path, kind: str) -> None:
    kit = harness(tmp_path)
    kit.note("30-knowledge/note.md", "Keep safe\n")
    before = snapshot(kit.root)
    appdata = tmp_path / "appdata" / "logs"
    appdata.mkdir(parents=True)
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("not a folder\n", encoding="utf-8")
    cases: list[tuple[str, str]] = [
        (f"\\\\?\\{kit.dest}", "invalid_path"),  # the past security fix: never normalised
        ("\\\\server\\share", "invalid_path"),
        ("//server/share", "invalid_path"),
        ("", "invalid_path"),
        (str(tmp_path / "missing"), "invalid_path"),
        (str(a_file), "invalid_path"),
        (str(appdata.parent), "denied_destination"),  # the app data directory
        (str(appdata), "denied_destination"),  # and anything under it
        (str(kit.root), "denied_destination"),  # the registered root itself
        (str(kit.root / "30-knowledge"), "denied_destination"),  # and inside the vault
    ]
    for destination, code in cases:
        with pytest.raises(BackupError) as excinfo:
            if kind == "preview":
                await kit.service.preview(kit.wid, destination)
            else:
                await kit.service.run(str(uuid.uuid4()), kit.wid, destination)
        assert excinfo.value.code == code, destination

    assert list(kit.dest.iterdir()) == []
    assert list(appdata.parent.iterdir()) == [appdata]  # nothing was written into the deny set
    assert snapshot(kit.root) == before


async def test_a_reparse_point_is_skipped_and_never_followed(tmp_path: Path) -> None:
    kit = harness(tmp_path)
    kit.note("note.md", "An ordinary note\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.md").write_text("A note elsewhere\n", encoding="utf-8")
    make_junction(kit.root / "linked", outside)
    before, outside_before = snapshot(kit.root), snapshot(outside)

    preview = await kit.service.preview(kit.wid, str(kit.dest))
    assert [(row.ref, row.reason) for row in preview.skipped] == [("linked", "reparse_point")]
    assert preview.files == 1

    result, _ = await kit.run()
    folder = kit.dest / result.folder
    document = manifest_of(folder)
    assert document["skipped"] == [{"path": "linked", "reason": "reparse_point"}]
    assert document["totals"] == {"files": 1, "bytes": result.bytes, "skipped": 1}
    assert [row["path"] for row in document["files"]] == ["note.md"]
    assert not (folder / "linked").exists()
    assert not (folder / "elsewhere.md").exists()
    assert snapshot(kit.root) == before
    assert snapshot(outside) == outside_before  # the junction's target was never read or written


# Names the manifest path rule refuses, so the restore would refuse a manifest that listed one.
# Windows 11 creates only the device names; it refuses the rest outright.
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses the name itself")
REFUSED_NAMES = [
    "aux.md",
    "CON.markdown",
    pytest.param("Why?.md", marks=POSIX_ONLY),
    pytest.param('Say "hi".md', marks=POSIX_ONLY),
    pytest.param("<draft>.md", marks=POSIX_ONLY),
    pytest.param("a|b.md", marks=POSIX_ONLY),
    pytest.param("Acme Inc./note.md", marks=POSIX_ONLY),
    pytest.param("trailing /note.md", marks=POSIX_ONLY),
]


@pytest.mark.parametrize("ref", REFUSED_NAMES)
async def test_a_name_outside_the_manifest_path_rule_is_listed_as_skipped(
    tmp_path: Path, ref: str
) -> None:
    kit = harness(tmp_path)
    kit.note("note.md", "An ordinary note\n")
    kit.note(ref, "A note whose name fails\n")
    before = snapshot(kit.root)

    preview = await kit.service.preview(kit.wid, str(kit.dest))
    assert [(row.ref, row.reason) for row in preview.skipped] == [(ref, "unsafe_name")]
    result, _ = await kit.run()
    assert (result.files, result.skipped) == (1, preview.skipped)
    folder = kit.dest / result.folder
    document = manifest_of(folder)
    assert [row["path"] for row in document["files"]] == ["note.md"]
    assert document["skipped"] == [{"path": ref, "reason": "unsafe_name"}]
    assert not (folder / ref).exists()
    assert snapshot(kit.root) == before


async def test_names_one_filesystem_would_store_as_one_keep_only_the_first(tmp_path: Path) -> None:
    """A case-sensitive filesystem holds names a restore onto a case- or normalisation-insensitive
    one could not tell apart; the first in sorted order is copied and the rest are skipped."""
    kit = harness(tmp_path)
    kit.note("Note.md", "The bigger one\n")
    try:
        with open(kit.root / "note.md", "xb") as handle:
            handle.write(b"small\n")
    except FileExistsError:
        pytest.skip("this filesystem stores the two names as one")
    kit.note("A.md/x.md", "a folder\n")
    kit.note("a.md", "a note of the same name\n")
    kit.note("café.md", "NFC\n")
    kit.note("café.md", "NFD\n")
    before = snapshot(kit.root)

    result, _ = await kit.run()
    document = manifest_of(kit.dest / result.folder)
    assert [row["path"] for row in document["files"]] == ["A.md/x.md", "Note.md", "café.md"]
    assert document["skipped"] == [
        {"path": ref, "reason": "unsafe_name"} for ref in ("a.md", "café.md", "note.md")
    ]
    assert snapshot(kit.root) == before


async def test_a_note_past_the_note_cap_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = harness(tmp_path)
    kit.note("small.md", "short\n")
    kit.note("big.md", "x" * 64)
    before = snapshot(kit.root)
    monkeypatch.setattr(vault_backup, "MAX_NOTE_BYTES", 16)

    result, _ = await kit.run()
    assert [(row.ref, row.reason) for row in result.skipped] == [("big.md", "too_large")]
    folder = kit.dest / result.folder
    assert [row["path"] for row in manifest_of(folder)["files"]] == ["small.md"]
    assert not (folder / "big.md").exists()
    assert snapshot(kit.root) == before


async def test_a_copy_that_reads_back_wrong_refuses_and_renames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = harness(tmp_path)
    kit.note("note.md", "A note to verify\n")
    before = snapshot(kit.root)
    monkeypatch.setattr(vault_backup, "_read_digest", lambda path: (0, "0" * 64))

    with pytest.raises(BackupError) as excinfo:
        await kit.run()
    assert excinfo.value.code == "backup_unverified"

    partial = [item.name for item in kit.dest.iterdir()]
    assert partial == [f"{VAULT_NAME} backup {STAMP}{PARTIAL_SUFFIX}"]  # never renamed
    assert not (kit.dest / partial[0] / MANIFEST_NAME).exists()  # the manifest is written last
    assert snapshot(kit.root) == before


@pytest.mark.parametrize("bound", ["MAX_BACKUP_FILES", "MAX_BACKUP_BYTES"])
async def test_over_the_bound_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    kit = harness(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        kit.note(name, "big\n")
    before = snapshot(kit.root)
    monkeypatch.setattr(vault_backup, bound, 2)

    for call in (
        kit.service.preview(kit.wid, str(kit.dest)),
        kit.service.run(str(uuid.uuid4()), kit.wid, str(kit.dest)),
    ):
        with pytest.raises(BackupError) as excinfo:
            await call
        assert excinfo.value.code == "backup_too_large"

    assert list(kit.dest.iterdir()) == []
    assert snapshot(kit.root) == before


async def test_a_held_lease_past_its_deadline_refuses_and_writes_nothing(tmp_path: Path) -> None:
    kit = harness(tmp_path, policy="ordinary")
    kit.note("note.md", "A note being saved\n")
    before = snapshot(kit.root)
    kit.lease.locked = True

    with pytest.raises(BackupError) as excinfo:
        await kit.run()
    assert excinfo.value.code == "vault_locked"
    assert list(kit.dest.iterdir()) == []
    assert snapshot(kit.root) == before


async def test_an_unknown_workspace_or_an_unavailable_root_refuses(tmp_path: Path) -> None:
    kit = harness(tmp_path)
    with pytest.raises(BackupError) as excinfo:
        await kit.service.preview(str(uuid.uuid4()), str(kit.dest))
    assert excinfo.value.code == "unknown_workspace"

    kit.spaces.available = False
    with pytest.raises(BackupError) as excinfo:
        await kit.run()
    assert excinfo.value.code == "root_unavailable"
    assert list(kit.dest.iterdir()) == []


@pytest.mark.parametrize(
    ("start", "then", "recorded"),
    [
        ("ordinary", "protected", "protected"),
        ("protected", "ordinary", "protected"),
        ("ordinary", "existing_readonly", "existing_readonly"),
    ],
)
async def test_the_manifest_names_the_stricter_of_the_two_policy_readings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: str, then: str, recorded: str
) -> None:
    """Binding can only narrow, so a vault whose policy changes during the copy is labelled
    with the stricter of the policy read at the start and the one read before the manifest."""
    kit = harness(tmp_path, policy=start)
    kit.note("note.md", "A note whose policy changes mid-copy\n")
    copy = kit.service._copy

    def change_then_copy(ref: str, source: Path, target: Path) -> Any:
        # A fresh record, as the real service reads its row on every `get`.
        kit.spaces.spaces[kit.wid] = SimpleNamespace(
            **{**vars(kit.spaces.get(kit.wid)), "policy": then}
        )
        return copy(ref, source, target)

    monkeypatch.setattr(kit.service, "_copy", change_then_copy)

    result, _ = await kit.run()

    assert manifest_of(kit.dest / result.folder)["source"]["policy"] == recorded


async def test_an_empty_command_id_refuses_before_the_vault_is_read(tmp_path: Path) -> None:
    kit = harness(tmp_path)
    with pytest.raises(BackupError) as excinfo:
        await kit.service.run("", kit.wid, str(kit.dest))
    assert excinfo.value.code == "invalid_request"
    assert list(kit.dest.iterdir()) == []


async def test_escaped_skip_rows_over_the_manifest_cap_leave_only_an_unfinished_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = harness(tmp_path)
    kit.note("kept.md", "a valid copied note\n")
    before = snapshot(kit.root)
    names = {"\u5408" * 90 + f"_{index:05}.md" for index in range(18_000)}
    assert len(names) + 1 < vault_backup.MAX_VISITED_ITEMS
    real_lstat = Path.lstat
    real_scandir = os.scandir

    class Listing:
        def __enter__(self) -> Listing:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def __iter__(self) -> Any:
            return iter(SimpleNamespace(name=name) for name in [*names, "kept.md"])

    def oversized(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.parent == kit.root and path.name in names:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=vault_backup.MAX_NOTE_BYTES + 1,
                st_file_attributes=0,
            )
        return real_lstat(path, *args, **kwargs)

    def listing(path: Any) -> Any:
        return Listing() if Path(path) == kit.root else real_scandir(path)

    command = str(uuid.uuid4())
    with monkeypatch.context() as patched:
        patched.setattr(Path, "lstat", oversized)
        patched.setattr(os, "scandir", listing)
        with pytest.raises(BackupError) as refused:
            await kit.service.run(command, kit.wid, str(kit.dest))
    assert refused.value.code == "backup_too_large"
    partial = kit.dest / f"{VAULT_NAME} backup {STAMP}{PARTIAL_SUFFIX}"
    assert list(kit.dest.iterdir()) == [partial]
    assert (partial / "kept.md").read_bytes() == (kit.root / "kept.md").read_bytes()
    assert not (partial / MANIFEST_NAME).exists()
    assert command not in kit.service._receipts
    assert not kit.spaces.registration_lock.locked()
    assert snapshot(kit.root) == before


@pytest.mark.parametrize("over_by", [0, 1])
async def test_the_exact_manifest_byte_bound_controls_final_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, over_by: int
) -> None:
    kit = harness(tmp_path)
    kit.note("note.md", "a valid note\n")
    made, _ = await kit.run()
    raw = (kit.dest / made.folder / MANIFEST_NAME).read_bytes()
    destination = tmp_path / "bounded"
    destination.mkdir()
    monkeypatch.setattr(vault_backup, "MAX_MANIFEST_BYTES", len(raw) - over_by)

    if over_by:
        with pytest.raises(BackupError) as refused:
            await kit.run(str(destination))
        assert refused.value.code == "backup_too_large"
        assert [path.name for path in destination.iterdir()] == [made.folder + PARTIAL_SUFFIX]
        assert not (destination / (made.folder + PARTIAL_SUFFIX) / MANIFEST_NAME).exists()
    else:
        result, _ = await kit.run(str(destination))
        assert (destination / result.folder / MANIFEST_NAME).read_bytes() == raw
