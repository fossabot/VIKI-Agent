"""Bounded Mattermost API operations against synthetic public transports."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import logging.config
import ssl
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx2
import pytest

from viki_agent.messaging import mattermost_http
from viki_agent.messaging.mattermost_config import MattermostConfig
from viki_agent.messaging.mattermost_http import (
    Binding,
    FixedHttpError,
    FixedJsonHttp,
    MattermostHttp,
    MattermostHttpError,
    ProgressError,
    ProgressHttp,
)
from viki_agent.messaging.messaging_config import SlackConfig
from viki_agent.messaging.slack_http import SlackHttp, SlackHttpError

BOT, OWNER, CHANNEL, POST, OTHER = (char * 26 for char in "bocpx")
TOKEN = "synthetic-secret-token"
PREFIX = "/deployment"
DATE = "Tue, 13 Oct 2026 01:02:03 GMT"
CUTOFF = 1791853324000
CONFIG = MattermostConfig("https://chat.example.test" + PREFIX, OWNER, TOKEN)


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], blocked: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.blocked = blocked
        self.started = asyncio.Event()
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        if self.blocked is not None:
            await self.blocked.wait()
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.streams: list[Stream] = []
        self.headers: dict[str, str] = {"date": DATE}
        self.bodies: dict[str, Any] = {
            "/api/v4/users/me": {"id": BOT, "is_bot": True, "delete_at": 0, "roles": "system_user"},
            "/api/v4/users/" + OWNER: {"id": OWNER, "delete_at": 0, "roles": "system_user"},
            "/api/v4/channels/direct": {"id": CHANNEL, "type": "D", "delete_at": 0},
            "/api/v4/channels/" + CHANNEL + "/members": [
                {"channel_id": CHANNEL, "user_id": BOT},
                {"channel_id": CHANNEL, "user_id": OWNER},
            ],
            "/api/v4/posts/" + POST: {
                "id": POST,
                "channel_id": CHANNEL,
                "user_id": OWNER,
                "create_at": CUTOFF,
                "edit_at": 0,
                "delete_at": 0,
                "root_id": "",
                "message": "literal synthetic message",
            },
        }
        self.statuses: dict[str, int] = {}
        self.override: httpx2.Response | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
        assert str(request.url).startswith(CONFIG.url + "/api/v4/")
        if self.override is not None:
            return self.override
        path = request.url.path.removeprefix(PREFIX)
        if path in self.statuses:
            stream = Stream([b"{}"])
            self.streams.append(stream)
            return httpx2.Response(self.statuses[path], headers=self.headers, stream=stream)
        if path == "/api/v4/posts" and request.method == "POST":
            body = {"id": POST, "user_id": BOT, **json.loads(request.content)}
        else:
            body = self.bodies[path]
        stream = Stream([json.dumps(body).encode("utf-8")])
        self.streams.append(stream)
        return httpx2.Response(
            201 if request.method == "POST" else 200, headers=self.headers, stream=stream
        )


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
async def http(server: Server) -> AsyncIterator[MattermostHttp]:
    client = MattermostHttp(CONFIG, transport=httpx2.MockTransport(server))
    try:
        yield client
    finally:
        await client.close()


async def test_exact_bot_owner_dm_binding(http: MattermostHttp, server: Server) -> None:
    assert await http.bind() == Binding(BOT, OWNER, CHANNEL)
    assert [
        (request.method, request.url.path, request.url.query) for request in server.requests
    ] == [
        ("GET", PREFIX + "/api/v4/users/me", b""),
        ("GET", PREFIX + "/api/v4/users/" + OWNER, b""),
        ("POST", PREFIX + "/api/v4/channels/direct", b""),
        ("GET", PREFIX + "/api/v4/channels/" + CHANNEL + "/members", b"per_page=3&page=0"),
    ]
    assert json.loads(server.requests[2].content) == [BOT, OWNER]
    assert all(stream.closed for stream in server.streams)
    server.bodies["/api/v4/users/me"]["is_bot"] = False
    with pytest.raises(MattermostHttpError, match="^mattermost_binding_failed$"):
        await http.bind()


async def test_owner_nonbot_flag_can_be_omitted(http: MattermostHttp, server: Server) -> None:
    # Mattermost's User model serializes is_bot with omitempty.
    assert "is_bot" not in server.bodies["/api/v4/users/" + OWNER]
    assert (await http.bind()).owner_user_id == OWNER


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v4/users/me", {"id": BOT, "delete_at": 0, "roles": "system_user"}),
        ("/api/v4/users/me", {"id": BOT, "is_bot": 1, "delete_at": 0, "roles": "system_user"}),
        ("/api/v4/users/me", {"id": BOT, "is_bot": True, "delete_at": 1, "roles": "system_user"}),
        (
            "/api/v4/users/me",
            {"id": BOT, "is_bot": True, "delete_at": False, "roles": "system_user"},
        ),
        ("/api/v4/users/me", {"id": BOT, "is_bot": True, "delete_at": 0, "roles": "system_admin"}),
        ("/api/v4/users/me", {"id": OWNER, "is_bot": True, "delete_at": 0, "roles": "system_user"}),
        ("/api/v4/users/me", {"id": "bad", "is_bot": True, "delete_at": 0, "roles": "system_user"}),
        ("/api/v4/users/" + OWNER, {"id": OTHER, "is_bot": False, "delete_at": 0}),
        ("/api/v4/users/" + OWNER, {"id": OWNER, "is_bot": True, "delete_at": 0}),
        ("/api/v4/users/" + OWNER, {"id": OWNER, "is_bot": 0, "delete_at": 0}),
        ("/api/v4/users/" + OWNER, {"id": OWNER, "is_bot": None, "delete_at": 0}),
        ("/api/v4/users/" + OWNER, {"id": OWNER, "is_bot": False, "delete_at": 1}),
        ("/api/v4/channels/direct", {"id": CHANNEL, "type": "G", "delete_at": 0}),
        ("/api/v4/channels/direct", {"id": CHANNEL, "type": "D", "delete_at": 1}),
        ("/api/v4/channels/direct", {"id": CHANNEL, "type": "D", "delete_at": False}),
        ("/api/v4/channels/direct", {"id": "bad", "type": "D", "delete_at": 0}),
        ("/api/v4/channels/" + CHANNEL + "/members", []),
        ("/api/v4/channels/" + CHANNEL + "/members", [{"channel_id": CHANNEL, "user_id": BOT}] * 2),
        (
            "/api/v4/channels/" + CHANNEL + "/members",
            [{"channel_id": OTHER, "user_id": BOT}, {"channel_id": CHANNEL, "user_id": OWNER}],
        ),
        (
            "/api/v4/channels/" + CHANNEL + "/members",
            [
                {"channel_id": CHANNEL, "user_id": BOT},
                {"channel_id": CHANNEL, "user_id": OWNER},
                {"channel_id": CHANNEL, "user_id": OTHER},
            ],
        ),
    ],
)
async def test_binding_refuses_remote_authority_mismatches(
    http: MattermostHttp, server: Server, path: str, body: Any
) -> None:
    server.bodies[path] = body
    with pytest.raises(MattermostHttpError, match="^mattermost_binding_failed$"):
        await http.bind()


@pytest.mark.parametrize(
    ("path", "status", "code", "requests"),
    [
        ("/api/v4/users/me", 401, "mattermost_token_rejected", 1),
        ("/api/v4/users/me", 403, "mattermost_token_rejected", 1),
        ("/api/v4/users/me", 404, "mattermost_http_failed", 1),
        ("/api/v4/users/me", 500, "mattermost_http_failed", 1),
        ("/api/v4/users/" + OWNER, 404, "mattermost_binding_failed", 2),
        ("/api/v4/users/" + OWNER, 401, "mattermost_binding_failed", 2),
        ("/api/v4/channels/direct", 403, "mattermost_binding_failed", 3),
        ("/api/v4/channels/" + CHANNEL + "/members", 400, "mattermost_binding_failed", 4),
        ("/api/v4/users/" + OWNER, 429, "mattermost_http_failed", 2),
        ("/api/v4/users/" + OWNER, 500, "mattermost_http_failed", 2),
    ],
    ids=[
        "me-401",
        "me-403",
        "me-404",
        "me-500",
        "owner-404",
        "owner-401",
        "direct-403",
        "members-400",
        "owner-429",
        "owner-500",
    ],
)
async def test_bind_names_why_it_failed(
    http: MattermostHttp, server: Server, path: str, status: int, code: str, requests: int
) -> None:
    server.statuses[path] = status
    with pytest.raises(MattermostHttpError, match=f"^{code}$") as error:
        await http.bind()
    assert error.value.args == (code,)
    assert error.value.__context__ is None
    assert len(server.requests) == requests


async def test_transport_never_follows_redirects_or_proxies(
    monkeypatch: pytest.MonkeyPatch, server: Server
) -> None:
    transport_options: dict[str, Any] = {}
    client_options: dict[str, Any] = {}
    real_client = httpx2.AsyncClient

    def transport(**options: Any) -> httpx2.MockTransport:
        transport_options.update(options)
        return httpx2.MockTransport(server)

    def client(**options: Any) -> httpx2.AsyncClient:
        client_options.update(options)
        return real_client(**options)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(httpx2, "AsyncClient", client)
    client_instance = MattermostHttp(CONFIG)
    try:
        context = client_instance.ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert transport_options["verify"] is context
        assert transport_options["trust_env"] is False
        assert transport_options["retries"] == 0
        assert transport_options["proxy"] is None
        assert transport_options["http1"] is True and transport_options["http2"] is False
        limits = transport_options["limits"]
        assert limits.max_connections == limits.max_keepalive_connections == 2
        assert client_options["trust_env"] is False
        assert client_options["follow_redirects"] is False
        assert client_options["auth"] is None and client_options["proxy"] is None
        stream = Stream([b"{}"])
        server.override = httpx2.Response(
            302, headers={"location": "https://other.example.test/token"}, stream=stream
        )
        with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
            await client_instance.bind()
        assert len(server.requests) == 1 and stream.closed
    finally:
        await client_instance.close()


async def test_fresh_cutoff_rechecks_bot_using_server_date(
    http: MattermostHttp, server: Server
) -> None:
    await http.bind()
    assert await http.fresh_cutoff() == CUTOFF
    server.headers["date"] = "Tue, 13 Oct 2026 01:02:04 GMT"
    assert await http.fresh_cutoff() == CUTOFF + 1000
    server.headers["date"] = DATE
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.fresh_cutoff()


@pytest.mark.parametrize(
    "date",
    [
        None,
        "bad date",
        "Mon, 13 Oct 2026 01:02:03 GMT",
        "Tue, 13 Oct 2026 01:02:02 GMT",
        "Tue, 13 Oct 2026 01:02:03 +0100",
    ],
)
async def test_missing_malformed_or_backwards_date_is_refused(
    http: MattermostHttp, server: Server, date: str | None
) -> None:
    await http.bind()
    server.headers = {} if date is None else {"date": date}
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.fresh_cutoff()


async def test_fresh_cutoff_refuses_changed_or_admin_bot(
    http: MattermostHttp, server: Server
) -> None:
    await http.bind()
    server.bodies["/api/v4/users/me"]["id"] = OTHER
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.fresh_cutoff()
    server.bodies["/api/v4/users/me"].update(id=BOT, roles="system_admin")
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.fresh_cutoff()


async def test_initial_and_duplicate_date_headers_fail_closed(
    http: MattermostHttp, server: Server
) -> None:
    server.headers = {}
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.bind()
    assert len(server.requests) == 1
    server.headers = {"date": DATE}
    await http.bind()
    stream = Stream([json.dumps(server.bodies["/api/v4/users/me"]).encode()])
    server.override = httpx2.Response(200, headers=[("date", DATE), ("date", DATE)], stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await http.fresh_cutoff()
    assert stream.closed


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", OTHER),
        ("channel_id", OTHER),
        ("user_id", BOT),
        ("create_at", 0),
        ("create_at", True),
        ("create_at", "1"),
        ("edit_at", 1),
        ("edit_at", False),
        ("delete_at", 1),
        ("delete_at", False),
    ],
)
async def test_rest_post_revalidates_owner_channel_id_and_timestamps(
    http: MattermostHttp, server: Server, key: str, value: Any
) -> None:
    await http.bind()
    server.bodies["/api/v4/posts/" + POST][key] = value
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.post(POST)


async def test_post_and_send_use_only_bound_ids_and_exact_receipt(
    http: MattermostHttp, server: Server
) -> None:
    binding = await http.bind()
    assert (await http.post(POST))["id"] == POST
    message = "literal **markdown** and @mention"
    assert await http.send(binding, message, POST) == POST
    assert json.loads(server.requests[-1].content) == {
        "channel_id": CHANNEL,
        "message": message,
        "root_id": POST,
    }
    count = len(server.requests)
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.send(replace(binding, channel_id=OTHER), message)
    assert len(server.requests) == count


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", "bad"),
        ("channel_id", OTHER),
        ("user_id", OWNER),
        ("message", "changed"),
        ("root_id", POST),
    ],
)
async def test_send_refuses_mismatched_receipt(
    http: MattermostHttp, server: Server, key: str, value: Any
) -> None:
    binding = await http.bind()
    body = {
        "id": POST,
        "channel_id": CHANNEL,
        "user_id": BOT,
        "message": "hello",
        "root_id": "",
        key: value,
    }
    stream = Stream([json.dumps(body).encode()])
    server.override = httpx2.Response(201, stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.send(binding, "hello")
    assert stream.closed


@pytest.mark.parametrize(
    ("message", "root"),
    [
        ("a" * (12 * 1024 + 1), ""),
        ("\u00e9" * (6 * 1024 + 1), ""),
        ("\ud800", ""),
        ("hello", "../other"),
        ("hello", "A" * 26),
    ],
)
async def test_invalid_send_inputs_are_refused_before_io(
    http: MattermostHttp, server: Server, message: str, root: str
) -> None:
    binding = await http.bind()
    count = len(server.requests)
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.send(binding, message, root)
    assert len(server.requests) == count


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example.test/api/v4/users/me",
        "//other.example.test",
        "/api/v4/users/me?other=1",
        "/api/v4/posts/../other",
        "/api/v4/files",
        "/api/v4/channels/direct?other=1",
    ],
)
async def test_internal_endpoint_allowlist_refuses_arbitrary_paths(
    http: MattermostHttp, server: Server, path: str
) -> None:
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
        await http._request("GET", path)
    assert server.requests == []


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b'{"id":1,"id":2}',
        b'{"nested":{"same":1,"same":2}}',
        b'{"value":NaN}',
        b"[",
        b"[[" * 1000,
    ],
)
async def test_invalid_utf8_json_and_duplicate_keys_are_refused(
    http: MattermostHttp, server: Server, raw: bytes
) -> None:
    stream = Stream([raw])
    server.override = httpx2.Response(200, stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$") as error:
        await http.bind()
    assert error.value.args == ("mattermost_http_failed",)
    assert error.value.__context__ is None
    assert stream.closed


async def test_raw_body_cap_stops_before_later_chunks(http: MattermostHttp, server: Server) -> None:
    stream = Stream([b" " * (128 * 1024), b" ", b"not consumed"])
    server.override = httpx2.Response(200, stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
        await http.bind()
    assert stream.reads == 2 and stream.closed
    raw = b"{}" + b" " * (128 * 1024 - 2)
    server.override = httpx2.Response(200, stream=Stream([raw]))
    body, _ = await http._request("GET", "/api/v4/users/me")
    assert body == {}


@pytest.mark.parametrize(
    "headers",
    [
        {"set-cookie": "synthetic=secret; Path=/"},
        {"content-encoding": "gzip"},
        {"content-encoding": "br"},
        {"content-encoding": "identity, gzip"},
    ],
)
async def test_cookies_and_encodings_are_rejected_before_body_read(
    http: MattermostHttp, server: Server, headers: dict[str, str]
) -> None:
    stream = Stream([b"{}"])
    server.override = httpx2.Response(200, headers=headers, stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
        await http.bind()
    assert stream.reads == 0 and stream.closed
    assert len(http._client.cookies) == 0


async def test_cookie_jar_is_cleared_before_each_request_and_on_headers(
    http: MattermostHttp, server: Server
) -> None:
    http._client.cookies.set("synthetic", "secret")
    await http.bind()
    assert len(http._client.cookies) == 0
    assert all("cookie" not in request.headers for request in server.requests)


async def test_cookie_header_is_refused_before_send(http: MattermostHttp, server: Server) -> None:
    http._client.headers["Cookie"] = "synthetic=secret"
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
        await http.bind()
    assert server.requests == []


async def test_set_cookie_drop_is_opt_in() -> None:
    # Only a client built with set_cookie="drop" reads past a set-cookie reply. The default and
    # Slack (no cookie test of its own) refuse it unread; a cookie never rides a request.
    parameter = inspect.signature(FixedJsonHttp).parameters["set_cookie"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default == "refuse"
    sent: list[httpx2.Request] = []
    streams: list[Stream] = []

    def cookie_reply(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        streams.append(Stream([b'{"ok":true}']))
        return httpx2.Response(
            200, headers={"set-cookie": "synthetic=secret; Path=/"}, stream=streams[-1]
        )

    def fixed(**options: Any) -> FixedJsonHttp:
        return FixedJsonHttp(
            "https://provider.example.test",
            {"User-Agent": "VIKI Agent"},
            allowed=lambda method, path: (method, path) == ("GET", "/fixed"),
            transport=httpx2.MockTransport(cookie_reply),
            **options,
        )

    refusing, dropping = fixed(), fixed(set_cookie="drop")
    slack = SlackHttp(
        SlackConfig("xoxb-synthetic-bot", "xapp-synthetic-app", "U87654321"),
        transport=httpx2.MockTransport(cookie_reply),
    )
    try:
        with pytest.raises(FixedHttpError, match="^messaging_http_failed$"):
            await refusing.request("GET", "/fixed")
        with pytest.raises(SlackHttpError, match="^slack_http_failed$"):
            await slack.bind()
        assert [(stream.reads, stream.closed) for stream in streams] == [(0, True), (0, True)]
        for _ in range(2):
            assert await dropping.request("GET", "/fixed") == ({"ok": True}, {})
            assert len(dropping._client.cookies) == 0
        assert all("cookie" not in request.headers for request in sent)
        dropping._client.headers["Cookie"] = "synthetic=secret"
        with pytest.raises(FixedHttpError, match="^messaging_http_failed$"):
            await dropping.request("GET", "/fixed")
        assert len(sent) == 4
    finally:
        await refusing.close()
        await dropping.close()
        await slack.close()


async def test_utf8_message_limit_is_inclusive(http: MattermostHttp) -> None:
    binding = await http.bind()
    assert await http.send(binding, "\u00e9" * (6 * 1024)) == POST


async def test_http_dependency_logs_are_suppressed_at_debug(
    http: MattermostHttp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await http.bind()
    for name in (
        "httpx2",
        "httpcore2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
        "httpcore2.socks",
    ):
        logger = logging.getLogger(name)
        for level in (logging.DEBUG, logging.INFO, logging.WARNING):
            logger.log(level, "%s %s private body", CONFIG.url, TOKEN)
    assert caplog.records == []


async def test_http_logging_survives_reconfiguration_during_request(
    server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    names = (
        "httpx2",
        "httpcore2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
        "httpcore2.socks",
    )
    requests = 0

    def reconfigure() -> None:
        logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        reconfigure()
        for name in names:
            logger = logging.getLogger(name)
            for level in (
                logging.DEBUG,
                logging.INFO,
                logging.WARNING,
                logging.ERROR,
                logging.CRITICAL,
            ):
                logger.log(level, "%s %s private body", CONFIG.url, TOKEN)
        return server(request)

    client = MattermostHttp(CONFIG, transport=httpx2.MockTransport(handler))
    try:
        reconfigure()
        caplog.set_level(logging.DEBUG)
        assert await client.bind() == Binding(BOT, OWNER, CHANNEL)
        assert requests == 4
        logging.getLogger("viki_agent.mattermost_bot").warning("mattermost_http_failed")
        assert [(record.name, record.getMessage()) for record in caplog.records] == [
            ("viki_agent.mattermost_bot", "mattermost_http_failed")
        ]
    finally:
        await client.close()


async def test_absolute_deadline_and_cancellation_close_streams(
    http: MattermostHttp, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mattermost_http, "REQUEST_DEADLINE_S", 0.01)
    stream = Stream([b"{}"], asyncio.Event())
    server.override = httpx2.Response(200, stream=stream)
    with pytest.raises(MattermostHttpError, match="^mattermost_http_failed$"):
        await http.bind()
    assert stream.closed
    stream = Stream([b"{}"], asyncio.Event())
    server.override = httpx2.Response(200, stream=stream)
    task = asyncio.create_task(http.bind())
    await asyncio.wait_for(stream.started.wait(), 0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_transport_errors_hide_details_and_client_closes(server: Server) -> None:
    class Broken(httpx2.AsyncBaseTransport):
        closed = False
        calls = 0

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            self.calls += 1
            raise httpx2.ConnectError(TOKEN + " private request details", request=request)

        async def aclose(self) -> None:
            self.closed = True

    transport = Broken()
    http = MattermostHttp(CONFIG, transport=transport)
    try:
        with pytest.raises(MattermostHttpError) as error:
            await http.bind()
        assert error.value.args == ("mattermost_unreachable",)
        assert TOKEN not in str(error.value) + repr(error.value)
        assert error.value.__context__ is None
        assert transport.calls == 1
    finally:
        await http.close()
    assert transport.closed and http._client.is_closed
    await http.close()


async def test_no_answer_before_the_deadline_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mattermost_http, "REQUEST_DEADLINE_S", 0.01)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http = MattermostHttp(CONFIG, transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(MattermostHttpError) as error:
            await http.bind()
        assert error.value.args == ("mattermost_unreachable",)
        assert error.value.__context__ is None
    finally:
        await http.close()


async def test_reactions_reads_one_posts_reactions(http: MattermostHttp, server: Server) -> None:
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.reactions(POST)
    await http.bind()
    count = len(server.requests)
    reaction = {"user_id": BOT, "post_id": POST, "emoji_name": "eyes"}
    server.bodies["/api/v4/posts/" + POST + "/reactions"] = [reaction, "skipped", 3]
    assert await http.reactions(POST) == [reaction]
    assert [(r.method, r.url.path) for r in server.requests[count:]] == [
        ("GET", PREFIX + "/api/v4/posts/" + POST + "/reactions")
    ]
    server.bodies["/api/v4/posts/" + POST + "/reactions"] = None
    assert await http.reactions(POST) == []


@pytest.mark.parametrize("body", [{"emoji_name": "eyes"}, "eyes", 1])
async def test_reactions_refuses_other_bodies_and_ids(
    http: MattermostHttp, server: Server, body: Any
) -> None:
    await http.bind()
    server.bodies["/api/v4/posts/" + POST + "/reactions"] = body
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.reactions(POST)
    count = len(server.requests)
    with pytest.raises(MattermostHttpError, match="^mattermost_post_invalid$"):
        await http.reactions("A" * 26)
    assert len(server.requests) == count


class Progress:
    """Answers the progress calls with the statuses Mattermost uses."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.post: dict[str, Any] = {}

    def reply(
        self, status: int, body: Any, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return httpx2.Response(
            status, headers=headers, stream=Stream([json.dumps(body).encode("utf-8")])
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        path = request.url.path.removeprefix(PREFIX)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if self.status is not None:
            return self.reply(self.status, {}, self.headers)
        if path == "/api/v4/reactions":
            return self.reply(200, {**body, **self.post})
        if request.method == "POST" and path == "/api/v4/posts":
            return self.reply(201, {"id": POST, "user_id": BOT, **body, **self.post})
        if request.method == "PUT":
            return self.reply(200, {"id": POST, "user_id": BOT, **self.post})
        return self.reply(200, {"status": "OK"})


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def progress_server() -> Progress:
    return Progress()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def progress(progress_server: Progress, clock: Clock) -> AsyncIterator[ProgressHttp]:
    client = ProgressHttp(CONFIG, transport=httpx2.MockTransport(progress_server), clock=clock)
    client.bind(BOT, CHANNEL)
    try:
        yield client
    finally:
        await client.close()


async def test_progress_calls_send_fixed_bodies_and_accept_200_and_201(
    progress: ProgressHttp, progress_server: Progress
) -> None:
    await progress.react(POST, "eyes")
    await progress.unreact(POST)
    await progress.typing(POST)
    await progress.typing("")
    assert await progress.create("hello", POST) == POST
    await progress.edit(POST, "changed")
    seen = [
        (r.method, r.url.path.removeprefix(PREFIX), json.loads(r.content) if r.content else None)
        for r in progress_server.requests
    ]
    assert seen == [
        ("POST", "/api/v4/reactions", {"user_id": BOT, "post_id": POST, "emoji_name": "eyes"}),
        ("DELETE", f"/api/v4/users/{BOT}/posts/{POST}/reactions/eyes", None),
        ("POST", f"/api/v4/users/{BOT}/typing", {"channel_id": CHANNEL, "parent_id": POST}),
        ("POST", f"/api/v4/users/{BOT}/typing", {"channel_id": CHANNEL, "parent_id": ""}),
        ("POST", "/api/v4/posts", {"channel_id": CHANNEL, "message": "hello", "root_id": POST}),
        ("PUT", f"/api/v4/posts/{POST}/patch", {"message": "changed"}),
    ]


async def test_progress_refuses_calls_before_bind(progress_server: Progress) -> None:
    client = ProgressHttp(CONFIG, transport=httpx2.MockTransport(progress_server))
    try:
        for call in (
            client.react(POST, "eyes"),
            client.unreact(POST),
            client.typing(POST),
            client.create("hello", ""),
            client.edit(POST, "hello"),
        ):
            with pytest.raises(ProgressError, match="^mattermost_progress_failed$"):
                await call
        for method, path in (("POST", "/api/v4/posts"), ("POST", "/api/v4/reactions")):
            with pytest.raises(FixedHttpError):
                await client.request(method, path, {}, status_codes=(200, 201))
        assert progress_server.requests == []
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v4/users/me"),
        ("GET", "/api/v4/posts/" + POST),
        ("GET", "/api/v4/posts/" + POST + "/reactions"),
        ("POST", "/api/v4/channels/direct"),
        ("POST", "/api/v4/posts?x=1"),
        ("POST", f"/api/v4/users/{OTHER}/typing"),
        ("DELETE", "/api/v4/posts/" + POST),
        ("DELETE", f"/api/v4/users/{BOT}/posts/{POST}/reactions/warning"),
        ("DELETE", f"/api/v4/users/{OTHER}/posts/{POST}/reactions/eyes"),
        ("DELETE", f"/api/v4/users/{BOT}/posts/{'A' * 26}/reactions/eyes"),
        ("PUT", "/api/v4/posts/" + POST),
        ("PUT", f"/api/v4/posts/{POST}/patch?x=1"),
        ("PUT", "/api/v4/reactions"),
    ],
)
async def test_progress_client_refuses_other_paths_and_methods(
    progress: ProgressHttp, progress_server: Progress, method: str, path: str
) -> None:
    with pytest.raises(FixedHttpError):
        await progress.request(method, path, {}, status_codes=(200, 201))
    assert progress_server.requests == []


