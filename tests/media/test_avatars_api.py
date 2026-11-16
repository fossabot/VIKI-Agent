"""The avatar transform and variant read routes over a real store and a fake worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.types import Message

from tests.media.test_avatars import HANG_GUARD_S, FakeAssets, FakeWorker
from tests.test_server import TOKEN as APP_TOKEN
from tests.test_server import running
from viki_agent import protection, server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.media import avatars, avatars_api
from viki_agent.media.assets import AssetService
from viki_agent.media.avatars import (
    AVATAR_SOURCE_BYTES,
    Avatar,
    AvatarDownload,
    AvatarError,
    AvatarService,
    Crop,
)
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore

BASE = "http://127.0.0.1:8765"
TOKEN = "avatar-route-token"
HEADERS = {"authorization": f"Bearer {TOKEN}", "origin": BASE}
PATH = "/api/v2/avatars"
CROP = {"x": 0, "y": 0, "width": 64, "height": 64}


class GatedWorker(FakeWorker):
    """Holds each transform in the worker until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, stage: Path, crop: Crop, rotation: int) -> Any:
        self.entered.set()
        await self.gate.wait()
        return await super().__call__(stage, crop, rotation)


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: FakeAssets | None = None,
    worker: FakeWorker | None = None,
) -> tuple[Starlette, AvatarService, SessionStore, set[asyncio.Task[Any]]]:
    monkeypatch.setattr(protection, "require_private_directory", lambda _path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda _path: None)
    monkeypatch.setattr(
        protection,
        "create_private_directory",
        lambda path: (path.mkdir(), True)[1] if not path.exists() else False,
    )
    monkeypatch.setattr(avatars, "require_private_store", lambda _store: None)
    (tmp_path / "assets").mkdir()
    store = SessionStore(tmp_path / "state.sqlite3", lambda: {})
    service = AvatarService(
        tmp_path / "assets" / "avatars",
        store,
        cast(AssetService, source or FakeAssets()),
        worker=worker or FakeWorker(),
    )
    workers: set[asyncio.Task[Any]] = set()
    routes = avatar_routes_for(service, workers)
    return Starlette(routes=routes), service, store, workers


def avatar_routes_for(service: AvatarService, workers: set[asyncio.Task[Any]]) -> list[Any]:
    return avatars_api.avatar_routes(
        service,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({BASE}),
        workers=workers,
    )


def client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def request_body() -> dict[str, object]:
    return {
        "avatar_id": str(uuid.uuid4()),
        "source_asset_id": str(uuid.uuid4()),
        "crop": {"x": 0, "y": 0, "width": 64, "height": 64},
        "rotation": 90,
    }


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def test_transform_answers_the_manifest_and_an_exact_retry_the_same_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service, store, _workers = build(tmp_path, monkeypatch)
    body = request_body()
    # The body bound is inclusive: a request padded to exactly the limit is still admitted.
    padded = json.dumps(body).ljust(avatars_api.MAX_BODY_BYTES)
    json_headers = {**HEADERS, "content-type": "application/json"}
    async with client(app) as api:
        first = await api.post(PATH, headers=json_headers, content=padded)
        retry = await api.post(PATH, headers=HEADERS, json=body)
        conflict = await api.post(
            PATH,
            headers=HEADERS,
            json={**body, "crop": {"x": 1, "y": 0, "width": 64, "height": 64}},
        )

    assert first.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    manifest = first.json()
    assert set(manifest) == {"avatar_id", "master", "variants", "total_bytes", "created_at"}
    assert manifest["avatar_id"] == body["avatar_id"]
    assert set(manifest["master"]) == {"size", "byte_size", "sha256"}
    assert [item["size"] for item in manifest["variants"]] == [24, 32, 64, 128, 256]
    assert manifest["total_bytes"] == manifest["master"]["byte_size"] + sum(
        item["byte_size"] for item in manifest["variants"]
    )
    assert retry.status_code == 201 and retry.json() == manifest
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM avatars").fetchone()[0] == 1
    assert [path.name for path in (service.root / "blobs").iterdir()] == [body["avatar_id"]]
    assert conflict.status_code == 409 and code(conflict) == "command_conflict"
    assert conflict.headers["cache-control"] == "no-store"


