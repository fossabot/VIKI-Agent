"""The Decision loop's service over a real store and the real modules: the launcher's filter
(silent, timed, in-context and after-focus items, vault changes, a chat-only workspace), its
weekly check card and its count of VIKI's chats whose plan waits, presence wakes, every feedback
path (Useful leaving an item open while its work can still start) and its refusals, page events,
reports and preferences, the page's numbers (today's from the day counter) and its learning
block, the key's save, removal and test, the call views, the user's missions, Delete all and
Reset beside a cycle and a beat (the beat's self-improvement candidates dropped), question
wording proposals (their view and trial, acts between cycles), vault-change proposals (acts
between cycles, their labels, the page's count of the day's), and the guards that no view names
the model id or holds the key.
The engine is real but never started: tests run its cycles with `recheck` and its beats with
`_beat`, on a fake clock, model and session host."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.decision_loop.fakes import (
    WALL,
    FakeCheckHost,
    FakeClock,
    FakeHost,
    FakeInboxHost,
    FakeModel,
    FakeSeatHost,
    settings_values,
)
from viki_agent.automation.brief import InboxFacts, TaskFacts, TaskItem, TickedFile, VaultFacts
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import arbiter, detectors, improve, learning, playbook, wire
from viki_agent.decision_loop.agenda import CallWrite, DecisionAgenda, ItemWrite
from viki_agent.decision_loop.changes import change_draft
from viki_agent.decision_loop.contracts import (
    CHANGES,
    LATER,
    MAX_CALLS,
    MAX_OPEN_ITEMS,
    MESSAGE_DRAFTS,
    MIN_LABELS,
    MODEL_IDS,
    OBSERVE_S,
    PROJECT_PLANS,
    QUESTION_LIMIT,
    SECOND_LOOK_WINDOW,
    ActionSpec,
    Answer,
    CallBody,
    CheckFolder,
    ChoiceAnswer,
    DecisionLoopError,
    DecisionSettings,
    EvidenceLabel,
    ItemBody,
    ItemRecord,
    ModelReply,
    ModelRequest,
    NoulAnswer,
    PageFacts,
    Presence,
    ScoreAnswer,
    SeatFacts,
    SeatRoutine,
    SeatRun,
    SeatTeam,
    SecondLookHint,
    item_key,
    parse_settings,
    sha256_hex,
)
from viki_agent.decision_loop.cycle import ShadowSummary, TrialSummary, encode_answers
from viki_agent.decision_loop.detectors import Observation
from viki_agent.decision_loop.engine import DecisionLoopEngine
from viki_agent.decision_loop.key_store import DecisionKeyStore
from viki_agent.decision_loop.missions import MissionError, MissionService
from viki_agent.decision_loop.presence import PresenceTracker
from viki_agent.decision_loop.rules import POLICY_VERSION
from viki_agent.decision_loop.service import DecisionLoopService
from viki_agent.decision_loop.state import History
from viki_agent.decision_loop.stats import DecisionLoopStats
from viki_agent.decision_loop.store import DecisionLoopStore, EpisodeInput
from viki_agent.decision_loop.vault_watch import VaultChanges
from viki_agent.decision_loop.wire import FeedbackRequest
from viki_agent.domain import SCRATCH_WORKSPACE_ID
from viki_agent.events import PermissionRequested, RequestAnswered
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService

TODAY = WALL.astimezone().date()  # the user's day on this computer's clock, as the service reads it
DAY = TODAY.isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
WEEK = timedelta(days=7)
SCOPE = "00000000-0000-4000-8000-0000000000a1"
OTHER_SCOPE = "00000000-0000-4000-8000-0000000000b2"
THIRD_SCOPE = "00000000-0000-4000-8000-0000000000b3"
WINDOW = "00000000-0000-4000-8000-0000000000d1"
CHAT = "00000000-0000-4000-8000-0000000000c1"
MISSING = "00000000-0000-4000-8000-0000000000ff"
KEY = "ts-test-key-5d1e8a2f"
TASKS = "10-projects/plan/tasks.md"
GARDEN = "10-projects/garden/tasks.md"
ELSEWHERE = "10-projects/other/notes.md"
# The note each file of ticked tasks would be archived to.
ARCHIVES = {
    TASKS: "90-archive/plan tasks done 2026.md",
    GARDEN: "90-archive/garden tasks done 2026.md",
}
SEEDLINGS = "Water the seedlings"
HANG_GUARD_S = 5.0  # bounds a wait that settles in milliseconds; it never fires in a healthy run
SETTLE_S = 0.05  # long enough for a delete that need not wait to finish

# Every answer high: a new item scores its way to a nudge once the reversed question agrees.
STRONG: Mapping[str, Answer] = {
    "goal_link": NoulAnswer(0.95),
    "owner_only": NoulAnswer(0.9),
    "news": NoulAnswer(0.9),
    "actionable": NoulAnswer(0.9),
    "same_as_muted": NoulAnswer(0.0),
    "fits_now": NoulAnswer(0.9),
    "sensitive": NoulAnswer(0.0),
    "importance": ScoreAnswer(3.0, 0.9, {3: 0.9, 2: 0.1}),
    "kind": ChoiceAnswer("ask", 0.9, {"ask": 0.9, "inform": 0.1}),
    "work": ChoiceAnswer("none", 0.9, {"none": 0.9, "prepare": 0.1}),
}

# A "learned" snapshot as `learning.encode` writes it: fitted at WALL, the start values but for
# the bias.
LEARNED = learning.encode(
    replace(learning.relearn([], now=WALL), weights={**learning.inputs(None)[0], "bias": -1.0})
)


def worded(question: str) -> dict[str, object]:
    """A wording of goal_link that `playbook.check_question` takes."""
    return {
        "type": "noul",
        "instructions": {"question": question, "focus": playbook.OUTSIDE_SENTENCE},
        "criteria": {"true": "Moves a listed goal", "false": "Touches no listed goal"},
    }


ASKED = "Does `candidate` move one of `owner.goals` forward or hold it back?"
PROPOSED = worded(ASKED)  # the user's proposed wording of goal_link
FIVE = tuple(f"Task {name}" for name in "abcde")  # five Not useful labels call for a retro


class Engine(DecisionLoopEngine):
    """The real engine, noting each wake it is asked for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.wakes: list[str] = []

    def wake(self, reason: str) -> None:
        self.wakes.append(reason)
        super().wake(reason)


class Model(FakeModel):
    """`FakeModel` that sets `asked` as each ask starts."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = asyncio.Event()

    async def ask(self, request: ModelRequest, *, key: str) -> ModelReply:
        self.asked.set()
        return await super().ask(request, key=key)


class Rig:
    """The service over a real store and the real modules, for an active workspace of `scope`;
    the engine is never started. A set `hold` runs as a cycle looks up an item's history, in the
    cycle's worker thread, after the engine's last check for a halt before that item's writes."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        scope: str = SCOPE,
        chat_only: bool = False,
        seats: FakeSeatHost | None = None,
        checks: FakeCheckHost | None = None,
        inbox: FakeInboxHost | None = None,
        kind: str = "vault",
    ) -> None:
        self.scope = scope
        self.policy: str | None = "ordinary"  # the vault's policy as its registration holds it
        self.clock = FakeClock()
        self.state = AppState(tmp_path / "state.json", Prefs()).store
        self.store = DecisionLoopStore(self.state)
        self.agenda = DecisionAgenda(self.state)
        self.keys = DecisionKeyStore(self.state, now=self.clock.wall)
        self.model = Model()
        self.host = FakeHost()
        self.values = settings_values(**{"decision.enabled": True})
        self.missions = MissionService(self.state, self.host, self.settings, wall=self.clock.wall)
        self.presence = PresenceTracker(monotonic=self.clock.monotonic)
        self.observation = observation()
        self.hold: Callable[[str], None] | None = None
        # The plan reading the service and the engine see: one far below every brake, since
        # VIKI holds its own starts until a launch's first reading comes.
        self.reading: Mapping[str, object] | None = holding(0.1)
        self.engine = Engine(
            self.store,
            self.agenda,
            self.keys,
            self.model,
            self.missions,
            self.presence,
            self.settings,
            self.gather,
            detectors.detect,
            clock=self.clock,
            attended=lambda: True,
            history_for=self.history,
            plan_reading=lambda: self.reading,
        )
        active = SimpleNamespace(
            workspace=SimpleNamespace(workspace_id=scope, kind=kind, persistent=True),
            chat_only=chat_only,
        )
        workspaces = SimpleNamespace(
            resolve_active=lambda: active, get=lambda _id: SimpleNamespace(policy=self.policy)
        )
        self.service = DecisionLoopService(
            cast(WorkspaceService, workspaces),
            self.engine,
            self.store,
            self.agenda,
            DecisionLoopStats(self.state),
            self.keys,
            self.missions,
            self.presence,
            self.settings,
            now=self.clock.wall,
            plan_reading=lambda: self.reading,
            seats=seats,
            checks=checks,
            inbox=inbox,
        )

    def settings(self) -> DecisionSettings:
        return parse_settings(self.values)

    def gather(self, page: PageFacts | None, today: date, kanban: bool = False) -> Observation:
        return self.observation

    def history(self, scope: str, key: str) -> History:
        if self.hold is not None:
            self.hold(key)
        return History()

    def rows(self, table: str) -> list[dict[str, Any]]:
        with self.state._connection() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


def task(text: str, due: date = TODAY) -> TaskItem:
    return TaskItem(ref=TASKS, line=1, text=text, due=due, tags=())


def observation(*items: TaskItem, inbox: int = 0, ticked: tuple[str, ...] = ()) -> Observation:
    """The active vault workspace with these tasks due and `inbox` fresh captures, too few for the
    rules to bring up; each tasks file in `ticked` holds five ticked tasks, archived as
    `ARCHIVES` names."""
    tasks = TaskFacts(
        overdue=sum(1 for item in items if item.due < TODAY),
        today=sum(1 for item in items if item.due == TODAY),
        soon=sum(1 for item in items if item.due > TODAY),
        items=items,
        files=1,
        files_key="tasks",
        partial=False,
        ticked=tuple(TickedFile(ref, 5, sha256_hex(ref), ()) for ref in ticked),
    )
    oldest = stamp(WALL - timedelta(hours=1)) if inbox else None
    captures = InboxFacts(count=inbox, oldest=oldest, checked=inbox, undated=0, partial=False)
    changes = VaultChanges((), (), (), {ref: ARCHIVES[ref] for ref in ticked}, False)
    vault = VaultFacts(captures, tasks)
    return Observation(SCOPE, "vault", vault, (), (), None, (), changes if ticked else None)


async def decided(rig: Rig) -> tuple[ItemRecord, ItemRecord]:
    """One cycle on the rules: a task due today, listed, and a fresh capture, held back."""
    rig.observation = observation(task(SEEDLINGS), inbox=1)
    await rig.engine.recheck()
    listed = rig.agenda.item(SCOPE, item_key("tasks_due", TASKS, SEEDLINGS))
    held = rig.agenda.item(SCOPE, item_key("inbox", SCOPE))
    assert listed is not None and (listed.level, listed.state) == ("list", "open")
    assert held is not None and (held.level, held.state) == ("silent", "open")
    return listed, held


def put(
    rig: Rig,
    what: str,
    *,
    level: str = "list",
    timing: str = "now",
    defer_until: datetime | None = None,
    kind: str = "inform",
    evidence: tuple[EvidenceLabel, ...] = (),
    actions: tuple[ActionSpec, ...] = (),
    version: int = 1,
    candidate: str = "tasks_due",
    scope: str | None = None,
    key: str | None = None,
) -> ItemRecord:
    """Store an open item of the rig's scope (or of `scope`), with an episode of its own, as a
    cycle would; a new `version` records a new episode for the same item. `key` replaces the
    item's key."""
    scope = scope or rig.scope
    key = key or item_key(candidate, TASKS, what)
    entry = EpisodeInput(
        candidate=candidate,
        evidence={"what": what, "version": version},
        policy="rules",
        policy_version=POLICY_VERSION,
        level=level,
        timing=timing,
        work="none",
        reason="rules:due_or_overdue",
        item_key=key,
        defer_until=defer_until,
    )
    (recorded,) = rig.store.record(scope, [entry], trigger="change", now=WALL, day=DAY)
    write = ItemWrite(
        item_key=key,
        episode_id=recorded.episode_id,
        candidate=candidate,
        kind=kind,
        level=level,
        timing=timing,
        defer_until=defer_until,
        state_hash=None,
        answers_json=None,
        mission_id=None,
        body=ItemBody(title=what, what=what, why="due today", evidence=evidence, actions=actions),
    )
    return rig.agenda.upsert(scope, write, WALL)


def kept_call(key: str, *, held_back: int, tokens: int | None) -> CallWrite:
    """A model call about item `key`, as a cycle records it."""
    return CallWrite(
        item_key=key,
        episode_id=None,
        session_id=None,
        model=MODEL_IDS["pinned"],
        outcome="answered",
        failure=None,
        latency_ms=10,
        input_tokens=tokens,
        cost_micro_usd=None,
        body=CallBody(
            title=SEEDLINGS,
            candidate="tasks_due",
            state_text="{}",
            questions_text="{}",
            split=(("candidate", 5), ("questions", 7)),
            answers_json=None,
            held_back=held_back,
            version_setting="pinned",
            playbook_version=playbook.PLAYBOOK_VERSION,
        ),
    )


def looking_at(*, note: tuple[str, str] | None = None, chat: str | None = None) -> Presence:
    """A focused window with this note or chat open."""
    return Presence(
        focused=True,
        typing=False,
        away=False,
        chat_id=chat,
        chat_title=None,
        note_ref=note,
        launcher_open=False,
        page=None,
    )


def shown(key: str, value: str, reason: str | None = None) -> FeedbackRequest:
    return FeedbackRequest(
        item_key=key, episode_id=None, value=value, reason=reason, source="launcher"
    )


def held(episode_id: str, value: str) -> FeedbackRequest:
    return FeedbackRequest(
        item_key=None, episode_id=episode_id, value=value, reason=None, source="page"
    )


def whats(view: Mapping[str, Any]) -> list[str]:
    return [item["what"] for item in view["items"]]


def episode(rig: Rig, episode_id: str) -> dict[str, Any]:
    (row,) = [row for row in rig.rows("decision_episodes") if row["episode_id"] == episode_id]
    return row


def recorded(
    rig: Rig,
    what: str,
    at: datetime = WALL,
    *,
    level: str = "silent",
    p: float | None = None,
    answers: Mapping[str, Answer] | None = None,
    shadow: ShadowSummary | None = None,
    trial: TrialSummary | None = None,
    feedback: str | None = None,
    candidate: str = "tasks_due",
    reason: str | None = None,
) -> str:
    """Record one "now" decision of the rig's scope at `at`, as a cycle would but with no item;
    answer its episode id. With `p` the Decision model made it, storing `answers` (with a
    `shadow` and a `trial` summary) and `p` as its terms; else the rules did. `feedback`
    labels it as the user would, at `at`, with `reason` as the Not useful reason given."""
    entry = EpisodeInput(
        candidate=candidate,
        evidence={"what": what},
        policy="rules" if p is None else "model",
        policy_version=POLICY_VERSION,
        level=level,
        timing="now",
        work="none",
        reason="rules:due_or_overdue" if p is None else f"model:{level}",
        item_key=item_key("tasks_due", TASKS, what),
        model=None if p is None else MODEL_IDS["pinned"],
        answers_json=(
            None if p is None else encode_answers(answers or {}, shadow=shadow, trial=trial)
        ),
        terms=None if p is None else {"p": p},
    )
    day = at.astimezone().date().isoformat()
    (made,) = rig.store.record(rig.scope, [entry], trigger="change", now=at, day=day)
    if feedback is not None:
        rig.store.feedback(
            rig.scope,
            episode_id=made.episode_id,
            value=feedback,
            reason=reason,
            source="page",
            now=at,
        )
    return made.episode_id


def at_start(block: Mapping[str, Any]) -> bool:
    """Whether every number of a page's learning block reads its start value."""
    rows = [block["bias"], *block["weights"], *block["topics"], *block["reach"], *block["costs"]]
    return all(row["now"] == row["start"] for row in rows)


# ---- the launcher ------------------------------------------------------------------------------


async def test_open_launcher_runs_a_cycle_first(rig: Rig) -> None:
    rig.observation = observation(task(SEEDLINGS))
    before = await rig.service.launcher()
    assert (before["items"], before["checked_at"], before["glow"]) == ([], None, 0)
    view = await rig.service.open_launcher()
    key = item_key("tasks_due", TASKS, SEEDLINGS)
    assert [(item["item_key"], item["level"]) for item in view["items"]] == [(key, "list")]
    assert whats(view) == ["A task is due today."]
    assert (view["state"], view["level"], view["open"], view["count"]) == ("on", "list", 1, 0)
    assert (view["checked_at"], view["model"], view["glow"], view["nudge"]) == (
        stamp(WALL),
        "no_key",
        1,
        0,
    )
    assert (view["working"], view["started"]) == (0, [])


async def test_the_launcher_lists_what_the_owner_should_see_now(rig: Rig) -> None:
    put(rig, "Listed")
    put(rig, "Held back", level="silent")
    put(rig, "Later", level="badge", timing="at", defer_until=WALL + timedelta(hours=1))
    put(rig, "While typing", level="nudge", timing="after_focus", kind="ask")
    view = await rig.service.launcher()
    # Silent and not-yet-due items stay out; a nudge held for focus shows as a badge, which
    # asks, loudest first.
    assert [(item["what"], item["level"]) for item in view["items"]] == [
        ("While typing", "badge"),
        ("Listed", "list"),
    ]
    assert (view["level"], view["count"], view["open"]) == ("question", 1, 2)
    assert (await rig.service.page())["status"]["open"] == 2

    # At its time the deferred item shows, here at its own level, a badge.
    rig.clock.advance(timedelta(hours=1).total_seconds())
    view = await rig.service.launcher()
    assert sorted(whats(view)) == ["Later", "Listed", "While typing"]
    assert [item["level"] for item in view["items"]] == ["badge", "badge", "list"]


