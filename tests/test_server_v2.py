"""Adversarial wire tests for actor, subscription and native-request identity.

Only synthetic drivers and temporary app-owned storage are used. The client speaks the raw
protocol, so a browser helper cannot accidentally hide a missing server-side guard.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.test_server import (
    BEARER,
    NEW,
    ORIGIN,
    TOKEN,
    WS_HEADERS,
    EchoDriver,
    EmptyCatalog,
    recv,
    running,
)
from viki_agent import server
from viki_agent.catalog import HistoryPage
from viki_agent.conversations import approvals as approvals_module
from viki_agent.conversations.approvals import ApprovalError
from viki_agent.drivers.base import Answer
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import (
    Event,
    PermissionRequested,
    RequestAnswered,
    TextDone,
    UserMessage,
    history_item,
)
from viki_agent.session import SessionActor
from viki_agent.state import Prefs
from viki_agent.storage import StateError

FORK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# The one allowed exception to the 50 ms wait bound: frames behind a durable SQLite COMMIT
# (session registration, and the approval and command ledgers) can stall well past 50 ms on
# Windows disk I/O, even though they normally arrive in well under 50 ms. Every wait for a
# frame or a disconnect uses this guard; an in-process barrier wait keeps the 50 ms bound,
# since nothing there depends on disk I/O.
HANG_GUARD_S = 5


def identity() -> str:
    return str(uuid.uuid4())


def until(
    ws: WebSocketTestSession,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    received = []
    for _ in range(64):
        frame = recv(ws, timeout=HANG_GUARD_S)
        received.append(frame)
        if predicate(frame):
            return received
    raise AssertionError("expected frame did not arrive within the bounded message count")


@contextmanager
def connected(client: TestClient) -> Iterator[tuple[WebSocketTestSession, dict[str, Any]]]:
    with client.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "v": 2})
        initial = recv(ws, timeout=HANG_GUARD_S)
        assert initial["type"] == "vault.sessions"
        assert initial["v"] == 2
        assert initial["session_id"] == initial["request_id"] == ""
        assert str(uuid.UUID(initial["generation"])) == initial["generation"]
        yield ws, initial


def subscribe(
    ws: WebSocketTestSession,
    session_id: str = NEW,
    *,
    generation: str | None = None,
    after_seq: int | None = None,
) -> list[dict[str, Any]]:
    request_id = identity()
    message: dict[str, Any] = {
        "type": "session.open",
        "session_id": session_id,
        "request_id": request_id,
    }
    if generation is not None:
        message["generation"] = generation
    if after_seq is not None:
        message["after_seq"] = after_seq
    ws.send_json(message)
    frames = until(
        ws, lambda frame: frame["type"] == "session.synced" and frame["request_id"] == request_id
    )
    for frame in frames:
        if frame["session_id"] == session_id:
            assert frame["v"] == 2
            assert frame["request_id"] == request_id
            assert frame["generation"] == frames[-1]["generation"]
    return frames


APPROVAL_CODES = frozenset({"capacity", "already_decided", "protection"})


def ledger(app: Any) -> Any:
    """The durable approval store behind this app's socket seam."""
    return app.client.app.state.approvals.service


def mutation(
    type_: str, generation: str, *, session_id: str = NEW, **fields: Any
) -> dict[str, Any]:
    return {
        "type": type_,
        "session_id": session_id,
        "generation": generation,
        "command_id": identity(),
        **fields,
    }


def receipt(ws: WebSocketTestSession, command: dict[str, Any]) -> dict[str, Any]:
    return until(
        ws,
        lambda frame: (
            frame["type"] == "control.receipt" and frame["command_id"] == command["command_id"]
        ),
    )[-1]


class RecordingDriver(EchoDriver):
    def __init__(self, launch: Launch, emit: Callable[[Event], None]) -> None:
        super().__init__(launch, emit)
        self.calls: list[tuple[str, Any]] = []
        self.requests: set[str] = set()

    async def send(self, text: str) -> None:
        self.calls.append(("send", text))

    async def interrupt(self) -> None:
        self.calls.append(("interrupt", None))

    async def set_model(self, model: str) -> None:
        self.calls.append(("model", model))

    def ask(self, tool_use_id: str = "same-native-id") -> None:
        self.requests.add(tool_use_id)
        self._emit(
            PermissionRequested(
                tool_use_id=tool_use_id, kind="tool", tool="Read", summary="synthetic file"
            )
        )

    def respond(self, tool_use_id: str, answer: Answer) -> bool:
        self.calls.append(("respond", answer))
        if tool_use_id not in self.requests:
            return False
        self.requests.remove(tool_use_id)
        self._emit(RequestAnswered(tool_use_id=tool_use_id))
        return True

    def pending(self) -> list[str]:
        return list(self.requests)


class RecordingDrivers:
    def __init__(self) -> None:
        self.made: list[RecordingDriver] = []

    def __call__(
        self, launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]
    ) -> RecordingDriver:
        driver = RecordingDriver(launch, emit)
        self.made.append(driver)
        return driver