async def test_progress_bind_requires_valid_ids_and_one_pair(progress: ProgressHttp) -> None:
    progress.bind(BOT, CHANNEL)
    with pytest.raises(ValueError, match="^mattermost_progress_bound$"):
        progress.bind(BOT, OTHER)
    with pytest.raises(ValueError, match="^mattermost_progress_bound$"):
        progress.bind(OTHER, CHANNEL)
    fresh = ProgressHttp(CONFIG, transport=httpx2.MockTransport(Progress()))
    try:
        with pytest.raises(ValueError):
            fresh.bind("bad", CHANNEL)
    finally:
        await fresh.close()


async def test_progress_refuses_invalid_arguments_before_io(
    progress: ProgressHttp, progress_server: Progress
) -> None:
    for call in (
        progress.react(POST, "thumbsup"),
        progress.react("bad", "eyes"),
        progress.unreact("bad"),
        progress.typing("bad"),
        progress.create("a" * (12 * 1024 + 1), ""),
        progress.create("\ud800", ""),
        progress.create("hello", "bad"),
    ):
        with pytest.raises(ProgressError):
            await call
    assert progress_server.requests == []


async def test_progress_edit_refuses_an_id_it_did_not_create(
    progress: ProgressHttp, progress_server: Progress
) -> None:
    with pytest.raises(ProgressError):
        await progress.edit(POST, "changed")
    assert progress_server.requests == []
    assert await progress.create("hello", "") == POST
    await progress.edit(POST, "changed")
    with pytest.raises(ProgressError):
        await progress.edit(OTHER, "changed")
    assert len(progress_server.requests) == 2


