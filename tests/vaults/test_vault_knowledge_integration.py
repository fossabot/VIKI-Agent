"""Keep in vault, memory, backup and restore through `server.create_app`.

Every root is a synthetic folder under `tmp_path`. The app is built the way the launcher builds
it: the data services and the knowledge routes share `__main__._denied_roots`, and every vault
writer shares one coordinator and one resolver. Each test checks with `snapshot` that nothing
outside the data directory changed but what it names.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from tests.media.test_assets import PNG
from tests.vaults.test_vault_restore import live_operation_row
from tests.vaults.test_vault_write_posix import Entry, snapshot
from viki_agent import __main__ as launcher
from viki_agent import protection, server
from viki_agent.app.settings import SettingsService
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.domain import CanonicalRoot, Workspace
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.media.assets import AssetService, DecodedImage
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
)
from viki_agent.workspaces import RowKind, WorkspaceService, root_key

TOKEN = "vault-knowledge-integration-token"
BASE = "http://127.0.0.1:8765"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": "http://127.0.0.1:8765"}
OWNER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
CLAIM = "The synthetic vault keeps its weekly review on Fridays."
# A hang guard, not a sleep: a bounded lease wait answers in milliseconds; this only stops the
# test if the request waits without a deadline.
HANG_GUARD_S = 5


def trees(tmp_path: Path) -> dict[str, list[Entry]]:
    """Every root the test owns except the app's data directory, which the app may write."""
    return {path.name: snapshot(path) for path in tmp_path.iterdir() if path.name != "data"}


def added(before: dict[str, list[Entry]], after: dict[str, list[Entry]]) -> set[str]:
    """The entries `after` gained, as `<root>/<posix path>`; fails if any entry changed or went."""
    assert before.keys() <= after.keys()
    gained: set[str] = set()
    for name, entries in after.items():
        old = set(before.get(name, []))
        assert old <= set(entries), f"{name} lost or changed an entry"
        gained |= {f"{name}/{Path(entry[0]).as_posix()}" for entry in set(entries) - old}
        if name not in before:
            gained.add(name)
    return gained


@dataclass
class Harness:
    client: TestClient
    spaces: WorkspaceService
    bootstrap: VaultBootstrap
    vaults: Path

    def create_vault(self, name: str) -> Workspace:
        workspace, created, _ = self.bootstrap.create(
            str(uuid.uuid4()), name, str(self.vaults), None, "en", False
        )
        assert created and workspace.policy == "ordinary" and workspace.bootstrap == "complete"
        return workspace

    def admission(self, workspace: Workspace) -> dict[str, Any]:
        current = self.spaces.get(workspace.workspace_id)
        return {
            "policy_revision": current.policy_revision,
            "root_physical_id": current.root.physical_id,
        }

    def upload(self, data: bytes) -> str:
        uploaded = self.client.post(
            "/api/v2/assets",
            headers={**HEADERS, "x-file-name": quote("photo.png", safe="")},
            content=data,
        )
        assert uploaded.status_code == 201, uploaded.text
        return uploaded.json()["asset_id"]

    def keep(self, workspace: Workspace, asset_id: str) -> Any:
        return self.client.post(
            f"/api/v2/vaults/{workspace.workspace_id}/attachments",
            headers=HEADERS,
            json={
                **self.admission(workspace),
                "command_id": str(uuid.uuid4()),
                "asset_id": asset_id,
                "name": "photo.png",
            },
        )

    def create_memory(self, workspace: Workspace, scope: str = "vault") -> Any:
        return self.client.post(
            f"/api/v2/vaults/{workspace.workspace_id}/memories",
            headers=HEADERS,
            json={
                **self.admission(workspace),
                "command_id": str(uuid.uuid4()),
                "kind": "owner",
                "scope": scope,
                "claim": CLAIM,
                "sources": [],
            },
        )

    def approve(self, workspace: Workspace, memory_id: str, sha256: str) -> Any:
        return self.client.patch(
            f"/api/v2/vaults/{workspace.workspace_id}/memories/{memory_id}",
            headers=HEADERS,
            json={**self.admission(workspace), "action": "approve", "sha256": sha256},
        )

    def memory_states(self, workspace: Workspace) -> dict[str, str]:
        listed = self.client.get(
            f"/api/v2/vaults/{workspace.workspace_id}/memories", headers=HEADERS
        )
        assert listed.status_code == 200, listed.text
        return {item["ref"]: item["state"] for item in listed.json()["items"]}

    def backup(self, workspace: Workspace, destination: Path) -> Any:
        return self.client.post(
            f"/api/v2/vaults/{workspace.workspace_id}/backup",
            headers=HEADERS,
            json={"command_id": str(uuid.uuid4()), "destination": str(destination)},
        )

    @contextmanager
    def holding(
        self, coordinator: MutationCoordinator, workspace: Workspace, refs: tuple[str, ...]
    ) -> Iterator[Callable[[Callable[[], Any]], Any]]:
        """Hold a lease on `refs` in the app's own loop, as another writer would, and yield a
        sender: each request runs on its own thread and answers None if it is still waiting
        after HANG_GUARD_S. The lease is released, and every sender joined, on exit."""
        portal = self.client.portal
        assert portal is not None
        root = OrdinaryMutationRoot.from_admitted_workspace(self.spaces.get(workspace.workspace_id))
        stack = contextlib.AsyncExitStack()
        portal.call(stack.enter_async_context, coordinator.lease(root, refs))
        workers: list[threading.Thread] = []

        def send(request: Callable[[], Any]) -> Any:
            answers: list[Any] = []
            worker = threading.Thread(target=lambda: answers.append(request()))
            workers.append(worker)
            worker.start()
            worker.join(HANG_GUARD_S)
            return answers[0] if answers else None

        try:
            yield send
        finally:
            portal.call(stack.aclose)
            for worker in workers:
                worker.join()


