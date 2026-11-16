"""Real saves and startup recovery through `server.create_app`.

Every root is a synthetic scratch directory under `tmp_path`. A relaunch is a second
`create_app` over the same data directory, entered only after the first one has shut down, so
its lifespan runs the startup recovery the way a restart of the app would.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.domain import Workspace
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationLease,
    OrdinaryMutationRoot,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import PublicationJournal, PublicationRow
from viki_agent.workspaces import WorkspaceService

TOKEN = "vault-write-integration-token"
BASE = "http://127.0.0.1:8765"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": "http://127.0.0.1:8765"}
LEFTOVERS = (".viki-stage-", ".viki-backup-")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leftovers(vault: Path) -> list[Path]:
    return [path for path in vault.rglob("*") if path.name.startswith(LEFTOVERS)]


def tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root`: a file's bytes, or None for a directory."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


@dataclass
class Harness:
    client: TestClient
    spaces: WorkspaceService
    bootstrap: VaultBootstrap
    database: Path
    vaults: Path

    def rows(self) -> list[tuple[str, str]]:
        """The live journal as (operation_id, ref), read-only."""
        uri = f"{self.database.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            return connection.execute(
                "SELECT operation_id,ref FROM vault_publications ORDER BY operation_id"
            ).fetchall()

    def save(self, workspace: Workspace, ref: str, content: bytes, expected: str | None) -> Any:
        current = self.spaces.get(workspace.workspace_id)
        return self.client.put(
            f"/api/v2/vaults/{workspace.workspace_id}/notes/{ref}",
            headers=HEADERS,
            json={
                "policy_revision": current.policy_revision,
                "root_physical_id": current.root.physical_id,
                "content": content.decode("utf-8"),
                "expected_sha256": expected,
            },
        )

    def create_vault(self, name: str) -> Workspace:
        workspace, created, _ = self.bootstrap.create(
            str(uuid.uuid4()), name, str(self.vaults), None, "en", False
        )
        assert created and workspace.policy == "ordinary" and workspace.bootstrap == "complete"
        return workspace

    def plant(
        self,
        workspace: Workspace,
        ref: str,
        content: bytes,
        expected: str | None,
        *,
        prepared: bool = False,
    ) -> PublicationRow:
        """A save interrupted after its journal row and stage (or its `prepared` checkpoint):
        the state a crash leaves."""
        root = OrdinaryMutationRoot.from_admitted_workspace(self.spaces.get(workspace.workspace_id))
        journal = PublicationJournal(SessionStore(self.database, lambda: {}))
        native = VaultPublication()
        reservation = native.reserve()
        kind = "create" if expected is None else "replace"
        row = journal.reserve(root, ref, reservation, kind, sha(content), len(content), expected)

        async def interrupted() -> PublicationRow:
            async with MutationCoordinator().lease(root, (ref,)) as lease:
                staged = native.stage(lease, root, ref, reservation, content)
                stage_row = journal.record_stage(row.operation_id, staged.physical_id)
                if not prepared:
                    return stage_row
                assert expected is not None
                with native.prepare_replace(lease, staged, expected, staged.backup_name) as ready:
                    return journal.record_prepared(row.operation_id, ready.old)

        return asyncio.run(interrupted())


@contextmanager
def launch(tmp_path: Path) -> Iterator[Harness]:
    """One app launch over `tmp_path`'s data directory; the lifespan runs startup recovery."""
    data, home, cwd, vaults, resources = (
        tmp_path / name for name in ("data", "home", "cwd", "vaults", "resources")
    )
    for folder in (data, home, cwd, vaults, resources):
        folder.mkdir(parents=True, exist_ok=True)
    (resources / "welcome.en.md").write_text("# Welcome\n", encoding="utf-8")
    config = Config(vault=cwd, claude_home=home, cli=Path("fake"), edge=None, data_dir=data)
    state = AppState(data / "state.json", Prefs())
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=cwd, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("no vault write may start a provider"),
    )
    spaces = WorkspaceService(
        state.store,
        env={},
        data_dir=data,
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    bootstrap = VaultBootstrap(spaces, resources=resources, home=tmp_path / "profile")
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=Catalog(home, cwd),
        static_dir=None,
        workspaces=spaces,
        bootstrap=bootstrap,
    )
    with TestClient(app, base_url=BASE) as client:
        yield Harness(client, spaces, bootstrap, state.database_path, vaults)


