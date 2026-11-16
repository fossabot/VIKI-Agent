"""Export destinations stay outside real registrations through the create-only write."""

from __future__ import annotations

import builtins
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.test_workspaces import service_at

from viki_agent.app import exports
from viki_agent.app.exports import ExportError, ExportService
from viki_agent.domain import Workspace
from viki_agent.workspaces import WorkspaceService

HANG_GUARD_S = 5  # Guard on events normally raised in milliseconds, not a work budget.


def exporter(tmp_path: Path, spaces: WorkspaceService) -> ExportService:
    service = ExportService(
        diagnostics_document=lambda _paths, _identity: {},
        redact=lambda text, _roots: (text, 0),
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs={},
        workspaces_export=lambda: (),
        denied_roots=lambda: [Path(w.root.display) for w in spaces.list(kind=None)],
        destination_guard=spaces.registration_lock,
        log_path=tmp_path / "unused.log",
        app_version="synthetic",
        launch_id="synthetic",
    )
    return service


def test_a_registration_winning_worker_admission_gets_no_export_bytes(tmp_path: Path) -> None:
    spaces = service_at(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    service = exporter(tmp_path, spaces)
    preview = service.preview("settings", None)
    started, finished = threading.Event(), threading.Event()

    def write():
        started.set()
        try:
            return service.write(
                command_id=str(uuid.uuid4()),
                preview_id=preview.preview_id,
                destination=str(destination),
            )
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with spaces.registration_lock:
            pending = workers.submit(write)
            assert started.wait(0.05)
            assert not finished.wait(0.05)
            workspace, created = spaces.register_existing(str(uuid.uuid4()), str(destination))
            assert created and workspace.policy == "existing_readonly"
        with pytest.raises(ExportError) as refused:
            pending.result(timeout=HANG_GUARD_S)
    assert refused.value.code == "denied_destination"
    assert list(destination.iterdir()) == []
    assert service._receipts == {}


def test_registration_mid_export_waits_for_file_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spaces = service_at(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    service = exporter(tmp_path, spaces)
    preview = service.preview("settings", None)
    attempted, committed, published = threading.Event(), threading.Event(), threading.Event()
    registration: Future[tuple[Workspace, bool, bool]] | None = None

    def register() -> tuple[Workspace, bool, bool]:
        attempted.set()
        workspace, created = spaces.register_existing(str(uuid.uuid4()), str(destination))
        saw_publication = published.is_set()
        committed.set()
        return workspace, created, saw_publication

    with ThreadPoolExecutor(max_workers=1) as workers:

        @contextmanager
        def writing(path, mode, *args, **kwargs):
            nonlocal registration
            registration = workers.submit(register)
            assert attempted.wait(0.05)
            assert not committed.wait(0.05)
            with builtins.open(path, mode, *args, **kwargs) as handle:
                yield handle
            published.set()

        monkeypatch.setattr(exports, "open", writing, raising=False)
        result, replayed = service.write(
            command_id=str(uuid.uuid4()),
            preview_id=preview.preview_id,
            destination=str(destination),
        )
        assert registration is not None
        workspace, created, saw_publication = registration.result(timeout=HANG_GUARD_S)
    assert (workspace.policy, created, saw_publication) == ("existing_readonly", True, True)
    assert not replayed
    assert Path(result.path).read_bytes() == preview.content[1].encode("utf-8")