@contextmanager
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """One app launch over `tmp_path`, with the data routes and the launcher's deny list."""
    data, home, cwd, vaults, resources = (
        tmp_path / name for name in ("data", "home", "cwd", "vaults", "resources")
    )
    # The asset store and the recovery store require a private data folder, as the launcher's is.
    assert protection.create_private_directory(data)
    for folder in (home, cwd, vaults, resources):
        folder.mkdir()
    (resources / "welcome.en.md").write_text("# Welcome\n", encoding="utf-8")

    async def decoded(_service: AssetService, _stage: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)  # no image worker process in this test

    monkeypatch.setattr(AssetService, "_decode_in_worker", decoded)
    config = Config(vault=cwd, claude_home=home, cli=Path("fake"), edge=None, data_dir=data)
    state = AppState(data / "state.json", Prefs())
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=cwd, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("no knowledge request may start a provider"),
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
    settings = SettingsService(
        state.store, provider_defaults=Prefs, on_chat_defaults=lambda _prefs: None
    )
    active = spaces.resolve_active()
    denied_roots = launcher._denied_roots(config, spaces, active)
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=Catalog(home, cwd),
        static_dir=None,
        workspaces=spaces,
        bootstrap=bootstrap,
        data=launcher._data_services(
            config,
            settings,
            spaces,
            active,
            store=state.store,
            document=lambda *_: {},
            log_file=data / "logs" / "viki-agent.log",
            launch_id="launch-1",
            scratch_in_use=lambda: False,
            owner=OWNER,
            denied_roots=denied_roots,
        ),
        owner=OWNER,
        denied_roots=denied_roots,
        app_version="0.0.0-test",
    )
    with TestClient(app, base_url=BASE) as client:
        yield Harness(client, spaces, bootstrap, vaults)


def test_keep_in_vault_copies_the_asset_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("keep")
        asset_id = app.upload(PNG)
        before = trees(tmp_path)

        kept = app.keep(workspace, asset_id)

        assert kept.status_code == 201, kept.text
        assert kept.json() == {
            "ref": "attachments/photo.png",
            "reused": False,
            "byte_size": len(PNG),
        }
        assert (Path(workspace.root.display) / "attachments" / "photo.png").read_bytes() == PNG
        assert added(before, trees(tmp_path)) == {
            "vaults/keep/attachments",
            "vaults/keep/attachments/photo.png",
        }


