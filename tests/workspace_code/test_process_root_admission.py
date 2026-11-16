"""Recorded worktree paths cannot redirect check, preview or terminal destinations."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from tests.app.test_workspaces import protected_at, service_at, store_at
from tests.workspace_code.test_code_checks import WORKTREE, Env
from viki_agent import winproc
from viki_agent.code import code_checks, previews, terminal_launch
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.code_checks import CheckService, CodeRunError, trusted_root
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.preview_windows import PreviewWindows
from viki_agent.code.previews import PreviewService
from viki_agent.code.session_roots import SessionRoots
from viki_agent.workspaces import WorkspaceError, chain_has_reparse


def _redirect(path: Path, protected: Path, *, ancestor: bool) -> Path:
    path.rmdir()
    if ancestor:
        path.parent.rmdir()
        (protected / path.name).mkdir()
        path = path.parent
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(protected), str(path))
    else:
        path.symlink_to(protected, target_is_directory=True)
    return path


@pytest.mark.parametrize("ancestor", [False, True], ids=["leaf", "ancestor"])
@pytest.mark.parametrize("caller", ["gate", "check", "preview", "terminal"])
@pytest.mark.parametrize("inspection_failure", [False, True], ids=["redirect", "inspection"])
async def test_unsafe_worktree_chains_refuse_before_every_process_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor: bool,
    caller: str,
    inspection_failure: bool,
) -> None:
    store = store_at(tmp_path)
    workspaces = service_at(tmp_path, store)
    env = Env(tmp_path, store, workspaces)
    workspace_id = env.code()
    path = env.worktree(workspace_id)
    assert trusted_root(store, workspaces, workspace_id, WORKTREE) == path
    link: Path | None = None
    inspected: list[Path] = []
    if inspection_failure:
        real_lstat = Path.lstat
        unavailable = path.parent if ancestor else path

        def lstat(candidate: Path) -> os.stat_result:
            if candidate == unavailable:
                inspected.append(candidate)
                raise OSError("synthetic private inspection path")
            return real_lstat(candidate)

        monkeypatch.setattr(Path, "lstat", lstat)
    else:
        protected = tmp_path / "protected"
        protected.mkdir()
        protected_at(workspaces, protected)
        link = _redirect(path, protected, ancestor=ancestor)
        assert path.resolve().is_relative_to(protected)
    roots = SessionRoots(store, workspaces, exists=lambda _: False, actor=lambda _: None)
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        calls.append("process")
        raise AssertionError("process seam reached")

    for name in ("git", "run", "spawn", "start_shell", "open_terminal"):
        monkeypatch.setattr(winproc, name, forbidden)
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", True)
    monkeypatch.setattr(terminal_launch, "TERMINAL_SUPPORTED", True)
    try:
        if caller == "terminal":
            workers: set[asyncio.Task[Any]] = set()
            app = Starlette(
                routes=terminal_launch.terminal_launch_routes(
                    store,
                    workspaces,
                    roots,
                    authorized=lambda _: True,
                    origins=frozenset({"http://synthetic"}),
                    workers=workers,
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
            ) as client:
                response = await client.post(
                    f"/api/v2/code/{workspace_id}/terminal",
                    json={"worktree_id": WORKTREE},
                    headers={"Origin": "http://synthetic"},
                )
            assert response.status_code == 409
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["error"]["code"] == "worktree_not_ready"
            assert "synthetic private inspection path" not in response.text
        else:
            with pytest.raises(CodeRunError) as refused:
                if caller == "gate":
                    trusted_root(store, workspaces, workspace_id, WORKTREE)
                elif caller == "check":
                    check = CheckService(store, workspaces).add_check(
                        workspace_id, "synthetic", "echo synthetic", 60
                    )
                    row = store.get_check(check.check_id)
                    assert row is not None
                    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: False)
                    runner = CheckRunner(store, workspaces, git, roots, start=forbidden)
                    # The Git snapshot is evidence only; its failure must not admit a redirect.
                    monkeypatch.setattr(runner, "_snapshot", lambda *_args: (None, None))
                    runner._launch("11111111-1111-4111-8111-111111111111", row, WORKTREE, "test")
                else:
                    service = PreviewService(
                        store,
                        workspaces,
                        roots,
                        PreviewWindows(None, tmp_path / "data", spawn=forbidden),
                        start=forbidden,
                        listeners=forbidden,
                    )
                    service._launch(workspace_id, WORKTREE, "test")
            assert refused.value.code == "worktree_not_ready"
            assert str(refused.value) == "worktree_not_ready"
        assert calls == []
        if inspection_failure:
            assert inspected
    finally:
        if link is not None:
            if sys.platform == "win32":
                link.rmdir()
            else:
                link.unlink()


def test_ordinary_ready_worktree_beneath_localappdata_is_still_admitted(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    # Unlike an ordinary registration, an app-created worktree may live under LOCALAPPDATA.
    workspaces = service_at(tmp_path, store, env={"LOCALAPPDATA": str(tmp_path / "worktrees")})
    env = Env(tmp_path, store, workspaces)
    workspace_id = env.code()
    path = env.worktree(workspace_id)
    assert trusted_root(store, workspaces, workspace_id, WORKTREE) == path
    if sys.platform == "win32":
        with pytest.raises(WorkspaceError) as refused:
            workspaces.canonicalise(str(path))
        assert refused.value.code == "denied_location"


def test_default_chain_inspection_still_skips_unreadable_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worktree"
    path.mkdir()
    real_lstat = Path.lstat
    inspected: list[Path] = []

    def lstat(candidate: Path) -> os.stat_result:
        if candidate == path:
            inspected.append(candidate)
            raise OSError("synthetic inspection failure")
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert not chain_has_reparse(path)
    assert inspected == [path]