@pytest.mark.parametrize("version", [None, 1, 3, True, "2"])
def test_authenticated_obsolete_or_invalid_protocol_is_refused(
    tmp_path: Path, version: Any
) -> None:
    with running(tmp_path) as app, app.client.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        auth: dict[str, Any] = {"type": "auth", "token": TOKEN}
        if version is not None:
            auth["v"] = version
        ws.send_json(auth)
        with pytest.raises(WebSocketDisconnect) as refused:
            recv(ws, timeout=HANG_GUARD_S)
        assert refused.value.code == 4406


def test_escaped_surrogate_in_auth_token_is_unauthorized_without_server_exception(
    tmp_path: Path,
) -> None:
    with running(tmp_path) as app, app.client.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_text('{"type":"auth","token":"\\ud800","v":2}')
        with pytest.raises(WebSocketDisconnect) as refused:
            recv(ws, timeout=HANG_GUARD_S)
        assert refused.value.code == 4401


def test_global_stream_identity_is_shared_within_launch_and_changes_on_restart(
    tmp_path: Path,
) -> None:
    with (
        running(tmp_path / "first") as app,
        connected(app.client) as (_, first),
        connected(app.client) as (_, second),
    ):
        assert (first["generation"], first["seq"]) == (second["generation"], second["seq"])
    with running(tmp_path / "second") as app, connected(app.client) as (_, restarted):
        assert restarted["generation"] != first["generation"]


def test_new_connection_snapshot_cannot_hide_live_change_from_existing_windows() -> None:
    live: list[str] = []
    hub = server._VaultHub(live=lambda: list(live))
    received: list[dict[str, Any]] = []
    hub.subscribe(received.append)
    live.append(NEW)
    newcomer = hub.snapshot()
    assert newcomer["data"]["live"] == [NEW]
    hub.update([])
    assert len(received) == 1
    assert received[0] == newcomer