def test_an_approved_memory_lists_as_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("recall")
        before = trees(tmp_path)

        created = app.create_memory(workspace)
        assert created.status_code == 201, created.text
        record = created.json()
        ref = record["ref"]
        assert ref == f"memory/{record['memory_id']}.md" and record["replayed"] is False
        assert app.memory_states(workspace) == {ref: "candidate"}

        approved = app.approve(workspace, record["memory_id"], record["sha256"])

        assert approved.status_code == 200, approved.text
        assert approved.json()["outcome"] == "written"
        assert app.memory_states(workspace) == {ref: "active"}
        assert added(before, trees(tmp_path)) == {"vaults/recall/memory", f"vaults/recall/{ref}"}


def test_a_backup_restores_into_a_new_read_only_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("source")
        source = Path(workspace.root.display)
        (source / "note.md").write_bytes(b"# A synthetic note\n")
        destination, parent = tmp_path / "backups", tmp_path / "restored"
        destination.mkdir()
        parent.mkdir()
        before = trees(tmp_path)

        backed = app.backup(workspace, destination)
        assert backed.status_code == 201, backed.text
        restored = app.client.post(
            "/api/v2/backup-restore",
            headers=HEADERS,
            json={
                "command_id": str(uuid.uuid4()),
                "backup": str(destination / backed.json()["folder"]),
                "parent": str(parent),
                "folder": "copy",
            },
        )

        assert restored.status_code == 201, restored.text
        answer = restored.json()
        assert answer["verified"] is True and answer["problems"] == []
        assert answer["workspace"]["policy"] == "existing_readonly"
        registered = app.spaces.get(answer["workspace"]["workspace_id"])
        assert registered.policy == "existing_readonly"
        assert Path(registered.root.display) == (parent / "copy").resolve()
        assert (parent / "copy" / "note.md").read_bytes() == b"# A synthetic note\n"
        gained = added(before, trees(tmp_path))
        assert "restored/copy/note.md" in gained
        assert all(path.startswith(("backups/", "restored/")) for path in gained)