async def test_a_held_nudge_past_its_time_lists_as_a_badge(rig: Rig) -> None:
    # The cycle applies the hourly nudge cap, so until one decides it again a nudge held to a
    # time (quiet hours) lists no louder than a badge; a quieter item keeps its level.
    put(rig, "Quiet hours", level="nudge", timing="at", defer_until=WALL + timedelta(hours=1))
    put(rig, "Listed later", level="list", timing="at", defer_until=WALL + timedelta(hours=1))
    assert whats(await rig.service.launcher()) == []
    rig.clock.advance(timedelta(hours=1).total_seconds())
    view = await rig.service.launcher()
    assert [(item["what"], item["level"]) for item in view["items"]] == [
        ("Quiet hours", "badge"),
        ("Listed later", "list"),
    ]
    assert (view["level"], view["count"], view["open"]) == ("badge", 1, 2)


async def test_an_item_held_in_context_shows_once_its_note_or_chat_opens(rig: Rig) -> None:
    note = (ActionSpec("open_note", "Open note", True, workspace_id=SCOPE, ref=TASKS),)
    put(rig, "Note", timing="in_context", actions=note)
    put(rig, "Chat", timing="in_context", evidence=(EvidenceLabel("A chat", session_id=CHAT),))
    assert whats(await rig.service.launcher()) == []

    # Its note opens: the item shows, and keeps showing once the user moves on.
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))
    assert whats(await rig.service.launcher()) == ["Note"]
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, ELSEWHERE)))
    assert whats(await rig.service.launcher()) == ["Note"]

    # A chat opened and left between two looks counts too.
    await rig.service.presence(WINDOW, looking_at(chat=CHAT))
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, ELSEWHERE)))
    assert sorted(whats(await rig.service.launcher())) == ["Chat", "Note"]

    # A new decision that holds the item again hides it again, until its note opens again.
    put(rig, "Note", timing="in_context", actions=note, version=2)
    assert whats(await rig.service.launcher()) == ["Chat"]
    assert (await rig.service.page())["status"]["open"] == 1
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))
    assert sorted(whats(await rig.service.launcher())) == ["Chat", "Note"]


async def test_in_context_release_holds_across_a_new_episode_while_still_open(rig: Rig) -> None:
    # The chat or note open *now* keeps an in-context item released through a new episode that
    # holds it again, even with no fresh open event in between -- `_here` must count both the
    # opened-since log and the window's current chat and note, not just the log.
    note = (ActionSpec("open_note", "Open note", True, workspace_id=SCOPE, ref=TASKS),)
    chat_evidence = (EvidenceLabel("A chat", session_id=CHAT),)
    put(rig, "Note", timing="in_context", actions=note)
    put(rig, "Chat", timing="in_context", evidence=chat_evidence)
    assert whats(await rig.service.launcher()) == []

    # The note opens and releases "Note". A new episode holds it again while the note stays
    # open and sends no fresh open event; it must still show.
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))
    assert whats(await rig.service.launcher()) == ["Note"]
    put(rig, "Note", timing="in_context", actions=note, version=2)
    assert whats(await rig.service.launcher()) == ["Note"]

    # The same for a chat left open across its own item's new episode.
    await rig.service.presence(WINDOW, looking_at(chat=CHAT))
    assert sorted(whats(await rig.service.launcher())) == ["Chat", "Note"]
    put(rig, "Chat", timing="in_context", evidence=chat_evidence, version=2)
    assert sorted(whats(await rig.service.launcher())) == ["Chat", "Note"]


async def test_visible_hides_changes(rig: Rig) -> None:
    """A vault change (`CHANGES`) never lists in the launcher, at any level or timing, and the
    page's open count leaves it out: the change proposal the engine files for it shows in
    Approvals, whose own `approvals` item counts the open ones."""
    note = (ActionSpec("open_note", "Open note", True, workspace_id=SCOPE, ref=TASKS),)
    put(rig, "Listed")
    for change in CHANGES:
        put(rig, f"Loud {change}", candidate=change, level="nudge", kind="suggest")
    put(
        rig,
        "Held change",
        candidate="tasks_ticked",
        level="badge",
        timing="at",
        defer_until=WALL + timedelta(hours=1),
        kind="suggest",
    )
    put(rig, "Change in context", candidate="note_link", timing="in_context", actions=note)
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))
    for _ in range(2):  # now, and again once the held change's time has come
        view = await rig.service.launcher()
        assert [(item["what"], item["level"]) for item in view["items"]] == [("Listed", "list")]
        assert (view["level"], view["count"], view["open"]) == ("list", 0, 1)
        assert (await rig.service.page())["status"]["open"] == 1
        rig.clock.advance(timedelta(hours=1).total_seconds())


async def test_a_chat_only_workspace_lists_no_items(tmp_path: Path) -> None:
    rig = Rig(tmp_path, scope=SCRATCH_WORKSPACE_ID, chat_only=True)
    listed = put(rig, "Listed", level="badge")
    view = await rig.service.launcher()
    assert (view["items"], view["open"], view["count"], view["level"]) == ([], 0, 0, "idle")
    assert (await rig.service.page())["status"]["open"] == 0
    with pytest.raises(MissionError) as refused:
        await rig.service.start_mission(listed.item_key, "prepare")
    assert refused.value.code == "no_folder" and rig.host.calls == []


async def test_a_chat_only_workspace_takes_no_proposal(tmp_path: Path) -> None:
    """Nothing there to decide on: a proposal is not_applicable, though a wording the check
    refuses is refused as such first."""
    rig = Rig(tmp_path, scope=SCRATCH_WORKSPACE_ID, chat_only=True)
    for question, code in (
        (PROPOSED, "not_applicable"),
        ({**PROPOSED, "type": "score"}, "invalid_request"),
    ):
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.create_proposal("goal_link", question)
        assert refused.value.code == code
    assert rig.rows("decision_proposals") == [] and rig.engine.wakes == []


async def test_presence_wakes_the_engine_on_a_change(rig: Rig) -> None:
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))
    await rig.service.presence(WINDOW, looking_at(note=(SCOPE, TASKS)))  # nothing changed
    await rig.service.presence(WINDOW, looking_at(chat=CHAT))
    assert rig.engine.wakes == ["context", "context"]


async def test_launcher_review(rig: Rig) -> None:
    # The weekly check card counts last week's held-back decisions the user has not labelled,
    # and is due until they are labelled or the user skips the week.
    last_week = [recorded(rig, f"Held {n}", WALL - WEEK - timedelta(hours=n)) for n in range(2)]
    recorded(rig, "Held this week", WALL - timedelta(hours=1))
    recorded(rig, "Shown last week", WALL - WEEK, level="list")
    assert (await rig.service.launcher())["review"] == {"due": True, "count": 2}

    view = await rig.service.feedback(held(last_week[0], "right_to_hold"))
    assert view["review"] == {"due": True, "count": 1}
    await rig.service.preference("review_seen", None)  # Skip this week
    assert (await rig.service.launcher())["review"] == {"due": False, "count": 1}

    # Next week's check holds this week's decision, and the skip was last week's.
    rig.clock.advance(WEEK.total_seconds())
    assert (await rig.service.launcher())["review"] == {"due": True, "count": 1}


# ---- feedback ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason", "state"),
    [
        ("useful", None, "acted"),
        ("wrong", "already_knew", "dismissed"),
        ("mute", None, "dismissed"),
    ],
)
async def test_feedback_on_a_shown_item_closes_it(
    rig: Rig, value: str, reason: str | None, state: str
) -> None:
    listed, _held = await decided(rig)
    view = await rig.service.feedback(shown(listed.item_key, value, reason))
    assert (view["items"], view["open"]) == ([], 0)
    found = rig.agenda.item(SCOPE, listed.item_key)
    assert found is not None and found.state == state
    row = episode(rig, listed.episode_id)
    assert (row["feedback"], row["feedback_reason"], row["feedback_source"]) == (
        value,
        reason,
        "launcher",
    )
    assert rig.engine.wakes == ["change"]
    assert (await rig.service.page())["muted"] == (["tasks_due"] if value == "mute" else [])


async def test_later_keeps_the_item_open_until_its_time(rig: Rig) -> None:
    listed, _held = await decided(rig)
    view = await rig.service.feedback(shown(listed.item_key, "later"))
    assert view["items"] == []
    found = rig.agenda.item(SCOPE, listed.item_key)
    assert found is not None
    assert (found.state, found.timing, found.defer_until) == ("open", "at", stamp(WALL + LATER))
    assert episode(rig, listed.episode_id)["feedback"] == "later"
    assert rig.store.preferences(SCOPE, WALL).later == {"tasks_due": WALL + LATER}
    assert rig.engine.wakes == ["change"]

    rig.clock.advance((LATER - timedelta(seconds=1)).total_seconds())
    assert whats(await rig.service.launcher()) == []
    rig.clock.advance(1)
    assert [item["item_key"] for item in (await rig.service.launcher())["items"]] == [
        listed.item_key
    ]


@pytest.mark.parametrize("value", ["missed", "right_to_hold"])
async def test_feedback_on_a_held_back_episode_labels_it_alone(rig: Rig, value: str) -> None:
    listed, held_back = await decided(rig)
    # A week on, the held-back decision is last week's: the page's weekly check lists it.
    rig.clock.advance(WEEK.total_seconds())
    checked = (await rig.service.page())["held_back"]
    assert [(row["episode_id"], row["why"], row["feedback"]) for row in checked] == [
        (held_back.episode_id, held_back.body.why, None)
    ]
    view = await rig.service.feedback(held(held_back.episode_id, value))
    assert [item["item_key"] for item in view["items"]] == [listed.item_key]
    assert view["review"] == {"due": False, "count": 0}  # nothing left to check
    row = episode(rig, held_back.episode_id)
    assert (row["feedback"], row["feedback_source"]) == (value, "page")
    found = rig.agenda.item(SCOPE, held_back.item_key)
    assert found is not None and (found.state, found.timing) == ("open", "now")
    assert [row["feedback"] for row in (await rig.service.page())["held_back"]] == [value]
    assert rig.engine.wakes == ["change"]


async def test_feedback_refusals_change_nothing(rig: Rig) -> None:
    listed, held_back = await decided(rig)
    refusals = [
        (shown(item_key("tasks_due", TASKS, "Not a task"), "useful"), "unknown_item"),
        (shown(listed.item_key, "missed"), "not_applicable"),  # a held value on a shown item
        (shown(held_back.item_key, "useful"), "not_applicable"),  # a shown value on a held one
        (held(listed.episode_id, "right_to_hold"), "not_applicable"),
        (held(MISSING, "missed"), "unknown_item"),
        (FeedbackRequest(None, None, "useful", None, "launcher"), "invalid_request"),
        (
            FeedbackRequest(listed.item_key, listed.episode_id, "useful", None, "launcher"),
            "invalid_request",
        ),
    ]
    for request, code in refusals:
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.feedback(request)
        assert refused.value.code == code, request
    for found in (listed, held_back):
        assert episode(rig, found.episode_id)["feedback"] is None
        assert rig.agenda.item(SCOPE, found.item_key) == found
    assert rig.engine.wakes == []

    # A closed item takes no more feedback.
    await rig.service.feedback(shown(listed.item_key, "useful"))
    with pytest.raises(DecisionLoopError) as closed:
        await rig.service.feedback(shown(listed.item_key, "wrong"))
    assert closed.value.code == "not_applicable"
    assert episode(rig, listed.episode_id)["feedback"] == "useful"
    assert rig.engine.wakes == ["change"]


async def test_useful_leaves_an_item_that_offers_a_start_open(rig: Rig) -> None:
    """Useful labels an item that offers a start and leaves it open, so its start still works;
    Not useful still closes such an item, and Useful still closes one with no start."""
    start = (ActionSpec("start_prepare", "Prepare a plan", True),)
    offered = put(rig, "Offered", actions=start)
    view = await rig.service.feedback(shown(offered.item_key, "useful"))
    assert [item["item_key"] for item in view["items"]] == [offered.item_key]
    assert episode(rig, offered.episode_id)["feedback"] == "useful"
    mission = await rig.service.start_mission(offered.item_key, "prepare")
    assert mission["state"] == "running"

    turned_down = put(rig, "Turned down", actions=start)
    plain = put(rig, "Plain")
    await rig.service.feedback(shown(turned_down.item_key, "wrong"))
    await rig.service.feedback(shown(plain.item_key, "useful"))
    found = [rig.agenda.item(SCOPE, item.item_key) for item in (turned_down, plain)]
    assert [item.state if item else None for item in found] == ["dismissed", "acted"]


LOOKED_AT = "00000000-0000-4000-8000-0000000000e1"  # the user's chat, its last reply worth a look
TURN = "00000000-0000-4000-8000-0000000000e2"
HINT = SecondLookHint(LOOKED_AT, TURN, "long_session", "Garden plan", ("notes/garden-plan.md",))
OFFER = item_key("long_task_look", LOOKED_AT, TURN)


async def test_useful_leaves_an_allowed_item_open_for_vikis_own_start(rig: Rig) -> None:
    """A Stop all holds VIKI's own start of an Allowed second look, which offers the user no
    start: Useful labels it and leaves it open, and VIKI's own start runs it once the hold
    ends."""
    page = PageFacts(0, 0, 0, "unknown", None, None, None, None, second_look=HINT)
    rig.observation = replace(observation(), page=page)
    rig.engine.note_stop_all()
    await rig.engine.recheck()
    offer = rig.agenda.item(SCOPE, OFFER)
    assert offer is not None and (offer.state, offer.mission_id) == ("open", None)
    assert [action.type for action in offer.body.actions] == ["open_chat"]

    await rig.service.feedback(shown(OFFER, "useful"))
    kept = rig.agenda.item(SCOPE, OFFER)
    assert kept is not None and kept.state == "open"
    assert episode(rig, offer.episode_id)["feedback"] == "useful"

    rig.clock.advance(OBSERVE_S, wall=False)  # a Stop all holds VIKI's starts for a cadence
    await rig.engine.recheck()
    (mission,) = rig.missions.running()
    assert (mission.item_key, mission.work) == (OFFER, "prepare")


async def test_useful_closes_an_allowed_item_whose_mission_vikis_start_holds(rig: Rig) -> None:
    """Once VIKI's own start runs an Allowed second look, its work cannot start again: Useful
    labels the offer, still open, and closes it."""
    page = PageFacts(0, 0, 0, "unknown", None, None, None, None, second_look=HINT)
    rig.observation = replace(observation(), page=page)
    await rig.engine.recheck()
    (mission,) = rig.missions.running()
    offer = rig.agenda.item(SCOPE, OFFER)
    assert offer is not None and (offer.state, mission.item_key) == ("open", OFFER)

    await rig.service.feedback(shown(OFFER, "useful"))
    closed = rig.agenda.item(SCOPE, OFFER)
    assert closed is not None and closed.state == "acted"
    assert episode(rig, offer.episode_id)["feedback"] == "useful"


# ---- page events, reports and preferences ------------------------------------------------------


async def test_observe_stamps_items_and_their_episodes(rig: Rig) -> None:
    listed, held_back = await decided(rig)
    # The most keys one observe takes, as many as the launcher lists: the first and the last
    # are both stamped.
    strangers = [item_key("stranger", str(n)) for n in range(MAX_OPEN_ITEMS - 2)]
    await rig.service.observe("shown", [listed.item_key, *strangers, held_back.item_key])
    for item in (listed, held_back):
        found = rig.agenda.item(SCOPE, item.item_key)
        assert found is not None and found.shown_at == stamp(WALL)
    row = episode(rig, listed.episode_id)
    assert (row["exposures"], row["first_exposed_at"]) == (1, stamp(WALL))

    rig.clock.advance(60)
    later = stamp(WALL + timedelta(seconds=60))
    await rig.service.observe("opened", [listed.item_key, held_back.item_key])
    await rig.service.observe("acted", [listed.item_key])
    (item_row,) = [row for row in rig.rows("decision_items") if row["item_key"] == listed.item_key]
    row = episode(rig, listed.episode_id)
    assert (item_row["opened_at"], row["opened_at"], row["acted_at"]) == (later, later, later)
    assert item_row["state"] == "open"  # acting on an item is no feedback
    assert episode(rig, held_back.episode_id)["opened_at"] is None  # held back: no page event
    with pytest.raises(DecisionLoopError) as refused:
        await rig.service.observe("dismissed", [listed.item_key])
    assert refused.value.code == "invalid_request"
    assert rig.engine.wakes == []


async def test_reports_and_preferences_answer_the_page(rig: Rig) -> None:
    page = await rig.service.report("unwatched", "page")
    assert (page["counts"]["missed"], page["counts"]["shown"]) == (1, 1)
    (row,) = rig.rows("decision_episodes")
    assert (row["entry"], row["candidate"], row["trigger"], row["feedback_source"]) == (
        "report",
        "unwatched",
        "change",
        "page",
    )

    listed, _held = await decided(rig)
    await rig.service.feedback(shown(listed.item_key, "mute"))
    assert (await rig.service.page())["muted"] == ["tasks_due"]
    assert (await rig.service.preference("unmute", "tasks_due"))["muted"] == []
    await rig.service.preference("review_seen", None)
    assert rig.store.review_seen_at(SCOPE) == WALL
    refusals = (
        ("unmute", None),
        ("review_seen", "inbox"),
        ("reset_learning", "inbox"),
        ("study", None),
    )
    for name, candidate in refusals:
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.preference(name, candidate)
        assert refused.value.code == "invalid_request"
    assert rig.engine.wakes == ["change"] * 4


