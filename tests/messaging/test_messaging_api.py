"""Synthetic messaging status, opt-out and saved settings; no remote account or connection."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.messaging.test_mattermost_bot import CONFIG, HANG_GUARD_S, Rig, take
from tests.messaging.test_mattermost_integration import Integration
from tests.messaging.test_messaging_runtime import Factory, Harness, checkpoint
from tests.messaging.test_messaging_store import CANONICAL, VALUES
from tests.test_server import BEARER, ORIGIN, TOKEN
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.messaging import messaging_api
from viki_agent.messaging.mattermost_bot import RemoteBot
from viki_agent.messaging.messaging_api import (
    MAX_CONFIG_BODY,
    messaging_routes,
    messaging_settings_routes,
)
from viki_agent.messaging.messaging_channel import Provider, StopCause
from viki_agent.messaging.messaging_runtime import (
    BridgeFactory,
    MessagingError,
    MessagingRuntime,
    ProviderStatus,
    _Entry,
)
from viki_agent.messaging.messaging_store import (
    SECRETS,
    Config,
    MessagingConfigStore,
    Saved,
    build,
)
from viki_agent.state import AppState, Prefs

WRITE = {**BEARER, "Origin": ORIGIN}
DISABLE = "/api/v2/messaging/mattermost/disable"


class Bridge(RemoteBot):
    def __init__(self, state: Literal["connecting", "connected", "stopped"]) -> None:
        self._state: Literal["connecting", "connected", "stopped"] = state
        self.config = CONFIG
        self.disarmed = 0

    @property
    def state(self) -> Literal["connecting", "connected", "stopped"]:
        return self._state

    def disarm(self, reason: StopCause | None = None) -> None:
        self.disarmed += 1
        self._state = "stopped"

    async def close(self) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()


def app(
    bridge: RemoteBot | None = None,
    *,
    configured: bool = False,
    close: Callable[[], Awaitable[None]] | None = None,
    workers: set[asyncio.Task[Any]] | None = None,
    runtime: MessagingRuntime | None = None,
    settings: MessagingConfigStore | None = None,
    factory_for: Callable[[Config], BridgeFactory] | None = None,
) -> Starlette:
    class Runtime(MessagingRuntime):
        async def _retire(self, provider: Provider, entry: _Entry) -> None:
            if close is not None:
                await close()
            await super()._retire(provider, entry)

    if runtime is None:
        runtime = Runtime(mattermost=(lambda _: Bridge("connecting")) if configured else None)
        if bridge is not None:
            entry = runtime._entries["mattermost"]
            entry.bot = bridge
            entry.desired = bridge.state != "stopped"
            entry.failed = bridge.state == "stopped"

    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {TOKEN}"

    workers = workers if workers is not None else set()
    routes = messaging_routes(
        runtime=runtime, authorized=authorized, origins=frozenset({ORIGIN}), workers=workers
    )
    if settings is not None:
        routes += messaging_settings_routes(
            runtime=runtime,
            authorized=authorized,
            origins=frozenset({ORIGIN}),
            workers=workers,
            settings=settings,
            factory_for=factory_for if factory_for is not None else Built(),
        )
    return Starlette(routes=routes)


@pytest.mark.parametrize(
    ("configured", "state", "expected", "restart"),
    [
        (False, None, "not_configured", False),
        (True, None, "disabled", False),
        (True, "connecting", "connecting", False),
        (True, "connected", "connected", False),
        (True, "stopped", "stopped", False),
    ],
)
def test_status_is_safe_and_distinguishes_configuration_from_connection(
    configured: bool,
    state: Literal["connecting", "connected", "stopped"] | None,
    expected: str,
    restart: bool,
) -> None:
    with TestClient(app(Bridge(state) if state else None, configured=configured)) as client:
        response = client.get("/api/v2/messaging", headers=BEARER)
    assert response.status_code == 200
    assert response.json() == {
        "mattermost": {
            "configured": configured,
            "state": expected,
            "restart_required": restart,
            "reason": None,
        },
        "slack": {
            "configured": False,
            "state": "not_configured",
            "restart_required": False,
            "reason": None,
        },
        "discord": {
            "configured": False,
            "state": "not_configured",
            "restart_required": False,
            "reason": None,
        },
    }
    assert response.headers["cache-control"] == "no-store"
    assert all(
        value not in response.text for value in (CONFIG.url, CONFIG.owner_user_id, CONFIG.bot_token)
    )


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/api/v2/messaging", {}, 401),
        ("POST", DISABLE, {}, 401),
        ("GET", "/api/v2/messaging", {**BEARER, "Origin": "https://foreign.test"}, 403),
        ("POST", DISABLE, BEARER, 403),
        ("POST", DISABLE, {**BEARER, "Origin": "https://foreign.test"}, 403),
        ("GET", "/api/v2/messaging?q=1", BEARER, 400),
        ("POST", DISABLE + "?q=1", WRITE, 400),
        ("POST", DISABLE, {**WRITE, "Transfer-Encoding": "chunked"}, 400),
        ("POST", DISABLE, {**WRITE, "Content-Encoding": "gzip"}, 400),
    ],
)
def test_boundaries_precede_disarm(
    method: str, path: str, headers: dict[str, str], status: int
) -> None:
    bridge = Bridge("connected")
    with TestClient(app(bridge, configured=True)) as client:
        response = client.request(method, path, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert bridge.disarmed == 0


@pytest.mark.parametrize("declared", [None, "0"])
def test_disable_refuses_actual_body_even_with_zero_content_length(declared: str | None) -> None:
    bridge = Bridge("connected")
    headers = WRITE if declared is None else {**WRITE, "Content-Length": declared}
    with TestClient(app(bridge, configured=True)) as client:
        response = client.post(DISABLE, headers=headers, content=b"{}")
    assert response.status_code == 400
    assert bridge.disarmed == 0


def test_disable_is_idempotent_and_absent_configuration_stays_absent() -> None:
    bridge = Bridge("connected")
    with TestClient(app(bridge, configured=True)) as client:
        first = client.post(DISABLE, headers=WRITE)
        second = client.post(DISABLE, headers=WRITE)
    assert first.status_code == second.status_code == 200
    assert (
        first.json()
        == second.json()
        == {
            "mattermost": {
                "configured": True,
                "state": "disabled",
                "restart_required": False,
                "reason": None,
            },
            "slack": {
                "configured": False,
                "state": "not_configured",
                "restart_required": False,
                "reason": None,
            },
            "discord": {
                "configured": False,
                "state": "not_configured",
                "restart_required": False,
                "reason": None,
            },
        }
    )
    with TestClient(app()) as client:
        absent = client.post(DISABLE, headers=WRITE)
    assert absent.json() == {
        provider: {
            "configured": False,
            "state": "not_configured",
            "restart_required": False,
            "reason": None,
        }
        for provider in ("mattermost", "slack", "discord")
    }


async def test_disarm_survives_caller_cancellation_and_cleanup_failure() -> None:
    bridge = Bridge("connected")
    entered, release = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()

    async def close() -> None:
        assert bridge.state == "stopped"
        entered.set()
        await release.wait()
        raise RuntimeError(CONFIG.bot_token)

    transport = httpx.ASGITransport(app=app(bridge, configured=True, close=close, workers=workers))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        caller = asyncio.create_task(client.post(DISABLE, headers=WRITE))
        async with asyncio.timeout(HANG_GUARD_S):
            await entered.wait()
        assert bridge.state == "stopped" and len(workers) == 1
        worker = next(iter(workers))
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert not worker.done()
        release.set()
        response = await worker
    assert response.status_code == 503
    assert CONFIG.bot_token not in response.body.decode("utf-8")
    assert not workers


async def test_disable_capacity_includes_cleanup_in_flight() -> None:
    bridge = Bridge("connected")
    entered, release = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    calls = 0

    async def close() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
        await release.wait()

    transport = httpx.ASGITransport(app=app(bridge, configured=True, close=close, workers=workers))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        requests = [asyncio.create_task(client.post(DISABLE, headers=WRITE)) for _ in range(4)]
        async with asyncio.timeout(HANG_GUARD_S):
            await entered.wait()
        refused = await client.post(DISABLE, headers=WRITE)
        assert refused.status_code == 429 and len(workers) == 4
        assert bridge.disarmed == 4
        release.set()
        assert [response.status_code for response in await asyncio.gather(*requests)] == [200] * 4
    assert not workers


@pytest.mark.parametrize("disconnected", [False, True])
async def test_incomplete_intake_cannot_disarm(
    monkeypatch: pytest.MonkeyPatch, disconnected: bool
) -> None:
    bridge = Bridge("connected")
    monkeypatch.setattr(messaging_api, "INTAKE_TIMEOUT_S", 0.01)

    async def receive() -> Any:
        if disconnected:
            raise ClientDisconnect
        await asyncio.Event().wait()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": DISABLE,
            "query_string": b"",
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii")) for key, value in WRITE.items()
            ],
        },
        receive=receive,
    )
    route = app(bridge, configured=True).routes[-1]
    assert isinstance(route, Route)
    response = await route.endpoint(request)
    assert response.status_code == (400 if disconnected else 408)
    assert response.headers["cache-control"] == "no-store"
    assert bridge.disarmed == 0


async def test_server_registers_messaging_and_prestart_disable_prevents_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = Integration(tmp_path, monkeypatch)
    local_token = "synthetic-local-token"
    bearer = {"Authorization": f"Bearer {local_token}"}
    transport = httpx.ASGITransport(app=integration.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            before = await client.get("/api/v2/messaging", headers=bearer)
            assert before.json()["mattermost"]["state"] == "disabled"
            disabled = await client.post(DISABLE, headers={**bearer, "Origin": ORIGIN})
            assert disabled.json()["mattermost"] == {
                "configured": True,
                "state": "disabled",
                "restart_required": False,
                "reason": None,
            }
        runtime = integration.app.state.messaging_runtime
        async with integration.app.router.lifespan_context(integration.app):
            assert runtime.bridge("mattermost") is None
            assert integration.http.sent.empty()
    finally:
        await integration.close()


async def test_connected_requires_successful_ready_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    delivered = asyncio.Event()
    original = rig.http.send

    async def send(*args: Any, **kwargs: Any) -> str:
        result = await original(*args, **kwargs)
        delivered.set()
        return result

    monkeypatch.setattr(rig.http, "send", send)
    rig.http.gate.clear()
    try:
        assert rig.bot.state == "connecting"
        await rig.start(monkeypatch)
        assert rig.bot.state == "connecting"
        rig.http.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            await delivered.wait()
        assert rig.bot.state == "connected"
        rig.bot.disarm()
        assert rig.bot.state == "stopped"
    finally:
        await rig.bot.close()
        if rig.run is not None:
            await rig.run
        await rig.approvals.close()
        await rig.manager.close_all()


async def test_disable_denies_late_owned_requests_and_preserves_local_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    await rig.start(monkeypatch)
    actor, driver = await rig.chat()
    sibling = await rig.create()
    await sibling.send("Synthetic local prompt")
    sibling_driver = rig.drivers[-1]
    assert await take(sibling_driver.sent) == "Synthetic local prompt"
    sibling_driver.ask("local-card")

    async def close() -> None:
        entered.set()
        await release.wait()
        await rig.bot.close()
        if rig.run is not None:
            await rig.run

    transport = httpx.ASGITransport(app=app(rig.bot, configured=True, close=close))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            call = asyncio.create_task(client.post(DISABLE, headers=WRITE))
            async with asyncio.timeout(HANG_GUARD_S):
                await entered.wait()
            assert rig.bot.state == "stopped"
            driver.ask("late-card")
            assert await take(driver.answers) == ("late-card", PermissionAnswer(allow=False))
            rig.post("Synthetic late prompt")
            assert driver.sent.empty()
            assert sibling_driver.answers.empty() and sibling_driver.inputs.get("local-card")
            release.set()
            assert (await call).status_code == 200
            assert rig.manager.get(actor.session_id) is None
            assert rig.manager.get(sibling.session_id) is sibling
    finally:
        release.set()
        await rig.bot.close()
        if rig.run is not None:
            await rig.run
        await rig.approvals.close()
        await rig.manager.close_all()


async def test_enable_requires_origin_and_disable_precedes_cleanup(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=app(runtime=harness.runtime, workers=workers))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            refused = await client.post("/api/v2/messaging/slack/enable", headers=BEARER)
            assert refused.status_code == 403
            assert not harness.factories["slack"].channels
            enabled = await client.post("/api/v2/messaging/slack/enable", headers=WRITE)
            assert enabled.status_code == 200
            assert enabled.json()["slack"]["state"] == "connecting"
            channel = harness.factories["slack"].channels[0]
            await take(channel.sent)
            channel.close_release.clear()
            entry = harness.runtime._entries["slack"]
            await entry.lock.acquire()
            disabled = asyncio.create_task(
                client.post("/api/v2/messaging/slack/disable", headers=WRITE)
            )
            try:
                await checkpoint()
                assert harness.runtime.bridge("slack") is None
                assert not channel.close_entered.is_set() and not disabled.done()
            finally:
                entry.lock.release()
            async with asyncio.timeout(0.05):
                await channel.close_entered.wait()
            assert harness.runtime.status()["discord"]["state"] == "disabled"
            channel.close_release.set()
            response = await disabled
            assert response.status_code == 200 and response.json()["slack"]["state"] == "disabled"
            assert response.headers["cache-control"] == "no-store"
            assert not workers
    finally:
        await harness.close()


@pytest.mark.parametrize("provider", ["mattermost", "slack", "discord"])
@pytest.mark.parametrize("action", ["enable", "disable"])
def test_every_fixed_action_enforces_headers_and_empty_body(provider: str, action: str) -> None:
    path = f"/api/v2/messaging/{provider}/{action}"
    with TestClient(app()) as client:
        assert client.post(path).status_code == 401
        assert client.post(path, headers=BEARER).status_code == 403
        assert client.post(path + "?q=1", headers=WRITE).status_code == 400
        response = client.post(path, headers={**WRITE, "Content-Length": "0"}, content=b"x")
        assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
        response = client.post(path, headers=WRITE)
        assert response.status_code == (409 if action == "enable" else 200)


async def test_cooldown_exposes_only_bounded_retry_seconds(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    transport = httpx.ASGITransport(app=app(runtime=harness.runtime))
    try:
        await harness.enable("discord")
        harness.rig.now, harness.cooldown = 10.0, 15.0
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            assert (
                await client.post("/api/v2/messaging/discord/disable", headers=WRITE)
            ).status_code == 200
            response = await client.post("/api/v2/messaging/discord/enable", headers=WRITE)
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "cooldown" and error["retry_after_seconds"] == 5
        assert set(error) == {"code", "message", "retry_after_seconds"}
    finally:
        await harness.close()


@pytest.mark.parametrize("seconds", [None, True, 0, -1, 86401])
async def test_unsafe_cooldown_field_is_not_returned(
    seconds: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = MessagingRuntime()

    async def invalid_cooldown(provider: Provider, enabled: bool) -> Any:
        raise MessagingError("cooldown", retry_after_seconds=seconds)

    monkeypatch.setattr(runtime, "set_enabled", invalid_cooldown)
    transport = httpx.ASGITransport(app=app(runtime=runtime))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        response = await client.post("/api/v2/messaging/slack/enable", headers=WRITE)
    assert response.status_code == 503
    assert "retry_after_seconds" not in response.json()["error"]


def test_unknown_provider_has_no_action_route() -> None:
    with TestClient(app()) as client:
        assert client.post("/api/v2/messaging/telegram/enable", headers=WRITE).status_code == 404


def test_status_refuses_actual_body_even_with_zero_content_length() -> None:
    with TestClient(app()) as client:
        response = client.request(
            "GET", "/api/v2/messaging", headers={**BEARER, "Content-Length": "0"}, content=b"x"
        )
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"


PROVIDERS: tuple[Provider, ...] = ("mattermost", "slack", "discord")
CONFIG_PATH = "/api/v2/messaging/config"
JSON_WRITE = {**WRITE, "Content-Type": "application/json"}
WORKSPACE = str(uuid.uuid4())
CHANGED: dict[Provider, dict[str, str]] = {
    "mattermost": {
        "url": "https://other.example.test",
        "owner_id": "zyxwvutsrqponmlkjihgfedcba",
        "bot_token": "syntheticmattermosttoken02",
    },
    "slack": {
        "bot_token": "xoxb-synthetic-bot-token-2",
        "app_token": "xapp-synthetic-app-token-2",
        "owner_id": "U87654321",
    },
    "discord": {
        "bot_token": "synthetic-discord-token-2",
        "owner_id": "876543210987654321",
        "client_url": "https://client.example.test/other",
    },
}
TOKENS = tuple(
    values[name]
    for table in (VALUES, CHANGED)
    for values in table.values()
    for name in values
    if name in SECRETS
) + (CONFIG.bot_token,)
ABSENT: ProviderStatus = {
    "configured": False,
    "state": "not_configured",
    "restart_required": False,
    "reason": None,
}
DISABLED: ProviderStatus = {
    "configured": True,
    "state": "disabled",
    "restart_required": False,
    "reason": None,
}
CONNECTED: ProviderStatus = {
    "configured": True,
    "state": "connected",
    "restart_required": False,
    "reason": None,
}


class Built:
    """A synthetic factory_for: each bridge it builds records the settings it was built from."""

    def __init__(self) -> None:
        self.configs: list[Config] = []

    def __call__(self, config: Config) -> BridgeFactory:
        def factory(_on_disarm: Callable[[], None]) -> RemoteBot:
            self.configs.append(config)
            return Bridge("connecting")

        return factory


class CountingStore(MessagingConfigStore):
    loads = 0

    def load(self, provider: Provider) -> Saved | None:
        try:
            return super().load(provider)
        finally:
            self.loads += 1


@pytest.fixture
def store(tmp_path: Path) -> CountingStore:
    return CountingStore(AppState(tmp_path / "state.json", Prefs()).store)


def config_path(provider: str) -> str:
    return f"/api/v2/messaging/{provider}/config"


def clean(*texts: str) -> bool:
    return not any(token in text for token in TOKENS for text in texts)


def row(store: MessagingConfigStore, provider: Provider) -> tuple[Any, ...] | None:
    with store.store._connection() as connection:
        found = connection.execute(
            "SELECT body,enabled_workspace,updated_at FROM messaging_config WHERE provider=?",
            (provider,),
        ).fetchone()
    return tuple(found) if found is not None else None


def tamper_discord(store: MessagingConfigStore) -> None:
    """A Slack row's sealed body copied into the Discord row cannot be read as Discord's."""
    store.save("slack", build("slack", VALUES["slack"]))
    with store.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO messaging_config SELECT 'discord',format,body,enabled_workspace,"
            "updated_at FROM messaging_config WHERE provider='slack'"
        )


def running(provider: Provider) -> tuple[MessagingRuntime, Bridge]:
    """All three providers configured; `provider` has a connected bridge."""

    def factory(_on_disarm: Callable[[], None]) -> RemoteBot:
        return Bridge("connecting")

    runtime = MessagingRuntime(mattermost=factory, slack=factory, discord=factory)
    bridge = Bridge("connected")
    entry = runtime._entries[provider]
    entry.bot, entry.desired = bridge, True
    return runtime, bridge


def status_with(provider: Provider, state: ProviderStatus) -> dict[str, ProviderStatus]:
    return {name: state if name == provider else ABSENT for name in PROVIDERS}


def test_config_reads_each_source_and_never_a_token(
    store: CountingStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(app(settings=store)) as client:
        empty = client.get(CONFIG_PATH, headers=BEARER)
        store.save("mattermost", build("mattermost", VALUES["mattermost"]))
        tamper_discord(store)
        filled = client.get(CONFIG_PATH, headers=WRITE)
    assert empty.status_code == filled.status_code == 200
    assert empty.json() == {provider: {"source": "none", "values": {}} for provider in PROVIDERS}
    assert filled.json() == {
        "mattermost": {
            "source": "saved",
            "values": {
                "url": CANONICAL["mattermost"]["url"],
                "owner_id": VALUES["mattermost"]["owner_id"],
            },
        },
        "slack": {"source": "saved", "values": {"owner_id": VALUES["slack"]["owner_id"]}},
        "discord": {"source": "unreadable", "values": {}},
    }
    assert empty.headers["cache-control"] == filled.headers["cache-control"] == "no-store"
    assert [
        record.getMessage() for record in caplog.records if record.name == messaging_api.__name__
    ] == ["messaging settings unavailable (corrupt)"]
    assert clean(empty.text, filled.text, caplog.text)


@pytest.mark.parametrize(
    ("headers", "query", "content", "status"),
    [
        pytest.param({}, "", None, 401, id="no-bearer"),
        pytest.param({**BEARER, "Origin": "https://foreign.test"}, "", None, 403, id="foreign"),
        pytest.param(BEARER, "?q=1", None, 400, id="query"),
        pytest.param({**BEARER, "Content-Length": "0"}, "", b"x", 400, id="body"),
        pytest.param({**BEARER, "Content-Encoding": "gzip"}, "", None, 400, id="encoding"),
    ],
)
def test_config_read_guards(
    store: CountingStore,
    headers: dict[str, str],
    query: str,
    content: bytes | None,
    status: int,
) -> None:
    store.save("slack", build("slack", VALUES["slack"]))
    with TestClient(app(settings=store)) as client:
        response = client.request("GET", CONFIG_PATH + query, headers=headers, content=content)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert "values" not in response.text and clean(response.text)


def body(values: Mapping[str, object]) -> bytes:
    return json.dumps(values).encode("ascii")


MATTERMOST = body(CHANGED["mattermost"])


@pytest.mark.parametrize(
    ("headers", "query", "content", "status", "code"),
    [
        pytest.param(
            {"Content-Type": "application/json"}, "", MATTERMOST, 401, "unauthorized", id="bearer"
        ),
        pytest.param(
            {**BEARER, "Content-Type": "application/json"},
            "",
            MATTERMOST,
            403,
            "forbidden_origin",
            id="no-origin",
        ),
        pytest.param(
            {**JSON_WRITE, "Origin": "https://foreign.test"},
            "",
            MATTERMOST,
            403,
            "forbidden_origin",
            id="foreign-origin",
        ),
        pytest.param(JSON_WRITE, "?q=1", MATTERMOST, 400, "invalid_input", id="query"),
        pytest.param(WRITE, "", MATTERMOST, 400, "invalid_input", id="no-type"),
        pytest.param(
            {**WRITE, "Content-Type": "text/plain"}, "", MATTERMOST, 400, "invalid_input", id="text"
        ),
        pytest.param(
            {**WRITE, "Content-Type": "application/json; charset=latin-1"},
            "",
            MATTERMOST,
            400,
            "invalid_input",
            id="charset",
        ),
        pytest.param(
            {**JSON_WRITE, "Content-Encoding": "gzip"},
            "",
            MATTERMOST,
            400,
            "invalid_input",
            id="encoding",
        ),
        pytest.param(
            {**JSON_WRITE, "Transfer-Encoding": "chunked"},
            "",
            MATTERMOST,
            400,
            "invalid_input",
            id="chunked",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            MATTERMOST + b" " * (MAX_CONFIG_BODY + 1 - len(MATTERMOST)),
            400,
            "invalid_input",
            id="over-16k",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            b'{"url":"https://a.test","url":"https://b.test","owner_id":"'
            + CHANGED["mattermost"]["owner_id"].encode("ascii")
            + b'"}',
            400,
            "invalid_input",
            id="duplicate-key",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            body({**CHANGED["mattermost"], "app_token": "xapp-synthetic-app-token-2"}),
            400,
            "invalid_input",
            id="unknown-key",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            body({**CHANGED["mattermost"], "owner_id": 26}),
            400,
            "invalid_input",
            id="not-a-string",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            body({**CHANGED["mattermost"], "bot_token": None}),
            400,
            "invalid_input",
            id="null-secret",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            body({"owner_id": CHANGED["mattermost"]["owner_id"]}),
            400,
            "invalid_input",
            id="missing-url",
        ),
        pytest.param(
            JSON_WRITE,
            "",
            body({**CHANGED["mattermost"], "bot_token": "t" * 4097}),
            400,
            "invalid_input",
            id="too-long",
        ),
        pytest.param(JSON_WRITE, "", b"[]", 400, "invalid_input", id="array"),
        pytest.param(JSON_WRITE, "", b"{", 400, "invalid_input", id="truncated"),
        pytest.param(JSON_WRITE, "", b"\xff", 400, "invalid_input", id="not-utf8"),
    ],
)
def test_save_refusals_change_nothing(
    store: CountingStore,
    headers: dict[str, str],
    query: str,
    content: bytes,
    status: int,
    code: str,
) -> None:
    runtime, bridge = running("mattermost")
    with TestClient(app(runtime=runtime, settings=store)) as client:
        response = client.put(config_path("mattermost") + query, headers=headers, content=content)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert bridge.disarmed == 0 and runtime.status()["mattermost"] == CONNECTED
    assert row(store, "mattermost") is None and store.loads == 0
    assert clean(response.text)


@pytest.mark.parametrize(
    ("provider", "values", "saved"),
    [
        ("mattermost", {**CHANGED["mattermost"], "url": "http://other.example.test"}, True),
        ("mattermost", {**CHANGED["mattermost"], "owner_id": "short"}, True),
        ("mattermost", {**CHANGED["mattermost"], "bot_token": ""}, True),
        ("slack", {**CHANGED["slack"], "owner_id": "u87654321"}, True),
        ("discord", {**CHANGED["discord"], "owner_id": "0"}, True),
        ("discord", {**CHANGED["discord"], "client_url": "http://client.example.test"}, True),
        ("slack", {"owner_id": CHANGED["slack"]["owner_id"]}, False),
        ("discord", {"owner_id": "1", "client_url": "https://client.example.test"}, False),
    ],
)
def test_invalid_settings_refuse_without_storing_or_stopping(
    store: CountingStore,
    caplog: pytest.LogCaptureFixture,
    provider: Provider,
    values: dict[str, str],
    saved: bool,
) -> None:
    caplog.set_level(logging.DEBUG)
    if saved:
        store.save(provider, build(provider, VALUES[provider]))
    elif provider == "discord":
        tamper_discord(store)  # An unreadable row keeps no token to reuse.
    before = row(store, provider)
    runtime, bridge = running(provider)
    built = Built()
    with TestClient(app(runtime=runtime, settings=store, factory_for=built)) as client:
        response = client.put(config_path(provider), headers=JSON_WRITE, json=values)
    assert response.status_code == 400 and response.json()["error"] == {
        "code": "invalid_config",
        "message": "Check the messaging settings and try again.",
    }
    assert response.headers["cache-control"] == "no-store"
    assert row(store, provider) == before
    assert bridge.disarmed == 0 and runtime.status()[provider] == CONNECTED
    assert clean(response.text, caplog.text) and not built.configs


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_save_applies_off_without_restart_and_enable_builds_from_it(
    store: CountingStore, caplog: pytest.LogCaptureFixture, provider: Provider
) -> None:
    caplog.set_level(logging.DEBUG)
    store.save(provider, build(provider, VALUES[provider]))
    store.remember(provider, WORKSPACE)
    runtime, built = MessagingRuntime(), Built()
    transport = httpx.ASGITransport(app=app(runtime=runtime, settings=store, factory_for=built))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            saved = await client.put(
                config_path(provider), headers=JSON_WRITE, json=CHANGED[provider]
            )
            config = build(provider, CHANGED[provider])
            assert saved.status_code == 200 and saved.json() == status_with(provider, DISABLED)
            assert saved.headers["cache-control"] == "no-store"
            assert store.load(provider) == Saved(config, None) and not built.configs
            enabled = await client.post(f"/api/v2/messaging/{provider}/enable", headers=WRITE)
            assert enabled.status_code == 200 and enabled.json()[provider]["state"] == "connecting"
            assert built.configs == [config]
            read = await client.get(CONFIG_PATH, headers=BEARER)
    finally:
        await runtime.close()
    assert clean(saved.text, enabled.text, read.text, caplog.text)


def test_save_accepts_the_size_and_length_limits(store: CountingStore) -> None:
    values = {**CHANGED["discord"], "bot_token": "t" * 4096}
    content = body(values)
    content += b" " * (MAX_CONFIG_BODY - len(content))
    with TestClient(app(settings=store)) as client:
        response = client.put(
            config_path("discord"),
            headers={**WRITE, "Content-Type": 'application/json; charset="UTF-8"'},
            content=content,
        )
    assert response.status_code == 200 and response.json()["discord"] == DISABLED
    assert store.load("discord") == Saved(build("discord", values), None)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("x" * 16, id="x16"),
        pytest.param("A" * 26, id="uppercase"),
        pytest.param("a" * 25, id="short"),
        pytest.param("a" * 27, id="long"),
        pytest.param("a" * 25 + "-", id="dash"),
    ],
)
def test_save_refuses_a_token_mattermost_cannot_have(
    store: CountingStore, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    caplog.set_level(logging.DEBUG)
    store.save("mattermost", build("mattermost", VALUES["mattermost"]))
    before = row(store, "mattermost")
    runtime, bridge = running("mattermost")
    built = Built()
    with TestClient(app(runtime=runtime, settings=store, factory_for=built)) as client:
        response = client.put(
            config_path("mattermost"),
            headers=JSON_WRITE,
            json={**CHANGED["mattermost"], "bot_token": token},
        )
    assert response.status_code == 400 and response.json()["error"] == {
        "code": "invalid_token",
        "message": "Check the bot token and try again.",
    }
    assert response.headers["cache-control"] == "no-store"
    assert row(store, "mattermost") == before
    assert bridge.disarmed == 0 and runtime.status()["mattermost"] == CONNECTED
    assert clean(response.text, caplog.text) and not built.configs
    assert token not in response.text and token not in caplog.text


@pytest.mark.parametrize(
    ("field", "token", "code", "message"),
    [
        pytest.param(
            "bot_token",
            "xapp-synthetic",
            "invalid_token",
            "Check the bot token and try again.",
            id="bot-xapp-prefix",
        ),
        pytest.param(
            "bot_token",
            "synthetic-bot-token",
            "invalid_token",
            "Check the bot token and try again.",
            id="bot-no-prefix",
        ),
        pytest.param(
            "bot_token",
            "xoxb-",
            "invalid_token",
            "Check the bot token and try again.",
            id="bot-prefix-only",
        ),
        pytest.param(
            "app_token",
            "xoxb-synthetic",
            "invalid_app_token",
            "Check the app token and try again.",
            id="app-xoxb-prefix",
        ),
        pytest.param(
            "app_token",
            "xapp-",
            "invalid_app_token",
            "Check the app token and try again.",
            id="app-prefix-only",
        ),
    ],
)
def test_save_names_the_slack_token_that_cannot_be_one(
    store: CountingStore,
    caplog: pytest.LogCaptureFixture,
    field: str,
    token: str,
    code: str,
    message: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    store.save("slack", build("slack", VALUES["slack"]))
    before = row(store, "slack")
    runtime, bridge = running("slack")
    built = Built()
    with TestClient(app(runtime=runtime, settings=store, factory_for=built)) as client:
        response = client.put(
            config_path("slack"), headers=JSON_WRITE, json={**CHANGED["slack"], field: token}
        )
    assert response.status_code == 400 and response.json()["error"] == {
        "code": code,
        "message": message,
    }
    assert response.headers["cache-control"] == "no-store"
    assert row(store, "slack") == before
    assert bridge.disarmed == 0 and runtime.status()["slack"] == CONNECTED
    assert clean(response.text, caplog.text) and not built.configs
    assert token not in response.text and token not in caplog.text


@pytest.mark.parametrize(
    ("provider", "omitted"),
    [
        ("discord", ("bot_token",)),  # A new client URL only names the bot; it receives nothing.
        ("slack", ("bot_token",)),
        ("slack", ("app_token",)),
        ("slack", ("bot_token", "app_token")),
    ],
)
def test_omitted_secret_keeps_the_saved_token(
    store: CountingStore, provider: Provider, omitted: tuple[str, ...]
) -> None:
    store.save(provider, build(provider, VALUES[provider]))
    store.remember(provider, WORKSPACE)
    sent = {name: value for name, value in CHANGED[provider].items() if name not in omitted}
    with TestClient(app(settings=store)) as client:
        response = client.put(config_path(provider), headers=JSON_WRITE, json=sent)
    assert response.status_code == 200 and response.json()[provider] == DISABLED
    kept = {**CHANGED[provider], **{name: VALUES[provider][name] for name in omitted}}
    assert store.load(provider) == Saved(build(provider, kept), None)
    assert clean(response.text)


MATTERMOST_KEPT = {"url": VALUES["mattermost"]["url"], "owner_id": VALUES["mattermost"]["owner_id"]}


@pytest.mark.parametrize(
    ("provider", "winner", "late"),
    [
        pytest.param("slack", CHANGED["slack"], {"owner_id": "U99999999"}, id="new-token"),
        pytest.param("slack", None, {"owner_id": "U99999999"}, id="removed"),
        pytest.param(
            "mattermost",
            {**CHANGED["mattermost"], "bot_token": VALUES["mattermost"]["bot_token"]},
            MATTERMOST_KEPT,
            id="same-token-new-server",
        ),
    ],
)
async def test_save_never_restores_a_token_replaced_while_it_waited(
    store: CountingStore,
    provider: Provider,
    winner: dict[str, str] | None,
    late: dict[str, str],
) -> None:
    store.save(provider, build(provider, VALUES[provider]))
    entered, release = asyncio.Event(), asyncio.Event()

    async def close() -> None:
        if not entered.is_set():
            entered.set()
            await release.wait()

    transport = httpx.ASGITransport(app=app(close=close, settings=store))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        first = asyncio.create_task(
            client.put(config_path(provider), headers=JSON_WRITE, json=winner)
            if winner is not None
            else client.delete(config_path(provider), headers=WRITE)
        )
        try:
            async with asyncio.timeout(HANG_GUARD_S):
                await entered.wait()
            loads = store.loads
            second = asyncio.create_task(
                client.put(config_path(provider), headers=JSON_WRITE, json=late)
            )
            async with asyncio.timeout(HANG_GUARD_S):
                while store.loads == loads:  # The late Save read the old row before the winner.
                    await asyncio.sleep(0.001)
        finally:
            release.set()
        assert (await first).status_code == 200
        refused = await second
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "unavailable"
    expected = Saved(build(provider, winner), None) if winner is not None else None
    assert store.load(provider) == expected
    assert clean(refused.text)


@pytest.mark.parametrize(
    ("url", "token", "status"),
    [
        pytest.param("https://elsewhere.attacker.test", None, 400, id="other-host"),
        pytest.param("https://chat.example.test:443/other", None, 400, id="other-path"),
        pytest.param("https://chat.example.test:8443/team", None, 400, id="other-port"),
        pytest.param(VALUES["mattermost"]["url"], None, 200, id="same"),
        pytest.param(CANONICAL["mattermost"]["url"], None, 200, id="canonically-same"),
        pytest.param(
            "https://elsewhere.attacker.test",
            CHANGED["mattermost"]["bot_token"],
            200,
            id="new-token",
        ),
    ],
)
def test_kept_token_stays_with_its_server(
    store: CountingStore,
    caplog: pytest.LogCaptureFixture,
    url: str,
    token: str | None,
    status: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    store.save("mattermost", build("mattermost", VALUES["mattermost"]))
    before = row(store, "mattermost")
    runtime, bridge = running("mattermost")
    sent = {**MATTERMOST_KEPT, "url": url} | ({"bot_token": token} if token is not None else {})
    built = Built()
    with TestClient(app(runtime=runtime, settings=store, factory_for=built)) as client:
        response = client.put(config_path("mattermost"), headers=JSON_WRITE, json=sent)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    if status == 400:
        assert response.json()["error"]["code"] == "invalid_config" and not built.configs
        assert row(store, "mattermost") == before
        assert bridge.disarmed == 0 and runtime.status()["mattermost"] == CONNECTED
    else:
        saved = {"bot_token": token or VALUES["mattermost"]["bot_token"], **sent}
        assert store.load("mattermost") == Saved(build("mattermost", saved), None)
        assert bridge.disarmed == 1 and response.json()["mattermost"] == DISABLED
    assert clean(response.text, caplog.text)


def test_saved_malformed_token_still_loads_and_can_be_kept(store: CountingStore) -> None:
    config = build("mattermost", {**VALUES["mattermost"], "bot_token": "x" * 16})
    store.save("mattermost", config)
    assert store.load("mattermost") == Saved(config, None)
    with TestClient(app(settings=store)) as client:
        read = client.get(CONFIG_PATH, headers=BEARER)
        assert read.status_code == 200 and read.json()["mattermost"] == {
            "source": "saved",
            "values": {
                "url": CANONICAL["mattermost"]["url"],
                "owner_id": VALUES["mattermost"]["owner_id"],
            },
        }
        sent = {**MATTERMOST_KEPT, "owner_id": CHANGED["mattermost"]["owner_id"]}
        response = client.put(config_path("mattermost"), headers=JSON_WRITE, json=sent)
    assert response.status_code == 200 and response.json()["mattermost"] == DISABLED
    assert store.load("mattermost") == Saved(
        build("mattermost", {**sent, "bot_token": "x" * 16}), None
    )


async def test_save_while_connected_closes_bridge_first_then_enable_uses_new_settings(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    store = MessagingConfigStore(harness.rig.state.store)
    made: list[tuple[Config, Factory]] = []

    def factory_for(config: Config) -> BridgeFactory:
        factory = Factory(harness.rig, "slack")
        made.append((config, factory))
        return factory

    transport = httpx.ASGITransport(
        app=app(runtime=harness.runtime, settings=store, factory_for=factory_for)
    )
    try:
        old = await harness.enable("slack")
        old.close_release.clear()
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            put = asyncio.create_task(
                client.put(config_path("slack"), headers=JSON_WRITE, json=VALUES["slack"])
            )
            async with asyncio.timeout(HANG_GUARD_S):
                await old.close_entered.wait()
            assert harness.runtime.bridge("slack") is None
            assert harness.runtime.status()["slack"] == DISABLED
            assert store.load("slack") is None and not put.done()
            old.close_release.set()
            saved = await put
            assert saved.status_code == 200 and saved.json()["slack"] == DISABLED
            assert old.closed and store.load("slack") == Saved(
                build("slack", VALUES["slack"]), None
            )
            enabled = await client.post("/api/v2/messaging/slack/enable", headers=WRITE)
            assert enabled.status_code == 200 and enabled.json()["slack"]["state"] == "connecting"
            [(config, factory)] = made
            assert config == build("slack", VALUES["slack"])
            assert len(factory.channels) == 1 and len(harness.factories["slack"].channels) == 1
            await take(factory.channels[0].sent)
    finally:
        await harness.close()


def test_remove_stops_the_bridge_and_forgets_settings(
    store: CountingStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store.save("mattermost", build("mattermost", VALUES["mattermost"]))
    store.save("discord", build("discord", VALUES["discord"]))
    runtime, bridge = running("mattermost")
    with TestClient(app(runtime=runtime, settings=store)) as client:
        removed = client.delete(config_path("mattermost"), headers=WRITE)
        again = client.delete(config_path("mattermost"), headers=WRITE)
        read = client.get(CONFIG_PATH, headers=BEARER)
        enable = client.post("/api/v2/messaging/mattermost/enable", headers=WRITE)
    assert removed.status_code == again.status_code == 200
    assert (
        removed.json()
        == again.json()
        == {**status_with("mattermost", ABSENT), "slack": DISABLED, "discord": DISABLED}
    )
    assert removed.headers["cache-control"] == "no-store"
    assert bridge.disarmed == 1 and bridge.state == "stopped"
    assert store.load("mattermost") is None and store.load("discord") is not None
    assert read.json()["mattermost"] == {"source": "none", "values": {}}
    assert enable.status_code == 409 and enable.json()["error"]["code"] == "not_configured"
    assert clean(removed.text, read.text, caplog.text)


@pytest.mark.parametrize(
    ("headers", "query", "content", "status"),
    [
        pytest.param({}, "", None, 401, id="no-bearer"),
        pytest.param(BEARER, "", None, 403, id="no-origin"),
        pytest.param({**BEARER, "Origin": "https://foreign.test"}, "", None, 403, id="foreign"),
        pytest.param(WRITE, "?q=1", None, 400, id="query"),
        pytest.param({**WRITE, "Content-Length": "0"}, "", b"x", 400, id="body"),
        pytest.param(JSON_WRITE, "", b"{}", 400, id="json-body"),
        pytest.param({**WRITE, "Transfer-Encoding": "chunked"}, "", None, 400, id="chunked"),
        pytest.param({**WRITE, "Content-Encoding": "gzip"}, "", None, 400, id="encoding"),
    ],
)
def test_remove_guards_precede_any_change(
    store: CountingStore,
    headers: dict[str, str],
    query: str,
    content: bytes | None,
    status: int,
) -> None:
    store.save("mattermost", build("mattermost", VALUES["mattermost"]))
    runtime, bridge = running("mattermost")
    with TestClient(app(runtime=runtime, settings=store)) as client:
        response = client.request(
            "DELETE", config_path("mattermost") + query, headers=headers, content=content
        )
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert bridge.disarmed == 0 and runtime.status()["mattermost"] == CONNECTED
    assert store.load("mattermost") is not None


def test_settings_routes_are_fixed(store: CountingStore) -> None:
    with TestClient(app(settings=store)) as client:
        assert client.put(config_path("telegram"), headers=JSON_WRITE, json={}).status_code == 404
        assert client.post(config_path("slack"), headers=JSON_WRITE, json={}).status_code == 405
        assert client.post(CONFIG_PATH, headers=WRITE).status_code == 405


async def test_settings_capacity_is_separate_and_includes_cleanup(store: CountingStore) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()

    async def close() -> None:
        entered.set()
        await release.wait()

    transport = httpx.ASGITransport(app=app(close=close, workers=workers, settings=store))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        removals = [
            asyncio.create_task(client.delete(config_path(provider), headers=WRITE))
            for provider in (*PROVIDERS, "slack")
        ]
        try:
            async with asyncio.timeout(HANG_GUARD_S):
                await entered.wait()
                while len(workers) < 4:
                    await checkpoint()
                refused = await client.delete(config_path("discord"), headers=WRITE)
            assert refused.status_code == 429 and refused.json()["error"]["code"] == "overloaded"
            assert refused.headers["cache-control"] == "no-store"
            disable = asyncio.create_task(client.post(DISABLE, headers=WRITE))
            async with asyncio.timeout(HANG_GUARD_S):
                while len(workers) < 5:  # The action routes keep their own slots.
                    await checkpoint()
        finally:
            release.set()
        responses = await asyncio.gather(*removals, disable)
    assert [response.status_code for response in responses] == [200] * 5
    assert not workers


async def test_save_survives_caller_cancellation(store: CountingStore) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()

    async def close() -> None:
        entered.set()
        await release.wait()

    transport = httpx.ASGITransport(app=app(close=close, workers=workers, settings=store))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        caller = asyncio.create_task(
            client.put(config_path("discord"), headers=JSON_WRITE, json=VALUES["discord"])
        )
        async with asyncio.timeout(HANG_GUARD_S):
            await entered.wait()
        assert len(workers) == 1
        worker = next(iter(workers))
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert not worker.done()
        release.set()
        response = await worker
    assert response.status_code == 200
    assert store.load("discord") == Saved(build("discord", VALUES["discord"]), None)
    assert not workers


@pytest.mark.parametrize(
    ("intake", "status"),
    [
        ("complete", 200),
        ("oversized", 400),
        ("timeout", 408),
        ("disconnected", 400),
        ("declared-over", 400),
        ("declared-twice", 400),
        ("undeclared", 400),
    ],
)
async def test_save_intake_is_bounded(
    store: CountingStore, monkeypatch: pytest.MonkeyPatch, intake: str, status: int
) -> None:
    monkeypatch.setattr(messaging_api, "INTAKE_TIMEOUT_S", 0.01)
    chunks = [b" " * MAX_CONFIG_BODY, MATTERMOST] if intake == "oversized" else [MATTERMOST]
    declared = str(len(MATTERMOST))
    lengths = {
        "declared-over": [str(MAX_CONFIG_BODY + 1)],
        "declared-twice": [declared, declared],
        "undeclared": [],
    }.get(intake, [declared])

    async def receive() -> Any:
        if intake == "disconnected":
            raise ClientDisconnect
        if intake == "timeout" or not chunks:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": config_path("mattermost"),
            "query_string": b"",
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii"))
                for key, value in JSON_WRITE.items()
            ]
            + [(b"content-length", length.encode("ascii")) for length in lengths],
        },
        receive=receive,
    )
    runtime, bridge = running("mattermost")
    [route] = [
        route
        for route in app(runtime=runtime, settings=store).routes
        if isinstance(route, Route) and route.path == config_path("mattermost")
    ]
    response = await route.endpoint(request)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    if intake == "complete":
        assert bridge.disarmed == 1
        assert store.load("mattermost") == Saved(build("mattermost", CHANGED["mattermost"]), None)
    else:
        assert bridge.disarmed == 0 and row(store, "mattermost") is None
