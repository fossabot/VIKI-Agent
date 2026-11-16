"""Vault bootstrap in `tmp_path`: what it writes, what it resumes and what it can undo."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import viki_agent
from tests.app.test_workspaces import COMMAND, OTHER_COMMAND, make_junction, service_at, store_at
from viki_agent import vault_storage
from viki_agent.vault_storage import MANIFEST, VaultBootstrap, folder_name, suggested_destination
from viki_agent.workspaces import WorkspaceError, WorkspaceService

RESOURCES = Path(viki_agent.__file__).parent / "resources"


def bootstrap_at(tmp_path: Path, service: WorkspaceService | None = None) -> VaultBootstrap:
    return VaultBootstrap(
        service if service is not None else service_at(tmp_path),
        resources=RESOURCES,
        home=tmp_path / "home",
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_folder_name_and_suggestion_are_bounded(tmp_path: Path) -> None:
    assert folder_name("  My Vault  ") == "My Vault"
    assert folder_name("ábc") == "ábc"  # NFC
    assert folder_name("bad:name/with\\slashes") == "badnamewithslashes"
    assert folder_name("trailing dot.") == "trailing dot"
    for refused in ("", "   ", ".", "\x01\x02", "COM1", "x" * 65, "..."):
        with pytest.raises(WorkspaceError) as failure:
            folder_name(refused)
        assert failure.value.code == "invalid_name", refused
    parent, folder = suggested_destination(tmp_path / "home", "My Vault")
    assert parent == tmp_path / "home" / "VIKI" / "Vaults" and folder == "My Vault"


def test_create_writes_only_the_welcome_note_and_manifest(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    workspace, created, files = bootstrap.create(COMMAND, "My Vault", None, None, "en", True)
    assert created is True and files == ["Welcome.md", MANIFEST]
    destination = Path(workspace.root.display)
    assert destination == tmp_path / "home" / "VIKI" / "Vaults" / "My Vault"
    assert sorted(path.name for path in destination.iterdir()) == [".viki", "Welcome.md"]
    welcome = (destination / "Welcome.md").read_bytes()
    assert b"\r\n" not in welcome and not welcome.startswith(b"\xef\xbb\xbf")
    assert welcome == (RESOURCES / "welcome.en.md").read_text(encoding="utf-8").encode("utf-8")
    assert (workspace.policy, workspace.layout, workspace.bootstrap) == (
        "ordinary",
        "unknown",
        "complete",
    )
    assert workspace.source == "created" and workspace.portable_id is not None
    # Creation installs no hooks and copies no instruction file.
    assert not (destination / ".claude").exists() and not (destination / "CLAUDE.md").exists()
    assert service.get(workspace.workspace_id).root.physical_id == workspace.root.physical_id


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits; Windows folders get a DACL instead"
)
@pytest.mark.usefixtures("desktop_umask")
def test_bootstrap_creates_every_new_folder_and_file_private(tmp_path: Path) -> None:
    """Under a desktop umask, every folder the app creates is 0700 and every file 0600.

    An empty destination the user already had keeps whatever mode it already had.
    """
    assert sys.platform != "win32"
    parents = tmp_path / "parents"
    parents.mkdir()
    existing = parents / "Existing"
    existing.mkdir(mode=0o751)
    before_mode = stat.S_IMODE(os.lstat(existing).st_mode)

    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    workspace, created, _ = bootstrap.create(COMMAND, "Existing", str(parents), None, "en", True)
    assert created is True and Path(workspace.root.display) == existing
    assert stat.S_IMODE(os.lstat(existing).st_mode) == before_mode

    other, created_other, _ = bootstrap.create(OTHER_COMMAND, "My Vault", None, None, "en", True)
    assert created_other is True
    destination = Path(other.root.display)
    assert destination == tmp_path / "home" / "VIKI" / "Vaults" / "My Vault"
    for folder in (
        tmp_path / "home",
        tmp_path / "home" / "VIKI",
        tmp_path / "home" / "VIKI" / "Vaults",
        destination,
        destination / ".viki",
    ):
        assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700, folder
    assert stat.S_IMODE(os.lstat(destination / "Welcome.md").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(destination / ".viki" / "vault.json").st_mode) == 0o600


def test_manifest_keys_are_exactly_schema_id_name_created_at(tmp_path: Path) -> None:
    workspace, _, _ = bootstrap_at(tmp_path).create(COMMAND, "Vault", None, None, "en", True)
    document = json.loads(
        (Path(workspace.root.display) / ".viki" / "vault.json").read_text(encoding="utf-8")
    )
    assert set(document) == {"schema", "vault_id", "name", "created_at"}
    assert document["schema"] == 1 and document["vault_id"] == workspace.portable_id
    assert document["name"] == "Vault"
    assert not any(isinstance(value, str) and ":\\" in value for value in document.values())


def test_create_refuses_nonempty_and_open_changes_no_bytes(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    parents = tmp_path / "parents"
    existing = parents / "existing"
    existing.mkdir(parents=True)
    (existing / "note.md").write_text("owner's note\n", encoding="utf-8")
    (existing / "CLAUDE.md").write_text("# owner's rules\n", encoding="utf-8")
    before = snapshot(existing)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "existing", str(parents), None, "en", False)
    assert refused.value.code == "destination_not_empty"
    assert refused.value.extra["hint"] == "open_existing"

    workspace, created = service.register_existing(OTHER_COMMAND, str(existing))
    assert created is True and workspace.policy == "existing_readonly"
    assert snapshot(existing) == before
    assert sorted(path.name for path in existing.iterdir()) == ["CLAUDE.md", "note.md"]

    # An empty folder is an acceptable destination; it is the only pre-existing one.
    empty = parents / "empty"
    empty.mkdir()
    made, _, files = bootstrap.create(COMMAND, "empty", str(parents), None, "en", False)
    assert Path(made.root.display) == empty and files == ["Welcome.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="dangling symlinks are created here on POSIX")
def test_a_dangling_suggested_parent_is_unavailable_not_missing(tmp_path: Path) -> None:
    """A suggested parent that is a dangling link (an unmounted volume, for example) must answer
    `unavailable`, the same as any other mkdir failure - never `not_found`, which is what a
    swallowed EEXIST would let `resolve(strict=True)` answer instead."""
    assert sys.platform != "win32"
    home = tmp_path / "home"
    (home / "VIKI").mkdir(parents=True)
    os.symlink(home / "VIKI" / "missing-target", home / "VIKI" / "Vaults")
    bootstrap = bootstrap_at(tmp_path)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "My Vault", None, None, "en", False)
    assert refused.value.code == "unavailable"


def test_a_destination_created_by_another_writer_is_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that lands its own folder, with its own note in it, between our emptiness check
    and our own mkdir must be refused, not adopted; the reservation stays pending for a resume
    or a cancel, exactly as it did before the destination's mkdir learned to swallow EEXIST."""
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    parents = tmp_path / "parents"
    parents.mkdir()
    destination = parents / "Raced"
    real_mkdir = vault_storage._mkdir

    def racing_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == destination:
            real_mkdir(path, *args, **kwargs)  # the other writer's own folder lands first
            (destination / "their-note.md").write_text("not ours\n", encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "another writer got there first")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(vault_storage, "_mkdir", racing_mkdir)
    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "Raced", str(parents), None, "en", False)
    assert refused.value.code == "unavailable"
    assert (destination / "their-note.md").is_file()
    assert not (destination / "Welcome.md").exists()
    pending = service.by_command(COMMAND)
    assert pending is not None and pending.bootstrap == "pending"


