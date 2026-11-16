"""Slack Socket Mode proofs using injected sockets and synthetic envelopes."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import ssl
import uuid
from copy import deepcopy
from typing import Any

import pytest

from tests.messaging.test_mattermost_transport import Connector, HeldExitConnector, Socket
from tests.messaging.test_slack_http import APP, BINDING, BOT_USER, CUTOFF, EVENT, OWNER, TEAM
from viki_agent.messaging.messaging_channel import RemoteBinding

TLS = ssl.create_default_context()
URL = "wss://wss-primary.slack.com/link/?ticket=synthetic-secret"
HELLO = {"type": "hello", "connection_info": {"app_id": APP}, "num_connections": 1}


def envelope(number: int = 1, **event_changes: Any) -> dict[str, Any]:
    return {
        "type": "events_api",
        "envelope_id": str(uuid.UUID(int=number)),
        "accepts_response_payload": False,
        "payload": {
            "type": "event_callback",
            "api_app_id": APP,
            "team_id": TEAM,
            "event_id": f"Ev{number:08d}",
            "authorizations": [{"team_id": TEAM, "user_id": BOT_USER, "is_bot": True}],
            "event": {**EVENT, "ts": f"1791853324.{number:06d}", **event_changes},
        },
    }


class Http:
    binding: RemoteBinding | None = BINDING
    ssl_context = TLS
    calls = 0

    async def socket_url(self) -> str:
        self.calls += 1
        return URL


@pytest.fixture
def api() -> Any:
    return importlib.import_module("viki_agent.messaging.slack_transport")


class Rig:
    def __init__(self, api: Any, frames: list[Any]) -> None:
        self.api, self.http = api, Http()
        self.socket = Socket(frames)
        self.connector = Connector(self.socket)
        self.cutoff_calls = 0

    async def fresh(self) -> int:
        self.cutoff_calls += 1
        assert json.dumps(HELLO) in self.socket.received
        return CUTOFF

    def stream(self, fresh: Any = None) -> Any:
        return self.api.posted(
            self.http, BINDING, fresh_cutoff=fresh or self.fresh, connect=self.connector
        )

    async def live(self) -> tuple[Any, asyncio.Task[Any]]:
        iterator = self.stream()
        task = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(self.socket.waiting.wait(), 0.05)
        return iterator, task


@pytest.mark.parametrize("callback_error", [None, RuntimeError, asyncio.CancelledError])
async def test_terminal_notification_precedes_held_socket_exit(
    api: Any,
    callback_error: type[BaseException] | None,
) -> None:
    socket = Socket([{**HELLO, "connection_info": {"app_id": "A87654321"}}])
    connector = HeldExitConnector(socket)
    notices: list[str] = []

    def failed() -> None:
        notices.append("failed")
        if callback_error is not None:
            raise callback_error("synthetic private callback")

    pending = asyncio.create_task(
        anext(
            api.posted(
                Http(),
                BINDING,
                fresh_cutoff=lambda: _fresh_value(),
                connect=connector,
                on_failure=failed,
            )
        )
    )
    try:
        await asyncio.wait_for(connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not socket.closed
    finally:
        connector.release.set()
    with pytest.raises(api.SlackTransportError):
        await pending
    assert notices == ["failed"] and socket.closed


async def _fresh_value() -> int:
    return CUTOFF


@pytest.mark.parametrize(
    "terminal", ["disconnect", "refresh", "malformed", "eof", "ack", "cancel", "cutoff", "exit"]
)
async def test_live_terminal_or_cancel_notifies_before_socket_cleanup(
    api: Any,
    terminal: str,
) -> None:
    socket, ready = Socket([HELLO]), asyncio.Event()
    connector = HeldExitConnector(socket)
    notices: list[str] = []

    async def fresh() -> int:
        ready.set()
        return 0 if terminal == "cutoff" else CUTOFF

    iterator = api.posted(
        Http(),
        BINDING,
        fresh_cutoff=fresh,
        connect=connector,
        on_failure=lambda: notices.append("failed"),
    )
    pending = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(ready.wait(), 0.05)
        if terminal != "cutoff":
            await asyncio.wait_for(socket.waiting.wait(), 0.05)
            assert notices == []
            if terminal == "cancel":
                pending.cancel()
            elif terminal == "exit":
                socket.put(envelope())
                assert await asyncio.wait_for(pending, 0.05)
                assert notices == []
                connector.exit_error = RuntimeError("synthetic private socket exit")
                pending = asyncio.create_task(iterator.aclose())
            elif terminal == "ack":

                async def bad_ack(value: str) -> None:
                    raise RuntimeError("private ACK failure")

                socket.send = bad_ack
                socket.put(envelope())
            else:
                frames = {
                    "disconnect": {"type": "disconnect"},
                    "refresh": {"type": "refresh_requested"},
                    "malformed": "{bad",
                    "eof": RuntimeError("private EOF"),
                }
                socket.put(frames[terminal])
        await asyncio.wait_for(connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not socket.closed
    finally:
        connector.release.set()
    with pytest.raises(asyncio.CancelledError if terminal == "cancel" else api.SlackTransportError):
        await pending
    assert notices == ["failed"] and socket.closed


async def test_invalid_preconnect_authority_notifies_once(api: Any) -> None:
    http = Http()
    http.binding = None
    connector, notices = Connector(Socket([])), []
    with pytest.raises(api.SlackTransportError):
        await anext(
            api.posted(
                http,
                BINDING,
                fresh_cutoff=_fresh_value,
                connect=connector,
                on_failure=lambda: notices.append("failed"),
            )
        )
    assert notices == ["failed"] and connector.calls == []


async def test_ready_order_and_buffered_commands_discarded(api: Any) -> None:
    rig = Rig(api, [envelope(1), HELLO, envelope(2)])
    iterator, pending = await rig.live()
    assert rig.cutoff_calls == 1 and not pending.done()
    assert [json.loads(frame)["envelope_id"] for frame in rig.socket.sent] == [
        envelope(1)["envelope_id"],
        envelope(2)["envelope_id"],
    ]
    rig.socket.put(envelope(3, text="live"))
    post = await asyncio.wait_for(pending, 0.05)
    assert post.message == "live" and post.id == "1791853324.000003"
    await iterator.aclose()
    assert rig.socket.closed and rig.http.calls == 1


async def test_envelope_ack_precedes_confirmation_and_duplicate_is_ignored(api: Any) -> None:
    rig = Rig(api, [HELLO])
    iterator, first = await rig.live()
    rig.socket.put(envelope(1))
    candidate = await asyncio.wait_for(first, 0.05)
    assert rig.socket.sent
    gate, confirming = asyncio.Event(), asyncio.Event()

    async def confirm() -> None:
        assert json.loads(rig.socket.sent[-1]) == {"envelope_id": envelope(1)["envelope_id"]}
        confirming.set()
        await gate.wait()

    confirmation = asyncio.create_task(confirm())
    await asyncio.wait_for(confirming.wait(), 0.05)
    second = asyncio.create_task(anext(iterator))
    rig.socket.put(envelope(1))
    duplicate = envelope(2)
    duplicate["payload"] = deepcopy(envelope(1)["payload"])
    rig.socket.put(duplicate)
    rig.socket.put(envelope(3, user=BOT_USER))
    rig.socket.put(envelope(4, text="next"))
    result = await asyncio.wait_for(second, 0.05)
    assert result.id != candidate.id and result.message == "next"
    assert len(rig.socket.sent) == 5 and not confirmation.done()
    gate.set()
    await confirmation
    await iterator.aclose()


async def test_same_message_with_new_event_and_envelope_is_duplicate(api: Any) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.put(envelope(1))
    await asyncio.wait_for(pending, 0.05)
    pending = asyncio.create_task(anext(iterator))
    rig.socket.put(envelope(2, ts="1791853324.000001"))
    rig.socket.put(envelope(3, text="new"))
    assert (await asyncio.wait_for(pending, 0.05)).message == "new"
    assert len(rig.socket.sent) == 3
    await iterator.aclose()


async def test_frames_received_during_cutoff_are_acked_and_discarded(api: Any) -> None:
    rig = Rig(api, [HELLO])
    entered, release = asyncio.Event(), asyncio.Event()

    async def fresh() -> int:
        entered.set()
        await release.wait()
        return CUTOFF

    iterator = rig.stream(fresh)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(entered.wait(), 0.05)
    rig.socket.put(envelope(1, ts="1791999999.000001", text="buffered future"))
    release.set()
    await asyncio.wait_for(rig.socket.waiting.wait(), 0.05)
    rig.socket.put(envelope(2, text="live"))
    assert (await asyncio.wait_for(pending, 0.05)).message == "live"
    assert len(rig.socket.sent) == 2
    await iterator.aclose()


@pytest.mark.parametrize(
    "hello",
    [
        {"type": "hello", "connection_info": {"app_id": "A87654321"}},
        {"type": "hello"},
        {"type": "disconnect", "reason": "synthetic secret"},
    ],
)
async def test_bad_hello_never_calls_cutoff(api: Any, hello: Any) -> None:
    rig = Rig(api, [hello])
    with pytest.raises(api.SlackTransportError, match="^slack_transport_failed$") as error:
        await anext(rig.stream())
    assert rig.cutoff_calls == 0 and rig.socket.closed and error.value.__context__ is None


@pytest.mark.parametrize(
    ("where", "field", "value"),
    [
        ("payload", "team_id", "T87654321"),
        ("payload", "api_app_id", "A87654321"),
        ("payload", "type", "interactive"),
        ("payload", "event_id", "not canonical"),
        (
            "payload",
            "authorizations",
            [{"team_id": "T87654321", "user_id": BOT_USER, "is_bot": True}],
        ),
        ("payload", "authorizations", [{"team_id": TEAM, "user_id": OWNER, "is_bot": False}]),
        ("payload", "authorizations", []),
        ("event", "channel", "D87654321"),
        ("event", "channel_type", "mpim"),
        ("event", "user", BOT_USER),
        ("event", "subtype", "bot_message"),
        ("event", "edited", {}),
        ("event", "thread_ts", EVENT["ts"]),
        ("event", "attachments", []),
        ("event", "ts", "1791853323.999999"),
    ],
)
async def test_foreign_or_forged_payload_is_acked_without_yield(
    api: Any, where: str, field: str, value: Any
) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    forged = envelope(1)
    target = forged["payload"] if where == "payload" else forged["payload"]["event"]
    target[field] = value
    rig.socket.put(forged)
    rig.socket.put(envelope(2, text="admitted"))
    assert (await asyncio.wait_for(pending, 0.05)).message == "admitted"
    assert len(rig.socket.sent) == 2
    await iterator.aclose()


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        "[]",
        '{"type":"hello","type":"events_api"}',
        '{"number":NaN}',
        '{"number":1e999}',
        b"{}",
        '{"nested":' + "[" * 33 + "0" + "]" * 33 + "}",
        '"' + "x" * (128 * 1024) + '"',
        '"\ud800"',
    ],
    ids=[
        "json",
        "array",
        "duplicate",
        "nan",
        "overflow",
        "binary",
        "deep",
        "oversize",
        "surrogate",
    ],
)
async def test_malformed_payload_is_fixed_failure(api: Any, bad: Any) -> None:
    rig = Rig(api, [bad])
    with pytest.raises(api.SlackTransportError) as error:
        await anext(rig.stream())
    assert error.value.args == ("slack_transport_failed",) and error.value.__context__ is None
    assert rig.socket.closed


@pytest.mark.parametrize(
    "bad",
    ["{}", '{"deep":' + "[" * 33 + "0" + "]" * 33 + "}", "x" * (128 * 1024 + 1)],
    ids=["object", "deep", "oversize"],
)
def test_preallocation_limits_precede_json_parse(
    api: Any, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    calls = []
    real = json.loads

    def parse(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(api.json, "loads", parse)
    if bad == "{}":
        assert api._object(bad) == {} and calls == [True]
    else:
        with pytest.raises(ValueError):
            api._object(bad)
        assert calls == []


async def test_connector_has_no_redirect_proxy_compression_or_credentials(api: Any) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    uri, options = rig.connector.calls[0]
    assert uri == URL and options["ssl"] is TLS
    assert options["proxy"] is None and options["compression"] is None
    assert options["max_size"] == 128 * 1024 and options["max_queue"] == 4
    assert options["open_timeout"] == 5 and "additional_headers" not in options
    assert options["logger"].disabled and not options["logger"].propagate
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()
    assert rig.socket.closed


async def test_ticket_is_rechecked_before_connect(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(api, [])

    async def invalid() -> str:
        return "wss://evil.test/link/?ticket=secret"

    monkeypatch.setattr(rig.http, "socket_url", invalid)
    with pytest.raises(api.SlackTransportError):
        await anext(rig.stream())
    assert not rig.connector.calls


@pytest.mark.parametrize("kind", ["disconnect", "warning", "refresh_requested", "error"])
async def test_refresh_or_failure_stops_without_reconnect(api: Any, kind: str) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.put({"type": kind, "reason": URL})
    with pytest.raises(api.SlackTransportError) as error:
        await asyncio.wait_for(pending, 0.05)
    assert error.value.__context__ is None and URL not in repr(error.value)
    assert rig.socket.closed and len(rig.connector.calls) == 1
    await iterator.aclose()


async def test_rate_event_is_acked_then_stops(api: Any) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.put(envelope(1, type="app_rate_limited"))
    with pytest.raises(api.SlackTransportError):
        await asyncio.wait_for(pending, 0.05)
    assert len(rig.socket.sent) == 1 and rig.socket.closed
    await iterator.aclose()


async def test_failed_ack_closes_before_yield_without_details(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()

    async def failed(_: str) -> None:
        raise RuntimeError(URL + " synthetic private message")

    monkeypatch.setattr(rig.socket, "send", failed)
    rig.socket.put(envelope())
    with pytest.raises(api.SlackTransportError) as error:
        await asyncio.wait_for(pending, 0.05)
    assert error.value.__context__ is None and "private" not in repr(error.value)
    assert rig.socket.closed
    await iterator.aclose()


async def test_cancelled_ack_does_not_yield(api: Any) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.send_gate = asyncio.Event()
    rig.socket.put(envelope())
    await asyncio.wait_for(rig.socket.send_started.wait(), 0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert rig.socket.closed and not rig.socket.sent
    await iterator.aclose()


async def test_ack_deadline_and_dedup_cap_stop(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "ACK_DEADLINE_S", 0.01)
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.send_gate = asyncio.Event()
    rig.socket.put(envelope())
    with pytest.raises(api.SlackTransportError):
        await asyncio.wait_for(pending, 0.05)
    assert rig.socket.closed
    await iterator.aclose()
    monkeypatch.setattr(api, "_DEDUP_LIMIT", 1)
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    rig.socket.put(envelope())
    with pytest.raises(api.SlackTransportError):
        await asyncio.wait_for(pending, 0.05)
    assert len(rig.socket.sent) == 1 and rig.socket.closed
    await iterator.aclose()


async def test_quiet_logger_does_not_emit_ticket(
    api: Any, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(api, [HELLO])
    iterator, pending = await rig.live()
    logger = rig.connector.calls[0][1]["logger"]
    for level in (logging.DEBUG, logging.WARNING, logging.ERROR):
        logger.log(level, URL)
    assert URL not in caplog.text
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()


async def test_auth_deadline_stops_without_cutoff(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "AUTH_DEADLINE_S", 0.01)
    rig = Rig(api, [])
    with pytest.raises(api.SlackTransportError) as error:
        await asyncio.wait_for(anext(rig.stream()), 0.05)
    assert rig.cutoff_calls == 0 and rig.socket.closed and error.value.__context__ is None


def test_default_connector_refuses_redirect_without_ticket(api: Any) -> None:
    connector = api._NoRedirectConnect(URL)
    refusal = connector.process_redirect(RuntimeError(URL))
    assert isinstance(refusal, Exception) and URL not in str(refusal) + repr(refusal)