async def test_the_gate_refuses_before_any_transform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeWorker()
    source = FakeAssets()
    app, _service, _store, _workers = build(tmp_path, monkeypatch, source=source, worker=worker)
    json_headers = {**HEADERS, "content-type": "application/json"}
    good = json.dumps(request_body())
    cases: list[tuple[str, dict[str, str], str, int, str]] = [
        (PATH, {"origin": BASE, "content-type": "application/json"}, good, 401, "unauthorized"),
        (PATH, {**json_headers, "origin": ""}, good, 403, "forbidden_origin"),
        (PATH, {**json_headers, "origin": "https://evil.test"}, good, 403, "forbidden_origin"),
        (PATH + "?x=1", json_headers, good, 400, "invalid_request"),
        (PATH, {**json_headers, "content-type": "text/plain"}, good, 400, "invalid_request"),
        (
            PATH,
            {**json_headers, "content-encoding": "gzip"},
            good,
            415,
            "invalid_representation",
        ),
        (PATH, json_headers, " " * (avatars_api.MAX_BODY_BYTES + 1), 413, "too_large"),
        (PATH, json_headers, json.dumps({**request_body(), "extra": 1}), 400, "invalid_request"),
        (PATH, json_headers, good[:-1] + ',"rotation":0}', 400, "invalid_request"),
        (PATH, json_headers, "[1]", 400, "invalid_request"),
    ]
    async with client(app) as api:
        missing_origin = await api.post(
            PATH,
            headers={"authorization": HEADERS["authorization"], "content-type": "application/json"},
            content=good,
        )
        answers = [
            await api.post(path, headers=headers, content=content)
            for path, headers, content, _status, _code in cases
        ]

    assert missing_origin.status_code == 403 and code(missing_origin) == "forbidden_origin"
    for response, (_path, _headers, _content, status, expected) in zip(answers, cases, strict=True):
        assert (response.status_code, code(response)) == (status, expected)
        assert response.headers["cache-control"] == "no-store"
    assert worker.calls == 0 and source.downloads == 0


async def test_a_body_that_never_finishes_arriving_is_refused_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # httpx's ASGI transport hands the whole body over without ever yielding, so the deadline
    # can only be exercised by driving the ASGI app directly with a receive() that never delivers.
    monkeypatch.setattr(avatars_api, "TRANSFORM_BODY_TIMEOUT_S", 0.01)
    source = FakeAssets()
    worker = FakeWorker()
    app, _service, _store, _workers = build(tmp_path, monkeypatch, source=source, worker=worker)
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"origin", BASE.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", b"20"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    async with asyncio.timeout(HANG_GUARD_S):
        await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 408
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(body)["error"]["code"] == "intake_timeout"
    assert worker.calls == 0 and source.downloads == 0


async def test_a_body_missing_a_field_is_refused_before_any_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _service, _store, _workers = build(tmp_path, monkeypatch)
    incomplete = request_body()
    del incomplete["rotation"]
    json_headers = {**HEADERS, "content-type": "application/json"}
    async with client(app) as api:
        refused = await api.post(PATH, headers=json_headers, content=json.dumps(incomplete))
        accepted = await api.post(PATH, headers=HEADERS, json=request_body())
    assert refused.status_code == 400 and code(refused) == "invalid_request"
    assert accepted.status_code == 201  # the refused body's exact-key check used no permit


async def test_a_third_concurrent_transform_is_overloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = GatedWorker()
    app, _service, _store, workers = build(tmp_path, monkeypatch, worker=worker)
    async with client(app) as api:
        running = [asyncio.create_task(api.post(PATH, headers=HEADERS, json=request_body()))]
        async with asyncio.timeout(HANG_GUARD_S):
            await worker.entered.wait()
            running.append(
                asyncio.create_task(api.post(PATH, headers=HEADERS, json=request_body()))
            )
            while len(workers) < 2:
                await asyncio.sleep(0)
        third = await api.post(PATH, headers=HEADERS, json=request_body())
        worker.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            finished = await asyncio.gather(*running)

    assert third.status_code == 429 and code(third) == "overloaded"
    assert [response.status_code for response in finished] == [201, 201]
    assert worker.calls == 2 and not workers


