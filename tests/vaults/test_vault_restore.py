"""Backup restore: an untrusted manifest copied into a fresh folder, registered read-only.

Every case builds a synthetic vault, backs it up with the real `VaultBackupService`, and restores
it with a real `WorkspaceService` in `tmp_path`. `snapshot` equality shows what a restore leaves
alone: the source vault, the backup and every folder that already existed (hard rule 1).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import make_junction, protected_at, service_at
from tests.vaults.test_vault_backup import snapshot
from viki_agent.domain import Workspace
from viki_agent.storage import StateError
from viki_agent.vaults import vault_restore
from viki_agent.vaults.vault_backup import MANIFEST_NAME, MAX_BACKUP_FILES, VaultBackupService
from viki_agent.vaults.vault_restore import (
    BackupRestoreError,
    BackupRestoreProblem,
    BackupRestoreService,
)
from viki_agent.workspaces import WorkspaceService

NOTES = {"a.md": "# A\n", "sub/b.md": "# B\n", "sub/deep/c.markdown": "c\n"}


@asynccontextmanager
async def _no_lease(workspace: Workspace) -> AsyncIterator[None]:
    yield None


@dataclass
class Harness:
    tmp: Path
    workspaces: WorkspaceService
    vault: Path
    source_id: str
    backup: Path
    restores: Path
    service: BackupRestoreService

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.backup / MANIFEST_NAME).read_text(encoding="utf-8"))

    def write_manifest(self, document: Any) -> None:
        (self.backup / MANIFEST_NAME).write_text(json.dumps(document), encoding="utf-8")

    def edit(self, change: Callable[[dict[str, Any]], None]) -> None:
        document = self.manifest()
        change(document)
        self.write_manifest(document)

    async def apply(self, folder: str = "Restored", parent: Path | None = None) -> Any:
        result, replayed = await self.service.apply(
            str(uuid.uuid4()), str(self.backup), str(parent or self.restores), folder
        )
        assert replayed is False
        return result

    async def refused(self, folder: str = "Restored", parent: Path | None = None) -> str:
        with pytest.raises(BackupRestoreError) as failure:
            await self.apply(folder, parent)
        return failure.value.code


async def harness(
    tmp_path: Path, *, protected: bool = True, extra: dict[str, str] | None = None
) -> Harness:
    workspaces = service_at(tmp_path)
    vault = tmp_path / "vaults" / "Synthetic"
    for ref, text in {**NOTES, **(extra or {})}.items():
        (vault / ref).parent.mkdir(parents=True, exist_ok=True)
        (vault / ref).write_bytes(text.encode("utf-8"))
    if protected:
        source_id = protected_at(workspaces, vault)
    else:
        source_id = workspaces.register_existing(str(uuid.uuid4()), str(vault))[0].workspace_id

    def denied() -> list[Path]:
        return [tmp_path / "data", *(Path(w.root.display) for w in workspaces.list())]

    backups = tmp_path / "backups"
    backups.mkdir()
    made, _ = await VaultBackupService(
        workspaces, lease=_no_lease, denied_roots=denied, app_version="test", now=lambda: 1.0e9
    ).run(str(uuid.uuid4()), source_id, str(backups))
    restores = tmp_path / "restores"
    restores.mkdir()
    return Harness(
        tmp=tmp_path,
        workspaces=workspaces,
        vault=vault,
        source_id=source_id,
        backup=backups / made.folder,
        restores=restores,
        service=BackupRestoreService(workspaces, denied_roots=denied),
    )


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def test_restore_keeps_protection_and_never_overwrites(tmp_path: Path) -> None:
    """A new folder only, every byte verified, bound protected as its source was."""
    h = await harness(tmp_path)
    vault_before, backup_before = snapshot(h.vault), snapshot(h.backup)
    registrations = len(h.workspaces.list())
    command = str(uuid.uuid4())

    result, replayed = await h.service.apply(command, str(h.backup), str(h.restores), "Restored")

    assert replayed is False
    copy = h.restores / "Restored"
    assert tree(copy) == {ref: text.encode("utf-8") for ref, text in NOTES.items()}
    assert (result.files, result.bytes, result.verified, result.problems) == (
        3,
        sum(len(text.encode("utf-8")) for text in NOTES.values()),
        True,
        (),
    )
    workspace = h.workspaces.get(result.workspace.workspace_id)
    assert (workspace.policy, workspace.source) == ("protected", "opened")
    assert Path(workspace.root.display) == copy.resolve()
    assert not (h.restores / "Restored.partial").exists()
    assert len(h.workspaces.list()) == registrations + 1
    # The same command answers with the copy it made rather than making another.
    assert await h.service.apply(command, str(h.backup), str(h.restores), "Restored") == (
        result,
        True,
    )

    # Never an existing folder, a registered root, inside one, or the deny set.
    copy_before = snapshot(copy)
    assert await h.refused("Restored") == "conflict"
    assert await h.refused("x", parent=h.vault) == "protected_overlap"
    assert await h.refused("x", parent=copy) == "protected_overlap"
    (h.tmp / "data").mkdir(exist_ok=True)
    assert await h.refused("x", parent=h.tmp / "data") in {"denied_location", "denied_destination"}
    assert await h.refused("x", parent=h.tmp / "absent") == "invalid_path"
    assert await h.refused("CON") == "invalid_name"
    assert snapshot(copy) == copy_before
    assert snapshot(h.vault) == vault_before and snapshot(h.backup) == backup_before
    assert sorted(path.name for path in h.restores.iterdir()) == ["Restored"]
    assert len(h.workspaces.list()) == registrations + 1


async def test_an_existing_destination_refuses_even_when_empty(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    (h.restores / "Empty").mkdir()
    (h.restores / "Stale.partial").mkdir()
    registrations = len(h.workspaces.list())
    before = snapshot(h.restores)
    assert await h.refused("Empty") == "conflict"
    assert await h.refused("Stale") == "conflict"
    assert snapshot(h.restores) == before
    assert len(h.workspaces.list()) == registrations


def _drop_protected(document: dict[str, Any]) -> None:
    document["source"]["policy"] = "existing_readonly"


def _dot_dot(document: dict[str, Any]) -> None:
    document["files"][0]["path"] = "../escape.md"


def _dot_git(document: dict[str, Any]) -> None:
    document["files"][0]["path"] = "sub/.git/hooks.md"


def _wrong_hash(document: dict[str, Any]) -> None:
    row = next(row for row in document["files"] if row["path"] == "a.md")
    row["sha256"] = sha("# not A\n")


@pytest.mark.parametrize(
    "tamper", [_drop_protected, _dot_dot, _dot_git, _wrong_hash], ids=lambda f: f.__name__
)
async def test_a_tampered_manifest_widens_nothing(
    tmp_path: Path, tamper: Callable[[dict[str, Any]], None]
) -> None:
    h = await harness(tmp_path)
    h.edit(tamper)
    registrations = len(h.workspaces.list())
    outside = snapshot(h.tmp / "vaults"), snapshot(h.backup)

    if tamper in (_dot_dot, _dot_git):
        # A path outside the rule refuses the whole manifest before anything is created.
        assert await h.refused() == "invalid_backup"
        assert list(h.restores.iterdir()) == [] and not (h.tmp / "escape.md").exists()
        assert len(h.workspaces.list()) == registrations
    else:
        result = await h.apply()
        assert h.workspaces.get(result.workspace.workspace_id).policy == "protected"
        if tamper is _wrong_hash:
            # The file whose bytes differ from the manifest is not copied, and the restore says
            # it is unverified rather than looking like a verified one.
            assert result.verified is False
            assert result.problems == (BackupRestoreProblem("a.md", "mismatch"),)
            assert set(tree(h.restores / "Restored")) == {"sub/b.md", "sub/deep/c.markdown"}
        else:
            assert result.verified is True
    assert (snapshot(h.tmp / "vaults"), snapshot(h.backup)) == outside


async def test_a_forged_source_is_still_bound_protected_by_its_identity(tmp_path: Path) -> None:
    """With the policy dropped and the source id forged, the copied instruction file's hash
    still matches the protected source, so the copy inherits protection."""
    h = await harness(tmp_path, extra={"CLAUDE.md": "# governed\n"})

    def forge(document: dict[str, Any]) -> None:
        document["source"] = {"name": "x", "policy": "ordinary", "workspace_id": str(uuid.uuid4())}

    h.edit(forge)
    preview = await h.service.preview(str(h.backup))
    assert preview.protected is False and preview.instruction_files == ("CLAUDE.md",)
    result = await h.apply()
    workspace = h.workspaces.get(result.workspace.workspace_id)
    assert workspace.policy == "protected" and workspace.identity.inherits_from == h.source_id


async def test_a_protected_restore_never_commits_an_unprotected_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is registered and bound protected in one store transaction, so no commit
    ever holds it `existing_readonly`, a policy the user could later make writable."""
    h = await harness(tmp_path)
    store = h.workspaces._store
    write = store.update_workspace
    copies: list[str] = []

    def recorded(workspace_id: str, changed: Any) -> Any:
        row = write(workspace_id, changed)
        copies.extend(
            r["policy"] for r in store.list_workspaces() if r["workspace_id"] != h.source_id
        )
        return row

    monkeypatch.setattr(store, "update_workspace", recorded)
    result = await h.apply()
    assert copies == ["protected"]
    assert h.workspaces.get(result.workspace.workspace_id).policy == "protected"