async def test_page_counts_the_day(rig: Rig) -> None:
    listed, held_back = await decided(rig)
    midnight = datetime.combine(TODAY, time()).astimezone()
    other = item_key("tasks_due", TASKS, "Another task")
    calls = rig.agenda.record_call
    item_newest = calls(
        SCOPE, kept_call(listed.item_key, held_back=1, tokens=200), midnight + timedelta(minutes=2)
    )
    newest = calls(
        SCOPE, kept_call(other, held_back=3, tokens=None), midnight + timedelta(minutes=3)
    )
    # Each request adds to its scope's day counter; the page sums every scope's for today, as
    # the cap sums spend, and leaves another day's out.
    usage = rig.store.add_usage
    usage(SCOPE, DAY, calls=2, shadow_calls=1, tokens=300, held_back=2, micro_usd=1_234, now=WALL)
    usage(SCOPE, DAY, calls=1, shadow_calls=0, tokens=200, held_back=1, micro_usd=0, now=WALL)
    usage(
        OTHER_SCOPE, DAY, calls=1, shadow_calls=0, tokens=0, held_back=1, micro_usd=1_000, now=WALL
    )
    usage(
        THIRD_SCOPE,
        YESTERDAY,
        calls=9,
        shadow_calls=9,
        tokens=900,
        held_back=9,
        micro_usd=9,
        now=WALL,
    )
    mission = await rig.service.start_mission(listed.item_key, "prepare")

    page = await rig.service.page()
    assert page["status"] == {"open": 1, "checked_at": stamp(WALL)}
    assert page["today"] == {
        "decisions": 2,
        "model_calls": 4,
        "tokens": 500,
        "cost_usd": 2_234 / 1_000_000,
        "cap_usd": 0.5,
        "capped": False,
        "shadow_calls": 1,
    }
    # The newest call measured nothing: its tokens are its split's estimate.
    assert page["privacy"] == {
        "last_call": {
            "call_id": newest,
            "at": stamp(midnight + timedelta(minutes=3)),
            "tokens": 12,
        },
        "held_back_today": 4,
    }
    assert page["model"]["key"] == "missing"
    assert page["model"]["version"] == {"setting": "pinned", "display": "1.14.0"}
    assert page["held_back"] == []  # today's held-back decision waits for next week's check
    assert {row["episode_id"] for row in page["recent"]} == {
        listed.episode_id,
        held_back.episode_id,
    }
    assert (page["counts"]["shown"], page["data"]["episodes"]) == (1, 2)
    assert page["missions"] == {
        "running": [mission],
        "today": {"prepare": 1, "run": 0},
        "limits": {"prepare": 5, "run": 3},
        "hold": None,
        "team": {"today": 0, "limit": 4, "running": 0},
        "look": {"today": 0, "limit": 3},
        "second_look": {"today": 0, "limit": 3},
        "project_plan": {"today": 0, "limit": 3},
        "inbox": {"today": 0, "limit": 3},
        "checks": None,
    }

    # Once the newest call's body no longer opens, the page names the newest one that does; the
    # day's counts stay the counter's.
    with rig.state._connection(write=True) as connection:
        connection.execute("UPDATE decision_calls SET body=? WHERE call_id=?", (b"\0" * 64, newest))
    page = await rig.service.page()
    assert page["privacy"] == {
        "last_call": {
            "call_id": item_newest,
            "at": stamp(midnight + timedelta(minutes=2)),
            "tokens": 200,
        },
        "held_back_today": 4,
    }
    assert (page["today"]["model_calls"], page["today"]["tokens"]) == (4, 500)


async def test_today_counts_beyond_twenty_calls(rig: Rig) -> None:
    # A cycle counts each request in the day as it makes it. The agenda keeps only the newest
    # MAX_CALLS call records; today's numbers come from the day counter, so they go past them.
    await rig.service.save_key(KEY)
    rig.model.answers = dict(STRONG)
    rig.observation = observation(task(SEEDLINGS))
    await rig.engine.recheck()  # the playbook's questions, then the reversed kind question
    made = rig.agenda.calls(SCOPE)
    tokens = sum(call.input_tokens or 0 for call in made)
    page = await rig.service.page()
    assert len(made) == 2 and tokens > 0
    assert (page["today"]["model_calls"], page["today"]["tokens"]) == (2, tokens)
    held_back_today = page["privacy"]["held_back_today"]

    listed = item_key("tasks_due", TASKS, SEEDLINGS)
    for n in range(MAX_CALLS):  # later requests today, the first one in the shadow
        rig.agenda.record_call(
            SCOPE, kept_call(listed, held_back=1, tokens=10), WALL + timedelta(minutes=n + 1)
        )
        rig.store.add_usage(
            SCOPE,
            DAY,
            calls=1,
            shadow_calls=1 if n == 0 else 0,
            tokens=10,
            held_back=1,
            micro_usd=0,
            now=WALL,
        )
    assert len(rig.agenda.calls(SCOPE)) == MAX_CALLS
    page = await rig.service.page()
    assert page["today"]["model_calls"] == 2 + MAX_CALLS
    assert page["today"]["tokens"] == tokens + 10 * MAX_CALLS
    assert page["today"]["shadow_calls"] == 1
    assert page["privacy"]["held_back_today"] == held_back_today + MAX_CALLS


# ---- the key -----------------------------------------------------------------------------------


async def test_save_remove_and_test_the_key(rig: Rig) -> None:
    assert await rig.service.test() == {
        "ok": False,
        "latency_ms": None,
        "version": None,
        "error": "no_key",
    }
    page = await rig.service.save_key(KEY)
    assert page["model"]["key"] == "saved" and rig.keys.load() == KEY
    rig.model.latency_ms = 112
    assert await rig.service.test() == {
        "ok": True,
        "latency_ms": 112,
        "version": "1.14.0",
        "error": None,
    }
    assert (await rig.service.page())["model"]["last_test"] == {
        "at": stamp(WALL),
        "latency_ms": 112,
    }
    page = await rig.service.remove_key()
    assert page["model"]["key"] == "missing" and rig.keys.load() is None
    assert rig.engine.wakes == ["change", "change"]

    # A key the wire's rule refuses is not saved, and the refusal is the code alone.
    for bad in ("", f"{KEY} {KEY}", f"{KEY}{chr(9)}", "k" * 4097):
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.save_key(bad)
        assert refused.value.code == "invalid_request" and refused.value.extra == {}
    assert rig.keys.load() is None and rig.engine.wakes == ["change", "change"]


# ---- the Context tab ---------------------------------------------------------------------------


async def test_calls_and_one_call_in_detail(rig: Rig) -> None:
    assert await rig.service.calls() == {"enabled": True, "latest": None, "calls": []}
    rig.observation = observation(task(SEEDLINGS))
    await rig.engine.recheck()
    assert await rig.service.calls() == {"enabled": True, "latest": "rules", "calls": []}

    # A minute on, with a key, the model decides: the playbook's questions, then the reversed
    # kind question.
    await rig.service.save_key(KEY)
    rig.model.answers = dict(STRONG)
    rig.clock.advance(60)
    await rig.engine.recheck()
    view = await rig.service.calls()
    assert view["latest"] == "model"
    assert [(call["title"], call["candidate"]) for call in view["calls"]] == [
        (SEEDLINGS, "tasks_due")
    ] * 2
    details = [await rig.service.call(call["call_id"]) for call in view["calls"]]
    main, reversal = sorted(details, key=lambda detail: -len(detail["questions"]))
    # A tasks_due call has no own question (`playbook.OWN`), so it asks exactly the playbook's
    # ten, not every name `playbook.LABELS` holds (the two own labels included).
    assert [(entry["name"], entry["label"]) for entry in main["questions"]] == [
        (name, playbook.LABELS[name]) for name in playbook.questions()
    ]
    assert [entry["answer"] for entry in main["questions"]] == [
        playbook.answer_text(name, STRONG[name]) for name in playbook.questions()
    ]
    assert reversal["questions"] == [{"name": "kind", "label": "What kind", "answer": "ask, 0.90"}]
    state = sum(part["tokens"] for part in main["split"] if part["part"] != "questions")
    largest = max(
        playbook.estimate_tokens(playbook.questions_text({name: question}))
        for name, question in playbook.questions().items()
    )
    assert main["largest_question"] == {"used": state + largest, "max": QUESTION_LIMIT}
    assert (main["version"], main["version_setting"], main["outcome"]) == (
        "1.14.0",
        "pinned",
        "answered",
    )
    assert (await rig.service.launcher())["nudge"] == 1
    with pytest.raises(DecisionLoopError) as unknown:
        await rig.service.call(MISSING)
    assert unknown.value.code == "unknown_item"


async def test_an_answer_its_question_cannot_word_is_left_out(rig: Rig) -> None:
    """A stored call whose answers read back but give a question an answer of the wrong shape
    (a body written wrong, never a cycle's own) shows its other answers: a score answer to a
    yes/no question, or to an own question, which has no playbook entry of its own."""
    kept = kept_call(item_key("tasks_due", TASKS, SEEDLINGS), held_back=0, tokens=10)
    score = ScoreAnswer(2.0, 0.9, {2: 0.9, 1: 0.1})
    answers = {"goal_link": NoulAnswer(0.9), "news": score, "worth_note": score}
    body = replace(kept.body, answers_json=encode_answers(answers))
    call_id = rig.agenda.record_call(SCOPE, replace(kept, body=body), WALL)
    detail = await rig.service.call(call_id)
    assert detail["questions"] == [
        {"name": "goal_link", "label": "Linked to a goal?", "answer": "Yes, 0.90"}
    ]


# ---- missions ----------------------------------------------------------------------------------


def holding(use: float, *, status: str = "allowed", reset: int = 2 * 3600) -> dict[str, object]:
    """A plan reading whose five-hour window uses `use` and resets `reset` seconds after WALL."""
    ends = int(WALL.timestamp()) + reset
    window = {"utilization": use, "resets_at": ends}
    return {
        "observed_at": "2026-10-21T08:59:00.000Z",
        "status": status,
        "limiting": "five_hour",
        "utilization": use,
        "resets_at": ends,
        "windows": {"five_hour": window, "seven_day": None},
    }


async def test_the_page_names_the_plan_brake_while_it_holds(rig: Rig) -> None:
    ends = {"until": "2026-10-21T11:00:00.000Z"}
    assert (await rig.service.page())["missions"]["hold"] is None  # a reading far below
    rig.reading = None  # none yet this launch: held, with no end to name
    assert (await rig.service.page())["missions"]["hold"] == {"until": None}
    assert (await rig.service.missions())["hold"] == {"until": None}
    rig.reading = holding(0.9)
    assert (await rig.service.page())["missions"]["hold"] == ends
    assert (await rig.service.missions())["hold"] == ends
    rig.values["decision.plan_brake"] = "95"
    assert (await rig.service.page())["missions"]["hold"] is None
    assert (await rig.service.missions())["hold"] is None
    rig.values["decision.plan_brake"] = "off"
    rig.reading = holding(1.0, status="rejected")
    assert (await rig.service.page())["missions"]["hold"] == ends
    assert (await rig.service.missions())["hold"] == ends
    rig.reading = None  # at every brake, "off" included
    assert (await rig.service.page())["missions"]["hold"] == {"until": None}


async def test_the_owner_starts_while_the_plan_brake_holds(rig: Rig) -> None:
    listed, _held = await decided(rig)
    rig.values["decision.grant.prepare"] = "ask"
    rig.reading = holding(0.99)
    assert (await rig.service.missions())["hold"] is not None
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    assert mission["state"] == "running"
    assert ("create", mission["session_id"]) in rig.host.calls


async def test_the_page_and_missions_block_count_todays_looks(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A started look counts in `look`, not in `prepare` (MissionService.count_looks keeps them
    # apart); the service only passes the day's count on.
    days: list[str] = []

    def count_looks(day: str) -> int:
        days.append(day)
        return 2

    monkeypatch.setattr(rig.missions, "count_looks", count_looks)
    page = await rig.service.page()
    assert page["missions"]["look"] == {"today": 2, "limit": 3}
    assert page["missions"]["today"] == {"prepare": 0, "run": 0}
    assert (await rig.service.missions())["look"] == {"today": 2, "limit": 3}
    assert days == [DAY, DAY]


def second_look_item(rig: Rig, what: str, candidate: str = "long_task_look") -> ItemRecord:
    """An open second look of the rig's scope at a chat, as a cycle lists it."""
    opens = ActionSpec("open_chat", "Open chat", True, session_id=str(uuid.uuid4()))
    return put(rig, what, candidate=candidate, actions=(opens,))


YESTERDAY_AT = WALL - timedelta(days=1)  # inside the window, outside today's count
TICKS = itertools.count()  # one second apart, so the looks have an order


def look_row(
    rig: Rig,
    what: str,
    reason: str,
    *,
    candidate: str = "long_task_look",
    at: datetime | None = None,
) -> None:
    """A second look of the rig's scope that began at `at` and ended `reason`, with its item."""
    key = second_look_item(rig, what, candidate).item_key
    at = at or YESTERDAY_AT + timedelta(seconds=next(TICKS))
    with rig.state._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
            "reason,body,created_at,updated_at,deadline_at,finished_at) "
            "VALUES (?,?,?,?,'prepare','done',?,?,?,?,?,?)",
            (str(uuid.uuid4()), rig.scope, str(uuid.uuid4()), key, reason, b"xx")
            + (stamp(at),) * 4,
        )


def trigger(block: Mapping[str, Any], candidate: str = "long_task_look") -> dict[str, Any]:
    (row,) = [row for row in block["second_looks"]["triggers"] if row["candidate"] == candidate]
    return row


def kind(block: Mapping[str, Any], name: str) -> dict[str, Any]:
    (row,) = [row for row in block["second_looks"]["kinds"] if row["kind"] == name]
    return row


async def test_the_page_counts_todays_second_looks_apart(rig: Rig) -> None:
    # A started second look counts in `second_look` alone: not in `look`, nor in `prepare`.
    listed = second_look_item(rig, "Garden plan")
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    assert mission["state"] == "running"
    expected = {"today": 1, "limit": 3}
    page = await rig.service.page()
    assert page["missions"]["second_look"] == expected
    assert page["missions"]["look"] == {"today": 0, "limit": 3}
    assert page["missions"]["today"] == {"prepare": 0, "run": 0}
    assert (await rig.service.missions())["second_look"] == expected


async def test_learning_shows_second_looks_and_try_again_and_raise_again_restart_them(
    rig: Rig,
) -> None:
    # Twelve long-task looks: 1 accepted, 5 turned down as flaws, 4 rejected otherwise and 2
    # that found nothing, which do not count toward the pause: ten answered pause the type. One
    # older than the window and one of the other type stay out of the counts.
    old = WALL - SECOND_LOOK_WINDOW - timedelta(days=1)
    look_row(rig, "Accepted", "accepted:flaw")
    for n in range(2):
        look_row(rig, f"Nothing {n}", "nothing")
    for n in range(4):
        look_row(rig, f"Approach {n}", "rejected:approach")
    look_row(rig, "Other type", "accepted:flaw", candidate="correction_look")
    for n in range(5):
        look_row(rig, f"Flawed {n}", "rejected:flaw")
    look_row(rig, "Old", "accepted:flaw", at=old)
    recorded(
        rig,
        "Wrong",
        level="badge",
        feedback="wrong",
        reason="already_knew",
        candidate="long_task_look",
    )
    recorded(
        rig,
        "Old wrong",
        old,
        level="badge",
        feedback="wrong",
        reason="bad_timing",
        candidate="long_task_look",
    )
    block = (await rig.service.page())["learning"]
    assert trigger(block) == {
        "candidate": "long_task_look",
        "made": 12,
        "accepted": 1,
        "rejected": 9,
        "nothing": 2,
        "reasons": {"out_of_date": 0, "already_knew": 1, "wrong_topic": 0, "bad_timing": 0},
        "paused": True,
    }
    assert (trigger(block, "correction_look")["made"], kind(block, "flaw")) == (
        1,
        {"kind": "flaw", "rejected_in_a_row": 5, "paused": True},
    )
    assert kind(block, "approach")["paused"] is False

    # Learning off pauses nothing; the counts stay.
    rig.values["decision.learning"] = False
    off = (await rig.service.page())["learning"]
    assert (trigger(off)["made"], trigger(off)["paused"], kind(off, "flaw")["paused"]) == (
        12,
        False,
        False,
    )
    rig.values["decision.learning"] = True

    # Try again and Raise again each restart one count from now, wake the engine and answer the
    # page; the other type, the other kind and the user's bounds stay.
    rig.clock.advance(60)
    page = await rig.service.preference("try_again", "long_task_look")
    assert (trigger(page["learning"])["made"], trigger(page["learning"])["paused"]) == (0, False)
    assert trigger(page["learning"], "correction_look")["made"] == 1
    assert kind(page["learning"], "flaw")["paused"] is True
    page = await rig.service.preference("raise_again", "flaw")
    assert kind(page["learning"], "flaw") == {
        "kind": "flaw",
        "rejected_in_a_row": 0,
        "paused": False,
    }
    assert rig.engine.wakes == ["change"] * 2
    assert page["missions"]["second_look"]["limit"] == 3
    assert rig.store.get_json(OTHER_SCOPE, "second_looks") is None
    stored = cast(dict[str, Any], rig.store.get_json(SCOPE, "second_looks"))
    assert set(stored["looks"]) == {"long_task_look"} and set(stored["kinds"]) == {"flaw"}

    # A name that is not one is refused and stores nothing more.
    for name, value in (
        ("try_again", None),
        ("try_again", "tasks_due"),
        ("raise_again", "other"),
        ("raise_again", None),
    ):
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.preference(name, value)
        assert refused.value.code == "invalid_request"
    assert rig.store.get_json(SCOPE, "second_looks") == stored
    assert rig.engine.wakes == ["change"] * 2


async def test_reset_and_delete_all_restart_second_look_counts(rig: Rig) -> None:
    look_row(rig, "Accepted", "accepted:flaw")
    recorded(
        rig,
        "Wrong",
        level="badge",
        feedback="wrong",
        reason="already_knew",
        candidate="long_task_look",
    )
    assert trigger((await rig.service.page())["learning"])["made"] == 1

    rig.clock.advance(60)
    await rig.service.preference("raise_again", "missing")
    after = (await rig.service.preference("reset_learning", None))["learning"]
    # Reset: looks and reasons from before it no longer count; the restart stamps stay.
    assert trigger(after)["made"] == 0 and trigger(after)["reasons"]["already_knew"] == 0
    assert rig.store.get_json(SCOPE, "second_looks") is not None

    # Delete all drops the items, so the looks that joined them read none.
    look_row(rig, "Afterwards", "nothing", at=WALL + timedelta(seconds=60))
    assert trigger((await rig.service.page())["learning"])["made"] == 1
    await rig.service.delete_all()
    assert trigger((await rig.service.page())["learning"])["made"] == 0


async def test_the_page_counts_todays_project_plans_apart(rig: Rig) -> None:
    # A started project plan counts in `project_plan` alone: not in `second_look`, nor `prepare`.
    listed = put(
        rig,
        "Garden",
        candidate="stalled_project",
        evidence=(
            EvidenceLabel("Garden", workspace_id=rig.scope, ref="10-projects/garden/garden.md"),
        ),
    )
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    assert mission["state"] == "running"
    expected = {"today": 1, "limit": 3}
    page = await rig.service.page()
    assert page["missions"]["project_plan"] == expected
    assert page["missions"]["second_look"] == {"today": 0, "limit": 3}
    assert page["missions"]["today"] == {"prepare": 0, "run": 0}
    assert (await rig.service.missions())["project_plan"] == expected


def plan_trigger(block: Mapping[str, Any], candidate: str) -> dict[str, Any]:
    (row,) = [row for row in block["project_plans"] if row["candidate"] == candidate]
    return row


async def test_learning_shows_project_plans_and_try_again_restarts_them(rig: Rig) -> None:
    # Ten stalled-project plans: 1 accepted, 5 turned down, 2 found nothing, 2 rejected; a
    # second look and an old plan stay out of the counts.
    old = WALL - SECOND_LOOK_WINDOW - timedelta(days=1)
    look_row(rig, "Accepted", "accepted:flaw", candidate="stalled_project")
    for n in range(2):
        look_row(rig, f"Nothing {n}", "nothing", candidate="stalled_project")
    for n in range(7):
        look_row(rig, f"Rejected {n}", "rejected:flaw", candidate="stalled_project")
    look_row(rig, "Old", "accepted:flaw", candidate="stalled_project", at=old)
    look_row(rig, "Other type", "nothing", candidate="overdue_tasks")
    recorded(
        rig,
        "Wrong",
        level="badge",
        feedback="wrong",
        reason="bad_timing",
        candidate="stalled_project",
    )
    block = (await rig.service.page())["learning"]
    assert [row["candidate"] for row in block["project_plans"]] == list(PROJECT_PLANS)
    assert plan_trigger(block, "stalled_project") == {
        "candidate": "stalled_project",
        "made": 10,
        "accepted": 1,
        "rejected": 7,
        "nothing": 2,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 0, "bad_timing": 1},
        "paused": True,
    }
    assert plan_trigger(block, "overdue_tasks")["made"] == 1
    # The second looks' block does not count them.
    assert all(row["made"] == 0 for row in block["second_looks"]["triggers"])

    # Try again restarts the project plan type alone and leaves "second_looks" unwritten.
    rig.clock.advance(60)
    page = await rig.service.preference("try_again", "stalled_project")
    assert plan_trigger(page["learning"], "stalled_project")["made"] == 0
    assert plan_trigger(page["learning"], "stalled_project")["paused"] is False
    assert plan_trigger(page["learning"], "overdue_tasks")["made"] == 1
    assert rig.engine.wakes == ["change"]
    assert rig.store.get_json(SCOPE, "second_looks") is None
    assert rig.store.get_json(OTHER_SCOPE, "project_plans") is None
    stored = cast(dict[str, Any], rig.store.get_json(SCOPE, "project_plans"))
    assert set(stored["looks"]) == {"stalled_project"} and stored["kinds"] == {}

    # A second look's Try again leaves the project plans' preference as it is.
    await rig.service.preference("try_again", "long_task_look")
    assert rig.store.get_json(SCOPE, "project_plans") == stored
    assert set(cast(dict[str, Any], rig.store.get_json(SCOPE, "second_looks"))["looks"]) == {
        "long_task_look"
    }

    # A project plan has no kinds to raise again.
    with pytest.raises(DecisionLoopError) as refused:
        await rig.service.preference("raise_again", "stalled_project")
    assert refused.value.code == "invalid_request"
    assert rig.store.get_json(SCOPE, "project_plans") == stored


