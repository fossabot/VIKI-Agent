"""Canonical roots, identity, policy and the one-time legacy binding, all in `tmp_path`.

Nothing here reads or writes the user's vault, `~/.claude` or the real environment: every
root, home, data directory and deny mapping is synthetic.
"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from viki_agent import rails, winproc, workspaces
from viki_agent.code.code_files import PRIVATE_NAMES as CODE_PRIVATE_NAMES
from viki_agent.code.code_files import PRIVATE_SUFFIXES
from viki_agent.domain import CanonicalRoot, RootIdentity
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults.vault import (
    BACKUP_PREFIX,
    EXCLUDED_DIRS,
    STAGE_PREFIX,
)
from viki_agent.vaults.vault import (
    PRIVATE_NAMES as VAULT_PRIVATE_NAMES,
)
from viki_agent.vaults.vault_mutation import code_ref
from viki_agent.workspaces import (
    DenyEntry,
    WorkspaceError,
    WorkspaceService,
    canonical_root,
    denied_roots,
)

COMMAND = "11111111-1111-4111-8111-111111111111"
OTHER_COMMAND = "22222222-2222-4222-8222-222222222222"
# Drive letters, reserved names, UNC and device forms, 8.3 names, junctions and subst.
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path grammar")


def store_at(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "data" / "state.json", Prefs()).store


def service_at(
    tmp_path: Path,
    store: SessionStore | None = None,
    *,
    env: dict[str, str] | None = None,
    git: bool = False,
    hide_previews: bool = False,
) -> WorkspaceService:
    return WorkspaceService(
        store if store is not None else store_at(tmp_path),
        env=env if env is not None else {},
        data_dir=tmp_path / "data",
        home=tmp_path / "home",
        install_dir=tmp_path / "install",
        git_available=lambda: git,
        hide_previews=lambda: hide_previews,
    )


def short_path(path: Path) -> str:
    """The 8.3 form when the volume still generates one; the long path otherwise."""
    assert sys.platform == "win32"
    buffer = ctypes.create_unicode_buffer(4096)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return str(path)
    return buffer.value


def make_junction(link: Path, target: Path) -> None:
    assert sys.platform == "win32"
    done = winproc.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        cwd=str(link.parent),
        timeout=10.0,
    )
    if done.returncode != 0:
        pytest.skip("this volume refuses junctions")


def fake_stat(replacements: dict[str, Any]) -> Any:
    real = os.stat

    def stat(path: Any, **kwargs: Any) -> os.stat_result:
        info = real(path, **kwargs)
        values = list(info)
        for name, value in replacements.items():
            values[{"st_ino": 1, "st_dev": 2}[name]] = value
        return os.stat_result(values)

    return stat


def deny_free(path: Path) -> tuple[DenyEntry, ...]:
    """A deny set that admits `path`: only a drive root far from it, for canonical tests."""
    return (
        DenyEntry(
            key=os.path.normcase(str(path.drive) + "\\"),
            display=str(path.drive) + "\\",
            physical_id="0:0",
            aliased=False,
            category="drive_root",
            prefix=False,
        ),
    )


# --- canonicalisation ---------------------------------------------------------------------


@windows_only
def test_canonical_root_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sys.platform == "win32"
    root = tmp_path / "Vault Folder"
    root.mkdir()
    deny = deny_free(root)
    canonical = canonical_root(str(root), deny=deny)
    assert canonical.key == os.path.normcase(str(root))
    assert canonical.display == str(root)
    assert canonical.physical_id.count(":") == 1 and not canonical.aliased
    # 8.3 name, case variant, lowercase drive letter and a trailing separator all name it.
    for variant in (
        short_path(root),
        str(root).upper(),
        str(root)[0].lower() + str(root)[1:],
        str(root) + "\\",
    ):
        assert canonical_root(variant, deny=deny).key == canonical.key
        assert canonical_root(variant, deny=deny).display == canonical.display

    link = tmp_path / "alias"
    make_junction(link, root)
    through = canonical_root(str(link), deny=deny)
    assert (through.key, through.physical_id) == (canonical.key, canonical.physical_id)
    assert through.aliased is True  # informational: aliases are deduplicated, never refused

    file_path = tmp_path / "note.md"
    file_path.write_text("x", encoding="utf-8")
    refusals = {
        "\\\\?\\" + str(root): "invalid_path",
        "\\\\.\\C:\\": "invalid_path",
        "\\\\server\\share": "invalid_path",
        "C:relative": "invalid_path",
        "relative\\path": "invalid_path",
        f"{root}\\bad\x01name": "invalid_path",
        f"{root}\\trailing.": "invalid_path",
        f"{root}\\trailing ": "invalid_path",
        f"{root}\\COM1": "invalid_path",
        str(tmp_path / "missing"): "not_found",
        str(file_path): "not_directory",
    }
    for raw, code in refusals.items():
        with pytest.raises(WorkspaceError) as failure:
            canonical_root(raw, deny=deny)
        assert failure.value.code == code, raw

    # A resolve() that answers with a device form, as a volume-GUID input does, is refused
    # by the second syntax gate rather than registered under a path no other rule accepts.
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path("\\\\?\\C:\\vault"))
    with pytest.raises(WorkspaceError) as device:
        canonical_root(str(root), deny=deny)
    assert device.value.code == "unsafe_path"
    # A resolve() that gives up and returns the reparse point itself is refused as well.
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: link)
    with pytest.raises(WorkspaceError) as fallback:
        canonical_root(str(link), deny=deny)
    assert fallback.value.code == "unsafe_path"
    monkeypatch.undo()

    monkeypatch.setattr(workspaces, "volume_path", lambda path: str(tmp_path) + "\\")
    with pytest.raises(WorkspaceError) as mounted:
        canonical_root(str(root), deny=deny)
    assert (mounted.value.code, mounted.value.extra) == (
        "unsupported_location",
        {"reason": "mount_point"},
    )
    monkeypatch.undo()

    monkeypatch.setattr(workspaces, "drive_type", lambda drive: 4)
    with pytest.raises(WorkspaceError) as remote:
        canonical_root(str(root), deny=deny)
    assert remote.value.extra == {"reason": "drive_type"}
    monkeypatch.undo()


def test_a_root_without_a_file_index_is_refused_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No FAT32 volume's file index has been measured yet. A zero file index must refuse the
    # root rather than let two directories share one identity.
    root = tmp_path / "removable"
    root.mkdir()
    monkeypatch.setattr(os, "stat", fake_stat({"st_ino": 0}))
    with pytest.raises(WorkspaceError) as failure:
        canonical_root(str(root), deny=deny_free(root))
    assert (failure.value.code, failure.value.extra) == (
        "unsupported_location",
        {"reason": "no_identity"},
    )


@windows_only
def test_subst_drive_resolves_to_its_target(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    root = tmp_path / "substituted"
    root.mkdir()
    taken = {letter for letter in "DEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists()}
    free = next((letter for letter in "XYWVUT" if letter not in taken), None)
    if free is None:
        pytest.skip("no free drive letter for subst")
    if winproc.run(["subst", f"{free}:", str(root)], cwd=str(tmp_path), timeout=10.0).returncode:
        pytest.skip("subst unavailable")
    try:
        canonical = canonical_root(f"{free}:\\", deny=deny_free(root))
    finally:
        winproc.run(["subst", f"{free}:", "/D"], cwd=str(tmp_path), timeout=10.0)
    assert canonical.key == os.path.normcase(str(root))


@windows_only
def test_denied_roots_come_only_from_the_injected_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"
    data_dir, home, install = tmp_path / "data", tmp_path / "home", tmp_path / "install"
    elsewhere = tmp_path / "elsewhere"
    for path in (data_dir, home, install, elsewhere):
        path.mkdir()
    env = {"TEMP": str(elsewhere), "TMP": str(elsewhere), "LOCALAPPDATA": str(elsewhere)}

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the deny set must not read the real environment")

    monkeypatch.setattr(Path, "home", raising)
    monkeypatch.setattr(os, "environ", {})
    deny = denied_roots(env, data_dir=data_dir, home=home, install=install)

    candidate = tmp_path / "vault"
    candidate.mkdir()
    # tmp_path lies beneath a drive root and, on a real profile, beneath %TEMP%; the injected
    # mapping points those elsewhere, so an ordinary synthetic root stays registrable.
    assert canonical_root(str(candidate), deny=deny).key == os.path.normcase(str(candidate))

    # The prefix entries name their own category; the drive root and the profile root are
    # refused as well, by whichever rule fires first (both hold a denied root beneath them).
    refusals = {
        str(elsewhere): "temp",
        str(data_dir): "data_dir",
        str(install): "install",
    }
    for raw, category in refusals.items():
        with pytest.raises(WorkspaceError) as failure:
            canonical_root(raw, deny=deny)
        assert failure.value.code == "denied_location"
        assert failure.value.extra["denied"] == category, raw
    for raw in (str(tmp_path.drive) + "\\", str(home)):
        with pytest.raises(WorkspaceError) as refused:
            canonical_root(raw, deny=deny)
        assert refused.value.code == "denied_location", raw
    # Taken alone, each of those two entries denies itself and nothing below it.
    for raw, category in ((str(tmp_path.drive) + "\\", "drive_root"), (str(home), "profile")):
        tier = [
            entry for entry in deny if isinstance(entry, DenyEntry) and entry.category == category
        ]
        with pytest.raises(WorkspaceError) as alone:
            canonical_root(raw, deny=tier)
        assert alone.value.extra["denied"] == category, raw
        assert canonical_root(str(candidate), deny=tier).display == str(candidate)

    # A candidate that contains a denied root is refused in either tier.
    with pytest.raises(WorkspaceError) as containing:
        canonical_root(str(tmp_path), deny=deny)
    assert containing.value.code == "denied_location"
    # A folder under the profile root, including the suggested parent, stays registrable.
    (home / "VIKI").mkdir()
    assert canonical_root(str(home / "VIKI"), deny=deny).display == str(home / "VIKI")


@windows_only
def test_denied_and_unsupported_locations(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    cloud = tmp_path / "OneDrive"
    inside = cloud / "Notes"
    inside.mkdir(parents=True)
    data_dir = tmp_path / "data" / "inner"
    data_dir.mkdir(parents=True)
    deny = denied_roots(
        {"OneDrive": str(cloud)},
        data_dir=data_dir,
        home=tmp_path / "home",
        install=tmp_path / "install",
    )
    for raw in (str(cloud), str(inside)):
        with pytest.raises(WorkspaceError) as failure:
            canonical_root(raw, deny=deny)
        assert (failure.value.code, failure.value.extra) == (
            "unsupported_location",
            {"reason": "cloud_sync"},
        )
    with pytest.raises(WorkspaceError) as containing:
        canonical_root(str(tmp_path / "data"), deny=deny)
    assert containing.value.code == "denied_location"


@windows_only
def test_a_scrubbed_environment_still_denies_every_named_root(tmp_path: Path) -> None:
    # os.environ hands its names over upper-cased, and the deny set must not care how a name
    # is spelled: the launch passes exactly such a scrubbed copy.
    assert sys.platform == "win32"
    named = {
        "PROGRAMDATA": ("programdata", "denied_location"),
        "PROGRAMFILES": ("program_files", "denied_location"),
        "programfiles(x86)": ("program_files", "denied_location"),
        "SYSTEMROOT": ("system_root", "denied_location"),
        "ONEDRIVE": ("onedrive", "unsupported_location"),
        "onedrivecommercial": ("onedrive", "unsupported_location"),
    }
    env = winproc.scrub_env({name: str(tmp_path / f"root{n}") for n, name in enumerate(named)})
    deny = denied_roots(
        env, data_dir=tmp_path / "data", home=tmp_path / "home", install=tmp_path / "install"
    )
    for n, (category, code) in enumerate(named.values()):
        with pytest.raises(WorkspaceError) as failure:
            workspaces.refuse_denied(workspaces.root_key(tmp_path / f"root{n}" / "Notes"), deny)
        assert failure.value.code == code, category
        expected = {"reason": "cloud_sync"} if category == "onedrive" else {"denied": category}
        assert failure.value.extra == expected


# --- the scratch workspace ----------------------------------------------------------------


def test_scratch_root_is_private_under_data_dir_and_never_home_or_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pretend-vault").mkdir()
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "pretend-vault"))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert workspaces.SCRATCH_CHAT == ("scratch", "chat")
    assert workspaces.scratch_path(data_dir) == data_dir.joinpath(*workspaces.SCRATCH_CHAT)
    assert not workspaces.scratch_path(data_dir).exists()  # naming it creates nothing
    root = workspaces.scratch_root(data_dir)
    assert root == workspaces.scratch_path(data_dir)
    assert root.is_dir()
    workspace = workspaces.scratch_workspace(data_dir)
    assert workspace.persistent is False and workspace.kind == "scratch"
    assert Path(workspace.root.display) == root
    assert workspaces.scratch_root(data_dir) == root  # the second launch reuses it


@windows_only
def test_scratch_root_refuses_a_link_in_its_chain(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    data_dir = tmp_path / "data"
    (data_dir / "scratch").mkdir(parents=True)
    make_junction(data_dir / "scratch" / "chat", tmp_path)
    with pytest.raises(StateError) as failure:
        workspaces.scratch_root(data_dir)
    assert failure.value.code == "unsafe_path"


# --- identity, aliases and overlap --------------------------------------------------------


@windows_only
def test_identity_dedupes_aliases_refuses_case_collisions_and_reattaches_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"
    root = tmp_path / "vault"
    root.mkdir()
    service = service_at(tmp_path)
    first, created = service.register_existing(COMMAND, str(root))
    assert created is True and first.policy == "existing_readonly"

    link = tmp_path / "alias"
    make_junction(link, root)
    again, created_again = service.register_existing(OTHER_COMMAND, str(link))
    assert (again.workspace_id, created_again) == (first.workspace_id, False)
    assert len(service.list()) == 1

    # An equal key with a different identity is a per-directory case difference, refused.
    monkeypatch.setattr(os, "stat", fake_stat({"st_ino": 7}))
    with pytest.raises(WorkspaceError) as collision:
        service.register_existing(OTHER_COMMAND, str(root))
    assert collision.value.code == "case_collision"
    assert collision.value.extra["display"] == str(root)
    monkeypatch.undo()

    moved = tmp_path / "renamed"
    root.rename(moved)
    reattached, created_move = service.register_existing(OTHER_COMMAND, str(moved))
    assert (reattached.workspace_id, created_move) == (first.workspace_id, False)
    assert reattached.root.display == str(moved)
    assert reattached.revision == first.revision + 1
    assert len(service.list()) == 1


def test_reattach_relabels_a_default_named_folder_to_its_new_basename(tmp_path: Path) -> None:
    """Settings > Workspaces re-adding a renamed code folder reattaches it by physical id
    (`_evaluate`'s criterion 1), through `_reattach`. A name the user never customised — still
    the old folder's own basename — must follow the move to the new basename, the same way a
    fresh registration derives one."""
    service = service_at(tmp_path)
    before = tmp_path / "project-dev"
    before.mkdir()
    workspace, _ = service.register_existing(COMMAND, str(before), kind="code")
    assert workspace.name == "project-dev"

    renamed = tmp_path / "project-renamed"
    before.rename(renamed)
    reattached, created = service.register_existing(OTHER_COMMAND, str(renamed), kind="code")

    assert created is False and reattached.workspace_id == workspace.workspace_id
    assert reattached.name == "project-renamed"


def test_reattach_keeps_a_name_the_owner_set(tmp_path: Path) -> None:
    """The other half of the relabel: a name that is not the folder's own old basename (the user
    typed one in) is untouched by the same move."""
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    before = tmp_path / "project-dev"
    before.mkdir()
    workspace, _ = service.register_existing(COMMAND, str(before), kind="code")

    def custom_name(current: Any, rows: Any) -> Any:
        return {**current, "name": "My dev copy"}

    store.update_workspace(workspace.workspace_id, custom_name)

    renamed = tmp_path / "project-renamed"
    before.rename(renamed)
    reattached, _ = service.register_existing(OTHER_COMMAND, str(renamed), kind="code")

    assert reattached.name == "My dev copy"


def test_register_created_reserves_a_destination_once_per_command(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    destination = tmp_path / "vaults" / "New"
    root = CanonicalRoot(
        key=os.path.normcase(str(destination)),
        display=str(destination),
        physical_id=f"pending:{COMMAND}",
        aliased=False,
    )
    first = service.register_created(COMMAND, root, "New", portable_id=None)
    assert first.bootstrap == "pending" and first.policy == "ordinary"
    again = service.register_created(COMMAND, root, "New", portable_id=None)
    assert again.workspace_id == first.workspace_id
    assert len(service.list()) == 1
    with pytest.raises(WorkspaceError) as taken:
        service.register_created(OTHER_COMMAND, root, "New", portable_id=None)
    assert taken.value.code == "destination_not_empty"
    assert taken.value.extra == {
        "hint": "cancel_bootstrap",
        "workspace_id": first.workspace_id,
    }


def write_lf(path: Path, text: str) -> None:
    """Fixture bytes are the fixture's business: an instruction head is read byte for byte."""
    with path.open("w", encoding="utf-8", newline="\n") as writer:
        writer.write(text)


