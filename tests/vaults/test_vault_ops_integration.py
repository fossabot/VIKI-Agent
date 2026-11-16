"""Note operations through `server.create_app`, across real process death.

A crash is a real `os._exit` in a windowless child that runs one operation against the same
data directory and dies just after one step boundary, as the helpers of
`tests/persistence/test_storage_crash.py` do. The relaunch is the next `create_app`, whose
lifespan reconciles what the child left. Every root is a synthetic scratch directory under
`tmp_path`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.vaults.test_vault_write_integration import HEADERS, Harness, launch, leftovers, sha, tree
from viki_agent import protection, server, storage, winproc
from viki_agent.domain import Workspace
from viki_agent.vaults.vault_mutation import (
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    VaultPublication,
)

NOTE = b"synthetic note\n"

# argv: base folder, the src folder to import, the boundary, the request as JSON. The child
# builds the same services the app does over the same store, then dies just after the boundary.
_OPERATION_CRASH = """
import asyncio
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from viki_agent import workspaces
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.vault_lifecycle import VaultLifecycleService
from viki_agent.vaults.vault_mutation import MutationCoordinator, VaultPublication
from viki_agent.vaults.vault_operations import OperationJournal, RecoveryStore
from viki_agent.vaults.vault_publication import PublicationJournal
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService
base = Path(sys.argv[1])
boundary = sys.argv[3]
request = json.loads(sys.argv[4])
# This child never loads tests/conftest.py's session-scoped posix_system_deny fixture, so the
# fixed deny set would otherwise refuse tmp_path's own tree (it lies under /tmp, or /private/var
# on macOS); drop the same entries that fixture drops for the parent process.
if sys.platform != "win32":
    real_base = Path(os.path.realpath(base))
    workspaces.SYSTEM_DENY = tuple(
        spec
        for spec in workspaces.SYSTEM_DENY
        if not real_base.is_relative_to(os.path.realpath(spec[0]))
    )
owner, name, new_state = {
    'planned': (OperationJournal, 'plan', None),
    'kept': (RecoveryStore, 'put', None),
    'preserved': (OperationJournal, 'transition', 'preserved'),
    'applying': (OperationJournal, 'transition', 'applying'),
    'folders': (VaultPublication, 'ensure_folders', None),
    'moved': (VaultPublication, 'move_verified', None),
    'removed': (VaultPublication, 'remove_verified', None),
    'staged': (VaultPublication, 'stage', None),
    'published': (VaultPublication, 'publish_create', None),
    'tidied': (RecoveryStore, 'drop', None),
    'finished': (OperationJournal, 'delete', None),
}[boundary]
real = getattr(owner, name)
def crash_after(*args, **kwargs):
    result = real(*args, **kwargs)
    if new_state is None or args[3] == new_state:
        os._exit(73)
    return result