def test_a_backup_restore_under_the_registration_freeze_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck row in another vault freezes registrations, so the restore answers
    `recovery_required` (503, as `test_vault_knowledge_api`'s status map pins) before it creates
    a folder, and registers nothing."""
    with launch(tmp_path, monkeypatch) as app:
        source = app.create_vault("source")
        other = app.create_vault("other")
        (Path(source.root.display) / "note.md").write_bytes(b"# A synthetic note\n")
        destination, parent = tmp_path / "backups", tmp_path / "restored"
        destination.mkdir()
        parent.mkdir()
        backed = app.backup(source, destination)
        assert backed.status_code == 201, backed.text
        live_operation_row(app.spaces, other.workspace_id)
        registrations = app.spaces.list()

        restored = app.client.post(
            "/api/v2/backup-restore",
            headers=HEADERS,
            json={
                "command_id": str(uuid.uuid4()),
                "backup": str(destination / backed.json()["folder"]),
                "parent": str(parent),
                "folder": "copy",
            },
        )

        assert restored.status_code == 503, restored.text
        assert restored.json()["error"]["code"] == "recovery_required"
        assert list(parent.iterdir()) == []
        assert app.spaces.list() == registrations


def test_keep_memory_and_backup_wait_on_the_shared_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keep, a memory save and a backup sent while another writer holds the file wait on the
    one coordinator every vault writer shares, and refuse `vault_locked` past the deadline."""
    monkeypatch.setattr(server, "VAULT_LEASE_WAIT_S", 0.02)
    coordinators: list[MutationCoordinator] = []

    class Recorded(MutationCoordinator):
        def __init__(self) -> None:
            super().__init__()
            coordinators.append(self)

    monkeypatch.setattr(server, "MutationCoordinator", Recorded)
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("shared")
        asset_id = app.upload(PNG)
        assert app.keep(workspace, asset_id).status_code == 201
        record = app.create_memory(workspace).json()
        destination = tmp_path / "backups"
        destination.mkdir()
        before = trees(tmp_path)

        with app.holding(
            coordinators[0], workspace, ("attachments/photo.png", record["ref"])
        ) as send:
            answers = [
                send(lambda: app.keep(workspace, asset_id)),
                send(lambda: app.approve(workspace, record["memory_id"], record["sha256"])),
                send(lambda: app.backup(workspace, destination)),
            ]

        for answer in answers:
            assert answer is not None, "a request waited past its deadline"
            assert answer.status_code == 409, answer.text
            assert answer.json()["error"]["code"] == "vault_locked"
        assert trees(tmp_path) == before
        assert app.memory_states(workspace) == {record["ref"]: "candidate"}
        # Released, the same keep finds its file.
        assert app.keep(workspace, asset_id).status_code == 200


def unreachable(app: Harness, situation: str) -> Workspace:
    """A registered vault holding one note that no save can reach. `twin` is an app-created
    ordinary vault whose instructions match a protected vault's, which the resolver refuses."""
    folder = app.vaults / situation
    if situation == "twin":
        guarded = app.vaults / "guarded"
        guarded.mkdir()
        (guarded / "CLAUDE.md").write_bytes(b"# Shared synthetic instructions\n")
        protected, _ = app.spaces.register_existing(str(uuid.uuid4()), str(guarded))
        app.spaces.set_policy(
            str(uuid.uuid4()), protected.workspace_id, protected.policy_revision, "protected", []
        )
        twin = app.create_vault(situation)
        (folder / "CLAUDE.md").write_bytes(b"# Shared synthetic instructions\n")
        (folder / "note.md").write_bytes(b"# A synthetic note\n")
        return twin
    folder.mkdir()
    (folder / "note.md").write_bytes(b"# A synthetic note\n")
    workspace, created = app.spaces.register_existing(str(uuid.uuid4()), str(folder))
    assert created and workspace.policy == "existing_readonly" and workspace.bootstrap == "none"
    if situation == "existing_readonly":
        return workspace
    acknowledged = (
        list(app.spaces.inspect_policy(workspace.workspace_id).acknowledgements_required)
        if situation == "ordinary"
        else []
    )
    policy: Any = situation
    return app.spaces.set_policy(
        str(uuid.uuid4()), workspace.workspace_id, workspace.policy_revision, policy, acknowledged
    )


@pytest.mark.parametrize("situation", ["protected", "existing_readonly", "ordinary", "twin"])
def test_a_vault_no_save_can_reach_backs_up_without_a_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, situation: str
) -> None:
    """A notes backup works for any policy. Only a vault a save can reach takes the mutation
    lease; `ordinary` here was opened (bootstrap `none`) and then set ordinary."""
    with launch(tmp_path, monkeypatch) as app:
        workspace = unreachable(app, situation)
        source = Path(workspace.root.display)
        destination = tmp_path / "backups"
        destination.mkdir()
        before = snapshot(source)

        backed = app.backup(workspace, destination)

        assert backed.status_code == 201, backed.text
        copy = destination / backed.json()["folder"]
        manifest = json.loads((copy / "viki-backup.json").read_bytes())
        assert manifest["source"]["policy"] == app.spaces.get(workspace.workspace_id).policy
        listed = {row["path"]: row["sha256"] for row in manifest["files"]}
        assert listed["note.md"] == hashlib.sha256(b"# A synthetic note\n").hexdigest()
        for path, digest in listed.items():
            assert hashlib.sha256((copy / path).read_bytes()).hexdigest() == digest
        assert snapshot(source) == before


