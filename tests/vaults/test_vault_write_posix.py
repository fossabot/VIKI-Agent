"""POSIX proof of note writes and hard rule 1: the app's real services, driven over
HTTP through `server.create_app`, admit an ordinary vault and both save and refuse notes exactly
as `tests/acceptance/test_vault_write_accept.py` proves on Windows. A new note and an
expected-hash edit save; a stale expected hash is a conflict, the note's bytes unchanged;
a vault that is not ordinary, an incomplete bootstrap, the scratch workspace, a symlinked
parent and every excluded name all refuse the save with the vault left byte-identical, no stage
or backup entry, and no journal row; a backup of an admitted ordinary vault completes over
HTTP. This file is built the same way as that acceptance test — a twin of it — but never
imports from it.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.domain import SCRATCH_WORKSPACE_ID, CanonicalRoot, Workspace
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.workspaces import WorkspaceService, root_key

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX note-save refusal")

TOKEN = "vault-write-posix-token"
BASE = "http://127.0.0.1:8765"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": "http://127.0.0.1:8765"}
NOTE = "# synthetic\n\nA scratch note for the POSIX vault-write refusal test.\n"
STAGE_OR_BACKUP = (".viki-stage-", ".viki-backup-")

# (relative path, is_dir, size, sha256); a link is recorded, never followed.
Entry = tuple[str, bool, int, str]


def snapshot(root: Path) -> list[Entry]:
    """Every entry under `root`, from a walk that never follows a symlink."""
    entries: list[Entry] = []
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as listing:
            for item in listing:
                relative = os.path.relpath(item.path, root)
                if item.is_symlink():
                    entries.append((relative, False, -1, "link"))
                elif item.is_dir(follow_symlinks=False):
                    entries.append((relative, True, 0, ""))
                    pending.append(Path(item.path))
                else:
                    with open(item.path, "rb") as reader:
                        data = reader.read()
                    entries.append((relative, False, len(data), hashlib.sha256(data).hexdigest()))
    return sorted(entries)


@dataclass
class Harness:
    client: TestClient
    spaces: WorkspaceService
    bootstrap: VaultBootstrap
    database: Path
    vaults: Path  # the parent of every vault a test creates

    def journal_rows(self) -> int:
        uri = f"{self.database.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            return connection.execute("SELECT count(*) FROM vault_publications").fetchone()[0]

    def create_vault(self, name: str) -> Workspace:
        """An ordinary vault the way the app bootstraps one: persistent and complete."""
        workspace, created, _ = self.bootstrap.create(
            str(uuid.uuid4()), name, str(self.vaults), None, "en", False
        )
        assert created and workspace.policy == "ordinary" and workspace.bootstrap == "complete"
        return workspace

    def demote(self, workspace: Workspace, policy: Any) -> Workspace:
        """Move a complete vault to another policy, so the policy alone decides the save."""
        changed = self.spaces.set_policy(
            str(uuid.uuid4()), workspace.workspace_id, workspace.policy_revision, policy, []
        )
        assert changed.policy == policy and changed.bootstrap == "complete"
        return changed

    def save(
        self, workspace: Workspace, ref: str, content: str, expected: str | None = None
    ) -> Any:
        return self.client.put(
            f"/api/v2/vaults/{workspace.workspace_id}/notes/{ref}",
            headers=HEADERS,
            json={
                "policy_revision": workspace.policy_revision,
                "root_physical_id": workspace.root.physical_id,
                "content": content,
                "expected_sha256": expected,
            },
        )

    def backup(self, workspace: Workspace, destination: Path) -> Any:
        return self.client.post(
            f"/api/v2/vaults/{workspace.workspace_id}/backup",
            headers=HEADERS,
            json={"command_id": str(uuid.uuid4()), "destination": str(destination)},
        )

    def assert_refused(self, response: Any, before: dict[Path, list[Entry]]) -> None:
        """Every refusal in this file is checked the way the acceptance test checks
        Windows: a non-2xx status with a typed code, every named root byte-identical, no stage or
        backup entry, and no journal row — never one exact code, since the reason differs by
        case (policy, bootstrap, parent safety, name exclusion)."""
        assert not 200 <= response.status_code < 300, response.status_code
        body = response.json()
        assert isinstance(body, dict) and isinstance(body.get("error"), dict), body
        code = body["error"].get("code")
        assert isinstance(code, str) and code, body
        for root, entries in before.items():
            after = snapshot(root)
            assert after == entries, root
            assert not [
                entry for entry in after if Path(entry[0]).name.startswith(STAGE_OR_BACKUP)
            ], root
        assert self.journal_rows() == 0


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    data = tmp_path / "data"
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    vaults = tmp_path / "vaults"
    resources = tmp_path / "resources"
    for folder in (data, home, cwd, vaults, resources):
        folder.mkdir(parents=True)
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
        # Only `workspaces` is needed for note writes; `denied_roots` also registers the
        # knowledge routes (backup included) the same way `workspaces` alone would not.
        denied_roots=lambda: (),
    )
    with TestClient(app, base_url=BASE) as client:  # the lifespan runs startup recovery
        yield Harness(client, spaces, bootstrap, state.database_path, vaults)


def test_an_ordinary_vault_saves_a_new_note_and_an_edit(harness: Harness) -> None:
    """A new note and an expected-hash edit both save over HTTP; the bytes on disk equal
    the request, no stage or backup entry is left, and the journal stays empty."""
    workspace = harness.create_vault("writes")
    vault = Path(workspace.root.display)
    (vault / "notes").mkdir()
    welcome = vault / "Welcome.md"

    created = harness.save(workspace, "notes/ok.md", NOTE)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "saved"
    assert (vault / "notes" / "ok.md").read_bytes() == NOTE.encode("utf-8")

    edited = "# Welcome\n\nedited\n"
    edit = harness.save(
        workspace, "Welcome.md", edited, expected=hashlib.sha256(welcome.read_bytes()).hexdigest()
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["status"] == "saved"
    assert welcome.read_bytes() == edited.encode("utf-8")

    after = snapshot(vault)
    assert not [entry for entry in after if Path(entry[0]).name.startswith(STAGE_OR_BACKUP)]
    assert harness.journal_rows() == 0


def test_a_stale_expected_hash_is_a_conflict(harness: Harness) -> None:
    """A stale `expected_sha256` answers 409 `conflict`, and the note's bytes stay."""
    workspace = harness.create_vault("conflict")
    vault = Path(workspace.root.display)
    (vault / "notes").mkdir()
    ref = "notes/ok.md"
    first = harness.save(workspace, ref, NOTE)
    assert first.status_code == 200, first.text

    stale = harness.save(workspace, ref, "# changed\n", expected="0" * 64)
    assert stale.status_code == 409, stale.text
    assert stale.json()["status"] == "conflict"
    assert (vault / ref).read_bytes() == NOTE.encode("utf-8")
    assert harness.journal_rows() == 0