def test_the_identity_document_stays_inside_the_column_bound() -> None:
    crowded = RootIdentity(
        git_root_oids=("a" * 40,),
        git_common_dir="c:\\" + "d" * 5000,
        instruction_hashes={"CLAUDE.md": "b" * 64},
        established=True,
        inherits_from=None,
        probed_at="2026-09-17T00:00:00.000Z",
    )
    text = workspaces._identity_json(crowded)
    assert len(text.encode("utf-8")) <= workspaces.MAX_IDENTITY_BYTES
    document = json.loads(text)
    assert document["git_common_dir"] is None  # the one unbounded value is what gives way
    assert document["git_root_oids"] == ["a" * 40]
    assert document["instruction_hashes"] == {"CLAUDE.md": "b" * 64}


def test_a_move_into_a_protected_root_is_refused_not_reattached(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "vault"
    outside.mkdir(parents=True)
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    service = service_at(tmp_path)
    moving, _ = service.register_existing(COMMAND, str(outside))
    protected_id = protected_at(service, guarded)

    outside.rename(guarded / "vault")  # same volume: the file index, and criterion 1, survive
    with pytest.raises(WorkspaceError) as refused:
        service.register_existing(OTHER_COMMAND, str(guarded / "vault"))
    assert refused.value.code == "protected_overlap"
    assert refused.value.extra["workspace_id"] == protected_id
    kept = service.get(moving.workspace_id)
    assert kept.root.key == moving.root.key and kept.policy == "existing_readonly"
    assert kept.revision == moving.revision

    # A move onto another registration's key is a case collision, not a silent takeover.
    other = tmp_path / "other"
    other.mkdir()
    taken, _ = service.register_existing(str(uuid.uuid4()), str(other))
    (guarded / "vault").rename(tmp_path / "outside" / "vault")
    other.rename(tmp_path / "other-moved")
    (tmp_path / "outside" / "vault").rename(other)
    with pytest.raises(WorkspaceError) as collision:
        service.register_existing(str(uuid.uuid4()), str(other))
    assert collision.value.code == "case_collision"
    assert service.get(taken.workspace_id).root.key == taken.root.key


def test_an_unwritable_store_still_answers_the_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    governed = tmp_path / "governed"
    governed.mkdir()
    write_lf(governed / "CLAUDE.md", "# first\n")
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    workspace, _ = service.register_existing(COMMAND, str(governed))
    recorded = workspace.identity.instruction_hashes["CLAUDE.md"]

    write_lf(governed / "CLAUDE.md", "# second\n")

    def busy(*args: Any, **kwargs: Any) -> None:
        raise StateError("busy")

    monkeypatch.setattr(store, "update_workspace", busy)
    inspection = service.inspect_policy(workspace.workspace_id)
    assert inspection.policy == "existing_readonly"
    assert inspection.root_available is True
    assert inspection.instructions[0].sha256 == stale_digest(governed / "CLAUDE.md")
    monkeypatch.undo()
    # The row keeps what it had; the refresh is an observation the store may refuse to take.
    assert service.get(workspace.workspace_id).identity.instruction_hashes == {
        "CLAUDE.md": recorded
    }


def test_code_editor_refusals_match_the_owner_constants_and_refs_admit_instruction_names() -> None:
    expected = (
        *(f"{name}/" for name in sorted(EXCLUDED_DIRS)),
        *sorted(VAULT_PRIVATE_NAMES),
        ".env.*",
        *sorted(CODE_PRIVATE_NAMES),
        *(f"*{suffix}" for suffix in PRIVATE_SUFFIXES),
        f"{STAGE_PREFIX}*",
        f"{BACKUP_PREFIX}*",
    )
    assert expected == workspaces.CODE_EDITOR_REFUSES
    assert len(workspaces.CODE_EDITOR_REFUSES) == len(set(workspaces.CODE_EDITOR_REFUSES))
    assert code_ref("CLAUDE.md") == ("CLAUDE.md",)
    assert code_ref("AGENTS.md") == ("AGENTS.md",)


def test_inspect_policy_uses_code_editor_refusals_for_code_folders(tmp_path: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root), kind="code")
    assert service.inspect_policy(workspace.workspace_id).excluded_from_writes == list(
        workspaces.CODE_EDITOR_REFUSES
    )


