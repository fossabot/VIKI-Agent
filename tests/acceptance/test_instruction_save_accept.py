"""Hard rule 1's one instruction-file exception, proved over HTTP: `PUT
/api/v2/vaults/{workspace_id}/instructions/{name}` replaces an existing root `AGENTS.md` or
`CLAUDE.md` of an admitted ordinary vault, and every other request leaves every root
byte-identical.

The real services run through `server.create_app` and are driven over HTTP, so a refusal here is
the one a browser would meet. Each refusal is checked against a before/after snapshot of every
root it could touch, plus the absence of stage and backup entries and of journal rows. Every root
is a synthetic scratch directory under `tmp_path`; no real vault is ever read or written. This
file is built the way `test_vault_write_accept.py` is, but imports nothing from it.
"""

from __future__ import annotations

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

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.domain import SCRATCH_WORKSPACE_ID, CanonicalRoot, Workspace
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.workspaces import WorkspaceService, root_key

TOKEN = "instruction-save-acceptance-token"
BASE = "http://127.0.0.1:8765"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": "http://127.0.0.1:8765"}
STAGE_OR_BACKUP = (".viki-stage-", ".viki-backup-")
INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md")
OLD = "﻿# synthetic\r\n".encode()
NEW = "﻿# changed\r\n"

# (relative path, is_dir, size, sha256); a link is recorded, never followed.
Entry = tuple[str, bool, int, str]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
                    entries.append((relative, False, len(data), sha(data)))
    return sorted(entries)


def put_file(root: Path, ref: str, data: bytes = OLD) -> Path:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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

    def put(
        self, workspace: Workspace, route: str, ref: str, content: str, expected: str | None
    ) -> Any:
        """PUT with the revision and physical id of the workspace's current row."""
        current = (
            self.spaces.get(workspace.workspace_id, kind=None)
            if workspace.persistent
            else workspace
        )
        return self.client.put(
            f"/api/v2/vaults/{workspace.workspace_id}/{route}/{ref}",
            headers=HEADERS,
            json={
                "policy_revision": current.policy_revision,
                "root_physical_id": current.root.physical_id,
                "content": content,
                "expected_sha256": expected,
            },
        )

    def instruction(
        self, workspace: Workspace, name: str, content: str, expected: str | None
    ) -> Any:
        return self.put(workspace, "instructions", name, content, expected)

    def note(self, workspace: Workspace, ref: str, content: str, expected: str | None) -> Any:
        return self.put(workspace, "notes", ref, content, expected)

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

    def trusted_code_folder(self, root: Path) -> Workspace:
        """Register `root` as code and trust it with every acknowledgement it asks for."""
        workspace, _ = self.spaces.register_existing(str(uuid.uuid4()), str(root), kind="code")
        required = self.spaces.inspect_policy(workspace.workspace_id).acknowledgements_required
        return self.spaces.set_policy(
            str(uuid.uuid4()),
            workspace.workspace_id,
            workspace.policy_revision,
            "ordinary",
            required,
        )

    def assert_refused(self, response: Any, before: dict[Path, list[Entry]]) -> None:
        assert not 200 <= response.status_code < 300, response.status_code
        body = response.json()
        assert isinstance(body, dict) and isinstance(body.get("error"), dict), body
        code = body["error"].get("code")
        assert isinstance(code, str) and code, body
        self.assert_untouched(before)

    def assert_untouched(self, before: dict[Path, list[Entry]]) -> None:
        for root, entries in before.items():
            after = snapshot(root)
            assert after == entries, root  # no change, and so no new name
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


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_an_ordinary_vault_replaces_its_existing_instruction_file(
    harness: Harness, name: str
) -> None:
    """The positive control: without it every refusal below could be a broken route."""
    workspace = harness.create_vault("control")
    vault = Path(workspace.root.display)
    put_file(vault, name)
    before = snapshot(vault)

    response = harness.instruction(workspace, name, NEW, sha(OLD))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "saved"
    assert body["sha256"] == sha(NEW.encode())
    assert (vault / name).read_bytes() == NEW.encode()
    only_that_entry = [
        (name, False, len(NEW.encode()), sha(NEW.encode())) if entry[0] == name else entry
        for entry in before
    ]
    assert snapshot(vault) == only_that_entry
    assert harness.journal_rows() == 0


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_missing_instruction_file_is_never_created(harness: Harness, name: str) -> None:
    workspace = harness.create_vault("missing")
    vault = Path(workspace.root.display)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)

    assert not (vault / name).exists()


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_null_expected_hash_is_refused_with_the_file_present(harness: Harness, name: str) -> None:
    workspace = harness.create_vault("null-hash")
    vault = Path(workspace.root.display)
    put_file(vault, name)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, None), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_stale_expected_hash_is_a_conflict(harness: Harness, name: str) -> None:
    workspace = harness.create_vault("conflict")
    vault = Path(workspace.root.display)
    put_file(vault, name)
    before = snapshot(vault)

    response = harness.instruction(workspace, name, NEW, "0" * 64)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == "conflict" and body["sha256"] is None
    assert (vault / name).read_bytes() == OLD
    assert snapshot(vault) == before  # bytes unchanged, no stage or backup entry
    assert harness.journal_rows() == 0