async def test_a_malformed_body_is_refused_not_overloaded_while_both_permits_are_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = GatedWorker()
    app, _service, _store, workers = build(tmp_path, monkeypatch, worker=worker)
    incomplete = request_body()
    del incomplete["rotation"]
    json_headers = {**HEADERS, "content-type": "application/json"}
    async with client(app) as api:
        running = [asyncio.create_task(api.post(PATH, headers=HEADERS, json=request_body()))]
        async with asyncio.timeout(HANG_GUARD_S):
            await worker.entered.wait()
            running.append(
                asyncio.create_task(api.post(PATH, headers=HEADERS, json=request_body()))
            )
            while len(workers) < 2:
                await asyncio.sleep(0)
        malformed = [
            await api.post(PATH, headers=json_headers, content=json.dumps(incomplete))
            for _ in range(3)
        ]
        worker.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            finished = await asyncio.gather(*running)
        # Both permits are free now: a refusal that leaked one only when one is free shows here.
        malformed_when_free = [
            await api.post(PATH, headers=json_headers, content=json.dumps(incomplete))
            for _ in range(3)
        ]
        opened = await api.post(PATH, headers=HEADERS, json=request_body())

    assert [(response.status_code, code(response)) for response in malformed] == [
        (400, "invalid_request")
    ] * 3
    assert [(response.status_code, code(response)) for response in malformed_when_free] == [
        (400, "invalid_request")
    ] * 3
    assert [response.status_code for response in finished] == [201, 201]
    assert opened.status_code == 201


async def test_a_source_over_five_mib_is_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeWorker()
    source = FakeAssets(b"x" * (AVATAR_SOURCE_BYTES + 1))
    app, _service, store, _workers = build(tmp_path, monkeypatch, source=source, worker=worker)
    async with client(app) as api:
        response = await api.post(PATH, headers=HEADERS, json=request_body())

    assert response.status_code == 413 and code(response) == "too_large"
    assert worker.calls == 0
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM avatars").fetchone()[0] == 0


class ManualDeadlines:
    """The route's `asyncio`, except that a deadline runs no timer until the test expires it."""

    def __init__(self) -> None:
        self.started: list[tuple[float, asyncio.Timeout]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def timeout(self, delay: float) -> asyncio.Timeout:
        self.started.append((delay, asyncio.timeout(None)))
        return self.started[-1][1]


async def test_the_deadline_cancels_the_transform_once_and_logs_only_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    deadlines = ManualDeadlines()
    monkeypatch.setattr(avatars_api, "asyncio", deadlines)
    worker = GatedWorker()
    app, service, _store, workers = build(tmp_path, monkeypatch, worker=worker)
    with caplog.at_level(logging.DEBUG):
        async with client(app) as api:
            posting = asyncio.create_task(api.post(PATH, headers=HEADERS, json=request_body()))
            async with asyncio.timeout(HANG_GUARD_S):
                await worker.entered.wait()
                [transform] = workers
                # The worker holds its stage now; expire the route's deadline on the next pass.
                [(delay, deadline)] = deadlines.started
                deadline.reschedule(asyncio.get_running_loop().time())
                response = await posting
                # Not gather: for a task that has already ended, gather returns before the
                # task's done callbacks run; wait() lets the route's callback run first.
                await asyncio.wait([transform])

    assert delay == avatars_api.TRANSFORM_TIMEOUT_S
    assert response.status_code == 503 and code(response) == "worker_timeout"
    assert response.headers["cache-control"] == "no-store"
    # The cancelled transform released its permit and left no stage behind.
    assert not workers and list((service.root / "incoming").iterdir()) == []
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.getMessage() for record in warnings] == ["avatar refused: worker_timeout"]
    for secret in (TOKEN, str(tmp_path), tmp_path.name, ".png", "source"):
        assert secret not in caplog.text