def test_inspection_refreshes_the_recorded_identity(tmp_path: Path) -> None:
    governed = tmp_path / "governed"
    governed.mkdir()
    write_lf(governed / "CLAUDE.md", "# first\n")
    service = service_at(tmp_path)
    protected_id = protected_at(service, governed)
    assert service.get(protected_id).identity.instruction_hashes["CLAUDE.md"]

    write_lf(governed / "CLAUDE.md", "# second\n")
    stale = tmp_path / "copy-before"
    stale.mkdir()
    write_lf(stale / "CLAUDE.md", "# second\n")
    unbound, _ = service.register_existing(str(uuid.uuid4()), str(stale))
    assert unbound.policy == "existing_readonly"  # the row still records the old hash

    service.inspect_policy(protected_id)
    refreshed = service.get(protected_id)
    assert refreshed.identity.instruction_hashes == {
        "CLAUDE.md": stale_digest(governed / "CLAUDE.md")
    }
    assert refreshed.revision == service.get(protected_id).revision  # an observation, not a change

    fresh = tmp_path / "copy-after"
    fresh.mkdir()
    write_lf(fresh / "CLAUDE.md", "# second\n")
    bound, created = service.register_existing(str(uuid.uuid4()), str(fresh))
    assert (bound.policy, created) == ("protected", True)
    assert bound.identity.inherits_from == protected_id