@pytest.mark.parametrize("replacement_events", [0, 12])
def test_replacement_recovers_when_new_cursor_is_below_or_above_old_cursor(
    tmp_path: Path, replacement_events: int
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app:
        with connected(app.client) as (ws, _):
            original = subscribe(ws)[-1]
            old_seq = original["seq"]
            for index in range(5):
                ws.portal.call(
                    drivers.made[-1]._emit, TextDone(block_id=f"old-{index}", text="old")
                )
                old_seq = until(ws, lambda frame: frame["type"] == "text.done")[-1]["seq"]
            ws.portal.call(app.manager.close, NEW)
            actor = ws.portal.call(app.manager.create, NEW)
            ws.portal.call(actor.start)
            for index in range(replacement_events):
                ws.portal.call(
                    drivers.made[-1]._emit, TextDone(block_id=f"new-{index}", text="replacement")
                )
        with connected(app.client) as (ws, _):
            frames = subscribe(ws, generation=original["generation"], after_seq=old_seq)
            session_frames = [frame for frame in frames if frame["session_id"] == NEW]
            assert session_frames[0]["type"] == "session.snapshot"
            assert session_frames[-1]["type"] == "session.synced"
            assert session_frames[-1]["generation"] == actor.generation
            assert actor.generation != original["generation"]
            assert session_frames[-1]["seq"] == actor.seq
            assert all(frame["type"] != "session.closed" for frame in session_frames)
            assert len(drivers.made) == 2


def test_replay_and_later_live_frames_use_the_latest_subscription_request(tmp_path: Path) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        initial = subscribe(ws)[-1]
        ws.portal.call(drivers.made[0]._emit, TextDone(block_id="first", text="first"))
        first_live = until(ws, lambda frame: frame["type"] == "text.done")[-1]
        assert first_live["request_id"] == initial["request_id"]
        replay = subscribe(ws, generation=initial["generation"], after_seq=initial["seq"])
        assert [frame["type"] for frame in replay if frame["session_id"] == NEW] == [
            "text.done",
            "session.synced",
        ]
        assert replay[-1]["request_id"] != initial["request_id"]
        ws.portal.call(drivers.made[0]._emit, TextDone(block_id="second", text="second"))
        second_live = until(ws, lambda frame: frame["type"] == "text.done")[-1]
        assert second_live["request_id"] == replay[-1]["request_id"]
        assert second_live["generation"] == initial["generation"]


@pytest.mark.parametrize(
    ("type_", "fields"),
    [
        ("turn.send", {"text": "must not run"}),
        ("turn.interrupt", {}),
        ("session.set_model", {"model": "default"}),
        ("session.set_effort", {"effort": "high"}),
        ("session.close", {}),
        ("session.fork", {"fork_id": FORK}),
    ],
)
def test_old_generation_cannot_mutate_replacement_actor(
    tmp_path: Path, type_: str, fields: dict[str, Any]
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        original = subscribe(ws)[-1]
        ws.portal.call(app.manager.close, NEW)
        replacement = ws.portal.call(app.manager.create, NEW)
        ws.portal.call(replacement.start)
        command = mutation(type_, original["generation"], **fields)
        ws.send_json(command)
        result = receipt(ws, command)
        assert result["data"]["status"] == "rejected"
        assert drivers.made[-1].calls == []
        assert app.manager.get(NEW) is replacement
        assert app.manager.get(FORK) is None
        assert app.manager.app_state.prefs(NEW).effort == "low"


@pytest.mark.parametrize("altered", ["runtime_id", "request_id", "turn_id"])
def test_forged_native_request_component_cannot_authorize_current_card(
    tmp_path: Path, altered: str
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        driver = drivers.made[-1]
        ws.portal.call(driver.ask)
        card = until(ws, lambda frame: frame["type"] == "permission.requested")[-1]
        binding = card["native_request"]
        assert set(binding) == {"runtime_id", "request_id", "turn_id"}
        assert all(str(uuid.UUID(value)) == value for value in binding.values())
        forged = {**binding, altered: identity()}
        rejected = mutation(
            "permission.respond",
            target["generation"],
            tool_use_id="same-native-id",
            native_request=forged,
            allow=True,
        )
        ws.send_json(rejected)
        assert receipt(ws, rejected)["data"]["status"] == "rejected"
        assert driver.calls == []
        assert driver.pending() == ["same-native-id"]
        assert ledger(app).list().approvals == []  # a forged binding records nothing
        accepted = mutation(
            "permission.respond",
            target["generation"],
            tool_use_id="same-native-id",
            native_request=binding,
            allow=True,
        )
        ws.send_json(accepted)
        frames = until(
            ws,
            lambda frame: (
                frame["type"] == "control.receipt" and frame["command_id"] == accepted["command_id"]
            ),
        )
        assert frames[-1]["data"]["status"] == "applied"
        answered = [frame for frame in frames if frame["type"] == "request.answered"]
        assert len(answered) == 1
        assert answered[0]["native_request"] == binding
        assert len(driver.calls) == 1


def test_answer_for_reused_native_id_cannot_answer_new_request_in_same_runtime(
    tmp_path: Path,
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        driver = drivers.made[-1]
        ws.portal.call(driver.ask)
        first = until(ws, lambda frame: frame["type"] == "permission.requested")[-1]
        ws.portal.call(driver.ask)
        second = until(ws, lambda frame: frame["type"] == "permission.requested")[-1]
        assert first["native_request"]["runtime_id"] == second["native_request"]["runtime_id"]
        assert first["native_request"]["request_id"] != second["native_request"]["request_id"]
        stale = mutation(
            "permission.respond",
            target["generation"],
            tool_use_id="same-native-id",
            native_request=first["native_request"],
            allow=True,
        )
        ws.send_json(stale)
        assert receipt(ws, stale)["data"]["status"] == "rejected"
        assert driver.calls == []
        assert driver.pending() == ["same-native-id"]
        assert ledger(app).list().approvals == []  # a retired request records nothing


def test_missing_native_binding_is_dropped_without_resolving_card(tmp_path: Path) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        driver = drivers.made[-1]
        ws.portal.call(driver.ask)
        until(ws, lambda frame: frame["type"] == "permission.requested")
        ws.send_json(
            mutation(
                "permission.respond", target["generation"], tool_use_id="same-native-id", allow=True
            )
        )
        # A later correlated read supplies an observable barrier without relying on silence.
        subscribe(ws, generation=target["generation"], after_seq=target["seq"])
        assert driver.calls == []
        assert driver.pending() == ["same-native-id"]
        assert ledger(app).list().approvals == []  # dropped before the seam, so nothing is saved


def test_identical_control_retry_returns_receipt_without_second_native_send(tmp_path: Path) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        command = mutation("turn.send", target["generation"], text="one native call")
        ws.send_json(command)
        first = receipt(ws, command)
        assert first["data"]["status"] == "applied"
        # Object key ordering is not a different command.
        ws.send_json(dict(reversed(list(command.items()))))
        assert receipt(ws, command) == first
        assert drivers.made[-1].calls == [("send", "one native call")]
        ws.send_json({**command, "text": "changed under same id"})
        conflict = receipt(ws, command)
        assert conflict["data"]["status"] == "rejected"
        assert drivers.made[-1].calls == [("send", "one native call")]


def test_old_card_cannot_authorize_replacement_driver_within_same_actor(tmp_path: Path) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        ws.portal.call(drivers.made[-1].ask)
        original = until(ws, lambda frame: frame["type"] == "permission.requested")[-1]
        effort = mutation("session.set_effort", target["generation"], effort="high")
        ws.send_json(effort)
        assert receipt(ws, effort)["data"]["status"] == "applied"
        send = mutation("turn.send", target["generation"], text="explicit next turn")
        ws.send_json(send)
        assert receipt(ws, send)["data"]["status"] == "applied"
        assert len(drivers.made) == 2
        replacement = drivers.made[-1]
        ws.portal.call(replacement.ask)
        new_card = until(ws, lambda frame: frame["type"] == "permission.requested")[-1]
        assert new_card["generation"] == original["generation"]
        assert new_card["native_request"]["runtime_id"] != original["native_request"]["runtime_id"]
        stale = mutation(
            "permission.respond",
            target["generation"],
            tool_use_id="same-native-id",
            native_request=original["native_request"],
            allow=True,
        )
        ws.send_json(stale)
        assert receipt(ws, stale)["data"]["status"] == "rejected"
        assert replacement.calls == [("send", "explicit next turn")]
        assert replacement.pending() == ["same-native-id"]
        assert ledger(app).list().approvals == []  # the old card records nothing either


def test_fork_receipt_names_target_before_target_subscription_and_first_send(
    tmp_path: Path,
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        source = subscribe(ws)[-1]
        fork = mutation("session.fork", source["generation"], fork_id=FORK)
        ws.send_json(fork)
        admitted = receipt(ws, fork)
        assert admitted["data"]["status"] == "applied"
        target = admitted["data"]["target"]
        assert target["session_id"] == FORK
        assert target["generation"] != source["generation"]
        assert len(drivers.made) == 1
        synced = subscribe(ws, FORK, generation=target["generation"])[-1]
        assert synced["generation"] == target["generation"]
        turn = mutation("turn.send", target["generation"], session_id=FORK, text="fork only")
        ws.send_json(turn)
        assert receipt(ws, turn)["data"]["status"] != "rejected"
        assert drivers.made[0].calls == []
        assert drivers.made[-1].launch.fork_of == NEW
        assert drivers.made[-1].calls == [("send", "fork only")]


class DelayedCatalog(EmptyCatalog):
    def __init__(self, home: Path, vault: Path) -> None:
        super().__init__(home, vault)
        self.history_started = threading.Event()
        self.release_history = threading.Event()
        self.first_history = True

    def history(
        self, session_id: str, *, before_uuid: str | None = None, until: str | None = None
    ) -> HistoryPage:
        if self.first_history:
            self.first_history = False
            self.history_started.set()
            self.release_history.wait()
            text = "old generation history"
        else:
            text = "replacement history"
        return HistoryPage(events=[UserMessage(text=text)], next_before_uuid=None)


def test_delayed_history_never_acquires_replacement_generation_or_request(tmp_path: Path) -> None:
    catalog = DelayedCatalog(tmp_path / "home", tmp_path / "vault")
    with running(tmp_path, catalog=catalog) as app, connected(app.client) as (ws, _):
        original = subscribe(ws)[-1]
        old_request = identity()
        ws.send_json(
            {
                "type": "history.more",
                "session_id": NEW,
                "generation": original["generation"],
                "request_id": old_request,
            }
        )
        try:
            assert catalog.history_started.wait(timeout=0.05)
            ws.portal.call(app.manager.close, NEW)
            until(ws, lambda frame: frame["type"] == "session.closed")
            replacement = ws.portal.call(app.manager.create, NEW)
            recovered = subscribe(ws, generation=original["generation"], after_seq=original["seq"])
            assert recovered[-1]["generation"] == replacement.generation
        finally:
            catalog.release_history.set()
        ws.portal.call(finish_socket_commands)
        current_request = identity()
        ws.send_json(
            {
                "type": "history.more",
                "session_id": NEW,
                "generation": replacement.generation,
                "request_id": current_request,
            }
        )
        frames = until(
            ws,
            lambda frame: (
                frame["type"] == "history.page" and frame["request_id"] == current_request
            ),
        )
        assert frames[-1]["generation"] == replacement.generation
        assert frames[-1]["data"]["events"][0]["data"]["text"] == "replacement history"
        for frame in frames[:-1]:
            if frame["type"] == "history.page":
                assert frame["request_id"] == old_request
                assert frame["generation"] == original["generation"]


class StatusCatalog(EmptyCatalog):
    """history() answers with one configured page. `existing` decides what kind of actor the
    socket's open produces: a resumed one, which should already have a transcript, or a
    brand-new session, whose transcript the CLI only writes on the first turn."""

    def __init__(self, home: Path, vault: Path, page: HistoryPage, *, existing: bool) -> None:
        super().__init__(home, vault)
        self.page = page
        self.existing = existing

    def exists(self, session_id: str) -> bool:
        return self.existing

    def history(self, session_id: str, **_kwargs: Any) -> HistoryPage:
        return self.page


def history_data(ws: WebSocketTestSession, target: dict[str, Any]) -> dict[str, Any]:
    """Ask for one history page and return its `data`."""
    request_id = identity()
    ws.send_json(
        {
            "type": "history.more",
            "session_id": target["session_id"],
            "generation": target["generation"],
            "request_id": request_id,
        }
    )
    page = until(
        ws,
        lambda frame: frame["type"] == "history.page" and frame["request_id"] == request_id,
    )[-1]
    return page["data"]


def test_history_page_maps_every_catalog_status(tmp_path: Path) -> None:
    """Every answer the catalog can give becomes a page the conversation can act on: events with
    their cursor, the same shape plus the note that older turns are past the tail budget, or one
    of the named errors. A missing transcript is only a loss where one should exist — for a
    brand-new session the file appears on the first turn, and an empty complete page is the
    truthful answer."""
    older = UserMessage(text="older", at="2026-09-01T00:00:00.000Z")
    item = history_item(older)  # the events pass through the mapping untouched
    cases: list[tuple[str, HistoryPage, bool, dict[str, Any]]] = [
        (
            "a page with more behind it",
            HistoryPage(events=[older], next_before_uuid=FORK),
            True,
            {"events": [item], "next_before_uuid": FORK},
        ),
        (
            "a page read from the transcript's newest window",
            HistoryPage(
                events=[older], next_before_uuid=None, status="partial", reason="tail_budget"
            ),
            True,
            {"events": [item], "next_before_uuid": None, "partial": "tail_budget"},
        ),
        (
            "the transcript of a resumed session is gone",
            HistoryPage(events=[], next_before_uuid=None, status="missing"),
            True,
            {"events": [], "next_before_uuid": None, "error": "transcript_missing"},
        ),
        (
            "a brand-new session has not been written yet",
            HistoryPage(events=[], next_before_uuid=None, status="missing"),
            False,
            {"events": [], "next_before_uuid": None},
        ),
        (
            "another read holds the parse permit",
            HistoryPage(events=[], next_before_uuid=None, status="busy"),
            True,
            {"events": [], "next_before_uuid": None, "error": "busy"},
        ),
        (
            "the transcript could not be read",
            HistoryPage(
                events=[], next_before_uuid=None, status="unreadable", reason="read_failed"
            ),
            True,
            {"events": [], "next_before_uuid": None, "error": "unavailable"},
        ),
    ]
    for index, (name, page, existing, expected) in enumerate(cases):
        catalog = StatusCatalog(tmp_path / "home", tmp_path / "vault", page, existing=existing)
        with (
            running(tmp_path / str(index), catalog=catalog) as app,
            connected(app.client) as (ws, _),
        ):
            target = subscribe(ws)[-1]
            actor = app.manager.get(NEW)
            assert actor is not None and actor.transcript_expected is existing, name
            assert history_data(ws, target) == expected, name


class DelayedFirstLookup(EmptyCatalog):
    def __init__(self, home: Path, vault: Path) -> None:
        super().__init__(home, vault)
        self.started = threading.Event()
        self.release = threading.Event()
        self.first = True

    def exists(self, session_id: str) -> bool:
        if self.first:
            self.first = False
            self.started.set()
            self.release.wait()
        return False


async def finish_socket_commands() -> None:
    # Wait for the actual admitted command, including its executor completion: the stale
    # open's off-loop SQLite reads (session_origin and the label read), which can stall past
    # a short bound under a committing writer. A fixed number of loop yields would let this
    # race test pass before the stale open finishes.
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_coro().__qualname__.endswith(".connected_socket.<locals>.dispatch")
    ]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=HANG_GUARD_S)


async def emit_after_pending_callbacks(actor: SessionActor) -> None:
    await finish_socket_commands()
    actor._emit(TextDone(block_id="latest", text="latest subscription"))


def test_slow_old_open_cannot_replace_a_newer_subscription_request(tmp_path: Path) -> None:
    catalog = DelayedFirstLookup(tmp_path / "home", tmp_path / "vault")
    with running(tmp_path, catalog=catalog) as app, connected(app.client) as (ws, _):
        old_request = identity()
        ws.send_json({"type": "session.open", "session_id": NEW, "request_id": old_request})
        try:
            assert catalog.started.wait(timeout=0.05)
            latest = subscribe(ws)[-1]
            assert latest["request_id"] != old_request
        finally:
            catalog.release.set()
        actor = app.manager.get(NEW)
        assert actor is not None
        ws.portal.call(emit_after_pending_callbacks, actor)
        frames = until(ws, lambda frame: frame["type"] == "text.done")
        assert frames[-1]["request_id"] == latest["request_id"]
        assert all(frame.get("request_id") != old_request for frame in frames)


def test_connection_limit_is_released_after_socket_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_SOCKET_CONNECTIONS", 2)
    with running(tmp_path) as app, connected(app.client):
        with (
            connected(app.client),
            pytest.raises(WebSocketDisconnect) as refused,
            app.client.websocket_connect("/ws", headers=WS_HEADERS),
        ):
            raise AssertionError("a third connection was admitted")
        assert refused.value.code == 1013
        with connected(app.client) as (_, frame):
            assert frame["type"] == "vault.sessions"


class PausedSendDriver(RecordingDriver):
    def __init__(self, launch: Launch, emit: Callable[[Event], None]) -> None:
        super().__init__(launch, emit)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, text: str) -> None:
        self.calls.append(("send", text))
        self.entered.set()
        await self.release.wait()


async def wait_entered(driver: PausedSendDriver) -> None:
    await asyncio.wait_for(driver.entered.wait(), timeout=0.05)


@pytest.mark.parametrize("global_limit", [False, True])
def test_command_capacity_refuses_new_work_and_keeps_inflight_duplicate_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_limit: bool
) -> None:
    made: list[PausedSendDriver] = []

    def make(launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]) -> PausedSendDriver:
        driver = PausedSendDriver(launch, emit)
        made.append(driver)
        return driver

    monkeypatch.setattr(
        server, "MAX_APP_SOCKET_COMMANDS" if global_limit else "MAX_SOCKET_COMMANDS", 2
    )
    with (
        running(tmp_path, make_driver=make) as app,
        connected(app.client) as (ws, _),
        connected(app.client) as (other, _),
    ):
        target = subscribe(ws)[-1]
        subscribe(other)
        first = mutation("turn.send", target["generation"], text="first")
        second = mutation("turn.send", target["generation"], text="second")
        excess = mutation("turn.send", target["generation"], text="refused")
        ws.send_json(first)
        driver = made[0]
        try:
            ws.portal.call(wait_entered, driver)
            ws.send_json(second)
            receiver = other if global_limit else ws
            receiver.send_json(excess)
            result = receipt(receiver, excess)
            assert result["data"] == {"status": "rejected", "reason": "overloaded"}
            ws.send_json(first)
            duplicate = receipt(ws, first)
            assert duplicate["data"] == {"status": "unknown", "reason": "in_progress"}
            assert driver.calls == [("send", "first")]
        finally:
            ws.portal.call(driver.release.set)
        assert receipt(ws, first)["data"]["status"] == "applied"
        assert receipt(ws, second)["data"]["status"] == "applied"
        assert driver.calls == [("send", "first"), ("send", "second")]