def test_a_resolver_refusal_other_than_unreachable_still_refuses_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `ordinary_workspace_required` means no save can reach the vault; any other refusal
    of a vault a save can reach is the backup's, with nothing written."""

    class Changed(server.WorkspaceMutationResolver):
        def __call__(
            self,
            workspace_id: str,
            policy_revision: int,
            physical_id: str,
            *,
            kind: RowKind | None = "vault",
        ) -> OrdinaryMutationRoot:
            raise MutationError("workspace_changed")

    monkeypatch.setattr(server, "WorkspaceMutationResolver", Changed)
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("moved")
        destination = tmp_path / "backups"
        destination.mkdir()
        before = trees(tmp_path)

        backed = app.backup(workspace, destination)

        assert backed.status_code == 409, backed.text
        assert backed.json()["error"]["code"] == "workspace_changed"
        assert trees(tmp_path) == before


def test_a_memory_is_a_plain_vault_file_and_a_reset_leaves_every_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Portable knowledge is the vault's own file, and a reset of every app category it may clear
    touches no vault byte."""
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("portable")
        other = app.create_vault("other")
        (Path(other.root.display) / "note.md").write_bytes(b"# Another vault\n")
        created = app.create_memory(workspace)
        assert created.status_code == 201, created.text
        record = created.json()
        path = Path(workspace.root.display) / record["ref"]
        with open(path, encoding="utf-8") as plain:
            text = plain.read()
        assert CLAIM in text and record["memory_id"] in text
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        before = trees(tmp_path)

        reset = app.client.post(
            "/api/v2/data/reset",
            headers=HEADERS,
            json={
                "command_id": str(uuid.uuid4()),
                "categories": ["settings", "drafts", "commands", "approvals", "scratch"],
                "confirmation": "RESET",
            },
        )

        assert reset.status_code == 200, reset.text
        assert {result["status"] for result in reset.json()["results"].values()} == {"done"}
        assert trees(tmp_path) == before
        assert app.memory_states(workspace) == {record["ref"]: "candidate"}


# Keep and memory writes in a vault that is not an admitted ordinary one. Each row is the
# (status, error code) the route answers; a protected vault's memory create is the one admitted
# case, and it seals a proposal instead of writing. A bootstrap-incomplete vault's
# registered id (`pending:<command id>`) is not a physical id the routes accept, and the scratch
# workspace is no registered vault, so those two are refused before the resolver is reached.
SITUATIONS = ["protected", "existing_readonly", "incomplete_bootstrap", "scratch", "stale_root"]
REFUSALS = {
    ("keep", "protected"): (403, "vault_protected"),
    ("keep", "existing_readonly"): (403, "vault_read_only"),
    ("keep", "incomplete_bootstrap"): (400, "invalid_request"),
    ("keep", "scratch"): (404, "unknown_workspace"),
    ("keep", "stale_root"): (409, "workspace_changed"),
    ("create", "protected"): (201, None),
    ("create", "existing_readonly"): (409, "ordinary_workspace_required"),
    ("create", "incomplete_bootstrap"): (400, "invalid_request"),
    ("create", "scratch"): (404, "unknown_workspace"),
    ("create", "stale_root"): (409, "workspace_changed"),
    ("approve", "protected"): (403, "vault_protected"),
    ("approve", "existing_readonly"): (409, "ordinary_workspace_required"),
    ("approve", "incomplete_bootstrap"): (400, "invalid_request"),
    ("approve", "scratch"): (404, "unknown_workspace"),
    ("approve", "stale_root"): (409, "workspace_changed"),
    **{
        (action, situation): refusal
        for action in ("retire", "correct", "narrow_scope")
        for situation, refusal in (
            ("protected", (403, "vault_protected")),
            ("existing_readonly", (409, "ordinary_workspace_required")),
            ("incomplete_bootstrap", (400, "invalid_request")),
            ("scratch", (404, "unknown_workspace")),
            ("stale_root", (409, "workspace_changed")),
        )
    },
}
# The body of each memory PATCH beyond the admission and the seed's hash.
CHANGES: dict[str, dict[str, Any]] = {
    "approve": {"action": "approve"},
    "retire": {"action": "retire", "retired_by": None},
    "correct": {"action": "correct", "claim": "The synthetic vault reviews on Mondays."},
    "narrow_scope": {"action": "narrow_scope"},
}


def reviews(tmp_path: Path) -> list[tuple[Any, ...]]:
    """Every `memory_reviews` row, read through a read-only connection to the app's store."""
    uri = f"{(tmp_path / 'data' / 'app.sqlite3').as_uri()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute("SELECT * FROM memory_reviews ORDER BY 1, 2").fetchall()