setattr(owner, name, crash_after)
state = AppState(base / 'data' / 'state.json', Prefs())
spaces = WorkspaceService(
    state.store,
    env={},
    data_dir=base / 'data',
    home=base / 'profile',
    install_dir=base / 'install',
    git_available=lambda: False,
    hide_previews=lambda: False,
)
journal = OperationJournal(state.store)
service = VaultLifecycleService(
    journal,
    RecoveryStore(base / 'data' / 'vault-recovery', journal),
    WorkspaceMutationResolver(spaces, git_available=lambda: False),
    MutationCoordinator(),
    lease_wait_s=1.0,
    publications=PublicationJournal(state.store),
)
asyncio.run(service.apply(**request))
os._exit(74)
"""


def private_data(tmp_path: Path) -> None:
    # The recovery store requires its parent private, as the launcher's data folder is.
    assert protection.create_private_directory(tmp_path / "data")


def body(app: Harness, workspace: Workspace, kind: str, **fields: str) -> dict[str, Any]:
    current = app.spaces.get(workspace.workspace_id)
    return {
        "policy_revision": current.policy_revision,
        "root_physical_id": current.root.physical_id,
        "kind": kind,
        "command_id": str(uuid.uuid4()),
        **fields,
    }


def apply(app: Harness, workspace: Workspace, request: dict[str, Any]) -> Any:
    return app.client.post(
        f"/api/v2/vaults/{workspace.workspace_id}/operations", headers=HEADERS, json=request
    )


def recovery(app: Harness, workspace: Workspace) -> dict[str, Any]:
    listed = app.client.get(f"/api/v2/vaults/{workspace.workspace_id}/recovery", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    return listed.json()


def operation_rows(app: Harness) -> list[tuple[str, str]]:
    uri = f"{app.database.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute(
            "SELECT operation_id,state FROM vault_operations ORDER BY operation_id"
        ).fetchall()


def crash(tmp_path: Path, workspace: Workspace, request: dict[str, Any], boundary: str) -> None:
    """Run `request` in a child that dies just after `boundary`; the app is not running."""
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _OPERATION_CRASH,
            str(tmp_path),
            str(Path(storage.__file__).parents[1]),
            boundary,
            json.dumps({**request, "workspace_id": workspace.workspace_id}),
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr


def create_vault(tmp_path: Path) -> tuple[Workspace, Path]:
    with launch(tmp_path) as app:
        workspace = app.create_vault("ops")
    vault = Path(workspace.root.display)
    (vault / "n.md").write_bytes(NOTE)
    return workspace, vault


def _loaded() -> dict[str, str]:
    """The revision the user loaded: the planted note's."""
    return {"expected_sha256": sha(NOTE)}


def create_request(tmp_path: Path, workspace: Workspace, kind: str) -> dict[str, Any]:
    with launch(tmp_path) as app:
        if kind == "move":
            return body(app, workspace, "move", ref="n.md", to="a/b/m.md", **_loaded())
        delete = body(app, workspace, "delete", ref="n.md", **_loaded())
        if kind == "delete":
            return delete
        # A restore needs an entry: the note is deleted first, through the app.
        deleted = apply(app, workspace, delete)
        assert deleted.status_code == 200 and deleted.json()["status"] == "done", deleted.text
        [entry] = recovery(app, workspace)["entries"]
        return body(app, workspace, "restore", recovery_id=entry["recovery_id"], to="n.md")


# (kind, boundary, the reconciled status or None when the row was already gone, where the note's
# bytes are after the relaunch: at its old place, its new place, or both). For a delete the new
# place is its recovery entry; for a restore the old place is.
MATRIX = [
    ("move", "planned", "failed", "old"),
    ("move", "preserved", "failed", "old"),
    ("move", "applying", "failed", "old"),
    ("move", "folders", "failed", "old"),
    ("move", "moved", "done", "new"),
    ("move", "finished", None, "new"),
    ("delete", "planned", "failed", "old"),
    # The copy was kept before the row named it: the note stays, and so does a stray entry.
    ("delete", "kept", "failed", "both"),
    ("delete", "preserved", "failed", "old"),
    ("delete", "applying", "failed", "old"),
    ("delete", "removed", "done", "new"),
    ("delete", "finished", None, "new"),
    ("restore", "planned", "failed", "old"),
    ("restore", "preserved", "failed", "old"),
    ("restore", "applying", "failed", "old"),
    ("restore", "staged", "failed", "old"),
    ("restore", "published", "done", "new"),
    ("restore", "tidied", "done", "new"),
    ("restore", "finished", None, "new"),
]


@pytest.mark.parametrize(("kind", "boundary", "status", "where"), MATRIX)
def test_a_real_process_death_at_each_boundary_reconciles(
    tmp_path: Path, kind: str, boundary: str, status: str | None, where: str
) -> None:
    private_data(tmp_path)
    workspace, vault = create_vault(tmp_path)
    request = create_request(tmp_path, workspace, kind)
    crash(tmp_path, workspace, request, boundary)

    with launch(tmp_path) as app:
        assert operation_rows(app) == []
        assert leftovers(vault) == []
        entries = recovery(app, workspace)["entries"]
        kept = [
            (tmp_path / "data" / "vault-recovery" / f"{entry['recovery_id']}.bin").read_bytes()
            for entry in entries
        ]
        if kind == "move":
            old, new = (vault / "n.md").is_file(), (vault / "a" / "b" / "m.md").is_file()
            assert entries == []
        elif kind == "delete":
            old, new = (vault / "n.md").is_file(), kept == [NOTE]
        else:
            old, new = kept == [NOTE], (vault / "n.md").is_file()
        # Never both gone, and never both unless a stray copy is all that is left over.
        assert (old, new) == {"old": (True, False), "new": (False, True), "both": (True, True)}[
            where
        ]
        for path in (vault / "n.md", vault / "a" / "b" / "m.md"):
            assert not path.exists() or path.read_bytes() == NOTE
        if status is not None:
            # The relaunch decided the row; the command now answers what it decided.
            replay = apply(app, workspace, request)
            assert replay.status_code == 200, replay.text
            assert (replay.json()["status"], replay.json()["replayed"]) == (status, True)