def test_outbound_overflow_closes_only_socket_and_reconnect_recovers_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app:
        with connected(app.client) as (ws, _):
            original = subscribe(ws)[-1]
            actor = app.manager.get(NEW)
            assert actor is not None
            monkeypatch.setattr(server, "MAX_OUTBOUND_BYTES", 4096)

            def burst() -> None:
                for index in range(3):
                    drivers.made[0]._emit(TextDone(block_id=f"burst-{index}", text="x" * 2048))

            ws.portal.call(burst)
            with pytest.raises(WebSocketDisconnect) as closed:
                until(ws, lambda _: False)
            assert closed.value.code == 4410
        assert not actor._subscribers
        assert drivers.made[0].alive
        monkeypatch.setattr(server, "MAX_OUTBOUND_BYTES", 4 << 20)
        with connected(app.client) as (ws, _):
            recovered = subscribe(ws, generation=original["generation"], after_seq=original["seq"])
            assert recovered[-1]["generation"] == actor.generation
            assert len([frame for frame in recovered if frame["type"] == "text.done"]) == 3
            assert len(drivers.made) == 1


def test_reconnect_uses_snapshot_when_retained_replay_exceeds_socket_budget(tmp_path: Path) -> None:
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app:
        with connected(app.client) as (ws, _):
            original = subscribe(ws)[-1]
            actor = app.manager.get(NEW)
            assert actor is not None
            portal = ws.portal

        def fill_offline_replay() -> None:
            actor._emit(UserMessage(text="synthetic offline turn"))
            for index in range(10):
                actor._emit(TextDone(block_id=f"large-{index}", text="x" * (512 << 10)))

        portal.call(fill_offline_replay)
        raw_replay = actor.events_after(original["seq"])
        replay_bytes = len(json.dumps(raw_replay).encode("utf-8"))
        assert (4 << 20) < replay_bytes < (8 << 20)
        assert actor.replay_floor <= original["seq"]
        with connected(app.client) as (ws, _):
            recovered = subscribe(ws, generation=original["generation"], after_seq=original["seq"])
            session_frames = [frame for frame in recovered if frame["session_id"] == NEW]
            assert [frame["type"] for frame in session_frames] == [
                "session.snapshot",
                "session.synced",
            ]
            snapshot = session_frames[0]
            assert len(json.dumps(snapshot).encode("utf-8")) <= (3 << 20)
            assert snapshot["data"]["truncated"]
            assert any(
                item["type"] == "text.done" and item["data"]["block_id"] == "large-9"
                for item in snapshot["data"]["events"]
            )
            assert session_frames[-1]["seq"] == actor.seq
            assert session_frames[-1]["generation"] == original["generation"]
            assert len(drivers.made) == 1