def stale_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_root_reports_unavailable_and_keeps_registration(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root))
    assert service.current_root(workspace) is not None
    root.rename(tmp_path / "moved-away")
    assert service.current_root(workspace) is None
    assert [row.workspace_id for row in service.list()] == [workspace.workspace_id]
    assert service.inspect_policy(workspace.workspace_id).root_available is False


def test_data_dir_containment(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "faces").mkdir(parents=True)
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root))
    assert service.contains_data_dir(tmp_path / "data") is None
    inside = service.contains_data_dir(root / "app-data")
    assert inside is not None and inside.workspace_id == workspace.workspace_id
    assert service.contains_data_dir(root) is not None


def test_overlapping_registrations_are_refused(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    parent = tree / "vault"
    child = parent / "inner"
    child.mkdir(parents=True)
    service = service_at(tmp_path)
    registered, _ = service.register_existing(COMMAND, str(parent))
    for raw in (str(child), str(tree)):
        with pytest.raises(WorkspaceError) as failure:
            service.register_existing(OTHER_COMMAND, raw)
        assert failure.value.code == "overlaps_registration", raw
        assert failure.value.extra["workspace_id"] == registered.workspace_id
    assert len(service.list()) == 1


# --- policy -------------------------------------------------------------------------------


def seed_instructions(root: Path) -> None:
    (root / "CLAUDE.md").write_text("# rules\nsynthetic instructions\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": []}}', encoding="utf-8"
    )
    (root / ".git").mkdir()