async def test_reset_and_delete_all_restart_project_plan_counts(rig: Rig) -> None:
    look_row(rig, "Accepted", "accepted:flaw", candidate="stalled_project")
    recorded(
        rig,
        "Wrong",
        level="badge",
        feedback="wrong",
        reason="bad_timing",
        candidate="stalled_project",
    )
    before = (await rig.service.page())["learning"]
    assert plan_trigger(before, "stalled_project")["made"] == 1

    rig.clock.advance(60)
    await rig.service.preference("try_again", "overdue_tasks")
    after = (await rig.service.preference("reset_learning", None))["learning"]
    # Reset: plans and reasons from before it no longer count; the restart stamps stay.
    assert plan_trigger(after, "stalled_project")["made"] == 0
    assert plan_trigger(after, "stalled_project")["reasons"]["bad_timing"] == 0
    assert rig.store.get_json(SCOPE, "project_plans") is not None

    look_row(rig, "Afterwards", "nothing", candidate="stalled_project", at=WALL + timedelta(60))
    assert plan_trigger((await rig.service.page())["learning"], "stalled_project")["made"] == 1
    await rig.service.delete_all()
    assert plan_trigger((await rig.service.page())["learning"], "stalled_project")["made"] == 0


def draft_item(
    rig: Rig, what: str, outcome: str = "", *, candidate: str = "context_handoff", scope: str = ""
) -> ItemRecord:
    """A message draft the launcher showed, as `outcome` left it: "used" (Useful), "wrong" (Not
    useful), "nothing" (closed unused), "empty" (an inbox suggestion run that found nothing, left
    "done") or "" (still open)."""
    scope = scope or rig.scope
    listed = put(rig, what, candidate=candidate, scope=scope)
    with rig.state._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_items SET shown_at=created_at WHERE scope=? AND item_key=?",
            (scope, listed.item_key),
        )
    if outcome in ("used", "wrong"):
        rig.store.feedback(
            scope,
            episode_id=listed.episode_id,
            value="useful" if outcome == "used" else "wrong",
            reason="bad_timing" if outcome == "wrong" else None,
            source="page",
            now=WALL,
        )
    if outcome == "nothing":
        rig.agenda.set_state(scope, listed.item_key, "dismissed", WALL)
    if outcome == "empty":
        rig.agenda.set_state(scope, listed.item_key, "done", WALL)
    return listed


def draft_trigger(block: Mapping[str, Any], candidate: str) -> dict[str, Any]:
    (row,) = [row for row in block["message_drafts"] if row["candidate"] == candidate]
    return row


async def test_the_page_counts_todays_message_drafts_across_workspaces(rig: Rig) -> None:
    # Drafts raised today in any workspace count, whatever their state; yesterday's, a second
    # look and a project plan do not.
    draft_item(rig, "Handoff", "used")
    draft_item(rig, "Sources", candidate="source_note")
    draft_item(rig, "Elsewhere", "nothing", scope=OTHER_SCOPE)
    old = draft_item(rig, "Old")
    second_look_item(rig, "A look")
    put(rig, "A plan", candidate="stalled_project")
    with rig.state._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_items SET created_at=? WHERE item_key=?",
            (stamp(YESTERDAY_AT), old.item_key),
        )
    page = await rig.service.page()
    assert page["message_drafts"] == {"today": 3, "limit": 3}
    assert page["missions"]["project_plan"] == {"today": 0, "limit": 3}


async def test_learning_shows_message_drafts_and_try_again_restarts_them(rig: Rig) -> None:
    # Ten handoffs: 1 used, 2 closed unused, 7 turned down; an unshown one, a source note's, a
    # second look's and another workspace's stay out of the handoff counts.
    draft_item(rig, "Used", "used")
    for n in range(2):
        draft_item(rig, f"Nothing {n}", "nothing")
    for n in range(7):
        draft_item(rig, f"Wrong {n}", "wrong")
    draft_item(rig, "Elsewhere", "used", scope=OTHER_SCOPE)
    draft_item(rig, "Sources", "used", candidate="source_note")
    put(rig, "Unshown", candidate="context_handoff")  # no shown_at, so no row
    look_row(rig, "A look", "accepted:flaw")
    block = (await rig.service.page())["learning"]
    assert [row["candidate"] for row in block["message_drafts"]] == list(MESSAGE_DRAFTS)
    assert draft_trigger(block, "context_handoff") == {
        "candidate": "context_handoff",
        "made": 10,
        "accepted": 1,
        "rejected": 7,
        "nothing": 2,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 0, "bad_timing": 7},
        "paused": True,
    }
    assert draft_trigger(block, "source_note")["made"] == 1
    assert trigger(block)["made"] == 1

    # Try again restarts the handoff alone; the other two preferences stay unwritten.
    rig.clock.advance(60)
    page = await rig.service.preference("try_again", "context_handoff")
    assert draft_trigger(page["learning"], "context_handoff")["made"] == 0
    assert draft_trigger(page["learning"], "context_handoff")["paused"] is False
    assert draft_trigger(page["learning"], "source_note")["made"] == 1
    assert rig.engine.wakes == ["change"]
    assert rig.store.get_json(SCOPE, "second_looks") is None
    assert rig.store.get_json(SCOPE, "project_plans") is None
    assert rig.store.get_json(OTHER_SCOPE, "message_drafts") is None
    stored = cast(dict[str, Any], rig.store.get_json(SCOPE, "message_drafts"))
    assert set(stored["looks"]) == {"context_handoff"} and stored["kinds"] == {}

    # A second look's and a project plan's Try again leave the drafts' preference as it is.
    await rig.service.preference("try_again", "long_task_look")
    await rig.service.preference("try_again", "stalled_project")
    assert rig.store.get_json(SCOPE, "message_drafts") == stored

    # A message draft has no kinds to raise again.
    with pytest.raises(DecisionLoopError) as refused:
        await rig.service.preference("raise_again", "context_handoff")
    assert refused.value.code == "invalid_request"
    assert rig.store.get_json(SCOPE, "message_drafts") == stored


def check_trigger(block: Mapping[str, Any]) -> dict[str, Any]:
    (row,) = [row for row in block["saved_checks"] if row["candidate"] == "commit_checks"]
    return row


async def test_learning_shows_saved_checks_and_try_again_restarts_them(rig: Rig) -> None:
    # Thirteen offers: 1 used, 9 turned down, 2 closed unanswered, 1 still open. The ten answers
    # pause the type; unanswered ones count neither way. An unshown one, another workspace's and
    # a message draft stay out.
    draft_item(rig, "Used", "used", candidate="commit_checks")
    for n in range(9):
        draft_item(rig, f"Wrong {n}", "wrong", candidate="commit_checks")
    for n in range(2):
        draft_item(rig, f"Moved on {n}", "nothing", candidate="commit_checks")
    draft_item(rig, "Waiting", candidate="commit_checks")
    draft_item(rig, "Elsewhere", "used", candidate="commit_checks", scope=OTHER_SCOPE)
    draft_item(rig, "Handoff", "used")
    put(rig, "Unshown", candidate="commit_checks")  # no shown_at, so no row
    block = (await rig.service.page())["learning"]
    assert check_trigger(block) == {
        "candidate": "commit_checks",
        "made": 13,
        "accepted": 1,
        "rejected": 9,
        "nothing": 0,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 0, "bad_timing": 9},
        "paused": True,
    }
    assert draft_trigger(block, "context_handoff")["made"] == 1  # a draft's rows stay its own

    # Try again restarts the checks alone; the other preferences stay unwritten.
    rig.clock.advance(60)
    page = await rig.service.preference("try_again", "commit_checks")
    assert (check_trigger(page["learning"])["made"], check_trigger(page["learning"])["paused"]) == (
        0,
        False,
    )
    assert draft_trigger(page["learning"], "context_handoff")["made"] == 1
    assert rig.engine.wakes == ["change"]
    for name in ("second_looks", "project_plans", "message_drafts"):
        assert rig.store.get_json(SCOPE, name) is None
    assert rig.store.get_json(OTHER_SCOPE, "saved_checks") is None
    stored = cast(dict[str, Any], rig.store.get_json(SCOPE, "saved_checks"))
    assert set(stored["looks"]) == {"commit_checks"} and stored["kinds"] == {}

    # A message draft's Try again leaves the checks' preference as it is, and a saved check has no
    # kinds to raise again.
    await rig.service.preference("try_again", "context_handoff")
    assert rig.store.get_json(SCOPE, "saved_checks") == stored
    with pytest.raises(DecisionLoopError) as refused:
        await rig.service.preference("raise_again", "commit_checks")
    assert refused.value.code == "invalid_request"
    assert rig.store.get_json(SCOPE, "saved_checks") == stored


def triage_trigger(block: Mapping[str, Any]) -> dict[str, Any]:
    (row,) = [row for row in block["inbox_triage"] if row["candidate"] == "inbox_triage"]
    return row


async def test_learning_shows_inbox_suggestions_and_try_again_restarts_them(rig: Rig) -> None:
    # Thirteen offers: 1 used, 3 turned down (one after its run found nothing), 6 found nothing
    # (one of them also used), 2 closed unanswered and 1 still open. The ten answers pause the
    # type. An unshown one, another workspace's, a saved check and a message draft stay out.
    draft_item(rig, "Used", "used", candidate="inbox_triage")
    for n in range(2):
        draft_item(rig, f"Wrong {n}", "wrong", candidate="inbox_triage")
    for n in range(5):
        draft_item(rig, f"Empty {n}", "empty", candidate="inbox_triage")
    # Turning down wins over a run that found nothing, which wins over use.
    turned = draft_item(rig, "Wrong after nothing", "wrong", candidate="inbox_triage")
    rig.agenda.set_state(SCOPE, turned.item_key, "done", WALL)
    used = draft_item(rig, "Used after nothing", "used", candidate="inbox_triage")
    rig.agenda.set_state(SCOPE, used.item_key, "done", WALL)
    for n in range(2):
        draft_item(rig, f"Moved on {n}", "nothing", candidate="inbox_triage")
    draft_item(rig, "Waiting", candidate="inbox_triage")
    draft_item(rig, "Elsewhere", "used", candidate="inbox_triage", scope=OTHER_SCOPE)
    draft_item(rig, "Check", "used", candidate="commit_checks")
    draft_item(rig, "Handoff", "used")
    put(rig, "Unshown", candidate="inbox_triage")  # no shown_at, so no row
    block = (await rig.service.page())["learning"]
    assert triage_trigger(block) == {
        "candidate": "inbox_triage",
        "made": 13,
        "accepted": 1,
        "rejected": 3,
        "nothing": 6,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 0, "bad_timing": 3},
        "paused": True,
    }
    assert draft_trigger(block, "context_handoff")["made"] == 1  # a draft's rows stay its own
    assert check_trigger(block)["made"] == 1

    # Try again restarts the suggestions alone; the other preferences stay unwritten.
    rig.clock.advance(60)
    page = await rig.service.preference("try_again", "inbox_triage")
    assert (
        triage_trigger(page["learning"])["made"],
        triage_trigger(page["learning"])["paused"],
    ) == (
        0,
        False,
    )
    assert check_trigger(page["learning"])["made"] == 1
    assert rig.engine.wakes == ["change"]
    for name in ("second_looks", "project_plans", "message_drafts", "saved_checks"):
        assert rig.store.get_json(SCOPE, name) is None
    assert rig.store.get_json(OTHER_SCOPE, "inbox_triage") is None
    stored = cast(dict[str, Any], rig.store.get_json(SCOPE, "inbox_triage"))
    assert set(stored["looks"]) == {"inbox_triage"} and stored["kinds"] == {}

    # A saved check's Try again leaves the suggestions' preference as it is, and they have no
    # kinds to raise again.
    await rig.service.preference("try_again", "commit_checks")
    assert rig.store.get_json(SCOPE, "inbox_triage") == stored
    with pytest.raises(DecisionLoopError) as refused:
        await rig.service.preference("raise_again", "inbox_triage")
    assert refused.value.code == "invalid_request"
    assert rig.store.get_json(SCOPE, "inbox_triage") == stored


async def test_reset_and_delete_all_restart_message_draft_counts(rig: Rig) -> None:
    draft_item(rig, "Wrong", "wrong")
    before = (await rig.service.page())["learning"]
    assert draft_trigger(before, "context_handoff")["made"] == 1

    rig.clock.advance(60)
    await rig.service.preference("try_again", "source_note")
    after = (await rig.service.preference("reset_learning", None))["learning"]
    # Reset: drafts and reasons from before it no longer count; the restart stamps stay.
    assert draft_trigger(after, "context_handoff")["made"] == 0
    assert draft_trigger(after, "context_handoff")["reasons"]["bad_timing"] == 0
    assert rig.store.get_json(SCOPE, "message_drafts") is not None

    later = draft_item(rig, "Afterwards", "nothing")
    with rig.state._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_items SET created_at=? WHERE item_key=?",
            (stamp(WALL + timedelta(60)), later.item_key),
        )
    assert draft_trigger((await rig.service.page())["learning"], "context_handoff")["made"] == 1
    await rig.service.delete_all()
    assert draft_trigger((await rig.service.page())["learning"], "context_handoff")["made"] == 0


async def test_the_owners_take_a_second_look_leaves_out_paused_kinds(rig: Rig) -> None:
    for n in range(5):
        look_row(rig, f"Flawed {n}", "rejected:flaw")
    look_row(rig, "Approach", "rejected:approach")
    listed = second_look_item(rig, "Garden plan", "correction_look")
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    assert rig.host.actors[mission["session_id"]].sent[0].count(" Leave out flaws.") == 1

    # Learning off pauses no kind, so the brief names none.
    rig.values["decision.learning"] = False
    off = second_look_item(rig, "Beds", "long_task_look")
    sent = (await rig.service.start_mission(off.item_key, "prepare"))["session_id"]
    assert "Leave out" not in rig.host.actors[sent].sent[0]


