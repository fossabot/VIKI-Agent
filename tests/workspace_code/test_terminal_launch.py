"""The terminal launch route wired through `create_app`: it opens Windows PowerShell in a trusted
code folder or its ready worktree with the scrubbed environment, and refuses every other request
before any Git call or process.

No test opens a window: every test replaces `winproc.open_terminal` with a recorder."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import viki_agent
from tests.app.test_workspaces import protected_at
from tests.test_server import BASE, BEARER, ORIGIN
from tests.workspace_code.test_checks_refusals import SHELL_WORDS
from tests.workspace_code.test_integration_refusals import _strings
from tests.workspace_code.test_roots_server import (
    JSON_HEADERS,
    Built,
    Folders,
    _build,
    _create_worktree,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _folders,
    needs_git,
    running,
)
from viki_agent import winproc
from viki_agent.code import terminal_launch
from viki_agent.code.session_roots import SessionRoots
from viki_agent.code.terminal_launch import terminal_launch_routes

STAMP = "2026-10-04T00:00:00.000Z"
WORKTREE = "00000000-0000-4000-8000-0000000000e2"
LEAKED = "CLAUDECODE"
HANG_GUARD_S = 10.0


@pytest.fixture(autouse=True)
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route reaches the root gate on every platform."""
    monkeypatch.setattr(terminal_launch, "TERMINAL_SUPPORTED", True)


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, dict[str, str]]]:
    """Every terminal the app would open (the route looks `open_terminal` up at call time)."""
    recorded: list[tuple[Path, dict[str, str]]] = []

    def record(cwd: str | os.PathLike[str], env: dict[str, str]) -> None:
        recorded.append((Path(cwd), env))

    monkeypatch.setattr(winproc, "open_terminal", record)
    return recorded


def _url(workspace_id: str) -> str:
    return f"/api/v2/code/{workspace_id}/terminal"


def _open(client: TestClient, workspace_id: str, worktree_id: str | None = None) -> Any:
    body = json.dumps({"worktree_id": worktree_id})
    return client.post(_url(workspace_id), content=body, headers=JSON_HEADERS)


def _code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["error"]["code"]


def _worktree(built: Built, folders: Folders, state: str = "ready") -> Path:
    """A worktree row of the code folder in `state`, with a folder at its path."""
    path = Path(tempfile.mkdtemp(dir=folders.vault.parent, prefix="w"))
    store = built.state.store
    store.insert_worktree(
        {
            "worktree_id": WORKTREE,
            "workspace_id": folders.code_id,
            "path": str(path),
            "branch": "viki/20261004-e28a",
            "base_oid": "a" * 40,
            "state": "creating",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
        limit=32,
    )
    if state != "creating":
        store.update_worktree_state(WORKTREE, "creating", state, STAMP)
    return path


def test_open_terminal_here_starts_in_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launches: list[tuple[Path, dict[str, str]]]
) -> None:
    monkeypatch.setenv(LEAKED, "1")
    folders = _folders(tmp_path)
    with running(_build(folders)) as client:
        response = _open(client, folders.code_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"opened": True}
    [(cwd, env)] = launches
    assert cwd == folders.display
    assert env == winproc.scrub_env(os.environ) and LEAKED not in env


def test_open_terminal_here_starts_in_the_worktree(
    tmp_path: Path, launches: list[tuple[Path, dict[str, str]]]
) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    path = _worktree(built, folders)
    with running(built) as client:
        response = _open(client, folders.code_id, WORKTREE)
    assert response.status_code == 200, response.text
    assert response.json() == {"opened": True}
    assert [cwd for cwd, _env in launches] == [path]


# --- refusals before the gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"headers": {"origin": ORIGIN, "content-type": "application/json"}}, 401, "unauthorized"),
        ({"headers": {**BEARER, "content-type": "application/json"}}, 403, "forbidden_origin"),
        ({"headers": {**JSON_HEADERS, "origin": "http://evil.example"}}, 403, "forbidden_origin"),
        ({"workspace": "not-a-uuid"}, 404, "unknown_workspace"),
        ({"query": "?worktree=x"}, 400, "invalid_query"),
        ({"headers": {**JSON_HEADERS, "content-type": "text/plain"}}, 400, "invalid_body"),
        ({"body": "{}"}, 400, "invalid_body"),
        ({"body": '{"worktree_id": null, "cwd": "C:/"}'}, 400, "invalid_body"),
        ({"body": '{"worktree_id": "not-a-uuid"}'}, 400, "invalid_body"),
        ({"body": '{"worktree_id": null, "worktree_id": null}'}, 400, "invalid_body"),
        ({"body": " " * 65_537}, 413, "too_large"),
    ],
    ids=[
        "no-bearer",
        "no-origin",
        "foreign-origin",
        "uuid",
        "query",
        "content-type",
        "missing-key",
        "extra-key",
        "bad-worktree",
        "duplicate-key",
        "too-large",
    ],
)
def test_a_malformed_request_is_refused_without_a_launch(
    tmp_path: Path,
    launches: list[tuple[Path, dict[str, str]]],
    change: dict[str, Any],
    status: int,
    code: str,
) -> None:
    folders = _folders(tmp_path)
    workspace = change.get("workspace", folders.code_id)
    url = _url(workspace) + change.get("query", "")
    with running(_build(folders)) as client:
        response = client.post(
            url,
            content=change.get("body", '{"worktree_id": null}'),
            headers=change.get("headers", JSON_HEADERS),
        )
    assert _code(response, status) == code
    assert launches == []