def test_create_then_replace_and_a_stale_hash_is_a_conflict(tmp_path: Path) -> None:
    with launch(tmp_path) as app:
        workspace = app.create_vault("edits")
        note = Path(workspace.root.display) / "note.md"

        created = app.save(workspace, "note.md", b"first\n", None)
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "saved"
        assert note.read_bytes() == b"first\n"

        replaced = app.save(workspace, "note.md", b"second\n", sha(b"first\n"))
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["status"] == "saved"
        assert note.read_bytes() == b"second\n"

        stale = app.save(workspace, "note.md", b"third\n", sha(b"first\n"))
        assert stale.status_code == 409, stale.text
        assert stale.json()["status"] == "conflict"
        assert note.read_bytes() == b"second\n"


def test_a_conflict_is_discarded_and_a_relaunch_publishes_nothing(tmp_path: Path) -> None:
    """A refused draft leaves no row or stage, so no restart can publish it."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("discard")
        vault = Path(workspace.root.display)
        note = vault / "note.md"
        note.write_bytes(b"edited elsewhere\n")

        refused = app.save(workspace, "note.md", b"abandoned draft\n", sha(b"original\n"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["status"] == "conflict"
        assert app.rows() == []
        assert leftovers(vault) == []
    # The user restores the bytes the draft expected: a kept stage would now publish.
    note.write_bytes(b"original\n")

    with launch(tmp_path) as app:
        assert note.read_bytes() == b"original\n"
        assert app.rows() == []
        assert leftovers(vault) == []
        retry = app.save(workspace, "note.md", b"deliberate retry\n", sha(b"original\n"))
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "saved"
        assert note.read_bytes() == b"deliberate retry\n"


def short_name(path: Path) -> str:
    """The 8.3 name Windows gave the last component of `path`, or the long name if it gave none."""
    assert sys.platform == "win32"
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    length = get_short(str(path), buffer, len(buffer))
    assert 0 < length < len(buffer), ctypes.get_last_error()
    return Path(buffer.value).name


@pytest.mark.skipif(
    sys.platform != "win32", reason="8.3 short names are a Windows-only filesystem feature"
)
def test_a_short_name_leaf_is_refused_and_a_relaunch_publishes_nothing(tmp_path: Path) -> None:
    """The alias is refused after its stage exists; nothing stays behind."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("alias")
        vault = Path(workspace.root.display)
        (vault / "notes").mkdir()
        note = vault / "notes" / "a long note name.md"
        note.write_bytes(b"old\n")
        alias = short_name(note)
        if alias == note.name:
            pytest.skip("this volume makes no 8.3 short names, so the note has no alias to send")

        refused = app.save(workspace, f"notes/{alias}", b"draft\n", sha(b"old\n"))
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == "excluded_path"
        assert app.rows() == []
        assert leftovers(vault) == []

    with launch(tmp_path) as app:
        assert note.read_bytes() == b"old\n"
        assert app.rows() == []
        assert leftovers(vault) == []


@pytest.mark.parametrize("kind", ["create", "replace"])
def test_the_lifespan_completes_an_interrupted_save(tmp_path: Path, kind: str) -> None:
    with launch(tmp_path) as app:
        workspace = app.create_vault("interrupted")
        vault = Path(workspace.root.display)
        note = vault / "note.md"
        expected = None
        if kind == "replace":
            note.write_bytes(b"old\n")
            expected = sha(b"old\n")
        row = app.plant(workspace, "note.md", b"new\n", expected)
        assert row.state == "staged"
        assert app.rows() == [(row.operation_id, "note.md")]
        assert (vault / row.stage_name).read_bytes() == b"new\n"

    with launch(tmp_path) as app:
        assert note.read_bytes() == b"new\n"
        assert app.rows() == []
        assert leftovers(vault) == []