async def test_a_restore_killed_while_binding_protection_leaves_no_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process killed as protection is written leaves no registration, never an unprotected
    one."""
    h = await harness(tmp_path)
    store = h.workspaces._store
    write = store.update_workspace
    before = [w.workspace_id for w in h.workspaces.list()]

    def killed(workspace_id: str, changed: Any) -> Any:
        def binding(current: Any, rows: Any) -> Any:
            row = changed(current, rows)
            if row is not None and row["policy"] == "protected":
                raise RuntimeError("process killed")
            return row

        return write(workspace_id, binding)

    monkeypatch.setattr(store, "update_workspace", killed)
    with pytest.raises(RuntimeError):
        await h.apply()
    assert [w.workspace_id for w in h.workspaces.list()] == before


async def test_the_manifest_alone_saying_protected_binds_the_copy_protected(
    tmp_path: Path,
) -> None:
    """Nothing on this device says so: the source id is unknown here, no instruction file is
    copied and no protected registration exists for the copy's identity to match."""
    h = await harness(tmp_path, protected=False)

    def claim_protected(document: dict[str, Any]) -> None:
        document["source"].update(policy="protected", workspace_id=str(uuid.uuid4()))

    h.edit(claim_protected)
    assert all(w.policy != "protected" for w in h.workspaces.list())
    preview = await h.service.preview(str(h.backup))
    assert (preview.protected, preview.instruction_files) == (True, ())
    result = await h.apply()
    assert h.workspaces.get(result.workspace.workspace_id).policy == "protected"


