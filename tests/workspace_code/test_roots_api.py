"""The session-root and worktree routes over real services in `tmp_path`.

Writing cases run with Git, as the worktree service's own tests do. Every refusal that stops
before a write runs on any platform that has Git (or none), and the gate checks run over a spy
that records any service call it gets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.workspace_code.test_code_api import ORIGIN, ROOT_NAME
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_session_roots import FORK, bot_copy
from tests.workspace_code.test_workspaces_code import trusted
from tests.workspace_code.test_worktree_sweep import PAST, _age
from tests.workspace_code.test_worktree_sweep import _rig as sweep_rig
from viki_agent.code import roots_api, worktrees
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.roots_api import MAX_BODY_BYTES, MAX_ROOT_REQUESTS, root_routes
from viki_agent.code.session_roots import RootError, SessionRoot, SessionRoots
from viki_agent.code.worktrees import (
    Removal,
    Worktree,
    WorktreeError,
    WorktreeService,
    worktree_area,
)
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import MutationCoordinator, MutationError
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")

SESSION = "33333333-3333-4333-8333-333333333333"
OTHER_SESSION = "44444444-4444-4444-8444-444444444444"
WS = "11111111-1111-4111-8111-111111111111"
WT = "22222222-2222-4222-8222-222222222222"
ALPHA = "abcdefab-cdef-4abc-8def-abcdefabcdef"  # hex letters, so its upper case is not canonical
BRANCH = "viki/20261003-ab12"
OID = "a" * 40
EARLY = "2026-10-01T00:00:00.000Z"
LATE = "2026-10-02T00:00:00.000Z"
ROOTS = "/api/v2/session-roots"
WORKTREES = "/api/v2/worktrees"
W_KEYS = {
    "worktree_id",
    "workspace_id",
    "branch",
    "base_oid",
    "path",
    "state",
    "created_at",
    "updated_at",
}

BEARER = {"Authorization": "Bearer secret"}
AUTH = {**BEARER, "Origin": ORIGIN}
JSON = {**AUTH, "Content-Type": "application/json"}
HANG_GUARD_S = 2.0  # a guard on events that fire at once, never a bound on real work


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable (each turns the gate into `git_unavailable`), and an empty
    home, so the user's global Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


# --- the rig: real services ---------------------------------------------------------------------


@dataclass
class FakeActor:
    live: bool


@dataclass
class Rig:
    tmp_path: Path
    store: SessionStore
    workspaces: WorkspaceService
    git: CodeGit
    coordinator: MutationCoordinator
    resolver: WorkspaceMutationResolver
    roots: SessionRoots
    service: WorktreeService
    workspace_id: str
    root: Path
    area: Path
    actors: dict[str, FakeActor] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)  # sessions the catalog already has

    def app(self, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
        return _app(self.roots, self.service, workers)

    def workspace(self) -> Any:
        return self.workspaces.get(self.workspace_id, kind="code")

    def root_fields(self, **changes: Any) -> dict[str, Any]:
        return {
            "session_id": SESSION,
            "workspace_id": self.workspace_id,
            "worktree_id": None,
            **changes,
        }

    def create_fields(self, branch: str = BRANCH, **changes: Any) -> dict[str, Any]:
        workspace = self.workspace()
        return {
            "policy_revision": workspace.policy_revision,
            "root_physical_id": workspace.root.physical_id,
            "branch": branch,
            **changes,
        }

    def remove_fields(self, **changes: Any) -> dict[str, Any]:
        fields = self.create_fields(**changes)
        del fields["branch"]
        return fields

    def create_url(self, workspace_id: str | None = None) -> str:
        return f"/api/v2/code/{workspace_id or self.workspace_id}/worktrees"

    def put_worktree(
        self,
        worktree_id: str = WT,
        *,
        state: str = "ready",
        folder: bool = True,
        created_at: str = EARLY,
        workspace_id: str | None = None,
    ) -> Path:
        """A row, and its folder, for a worktree Git never made."""
        path = self.tmp_path / "held" / worktree_id
        if folder:
            path.mkdir(parents=True)
        self.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id or self.workspace_id,
                "path": str(path),
                "branch": f"held-{worktree_id[:8]}",
                "base_oid": OID,
                "state": "creating",
                "created_at": created_at,
                "updated_at": created_at,
            },
            limit=worktrees.MAX_WORKTREES,
        )
        if state != "creating":
            self.store.update_worktree_state(worktree_id, "creating", state, created_at)
        return path

    def state(self) -> tuple[Any, Any, list[str]]:
        """Every row and folder a route could change."""
        names = sorted(item.name for item in self.area.iterdir()) if self.area.exists() else []
        return self.store.list_session_roots(100), self.store.list_worktrees(100), names


def _rig(tmp_path: Path, root: Path | None = None, *, trust: bool = True, git: bool = False) -> Rig:
    if root is None:
        root = tmp_path / ROOT_NAME
        root.mkdir()
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(tmp_path, store, git=git)
    if trust:
        workspace_id = trusted(workspaces, root)
    else:
        workspace_id = workspaces.register_existing(COMMAND, str(root), kind="code")[0].workspace_id
    code_git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: git)
    coordinator = MutationCoordinator()
    resolver = WorkspaceMutationResolver(workspaces, git_available=lambda: git)
    actors: dict[str, FakeActor] = {}
    known: set[str] = set()
    roots = SessionRoots(store, workspaces, exists=known.__contains__, actor=actors.get)
    area = worktree_area(tmp_path / "data")
    service = WorktreeService(store, workspaces, code_git, coordinator, resolver, roots, area=area)
    return Rig(
        tmp_path,
        store,
        workspaces,
        code_git,
        coordinator,
        resolver,
        roots,
        service,
        workspace_id,
        root,
        area,
        actors,
        known,
    )


def _committed(tmp_path: Path) -> Path:
    root = _repo(tmp_path / ROOT_NAME)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "dir").mkdir()
    (root / "dir" / "b.txt").write_text("beta\n", encoding="utf-8")
    _commit(root, "first")
    return root


# --- the spy: stands in for both services -------------------------------------------------------


class Spy:
    """Records each call and answers a well-formed result, or raises what it was given. With `gate`
    it waits there, so a test can hold requests in flight."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises = raises
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None
        self.expect = MAX_ROOT_REQUESTS  # `entered` is set by this many calls
        self.finished = 0

    def list(self, limit: int) -> tuple[list[Any], bool]:
        self.calls.append(("list", (limit,)))
        return [], False

    async def _enter(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.entered is not None and len(self.calls) == self.expect:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        self.finished += 1

    @staticmethod
    def _worktree(workspace_id: str, branch: str, state: str) -> Worktree:
        return Worktree(WT, workspace_id, "<folder>", branch, OID, state, EARLY, EARLY)

    async def prepare(self, *args: Any) -> tuple[SessionRoot, bool]:
        await self._enter("prepare", args)
        return SessionRoot(*args), True

    async def create(self, *args: Any) -> Worktree:
        await self._enter("create", args)
        return self._worktree(args[0], args[3], "ready")

    async def remove(self, *args: Any) -> Removal:
        await self._enter("remove", args)
        return Removal(self._worktree(WS, "b", "removed"), "removed", None, None)


def _app(
    roots: Any,
    worktree_service: Any,
    workers: set[asyncio.Task[Any]] | None = None,
    *,
    describe: Callable[[Sequence[str]], Mapping[str, Mapping[str, object]]] | None = None,
) -> Starlette:
    # Leave `describe` out when a test gives none, so the route keeps its own default.
    extra: dict[str, Any] = {} if describe is None else {"describe": describe}
    return Starlette(
        routes=root_routes(
            roots,
            worktree_service,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
            **extra,
        )
    )


def _spied(spy: Spy, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return _app(spy, spy, workers)


# --- helpers ------------------------------------------------------------------------------------


def post(
    client: TestClient,
    url: str,
    fields: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    content: str | bytes | None = None,
) -> Any:  # TestClient's response and httpx's are two unrelated types to pyright
    body = json.dumps(fields) if content is None else content
    return client.post(url, content=body, headers={**JSON, **(headers or {})})


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _refused(response: Any, status: int, code: str, rig: Rig | None = None) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert list(body) == ["error"]
    error = body["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == code and error["message"] and error["message"].isascii()
    if rig is not None:
        # Compare decoded strings: JSON escapes a Windows path's backslashes in the raw text.
        for text in _strings(body):
            assert ROOT_NAME not in text and str(rig.tmp_path) not in text
    assert response.content.isascii()
    return error


def _no_store(response: Any, status: int) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


POSTS: dict[str, tuple[str, dict[str, Any]]] = {
    "record": (ROOTS, {"session_id": SESSION, "workspace_id": WS, "worktree_id": None}),
    "create": (
        f"/api/v2/code/{WS}/worktrees",
        {"policy_revision": 1, "root_physical_id": "1:2", "branch": BRANCH},
    ),
    "remove": (
        f"/api/v2/worktrees/{WT}/remove",
        {"policy_revision": 1, "root_physical_id": "1:2"},
    ),
}
GETS = [ROOTS, WORKTREES]
SERVICE_CALL = {"record": "prepare", "create": "create", "remove": "remove"}


# --- recording a session's root, over the real services -----------------------------------------


def test_post_session_root_records_a_trusted_folder(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    expected = {"session_id": SESSION, "workspace_id": rig.workspace_id, "worktree_id": None}
    with TestClient(rig.app()) as client:
        created = post(client, ROOTS, rig.root_fields())
        again = post(client, ROOTS, rig.root_fields())
        listed = client.get(ROOTS, headers=AUTH)
    assert _no_store(created, 201) == {"root": expected}
    assert _no_store(again, 200) == {"root": expected}
    row = rig.store.get_session_root(SESSION)
    assert row is not None and row["display_root"] == rig.workspace().root.display
    assert _no_store(listed, 200) == {"roots": [expected], "truncated": False}


def test_post_session_root_records_a_ready_worktree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    path = rig.put_worktree()
    with TestClient(rig.app()) as client:
        created = post(client, ROOTS, rig.root_fields(worktree_id=WT))
    body = _no_store(created, 201)
    assert body == {
        "root": {"session_id": SESSION, "workspace_id": rig.workspace_id, "worktree_id": WT}
    }
    row = rig.store.get_session_root(SESSION)
    assert row is not None and row["display_root"] == str(path)
    assert str(path) not in created.text


@pytest.mark.parametrize("state", ["bound", "released"])
@pytest.mark.parametrize("thread", [False, True])
def test_crafted_root_request_cannot_claim_a_bot_copy(
    tmp_path: Path, state: str, thread: bool
) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree()
    bot_copy(rig.store, rig.workspace_id, WT, state=state, thread=thread)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields(worktree_id=WT))
    error = _refused(response, 409, "bot_copy_root", rig)
    assert error["message"] == "This worktree belongs to a Bot chat. Open that chat instead."
    assert rig.state() == before


def test_a_different_root_for_a_recorded_session_is_409_session_exists(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree()
    with TestClient(rig.app()) as client:
        assert post(client, ROOTS, rig.root_fields()).status_code == 201
        before = rig.state()
        response = post(client, ROOTS, rig.root_fields(worktree_id=WT))
    _refused(response, 409, "session_exists", rig)
    assert rig.state() == before


@pytest.mark.parametrize("how", ["known", "live"])
def test_a_session_the_app_already_has_is_409_session_exists(tmp_path: Path, how: str) -> None:
    rig = _rig(tmp_path)
    if how == "known":
        rig.known.add(SESSION)
    else:
        rig.actors[SESSION] = FakeActor(live=False)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields())
    _refused(response, 409, "session_exists", rig)
    assert rig.state() == before


def test_an_untrusted_folder_is_409_trust_required(tmp_path: Path) -> None:
    rig = _rig(tmp_path, trust=False)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields())
    _refused(response, 409, "trust_required", rig)
    assert rig.state() == before


def test_a_vault_or_unknown_workspace_is_404_unknown_workspace(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    vault_root = tmp_path / "notes"
    vault_root.mkdir()
    vault, _ = rig.workspaces.register_existing(COMMAND, str(vault_root))
    before = rig.state()
    with TestClient(rig.app()) as client:
        for workspace_id in (vault.workspace_id, WS):
            response = post(client, ROOTS, rig.root_fields(workspace_id=workspace_id))
            _refused(response, 404, "unknown_workspace", rig)
    assert rig.state() == before


def test_an_unknown_or_other_folders_worktree_is_404_unknown_worktree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_id = trusted(rig.workspaces, other)
    rig.put_worktree(workspace_id=other_id)
    before = rig.state()
    with TestClient(rig.app()) as client:
        for worktree_id in (WT, "99999999-9999-4999-8999-999999999999"):
            response = post(client, ROOTS, rig.root_fields(worktree_id=worktree_id))
            _refused(response, 404, "unknown_worktree", rig)
    assert rig.state() == before


@pytest.mark.parametrize("state", ["creating", "removing", "removed", "kept"])
def test_a_worktree_that_is_not_ready_is_409_worktree_not_ready(tmp_path: Path, state: str) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree(state=state)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields(worktree_id=WT))
    _refused(response, 409, "worktree_not_ready", rig)
    assert rig.state() == before


def test_a_ready_worktree_without_its_folder_is_409_worktree_not_ready(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree(folder=False)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields(worktree_id=WT))
    _refused(response, 409, "worktree_not_ready", rig)
    assert rig.state() == before


def test_a_folder_that_moved_is_409_root_unavailable(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.root.rename(tmp_path / "moved")
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, ROOTS, rig.root_fields())
    _refused(response, 409, "root_unavailable", rig)
    assert rig.state() == before


# --- the two lists ------------------------------------------------------------------------------


def test_get_session_roots_lists_the_newest_first(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    for session, stamp in ((SESSION, EARLY), (OTHER_SESSION, LATE)):
        rig.store.put_session_root(
            {
                "session_id": session,
                "workspace_id": rig.workspace_id,
                "worktree_id": None,
                "display_root": rig.workspace().root.display,
                "created_at": stamp,
            }
        )
    with TestClient(rig.app()) as client:
        body = _no_store(client.get(ROOTS, headers=AUTH), 200)
    assert [root["session_id"] for root in body["roots"]] == [OTHER_SESSION, SESSION]
    assert all(set(root) == {"session_id", "workspace_id", "worktree_id"} for root in body["roots"])
    assert body["truncated"] is False


def test_get_session_roots_says_when_rows_were_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    for session in (SESSION, OTHER_SESSION):
        rig.store.put_session_root(
            {
                "session_id": session,
                "workspace_id": rig.workspace_id,
                "worktree_id": None,
                "display_root": rig.workspace().root.display,
                "created_at": EARLY,
            }
        )
    monkeypatch.setattr(roots_api, "MAX_ROOT_ROWS", 1)
    with TestClient(rig.app()) as client:
        body = _no_store(client.get(ROOTS, headers=AUTH), 200)
    assert len(body["roots"]) == 1 and body["truncated"] is True


def test_get_worktrees_lists_non_removed_states_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    older = "55555555-5555-4555-8555-555555555555"
    rig.put_worktree(older, state="kept", created_at=EARLY)
    rig.put_worktree(WS, state="removed", created_at=LATE)
    path = rig.put_worktree(WT, state="ready", created_at=LATE)
    with TestClient(rig.app()) as client:
        response = client.get(WORKTREES, headers=AUTH)
        body = _no_store(response, 200)
        monkeypatch.setattr(roots_api, "MAX_WORKTREE_ROWS", 1)
        cut = client.get(WORKTREES, headers=AUTH).json()
    assert [w["worktree_id"] for w in body["worktrees"]] == [WT, older]
    assert [w["state"] for w in body["worktrees"]] == ["ready", "kept"]
    assert all(set(w) == W_KEYS for w in body["worktrees"])
    assert body["worktrees"][0]["path"] == str(path)
    assert body["truncated"] is False
    assert len(cut["worktrees"]) == 1 and cut["truncated"] is True


def test_old_active_worktree_survives_more_than_4096_removed_rows(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree(created_at=EARLY)
    with rig.store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO worktrees VALUES (?,?,?,?,?,'removed',?,?)",
            (
                (
                    str(uuid.UUID(int=index)),
                    rig.workspace_id,
                    f"<removed>/{index}",
                    "old",
                    OID,
                    LATE,
                    LATE,
                )
                for index in range(4097)
            ),
        )
    with TestClient(rig.app()) as client:
        body = _no_store(client.get(WORKTREES, headers=AUTH), 200)
    assert [row["worktree_id"] for row in body["worktrees"]] == [WT]
    assert body["truncated"] is False


@pytest.mark.parametrize("state", ["bound", "released"])
@pytest.mark.parametrize("kind", ["session", "task", "thread"])
def test_worktree_bot_description_is_optional_json_data(
    tmp_path: Path, state: str, kind: str
) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree()
    rig.put_worktree(WS)
    bot_copy(rig.store, rig.workspace_id, WT, state=state, thread=kind == "thread")
    description = {
        "kind": kind,
        "bot_name": '<script>"Bot"</script>\n\u2603',
        "title": None if kind == "task" else '<img src=x onerror="alert(1)">',
        "released": state == "released",
    }
    calls: list[tuple[str, ...]] = []

    def describe(worktree_ids: Sequence[str]) -> Mapping[str, Mapping[str, object]]:
        calls.append(tuple(worktree_ids))
        return {WT: description}

    with TestClient(_app(rig.roots, rig.service, describe=describe)) as client:
        response = client.get(WORKTREES, headers=AUTH)
    rows = _no_store(response, 200)["worktrees"]
    assert len(calls) == 1 and sorted(calls[0]) == sorted([WT, WS])
    assert response.content.isascii() and b"\\u2603" in response.content
    assert response.headers["content-type"] == "application/json"
    projected = {row["worktree_id"]: row for row in rows}
    assert projected[WT]["bot_copy"] == description
    assert set(projected[WT]) == W_KEYS | {"bot_copy"}
    assert set(projected[WS]) == W_KEYS
    with TestClient(rig.app()) as client:
        legacy = _no_store(client.get(WORKTREES, headers=AUTH), 200)
    assert all(set(row) == W_KEYS for row in legacy["worktrees"])


def test_bot_description_keeps_unknown_labels_nullable(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.put_worktree()
    description = {"kind": "session", "bot_name": None, "title": None, "released": True}
    with TestClient(
        _app(rig.roots, rig.service, describe=lambda _ids: {WT: description})
    ) as client:
        body = _no_store(client.get(WORKTREES, headers=AUTH), 200)
    assert body["worktrees"][0]["bot_copy"] == description


def test_the_lists_are_empty_on_a_new_store(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    with TestClient(rig.app()) as client:
        assert _no_store(client.get(ROOTS, headers=AUTH), 200) == {"roots": [], "truncated": False}
        assert _no_store(client.get(WORKTREES, headers=AUTH), 200) == {
            "worktrees": [],
            "truncated": False,
        }


# --- create and remove, over real Git --------------------------------------------------------


@needs_git
def test_create_answers_201_with_the_worktree_and_lists_it(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields())
        assert response.status_code == 201
        before = rig.state()
        duplicate = post(client, rig.create_url(), rig.create_fields())
        _refused(duplicate, 409, "branch_exists", rig)
        assert rig.state() == before
        listed = client.get(WORKTREES, headers=AUTH)
    body = _no_store(response, 201)
    made = body["worktree"]
    assert list(body) == ["worktree"] and set(made) == W_KEYS
    assert (made["workspace_id"], made["branch"], made["state"]) == (
        rig.workspace_id,
        BRANCH,
        "ready",
    )
    assert made["path"] == str(rig.area / made["worktree_id"]) and Path(made["path"]).is_dir()
    assert made["base_oid"] == _git(rig.root, "rev-parse", "HEAD").strip()
    assert listed.json() == {"worktrees": [made], "truncated": False}
    # A session can now record the new folder as its root.
    with TestClient(rig.app()) as client:
        recorded = post(client, ROOTS, rig.root_fields(worktree_id=made["worktree_id"]))
    assert recorded.status_code == 201


@needs_git
def test_create_with_a_stale_revision_or_root_is_409_workspace_changed(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    before = rig.state()
    changes: list[dict[str, Any]] = [{"policy_revision": 999}, {"root_physical_id": "1:2"}]
    with TestClient(rig.app()) as client:
        for change in changes:
            response = post(client, rig.create_url(), rig.create_fields(**change))
            _refused(response, 409, "workspace_changed", rig)
    assert rig.state() == before


@needs_git
async def test_create_while_the_root_is_leased_is_423_vault_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    monkeypatch.setattr(worktrees, "LEASE_WAIT_S", 0.05)
    workspace = rig.workspace()
    root = rig.resolver(
        rig.workspace_id, workspace.policy_revision, workspace.root.physical_id, kind="code"
    )
    before = rig.state()
    transport = httpx.ASGITransport(app=rig.app())
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        rig.coordinator.lease(root, ()),
    ):
        response = await client.post(
            rig.create_url(), content=json.dumps(rig.create_fields()), headers=JSON
        )
    _refused(response, 423, "vault_locked", rig)
    assert rig.state() == before


@needs_git
def test_remove_deletes_a_clean_worktree_and_keeps_a_dirty_one(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    with TestClient(rig.app()) as client:
        clean = post(client, rig.create_url(), rig.create_fields()).json()["worktree"]
        dirty = post(client, rig.create_url(), rig.create_fields("viki/dirty")).json()["worktree"]
        (Path(dirty["path"]) / "new.txt").write_text("new\n", encoding="utf-8")

        removed = post(
            client, f"/api/v2/worktrees/{clean['worktree_id']}/remove", rig.remove_fields()
        )
        kept = post(client, f"/api/v2/worktrees/{dirty['worktree_id']}/remove", rig.remove_fields())
        listed = client.get(WORKTREES, headers=AUTH).json()

    body = _no_store(removed, 200)
    assert list(body) == ["worktree", "outcome", "reason", "changes"]
    assert (body["outcome"], body["reason"], body["changes"]) == ("removed", None, None)
    assert body["worktree"] == {
        **clean,
        "state": "removed",
        "updated_at": body["worktree"]["updated_at"],
    }
    assert not Path(clean["path"]).exists()
    body = _no_store(kept, 200)
    assert (body["outcome"], body["reason"], body["changes"]) == ("kept", "dirty", 1)
    assert body["worktree"]["state"] == "ready"
    assert (Path(dirty["path"]) / "new.txt").is_file()
    states = {w["worktree_id"]: w["state"] for w in listed["worktrees"]}
    assert states == {dirty["worktree_id"]: "ready"}


@needs_git
@pytest.mark.parametrize("thread", [False, True])
async def test_live_bot_task_or_thread_prevents_remove_and_sweep(
    tmp_path: Path, thread: bool
) -> None:
    actors: dict[str, FakeActor] = {}
    rig = sweep_rig(tmp_path, actor=actors.get)
    made = await rig.create()
    tree = Path(made.path)
    _age(tree, PAST)
    bot_copy(rig.store, rig.workspace_id, made.worktree_id, thread=thread)
    actors[FORK if thread else SESSION] = FakeActor(live=True)
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    with pytest.raises(WorktreeError) as refused:
        await rig.service.remove(
            made.worktree_id, workspace.policy_revision, workspace.root.physical_id
        )
    assert refused.value.code == "worktree_in_use"
    decisions = await rig.service.sweep()
    assert [(row.action, row.reason) for row in decisions] == [("keep", "live")]
    assert rig.state(made.worktree_id) == "ready" and tree.is_dir()
    assert not rig.roots.blocked(made.worktree_id)
    actors.clear()
    with rig.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE bot_code_copies SET state='released' WHERE worktree_id=?",
            (made.worktree_id,),
        )
    decisions = await rig.service.sweep()
    assert [(row.action, row.reason) for row in decisions] == [("remove", "merged")]
    assert rig.state(made.worktree_id) == "removed" and not tree.exists()


@needs_git
async def test_archived_changes_remain_in_restore_list_not_public_list(tmp_path: Path) -> None:
    rig = sweep_rig(tmp_path)
    made = await rig.create()
    tree = Path(made.path)
    (tree / "new.txt").write_text("synthetic change\n", encoding="utf-8")
    _age(tree, PAST)
    decisions = await rig.service.sweep()
    assert [(row.action, row.reason) for row in decisions] == [("archive", "dirty")]
    with TestClient(_app(rig.roots, rig.service)) as client:
        listed = _no_store(client.get(WORKTREES, headers=AUTH), 200)
        archives = _no_store(
            client.get(f"/api/v2/code/{rig.workspace_id}/worktree-archives", headers=AUTH), 200
        )
    assert listed == {"worktrees": [], "truncated": False}
    assert archives["enabled"] is True
    assert [row["worktree_id"] for row in archives["archives"]] == [made.worktree_id]


@needs_git
def test_remove_with_a_stale_revision_is_409_and_removes_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    with TestClient(rig.app()) as client:
        made = post(client, rig.create_url(), rig.create_fields()).json()["worktree"]
        before = rig.state()
        response = post(
            client,
            f"/api/v2/worktrees/{made['worktree_id']}/remove",
            rig.remove_fields(policy_revision=999),
        )
    _refused(response, 409, "workspace_changed", rig)
    assert rig.state() == before and Path(made["path"]).is_dir()


# --- refusals before the lease, on every platform that has Git ----------------------------------


def test_a_script_branch_is_400_invalid_branch_and_no_answer_holds_a_path(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    before = rig.state()
    branch = "<script>alert('x')</script>"
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields(branch))
    error = _refused(response, 400, "invalid_branch", rig)
    assert "script" not in error["message"] and "script" not in response.text
    assert rig.state() == before


def test_create_in_an_untrusted_folder_is_409_trust_required(tmp_path: Path) -> None:
    rig = _rig(tmp_path, trust=False)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields())
    _refused(response, 409, "trust_required", rig)
    assert rig.state() == before


def test_create_in_a_vault_or_unknown_workspace_is_404(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    vault_root = tmp_path / "notes"
    vault_root.mkdir()
    vault, _ = rig.workspaces.register_existing(COMMAND, str(vault_root))
    before = rig.state()
    with TestClient(rig.app()) as client:
        for workspace_id in (vault.workspace_id, WS):
            response = post(client, rig.create_url(workspace_id), rig.create_fields())
            _refused(response, 404, "unknown_workspace", rig)
    assert rig.state() == before


def test_create_without_git_is_503_git_unavailable(tmp_path: Path) -> None:
    rig = _rig(tmp_path, git=False)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields())
    _refused(response, 503, "git_unavailable", rig)
    assert rig.state() == before


@needs_git
def test_create_in_a_folder_that_is_not_a_repository_is_409(tmp_path: Path) -> None:
    rig = _rig(tmp_path, git=True)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields())
    _refused(response, 409, "not_repository", rig)
    assert rig.state() == before


@needs_git
def test_create_in_a_repository_without_commits_is_409_no_commits(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _repo(tmp_path / ROOT_NAME), git=True)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, rig.create_url(), rig.create_fields())
    _refused(response, 409, "no_commits", rig)
    assert rig.state() == before


def test_remove_of_an_unknown_worktree_is_404_unknown_worktree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, f"/api/v2/worktrees/{WT}/remove", rig.remove_fields())
    _refused(response, 404, "unknown_worktree", rig)
    assert rig.state() == before


@needs_git
@pytest.mark.parametrize("state", ["creating", "removing", "removed"])
def test_remove_of_a_worktree_that_is_not_ready_is_409(tmp_path: Path, state: str) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    rig.put_worktree(state=state)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, f"/api/v2/worktrees/{WT}/remove", rig.remove_fields())
    _refused(response, 409, "worktree_not_ready", rig)
    assert rig.state() == before