async def test_the_owner_starts_and_stops_missions(rig: Rig) -> None:
    listed, _held = await decided(rig)
    rig.values["decision.grant.prepare"] = "off"
    with pytest.raises(MissionError) as off:
        await rig.service.start_mission(listed.item_key, "prepare")
    assert off.value.code == "off" and rig.host.calls == []

    # "ask" lets the user's own start through: a plan-mode chat whose brief has no deadline.
    rig.values["decision.grant.prepare"] = "ask"
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    session = mission["session_id"]
    assert (mission["work"], mission["state"], mission["title"]) == (
        "prepare",
        "running",
        SEEDLINGS,
    )
    assert rig.host.calls == [
        ("remember", session, {"new_session": True, "mode": "plan"}),
        ("create", session),
    ]
    assert "\nDeadline: none\n" in rig.host.actors[session].sent[0]
    assert await rig.service.missions() == {
        "running": [mission],
        "today": {"prepare": 1, "run": 0},
        "limits": {"prepare": 5, "run": 3},
        "hold": None,
        "team": {"today": 0, "limit": 4, "running": 0},
        "look": {"today": 0, "limit": 3},
        "second_look": {"today": 0, "limit": 3},
        "project_plan": {"today": 0, "limit": 3},
        "inbox": {"today": 0, "limit": 3},
        "checks": None,
    }
    launcher = await rig.service.launcher()
    assert (launcher["working"], launcher["started"]) == (1, [session])

    stopped = await rig.service.stop_mission(mission["mission_id"])
    assert (stopped["mission_id"], stopped["state"], stopped["reason"]) == (
        mission["mission_id"],
        "stopped",
        "owner",
    )
    assert rig.host.actors[session].interrupts == 1
    again = await rig.service.start_mission(listed.item_key, "run")
    assert (again["work"], again["state"]) == ("run", "running")
    assert await rig.service.stop_missions() == {
        "running": [],
        "today": {"prepare": 1, "run": 1},
        "limits": {"prepare": 5, "run": 3},
        "hold": None,
        "team": {"today": 0, "limit": 4, "running": 0},
        "look": {"today": 0, "limit": 3},
        "second_look": {"today": 0, "limit": 3},
        "project_plan": {"today": 0, "limit": 3},
        "inbox": {"today": 0, "limit": 3},
        "checks": None,
    }
    assert rig.host.actors[again["session_id"]].interrupts == 1
    assert rig.engine.wakes == ["mission"] * 4

    # Only an open item of the scope takes a start.
    with pytest.raises(DecisionLoopError) as unknown:
        await rig.service.start_mission(item_key("tasks_due", TASKS, "Not a task"), "prepare")
    assert unknown.value.code == "unknown_item"
    await rig.service.feedback(shown(listed.item_key, "useful"))
    with pytest.raises(DecisionLoopError) as closed:
        await rig.service.start_mission(listed.item_key, "prepare")
    assert closed.value.code == "not_applicable"


async def test_the_launcher_counts_vikis_chats_whose_plan_waits(rig: Rig) -> None:
    listed, _held = await decided(rig)
    assert (await rig.service.launcher())["plans_waiting"] == 0
    mission = await rig.service.start_mission(listed.item_key, "prepare")
    actor = rig.host.actors[mission["session_id"]]
    plan = PermissionRequested(
        tool_use_id="plan-1", kind="plan", tool="ExitPlanMode", summary="A plan", plan="1. Sow."
    )
    actor.emit(plan)
    assert (await rig.service.launcher())["plans_waiting"] == 1
    actor.emit(RequestAnswered(tool_use_id="plan-1"))
    assert (await rig.service.launcher())["plans_waiting"] == 0


# ---- seats -------------------------------------------------------------------------------------

ROUTINE = "00000000-0000-4000-8000-0000000000f1"
RIVAL = "00000000-0000-4000-8000-0000000000f2"
SINCE = WALL.astimezone() - timedelta(days=30)  # the launcher's `started` window


def seat(
    routine_id: str = ROUTINE,
    *,
    state: str = "enabled",
    tested: int | None = 3,
    name: str | None = "research",
) -> SeatRoutine:
    """A routine at revision 3 that claims a seat."""
    return SeatRoutine(routine_id, state, 3, tested, name, False)


def seat_run(routine_id: str = ROUTINE) -> SeatRun:
    return SeatRun(
        "00000000-0000-4000-8000-0000000000a9",
        routine_id,
        "decision",
        CHAT,
        WALL,
        True,
        WALL,
        "succeeded",
        "",
    )


def seat_host(
    *routines: SeatRoutine,
    newest: Mapping[str, SeatRun] | None = None,
    complete: bool = True,
    team: SeatTeam | None = None,
    refuse: str | None = None,
) -> FakeSeatHost:
    facts = SeatFacts(routines, (), newest or {}, 2, 0, False, complete)
    return FakeSeatHost(facts, team, refuse=refuse)


@contextlib.asynccontextmanager
async def seated(tmp_path: Path, host: FakeSeatHost, **kwargs: Any) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path, seats=host, **kwargs)
    try:
        yield rig
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


def started(host: FakeSeatHost) -> list[tuple[Any, ...]]:
    return [call for call in host.calls if call[0] == "start"]


async def test_the_owner_starts_a_seat_past_the_brake_and_quiet_hours_but_not_the_limit(
    tmp_path: Path,
) -> None:
    run = seat_run()
    host = seat_host(seat(), newest={ROUTINE: run}, team=SeatTeam(1, 1, ()))
    async with seated(tmp_path, host) as rig:
        rig.reading = holding(0.99)
        assert (await rig.service.missions())["hold"] is not None  # the brake holds
        now = rig.clock.wall().astimezone()
        rig.values["notifications.quiet_start"] = (now - timedelta(hours=1)).strftime("%H:%M")
        rig.values["notifications.quiet_end"] = (now + timedelta(hours=1)).strftime("%H:%M")
        assert rig.settings().quiet is not None  # and so do the quiet hours

        block = await rig.service.start_seat(ROUTINE)
        assert (block["team"], block["hold"] is not None) == (
            {"today": 1, "limit": 4, "running": 1},
            True,
        )
        assert [call for call in host.calls if call[0] in ("facts", "start")] == [
            ("facts", SCOPE, None, DAY),
            ("start", ROUTINE, 3, run.attempt_id, None, 4, DAY),
        ]
        assert rig.engine.wakes == ["mission"]

        # The grant "allowed" lets it through too; the limit is the port's claim, given the
        # setting, and a refusal there wakes nothing.
        rig.values["decision.grant.team"] = "allowed"
        rig.values["decision.limit.team"] = 2
        host.refuse = "limit"
        with pytest.raises(DecisionLoopError) as full:
            await rig.service.start_seat(ROUTINE)
        assert full.value.code == "limit"
        assert started(host)[-1] == ("start", ROUTINE, 3, run.attempt_id, None, 2, DAY)
        assert rig.engine.wakes == ["mission"]


SEAT_REFUSALS = {
    "decision off": ({}, {"decision.enabled": False}, "ordinary", (seat(),), True),
    "decision paused": (
        {},
        {"decision.paused_until": stamp(WALL + timedelta(hours=1))},
        "ordinary",
        (seat(),),
        True,
    ),
    "grant off": ({}, {"decision.grant.team": "off"}, "ordinary", (seat(),), True),
    "read-only vault": ({}, {}, "existing_readonly", (seat(),), True),
    "protected vault": ({}, {}, "protected", (seat(),), True),
    "unreadable policy": ({}, {}, None, (seat(),), True),
    "chat only": ({"chat_only": True}, {}, "ordinary", (seat(),), True),
    "a code folder": ({"kind": "code"}, {}, "ordinary", (seat(),), True),
    "no such routine": ({}, {}, "ordinary", (seat(RIVAL),), True),
    "routine paused": ({}, {}, "ordinary", (seat(state="paused"),), True),
    "routine changed since its test": ({}, {}, "ordinary", (seat(tested=2),), True),
    "routine never tested": ({}, {}, "ordinary", (seat(tested=None),), True),
    "routine without a seat": ({}, {}, "ordinary", (seat(name=None),), True),
    "two routines for the seat": ({}, {}, "ordinary", (seat(), seat(RIVAL)), True),
    "facts incomplete": ({}, {}, "ordinary", (seat(),), False),
}


@pytest.mark.parametrize("case", SEAT_REFUSALS)
async def test_the_start_route_refuses_outside_an_ordinary_vault_and_under_off(
    tmp_path: Path, case: str
) -> None:
    rig_kwargs, values, policy, routines, complete = SEAT_REFUSALS[case]
    host = seat_host(*routines, complete=complete)
    async with seated(tmp_path, host, **rig_kwargs) as rig:
        rig.policy = policy
        rig.values.update(values)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.start_seat(ROUTINE)
        assert refused.value.code == "not_applicable"
        assert started(host) == [] and rig.engine.wakes == []


async def test_the_owner_cannot_start_a_seat_in_a_chat_only_workspace(tmp_path: Path) -> None:
    host = seat_host(seat())
    async with seated(tmp_path, host, chat_only=True) as rig:
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.start_seat(ROUTINE)
        assert refused.value.code == "not_applicable"
        assert started(host) == [] and rig.engine.wakes == []


async def test_the_seat_routes_need_a_port(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        for route in (rig.service.start_seat, rig.service.stop_seat):
            with pytest.raises(DecisionLoopError) as refused:
                await route(ROUTINE)
            assert refused.value.code == "unavailable"
        assert (await rig.service.stop_missions())["team"] == {"today": 0, "limit": 4, "running": 0}
        assert rig.engine.wakes == ["mission"]  # Stop all alone
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


async def test_the_launcher_lists_seat_runs_as_started_by_viki(tmp_path: Path) -> None:
    first, second = "00000000-0000-4000-8000-0000000000c2", "00000000-0000-4000-8000-0000000000c3"
    host = seat_host(team=SeatTeam(2, 1, (first, second)))
    async with seated(tmp_path, host) as rig:
        listed, _held = await decided(rig)
        mission = await rig.service.start_mission(listed.item_key, "run")
        view = await rig.service.launcher()
        assert view["started"] == [mission["session_id"], first, second]
        assert ("team", DAY, SINCE) in host.calls
        page = (await rig.service.page())["missions"]
        assert page["team"] == {"today": 2, "limit": 4, "running": 1}
        assert (await rig.service.missions())["team"] == page["team"]

        # Past the cap the launcher cuts the list: the missions' chats come first.
        host.seat_team = SeatTeam(60, 0, tuple(str(uuid.uuid4()) for _ in range(60)))
        assert len((await rig.service.launcher())["started"]) == 50


async def test_stop_seat_and_stop_all_interrupt_seat_runs(tmp_path: Path) -> None:
    host = seat_host(seat(), team=SeatTeam(1, 0, ()))
    async with seated(tmp_path, host) as rig:
        block = await rig.service.stop_seat(ROUTINE)
        assert block["team"] == {"today": 1, "limit": 4, "running": 0}
        assert ("stop", ROUTINE) in host.calls and rig.engine.wakes == ["mission"]
        await rig.service.start_seat(ROUTINE)
        await rig.service.stop_missions()
        assert ("stop_all",) in host.calls
        assert rig.engine.wakes == ["mission"] * 3


# ---- saved checks ------------------------------------------------------------------------------

FOLDER = "00000000-0000-4000-8000-0000000000e5"
HEAD = "a" * 40
MOVED = "b" * 40  # the commit an item was raised for before the folder moved on


class ThreadedChecks(FakeCheckHost):
    """`FakeCheckHost` noting the threads its blocking methods run on."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.threads: set[int] = set()

    def supported(self) -> bool:
        self.threads.add(threading.get_ident())
        return super().supported()

    def folder(self, workspace_id: str, now: datetime) -> CheckFolder | None:
        self.threads.add(threading.get_ident())
        return super().folder(workspace_id, now)

    def running(self) -> int:
        self.threads.add(threading.get_ident())
        return super().running()


def check_folder(phase: str = "waiting", pending: tuple[str, ...] = ("c1",)) -> CheckFolder:
    return CheckFolder(FOLDER, "garden-app", HEAD, phase, pending)


def check_host(folder: CheckFolder | None = None, **kwargs: Any) -> ThreadedChecks:
    return ThreadedChecks(folder=check_folder() if folder is None else folder, **kwargs)


def check_item(rig: Rig, head: str = HEAD, *, scope: str = "") -> ItemRecord:
    """The open item a cycle raises for the folder's commit `head`."""
    key = item_key("commit_checks", FOLDER, head)
    return put(rig, "Checks", candidate="commit_checks", key=key, scope=scope)


@contextlib.asynccontextmanager
async def checked(tmp_path: Path, host: FakeCheckHost, **kwargs: Any) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path, checks=host, **kwargs)
    try:
        yield rig
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


def check_starts(host: FakeCheckHost) -> list[tuple[Any, ...]]:
    return [call for call in host.calls if call[0] == "start"]


async def test_the_owner_runs_a_folders_checks_only_for_its_open_item(tmp_path: Path) -> None:
    host = check_host()
    async with checked(tmp_path, host) as rig:
        check_item(rig)
        block = await rig.service.run_checks(FOLDER)
        assert block["checks"] == {"today": 1, "limit": 3, "running": 0}
        assert check_starts(host) == [("start", FOLDER, HEAD, ("c1",))]
        assert ("folder", FOLDER, rig.clock.wall().astimezone()) in host.calls
        assert rig.engine.wakes == ["mission"]
        # The blocking reads ran in a worker thread; the start is awaited on the loop.
        assert threading.get_ident() not in host.threads

        # The grant "allowed" lets the user's Run checks through too.
        rig.values["decision.grant.check"] = "allowed"
        await rig.service.run_checks(FOLDER)
        assert len(check_starts(host)) == 2 and rig.engine.wakes == ["mission"] * 2


async def test_a_run_that_starts_no_check_is_busy_and_a_refusal_wakes_nothing(
    tmp_path: Path,
) -> None:
    host = check_host(started=0)
    async with checked(tmp_path, host) as rig:
        check_item(rig)
        with pytest.raises(DecisionLoopError) as busy:
            await rig.service.run_checks(FOLDER)
        assert busy.value.code == "busy" and len(check_starts(host)) == 1  # nothing retried
        host.refuse = "not_applicable"  # the port saw the commit move
        with pytest.raises(DecisionLoopError) as moved:
            await rig.service.run_checks(FOLDER)
        assert moved.value.code == "not_applicable" and len(check_starts(host)) == 2
        assert rig.engine.wakes == []


async def test_the_owner_runs_checks_past_quiet_hours_and_the_brake(tmp_path: Path) -> None:
    host = check_host()
    async with checked(tmp_path, host) as rig:
        check_item(rig)
        rig.reading = holding(0.99)
        assert (await rig.service.missions())["hold"] is not None  # the brake holds
        now = rig.clock.wall().astimezone()
        rig.values["notifications.quiet_start"] = (now - timedelta(hours=1)).strftime("%H:%M")
        rig.values["notifications.quiet_end"] = (now + timedelta(hours=1)).strftime("%H:%M")
        assert rig.settings().quiet is not None  # and so do the quiet hours

        block = await rig.service.run_checks(FOLDER)
        assert block["hold"] is not None
        assert check_starts(host) == [("start", FOLDER, HEAD, ("c1",))]
        assert rig.engine.wakes == ["mission"]


# What each case changes: the rig's arguments, the settings, the vault's policy, the host's
# arguments (its folder replaced when named) and the item the commit has ("moved": one raised for
# an earlier commit; "closed"; "none").
CHECK_REFUSALS: dict[str, dict[str, Any]] = {
    "host not supported": {"host": {"supported": False}},
    "decision off": {"values": {"decision.enabled": False}},
    "decision paused": {"values": {"decision.paused_until": stamp(WALL + timedelta(hours=1))}},
    "grant off": {"values": {"decision.grant.check": "off"}},
    "read-only vault": {"policy": "existing_readonly"},
    "protected vault": {"policy": "protected"},
    "unreadable policy": {"policy": None},
    "chat only": {"rig": {"chat_only": True}},
    "a code folder": {"rig": {"kind": "code"}},
    "folder not readable": {"folder": None},
    "phase running": {"folder": check_folder("running")},
    "phase passed": {"folder": check_folder("passed", ())},
    "phase failed": {"folder": check_folder("failed", ())},
    "phase stopped": {"folder": check_folder("stopped", ())},
    "waiting with nothing pending": {"folder": check_folder("waiting", ())},
    "no item at all": {"item": "none"},
    "item of an earlier commit": {"item": "moved"},
    "closed item": {"item": "closed"},
}


@pytest.mark.parametrize("case", CHECK_REFUSALS)
async def test_run_checks_refuses_off_paused_or_outside_an_ordinary_vault(
    tmp_path: Path, case: str
) -> None:
    changes = CHECK_REFUSALS[case]
    folder = changes["folder"] if "folder" in changes else check_folder()
    host = ThreadedChecks(folder=folder, **changes.get("host", {}))
    async with checked(tmp_path, host, **changes.get("rig", {})) as rig:
        rig.policy = changes.get("policy", "ordinary")
        rig.values.update(changes.get("values", {}))
        if changes.get("item") == "moved":
            check_item(rig, MOVED)
        elif changes.get("item") != "none":
            item = check_item(rig)
            if changes.get("item") == "closed":
                rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.run_checks(FOLDER)
        assert refused.value.code == "not_applicable"
        assert check_starts(host) == [] and rig.engine.wakes == []


