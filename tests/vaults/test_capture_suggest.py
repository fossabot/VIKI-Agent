"""The suggestion run: one session per click, every card denied, every ending closed.

A fake host stands in for `SessionManager` and records remember, create and close in order, so a
test reads the exact sequence a run made. One test drives a real manager to show what the spawn
received.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_session import Drivers, gated
from viki_agent.conversations.stream_state import MAX_TEXT_CHARS
from viki_agent.drivers.claude.control import Launch, build_argv
from viki_agent.events import Error, TextDone, TurnDone, Usage
from viki_agent.state import Prefs
from viki_agent.vaults.capture_reply import MAX_WHY_CHARS, Suggestion, build_prompt
from viki_agent.vaults.capture_suggest import (
    MAX_REPLY_CHARS,
    CaptureSuggester,
    SuggestionRun,
    _Turn,
    bound_listing,
)
from viki_agent.vaults.captures import Capture, CaptureError, CaptureListing

WS = "11111111-1111-4111-8111-111111111111"
REF_A = "00-inbox/a.md"
REF_B = "00-inbox/b.md"
REVISIONS = {REF_A: "a" * 64, REF_B: "b" * 64}
WALL = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
STAMP = "2026-10-10T09:00:00.000Z"
WHY = "a claim worth keeping"
PROSE = "Here is where each capture could go."
SPAWNED = {"model": None, "mode": "default", "effort": "low"}
READ_ONLY = {"read_only_tools": True}  # what every run asks `create` for
HANG_GUARD_S = 5  # a guard on waits that settle in milliseconds; it never fires in a healthy run

Envelope = dict[str, Any]
Event = tuple[str, dict[str, Any]]
Script = Callable[["FakeSession"], Awaitable[bool | None]]


def listing(preview: str = "a thought") -> CaptureListing:
    captures = tuple(
        Capture(ref, revision, STAMP, None, preview, False, None)
        for ref, revision in REVISIONS.items()
    )
    return CaptureListing(WS, captures, (), False, 0)


class FakeCaptures:
    def __init__(self, value: CaptureListing) -> None:
        self.value = value
        self.calls: list[str] = []

    def list(self, workspace_id: str) -> CaptureListing:
        self.calls.append(workspace_id)
        return self.value


class FakeSession:
    """The session surface a run reaches; `script` plays the turn while the prompt is sent."""

    def __init__(self, script: Script, sent: asyncio.Event) -> None:
        self.script = script
        self.sent = sent
        self.subscribers: list[Callable[[Envelope], None]] = []
        self.prompts: list[str] = []
        self.pending: list[str] = []
        self.denied: list[str] = []
        self.answered = asyncio.Event()
        self.interrupts = 0
        self.emitting = False
        self.denied_while_emitting = False
        self.player: asyncio.Task[None] | None = None

    def subscribe(self, subscriber: Callable[[Envelope], None]) -> Callable[[], None]:
        self.subscribers.append(subscriber)
        return lambda: self.subscribers.remove(subscriber)

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        if kind in ("permission.requested", "question.asked"):
            self.pending.append(data["tool_use_id"])
        self.emitting = True
        try:
            for subscriber in list(self.subscribers):
                subscriber({"type": kind, "data": data})
        finally:
            self.emitting = False

    async def send(self, text: str) -> bool | None:
        self.prompts.append(text)
        self.sent.set()
        return await self.script(self)

    def deny_pending(self) -> None:
        self.denied_while_emitting |= self.emitting
        if self.pending:
            self.denied += self.pending
            self.pending = []
            self.answered.set()

    async def interrupt(self) -> None:
        self.interrupts += 1


class FakeHost:
    """`SessionManager` as a run reaches it. It is its own `app_state`, so `calls` holds every
    remember, create and close in the order the run made them."""

    def __init__(self, *scripts: Script) -> None:
        self.app_state = self
        self.scripts = list(scripts)
        self.calls: list[tuple[Any, ...]] = []
        self.sessions: dict[str, FakeSession] = {}
        self.sent = asyncio.Event()
        self.close_error: Exception | None = None

    def remember(self, session_id: str, **changes: Any) -> None:
        self.calls.append(("remember", session_id, changes))

    def native_model(self) -> str | None:
        return None

    def create(self, session_id: str, **options: Any) -> FakeSession:
        self.calls.append(("create", session_id, options))
        session = self.sessions[session_id] = FakeSession(self.scripts.pop(0), self.sent)
        return session

    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))
        if self.close_error is not None:
            raise self.close_error


def turn(*events: Event, sent: bool | None = True) -> Script:
    """Emit `events` while the prompt is being sent, then answer `sent`."""

    async def script(session: FakeSession) -> bool | None:
        for kind, data in events:
            session.emit(kind, data)
        return sent

    return script


async def silent(session: FakeSession) -> bool | None:
    return True  # the turn never ends


async def hang(session: FakeSession) -> bool | None:
    await asyncio.Event().wait()  # the send itself never returns
    return True


def text(value: str, block_id: str = "b1") -> Event:
    return "text.done", {"block_id": block_id, "text": value}


def delta(value: str, block_id: str = "b1") -> Event:
    return "text.delta", {"block_id": block_id, "text": value}


def done(status: str = "success") -> Event:
    return "turn.done", {"status": status}


def error(code: str) -> Event:
    return "error", {"code": code, "message": "", "recoverable": True}


def closed(reason: str) -> Event:
    return "session.closed", {"reason": reason}


def reply(*refs: str, why: str = WHY) -> str:
    items = [{"ref": ref, "to": "knowledge", "why": why} for ref in refs]
    return f"```viki-inbox\n{json.dumps({'suggestions': items})}\n```"


def answer(*refs: str) -> Script:
    return turn(text(PROSE, "b1"), text(reply(*refs), "b2"), done())


def expected(*refs: str) -> dict[str, Suggestion]:
    return {
        ref: Suggestion(ref, REVISIONS[ref], STAMP, "knowledge", None, WHY, STAMP) for ref in refs
    }


def remembered(session_id: str) -> list[tuple[Any, ...]]:
    return [
        ("remember", session_id, SPAWNED),
        ("create", session_id, READ_ONLY),
        ("close", session_id),
    ]


# --- _Turn.text -------------------------------------------------------------------------------


def test_turn_text_falls_back_to_the_running_delta_for_a_block_the_turn_never_finished() -> None:
    """`order` can hold a delta-only id (a block still streaming when the turn ended); the
    bare `self.finished[block_id]` raised `KeyError` on it instead of using what `deltas` has."""
    turn = _Turn()
    turn({"type": "text.done", "data": {"block_id": "b1", "text": "hello world"}})
    turn({"type": "text.delta", "data": {"block_id": "b2", "text": "still going"}})

    assert turn.order == ["b1", "b2"]
    assert "b2" not in turn.finished
    assert turn.text == "hello world\nstill going"


def make(
    host: Any, captures: FakeCaptures | None = None, *, deadline_s: float = 300.0
) -> tuple[CaptureSuggester, set[asyncio.Task[None]]]:
    tasks: set[asyncio.Task[None]] = set()
    suggester = CaptureSuggester(
        host,
        captures or FakeCaptures(listing()),
        tasks=tasks,
        deadline_s=deadline_s,
        wall=lambda: WALL,
    )
    return suggester, tasks


async def settle(tasks: set[asyncio.Task[None]]) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not tasks  # each run's task leaves the set when it is done


def ended(run: SuggestionRun, reason: str | None = None, **counts: int) -> SuggestionRun:
    state = "done" if reason is None else "failed"
    return replace(run, state=state, finished_at=STAMP, reason=reason, **counts)


# --- one click ------------------------------------------------------------------------------------


async def test_one_click_runs_one_session_in_default_mode_at_low_effort() -> None:
    host = FakeHost(answer(REF_A, REF_B))
    captures = FakeCaptures(listing())
    suggester, tasks = make(host, captures)

    run = await suggester.start(WS)

    assert run == SuggestionRun(run.run_id, WS, run.session_id, "running", STAMP)
    assert str(uuid.UUID(run.run_id)) == run.run_id != run.session_id
    assert str(uuid.UUID(run.session_id)) == run.session_id
    assert suggester.run_for(WS) == run
    await settle(tasks)
    assert host.calls == remembered(run.session_id)
    assert captures.calls == [WS]
    (prompt,) = host.sessions[run.session_id].prompts
    assert prompt == build_prompt(listing())
    assert REF_A in prompt and REF_B in prompt
    assert suggester.run_for(WS) == ended(run, suggested=2)
    assert suggester.suggestions(WS) == expected(REF_A, REF_B)
    assert suggester.run_for("another") is None
    assert suggester.suggestions("another") == {}


async def test_the_read_only_actor_exists_before_start_hands_out_its_id() -> None:
    """`start` must not hand out `session_id` in its 202 before the read-only actor exists under
    it, or an Open chat reaching `_actor_for(start_if_new=True)` in that gap would make its own
    normal-tools actor, later orphaned when this run's own create replaces it. A slow `remember`
    (run in a real thread by `run_sync`) must not open that gap: `start` awaits it, then
    `create`, before it returns."""

    class SlowHost(FakeHost):
        def remember(self, session_id: str, **changes: Any) -> None:
            time.sleep(0.05)
            super().remember(session_id, **changes)

    host = SlowHost(answer(REF_A))
    suggester, tasks = make(host)

    run = await suggester.start(WS)

    assert host.calls == remembered(run.session_id)[:2]  # remember, then create; no close yet
    await settle(tasks)


async def test_a_second_start_while_one_runs_is_refused() -> None:
    host = FakeHost(silent, answer(REF_A))
    captures = FakeCaptures(listing())
    suggester, tasks = make(host, captures)

    starts = await asyncio.gather(suggester.start(WS), suggester.start(WS), return_exceptions=True)

    (first,) = [value for value in starts if isinstance(value, SuggestionRun)]
    (refused,) = [value for value in starts if isinstance(value, CaptureError)]
    assert (refused.code, refused.extra) == ("suggestion_running", {"run_id": first.run_id})
    assert captures.calls == [WS]  # the refused start listed nothing
    async with asyncio.timeout(HANG_GUARD_S):
        await host.sent.wait()
    for task in tasks:
        task.cancel()
    await settle(tasks)
    second = await suggester.start(WS)  # an ended run no longer blocks a start
    await settle(tasks)
    assert {second.run_id, second.session_id}.isdisjoint({first.run_id, first.session_id})
    assert host.calls == remembered(first.session_id) + remembered(second.session_id)
    assert suggester.run_for(WS) == ended(second, suggested=1)


async def test_an_empty_inbox_starts_nothing() -> None:
    host = FakeHost()
    suggester, tasks = make(host, FakeCaptures(replace(listing(), captures=())))

    with pytest.raises(CaptureError) as refused:
        await suggester.start(WS)

    assert refused.value.code == "no_captures"
    assert not tasks and not host.calls
    assert suggester.run_for(WS) is None


async def test_a_bound_start_whose_listing_is_not_its_state_sends_nothing() -> None:
    """A start made under `bound_listing` (VIKI's own, through the inbox host) checks the listing
    it is about to send; another state refuses it before any session exists."""
    host = FakeHost(answer(REF_A))
    suggester, tasks = make(host)
    seen: list[CaptureListing] = []

    def other_state(value: CaptureListing) -> bool:
        seen.append(value)
        return False

    token = bound_listing.set(other_state)
    try:
        with pytest.raises(CaptureError) as refused:
            await suggester.start(WS)
    finally:
        bound_listing.reset(token)

    assert refused.value.code == "inbox_changed"
    assert seen == [listing()]
    assert not tasks and not host.calls
    assert suggester.run_for(WS) is None
    run = await suggester.start(WS)  # unbound, as the user's own run: the same listing goes out
    async with asyncio.timeout(HANG_GUARD_S):
        await asyncio.gather(*tasks)
    assert suggester.run_for(WS) == ended(run, suggested=1)


async def test_every_card_is_denied_from_the_run_task() -> None:
    async def cards(session: FakeSession) -> bool | None:
        async def play() -> None:
            for kind, tool_use_id in (("permission.requested", "t1"), ("question.asked", "t2")):
                session.emit(kind, {"tool_use_id": tool_use_id})
                await session.answered.wait()
                session.answered.clear()
            for kind, data in (text(reply(REF_A)), done()):
                session.emit(kind, data)

        session.player = asyncio.create_task(play())
        return True

    host = FakeHost(cards)
    suggester, tasks = make(host)

    run = await suggester.start(WS)
    await settle(tasks)

    session = host.sessions[run.session_id]
    assert session.denied == ["t1", "t2"]
    assert not session.denied_while_emitting  # denied by the task, never by its observer
    assert suggester.run_for(WS) == ended(run, suggested=1)
    assert host.calls == remembered(run.session_id)


# --- endings --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        pytest.param(answer(REF_A), None, id="success"),
        pytest.param(turn(done("error"), error("crashed")), "crashed", id="turn-error"),
        pytest.param(
            turn(done("error"), error("cli_missing")), "cli_missing", id="turn-error-listed-code"
        ),
        pytest.param(
            turn(done("error"), error("bad_request")), "error", id="turn-error-unlisted-code"
        ),
        pytest.param(turn(done("error")), "error", id="turn-error-without-code"),
        pytest.param(turn(done("cancelled")), "cancelled", id="turn-stopped"),
        pytest.param(
            turn(error("too_many_sessions"), sent=False), "too_many_sessions", id="refused"
        ),
        pytest.param(turn(sent=None), "not_sent", id="send-unknown"),
        pytest.param(turn(closed("browser_gone")), "app_closed", id="closed-by-the-app"),
        pytest.param(turn(closed("requested")), "cancelled", id="closed-by-the-owner"),
        pytest.param(silent, "deadline", id="deadline"),
    ],
)
async def test_every_ending_closes_its_session_once(script: Script, reason: str | None) -> None:
    host = FakeHost(answer(REF_A, REF_B), script)
    suggester, tasks = make(host, deadline_s=0.05)
    first = await suggester.start(WS)
    await settle(tasks)
    kept = suggester.suggestions(WS)

    run = await suggester.start(WS)
    await settle(tasks)

    assert host.calls == remembered(first.session_id) + remembered(run.session_id)
    assert host.sessions[run.session_id].interrupts == (1 if reason == "deadline" else 0)
    if reason is None:  # a successful run replaces the map; it does not merge into it
        assert suggester.run_for(WS) == ended(run, suggested=1)
        assert suggester.suggestions(WS) == expected(REF_A)
    else:
        assert suggester.run_for(WS) == ended(run, reason)
        assert suggester.suggestions(WS) == kept == expected(REF_A, REF_B)


async def test_a_chatty_turn_still_meets_its_absolute_deadline() -> None:
    """`_outcome` waits on one absolute `deadline` (`asyncio.timeout_at`), computed once before
    the loop. A turn that keeps streaming deltas faster than `deadline_s` must not be able to
    postpone it by waking the loop again (a relative `asyncio.timeout(self._deadline_s)` would
    restart on every iteration and never fire here)."""

    async def chatty(session: FakeSession) -> bool | None:
        async def play() -> None:
            while True:  # streams text forever; only the deadline can end this turn
                session.emit("text.delta", {"block_id": "b1", "text": "x"})
                await asyncio.sleep(0.01)

        session.player = asyncio.create_task(play())
        return True

    host = FakeHost(chatty)
    suggester, tasks = make(host, deadline_s=0.1)

    run = await suggester.start(WS)
    try:
        async with asyncio.timeout(2):  # 20x the deadline: ample if the deadline is absolute
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        player = host.sessions[run.session_id].player
        if player is not None:
            player.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert suggester.run_for(WS) == ended(run, "deadline")
    assert host.sessions[run.session_id].interrupts == 1


@pytest.mark.parametrize(
    ("script", "rejected"),
    [
        pytest.param(turn(text("No block here."), done()), 0, id="no-block"),
        pytest.param(turn(text(reply("00-inbox/gone.md")), done()), 1, id="nothing-usable"),
    ],
)
async def test_a_reply_without_a_block_keeps_old_suggestions(script: Script, rejected: int) -> None:
    host = FakeHost(answer(REF_A, REF_B), script)
    suggester, tasks = make(host)
    await suggester.start(WS)
    await settle(tasks)

    run = await suggester.start(WS)
    await settle(tasks)

    assert suggester.run_for(WS) == ended(run, "no_suggestions", rejected=rejected)
    assert suggester.suggestions(WS) == expected(REF_A, REF_B)


@pytest.mark.parametrize(("over", "reason"), [(0, None), (1, "no_suggestions")])
async def test_the_reply_is_read_up_to_its_cap(over: int, reason: str | None) -> None:
    block = reply(REF_A)
    filler = "x" * (MAX_REPLY_CHARS - len(block) - 1 + over)  # one line break joins the two
    host = FakeHost(turn(text(filler, "b1"), text(block, "b2"), done()))
    suggester, tasks = make(host)

    run = await suggester.start(WS)
    await settle(tasks)

    assert suggester.run_for(WS) == ended(run, reason, suggested=int(reason is None))


async def test_a_block_built_from_deltas_survives_its_own_cut_text_done() -> None:
    """The mapper cuts every text.done at MAX_TEXT_CHARS; a full-listing viki-inbox block can
    lose its closing fence there. Built from its own deltas instead, the run still reads the
    whole thing."""
    block = reply(REF_A, why="x" * 20_000)
    assert len(block) > MAX_TEXT_CHARS
    chunks = [delta(block[i : i + 4096], "b1") for i in range(0, len(block), 4096)]
    cut_done = text(block[:MAX_TEXT_CHARS], "b1")  # the mapper's own cut: fence gone
    host = FakeHost(turn(*chunks, cut_done, done()))
    suggester, tasks = make(host)

    run = await suggester.start(WS)
    await settle(tasks)

    assert suggester.run_for(WS) == ended(run, suggested=1)
    assert suggester.suggestions(WS)[REF_A].why == "x" * MAX_WHY_CHARS


async def test_a_block_seen_only_as_text_done_still_parses() -> None:
    host = FakeHost(turn(text(reply(REF_A)), done()))
    suggester, tasks = make(host)

    run = await suggester.start(WS)
    await settle(tasks)

    assert suggester.run_for(WS) == ended(run, suggested=1)
    assert suggester.suggestions(WS) == expected(REF_A)


@pytest.mark.parametrize("script", [silent, hang], ids=["while-waiting", "while-sending"])
async def test_a_cancel_ends_the_run_as_app_closed_and_closes_its_session(script: Script) -> None:
    host = FakeHost(script)
    suggester, tasks = make(host)
    run = await suggester.start(WS)
    (task,) = tasks
    async with asyncio.timeout(HANG_GUARD_S):
        await host.sent.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.calls == remembered(run.session_id)
    assert suggester.run_for(WS) == ended(run, "app_closed")


async def test_logs_carry_no_text(caplog: pytest.LogCaptureFixture) -> None:
    capture_text, reply_text = "capture-words-7f3a", "reply-words-91c2"

    async def broken(session: FakeSession) -> bool | None:
        raise RuntimeError(reply_text)

    succeeded = turn(text(reply_text, "b1"), text(reply(REF_A, why=reply_text), "b2"), done())
    host = FakeHost(succeeded, broken)
    host.close_error = RuntimeError(capture_text)
    suggester, tasks = make(host, FakeCaptures(listing(preview=capture_text)))
    caplog.set_level(logging.DEBUG)

    first = await suggester.start(WS)
    await settle(tasks)
    second = await suggester.start(WS)
    await settle(tasks)

    assert capture_text in host.sessions[first.session_id].prompts[0]  # the text did travel
    assert suggester.run_for(WS) == ended(second, "internal")
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("capture suggestion session close failed: RuntimeError") == 2
    assert "capture suggestion run failed: RuntimeError" in messages
    assert capture_text not in caplog.text and reply_text not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


# --- a real manager -------------------------------------------------------------------------------


@pytest.mark.parametrize("custom_default", [False, True])
async def test_a_real_manager_spawns_read_only_in_default_mode_at_low_effort(
    tmp_path: Path,
    custom_default: bool,
) -> None:
    from tests.llm.test_connections import FORM
    from viki_agent.app.settings import SettingsService
    from viki_agent.llm.connections import Connections, parse_connection
    from viki_agent.llm.gateway import Gateway

    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    state = manager.app_state
    settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model="sonnet"),
        on_chat_defaults=state.set_defaults,
    )
    connections = Connections(settings)
    manager.bind_model_connections(connections, Gateway())
    if custom_default:
        row = parse_connection(FORM)
        connections.write(0, [row])
        settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": row.ref})
    else:
        state.set_defaults(Prefs(model="sonnet", mode="auto", effort="high"))
    tasks: set[asyncio.Task[None]] = set()
    suggester = CaptureSuggester(manager, FakeCaptures(listing()), tasks=tasks, wall=lambda: WALL)

    run = await suggester.start(WS)
    async with asyncio.timeout(HANG_GUARD_S):
        await drivers.created.wait()
        driver, launch, prefs, emit = drivers.made[0]
        while not any(call.startswith("send ") for call in driver.calls):
            await asyncio.sleep(0)

    assert (launch.session_id, prefs.mode, prefs.effort) == (run.session_id, "default", "low")
    assert prefs.model == "sonnet"
    assert launch == Launch(run.session_id, read_only_tools=True)
    argv = build_argv(["claude.exe"], launch, mode=prefs.mode, effort=prefs.effort)
    assert argv[-2:] == ["--tools=Read,Glob,Grep", "--strict-mcp-config"]
    assert driver.calls[-1] == f"send {build_prompt(listing())}"
    emit(TextDone(block_id="b1", text=reply(REF_A)))
    emit(TurnDone(status="success", usage=Usage()))
    await settle(tasks)
    assert suggester.run_for(WS) == ended(run, suggested=1)
    assert driver.calls[-1] == "close"
    assert manager.get(run.session_id) is None  # closed and forgotten


async def test_a_read_only_session_respawns_read_only(tmp_path: Path) -> None:
    """A dropped process re-arms its session as a resume of the same transcript. The re-arm keeps
    `read_only_tools`, so a later process of that session does not get the full tool set back."""
    drivers = Drivers()
    session_id = str(uuid.uuid4())
    actor = gated(tmp_path, drivers).create(session_id, read_only_tools=True)
    assert await actor.send("one")
    first, _, _, emit = drivers.made[0]
    first.alive = False
    emit(Error(code="crashed", message="", recoverable=True))  # the process is gone
    assert not actor.live

    assert await actor.send("two")

    assert [launch for _, launch, _, _ in drivers.made] == [
        Launch(session_id, read_only_tools=True),
        Launch(session_id, resume=True, read_only_tools=True),
    ]
    await actor.close()