@needs_git
def test_remove_while_a_session_runs_there_is_409_worktree_in_use(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    path = rig.put_worktree()
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": WT,
            "display_root": str(path),
            "created_at": now_iso(),
        }
    )
    rig.actors[SESSION] = FakeActor(live=True)
    before = rig.state()
    with TestClient(rig.app()) as client:
        response = post(client, f"/api/v2/worktrees/{WT}/remove", rig.remove_fields())
    _refused(response, 409, "worktree_in_use", rig)
    assert rig.state() == before and path.is_dir()


@pytest.mark.parametrize("route", ["create", "remove"])
def test_off_windows_create_and_remove_are_409_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A drive the publication service does not back refuses create outright, and refuses the
    remove of a worktree already made there, in each case changing nothing durable."""
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    with TestClient(rig.app()) as client:
        if route == "create":
            before = rig.state()
            monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)
            response = post(client, rig.create_url(), rig.create_fields())
            _refused(response, 409, "unsupported_filesystem", rig)
            assert rig.state() == before
        else:
            made = post(client, rig.create_url(), rig.create_fields()).json()["worktree"]
            monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)
            response = post(
                client, f"/api/v2/worktrees/{made['worktree_id']}/remove", rig.remove_fields()
            )
            _refused(response, 409, "unsupported_filesystem", rig)
            rows, _ = rig.store.list_worktrees(100)
            assert [row["state"] for row in rows] == ["ready"]
            assert Path(made["path"]).is_dir()


# --- the gate: Bearer, Origin, ids, query, over the spy -----------------------------------------


@pytest.mark.parametrize("route", POSTS)
def test_a_post_needs_bearer(route: str) -> None:
    url, fields = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        for headers in ({"Origin": ORIGIN}, {"Authorization": "Bearer other", "Origin": ORIGIN}):
            response = client.post(
                url,
                content=json.dumps(fields),
                headers={**headers, "Content-Type": "application/json"},
            )
            _refused(response, 401, "unauthorized")
    assert spy.calls == []


@pytest.mark.parametrize("url", GETS)
def test_a_get_needs_bearer(url: str) -> None:
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        for headers in ({}, {"Authorization": "Bearer other"}):
            _refused(client.get(url, headers=headers), 401, "unauthorized")
    assert spy.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_post_needs_a_listed_origin(route: str) -> None:
    url, fields = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        absent = client.post(
            url,
            content=json.dumps(fields),
            headers={**BEARER, "Content-Type": "application/json"},
        )
        _refused(absent, 403, "forbidden_origin")
        foreign = post(client, url, fields, headers={"Origin": "https://evil.invalid"})
        _refused(foreign, 403, "forbidden_origin")
        assert spy.calls == []
        assert post(client, url, fields).status_code in (200, 201)
    assert len(spy.calls) == 1


@pytest.mark.parametrize("url", GETS)
def test_a_get_may_omit_origin_but_not_name_a_foreign_one(url: str) -> None:
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _no_store(client.get(url, headers=BEARER), 200)
        _no_store(client.get(url, headers=AUTH), 200)
        assert len(spy.calls) == 2
        foreign = client.get(url, headers={**BEARER, "Origin": "https://evil.invalid"})
        _refused(foreign, 403, "forbidden_origin")
    assert len(spy.calls) == 2


@pytest.mark.parametrize(
    ("route", "build"),
    [
        ("create", lambda raw: f"/api/v2/code/{raw}/worktrees"),
        ("remove", lambda raw: f"/api/v2/worktrees/{raw}/remove"),
    ],
)
def test_a_non_canonical_path_id_is_404_before_the_query_or_the_body(
    route: str, build: Callable[[str], str]
) -> None:
    _, fields = POSTS[route]
    unknown = "unknown_workspace" if route == "create" else "unknown_worktree"
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        for raw in (
            ALPHA.upper(),
            f"{{{ALPHA}}}",
            ALPHA.replace("-", ""),
            f"urn:uuid:{ALPHA}",
            "w",
        ):
            _refused(post(client, build(raw), fields), 404, unknown)
            _refused(post(client, build(raw) + "?x=1", content="{"), 404, unknown)
    assert spy.calls == []


@pytest.mark.parametrize("query", ["?x=1", "?session_id=" + SESSION, "?a&b", "?=", "?%00"])
@pytest.mark.parametrize("route", POSTS)
def test_any_query_key_on_a_post_is_400_invalid_query(route: str, query: str) -> None:
    url, fields = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, url + query, fields), 400, "invalid_query")
    assert spy.calls == []


@pytest.mark.parametrize("query", ["?x=1", "?limit=5", "?a"])
@pytest.mark.parametrize("url", GETS)
def test_any_query_key_on_a_get_is_400_invalid_query(url: str, query: str) -> None:
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(client.get(url + query, headers=AUTH), 400, "invalid_query")
    assert spy.calls == []


def test_another_method_is_405() -> None:
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        for method, url in (
            ("PUT", ROOTS),
            ("DELETE", ROOTS),
            ("POST", WORKTREES),
            ("GET", POSTS["create"][0]),
            ("GET", POSTS["remove"][0]),
        ):
            assert client.request(method, url, headers=JSON).status_code == 405
    assert spy.calls == []


# --- the body -----------------------------------------------------------------------------------

DROP = object()
NOT_UUIDS: list[Any] = [
    "",
    "x",
    ALPHA.upper(),
    f"{{{ALPHA}}}",
    ALPHA.replace("-", ""),
    5,
    [ALPHA],
]
BAD_VALUES: dict[str, list[Any]] = {
    "session_id": [*NOT_UUIDS, None],
    "workspace_id": [*NOT_UUIDS, None],
    "worktree_id": [*NOT_UUIDS, True],
    "policy_revision": [0, -1, 9_007_199_254_740_992, True, "1", 1.0, None],
    "root_physical_id": ["", "1", "1:", "a:1", "1:" + "2" * 80, "1:2\n", 12, None],
    "branch": ["", "a" * 101, 5, None, ["a"]],
}


def _bad_bodies() -> list[Any]:
    cases = []
    for route, (_, fields) in POSTS.items():
        for key in fields:
            for index, value in enumerate([DROP, *BAD_VALUES[key]]):
                cases.append(pytest.param(route, key, value, id=f"{route}-{key}-{index}"))
        cases.append(pytest.param(route, "extra", 1, id=f"{route}-extra"))
    return cases


@pytest.mark.parametrize(("route", "key", "value"), _bad_bodies())
def test_a_body_of_any_other_shape_is_400_invalid_body(route: str, key: str, value: Any) -> None:
    url, fields = POSTS[route]
    if value is DROP:
        body = {name: item for name, item in fields.items() if name != key}
    else:
        body = {**fields, key: value}
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, url, body), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '"x"', "5", "NaN", b"\xff\xfe"])
def test_a_body_that_is_not_one_json_object_is_400_invalid_body(
    route: str, raw: str | bytes
) -> None:
    url, _ = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_duplicate_key_is_400_invalid_body(route: str) -> None:
    url, fields = POSTS[route]
    first = next(iter(fields))
    raw = json.dumps(fields)[:-1] + "," + json.dumps({first: fields[first]})[1:]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
def test_another_content_type_or_encoding_is_400_invalid_body(
    route: str, headers: dict[str, str]
) -> None:
    url, fields = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        response = client.post(url, content=json.dumps(fields), headers={**AUTH, **headers})
        _refused(response, 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_body_over_4096_bytes_is_413_too_large_and_exactly_4096_passes(route: str) -> None:
    url, fields = POSTS[route]
    assert MAX_BODY_BYTES == 4096
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        too_big = post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES + 1))
        _refused(too_big, 413, "too_large")
        assert spy.calls == []
        padded = post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES))
        assert padded.status_code in (200, 201)
    assert [name for name, _ in spy.calls] == [SERVICE_CALL[route]]


def test_the_body_reaches_the_service_as_the_client_sent_it() -> None:
    spy = Spy()
    fields = {"policy_revision": 7, "root_physical_id": "12:345", "branch": "viki/x"}
    with TestClient(_spied(spy)) as client:
        created = post(client, POSTS["create"][0], fields)
        removed = post(
            client, POSTS["remove"][0], {k: v for k, v in fields.items() if k != "branch"}
        )
        recorded = post(
            client, ROOTS, {"session_id": SESSION, "workspace_id": WS, "worktree_id": WT}
        )
    assert created.status_code == 201 and removed.status_code == 200 and recorded.status_code == 201
    assert spy.calls == [
        ("create", (WS, 7, "12:345", "viki/x")),
        ("remove", (WT, 7, "12:345")),
        ("prepare", (SESSION, WS, WT)),
    ]
    assert removed.json() == {
        "worktree": {
            "worktree_id": WT,
            "workspace_id": WS,
            "path": "<folder>",
            "branch": "b",
            "base_oid": OID,
            "state": "removed",
            "created_at": EARLY,
            "updated_at": EARLY,
        },
        "outcome": "removed",
        "reason": None,
        "changes": None,
    }


# --- every code a service can raise -------------------------------------------------------------

ERRORS: list[tuple[Exception, int, str]] = [
    *[
        (RootError(code), status, code)
        for code, status in (
            ("session_exists", 409),
            ("unknown_workspace", 404),
            ("trust_required", 409),
            ("root_unavailable", 409),
            ("unknown_worktree", 404),
            ("worktree_not_ready", 409),
            ("bot_copy_root", 409),
        )
    ],
    *[
        (WorktreeError(code), status, code)
        for code, status in (
            ("invalid_branch", 400),
            ("unknown_workspace", 404),
            ("unknown_worktree", 404),
            ("trust_required", 409),
            ("workspace_changed", 409),
            ("root_unavailable", 409),
            ("not_repository", 409),
            ("not_repository_root", 409),
            ("no_commits", 409),
            ("branch_exists", 409),
            ("worktree_limit", 409),
            ("worktree_not_ready", 409),
            ("worktree_busy", 409),
            ("worktree_in_use", 409),
            ("unsupported_filesystem", 409),
            ("git_common_unavailable", 409),
            ("vault_locked", 423),
            ("git_unavailable", 503),
            ("git_failed", 503),
            ("unavailable", 503),
        )
    ],
    (CodeError("not_repository"), 409, "not_repository"),
    (CodeError("git_unavailable"), 503, "git_unavailable"),
    (CodeError("unknown_workspace"), 404, "unknown_workspace"),
    (MutationError("ordinary_workspace_required"), 409, "trust_required"),
    (MutationError("stale_root"), 409, "root_unavailable"),
    (MutationError("git_common_unsafe"), 409, "git_common_unavailable"),
    (MutationError("workspace_changed"), 409, "workspace_changed"),
    (MutationError("vault_locked"), 423, "vault_locked"),
    (WorktreeError("ordinary_workspace_required"), 409, "trust_required"),
    (WorktreeError("stale_root"), 409, "root_unavailable"),
    (WorktreeError("git_common_unsafe"), 409, "git_common_unavailable"),
    (StateError("unavailable"), 503, "unavailable"),
    (RootError("a_code_nobody_mapped"), 503, "unavailable"),
    (MutationError("a_code_nobody_mapped"), 503, "unavailable"),
]
ROUTE_ERRORS: list[tuple[Exception, int, str]] = [
    (RootError("session_exists"), 409, "session_exists"),
    (WorktreeError("invalid_branch"), 400, "invalid_branch"),
    (CodeError("not_repository"), 409, "not_repository"),
    (MutationError("stale_root"), 409, "root_unavailable"),
    (StateError("unavailable"), 503, "unavailable"),
]


# The endpoint shares its mapper; the other POSTs still cover every caught exception class.
@pytest.mark.parametrize(
    ("raised", "status", "code", "route"),
    [
        pytest.param(
            error,
            status,
            code,
            route,
            id=f"{type(error).__name__}-{getattr(error, 'code', '')}-{route}",
        )
        for route, cases in (
            ("record", ERRORS),
            ("create", ROUTE_ERRORS),
            ("remove", ROUTE_ERRORS),
        )
        for error, status, code in cases
    ],
)
def test_each_service_refusal_has_its_status_and_a_fixed_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    route: str,
    raised: RootError | WorktreeError | CodeError | MutationError | StateError,
    status: int,
    code: str,
) -> None:
    url, fields = POSTS[route]
    raised.args = (str(tmp_path / ROOT_NAME / "private-marker"),)
    spy = Spy(raised)
    with caplog.at_level(logging.WARNING), TestClient(_spied(spy)) as client:
        response = post(client, url, fields)
    error = _refused(response, status, code)
    assert error["message"] == {
        "branch_exists": "A branch with this name already exists. Choose a different name.",
        "worktree_limit": "You've reached the worktree limit. Remove one first.",
        "not_repository_root": "This isn't the repository's top-level folder.",
    }.get(code, roots_api._MESSAGES[code])
    for text in _strings(response.json()):
        assert ROOT_NAME not in text and str(tmp_path) not in text
    assert raised.code not in error["message"]
    assert not isinstance(raised, StateError) or raised.message not in error["message"]
    assert str(raised) not in error["message"] and error["message"] != str(raised)
    assert [r for r in caplog.records if r.name == roots_api.__name__] == []
    assert len(spy.calls) == 1


@pytest.mark.parametrize("route", POSTS)
def test_anything_else_is_503_unavailable_with_one_warning_naming_the_type(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    url, fields = POSTS[route]
    spy = Spy(OSError("C:\\secret\\place\\token"))
    with caplog.at_level(logging.WARNING), TestClient(_spied(spy)) as client:
        response = post(client, url, fields)
    error = _refused(response, 503, "unavailable")
    assert "secret" not in error["message"] and "secret" not in response.text
    records = [r for r in caplog.records if r.name == roots_api.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "roots route failed (OSError)")
    ]


def test_a_service_that_fails_on_a_list_is_503_unavailable() -> None:
    class Broken(Spy):
        def list(self, limit: int) -> tuple[list[Any], bool]:
            raise StateError("unavailable")

    with TestClient(_spied(Broken())) as client:
        for url in GETS:
            _refused(client.get(url, headers=AUTH), 503, "unavailable")


# --- intake clock, disconnect, capacity ---------------------------------------------------------


async def _drive(
    app: Starlette, receive: Callable[[], Awaitable[dict[str, Any]]]
) -> tuple[int, dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": ROOTS,
        "raw_path": ROOTS.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"authorization", b"Bearer secret"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body)


async def test_a_body_that_never_arrives_is_408_intake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy()
    real_fail_after = roots_api.anyio.fail_after
    monkeypatch.setattr(roots_api.anyio, "fail_after", lambda _seconds: real_fail_after(0.02))

    async def never() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    status, body = await _drive(_spied(spy), never)
    assert (status, body["error"]["code"]) == (408, "intake_timeout")
    assert spy.calls == []


async def test_a_client_that_leaves_mid_body_is_400_disconnected() -> None:
    spy = Spy()
    sent_part = False

    async def leaves() -> dict[str, Any]:
        nonlocal sent_part
        if not sent_part:
            sent_part = True
            return {"type": "http.request", "body": b'{"session', "more_body": True}
        return {"type": "http.disconnect"}

    status, body = await _drive(_spied(spy), leaves)
    assert (status, body["error"]["code"]) == (400, "disconnected")
    assert spy.calls == []


async def test_a_fifth_request_is_429_overloaded_for_any_route_and_the_permits_come_back() -> None:
    spy = Spy()
    spy.gate, spy.entered = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_spied(spy, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS["create"]

        async def create() -> httpx.Response:
            return await client.post(url, content=json.dumps(fields), headers=JSON)

        calls = [asyncio.create_task(create()) for _ in range(MAX_ROOT_REQUESTS)]
        await asyncio.wait_for(spy.entered.wait(), HANG_GUARD_S)
        assert len(workers) == MAX_ROOT_REQUESTS
        _refused(await create(), 429, "overloaded")
        _refused(await client.get(ROOTS, headers=AUTH), 429, "overloaded")
        _refused(await client.get(WORKTREES, headers=AUTH), 429, "overloaded")
        spy.gate.set()
        assert all(r.status_code == 201 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        assert (await create()).status_code == 201
        assert (await client.get(ROOTS, headers=AUTH)).status_code == 200


async def test_a_client_that_leaves_does_not_cancel_the_operation() -> None:
    spy = Spy()
    spy.gate, spy.entered, spy.expect = asyncio.Event(), asyncio.Event(), 1
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_spied(spy, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS["remove"]
        call = asyncio.create_task(client.post(url, content=json.dumps(fields), headers=JSON))
        await asyncio.wait_for(spy.entered.wait(), HANG_GUARD_S)
        assert len(workers) == 1
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert spy.finished == 0 and len(workers) == 1
        spy.gate.set()
        await asyncio.wait_for(asyncio.gather(*workers), HANG_GUARD_S)
        await asyncio.sleep(0)
        assert spy.finished == 1 and not workers
