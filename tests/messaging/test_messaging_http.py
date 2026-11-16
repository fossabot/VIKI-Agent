"""Shared fixed-provider HTTP boundary, using synthetic streams only."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from viki_agent.messaging import mattermost_http as boundary

DATE = "Tue, 13 Oct 2026 01:02:03 GMT"
BASE = "https://provider.example.test/api"
TOKEN = "synthetic-messaging-secret"


class Stream(httpx2.AsyncByteStream):
    closed = False
    reads = 0

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def client(handler: Any) -> Any:
    return boundary.FixedJsonHttp(
        BASE,
        {"Authorization": "Bot " + TOKEN, "User-Agent": "VIKI Agent"},
        allowed=lambda method, path: method == "POST" and path == "/message",
        transport=httpx2.MockTransport(handler),
    )


@pytest.mark.parametrize("status", [200, 201])
async def test_exact_status_and_safe_headers(status: int) -> None:
    stream = Stream([b'{"ok":true}'])

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == BASE + "/message"
        assert request.headers["authorization"] == "Bot " + TOKEN
        assert request.headers["user-agent"] == "VIKI Agent"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"] == "application/json"
        assert "cookie" not in request.headers
        assert request.content == b'{"text":"hello"}'
        return httpx2.Response(
            status,
            headers={"Date": DATE, "X-RateLimit-Remaining": "0", "private": TOKEN},
            stream=stream,
        )

    http = client(handler)
    try:
        body, headers = await http.request(
            "POST", "/message", {"text": "hello"}, status_codes=(status,)
        )
        assert body == {"ok": True}
        assert headers == {"date": DATE, "x-ratelimit-remaining": "0"}
        assert boundary.server_date(headers) == 1791853323000
        assert stream.closed
    finally:
        await http.close()


async def test_fixed_http_rejects_redirect_and_secret_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = Stream([TOKEN.encode()])
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        logging.getLogger("httpx2").warning("%s %s %s", BASE, TOKEN, "private body")
        return httpx2.Response(
            302, headers={"location": BASE + "/other", "retry-after": "2"}, stream=stream
        )

    http = client(handler)
    try:
        with pytest.raises(boundary.FixedHttpError, match="^messaging_http_failed$") as error:
            await http.request("POST", "/message", status_codes=(302,))
        assert error.value.__context__ is None
        assert TOKEN not in repr(error.value) + str(error.value) + caplog.text
        assert BASE not in caplog.text
        assert len(requests) == 1 and stream.closed and stream.reads == 0
    finally:
        await http.close()


async def test_rate_refusal_exposes_only_bounded_safe_headers() -> None:
    stream = Stream([TOKEN.encode()])
    http = client(
        lambda _: httpx2.Response(
            429,
            headers=[
                ("date", DATE),
                ("retry-after", "2"),
                ("x-ratelimit-limit", "1"),
                ("x-ratelimit-limit", "2"),
                ("x-ratelimit-bucket", "x" * 129),
                ("x-ratelimit-reset", "bad\nvalue"),
                ("private", TOKEN),
            ],
            stream=stream,
        )
    )
    try:
        with pytest.raises(boundary.FixedHttpError) as error:
            await http.request("POST", "/message")
        assert error.value.status_code == 429
        assert error.value.headers == {"date": DATE, "retry-after": "2"}
        assert stream.closed and stream.reads == 0
    finally:
        await http.close()


@pytest.mark.parametrize("body", [{"bad": float("nan")}, {"bad": "x" * (128 * 1024)}])
async def test_invalid_or_oversized_request_is_refused_before_send(body: Any) -> None:
    requests = []
    http = client(lambda request: requests.append(request))
    try:
        with pytest.raises(boundary.FixedHttpError):
            await http.request("POST", "/message", body)
        assert not requests
    finally:
        await http.close()


@pytest.mark.parametrize("raw", [b'{"bad":1e999}', b'{"bad":-1e999}', b'{"same":1,"same":2}'])
async def test_nonfinite_and_duplicate_response_are_refused(raw: bytes) -> None:
    stream = Stream([raw])
    http = client(lambda _: httpx2.Response(200, stream=stream))
    try:
        with pytest.raises(boundary.FixedHttpError) as error:
            await http.request("POST", "/message")
        assert error.value.__context__ is None and stream.closed
    finally:
        await http.close()


@pytest.mark.parametrize(("method", "path"), [("GET", "/message"), ("POST", "/other")])
async def test_closed_admission_precedes_network(method: str, path: str) -> None:
    requests = []
    http = client(lambda request: requests.append(request))
    try:
        with pytest.raises(boundary.FixedHttpError):
            await http.request(method, path)
        assert not requests
    finally:
        await http.close()


async def test_authorization_override_is_limited_to_one_request() -> None:
    authorizations = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        authorizations.append(request.headers.get_list("authorization"))
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
        return httpx2.Response(200, stream=Stream([b"{}"]))

    http = client(handler)
    try:
        await http.request("POST", "/message", authorization="Bearer synthetic-app-token")
        await http.request("POST", "/message")
        assert authorizations == [["Bearer synthetic-app-token"], ["Bot " + TOKEN]]
    finally:
        await http.close()


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer ",
        "Basic synthetic",
        "Bearer synthetic\nsecret",
        "Bearer synthetic secret",
        "Bearer " + "x" * 4097,
        123,
    ],
)
async def test_invalid_authorization_override_is_refused_without_send_or_details(
    authorization: Any, caplog: pytest.LogCaptureFixture
) -> None:
    requests = []
    http = client(lambda request: requests.append(request))
    try:
        with pytest.raises(boundary.FixedHttpError, match="^messaging_http_failed$") as error:
            await http.request("POST", "/message", authorization=authorization)
        assert not requests and error.value.__context__ is None
        assert "synthetic" not in repr(error.value) + str(error.value) + caplog.text
    finally:
        await http.close()


class HeldCloseStream(Stream):
    def __init__(self, chunks: list[bytes], *, block_read: bool = False) -> None:
        super().__init__(chunks)
        self.block_read = block_read
        self.reading = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.reading.set()
        if self.block_read:
            await self.release.wait()
        async for chunk in super().__aiter__():
            yield chunk

    async def aclose(self) -> None:
        self.closing.set()
        await self.release.wait()
        await super().aclose()


@pytest.mark.parametrize(
    ("status", "headers", "raw"),
    [
        (500, {}, b"private response"),
        (200, {"set-cookie": TOKEN}, b"{}"),
        (200, {"content-encoding": "gzip"}, b"{}"),
        (200, {}, b'{"same":1,"same":2}'),
        (200, {}, b"x" * (128 * 1024 + 1)),
    ],
    ids=("status", "cookie", "compression", "invalid-json", "oversize"),
)
async def test_failure_callback_precedes_response_cleanup_and_has_only_safe_metadata(
    status: int, headers: dict[str, str], raw: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    stream = HeldCloseStream([raw])
    http = client(
        lambda _: httpx2.Response(
            status,
            headers={"date": DATE, "retry-after": "7", "private": TOKEN, **headers},
            stream=stream,
        )
    )
    seen = []

    def failure(error: boundary.FixedHttpError) -> None:
        assert vars(error) == {"status_code": status, "headers": {"date": DATE, "retry-after": "7"}}
        assert error.unanswered is False
        assert error.__context__ is error.__cause__ is None and error.__traceback__ is None
        assert TOKEN not in repr(error) + str(vars(error)) and BASE not in str(vars(error))
        seen.append(error)
        error.headers["retry-after"] = "99"  # The callback cannot mutate the raised refusal.
        raise RuntimeError(TOKEN)

    http.set_failure_callback(failure)
    task = asyncio.create_task(http.request("POST", "/message"))
    try:
        await asyncio.wait_for(stream.closing.wait(), 0.05)
        assert len(seen) == 1 and not task.done() and not stream.closed
        stream.release.set()
        with pytest.raises(boundary.FixedHttpError, match="^messaging_http_failed$") as error:
            await task
        assert error.value.headers == {"date": DATE, "retry-after": "7"}
        assert error.value.unanswered is seen[0].unanswered is False
        assert error.value.__context__ is error.value.__cause__ is None
        assert stream.closed and TOKEN not in caplog.text
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await http.close()


async def test_cancellation_notifies_before_waiting_for_response_cleanup() -> None:
    stream = HeldCloseStream([b"{}"], block_read=True)
    http = client(lambda _: httpx2.Response(200, stream=stream))
    seen = []
    http.set_failure_callback(seen.append)
    task = asyncio.create_task(http.request("POST", "/message"))
    try:
        await asyncio.wait_for(stream.reading.wait(), 0.05)
        task.cancel()
        await asyncio.wait_for(stream.closing.wait(), 0.05)
        assert len(seen) == 1 and seen[0].status_code == 200 and not task.done()
        stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await http.close()


async def test_failure_callback_is_one_replaceable_optional_slot() -> None:
    http = client(lambda _: httpx2.Response(500, stream=Stream([b"unused"])))
    first, second = [], []
    try:
        http.set_failure_callback(first.append)
        http.set_failure_callback(second.append)
        with pytest.raises(boundary.FixedHttpError):
            await http.request("POST", "/message")
        assert not first and len(second) == 1
        http.set_failure_callback(None)
        with pytest.raises(boundary.FixedHttpError):
            await http.request("POST", "/message")
        assert len(second) == 1
    finally:
        await http.close()


async def test_mattermost_zero_data_handler_precedes_held_http_cleanup() -> None:
    from tests.messaging.test_mattermost_http import CONFIG

    stream = HeldCloseStream([b"private response"])
    http = boundary.MattermostHttp(
        CONFIG, transport=httpx2.MockTransport(lambda _: httpx2.Response(500, stream=stream))
    )
    seen = []
    http.set_failure_handler(lambda: seen.append("stopped"))
    task = asyncio.create_task(http.bind())
    try:
        await asyncio.wait_for(stream.closing.wait(), 0.05)
        assert seen == ["stopped"] and not task.done()
        stream.release.set()
        with pytest.raises(boundary.MattermostHttpError, match="^mattermost_http_failed$"):
            await task
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await http.close()


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_transport_error_callback_does_not_expose_request_or_exception_context(
    failure: type[httpx2.TransportError],
) -> None:
    seen = []

    def broken(request: httpx2.Request) -> httpx2.Response:
        raise failure(TOKEN, request=request)

    http = client(broken)
    http.set_failure_callback(seen.append)
    try:
        with pytest.raises(boundary.FixedHttpError) as raised:
            await http.request("POST", "/message", {"secret": TOKEN})
        assert len(seen) == 1
        assert vars(seen[0]) == {"status_code": None, "headers": {}, "unanswered": True}
        assert raised.value.unanswered is seen[0].unanswered is True
        assert seen[0].__context__ is seen[0].__cause__ is seen[0].__traceback__ is None
        assert raised.value.__context__ is raised.value.__cause__ is None
    finally:
        await http.close()


@pytest.mark.parametrize("retry", ["7", "86400", "0", "-1", "bad", "86401", ""])
async def test_mattermost_cooldown_is_saved_before_callback_and_refuses_new_requests(
    retry: str,
) -> None:
    from tests.messaging.test_mattermost_http import CONFIG, Server

    clock = [10.0]
    server = Server()
    http = boundary.MattermostHttp(
        CONFIG, transport=httpx2.MockTransport(server), clock=lambda: clock[0]
    )
    stream = HeldCloseStream([b"private response"])
    seen = []
    http.set_failure_handler(lambda: seen.append(http.cooldown_until))
    server.override = httpx2.Response(429, headers={"retry-after": retry}, stream=stream)
    expected = 10 + (int(retry) if retry in ("7", "86400") else 86400)
    task = asyncio.create_task(http.bind())
    try:
        await asyncio.wait_for(stream.closing.wait(), 0.05)
        assert seen == [expected] and http.cooldown_until == expected
        stream.release.set()
        with pytest.raises(boundary.MattermostHttpError, match="^mattermost_http_failed$"):
            await task
        with pytest.raises(boundary.MattermostHttpError, match="^mattermost_http_failed$"):
            await http.bind()
        assert len(server.requests) == 1
        server.override = None
        clock[0] = http.cooldown_until
        assert await http.bind()
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await http.close()


async def test_mattermost_429_callback_self_cancel_retains_runtime_cooldown() -> None:
    from tests.messaging.test_discord_http import Clock, runtime_reenable_after_public_deadline
    from tests.messaging.test_mattermost_http import CONFIG, Server

    clock = Clock()
    server = Server()

    def make_http() -> boundary.MattermostHttp:
        return boundary.MattermostHttp(CONFIG, transport=httpx2.MockTransport(server), clock=clock)

    async def establish(http: boundary.MattermostHttp) -> float:
        stream = HeldCloseStream([b"private rate response"])
        server.override = httpx2.Response(429, headers={"retry-after": "7"}, stream=stream)
        previous = http._failure_handler
        seen = []

        def stop() -> None:
            seen.append(http.cooldown_until)
            assert http.cooldown_until == clock.now + 7
            assert previous is not None
            previous()
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

        http.set_failure_handler(stop)
        sending = asyncio.create_task(http.fresh_cutoff())
        try:
            await asyncio.wait_for(stream.closing.wait(), 0.05)
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert len(seen) == 1
            return seen[0]
        finally:
            stream.release.set()
            await asyncio.gather(sending, return_exceptions=True)
            server.override = None

    await runtime_reenable_after_public_deadline("mattermost", make_http, clock, establish)