def interrupted_rename(tmp_path: Path) -> tuple[Workspace, Path, dict[str, Any]]:
    """A rename that died after its move: the row is `applying` and `m.md` holds the note."""
    private_data(tmp_path)
    workspace, vault = create_vault(tmp_path)
    with launch(tmp_path) as app:
        request = body(app, workspace, "rename", ref="n.md", to="m.md", **_loaded())
    crash(tmp_path, workspace, request, "moved")
    assert (vault / "m.md").read_bytes() == NOTE
    return workspace, vault, request


def test_a_row_reconcile_blocks_still_blocks_after_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace, vault, request = interrupted_rename(tmp_path)
    # A second hard link on the destination: reconcile cannot read the note's identity.
    os.link(vault / "m.md", tmp_path / "outside-link.md")
    (vault / "x.md").write_bytes(NOTE)
    before = tree(vault)

    for _ in range(2):
        with (
            caplog.at_level("WARNING", logger="viki_agent.vaults.vault_lifecycle"),
            launch(tmp_path) as app,
        ):
            assert operation_rows(app) == [(request["command_id"], "applying")]
            assert tree(vault) == before
            assert [row["operation_id"] for row in recovery(app, workspace)["stuck"]] == [
                request["command_id"]
            ]
            # The row still holds both of its names: a rename onto its old one is refused.
            onto = body(app, workspace, "rename", ref="x.md", to="n.md", **_loaded())
            again = apply(app, workspace, onto)
            assert again.status_code == 423, again.text
            assert again.json()["error"]["code"] == "operation_busy"
            assert tree(vault) == before
    assert [record.getMessage() for record in caplog.records] == [
        f"vault operation {request['command_id']} blocked: rename applying"
    ] * 2


def recovery_required(tmp_path: Path) -> tuple[Workspace, Path, str]:
    """A rename that died after its move, whose destination then changed elsewhere: the
    relaunch can neither finish nor undo it, so the row stays `recovery_required`."""
    workspace, vault, request = interrupted_rename(tmp_path)
    (vault / "m.md").write_bytes(b"changed elsewhere\n")
    return workspace, vault, request["command_id"]


def test_a_recovery_required_operation_refuses_a_note_save(tmp_path: Path) -> None:
    workspace, vault, operation_id = recovery_required(tmp_path)

    with launch(tmp_path) as app:
        assert operation_rows(app) == [(operation_id, "recovery_required")]
        before = tree(vault)
        refused = app.save(workspace, "other.md", b"draft\n", None)
        assert refused.json()["error"]["code"] == "vault_recovery_required", refused.text
        assert tree(vault) == before
        assert app.rows() == []

        forgotten = app.client.post(
            f"/api/v2/vault-operations/{operation_id}/resolve",
            headers=HEADERS,
            json={"choice": "forget"},
        )
        assert forgotten.status_code == 200, forgotten.text
        saved = app.save(workspace, "other.md", b"draft\n", None)
        assert saved.status_code == 200, saved.text
        assert (vault / "other.md").read_bytes() == b"draft\n"