def test_multiple_snapshots_wait_for_healthy_writer_and_recapture_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two individually valid 36 KiB snapshots exceed this injected 64 KiB queue while the
    # first write is in flight. They must drain in order without forcing a reconnect.
    monkeypatch.setattr(server, "MAX_OUTBOUND_BYTES", 64 << 10)
    original_send = WebSocket.send_text
    writer_entered = threading.Event()
    second_captured = threading.Event()
    writer_release = asyncio.Event()
    blocked_once = False

    async def delayed_send(socket: WebSocket, data: str) -> None:
        nonlocal blocked_once
        frame = json.loads(data)
        if frame["type"] == "session.snapshot" and frame["session_id"] == NEW and not blocked_once:
            blocked_once = True
            writer_entered.set()
            await writer_release.wait()
        await original_send(socket, data)

    monkeypatch.setattr(WebSocket, "send_text", delayed_send)
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        first_actor = ws.portal.call(app.manager.create, NEW)
        second_actor = ws.portal.call(app.manager.create, FORK)

        def populate() -> None:
            for actor in (first_actor, second_actor):
                actor._emit(UserMessage(text="synthetic recovered turn"))
                for index in range(3):
                    actor._emit(TextDone(block_id=f"large-{index}", text="x" * (12 << 10)))

        ws.portal.call(populate)
        recovery = second_actor.recovery

        def observed_recovery(generation: str | None, after_seq: int) -> list[dict[str, Any]]:
            result = recovery(generation, after_seq)
            second_captured.set()
            return result

        monkeypatch.setattr(second_actor, "recovery", observed_recovery)
        first_request, second_request = identity(), identity()
        ws.send_json({"type": "session.open", "session_id": NEW, "request_id": first_request})
        try:
            assert writer_entered.wait(timeout=0.05)
            ws.send_json({"type": "session.open", "session_id": FORK, "request_id": second_request})
            assert second_captured.wait(timeout=0.05)
            ws.portal.call(
                second_actor._emit, TextDone(block_id="after-capacity-wait", text="newest state")
            )
        finally:
            ws.portal.call(writer_release.set)
        frames = until(
            ws, lambda frame: frame["type"] == "session.synced" and frame["session_id"] == FORK
        )
        assert [(frame["type"], frame["session_id"]) for frame in frames] == [
            ("session.snapshot", NEW),
            ("session.synced", NEW),
            ("session.snapshot", FORK),
            ("session.synced", FORK),
        ]
        assert [frame["request_id"] for frame in frames] == [
            first_request,
            first_request,
            second_request,
            second_request,
        ]
        assert frames[-1]["seq"] == second_actor.seq
        assert any(
            item["type"] == "text.done" and item["data"]["block_id"] == "after-capacity-wait"
            for item in frames[-2]["data"]["events"]
        )
        assert drivers.made == []