def test_bootstrap_replay_and_resume_after_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    first = bootstrap_at(tmp_path, service_at(tmp_path, store))
    destination = tmp_path / "home" / "VIKI" / "Vaults" / "Vault"

    # A process that dies before `mkdir` leaves the reservation and no folder.
    monkeypatch.setattr(vault_storage, "_mkdir", _dying_mkdir(destination))
    with pytest.raises(WorkspaceError):
        first.create(COMMAND, "Vault", None, None, "en", True)
    monkeypatch.undo()
    assert not destination.exists()
    pending = service_at(tmp_path, store).by_command(COMMAND)
    assert pending is not None and pending.bootstrap == "pending"
    assert pending.root.physical_id == f"pending:{COMMAND}"

    # A process that dies between the folder and the manifest leaves both resumable.
    second = bootstrap_at(tmp_path, service_at(tmp_path, store))
    monkeypatch.setattr(vault_storage, "_dumps", _dying_manifest())
    with pytest.raises(WorkspaceError):
        second.create(COMMAND, "Vault", None, None, "en", True)
    monkeypatch.undo()
    assert (destination / "Welcome.md").is_file()
    assert not (destination / ".viki" / "vault.json").exists()

    # What is already on disk is the user's now; the resume adds, it never rewrites.
    edited = (destination / "Welcome.md").read_text(encoding="utf-8") + "own note\n"
    with (destination / "Welcome.md").open("w", encoding="utf-8", newline="\n") as writer:
        writer.write(edited)

    third = bootstrap_at(tmp_path, service_at(tmp_path, store))
    workspace, created, files = third.create(COMMAND, "Vault", None, None, "en", True)
    assert created is True and files == ["Welcome.md", MANIFEST]
    assert workspace.bootstrap == "complete"
    assert (destination / "Welcome.md").read_text(encoding="utf-8") == edited
    assert workspace.workspace_id == pending.workspace_id

    replayed, created_again, replay_files = third.create(COMMAND, "Vault", None, None, "en", True)
    assert created_again is False and replay_files == files
    assert replayed.workspace_id == workspace.workspace_id
    assert len(service_at(tmp_path, store).list()) == 1