def test_a_live_operation_row_refuses_every_registration_change(tmp_path: Path) -> None:
    """Through the app: an insert and a policy change are refused while a row lives."""
    workspace, _vault, operation_id = recovery_required(tmp_path)

    def create(app: Harness) -> Any:
        return app.client.post(
            "/api/v2/vaults/create",
            headers=HEADERS,
            json={"command_id": str(uuid.uuid4()), "name": "later", "parent": str(app.vaults)},
        )

    def demote(app: Harness) -> Any:
        return app.client.patch(
            f"/api/v2/vaults/{workspace.workspace_id}/policy",
            headers=HEADERS,
            json={
                "command_id": str(uuid.uuid4()),
                "expected_policy_revision": app.spaces.get(workspace.workspace_id).policy_revision,
                "policy": "existing_readonly",
                "acknowledged": [],
            },
        )

    with launch(tmp_path) as app:
        assert operation_rows(app) == [(operation_id, "recovery_required")]
        registered = app.client.get("/api/v2/workspaces", headers=HEADERS).json()
        for change in (create, demote):
            refused = change(app)
            assert refused.json()["error"]["code"] == "recovery_required", refused.text
        assert app.client.get("/api/v2/workspaces", headers=HEADERS).json() == registered

        app.client.post(
            f"/api/v2/vault-operations/{operation_id}/resolve",
            headers=HEADERS,
            json={"choice": "forget"},
        )
        assert create(app).status_code == 201
        assert demote(app).status_code == 200


def test_the_launch_sweep_removes_an_expired_recovery_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_data(tmp_path)
    workspace, vault = create_vault(tmp_path)
    with launch(tmp_path) as app:
        deleted = apply(app, workspace, body(app, workspace, "delete", ref="n.md", **_loaded()))
        assert deleted.status_code == 200 and deleted.json()["status"] == "done", deleted.text
        [entry] = recovery(app, workspace)["entries"]
    kept = tmp_path / "data" / "vault-recovery" / f"{entry['recovery_id']}.bin"
    assert kept.read_bytes() == NOTE

    class Later(datetime):
        """The launch's clock, one day past the entry's expiry."""

        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.now(tz) + timedelta(days=31)

    monkeypatch.setattr(server, "datetime", Later)
    with launch(tmp_path) as app:
        assert not kept.exists()
        uri = f"{app.database.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            assert connection.execute("SELECT count(*) FROM vault_recovery").fetchone() == (0,)
    assert not (vault / "n.md").exists()
    assert datetime.now(UTC) < datetime.fromisoformat(entry["expires_at"])


def test_a_save_and_an_operation_share_one_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation started while a save holds the note waits on the same coordinator, and
    its bounded wait refuses `vault_locked` instead of touching the note under the save."""
    monkeypatch.setattr(server, "VAULT_LEASE_WAIT_S", 0.02)
    private_data(tmp_path)
    workspace, vault = create_vault(tmp_path)
    answers: list[Any] = []
    real_stage = VaultPublication.stage

    with launch(tmp_path) as app:

        def stage_then_operate(
            self: VaultPublication,
            lease: MutationLease,
            root: OrdinaryMutationRoot,
            *args: Any,
        ) -> Any:
            # In the save's worker thread, under its lease: the app's loop is free to serve.
            request = body(app, workspace, "rename", ref="n.md", to="m.md", **_loaded())
            answers.append(apply(app, workspace, request))
            return real_stage(self, lease, root, *args)

        monkeypatch.setattr(VaultPublication, "stage", stage_then_operate)
        saved = app.save(workspace, "n.md", b"saved\n", sha(NOTE))
        assert saved.status_code == 200, saved.text

    [refused] = answers
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "vault_locked"
    assert (vault / "n.md").read_bytes() == b"saved\n"
    assert not (vault / "m.md").exists()


def test_a_vault_locked_answer_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 body names neither the vault's folder nor the note's ref nor its target."""
    monkeypatch.setattr(server, "VAULT_LEASE_WAIT_S", 0.02)
    private_data(tmp_path)
    workspace, vault = create_vault(tmp_path)
    answers: list[Any] = []
    real_stage = VaultPublication.stage

    with launch(tmp_path) as app:

        def stage_then_operate(
            self: VaultPublication,
            lease: MutationLease,
            root: OrdinaryMutationRoot,
            *args: Any,
        ) -> Any:
            request = body(app, workspace, "rename", ref="n.md", to="m.md", **_loaded())
            answers.append(apply(app, workspace, request))
            return real_stage(self, lease, root, *args)

        monkeypatch.setattr(VaultPublication, "stage", stage_then_operate)
        assert app.save(workspace, "n.md", b"saved\n", sha(NOTE)).status_code == 200

    [refused] = answers
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "vault_locked"
    # Decoded strings: the raw JSON escapes every backslash, hiding a Windows path.
    strings = _strings_in(refused.json())
    named_paths = (str(vault), vault.as_posix(), str(tmp_path), tmp_path.as_posix(), "n.md", "m.md")
    for named in named_paths:
        assert not [text for text in strings if named in text]
        assert named not in refused.text