def test_a_blocked_row_still_blocks_its_ref_after_a_restart(tmp_path: Path) -> None:
    """A replace interrupted at `prepared` whose note then changed elsewhere: recovery can
    neither finish nor undo it without guessing, so it blocks, and the ref stays held."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("blocked")
        vault = Path(workspace.root.display)
        (vault / "Notes").mkdir()
        note = vault / "Notes" / "Blocked.md"
        note.write_bytes(b"old\n")
        row = app.plant(
            workspace, "Notes/Blocked.md", b"interrupted\n", sha(b"old\n"), prepared=True
        )
        assert row.state == "prepared"
    note.write_bytes(b"edited elsewhere\n")
    before = tree(vault)

    with launch(tmp_path) as app:
        assert app.rows() == [(row.operation_id, "Notes/Blocked.md")]
        assert tree(vault) == before
        for ref in ("Notes/Blocked.md", "notes/BLOCKED.md"):
            refused = app.save(workspace, ref, b"later\n", sha(b"edited elsewhere\n"))
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "publication_busy"
        assert app.rows() == [(row.operation_id, "Notes/Blocked.md")]
        assert tree(vault) == before


def test_a_legacy_refused_ref_blocks_alone_and_the_rest_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row recorded before its ref joined the excluded set blocks on its own; the other rows
    still recover."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("legacy")
        vault = Path(workspace.root.display)
        ordinary = app.plant(workspace, "note.md", b"new\n", None)
        legacy = VaultPublication().reserve()
        hidden = vault / "sub" / ".git"
        hidden.mkdir(parents=True)
        (hidden / "x.md").write_bytes(b"legacy target\n")
        stage = hidden / legacy.stage_name
        stage.write_bytes(b"legacy draft\n")
        info = stage.stat()
        recorded = "2026-01-01T00:00:00.000Z"
        with closing(sqlite3.connect(app.database)) as connection, connection:
            connection.execute(
                "INSERT INTO vault_publications (operation_id,workspace_id,root_physical_id,"
                "policy_revision,ref,kind,stage_name,backup_name,stage_physical_id,stage_sha256,"
                "stage_bytes,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    legacy.operation_id,
                    workspace.workspace_id,
                    workspace.root.physical_id,
                    workspace.policy_revision,
                    "sub/.git/x.md",
                    "create",
                    legacy.stage_name,
                    legacy.backup_name,
                    f"{info.st_dev}:{info.st_ino}",
                    sha(b"legacy draft\n"),
                    len(b"legacy draft\n"),
                    "staged",
                    recorded,
                    recorded,
                ),
            )
        before = tree(vault / "sub")

    with (
        caplog.at_level("WARNING", logger="viki_agent.vaults.vault_publication"),
        launch(tmp_path) as app,
    ):
        assert (vault / "note.md").read_bytes() == b"new\n"
        assert not (vault / ordinary.stage_name).exists()
        assert app.rows() == [(legacy.operation_id, "sub/.git/x.md")]
        assert tree(vault / "sub") == before
        status = app.client.get("/api/v2/vault-publications/recovery", headers=HEADERS)
        assert status.status_code == 200, status.text
        assert status.json() == {
            "publications": [
                {
                    "operation_id": legacy.operation_id,
                    "workspace_id": workspace.workspace_id,
                    "ref": "sub/.git/x.md",
                    "state": "staged",
                    "reason": "recovery_required",
                    "resolvable": ["forget"],
                }
            ]
        }
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "viki_agent.vaults.vault_publication"
    ] == [f"vault publication {legacy.operation_id} blocked: invalid_ref"]


@pytest.mark.skipif(
    sys.platform != "win32", reason="sharing-mode holds (stage_busy) are Windows-only"
)
def test_a_create_whose_stage_was_held_publishes_nothing_after_a_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create refused before its rename leaves no row or stage."""
    assert sys.platform == "win32"
    real_publish = VaultPublication.publish_create

    def held_while_publishing(
        self: VaultPublication, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        # A scanner holds the fresh stage while the create opens it, then lets go.
        held = vault_mutation._open_file(
            staged.root.path / staged.stage_name, vault_mutation._GENERIC_READ, share=0
        )
        try:
            return real_publish(self, lease, staged)
        finally:
            vault_mutation._CloseHandle(held)

    with launch(tmp_path) as app:
        workspace = app.create_vault("held")
        vault = Path(workspace.root.display)
        with monkeypatch.context() as patched:
            patched.setattr(VaultPublication, "publish_create", held_while_publishing)
            refused = app.save(workspace, "idea.md", b"draft\n", None)
        assert refused.status_code == 423, refused.text
        assert refused.json()["error"] == {
            "code": "stage_busy",
            "message": "The note could not be saved.",
        }
        assert app.rows() == []
        assert leftovers(vault) == []

    with launch(tmp_path) as app:
        assert not (vault / "idea.md").exists()
        assert app.rows() == []
        assert leftovers(vault) == []


def test_a_folder_deleted_after_a_crash_clears_at_the_next_launch(tmp_path: Path) -> None:
    """The deleted folder held the stage, so the row goes and registration unfreezes."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("gone")
        vault = Path(workspace.root.display)
        (vault / "sub").mkdir()
        (vault / "sub" / "note.md").write_bytes(b"old\n")
        replace = app.plant(workspace, "sub/note.md", b"new\n", sha(b"old\n"))
        create = app.plant(workspace, "sub/idea.md", b"idea\n", None)
        assert sorted(app.rows()) == sorted(
            [(replace.operation_id, "sub/note.md"), (create.operation_id, "sub/idea.md")]
        )
    shutil.rmtree(vault / "sub")

    with launch(tmp_path) as app:
        assert app.rows() == []
        assert not (vault / "sub").exists()
        app.create_vault("after")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="a folder at the target's name raises a raw, untyped OSError only on Windows",
)
def test_a_raw_oserror_before_prepared_answers_503_without_an_id(tmp_path: Path) -> None:
    """The discard leaves nothing pending, so the answer names no operation."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("folder")
        vault = Path(workspace.root.display)
        (vault / "note.md").mkdir()
        refused = app.save(workspace, "note.md", b"draft\n", sha(b"old\n"))
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"] == {
            "code": "unavailable",
            "message": "The note could not be saved.",
        }
        assert app.rows() == []
        assert leftovers(vault) == []
        assert (vault / "note.md").is_dir()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows answers unavailable for this case; see the test above"
)
def test_a_folder_at_the_targets_name_refuses_unsafe_target(tmp_path: Path) -> None:
    """POSIX twin: `_open_target` answers the typed `unsafe_target` refusal for a directory at
    the target's name, instead of the raw OSError Windows raises there."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("folder")
        vault = Path(workspace.root.display)
        (vault / "note.md").mkdir()
        refused = app.save(workspace, "note.md", b"draft\n", sha(b"old\n"))
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"] == {
            "code": "unsafe_target",
            "message": "The note could not be saved.",
        }
        assert app.rows() == []
        assert leftovers(vault) == []
        assert (vault / "note.md").is_dir()


def test_a_busy_prepared_checkpoint_is_retryable_and_a_relaunch_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store `busy` at `record_prepared` did not commit, so the save is refused with no
    operation ID, and nothing of it is left for a restart to publish."""

    def busy(*_args: object) -> None:
        raise StateError("busy")

    with launch(tmp_path) as app:
        workspace = app.create_vault("busy")
        vault = Path(workspace.root.display)
        note = vault / "note.md"
        note.write_bytes(b"old\n")
        with monkeypatch.context() as patched:
            patched.setattr(PublicationJournal, "record_prepared", busy)
            refused = app.save(workspace, "note.md", b"abandoned\n", sha(b"old\n"))
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"] == {
            "code": "journal_unavailable",
            "message": "The note could not be saved.",
        }
        assert app.rows() == []
        assert leftovers(vault) == []

    with launch(tmp_path) as app:
        assert note.read_bytes() == b"old\n"
        assert app.rows() == []
        assert leftovers(vault) == []