# Names the manifest path rule admits beside names it refuses, including ones a case- or
# normalisation-insensitive filesystem would store as one.
MIXED_NAMES = (
    "good.md",
    "x .md",
    "aux.md",
    "CON.markdown",
    "Why?.md",
    'Say "hi".md',
    "<draft>.md",
    "a|b.md",
    "Acme Inc./note.md",
    "trailing /note.md",
    "Note.md",
    "note.md",
    "café.md",
    "café.md",
    "A.md/x.md",
)


def _creatable(probe: Path, refs: Sequence[str]) -> dict[str, str]:
    """The refs this filesystem holds when written after `NOTES` in this order, as `harness`
    writes them; a name it refuses is left out."""
    kept: dict[str, str] = {}
    for ref, text in [*NOTES.items(), *((ref, f"{ref}\n") for ref in refs)]:
        try:
            (probe / ref).parent.mkdir(parents=True, exist_ok=True)
            (probe / ref).write_bytes(text.encode("utf-8"))
        except OSError:
            continue
        if ref not in NOTES:
            kept[ref] = text
    return kept


async def test_every_manifest_a_backup_writes_passes_the_restore_preview(tmp_path: Path) -> None:
    h = await harness(tmp_path, extra=_creatable(tmp_path / "probe", MIXED_NAMES))
    document = h.manifest()

    preview = await h.service.preview(str(h.backup))

    assert preview.problems == ()
    assert (preview.files, preview.backup_skipped) == (
        len(document["files"]),
        len(document["skipped"]),
    )
    result = await h.apply()
    assert (result.verified, result.files) == (True, len(document["files"]))
    assert "aux.md" in {row["path"] for row in document["skipped"]}  # a refused name was present


