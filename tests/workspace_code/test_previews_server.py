"""Previews wired through `create_app`: a preview goes from start to its window and away, a live
preview holds its worktree, the launch sweeps the preview profiles and shutdown ends the previews
and their windows.

A recording `preview_spawn` stands for Edge; no test opens a window. Process tests skip off
Windows and poll real OS state (a server's listen, a tree's end, a profile's deletion) under
`HANG_GUARD_S`; each ends its processes through a stop or the app's shutdown."""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.test_server import BEARER, TOKEN
from tests.workspace_code.test_checks_server import HANG_GUARD_S, _answer, _post, _until_dead
from tests.workspace_code.test_preview_windows import EDGE, Spawner
from tests.workspace_code.test_previews import _free_port
from tests.workspace_code.test_roots_server import (
    Built,
    Folders,
    _build,
    _create_worktree,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _folders,
    needs_windows_git,
    running,
)
from viki_agent import server

PING = "ping -n 30 127.0.0.1 >nul"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="previews run on Windows only")


def _build_previews(folders: Folders, spawn: Spawner, monkeypatch: pytest.MonkeyPatch) -> Built:
    """`_build`'s launch, with a browser path and the recording spawn for preview windows."""
    real = server.create_app

    def create_app(**kwargs: Any) -> Any:
        kwargs["config"] = dataclasses.replace(kwargs["config"], edge=EDGE)
        return real(**kwargs, preview_spawn=spawn)

    monkeypatch.setattr(server, "create_app", create_app)
    return _build(folders)


def _start(
    client: TestClient, folders: Folders, command: str, port: int, worktree_id: str | None = None
) -> dict[str, Any]:
    body = {"worktree_id": worktree_id, "command": command, "port": port}
    url = f"/api/v2/code/{folders.code_id}/previews"
    return _answer(_post(client, url, body), 201)["preview"]


def _serve(port: int) -> str:
    return f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'


def _listed(client: TestClient, folders: Folders) -> list[dict[str, Any]]:
    url = f"/api/v2/code/{folders.code_id}/previews"
    return _answer(client.get(url, headers=BEARER))["previews"]


def _until_state(
    client: TestClient, folders: Folders, preview_id: str, state: str
) -> dict[str, Any]:
    deadline = time.monotonic() + HANG_GUARD_S
    while True:
        [view] = [v for v in _listed(client, folders) if v["preview_id"] == preview_id]
        if view["state"] == state:
            return view
        assert time.monotonic() < deadline, f"the preview did not reach {state}"
        time.sleep(0.02)  # a listen and a tree's end are real OS events; poll the state


def _until_gone(path: Path) -> None:
    deadline = time.monotonic() + HANG_GUARD_S
    while path.exists():
        assert time.monotonic() < deadline, "the profile outlived its window"
        time.sleep(0.02)  # the profile is deleted in a thread after the window ends; poll it


def _open(client: TestClient, preview_id: str) -> None:
    assert _answer(_post(client, f"/api/v2/previews/{preview_id}/open", {})) == {"opened": True}


@windows_only
def test_a_preview_goes_from_start_to_window_through_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path)
    spawn = Spawner()
    built = _build_previews(folders, spawn, monkeypatch)
    port = _free_port()
    with running(built) as client:
        preview = _start(client, folders, _serve(port), port)
        assert (preview["state"], preview["worktree_id"]) == ("starting", None)
        preview_id = preview["preview_id"]
        listening = _until_state(client, folders, preview_id, "listening")

        _open(client, preview_id)
        [call] = spawn.calls
        assert call.argv[-1] == f"--app=http://localhost:{port}/"
        bypass = f"--proxy-bypass-list=<-loopback>;localhost:{port};127.0.0.1:{port};[::1]:{port}"
        assert bypass in call.argv
        assert [a for a in call.argv if a.startswith("--proxy-server=127.0.0.1:")] != []
        [rules] = [a for a in call.argv if a.startswith("--host-resolver-rules=")]
        assert rules.endswith("EXCLUDE 127.0.0.1")
        held = [*call.argv, *call.env, *call.env.values()]
        assert [element for element in held if TOKEN in element] == []

        _post(client, f"/api/v2/previews/{preview_id}/stop", {})
        stopped = _until_state(client, folders, preview_id, "stopped")
        assert (stopped["exit_code"], stopped["listener_pid"]) == (None, None)
        [window] = spawn.windows
        assert window.kills == 1
        _until_gone(call.cwd)

        removed = _post(client, f"/api/v2/previews/{preview_id}/remove", {})
        assert _answer(removed) == {"removed": True}
        assert _listed(client, folders) == []
    _until_dead(listening["listener_pid"])
    _until_dead(listening["pid"])


@needs_windows_git
def test_a_live_preview_holds_its_worktree_against_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build_previews(folders, Spawner(), monkeypatch)
    with running(built) as client:
        worktree = _create_worktree(client, built, folders)
        worktree_id, path = worktree["worktree_id"], Path(worktree["path"])
        preview = _start(client, folders, PING, _free_port(), worktree_id)
        workspace = built.spaces.get(folders.code_id, kind="code")
        admission = {
            "policy_revision": workspace.policy_revision,
            "root_physical_id": workspace.root.physical_id,
        }
        refused = _post(client, f"/api/v2/worktrees/{worktree_id}/remove", admission)
        assert _answer(refused, 409)["error"]["code"] == "worktree_in_use"
        row = built.state.store.get_worktree(worktree_id)
        assert row is not None and row["state"] == "ready" and path.is_dir()

        _post(client, f"/api/v2/previews/{preview['preview_id']}/stop", {})
        _until_state(client, folders, preview["preview_id"], "stopped")
    _until_dead(preview["pid"])


def test_the_launch_sweeps_the_preview_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path)
    stale = folders.tmp_path / "data" / "preview-edge" / "stale"
    stale.mkdir(parents=True)
    (stale / "Local State").write_text("{}", encoding="utf-8")
    built = _build_previews(folders, Spawner(), monkeypatch)
    with running(built):
        assert not stale.exists()


@windows_only
def test_shutdown_ends_the_previews_and_closes_their_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path)
    spawn = Spawner()
    built = _build_previews(folders, spawn, monkeypatch)
    port = _free_port()
    with running(built) as client:
        preview = _start(client, folders, _serve(port), port)
        listening = _until_state(client, folders, preview["preview_id"], "listening")
        _open(client, preview["preview_id"])
    _until_dead(listening["listener_pid"])
    _until_dead(listening["pid"])
    [window] = spawn.windows
    assert window.ended.is_set() and window.releases >= 1
    assert list((folders.tmp_path / "data" / "preview-edge").iterdir()) == []
