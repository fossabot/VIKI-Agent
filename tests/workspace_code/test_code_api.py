"""The code-folder read routes over real services in temporary folders."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.app.test_workspaces import COMMAND, service_at
from tests.workspace_code.test_code_git import _commit, _git, _repo, needs_git
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code import code_search
from viki_agent.code.code_api import MAX_QUERY_BYTES, code_routes
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
ROOT_NAME = "secret-root-ZZZ"
CODE = "/api/v2/code"


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


@dataclass
class Env:
    files: CodeFiles
    git: CodeGit
    workspace_id: str
    root: Path

    def app(self, workers: set[asyncio.Task[object]] | None = None) -> Starlette:
        return Starlette(
            routes=code_routes(
                self.files,
                self.git,
                authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
                origins=frozenset({ORIGIN}),
                workers=set() if workers is None else workers,  # type: ignore[arg-type]
            )
        )

    def url(self, route: str, workspace_id: str | None = None) -> str:
        return f"{CODE}/{workspace_id or self.workspace_id}/{route}"


def _env(
    tmp_path: Path,
    root: Path | None = None,
    *,
    trust: bool = True,
    git_available: bool = True,
) -> Env:
    folder = root if root is not None else tmp_path / ROOT_NAME
    folder.mkdir(parents=True, exist_ok=True)
    service = service_at(tmp_path)
    if trust:
        workspace_id = trusted(service, folder)
    else:
        workspace_id = service.register_existing(COMMAND, str(folder), kind="code")[0].workspace_id
    files = CodeFiles(service)
    git = CodeGit(service, files, git_available=lambda: git_available)
    return Env(files, git, workspace_id, folder)


def _plain(tmp_path: Path, **options: bool) -> Env:
    root = tmp_path / ROOT_NAME
    (root / "dir").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"hello\n")
    (root / ".npmrc").write_text("SYNTHETIC=1\n", encoding="utf-8")
    return _env(tmp_path, root, **options)


def _committed(tmp_path: Path) -> Env:
    root = _repo(tmp_path / ROOT_NAME)
    (root / "src").mkdir()
    (root / "a.txt").write_bytes(b"hello\n")
    (root / "src" / "main.py").write_bytes(b"print('x')\n")
    (root / "日本語").mkdir()
    (root / "日本語" / "メモ.txt").write_bytes("牛乳\n".encode())
    _commit(root, "first")
    return _env(tmp_path, root)


def _error(response: Any, status: int, code: str, env: Env) -> None:
    """`response` is Starlette's or httpx's, two unrelated types to pyright."""
    assert response.status_code == status
    body = response.json()
    assert list(body) == ["error"] and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    # No answer names the folder, its parent or its location.
    assert ROOT_NAME not in response.text and str(env.root.parent) not in response.text
    assert response.content.isascii()


# --- Bearer, Origin, UUID, query keys ---------------------------------------------------------

ROUTES = ("tree", "file?path=a.txt", "search?q=a", "changes", "diff?path=a.txt")


def test_routes_need_bearer(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for route in ROUTES:
            response = client.get(env.url(route))
            _error(response, 401, "unauthorized", env)
            wrong = client.get(env.url(route), headers={"Authorization": "Bearer other"})
            _error(wrong, 401, "unauthorized", env)


def test_a_foreign_origin_is_403_and_a_listed_one_passes(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for route in ROUTES:
            foreign = client.get(env.url(route), headers={**AUTH, "Origin": "https://evil.invalid"})
            _error(foreign, 403, "forbidden_origin", env)
        listed = client.get(env.url("tree"), headers={**AUTH, "Origin": ORIGIN})
        assert listed.status_code == 200


def test_a_non_canonical_workspace_id_is_404_before_any_service_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _plain(tmp_path)

    def boom(*_args: object) -> object:
        raise AssertionError("a service was called")

    monkeypatch.setattr(env.files, "tree", boom)
    real = env.workspace_id
    with TestClient(env.app()) as client:
        for raw in (real.upper(), f"{{{real}}}", real.replace("-", ""), f"urn:uuid:{real}", "w"):
            _error(client.get(env.url("tree", raw), headers=AUTH), 404, "unknown_workspace", env)


@pytest.mark.parametrize(
    ("route", "query"),
    [
        ("tree", "?x=1"),
        ("tree", "?path=a&path=b"),
        ("tree", "?path"),
        ("tree", "?path=a&x=1"),
        ("file", ""),
        ("file", "?q=a"),
        ("file", "?path=%ZZ"),
        ("file", "?path=%FF"),
        ("file", "?path=%4"),
        ("diff", ""),
        ("diff", "?path=a.txt&path=a.txt"),
        ("search", ""),
        ("search", "?path=a"),
        ("search", "?q=a&x=1"),
        ("changes", "?path=a.txt"),
        ("changes", "?x"),
    ],
)
def test_an_unknown_repeated_or_missing_key_is_400_invalid_query(
    tmp_path: Path, route: str, query: str
) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url(route) + query, headers=AUTH), 400, "invalid_query", env)


def test_an_over_size_query_is_400_before_decoding(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for route, key in (("tree", "path"), ("file", "path"), ("search", "q")):
            query = f"?{key}=" + "a" * MAX_QUERY_BYTES
            _error(client.get(env.url(route) + query, headers=AUTH), 400, "invalid_query", env)


# --- one test per status code of the table ---------------------------------------------------


def test_400_invalid_path(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for route in ("tree?path=a//b", "file?path=..%2Fx", "file?path=a.txt.", "diff?path="):
            _error(client.get(env.url(route), headers=AUTH), 400, "invalid_path", env)


def test_400_not_a_file(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("file?path=dir"), headers=AUTH), 400, "not_a_file", env)


def test_400_not_a_directory(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("tree?path=a.txt"), headers=AUTH), 400, "not_a_directory", env)


def test_401_unauthorized(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("tree")), 401, "unauthorized", env)


def test_403_forbidden_origin(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        response = client.get(env.url("tree"), headers={**AUTH, "Origin": "https://evil.invalid"})
    _error(response, 403, "forbidden_origin", env)


def test_403_private_path(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("file?path=.npmrc"), headers=AUTH), 403, "private_path", env)
        _error(client.get(env.url("diff?path=.npmrc"), headers=AUTH), 403, "private_path", env)


def test_404_unknown_workspace(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for route in ROUTES:
            response = client.get(env.url(route, str(uuid.uuid4())), headers=AUTH)
            _error(response, 404, "unknown_workspace", env)


def test_404_not_found(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("file?path=missing.txt"), headers=AUTH), 404, "not_found", env)
        _error(client.get(env.url("tree?path=missing"), headers=AUTH), 404, "not_found", env)


def test_409_trust_required(tmp_path: Path) -> None:
    env = _plain(tmp_path, trust=False)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("changes"), headers=AUTH), 409, "trust_required", env)
        # Reading files needs no trust.
        assert client.get(env.url("tree"), headers=AUTH).status_code == 200
        assert client.get(env.url("file?path=a.txt"), headers=AUTH).status_code == 200


def test_409_root_unavailable(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    env.root.rename(env.root.with_name("moved"))
    with TestClient(env.app()) as client:
        _error(client.get(env.url("tree"), headers=AUTH), 409, "root_unavailable", env)


@needs_git
def test_409_not_repository(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("changes"), headers=AUTH), 409, "not_repository", env)


@needs_git
def test_409_not_repository_root(tmp_path: Path) -> None:
    outer = _repo(tmp_path / "outer")
    env = _env(tmp_path, outer / ROOT_NAME)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("changes"), headers=AUTH), 409, "not_repository_root", env)


@needs_git
def test_413_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _committed(tmp_path)
    monkeypatch.setattr(code_search, "MAX_INDEX_BYTES", 1)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("search?q=a"), headers=AUTH), 413, "too_large", env)


async def test_429_overloaded_when_four_requests_are_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _plain(tmp_path)
    entered = 0
    lock = threading.Lock()
    four_started = threading.Event()
    release = threading.Event()

    real_tree = env.files.tree

    def blocking(workspace_id: str, ref: str = "") -> object:
        nonlocal entered
        with lock:
            entered += 1
            if entered == 4:
                four_started.set()
        release.wait(5)
        return real_tree(workspace_id, ref)

    monkeypatch.setattr(env.files, "tree", blocking)
    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=env.app(workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [asyncio.create_task(client.get(env.url("tree"), headers=AUTH)) for _ in range(4)]
        assert await asyncio.to_thread(four_started.wait, 2)
        assert len(workers) == 4
        overloaded = await client.get(env.url("tree"), headers=AUTH)
        _error(overloaded, 429, "overloaded", env)
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        # The permits came back.
        assert (await client.get(env.url("tree"), headers=AUTH)).status_code == 200


def test_503_git_unavailable(tmp_path: Path) -> None:
    env = _plain(tmp_path, git_available=False)
    with TestClient(env.app()) as client:
        _error(client.get(env.url("changes"), headers=AUTH), 503, "git_unavailable", env)


@pytest.mark.parametrize("code", ["unavailable", "a_code_nobody_mapped"])
def test_503_unavailable_and_any_unmapped_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    env = _plain(tmp_path)

    def refuse(*_args: object) -> object:
        raise CodeError(code)

    monkeypatch.setattr(env.files, "tree", refuse)
    with TestClient(env.app()) as client:
        response = client.get(env.url("tree"), headers=AUTH)
    _error(response, 503, code, env)


# --- the five success bodies -------------------------------------------------------------------


def test_tree_lists_a_directory_and_an_empty_or_absent_path_is_the_root(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    with TestClient(env.app()) as client:
        for query in ("", "?path="):
            response = client.get(env.url("tree") + query, headers=AUTH)
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {"path", "entries", "truncated"}
            assert body["path"] == "" and body["truncated"] is False
            assert body["entries"] == [
                {"name": "dir", "type": "dir", "size": None},
                {"name": "a.txt", "type": "file", "size": 6},
            ]
        nested = client.get(env.url("tree"), params={"path": "dir"}, headers=AUTH)
        assert nested.json() == {"path": "dir", "entries": [], "truncated": False}


def test_file_answers_the_preview_with_every_field(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    (env.root / "dir" / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (env.root / "dir" / "blob.bin").write_bytes(b"\x00\x01")
    with TestClient(env.app()) as client:
        body = client.get(env.url("file?path=a.txt"), headers=AUTH).json()
        assert body == {
            "path": "a.txt",
            "size": 6,
            "sha256": hashlib.sha256(b"hello\n").hexdigest(),
            "encoding": "utf-8",
            "newline": "lf",
            "text": "hello\n",
            "reason": None,
        }
        crlf = client.get(env.url("file?path=dir/crlf.txt"), headers=AUTH).json()
        assert (crlf["newline"], crlf["text"]) == ("crlf", "one\r\ntwo\r\n")
        binary = client.get(env.url("file"), params={"path": "dir/blob.bin"}, headers=AUTH).json()
        assert binary["reason"] == "binary"
        assert binary["text"] is None and binary["encoding"] is None and binary["newline"] is None


def test_a_percent_encoded_japanese_ref_reaches_the_service(tmp_path: Path) -> None:
    env = _plain(tmp_path)
    (env.root / "日本語").mkdir()
    (env.root / "日本語" / "メモ.txt").write_bytes("牛乳\n".encode())
    encoded = "%E6%97%A5%E6%9C%AC%E8%AA%9E/%E3%83%A1%E3%83%A2.txt"
    with TestClient(env.app()) as client:
        by_hand = client.get(env.url(f"file?path={encoded}"), headers=AUTH)
        by_params = client.get(env.url("file"), params={"path": "日本語/メモ.txt"}, headers=AUTH)
        listing = client.get(env.url("tree"), params={"path": "日本語"}, headers=AUTH)
    for response in (by_hand, by_params):
        assert response.status_code == 200
        assert response.json()["path"] == "日本語/メモ.txt"
        assert response.json()["text"] == "牛乳\n"
        assert response.content.isascii()
    assert listing.json()["entries"] == [{"name": "メモ.txt", "type": "file", "size": 7}]


@needs_git
def test_changes_search_and_diff_answer_their_bodies(tmp_path: Path) -> None:
    env = _committed(tmp_path)
    (env.root / "a.txt").write_bytes(b"changed\n")
    (env.root / "new.txt").write_bytes(b"new\n")
    with TestClient(env.app()) as client:
        changes = client.get(env.url("changes"), headers=AUTH).json()
        head = _git(env.root, "rev-parse", "HEAD").strip()[:12]
        assert set(changes) == {
            "state",
            "branch",
            "head",
            "detached",
            "upstream",
            "ahead",
            "behind",
            "entries",
            "truncated",
        }
        assert (changes["state"], changes["branch"], changes["head"]) == ("ready", "main", head)
        assert (changes["detached"], changes["upstream"], changes["truncated"]) == (
            False,
            None,
            False,
        )
        assert (changes["ahead"], changes["behind"]) == (None, None)
        assert changes["entries"] == [
            {
                "path": "a.txt",
                "orig_path": None,
                "index": ".",
                "worktree": "M",
                "kind": "tracked",
                "private": False,
            },
            {
                "path": "new.txt",
                "orig_path": None,
                "index": "?",
                "worktree": "?",
                "kind": "untracked",
                "private": False,
            },
        ]

        search = client.get(env.url("search"), params={"q": "MAIN"}, headers=AUTH)
        assert search.json() == {"hits": ["src/main.py"], "truncated": False}
        japanese = client.get(env.url("search"), params={"q": "メモ"}, headers=AUTH)
        assert japanese.json()["hits"] == ["日本語/メモ.txt"]
        assert japanese.content.isascii()

        diff = client.get(env.url("diff?path=a.txt"), headers=AUTH).json()
        assert set(diff) == {"path", "text", "reason"}
        assert diff["path"] == "a.txt" and diff["reason"] is None
        assert "-hello" in diff["text"] and "+changed" in diff["text"]
        clean = client.get(env.url("diff"), params={"path": "src/main.py"}, headers=AUTH).json()
        assert clean["reason"] == "no_diff"