async def test_an_unprotected_source_opens_read_only_never_ordinary(tmp_path: Path) -> None:
    h = await harness(tmp_path, protected=False)
    assert h.manifest()["source"]["policy"] == "existing_readonly"

    def claim_ordinary(document: dict[str, Any]) -> None:
        document["source"]["policy"] = "ordinary"

    h.edit(claim_ordinary)
    result = await h.apply()
    assert h.workspaces.get(result.workspace.workspace_id).policy == "existing_readonly"


def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def change(document: dict[str, Any]) -> None:
        target: Any = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


def _path(value: Any) -> Callable[[dict[str, Any]], None]:
    return _set(("files", 0, "path"), value)


def _add_file(path: str) -> Callable[[dict[str, Any]], None]:
    def change(document: dict[str, Any]) -> None:
        document["files"].append({"path": path, "bytes": 2, "sha256": sha("c\n")})
        document["totals"]["files"] += 1
        document["totals"]["bytes"] += 2

    return change


def _too_many(document: dict[str, Any]) -> None:
    rows = [
        {"path": f"n{index}.md", "bytes": 0, "sha256": sha("")}
        for index in range(MAX_BACKUP_FILES + 1)
    ]
    document["files"] = rows
    document["totals"].update(files=len(rows), bytes=0)


def _drop(key: str) -> Callable[[dict[str, Any]], None]:
    def change(document: dict[str, Any]) -> None:
        del document[key]

    return change


REFUSED: dict[str, Callable[[dict[str, Any]], None]] = {
    "unknown format": _set(("format",), "viki-backup-2"),
    "unknown version": _set(("version",), 2),
    "version as bool": _set(("version",), True),
    "version as text": _set(("version",), "1"),
    "scope all": _set(("scope",), "all"),
    "encrypted": _set(("encrypted",), True),
    "policy dropped": lambda document: document["source"].pop("policy"),
    "unknown policy": _set(("source", "policy"), "admin"),
    "absolute source path": _set(("source", "path"), "C:/vault"),
    "files dropped": _drop("files"),
    "totals dropped": _drop("totals"),
    "posix absolute": _path("/etc/a.md"),
    "drive absolute": _path("C:/a.md"),
    "backslash": _path("sub\\b.md"),
    "dot segment": _path("./a.md"),
    "empty segment": _path("sub//b.md"),
    "git at the root": _path(".git/config.md"),
    "git in any case": _path("sub/.GIT/x.md"),
    "dot directory": _path(".obsidian/x.md"),
    "claude home": _path(".claude/x.md"),
    "private name": _path(".env.md/x.md"),
    "stage name": _path(".viki-stage-x.md"),
    "not a note": _path("a.txt"),
    "device name": _path("CON/a.md"),
    "trailing dot alias": _path("sub./a.md"),
    "control code": _path("a\x01.md"),
    "path as list": _path(["a.md"]),
    "duplicate": _add_file("a.md"),
    "duplicate by case": _add_file("A.md"),
    "note and folder": _add_file("a.md/x.md"),
    "size over the note cap": _set(("files", 0, "bytes"), 2 * 1024 * 1024 + 1),
    "size as bool": _set(("files", 0, "bytes"), True),
    "negative size": _set(("files", 0, "bytes"), -1),
    "short hash": _set(("files", 0, "sha256"), "ab"),
    "upper-case hash": _set(("files", 0, "sha256"), sha("# A\n").upper()),
    "extra file key": _set(("files", 0, "mode"), 0o777),
    "file count over the cap": _too_many,
    "totals files": _set(("totals", "files"), 2),
    "totals bytes": _set(("totals", "bytes"), 1),
    "totals skipped": _set(("totals", "skipped"), 1),
    "totals as bool": _set(("totals", "skipped"), False),
    "skipped row shape": _set(("skipped",), [{"path": 1, "reason": "x"}]),
}