class RefusingService:
    def __init__(self, code: str) -> None:
        self.code = code

    async def transform(self, *_args: object) -> Any:
        raise AvatarError(self.code)


@pytest.mark.parametrize(
    ("raised", "status", "answered"),
    [
        ("expired", 404, "not_found"),
        ("missing", 404, "not_found"),
        ("invalid_crop", 400, "invalid_crop"),
        ("source_changed", 409, "source_changed"),
        ("transform_failed", 422, "transform_failed"),
        ("capacity", 429, "capacity"),
        ("protection", 503, "unavailable"),
        ("corrupt", 503, "unavailable"),
        ("decoder_unavailable", 503, "decoder_unavailable"),
    ],
)
async def test_service_codes_map_to_the_typed_answers(
    raised: str, status: int, answered: str
) -> None:
    workers: set[asyncio.Task[Any]] = set()
    routes = avatar_routes_for(cast(AvatarService, RefusingService(raised)), workers)
    async with client(Starlette(routes=routes)) as api:
        response = await api.post(PATH, headers=HEADERS, json=request_body())

    assert (response.status_code, code(response)) == (status, answered)
    assert raised not in response.json()["error"]["message"]


async def test_each_variant_reads_its_stored_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, service, _store, workers = build(tmp_path, monkeypatch)
    avatar_id = str(uuid.uuid4())
    avatar = await service.transform(avatar_id, str(uuid.uuid4()), CROP, 0)
    with caplog.at_level(logging.DEBUG, logger="viki_agent"):
        async with client(app) as api:
            # A same-origin image fetch may send no Origin at all.
            reads = {
                item.size: await api.get(
                    f"{PATH}/{avatar_id}/{item.size}",
                    headers={"authorization": HEADERS["authorization"]},
                )
                for item in avatar.variants
            }

    assert sorted(reads) == [24, 32, 64, 128, 256]
    for item in avatar.variants:
        response = reads[item.size]
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == item.sha256
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-length"] == str(item.byte_size)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
    assert not workers
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


async def test_a_read_refuses_bad_requests_and_a_tampered_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, service, store, _workers = build(tmp_path, monkeypatch)
    avatar_id = str(uuid.uuid4())
    await service.transform(avatar_id, str(uuid.uuid4()), CROP, 0)
    (service.root / "blobs" / avatar_id / "64.png").write_bytes(b"\x89PNG\r\n\x1a\ntampered")
    auth = {"authorization": HEADERS["authorization"]}
    cases: list[tuple[str, dict[str, str], int, str]] = [
        (f"{PATH}/{avatar_id}/64", auth, 503, "unavailable"),
        (f"{PATH}/{avatar_id}/master", auth, 404, "not_found"),
        (f"{PATH}/{avatar_id}/48", auth, 404, "not_found"),
        (f"{PATH}/{avatar_id}/0256", auth, 404, "not_found"),
        (f"{PATH}/{uuid.uuid4()}/64", auth, 404, "not_found"),
        (f"{PATH}/{avatar_id.upper()}/64", auth, 400, "invalid_avatar"),
        (f"{PATH}/{avatar_id}/64?x=1", auth, 400, "invalid_request"),
        (f"{PATH}/{avatar_id}/64", {}, 401, "unauthorized"),
        (
            f"{PATH}/{avatar_id}/64",
            {**auth, "origin": "https://evil.test"},
            403,
            "forbidden_origin",
        ),
    ]
    with caplog.at_level(logging.DEBUG, logger="viki_agent"):
        async with client(app) as api:
            answers = [await api.get(path, headers=headers) for path, headers, _s, _c in cases]

    for response, (_path, _headers, status, expected) in zip(answers, cases, strict=True):
        assert (response.status_code, code(response)) == (status, expected)
        assert response.headers["cache-control"] == "no-store"
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM avatars").fetchone()[0] == 1
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.getMessage() for record in warnings] == ["avatar refused: unavailable"]
    for secret in (TOKEN, str(tmp_path), avatar_id, ".png"):
        assert secret not in caplog.text