async def test_the_check_routes_need_a_port(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        for route in (rig.service.run_checks, rig.service.stop_checks):
            with pytest.raises(DecisionLoopError) as refused:
                await route(FOLDER)
            assert refused.value.code == "unavailable"
        assert (await rig.service.stop_missions())["checks"] is None
        assert rig.engine.wakes == ["mission"]  # Stop all alone
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


async def test_stop_checks_and_stop_all_also_stop_check_runs(tmp_path: Path) -> None:
    host = check_host(live=2)
    async with checked(tmp_path, host) as rig:
        block = await rig.service.stop_checks(FOLDER)
        assert block["checks"] == {"today": 0, "limit": 3, "running": 2}
        assert ("stop", FOLDER) in host.calls and rig.engine.wakes == ["mission"]
        await rig.service.stop_missions()
        assert ("stop_all",) in host.calls
        assert rig.engine.wakes == ["mission"] * 2


async def test_stop_all_also_stops_check_runs(tmp_path: Path) -> None:
    host = check_host(live=1)
    seats = seat_host(team=SeatTeam(0, 0, ()))
    async with checked(tmp_path, host, seats=seats) as rig:
        await rig.service.stop_missions()
        assert [call for call in host.calls if call[0].startswith("stop")] == [("stop_all",)]
        assert ("stop_all",) in seats.calls and rig.engine.wakes == ["mission"]


async def test_the_missions_block_shows_checks_only_where_they_run(tmp_path: Path) -> None:
    # No port, or one that is not supported: null, and a port off Windows is not asked to count.
    rig = Rig(tmp_path)
    try:
        assert (await rig.service.missions())["checks"] is None
        assert (await rig.service.page())["missions"]["checks"] is None
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    off = check_host(supported=False, live=3)
    (tmp_path / "off").mkdir()
    async with checked(tmp_path / "off", off) as rig:
        assert (await rig.service.missions())["checks"] is None
        assert (await rig.service.page())["missions"]["checks"] is None
        assert ("running",) not in off.calls

    # Supported: the items raised today in any workspace and the runs VIKI started that go on.
    host = check_host(live=2)
    (tmp_path / "on").mkdir()
    async with checked(tmp_path / "on", host) as rig:
        check_item(rig)
        check_item(rig, MOVED, scope=OTHER_SCOPE)
        old = check_item(rig, "c" * 40)
        put(rig, "A draft", candidate="context_handoff")
        with rig.state._connection(write=True) as connection:
            connection.execute(
                "UPDATE decision_items SET created_at=? WHERE item_key=?",
                (stamp(YESTERDAY_AT), old.item_key),
            )
        rig.values["decision.limit.check"] = 5
        expected = {"today": 2, "limit": 5, "running": 2}
        assert (await rig.service.missions())["checks"] == expected
        assert (await rig.service.page())["missions"]["checks"] == expected
        assert threading.get_ident() not in host.threads


# ---- inbox suggestions -------------------------------------------------------------------------

TRIAGE = item_key("inbox_triage", SCOPE, "the inbox as listed")


def triage_item(
    rig: Rig, *, kind: str = "suggest", candidate: str = "inbox_triage", key: str = TRIAGE
) -> ItemRecord:
    """The open item a cycle raises for an inbox state, of the kind its phase gives."""
    return put(rig, "Inbox", candidate=candidate, kind=kind, key=key)


@contextlib.asynccontextmanager
async def suggesting(tmp_path: Path, host: FakeInboxHost, **kwargs: Any) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path, inbox=host, **kwargs)
    try:
        yield rig
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


def triage_starts(host: FakeInboxHost) -> list[tuple[Any, ...]]:
    return [call for call in host.calls if call[0] == "start"]


async def test_the_owner_suggests_only_for_an_open_waiting_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeInboxHost()
    async with suggesting(tmp_path, host) as rig:
        triage_item(rig)
        threads: set[int] = set()
        looked = rig.agenda.item

        def noting(scope: str, key: str) -> ItemRecord | None:
            threads.add(threading.get_ident())
            return looked(scope, key)

        monkeypatch.setattr(rig.agenda, "item", noting)
        block = await rig.service.suggest_inbox(TRIAGE)
        assert block["inbox"] == {"today": 1, "limit": 3}
        assert triage_starts(host) == [("start", SCOPE, TRIAGE)]
        assert rig.engine.wakes == ["mission"]
        assert rig.rows("decision_missions") == []  # the suggester's run, never a mission
        assert threads and threading.get_ident() not in threads  # read in a worker thread

        # Allowed lets the user's click through too, and so does a protected vault: its plans
        # (prepare) are allowed.
        rig.values["decision.grant.inbox_triage"] = "allowed"
        rig.policy = "protected"
        await rig.service.suggest_inbox(TRIAGE)
        assert len(triage_starts(host)) == 2 and rig.engine.wakes == ["mission"] * 2


async def test_the_owner_suggests_past_quiet_hours_and_the_brake(tmp_path: Path) -> None:
    host = FakeInboxHost()
    async with suggesting(tmp_path, host) as rig:
        triage_item(rig)
        rig.reading = holding(0.99)
        assert (await rig.service.missions())["hold"] is not None  # the brake holds
        now = rig.clock.wall().astimezone()
        rig.values["notifications.quiet_start"] = (now - timedelta(hours=1)).strftime("%H:%M")
        rig.values["notifications.quiet_end"] = (now + timedelta(hours=1)).strftime("%H:%M")
        assert rig.settings().quiet is not None  # and so do the quiet hours

        block = await rig.service.suggest_inbox(TRIAGE)
        assert block["hold"] is not None
        assert triage_starts(host) == [("start", SCOPE, TRIAGE)]
        assert rig.engine.wakes == ["mission"]


# What each case changes: the rig's arguments, the settings, the vault's policy and the item the
# key names ("none"; "closed"; another type's; a kind that is not "suggest").
TRIAGE_REFUSALS: dict[str, dict[str, Any]] = {
    "decision off": {"values": {"decision.enabled": False}},
    "decision paused": {"values": {"decision.paused_until": stamp(WALL + timedelta(hours=1))}},
    "grant off": {"values": {"decision.grant.inbox_triage": "off"}},
    "read-only vault": {"policy": "existing_readonly"},
    "unreadable policy": {"policy": None},
    "unknown policy": {"policy": "something_new"},
    "chat only": {"rig": {"chat_only": True}},
    "a code folder": {"rig": {"kind": "code"}},
    "no item at all": {"item": "none"},
    "closed item": {"item": "closed"},
    "another type": {"item": "other"},
    "an item that only informs": {"item": "inform"},
    "an item that is ready": {"item": "ready"},
    "an item that reports": {"item": "report"},
}


@pytest.mark.parametrize("case", TRIAGE_REFUSALS)
async def test_suggest_refuses_off_paused_or_outside_a_prepare_vault(
    tmp_path: Path, case: str
) -> None:
    changes = TRIAGE_REFUSALS[case]
    host = FakeInboxHost()
    async with suggesting(tmp_path, host, **changes.get("rig", {})) as rig:
        rig.policy = changes.get("policy", "ordinary")
        rig.values.update(changes.get("values", {}))
        item = changes.get("item")
        if item == "other":
            triage_item(rig, candidate="commit_checks")
        elif item in ("inform", "ready", "report"):
            triage_item(rig, kind=item)
        elif item != "none":
            listed = triage_item(rig)
            if item == "closed":
                rig.agenda.set_state(SCOPE, listed.item_key, "dismissed", WALL)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.suggest_inbox(TRIAGE)
        assert refused.value.code == "not_applicable"
        assert host.calls == [] and rig.engine.wakes == []


async def test_suggest_refuses_another_workspaces_item(tmp_path: Path) -> None:
    host = FakeInboxHost()
    async with suggesting(tmp_path, host) as rig:
        put(rig, "Inbox", candidate="inbox_triage", kind="suggest", key=TRIAGE, scope=OTHER_SCOPE)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.suggest_inbox(TRIAGE)
        assert refused.value.code == "not_applicable" and host.calls == []


async def test_the_hosts_refusals_pass_through_and_wake_nothing(tmp_path: Path) -> None:
    host = FakeInboxHost()
    async with suggesting(tmp_path, host) as rig:
        triage_item(rig)
        for code in ("busy", "not_applicable", "unavailable"):
            host.refuse = code
            with pytest.raises(DecisionLoopError) as refused:
                await rig.service.suggest_inbox(TRIAGE)
            assert refused.value.code == code
        assert len(triage_starts(host)) == 3 and rig.engine.wakes == []  # nothing retried


async def test_the_inbox_route_needs_a_port_and_stop_all_does_not_reach_the_run(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    try:
        triage_item(rig)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.suggest_inbox(TRIAGE)
        assert refused.value.code == "unavailable"
        assert rig.engine.wakes == []
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    host = FakeInboxHost()
    (tmp_path / "ported").mkdir()
    async with suggesting(tmp_path / "ported", host) as rig:
        await rig.service.stop_missions()
        assert host.calls == []  # Decision starts the run and has no way to stop it


async def test_the_missions_block_counts_todays_inbox_suggestions(rig: Rig) -> None:
    # Items raised today in any workspace count, whatever their state or kind; yesterday's and
    # the other types do not. The count needs no port.
    triage_item(rig)
    triage_item(rig, kind="ready", key=item_key("inbox_triage", SCOPE, "ready"))
    put(rig, "Elsewhere", candidate="inbox_triage", scope=OTHER_SCOPE, key=TRIAGE)
    old = triage_item(rig, key=item_key("inbox_triage", SCOPE, "old"))
    put(rig, "A check", candidate="commit_checks")
    put(rig, "A draft", candidate="context_handoff")
    with rig.state._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_items SET created_at=? WHERE item_key=?",
            (stamp(YESTERDAY_AT), old.item_key),
        )
    rig.values["decision.limit.inbox_triage"] = 5
    expected = {"today": 3, "limit": 5}
    assert (await rig.service.missions())["inbox"] == expected
    assert (await rig.service.page())["missions"]["inbox"] == expected


def data_query(kind: str, *, delete: bool = False, **changes: object) -> wire.DataQuery:
    body: dict[str, object] = {
        name: None
        for name in (
            "kind",
            "ids",
            "topic",
            "shown",
            "answer",
            "state",
            "outcome",
            "since",
            "until",
            "before",
        )
    }
    body.update(kind=kind, **changes)
    return wire.parse_data_query(body, delete=delete)


async def test_data_summary_is_scoped_and_reports_key_changes(rig: Rig) -> None:
    recorded(rig, "Here", feedback="missed")
    put(rig, "Elsewhere", scope=OTHER_SCOPE)
    summary = await rig.service.data()
    assert summary["as_of"] == stamp(rig.clock.wall())
    assert summary["today"] == DAY
    assert summary["decisions"]["decisions"] == 1
    assert summary["suggestions"]["open"] == 0
    assert summary["calls"]["count"] == 0
    assert summary["chats"]["count"] == 0
    assert summary["proposals"]["wordings"] == 0
    assert summary["preferences"]["learned"] is False
    assert summary["key"] == {"saved": False, "updated": None}
    assert summary["days"] == [
        {
            "day": DAY,
            "held": 1,
            "listed": 0,
            "attention": 0,
            "reports": 0,
            "useful": 0,
            "wrong": 0,
            "later": 0,
            "mute": 0,
            "right_to_hold": 0,
            "missed": 1,
        }
    ]
    assert summary["topics"] == [
        {"topic": "tasks_due", "held": 1, "shown": 0, "reports": 0, "useful": 0, "wrong": 0}
    ]
    await rig.service.save_key(KEY)
    saved = (await rig.service.data())["key"]
    assert saved == {"saved": True, "updated": stamp(WALL)}
    await rig.service.save_key(KEY)
    assert (await rig.service.data())["key"]["updated"] > saved["updated"]


async def test_data_records_and_details_use_the_active_scope(rig: Rig) -> None:
    listed = put(rig, "Listed")
    put(rig, "Other", scope=OTHER_SCOPE)
    held_id = recorded(rig, "Held", feedback="missed")
    calls = [
        rig.agenda.record_call(SCOPE, kept_call(listed.item_key, held_back=0, tokens=12), WALL),
        rig.agenda.record_call(
            SCOPE,
            replace(
                kept_call(listed.item_key, held_back=0, tokens=8),
                outcome="failed",
                failure="unreachable",
            ),
            WALL,
        ),
    ]
    rig.agenda.record_call(OTHER_SCOPE, kept_call(listed.item_key, held_back=0, tokens=3), WALL)
    decisions = await rig.service.records(data_query("decisions", answer="none"))
    assert decisions["kind"] == "decisions" and decisions["total"] == 1
    assert [row["id"] for row in decisions["rows"]] == [listed.episode_id]
    suggestions = await rig.service.records(data_query("suggestions", state="open"))
    assert suggestions["kind"] == "suggestions" and suggestions["total"] == 1
    assert [row["id"] for row in suggestions["rows"]] == [listed.item_key]
    failed = await rig.service.records(data_query("calls", outcome="failed"))
    assert failed["kind"] == "calls" and failed["total"] == 1
    assert [row["id"] for row in failed["rows"]] == [calls[1]]
    assert {row["id"] for row in (await rig.service.records(data_query("calls")))["rows"]} == set(
        calls
    )
    decision = await rig.service.record("decisions", held_id)
    assert (decision["kind"], decision["id"], decision["answer"]) == (
        "decisions",
        held_id,
        "missed",
    )
    suggestion = await rig.service.record("suggestions", listed.item_key)
    assert (suggestion["kind"], suggestion["title"], suggestion["why"]) == (
        "suggestions",
        "Listed",
        "due today",
    )
    for kind, record_id in (("decisions", MISSING), ("suggestions", "f" * 64)):
        with pytest.raises(DecisionLoopError) as unknown:
            await rig.service.record(kind, record_id)
        assert unknown.value.code == "unknown_item"


async def test_data_delete_couples_only_decisions_and_wakes_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_item = put(rig, "Open")
    closed_item = put(rig, "Closed")
    rig.agenda.set_state(SCOPE, closed_item.item_key, "acted", WALL)
    rig.store.set_json(SCOPE, "learned", LEARNED, WALL)
    call_id = rig.agenda.record_call(
        SCOPE, kept_call(open_item.item_key, held_back=0, tokens=1), WALL
    )
    original = rig.engine.between_cycles
    coupled: list[str] = []

    async def watched(work: Callable[[], Any]) -> Any:
        coupled.append("decision")
        return await original(work)

    monkeypatch.setattr(rig.engine, "between_cycles", watched)
    assert await rig.service.delete(
        data_query("decisions", ids=[open_item.episode_id], delete=True)
    ) == {"deleted": 0, "kept": 1}
    assert rig.store.get_json(SCOPE, "learned") == LEARNED
    assert await rig.service.delete(
        data_query("decisions", ids=[closed_item.episode_id], delete=True)
    ) == {"deleted": 1, "kept": 0}
    assert rig.store.get_json(SCOPE, "learned") is None
    assert await rig.service.delete(
        data_query("suggestions", ids=[open_item.item_key], delete=True)
    ) == {"deleted": 0, "kept": 1}
    assert await rig.service.delete(
        data_query("suggestions", ids=[closed_item.item_key], delete=True)
    ) == {"deleted": 1, "kept": 0}
    assert await rig.service.delete(data_query("calls", ids=[call_id], delete=True)) == {
        "deleted": 1,
        "kept": 0,
    }
    assert coupled == ["decision", "decision"]
    assert rig.engine.wakes == ["change"] * 5


async def test_data_delete_waits_for_a_cycle_and_cancels_model_requests(rig: Rig) -> None:
    before = recorded(rig, "Before")
    await rig.service.save_key(KEY)
    rig.model.gate = asyncio.Event()
    rig.model.asked.clear()
    testing = asyncio.ensure_future(rig.service.test())
    await asyncio.wait_for(rig.model.asked.wait(), HANG_GUARD_S)
    rig.model.asked.clear()
    rig.observation = observation(task("Task a"))
    reached, release = threading.Event(), threading.Event()

    def hold(_key: str) -> None:
        reached.set()
        release.wait(HANG_GUARD_S)

    rig.hold = hold
    cycle = asyncio.ensure_future(rig.engine.recheck())
    assert await asyncio.to_thread(reached.wait, HANG_GUARD_S)
    deleting = asyncio.ensure_future(
        rig.service.delete(data_query("decisions", ids=[before], delete=True))
    )
    await asyncio.wait((deleting,), timeout=SETTLE_S)
    assert not deleting.done()
    assert episode(rig, before)["episode_id"] == before
    release.set()
    assert await asyncio.wait_for(deleting, HANG_GUARD_S) == {"deleted": 1, "kept": 0}
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    assert await asyncio.wait_for(testing, HANG_GUARD_S) == {
        "ok": False,
        "latency_ms": None,
        "version": None,
        "error": "unreachable",
    }
    assert rig.model.in_flight == 0
    assert [row["episode_id"] for row in rig.rows("decision_episodes")] == []


async def test_data_delete_drops_beats_candidates(rig: Rig) -> None:
    await five_marked_wrong(rig)
    await rig.engine._beat()
    assert SCOPE in rig.engine._improve
    result = await rig.service.delete(
        data_query("decisions", delete=True, topic="tasks_due", until=stamp(WALL + WEEK))
    )
    assert result == {"deleted": len(FIVE), "kept": 0}
    assert SCOPE not in rig.engine._improve


async def test_delete_all_answers_the_count(rig: Rig) -> None:
    await rig.service.save_key(KEY)
    rig.model.answers = dict(STRONG)
    rig.observation = observation(task(SEEDLINGS), inbox=1)
    await rig.engine.recheck()
    tables = ("decision_episodes", "decision_items", "decision_calls")
    before = [len(rig.rows(table)) for table in tables]
    assert all(before)  # items and calls differ from episodes, so a summed count would show
    episodes = (await rig.service.page())["data"]["episodes"]  # what the user was asked about
    assert await rig.service.delete_all() == {"deleted": episodes}
    assert [rig.rows(table) for table in tables] == [[], [], []]  # episodes, items and calls gone
    assert rig.store.spent_on(DAY) > 0  # the spend stays
    assert rig.engine.wakes == ["change", "change"]

    # A stopped engine still deletes.
    await rig.engine.stop()
    put(rig, "Listed")
    assert await rig.service.delete_all() == {"deleted": 1}
    assert [rig.rows(table) for table in tables] == [[], [], []]


def dangling(rig: Rig) -> list[str]:
    """The open items whose episode is gone."""
    episodes = {row["episode_id"] for row in rig.rows("decision_episodes")}
    return [
        row["item_key"]
        for row in rig.rows("decision_items")
        if row["state"] == "open" and row["episode_id"] not in episodes
    ]