@pytest.mark.parametrize("change", list(REFUSED.values()), ids=list(REFUSED))
async def test_a_malformed_manifest_refuses_before_any_write(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    h = await harness(tmp_path)
    h.edit(change)
    registrations = len(h.workspaces.list())
    with pytest.raises(BackupRestoreError) as previewed:
        await h.service.preview(str(h.backup))
    assert previewed.value.code == "invalid_backup"
    assert await h.refused() == "invalid_backup"
    assert list(h.restores.iterdir()) == []
    assert len(h.workspaces.list()) == registrations


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"format": "viki-backup", "format": "viki-backup"}',
        b"\xef\xbb\xbf{}",
        b"[" * 100_000,
    ],
    ids=["text", "list", "duplicate key", "bom", "deep nesting"],
)
async def test_an_unparseable_manifest_is_invalid(tmp_path: Path, raw: bytes) -> None:
    h = await harness(tmp_path)
    (h.backup / MANIFEST_NAME).write_bytes(raw)
    assert await h.refused() == "invalid_backup"
    assert list(h.restores.iterdir()) == []


async def test_a_manifest_over_its_byte_bound_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await harness(tmp_path)
    monkeypatch.setattr(
        vault_restore, "MAX_MANIFEST_BYTES", (h.backup / MANIFEST_NAME).stat().st_size - 1
    )
    assert await h.refused() == "invalid_backup"


async def test_a_duplicate_key_cannot_hide_a_dropped_protection(tmp_path: Path) -> None:
    """Two `policy` keys would read as `protected` to one parser and `ordinary` to another."""
    h = await harness(tmp_path)
    text = (h.backup / MANIFEST_NAME).read_text(encoding="utf-8")
    forged = text.replace('"policy": "protected"', '"policy": "protected", "policy": "ordinary"')
    assert forged != text
    (h.backup / MANIFEST_NAME).write_text(forged, encoding="utf-8")
    assert await h.refused() == "invalid_backup"


async def test_preview_reports_the_source_and_every_problem_and_writes_nothing(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path, extra={"AGENTS.md": "# agents\n"})
    (h.backup / "sub" / "b.md").write_bytes(b"# changed\n")
    (h.backup / "sub" / "deep" / "c.markdown").unlink()
    folders = (h.tmp / "vaults", h.tmp / "backups", h.restores)
    everything = [snapshot(folder) for folder in folders]

    preview = await h.service.preview(str(h.backup))

    assert (preview.source_name, preview.source_policy, preview.protected) == (
        "Synthetic",
        "protected",
        True,
    )
    assert (preview.files, preview.bytes, preview.backup_skipped) == (
        4,
        sum(len(text.encode("utf-8")) for text in NOTES.values()) + len(b"# agents\n"),
        0,
    )
    assert preview.instruction_files == ("AGENTS.md",)
    assert preview.problems == (
        BackupRestoreProblem("sub/b.md", "mismatch"),
        BackupRestoreProblem("sub/deep/c.markdown", "missing"),
    )
    assert [snapshot(folder) for folder in folders] == everything


