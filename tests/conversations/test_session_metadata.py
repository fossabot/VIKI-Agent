from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
import httpx
import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Scope

from tests.conversations.test_draft_api import app_at
from viki_agent.conversations.session_metadata import SessionMetadataError, SessionMetadataService
from viki_agent.state import AppState, Prefs

SID = "11111111-1111-4111-8111-111111111111"
AUTH = {"Authorization": "Bearer draft-test-token"}
MUTATION = {**AUTH, "Origin": "http://127.0.0.1:8765"}
PATH = "/api/v2/session-metadata"


@pytest.mark.parametrize("color", ["blue", "green", "orange", "purple", "red", "slate", None])
def test_manual_labels_persist_with_cas_and_remain_independent(
    tmp_path: Path, color: str | None
) -> None:
    path = tmp_path / "data/state.json"
    state = AppState(path, Prefs())
    service = SessionMetadataService(state.store, "a" * 64)
    before = service.patch(SID, 0, {"title": "Keep", "pinned": True, "archived": True})
    changed = service.patch(SID, before["revision"], {"unread": True, "color": color})
    assert changed == {**before, "revision": 2, "unread": True, "color": color}
    assert service.patch(SID, 1, {"unread": True, "color": color}) == changed
    with pytest.raises(SessionMetadataError, match="conflict"):
        service.patch(SID, 1, {"unread": False})
    restarted = SessionMetadataService(AppState(path, Prefs()).store, "a" * 64)
    assert restarted.list() == [changed]
    assert service.patch(SID, 2, {"unread": False, "color": None}) == {
        **changed,
        "revision": 3,
        "unread": False,
        "color": None,
    }
    assert not state.ours(SID)


def test_labels_survive_restart_are_scope_bound_and_do_not_create_native_session(
    tmp_path: Path,
) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())
    service = SessionMetadataService(state.store, "a" * 64)
    changed = service.patch(SID, 0, {"title": "Café plans", "pinned": True})
    assert changed == {
        "session_id": SID,
        "revision": 1,
        "title": "Café plans",
        "pinned": True,
        "archived": False,
        "unread": False,
        "color": None,
    }
    assert service.patch(SID, 0, {"title": "Café plans", "pinned": True}) == changed
    assert service.patch(SID, 1, {"pinned": True}) == changed
    restarted = SessionMetadataService(
        AppState(tmp_path / "data/state.json", Prefs()).store, "a" * 64
    )
    assert restarted.list() == [changed]
    assert SessionMetadataService(state.store, "b" * 64).list() == []
    assert not state.ours(SID)
    with pytest.raises(SessionMetadataError, match="conflict"):
        service.patch(SID, 0, {"title": "old window"})
    assert service.patch(SID, 1, {"archived": True})["archived"] is True
    assert not state.ours(SID)


def test_delete_removes_a_matching_label_and_rejects_a_stale_revision(tmp_path: Path) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())
    service = SessionMetadataService(state.store, "a" * 64)
    assert service.delete(SID, 0) is False
    service.patch(SID, 0, {"title": "keep"})
    with pytest.raises(SessionMetadataError, match="conflict"):
        service.delete(SID, 0)
    assert service.delete(SID, 1) is True
    assert service.list() == []
    assert service.delete(SID, 1) is False


@pytest.mark.parametrize("expected_revision", [-1, "1", 1.0, (1 << 53) - 1])
def test_delete_rejects_an_invalid_revision(tmp_path: Path, expected_revision: object) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())
    service = SessionMetadataService(state.store, "a" * 64)
    with pytest.raises(SessionMetadataError, match="invalid_input"):
        service.delete(SID, expected_revision)
    with pytest.raises(SessionMetadataError, match="invalid_input"):
        service.delete("not-a-uuid", 0)


@pytest.mark.parametrize(
    "patch",
    [
        {"title": "x\nsecret"},
        {"pinned": 1},
        {"archive": True},
        {},
        {"title": "x" * 257},
        {"unread": 1},
        {"unread": "true"},
        {"unread": None},
        {"color": "amber"},
        {"color": 1},
        {"color": {}},
        {"color": []},
    ],
)
def test_invalid_metadata_is_rejected_before_write(
    tmp_path: Path, patch: dict[str, object]
) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())
    service = SessionMetadataService(state.store, "a" * 64)
    with pytest.raises(SessionMetadataError, match="invalid_input"):
        service.patch(SID, 0, patch)
    assert service.list() == []


