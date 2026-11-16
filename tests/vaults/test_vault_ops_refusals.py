"""Hard rule 1 for note operations: every operation on a vault that is not an
admitted ordinary one, or naming an excluded name, an alias of one or a path through a reparse
point at either end, is refused over HTTP with no byte changed in any root it could touch.

The real services run through `server.create_app`. Every root is a synthetic scratch directory
under `tmp_path`; no real vault is ever read or written.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.vaults.test_vault_ops_integration import operation_rows, private_data
from tests.vaults.test_vault_write_integration import HEADERS, Harness, launch, sha, short_name
from viki_agent import winproc
from viki_agent.domain import CanonicalRoot, Workspace
from viki_agent.workspaces import root_key

NOTE = b"synthetic note\n"

# (relative path, is_dir, size, sha256); a link or junction is recorded, never followed.
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


def operate(
    app: Harness,
    workspace: Workspace,
    kind: str,
    fields: dict[str, str],
    revision: int | None = None,
) -> Any:
    return app.client.post(
        f"/api/v2/vaults/{workspace.workspace_id}/operations",
        headers=HEADERS,
        json={
            "policy_revision": workspace.policy_revision if revision is None else revision,
            "root_physical_id": workspace.root.physical_id,
            "kind": kind,
            "command_id": str(uuid.uuid4()),
            **fields,
        },
    )


def assert_refused(
    app: Harness, response: Any, before: dict[Path, list[Entry] | None], code: str | None = None
) -> None:
    """A typed refusal, or (`code` given) a `failed` outcome whose step refused with `code`
    before it changed anything; either way every root is as it was and no row is left."""
    answer = response.json()
    if code is None:
        assert not 200 <= response.status_code < 300, response.text
        assert isinstance(answer["error"]["code"], str) and answer["error"]["code"]
    else:
        assert response.status_code == 200, response.text
        assert (answer["status"], answer["reason"], answer["steps_applied"]) == ("failed", code, 0)
    assert roots(*before) == before
    assert operation_rows(app) == []


def roots(*paths: Path) -> dict[Path, list[Entry] | None]:
    """Each root's snapshot, or None while it does not exist (the recovery folder until a
    first delete), so that creating it is a change too."""
    return {path: snapshot(path) if path.exists() else None for path in paths}


def kept_entry(app: Harness, workspace: Workspace, vault: Path) -> str:
    """Delete `d.md` while the vault is ordinary, so a restore has an entry of its own."""
    (vault / "d.md").write_bytes(NOTE)
    deleted = operate(app, workspace, "delete", {"ref": "d.md", "expected_sha256": sha(NOTE)})
    assert deleted.status_code == 200 and deleted.json()["status"] == "done", deleted.text
    listed = app.client.get(f"/api/v2/vaults/{workspace.workspace_id}/recovery", headers=HEADERS)
    [entry] = listed.json()["entries"]
    return entry["recovery_id"]


def every_kind(recovery_id: str) -> list[tuple[str, dict[str, str]]]:
    loaded = {"expected_sha256": sha(NOTE)}
    return [
        ("rename", {"ref": "n.md", "to": "m.md", **loaded}),
        ("move", {"ref": "n.md", "to": "sub/n.md", **loaded}),
        ("archive", {"ref": "n.md", "to": "archive/n.md", **loaded}),
        ("delete", {"ref": "n.md", **loaded}),
        ("restore", {"recovery_id": recovery_id, "to": "r.md"}),
    ]


def not_ordinary(app: Harness, situation: str) -> tuple[Workspace, Path, str, int | None]:
    """A workspace in `situation`, its root, a recovery id and the revision a request sends."""
    if situation == "scratch":
        workspace = app.spaces.resolve_active().workspace
        assert not workspace.persistent
        return workspace, Path(workspace.root.display), str(uuid.uuid4()), None
    if situation == "incomplete_bootstrap":
        vault = app.vaults / "pending"
        command_id = str(uuid.uuid4())
        workspace = app.spaces.register_created(
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
        vault.mkdir(parents=True, exist_ok=True)
        return workspace, vault, str(uuid.uuid4()), None
    workspace = app.create_vault(situation)
    vault = Path(workspace.root.display)
    recovery_id = kept_entry(app, workspace, vault)
    stale = workspace.policy_revision
    policy: Any = "ordinary" if situation == "stale_root" else situation
    if situation == "stale_root":
        # Out and back: still ordinary, but the request's snapshot is no longer current.
        workspace = app.spaces.set_policy(
            str(uuid.uuid4()), workspace.workspace_id, stale, "existing_readonly", []
        )
    workspace = app.spaces.set_policy(
        str(uuid.uuid4()), workspace.workspace_id, workspace.policy_revision, policy, []
    )
    return workspace, vault, recovery_id, stale if situation == "stale_root" else None


@pytest.mark.parametrize(
    "situation",
    ["protected", "existing_readonly", "incomplete_bootstrap", "scratch", "stale_root"],
)
def test_a_vault_that_is_not_ordinary_refuses_every_write(tmp_path: Path, situation: str) -> None:
    private_data(tmp_path)
    with launch(tmp_path) as app:
        workspace, vault, recovery_id, revision = not_ordinary(app, situation)
        (vault / "n.md").write_bytes(NOTE)
        before = roots(vault, tmp_path / "data" / "vault-recovery")
        for kind, fields in every_kind(recovery_id):
            refused = operate(app, workspace, kind, fields, revision)
            assert_refused(app, refused, before)


CREDENTIALS = "Cred" + "entials"  # built from halves, as the hygiene guard requires
EXCLUDED = [
    ".Git/n.md",
    "sub/.CLAUDE/n.md",
    "sub/.Viki/n.md",
    f"sub/{CREDENTIALS}/n.md",
    "sub/.Env.Local/n.md",
    "Agents.md",
    "sub/Agents.Override.md",
    "sub/CLAUDE.md",
    "sub/Claude.Local.md",
    "sub/Gemini.md",
    "sub/.Viki-Stage-x.md",
    "sub/.VIKI-Backup-x.md",
]


def bad_ref(tmp_path: Path, vault: Path, case: str, *, plant: bool) -> tuple[str, list[Path]]:
    """The ref for `case` and every root it could touch. Its folders exist, and a note is
    planted behind it only when `plant` says so: a destination must be absent, so that a
    refusal cannot come from the name being taken."""
    if case == "junction":
        outside = tmp_path / "outside"
        outside.mkdir()
        if plant:
            (outside / "x.md").write_bytes(NOTE)
        link = vault / "link"
        if sys.platform == "win32":
            done = winproc.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)], cwd=str(vault), timeout=10.0
            )
            if done.returncode != 0:
                pytest.skip("this volume refuses junctions")
            assert link.is_junction() and os.path.samefile(link, outside)
        else:
            # A symlink is the POSIX reparse point the reader's dir-fd walk never follows.
            link.symlink_to(outside, target_is_directory=True)
            assert link.is_symlink() and os.path.samefile(link, outside)
        return "link/x.md", [vault, outside]
    if case == "alias":
        (vault / ".claude" / "commands").mkdir(parents=True)
        if plant:
            (vault / ".claude" / "commands" / "x.md").write_bytes(NOTE)
        alias = short_name(vault / ".claude")
        if alias == ".claude":  # this volume makes no short names; the refusal must hold anyway
            alias = "CLAUDE~1"
        return f"{alias}/commands/x.md", [vault]
    target = vault / case
    target.parent.mkdir(parents=True, exist_ok=True)
    if plant:
        target.write_bytes(NOTE)
    return case, [vault]


@pytest.mark.parametrize("end", ["source", "destination"])
@pytest.mark.parametrize(
    "case",
    [
        *EXCLUDED,
        pytest.param(
            "alias",
            marks=pytest.mark.skipif(
                sys.platform != "win32",
                reason="8.3 short names are a Windows-only filesystem feature",
            ),
        ),
        "junction",
    ],
    ids=lambda case: case.replace(CREDENTIALS, "Cred-entials"),
)
def test_an_excluded_name_or_alias_is_refused_at_either_end(
    tmp_path: Path, case: str, end: str
) -> None:
    private_data(tmp_path)
    with launch(tmp_path) as app:
        workspace = app.create_vault("excluded")
        vault = Path(workspace.root.display)
        recovery_id = kept_entry(app, workspace, vault)
        (vault / "n.md").write_bytes(NOTE)
        ref, touched = bad_ref(tmp_path, vault, case, plant=end == "source")
        loaded = {"expected_sha256": sha(NOTE)}
        if end == "source":
            attempts = [
                ("move", {"ref": ref, "to": "moved.md", **loaded}),
                ("delete", {"ref": ref, **loaded}),
            ]
        else:
            attempts = [
                ("move", {"ref": "n.md", "to": ref, **loaded}),
                ("restore", {"recovery_id": recovery_id, "to": ref}),
            ]
        before = roots(*touched, tmp_path / "data" / "vault-recovery")
        for kind, fields in attempts:
            # An alias destination passes the name checks and is refused by the native step.
            late = "excluded_path" if (case, end) == ("alias", "destination") else None
            assert_refused(app, operate(app, workspace, kind, fields), before, late)