def test_existing_root_admission_requires_acknowledgements(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    seed_instructions(root)
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root))
    inspection = service.inspect_policy(workspace.workspace_id)
    assert inspection.policy == "existing_readonly"
    assert inspection.markers == {
        "claude_md": True,
        "agents_md": False,
        "claude_dir": True,
        "hooks": True,
        "git": True,
        "viki_manifest": False,
    }
    assert inspection.acknowledgements_required == [
        "instructions_present",
        "hooks_present",
        "git_present",
    ]
    assert inspection.excluded_from_writes == ["CLAUDE.md", ".claude/", ".git/"]
    assert inspection.admissible == {"ordinary": True, "protected": True, "reason": None}

    with pytest.raises(WorkspaceError) as missing:
        service.set_policy(COMMAND, workspace.workspace_id, 1, "ordinary", ["git_present"])
    assert missing.value.code == "acknowledgement_required"
    assert missing.value.extra["required"] == ["instructions_present", "hooks_present"]

    with pytest.raises(WorkspaceError) as stale:
        service.set_policy(
            COMMAND, workspace.workspace_id, 9, "ordinary", inspection.acknowledgements_required
        )
    assert stale.value.code == "stale_revision"

    admitted = service.set_policy(
        COMMAND, workspace.workspace_id, 1, "ordinary", inspection.acknowledgements_required
    )
    assert (admitted.policy, admitted.policy_revision) == ("ordinary", 2)
    assert admitted.revision == workspace.revision + 1
    relocked = service.set_policy(COMMAND, workspace.workspace_id, 2, "existing_readonly", [])
    assert (relocked.policy, relocked.policy_revision) == ("existing_readonly", 3)


