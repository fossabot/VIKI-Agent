"""The asset routes accept raw bytes and never expose app-owned paths."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message

from tests.media.test_assets import FakeStore
from tests.media.test_assets_api_bounds import disconnect_waiter, invoke, scope
from viki_agent import protection
from viki_agent.media import assets
from viki_agent.media.assets import (
    AssetDownload,
    AssetError,
    AssetService,
    DecodedImage,
    Decoder,
    UploadedAsset,
)
from viki_agent.media.assets_api import asset_routes

BASE = "http://127.0.0.1:8765"
AUTH = {"authorization": "Bearer asset-token"}
MUTATION = {**AUTH, "origin": BASE}
SCOPE = "b" * 64
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def app_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoder: Decoder
) -> tuple[Starlette, AssetService, set[asyncio.Task[Any]]]:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    service = AssetService(
        tmp_path / "assets",
        FakeStore(tmp_path / "state.sqlite3"),  # type: ignore[arg-type]
        SCOPE,
        decoder=decoder,
    )
    workers: set[asyncio.Task[Any]] = set()
    routes = asset_routes(
        service,
        authorized=lambda request: request.headers.get("authorization") == "Bearer asset-token",
        origins=frozenset({BASE}),
        workers=workers,
    )
    return Starlette(routes=routes), service, workers


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    app, _service, _workers = app_with(tmp_path, monkeypatch, decoder)
    return TestClient(app, base_url=BASE)


def test_upload_read_and_remove_roundtrip(client: TestClient) -> None:
    uploaded = client.post(
        "/api/v2/assets",
        headers={
            **MUTATION,
            "content-type": "text/plain",
            "x-file-name": quote("Résumé 1.png", safe=""),
        },
        content=PNG,
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert set(body) == {"asset_id", "media_type", "byte_size", "width", "height", "name"}
    assert body["media_type"] == "image/png"  # detected bytes win over the request MIME hint
    assert body["byte_size"] == len(PNG)
    assert body["name"] == "Résumé 1.png"

    read = client.get(f"/api/v2/assets/{body['asset_id']}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    assert read.headers["content-type"] == "image/png"
    assert read.headers["cache-control"] == "no-store"
    assert read.headers["x-content-type-options"] == "nosniff"
    assert "filename" not in read.headers["content-disposition"]
    removed = client.post(f"/api/v2/assets/{body['asset_id']}/remove", headers=MUTATION)
    assert removed.status_code == 204
    assert client.get(f"/api/v2/assets/{body['asset_id']}", headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "headers,status,code",
    [
        ({"origin": BASE, "x-file-name": "a.png"}, 401, "unauthorized"),
        (
            {**AUTH, "origin": "http://evil.invalid", "x-file-name": "a.png"},
            403,
            "forbidden_origin",
        ),
        ({**AUTH, "x-file-name": "a.png"}, 403, "forbidden_origin"),
        ({**MUTATION}, 400, "invalid_name"),
    ],
)
def test_upload_authenticates_and_requires_origin(
    client: TestClient, headers: dict[str, str], status: int, code: str
) -> None:
    response = client.post("/api/v2/assets", headers=headers, content=PNG)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_an_upload_deadline_cancels_the_service_once_and_frees_its_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline's cancellation reaches the service task once, so its cleanup runs to the
    end: a repeated cancel would land inside the unlink or the release and leak both."""
    import viki_agent.media.assets_api as api

    scopes: list[anyio.CancelScope] = []

    @contextmanager
    def deadline(_seconds: float) -> Iterator[anyio.CancelScope]:
        # anyio.fail_after's own contract: a cancelled scope ends in TimeoutError.
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            yield scope
        if scope.cancelled_caught:
            raise TimeoutError

    monkeypatch.setattr(api.anyio, "fail_after", deadline)

    async def decoder(_path: Path) -> DecodedImage:
        scopes[-1].cancel()  # the deadline expires while the decoder runs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    app, service, workers = app_with(tmp_path, monkeypatch, decoder)
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": PNG, "more_body": False}
        return await disconnect_waiter()

    headers = {**MUTATION, "x-file-name": "a.png", "content-length": str(len(PNG))}
    messages = await invoke(
        cast(ASGIApp, app), scope("/api/v2/assets", method="POST", headers=headers), receive
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    assert status == 408
    await asyncio.gather(*tuple(workers), return_exceptions=True)
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)
    assert list((tmp_path / "assets" / "incoming").iterdir()) == []