async def test_a_reparse_point_in_the_backup_is_not_followed(tmp_path: Path) -> None:
    """A linked folder holding the very bytes the manifest names is still not read."""
    h = await harness(tmp_path)
    elsewhere = h.tmp / "elsewhere"
    (elsewhere / "deep").mkdir(parents=True)
    (elsewhere / "b.md").write_bytes(NOTES["sub/b.md"].encode("utf-8"))
    (elsewhere / "deep" / "c.markdown").write_bytes(NOTES["sub/deep/c.markdown"].encode("utf-8"))
    for item in (h.backup / "sub" / "deep" / "c.markdown", h.backup / "sub" / "b.md"):
        item.unlink()
    (h.backup / "sub" / "deep").rmdir()
    (h.backup / "sub").rmdir()
    if sys.platform == "win32":
        make_junction(h.backup / "sub", elsewhere)
    else:
        os.symlink(elsewhere, h.backup / "sub", target_is_directory=True)

    result = await h.apply()

    assert result.verified is False
    assert {problem.reason for problem in result.problems} == {"reparse_point"}
    assert set(tree(h.restores / "Restored")) == {"a.md"}


async def test_a_copy_that_reads_back_wrong_is_never_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await harness(tmp_path)
    registrations = len(h.workspaces.list())
    monkeypatch.setattr(vault_restore, "_read_back", lambda path, size: b"other bytes")
    assert await h.refused() == "backup_restore_unverified"
    assert not (h.restores / "Restored").exists()
    assert len(h.workspaces.list()) == registrations


def live_operation_row(workspaces: WorkspaceService, workspace_id: str) -> None:
    """A stuck operation row in any vault: the app-wide registration freeze."""
    steps = [
        {
            "n": 0,
            "action": "remove",
            "ref": "n.md",
            "to": None,
            "pre": "a" * 64,
            "post": None,
            "preimage": None,
        }
    ]
    stamp = "2026-10-03T00:00:00.000Z"
    with workspaces._store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                workspace_id,
                "1:2",
                1,
                "delete",
                "b" * 64,
                json.dumps(steps, separators=(",", ":")),
                "recovery_required",
                stamp,
                stamp,
            ),
        )


async def test_a_restore_under_the_registration_freeze_creates_nothing(tmp_path: Path) -> None:
    """The freeze refuses before the `.partial` folder is made, not after the whole copy."""
    h = await harness(tmp_path)
    live_operation_row(h.workspaces, h.source_id)
    registrations = len(h.workspaces.list())

    assert await h.refused() == "recovery_required"
    assert list(h.restores.iterdir()) == []
    assert len(h.workspaces.list()) == registrations


async def test_a_registration_refused_by_the_store_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that appears after the freeze check still refuses the registration, typed."""
    h = await harness(tmp_path)

    def frozen(*_args: Any, **_kwargs: Any) -> Any:
        raise StateError("recovery_required")

    monkeypatch.setattr(h.workspaces, "register_existing", frozen)
    assert await h.refused() == "recovery_required"


async def test_a_backup_folder_in_the_deny_set_or_without_a_manifest_refuses(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    inside = h.tmp / "data" / "backup"
    inside.mkdir(parents=True)
    (inside / MANIFEST_NAME).write_bytes((h.backup / MANIFEST_NAME).read_bytes())
    for folder, code in (
        (inside, "denied_destination"),
        (h.vault, "denied_destination"),
        (h.restores, "invalid_backup"),
        (h.tmp / "absent", "invalid_path"),
        ("\\\\server\\share\\backup", "invalid_path"),
    ):
        with pytest.raises(BackupRestoreError) as failure:
            await h.service.preview(str(folder))
        assert failure.value.code == code, folder