def test_a_settings_json_nested_too_deep_reports_no_hooks(tmp_path: Path) -> None:
    """json.loads raises RecursionError, not ValueError, on JSON nested this deep. `_has_hooks`
    must read that the same way it reads any other unparseable settings.json: no hooks found,
    never a 500 from the policy inspection."""
    root = tmp_path / "vault"
    root.mkdir()
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("[" * 200_000, encoding="utf-8")
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root))
    inspection = service.inspect_policy(workspace.workspace_id)
    assert inspection.markers["hooks"] is False


def test_instruction_head_is_null_when_previews_are_hidden(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    write_lf(root / "CLAUDE.md", "# synthetic\n")
    store = store_at(tmp_path)
    shown = service_at(tmp_path, store)
    workspace, _ = shown.register_existing(COMMAND, str(root))
    (instruction,) = shown.inspect_policy(workspace.workspace_id).instructions
    assert instruction.path == "CLAUDE.md" and instruction.head == "# synthetic\n"
    assert instruction.bytes == 12 and instruction.too_large is False
    assert instruction.sha256 is not None and len(instruction.sha256) == 64
    hidden = service_at(tmp_path, store, hide_previews=True)
    (quiet,) = hidden.inspect_policy(workspace.workspace_id).instructions
    assert quiet.head is None and quiet.sha256 == instruction.sha256

    # A file too large to hash is still reported as present, with no digest and no preview.
    write_lf(root / "AGENTS.md", "x" * (workspaces.MAX_INSTRUCTION_BYTES + 1))
    listed = {item.path: item for item in shown.inspect_policy(workspace.workspace_id).instructions}
    assert listed["AGENTS.md"].too_large is True
    assert listed["AGENTS.md"].sha256 is None
    assert listed["AGENTS.md"].bytes == workspaces.MAX_INSTRUCTION_BYTES + 1
    # too_large means "not hashed", not "not shown": the head is still the first 4 KiB.
    assert listed["AGENTS.md"].head == "x" * workspaces.HEAD_BYTES

    # The bound is bytes, not characters: 4,096 Japanese characters are 12,288 bytes.
    write_lf(root / "CLAUDE.md", "あ" * workspaces.HEAD_BYTES)
    (japanese,) = [
        item
        for item in shown.inspect_policy(workspace.workspace_id).instructions
        if item.path == "CLAUDE.md"
    ]
    assert japanese.head is not None
    # A character read would quote all 4,096 of them; a byte read stops after about 1,365
    # (the cut codepoint becomes one replacement character).
    assert 1 <= len(japanese.head) <= workspaces.HEAD_BYTES // 3 + 1
    assert japanese.head.startswith("ああ")
    # and it never enters the identity, where a null digest would match every other one
    assert "AGENTS.md" not in shown.get(workspace.workspace_id).identity.instruction_hashes


def test_policy_refuses_an_unavailable_root_and_an_unknown_transition(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root))
    with pytest.raises(WorkspaceError) as same:
        service.set_policy(COMMAND, workspace.workspace_id, 1, "existing_readonly", [])
    assert same.value.code == "invalid_transition"
    root.rename(tmp_path / "gone")
    with pytest.raises(WorkspaceError) as unavailable:
        service.set_policy(COMMAND, workspace.workspace_id, 1, "protected", [])
    assert unavailable.value.code == "root_unavailable"


