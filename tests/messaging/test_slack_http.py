"""Slack owner-DM API compatibility against synthetic HTTPS transports only."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from viki_agent.messaging.mattermost_bot import RemoteBot
from viki_agent.messaging.messaging_channel import RemoteBinding
from viki_agent.messaging.messaging_config import SlackConfig
from viki_agent.messaging.messaging_runtime import MessagingRuntime
from viki_agent.messaging.slack_channel import SlackChannel

TEAM, APP, BOT, BOT_USER, OWNER, DM = (
    "T12345678",
    "A12345678",
    "B12345678",
    "U12345678",
    "U87654321",
    "D12345678",
)
CONFIG = SlackConfig("xoxb-synthetic-bot", "xapp-synthetic-app", OWNER)
DATE = "Tue, 13 Oct 2026 01:02:03 GMT"
CUTOFF = 1791853324000
TS = "1791853324.000123"
BINDING = RemoteBinding("slack", (TEAM, APP), BOT_USER, OWNER, DM)
EVENT = {
    "type": "message",
    "channel_type": "im",
    "channel": DM,
    "user": OWNER,
    "text": "Synthetic text",
    "ts": TS,
}


class Stream(httpx2.AsyncByteStream):
    closed = False

    def __init__(self, body: Any) -> None:
        self.raw = json.dumps(body).encode()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.raw

    async def aclose(self) -> None:
        self.closed = True


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.streams: list[Stream] = []
        self.headers = {"date": DATE}
        channel = {"id": DM, "is_im": True, "is_org_shared": False, "user": OWNER}
        self.bodies: dict[str, Any] = {
            "/auth.test": {"ok": True, "team_id": TEAM, "user_id": BOT_USER, "bot_id": BOT},
            "/bots.info": {
                "ok": True,
                "bot": {"id": BOT, "user_id": BOT_USER, "app_id": APP, "deleted": False},
            },
            "/users.info": {
                "ok": True,
                "user": {"id": BOT_USER, "team_id": TEAM, "is_bot": True, "deleted": False},
            },
            "/owner": {
                "ok": True,
                "user": {
                    "id": OWNER,
                    "team_id": TEAM,
                    "is_bot": False,
                    "is_app_user": False,
                    "deleted": False,
                },
            },
            "/conversations.open": {"ok": True, "channel": deepcopy(channel)},
            "/conversations.info": {"ok": True, "channel": deepcopy(channel)},
            "/apps.connections.open": {
                "ok": True,
                "url": "wss://wss-primary.slack.com/link/?ticket=synthetic&app_id=123",
            },
            "/conversations.history": {
                "ok": True,
                "messages": [
                    {
                        key: value
                        for key, value in EVENT.items()
                        if key not in ("channel", "channel_type")
                    }
                ],
            },
        }
        self.override: httpx2.Response | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api")
        assert request.url.host == "slack.com" and request.url.scheme == "https"
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
        expected = CONFIG.app_token if path == "/apps.connections.open" else CONFIG.bot_token
        assert request.headers["authorization"] == "Bearer " + expected
        if self.override is not None:
            return self.override
        if path == "/users.info" and request.url.params["user"] == OWNER:
            path = "/owner"
        if path == "/chat.postMessage":
            sent = json.loads(request.content)
            body = {
                "ok": True,
                "channel": DM,
                "ts": TS,
                "message": {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": BOT,
                    "user": BOT_USER,
                    "text": sent["text"],
                    "ts": TS,
                },
            }
            body.update(self.bodies.get(path, {}))
        else:
            body = self.bodies[path]
        stream = Stream(body)
        self.streams.append(stream)
        return httpx2.Response(200, headers=self.headers, stream=stream)


@pytest.fixture
def api() -> Any:
    return importlib.import_module("viki_agent.messaging.slack_http")


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
async def http(api: Any, server: Server) -> AsyncIterator[Any]:
    instance = api.SlackHttp(CONFIG, transport=httpx2.MockTransport(server))
    try:
        yield instance
    finally:
        await instance.close()


async def ready(http: Any) -> None:
    assert await http.bind() == BINDING
    assert await http.fresh_cutoff() == CUTOFF


async def test_exact_identity_binding_and_app_token_isolation(http: Any, server: Server) -> None:
    await ready(http)
    assert http.binding == BINDING
    assert await http.socket_url() == server.bodies["/apps.connections.open"]["url"]
    assert await http.fresh_cutoff() == CUTOFF
    assert json.loads(server.requests[4].content) == {"users": OWNER, "return_im": True}
    assert [request.url.path for request in server.requests[:6]] == [
        "/api/auth.test",
        "/api/bots.info",
        "/api/users.info",
        "/api/users.info",
        "/api/conversations.open",
        "/api/conversations.info",
    ]
    assert all(stream.closed for stream in server.streams)


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ("/owner", "team_id", "T87654321"),
        ("/conversations.open", "is_mpim", True),
        ("/conversations.info", "is_im", False),
    ],
)
async def test_foreign_team_or_mpim_never_binds(
    http: Any, server: Server, api: Any, path: str, field: str, value: Any
) -> None:
    server.bodies[path]["user" if path == "/owner" else "channel"][field] = value
    with pytest.raises(api.SlackHttpError, match="^slack_binding_failed$"):
        await http.bind()
    assert http.binding is None


@pytest.mark.parametrize(
    ("path", "container", "field", "value"),
    [
        ("/auth.test", None, "bot_id", "bad"),
        ("/auth.test", None, "team_id", "bad"),
        ("/bots.info", "bot", "id", "B87654321"),
        ("/bots.info", "bot", "user_id", OWNER),
        ("/bots.info", "bot", "app_id", "bad"),
        ("/bots.info", "bot", "deleted", True),
        ("/users.info", "user", "is_bot", False),
        ("/users.info", "user", "deleted", 0),
        ("/users.info", "user", "team_id", "T87654321"),
        ("/owner", "user", "id", BOT_USER),
        ("/owner", "user", "is_bot", True),
        ("/owner", "user", "is_app_user", True),
        ("/owner", "user", "deleted", True),
        ("/conversations.info", "channel", "id", "D87654321"),
        ("/conversations.info", "channel", "user", BOT_USER),
        ("/conversations.info", "channel", "is_private", False),
        ("/conversations.info", "channel", "is_org_shared", True),
        ("/conversations.info", "channel", "is_ext_shared", True),
        ("/conversations.info", "channel", "shared_team_ids", [TEAM, "T87654321"]),
    ],
)
async def test_binding_identity_matrix(
    http: Any, server: Server, api: Any, path: str, container: str | None, field: str, value: Any
) -> None:
    target = server.bodies[path][container] if container else server.bodies[path]
    target[field] = value
    with pytest.raises(api.SlackHttpError):
        await http.bind()
    count = len(server.requests)
    with pytest.raises(api.SlackHttpError):
        await http.fresh_cutoff()
    assert len(server.requests) == count


@pytest.mark.parametrize(
    "date", [None, "bad", "Mon, 13 Oct 2026 01:02:03 GMT", "Tue, 13 Oct 2026 01:02:02 GMT"]
)
async def test_fresh_date_is_validated_and_monotonic(
    http: Any, server: Server, api: Any, date: str | None
) -> None:
    await ready(http)
    server.headers = {} if date is None else {"date": date}
    with pytest.raises(api.SlackHttpError, match="^slack_freshness_failed$"):
        await http.fresh_cutoff()
    count = len(server.requests)
    with pytest.raises(api.SlackHttpError):
        await http.fresh_cutoff()
    assert len(server.requests) == count


@pytest.mark.parametrize(
    "url",
    [
        "ws://wss.slack.com/link/?ticket=x",
        "wss://evil.test/link/?ticket=x",
        "wss://wss.slack.com.evil.test/link/?ticket=x",
        "wss://user" + "@" + "wss.slack.com/link/?ticket=x",
        "wss://wss.slack.com:444/link/?ticket=x",
        "wss://wss.slack.com/link/?ticket=x#frag",
        "wss://wss.slack.com/other?ticket=x",
        "wss://wss.slack.com/link/",
        "wss://wss.slack.com/link/?ticket=%ZZ",
        "wss://wss-slack.com/link/?ticket=x",
        "wss://wss.slack.com/link/?ticket=" + "x" * 4097,
    ],
)
async def test_socket_url_attacks_are_fixed_refusals(
    http: Any, server: Server, api: Any, url: str
) -> None:
    await ready(http)
    server.bodies["/apps.connections.open"]["url"] = url
    with pytest.raises(api.SlackHttpError, match="^slack_socket_invalid$") as error:
        await http.socket_url()
    assert error.value.__context__ is None and url not in repr(error.value)


def test_normalization_keeps_exact_timestamp_and_ignores_rich_text(api: Any) -> None:
    event = {
        **EVENT,
        "blocks": [{"type": "rich_text", "elements": [{"unused": "private"}]}],
        "unrelated": "unused",
    }
    post = api.normalize_post(event, BINDING, CUTOFF)
    assert post == api.normalize_post(EVENT, BINDING, CUTOFF)
    assert post.id == TS and post.created_ms == CUTOFF and post.root_id == ""
    assert len(post.fingerprint) == hashlib.sha256().digest_size * 2
    assert post != api.normalize_post(EVENT, replace(BINDING, scope=(TEAM, "A87654321")), CUTOFF)
    assert post != api.normalize_post({**EVENT, "text": "Other"}, BINDING, CUTOFF)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "message_changed"),
        ("channel_type", "mpim"),
        ("channel", "D87654321"),
        ("user", BOT_USER),
        ("subtype", "bot_message"),
        ("bot_id", BOT),
        ("edited", {}),
        ("thread_ts", TS),
        ("files", []),
        ("attachments", []),
        ("text", "\ud800"),
        ("text", "x" * (128 * 1024)),
        ("ts", "1791853323.999999"),
        ("ts", "1e10"),
        ("blocks", [{"type": "actions"}]),
    ],
    ids=lambda value: value[:40] if isinstance(value, str) else None,
)
def test_candidate_admission_matrix(api: Any, field: str, value: Any) -> None:
    assert api.normalize_post({**EVENT, field: value}, BINDING, CUTOFF) is None


async def test_confirmation_is_one_exact_live_window(http: Any, server: Server) -> None:
    await ready(http)
    candidate = http.validate(EVENT, CUTOFF)
    assert candidate is not None and await http.confirm(candidate) == candidate
    request = server.requests[-1]
    assert request.method == "GET" and dict(request.url.params) == {
        "channel": DM,
        "oldest": TS,
        "latest": TS,
        "inclusive": "true",
        "limit": "1",
    }
    count = len(server.requests)
    assert await http.confirm(replace(candidate, id="bad")) is None
    assert len(server.requests) == count


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ts", "1791853324.000124"),
        ("user", BOT_USER),
        ("text", "Changed"),
        ("edited", {}),
        ("attachments", []),
        ("thread_ts", TS),
        ("channel", "D87654321"),
    ],
)
async def test_exact_confirmation_rejects_mutation(
    http: Any, server: Server, field: str, value: Any
) -> None:
    await ready(http)
    candidate = http.validate(EVENT, CUTOFF)
    server.bodies["/conversations.history"]["messages"][0][field] = value
    assert await http.confirm(candidate) is None


@pytest.mark.parametrize("messages", [[], [EVENT, EVENT]])
async def test_confirmation_refuses_missing_or_multiple_without_paging(
    http: Any, server: Server, messages: list[Any]
) -> None:
    await ready(http)
    candidate = http.validate(EVENT, CUTOFF)
    server.bodies["/conversations.history"]["messages"] = messages
    count = len(server.requests)
    assert await http.confirm(candidate) is None
    assert len(server.requests) == count + 1


async def test_send_exact_receipt_and_cancelable_pacing(api: Any, server: Server) -> None:
    clock = [10.0]
    waits: list[float] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay: float) -> None:
        waits.append(delay)
        entered.set()
        await release.wait()
        clock[0] += delay

    http = api.SlackHttp(
        CONFIG, transport=httpx2.MockTransport(server), clock=lambda: clock[0], sleep=sleep
    )
    try:
        await ready(http)
        await http.send("First")
        count = len(server.requests)
        pending = asyncio.create_task(http.send("Cancelled"))
        await asyncio.wait_for(entered.wait(), 0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(server.requests) == count
        release.set()
        await http.send("Second")
        assert waits == [1.0, 1.0]
        assert json.loads(server.requests[-1].content) == {
            "channel": DM,
            "text": "Second",
            "mrkdwn": True,
            "parse": "none",
            "unfurl_links": False,
            "unfurl_media": False,
        }
    finally:
        await http.close()


async def test_send_pacing_survives_runtime_disable_close_and_fresh_objects(
    api: Any, server: Server
) -> None:
    from tests.messaging.test_discord_http import Clock, runtime_reenable_after_public_deadline

    clock = Clock()

    def make_http() -> Any:
        return api.SlackHttp(
            CONFIG, transport=httpx2.MockTransport(server), clock=clock, sleep=clock.sleep
        )

    async def establish(client: Any) -> float:
        await client.send("one successful message")
        return clock.now + 1

    await runtime_reenable_after_public_deadline("slack", make_http, clock, establish)


@pytest.mark.parametrize(
    "body",
    [
        {"channel": "D87654321"},
        {"message": {"text": "changed", "bot_id": BOT, "ts": TS}},
        {"message": {"text": "hello", "bot_id": "B87654321", "ts": TS}},
    ],
)
async def test_send_receipt_mutation_stops_client(
    http: Any, server: Server, api: Any, body: Any
) -> None:
    await ready(http)
    server.bodies["/chat.postMessage"] = body
    with pytest.raises(api.SlackHttpError, match="^slack_post_invalid$"):
        await http.send("hello")


@pytest.mark.parametrize(("part", "reply"), [("x" * 4001, ""), ("hello", TS), ("\ud800", "")])
async def test_invalid_send_input_precedes_network(
    http: Any, server: Server, api: Any, part: str, reply: str
) -> None:
    await ready(http)
    count = len(server.requests)
    with pytest.raises(api.SlackHttpError):
        await http.send(part, reply)
    assert len(server.requests) == count


async def test_rate_limit_stores_cooldown_without_retry(api: Any, server: Server) -> None:
    http = api.SlackHttp(CONFIG, transport=httpx2.MockTransport(server), clock=lambda: 10.0)
    try:
        await ready(http)
        server.override = httpx2.Response(
            429, headers={"retry-after": "7"}, stream=Stream({"secret": CONFIG.bot_token})
        )
        count = len(server.requests)
        with pytest.raises(api.SlackHttpError, match="^slack_rate_limited$") as error:
            await http.send("hello")
        assert http.cooldown_until == 17.0 and error.value.__context__ is None
        assert CONFIG.bot_token not in repr(error.value)
        with pytest.raises(api.SlackHttpError):
            await http.socket_url()
        assert len(server.requests) == count + 1
    finally:
        await http.close()


@pytest.mark.parametrize("date", [None, "bad"])
async def test_initial_date_refusal_stops_before_identity_lookups(
    http: Any, server: Server, api: Any, date: str | None
) -> None:
    server.headers = {} if date is None else {"date": date}
    with pytest.raises(api.SlackHttpError, match="^slack_freshness_failed$"):
        await http.bind()
    assert len(server.requests) == 1


@pytest.mark.parametrize("identity", ["team_id", "user_id", "bot_id", "app_id"])
async def test_fresh_cutoff_refuses_identity_drift(
    http: Any, server: Server, api: Any, identity: str
) -> None:
    await ready(http)
    server.bodies["/auth.test"][identity] = "foreign"
    with pytest.raises(api.SlackHttpError, match="^slack_freshness_failed$"):
        await http.fresh_cutoff()


@pytest.mark.parametrize(
    "profile", [{"id": "B87654321", "app_id": APP}, {"id": BOT, "app_id": "A87654321"}, "invalid"]
)
async def test_send_refuses_contradictory_bot_profile(
    http: Any, server: Server, api: Any, profile: Any
) -> None:
    await ready(http)
    server.bodies["/chat.postMessage"] = {
        "message": {
            "type": "message",
            "bot_id": BOT,
            "text": "hello",
            "ts": TS,
            "bot_profile": profile,
        }
    }
    with pytest.raises(api.SlackHttpError, match="^slack_post_invalid$"):
        await http.send("hello")


async def test_failure_handler_saves_429_before_self_cancel_and_runtime_replacement(
    api: Any, server: Server
) -> None:
    from tests.messaging.test_discord_http import Clock, runtime_reenable_after_public_deadline
    from tests.messaging.test_messaging_http import HeldCloseStream

    clock = Clock()

    def make_http() -> Any:
        return api.SlackHttp(
            CONFIG, transport=httpx2.MockTransport(server), clock=clock, sleep=clock.sleep
        )

    async def establish(client: Any) -> float:
        stream = HeldCloseStream([b"private rate response"])
        server.override = httpx2.Response(429, headers={"retry-after": "7"}, stream=stream)
        previous = client._failure_handler
        seen: list[float] = []

        def stop() -> None:
            seen.append(client.cooldown_until)
            assert client.cooldown_until == clock.now + 7 and client.binding is None
            assert previous is not None
            previous()
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

        client.set_failure_handler(stop)
        count = len(server.requests)
        sending = asyncio.create_task(client.send("one send"))
        try:
            await asyncio.wait_for(stream.closing.wait(), 0.05)
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert len(seen) == 1 and len(server.requests) == count + 1
            return seen[0]
        finally:
            stream.release.set()
            await asyncio.gather(sending, return_exceptions=True)
            server.override = None

    await runtime_reenable_after_public_deadline("slack", make_http, clock, establish)


def _error(path: str, error: object) -> Callable[[Server], Any]:
    def prepare(server: Server) -> Any:
        server.bodies[path] = {"ok": False, "error": error}
        return httpx2.MockTransport(server)

    return prepare


def _owner_is_bot(server: Server) -> Any:
    server.bodies["/owner"]["user"]["is_bot"] = True
    return httpx2.MockTransport(server)


def _status_500(server: Server) -> Any:
    server.override = httpx2.Response(500, headers=server.headers, stream=Stream({}))
    return httpx2.MockTransport(server)


def _unreachable(_server: Server) -> Any:
    class Unreachable(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("synthetic unreachable", request=request)

    return Unreachable()


_TOKEN_ERROR_IDS = (
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_revoked",
    "token_expired",
    "not_allowed_token_type",
)


@pytest.mark.parametrize(
    ("prepare", "step", "reason", "early", "one_request"),
    [
        *(
            pytest.param(
                _error("/auth.test", error),
                "bind",
                "token_rejected",
                False,
                True,
                id=f"auth-{error}",
            )
            for error in _TOKEN_ERROR_IDS
        ),
        pytest.param(
            _error("/auth.test", "internal_error"), "bind", None, False, True, id="auth-internal"
        ),
        pytest.param(
            _error("/auth.test", {"code": "invalid_auth"}),
            "bind",
            None,
            False,
            True,
            id="auth-nonstring",
        ),
        pytest.param(_status_500, "bind", None, True, True, id="status-500"),
        pytest.param(
            _error("/owner", "user_not_found"),
            "bind",
            "account_check_failed",
            False,
            False,
            id="owner-not-found",
        ),
        pytest.param(
            _error("/owner", "internal_error"),
            "bind",
            None,
            False,
            False,
            id="owner-internal",
        ),
        pytest.param(
            _error("/bots.info", "missing_scope"),
            "bind",
            "account_check_failed",
            False,
            False,
            id="bots-scope",
        ),
        pytest.param(
            _owner_is_bot, "bind", "account_check_failed", False, False, id="owner-is-bot"
        ),
        pytest.param(
            _error("/owner", "token_revoked"),
            "bind",
            "token_rejected",
            False,
            False,
            id="owner-revoked",
        ),
        # The transport fails before reaching `server`, so `server.requests` stays empty.
        pytest.param(_unreachable, "bind", "server_unreachable", False, False, id="unanswered"),
        pytest.param(
            _error("/apps.connections.open", "invalid_auth"),
            "socket",
            "app_token_rejected",
            False,
            False,
            id="app-invalid_auth",
        ),
        pytest.param(
            _error("/apps.connections.open", "missing_scope"),
            "socket",
            "app_token_rejected",
            False,
            False,
            id="app-missing_scope",
        ),
        pytest.param(
            _error("/chat.postMessage", "missing_scope"),
            "send",
            "account_check_failed",
            False,
            False,
            id="post-scope",
        ),
    ],
)
async def test_slack_names_why_it_failed(
    api: Any,
    server: Server,
    prepare: Callable[[Server], Any],
    step: str,
    reason: str | None,
    early: bool,
    one_request: bool,
) -> None:
    http = api.SlackHttp(CONFIG, transport=prepare(server))
    seen: list[Any] = []
    http.set_failure_handler(lambda: seen.append(http.stop_reason))
    try:
        if step != "bind":
            await ready(http)
        with pytest.raises(api.SlackHttpError):
            if step == "bind":
                await http.bind()
            elif step == "socket":
                await http.socket_url()
            else:
                await http.send("hello")
        assert http.stop_reason == reason
        assert seen == [None if early else reason]
        if one_request:
            assert len(server.requests) == 1
        if reason is not None:
            # A later failure (here, the same probe again) keeps the first reason.
            with pytest.raises(api.SlackHttpError):
                await http.socket_url()
            assert http.stop_reason == reason
    finally:
        await http.close()


@pytest.mark.parametrize(
    ("path", "error", "reason"),
    [
        pytest.param("/auth.test", "invalid_auth", "token_rejected", id="auth-invalid_auth"),
        pytest.param(
            "/apps.connections.open",
            "invalid_auth",
            "app_token_rejected",
            id="connections-invalid_auth",
        ),
        pytest.param("/owner", "user_not_found", "account_check_failed", id="owner-user_not_found"),
        pytest.param(
            "/apps.connections.open",
            httpx2.ConnectError,
            "server_unreachable",
            id="connections-unanswered",
        ),
        pytest.param(
            "/apps.connections.open",
            httpx2.ReadTimeout,
            "server_unreachable",
            id="connections-timeout",
        ),
    ],
)
async def test_failed_slack_start_reads_its_reason(
    api: Any, tmp_path: Path, path: str, error: str | type[httpx2.TransportError], reason: str
) -> None:
    from tests.messaging.test_mattermost_bot import HANG_GUARD_S, Rig

    server = Server()
    if isinstance(error, str):
        server.bodies[path] = {"ok": False, "error": error}
    failed: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if not isinstance(error, str) and request.url.path.endswith(path):
            failed.append(request)
            raise error("synthetic network refusal", request=request)
        return server(request)

    http = api.SlackHttp(CONFIG, transport=httpx2.MockTransport(respond))
    rig = Rig(tmp_path)
    stopped = asyncio.Event()

    def factory(on_disarm: Callable[[], None]) -> RemoteBot:
        def notify() -> None:
            on_disarm()
            stopped.set()

        return RemoteBot(
            SlackChannel(CONFIG, http),
            rig.manager,
            rig.approvals,
            create_actor=rig.create,
            on_disarm=notify,
        )

    runtime = MessagingRuntime(slack=factory)
    try:
        enabled = await runtime.set_enabled("slack", True)
        assert enabled["state"] == "connecting"
        async with asyncio.timeout(HANG_GUARD_S):
            await stopped.wait()
            run_task = runtime._entries["slack"].run
            assert run_task is not None
            await run_task
        assert runtime.status()["slack"] == {
            "configured": True,
            "state": "stopped",
            "restart_required": False,
            "reason": reason,
        }
        assert http.stop_reason == reason
        assert len(failed) == (0 if isinstance(error, str) else 1)
    finally:
        await runtime.close()
        await rig.approvals.close()
        await rig.manager.close_all()


async def test_late_429_after_concurrent_500_retains_response_deadline_across_runtime(
    api: Any, server: Server
) -> None:
    from tests.messaging.test_discord_http import Clock, runtime_reenable_after_public_deadline
    from tests.messaging.test_messaging_http import HeldCloseStream

    clock = Clock()
    confirmation_started, sending_started = asyncio.Event(), asyncio.Event()
    permit_500, permit_429 = asyncio.Event(), asyncio.Event()
    failed = HeldCloseStream([b"private failure"])
    limited = HeldCloseStream([b"private rate response"])

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/conversations.history"):
            server.requests.append(request)
            confirmation_started.set()
            await permit_429.wait()
            return httpx2.Response(429, headers={"retry-after": "20"}, stream=limited)
        if request.url.path.endswith("/chat.postMessage"):
            server.requests.append(request)
            sending_started.set()
            await permit_500.wait()
            return httpx2.Response(500, stream=failed)
        return server(request)

    def make_http() -> Any:
        return api.SlackHttp(
            CONFIG, transport=httpx2.MockTransport(respond), clock=clock, sleep=clock.sleep
        )

    async def establish(client: Any) -> float:
        candidate = client.validate(EVENT, CUTOFF)
        assert candidate is not None
        count = len(server.requests)
        confirming = asyncio.create_task(client.confirm(candidate))
        sending: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(confirmation_started.wait(), 0.05)
            sending = asyncio.create_task(client.send("one send"))
            await asyncio.wait_for(sending_started.wait(), 0.05)
            permit_500.set()
            await asyncio.wait_for(failed.closing.wait(), 0.05)
            assert client._failed and client.binding is None
            clock.now += 3
            permit_429.set()
            await asyncio.wait_for(limited.closing.wait(), 0.05)
            deadline = clock.now + 20
            assert client.cooldown_until == deadline
            clock.now += 1
            failed.release.set()
            limited.release.set()
            with pytest.raises(api.SlackHttpError, match="^slack_http_failed$"):
                await sending
            with pytest.raises(api.SlackHttpError, match="^slack_rate_limited$"):
                await confirming
            assert (
                client.cooldown_until == deadline
            )  # Cleanup did not start another 20-second wait.
            assert len(server.requests) == count + 2
            return deadline
        finally:
            permit_500.set()
            permit_429.set()
            failed.release.set()
            limited.release.set()
            await asyncio.gather(
                confirming, *([sending] if sending is not None else []), return_exceptions=True
            )

    await runtime_reenable_after_public_deadline("slack", make_http, clock, establish)
