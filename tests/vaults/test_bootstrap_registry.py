"""Bootstrap command roles and registry changes, using synthetic temporary folders only."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.app.test_workspaces import service_at
from tests.vaults.test_vault_storage import bootstrap_at, snapshot
from viki_agent import workspaces
from viki_agent.domain import CanonicalRoot, PolicyKind, Workspace
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

HANG_GUARD_S = 5.0  # Events normally fire immediately; a stuck worker fails the test.


def _reserve(service: WorkspaceService, command_id: str, destination: Path) -> Workspace:
    return service.register_created(
        command_id,
        CanonicalRoot(root_key(destination), str(destination), f"pending:{command_id}", False),
        "Pending",
        portable_id=None,
    )


def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("an unrelated registration must not reach bootstrap filesystem work")


@pytest.mark.parametrize("kind", ["vault", "code"])
@pytest.mark.parametrize("policy", ["existing_readonly", "ordinary", "protected"])
def test_an_open_or_policy_command_cannot_become_a_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["vault", "code"],
    policy: PolicyKind,
) -> None:
    service = service_at(tmp_path)
    root = tmp_path / "existing"
    root.mkdir()
    command_id = str(uuid.uuid4())
    opened, _ = service.register_existing(command_id, str(root), kind=kind)
    if policy != "existing_readonly":
        service.set_policy(command_id, opened.workspace_id, 1, policy, [])
    before = service.get(opened.workspace_id, kind=None)
    bootstrap = bootstrap_at(tmp_path, service)
    monkeypatch.setattr(bootstrap, "_destination", _unexpected)
    monkeypatch.setattr(bootstrap, "_write", _unexpected)
    monkeypatch.setattr(bootstrap, "_present", _unexpected)
    monkeypatch.setattr(workspaces, "probe_identity", _unexpected)

    with pytest.raises(WorkspaceError) as refused:
        bootstrap.create(command_id, "Other", None, None, "en", False)
    assert refused.value.code == "not_app_created"
    for target in (root, tmp_path / "another-destination"):
        with pytest.raises(WorkspaceError) as reservation:
            _reserve(service, command_id, target)
        assert reservation.value.code == "not_app_created"
    with pytest.raises(WorkspaceError) as completion:
        service.complete_bootstrap(before.workspace_id, before.root)
    assert completion.value.code == "not_app_created"
    assert service.get(before.workspace_id, kind=None) == before
    assert service.list(kind=None) == [before]
    assert list(root.iterdir()) == []
    assert not (tmp_path / "home" / "VIKI").exists()


@pytest.mark.parametrize("policy", ["ordinary", "existing_readonly", "protected"])
def test_a_completed_created_vault_replays_without_writes_or_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: PolicyKind
) -> None:
    service = service_at(tmp_path)
    bootstrap = bootstrap_at(tmp_path, service)
    command_id = str(uuid.uuid4())
    complete, _, files = bootstrap.create(command_id, "First", None, None, "en", False)
    if policy != "ordinary":
        complete = service.set_policy(command_id, complete.workspace_id, 1, policy, [])
    before = snapshot(Path(complete.root.display))
    other = tmp_path / "other"
    other.mkdir()
    other_root = service.canonicalise(str(other))
    monkeypatch.setattr(bootstrap, "_write", _unexpected)
    monkeypatch.setattr(workspaces, "probe_identity", _unexpected)

    replayed, created, replay_files = bootstrap.create(
        command_id, "Other", str(tmp_path / "missing"), None, "en", True, template="viki"
    )
    assert (replayed, created, replay_files) == (complete, False, files)
    assert service.complete_bootstrap(complete.workspace_id, complete.root) == complete
    for retarget in (
        other_root,
        replace(complete.root, physical_id=other_root.physical_id),
        replace(complete.root, display=str(other)),
    ):
        with pytest.raises(WorkspaceError) as refused:
            service.complete_bootstrap(complete.workspace_id, retarget)
        assert refused.value.code == "not_app_created"
    assert service.get(complete.workspace_id) == complete
    assert snapshot(Path(complete.root.display)) == before
    assert list(other.iterdir()) == []


def test_a_pending_completion_keeps_its_reserved_destination(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    destination, other = tmp_path / "pending", tmp_path / "other"
    destination.mkdir()
    other.mkdir()
    command_id = str(uuid.uuid4())
    pending = _reserve(service, command_id, destination)
    with pytest.raises(WorkspaceError) as refused:
        service.complete_bootstrap(pending.workspace_id, service.canonicalise(str(other)))
    assert refused.value.code == "not_app_created"
    assert service.get(pending.workspace_id) == pending
    assert list(destination.iterdir()) == [] and list(other.iterdir()) == []

    with pytest.raises(WorkspaceError) as policy:
        service.set_policy(str(uuid.uuid4()), pending.workspace_id, 1, "protected", [])
    assert policy.value.code == "root_unavailable"
    complete, created, files = bootstrap_at(tmp_path, service).create(
        command_id, "Other", str(tmp_path / "missing"), None, "en", False
    )
    assert created and files == ["Welcome.md"]
    assert complete.workspace_id == pending.workspace_id
    assert complete.root.display == str(destination) and complete.bootstrap == "complete"
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("pending", [False, True])
def test_a_native_case_alias_keeps_the_same_destination(tmp_path: Path, pending: bool) -> None:
    parent = tmp_path / "parent"
    destination = parent / "Existing"
    destination.mkdir(parents=True)
    alias = parent / "existing"
    if not alias.exists() or not alias.samefile(destination):
        pytest.skip("the temporary filesystem distinguishes these letter-case spellings")
    service = service_at(tmp_path)
    command_id = str(uuid.uuid4())
    expected = service.canonicalise(str(alias))
    if pending:
        _reserve(service, command_id, alias)  # a reservation made before canonical spelling
    workspace, created, files = bootstrap_at(tmp_path, service).create(
        command_id, "existing", str(parent), None, "en", False
    )
    assert created and files == ["Welcome.md"]
    assert workspace.root == expected
    assert Path(workspace.root.display).samefile(alias)


@pytest.mark.parametrize("reserved_exists", [False, True])
def test_equal_folded_keys_do_not_reattach_a_missing_or_different_pending_directory(
    tmp_path: Path, reserved_exists: bool
) -> None:
    """Model a conservative key folding two paths on a case-sensitive volume."""
    service = service_at(tmp_path)
    destination, other = tmp_path / "pending", tmp_path / "other"
    if reserved_exists:
        destination.mkdir()
    other.mkdir()
    pending = _reserve(service, str(uuid.uuid4()), destination)
    observed = service.canonicalise(str(other))
    same_fold = replace(observed, key=pending.root.key)
    assert same_fold.key == pending.root.key
    if reserved_exists:
        assert not destination.samefile(other)
    with pytest.raises(WorkspaceError) as refused:
        service.complete_bootstrap(pending.workspace_id, same_fold)
    assert refused.value.code == "not_app_created"
    assert service.get(pending.workspace_id) == pending
    assert list(other.iterdir()) == []


def test_cancel_then_protected_open_waits_until_the_resuming_bootstrap_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_at(tmp_path)
    destination = tmp_path / "pending"
    destination.mkdir()
    command_id = str(uuid.uuid4())
    pending = _reserve(service, command_id, destination)
    bootstrap = bootstrap_at(tmp_path, service)
    attempted, finished = threading.Event(), threading.Event()
    cancellation: Future[str] | None = None
    write = bootstrap._write

    def cancel_then_open_protected() -> str:
        attempted.set()
        try:
            bootstrap.cancel(str(uuid.uuid4()), pending.workspace_id)
            service.register_existing(str(uuid.uuid4()), str(destination), bind_protected=True)
            return "protected"
        except WorkspaceError as exc:
            return exc.code
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as workers:

        def write_while_cancelling(*args: Any, **kwargs: Any) -> None:
            nonlocal cancellation
            cancellation = workers.submit(cancel_then_open_protected)
            assert attempted.wait(HANG_GUARD_S)
            assert not finished.wait(0.05)
            write(*args, **kwargs)

        monkeypatch.setattr(bootstrap, "_write", write_while_cancelling)
        complete, created, files = bootstrap.create(command_id, "Pending", None, None, "en", False)
        assert cancellation is not None
        assert cancellation.result(HANG_GUARD_S) == "not_app_created"

    assert created and files == ["Welcome.md"]
    assert (complete.policy, complete.bootstrap) == ("ordinary", "complete")
    assert service.list() == [complete]
    assert (destination / "Welcome.md").is_file()


@pytest.mark.parametrize("policy", ["ordinary", "protected"])
def test_a_reattach_rechecks_an_ancestor_registered_after_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: PolicyKind
) -> None:
    service = service_at(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    moving, _ = service.register_existing(str(uuid.uuid4()), str(original))
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    destination = ancestor / "moved"
    original.rename(destination)
    evaluated, resume = threading.Event(), threading.Event()
    reattach = service._reattach

    def pause(workspace_id: str, root: CanonicalRoot) -> dict[str, Any]:
        evaluated.set()
        assert resume.wait(HANG_GUARD_S)
        return reattach(workspace_id, root)

    monkeypatch.setattr(service, "_reattach", pause)
    with ThreadPoolExecutor(max_workers=1) as workers:
        relocation = workers.submit(service.register_existing, str(uuid.uuid4()), str(destination))
        try:
            assert evaluated.wait(HANG_GUARD_S)
            registered, _ = service.register_existing(str(uuid.uuid4()), str(ancestor))
            registered = service.set_policy(
                str(uuid.uuid4()), registered.workspace_id, 1, policy, []
            )
        finally:
            resume.set()
        with pytest.raises(WorkspaceError) as refused:
            relocation.result(HANG_GUARD_S)

    expected = "protected_overlap" if policy == "protected" else "overlaps_registration"
    assert refused.value.code == expected
    assert refused.value.extra["workspace_id"] == registered.workspace_id
    assert service.get(moving.workspace_id) == moving
    assert service.get(registered.workspace_id) == registered