# --- protection -----------------------------------------------------------------------------


def protected_at(service: WorkspaceService, root: Path) -> str:
    workspace, _ = service.register_existing(str(uuid.uuid4()), str(root))
    upgraded = service.set_policy(str(uuid.uuid4()), workspace.workspace_id, 1, "protected", [])
    assert upgraded.policy == "protected"
    return upgraded.workspace_id


def test_bind_protected_commits_the_row_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is protected in the commit that creates it, so no commit ever holds it
    `existing_readonly`, a policy the user could later make writable. It is the row the
    two-step path (register, then `set_policy`) leaves: protected at policy revision 2."""
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    copy, plain = tmp_path / "copy", tmp_path / "plain"
    copy.mkdir()
    plain.mkdir()
    write = store.update_workspace
    commits: list[list[tuple[str, int]]] = []

    def recorded(workspace_id: str, changed: Any) -> Any:
        row = write(workspace_id, changed)
        commits.append([(r["policy"], r["policy_revision"]) for r in store.list_workspaces()])
        return row

    monkeypatch.setattr(store, "update_workspace", recorded)
    workspace, created = service.register_existing(COMMAND, str(copy), bind_protected=True)
    assert created and commits == [[("protected", 2)]]
    assert (workspace.policy, workspace.policy_revision, workspace.source) == (
        "protected",
        2,
        "opened",
    )
    with pytest.raises(WorkspaceError) as one_way:
        service.set_policy(OTHER_COMMAND, workspace.workspace_id, 2, "ordinary", [])
    assert one_way.value.code == "protected_root"

    # A registration that already exists is returned as it is; only a new row is bound.
    existing, _ = service.register_existing(COMMAND, str(plain))
    again, created_again = service.register_existing(OTHER_COMMAND, str(plain), bind_protected=True)
    assert (again, created_again) == (existing, False)
    assert again.policy == "existing_readonly"


def test_a_failure_while_binding_protected_leaves_no_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The protected transition runs inside the registration's transaction: when it fails, the
    registration rolls back with it."""
    service = service_at(tmp_path)
    root = tmp_path / "copy"
    root.mkdir()

    def killed(*_args: Any) -> Any:
        raise RuntimeError("process killed")

    monkeypatch.setattr(workspaces, "_with_policy", killed)
    with pytest.raises(RuntimeError):
        service.register_existing(COMMAND, str(root), bind_protected=True)
    assert service.list() == []


