"""Window handoff routes use synthetic sessions and browser-process doubles."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.conversations.test_session_windows_launch import SESSION_ID, Spawner, manager_for
from tests.profiles.test_profile_launch_api import BASE, TOKEN, Rig, ident
from viki_agent import server
from viki_agent.app.diagnostics import ProviderChecks
from viki_agent.conversations import session_windows_api
from viki_agent.conversations.drafts import draft_scope
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError
from viki_agent.window import WindowManager

URL = "/api/v2/session-windows"
HEADERS = {
    "authorization": f"Bearer {TOKEN}",
    "origin": BASE,
    "content-type": "application/json",
}


def built(rig: Rig, windows: WindowManager | None) -> Starlette:
    return server.create_app(
        config=rig.config,
        token=TOKEN,
        manager=rig.manager(),
        catalog=rig.catalog,
        static_dir=None,
        settings=rig.settings,
        workspaces=rig.workspaces,
        readiness=rig.readiness,
        windows=windows,
        vault_poll_s=60,
    )


def code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["error"]["code"]


@pytest.mark.parametrize("known", [False, True])
def test_only_known_history_opens_native_window_without_provider_start(
    tmp_path: Path, known: bool
) -> None:
    rig = Rig(tmp_path)
    spawn = Spawner()
    windows = manager_for(spawn, [])
    if known:
        rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, windows), base_url=BASE) as client:
        response = client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS)
        if known:
            assert response.status_code == 200 and response.json() == {"opened": True}
            assert response.headers["cache-control"] == "no-store"
            assert spawn.calls == [["browser", "ordinary"], ["browser", SESSION_ID]]
        else:
            assert code(response, 404) == "unavailable_target"
            assert spawn.calls == [["browser", "ordinary"]]
        assert windows.count == (2 if known else 1)
    assert rig.drivers == []


@pytest.mark.parametrize(
    ("change", "status", "reason"),
    [
        ({"headers": {"origin": BASE, "content-type": "application/json"}}, 401, "unauthorized"),
        ({"headers": {**HEADERS, "authorization": "Bearer wrong"}}, 401, "unauthorized"),
        (
            {"headers": {key: value for key, value in HEADERS.items() if key != "origin"}},
            403,
            "forbidden_origin",
        ),
        ({"headers": {**HEADERS, "origin": "https://example.com"}}, 403, "forbidden_origin"),
        ({"url": URL + "?session_id=x"}, 400, "invalid_input"),
        ({"headers": {**HEADERS, "content-type": "text/plain"}}, 415, "invalid_representation"),
        ({"headers": {**HEADERS, "content-encoding": "gzip"}}, 415, "invalid_representation"),
        ({"body": "{}"}, 400, "invalid_input"),
        ({"body": "[]"}, 400, "invalid_input"),
        ({"body": '{"session_id":"' + SESSION_ID + '","extra":true}'}, 400, "invalid_input"),
        (
            {"body": '{"session_id":"' + SESSION_ID + '","session_id":"' + SESSION_ID + '"}'},
            400,
            "invalid_input",
        ),
        ({"body": '{"session_id":null}'}, 400, "invalid_input"),
        ({"body": '{"session_id":"invalid"}'}, 400, "invalid_input"),
        ({"body": json.dumps({"session_id": SESSION_ID.upper()})}, 400, "invalid_input"),
        ({"body": b"\xff"}, 400, "invalid_input"),
        ({"body": " " * 1025}, 413, "too_large"),
    ],
)
def test_invalid_requests_launch_nothing(
    tmp_path: Path, change: dict[str, Any], status: int, reason: str
) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        response = client.post(
            change.get("url", URL),
            content=change.get("body", json.dumps({"session_id": SESSION_ID})),
            headers=change.get("headers", HEADERS),
        )
        assert code(response, status) == reason
        assert spawn.calls == [["browser", "ordinary"]]
    assert rig.drivers == []


def test_label_without_source_does_not_open_or_create(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    rig.state.store.update_session_metadata(
        draft_scope(rig.vault),
        SESSION_ID,
        lambda _: {
            "session_id": SESSION_ID,
            "revision": 1,
            "title": "Synthetic missing source",
            "pinned": 1,
            "archived": 0,
            "unread": 1,
            "color": "orange",
        },
    )
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 404) == (
            "unavailable_target"
        )
        assert spawn.calls == [["browser", "ordinary"]]
    assert rig.drivers == []


def test_held_prepared_profile_target_opens_same_id(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        session_id = ident()
        rig.preview(client, session_id, ident())
        response = client.post(URL, json={"session_id": session_id}, headers=HEADERS)
        assert response.status_code == 200 and response.json() == {"opened": True}
        assert spawn.calls[-1] == ["browser", session_id]
    assert rig.drivers == []


def test_unheld_prepared_profile_target_opens_without_attaching_actor(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    manager = rig.manager()
    app = server.create_app(
        config=rig.config,
        token=TOKEN,
        manager=manager,
        catalog=rig.catalog,
        static_dir=None,
        settings=rig.settings,
        workspaces=rig.workspaces,
        readiness=rig.readiness,
        windows=manager_for(spawn, []),
        vault_poll_s=60,
    )
    with TestClient(app, base_url=BASE) as client:
        session_id = ident()
        rig.preview(client, session_id, ident())
        manager._actors.pop(session_id)
        response = client.post(URL, json={"session_id": session_id}, headers=HEADERS)
        assert response.status_code == 200 and response.json() == {"opened": True}
        assert manager.get(session_id) is None
        assert spawn.calls[-1] == ["browser", session_id]
    assert rig.drivers == []


def test_history_source_disappearing_during_proof_does_not_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        read = rig.state.store.session_origin

        def disappear(session_id: str) -> str | None:
            origin = read(session_id)
            rig.catalog.transcripts.discard(session_id)
            return origin

        monkeypatch.setattr(rig.state.store, "session_origin", disappear)
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 404) == (
            "unavailable_target"
        )
        assert spawn.calls == [["browser", "ordinary"]]
    assert rig.drivers == []


@pytest.mark.parametrize("failure", [StateError("busy"), ProtectionError()])
def test_storage_or_privacy_failure_refuses_without_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        monkeypatch.setattr(rig.state.store, "get_settings", Mock(side_effect=failure))
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 503) == (
            "storage_unavailable"
        )
        assert spawn.calls == [["browser", "ordinary"]]
    assert rig.drivers == []


def test_history_handoff_does_not_require_provider_readiness(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    rig.readiness.observe_version("")
    rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, manager_for(spawn, [])), base_url=BASE) as client:
        response = client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS)
        assert response.status_code == 200 and response.json() == {"opened": True}
    assert rig.drivers == []


def test_unavailable_window_host_refuses(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, None), base_url=BASE) as client:
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 503) == (
            "unavailable"
        )
    assert rig.drivers == []


def test_recovery_shell_has_no_window_handoff_route(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    app = server.create_recovery_app(
        config=rig.config,
        token=TOKEN,
        readiness=rig.readiness,
        checks=ProviderChecks(
            readiness=rig.readiness, cli_version=lambda: "", git_version=lambda: ""
        ),
        diagnostics_document=lambda: {},
        static_dir=None,
    )
    with TestClient(app, base_url=BASE) as client:
        response = client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS)
        assert response.status_code == 404
    assert rig.drivers == []


def test_capacity_and_spawn_failure_are_explicit(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    windows = manager_for(spawn, [])
    rig.catalog.transcripts.add(SESSION_ID)
    with TestClient(built(rig, windows), base_url=BASE) as client:
        spawn.fail = True
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 503) == (
            "start_failed"
        )
        spawn.fail = False
        for _ in range(7):
            assert (
                client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS).status_code
                == 200
            )
        assert windows.admitted_count == 8
        assert code(client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 429) == (
            "window_limit"
        )
    assert rig.drivers == []


def isolated(windows: WindowManager, workers: set[asyncio.Task[Any]]) -> Starlette:
    from viki_agent.conversations.session_windows_api import session_window_routes

    async def available(session_id: str) -> bool:
        return True

    return Starlette(
        routes=session_window_routes(
            windows,
            available=available,
            authorized=lambda request: (
                request.headers.get("authorization") == HEADERS["authorization"]
            ),
            origins=frozenset({BASE}),
            workers=workers,
        )
    )


async def test_admitted_launch_keeps_slot_when_client_leaves_until_window_stop() -> None:
    spawn, workers = Spawner(), set()
    windows = manager_for(spawn, [])
    await windows.start()
    spawn.defer = True
    spawn.entered.clear()
    app = isolated(windows, workers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        request = asyncio.create_task(
            client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS)
        )
        await spawn.entered.wait()
        worker = next(iter(workers))
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not worker.cancelled() and windows.admitted_count == 2
        await windows.stop()
        assert worker.cancelled() and windows.admitted_count == 0
        await asyncio.sleep(0)
        assert workers == set()


async def test_route_overload_counts_pending_intake_or_spawns() -> None:
    spawn, workers = Spawner(), set()
    windows = manager_for(spawn, [])
    await windows.start()
    spawn.defer = True
    spawn.entered.clear()
    app = isolated(windows, workers)
    pending: list[asyncio.Task[httpx.Response]] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        try:
            pending = [
                asyncio.create_task(
                    client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS)
                )
                for _ in range(2)
            ]
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(workers) == 2
            assert code(
                await client.post(URL, json={"session_id": SESSION_ID}, headers=HEADERS), 429
            ) == ("overloaded")
            spawn.resume.set()
            assert all(response.status_code == 200 for response in await asyncio.gather(*pending))
        finally:
            await windows.stop()
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("disconnect", [False, True])
async def test_bounded_intake_timeout_and_disconnect_are_explicit(
    monkeypatch: pytest.MonkeyPatch, disconnect: bool
) -> None:
    monkeypatch.setattr(session_windows_api, "INTAKE_TIMEOUT_S", 0.01)
    spawn, workers = Spawner(), set()
    windows = manager_for(spawn, [])
    app = isolated(windows, workers)
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:
        if disconnect:
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message: Any) -> None:
        sent.append(message)

    scope: Any = {
        "type": "http",
        "method": "POST",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "headers": [(key.encode(), value.encode()) for key, value in HEADERS.items()],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == (400 if disconnect else 408)
    assert json.loads(sent[1]["body"])["error"]["code"] == (
        "disconnected" if disconnect else "intake_timeout"
    )
    assert spawn.calls == [] and workers == set()


async def test_app_shutdown_cancels_admitted_intake_and_joins_worker(tmp_path: Path) -> None:
    rig, spawn = Rig(tmp_path), Spawner()
    windows = manager_for(spawn, [])
    app = built(rig, windows)
    entered = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        entered.set()
        await asyncio.Event().wait()
        yield b""

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        async with app.router.lifespan_context(app):
            request = asyncio.create_task(client.post(URL, content=body(), headers=HEADERS))
            await entered.wait()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert windows.admitted_count == 0
    assert all(window.killed and window.released for window in spawn.windows)
    assert rig.drivers == []