@pytest.mark.parametrize("superseded", [False, True])
def test_actor_replaced_during_capacity_wait_refuses_only_current_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, superseded: bool
) -> None:
    monkeypatch.setattr(server, "MAX_OUTBOUND_BYTES", 64 << 10)
    original_send = WebSocket.send_text
    writer_entered = threading.Event()
    waiting_capture = threading.Event()
    latest_capture = threading.Event()
    writer_release = asyncio.Event()

    async def delayed_send(socket: WebSocket, data: str) -> None:
        frame = json.loads(data)
        if frame["type"] == "session.snapshot" and frame["session_id"] == NEW:
            writer_entered.set()
            await writer_release.wait()
        await original_send(socket, data)

    monkeypatch.setattr(WebSocket, "send_text", delayed_send)
    with running(tmp_path) as app, connected(app.client) as (ws, _):
        first = ws.portal.call(app.manager.create, NEW)
        waiting = ws.portal.call(app.manager.create, FORK)

        def populate() -> None:
            for actor in (first, waiting):
                actor._emit(UserMessage(text="synthetic recovered turn"))
                for index in range(3):
                    actor._emit(TextDone(block_id=f"large-{index}", text="x" * (12 << 10)))

        ws.portal.call(populate)
        recovery = waiting.recovery

        def observe_wait(generation: str | None, after_seq: int) -> list[dict[str, Any]]:
            result = recovery(generation, after_seq)
            waiting_capture.set()
            return result

        monkeypatch.setattr(waiting, "recovery", observe_wait)
        first_request, waiting_request, latest_request = identity(), identity(), identity()
        ws.send_json({"type": "session.open", "session_id": NEW, "request_id": first_request})
        try:
            assert writer_entered.wait(timeout=0.05)
            ws.send_json(
                {"type": "session.open", "session_id": FORK, "request_id": waiting_request}
            )
            assert waiting_capture.wait(timeout=0.05)
            ws.portal.call(app.manager.close, FORK)
            replacement = ws.portal.call(app.manager.create, FORK)
            assert replacement.generation != waiting.generation
            if superseded:
                latest_recovery = replacement.recovery

                def observe_latest(generation: str | None, after_seq: int) -> list[dict[str, Any]]:
                    result = latest_recovery(generation, after_seq)
                    latest_capture.set()
                    return result

                monkeypatch.setattr(replacement, "recovery", observe_latest)
                ws.send_json(
                    {
                        "type": "session.open",
                        "session_id": FORK,
                        "request_id": latest_request,
                    }
                )
                assert latest_capture.wait(timeout=0.05)
        finally:
            ws.portal.call(writer_release.set)
        if not superseded:
            # The latest open must settle. Silently abandoning it leaves the browser waiting
            # forever on a healthy-looking connection; an explicit resync refusal is safe.
            with pytest.raises(WebSocketDisconnect) as refused:
                until(ws, lambda _: False)
            assert refused.value.code == 4410
        else:
            frames = until(
                ws,
                lambda frame: (
                    frame["type"] == "session.synced" and frame["request_id"] == latest_request
                ),
            )
            assert all(frame.get("request_id") != waiting_request for frame in frames)
            assert frames[-1]["generation"] == replacement.generation
            ws.portal.call(finish_socket_commands)
            ws.portal.call(replacement._emit, TextDone(block_id="live", text="still connected"))
            delivered = until(ws, lambda frame: frame["type"] == "text.done")[-1]
            assert delivered["generation"] == replacement.generation
            assert delivered["request_id"] == latest_request