def test_register_existing_cannot_bind_ordinary(tmp_path: Path) -> None:
    """The one binding the keyword offers is protected: no argument names a policy."""
    params = inspect.signature(WorkspaceService.register_existing).parameters
    assert list(params) == ["self", "command_id", "raw_path", "bind_protected", "kind"]
    assert params["bind_protected"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["kind"].kind is inspect.Parameter.KEYWORD_ONLY
    service = service_at(tmp_path)
    for name in ("asked", "plain", "bound"):
        (tmp_path / name).mkdir()
    ordinary: dict[str, Any] = {"policy": "ordinary"}
    with pytest.raises(TypeError):
        service.register_existing(COMMAND, str(tmp_path / "asked"), **ordinary)
    assert service.list() == []
    plain, _ = service.register_existing(COMMAND, str(tmp_path / "plain"), bind_protected=False)
    bound, _ = service.register_existing(
        OTHER_COMMAND, str(tmp_path / "bound"), bind_protected=True
    )
    assert (plain.policy, bound.policy) == ("existing_readonly", "protected")


@windows_only
def test_protected_binding_survives_every_downgrade_path(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    root = tmp_path / "VIKI"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    service = service_at(tmp_path)
    protected_id = protected_at(service, root)

    link = tmp_path / "alias"
    make_junction(link, root)
    for raw in (str(root), str(link), str(root).upper(), short_path(root)):
        workspace, created = service.register_existing(str(uuid.uuid4()), raw)
        assert (workspace.workspace_id, workspace.policy, created) == (
            protected_id,
            "protected",
            False,
        )
    assert service.inspect_policy(protected_id).admissible == {
        "ordinary": False,
        "protected": False,  # already protected: there is nothing left to set
        "reason": "protected_root",
    }
    with pytest.raises(WorkspaceError) as patched:
        service.set_policy(COMMAND, protected_id, 2, "ordinary", [])
    assert patched.value.code == "protected_root"
    assert service.get(protected_id).policy == "protected"

    # A copy carrying the same instruction hash is bound protected, never ordinary.
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    inherited, created_copy = service.register_existing(str(uuid.uuid4()), str(copy))
    assert (inherited.policy, created_copy) == ("protected", True)
    assert inherited.identity.inherits_from == protected_id
    assert service.get(protected_id).policy == "protected"


def test_identity_match_binds_protected_and_overlap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rails, "_has_git_scope_override", lambda: False)
    origin = tmp_path / "origin"
    origin.mkdir()
    try:
        winproc.git(origin, "init")
        (origin / "note.md").write_text("synthetic\n", encoding="utf-8")
        winproc.git(origin, "add", "note.md")
        winproc.git(
            origin,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "synthetic",
        )
        winproc.git(tmp_path, "clone", str(origin), str(tmp_path / "clone"))
    except Exception:
        pytest.skip("git is not available for the identity probe")
    service = service_at(tmp_path, git=True)
    protected_id = protected_at(service, origin)
    assert service.get(protected_id).identity.git_root_oids

    clone, created = service.register_existing(str(uuid.uuid4()), str(tmp_path / "clone"))
    assert (clone.policy, created) == ("protected", True)
    assert clone.identity.inherits_from == protected_id

    (origin / "inner").mkdir()
    for raw in (str(tmp_path), str(origin / "inner")):
        with pytest.raises(WorkspaceError) as overlap:
            service.register_existing(str(uuid.uuid4()), raw)
        assert overlap.value.code in ("protected_overlap", "denied_location")
    assert {row.workspace_id for row in service.list()} == {protected_id, clone.workspace_id}


# --- the environment override ---------------------------------------------------------------


@windows_only
def test_environment_override_runs_protected_precedence(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    root = tmp_path / "VIKI"
    root.mkdir()
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    protected_id = protected_at(service, root)
    link = tmp_path / "alias"
    make_junction(link, root)

    through_alias = service_at(tmp_path, store, env={"VIKI_VAULT": str(link)})
    active = through_alias.resolve_active()
    assert active.workspace.workspace_id == protected_id
    assert active.workspace.policy == "protected"
    assert active.selected_by == "environment" and active.chat_only is False
    assert active.cwd == Path(active.workspace.root.display)

    other = tmp_path / "other"
    other.mkdir()
    ephemeral = service_at(tmp_path, store, env={"VIKI_VAULT": str(other)}).resolve_active()
    assert ephemeral.workspace.persistent is False
    assert ephemeral.workspace.policy == "existing_readonly"
    assert ephemeral.workspace.source == "environment"
    assert len(service.list()) == 1  # the override is never persisted

    inside = root / "inner"
    inside.mkdir()
    refused = service_at(tmp_path, store, env={"VIKI_VAULT": str(inside)}).resolve_active()
    assert refused.workspace.kind == "scratch" and refused.chat_only is True


def test_environment_override_id_is_pinned_to_its_persisted_prefix(tmp_path: Path) -> None:
    """The id of an environment-override workspace is read back by value (draft scoping keys on
    it), never looked up, so the formula is pinned exactly, its fixed prefix included."""
    root = tmp_path / "vault"
    root.mkdir()
    active = service_at(tmp_path, env={"VIKI_VAULT": str(root)}).resolve_active()
    expected = str(
        # persisted: the fixed prefix in this id's formula, pinned exactly.
        uuid.uuid5(uuid.NAMESPACE_URL, f"viki-agent:environment:{workspaces.root_key(root)}")
    )
    assert active.workspace.workspace_id == expected


def test_an_unusable_override_falls_back_to_the_selection_then_to_scratch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    workspace, _ = service.register_existing(COMMAND, str(root))
    service.select(COMMAND, workspace.workspace_id)
    missing = service_at(tmp_path, store, env={"VIKI_VAULT": str(tmp_path / "absent")})
    active = missing.resolve_active()
    assert active.workspace.workspace_id == workspace.workspace_id
    assert active.selected_by == "registration"

    root.rename(tmp_path / "gone")
    fallen = service_at(tmp_path, store).resolve_active()
    assert fallen.workspace.kind == "scratch"
    assert fallen.selected_id == workspace.workspace_id  # the registration is kept
    assert fallen.selected_by == "builtin" and fallen.chat_only is True


def test_selection_is_recorded_and_the_launch_keeps_its_workspace(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    one, _ = service.register_existing(COMMAND, str(first))
    two, _ = service.register_existing(OTHER_COMMAND, str(second))
    service.select(COMMAND, one.workspace_id)
    assert service.resolve_active().workspace.workspace_id == one.workspace_id
    service.select(OTHER_COMMAND, two.workspace_id)
    assert service.selected_id() == two.workspace_id
    assert service.resolve_active().workspace.workspace_id == one.workspace_id
    assert service_at(tmp_path, store).resolve_active().workspace.workspace_id == two.workspace_id
    with pytest.raises(WorkspaceError) as unknown:
        service.select(COMMAND, str(uuid.uuid4()))
    assert unknown.value.code == "unknown_workspace"