def test_off_windows_the_route_refuses_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launches: list[tuple[Path, dict[str, str]]]
) -> None:
    monkeypatch.setattr(terminal_launch, "TERMINAL_SUPPORTED", False)
    folders = _folders(tmp_path)
    with running(_build(folders)) as client:
        known = _open(client, folders.code_id)
        unknown = _open(client, str(uuid.uuid4()))
    assert _code(known, 409) == _code(unknown, 409) == "unsupported_platform"
    assert launches == []


def test_a_terminal_that_cannot_start_answers_start_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_cwd: str | os.PathLike[str], _env: dict[str, str]) -> None:
        raise OSError("no console")

    monkeypatch.setattr(winproc, "open_terminal", fail)
    folders = _folders(tmp_path)
    with running(_build(folders)) as client:
        response = _open(client, folders.code_id)
    assert _code(response, 503) == "start_failed"
    assert "no console" not in response.text


# --- the root gate -----------------------------------------------------------------------------


def _untrusted(built: Built, short: Path) -> str:
    folder = short / "untrusted"
    folder.mkdir()
    workspace, _ = built.spaces.register_existing(str(uuid.uuid4()), str(folder), kind="code")
    assert workspace.policy == "existing_readonly"
    return workspace.workspace_id


def _protected_bound(built: Built, short: Path) -> str:
    vault, copy = short / "VIKI", short / "copy"
    for root in (vault, copy):
        root.mkdir()
        (root / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    protected_at(built.spaces, vault)
    workspace, _ = built.spaces.register_existing(str(uuid.uuid4()), str(copy), kind="code")
    assert workspace.policy == "protected"
    return workspace.workspace_id


def _vault(built: Built, short: Path) -> str:
    notes = short / "other-notes"
    notes.mkdir()
    return built.spaces.register_existing(str(uuid.uuid4()), str(notes))[0].workspace_id


def _refused(
    built: Built,
    monkeypatch: pytest.MonkeyPatch,
    launches: list[tuple[Path, dict[str, str]]],
    workspace_id: str,
    worktree_id: str | None = None,
) -> Any:
    """The answer to one launch request, which started no terminal and called no Git."""
    calls: list[tuple[str, ...]] = []
    with running(built) as client:
        monkeypatch.setattr(winproc, "git", lambda _cwd, *args, **_kw: calls.append(args))
        response = _open(client, workspace_id, worktree_id)
    assert (calls, launches) == ([], [])
    return response


@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (_untrusted, 409, "trust_required"),
        (_protected_bound, 409, "trust_required"),
        (_vault, 404, "unknown_workspace"),
    ],
    ids=["untrusted", "protected-bound", "vault"],
)
def test_an_untrusted_protected_or_vault_workspace_is_refused_without_a_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launches: list[tuple[Path, dict[str, str]]],
    make: Any,
    status: int,
    code: str,
) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    workspace_id = make(built, folders.vault.parent)
    assert _code(_refused(built, monkeypatch, launches, workspace_id), status) == code


@pytest.mark.parametrize("state", ["removing", "kept", "removed", "missing"])
def test_a_worktree_that_is_not_ready_is_refused_without_a_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launches: list[tuple[Path, dict[str, str]]],
    state: str,
) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    path = _worktree(built, folders, "ready" if state == "missing" else state)
    if state == "missing":  # a ready row whose folder is gone
        path.rmdir()
    response = _refused(built, monkeypatch, launches, folders.code_id, WORKTREE)
    assert _code(response, 409) == "worktree_not_ready"


def test_a_worktree_a_removal_holds_is_refused_without_a_launch(
    tmp_path: Path, launches: list[tuple[Path, dict[str, str]]]
) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    _worktree(built, folders)
    roots = SessionRoots(
        built.state.store, built.spaces, exists=lambda _sid: False, actor=lambda _sid: None
    )
    roots.block(WORKTREE)
    app = Starlette(
        routes=terminal_launch_routes(
            built.state.store,
            built.spaces,
            roots,
            authorized=lambda request: (
                request.headers.get("authorization") == BEARER["authorization"]
            ),
            origins=frozenset({ORIGIN}),
            workers=set(),
        )
    )
    with TestClient(app, base_url=BASE) as client:
        response = _open(client, folders.code_id, WORKTREE)
    assert _code(response, 409) == "worktree_not_ready"
    assert launches == []


