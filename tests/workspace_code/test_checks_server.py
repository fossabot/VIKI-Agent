"""Checks wired through `create_app`: a check goes from create to output and away, a live run holds
its worktree, the lifespan settles runs a previous launch left `running`, and shutdown ends a live
run.

Process tests skip off Windows and poll real OS state (a run settles when its process ends)
under `HANG_GUARD_S`; each ends its processes through a cancel or the app's shutdown."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.test_server import BEARER
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_roots_server import (
    JSON_HEADERS,
    Built,
    Folders,
    _build,
    _create_worktree,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _folders,
    needs_windows_git,
    running,
)
from viki_agent import winproc

HANG_GUARD_S = 10.0
SLOW = "ping -n 30 127.0.0.1 >nul"
STAMP = "2026-10-04T00:00:00.000Z"
CHECK = "00000000-0000-4000-8000-0000000000c1"
RUN = "00000000-0000-4000-8000-000000000001"


def _answer(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _post(client: TestClient, url: str, body: dict[str, Any]) -> Any:
    return client.post(url, content=json.dumps(body), headers=JSON_HEADERS)


def _shells(monkeypatch: pytest.MonkeyPatch) -> list[winproc.ShellRun]:
    """Every shell the app starts (the runner looks `start_shell` up at call time)."""
    started: list[winproc.ShellRun] = []
    real: Callable[..., winproc.ShellRun] = winproc.start_shell

    def record(command: str, **kwargs: Any) -> winproc.ShellRun:
        shell = real(command, **kwargs)
        started.append(shell)
        return shell

    monkeypatch.setattr(winproc, "start_shell", record)
    return started


def _add(client: TestClient, folders: Folders, name: str, command: str) -> dict[str, Any]:
    body = {"name": name, "command": command, "timeout_s": 600}
    return _answer(_post(client, f"/api/v2/code/{folders.code_id}/checks", body), 201)["check"]


def _start(client: TestClient, check_id: str, worktree_id: str | None = None) -> dict[str, Any]:
    body = {"worktree_id": worktree_id}
    return _answer(_post(client, f"/api/v2/checks/{check_id}/runs", body), 201)["run"]


def _until_done(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + HANG_GUARD_S
    while True:
        output = _answer(client.get(f"/api/v2/check-runs/{run_id}/output", headers=BEARER))
        if output["done"]:
            return output
        assert time.monotonic() < deadline, "the run did not settle"
        time.sleep(0.02)  # a run settles when its real process ends; poll it


def _until_dead(pid: int) -> None:
    deadline = time.monotonic() + HANG_GUARD_S
    while winproc.process_start_time(pid) is not None:
        assert time.monotonic() < deadline, "the process outlived the run"
        time.sleep(0.05)  # kill-on-close is asynchronous; poll the real OS state


def _row(built: Built, run_id: str) -> dict[str, Any]:
    row = built.state.store.get_check_run(run_id)
    assert row is not None
    return dict(row)


@needs_windows_git
def test_a_check_goes_from_create_to_output_through_the_app(tmp_path: Path) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    with running(built) as client:
        check = _add(client, folders, "echo", "echo viki-e09")
        assert (check["command"], check["readable"]) == ("echo viki-e09", True)
        run = _start(client, check["check_id"])
        assert (run["outcome"], run["worktree_id"]) == ("running", None)

        output = _until_done(client, run["run_id"])
        assert "viki-e09" in output["text"]
        settled = output["run"]
        assert (settled["outcome"], settled["exit_code"], settled["dirty"]) == ("passed", 0, False)
        assert settled["head_oid"] == _git(folders.display, "rev-parse", "HEAD").strip()

        runs = _answer(client.get(f"/api/v2/checks/{check['check_id']}/runs", headers=BEARER))
        assert runs == {"runs": [settled]}
        url = f"/api/v2/code/{folders.code_id}/checks"
        [listed] = _answer(client.get(url, headers=BEARER))["checks"]
        assert listed["last_run"] == settled

        deleted = _post(client, f"/api/v2/checks/{check['check_id']}/delete", {})
        assert _answer(deleted) == {"deleted": True}
        assert _answer(client.get(url, headers=BEARER)) == {"checks": []}


@needs_windows_git
def test_a_live_check_holds_its_worktree_against_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    shells = _shells(monkeypatch)
    with running(built) as client:
        worktree = _create_worktree(client, built, folders)
        worktree_id, path = worktree["worktree_id"], Path(worktree["path"])
        run = _start(client, _add(client, folders, "slow", SLOW)["check_id"], worktree_id)
        workspace = built.spaces.get(folders.code_id, kind="code")
        admission = {
            "policy_revision": workspace.policy_revision,
            "root_physical_id": workspace.root.physical_id,
        }
        refused = _post(client, f"/api/v2/worktrees/{worktree_id}/remove", admission)
        assert _answer(refused, 409)["error"]["code"] == "worktree_in_use"
        row = built.state.store.get_worktree(worktree_id)
        assert row is not None and row["state"] == "ready" and path.is_dir()

        _post(client, f"/api/v2/check-runs/{run['run_id']}/cancel", {})
        assert _until_done(client, run["run_id"])["run"]["outcome"] == "cancelled"
    [shell] = shells
    _until_dead(shell.pid)


def test_the_lifespan_interrupts_a_run_left_running(tmp_path: Path) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    store = built.state.store
    store.insert_check(
        {
            "check_id": CHECK,
            "workspace_id": folders.code_id,
            "body": b"sealed",  # never opened here
            "timeout_s": 600,
            "created_at": STAMP,
        },
        limit=32,
    )
    store.insert_check_run(
        {
            "run_id": RUN,
            "check_id": CHECK,
            "workspace_id": folders.code_id,
            "worktree_id": None,
            "head_oid": None,
            "dirty": None,
            "outcome": "running",
            "exit_code": None,
            "duration_ms": None,
            "output_bytes": None,
            "tail": None,
            "started_at": STAMP,
            "ended_at": None,
        },
        keep=20,
    )
    with running(built):
        row = _row(built, RUN)
    assert (row["outcome"], row["exit_code"], row["output_bytes"]) == ("interrupted", None, None)
    assert row["ended_at"] is not None


@needs_windows_git
def test_shutdown_interrupts_a_live_run_and_ends_its_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    shells = _shells(monkeypatch)
    with running(built) as client:
        run = _start(client, _add(client, folders, "slow", SLOW)["check_id"])
        assert _row(built, run["run_id"])["outcome"] == "running"
    assert _row(built, run["run_id"])["outcome"] == "interrupted"
    [shell] = shells
    _until_dead(shell.pid)