def test_writer_timeout_closes_socket_and_releases_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_send = WebSocket.send_text

    async def blocked_send(socket: WebSocket, data: str) -> None:
        if '"type":"text.done"' in data:
            await asyncio.Event().wait()
        await original_send(socket, data)

    monkeypatch.setattr(WebSocket, "send_text", blocked_send)
    monkeypatch.setattr(server, "SOCKET_WRITE_TIMEOUT_S", 0.01)
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app:
        with connected(app.client) as (ws, _):
            subscribe(ws)
            actor = app.manager.get(NEW)
            assert actor is not None
            ws.portal.call(drivers.made[0]._emit, TextDone(block_id="blocked", text="pending"))
            with pytest.raises(WebSocketDisconnect) as closed:
                recv(ws, timeout=HANG_GUARD_S)
            assert closed.value.code == 4410
        assert not actor._subscribers
        assert drivers.made[0].alive


# --- the socket answer as a compatibility transport over the durable approval seam ------------


def raise_card(ws: WebSocketTestSession, drivers: RecordingDrivers) -> dict[str, Any]:
    ws.portal.call(drivers.made[-1].ask)
    return until(ws, lambda frame: frame["type"] == "permission.requested")[-1]


def answer(
    ws: WebSocketTestSession, generation: str, card: dict[str, Any], *, allow: bool = True
) -> dict[str, Any]:
    command = mutation(
        "permission.respond",
        generation,
        tool_use_id=card["data"]["tool_use_id"],
        native_request=card["native_request"],
        allow=allow,
    )
    ws.send_json(command)
    return receipt(ws, command)


