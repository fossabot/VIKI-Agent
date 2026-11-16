from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.testclient import TestClient
from starlette.types import Message, Scope

from viki_agent.conversations import chat_archives as chat_archives_module
from viki_agent.conversations import chat_archives_api as chat_archives_api_module
from viki_agent.conversations.chat_archives import (
    ArchiveConflict,
    ArchiveError,
    ConversationArchive,
    ConversationArchiveService,
)
from viki_agent.conversations.chat_archives_api import conversation_archive_routes
from viki_agent.media.assets import AssetError, AssetService, DecodedImage
from viki_agent.state import AppState, Prefs
from viki_agent.storage import ConversationArchiveRow, StateError

SCOPE = "c" * 64
ARCHIVE_ID = "00000000-0000-4000-8000-000000000001"
SESSION_ID = "00000000-0000-4000-8000-000000000002"
ASSET_ID = "00000000-0000-4000-8000-000000000003"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def archive(*, media: dict[str, object] | None = None) -> dict[str, object]:
    attachment = {
        "asset_id": ASSET_ID,
        "media_type": "image/png",
        "byte_size": len(PNG),
        "width": 1,
        "height": 1,
        "name": "selected.png",
        "modified_at": None,
        "source": "picker",
    }
    return {
        "format_version": 1,
        "kind": "viki-conversation-archive",
        "read_only": True,
        "session": {"session_id": SESSION_ID, "title": "Saved conversation"},
        "turns": [
            {
                "turn_id": "h-synthetic-0",
                "role": "user",
                "text": "private archive text",
                "created_at": "2026-09-19T00:00:00.000Z",
                "attachments": [attachment],
            },
            {"turn_id": "h-synthetic-0", "role": "assistant", "text": "reply"},
        ],
        "media": [
            media
            or {
                "asset_id": ASSET_ID,
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "base64": base64.b64encode(PNG).decode("ascii"),
            }
        ],
    }


@pytest.fixture
def service(tmp_path: Path) -> tuple[ConversationArchiveService, AppState]:
    state = AppState(tmp_path / "data/state.json", Prefs())
    assets = AssetService(tmp_path / "data/assets", state.store, SCOPE)
    return ConversationArchiveService(state.store, SCOPE, assets.inspect_bytes), state


