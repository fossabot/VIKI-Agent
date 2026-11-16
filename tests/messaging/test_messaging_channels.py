"""Fixed channel composition through synthetic HTTPS and real WSS protocol adapters."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest

from tests.messaging import test_discord_http as discord_api
from tests.messaging import test_discord_transport as discord_socket
from tests.messaging import test_slack_http as slack_api
from tests.messaging import test_slack_transport as slack_socket
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.messaging import discord_transport, slack_transport
from viki_agent.messaging.discord_http import DiscordHttp
from viki_agent.messaging.discord_http import normalize_post as discord_post
from viki_agent.messaging.mattermost_bot import RemoteBot
from viki_agent.messaging.mattermost_format import chat_text, literal_text
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost
from viki_agent.messaging.messaging_runtime import MessagingRuntime
from viki_agent.messaging.slack_http import SlackHttp
from viki_agent.messaging.slack_http import normalize_post as slack_post


async def settle() -> None:
    for _ in range(12):
        turn = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(turn.set_result, None)
        await turn


class Transport(httpx2.MockTransport):
    closes = 0

    def __init__(self, callback: Any) -> None:
        super().__init__(callback)
        self.close_started = asyncio.Event()
        self.close_gate: asyncio.Event | None = None

    async def aclose(self) -> None:
        self.closes += 1
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        await super().aclose()


class Harness:
    def __init__(self, provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.provider = provider
        self.module = importlib.import_module("viki_agent.messaging." + provider + "_channel")
        self.clock = discord_api.Clock()
        self.timer = discord_socket.Scheduler()
        self.server = discord_api.Server() if provider == "discord" else slack_api.Server()
        self.sent = asyncio.Event()

        def request(value: httpx2.Request) -> httpx2.Response:
            response = self.server(value)
            if value.method == "POST" and (
                value.url.path.endswith("/messages") or value.url.path.endswith("/chat.postMessage")
            ):
                self.sent.set()
            return response

        self.transport = Transport(request)
        if provider == "discord":
            self.config, self.binding = discord_api.CONFIG, discord_socket.BINDING
            self.raw = discord_api.post(timestamp="2026-10-13T01:02:04.000123+00:00")
            self.server.bodies[f"/channels/{discord_api.CHANNEL}/messages/{discord_api.POST}"] = (
                self.raw
            )
            self.socket = discord_socket.Socket([discord_socket.HELLO, discord_socket.READY])
            self.connector = discord_socket.Connector(self.socket)
            self.http = DiscordHttp(
                self.config, transport=self.transport, clock=self.clock, sleep=self.clock.sleep
            )
            monkeypatch.setattr(
                self.module,
                "posted",
                partial(
                    discord_transport.posted,
                    connect=self.connector,
                    clock=self.clock,
                    sleep=self.timer.sleep,
                    jitter=lambda: 0.5,
                ),
            )
            self.channel = self.module.DiscordChannel(self.config, self.http)
        else:
            self.config, self.binding = slack_api.CONFIG, slack_api.BINDING
            self.raw = copy.deepcopy(slack_api.EVENT)
            self.socket = discord_socket.Socket([slack_socket.HELLO])
            self.connector = discord_socket.Connector(self.socket)
            self.http = SlackHttp(
                self.config, transport=self.transport, clock=self.clock, sleep=self.clock.sleep
            )
            monkeypatch.setattr(
                self.module, "posted", partial(slack_transport.posted, connect=self.connector)
            )
            self.channel = self.module.SlackChannel(self.config, self.http)
        self.fresh = asyncio.Event()
        self.iterator: Any = None

    @property
    def post(self) -> RemotePost:
        normalize = discord_post if self.provider == "discord" else slack_post
        value = normalize(self.raw, self.binding, discord_api.CUTOFF)
        assert value is not None
        return value

    async def ready(self) -> None:
        assert await self.channel.bind() == self.binding
        assert await self.channel.fresh_cutoff() == discord_api.CUTOFF

    def push(self, *, seq: int = 2) -> None:
        if self.provider == "discord":
            self.socket.put(discord_socket.event(seq, d=self.raw))
        else:
            self.socket.put(slack_socket.envelope(seq, **self.raw))

    async def listen(self) -> asyncio.Task[Any]:
        await self.channel.bind()

        async def fresh() -> int:
            hello = discord_socket.READY if self.provider == "discord" else slack_socket.HELLO
            assert json.dumps(hello) in self.socket.received
            cutoff = await self.channel.fresh_cutoff()
            self.fresh.set()
            return cutoff

        self.iterator = self.channel.receive(fresh_cutoff=fresh)
        pending = asyncio.create_task(anext(self.iterator))
        await asyncio.wait_for(self.fresh.wait(), 0.05)
        await asyncio.wait_for(self.socket.waiting.wait(), 0.05)
        await settle()
        return pending

    def sent_bodies(self) -> list[dict[str, Any]]:
        return [
            json.loads(value.content)
            for value in self.server.requests
            if value.method == "POST"
            and (
                value.url.path.endswith("/messages") or value.url.path.endswith("/chat.postMessage")
            )
        ]


@pytest.fixture(params=("slack", "discord"))
async def bridge(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    harness = Harness(request.param, monkeypatch)
    try:
        yield harness
    finally:
        await harness.channel.close()
        if harness.iterator is not None:
            await harness.iterator.aclose()


async def test_identity_scope_and_cutoff_survive_confirmation(bridge: Harness) -> None:
    await bridge.ready()
    original = bridge.post
    assert bridge.channel.http is bridge.http
    assert bridge.channel.validate(original, discord_api.CUTOFF) is original
    assert await bridge.channel.confirm(original) == original
    before = len(bridge.server.requests)
    old = replace(original, created_ms=discord_api.CUTOFF - 1)
    assert bridge.channel.validate(old, discord_api.CUTOFF) is None
    assert await bridge.channel.confirm(old) is None
    assert len(bridge.server.requests) == before
    scope = (
        (str(int(bridge.binding.scope[0]) + 1),)
        if bridge.provider == "discord"
        else ("T87654321", slack_api.APP)
    )
    other = replace(bridge.binding, scope=scope)
    normalize = discord_post if bridge.provider == "discord" else slack_post
    foreign = normalize(bridge.raw, other, discord_api.CUTOFF)
    assert foreign is not None and foreign.fingerprint != original.fingerprint
    assert await bridge.channel.confirm(foreign) is None
    if bridge.provider == "discord":
        # Millisecond storage cannot reconstruct this exact original microsecond fingerprint.
        bridge.raw["timestamp"] = "2026-10-13T01:02:04.000124+00:00"
    else:
        bridge.server.bodies["/conversations.history"]["messages"][0]["text"] = "changed"
    assert await bridge.channel.confirm(original) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../other"},
        {"message": "\ud800"},
        {"message": "x" * (128 * 1024 + 1)},
        {"root_id": "thread"},
        {"created_ms": True},
        {"created_ms": discord_api.CUTOFF + 1},
        {"fingerprint": "A" * 64},
        {"fingerprint": "f" * 65},
        {"size_bytes": True},
        {"size_bytes": 0},
        {"size_bytes": 128 * 1024 + 1},
        {"size_bytes": 1},
    ],
    ids=(
        "path",
        "surrogate",
        "large",
        "thread",
        "bool-time",
        "wrong-time",
        "upper-hash",
        "long-hash",
        "bool-size",
        "zero-size",
        "large-size",
        "small-size",
    ),
)
async def test_hostile_snapshots_never_reach_confirmation_http(
    bridge: Harness, changes: dict[str, Any]
) -> None:
    await bridge.ready()
    before = len(bridge.server.requests)
    candidate = replace(bridge.post, **changes)
    assert bridge.channel.validate(candidate, discord_api.CUTOFF) is None
    assert await bridge.channel.confirm(candidate) is None
    assert len(bridge.server.requests) == before
    assert bridge.channel.validate(bridge.raw, discord_api.CUTOFF) is None


@pytest.mark.parametrize("field", ["created_ms", "size_bytes"])
async def test_numeric_snapshot_fields_require_integers(bridge: Harness, field: str) -> None:
    await bridge.ready()
    original = bridge.post
    candidate = replace(original, **{field: float(getattr(original, field))})
    assert bridge.channel.validate(candidate, discord_api.CUTOFF) is None
    assert await bridge.channel.confirm(candidate) is None


@pytest.mark.parametrize("identity", ["scope", "bot_user_id", "owner_user_id", "channel_id"])
async def test_revoked_or_changed_binding_cannot_read_or_send(
    bridge: Harness, identity: str
) -> None:
    await bridge.ready()
    original = bridge.post
    value: Any = ("other-scope",) if identity == "scope" else "other"
    bridge.http._binding = replace(bridge.binding, **{identity: value})
    before = len(bridge.server.requests)
    assert bridge.channel.validate(original, discord_api.CUTOFF) is None
    assert await bridge.channel.confirm(original) is None
    with pytest.raises(ValueError, match="post_invalid"):
        await bridge.channel.send("bounded synthetic text", original.id)
    assert len(bridge.server.requests) == before


async def test_literal_parts_are_bounded_top_level_and_sent_once(bridge: Harness) -> None:
    await bridge.ready()
    formatted = literal_text("@everyone <https://example.test> `code` & " + "x" * 8100)
    parts = bridge.channel.render(formatted)
    assert len(parts) > 1
    for part in parts:
        await bridge.channel.send(part, bridge.post.id)
    bodies = bridge.sent_bodies()
    assert len(bodies) == len(parts)
    for body, part in zip(bodies, parts, strict=True):
        assert "thread_ts" not in body and "message_reference" not in body
        assert "@" not in part and "<" not in part and "&" not in part
        if bridge.provider == "slack":
            assert body == {
                "channel": bridge.binding.channel_id,
                "text": part,
                "mrkdwn": True,
                "parse": "none",
                "unfurl_links": False,
                "unfurl_media": False,
            }
            assert len(part) <= 4000
        else:
            assert body == {
                "content": part,
                "allowed_mentions": {"parse": [], "replied_user": False},
                "flags": 4,
                "tts": False,
            }
            assert len(part.encode("utf-16-le")) // 2 <= 2000
    assert all(value.closed for value in bridge.server.streams)


async def test_chat_posts_use_native_provider_format_with_safe_send_flags(bridge: Harness) -> None:
    await bridge.ready()
    parts = bridge.channel.render(chat_text("# Status\n\n- **Ready**\n- `safe code`"))
    assert len(parts) == 1
    await bridge.channel.send(parts[0], bridge.post.id)
    body = bridge.sent_bodies()[-1]
    if bridge.provider == "slack":
        assert body == {
            "channel": bridge.binding.channel_id,
            "text": "*Status*\n\n- *Ready*\n- `safe code`",
            "mrkdwn": True,
            "parse": "none",
            "unfurl_links": False,
            "unfurl_media": False,
        }
    else:
        assert body == {
            "content": "# Status\n\n- **Ready**\n- `safe code`",
            "allowed_mentions": {"parse": [], "replied_user": False},
            "flags": 4,
            "tts": False,
        }


@pytest.mark.parametrize("reply", ["../channel", "1", "123.2", "0", "x" * 100, None, True])
async def test_noncanonical_engine_reply_identity_refused_before_send(
    bridge: Harness, reply: Any
) -> None:
    await bridge.ready()
    before = len(bridge.server.requests)
    # Discord's canonical snowflake 1 is valid; use a leading zero there instead.
    if bridge.provider == "discord" and reply == "1":
        reply = "01"
    with pytest.raises(ValueError, match="post_invalid"):
        await bridge.channel.send("bounded synthetic text", reply)
    assert len(bridge.server.requests) == before


@pytest.mark.parametrize(
    "identity", ["provider", "scope", "bot_user_id", "owner_user_id", "channel_id"]
)
def test_destination_hash_covers_every_bound_identity_and_no_secret(
    bridge: Harness, identity: str
) -> None:
    key = bridge.channel.destination_key(bridge.binding)
    assert len(key) == 64
    assert key == bridge.channel.destination_key(replace(bridge.binding))
    value: Any = ("other-scope",) if identity == "scope" else "other"
    assert key != bridge.channel.destination_key(replace(bridge.binding, **{identity: value}))
    assert bridge.config.bot_token not in key


async def test_authenticated_receive_cutoff_and_normalized_post_reach_engine_boundary(
    bridge: Harness,
) -> None:
    pending = await bridge.listen()
    assert bridge.fresh.is_set() and not pending.done()
    bridge.push()
    candidate = await asyncio.wait_for(pending, 0.05)
    assert candidate == bridge.post
    assert bridge.channel.validate(candidate, discord_api.CUTOFF) == bridge.post
    assert await bridge.channel.confirm(candidate) == candidate
    if bridge.provider == "slack":
        assert bridge.socket.sent[-1] == {"envelope_id": slack_socket.envelope(2)["envelope_id"]}
    else:
        assert bridge.socket.sent[0]["d"]["token"] == bridge.config.bot_token
    await bridge.iterator.aclose()
    assert bridge.socket.closed.is_set() and bridge.transport.closes == 1
    assert not bridge.timer.sleepers


@pytest.mark.parametrize("point", ["waiting", "paused", "caller-paused"])
async def test_close_and_cancellation_release_socket_and_http(bridge: Harness, point: str) -> None:
    if point == "caller-paused":
        await bridge.channel.bind()
        bridge.iterator = bridge.channel.receive(fresh_cutoff=bridge.channel.fresh_cutoff)

        async def push_after_ready() -> None:
            await asyncio.wait_for(bridge.socket.waiting.wait(), 0.05)
            await settle()
            bridge.push()

        producer = asyncio.create_task(push_after_ready())
        assert await asyncio.wait_for(anext(bridge.iterator), 0.05) == bridge.post
        await producer
        await bridge.channel.close()
    else:
        pending = await bridge.listen()
        if point == "paused":
            bridge.push()
            assert await asyncio.wait_for(pending, 0.05) == bridge.post
            await bridge.channel.close()
        else:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    await bridge.channel.close()
    assert bridge.socket.closed.is_set() and bridge.transport.closes == 1
    assert not bridge.timer.sleepers and bridge.socket.receive_count == 0
    assert bridge.channel.validate(bridge.post, discord_api.CUTOFF) is None
    before = len(bridge.server.requests)
    assert await bridge.channel.confirm(bridge.post) is None
    with pytest.raises(ValueError, match="channel_closed"):
        await bridge.channel.bind()
    assert len(bridge.server.requests) == before


async def test_shared_engine_confirms_before_default_mode_actor_prompt(
    bridge: Harness, tmp_path: Path
) -> None:
    from tests.messaging.test_mattermost_bot import Rig

    rig = Rig(tmp_path)
    bot = RemoteBot(bridge.channel, rig.manager, rig.approvals, create_actor=rig.create)
    running = asyncio.create_task(bot.run())
    try:
        await asyncio.wait_for(bridge.sent.wait(), 0.05)
        await settle()
        assert bot.state == "connected" and bot.destination_key() is not None
        bridge.push()
        actor = await asyncio.wait_for(rig.created.get(), 0.05)
        assert actor is not None
        assert await asyncio.wait_for(rig.drivers[-1].sent.get(), 0.05) == bridge.post.message
        confirmations = [
            value
            for value in bridge.server.requests
            if "/messages/" in value.url.path or value.url.path.endswith("/conversations.history")
        ]
        assert len(confirmations) == 1
        assert rig.state.prefs(actor.session_id).mode == "default"
    finally:
        await bot.close()
        await asyncio.wait_for(running, 0.05)
        await rig.approvals.close()
        await rig.manager.close_all()
    assert bridge.socket.closed.is_set() and bridge.transport.closes == 1


@pytest.mark.parametrize("operation", ["bind", "fresh_cutoff", "confirm"])
async def test_close_cannot_adopt_late_http_binding_cutoff_or_confirmation(
    bridge: Harness, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    if operation != "bind":
        await bridge.ready()
    original = getattr(bridge.http, operation)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args: Any) -> Any:
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(bridge.http, operation, held)
    call = getattr(bridge.channel, operation)
    pending = asyncio.create_task(call(bridge.post) if operation == "confirm" else call())
    await asyncio.wait_for(entered.wait(), 0.05)
    await bridge.channel.close()
    release.set()
    if operation == "confirm":
        assert await asyncio.wait_for(pending, 0.05) is None
    else:
        with pytest.raises(ValueError, match="channel_closed"):
            await asyncio.wait_for(pending, 0.05)
    assert bridge.channel.validate(bridge.post, discord_api.CUTOFF) is None
    assert bridge.transport.closes == 1


@pytest.mark.parametrize("identity", ["provider", "owner_user_id"])
async def test_constructor_cannot_adopt_another_provider_or_configured_owner(
    bridge: Harness, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    original = bridge.http.bind

    async def foreign() -> RemoteBinding:
        binding = await original()
        altered = replace(binding, **{identity: "other"})
        bridge.http._binding = altered
        return altered

    monkeypatch.setattr(bridge.http, "bind", foreign)
    with pytest.raises(ValueError, match="channel_closed"):
        await bridge.channel.bind()
    assert bridge.channel.validate(bridge.post, discord_api.CUTOFF) is None


async def test_close_during_authenticated_cutoff_joins_stream_before_http(bridge: Harness) -> None:
    await bridge.channel.bind()
    entered = asyncio.Event()

    async def fresh() -> int:
        entered.set()
        await asyncio.Event().wait()
        return discord_api.CUTOFF

    bridge.iterator = bridge.channel.receive(fresh_cutoff=fresh)
    pending = asyncio.create_task(anext(bridge.iterator))
    await asyncio.wait_for(entered.wait(), 0.05)
    await bridge.channel.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert bridge.socket.closed.is_set() and not bridge.timer.sleepers
    assert bridge.transport.closes == 1


async def test_cancelled_close_retains_one_cleanup_task_and_releases_every_resource(
    bridge: Harness,
) -> None:
    pending = await bridge.listen()
    bridge.push()
    assert await asyncio.wait_for(pending, 0.05) == bridge.post
    bridge.transport.close_gate = asyncio.Event()
    closing = asyncio.create_task(bridge.channel.close())
    await asyncio.wait_for(bridge.transport.close_started.wait(), 0.05)
    assert bridge.socket.closed.is_set()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    bridge.transport.close_gate.set()
    await bridge.channel.close()
    assert bridge.transport.closes == 1 and not bridge.timer.sleepers


async def _hold_cleanup(entered: asyncio.Event, release: asyncio.Event) -> bool:
    """Model a resource that finishes cleanup before acknowledging repeated cancellation."""
    entered.set()
    cancelled = False
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


class HeldConnector(discord_socket.Connector):
    def __init__(self, socket: discord_socket.Socket) -> None:
        super().__init__(socket)
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def __aexit__(self, *args: Any) -> None:
        cancelled = await _hold_cleanup(self.closing, self.release)
        await super().__aexit__(*args)
        if cancelled:
            raise asyncio.CancelledError


class HeldFailureStream(discord_api.Stream):
    def __init__(self) -> None:
        super().__init__(b'{"error":"synthetic failure"}')
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def aclose(self) -> None:
        cancelled = await _hold_cleanup(self.closing, self.release)
        await super().aclose()
        if cancelled:
            raise asyncio.CancelledError


@pytest.mark.parametrize("provider", ["slack", "discord"])
@pytest.mark.parametrize("boundary", ["socket", "http"])
async def test_known_failure_denies_confirmed_allow_before_cleanup_and_preserves_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["slack", "discord"],
    boundary: str,
) -> None:
    from tests.messaging.test_mattermost_bot import Rig

    bridges = {name: Harness(name, monkeypatch) for name in ("slack", "discord")}
    target = bridges[provider]
    sibling = "discord" if provider == "slack" else "slack"
    connector = HeldConnector(target.socket)
    target.connector = connector
    options: dict[str, Any] = {"connect": connector}
    if provider == "discord":
        options.update(clock=target.clock, sleep=target.timer.sleep, jitter=lambda: 0.5)
    posted = discord_transport.posted if provider == "discord" else slack_transport.posted
    monkeypatch.setattr(target.module, "posted", partial(posted, **options))
    rig = Rig(tmp_path)
    bots: dict[str, RemoteBot] = {}

    def factory(name: str) -> Callable[[Callable[[], None]], RemoteBot]:
        def create(on_disarm: Callable[[], None]) -> RemoteBot:
            bot = RemoteBot(
                bridges[name].channel,
                rig.manager,
                rig.approvals,
                create_actor=rig.create,
                on_disarm=on_disarm,
            )
            bots[name] = bot
            return bot

        return create

    runtime = MessagingRuntime(
        slack=factory("slack"),
        discord=factory("discord"),
        cooldown_until=lambda name, _bot: bridges[name].http.cooldown_until,
        clock=target.clock,
    )
    drivers: dict[str, Any] = {}
    actors: dict[str, Any] = {}
    confirmed, confirm_release = asyncio.Event(), asyncio.Event()
    failed_stream = HeldFailureStream()
    try:
        for name in ("slack", "discord"):
            bridge = bridges[name]
            await runtime.set_enabled(name, True)
            await asyncio.wait_for(bridge.sent.wait(), 0.05)
            await settle()
            assert runtime.status()[name]["state"] == "connected"
            bridge.push()
            actors[name] = await asyncio.wait_for(rig.created.get(), 0.05)
            drivers[name] = rig.drivers[-1]
            assert await asyncio.wait_for(drivers[name].sent.get(), 0.05) == bridge.post.message
            drivers[name].ask()
            async with asyncio.timeout(0.05):
                while not any(
                    "!viki allow " in body.get("text", body.get("content", ""))
                    for body in bridge.sent_bodies()
                ):
                    await settle()
        instruction = next(
            body.get("text", body.get("content", ""))
            for body in target.sent_bodies()
            if "!viki allow " in body.get("text", body.get("content", ""))
        )
        nonce = instruction.split("!viki allow ")[1].split()[0]
        original_confirm = target.channel.confirm

        async def held(candidate: RemotePost) -> RemotePost | None:
            result = await original_confirm(candidate)
            assert result == candidate  # Exact provider REST confirmation has already succeeded.
            confirmed.set()
            await confirm_release.wait()
            return result

        monkeypatch.setattr(target.channel, "confirm", held)
        text = f"!viki allow {nonce}"
        if provider == "slack":
            frame = slack_socket.envelope(3, text=text)
            target.server.bodies["/conversations.history"]["messages"] = [
                {
                    key: value
                    for key, value in frame["payload"]["event"].items()
                    if key not in ("channel", "channel_type")
                }
            ]
        else:
            raw = {**target.raw, "id": str(int(discord_api.POST) + 3), "content": text}
            target.server.bodies[f"/channels/{discord_api.CHANNEL}/messages/{raw['id']}"] = raw
            frame = discord_socket.event(3, d=raw)
        target.socket.put(frame)
        await asyncio.wait_for(confirmed.wait(), 0.05)
        if boundary == "socket":
            target.socket.put(
                {"type": "disconnect", "reason": "refresh_requested"}
                if provider == "slack"
                else {"op": 7, "d": None}
            )
            await asyncio.wait_for(connector.closing.wait(), 0.05)
        else:
            original_handler = target.transport.handler

            def fail_send(request: httpx2.Request) -> httpx2.Response:
                if request.method == "POST" and (
                    request.url.path.endswith("/messages")
                    or request.url.path.endswith("/chat.postMessage")
                ):
                    return httpx2.Response(
                        500, headers={"date": slack_api.DATE}, stream=failed_stream
                    )
                response = original_handler(request)
                assert isinstance(response, httpx2.Response)
                return response

            target.transport.handler = fail_send
            assert bots[provider]._enqueue(literal_text("Synthetic status notice"), "")
            await asyncio.wait_for(failed_stream.closing.wait(), 0.05)
        assert not bots[provider]._armed and bots[provider].state == "stopped"
        assert runtime.status()[provider]["state"] == "stopped"
        assert runtime.bridge(provider) is None
        assert not connector.release.is_set() and not failed_stream.release.is_set()
        confirm_release.set()
        assert await asyncio.wait_for(drivers[provider].answers.get(), 0.05) == (
            "card",
            PermissionAnswer(False),
        )
        await settle()
        assert drivers[provider].answers.empty() and drivers[provider].pending() == []
        assert runtime.bridge(sibling) is bots[sibling] and bots[sibling]._armed
        assert runtime.status()[sibling]["state"] == "connected"
        assert rig.manager.get(actors[sibling].session_id) is actors[sibling]
        assert actors[sibling].live and drivers[sibling].pending() == ["card"]
        assert drivers[sibling].answers.empty()
    finally:
        confirm_release.set()
        failed_stream.release.set()
        connector.release.set()
        await runtime.close()
        await rig.approvals.close()
        await rig.manager.close_all()
        for bridge in bridges.values():
            assert bridge.socket.closed.is_set() and bridge.transport.closes == 1
            assert not bridge.timer.sleepers