def in_situation(
    app: Harness, situation: str, record: bytes, memory_id: str
) -> tuple[Workspace, dict[str, Any], Path]:
    """A workspace in `situation` holding the seed record, with the admission a request sends
    and its root. The stale one is still ordinary, but its request names an older revision."""
    if situation == "scratch":
        workspace = app.spaces.resolve_active().workspace
        assert not workspace.persistent
    elif situation == "incomplete_bootstrap":
        folder = app.vaults / "pending"
        command_id = str(uuid.uuid4())
        workspace = app.spaces.register_created(
            command_id,
            CanonicalRoot(
                key=root_key(folder),
                display=str(folder),
                physical_id=f"pending:{command_id}",
                aliased=False,
            ),
            "pending",
            portable_id=None,
        )
        assert workspace.bootstrap == "pending"
    else:
        workspace = app.create_vault(situation)
    root = Path(workspace.root.display)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "memory" / f"{memory_id}.md").write_bytes(record)
    stale = workspace.policy_revision
    if situation == "stale_root":
        # Out and back: still ordinary, but the request's snapshot is no longer current.
        workspace = app.spaces.set_policy(
            str(uuid.uuid4()), workspace.workspace_id, stale, "existing_readonly", []
        )
    if situation in {"protected", "existing_readonly", "stale_root"}:
        policy: Any = "ordinary" if situation == "stale_root" else situation
        workspace = app.spaces.set_policy(
            str(uuid.uuid4()), workspace.workspace_id, workspace.policy_revision, policy, []
        )
    admission = {
        "policy_revision": stale if situation == "stale_root" else workspace.policy_revision,
        "root_physical_id": workspace.root.physical_id,
    }
    return workspace, admission, root


@pytest.mark.parametrize("route", ["keep", "create", *CHANGES])
@pytest.mark.parametrize("situation", SITUATIONS)
def test_a_vault_that_is_not_ordinary_takes_no_keep_or_memory_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, situation: str, route: str
) -> None:
    with launch(tmp_path, monkeypatch) as app:
        # `personal`, so a narrowing reaches its save rather than `invalid_transition`.
        seed = app.create_memory(app.create_vault("seed"), scope="personal").json()
        record = (tmp_path / "vaults" / "seed" / seed["ref"]).read_bytes()
        asset_id = app.upload(PNG)
        workspace, admission, root = in_situation(app, situation, record, seed["memory_id"])
        base = f"/api/v2/vaults/{workspace.workspace_id}"
        sent = {
            "keep": lambda: app.client.post(
                f"{base}/attachments",
                headers=HEADERS,
                json={
                    **admission,
                    "command_id": str(uuid.uuid4()),
                    "asset_id": asset_id,
                    "name": "photo.png",
                },
            ),
            "create": lambda: app.client.post(
                f"{base}/memories",
                headers=HEADERS,
                json={
                    **admission,
                    "command_id": str(uuid.uuid4()),
                    "kind": "owner",
                    "scope": "vault",
                    "claim": CLAIM,
                    "sources": [],
                },
            ),
            **{
                action: lambda change=change: app.client.patch(
                    f"{base}/memories/{seed['memory_id']}",
                    headers=HEADERS,
                    json={**admission, **change, "sha256": seed["sha256"]},
                )
                for action, change in CHANGES.items()
            },
        }
        # The scratch root lives under the data folder, so it is a tree of its own here.
        before = {**trees(tmp_path), "<root>": snapshot(root)}
        reviewed = reviews(tmp_path)

        answer = sent[route]()

        status, code = REFUSALS[route, situation]
        assert answer.status_code == status, answer.text
        if code is None:
            assert answer.json()["outcome"] == "proposed"
        else:
            assert answer.json()["error"]["code"] == code
        assert {**trees(tmp_path), "<root>": snapshot(root)} == before
        assert reviews(tmp_path) == reviewed