def _dying_manifest() -> Any:
    """Die exactly where the manifest would be serialised, after the welcome note landed."""
    real = vault_storage._dumps

    def call(payload: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(payload, dict) and "schema" in payload:
            raise OSError("synthetic process death")
        return real(payload, *args, **kwargs)

    return call


def _dying_mkdir(target: Path) -> Any:
    """Die at exactly the destination's `mkdir`, the way a killed process would."""
    real = vault_storage._mkdir

    def call(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == target:
            raise OSError("synthetic process death")
        return real(path, *args, **kwargs)

    return call


def test_pending_destination_is_a_stale_bootstrap_not_a_case_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service_at(tmp_path, store))
    destination = tmp_path / "home" / "VIKI" / "Vaults" / "Vault"
    monkeypatch.setattr(vault_storage, "_mkdir", _dying_mkdir(destination))
    with pytest.raises(WorkspaceError):
        bootstrap.create(COMMAND, "Vault", None, None, "en", False)
    monkeypatch.undo()
    assert not destination.exists()
    pending = service_at(tmp_path, store).by_command(COMMAND)
    assert pending is not None

    # Another window, a fresh command: the reservation is reported, never a case collision.
    other = bootstrap_at(tmp_path, service_at(tmp_path, store))
    with pytest.raises(WorkspaceError) as stale:
        other.create(OTHER_COMMAND, "Vault", None, None, "en", False)
    assert stale.value.code == "destination_not_empty"
    assert stale.value.extra == {
        "hint": "cancel_bootstrap",
        "workspace_id": pending.workspace_id,
    }

    # Cancelling works although the folder was never created.
    other.cancel(OTHER_COMMAND, pending.workspace_id)
    assert service_at(tmp_path, store).list() == []
    made, created, _ = other.create(OTHER_COMMAND, "Vault", None, None, "en", False)
    assert created is True and Path(made.root.display) == destination


def test_cancel_removes_only_app_created_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    bootstrap = bootstrap_at(tmp_path, service)
    monkeypatch.setattr(vault_storage, "_dumps", _dying_manifest())
    with pytest.raises(WorkspaceError):
        bootstrap.create(COMMAND, "Vault", None, None, "en", True)
    monkeypatch.undo()
    pending = service_at(tmp_path, store).by_command(COMMAND)
    assert pending is not None
    destination = Path(pending.root.display)

    owned = destination / "my-note.md"
    owned.write_text("the owner's own file\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as refused:
        bootstrap.cancel(OTHER_COMMAND, pending.workspace_id)
    assert refused.value.code == "not_app_created"
    assert owned.is_file() and (destination / "Welcome.md").is_file()
    assert service_at(tmp_path, store).by_command(COMMAND) is not None

    owned.unlink()
    bootstrap.cancel(OTHER_COMMAND, pending.workspace_id)
    assert list(destination.iterdir()) == []  # the folder stays; only our files went
    assert service_at(tmp_path, store).list() == []

    # A completed registration is not a bootstrap and is never undone by this route.
    workspace, _, _ = bootstrap.create(OTHER_COMMAND, "Vault", None, None, "en", False)
    with pytest.raises(WorkspaceError) as complete:
        bootstrap.cancel(OTHER_COMMAND, workspace.workspace_id)
    assert complete.value.code == "not_app_created"
    assert (Path(workspace.root.display) / "Welcome.md").is_file()


def test_the_suggested_parent_is_never_created_inside_a_registration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    governed = home / "VIKI"
    governed.mkdir(parents=True)
    (governed / "note.md").write_text("owner's note\n", encoding="utf-8")
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    workspace, _ = service.register_existing(OTHER_COMMAND, str(governed))
    service.set_policy(OTHER_COMMAND, workspace.workspace_id, 1, "protected", [])
    before = snapshot(governed)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "My Vault", None, None, "en", False)
    assert refused.value.code == "protected_overlap"
    assert refused.value.extra["workspace_id"] == workspace.workspace_id
    assert not (governed / "Vaults").exists()
    assert snapshot(governed) == before
    assert service.by_command(COMMAND) is None


@pytest.mark.skipif(sys.platform != "win32", reason="mklink /J is a Windows junction")
def test_a_junctioned_home_is_judged_by_where_it_lands(tmp_path: Path) -> None:
    real = tmp_path / "real"
    governed = real / "VIKI"
    governed.mkdir(parents=True)
    link = tmp_path / "linked-home"
    make_junction(link, real)
    service = service_at(tmp_path)
    bootstrap = VaultBootstrap(service, resources=RESOURCES, home=link)
    workspace, _ = service.register_existing(OTHER_COMMAND, str(governed))
    service.set_policy(OTHER_COMMAND, workspace.workspace_id, 1, "protected", [])

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "My Vault", None, None, "en", False)
    assert refused.value.code == "protected_overlap"
    assert not (governed / "Vaults").exists() and not (link / "VIKI" / "Vaults").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX twin of the junctioned-home test")
