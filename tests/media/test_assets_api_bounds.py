"""Concurrency and intake limits for the private asset HTTP boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Scope

import viki_agent.media.assets as assets_module
from tests.media.test_assets import PNG, FakeStore
from viki_agent import protection
from viki_agent.media.assets import AssetDownload, AssetService, DecodedImage, Decoder
from viki_agent.media.assets_api import asset_routes
from viki_agent.storage import SessionStore
from viki_agent.turn_input import (
    CLAUDE_MAX_ATTACHMENT_BYTES,
    CLAUDE_MAX_DIMENSION,
    CLAUDE_MAX_TURN_ATTACHMENT_BYTES,
    IMAGE_MEDIA_TYPES,
    MAX_ATTACHMENTS,
)

BASE = "http://127.0.0.1:8765"
TOKEN = "asset-bound-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
MUTATION = {**AUTH, "origin": BASE}
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def real_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope_hex: str = "c" * 64,
    decoder: Decoder | None = None,
    store: FakeStore | None = None,
) -> tuple[Starlette, AssetService, FakeStore, set[asyncio.Task[Any]]]:
    """A real AssetService (a FakeStore, no fake decoder answers) behind the real routes."""
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets_module, "require_private_store", lambda store: None)
    if store is None:
        store = FakeStore(tmp_path / "state.sqlite3")

    async def default_decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, store),
        scope_hex,
        decoder=decoder or default_decoder,
    )
    workers: set[asyncio.Task[Any]] = set()
    routes = asset_routes(
        service,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({BASE}),
        workers=workers,
    )
    return Starlette(routes=routes), service, store, workers


@dataclass
class FakeAssets:
    download_gate: asyncio.Event | None = None
    download_started: asyncio.Event = field(default_factory=asyncio.Event)
    stream_gates: list[asyncio.Event] = field(default_factory=list)
    stream_started: list[asyncio.Event] = field(default_factory=list)
    download_calls: int = 0
    removed: list[str] = field(default_factory=list)

    async def download(self, asset_id: str) -> AssetDownload:
        self.download_calls += 1
        self.download_started.set()
        if self.download_gate is not None:
            await self.download_gate.wait()
        index = self.download_calls - 1
        gate = self.stream_gates[index] if index < len(self.stream_gates) else None
        started = self.stream_started[index] if index < len(self.stream_started) else None

        async def body() -> AsyncIterator[bytes]:
            if started is not None:
                started.set()
            try:
                if gate is not None:
                    await gate.wait()
                yield b"image"
            finally:
                if started is not None:
                    started.set()

        return AssetDownload("image/png", 5, body())

    async def remove(self, asset_id: str) -> bool:
        self.removed.append(asset_id)
        return True


def app_at(
    fake: FakeAssets, image_input: Callable[[], dict[str, str | None]] | None = None
) -> tuple[Starlette, set[asyncio.Task[Any]]]:
    workers: set[asyncio.Task[Any]] = set()
    return (
        Starlette(
            routes=asset_routes(
                cast(AssetService, fake),
                authorized=lambda request: (
                    request.headers.get("authorization") == f"Bearer {TOKEN}"
                ),
                origins=frozenset({BASE}),
                workers=workers,
                image_input=image_input,
            )
        ),
        workers,
    )


def test_attachment_capabilities_are_static_and_authenticated() -> None:
    app, _workers = app_at(FakeAssets())
    client = TestClient(app, base_url=BASE)
    path = "/api/v2/attachment-capabilities"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={**AUTH, "origin": "https://evil.test"}).status_code == 403
    assert client.get(path + "?ignored=1", headers=AUTH).status_code == 400
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "provider": "claude",
        "media_types": sorted(IMAGE_MEDIA_TYPES),
        "max_files": MAX_ATTACHMENTS,
        "max_file_bytes": CLAUDE_MAX_ATTACHMENT_BYTES,
        "max_turn_bytes": CLAUDE_MAX_TURN_ATTACHMENT_BYTES,
        "max_dimension": CLAUDE_MAX_DIMENSION,
        "image_input": {"status": "unverified", "reason": "evidence_unavailable"},
    }


def test_attachment_capabilities_carry_the_injected_image_input_status() -> None:
    app, _workers = app_at(
        FakeAssets(), image_input=lambda: {"status": "supported", "reason": None}
    )
    response = TestClient(app, base_url=BASE).get("/api/v2/attachment-capabilities", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["image_input"] == {"status": "supported", "reason": None}


def scope(path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> Scope:
    values = {"host": "127.0.0.1:8765", **(headers or {})}
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [(key.encode(), value.encode()) for key, value in values.items()],
    }


async def invoke(app: ASGIApp, request_scope: Scope, receive: Any) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await app(request_scope, receive, send)
    return messages


async def disconnect_waiter() -> Message:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_a_third_read_waits_until_a_stream_finishes() -> None:
    """A sent message renders every image preview at once; a third read must queue, not fail."""
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    fake = FakeAssets(stream_gates=gates, stream_started=started)
    app, _workers = app_at(fake)

    async def receive() -> Message:
        return await disconnect_waiter()

    def read() -> asyncio.Task[list[Message]]:
        return asyncio.create_task(
            invoke(cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive)
        )

    callers = [read(), read()]
    await asyncio.gather(*(event.wait() for event in started))
    third = read()
    done, _pending = await asyncio.wait({third}, timeout=0.05)
    assert not done and fake.download_calls == 2  # the read slots are both held
    gates[0].set()
    status = next(
        message["status"] for message in await third if message["type"] == "http.response.start"
    )
    assert status == 200
    gates[1].set()
    await asyncio.gather(*callers)


@pytest.mark.asyncio
async def test_a_read_that_waits_past_its_deadline_is_overloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import viki_agent.media.assets_api as api

    monkeypatch.setattr(api, "READ_WAIT_S", 0.01)
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    app, _workers = app_at(FakeAssets(stream_gates=gates, stream_started=started))

    async def receive() -> Message:
        return await disconnect_waiter()

    callers = [
        asyncio.create_task(
            invoke(cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive)
        )
        for _ in range(2)
    ]
    await asyncio.gather(*(event.wait() for event in started))
    third = await invoke(cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive)
    status = next(
        message["status"] for message in third if message["type"] == "http.response.start"
    )
    assert status == 429
    body = b"".join(
        message.get("body", b"") for message in third if message["type"] == "http.response.body"
    )
    assert json.loads(body)["error"]["code"] == "overloaded"
    for gate in gates:
        gate.set()
    await asyncio.gather(*callers)


@pytest.mark.asyncio
async def test_cancelled_read_drains_the_download_worker_before_releasing_its_slot() -> None:
    gate = asyncio.Event()
    fake = FakeAssets(download_gate=gate)
    app, workers = app_at(fake)

    async def receive() -> Message:
        return await disconnect_waiter()

    caller = asyncio.create_task(
        invoke(cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive)
    )
    await fake.download_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert len(workers) == 1
    draining = tuple(workers)
    gate.set()
    await asyncio.gather(*draining)
    assert not workers


@pytest.mark.asyncio
async def test_cancelling_streams_releases_their_read_slots() -> None:
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    app, _workers = app_at(FakeAssets(stream_gates=gates, stream_started=started))

    async def receive() -> Message:
        return await disconnect_waiter()

    callers = [
        asyncio.create_task(
            invoke(cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive)
        )
        for _ in range(2)
    ]
    await asyncio.gather(*(event.wait() for event in started))
    for caller in callers:
        caller.cancel()
    for caller in callers:
        with pytest.raises(asyncio.CancelledError):
            await caller
    response = await invoke(
        cast(ASGIApp, app), scope(f"/api/v2/assets/{SID}", headers=AUTH), receive
    )
    status = next(
        message["status"] for message in response if message["type"] == "http.response.start"
    )
    assert status == 200


@pytest.mark.asyncio
async def test_remove_empty_body_has_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import viki_agent.media.assets_api as api

    monkeypatch.setattr(api, "REMOVE_BODY_TIMEOUT_S", 0.01)
    fake = FakeAssets()
    app, _workers = app_at(fake)
    first = True

    async def receive() -> Message:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": True}
        return await disconnect_waiter()

    messages = await invoke(
        cast(ASGIApp, app),
        scope(
            f"/api/v2/assets/{SID}/remove",
            method="POST",
            headers={**AUTH, "origin": BASE},
        ),
        receive,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    assert status == 408
    assert fake.removed == []


async def _upload_invoke(
    app: ASGIApp, headers: dict[str, str], receive: Callable[[], Any]
) -> tuple[int, dict[str, Any]]:
    messages = await invoke(app, scope("/api/v2/assets", method="POST", headers=headers), receive)
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return status, json.loads(body) if body else {}


@pytest.mark.asyncio
async def test_partial_uploads_leave_no_trace_and_keep_an_earlier_asset_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service, store, _workers = real_app(tmp_path, monkeypatch)
    client = TestClient(cast(ASGIApp, app), base_url=BASE)
    earlier = client.post(
        "/api/v2/assets", headers={**MUTATION, "x-file-name": "earlier.png"}, content=PNG
    )
    assert earlier.status_code == 201
    earlier_id = earlier.json()["asset_id"]

    def assert_clean() -> None:
        assert list((tmp_path / "assets" / "incoming").iterdir()) == []
        assert (service._reserved_count, service._reserved_bytes) == (0, 0)
        assert set(store.rows) == {earlier_id}
        readback = client.get(f"/api/v2/assets/{earlier_id}", headers=AUTH)
        assert readback.status_code == 200 and readback.content == PNG

    # incomplete: declares more bytes than the body ever delivers
    delivered = False

    async def incomplete_receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": PNG[:10], "more_body": False}
        return await disconnect_waiter()

    status, payload = await _upload_invoke(
        cast(ASGIApp, app),
        {**MUTATION, "x-file-name": "a.png", "content-length": str(len(PNG))},
        incomplete_receive,
    )
    assert status == 400 and payload["error"]["code"] == "incomplete"
    assert_clean()

    # too_large mid-stream: no declared length, a later chunk overruns the (patched) cap
    remaining = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ]
    )

    async def too_large_receive() -> Message:
        try:
            return next(remaining)
        except StopIteration:
            return await disconnect_waiter()

    with monkeypatch.context() as capped:
        capped.setattr(assets_module, "MAX_ATTACHMENT_BYTES", 4)
        status, payload = await _upload_invoke(
            cast(ASGIApp, app), {**MUTATION, "x-file-name": "a.png"}, too_large_receive
        )
    assert status == 413 and payload["error"]["code"] == "too_large"
    assert_clean()

    # empty: the body ends without a single byte
    ended = False

    async def empty_receive() -> Message:
        nonlocal ended
        if not ended:
            ended = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await disconnect_waiter()

    status, payload = await _upload_invoke(
        cast(ASGIApp, app), {**MUTATION, "x-file-name": "a.png"}, empty_receive
    )
    assert status == 400 and payload["error"]["code"] == "empty"
    assert_clean()

    # a client disconnect partway through the body
    disconnected = False

    async def disconnect_receive() -> Message:
        nonlocal disconnected
        if not disconnected:
            disconnected = True
            return {"type": "http.request", "body": b"abc", "more_body": True}
        return {"type": "http.disconnect"}

    status, payload = await _upload_invoke(
        cast(ASGIApp, app), {**MUTATION, "x-file-name": "a.png"}, disconnect_receive
    )
    assert status == 400 and payload["error"]["code"] == "disconnected"
    assert_clean()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    [
        "..",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
        "a" * 35,
        "a" * 37,
    ],
)
async def test_malformed_asset_ids_are_refused_before_any_file_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    opened: list[Path] = []
    real_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)
    app, _service, _store, _workers = real_app(tmp_path, monkeypatch)

    # A raw ASGI call, never TestClient/httpx: its URL builder collapses a ".." path segment
    # before the request is even sent, which would test the client instead of the route.
    async def empty_body() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    read_messages = await invoke(
        cast(ASGIApp, app), scope(f"/api/v2/assets/{bad_id}", headers=AUTH), empty_body
    )
    read_status = next(
        message["status"] for message in read_messages if message["type"] == "http.response.start"
    )
    read_body = b"".join(
        message.get("body", b"")
        for message in read_messages
        if message["type"] == "http.response.body"
    )
    assert read_status == 400
    assert json.loads(read_body)["error"]["code"] == "invalid_asset"

    remove_messages = await invoke(
        cast(ASGIApp, app),
        scope(f"/api/v2/assets/{bad_id}/remove", method="POST", headers=MUTATION),
        empty_body,
    )
    remove_status = next(
        message["status"] for message in remove_messages if message["type"] == "http.response.start"
    )
    remove_body = b"".join(
        message.get("body", b"")
        for message in remove_messages
        if message["type"] == "http.response.body"
    )
    assert remove_status == 400
    assert json.loads(remove_body)["error"]["code"] == "invalid_asset"

    assert opened == []


@pytest.mark.asyncio
async def test_an_asset_from_another_scope_answers_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_a, _service_a, store, _workers_a = real_app(tmp_path, monkeypatch, scope_hex="a" * 64)
    client_a = TestClient(cast(ASGIApp, app_a), base_url=BASE)
    uploaded = client_a.post(
        "/api/v2/assets", headers={**MUTATION, "x-file-name": "a.png"}, content=PNG
    )
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["asset_id"]

    app_b, _service_b, _store_b, _workers_b = real_app(
        tmp_path, monkeypatch, scope_hex="b" * 64, store=store
    )
    client_b = TestClient(cast(ASGIApp, app_b), base_url=BASE)

    read = client_b.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 404

    removed = client_b.post(f"/api/v2/assets/{asset_id}/remove", headers=MUTATION)
    assert removed.status_code == 404


@pytest.mark.parametrize(
    "raw_name",
    ["..%2F", "%5C", "%00", "%FF", "%E3%81%82" * 171],
)
def test_bad_x_file_name_values_are_refused_as_invalid_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_name: str
) -> None:
    app, _service, store, _workers = real_app(tmp_path, monkeypatch)
    client = TestClient(cast(ASGIApp, app), base_url=BASE)

    response = client.post(
        "/api/v2/assets", headers={**MUTATION, "x-file-name": raw_name}, content=PNG
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_name"
    assert store.rows == {}
