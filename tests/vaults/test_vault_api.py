from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.domain import CanonicalRoot
from viki_agent.vaults.vault import Excerpt, ScanResult, VaultService
from viki_agent.vaults.vault_api import vault_routes
from viki_agent.vaults.vault_git import Commit, NoteHistory
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"


def _service(root: Path, active_id: str = "w") -> VaultService:
    info = root.stat()
    canonical = CanonicalRoot(
        key=root_key(root.resolve()),
        display=str(root.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    workspace = SimpleNamespace(root=canonical)
    owner = SimpleNamespace(
        get=lambda _id: workspace,
        current_root=lambda _workspace: workspace.root,
        resolve_active=lambda: SimpleNamespace(workspace=SimpleNamespace(workspace_id=active_id)),
    )
    return VaultService(cast(WorkspaceService, owner))


def _app(service: object, workers: set[asyncio.Task[object]] | None = None) -> Starlette:
    tracked = set() if workers is None else workers
    return Starlette(
        routes=vault_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=tracked,  # type: ignore[arg-type]
        )
    )


def test_routes_require_auth_origin_and_strict_query(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("牛乳", encoding="utf-8")
    workers: set[asyncio.Task[object]] = set()
    with TestClient(_app(_service(tmp_path), workers)) as client:
        path = "/api/v2/vaults/w/entries"
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={**AUTH, "Origin": "https://evil.invalid"}).status_code == 403
        )
        for query in ("x=milk", "q=a&q=b", "q=%ZZ"):
            response = client.get(f"{path}?{query}", headers=AUTH)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_query"
        response = client.get(path, headers=AUTH, params={"q": "牛乳"})
        assert response.status_code == 200
        assert response.json()["hits"][0]["exact"] == "牛乳"
        assert response.content.isascii()
        assert client.get("/api/v2/vaults/w/notes/note.md?q=x", headers=AUTH).status_code == 400
    assert not workers


