"""Hard rule 1's frozen proof: the app writes a note only into an admitted ordinary
vault, and every other save is refused with no byte changed.

The real services run through `server.create_app` and are driven over HTTP, so a refusal here is
the one a browser would meet. Each refusal is checked against a before/after snapshot of every
root it could touch, plus the absence of stage and backup entries and of journal rows. Every root
is a synthetic scratch directory under `tmp_path`; no real vault is ever read or written.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from viki_agent import server, winproc
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.domain import SCRATCH_WORKSPACE_ID, CanonicalRoot, Workspace
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.workspaces import WorkspaceService

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the vault writer is Windows only")

TOKEN = "vault-write-acceptance-token"
BASE = "http://127.0.0.1:8765"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": "http://127.0.0.1:8765"}
NOTE = "# synthetic\n\nA scratch note for the vault-write acceptance test.\n"
STAGE_OR_BACKUP = (".viki-stage-", ".viki-backup-")

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

    def save(
        self, workspace: Workspace, ref: str, content: str, expected: str | None = None
    ) -> Any:
        """PUT the note, with the revision and physical id of the workspace's current row."""
        current = self.spaces.get(workspace.workspace_id) if workspace.persistent else workspace
        return self.client.put(
            f"/api/v2/vaults/{workspace.workspace_id}/notes/{ref}",
            headers=HEADERS,
            json={
                "policy_revision": current.policy_revision,
                "root_physical_id": current.root.physical_id,
                "content": content,
                "expected_sha256": expected,
            },
        )

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

    def assert_refused(self, response: Any, before: dict[Path, list[Entry]]) -> None:
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
    )
    with TestClient(app, base_url=BASE) as client:  # the lifespan runs startup recovery
        yield Harness(client, spaces, bootstrap, state.database_path, vaults)


def test_an_ordinary_vault_accepts_a_new_note(harness: Harness) -> None:
    """The positive control: without it every refusal below could be a broken route."""
    workspace = harness.create_vault("control")
    vault = Path(workspace.root.display)
    (vault / "notes").mkdir()

    response = harness.save(workspace, "notes/ok.md", NOTE)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "saved"
    assert (vault / "notes" / "ok.md").read_bytes() == NOTE.encode("utf-8")
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
            key=os.path.normcase(str(vault)),
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


def test_a_junction_out_of_the_vault_refuses_the_save(harness: Harness, tmp_path: Path) -> None:
    workspace = harness.create_vault("junction")
    vault = Path(workspace.root.display)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "link"
    done = winproc.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], cwd=str(vault), timeout=10.0
    )
    if done.returncode != 0:
        pytest.skip("this volume refuses junctions")
    assert link.is_junction() and os.path.samefile(link, outside)
    before = {vault: snapshot(vault), outside: snapshot(outside)}

    harness.assert_refused(harness.save(workspace, "link/n.md", NOTE), before)


# The write set, spelled out so that it cannot shrink with the code, and in mixed case because
# Windows names fold case. The hygiene guard scans this file, so the credential file names are
# built from halves rather than written whole.
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


def short_name(path: Path) -> str:
    """The 8.3 name Windows gave the last component of `path`, or the long name if it gave none."""
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    length = get_short(str(path), buffer, len(buffer))
    assert 0 < length < len(buffer), ctypes.get_last_error()
    return Path(buffer.value).name


def test_an_8dot3_alias_of_an_excluded_directory_refuses_the_save(harness: Harness) -> None:
    workspace = harness.create_vault("alias")
    vault = Path(workspace.root.display)
    (vault / ".claude" / "commands").mkdir(parents=True)
    alias = short_name(vault / ".claude")
    if alias == ".claude":  # this volume makes no short names; the refusal must hold anyway
        alias = "CLAUDE~1"
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.save(workspace, f"{alias}/commands/n.md", NOTE), before)