async def test_delete_all_waits_for_a_cycles_item_and_leaves_none_without_its_episode(
    rig: Rig,
) -> None:
    first, second = task("Task a"), task("Task b")
    rig.observation = observation(first, second)
    await rig.engine.recheck()
    key = item_key("tasks_due", TASKS, first.text)
    before = rig.agenda.item(SCOPE, key)
    assert before is not None and before.level == "list"

    # The next cycle has read Task a and its episode and decides it again, the same way, when
    # Delete all lands.
    reached, release = threading.Event(), threading.Event()

    def hold(held: str) -> None:
        if held == key:
            reached.set()
            release.wait(HANG_GUARD_S)

    rig.hold = hold
    cycle = asyncio.ensure_future(rig.engine.recheck())
    assert await asyncio.to_thread(reached.wait, HANG_GUARD_S)
    episodes = (await rig.service.page())["data"]["episodes"]
    deleting = asyncio.ensure_future(rig.service.delete_all())
    await asyncio.wait((deleting,), timeout=SETTLE_S)
    release.set()
    assert await asyncio.wait_for(deleting, HANG_GUARD_S) == {"deleted": episodes}
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    # Task a was committed whole before the delete, and the cycle stopped before Task b.
    assert dangling(rig) == []
    assert [rig.rows(table) for table in ("decision_episodes", "decision_items")] == [[], []]

    # A cycle after the delete runs as usual: Task a comes back on an episode of its own, and
    # the user's feedback on it lands.
    rig.hold = None
    await rig.engine.recheck()
    again = rig.agenda.item(SCOPE, key)
    assert again is not None and again.episode_id != before.episode_id
    assert dangling(rig) == []
    await rig.service.feedback(shown(key, "useful"))
    closed = rig.agenda.item(SCOPE, key)
    assert closed is not None and closed.state == "acted"
    assert episode(rig, again.episode_id)["feedback"] == "useful"


async def test_delete_all_cancels_the_model_requests_in_flight(rig: Rig) -> None:
    await decided(rig)  # on the rules: no key yet
    await rig.service.save_key(KEY)
    rig.model.gate = asyncio.Event()  # the model never answers
    rig.model.asked.clear()
    testing = asyncio.ensure_future(rig.service.test())
    await asyncio.wait_for(rig.model.asked.wait(), HANG_GUARD_S)
    rig.model.asked.clear()
    cycle = asyncio.ensure_future(rig.engine.recheck())
    await asyncio.wait_for(rig.model.asked.wait(), HANG_GUARD_S)
    assert rig.model.in_flight == 2  # the key test's request and the cycle's

    # Delete all halts both and does not wait for an answer.
    episodes = (await rig.service.page())["data"]["episodes"]
    assert await asyncio.wait_for(rig.service.delete_all(), HANG_GUARD_S) == {"deleted": episodes}
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    assert await asyncio.wait_for(testing, HANG_GUARD_S) == {
        "ok": False,
        "latency_ms": None,
        "version": None,
        "error": "unreachable",
    }
    assert rig.model.in_flight == 0
    tables = ("decision_episodes", "decision_items", "decision_calls")
    assert [rig.rows(table) for table in tables] == [[], [], []]  # the halted cycle wrote none


# ---- learning ----------------------------------------------------------------------------------


async def test_page_learning_block(rig: Rig) -> None:
    # Nothing learned yet: the start values, no labels, no snapshot and no Reset.
    block = (await rig.service.page())["learning"]
    assert (block["enabled"], block["labels"], block["min_labels"]) == (True, 0, MIN_LABELS)
    assert (block["fitted_at"], block["since"]) == (None, None)
    assert block["attention_price"] == arbiter.START_LAMBDA and at_start(block)

    # Labelled model decisions, each with a shadow summary, one not yet labelled, a rules
    # decision the user labelled, and a stored snapshot and λ.
    rig.values["decision.shadow"] = "latest"
    shadowed = ShadowSummary(agree=True, kind_agree=False, level="badge", p=0.7, setting="latest")
    useful = {"goal_link": NoulAnswer(0.9)}
    wrong = {"goal_link": NoulAnswer(0.2)}
    recorded(
        rig, "Useful", level="badge", p=0.9, answers=useful, shadow=shadowed, feedback="useful"
    )
    other = replace(shadowed, setting="preview")  # a version not shadowed now
    recorded(
        rig,
        "Wrong",
        level="badge",
        p=0.2,
        answers=wrong,
        shadow=other,
        feedback="wrong",
    )
    recorded(rig, "No reply", level="badge", p=0.6, answers={"goal_link": NoulAnswer(0.6)})
    recorded(rig, "By the rules", level="list", feedback="useful")
    # Held back silent, unlabelled, where the shadow would have nudged: no row to fit, but a
    # decision in the shadow all the same.
    louder = replace(shadowed, agree=False, kind_agree=True, level="nudge")
    recorded(rig, "Held back", p=0.3, answers=wrong, shadow=louder)
    start = learning.relearn([], now=WALL)
    learned = replace(
        start,
        weights={**start.weights, "bias": -1.0, "topic.tasks_due": 0.5},
        reach={**start.reach, "badge": 0.7},
        costs={**start.costs, "nudge": 0.5},
        support={
            **start.support,
            "topic.tasks_due": 4,
            "reach.badge": 3,
            "costs.nudge": 2,
        },
    )
    rig.store.set_json(SCOPE, "learned", learning.encode(learned), WALL)
    rig.store.set_json(SCOPE, "lambda", 1.25, WALL)

    block = (await rig.service.page())["learning"]
    assert (block["enabled"], block["labels"], block["fitted_at"], block["since"]) == (
        True,
        2,
        stamp(WALL),
        None,
    )
    assert (block["attention_price"], block["bias"]) == (1.25, {"start": -0.5, "now": -1.0})
    topics = {row["candidate"]: row for row in block["topics"]}
    reach = {row["level"]: row for row in block["reach"]}
    costs = {row["level"]: row for row in block["costs"]}
    assert (topics["tasks_due"]["now"], topics["tasks_due"]["support"]) == (0.5, 4)
    assert (reach["badge"]["now"], reach["badge"]["support"]) == (0.7, 3)
    assert (costs["nudge"]["now"], costs["nudge"]["support"]) == (0.5, 2)
    assert "ja_floor" not in block
    # How well p matched the user, over the labelled model decisions alone.
    scored = block["calibration"]
    assert (scored["n"], scored["brier"]) == (2, 0.025)
    assert set(scored) == {"n", "brier", "bins"}
    assert [(one["n"], one["mean_p"], one["rate"]) for one in scored["bins"]] == [
        (0, None, None),
        (1, 0.2, 0.0),
        (0, None, None),
        (0, None, None),
        (1, 0.9, 1.0),
    ]
    goal_link = next(row for row in block["questions"] if row["name"] == "goal_link")
    assert (goal_link["yes_n"], goal_link["yes_rate"], goal_link["no_n"], goal_link["no_rate"]) == (
        1,
        1.0,
        1,
        0.0,
    )
    assert block["shadow"] == {
        "setting": "latest",
        "decisions": 2,
        "agree": 1,
        "kind_agree": 1,
        "labels": 1,
        "brier": 0.09,
        "live_brier": 0.01,
    }

    # Learning off: the start values decide, and show; what happened still shows.
    rig.values["decision.learning"] = False
    off = (await rig.service.page())["learning"]
    assert (off["enabled"], off["fitted_at"], off["labels"]) == (False, None, 2) and at_start(off)
    assert (off["calibration"], off["shadow"]) == (block["calibration"], block["shadow"])

    # A snapshot that does not read back is no snapshot.
    rig.values["decision.learning"] = True
    rig.store.set_json(SCOPE, "learned", {**LEARNED, "v": 2}, WALL)
    unread = (await rig.service.page())["learning"]
    assert unread["fitted_at"] is None and at_start(unread)


async def test_the_learning_block_reads_what_an_earlier_version_stored(rig: Rig) -> None:
    """Earlier versions stored each decision's detected language, and a snapshot that also held
    a confidence-floor lift for items they took to be Japanese with per-language label counts.
    Such rows count like any other, and such a snapshot steers as one without those members."""
    yes, no = {"goal_link": NoulAnswer(0.9)}, {"goal_link": NoulAnswer(0.2)}
    useful = recorded(rig, "Useful", level="badge", p=0.9, answers=yes, feedback="useful")
    wrong = recorded(rig, "Wrong", level="badge", p=0.2, answers=no, feedback="wrong")
    with rig.state._connection(write=True) as connection:
        for episode_id, language in ((useful, "en"), (wrong, "ja")):
            connection.execute(
                "UPDATE decision_episodes SET language=? WHERE episode_id=?", (language, episode_id)
            )
    start = learning.relearn([], now=WALL)
    learned = replace(start, weights={**start.weights, "bias": -1.0})
    older = learning.encode(learned) | {
        "ja_lift": 0.1,
        "support": dict(learned.support) | {"labels.en": 1, "labels.ja": 1},
    }
    rig.store.set_json(SCOPE, "learned", older, WALL)

    block = (await rig.service.page())["learning"]
    assert (block["labels"], block["fitted_at"]) == (2, stamp(WALL))
    assert block["bias"] == {"start": -0.5, "now": -1.0}
    assert "ja_floor" not in block
    assert (block["calibration"]["n"], block["calibration"]["brier"]) == (2, 0.025)
    assert set(block["calibration"]) == {"n", "brier", "bins"}


async def test_the_shadow_block_counts_silent_decisions_until_a_reset(rig: Rig) -> None:
    """Five fresh answers the live version held back silent, each with the shadow's summary,
    are five decisions in the shadow though none is shown or labelled; a Reset restarts the
    comparison with the rest of learning."""
    rig.values["decision.shadow"] = "latest"
    louder = ShadowSummary(agree=False, kind_agree=True, level="nudge", p=0.8, setting="latest")
    for n in range(5):
        recorded(rig, f"Held {n}", p=0.3, answers={"goal_link": NoulAnswer(0.4)}, shadow=louder)
    block = (await rig.service.page())["learning"]
    assert block["shadow"] == {
        "setting": "latest",
        "decisions": 5,
        "agree": 0,
        "kind_agree": 5,
        "labels": 0,
        "brier": None,
        "live_brier": None,
    }
    assert (block["labels"], block["calibration"]["n"]) == (0, 0)  # nothing to fit

    rig.clock.advance(60)
    after = (await rig.service.preference("reset_learning", None))["learning"]
    assert (after["shadow"]["decisions"], after["shadow"]["agree"]) == (0, 0)


async def test_reset_learning_forgets_numbers_and_keeps_decisions(rig: Rig) -> None:
    for what, p, feedback in (("Useful", 0.9, "useful"), ("Wrong", 0.2, "wrong")):
        answers = {"goal_link": NoulAnswer(p)}
        recorded(rig, what, level="badge", p=p, answers=answers, feedback=feedback)
    rig.store.set_json(SCOPE, "learned", LEARNED, WALL)
    rig.store.set_json(SCOPE, "lambda", 1.25, WALL)
    rig.store.set_json(OTHER_SCOPE, "learned", LEARNED, WALL)
    before = (await rig.service.page())["learning"]
    assert (before["labels"], before["fitted_at"], before["since"]) == (2, stamp(WALL), None)
    assert (before["bias"]["now"], before["attention_price"]) == (-1.0, 1.25)
    episodes = rig.rows("decision_episodes")

    rig.clock.advance(60)
    restart = WALL + timedelta(seconds=60)
    after = (await rig.service.preference("reset_learning", None))["learning"]
    # The snapshot and λ are gone and learning restarts now, so the start values decide.
    assert (after["labels"], after["fitted_at"], after["since"]) == (0, None, stamp(restart))
    assert after["attention_price"] == arbiter.START_LAMBDA and at_start(after)
    assert after["calibration"]["n"] == 0
    assert (rig.store.get_json(SCOPE, "learned"), rig.store.get_json(SCOPE, "lambda")) == (
        None,
        None,
    )
    assert rig.store.learn_since(SCOPE) == restart
    # The decisions and their labels stay, and so does another workspace's snapshot.
    assert rig.rows("decision_episodes") == episodes
    assert rig.store.get_json(OTHER_SCOPE, "learned") == LEARNED
    assert rig.engine.wakes == ["change"]

    # Decisions from the Reset on count again.
    answers = {"goal_link": NoulAnswer(0.9)}
    recorded(rig, "Afterwards", restart, level="badge", p=0.9, answers=answers, feedback="useful")
    assert (await rig.service.page())["learning"]["labels"] == 1


async def test_reset_learning_waits_for_a_running_cycle(rig: Rig) -> None:
    first, second = task("Task a"), task("Task b")
    rig.observation = observation(first, second)
    await rig.engine.recheck()
    rig.store.set_json(SCOPE, "learned", LEARNED, WALL)
    key = item_key("tasks_due", TASKS, first.text)

    # The next cycle is deciding Task a when Reset lands: Reset halts it and waits for it to
    # stop, as it waits for anything else that holds the engine's cycle lock.
    reached, release = threading.Event(), threading.Event()

    def hold(held: str) -> None:
        if held == key:
            reached.set()
            release.wait(HANG_GUARD_S)

    rig.hold = hold
    cycle = asyncio.ensure_future(rig.engine.recheck())
    assert await asyncio.to_thread(reached.wait, HANG_GUARD_S)
    resetting = asyncio.ensure_future(rig.service.preference("reset_learning", None))
    await asyncio.wait((resetting,), timeout=SETTLE_S)
    assert not resetting.done()
    assert rig.store.get_json(SCOPE, "learned") == LEARNED  # nothing forgotten mid-cycle
    release.set()
    page = await asyncio.wait_for(resetting, HANG_GUARD_S)
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    assert page["learning"]["fitted_at"] is None
    assert rig.store.get_json(SCOPE, "learned") is None
    assert dangling(rig) == []


async def test_delete_all_forgets_the_learned_snapshot(rig: Rig) -> None:
    await decided(rig)
    rig.store.set_json(SCOPE, "learned", LEARNED, WALL)
    rig.store.set_json(SCOPE, "lambda", 1.25, WALL)
    rig.store.set_json(OTHER_SCOPE, "learned", LEARNED, WALL)
    assert (await rig.service.page())["learning"]["fitted_at"] == stamp(WALL)
    assert await rig.service.delete_all() == {"deleted": 2}
    # The snapshot goes with the decisions it was learned from; λ stays, and nothing restarts.
    learned = (await rig.service.page())["learning"]
    assert (learned["fitted_at"], learned["since"], learned["attention_price"]) == (
        None,
        None,
        1.25,
    )
    assert rig.store.get_json(SCOPE, "learned") is None
    assert rig.store.get_json(OTHER_SCOPE, "learned") == LEARNED


# ---- the beat's self-improvement candidates --------------------------------------


def retro_states(rig: Rig) -> list[str]:
    """The state of each stored retro_due item."""
    return [row["state"] for row in rig.rows("decision_items") if row["candidate"] == "retro_due"]


async def five_marked_wrong(rig: Rig) -> None:
    """One cycle over five tasks due today, each of which the user then marks Not useful: the
    labels a beat counts toward a retro."""
    rig.observation = observation(*(task(text) for text in FIVE))
    await rig.engine.recheck()
    for text in FIVE:
        await rig.service.feedback(shown(item_key("tasks_due", TASKS, text), "wrong"))


async def test_delete_all_drops_the_beats_self_improvement_candidates(rig: Rig) -> None:
    """The beat keeps a retro from the labels; after Delete all the next cycle decides none,
    rather than bringing it back from rows that are gone until the next beat."""
    await five_marked_wrong(rig)
    await rig.engine._beat()
    await rig.engine.recheck()
    assert retro_states(rig) == ["open"]
    await rig.service.delete_all()
    await rig.engine.recheck()
    assert retro_states(rig) == []
    await rig.engine._beat()  # the next beat reads the rows anew: no label is left
    await rig.engine.recheck()
    assert retro_states(rig) == []


async def test_reset_drops_the_beats_self_improvement_candidates(rig: Rig) -> None:
    """After Reset the next cycle decides no retro counted from labels before it: the open one
    goes stale, and the next beat, reading only decisions since the Reset, keeps none."""
    await five_marked_wrong(rig)
    await rig.engine._beat()
    await rig.engine.recheck()
    assert retro_states(rig) == ["open"]
    rig.clock.advance(60)
    await rig.service.preference("reset_learning", None)
    await rig.engine.recheck()
    assert retro_states(rig) == ["stale"]
    await rig.engine._beat()
    await rig.engine.recheck()
    assert retro_states(rig) == ["stale"]


async def test_delete_all_waits_for_a_beat_and_drops_what_it_counted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A beat that read the rows before Delete all came keeps its candidates under the cycle
    lock; Delete all waits for it and then drops them, so the next cycle decides none."""
    await five_marked_wrong(rig)
    counted: list[str] = []
    entered, release = threading.Event(), threading.Event()
    from_rows = improve.from_rows

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()  # the rows are read; what the beat keeps from them waits
        release.wait(HANG_GUARD_S)
        found = from_rows(*args, **kwargs)
        counted.extend(candidate.type for candidate in found)
        return found

    monkeypatch.setattr(improve, "from_rows", held)
    beat = asyncio.ensure_future(rig.engine._beat())
    try:
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        deleting = asyncio.ensure_future(rig.service.delete_all())
        await asyncio.wait((deleting,), timeout=SETTLE_S)
        assert not deleting.done()  # Delete all waits for the beat
    finally:
        release.set()
    await asyncio.wait_for(beat, HANG_GUARD_S)
    assert await asyncio.wait_for(deleting, HANG_GUARD_S) == {"deleted": len(FIVE)}
    assert counted == ["retro_due"]  # the beat counted a retro from the rows Delete all removed
    await rig.engine.recheck()
    assert retro_states(rig) == []


# ---- question wording proposals -------------------------------------------------


def pretty(wording: Mapping[str, object]) -> str:
    """A wording as a proposal's view shows it."""
    return json.dumps(wording, indent=2, ensure_ascii=False, sort_keys=True)