class GatedReads:
    """Holds every variant read until the test opens the gate."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def download(self, _avatar_id: str, _size: int) -> AvatarDownload:
        await self.gate.wait()
        return AvatarDownload(3, hashlib.sha256(b"png").hexdigest(), b"png")


async def test_a_read_past_the_permits_waits_then_is_overloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(avatars_api, "READ_WAIT_S", 0.01)
    service = GatedReads()
    workers: set[asyncio.Task[Any]] = set()
    app = Starlette(routes=avatar_routes_for(cast(AvatarService, service), workers))
    path = f"{PATH}/{uuid.uuid4()}/64"
    async with client(app) as api:
        running = [
            asyncio.create_task(api.get(path, headers=HEADERS))
            for _ in range(avatars_api.MAX_READS)
        ]
        async with asyncio.timeout(HANG_GUARD_S):
            while len(workers) < avatars_api.MAX_READS:
                await asyncio.sleep(0)
        refused = await api.get(path, headers=HEADERS)
        service.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            finished = await asyncio.gather(*running)
        after = await api.get(path, headers=HEADERS)

    assert refused.status_code == 429 and code(refused) == "overloaded"
    assert [response.status_code for response in finished] == [200] * avatars_api.MAX_READS
    # Every permit came back when its read ended.
    assert after.status_code == 200 and after.content == b"png" and not workers


def test_the_app_serves_the_avatar_routes_behind_its_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bearer = {"authorization": f"Bearer {APP_TOKEN}"}
    with caplog.at_level(logging.DEBUG), running(tmp_path) as app:
        no_token = app.client.post(PATH, headers={"origin": BASE}, json=request_body())
        foreign = app.client.post(
            PATH, headers={**bearer, "origin": "https://evil.example"}, json=request_body()
        )
        bad_host = app.client.post(
            PATH,
            headers={**bearer, "origin": BASE, "host": "evil.example:8765"},
            json=request_body(),
        )
        unknown = app.client.get(f"{PATH}/{uuid.uuid4()}/64", headers=bearer)

    # TestClient answers with its own response type, so read the code off the JSON here.
    answers = [(r.status_code, r.json()["error"]["code"]) for r in (no_token, foreign, unknown)]
    assert answers == [(401, "unauthorized"), (403, "forbidden_origin"), (404, "not_found")]
    assert bad_host.status_code == 400
    for secret in (APP_TOKEN, str(tmp_path)):
        assert secret not in caplog.text


@pytest.mark.parametrize("present", [True, False])
def test_the_first_retention_pass_collects_avatars_only_once_their_folder_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    present: bool,
) -> None:
    assets_dir = tmp_path / "data" / "assets"
    assets_dir.mkdir(parents=True)
    if present:
        (assets_dir / "avatars").mkdir()
    collected: list[Path] = []
    pass_done = threading.Event()

    async def collect_avatars(self: AvatarService, **_options: object) -> None:
        collected.append(self.root)
        # A failed avatar pass is logged and the rest of the pass still runs.
        raise AvatarError("unavailable")

    async def collect_assets(_self: AssetService, **_options: object) -> None:
        return None

    async def collect_scopes(*_args: object, **_options: object) -> str:
        pass_done.set()
        return ""

    monkeypatch.setattr(AvatarService, "collect_due", collect_avatars)
    monkeypatch.setattr(AssetService, "collect_due", collect_assets)
    monkeypatch.setattr(server, "collect_scopes", collect_scopes)
    with caplog.at_level(logging.WARNING), running(tmp_path):
        assert pass_done.wait(HANG_GUARD_S)

    assert collected == ([assets_dir / "avatars"] if present else [])
    retention = [record.getMessage() for record in caplog.records if "retention" in record.message]
    assert retention == (["avatar retention pass unavailable"] if present else [])
    for secret in (APP_TOKEN, str(tmp_path)):
        assert secret not in caplog.text


def test_the_retention_pass_checks_the_asset_folders_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_dir = tmp_path / "data" / "assets"
    (assets_dir / "avatars").mkdir(parents=True)
    loop_thread: list[int] = []
    checked: dict[Path, int] = {}
    pass_done = threading.Event()
    real_exists = Path.exists

    def recording_exists(self: Path, **options: Any) -> bool:
        if self in (assets_dir, assets_dir / "avatars"):
            checked[self] = threading.get_ident()
        return real_exists(self, **options)

    async def collect_due(_self: object, **_options: object) -> None:
        loop_thread.append(threading.get_ident())

    async def collect_scopes(*_args: object, **_options: object) -> str:
        pass_done.set()
        return ""

    monkeypatch.setattr(Path, "exists", recording_exists)
    monkeypatch.setattr(AvatarService, "collect_due", collect_due)
    monkeypatch.setattr(AssetService, "collect_due", collect_due)
    monkeypatch.setattr(server, "collect_scopes", collect_scopes)
    with running(tmp_path):
        assert pass_done.wait(HANG_GUARD_S)

    assert set(checked) == {assets_dir, assets_dir / "avatars"}
    assert loop_thread and all(ident not in loop_thread for ident in checked.values())


async def test_a_failed_read_says_unavailable_and_a_failed_save_keeps_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service, _store, _workers = build(tmp_path, monkeypatch)
    avatar_id, source_id = str(uuid.uuid4()), str(uuid.uuid4())
    await service.transform(avatar_id, source_id, CROP, 0)
    (service.root / "blobs" / avatar_id / "64.png").write_bytes(b"\x89PNG\r\n\x1a\ntampered")
    body = {"avatar_id": avatar_id, "source_asset_id": source_id, "crop": CROP, "rotation": 0}
    async with client(app) as api:
        read = await api.get(f"{PATH}/{avatar_id}/64", headers=HEADERS)
        saved = await api.post(PATH, headers=HEADERS, json=body)

    for response in (read, saved):
        assert (response.status_code, code(response)) == (503, "unavailable")
    assert read.json()["error"]["message"] == "The avatar is unavailable."
    assert saved.json()["error"]["message"] == "Could not save the avatar."


def _app_with_real_avatars(tmp_path: Path) -> Starlette:
    # Exercises the full app's own drain tuple (server.py, read only here), not the bare
    # `avatar_routes` rig `build()` above returns: the shutdown gap this test pins is in the
    # lifespan the app assembles, and only `create_app` wires `avatar_workers` into it.
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    home = tmp_path / "home"
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    manager = SessionManager(
        config=DriverConfig(cli=("must-not-run",), vault=vault, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("this test never starts a provider"),
    )
    config = Config(
        vault=vault, claude_home=home, cli=Path("absent"), edge=None, data_dir=tmp_path / "data"
    )
    return server.create_app(
        config=config,
        token=APP_TOKEN,
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
    )


async def test_shutdown_drains_a_transform_whose_caller_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app_with_real_avatars(tmp_path)
    entered = asyncio.Event()
    exiting = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()

    async def blocked(
        self: AvatarService, avatar_id: str, source_asset_id: str, crop: object, rotation: int
    ) -> Avatar:
        entered.set()
        # Held like `transform`'s own thread steps (`_run_to_end`): the caller's cancel waits
        # for the thread, which only the test's release below ends.
        try:
            await avatars._run_to_end(release.wait)
        finally:
            finished.set()
        raise AvatarError("unavailable")

    monkeypatch.setattr(AvatarService, "transform", blocked)
    headers = {"authorization": f"Bearer {APP_TOKEN}", "origin": BASE}

    async def lifecycle() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as api:
                caller = asyncio.create_task(api.post(PATH, headers=headers, json=request_body()))
                async with asyncio.timeout(HANG_GUARD_S):
                    await entered.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            exiting.set()

    shutdown = asyncio.create_task(lifecycle())
    try:
        await asyncio.wait_for(exiting.wait(), HANG_GUARD_S)
        done, _pending = await asyncio.wait({shutdown}, timeout=0.05)
        assert not done
    finally:
        release.set()
        await shutdown
    assert finished.is_set()
