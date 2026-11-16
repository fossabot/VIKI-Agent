"""Code folders in the workspace service: kinds, trust and activation, all in `tmp_path`."""

from __future__ import annotations

import logging
import stat
import sys
import uuid
from pathlib import Path

import pytest

from tests.app.test_workspaces import (
    COMMAND,
    OTHER_COMMAND,
    make_junction,
    protected_at,
    service_at,
    store_at,
)
from viki_agent import rails, winproc
from viki_agent.storage import SessionStore
from viki_agent.vaults.workspace_mutation import admits_note_writes
from viki_agent.workspaces import RowKind, WorkspaceError, WorkspaceService


def folder(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def trusted(service: WorkspaceService, root: Path) -> str:
    """Register `root` as code and trust it with every acknowledgement it asks for."""
    workspace, _ = service.register_existing(COMMAND, str(root), kind="code")
    required = service.inspect_policy(workspace.workspace_id).acknowledgements_required
    service.set_policy(OTHER_COMMAND, workspace.workspace_id, 1, "ordinary", required)
    return workspace.workspace_id


# --- no relabelling ---------------------------------------------------------------------------


def test_a_vault_path_cannot_register_as_code(tmp_path: Path) -> None:
    root = folder(tmp_path, "notes")
    service = service_at(tmp_path)
    vault, _ = service.register_existing(COMMAND, str(root))
    with pytest.raises(WorkspaceError) as relabel:
        service.register_existing(OTHER_COMMAND, str(root), kind="code")
    assert relabel.value.code == "kind_mismatch"
    assert service.get(vault.workspace_id).kind == "vault"
    assert len(service.list(kind=None)) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows aliases")
def test_a_junction_alias_cannot_relabel_either_kind(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    vault_root, code_root = folder(tmp_path, "notes"), folder(tmp_path, "repo")
    service.register_existing(COMMAND, str(vault_root))
    service.register_existing(COMMAND, str(code_root), kind="code")
    vault_alias, code_alias = tmp_path / "notes-alias", tmp_path / "repo-alias"
    make_junction(vault_alias, vault_root)
    make_junction(code_alias, code_root)
    for raw, kind in ((str(vault_alias), "code"), (str(code_alias), "vault")):
        with pytest.raises(WorkspaceError) as relabel:
            service.register_existing(OTHER_COMMAND, raw, kind=kind)  # type: ignore[arg-type]
        assert relabel.value.code == "kind_mismatch"
    assert len(service.list(kind=None)) == 2


def test_a_code_path_cannot_register_as_a_vault(tmp_path: Path) -> None:
    root = folder(tmp_path, "repo")
    service = service_at(tmp_path)
    code, created = service.register_existing(COMMAND, str(root), kind="code")
    assert (code.kind, code.policy, created) == ("code", "existing_readonly", True)
    again, created_again = service.register_existing(OTHER_COMMAND, str(root), kind="code")
    assert (again.workspace_id, created_again) == (code.workspace_id, False)
    with pytest.raises(WorkspaceError) as relabel:
        service.register_existing(OTHER_COMMAND, str(root))
    assert relabel.value.code == "kind_mismatch"


def test_a_code_copy_of_a_protected_vault_is_bound_protected_and_never_trusted(
    tmp_path: Path,
) -> None:
    vault = folder(tmp_path, "VIKI")
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    service = service_at(tmp_path)
    protected_id = protected_at(service, vault)
    copy = folder(tmp_path, "copy")
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    code, created = service.register_existing(COMMAND, str(copy), kind="code")
    assert (code.kind, code.policy, created) == ("code", "protected", True)
    assert code.identity.inherits_from == protected_id
    assert service.inspect_policy(code.workspace_id).admissible["ordinary"] is False
    with pytest.raises(WorkspaceError) as trust:
        service.set_policy(
            OTHER_COMMAND, code.workspace_id, 1, "ordinary", ["instructions_present"]
        )
    assert trust.value.code == "protected_root"
    with pytest.raises(WorkspaceError) as select:
        service.select(COMMAND, code.workspace_id)
    assert select.value.code == "trust_required"


# --- vault services never see a code folder ---------------------------------------------------


def test_get_and_list_default_to_vaults(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    vault, _ = service.register_existing(COMMAND, str(folder(tmp_path, "notes")))
    code_id = trusted(service, folder(tmp_path, "repo"))
    assert [workspace.workspace_id for workspace in service.list()] == [vault.workspace_id]
    assert [workspace.workspace_id for workspace in service.list(kind="code")] == [code_id]
    assert len(service.list(kind=None)) == 2
    with pytest.raises(WorkspaceError) as hidden:
        service.get(code_id)
    assert hidden.value.code == "unknown_workspace"
    with pytest.raises(WorkspaceError):
        service.get(vault.workspace_id, kind="code")
    code = service.get(code_id, kind=None)
    assert (code.kind, code.policy) == ("code", "ordinary")
    assert service.get(code_id, kind="code") == code
    assert admits_note_writes(code) is False


# --- trust acknowledgements -------------------------------------------------------------------


def test_a_code_inspection_shows_mcp_json_and_requires_claude_config(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    plain, _ = service.register_existing(COMMAND, str(folder(tmp_path, "plain")), kind="code")
    inspection = service.inspect_policy(plain.workspace_id)
    assert inspection.markers["mcp_json"] is False
    assert inspection.acknowledgements_required == []

    with_mcp = folder(tmp_path, "mcp")
    (with_mcp / ".mcp.json").write_text("{}\n", encoding="utf-8")
    (with_mcp / "CLAUDE.md").write_text("# synthetic\n", encoding="utf-8")
    code, _ = service.register_existing(COMMAND, str(with_mcp), kind="code")
    inspection = service.inspect_policy(code.workspace_id)
    assert inspection.markers["mcp_json"] is True and inspection.markers["claude_dir"] is False
    assert inspection.acknowledgements_required == ["instructions_present", "claude_config_present"]
    with pytest.raises(WorkspaceError) as missing:
        service.set_policy(
            OTHER_COMMAND, code.workspace_id, 1, "ordinary", ["instructions_present"]
        )
    assert missing.value.extra["required"] == ["claude_config_present"]

    with_dir = folder(tmp_path, "dir")
    (with_dir / ".claude").mkdir()
    code_dir, _ = service.register_existing(COMMAND, str(with_dir), kind="code")
    assert service.inspect_policy(code_dir.workspace_id).acknowledgements_required == [
        "claude_config_present"
    ]


def test_a_vault_inspection_keeps_its_markers_and_acknowledgements(tmp_path: Path) -> None:
    root = folder(tmp_path, "notes")
    (root / ".mcp.json").write_text("{}\n", encoding="utf-8")
    (root / ".claude").mkdir()
    service = service_at(tmp_path)
    vault, _ = service.register_existing(COMMAND, str(root))
    inspection = service.inspect_policy(vault.workspace_id)
    assert "mcp_json" not in inspection.markers and len(inspection.markers) == 6
    assert inspection.acknowledgements_required == []


# --- untrusted runs nothing -----------------------------------------------------------------


def committed_repo(root: Path) -> Path:
    """A one-commit repository at `root`; skips the test when Git is missing."""
    root.mkdir()
    try:
        winproc.git(root, "init", "-q")
        (root / "a.txt").write_text(f"{root.name}\n", encoding="utf-8")
        winproc.git(root, "add", "a.txt")
        winproc.git(
            root,
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "synthetic",
        )
    except Exception:
        pytest.skip("git is not available")
    return root


def test_registering_a_folder_with_an_fsmonitor_hook_runs_no_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rails, "_has_git_scope_override", lambda: False)
    root = committed_repo(tmp_path / "repo")
    marker = tmp_path / "fsmonitor-ran"
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = \"echo ran > '{marker.as_posix()}'\"\n")

    service = service_at(tmp_path, git=True)
    code, _ = service.register_existing(COMMAND, str(root), kind="code")
    assert code.identity.git_root_oids  # the two Git calls did run
    service.inspect_policy(code.workspace_id)
    assert not marker.exists()

    # The hook is live: a command that reads the index runs it.
    winproc.git(root, "status", "--porcelain")
    assert marker.exists()


def test_a_missing_head_object_starts_no_promisor_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promisor remote's `uploadpack` is a shell command; a lazy fetch for a missing HEAD
    object would run it, so the identity probe must never fetch."""
    monkeypatch.setattr(rails, "_has_git_scope_override", lambda: False)
    origin = committed_repo(tmp_path / "origin")
    root = committed_repo(tmp_path / "repo")
    marker = tmp_path / "uploadpack-ran"
    head = winproc.git(root, "rev-parse", "HEAD").strip()
    loose = root / ".git" / "objects" / head[:2] / head[2:]
    loose.chmod(stat.S_IWRITE)  # Git writes objects read-only, which Windows will not unlink
    loose.unlink()
    for name, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.url", origin.as_posix()),
        ("remote.origin.uploadpack", f"touch '{marker.as_posix()}'; git-upload-pack"),
    ):
        winproc.git(root, "config", name, value)

    service = service_at(tmp_path, git=True)
    code, _ = service.register_existing(COMMAND, str(root), kind="code")
    service.inspect_policy(code.workspace_id)
    assert not marker.exists()


@pytest.mark.parametrize("entry", ["file", "folder"])
def test_a_clone_holding_a_top_level_head_entry_stays_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A clone has a common directory of its own, and this one shares no instruction file, so
    only its root commits bind it to the protected root it was cloned from. A top-level `HEAD`
    makes the bare revision `HEAD` ambiguous to Git, which must not cost the binding."""
    monkeypatch.setattr(rails, "_has_git_scope_override", lambda: False)
    origin = committed_repo(tmp_path / "origin")
    service = service_at(tmp_path, git=True)
    protected_id = protected_at(service, origin)
    clone = tmp_path / "clone"
    winproc.git(tmp_path, "clone", "-q", str(origin), str(clone))
    if entry == "file":
        (clone / "HEAD").write_text("a note named HEAD\n", encoding="utf-8")
    else:
        (clone / "HEAD").mkdir()

    copy, created = service.register_existing(str(uuid.uuid4()), str(clone))

    assert (copy.policy, created) == ("protected", True)
    assert copy.identity.inherits_from == protected_id


@pytest.mark.parametrize("kind", ["vault", "code"])
def test_a_bare_layout_of_plain_files_asks_for_the_git_acknowledgement(
    tmp_path: Path, kind: RowKind
) -> None:
    """Git opens a folder holding `HEAD`, `objects/` and `refs/` as a repository though it has
    no `.git`, and so does Claude Code: the trust step says Git is there."""
    service = service_at(tmp_path)
    bare, plain = folder(tmp_path, "bare"), folder(tmp_path, "plain")
    for root in (bare, plain):
        (root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (bare / "objects").mkdir()
    (bare / "refs").mkdir()
    inspected = {}
    for root in (bare, plain):
        workspace, _ = service.register_existing(str(uuid.uuid4()), str(root), kind=kind)
        inspected[root.name] = service.inspect_policy(workspace.workspace_id)

    assert inspected["bare"].markers["git"] is True
    assert "git_present" in inspected["bare"].acknowledgements_required
    # A lone file named HEAD is a note: Git opens no repository there.
    assert inspected["plain"].markers["git"] is False
    assert inspected["plain"].acknowledgements_required == []


def test_an_untrusted_code_folder_is_neither_selected_nor_activated(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    untrusted, _ = service.register_existing(COMMAND, str(folder(tmp_path, "repo")), kind="code")
    with pytest.raises(WorkspaceError) as select:
        service.select(COMMAND, untrusted.workspace_id)
    assert select.value.code == "trust_required"
    assert service.selected_id() is None

    # Trusted, selected, then revoked from another launch: the next launch does not activate it.
    code_id = trusted(service, folder(tmp_path, "trusted"))
    service.select(COMMAND, code_id)
    service_at(tmp_path, store).set_policy(COMMAND, code_id, 2, "existing_readonly", [])
    active = service_at(tmp_path, store).resolve_active()
    assert active.workspace.kind == "scratch" and active.chat_only is True
    assert active.selected_id == code_id


# --- the active code folder -------------------------------------------------------------------


def test_the_active_code_folder_runs_in_its_root_and_keeps_its_policy(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = folder(tmp_path, "repo")
    code_id = trusted(service, root)
    service.select(COMMAND, code_id)
    active = service.resolve_active()
    assert active.workspace.kind == "code" and active.workspace.workspace_id == code_id
    assert active.cwd == Path(active.workspace.root.display) == root
    assert active.selected_by == "registration" and active.chat_only is False
    with pytest.raises(WorkspaceError) as revoke:
        service.set_policy(COMMAND, code_id, 2, "existing_readonly", [])
    assert revoke.value.code == "workspace_active"
    assert service.get(code_id, kind=None).policy == "ordinary"

    # A vault's policy is not held by the active code folder.
    vault, _ = service.register_existing(COMMAND, str(folder(tmp_path, "notes")))
    assert service.set_policy(COMMAND, vault.workspace_id, 1, "protected", []).policy == "protected"


def test_viki_vault_naming_a_code_folder_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_at(tmp_path)
    root = folder(tmp_path, "repo")
    trusted(service_at(tmp_path, store), root)
    caplog.set_level(logging.WARNING, logger="viki_agent.workspaces")
    active = service_at(tmp_path, store, env={"VIKI_VAULT": str(root)}).resolve_active()
    assert active.workspace.kind == "scratch" and active.selected_by == "builtin"
    assert "VIKI_VAULT ignored: code_root" in caplog.messages


# --- removing a code folder -------------------------------------------------------------------


def _worktree_row(workspace_id: str, path: Path) -> dict[str, object]:
    return {
        "worktree_id": "44444444-4444-4444-8444-444444444444",
        "workspace_id": workspace_id,
        "path": str(path),
        "branch": "wt",
        "base_oid": "a" * 40,
        "state": "creating",
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
    }


def test_remove_deletes_a_code_row(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    trusted_root = folder(tmp_path, "repo")
    untrusted_root = folder(tmp_path, "other")
    trusted_id = trusted(service, trusted_root)
    untrusted, _ = service.register_existing(COMMAND, str(untrusted_root), kind="code")

    service.remove(trusted_id)
    service.remove(untrusted.workspace_id)

    assert service.list(kind=None) == []
    again, created = service.register_existing(OTHER_COMMAND, str(trusted_root), kind="code")
    assert created is True
    assert again.workspace_id != trusted_id


def test_remove_refuses_an_unknown_id(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    with pytest.raises(WorkspaceError) as missing:
        service.remove("missing")
    assert missing.value.code == "unknown_workspace"


def test_remove_refuses_a_protected_code_row_and_leaves_it_and_its_vault_untouched(
    tmp_path: Path,
) -> None:
    vault = folder(tmp_path, "VIKI")
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    service = service_at(tmp_path)
    protected_id = protected_at(service, vault)
    copy = folder(tmp_path, "copy")
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    code, _ = service.register_existing(COMMAND, str(copy), kind="code")
    assert code.policy == "protected"

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code.workspace_id)
    assert refused.value.code == "protected_root"

    after = service.get(code.workspace_id, kind=None)
    assert after.policy == "protected" and after.identity.inherits_from == protected_id
    assert service.get(protected_id, kind=None).policy == "protected"
    assert len(service.list(kind=None)) == 2


def test_remove_checks_protected_before_kind_and_kind_before_use(tmp_path: Path) -> None:
    """The refusal order: a protected vault is `protected_root`, never `not_removable`; a
    selected ordinary vault is `not_removable`, never `workspace_in_use`."""
    service = service_at(tmp_path)

    protected_vault_id = protected_at(service, folder(tmp_path, "VIKI"))
    with pytest.raises(WorkspaceError) as protected_first:
        service.remove(protected_vault_id)
    assert protected_first.value.code == "protected_root"

    vault, _ = service.register_existing(COMMAND, str(folder(tmp_path, "notes")))
    service.select(OTHER_COMMAND, vault.workspace_id)
    with pytest.raises(WorkspaceError) as kind_first:
        service.remove(vault.workspace_id)
    assert kind_first.value.code == "not_removable"


def test_remove_refuses_the_active_code_folder(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    code_id = trusted(service, folder(tmp_path, "repo"))
    service.select(COMMAND, code_id)

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "workspace_in_use"


def test_remove_refuses_a_revoked_but_still_selected_code_folder(tmp_path: Path) -> None:
    """The stored selection blocks removal even when this launch no longer resolves active to
    it (trust was revoked after selection, so `resolve_active` now falls back to scratch)."""
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    code_id = trusted(service, folder(tmp_path, "repo"))
    service.select(COMMAND, code_id)
    service.set_policy(COMMAND, code_id, 2, "existing_readonly", [])

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "workspace_in_use"
    assert service.resolve_active().workspace.kind == "scratch"


def test_remove_refuses_a_code_folder_resolved_active_before_a_later_reselection(
    tmp_path: Path,
) -> None:
    """`resolve_active` memoizes once per launch. If the launch resolved active to a code
    folder and the stored selection then moves to a vault, that code folder's `active` column
    is now 0, but the launch still runs in it, so removal must still be `workspace_in_use`."""
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    code_id = trusted(service, folder(tmp_path, "repo"))
    service.select(COMMAND, code_id)
    assert service.resolve_active().workspace.workspace_id == code_id

    vault, _ = service.register_existing(OTHER_COMMAND, str(folder(tmp_path, "notes")))
    service.select(OTHER_COMMAND, vault.workspace_id)

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "workspace_in_use"
    assert code_id in [workspace.workspace_id for workspace in service.list(kind="code")]


@pytest.mark.parametrize("state", ["ready", "creating", "removing", "kept"])
def test_remove_refuses_a_code_folder_with_a_live_worktree(tmp_path: Path, state: str) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = folder(tmp_path, "repo")
    code_id = trusted(service, root)
    store.insert_worktree(_worktree_row(code_id, root / "w"), limit=32)
    if state != "creating":
        store.update_worktree_state(
            "44444444-4444-4444-8444-444444444444", "creating", state, "2026-01-01T00:00:00.000Z"
        )

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "worktrees_present"

    # Every non-`removed` state blocks; moving on to `removed` lifts the block.
    store.update_worktree_state(
        "44444444-4444-4444-8444-444444444444", state, "removed", "2026-01-01T00:00:01.000Z"
    )
    service.remove(code_id)
    assert service.list(kind=None) == []


def history(store: SessionStore, count: int, state: str) -> None:
    """`count` worktree rows in `state`, newer than any a test makes, of a folder no
    registration names: written directly, past the worktree cap, to stand in for any amount of
    history a newest-rows window would stop at."""
    stamp = "2099-01-01T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO worktrees (worktree_id,workspace_id,path,branch,base_oid,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    str(uuid.uuid4()),
                    "99999999-9999-4999-8999-999999999999",
                    f"C:/Synthetic/history/{state}/{index}",
                    f"history-{index}",
                    "a" * 40,
                    state,
                    stamp,
                    stamp,
                )
                for index in range(count)
            ],
        )


def test_remove_finds_a_worktree_behind_any_history(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = folder(tmp_path, "repo")
    code_id = trusted(service, root)
    store.insert_worktree(_worktree_row(code_id, root / "w"), limit=32)
    store.update_worktree_state(
        "44444444-4444-4444-8444-444444444444", "creating", "kept", "2026-01-01T00:00:00.000Z"
    )
    history(store, 4100, "removed")
    history(store, 4100, "kept")

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "worktrees_present"


def test_remove_checks_use_before_worktrees(tmp_path: Path) -> None:
    """The active code folder is `workspace_in_use`, even carrying a live worktree that would
    otherwise be `worktrees_present`."""
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = folder(tmp_path, "repo")
    code_id = trusted(service, root)
    service.select(COMMAND, code_id)
    store.insert_worktree(_worktree_row(code_id, root / "w"), limit=32)
    store.update_worktree_state(
        "44444444-4444-4444-8444-444444444444", "creating", "ready", "2026-01-01T00:00:00.000Z"
    )

    with pytest.raises(WorkspaceError) as refused:
        service.remove(code_id)
    assert refused.value.code == "workspace_in_use"