async def test_a_client_that_leaves_does_not_cancel_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = threading.Event(), threading.Event()
    opened: list[Path] = []

    def held(cwd: str | os.PathLike[str], _env: dict[str, str]) -> None:
        entered.set()
        gate.wait(HANG_GUARD_S)
        opened.append(Path(cwd))

    monkeypatch.setattr(winproc, "open_terminal", held)
    folders = _folders(tmp_path)
    built = _build(folders)
    roots = SessionRoots(
        built.state.store, built.spaces, exists=lambda _sid: False, actor=lambda _sid: None
    )
    workers: set[asyncio.Task[Any]] = set()
    app = Starlette(
        routes=terminal_launch_routes(
            built.state.store,
            built.spaces,
            roots,
            authorized=lambda request: (
                request.headers.get("authorization") == BEARER["authorization"]
            ),
            origins=frozenset({ORIGIN}),
            workers=workers,
        )
    )
    body = json.dumps({"worktree_id": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        request = asyncio.create_task(
            client.post(_url(folders.code_id), content=body, headers=JSON_HEADERS)
        )
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        [worker] = workers
        request.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The client is gone; the launch still finishes and answers.
        await asyncio.wait([worker], timeout=HANG_GUARD_S)
    assert not worker.cancelled() and worker.result().status_code == 200
    assert opened == [folders.display] and not workers


@needs_git
async def test_a_pending_terminal_launch_blocks_worktree_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()

    def held(_cwd: str | os.PathLike[str], _env: dict[str, str]) -> None:
        entered.set()
        assert release.wait(HANG_GUARD_S)

    monkeypatch.setattr(winproc, "open_terminal", held)
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    with running(built) as client:
        worktree = _create_worktree(client, built, folders)
        worktree_id = worktree["worktree_id"]
        opening = asyncio.create_task(
            asyncio.to_thread(_open, client, folders.code_id, worktree_id)
        )
        try:
            assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
            workspace = built.spaces.get(folders.code_id, kind="code")
            response = await asyncio.to_thread(
                client.post,
                f"/api/v2/worktrees/{worktree_id}/remove",
                json={
                    "policy_revision": workspace.policy_revision,
                    "root_physical_id": workspace.root.physical_id,
                },
                headers=JSON_HEADERS,
            )
            assert _code(response, 409) == "worktree_in_use"
            assert Path(worktree["path"]).is_dir()
            row = built.state.store.get_worktree(worktree_id)
            assert row is not None and row["state"] == "ready"
        finally:
            release.set()
            assert (await opening).status_code == 200


@pytest.mark.parametrize("fail", [False, True])
async def test_concurrent_terminal_launches_keep_independent_worktree_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    lock = threading.Lock()
    count = 0

    def held(_cwd: str | os.PathLike[str], _env: dict[str, str]) -> None:
        nonlocal count
        with lock:
            slot = count
            count += 1
        entered[slot].set()
        assert release[slot].wait(HANG_GUARD_S)
        if fail:
            raise OSError("synthetic terminal failure")

    monkeypatch.setattr(winproc, "open_terminal", held)
    folders = _folders(tmp_path)
    built = _build(folders)
    _worktree(built, folders)
    roots = SessionRoots(
        built.state.store, built.spaces, exists=lambda _sid: False, actor=lambda _sid: None
    )
    pending: dict[str, int] = {}
    app = Starlette(
        routes=terminal_launch_routes(
            built.state.store,
            built.spaces,
            roots,
            authorized=lambda _request: True,
            origins=frozenset({ORIGIN}),
            workers=set(),
            pending=pending,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        first = asyncio.create_task(
            client.post(_url(folders.code_id), json={"worktree_id": WORKTREE}, headers=JSON_HEADERS)
        )
        second: asyncio.Task[httpx.Response] | None = None
        try:
            assert await asyncio.to_thread(entered[0].wait, HANG_GUARD_S)
            second = asyncio.create_task(
                client.post(
                    _url(folders.code_id), json={"worktree_id": WORKTREE}, headers=JSON_HEADERS
                )
            )
            assert await asyncio.to_thread(entered[1].wait, HANG_GUARD_S)
            assert pending == {WORKTREE: 2}
            release[0].set()
            assert (await first).status_code == (503 if fail else 200)
            assert pending == {WORKTREE: 1}
        finally:
            for gate in release:
                gate.set()
            await first
            if second is not None:
                assert (await second).status_code == (503 if fail else 200)
    assert pending == {}


def test_the_launch_module_holds_no_shell_string() -> None:
    source = (Path(viki_agent.__file__).parent / "code/terminal_launch.py").read_text(
        encoding="utf-8"
    )
    _, strings = _strings(source)
    found = [
        node.value
        for node in strings
        if isinstance(node.value, str)
        and any(word in node.value.casefold() for word in SHELL_WORDS)
    ]
    assert found == []