def test_note_and_backlink_wire_include_resolution_and_partial_metadata(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("Target", encoding="utf-8")
    (tmp_path / "source.md").write_text("[[target]]", encoding="utf-8")
    with TestClient(_app(_service(tmp_path))) as client:
        note = client.get("/api/v2/vaults/w/notes/source.md", headers=AUTH)
        assert note.status_code == 200
        body = note.json()
        assert body["note"]["source"] == "[[target]]"
        assert len(body["note"]["sha256"]) == 64
        assert body["resolved_links"][0] == {
            "source": "source.md",
            "target": "target",
            "state": "resolved",
            "candidates": ["target.md"],
        }
        assert body["partial_reasons"] == []

        backlinks = client.get("/api/v2/vaults/w/backlinks/target.md", headers=AUTH)
        assert backlinks.status_code == 200
        assert backlinks.json()["backlinks"] == [
            {"source": "source.md", "target": "target", "line": 1}
        ]


HISTORY = "/api/v2/vaults/w/history/notes/a.md"


class HistoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def history(self, workspace_id: str, ref: str) -> NoteHistory:
        self.calls.append((workspace_id, ref))
        return NoteHistory((Commit("abc1234", "2026-10-01T09:00:00+09:00", "Add note"),), "ready")


def test_history_answers_commits_and_state_behind_auth_and_origin() -> None:
    service = HistoryService()
    with TestClient(_app(service)) as client:
        assert client.get(HISTORY).status_code == 401
        foreign = client.get(HISTORY, headers={**AUTH, "Origin": "https://evil.invalid"})
        assert foreign.status_code == 403
        assert not service.calls
        response = client.get(HISTORY, headers={**AUTH, "Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json() == {
        "commits": [{"short": "abc1234", "at": "2026-10-01T09:00:00+09:00", "subject": "Add note"}],
        "state": "ready",
    }
    assert service.calls == [("w", "notes/a.md")]


def test_history_of_an_unknown_workspace_is_404_and_a_git_failure_is_not_an_error(
    tmp_path: Path,
) -> None:
    def missing(_id: str) -> object:
        raise WorkspaceError("unknown_workspace")

    owner = SimpleNamespace(get=missing, current_root=lambda _workspace: None)
    with TestClient(_app(VaultService(cast(WorkspaceService, owner)))) as client:
        response = client.get(HISTORY, headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_workspace"
    # tmp_path is no Git work tree, so the read fails: that is `unknown`, still a 200.
    with TestClient(_app(_service(tmp_path))) as client:
        unknown = client.get(HISTORY, headers=AUTH)
    assert unknown.status_code == 200
    assert unknown.json() == {"commits": [], "state": "unknown"}


def test_history_refuses_a_query_string() -> None:
    service = HistoryService()
    with TestClient(_app(service)) as client:
        for query in ("q=x", "x=1", "q=%ZZ"):
            response = client.get(f"{HISTORY}?{query}", headers=AUTH)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_query"
    assert not service.calls


async def test_a_slow_history_read_answers_429_only_for_history_and_leaves_entries_alone() -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowHistoryService:
        def history(self, _workspace_id: str, _ref: str) -> NoteHistory:
            started.set()
            release.wait(5)
            return NoteHistory((), "ready")

        def scan(self, _workspace_id: str) -> ScanResult:
            return ScanResult((), False, (), 0, 0, 0)

    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(SlowHistoryService(), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = asyncio.create_task(client.get(HISTORY, headers=AUTH))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # As many history reads as the shared pool holds: with one shared limiter they would
            # all be admitted and fill it; with history's own, three are refused before it.
            more = await asyncio.wait_for(
                asyncio.gather(*(client.get(HISTORY, headers=AUTH) for _ in range(3))), 2
            )
            assert [response.status_code for response in more] == [429, 429, 429]
            assert {response.json()["error"]["code"] for response in more} == {"overloaded"}
            entries = await client.get("/api/v2/vaults/w/entries", headers=AUTH)
            assert entries.status_code == 200
        finally:
            release.set()
        assert (await slow).status_code == 200
        await asyncio.sleep(0)
        assert not workers
        # Both permits came back: a new history read is admitted.
        assert (await client.get(HISTORY, headers=AUTH)).status_code == 200


async def test_four_request_admission_is_shielded_and_tracked() -> None:
    entered = 0
    lock = threading.Lock()
    four_started = threading.Event()
    release = threading.Event()

    class BlockingService:
        def scan(self, _workspace_id: str) -> ScanResult:
            nonlocal entered
            with lock:
                entered += 1
                if entered == 4:
                    four_started.set()
            release.wait(5)
            return ScanResult((), False, (), 0, 0, 0)

    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(BlockingService(), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.get("/api/v2/vaults/w/entries", headers=AUTH))
            for _ in range(4)
        ]
        assert await asyncio.to_thread(four_started.wait, 2)
        assert len(workers) == 4
        overloaded = await client.get("/api/v2/vaults/w/entries", headers=AUTH)
        assert overloaded.status_code == 429
        assert overloaded.json()["error"]["code"] == "overloaded"
        release.set()
        responses = await asyncio.gather(*calls)
        assert all(response.status_code == 200 for response in responses)
        await asyncio.sleep(0)
        assert not workers


EXCERPT = "/api/v2/vaults/w/excerpt"
EXCERPT_KEYS = {"part", "text", "block", "block_bytes", "revision", "modified_at"}


class ExcerptService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def excerpt(self, workspace_id: str, ref: str, start: int, end: int) -> Excerpt:
        self.calls.append((workspace_id, ref, start, end))
        return Excerpt({"kind": "vault_excerpt"}, "t", "b", 1, "r", "2026-10-01T00:00:00.000Z")


def test_excerpt_answers_exactly_the_six_fields_for_a_line_span(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "牛乳.md").write_bytes("一\r\n二\r\n三\r\n".encode())
    with TestClient(_app(_service(tmp_path))) as client:
        response = client.get(
            EXCERPT, headers=AUTH, params={"ref": "notes/牛乳.md", "start": "1", "end": "3"}
        )
    assert response.status_code == 200
    assert response.content.isascii()
    body = response.json()
    assert set(body) == EXCERPT_KEYS
    assert body["text"] == "一\n二\n三"
    assert body["block"].endswith("\n```\n一\n二\n三\n```")
    assert body["block_bytes"] == len(body["block"].encode("utf-8"))
    assert set(body["part"]) == {"kind", "vault_id", "ref", "start_line", "end_line", "sha256"}
    assert body["part"]["ref"] == "notes/牛乳.md"
    assert len(body["revision"]) == 64
    assert body["modified_at"].endswith("Z")


def test_excerpt_requires_auth_and_origin_and_reads_nothing_without_them() -> None:
    service = ExcerptService()
    query = "?ref=n.md&start=1&end=2"
    with TestClient(_app(service)) as client:
        assert client.get(EXCERPT + query).status_code == 401
        foreign = client.get(EXCERPT + query, headers={**AUTH, "Origin": "https://evil.invalid"})
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "forbidden_origin"
        allowed = client.get(EXCERPT + query, headers={**AUTH, "Origin": ORIGIN})
        assert allowed.status_code == 200
    assert service.calls == [("w", "n.md", 1, 2)]


def test_excerpt_of_a_missing_note_a_private_ref_and_an_unknown_workspace_is_404(
    tmp_path: Path,
) -> None:
    def missing(_id: str) -> object:
        raise WorkspaceError("unknown_workspace")

    gone = SimpleNamespace(
        get=missing,
        current_root=lambda _workspace: None,
        resolve_active=lambda: SimpleNamespace(workspace=SimpleNamespace(workspace_id="w")),
    )
    with TestClient(_app(_service(tmp_path))) as client:
        for ref, code in (("absent.md", "missing_note"), (".git/x.md", "private_path")):
            response = client.get(
                EXCERPT, headers=AUTH, params={"ref": ref, "start": "1", "end": "1"}
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == code
    with TestClient(_app(VaultService(cast(WorkspaceService, gone)))) as client:
        response = client.get(EXCERPT, headers=AUTH, params={"ref": "a.md", "start": 1, "end": 1})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_workspace"


def test_excerpt_range_size_and_vault_refusals_map_to_400_413_and_409(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("a\nb\n" + "x" * 40_000, encoding="utf-8", newline="\n")
    with TestClient(_app(_service(tmp_path))) as client:
        past = client.get(EXCERPT, headers=AUTH, params={"ref": "n.md", "start": 1, "end": 9})
        large = client.get(EXCERPT, headers=AUTH, params={"ref": "n.md", "start": 3, "end": 3})
    with TestClient(_app(_service(tmp_path, active_id="other"))) as client:
        inactive = client.get(EXCERPT, headers=AUTH, params={"ref": "n.md", "start": 1, "end": 1})
    assert [past.status_code, large.status_code, inactive.status_code] == [400, 413, 409]
    codes = [past, large, inactive]
    assert [r.json()["error"]["code"] for r in codes] == [
        "invalid_range",
        "excerpt_too_large",
        "not_active_vault",
    ]
    for response in codes:
        assert str(tmp_path) not in response.text
        assert "n.md" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "ref=n.md&start=0x1&end=2",
        "ref=n.md&start=-1&end=2",
        "ref=n.md&start=%2B1&end=2",
        "ref=n.md&start=+1&end=2",
        "ref=n.md&start=1.0&end=2",
        "ref=n.md&start=1e1&end=2",
        "ref=n.md&start=%31&end=2",
        "ref=n.md&start=12345678&end=12345678",
        "ref=n.md&start=&end=2",
        "ref=n.md&start=1&end=",
        "ref=n.md&start=1",
        "ref=n.md&end=2",
        "ref=n.md&start=%EF%BC%91&end=2",  # a full-width digit
    ],
)
def test_excerpt_start_and_end_must_be_one_to_seven_ascii_digits(query: str) -> None:
    service = ExcerptService()
    with TestClient(_app(service)) as client:
        response = client.get(f"{EXCERPT}?{query}", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_range"
    assert not service.calls


@pytest.mark.parametrize(
    "query",
    [
        "",
        "start=1&end=2",
        "ref=n.md&ref=m.md&start=1&end=2",
        "ref=n.md&start=1&end=2&extra=1",
        "q=n.md&start=1&end=2",
        "ref=n.md&start=1&end=2&",
        "ref=%ZZ&start=1&end=2",
        "ref=" + "a" * 1_100 + "&start=1&end=2",
    ],
)
def test_excerpt_refuses_a_query_that_is_not_exactly_ref_start_and_end(query: str) -> None:
    service = ExcerptService()
    with TestClient(_app(service)) as client:
        response = client.get(f"{EXCERPT}?{query}", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"
    assert not service.calls


def test_excerpt_accepts_its_fields_in_any_order_and_the_widest_line_number() -> None:
    service = ExcerptService()
    with TestClient(_app(service)) as client:
        response = client.get(f"{EXCERPT}?end=1000000&start=0000001&ref=a%20b.md", headers=AUTH)
    assert response.status_code == 200
    assert service.calls == [("w", "a b.md", 1, 1_000_000)]


async def test_a_third_excerpt_read_is_429_and_leaves_entries_alone() -> None:
    started = threading.Semaphore(0)
    release = threading.Event()

    class SlowService:
        def excerpt(self, _workspace_id: str, _ref: str, _start: int, _end: int) -> Excerpt:
            started.release()
            release.wait(5)
            return Excerpt({}, "", "", 0, "", "")

        def scan(self, _workspace_id: str) -> ScanResult:
            return ScanResult((), False, (), 0, 0, 0)

    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(SlowService(), workers))
    url = f"{EXCERPT}?ref=n.md&start=1&end=1"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = [asyncio.create_task(client.get(url, headers=AUTH)) for _ in range(2)]
        try:
            for _ in range(2):
                assert await asyncio.to_thread(started.acquire, True, 2)
            third = await asyncio.wait_for(client.get(url, headers=AUTH), 2)
            assert third.status_code == 429
            assert third.json()["error"]["code"] == "overloaded"
            assert (await client.get("/api/v2/vaults/w/entries", headers=AUTH)).status_code == 200
        finally:
            release.set()
        assert [(await call).status_code for call in slow] == [200, 200]
        await asyncio.sleep(0)
        assert not workers
        # Both permits came back.
        assert [(await client.get(url, headers=AUTH)).status_code for _ in range(2)] == [200, 200]


async def test_an_excerpt_refused_by_the_full_shared_pool_returns_its_own_permit() -> None:
    entered = threading.Semaphore(0)
    release = threading.Event()

    class FullPoolService:
        def scan(self, _workspace_id: str) -> ScanResult:
            entered.release()
            release.wait(5)
            return ScanResult((), False, (), 0, 0, 0)

        def excerpt(self, _workspace_id: str, _ref: str, _start: int, _end: int) -> Excerpt:
            return Excerpt({}, "", "", 0, "", "")

    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(FullPoolService(), workers))
    url = f"{EXCERPT}?ref=n.md&start=1&end=1"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.get("/api/v2/vaults/w/entries", headers=AUTH))
            for _ in range(4)
        ]
        try:
            for _ in range(4):
                assert await asyncio.to_thread(entered.acquire, True, 2)
            # Its own limiter admits it, then the full shared pool refuses it: three of these
            # would be refused by a leaked permit, because its own limiter holds only two.
            refused = [await client.get(url, headers=AUTH) for _ in range(3)]
            assert [response.status_code for response in refused] == [429, 429, 429]
        finally:
            release.set()
        await asyncio.gather(*calls)
        await asyncio.sleep(0)
        assert not workers
        assert [(await client.get(url, headers=AUTH)).status_code for _ in range(2)] == [200, 200]