def test_a_symlinked_home_is_refused_as_unsafe_path(tmp_path: Path) -> None:
    """A symlinked home is a documented limit: it is refused, not judged by its target."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked-home"
    os.symlink(real, link, target_is_directory=True)
    service = service_at(tmp_path)
    bootstrap = VaultBootstrap(service, resources=RESOURCES, home=link)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "My Vault", None, None, "en", False)
    assert refused.value.code == "unsafe_path"
    assert not (real / "VIKI").exists()
    assert service.by_command(COMMAND) is None


def test_a_file_at_the_destination_is_a_typed_refusal(tmp_path: Path) -> None:
    parents = tmp_path / "parents"
    parents.mkdir()
    (parents / "Taken").write_text("a file where a folder would go\n", encoding="utf-8")
    bootstrap = bootstrap_at(tmp_path)
    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "Taken", str(parents), None, "en", False)
    assert refused.value.code == "destination_not_empty"
    assert refused.value.extra["hint"] == "open_existing"
    assert (parents / "Taken").read_text(encoding="utf-8").startswith("a file")


def test_open_on_a_pending_destination_reports_the_stale_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    bootstrap = bootstrap_at(tmp_path, service)
    monkeypatch.setattr(vault_storage, "_dumps", _dying_manifest())
    with pytest.raises(WorkspaceError):
        bootstrap.create(COMMAND, "Vault", None, None, "en", True)
    monkeypatch.undo()
    pending = service.by_command(COMMAND)
    assert pending is not None and pending.bootstrap == "pending"

    with pytest.raises(WorkspaceError) as stale:
        service.register_existing(OTHER_COMMAND, pending.root.display)
    assert stale.value.code == "destination_not_empty"
    assert stale.value.extra == {
        "hint": "cancel_bootstrap",
        "workspace_id": pending.workspace_id,
    }
    assert len(service.list()) == 1


def test_cancel_leaves_a_manifest_this_bootstrap_never_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    bootstrap = bootstrap_at(tmp_path, service)
    destination = tmp_path / "home" / "VIKI" / "Vaults" / "Vault"
    monkeypatch.setattr(vault_storage, "_mkdir", _dying_mkdir(destination))
    with pytest.raises(WorkspaceError):
        bootstrap.create(COMMAND, "Vault", None, None, "en", False)  # no portable metadata
    monkeypatch.undo()
    pending = service.by_command(COMMAND)
    assert pending is not None and pending.portable_id is None

    destination.mkdir(parents=True)
    (destination / ".viki").mkdir()
    (destination / ".viki" / "vault.json").write_text('{"schema": 1}', encoding="utf-8")
    with pytest.raises(WorkspaceError) as refused:
        bootstrap.cancel(OTHER_COMMAND, pending.workspace_id)
    assert refused.value.code == "not_app_created"
    assert (destination / ".viki" / "vault.json").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="mklink /J is a Windows junction")
def test_a_link_at_the_destination_leaf_is_refused(tmp_path: Path) -> None:
    """A junction at the leaf would send `Welcome.md` wherever it points.

    Its POSIX twin, `test_a_symlink_at_the_destination_leaf_is_refused`, proves the same
    property with a symlink and already runs off Windows.
    """
    guarded = tmp_path / "guarded"
    inside = guarded / "empty-folder"
    inside.mkdir(parents=True)
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    protected, _ = service.register_existing(OTHER_COMMAND, str(guarded))
    service.set_policy(OTHER_COMMAND, protected.workspace_id, 1, "protected", [])

    vaults = tmp_path / "home" / "VIKI" / "Vaults"
    vaults.mkdir(parents=True)
    make_junction(vaults / "Notes", inside)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "Notes", None, None, "en", True)
    assert refused.value.code == "unsafe_path"
    assert list(inside.iterdir()) == []  # nothing was written through the link
    assert [row.workspace_id for row in service.list()] == [protected.workspace_id]
    assert service.by_command(COMMAND) is None


def test_a_symlink_at_the_destination_leaf_is_refused(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    inside = guarded / "empty-folder"
    inside.mkdir(parents=True)
    vaults = tmp_path / "home" / "VIKI" / "Vaults"
    vaults.mkdir(parents=True)
    try:
        os.symlink(inside, vaults / "Notes", target_is_directory=True)
    except OSError:
        pytest.skip("this account may not create symbolic links")
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    protected, _ = service.register_existing(OTHER_COMMAND, str(guarded))
    service.set_policy(OTHER_COMMAND, protected.workspace_id, 1, "protected", [])

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "Notes", None, None, "en", False)
    assert refused.value.code == "unsafe_path"
    assert list(inside.iterdir()) == []
    assert service.by_command(COMMAND) is None


def test_a_destination_that_resolves_elsewhere_is_judged_again_and_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The completion gate itself: a created folder whose real key is inadmissible."""
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    protected, _ = service.register_existing(OTHER_COMMAND, str(guarded))
    service.set_policy(OTHER_COMMAND, protected.workspace_id, 1, "protected", [])

    destination = tmp_path / "home" / "VIKI" / "Vaults" / "Notes"
    inside = guarded / "resolved-here"
    inside.mkdir()
    real = service.canonicalise

    def resolves_into_the_protected_root(raw: str) -> Any:
        return real(str(inside)) if raw == str(destination) else real(raw)

    monkeypatch.setattr(service, "canonicalise", resolves_into_the_protected_root)
    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(COMMAND, "Notes", None, None, "en", True)
    assert refused.value.code == "protected_overlap"
    monkeypatch.undo()
    # The reservation and everything it wrote are gone; the protected row is the only one.
    assert service.by_command(COMMAND) is None
    assert [row.workspace_id for row in service.list()] == [protected.workspace_id]
    assert list(destination.iterdir()) == []
    assert list(inside.iterdir()) == []