@pytest.mark.asyncio
async def test_archive_roundtrip_is_encrypted_idempotent_and_read_only(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, state = service
    first, created = await archives.create(ARCHIVE_ID, archive())
    assert created and first.archive["read_only"] is True
    assert await archives.create(ARCHIVE_ID, archive()) == (first, False)
    assert await archives.get(ARCHIVE_ID) == first
    assert (await archives.list())[0].archive_id == ARCHIVE_ID
    with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
        body = connection.execute("SELECT body FROM conversation_archives").fetchone()[0]
    assert b"private archive text" not in body
    changed = archive()
    changed["session"] = {"session_id": SESSION_ID, "title": "different"}
    with pytest.raises(ArchiveConflict):
        await archives.create(ARCHIVE_ID, changed)
    assert await archives.remove(ARCHIVE_ID) is True
    with pytest.raises(ArchiveError, match="not_found"):
        await archives.get(ARCHIVE_ID)


@pytest.mark.asyncio
async def test_archive_validates_embedded_media_without_publishing_asset(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, state = service
    bad = archive(
        media={
            "asset_id": ASSET_ID,
            "sha256": hashlib.sha256(b"<svg></svg>").hexdigest(),
            "base64": base64.b64encode(b"<svg></svg>").decode("ascii"),
        }
    )
    with pytest.raises(ArchiveError, match="invalid_media"):
        await archives.create(ARCHIVE_ID, bad)
    unavailable, created = await archives.create(
        ARCHIVE_ID, archive(media={"asset_id": ASSET_ID, "unavailable": True})
    )
    assert created and unavailable.archive["media"] == [{"asset_id": ASSET_ID, "unavailable": True}]
    assert state.store.asset_usage(SCOPE) == (0, 0)


def test_archive_routes_enforce_origin_and_roundtrip(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _state = service
    workers: set[asyncio.Task[object]] = set()
    routes = conversation_archive_routes(
        archives,
        authorized=lambda request: request.headers.get("authorization") == "Bearer token",
        origins=frozenset({"http://127.0.0.1:8765"}),
        workers=workers,
    )
    client = TestClient(Starlette(routes=routes), base_url="http://127.0.0.1:8765")
    assert client.get("/api/v2/conversation-archives").status_code == 401
    assert (
        client.post(
            "/api/v2/conversation-archives",
            headers={"authorization": "Bearer token"},
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        ).status_code
        == 403
    )
    response = client.post(
        "/api/v2/conversation-archives",
        headers={"authorization": "Bearer token", "origin": "http://127.0.0.1:8765"},
        json={"archive_id": ARCHIVE_ID, "archive": archive()},
    )
    assert response.status_code == 201
    fetched = client.get(
        f"/api/v2/conversation-archives/{ARCHIVE_ID}",
        headers={"authorization": "Bearer token"},
    )
    assert fetched.status_code == 200 and fetched.json()["archive"]["read_only"] is True
    removed = client.delete(
        f"/api/v2/conversation-archives/{ARCHIVE_ID}",
        headers={"authorization": "Bearer token", "origin": "http://127.0.0.1:8765"},
    )
    assert removed.status_code == 204


OTHER_ASSET_ID = "00000000-0000-4000-8000-000000000004"
AUTH = {"authorization": "Bearer token", "origin": "http://127.0.0.1:8765"}


def make_app(archives: ConversationArchiveService) -> Starlette:
    return Starlette(
        routes=conversation_archive_routes(
            archives,
            authorized=lambda request: request.headers.get("authorization") == "Bearer token",
            origins=frozenset({"http://127.0.0.1:8765"}),
            workers=set(),
        )
    )


@pytest.mark.asyncio
async def test_unreferenced_media_is_rejected(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _ = service
    bad = archive()
    media = bad["media"]
    assert isinstance(media, list)
    bad["media"] = [*media, {"asset_id": OTHER_ASSET_ID, "unavailable": True}]
    with pytest.raises(ArchiveError, match="invalid_archive"):
        await archives.create(ARCHIVE_ID, bad)


@pytest.mark.asyncio
async def test_duplicate_turn_identity_is_rejected(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _ = service
    bad = {
        "format_version": 1,
        "kind": "viki-conversation-archive",
        "read_only": True,
        "session": {"session_id": SESSION_ID, "title": "duplicate"},
        "turns": [
            {"turn_id": "t1", "role": "user", "text": "a"},
            {"turn_id": "t1", "role": "user", "text": "b"},
        ],
        "media": [],
    }
    with pytest.raises(ArchiveError, match="invalid_archive"):
        await archives.create(ARCHIVE_ID, bad)


@pytest.mark.asyncio
async def test_media_dimension_mismatch_is_rejected(tmp_path: Path) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())

    async def wrong_dims(data: bytes) -> DecodedImage:
        return DecodedImage(media_type="image/png", width=99, height=99)

    archives = ConversationArchiveService(state.store, SCOPE, wrong_dims)
    with pytest.raises(ArchiveError, match="invalid_media"):
        await archives.create(ARCHIVE_ID, archive())


async def test_capacity_route_returns_429(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, state = service

    def full(*args: object, **kwargs: object) -> object:
        raise StateError("capacity")

    monkeypatch.setattr(state.store, "insert_conversation_archive", full)
    app = make_app(archives)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
        assert response.status_code == 429


def test_conflict_route_returns_409(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _ = service
    app = make_app(archives)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        first = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
        assert first.status_code == 201
        changed = archive()
        changed["session"] = {"session_id": SESSION_ID, "title": "different"}
        conflict = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": changed},
        )
        assert conflict.status_code == 409


def test_delete_with_a_body_is_rejected(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _ = service
    app = make_app(archives)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.request(
            "DELETE",
            f"/api/v2/conversation-archives/{ARCHIVE_ID}",
            headers=AUTH,
            content=b'{"unexpected":true}',
        )
        assert response.status_code == 400


def test_delete_missing_archive_returns_404(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, _ = service
    app = make_app(archives)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.delete(
            f"/api/v2/conversation-archives/{ARCHIVE_ID}",
            headers=AUTH,
        )
        assert response.status_code == 404


async def test_delete_body_stall_returns_the_typed_408(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _ = service
    from viki_agent.conversations import chat_archives_api as capi

    original_fail_after = anyio.fail_after
    monkeypatch.setattr(capi.anyio, "fail_after", lambda seconds: original_fail_after(0.02))
    app = make_app(archives)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "DELETE",
        "scheme": "http",
        "path": f"/api/v2/conversation-archives/{ARCHIVE_ID}",
        "raw_path": f"/api/v2/conversation-archives/{ARCHIVE_ID}".encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [
            (k.encode(), v.encode()) for k, v in {"host": "127.0.0.1:8765", **AUTH}.items()
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


def test_route_maps_decode_failed_to_415_and_other_asset_errors_to_503(
    tmp_path: Path,
) -> None:
    state = AppState(tmp_path / "data/state.json", Prefs())

    async def decode_failed(data: bytes) -> DecodedImage:
        raise AssetError("decode_failed")

    app = make_app(ConversationArchiveService(state.store, SCOPE, decode_failed))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
        assert response.status_code == 415

    async def worker_unavailable(data: bytes) -> DecodedImage:
        raise AssetError("worker_unavailable")

    app = make_app(ConversationArchiveService(state.store, SCOPE, worker_unavailable))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
        assert response.status_code == 503


def test_head_on_list_and_id_answers_like_get_and_creates_nothing(
    service: tuple[ConversationArchiveService, AppState],
) -> None:
    archives, state = service
    app = make_app(archives)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        before = len(state.store.list_conversation_archives(SCOPE))
        assert client.head("/api/v2/conversation-archives", headers=AUTH).status_code == 200
        assert (
            client.head(f"/api/v2/conversation-archives/{ARCHIVE_ID}", headers=AUTH).status_code
            == 404
        )
        assert len(state.store.list_conversation_archives(SCOPE)) == before

        created = client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
        assert created.status_code == 201
        after_create = len(state.store.list_conversation_archives(SCOPE))
        assert after_create == before + 1

        assert (
            client.head(f"/api/v2/conversation-archives/{ARCHIVE_ID}", headers=AUTH).status_code
            == 200
        )
        assert client.head("/api/v2/conversation-archives", headers=AUTH).status_code == 200
        assert len(state.store.list_conversation_archives(SCOPE)) == after_create


async def test_a_second_create_decodes_the_stored_archive_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, state = service
    first, _created = await archives.create(ARCHIVE_ID, archive())
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = archives._decode

    def spy(row: ConversationArchiveRow) -> ConversationArchive:
        threads.append(threading.current_thread())
        return original(row)

    monkeypatch.setattr(archives, "_decode", spy)
    assert await archives.create(ARCHIVE_ID, archive()) == (first, False)
    # Hide the stored row from the first lookup only: the insert then loses to it, as it would
    # to a concurrent create, and the winner is decoded instead.
    stored = state.store.get_conversation_archive
    lookups = 0

    def racing_get(scope: str, archive_id: str) -> ConversationArchiveRow | None:
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else stored(scope, archive_id)

    monkeypatch.setattr(state.store, "get_conversation_archive", racing_get)
    assert await archives.create(ARCHIVE_ID, archive()) == (first, False)
    assert lookups == 2 and len(threads) == 2
    assert all(thread is not loop_thread for thread in threads)


async def test_get_decodes_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    first, _created = await archives.create(ARCHIVE_ID, archive())
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = archives._decode

    def spy(row: ConversationArchiveRow) -> ConversationArchive:
        threads.append(threading.current_thread())
        return original(row)

    monkeypatch.setattr(archives, "_decode", spy)
    assert await archives.get(ARCHIVE_ID) == first
    assert len(threads) == 1 and threads[0] is not loop_thread


async def test_normalise_runs_off_the_event_loop_thread(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    threads: list[threading.Thread] = []
    original = chat_archives_module._normalise

    def spy(raw: object) -> tuple[dict[str, object], dict[str, bytes]]:
        threads.append(threading.current_thread())
        return original(raw)

    monkeypatch.setattr(chat_archives_module, "_normalise", spy)
    await archives.create(ARCHIVE_ID, archive())
    assert threads and threads[0] is not threading.main_thread()


async def test_create_encodes_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = chat_archives_module._encode

    def spy(archive: dict[str, object]) -> bytes:
        threads.append(threading.current_thread())
        return original(archive)

    monkeypatch.setattr(chat_archives_module, "_encode", spy)
    await archives.create(ARCHIVE_ID, archive())
    assert threads and all(thread is not loop_thread for thread in threads)


async def test_create_seals_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = chat_archives_module.protection.protect

    def spy(*args: Any, **kwargs: Any) -> bytes:
        threads.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(chat_archives_module.protection, "protect", spy)
    await archives.create(ARCHIVE_ID, archive())
    assert len(threads) == 1 and threads[0] is not loop_thread


async def test_get_route_encodes_its_answer_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    await archives.create(ARCHIVE_ID, archive())
    app = make_app(archives)
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = chat_archives_api_module._json

    def spy(body: object, status: int = 200) -> Response:
        threads.append(threading.current_thread())
        return original(body, status)

    monkeypatch.setattr(chat_archives_api_module, "_json", spy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.get(f"/api/v2/conversation-archives/{ARCHIVE_ID}", headers=AUTH)
    assert response.status_code == 200
    assert len(threads) == 1 and threads[0] is not loop_thread


async def test_post_route_parses_and_encodes_off_the_event_loop(
    service: tuple[ConversationArchiveService, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    archives, _state = service
    app = make_app(archives)
    loop_thread = threading.current_thread()
    parses: list[threading.Thread] = []
    encodes: list[threading.Thread] = []
    original_unique = chat_archives_api_module._unique
    original_json = chat_archives_api_module._json

    def parse_spy(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parses.append(threading.current_thread())
        return original_unique(pairs)

    def encode_spy(body: object, status: int = 200) -> Response:
        encodes.append(threading.current_thread())
        return original_json(body, status)

    monkeypatch.setattr(chat_archives_api_module, "_unique", parse_spy)
    monkeypatch.setattr(chat_archives_api_module, "_json", encode_spy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post(
            "/api/v2/conversation-archives",
            headers=AUTH,
            json={"archive_id": ARCHIVE_ID, "archive": archive()},
        )
    assert response.status_code == 201
    assert parses and all(thread is not loop_thread for thread in parses)
    assert len(encodes) == 1 and encodes[0] is not loop_thread