def test_upload_answers_decoder_unavailable_at_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def decoder(_path: Path) -> DecodedImage:
        raise AssetError("decoder_unavailable")

    app, _service, _workers = app_with(tmp_path, monkeypatch, decoder)
    response = TestClient(app, base_url=BASE).post(
        "/api/v2/assets", headers={**MUTATION, "x-file-name": "a.png"}, content=PNG
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "decoder_unavailable"
    assert response.json()["error"]["message"] == (
        "The image-checking feature is unavailable. Quit VIKI and run `uv sync`."
    )


def test_upload_rejects_oversize_length_before_reading(client: TestClient) -> None:
    response = client.post(
        "/api/v2/assets",
        headers={**MUTATION, "x-file-name": "large.png", "content-length": str(20 << 20 | 1)},
        content=b"x",
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert response.json()["error"]["message"] == "File must be 20 MiB or smaller."


def test_read_authenticates_and_remove_refuses_a_body(client: TestClient) -> None:
    missing_id = "00000000-0000-4000-8000-000000000001"
    assert client.get(f"/api/v2/assets/{missing_id}").status_code == 401
    response = client.post(
        f"/api/v2/assets/{missing_id}/remove", headers=MUTATION, content=b"unexpected"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"


AVATAR_UPLOAD = {**MUTATION, "x-viki-asset-purpose": "avatar", "x-file-name": "private-face.png"}
AVATAR_SOURCE_CAP = 5 << 20


def fake_decoder_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, AssetService]:
    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    app, service, _workers = app_with(tmp_path, monkeypatch, decoder)
    return TestClient(app, base_url=BASE), service


def assert_nothing_kept(service: AssetService) -> None:
    assert cast(FakeStore, service._store).rows == {}
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


def test_an_avatar_upload_declared_past_its_cap_is_refused_before_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, service = fake_decoder_app(tmp_path, monkeypatch)
    with caplog.at_level("DEBUG"):
        response = client.post(
            "/api/v2/assets", headers=AVATAR_UPLOAD, content=b"x" * (AVATAR_SOURCE_CAP + 1)
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert response.json()["error"]["message"] == "File must be 5 MiB or smaller."
    assert_nothing_kept(service)
    # Refused from Content-Length alone: the service never prepared its stage folder.
    assert not (tmp_path / "assets" / "incoming").exists()
    assert "private-face" not in caplog.text


def test_an_avatar_upload_streamed_past_its_cap_stops_and_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, service = fake_decoder_app(tmp_path, monkeypatch)
    body = iter([b"x" * (1 << 20)] * 5 + [b"x"])  # no Content-Length: chunked
    with caplog.at_level("DEBUG"):
        response = client.post("/api/v2/assets", headers=AVATAR_UPLOAD, content=body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert response.json()["error"]["message"] == "File must be 5 MiB or smaller."
    assert_nothing_kept(service)
    # The streamed bound, not the declared one, refused it: the stage was made, then removed.
    assert list((tmp_path / "assets" / "incoming").iterdir()) == []
    assert "private-face" not in caplog.text


def test_an_avatar_upload_of_exactly_its_cap_passes_the_size_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _service = fake_decoder_app(tmp_path, monkeypatch)
    response = client.post(
        "/api/v2/assets", headers=AVATAR_UPLOAD, content=b"x" * AVATAR_SOURCE_CAP
    )
    assert response.status_code == 201
    assert response.json()["byte_size"] == AVATAR_SOURCE_CAP


def test_an_upload_without_a_purpose_keeps_the_attachment_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _service = fake_decoder_app(tmp_path, monkeypatch)
    response = client.post(
        "/api/v2/assets", headers={**MUTATION, "x-file-name": "a.png"}, content=b"x" * (20 << 20)
    )
    assert response.status_code == 201
    assert response.json()["byte_size"] == 20 << 20


@pytest.mark.parametrize(
    "purposes",
    [["Avatar"], ["chat"], [""], ["avatar", "avatar"]],
)
def test_an_upload_names_only_the_avatar_purpose_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, purposes: list[str]
) -> None:
    client, service = fake_decoder_app(tmp_path, monkeypatch)
    headers = [(key, value) for key, value in MUTATION.items()] + [("x-file-name", "a.png")]
    headers += [("x-viki-asset-purpose", purpose) for purpose in purposes]
    response = client.post("/api/v2/assets", headers=headers, content=PNG)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_purpose"
    assert_nothing_kept(service)


def test_relink_ignores_the_purpose_and_keeps_the_attachment_cap(client: TestClient) -> None:
    """Past the avatar cap with the purpose set, a relink still reaches its own lookup."""
    missing_id = "00000000-0000-4000-8000-000000000001"
    response = client.post(
        f"/api/v2/assets/{missing_id}/relink",
        headers={**MUTATION, "x-viki-asset-purpose": "chat"},
        content=b"x" * (AVATAR_SOURCE_CAP + 1),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def _fake_app(service: object) -> tuple[Starlette, set[asyncio.Task[Any]]]:
    workers: set[asyncio.Task[Any]] = set()
    return Starlette(
        routes=asset_routes(
            cast(AssetService, service),
            authorized=lambda request: True,
            origins=frozenset({BASE}),
            workers=workers,
        )
    ), workers


def _permit_releases(monkeypatch: pytest.MonkeyPatch) -> list[list[object]]:
    releases: list[list[object]] = []
    capacity_limiter = anyio.CapacityLimiter

    def observed(tokens: float) -> anyio.CapacityLimiter:
        limiter = capacity_limiter(tokens)
        returned: list[object] = []
        releases.append(returned)
        release = limiter.release_on_behalf_of

        class Observed:
            def __getattr__(self, name: str) -> Any:
                return getattr(limiter, name)

            def release_on_behalf_of(self, permit: object) -> None:
                returned.append(permit)
                release(permit)

        return cast(anyio.CapacityLimiter, Observed())

    monkeypatch.setattr(anyio, "CapacityLimiter", observed)
    return releases


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["upload", "relink", "remove"])
async def test_mutation_permits_follow_cancelled_service_cleanup(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    releases = _permit_releases(monkeypatch)
    entered: asyncio.Queue[None] = asyncio.Queue()
    cancelled: asyncio.Queue[None] = asyncio.Queue()
    cleanup = asyncio.Event()
    calls = 0

    async def work(*args: Any, **kwargs: Any) -> UploadedAsset | bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            entered.put_nowait(None)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.put_nowait(None)
                await cleanup.wait()
                raise
        return True if kind == "remove" else UploadedAsset("id", "image/png", 1, 1, 1, "a.png")

    app, workers = _fake_app(SimpleNamespace(upload=work, relink=work, remove=work))
    path = "/api/v2/assets" if kind == "upload" else f"/api/v2/assets/id/{kind}"

    async def request(*, invalid: bool = False) -> list[Message]:
        async def receive() -> Message:
            return {"type": "http.request", "body": b"x" if invalid else b"", "more_body": False}

        headers = {**MUTATION, "x-file-name": "a.png"}
        if invalid and kind != "remove":
            headers["content-length"] = "0"
        return await invoke(
            cast(ASGIApp, app), scope(path, method="POST", headers=headers), receive
        )

    callers: list[asyncio.Task[list[Message]]] = []
    try:
        # A refusal before any service call still returns its own permit exactly once.
        for _ in range(2):
            refused = await request(invalid=True)
            assert (
                next(item["status"] for item in refused if item["type"] == "http.response.start")
                == 400
            )
        assert calls == 0 and len(releases[0]) == 2
        for _ in range(2):
            caller = asyncio.create_task(request())
            callers.append(caller)
            await entered.get()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await cancelled.get()
        assert len(workers) == 2
        refused = await request()
        assert (
            next(item["status"] for item in refused if item["type"] == "http.response.start") == 429
        )
        assert calls == 2 and len(releases[0]) == 2
        draining = tuple(workers)
        cleanup.set()
        await asyncio.gather(*draining, return_exceptions=True)
        assert not workers and len(releases[0]) == 4
        accepted = await request()
        assert next(
            item["status"] for item in accepted if item["type"] == "http.response.start"
        ) == (201 if kind == "upload" else 204)
        assert calls == 3 and len(releases[0]) == len(set(releases[0])) == 5
    finally:
        cleanup.set()
        for caller in callers:
            caller.cancel()
        await asyncio.gather(*callers, *tuple(workers), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "disconnect"])
async def test_read_permits_return_when_response_headers_never_finish(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    import viki_agent.media.assets_api as api

    monkeypatch.setattr(api, "READ_WAIT_S", 0.01)
    releases = _permit_releases(monkeypatch)
    body_entries = 0

    async def download(_asset_id: str) -> AssetDownload:
        async def body():
            nonlocal body_entries
            body_entries += 1
            yield b"image"

        return AssetDownload("image/png", 5, body())

    app, workers = _fake_app(SimpleNamespace(download=download))
    for _ in range(2):
        started, disconnected = asyncio.Event(), asyncio.Event()

        async def send(message: Message, started: asyncio.Event = started) -> None:
            if message["type"] == "http.response.start":
                started.set()
                await asyncio.Event().wait()

        async def receive(disconnected: asyncio.Event = disconnected) -> Message:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        request_scope = scope("/api/v2/assets/id", headers=AUTH)
        request_scope["asgi"]["spec_version"] = "2.3"
        caller = asyncio.create_task(app(request_scope, receive, send))
        try:
            await started.wait()
            if ending == "cancel":
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            else:
                disconnected.set()
                await caller
        finally:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
    assert not workers and body_entries == 0
    messages = await invoke(
        cast(ASGIApp, app), scope("/api/v2/assets/id", headers=AUTH), disconnect_waiter
    )
    assert next(item["status"] for item in messages if item["type"] == "http.response.start") == 200
    assert body_entries == 1
    assert len(releases[1]) == len(set(releases[1])) == 3


@pytest.mark.asyncio
async def test_interrupted_asset_stream_closes_before_returning_its_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases = _permit_releases(monkeypatch)
    closing, closed = asyncio.Event(), asyncio.Event()

    async def download(_asset_id: str) -> AssetDownload:
        async def body():
            try:
                yield b"image"
            finally:
                closing.set()
                await closed.wait()

        return AssetDownload("image/png", 5, body())

    app, workers = _fake_app(SimpleNamespace(download=download))
    sent = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            sent.set()
            await asyncio.Event().wait()

    request_scope = scope("/api/v2/assets/id", headers=AUTH)
    request_scope["asgi"]["spec_version"] = "2.4"
    caller = asyncio.create_task(app(request_scope, disconnect_waiter, send))
    waiting = asyncio.create_task(closing.wait())
    try:
        await sent.wait()
        caller.cancel()
        done, _pending = await asyncio.wait(
            {caller, waiting}, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
        )
        assert waiting in done and not caller.done()
        caller.cancel()  # a later cancellation must not interrupt the iterator's close
        assert not workers and releases[1] == []
        closed.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(releases[1]) == 1
    finally:
        closed.set()
        caller.cancel()
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        await asyncio.gather(caller, return_exceptions=True)