def test_create_refuses_a_denied_or_overlapping_parent(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    with pytest.raises(WorkspaceError) as denied:
        bootstrap.create(COMMAND, "Vault", str(tmp_path / "data"), None, "en", False)
    assert denied.value.code == "denied_location"

    registered = tmp_path / "registered"
    (registered / "inner").mkdir(parents=True)
    service.register_existing(OTHER_COMMAND, str(registered))
    with pytest.raises(WorkspaceError) as overlapping:
        bootstrap.create(COMMAND, "Vault", str(registered), None, "en", False)
    assert overlapping.value.code == "overlaps_registration"
    assert not (registered / "Vault").exists()


def test_create_with_the_suggested_parent_spelled_out_creates_it_like_an_omitted_one(
    tmp_path: Path,
) -> None:
    """The form sends the prefilled suggestion back; on a clean profile that parent does not
    exist yet, and the first create must not be refused with `not_found`."""
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    parent, folder = suggested_destination(tmp_path / "home", "My Vault")
    assert not parent.exists()
    workspace, created, _ = bootstrap.create(COMMAND, "My Vault", str(parent), folder, "en", False)
    assert created is True
    assert Path(workspace.root.display) == parent / "My Vault"
    # A different, missing parent is still refused: only the app's own default is created.
    with pytest.raises(WorkspaceError) as failure:
        bootstrap.create(OTHER_COMMAND, "Other", str(tmp_path / "elsewhere"), "Other", "en", False)
    assert failure.value.code == "not_found"