def _strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.items() for part in item for text in _strings_in(part)]
    if isinstance(value, list):
        return [text for part in value for text in _strings_in(part)]
    return []


def open_a_vault(app: Harness) -> Any:
    return app.client.post(
        "/api/v2/vaults/create",
        headers=HEADERS,
        json={"command_id": str(uuid.uuid4()), "name": "later", "parent": str(app.vaults)},
    )


def demote_a_vault(app: Harness, workspace: Workspace) -> Any:
    return app.client.patch(
        f"/api/v2/vaults/{workspace.workspace_id}/policy",
        headers=HEADERS,
        json={
            "command_id": str(uuid.uuid4()),
            "expected_policy_revision": app.spaces.get(workspace.workspace_id).policy_revision,
            "policy": "existing_readonly",
            "acknowledged": [],
        },
    )


def resolve_publication(app: Harness, operation_id: str, choice: str) -> Any:
    return app.client.post(
        f"/api/v2/vault-publications/{operation_id}/resolve",
        headers=HEADERS,
        json={"choice": choice},
    )


def assert_registration_frozen_until_resolved(
    app: Harness, workspace: Workspace, operation_id: str, choice: str
) -> None:
    """A live publication row refuses every registration change; resolving it lifts that."""
    registered = app.client.get("/api/v2/workspaces", headers=HEADERS).json()
    for refused in (open_a_vault(app), demote_a_vault(app, workspace)):
        assert refused.json()["error"]["code"] == "recovery_required", refused.text
    assert app.client.get("/api/v2/workspaces", headers=HEADERS).json() == registered

    resolved = resolve_publication(app, operation_id, choice)
    assert resolved.status_code == 200, resolved.text
    assert app.rows() == []
    assert open_a_vault(app).status_code == 201
    assert demote_a_vault(app, workspace).status_code == 200


def test_forgetting_a_blocked_publication_lifts_the_registration_freeze(tmp_path: Path) -> None:
    """A replace interrupted at `prepared` whose note then changed elsewhere stays blocked."""
    with launch(tmp_path) as app:
        workspace = app.create_vault("blocked")
        note = Path(workspace.root.display) / "n.md"
        note.write_bytes(NOTE)
        row = app.plant(workspace, "n.md", b"interrupted\n", sha(NOTE), prepared=True)
    note.write_bytes(b"edited elsewhere\n")

    with launch(tmp_path) as app:
        assert app.rows() == [(row.operation_id, "n.md")]
        assert_registration_frozen_until_resolved(app, workspace, row.operation_id, "forget")
        assert note.read_bytes() == b"edited elsewhere\n"


@pytest.mark.parametrize("choice", ["forget", "drop_previous"])
def test_resolving_a_published_replace_lifts_the_registration_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    """A replace that published but could not delete its backup leaves a `published` row."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise MutationError("discard_failed")

    with launch(tmp_path) as app:
        workspace = app.create_vault("published")
        note = Path(workspace.root.display) / "n.md"
        note.write_bytes(NOTE)
        with monkeypatch.context() as patched:
            patched.setattr(VaultPublication, "discard_backup_after_publish", refuse)
            saved = app.save(workspace, "n.md", b"saved\n", sha(NOTE))
        assert saved.status_code == 200, saved.text
        [(operation_id, _ref)] = app.rows()

        assert_registration_frozen_until_resolved(app, workspace, operation_id, choice)
        assert note.read_bytes() == b"saved\n"