def test_a_nested_name_matches_no_route(harness: Harness) -> None:
    """`sub/AGENTS.md` is two path parts, so the route does not match it: a plain 404."""
    workspace = harness.create_vault("nested")
    vault = Path(workspace.root.display)
    put_file(vault, "sub/AGENTS.md")
    before = {vault: snapshot(vault)}

    response = harness.instruction(workspace, "sub/AGENTS.md", NEW, sha(OLD))

    assert response.status_code == 404, response.status_code
    harness.assert_untouched(before)


@pytest.mark.parametrize(
    "ref",
    [
        "agents.md",
        "Agents.md",
        "CLAUDE.MD",
        "AGENTS.override.md",
        "CLAUDE.local.md",
        "GEMINI.md",
        "ＡGENTS.md",
        "AGENTS.md.",
        "note.md",
    ],
)
def test_any_other_name_is_refused_with_that_file_present(harness: Harness, ref: str) -> None:
    workspace = harness.create_vault("names")
    vault = Path(workspace.root.display)
    put_file(vault, ref)  # on a case-insensitive volume this may be the root instruction file
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, ref, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_file_whose_name_on_disk_differs_in_case_is_refused(harness: Harness, name: str) -> None:
    """On a case-insensitive volume `Agents.md` opens as `AGENTS.md`; the save must still stop
    rather than respell the file. On a case-sensitive volume the exact file is simply absent."""
    workspace = harness.create_vault("on-disk-case")
    vault = Path(workspace.root.display)
    put_file(vault, name.capitalize())
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)

    assert name.capitalize() in os.listdir(vault)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_hard_linked_instruction_file_is_refused(harness: Harness, name: str) -> None:
    workspace = harness.create_vault("hard-link")
    vault = Path(workspace.root.display)
    put_file(vault, name)
    os.link(vault / name, vault / "other-name.md")
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_symlinked_instruction_file_is_refused(
    harness: Harness, tmp_path: Path, name: str
) -> None:
    workspace = harness.create_vault("symlink")
    vault = Path(workspace.root.display)
    outside = put_file(tmp_path / "outside", "target.md")
    try:
        os.symlink(outside, vault / name)
    except OSError, NotImplementedError:
        pytest.skip("this account or volume cannot create symlinks")
    before = {vault: snapshot(vault), outside.parent: snapshot(outside.parent)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("policy", ["protected", "existing_readonly"])
@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_vault_that_is_not_ordinary_refuses_the_save(
    harness: Harness, policy: str, name: str
) -> None:
    workspace = harness.create_vault(policy)
    vault = Path(workspace.root.display)
    put_file(vault, name)
    workspace = harness.demote(workspace, policy)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_an_incomplete_bootstrap_refuses_the_save(harness: Harness, name: str) -> None:
    """A reservation that never completed: its folder exists, but it is not yet a vault."""
    vault = harness.vaults / "pending"
    command_id = str(uuid.uuid4())
    workspace = harness.spaces.register_created(
        command_id,
        CanonicalRoot(
            key=root_key(vault),
            display=str(vault),
            physical_id=f"pending:{command_id}",
            aliased=False,
        ),
        "pending",
        portable_id=None,
    )
    assert workspace.bootstrap == "pending"
    put_file(vault, name)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_the_builtin_scratch_workspace_refuses_the_save(harness: Harness, name: str) -> None:
    workspace = harness.spaces.resolve_active().workspace
    assert workspace.workspace_id == SCRATCH_WORKSPACE_ID and not workspace.persistent
    scratch = Path(workspace.root.display)
    put_file(scratch, name)
    before = {scratch: snapshot(scratch)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_a_trusted_code_folder_refuses_the_save(
    harness: Harness, tmp_path: Path, name: str
) -> None:
    code = tmp_path / "repo"
    code.mkdir()
    put_file(code, name)
    workspace = harness.trusted_code_folder(code)
    assert workspace.kind == "code" and workspace.policy == "ordinary"
    before = {code: snapshot(code)}

    harness.assert_refused(harness.instruction(workspace, name, NEW, sha(OLD)), before)


@pytest.mark.parametrize("name", INSTRUCTION_NAMES)
def test_the_note_route_still_refuses_both_names(harness: Harness, name: str) -> None:
    workspace = harness.create_vault("note-route")
    vault = Path(workspace.root.display)
    put_file(vault, name)
    before = {vault: snapshot(vault)}

    harness.assert_refused(harness.note(workspace, name, NEW, sha(OLD)), before)