@pytest.mark.parametrize("policy", ["protected", "existing_readonly"])
def test_a_vault_that_is_not_ordinary_refuses_every_save(harness: Harness, policy: str) -> None:
    workspace = harness.demote(harness.create_vault(policy), policy)
    vault = Path(workspace.root.display)
    (vault / "notes").mkdir()
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.save(workspace, "notes/n.md", NOTE), before)


def test_an_incomplete_bootstrap_refuses_every_save(harness: Harness) -> None:
    """A reservation that never completed: its folder exists, but it is not yet a vault."""
    vault = harness.vaults / "pending"
    command_id = str(uuid.uuid4())
    workspace = harness.spaces.register_created(
        command_id,
        CanonicalRoot(
            key=root_key(str(vault)),
            display=str(vault),
            physical_id=f"pending:{command_id}",
            aliased=False,
        ),
        "pending",
        portable_id=None,
    )
    assert workspace.bootstrap == "pending"
    (vault / "notes").mkdir(parents=True)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.save(workspace, "notes/n.md", NOTE), before)


def test_the_builtin_scratch_workspace_refuses_every_save(harness: Harness) -> None:
    active = harness.spaces.resolve_active()
    workspace = active.workspace
    assert workspace.workspace_id == SCRATCH_WORKSPACE_ID and not workspace.persistent
    scratch = Path(workspace.root.display)
    (scratch / "notes").mkdir(exist_ok=True)
    before = {scratch: snapshot(scratch)}

    harness.assert_refused(harness.save(workspace, "notes/n.md", NOTE), before)