async def test_a_proposals_view_counts_its_trial_from_creation_to_its_first_decision(
    rig: Rig,
) -> None:
    """The view shows the wording in use beside the proposed one, and `tried` over the
    decisions that carry a trial summary from the proposal's creation (included) to its first
    decision (excluded), or to now while it is open; silent and unlabelled ones count too."""
    goal = {"goal_link": NoulAnswer(0.6)}
    recorded(rig, "Before", p=0.6, answers=goal, trial=TrialSummary(0.9, 0.9))
    rig.clock.advance(60)
    created = (await rig.service.create_proposal("goal_link", PROPOSED))["proposal"]
    assert created["created_at"] == stamp(rig.clock.wall())
    recorded(rig, "Apart", rig.clock.wall(), p=0.6, answers=goal, trial=TrialSummary(0.8, 0.3))
    rig.clock.advance(60)
    together = TrialSummary(0.7, 0.9)
    at = rig.clock.wall()
    recorded(
        rig, "Together", at, level="badge", p=0.6, answers=goal, trial=together, feedback="useful"
    )
    tried = {
        "decisions": 2,
        "agree": 1,
        "labels": 1,
        "live": {"yes_n": 1, "yes_rate": 1.0, "no_n": 0, "no_rate": None},
        "trial": {"yes_n": 1, "yes_rate": 1.0, "no_n": 0, "no_rate": None},
    }
    view = await rig.service.proposal(created["id"])
    assert (view["proposal"], view["label"], view["tried"]) == (created, "Linked to a goal?", tried)
    assert view["current_text"] == pretty(playbook.questions()["goal_link"])
    assert view["proposed_text"] == pretty(PROPOSED)

    # Approved, it is the wording in use; a decision from its approval on is not its trial's.
    rig.clock.advance(60)
    approved = await rig.service.act_proposal(created["id"], "approve", created["revision"], None)
    recorded(rig, "After", rig.clock.wall(), p=0.6, answers=goal, trial=TrialSummary(0.1, 0.9))
    view = await rig.service.proposal(created["id"])
    assert (view["proposal"], view["tried"]) == (approved["proposal"], tried)
    assert view["current_text"] == view["proposed_text"] == pretty(PROPOSED)

    # Undone later, its trial still ends at its approval, and the playbook's wording is back.
    rig.clock.advance(60)
    undo = approved["proposal"]["revision"]
    undone = await rig.service.act_proposal(created["id"], "reject", undo, "Too narrow")
    view = await rig.service.proposal(created["id"])
    assert (view["proposal"], view["tried"]) == (undone["proposal"], tried)
    assert view["current_text"] == pretty(playbook.questions()["goal_link"])
    assert (undone["proposal"]["state"], undone["proposal"]["reason"]) == ("rejected", "Too narrow")
    with pytest.raises(DecisionLoopError) as unknown:
        await rig.service.proposal(MISSING)
    assert unknown.value.code == "unknown_item"


async def test_act_proposal_runs_between_cycles(rig: Rig) -> None:
    """An act on a proposal halts a running cycle and waits for it to stop, as Reset does, so
    no cycle asks with a wording the act replaced: the next cycle asks the approved wording and
    tries nothing. The act is compare-and-set: one at a revision that moved is not_applicable."""
    await rig.service.save_key(KEY)
    rig.model.answers = dict(STRONG)
    created = (await rig.service.create_proposal("goal_link", PROPOSED))["proposal"]
    first, second = task("Task a"), task("Task b")
    rig.observation = observation(first, second)
    key = item_key("tasks_due", TASKS, first.text)
    reached, release = threading.Event(), threading.Event()

    def hold(held: str) -> None:
        if held == key:
            reached.set()
            release.wait(HANG_GUARD_S)

    rig.hold = hold
    cycle = asyncio.ensure_future(rig.engine.recheck())
    assert await asyncio.to_thread(reached.wait, HANG_GUARD_S)
    approving = asyncio.ensure_future(
        rig.service.act_proposal(created["id"], "approve", created["revision"], None)
    )
    await asyncio.wait((approving,), timeout=SETTLE_S)
    assert not approving.done()
    assert [record.state for record in rig.engine.proposals.list(SCOPE)] == ["open"]
    release.set()
    approved = (await asyncio.wait_for(approving, HANG_GUARD_S))["proposal"]
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    assert approved["state"] == "approved"
    assert rig.model.requests == []  # the cycle stopped before its first request

    rig.hold = None
    await rig.engine.recheck()
    asked = [request.questions for request in rig.model.requests]
    assert playbook.questions_for("tasks_due", {"goal_link": PROPOSED}) in asked
    assert all(questions.get("goal_link", PROPOSED) == PROPOSED for questions in asked)
    assert {"goal_link": PROPOSED} not in asked  # no trial: nothing is open

    for action, revision in (("reject", created["revision"]), ("approve", approved["revision"])):
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.act_proposal(created["id"], action, revision, None)
        assert refused.value.code == "not_applicable"
    assert [record.state for record in rig.engine.proposals.list(SCOPE)] == ["approved"]
    assert rig.engine.wakes == ["change"] * 3  # the key, the proposal and its approval


async def test_delete_all_keeps_the_proposals_and_the_wording_in_use(rig: Rig) -> None:
    """Delete all forgets a trial's numbers with the decisions that carry them, not the wording:
    the proposals and the wording in use stay."""
    created = (await rig.service.create_proposal("goal_link", PROPOSED))["proposal"]
    goal = {"goal_link": NoulAnswer(0.6)}
    recorded(rig, "Tried", p=0.6, answers=goal, trial=TrialSummary(0.7, 0.9))
    assert (await rig.service.proposal(created["id"]))["tried"]["decisions"] == 1
    rig.clock.advance(60)
    approved = await rig.service.act_proposal(created["id"], "approve", created["revision"], None)
    assert await rig.service.delete_all() == {"deleted": 1}
    view = await rig.service.proposal(created["id"])
    assert (view["proposal"], view["tried"]["decisions"]) == (approved["proposal"], 0)
    assert rig.engine.proposals.wording(SCOPE)[0] == {"goal_link": PROPOSED}


# ---- vault-change proposals ----------------------------------------------


async def filed(rig: Rig, *refs: str) -> dict[str, dict[str, Any]]:
    """One cycle on the rules over these files of ticked tasks: each one's item shows and is
    filed as an open change proposal. Answer each one's Approvals inbox item, by its ref."""
    rig.observation = observation(ticked=refs)
    await rig.engine.recheck()
    rows = {str(item["ref"]): item for item in rig.engine.changes.inbox_items(SCOPE)}
    assert sorted(rows) == sorted(refs) and {row["state"] for row in rows.values()} == {"open"}
    return rows


def filed_episode(rig: Rig, ref: str) -> dict[str, Any]:
    """The episode of the item the change for `ref`'s ticked tasks was filed for."""
    found = rig.agenda.item(SCOPE, item_key("tasks_ticked", ref, sha256_hex(ref)))
    assert found is not None and found.state == "done"
    return episode(rig, found.episode_id)


def label(row: Mapping[str, Any]) -> tuple[object, ...]:
    """An episode's label: its value, reason, source, time and revision."""
    return tuple(
        row[name]
        for name in (
            "feedback",
            "feedback_reason",
            "feedback_source",
            "feedback_at",
            "feedback_revision",
        )
    )


def act(rig: Rig, row: Mapping[str, Any], action: str, reason: str | None = None) -> Any:
    """The user's `action` on the change `row` shows, at the revision it shows."""
    return rig.service.act_change(str(row["id"]), action, str(row["revision"]), reason)


async def test_act_change_approves_drafts_again_and_rejects(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approve answers the change approved, with its draft; Draft again, an approval of the
    approved change, the same answer and writes nothing; Reject answers the change rejected,
    its reason stripped, and no draft. An act that moves a change's state labels the episode
    its item was filed with, from the page and at the moment of the act, read once: useful for
    an approval, wrong for a rejection. Draft again labels nothing. Each act wakes the engine."""
    rows = await filed(rig, TASKS, GARDEN)
    plan, garden = rows[TASKS], rows[GARDEN]
    unlabelled = (None, None, None, None, 0)
    assert [label(filed_episode(rig, ref)) for ref in (TASKS, GARDEN)] == [unlabelled] * 2
    # Each read of the service's clock is a second later than the last.
    reads = (WALL + timedelta(seconds=n) for n in itertools.count(1))
    monkeypatch.setattr(rig.service, "_now", lambda: next(reads))

    approved = await act(rig, plan, "approve")
    first = stamp(WALL + timedelta(seconds=1))
    changes = {item["id"]: item for item in rig.engine.changes.inbox_items(SCOPE)}
    draft = change_draft("tasks_ticked", TASKS, None, ARCHIVES[TASKS])
    assert approved == {"change": changes[plan["id"]], "draft": draft}
    assert approved["change"] == {
        **plan,
        "revision": approved["change"]["revision"],
        "state": "approved",
        "attention": False,
        "updated_at": first,
        "approved_at": first,
    }
    assert approved["change"]["revision"] != plan["revision"]
    assert label(filed_episode(rig, TASKS)) == ("useful", None, "page", first, 1)

    again = await act(rig, approved["change"], "approve")
    assert again == approved
    assert changes == {item["id"]: item for item in rig.engine.changes.inbox_items(SCOPE)}
    assert label(filed_episode(rig, TASKS)) == ("useful", None, "page", first, 1)

    rejected = await act(rig, garden, "reject", " Not now ")
    third = stamp(WALL + timedelta(seconds=3))
    changes = {item["id"]: item for item in rig.engine.changes.inbox_items(SCOPE)}
    assert rejected == {"change": changes[garden["id"]], "draft": None}
    assert rejected["change"] == {
        **garden,
        "revision": rejected["change"]["revision"],
        "state": "rejected",
        "attention": False,
        "updated_at": third,
        "rejected_at": third,
        "reason": "Not now",
    }
    assert label(filed_episode(rig, GARDEN)) == ("wrong", None, "page", third, 1)
    assert label(filed_episode(rig, TASKS)) == ("useful", None, "page", first, 1)
    assert rig.engine.wakes == ["change"] * 3


async def test_act_change_refusals_change_nothing(rig: Rig) -> None:
    """Refused as `ChangeProposals.act` orders it: a malformed act (invalid_request); an id that
    names no change of the workspace -- a question proposal's, another workspace's change, none
    -- (unknown_item); a revision that moved, or a state the action does not fit
    (not_applicable). A refusal writes nothing, labels nothing and wakes nothing."""
    rows = await filed(rig, TASKS, GARDEN)
    approved = (await act(rig, rows[TASKS], "approve"))["change"]
    rejected = (await act(rig, rows[GARDEN], "reject"))["change"]
    question = (await rig.service.create_proposal("goal_link", PROPOSED))["proposal"]
    elsewhere = rig.engine.changes.file(
        OTHER_SCOPE,
        kind="tasks_ticked",
        ref=TASKS,
        other=None,
        archive=ARCHIVES[TASKS],
        item_key=item_key("tasks_ticked", TASKS, OTHER_SCOPE),
        episode_id=MISSING,
        day=DAY,
        limit=10,
        now=WALL,
    )

    def written() -> tuple[object, ...]:
        wakes = list(rig.engine.wakes)
        return rig.rows("decision_proposals"), rig.rows("decision_episodes"), wakes

    before = written()
    plan, open_revision = approved["id"], rows[TASKS]["revision"]
    refusals = [
        ((plan, "approve", approved["revision"], "Reads well"), "invalid_request"),
        ((plan, "approve", "5" * 63, None), "invalid_request"),
        ((plan.upper(), "approve", approved["revision"], None), "invalid_request"),
        ((rejected["id"], "reject", rejected["revision"], " \n "), "invalid_request"),
        ((MISSING, "approve", approved["revision"], None), "unknown_item"),
        ((question["id"], "approve", question["revision"], None), "unknown_item"),
        ((elsewhere.proposal_id, "approve", elsewhere.revision, None), "unknown_item"),
        ((plan, "approve", open_revision, None), "not_applicable"),  # moved with the approval
        ((plan, "reject", approved["revision"], None), "not_applicable"),  # approved stays
        ((rejected["id"], "approve", rejected["revision"], None), "not_applicable"),
        ((rejected["id"], "reject", rejected["revision"], None), "not_applicable"),
    ]
    for args, code in refusals:
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.act_change(*args)
        assert refused.value.code == code, args
    # Nor does a question proposal act reach a change.
    with pytest.raises(DecisionLoopError) as crossed:
        await rig.service.act_proposal(plan, "approve", approved["revision"], None)
    assert crossed.value.code == "unknown_item"
    assert written() == before


async def test_act_change_runs_between_cycles(rig: Rig) -> None:
    """An act on a change halts a running cycle and waits for it to stop, as a proposal act
    does, so no cycle files a change or labels an episode beside it."""
    plan = (await filed(rig, TASKS))[TASKS]
    rig.observation = observation(task("Task a"))
    key = item_key("tasks_due", TASKS, "Task a")
    reached, release = threading.Event(), threading.Event()

    def hold(held: str) -> None:
        if held == key:
            reached.set()
            release.wait(HANG_GUARD_S)

    rig.hold = hold
    cycle = asyncio.ensure_future(rig.engine.recheck())
    assert await asyncio.to_thread(reached.wait, HANG_GUARD_S)
    approving = asyncio.ensure_future(act(rig, plan, "approve"))
    await asyncio.wait((approving,), timeout=SETTLE_S)
    assert not approving.done()
    assert [item["state"] for item in rig.engine.changes.inbox_items(SCOPE)] == ["open"]
    assert label(filed_episode(rig, TASKS))[0] is None
    release.set()
    approved = await asyncio.wait_for(approving, HANG_GUARD_S)
    await asyncio.wait_for(cycle, HANG_GUARD_S)
    assert approved["change"]["state"] == "approved"
    assert label(filed_episode(rig, TASKS))[0] == "useful"


async def test_act_change_stands_when_its_label_cannot_be_written(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label is written after the act: when the store refuses it, or its episode went with
    Delete all, the act stands and answers the change as it left it."""
    rows = await filed(rig, TASKS, GARDEN)

    def refuse(*args: object, **kwargs: object) -> None:
        raise DecisionLoopError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "feedback", refuse)
        approved = await act(rig, rows[TASKS], "approve")
    assert (approved["change"]["state"], approved["draft"]) == (
        "approved",
        change_draft("tasks_ticked", TASKS, None, ARCHIVES[TASKS]),
    )
    assert label(filed_episode(rig, TASKS))[0] is None

    assert await rig.service.delete_all() == {"deleted": 2}
    rejected = await act(rig, rows[GARDEN], "reject")
    assert (rejected["change"]["state"], rejected["draft"]) == ("rejected", None)
    assert rig.rows("decision_episodes") == []
    states = {item["ref"]: item["state"] for item in rig.engine.changes.inbox_items(SCOPE)}
    assert states == {TASKS: "approved", GARDEN: "rejected"}


async def test_page_counts_the_days_changes(rig: Rig) -> None:
    """The page's `changes`: the change rows created today in every workspace, the count the
    daily limit holds, and the limit setting."""
    assert (await rig.service.page())["changes"] == {"today": 0, "limit": 3}

    def file(scope: str, ref: str, at: datetime) -> None:
        rig.engine.changes.file(
            scope,
            kind="tasks_ticked",
            ref=ref,
            other=None,
            archive=ARCHIVES[ref],
            item_key=item_key("tasks_ticked", ref, scope),
            episode_id=MISSING,
            day=at.astimezone().date().isoformat(),
            limit=10,
            now=at,
        )

    file(SCOPE, TASKS, WALL)
    file(OTHER_SCOPE, TASKS, WALL)
    file(SCOPE, GARDEN, WALL - timedelta(days=1))
    rig.values["decision.limit.changes"] = 7
    assert (await rig.service.page())["changes"] == {"today": 2, "limit": 7}
    assert rig.engine.wakes == []


# ---- what no view holds -----------------------------------------------------------


async def every_view(rig: Rig) -> dict[str, Any]:
    """Each route's view, by route, over a scope whose one item the Decision model decided and
    which has had a mission."""
    views: dict[str, Any] = {"POST key": await rig.service.save_key(KEY)}
    rig.model.answers = dict(STRONG)
    rig.observation = observation(task(SEEDLINGS))
    await rig.engine.recheck()
    for table in ("decision_calls", "decision_episodes"):  # the store keeps the model id itself
        assert {row["model"] for row in rig.rows(table)} == {MODEL_IDS["pinned"]}
    views["launcher"] = await rig.service.launcher()
    views["launcher/open"] = await rig.service.open_launcher()
    views["page"] = await rig.service.page()
    views["calls"] = await rig.service.calls()
    for listed in views["calls"]["calls"]:
        views[f"calls/{listed['call_id']}"] = await rig.service.call(listed["call_id"])
    views["test"] = await rig.service.test()
    (item,) = views["launcher"]["items"]
    views["POST missions"] = await rig.service.start_mission(item["item_key"], "prepare")
    views["missions"] = await rig.service.missions()
    mission_id = views["POST missions"]["mission_id"]
    views["missions/id/stop"] = await rig.service.stop_mission(mission_id)
    views["missions/stop"] = await rig.service.stop_missions()
    views["reports"] = await rig.service.report("unwatched", "launcher")
    views["preferences"] = await rig.service.preference("review_seen", None)
    views["feedback"] = await rig.service.feedback(shown(item["item_key"], "useful"))
    views["DELETE key"] = await rig.service.remove_key()
    views["DELETE episodes"] = await rig.service.delete_all()
    return views


async def test_views_never_name_the_model_id(rig: Rig) -> None:
    views = await every_view(rig)
    for route, view in views.items():
        text = json.dumps(view).lower()
        assert "jev" not in text and "system 1" not in text, route
    assert views["page"]["model"]["version"] == {"setting": "pinned", "display": "1.14.0"}
    details = [view for route, view in views.items() if route.startswith("calls/")]
    assert len(details) == 2 and {detail["version"] for detail in details} == {"1.14.0"}
    assert views["test"]["version"] == "1.14.0"


async def test_no_view_holds_the_key(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        views = await every_view(rig)
        with pytest.raises(DecisionLoopError) as refused:
            await rig.service.save_key(f"{KEY} {KEY}")
    assert rig.model.keys and set(rig.model.keys) == {KEY}  # the key went to the model alone
    for route, view in views.items():
        assert KEY not in json.dumps(view), route
    assert (views["POST key"]["model"]["key"], views["DELETE key"]["model"]["key"]) == (
        "saved",
        "missing",
    )
    assert refused.value.code == "invalid_request"
    assert KEY not in repr(refused.value) and KEY not in repr(refused.value.extra)
    assert KEY not in caplog.text and rig.keys.load() is None