def test_socket_answer_creates_the_same_record_as_rest(tmp_path: Path) -> None:
    """The two client messages are a transport: they leave the record REST would leave."""
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        over_socket = raise_card(ws, drivers)
        command = mutation(
            "permission.respond",
            target["generation"],
            tool_use_id="same-native-id",
            native_request=over_socket["native_request"],
            allow=True,
        )
        ws.send_json(command)
        assert receipt(ws, command)["data"] == {"status": "applied", "reason": ""}
        over_rest = raise_card(ws, drivers)
        choice_id = identity()
        posted = app.client.post(
            "/api/v2/approvals",
            headers={**BEARER, "origin": ORIGIN},
            json={
                "choice_id": choice_id,
                "session_id": NEW,
                "generation": target["generation"],
                "tool_use_id": "same-native-id",
                "native_request": over_rest["native_request"],
                "choice": {"kind": "permission", "allow": True},
            },
        )
        assert posted.status_code == 202
        rows = {row.choice_id: row for row in ledger(app).list().approvals}
        assert len(rows) == 2
        # The socket's command id IS the choice id, so a retry of that frame finds this row.
        socket_row, rest_row = rows[command["command_id"]], rows[choice_id]
        for name in ("session_id", "generation", "tool_use_id", "state", "reason"):
            assert getattr(socket_row, name) == getattr(rest_row, name)
        assert (socket_row.state, socket_row.reason) == ("delivered_locally", "")
        assert socket_row.request_id == over_socket["native_request"]["request_id"]
        assert rest_row.request_id == over_rest["native_request"]["request_id"]
        socket_record, rest_record = (
            ledger(app).get(choice_id),
            ledger(app).get(socket_row.choice_id),
        )
        assert socket_record.choice == rest_record.choice
        assert rest_record.request["type"] == "permission.requested"
        assert rest_record.request["native_request"] == over_socket["native_request"]


def test_an_unclassified_exception_is_rejected_storage_unavailable_never_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception `decide()` does not type itself (neither `ApprovalError` nor
    `ApprovalDispatchRejected`) still recorded nothing, so the socket answer must stay a typed
    refusal, never the generic dispatch fallback's `unknown/internal`, which would claim a
    possible delivery for an attempt that certainly did not happen."""
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        card = raise_card(ws, drivers)

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("an exception the service does not normalise")

        monkeypatch.setattr(approvals_module, "require_private_store", boom)
        result = answer(ws, target["generation"], card)
        monkeypatch.undo()
        assert result["data"] == {"status": "rejected", "reason": "storage_unavailable"}
        assert ledger(app).list().approvals == []


def test_recorded_allow_for_retired_request_is_not_delivered_to_its_replacement(
    tmp_path: Path,
) -> None:
    """A recorded choice never migrates: the row holds the retired binding, and the next card
    for the same tool-use id is a different native request that only its own answer can close."""
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        first = raise_card(ws, drivers)
        second = raise_card(ws, drivers)  # the same tool-use id, a new native request
        assert second["native_request"] != first["native_request"]
        stale = answer(ws, target["generation"], first)
        assert stale["data"]["status"] == "rejected"
        assert ledger(app).list().approvals == []
        assert drivers.made[-1].pending() == ["same-native-id"]
        assert answer(ws, target["generation"], second)["data"]["status"] == "applied"
        rows = ledger(app).list().approvals
        assert len(rows) == 1
        assert rows[0].request_id == second["native_request"]["request_id"]


@pytest.mark.parametrize(
    "code,reason",
    [
        ("capacity", "capacity"),
        ("already_decided", "already_decided"),
        ("protection", "storage_unavailable"),
        ("corrupt", "storage_unavailable"),
        ("unavailable", "storage_unavailable"),
        ("busy", "busy"),
        ("incompatible", "incompatible"),
        ("recovery_required", "recovery_required"),
    ],
)
def test_a_refused_socket_answer_is_rejected_and_never_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, reason: str
) -> None:
    """`unknown` means "possibly delivered". A refusal that recorded nothing is not that.

    `busy`, `incompatible` and `recovery_required` come from StateError through the service's
    own guard, so they reach this seam from outside the approvals' list and must still say rejected.
    """
    drivers = RecordingDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        card = raise_card(ws, drivers)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            # Both kinds of failure arrive through the service's own access guard.
            raise ApprovalError(code) if code in APPROVAL_CODES else StateError(code)

        monkeypatch.setattr(approvals_module, "require_private_store", refuse)
        result = answer(ws, target["generation"], card)
        assert result["data"] == {"status": "rejected", "reason": reason}
        monkeypatch.undo()
        assert ledger(app).list().approvals == []
        assert drivers.made[-1].pending() == ["same-native-id"]