def test_routes_auth_origin_conflict_and_no_provider_activity(tmp_path: Path) -> None:
    app, state, manager = app_at(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.get(PATH).status_code == 401
    body = {"expected_revision": 0, "patch": {"title": "Hello", "pinned": True}}
    assert client.patch(f"{PATH}/{SID}", headers=AUTH, json=body).status_code == 403
    changed = client.patch(f"{PATH}/{SID}", headers=MUTATION, json=body)
    assert changed.status_code == 200
    assert client.patch(f"{PATH}/{SID}", headers=MUTATION, json=body).json() == changed.json()
    assert client.get(PATH, headers=AUTH).json() == {"sessions": [changed.json()]}
    conflict = client.patch(
        f"{PATH}/{SID}",
        headers=MUTATION,
        json={"expected_revision": 0, "patch": {"title": "other"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_revision"] == 1
    assert "Hello" not in conflict.text and "other" not in conflict.text
    assert manager.get(SID) is None and not state.ours(SID)


def test_delete_route_status_codes_and_never_touches_a_provider(tmp_path: Path) -> None:
    app, state, manager = app_at(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    body = {"expected_revision": 0}
    assert client.request("DELETE", f"{PATH}/{SID}", json=body).status_code == 401
    assert client.request("DELETE", f"{PATH}/{SID}", headers=AUTH, json=body).status_code == 403
    assert client.request("DELETE", f"{PATH}/{SID}", headers=MUTATION, json=body).status_code == 404
    created = client.patch(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "patch": {"title": "x"}}
    )
    assert created.status_code == 200
    conflict = client.request(
        "DELETE", f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_revision"] == 1
    deleted = client.request(
        "DELETE", f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 1}
    )
    assert deleted.status_code == 204
    assert client.get(PATH, headers=AUTH).json() == {"sessions": []}
    assert manager.get(SID) is None and not state.ours(SID)


def test_patch_at_the_cap_is_a_typed_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from viki_agent import storage

    app, _, _ = app_at(tmp_path)
    monkeypatch.setattr(storage, "MAX_SESSION_METADATA", 0)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    response = client.patch(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "patch": {"title": "x"}}
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "capacity"


def test_wrong_content_type_and_encoding_are_typed_415(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    body = {"expected_revision": 0, "patch": {"title": "x"}}
    assert client.patch(f"{PATH}/{SID}", headers=MUTATION, content=b"not json").status_code == 415
    assert (
        client.patch(
            f"{PATH}/{SID}",
            headers={**MUTATION, "content-type": "application/json", "content-encoding": "gzip"},
            content=b"{}",
        ).status_code
        == 415
    )
    assert client.patch(f"{PATH}/{SID}", headers=MUTATION, json=body).status_code == 200


def test_oversized_body_is_typed_413(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    response = client.patch(
        f"{PATH}/{SID}",
        headers={**MUTATION, "content-type": "application/json"},
        content=b"x" * 8193,
    )
    assert response.status_code == 413


async def test_body_stall_returns_the_typed_408(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from viki_agent.conversations import session_metadata_api

    app, _, _ = app_at(tmp_path)
    original_fail_after = anyio.fail_after
    monkeypatch.setattr(
        session_metadata_api.anyio, "fail_after", lambda seconds: original_fail_after(0.02)
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PATCH",
        "scheme": "http",
        "path": f"{PATH}/{SID}",
        "raw_path": f"{PATH}/{SID}".encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [
            (k.encode(), v.encode())
            for k, v in {
                "host": "127.0.0.1:8765",
                **{k.lower(): v for k, v in MUTATION.items()},
                "content-type": "application/json",
            }.items()
        ],
    }

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    output: list[Message] = []

    async def send(message: Message) -> None:
        output.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    assert status == 408


async def test_requests_over_admission_are_refused_with_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    # A seconds-long guard on an event that normally fires in milliseconds: an admission
    # regression that lets an over-limit request through must fail fast, not hang the suite.
    HANG_GUARD_S = 5

    app, _, _ = app_at(tmp_path)
    entered: asyncio.Queue[None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    original = SessionMetadataService.list

    def blocked(self: SessionMetadataService):
        loop.call_soon_threadsafe(entered.put_nowait, None)
        release.wait()
        return original(self)

    monkeypatch.setattr(SessionMetadataService, "list", blocked)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        active = [asyncio.create_task(client.get(PATH, headers=AUTH)) for _ in range(4)]
        try:
            for _ in active:
                await asyncio.wait_for(entered.get(), 1.0)
            async with asyncio.timeout(HANG_GUARD_S):
                rejected = await client.get(PATH, headers=AUTH)
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "overloaded"
        finally:
            release.set()
            await asyncio.gather(*active, return_exceptions=True)