@pytest.mark.parametrize(
    "post", [{"user_id": OWNER}, {"channel_id": OTHER}, {"root_id": OTHER}, {"message": "x"}]
)
async def test_progress_create_refuses_a_mismatched_post(
    progress: ProgressHttp, progress_server: Progress, post: dict[str, Any]
) -> None:
    progress_server.post = post
    with pytest.raises(ProgressError):
        await progress.create("hello", "")


async def test_progress_edit_refuses_a_response_naming_another_user(
    progress: ProgressHttp, progress_server: Progress
) -> None:
    await progress.create("hello", "")
    progress_server.post = {"user_id": OWNER}
    with pytest.raises(ProgressError):
        await progress.edit(POST, "changed")


async def test_progress_react_refuses_a_response_naming_another_user(
    progress: ProgressHttp, progress_server: Progress
) -> None:
    progress_server.post = {"user_id": OWNER}
    with pytest.raises(ProgressError):
        await progress.react(POST, "eyes")


async def test_progress_client_failures_stay_local(progress_server: Progress, clock: Clock) -> None:
    failures: list[None] = []
    main = MattermostHttp(CONFIG, transport=httpx2.MockTransport(progress_server), clock=clock)
    main.set_failure_handler(lambda: failures.append(None))
    progress = main.progress_client()
    progress.bind(BOT, CHANNEL)
    try:
        progress_server.status = 429
        progress_server.headers = {"retry-after": "7"}
        with pytest.raises(ProgressError, match="^mattermost_progress_failed$"):
            await progress.typing("")
        assert failures == []
        assert main.cooldown_until == 0.0
        progress_server.status = None
        count = len(progress_server.requests)
        clock.now = 6.9
        with pytest.raises(ProgressError):
            await progress.typing("")
        assert len(progress_server.requests) == count
        clock.now = 7.0
        await progress.typing("")
        progress_server.status = 500
        with pytest.raises(ProgressError):
            await progress.typing("")
        assert failures == []
    finally:
        await progress.close()
        await main.close()


async def test_progress_client_shares_the_transport(progress_server: Progress) -> None:
    main = MattermostHttp(CONFIG, transport=httpx2.MockTransport(progress_server))
    progress = main.progress_client()
    progress.bind(BOT, CHANNEL)
    try:
        await progress.typing("")
    finally:
        await progress.close()
        await main.close()
    assert [(r.method, r.url.path) for r in progress_server.requests] == [
        ("POST", f"{PREFIX}/api/v4/users/{BOT}/typing")
    ]
