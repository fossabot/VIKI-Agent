"""Acceptance (frozen): the server's security layer, routes and WebSocket protocol, driven
through Starlette's TestClient with an echoing driver and a scripted catalog. Every read from a
socket has a timeout, so a broken server fails a test instead of hanging it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.types import Message
from starlette.websockets import WebSocketDisconnect

from viki_agent import rails, server, winproc
from viki_agent.catalog import Catalog, HistoryPage, project_dir
from viki_agent.config import Config
from viki_agent.drivers.base import Answer, PermissionAnswer, QuestionAnswer
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import (
    Event,
    GitChip,
    Rails,
    SearchHit,
    SearchResult,
    SessionRow,
    SessionsResult,
    SessionStarted,
    Status,
    UserMessage,
    VaultCounts,
)
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # a session the client just made up
OLD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"  # a session with a transcript
FORK = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
U1 = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"  # a transcript entry uuid
TOKEN = "per-launch-token-for-tests"
BASE = "http://127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"
PLAN_CSP = (
    "default-src 'self'; script-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "font-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)


class EchoDriver:
    """Opens at once. Every command is recorded on a thread-safe queue (the test runs in
    another thread than the app) and echoed as a status event, so tests can wait for it."""

    pid: int | None = None
    busy = False

    def __init__(self, launch: Launch, calls: queue.Queue[str], emit: Callable[[Event], None]):
        self.launch = launch
        self.alive = False
        self._calls = calls
        self._emit = emit

    def _echo(self, what: str) -> None:
        self._calls.put(what)
        self._emit(Status(state=what))

    async def open(self) -> SessionStarted:
        self.alive = True
        launch = self.launch
        self._calls.put(f"open {launch.session_id} resume={launch.resume} fork_of={launch.fork_of}")
        return SessionStarted(
            model="default", mode="auto", effort="low", commands=[], agents=[], models=[],
            cli_version="2.1.259",
        )  # fmt: skip

    async def send(self, text: str) -> None:
        self._echo(f"send {text}")

    async def interrupt(self) -> None:
        self._echo("interrupt")

    def respond(self, tool_use_id: str, answer: Answer) -> bool:
        self._echo(f"respond {tool_use_id} {answer!r}")
        return True

    def pending(self) -> list[str]:
        return []

    async def set_model(self, model: str) -> None:
        self._echo(f"model {model}")

    async def close(self) -> None:
        self.alive = False
        self._echo("close")


class FakeCatalog(Catalog):
    def __init__(self, existing: set[str]) -> None:
        super().__init__(Path("unused-home"), Path("unused-vault"))
        self.existing = existing
        self.asked: queue.Queue[tuple[str, str | None, str | None]] = queue.Queue()
        # REST: each call records whether it ran off the event loop (in a thread), as it
        # must, since the real catalog scans files.
        self.off_loop: queue.Queue[bool] = queue.Queue()
        self.searched: queue.Queue[str] = queue.Queue()

    def exists(self, session_id: str) -> bool:
        return session_id in self.existing

    def history(
        self, session_id: str, *, before_uuid: str | None = None, until: str | None = None
    ) -> HistoryPage:
        self.asked.put((session_id, before_uuid, until))
        older = UserMessage(text="older", at="2026-09-01T00:00:00.000Z")
        return HistoryPage(events=[older], next_before_uuid=U1)

    def sessions(self) -> list[SessionRow]:
        self.off_loop.put(_off_loop())
        return [
            SessionRow(
                session_id=OLD,
                title="牛乳を買う",
                first_prompt="牛乳を買うのを忘れないで",
                entrypoint="claude-vscode",
                modified="2026-09-11T08:00:00.000Z",
                cli_version="2.1.266",
                other=False,
            ),
            SessionRow(
                session_id=U1,
                title="probe",
                first_prompt="probe",
                entrypoint="sdk-py",
                modified="2026-09-10T08:00:00.000Z",
                cli_version="2.1.259",
                other=True,
            ),
        ]

    def search(self, query: str) -> list[SearchHit]:
        self.off_loop.put(_off_loop())
        self.searched.put(query)
        return [
            SearchHit(
                session_id=OLD,
                title="牛乳を買う",
                role="user",
                snippet=f"…{query}…",
                at="2026-09-11T08:00:00.000Z",
            )
        ]

    # The server reads through the typed observations; routing them through the
    # frozen overrides above keeps the off_loop/searched recordings this suite asserts.
    def list_sessions(self, **_: Any) -> SessionsResult:
        return SessionsResult(sessions=self.sessions(), state="ready")

    def run_search(self, query: str, **_: Any) -> SearchResult:
        return SearchResult(hits=self.search(query), state="ready", scanned=1, total=1)


def _off_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


@dataclass
class App:
    client: TestClient
    manager: SessionManager
    catalog: FakeCatalog
    state: AppState
    calls: queue.Queue[str]  # driver calls, in order
    opened: queue.Queue[str]  # what the server handed to the OS opener
    vault: Path
    home: Path


@contextmanager
def running(tmp_path: Path, *, dev: bool = False, vault_poll_s: float = 60.0) -> Iterator[App]:
    vault = tmp_path / "vault"
    (vault / "30-knowledge").mkdir(parents=True)
    (vault / "30-knowledge" / "x.md").write_text("# x\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>VIKI</title>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    calls: queue.Queue[str] = queue.Queue()
    opened: queue.Queue[str] = queue.Queue()
    state = AppState(tmp_path / "data" / "state.json", Prefs(mode="auto", effort="low"))
    manager = SessionManager(
        config=DriverConfig(cli=("claude.exe",), vault=vault, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda launch, prefs, emit: EchoDriver(launch, calls, emit),
    )
    catalog = FakeCatalog({OLD})
    config = Config(
        vault=vault,
        claude_home=home,
        cli=Path("claude.exe"),
        edge=None,
        data_dir=tmp_path / "data",
    )
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=catalog,
        static_dir=static,
        dev=dev,
        opener=opened.put,
        auth_timeout_s=0.2,
        vault_poll_s=vault_poll_s,
    )
    with TestClient(app, base_url=BASE) as client:
        yield App(client, manager, catalog, state, calls, opened, vault, home)


# --- socket helpers -------------------------------------------------------------------------------


async def _next_message(ws: WebSocketTestSession, timeout: float) -> Message:
    with anyio.fail_after(timeout):
        return await ws._send_rx.receive()


def recv(ws: WebSocketTestSession, timeout: float = 3.0) -> dict[str, Any]:
    """The next server message; raises WebSocketDisconnect on close, TimeoutError if none."""
    message = ws.portal.call(_next_message, ws, timeout)
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000))
    return json.loads(message["text"])


# TestClient.websocket_connect ignores base_url and would present Host "testserver", so every
# socket names the real host itself (tests that check a foreign Host override it).
HOST = "127.0.0.1:8765"


def connect(client: TestClient, origin: str = ORIGIN) -> WebSocketTestSession:
    return client.websocket_connect("/ws", headers={"host": HOST, "origin": origin})


@contextmanager
def authed(client: TestClient) -> Iterator[WebSocketTestSession]:
    with connect(client) as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "v": 2})
        first = recv(ws)  # `live` depends on what the test started earlier
        assert {k: v for k, v in first.items() if k not in {"data", "generation", "seq"}} == {
            "v": 2,
            "type": "vault.sessions",
            "session_id": "",
            "request_id": "",
        }
        assert str(uuid.UUID(first["generation"])) == first["generation"]
        assert type(first["seq"]) is int and first["seq"] >= 0
        assert first["data"]["others"] == []
        yield ws


def refused(client: TestClient, headers: dict[str, str]) -> bool:
    """Whether the WebSocket was turned away before it was accepted."""
    try:
        with client.websocket_connect("/ws", headers={"host": HOST, **headers}):
            return False
    except WebSocketDisconnect:
        return True


def open_session(
    ws: WebSocketTestSession,
    app: App,
    session_id: str,
    *,
    generation: str | None = None,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    request_id = str(uuid.uuid4())
    message: dict[str, Any] = {
        "type": "session.open",
        "session_id": session_id,
        "request_id": request_id,
        "after_seq": after_seq,
    }
    if generation is not None:
        message["generation"] = generation
    ws.send_json(message)
    recovered = []
    while True:
        frame = recv(ws)
        assert frame["v"] == 2 and frame["session_id"] == session_id
        assert frame["request_id"] == request_id
        assert str(uuid.UUID(frame["generation"])) == frame["generation"]
        if frame["type"] == "session.synced":
            actor = app.manager.get(session_id)
            assert actor is not None and frame["seq"] == actor.seq
            return recovered
        recovered.append(frame)


def open_new(ws: WebSocketTestSession, app: App, session_id: str = NEW) -> None:
    recovered = open_session(ws, app, session_id)
    assert len(recovered) == 1
    snapshot = recovered[0]
    assert snapshot["type"] == "session.snapshot" and snapshot["seq"] == 1
    assert snapshot["data"]["state"]["info"] is not None
    assert app.calls.get(timeout=3) == f"open {session_id} resume=False fork_of=None"


def send_command(ws: WebSocketTestSession, app: App, message: dict[str, Any]) -> str:
    actor = app.manager.get(message["session_id"])
    assert actor is not None
    command_id = str(uuid.uuid4())
    ws.send_json({**message, "generation": actor.generation, "command_id": command_id})
    return command_id


def receipt(ws: WebSocketTestSession, command_id: str, status: str = "applied") -> dict[str, Any]:
    frame = recv(ws)
    assert frame["v"] == 2 and frame["type"] == "control.receipt"
    assert frame["command_id"] == command_id and frame["data"]["status"] == status
    return frame


def history(
    ws: WebSocketTestSession, app: App, sid: str, before: str | None = None
) -> dict[str, Any]:
    actor = app.manager.get(sid)
    assert actor is not None
    request_id = str(uuid.uuid4())
    ws.send_json(
        {
            "type": "history.more",
            "session_id": sid,
            "generation": actor.generation,
            "request_id": request_id,
            **({"before_uuid": before} if before else {}),
        }
    )
    frame = recv(ws)
    assert frame["v"] == 2 and frame["type"] == "history.page"
    assert frame["session_id"] == sid and frame["generation"] == actor.generation
    assert frame["request_id"] == request_id
    return frame


# --- HTTP -----------------------------------------------------------------------------------------


def test_http_needs_an_exact_local_host(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        assert app.client.get("/").status_code == 200
        assert app.client.get("/", headers={"host": "localhost:8765"}).status_code == 200
        for host in ("evil.example:8765", "127.0.0.1:9999", "127.0.0.1", "localhost"):
            assert app.client.get("/", headers={"host": host}).status_code == 400, host


def test_every_response_carries_the_security_headers(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        paths = ("/", "/assets/app.js", "/no/such/file")
        for path in (*paths, "/api/sessions", "/api/search?q=x", "/api/rails"):
            headers = app.client.get(path).headers
            assert headers["content-security-policy"] == PLAN_CSP, path
            assert headers["x-content-type-options"] == "nosniff", path
            assert headers["referrer-policy"] == "no-referrer", path
        rest = ("/api/sessions", "/api/search?q=x", "/api/rails")
        for path in rest:
            assert app.client.get(path).headers["cache-control"] == "no-store", path


def test_the_face_route_is_gone(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.get("/api/face/64")
        assert response.status_code == 404
        assert response.headers["content-security-policy"] == PLAN_CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_the_built_frontend_is_served_at_the_root(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        assert "<title>VIKI</title>" in app.client.get("/").text
        assert app.client.get("/assets/app.js").text == "console.log(1)"


# --- WebSocket: admission -------------------------------------------------------------------------


def test_foreign_origins_and_hosts_are_refused_before_accept(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        client = app.client
        assert refused(client, {})
        assert refused(client, {"origin": "https://evil.example"})
        assert refused(client, {"origin": "null"})
        assert refused(client, {"origin": "http://127.0.0.1:5173"})  # the Vite origin needs --dev
        assert refused(client, {"origin": ORIGIN, "host": "evil.example:8765"})
        assert not refused(client, {"origin": "http://127.0.0.1:8765"})
        assert not refused(client, {"origin": "http://localhost:8765", "host": "localhost:8765"})
    with running(tmp_path / "dev", dev=True) as app:
        assert not refused(app.client, {"origin": "http://127.0.0.1:5173"})
        assert not refused(app.client, {"origin": "http://localhost:5173"})
        assert refused(app.client, {"origin": "https://evil.example"})


def test_the_first_message_must_carry_the_token(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        for first in (
            {"type": "auth", "token": "nope"},
            {"type": "auth"},
            {"type": "session.open", "session_id": NEW, "after_seq": 0},
        ):
            with connect(app.client) as ws:
                ws.send_json(first)
                with pytest.raises(WebSocketDisconnect) as closed:
                    recv(ws)
                assert closed.value.code == server.UNAUTHORIZED, first
        with connect(app.client) as ws:  # says nothing: closed after auth_timeout_s
            with pytest.raises(WebSocketDisconnect) as closed:
                recv(ws)
            assert closed.value.code == server.UNAUTHORIZED
        assert app.manager.get(NEW) is None and app.calls.empty()


def test_the_token_and_prompts_never_reach_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running(tmp_path) as app:
        with connect(app.client) as ws:
            ws.send_json({"type": "auth", "token": "wrong-token-value", "v": 2})
            with pytest.raises(WebSocketDisconnect):
                recv(ws)
        with authed(app.client) as ws:
            open_new(ws, app)
            command_id = send_command(
                ws, app, {"type": "turn.send", "session_id": NEW, "text": "my private prompt"}
            )
            assert [recv(ws)["type"] for _ in range(3)] == [
                "user.message",
                "turn.started",
                "status",
            ]
            receipt(ws, command_id)
            ws.send_text('{"type": "turn.send", "session_id": "../../secret-path"}')
            command_id = send_command(ws, app, {"type": "turn.interrupt", "session_id": NEW})
            assert recv(ws)["data"]["state"] == "interrupt"
            receipt(ws, command_id, "unknown")
    for secret in (TOKEN, "wrong-token-value", "my private prompt", "secret-path"):
        assert secret not in caplog.text, secret


# --- WebSocket: sessions --------------------------------------------------------------------------


def test_a_new_session_starts_at_once_and_streams(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        open_new(ws, app)
        command_id = send_command(ws, app, {"type": "turn.send", "session_id": NEW, "text": "hi"})
        got = [recv(ws) for _ in range(3)]
        assert [(m["seq"], m["type"]) for m in got] == [
            (2, "user.message"),
            (3, "turn.started"),
            (4, "status"),
        ]
        actor = app.manager.get(NEW)
        assert actor is not None
        assert all(m["v"] == 2 and m["generation"] == actor.generation for m in got)
        assert len({m["request_id"] for m in got}) == 1
        assert got[0]["data"]["text"] == "hi" and got[2]["data"]["state"] == "send hi"
        receipt(ws, command_id)
        assert app.calls.get(timeout=3) == "send hi"
        assert app.state.ours(NEW)


def test_a_socket_gets_what_it_missed_then_follows_live_once(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        with authed(app.client) as first:
            open_new(first, app)
            command_id = send_command(
                first, app, {"type": "turn.send", "session_id": NEW, "text": "one"}
            )
            assert [recv(first)["seq"] for _ in range(3)] == [2, 3, 4]
            receipt(first, command_id)
            actor = app.manager.get(NEW)
            assert actor is not None
            generation = actor.generation
        with authed(app.client) as second:
            replay = open_session(second, app, NEW, generation=generation, after_seq=2)
            assert [(m["seq"], m["type"]) for m in replay] == [(3, "turn.started"), (4, "status")]
            command_id = send_command(
                second, app, {"type": "turn.send", "session_id": NEW, "text": "two"}
            )
            assert [recv(second)["seq"] for _ in range(3)] == [5, 6, 7]
            receipt(second, command_id)
            assert open_session(second, app, NEW, generation=generation, after_seq=7) == []
            command_id = send_command(
                second, app, {"type": "turn.send", "session_id": NEW, "text": "three"}
            )
            assert [recv(second)["seq"] for _ in range(3)] == [8, 9, 10]
            receipt(second, command_id)


def test_an_existing_transcript_opens_lazily_with_history(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        snapshot = open_session(ws, app, OLD)
        assert snapshot[0]["data"]["state"]["info"] is None
        page = history(ws, app, OLD)
        assert page["data"] == {
            "events": [
                {
                    "type": "user.message",
                    "data": {"text": "older", "command": "", "at": "2026-09-01T00:00:00.000Z"},
                }
            ],
            "next_before_uuid": U1,
        }
        assert app.catalog.asked.get(timeout=3) == (OLD, None, None)
        assert app.calls.empty()
        history(ws, app, OLD, U1)
        assert app.catalog.asked.get(timeout=3) == (OLD, U1, None)
        command_id = send_command(
            ws, app, {"type": "turn.send", "session_id": OLD, "text": "continue"}
        )
        assert [recv(ws)["type"] for _ in range(4)] == [
            "session.started",
            "user.message",
            "turn.started",
            "status",
        ]
        receipt(ws, command_id)
        assert app.calls.get(timeout=3) == f"open {OLD} resume=True fork_of=None"
        assert app.calls.get(timeout=3) == "send continue"


def test_history_stops_where_the_live_log_begins(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        open_new(ws, app)
        actor = app.manager.get(NEW)
        assert actor is not None and actor.since
        history(ws, app, NEW, U1)
        assert app.catalog.asked.get(timeout=3) == (NEW, U1, actor.since)


def test_a_fork_runs_under_its_own_id_and_reads_its_source(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        open_session(ws, app, OLD)
        command_id = send_command(
            ws, app, {"type": "session.fork", "session_id": OLD, "fork_id": FORK}
        )
        result = receipt(ws, command_id)
        assert result["data"]["target"]["session_id"] == FORK and app.calls.empty()
        open_session(ws, app, FORK)
        command_id = send_command(
            ws, app, {"type": "turn.send", "session_id": FORK, "text": "branch"}
        )
        got = [recv(ws) for _ in range(4)]
        assert [(m["seq"], m["type"]) for m in got] == [
            (1, "session.started"),
            (2, "user.message"),
            (3, "turn.started"),
            (4, "status"),
        ]
        assert all(m["session_id"] == FORK for m in got)
        receipt(ws, command_id)
        assert app.calls.get(timeout=3) == f"open {FORK} resume=False fork_of={OLD}"
        assert app.calls.get(timeout=3) == "send branch"
        fork = app.manager.get(FORK)
        assert fork is not None and fork.history_source == OLD
        history(ws, app, FORK)
        assert app.catalog.asked.get(timeout=3) == (OLD, None, fork.since)
        command_id = send_command(
            ws, app, {"type": "session.fork", "session_id": OLD, "fork_id": FORK}
        )
        assert receipt(ws, command_id, "rejected")["data"]["reason"] == "conflict"
        command_id = send_command(ws, app, {"type": "turn.interrupt", "session_id": FORK})
        assert recv(ws)["data"]["state"] == "interrupt"
        receipt(ws, command_id, "unknown")
        assert app.calls.get(timeout=3) == "interrupt" and app.calls.empty()


def test_commands_reach_the_session(tmp_path: Path) -> None:
    from viki_agent.events import PermissionRequested, QuestionAsked

    with running(tmp_path) as app, authed(app.client) as ws:
        open_new(ws, app)
        command_id = send_command(ws, app, {"type": "turn.interrupt", "session_id": NEW})
        assert recv(ws)["type"] == "status"
        receipt(ws, command_id, "unknown")
        assert app.calls.get(timeout=3) == "interrupt"
        actor = app.manager.get(NEW)
        assert actor is not None
        choices = [
            ("q1", QuestionAnswer(answers={"Tea or coffee?": "Tea"})),
            ("q2", QuestionAnswer(answers={}, response="neither")),
            ("p1", PermissionAnswer(allow=False)),
        ]
        for tool_id, answer in choices:
            event = (
                QuestionAsked(tool_use_id=tool_id, questions=[])
                if isinstance(answer, QuestionAnswer)
                else PermissionRequested(
                    tool_use_id=tool_id, kind="tool", tool="Read", summary="read fixture"
                )
            )
            ws.portal.call(actor._emit, event)
            card = recv(ws)
            message = (
                {
                    "type": "question.answer",
                    "session_id": NEW,
                    "tool_use_id": tool_id,
                    "native_request": card["native_request"],
                    "answers": answer.answers,
                    "response": answer.response,
                }
                if isinstance(answer, QuestionAnswer)
                else {
                    "type": "permission.respond",
                    "session_id": NEW,
                    "tool_use_id": tool_id,
                    "native_request": card["native_request"],
                    "allow": answer.allow,
                }
            )
            command_id = send_command(ws, app, message)
            assert recv(ws)["type"] == "status"
            receipt(ws, command_id)
            assert app.calls.get(timeout=3) == f"respond {tool_id} {answer!r}"
        for model in ("opus", "sonnet"):
            command_id = send_command(
                ws, app, {"type": "session.set_model", "session_id": NEW, "model": model}
            )
            assert recv(ws)["type"] == "status"
            receipt(ws, command_id, "unknown")
            assert app.calls.get(timeout=3) == f"model {model}"
        command_id = send_command(
            ws, app, {"type": "session.set_effort", "session_id": NEW, "effort": "max"}
        )
        receipt(ws, command_id)
        command_id = send_command(
            ws, app, {"type": "session.set_effort", "session_id": NEW, "effort": "turbo"}
        )
        receipt(ws, command_id, "rejected")
        assert app.state.prefs(NEW).effort == "max"


def test_closing_a_session_forgets_it_and_stops_its_stream_here(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        open_new(ws, app)
        command_id = send_command(ws, app, {"type": "session.close", "session_id": NEW})
        assert recv(ws)["type"] == "session.closed"
        receipt(ws, command_id)
        assert app.calls.get(timeout=3) == "close"
        assert app.manager.get(NEW) is None
        open_session(ws, app, OLD)
        history(ws, app, OLD)


def test_invalid_messages_are_dropped_and_the_socket_stays_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    # Each field violation starts from a valid v2 frame. Missing identities remain
    # separate cases so they cannot hide a broken payload validator.
    identity = {"session_id": NEW, "generation": U1, "command_id": FORK}
    opening = {"type": "session.open", "session_id": NEW, "request_id": U1}
    native = {"runtime_id": NEW, "request_id": U1, "turn_id": FORK}
    bad: list[Any] = [
        [1, 2],
        {"type": "no.such.type", "session_id": NEW},
        {"type": "turn.send", "session_id": NEW},
        {"type": "session.open", "session_id": NEW},
        {**opening, "after_seq": "3"},
        {**opening, "after_seq": -1},
        {**opening, "session_id": "../../etc"},
        {**opening, "session_id": NEW.upper()},
        {"type": "turn.send", **identity},
        {"type": "session.fork", **identity, "fork_id": "not-a-uuid"},
        {
            "type": "permission.respond",
            **identity,
            "tool_use_id": "p",
            "native_request": native,
            "allow": "yes",
        },
        {
            "type": "question.answer",
            **identity,
            "tool_use_id": "q",
            "native_request": native,
            "answers": {"a": 1},
        },
        {
            "type": "history.more",
            "session_id": OLD,
            "generation": U1,
            "request_id": FORK,
            "before_uuid": "x",
        },
    ]
    assert server._validate(opening) is not None
    for message in bad:
        assert server._validate(message) is None
    with running(tmp_path) as app, authed(app.client) as ws:
        ws.send_text("not json")
        for message in bad:
            ws.send_json(message)
        open_new(ws, app, FORK)  # the socket still works
        assert app.calls.empty()
        assert app.manager.get(NEW) is None and app.manager.get(OLD) is None
        assert app.catalog.asked.empty()
    assert "../../etc" not in caplog.text


def test_open_url_passes_only_web_and_mail_links(tmp_path: Path) -> None:
    good = ["https://example.com/a?b=1#c", "http://example.com", "mailto:someone@example.com"]
    bad: list[Any] = [
        "javascript:alert(1)",
        # Added 2026-09-13 (no assertion changed): a scheme with no authority.
        # The renderer used to accept it as a scheme and draw the link, which this side then
        # dropped on click — the two must refuse the same things, not merely overlap.
        "http:evil",
        "https:evil",
        "file:///C:/Windows/win.ini",
        "vscode://file/C:/x.md",
        "ms-settings:",
        "//example.com",
        "https://exa mple.com",
        "https://example.com/\n",
        "\x00https://example.com",
        "",
        42,
    ]
    with running(tmp_path) as app, authed(app.client) as ws:
        for url in [*bad, *good]:
            ws.send_json({"type": "open.url", "url": url})
        assert sorted(app.opened.get(timeout=3) for _ in good) == sorted(good)
        assert app.opened.empty()


def test_open_vault_opens_only_existing_files_inside_the_vault(tmp_path: Path) -> None:
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    with running(tmp_path) as app, authed(app.client) as ws:
        target = "vscode://file/" + (app.vault / "30-knowledge" / "x.md").resolve().as_posix()
        for path in (
            "../outside.md",
            str(tmp_path / "outside.md"),
            "30-knowledge/missing.md",
            "30-knowledge",
            "30-knowledge/x.md",
        ):
            ws.send_json({"type": "open.vault", "path": path})
        assert app.opened.get(timeout=3) == target
        ws.send_json({"type": "open.vault", "path": "30-knowledge\\x.md"})
        assert app.opened.get(timeout=3) == target
        assert app.opened.empty()


def test_open_transcript_opens_the_session_record_and_only_a_real_one(tmp_path: Path) -> None:
    """The row menu's third item. The client names a session, never a path: an id with no
    transcript opens nothing, and no message can steer the opener at a file of its choosing."""
    with running(tmp_path) as app, authed(app.client) as ws:
        ws.send_json({"type": "open.transcript", "session_id": NEW})  # no transcript
        ws.send_json({"type": "open.transcript", "session_id": OLD})
        expected = app.catalog.transcript_path(OLD)
        assert expected is not None
        assert app.opened.get(timeout=3) == "vscode://file/" + expected.as_posix()
        assert app.opened.empty()


def test_vault_sessions_arrive_on_connect_and_on_change(tmp_path: Path) -> None:
    with running(tmp_path, vault_poll_s=0.05) as app, authed(app.client) as ws:
        me = os.getpid()
        registry = app.home / "sessions" / f"{me}.json"
        entry = {
            "pid": me,
            "sessionId": OLD,
            "cwd": str(app.vault),
            "procStart": str(winproc.process_start_time(me)),
            "entrypoint": "claude-vscode",
            "name": "notes",
        }
        registry.write_text(json.dumps(entry), encoding="utf-8")
        update = recv(ws)
        assert update["v"] == 2 and update["type"] == "vault.sessions"
        assert update["session_id"] == "" and update["request_id"] == ""
        assert str(uuid.UUID(update["generation"])) == update["generation"]
        assert update["seq"] > 0
        assert update["data"] == {
            "others": [
                {"session_id": OLD, "entrypoint": "claude-vscode", "pid": me, "title": "notes"}
            ],
            "live": [],
        }
        registry.unlink()
        assert recv(ws)["data"] == {"others": [], "live": []}


def test_a_reloaded_page_learns_which_sessions_are_live_here(tmp_path: Path) -> None:
    with running(tmp_path, vault_poll_s=0.05) as app:
        with authed(app.client) as ws:
            open_new(ws, app)
            assert recv(ws)["data"] == {"others": [], "live": [NEW]}  # the poll saw it start
        with connect(app.client) as ws:  # the reload: a fresh socket, nothing tracked yet
            ws.send_json({"type": "auth", "token": TOKEN, "v": 2})
            assert recv(ws)["data"] == {"others": [], "live": [NEW]}


def test_shutdown_closes_every_live_session(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        with authed(app.client) as ws:
            open_new(ws, app)
        calls = app.calls
    assert calls.get(timeout=3) == "close"  # the TestClient ran the app's shutdown


# --- REST ----------------------------------------------------------------------------------

BEARER = {"authorization": f"Bearer {TOKEN}"}


def test_rest_routes_need_the_bearer_token(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        for path in ("/api/sessions", "/api/search?q=x", "/api/rails"):
            joiner = "&" if "?" in path else "?"
            for headers, url in (
                ({}, path),
                ({"authorization": "Bearer nope"}, path),
                ({"authorization": TOKEN}, path),
                ({"authorization": f"bearer {TOKEN}"}, path),
                ({}, f"{path}{joiner}token={TOKEN}"),  # a query-string token is not auth
            ):
                response = app.client.get(url, headers=headers)
                assert response.status_code == 401, (url, headers)
                assert response.json() == {"error": "unauthorized"}
            response = app.client.get(path, headers=BEARER)
            assert response.status_code == 200, path
            assert response.headers["content-type"] == "application/json"


def test_sessions_lists_the_catalog_rows_off_the_event_loop(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.get("/api/sessions", headers=BEARER)
        assert response.status_code == 200
        assert response.json() == {
            "sessions": [asdict(row) for row in app.catalog.sessions()],
            "state": "ready",
            "reason": "",
            "omitted": 0,
        }
        assert response.content.isascii()  # the one encoder: a lone surrogate cannot kill it
        assert app.catalog.off_loop.get(timeout=3) is True


def test_search_needs_a_query_and_passes_it_through(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        for path in ("/api/search", "/api/search?q=", "/api/search?q=%20%20"):
            response = app.client.get(path, headers=BEARER)
            assert response.status_code == 400, path
            assert response.json() == {"error": "q is required"}
        assert app.catalog.searched.empty()  # a blank query never reaches the catalog
        response = app.client.get("/api/search", params={"q": " 牛乳 買う "}, headers=BEARER)
        assert response.status_code == 200
        assert app.catalog.searched.get(timeout=3) == " 牛乳 買う "  # untouched: the catalog trims
        assert response.json() == {
            "hits": [asdict(hit) for hit in app.catalog.search(" 牛乳 買う ")],
            "state": "ready",
            "reason": "",
            "scanned": 1,
            "total": 1,
            "request_id": "",
        }
        assert app.catalog.off_loop.get(timeout=3) is True


def test_rails_are_read_in_a_thread_from_the_vault_and_its_project_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: queue.Queue[tuple[bool, Path, Path]] = queue.Queue()

    def fake_read(vault: Path, project: Path) -> Rails:
        seen.put((_off_loop(), vault, project))
        return Rails(
            agents=[],
            gates=[],
            routines=rails.routines(),
            counts=VaultCounts(knowledge=1, sources=0, inbox=0),
            git=GitChip(branch="", head="", committed_at="", dirty=None, state="not_repository"),
        )

    monkeypatch.setattr(rails, "rails_read", fake_read)
    with running(tmp_path) as app:
        response = app.client.get("/api/rails", headers=BEARER)
        assert response.status_code == 200
        assert response.json() == {
            "agents": [],
            "gates": [],
            "routines": [asdict(row) for row in rails.routines()],
            "counts": {"knowledge": 1, "sources": 0, "inbox": 0},
            "git": {
                "branch": "",
                "head": "",
                "committed_at": "",
                "dirty": None,
                "state": "not_repository",
                "ref_state": "unknown",
            },
        }
        off_loop, vault, project = seen.get(timeout=3)
        assert off_loop is True
        assert vault == tmp_path / "vault"
        assert project == project_dir(tmp_path / "home", tmp_path / "vault")


def test_rails_on_the_test_vault_are_real(tmp_path: Path) -> None:
    """No fake: the tmp vault running() builds has one knowledge note, no agents, no retro
    reports and no git repo."""
    with running(tmp_path) as app:
        body = app.client.get("/api/rails", headers=BEARER).json()
        assert body["counts"] == {"knowledge": 1, "sources": 0, "inbox": 0}
        assert body["agents"] == [] and body["gates"] == []
        assert body["git"] == {
            "branch": "",
            "head": "",
            "committed_at": "",
            "dirty": None,
            "state": "not_repository",
            "ref_state": "unknown",
        }