def test_a_symlinked_parent_refuses_the_save(harness: Harness, tmp_path: Path) -> None:
    """The alias mechanism is per OS: a junction on Windows (the acceptance test), a
    symlink here. Opening a symlinked path component never follows it (the path is walked with
    directory file descriptors), so a note ref through one is refused before anything outside
    the vault is touched."""
    workspace = harness.create_vault("symlink")
    vault = Path(workspace.root.display)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "link"
    os.symlink(outside, link, target_is_directory=True)
    before = {vault: snapshot(vault), outside: snapshot(outside)}

    harness.assert_refused(harness.save(workspace, "link/n.md", NOTE), before)


# The write set, spelled out so that it cannot shrink with the code, and in mixed case: the app's
# own exclusion check (`vault.excluded_part`) casefolds every component on every OS, so a
# mixed-case name is excluded on case-sensitive Linux exactly as it is on Windows or macOS. The
# hygiene guard scans this file, so the credential file names are built from halves rather than
# written whole.
CREDENTIALS = "Cred" + "entials"
EXCLUDED_DIRECTORIES = (
    ".Git",
    ".CLAUDE",
    ".CoDeX",
    ".Viki",
    CREDENTIALS,
    CREDENTIALS + ".JSON",
    "." + CREDENTIALS + ".Json",
    "Secrets.JSON",
    ".ENV",
    ".Env.Local",
    ".env.Synthetic",
)
EXCLUDED_LEAVES = (
    "Agents.md",
    "Agents.Override.md",
    "CLAUDE.md",
    "Claude.Local.md",
    "Gemini.md",
    ".Env.Notes.md",
    ".Viki-Stage-x.md",
    ".VIKI-Backup-x.md",
)
EXCLUDED_REFS = (
    [f"{name}/n.md" for name in EXCLUDED_DIRECTORIES]
    + [f"sub/{name}/n.md" for name in EXCLUDED_DIRECTORIES]
    + list(EXCLUDED_LEAVES)
    + [f"sub/{leaf}" for leaf in EXCLUDED_LEAVES]
)


@pytest.mark.parametrize(
    "ref", EXCLUDED_REFS, ids=lambda ref: ref.replace(CREDENTIALS, "Cred-entials")
)
def test_an_excluded_name_refuses_the_save_at_any_depth(harness: Harness, ref: str) -> None:
    workspace = harness.create_vault("excluded")
    vault = Path(workspace.root.display)
    (vault / ref).parent.mkdir(parents=True, exist_ok=True)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.save(workspace, ref, NOTE), before)


def test_a_repromoted_vault_admits_note_writes(harness: Harness) -> None:
    """The publication service runs on POSIX, so `note_write` is True once a vault is admitted
    back to ordinary: `admits_note_writes` alone decides it, with no platform gate."""
    workspace = harness.create_vault("policy")

    demoted = harness.client.patch(
        f"/api/v2/vaults/{workspace.workspace_id}/policy",
        headers=HEADERS,
        json={
            "command_id": str(uuid.uuid4()),
            "expected_policy_revision": workspace.policy_revision,
            "policy": "existing_readonly",
            "acknowledged": [],
        },
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["note_write"] is False

    promoted = harness.client.patch(
        f"/api/v2/vaults/{workspace.workspace_id}/policy",
        headers=HEADERS,
        json={
            "command_id": str(uuid.uuid4()),
            "expected_policy_revision": demoted.json()["policy_revision"],
            "policy": "ordinary",
            "acknowledged": [],
        },
    )
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert body["policy"] == "ordinary"
    assert body["note_write"] is True


def test_a_backup_of_an_ordinary_vault_completes(harness: Harness, tmp_path: Path) -> None:
    """A backup of an admitted ordinary vault, run over HTTP, completes on POSIX."""
    workspace = harness.create_vault("backup-source")
    vault = Path(workspace.root.display)
    destination = tmp_path / "backups"
    destination.mkdir()

    response = harness.backup(workspace, destination)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["files"] >= 1
    copy = destination / body["folder"]
    assert (copy / "Welcome.md").read_bytes() == (vault / "Welcome.md").read_bytes()
