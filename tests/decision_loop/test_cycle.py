"""One Decision cycle over a real store: the reversed kind question before a first nudge, answers
reused on an equal state hash, vanished items going stale, blocked and muted items sending
nothing, failed asks falling back to the rules, the nudge cap, the call records and spend, a
mission report's fixed kind, learned numbers reaching the terms, the attention a cycle spends,
the day's usage, the shadow ask, the wording in use and its version, a proposed wording's trial
ask, the self-improvement items' own questions and kind, a vault change listed where the arbiter
would show it louder, a Kanban card's kind, quiet period and kept item, a seat item's kind and
level, a Look item's kind and level, a code look's kind, level and work under its own grant (the
arbiter never seeing its `work_allowed`), a second look's offer and finding and a project plan's
offers and finding (the arbiter never seeing their `work_allowed` either), a message draft's kind,
level and work, a saved check's and an inbox suggestion's kind, level and work by its phase under
its own grant (the arbiter never seeing their `work_allowed`), and the round trip of the answers
kept at rest."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.decision_loop.fakes import FakeModel
from tests.decision_loop.test_arbiter import random_input
from viki_agent.automation.brief import Goal, InboxFacts, TaskFacts, TaskItem
from viki_agent.automation.kanban import Card, KanbanRead
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import (
    arbiter,
    inbox_triage,
    learning,
    message_drafts,
    playbook,
    project_plans,
    rules,
    saved_checks,
    second_looks,
    templates,
)
from viki_agent.decision_loop.agenda import DecisionAgenda, ItemWrite
from viki_agent.decision_loop.arbiter import (
    LAMBDA_MAX,
    START_LAMBDA,
    START_WEIGHTS,
    ArbiterInput,
    p_of,
)
from viki_agent.decision_loop.contracts import (
    ALPHA,
    CARDS,
    CHANGES,
    CHAT_WAIT,
    CHECK_PHASES,
    GRANTS,
    IMPROVE,
    KINDS,
    LOOKS,
    MODEL_IDS,
    TRIAGE_PHASES,
    Answer,
    AppLook,
    Candidate,
    CheckFolder,
    CheckLook,
    ChoiceAnswer,
    CodeFolder,
    CodeLook,
    ContextHint,
    DecisionLoopError,
    DecisionSettings,
    Evidence,
    Finding,
    HeavyServer,
    InboxLook,
    ItemBody,
    ItemRecord,
    MissionRecord,
    ModelError,
    ModelReply,
    ModelRequest,
    NoulAnswer,
    Outcome,
    PageFacts,
    Presence,
    SaveRow,
    ScoreAnswer,
    SecondLookHint,
    SourceHint,
    StoppedBridge,
    UncertainSend,
    VaultLook,
    WaitingChat,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.cycle import (
    ANSWERS_CHARS,
    Ask,
    CycleContext,
    CycleResult,
    ShadowSummary,
    Trial,
    TrialSummary,
    _stored_before,
    cost_micro_usd,
    decode_answers,
    decode_shadow,
    decode_trial,
    encode_answers,
    run_cycle,
)
from viki_agent.decision_loop.detectors import Observation, detect, seat_candidates
from viki_agent.decision_loop.looks import app_candidates, code_candidates, vault_candidates
from viki_agent.decision_loop.rules import POLICY_VERSION
from viki_agent.decision_loop.seats import (
    SeatAsk,
    SeatHold,
    SeatOffer,
    SeatPlan,
    SeatReport,
    SeatWorking,
)
from viki_agent.decision_loop.state import History, Mention, Owner, state_text
from viki_agent.decision_loop.stats import DecisionLoopStats
from viki_agent.decision_loop.store import DecisionLoopStore, Usage
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 22, 8, 30, tzinfo=JST)  # the user's morning, still 21 October in UTC
DAY = "2026-10-22"
SCOPE = "00000000-0000-4000-8000-0000000000a1"
PLAYBOOK = playbook.PLAYBOOK_VERSION  # the version the playbook's own questions are asked under
OTHER = "00000000-0000-4000-8000-0000000000b2"
CHAT = "00000000-0000-4000-8000-0000000000c1"
MISSION = "00000000-0000-4000-8000-0000000000f1"
REVERSED = {"kind": playbook.kind_reversed()}
# A title in Han and kana, built from code points: an item is decided alike in any script.
OTHER_SCRIPT = "".join(map(chr, (0x8A08, 0x753B, 0x3092, 0x898B, 0x76F4, 0x3059)))

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
# A goal link too weak for a nudge: the item stops at badge and needs no reversed question.
MILD: Mapping[str, Answer] = {**STRONG, "goal_link": NoulAnswer(0.6)}
# p 0.822: at the starting λ a nudge scores 1.519 and a badge 1.400, a gain short of the 0.15
# margin; at λ 1 the badge wins outright.
HELD: Mapping[str, Answer] = {
    **STRONG,
    "goal_link": NoulAnswer(0.9),
    "news": NoulAnswer(0.75),
    "actionable": NoulAnswer(0.6),
    "owner_only": NoulAnswer(0.5),
    "fits_now": NoulAnswer(0.5),
}
DISAGREE = {"kind": ChoiceAnswer("inform", 0.8, {"inform": 0.8, "ask": 0.2})}
SHADOW_SUMMARY = ShadowSummary(
    agree=True, kind_agree=False, level="nudge", p=0.823456, setting="latest"
)

HERE = Presence(
    focused=True,
    typing=False,
    away=False,
    chat_id=None,
    chat_title=None,
    note_ref=None,
    launcher_open=False,
    page=None,
)


def candidate(name: str = "plan", **changes: Any) -> Candidate:
    """An approvals item the rules list (one card waits)."""
    item = Candidate(
        item_key=item_key("approvals", SCOPE, name),
        type="approvals",
        title=f"Review the {name}",
        facts={"workspace_id": SCOPE},
        words={},
        evidence=(
            Evidence(f"10-projects/{name}/{name}.md", f"The {name} waits on a choice.", False, ()),
        ),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"native": 1, "proposals": 0},
        origin_session=None,
        work_allowed=(),
    )
    return replace(item, **changes)


class Gate:
    """The engine's `ask` as tests drive it: each request goes to `model` unless `skip` holds its
    `reversal` flag, when a limit skips it; a model error comes back as a value."""

    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.skip: set[bool] = set()
        self.seen: list[tuple[str, bool]] = []

    async def __call__(
        self, item_key: str, request: ModelRequest, *, reversal: bool
    ) -> ModelReply | ModelError | None:
        self.seen.append((item_key, reversal))
        if reversal in self.skip:
            return None
        try:
            return await self.model.ask(request, key="test-key")
        except ModelError as error:
            return error


@dataclass
class Rig:
    store: SessionStore
    episodes: DecisionLoopStore
    agenda: DecisionAgenda
    model: FakeModel
    gate: Gate

    def context(self, **changes: Any) -> CycleContext:
        ctx = CycleContext(
            scope=SCOPE,
            store=self.episodes,
            agenda=self.agenda,
            settings=DecisionSettings(enabled=True),
            presence=HERE,
            owner=Owner(),
            history_for=lambda _key: History(),
            now=NOW,
            lam=START_LAMBDA,
            weights=START_WEIGHTS,
            ask=self.gate,
            model_ok=True,
        )
        return replace(ctx, **changes)

    async def cycle(
        self, *candidates: Candidate, trigger: str = "change", **changes: Any
    ) -> CycleResult:
        return await run_cycle(self.context(**changes), candidates, trigger=trigger)

    def item(self, of: Candidate) -> ItemRecord:
        found = self.agenda.item(SCOPE, of.item_key)
        assert found is not None
        return found

    def rows(self, table: str) -> list[dict[str, Any]]:
        with self.store._connection() as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY created_at")
            ]

    def episode(self, of: Candidate) -> dict[str, Any]:
        """The newest episode recorded for `of`'s item."""
        return [row for row in self.rows("decision_episodes") if row["item_key"] == of.item_key][-1]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    store = AppState(tmp_path / "state.json", Prefs()).store
    model = FakeModel(STRONG)
    return Rig(store, DecisionLoopStore(store), DecisionAgenda(store), model, Gate(model))


def stored(record: ItemRecord) -> tuple[dict[str, Answer], bool | None]:
    assert record.answers_json is not None
    return decode_answers(record.answers_json)


def later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def measured(requests: Sequence[ModelRequest]) -> list[int]:
    """The input tokens FakeModel measures for each request."""
    return [
        math.ceil(len((canonical_json(r.state) + canonical_json(r.questions)).encode()) / 3)
        for r in requests
    ]


# ---- the reversed kind question --------------------------------------------------------


async def test_first_nudge_needs_the_reversed_question(rig: Rig) -> None:
    item = candidate()
    first = await rig.cycle(item)
    main, reversed_kind = rig.model.requests
    assert main.questions == playbook.questions()
    assert reversed_kind.questions == REVERSED
    assert (reversed_kind.state, reversed_kind.model) == (main.state, MODEL_IDS["pinned"])
    assert rig.gate.seen == [(item.item_key, False), (item.item_key, True)]
    record = rig.item(item)
    assert (record.level, record.timing, record.kind) == ("nudge", "now", "ask")
    assert stored(record) == (dict(STRONG), True)
    assert (first.loud, first.new_list, first.asked) == (1, 0, 2)
    (done,) = first.committed
    assert (done.policy, done.new_episode, done.outcome.level) == ("model", True, "nudge")

    # Later cycles on the same state pass the stored agreement on: the nudge stays, unasked.
    again = await rig.cycle(item, now=later(5))
    assert len(rig.model.requests) == 2
    assert rig.item(item).level == "nudge"
    assert (again.loud, again.asked, again.committed[0].new_episode) == (0, 0, False)

    # A new state hash clears the agreement: the ten questions and the reversed one again.
    changed = replace(item, title="Review the plan, now with a budget")
    await rig.cycle(changed, now=later(10))
    assert [request.questions for request in rig.model.requests[2:]] == [
        playbook.questions(),
        REVERSED,
    ]
    assert rig.item(changed).level == "nudge"


async def test_a_disagreeing_reversed_question_holds_the_item_at_badge(rig: Rig) -> None:
    rig.model.script({}, DISAGREE)
    item = candidate()
    result = await rig.cycle(item)
    record = rig.item(item)
    assert (record.level, result.committed[0].outcome.reason) == ("badge", "gate:reversal")
    assert stored(record) == (dict(STRONG), False)
    assert (result.loud, result.new_list) == (0, 1)

    await rig.cycle(item, now=later(5))
    assert len(rig.model.requests) == 2
    assert rig.item(item).level == "badge"


@pytest.mark.parametrize(
    ("failure", "agreement", "asked_next"),
    [
        (ModelError("refused"), False, False),  # for good: counts as disagreeing
        (ModelError("unreachable"), None, True),  # passing: asked again next cycle
        (None, None, True),  # a limit skipped it
    ],
)
async def test_an_unanswered_reversed_question_holds_the_item(
    rig: Rig, failure: ModelError | None, agreement: bool | None, asked_next: bool
) -> None:
    if failure is None:
        rig.gate.skip = {True}
    else:
        rig.model.script({}, failure)
    item = candidate()
    result = await rig.cycle(item)
    record = rig.item(item)
    assert record.level == "badge"
    assert stored(record) == (dict(STRONG), agreement)
    assert result.failed == {}
    outcomes = sorted(call.failure or call.outcome for call in rig.agenda.calls(SCOPE))
    assert outcomes == (["answered"] if failure is None else sorted(["answered", failure.code]))

    rig.gate.skip = set()
    before = len(rig.model.requests)
    await rig.cycle(item, now=later(1))
    asked = [request.questions for request in rig.model.requests[before:]]
    assert asked == ([REVERSED] if asked_next else [])
    assert rig.item(item).level == ("nudge" if asked_next else "badge")


# ---- reuse -----------------------------------------------------------------------------


async def test_same_hash_reuses_answers(rig: Rig) -> None:
    rig.model.answers = dict(MILD)
    item = candidate()
    await rig.cycle(item)
    assert len(rig.model.requests) == 1  # a badge needs no reversed question
    first = rig.item(item)
    assert (first.level, stored(first)) == ("badge", (dict(MILD), None))
    (see_why,) = [action for action in first.body.actions if action.type == "open_context"]

    # Other answers wait in the model, and the moment, the `owner` part and the presence moved on,
    # but nothing the state hash covers changed: no ask, the stored answers decide again.
    rig.model.answers = {}
    busy = replace(HERE, typing=True, chat_title="Budget talk")
    recent = Owner(recent=(Mention("Budget", None, ()),))
    result = await rig.cycle(item, now=later(30), presence=busy, owner=recent)
    assert len(rig.model.requests) == 1
    again = rig.item(item)
    assert (again.answers_json, again.state_hash) == (first.answers_json, first.state_hash)
    assert again.level == "badge" and see_why in again.body.actions
    assert (result.asked, result.committed[0].policy) == (0, "model")
    assert len(rig.rows("decision_episodes")) == 1

    # A changed excerpt is a new state: asked again, and the new answers are kept.
    edited = replace(item, evidence=(Evidence("10-projects/plan/plan.md", "Changed.", False, ()),))
    await rig.cycle(edited, now=later(40))
    assert len(rig.model.requests) == 2
    record = rig.item(edited)
    assert record.state_hash != first.state_hash
    assert stored(record)[0]["kind"] == ChoiceAnswer("inform", 1.0, {"inform": 1.0})


async def test_undecodable_stored_answers_are_treated_as_absent_not_raised(rig: Rig) -> None:
    """A stored item's answers that no longer decode — here, an int too large to become a float, as
    a corrupted row might hold — are treated by `_stored` like any other undecodable answers:
    ignored, so the cycle asks fresh instead of raising."""
    item = candidate()
    await rig.cycle(item)
    prior = rig.item(item)
    assert (prior.level, prior.timing) == ("nudge", "now")
    assert prior.state_hash is not None
    corrupt = '{"goal_link":{"noul":' + "9" * 400 + "}}"
    write = ItemWrite(
        item_key=prior.item_key,
        episode_id=prior.episode_id,
        candidate=prior.candidate,
        kind=prior.kind,
        level=prior.level,
        timing=prior.timing,
        defer_until=None,
        state_hash=prior.state_hash,
        answers_json=corrupt,
        mission_id=prior.mission_id,
        body=prior.body,
    )
    rig.agenda.upsert(SCOPE, write, NOW)
    assert rig.item(item).answers_json == corrupt

    before = len(rig.model.requests)
    result = await rig.cycle(item, now=later(1))  # must not raise OverflowError
    assert rig.item(item).state_hash == prior.state_hash  # the same state: _stored was tried
    assert len(rig.model.requests) == before + 2  # treated as absent: asked afresh, not reused
    assert (rig.item(item).level, result.committed[0].policy) == ("nudge", "model")


async def test_a_new_level_on_the_same_state_records_an_episode_without_asking(rig: Rig) -> None:
    item = candidate()
    await rig.cycle(item)
    result = await rig.cycle(item, lam=LAMBDA_MAX, now=later(5))  # a nudge now costs too much
    assert len(rig.model.requests) == 2
    assert rig.item(item).level == "badge"
    assert (result.committed[0].policy, result.committed[0].new_episode) == ("model", True)
    await rig.cycle(item, lam=LAMBDA_MAX, now=later(10))
    nudge, badge = rig.rows("decision_episodes")
    assert (nudge["level"], badge["level"]) == ("nudge", "badge")
    assert badge["state_hash"] == nudge["state_hash"]
    assert (badge["model"], badge["input_tokens"], badge["cost_micro_usd"]) == (
        MODEL_IDS["pinned"],
        None,
        None,
    )


async def test_a_new_timing_alone_records_no_episode(rig: Rig) -> None:
    item = candidate()
    await rig.cycle(item)
    result = await rig.cycle(item, presence=replace(HERE, typing=True), now=later(5))
    record = rig.item(item)
    assert (record.level, record.timing) == ("nudge", "after_focus")  # held while typing
    assert result.committed[0].new_episode is False
    (episode,) = rig.rows("decision_episodes")
    assert (episode["level"], episode["timing"]) == ("nudge", "now")


async def test_the_stored_level_is_where_hysteresis_starts(rig: Rig) -> None:
    rig.model.answers = dict(HELD)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, lam=1.0)  # a dear nudge: the badge wins
    assert rig.item(plan).level == "badge"
    # At the starting λ the nudge's gain over the badge is short of the margin: the stored badge
    # stays, unasked, while a new item with the same answers moves to the nudge.
    await rig.cycle(plan, budget, now=later(5))
    assert (rig.item(plan).level, rig.item(budget).level) == ("badge", "nudge")
    assert len(rig.model.requests) == 3  # plan once; budget's questions and its reversed one


async def test_quiet_hours_are_read_on_the_owners_clock(rig: Rig) -> None:
    quiet = DecisionSettings(enabled=True, quiet=("08:00", "09:00"))  # 08:30 here, 23:30 in UTC
    item = candidate()
    result = await rig.cycle(item, settings=quiet)
    record = rig.item(item)
    assert (record.level, record.timing) == ("nudge", "at")
    assert record.defer_until == stamp(datetime(2026, 10, 22, 9, 0, tzinfo=JST))
    assert (result.loud, result.new_list) == (0, 0)  # held, so nothing shows yet


# ---- stale -----------------------------------------------------------------------------


async def test_vanished_candidates_go_stale(rig: Rig) -> None:
    plan, budget = candidate("plan"), candidate("budget")
    first = await rig.cycle(plan, budget, model_ok=False)
    assert first.new_list == 2
    assert (rig.item(plan).state, rig.item(budget).state) == ("open", "open")

    second = await rig.cycle(plan, model_ok=False, now=later(1))
    assert (rig.item(plan).state, rig.item(budget).state) == ("open", "stale")
    assert [done.item.item_key for done in second.committed] == [plan.item_key]
    assert second.new_list == 0
    open_items, _ = rig.agenda.open_items(SCOPE)
    assert [record.item_key for record in open_items] == [plan.item_key]

    third = await rig.cycle(plan, budget, model_ok=False, now=later(2))
    assert rig.item(budget).state == "open"
    assert third.new_list == 1  # back in the list

    await rig.cycle(model_ok=False, now=later(3))
    assert (rig.item(plan).state, rig.item(budget).state) == ("stale", "stale")


# ---- nothing sent ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "blocked"),
    [
        ({"settings": DecisionSettings(enabled=True, never_send=("10-projects/plan/",))}, True),
        ({"settings": DecisionSettings(enabled=True, never_send_invalid=True)}, True),
        ({"model_ok": False}, False),
        ({"settings": DecisionSettings()}, False),  # Decision off
    ],
)
async def test_blocked_item_sends_nothing(rig: Rig, change: dict[str, Any], blocked: bool) -> None:
    item = candidate(related_ref="10-projects/plan/plan.md")
    result = await rig.cycle(item, **change)
    assert rig.model.requests == [] and rig.gate.seen == []
    record = rig.item(item)
    assert (record.level, record.answers_json) == ("list", None)
    assert (record.state_hash is None) == blocked
    assert (result.asked, result.spent_micro_usd, result.committed[0].policy) == (0, 0, "rules")
    assert result.committed[0].outcome.reason == "rules:waiting_on_you"
    assert rig.agenda.calls(SCOPE) == []
    (episode,) = rig.rows("decision_episodes")
    assert (episode["policy"], episode["policy_version"]) == ("rules", POLICY_VERSION)
    assert (episode["rules_level"], episode["answers"], episode["model"]) == ("list", None, None)


async def test_a_muted_topic_is_left_to_the_rules_unasked(rig: Rig) -> None:
    item = candidate()
    await rig.cycle(item, model_ok=False)
    episode_id = rig.item(item).episode_id
    rig.episodes.feedback(
        SCOPE, episode_id=episode_id, value="mute", reason=None, source="launcher", now=NOW
    )
    result = await rig.cycle(item, now=later(1))
    assert rig.model.requests == []
    assert rig.item(item).level == "silent"
    assert result.committed[0].outcome.reason == "rules:muted_by_owner"


# ---- failures ----------------------------------------------------------------------------------


async def test_failed_ask_falls_back_to_rules(rig: Rig) -> None:
    rig.model.script(ModelError("refused"))
    item = candidate(related_chat=CHAT)
    first = await rig.cycle(item)
    record = rig.item(item)
    assert (record.level, record.answers_json, first.committed[0].policy) == ("list", None, "rules")
    assert first.committed[0].outcome.reason == "rules:waiting_on_you"
    assert record.state_hash is not None
    assert first.failed == {item.item_key: record.state_hash}
    assert (first.asked, first.spent_micro_usd) == (1, 0)
    (call,) = rig.agenda.calls(SCOPE)
    assert (call.outcome, call.failure, call.input_tokens) == ("failed", "refused", None)
    assert (call.episode_id, call.session_id) == (record.episode_id, CHAT)
    assert not [action for action in record.body.actions if action.type == "open_context"]

    # Handed back, the memory keeps the item on the rules while its state stays the same.
    second = await rig.cycle(item, failed=first.failed, now=later(1))
    assert len(rig.model.requests) == 1
    assert (second.failed, second.asked) == (first.failed, 0)

    # A new state is asked again.
    changed = replace(item, title="Review the plan again")
    third = await rig.cycle(changed, failed=second.failed, now=later(2))
    assert len(rig.model.requests) == 3  # the questions and the reversed one
    assert (third.failed, rig.item(changed).level) == ({}, "nudge")


@pytest.mark.parametrize(
    ("code", "sticks"),
    [
        ("refused", True),
        ("too_large", True),
        ("invalid_reply", True),
        ("failed", True),
        ("unreachable", False),
        ("rate_limited", False),
        ("overloaded", False),
        ("key_rejected", False),
        ("no_key", False),
    ],
)
async def test_only_failures_of_the_request_itself_stick(rig: Rig, code: str, sticks: bool) -> None:
    rig.model.script(ModelError(code))
    item = candidate()
    result = await rig.cycle(item)
    assert rig.item(item).level == "list"
    assert (item.item_key in result.failed) == sticks
    await rig.cycle(item, failed=result.failed, now=later(1))
    assert len(rig.model.requests) == (1 if sticks else 3)


async def test_a_skipped_ask_leaves_the_rules_and_asks_next_cycle(rig: Rig) -> None:
    rig.gate.skip = {False}
    item = candidate()
    result = await rig.cycle(item)
    assert rig.model.requests == [] and rig.gate.seen == [(item.item_key, False)]
    assert (rig.item(item).level, result.asked, result.failed) == ("list", 0, {})
    assert rig.agenda.calls(SCOPE) == []
    rig.gate.skip = set()
    await rig.cycle(item, now=later(1))
    assert len(rig.model.requests) == 2 and rig.item(item).level == "nudge"


async def test_a_reply_missing_answers_is_an_invalid_reply_that_still_costs(rig: Rig) -> None:
    async def short(item_key: str, request: ModelRequest, *, reversal: bool) -> ModelReply:
        return ModelReply(MODEL_IDS["pinned"], {"goal_link": NoulAnswer(0.9)}, 3000, 40)

    item = candidate()
    result = await rig.cycle(item, ask=short)
    assert rig.item(item).level == "list"
    assert result.failed == {item.item_key: rig.item(item).state_hash}
    assert result.spent_micro_usd == cost_micro_usd(3000) == 126
    (call,) = rig.agenda.calls(SCOPE)
    assert (call.outcome, call.failure, call.input_tokens, call.cost_micro_usd) == (
        "failed",
        "invalid_reply",
        3000,
        126,
    )


# ---- the cap, the records and the spend --------------------------------------------------------


async def test_a_shown_nudge_keeps_its_level_while_new_nudges_stay_capped(rig: Rig) -> None:
    one = DecisionSettings(enabled=True, nudges_per_hour=1)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, settings=one)
    assert rig.item(plan).level == "nudge"
    result = await rig.cycle(plan, budget, settings=one, now=later(5))
    assert rig.item(plan).level == "nudge"  # its own nudge does not count against it
    capped = result.committed[1]
    assert (capped.item.level, capped.outcome.blocked_by) == ("badge", ("reversal", "nudge_cap"))
    assert [request.questions for request in rig.model.requests[2:]] == [playbook.questions()]
    assert result.loud == 0


# The cap counts showings, not decisions: the next four cases each let a second nudge into an
# hour while the cap counted nudge episodes by the time they were decided.
QUIET = ("06:00", "09:00")  # NOW, 08:30, is inside; 09:00 is not


async def test_a_held_nudge_counts_when_it_shows_not_when_it_was_decided(rig: Rig) -> None:
    one = DecisionSettings(enabled=True, nudges_per_hour=1, quiet=QUIET)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, settings=one, now=later(-120))  # 06:30: decided, held to 09:00
    assert (rig.item(plan).level, rig.item(plan).timing) == ("nudge", "at")
    shown = await rig.cycle(plan, settings=one, now=later(30))
    assert (rig.item(plan).timing, shown.loud) == ("now", 1)
    result = await rig.cycle(plan, budget, settings=one, now=later(35))
    assert rig.item(plan).level == "nudge"
    capped = result.committed[1]
    assert (capped.item.level, capped.outcome.blocked_by) == ("badge", ("reversal", "nudge_cap"))
    assert result.loud == 0
    # Budget's request tells the model of the hour's one showing.
    assert '"nudges_this_hour":"one"' in canonical_json(rig.model.requests[-1].state)


async def test_a_nudge_back_after_a_drop_counts_again(rig: Rig) -> None:
    one = DecisionSettings(enabled=True, nudges_per_hour=1)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, settings=one)
    await rig.cycle(plan, settings=one, lam=LAMBDA_MAX, now=later(30))  # a dear nudge: badge
    assert rig.item(plan).level == "badge"
    again = await rig.cycle(plan, settings=one, now=later(120))
    assert (rig.item(plan).level, again.loud) == ("nudge", 1)
    assert len(rig.rows("decision_episodes")) == 2  # the same day's nudge episode, reused
    result = await rig.cycle(plan, budget, settings=one, now=later(125))
    assert rig.item(plan).level == "nudge"
    capped = result.committed[1]
    assert (capped.item.level, capped.outcome.blocked_by) == ("badge", ("reversal", "nudge_cap"))


async def test_nudges_held_together_show_within_the_cap(rig: Rig) -> None:
    one = DecisionSettings(enabled=True, nudges_per_hour=1, quiet=QUIET)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, settings=one, now=later(-120))  # 06:30, an hour before budget's
    await rig.cycle(plan, budget, settings=one, now=later(-20))
    assert [rig.item(item).timing for item in (plan, budget)] == ["at", "at"]
    result = await rig.cycle(plan, budget, settings=one, now=later(30))  # both due at 09:00
    assert [rig.item(item).level for item in (plan, budget)] == ["nudge", "badge"]
    assert result.loud == 1


@pytest.mark.parametrize(("per_hour", "level"), [(1, "badge"), (2, "nudge")])
async def test_a_nudge_released_from_a_hold_counts_its_own_showing_too(
    rig: Rig, per_hour: int, level: str
) -> None:
    """Typing holds a shown nudge back; released, it would show anew, so its own showing of the
    hour counts against it, and showing again it counts against the next item."""
    settings = DecisionSettings(enabled=True, nudges_per_hour=per_hour)
    plan, budget = candidate("plan"), candidate("budget")
    await rig.cycle(plan, settings=settings)
    await rig.cycle(plan, settings=settings, presence=replace(HERE, typing=True), now=later(5))
    assert (rig.item(plan).level, rig.item(plan).timing) == ("nudge", "after_focus")
    result = await rig.cycle(plan, budget, settings=settings, now=later(10))
    assert (rig.item(plan).level, rig.item(budget).level) == (level, "badge")
    assert result.loud == (1 if level == "nudge" else 0)


async def test_a_nudge_promoted_to_an_alert_is_a_new_showing(rig: Rig) -> None:
    """The alert shows the user something new, so it counts as a showing of its own."""
    alerts = DecisionSettings(enabled=True, nudges_per_hour=1, alerts=True)
    task = candidate(
        "task",
        type="tasks_due",
        rules_evidence={"overdue": 0, "today": 1, "soon": 0},
        deadline=NOW + timedelta(hours=2, minutes=30),
    )
    budget = candidate("budget")
    await rig.cycle(task, settings=alerts)  # due in two and a half hours: a nudge
    assert rig.item(task).level == "nudge"
    promoted = await rig.cycle(task, settings=alerts, now=later(40))  # due within two hours
    assert (rig.item(task).level, promoted.loud) == ("alert", 1)
    # 65 minutes on, the first showing has left the hour; the alert's has not.
    result = await rig.cycle(task, budget, settings=alerts, now=later(65))
    assert (rig.item(task).level, rig.item(budget).level) == ("alert", "badge")
    assert "nudge_cap" in result.committed[1].outcome.blocked_by


async def test_each_request_is_recorded_with_its_measured_spend(rig: Rig) -> None:
    item = candidate(related_chat=CHAT)
    result = await rig.cycle(item)
    requests = rig.model.requests
    tokens = [
        math.ceil(len((canonical_json(r.state) + canonical_json(r.questions)).encode()) / 3)
        for r in requests
    ]
    costs = [cost_micro_usd(n) for n in tokens]
    assert result.spent_micro_usd == sum(costs) == rig.episodes.spent_on(DAY)
    assert rig.episodes.spent_on("2026-10-21") == 0  # the user's day, not the UTC one

    record = rig.item(item)
    # Both calls carry the cycle's time, so they are told apart by what they asked.
    calls = [rig.agenda.call(SCOPE, entry.call_id) for entry in rig.agenda.calls(SCOPE)]
    by_questions = {call.body.questions_text: call for call in calls}
    replies = (dict(STRONG), {"kind": STRONG["kind"]})
    for request, n, cost, answers in zip(requests, tokens, costs, replies, strict=True):
        call = by_questions[playbook.questions_text(request.questions)]
        assert (call.outcome, call.failure, call.input_tokens, call.cost_micro_usd) == (
            "answered",
            None,
            n,
            cost,
        )
        assert (call.episode_id, call.session_id, call.model) == (
            record.episode_id,
            CHAT,
            MODEL_IDS["pinned"],
        )
        body = call.body
        assert body.state_text == state_text(requests[0].state)
        assert body.questions_text == playbook.questions_text(request.questions)
        questions = playbook.estimate_tokens(playbook.questions_text(request.questions))
        assert body.split[-1] == ("questions", questions)
        assert body.answers_json is not None
        assert decode_answers(body.answers_json) == (answers, None)
        assert (body.version_setting, body.playbook_version) == ("pinned", PLAYBOOK)

    (see_why,) = [action for action in record.body.actions if action.type == "open_context"]
    main = by_questions[playbook.questions_text(playbook.questions())]
    assert see_why.call_id == main.call_id  # the call the answers came from

    (episode,) = rig.rows("decision_episodes")
    assert (episode["policy"], episode["policy_version"]) == ("model", f"playbook-{PLAYBOOK}")
    assert (episode["level"], episode["rules_level"], episode["day"]) == ("nudge", "list", DAY)
    assert (episode["input_tokens"], episode["cost_micro_usd"]) == (sum(tokens), sum(costs))
    assert (episode["trigger"], episode["state_hash"]) == ("change", record.state_hash)
    assert decode_answers(episode["answers"]) == (dict(STRONG), True)


async def test_a_running_mission_rides_on_its_item(rig: Rig) -> None:
    item = candidate(work_allowed=("prepare",))

    def mission(mission_id: str, **changes: Any) -> MissionRecord:
        record = MissionRecord(
            mission_id=mission_id,
            scope=SCOPE,
            session_id=CHAT,
            item_key=item.item_key,
            work="prepare",
            state="running",
            reason="",
            title="Review the plan",
            brief="",
            created_at="2026-10-21T23:00:00.000Z",
            deadline_at="2026-10-21T23:30:00.000Z",
            finished_at=None,
        )
        return replace(record, **changes)

    rig.model.answers = {**STRONG, "work": ChoiceAnswer("prepare", 0.8, {"prepare": 0.8})}
    elsewhere = mission("00000000-0000-4000-8000-0000000000f2", scope=OTHER)
    ended = mission("00000000-0000-4000-8000-0000000000f3", state="done")
    await rig.cycle(item, missions=(elsewhere, ended, mission(MISSION)))
    record = rig.item(item)
    assert record.mission_id == MISSION
    stop = [action for action in record.body.actions if action.type == "stop_mission"]
    assert [action.mission_id for action in stop] == [MISSION]
    (episode,) = rig.rows("decision_episodes")
    assert (episode["work"], episode["mission_id"]) == ("prepare", MISSION)


async def test_a_bad_trigger_or_candidate_is_refused_before_any_request(rig: Rig) -> None:
    item = candidate()
    with pytest.raises(DecisionLoopError) as raised:
        await rig.cycle(item, trigger="someday")
    assert raised.value.code == "invalid_request"
    for bad in (
        replace(candidate("x"), type="weather"),
        replace(candidate("x"), item_key="A" * 64),
    ):
        with pytest.raises(DecisionLoopError) as raised:
            await rig.cycle(item, bad)
        assert raised.value.code == "invalid_request"
    assert rig.model.requests == [] and rig.rows("decision_items") == []

    twice = await rig.cycle(item, replace(item, title="The same key"), model_ok=False)
    assert [done.candidate.title for done in twice.committed] == ["Review the plan"]


# ---- a mission report's kind -------------------------------------------------------------------


def report(reason: str) -> Candidate:
    """A mission report as the detectors make one: the mission ended for `reason`, or, with
    "ready", its plan waits on the user while the mission still runs."""
    state = "running" if reason == "ready" else "done"
    parts = ("ready",) if reason == "ready" else ()
    return Candidate(
        item_key=item_key("mission_report", MISSION, *parts),
        type="mission_report",
        title="Report from a chat VIKI started",
        facts={"workspace_id": SCOPE, "state": state, "reason": reason},
        words={},
        evidence=(),
        related_chat=CHAT,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"state": state},
        origin_session=CHAT,
        work_allowed=(),
    )


def rendered_kinds(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The kind of every outcome `templates.render` is handed, from now on."""
    kinds: list[str | None] = []
    render = templates.render

    def spy(candidate: Candidate, outcome: Outcome, *args: Any, **kwargs: Any) -> ItemBody:
        kinds.append(outcome.kind)
        return render(candidate, outcome, *args, **kwargs)

    monkeypatch.setattr(templates, "render", spy)
    return kinds


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
@pytest.mark.parametrize(
    ("reason", "kind"), [("", "report"), ("deadline", "report"), ("ready", "ready")]
)
async def test_a_mission_reports_kind_is_fixed_by_its_reason(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, model_ok: bool, reason: str, kind: str
) -> None:
    """Whatever kind the model answers ("ask" here): its reversed question still compares the
    model's kind, so the item reaches its nudge, and the report's own kind is what is kept."""
    rendered = rendered_kinds(monkeypatch)
    item = report(reason)
    result = await rig.cycle(item, model_ok=model_ok)
    record = rig.item(item)
    (done,) = result.committed
    (episode,) = rig.rows("decision_episodes")
    assert (done.outcome.kind, record.kind, episode["kind"], rendered) == (kind, kind, kind, [kind])
    if model_ok:
        assert [request.questions for request in rig.model.requests] == [
            playbook.questions(),
            REVERSED,
        ]
        assert (done.policy, record.level, stored(record)) == (
            "model",
            "nudge",
            (dict(STRONG), True),
        )
    else:
        assert (done.policy, record.level, rig.model.requests) == ("rules", "list", [])


@pytest.mark.parametrize(("model_ok", "kind"), [(True, "suggest"), (False, None)])
async def test_other_candidates_keep_the_arbiters_kind(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, model_ok: bool, kind: str | None
) -> None:
    suggest = ChoiceAnswer("suggest", 0.9, {"suggest": 0.9, "ask": 0.1})
    rig.model.answers = {**STRONG, "kind": suggest}
    rendered = rendered_kinds(monkeypatch)
    task = candidate(
        "task",
        type="tasks_due",
        rules_evidence={"overdue": 0, "today": 1, "soon": 0},
        deadline=NOW + timedelta(hours=5),
    )
    result = await rig.cycle(task, model_ok=model_ok)
    record = rig.item(task)
    (done,) = result.committed
    (episode,) = rig.rows("decision_episodes")
    assert (done.outcome.kind, record.kind, episode["kind"], rendered) == (kind, kind, kind, [kind])
    assert record.level == ("nudge" if model_ok else "list")


# ---- self-improvement items -------------------------------------------------------

LINK = "00-inbox/2026-10-20 paper.md"
CHATS = (CHAT, "00000000-0000-4000-8000-0000000000c2", "00000000-0000-4000-8000-0000000000c3")
TITLES = ("Plan the trip", "Trip budget", "Trip packing")


def improvement(kind: str, name: str = "") -> Candidate:
    """A self-improvement item of type `kind` as its detector makes one; `name` tells two items
    of a type apart."""
    item = replace(candidate(name), type=kind, facts={"workspace_id": SCOPE}, evidence=())
    if kind == "learn_source":
        return replace(
            item,
            item_key=item_key(kind, LINK, name),
            title="Sources to learn",
            evidence=(Evidence(LINK, "Read https://example.com/paper later.", False, ()),),
            related_ref=LINK,
            rules_evidence={"links": 1},
        )
    if kind == "skill_offer":
        trigger = "repeated_request"
        return replace(
            item,
            item_key=item_key(kind, trigger, *sorted(CHATS), name),
            title=TITLES[0],
            facts={"trigger": trigger, "sessions": CHATS, "titles": TITLES},
            evidence=tuple(Evidence(None, title, False, ()) for title in TITLES[1:]),
            related_chat=CHAT,
            rules_evidence={"chats": len(CHATS), "trigger": trigger},
        )
    if kind == "retro_due":
        return replace(
            item,
            item_key=item_key(kind, SCOPE, "2026-10-08T00:00:00.000Z", name),
            title="A retro",
            rules_evidence={"labels": 6},
        )
    wording = canonical_json(playbook.questions()["goal_link"])
    return replace(
        item,
        item_key=item_key(kind, SCOPE, "goal_link", "2026-10-08T00:00:00.000Z", name),
        title="Decision's questions",
        facts={"workspace_id": SCOPE, "name": "goal_link", "wording_json": wording},
        rules_evidence={"name": "goal_link"},
    )


@pytest.mark.parametrize(
    ("kind", "own"), [("learn_source", "worth_note"), ("skill_offer", "worth_skill")]
)
async def test_own_questions_ride_one_request(rig: Rig, kind: str, own: str) -> None:
    """A type's own question rides the one request its item makes, after the ten, and its
    answer is kept with theirs; the shadow asks it too. Below its floor the own-question gate
    holds the item silent. An own question is no yes/no question the user may reword, so a
    trial of it asks nothing."""
    trial_model, shadow_model = FakeModel(TRIED), FakeModel({**STRONG, own: NoulAnswer(0.9)})
    tried = {"trial": Trial(own, worded("Is it worth keeping?")), "trial_ask": Gate(trial_model)}
    rig.model.answers = {**STRONG, own: NoulAnswer(0.9)}
    item = improvement(kind)
    await rig.cycle(item, settings=SHADOW_ON, shadow_ask=Gate(shadow_model), **tried)
    main = rig.model.requests[0]
    assert main.questions == playbook.questions_for(kind, {})
    assert list(main.questions) == [*playbook.questions(), own]
    assert [request.questions for request in shadow_model.requests] == [main.questions]
    assert stored(rig.item(item))[0] == {**STRONG, own: NoulAnswer(0.9)}
    shadow = decode_shadow(rig.episode(item)["answers"])
    assert shadow is not None and shadow.agree  # the own answer reached the shadow's decision
    assert trial_model.requests == []

    rig.model.answers = {**STRONG, own: NoulAnswer(0.5)}
    below = improvement(kind, "below")
    before = len(rig.model.requests)
    result = await rig.cycle(below, now=later(5), **tried)
    (done,) = result.committed
    assert (done.outcome.level, done.outcome.reason) == ("silent", "gate:own_question")
    assert len(rig.model.requests) == before + 1  # no reversed question for a silent item
    assert trial_model.requests == []


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
@pytest.mark.parametrize("kind", IMPROVE)
async def test_improve_items_are_suggestions(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str, model_ok: bool
) -> None:
    """Whatever kind and work the model answers ("ask" and "prepare" here), a self-improvement
    item suggests and asks for no work, whichever policy decided: it only prepares a draft that
    the user sends."""
    rendered = rendered_kinds(monkeypatch)
    prepare = ChoiceAnswer("prepare", 0.9, {"prepare": 0.9, "none": 0.1})
    own = {name: NoulAnswer(0.9) for name in ("worth_note", "worth_skill")}
    rig.model.answers = {**STRONG, "work": prepare, **own}
    item = improvement(kind)
    result = await rig.cycle(item, model_ok=model_ok)
    record = rig.item(item)
    (done,) = result.committed
    (episode,) = rig.rows("decision_episodes")
    assert (done.outcome.kind, record.kind, episode["kind"], rendered) == (
        "suggest",
        "suggest",
        "suggest",
        ["suggest"],
    )
    assert (done.outcome.work, episode["work"], record.mission_id) == ("none", "none", None)
    assert (done.policy, bool(rig.model.requests)) == ("model" if model_ok else "rules", model_ok)


def vault_change(kind: str) -> Candidate:
    """A vault change as its builder makes one: a pair of notes, or a file of ticked tasks."""
    if kind == "tasks_ticked":
        tasks = "10-projects/plan/tasks.md"
        return replace(
            candidate(),
            item_key=item_key(kind, tasks, "d" * 64),
            type=kind,
            title="Ticked tasks",
            evidence=(Evidence(tasks, "", False, ()),),
            related_ref=tasks,
            rules_evidence={"ticked": 5},
        )
    first, second = "30-knowledge/cap design.md", "30-knowledge/cap tests.md"
    return replace(
        candidate(),
        item_key=item_key(kind, first, second),
        type=kind,
        title="Notes to link" if kind == "note_link" else "Superseded note",
        evidence=(
            Evidence(first, "A line names the cap tests.", False, ()),
            Evidence(second, "The cap tests begin here.", False, ()),
        ),
        related_ref=first,
        rules_evidence={"pairs": 1},
    )


@pytest.mark.parametrize(("answers", "decided"), [(STRONG, "nudge"), (MILD, "badge")])
@pytest.mark.parametrize("kind", CHANGES)
async def test_a_vault_change_the_arbiter_shows_commits_at_list(
    rig: Rig, kind: str, answers: Mapping[str, Answer], decided: str
) -> None:
    """On the same answers the arbiter puts a vault change and a self-improvement item that is
    none at a badge or a nudge, for the same reason. The other item keeps that level, logs its
    nudge as a showing and counts in the cycle's loud, new list and attention; the vault change
    commits at list, its episode too, timed now: it logs no showing for the hourly nudge cap
    and counts in none of them, as the proposal filed for it is how it shows."""
    own = dict.fromkeys(("should_link", "superseded", "worth_note"), NoulAnswer(0.9))
    rig.model.answers = {**answers, **own}
    change, other = vault_change(kind), improvement("learn_source")
    result = await rig.cycle(change, other)
    first, second = result.committed
    assert first.outcome.reason == second.outcome.reason
    assert [rig.item(item).level for item in (change, other)] == ["list", decided]
    assert (rig.item(change).timing, rig.episode(change)["level"]) == ("now", "list")
    loud = decided == "nudge"
    assert (result.loud, result.new_list) == (int(loud), int(not loud))
    assert result.attention == pytest.approx(ALPHA[decided])
    hour = NOW - timedelta(hours=1)
    assert rig.episodes.showings_since(SCOPE, hour) == int(loud)
    assert rig.episodes.showings_since(SCOPE, hour, exclude_item=other.item_key) == 0


# ---- Kanban cards -------------------------------------------------------------------


def card(status: str = "blocked", *, age: timedelta = timedelta(hours=1)) -> Candidate:
    """The one candidate `detect` makes of a Kanban card with `status`, last changed `age` ago."""
    kanban = KanbanRead((Card("kanban/a.md", "Card a", status, (), NOW - age),), (), complete=True)
    obs = Observation(SCOPE, "vault", None, (), (), None, (), kanban=kanban)
    (found,) = (c for c in detect(obs, DecisionSettings(), (), NOW) if c.type in CARDS)
    return found


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
@pytest.mark.parametrize(
    ("status", "age", "kind"),
    [
        ("blocked", timedelta(hours=1), "ask"),
        ("review", timedelta(hours=1), "ready"),
        ("working", timedelta(days=2), "report"),
        ("todo", timedelta(days=4), "report"),
    ],
)
async def test_a_card_item_takes_its_kind_from_its_status(
    rig: Rig, model_ok: bool, status: str, age: timedelta, kind: str
) -> None:
    """Whatever kind the model answers ("ask" here), whichever policy decided."""
    item = card(status, age=age)
    result = await rig.cycle(item, model_ok=model_ok)
    (done,) = result.committed
    assert (done.outcome.kind, rig.item(item).kind, rig.episode(item)["kind"]) == (kind,) * 3
    assert (done.policy, rig.item(item).level) == (
        ("model", "nudge") if model_ok else ("rules", "list")
    )


async def test_a_card_that_needs_you_lists_quietly_until_thirty_minutes_after_it_changed(
    rig: Rig,
) -> None:
    item = card(age=timedelta(minutes=29))
    quiet = await rig.cycle(item)
    record = rig.item(item)
    assert (record.level, record.timing, quiet.loud) == ("list", "now", 0)
    assert rig.episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == 0

    shown = await rig.cycle(item, now=later(1))  # its card changed 30 minutes ago
    assert (rig.item(item).level, shown.loud) == ("nudge", 1)
    assert rig.episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == 1


async def test_an_edited_card_that_already_nudged_keeps_its_nudge_and_logs_no_second_showing(
    rig: Rig,
) -> None:
    item = card(age=timedelta(hours=2))
    first = await rig.cycle(item)
    assert (rig.item(item).level, first.loud) == ("nudge", 1)

    edited = card(age=-timedelta(minutes=120))  # touched 2 h later, status unchanged
    assert edited.item_key == item.item_key
    await rig.cycle(edited, now=later(121))
    assert rig.item(item).level == "nudge"

    again = await rig.cycle(edited, now=later(151))  # 30 minutes after the edit
    assert (rig.item(item).level, again.loud) == ("nudge", 0)
    assert rig.episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == 1


async def test_an_edited_card_that_shows_a_badge_keeps_it(rig: Rig) -> None:
    rig.model.answers = dict(MILD)  # a badge: one request, no reversed question
    item = card(age=timedelta(hours=2))
    await rig.cycle(item)
    assert rig.item(item).level == "badge"

    edited = card(age=-timedelta(minutes=120))
    await rig.cycle(edited, now=later(121))
    assert rig.item(item).level == "badge"


async def test_kept_card_items_stay_open_unchanged_and_do_not_nudge_again(rig: Rig) -> None:
    item = card()
    first = await rig.cycle(item)
    record = rig.item(item)
    assert (record.level, first.loud) == ("nudge", 1)

    kept = await rig.cycle(now=later(5), keep=frozenset({item.item_key}))  # its card unread
    assert (rig.item(item), kept.committed) == (record, ())

    again = await rig.cycle(item, now=later(10))
    assert (rig.item(item).level, again.loud) == ("nudge", 0)
    assert rig.episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == 1

    await rig.cycle(now=later(15))
    assert rig.item(item).state == "stale"


# ---- seats -------------------------------------------------------------------------

ROUTINE = "00000000-0000-4000-8000-0000000000d1"


def seat_items() -> list[Candidate]:
    """One item of each seat type, a run report and a hold, as the engine builds them."""
    run = "00000000-0000-4000-8000-0000000000e{}"
    session = "00000000-0000-4000-8000-0000000000f{}"
    plan = SeatPlan(
        asks=(SeatAsk(ROUTINE, "research", run.format(1), session.format(1), False),),
        working=(SeatWorking(ROUTINE, "research", run.format(2), session.format(2)),),
        offers=(SeatOffer(ROUTINE, "research", False),),
        reports=(SeatReport(ROUTINE, "research", run.format(3), session.format(3), "failed"),),
        holds=(SeatHold(ROUTINE, "research", "limit"),),
    )
    return seat_candidates(plan, SCOPE, NOW.date().isoformat())


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
async def test_seat_items_take_their_kinds_and_list_levels(rig: Rig, model_ok: bool) -> None:
    """Whatever kind the model answers ("ask" here), whichever policy decided, each seat item
    takes its type's kind; the model's strong answers nudge only the waiting run, the others
    commit at list, their episodes too. None of them wants work."""
    items = seat_items()
    result = await rig.cycle(*items, model_ok=model_ok)
    shown = [(rig.item(item).kind, rig.item(item).level) for item in items]
    first = "nudge" if model_ok else "list"
    assert shown == [
        ("ask", first),
        ("inform", "list"),
        ("suggest", "list"),
        ("report", "list"),
        ("report", "list"),
    ]
    assert [rig.episode(item)["level"] for item in items] == [level for _, level in shown]
    assert [done.outcome.work for done in result.committed] == ["none"] * len(items)
    assert result.loud == int(model_ok)


# ---- Look items ------------------------------------------------------------------------


def look_items() -> list[Candidate]:
    """One item of each Look type, as `looks` builds them."""
    session = "00000000-0000-4000-8000-0000000000f{}"
    vault = VaultLook(
        saves=(SaveRow("save-1", False, True, NOW),), deleted=(), usage=(0, 0), drift=("m-1",)
    )
    app = AppLook(
        waiting=(WaitingChat(session.format(1), "tool-1", "question", NOW - CHAT_WAIT),),
        heavy=(HeavyServer(session.format(2), "search"),),
        sends=(UncertainSend("command-1", session.format(3), NOW),),
        bridges=(StoppedBridge("mattermost", False, None),),
    )
    found = vault_candidates(vault, SCOPE, "ordinary", NOW) + app_candidates(app, SCOPE, NOW)
    assert sorted(item.type for item in found) == sorted(LOOKS)
    return found


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
async def test_look_items_take_their_kinds_and_stay_listed(rig: Rig, model_ok: bool) -> None:
    """Whatever kind the model answers ("ask" here), whichever policy decided, a waiting chat and
    a memory drift ask and every other Look item reports; the model's strong answers that nudge
    other items leave every Look item at list, its episode too, with no work and no attention."""
    items = look_items()
    result = await rig.cycle(*items, model_ok=model_ok)
    shown = {item.type: (rig.item(item).kind, rig.item(item).level) for item in items}
    assert shown == {
        "vault_recovery": ("report", "list"),
        "memory_drift": ("ask", "list"),
        "bridge_stopped": ("report", "list"),
        "chat_waiting": ("ask", "list"),
        "send_uncertain": ("report", "list"),
        "mcp_context": ("report", "list"),
    }
    assert [rig.episode(item)["level"] for item in items] == ["list"] * len(items)
    assert [done.outcome.work for done in result.committed] == ["none"] * len(items)
    assert (result.loud, result.attention) == (0, 0.0)


# ---- code looks -------------------------------------------------------------------

FOLDER = "00000000-0000-4000-8000-0000000000c9"


def code_look(**changes: Any) -> Candidate:
    """A code look as `looks.code_candidates` builds one, for a folder idle two days."""
    look = CodeLook((CodeFolder(FOLDER, "garden-app", NOW - timedelta(days=2)),))
    (item,) = code_candidates(look, SCOPE, NOW)
    return replace(item, **changes)


@pytest.mark.parametrize("model_ok", [True, False], ids=["model", "rules"])
async def test_a_code_look_takes_suggest_and_stays_listed(rig: Rig, model_ok: bool) -> None:
    """Whatever the model answers (kind "ask" and work "none" here), whichever policy decided, a
    code look suggests, stays at list, its episode too, and prepares; under Allowed it offers
    its folder in Code and no start of its own."""
    item = code_look()
    result = await rig.cycle(item, model_ok=model_ok)
    record = rig.item(item)
    (done,) = result.committed
    episode = rig.episode(item)
    assert (done.outcome.kind, done.outcome.level, done.outcome.work) == (
        "suggest",
        "list",
        "prepare",
    )
    assert (record.kind, record.level) == ("suggest", "list")
    assert (episode["kind"], episode["level"], episode["work"]) == ("suggest", "list", "prepare")
    assert done.policy == ("model" if model_ok else "rules")
    first, *rest = record.body.actions
    assert (first.type, first.workspace_id) == ("open_code", FOLDER)
    assert "start_prepare" not in [action.type for action in rest]
    assert (result.loud, result.attention) == (0, 0.0)


def in_memory(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep what a cycle of one new item reads and writes in memory, so that hundreds of seeded
    cycles stay fast: no preferences, no stored item, no showings, each episode and item kept as
    written."""
    monkeypatch.setattr(rig.episodes, "preferences", lambda scope, now: rules.Prefs())
    monkeypatch.setattr(rig.episodes, "showings_since", lambda scope, since, **_: 0)
    monkeypatch.setattr(
        rig.episodes, "record", lambda scope, entries, **_: [SimpleNamespace(episode_id="e")]
    )
    monkeypatch.setattr(rig.agenda, "item", lambda scope, key: None)
    monkeypatch.setattr(rig.agenda, "close_missing", lambda scope, keys, now: None)

    def upsert(scope: str, write: ItemWrite, now: datetime) -> ItemRecord:
        return ItemRecord(
            write.item_key,
            write.episode_id,
            write.candidate,
            write.kind,
            write.level,
            "open",
            write.timing,
            None,
            "",
            None,
            write.mission_id,
            write.answers_json,
            write.state_hash,
            write.body,
        )

    monkeypatch.setattr(rig.agenda, "upsert", upsert)


async def test_a_look_items_work_stays_within_its_grant(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as a code look, a
    random Look grant and random `work_allowed`, decided by an arbiter that answers the draw's
    level and any work at all: the arbiter never sees a Look's `work_allowed`, and the look
    keeps kind "suggest", at most "list", and work "prepare" only while it is not silent, its
    `work_allowed` names prepare and the Look grant is not Off. It offers Take a look only for
    that work, under Ask first or while Allowed holds VIKI's own start for a later moment
    (`cycle.held_start`'s "later"), whatever the prepare grant, and never a run."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        return replace(out, work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: Counter[tuple[str, str]] = Counter()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        grant = rng.choice(GRANTS)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        draws.append((draw, rng))
        seen.clear()
        settings = replace(draw.settings, enabled=True, grant_look=grant)
        result = await rig.cycle(code_look(work_allowed=allowed), settings=settings, model_ok=False)
        assert seen and set(seen) == {()}, seed
        (done,) = result.committed
        out = done.outcome
        assert out.kind == "suggest" and out.level in ("silent", "list"), seed
        prepares = out.level != "silent" and "prepare" in allowed and grant != "off"
        assert out.work == ("prepare" if prepares else "none"), seed
        actions = [(action.type, action.label) for action in done.item.body.actions]
        starts = [action for action in actions if action[0] in ("start_prepare", "start_run")]
        held = grant == "allowed" and out.timing != "now"
        offers = prepares and (grant == "ask" or held)
        assert starts == ([("start_prepare", "Take a look")] if offers else []), seed
        shown[out.level, out.work] += 1
    # Each branch was reached: a silent look, a listed one with no work and one that prepares.
    assert {("silent", "none"), ("list", "none"), ("list", "prepare")} <= set(shown)


# ---- second looks -----------------------------------------------------------------

LOOKED_AT = "00000000-0000-4000-8000-0000000000d1"
TURN = "00000000-0000-4000-8000-0000000000d2"
LOOK_CHAT = "00000000-0000-4000-8000-0000000000d3"
OFFER = second_looks.offer(
    SecondLookHint(LOOKED_AT, TURN, "long_session", "Garden plan", ("notes/garden-plan.md",)),
    SCOPE,
)
FINDING = second_looks.finding(
    Finding(MISSION, SCOPE, LOOK_CHAT, "long_task_look", "flaw", "Garden plan")
)


def offer(**changes: Any) -> Candidate:
    assert OFFER is not None
    return replace(OFFER, **changes)


async def test_a_second_look_offer_stays_listed_and_a_finding_keeps_the_arbiters_level(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the arbiter answers (a nudge to ask, with work to prepare, here), which never
    sees either item's `work_allowed`: the offer suggests at list and prepares, offering the chat
    in view and no start of its own under Allowed; the finding suggests at the arbiter's nudge,
    with no work and Open plan to the look's chat alone."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []

    def loud(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        return replace(decide(inp), kind="ask", level="nudge", timing="now", work="prepare")

    monkeypatch.setattr(arbiter, "decide", loud)
    result = await rig.cycle(offer(), FINDING, model_ok=False)
    assert set(seen) == {()}
    looked, found = result.committed
    assert (looked.outcome.kind, looked.outcome.level, looked.outcome.work) == (
        "suggest",
        "list",
        "prepare",
    )
    assert (found.outcome.kind, found.outcome.level, found.outcome.work) == (
        "suggest",
        "nudge",
        "none",
    )
    assert [(a.type, a.session_id) for a in looked.item.body.actions] == [("open_chat", LOOKED_AT)]
    assert [(a.type, a.label, a.session_id) for a in found.item.body.actions] == [
        ("open_chat", "Open plan", LOOK_CHAT)
    ]
    assert (rig.episode(FINDING)["kind"], rig.episode(FINDING)["level"]) == ("suggest", "nudge")
    assert result.loud == 1


async def test_a_second_looks_work_stays_within_its_grant(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as a second-look offer
    or finding, a random second-look grant and random `work_allowed`, decided by an arbiter that
    answers the draw's level and any work at all: the arbiter never sees a second look's
    `work_allowed`. An offer keeps kind "suggest", at most "list", and work "prepare" only while
    it is not silent, its `work_allowed` names prepare and the grant is not Off, offering Take a
    second look only for that work, under Ask first or while Allowed holds VIKI's own start for
    a later moment, whatever the prepare grant; a finding keeps
    "suggest", the arbiter's level and no work, and offers no start."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []
    levels: list[str] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        levels.append(out.level)
        return replace(out, work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: Counter[tuple[bool, str, str]] = Counter()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        grant = rng.choice(GRANTS)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        is_finding = rng.random() < 0.5
        item = replace(FINDING if is_finding else offer(), work_allowed=allowed)
        draws.append((draw, rng))
        seen.clear()
        levels.clear()
        settings = replace(draw.settings, enabled=True, grant_second_look=grant)
        result = await rig.cycle(item, settings=settings, model_ok=False)
        assert seen and set(seen) == {()}, seed
        (done,) = result.committed
        out = done.outcome
        actions = [(action.type, action.label) for action in done.item.body.actions]
        starts = [action for action in actions if action[0] in ("start_prepare", "start_run")]
        if is_finding:
            assert (out.kind, out.level, out.work) == ("suggest", levels[0], "none"), seed
            assert starts == [], seed
        else:
            assert out.kind == "suggest" and out.level in ("silent", "list"), seed
            prepares = out.level != "silent" and "prepare" in allowed and grant != "off"
            assert out.work == ("prepare" if prepares else "none"), seed
            held = grant == "allowed" and out.timing != "now"
            asks = prepares and (grant == "ask" or held)
            assert starts == ([("start_prepare", "Take a second look")] if asks else []), seed
        shown[is_finding, out.level, out.work] += 1
    # Each branch was reached: a silent offer, a listed one with no work, one that prepares, and
    # a finding both quiet and louder than a list.
    assert {(False, "silent", "none"), (False, "list", "none"), (False, "list", "prepare")} <= set(
        shown
    )
    assert {level for finding, level, _ in shown if finding} >= {"silent", "list", "badge"}


# ---- project plans ---------------------------------------------------

PLAN_CHAT = "00000000-0000-4000-8000-0000000000d4"
GOAL = Goal("10-projects/garden/garden.md", "Garden", (), touched=NOW - timedelta(days=15))
(STALL,) = project_plans.stalled_projects((GOAL,), SCOPE, NOW, DecisionSettings())
LATE = TaskItem(
    "10-projects/garden/tasks.md", 3, "Sow the beans", NOW.date() - timedelta(days=4), ()
)
(OVERDUE,) = project_plans.overdue_tasks(
    TaskFacts(1, 0, 0, (LATE,), 1, "tasks", False), SCOPE, NOW.date(), DecisionSettings()
)
PLAN_FINDING = project_plans.finding(
    Finding(MISSION, SCOPE, PLAN_CHAT, "stalled_project", "other", "Garden")
)
PLAN_STARTS = {"stalled_project": "Plan next steps", "overdue_tasks": "Check these tasks"}


async def test_a_project_plan_offer_stays_listed_and_a_finding_keeps_the_arbiters_level(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the arbiter answers (a nudge to ask, with work to prepare, here), which never
    sees any of the items' `work_allowed`: each offer suggests at list and prepares, offering its
    note and no start of its own under Allowed; the finding suggests at the arbiter's nudge,
    with no work and Open plan to the plan's chat alone."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []

    def loud(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        return replace(decide(inp), kind="ask", level="nudge", timing="now", work="prepare")

    monkeypatch.setattr(arbiter, "decide", loud)
    result = await rig.cycle(STALL, OVERDUE, PLAN_FINDING, model_ok=False)
    assert seen and set(seen) == {()}
    *offers, found = result.committed
    for done in offers:
        out = done.outcome
        assert (out.kind, out.level, out.work) == ("suggest", "list", "prepare")
        assert [a.type for a in done.item.body.actions] == ["open_note"]
    assert (found.outcome.kind, found.outcome.level, found.outcome.work) == (
        "suggest",
        "nudge",
        "none",
    )
    assert [(a.type, a.label, a.session_id) for a in found.item.body.actions] == [
        ("open_chat", "Open plan", PLAN_CHAT)
    ]
    episode = rig.episode(PLAN_FINDING)
    assert (episode["kind"], episode["level"]) == ("suggest", "nudge")
    assert result.loud == 1


async def test_the_arbiter_never_sees_a_project_plans_work(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as a project-plan
    offer of either type or a finding, a random project-plan grant and random `work_allowed`,
    decided by an arbiter that answers the draw's level and any work at all: the arbiter never
    sees a project plan's `work_allowed`. An offer keeps kind "suggest", at most "list", and
    work "prepare" only while it is not silent, its `work_allowed` names prepare and the grant
    is not Off, offering its own start only for that work, under Ask first or while Allowed
    holds VIKI's own start for a later moment, whatever the prepare and second-look grants; a
    finding keeps "suggest", the arbiter's level and no work, and
    offers no start."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []
    levels: list[str] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        levels.append(out.level)
        return replace(out, work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: Counter[tuple[str, str, str]] = Counter()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        grant = rng.choice(GRANTS)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        item = replace(rng.choice((STALL, OVERDUE, PLAN_FINDING)), work_allowed=allowed)
        draws.append((draw, rng))
        seen.clear()
        levels.clear()
        settings = replace(
            draw.settings,
            enabled=True,
            grant_project_plan=grant,
            grant_second_look=rng.choice(GRANTS),
        )
        result = await rig.cycle(item, settings=settings, model_ok=False)
        assert seen and set(seen) == {()}, seed
        (done,) = result.committed
        out = done.outcome
        actions = [(action.type, action.label) for action in done.item.body.actions]
        starts = [action for action in actions if action[0] in ("start_prepare", "start_run")]
        is_finding = "found" in item.facts
        if is_finding:
            assert (out.kind, out.level, out.work) == ("suggest", levels[0], "none"), seed
            assert starts == [], seed
        else:
            assert out.kind == "suggest" and out.level in ("silent", "list"), seed
            prepares = out.level != "silent" and "prepare" in allowed and grant != "off"
            assert out.work == ("prepare" if prepares else "none"), seed
            held = grant == "allowed" and out.timing != "now"
            asks = prepares and (grant == "ask" or held)
            label = PLAN_STARTS[item.type]
            assert starts == ([("start_prepare", label)] if asks else []), seed
        shown[item.type if not is_finding else "found", out.level, out.work] += 1
    # Each branch was reached: for each type a silent offer, a listed one with no work and one
    # that prepares, and a finding both quiet and louder than a list.
    for kind in PLAN_STARTS:
        assert {(kind, "silent", "none"), (kind, "list", "none"), (kind, "list", "prepare")} <= set(
            shown
        )
    assert {level for name, level, _ in shown if name == "found"} >= {"silent", "list", "badge"}


# ---- message drafts --------------------------------------------------------------

DRAFT_CHAT = "00000000-0000-4000-8000-0000000000d5"
DRAFTS = message_drafts.offers(
    PageFacts(
        0,
        0,
        0,
        "unknown",
        None,
        None,
        None,
        None,
        context=ContextHint(DRAFT_CHAT, 75),
        sources=SourceHint(DRAFT_CHAT, "00000000-0000-4000-8000-0000000000d6", False),
    ),
    SCOPE,
    "",
)
DRAFT_LABELS = {"context_handoff": "Draft a handoff", "source_note": "Draft literature notes"}


async def test_a_message_draft_takes_suggest_the_arbiters_level_and_no_work(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as either message
    draft with random `work_allowed`, under random prepare and run grants, decided by an arbiter
    that answers the draw's level, any kind and any work at all: a draft keeps "suggest", the
    arbiter's level and no work, and offers only its draft into its chat."""
    decide = arbiter.decide
    levels: list[str] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        levels.append(out.level)
        return replace(out, kind=rng.choice(KINDS), work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: set[str] = set()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        item = replace(rng.choice(DRAFTS), work_allowed=allowed)
        draws.append((draw, rng))
        levels.clear()
        settings = replace(
            draw.settings, enabled=True, grant_prepare=rng.choice(GRANTS), grant_run="allowed"
        )
        result = await rig.cycle(item, settings=settings, model_ok=False)
        (done,) = result.committed
        out = done.outcome
        assert (out.kind, out.level, out.work) == ("suggest", levels[0], "none"), seed
        actions = [(a.type, a.label, a.session_id) for a in done.item.body.actions]
        assert actions == [("draft_chat", DRAFT_LABELS[item.type], DRAFT_CHAT)], seed
        shown.add(out.level)
    assert {"silent", "list", "badge", "nudge"} <= shown


# ---- saved checks ----------------------------------------------------------------

CHECK_FOLDER = "00000000-0000-4000-8000-0000000000e1"
(CHECKS,) = saved_checks.candidates(
    CheckLook((CheckFolder(CHECK_FOLDER, "garden-app", "a" * 40, "waiting", ("check-1",)),)),
    SCOPE,
)
CHECK_KINDS = {"waiting": "suggest", "running": "inform"}


async def test_a_check_item_takes_its_phase_kind_and_at_most_list(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as a saved check in a
    random phase with random `work_allowed`, under random check, run and prepare grants, decided
    by an arbiter that answers the draw's level, any kind and any work at all: the arbiter never
    sees a saved check's `work_allowed`. A waiting folder takes "suggest", a running one "inform"
    and an ended one "report", each at most "list", and work "run" only while it waits, is not
    silent, its `work_allowed` names run and the check grant is not Off. It offers its folder in
    Code, then Run checks only for that work under Ask first, and Stop only while it runs."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []
    levels: list[str] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        levels.append(out.level)
        return replace(out, kind=rng.choice(KINDS), work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: set[tuple[str, str, str]] = set()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        phase = rng.choice(CHECK_PHASES)
        grant = rng.choice(GRANTS)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        facts = {**CHECKS.facts, "phase": phase}
        item = replace(CHECKS, facts=facts, work_allowed=allowed)
        draws.append((draw, rng))
        seen.clear()
        levels.clear()
        settings = replace(
            draw.settings,
            enabled=True,
            grant_check=grant,
            grant_run=rng.choice(GRANTS),
            grant_prepare=rng.choice(GRANTS),
        )
        result = await rig.cycle(item, settings=settings, model_ok=False)
        assert seen and set(seen) == {()}, seed
        (done,) = result.committed
        out = done.outcome
        level = "list" if levels[0] not in ("silent", "list") else levels[0]
        runs = phase == "waiting" and level != "silent" and "run" in allowed and grant != "off"
        assert (out.kind, out.level) == (CHECK_KINDS.get(phase, "report"), level), seed
        assert out.work == ("run" if runs else "none"), seed
        actions = [(action.type, action.workspace_id) for action in done.item.body.actions]
        expected = [("open_code", CHECK_FOLDER)]
        if runs and grant == "ask":
            expected.append(("run_checks", CHECK_FOLDER))
        if phase == "running":
            expected.append(("stop_checks", CHECK_FOLDER))
        assert actions == expected, seed
        shown.add((phase, out.level, out.work))
    # Each branch was reached: a waiting folder silent, listed with no work and listed to run,
    # and every other phase both silent and listed.
    assert {("waiting", "silent", "none"), ("waiting", "list", "none")} <= shown
    assert ("waiting", "list", "run") in shown
    for phase in CHECK_PHASES[1:]:
        assert {(phase, "silent", "none"), (phase, "list", "none")} <= shown


# ---- inbox suggestions -----------------------------------------------------------------

(TRIAGE,) = inbox_triage.candidates(
    InboxLook(None, {}, {}),
    (("00-inbox/seed-idea.md", stamp(NOW - timedelta(hours=1))),),
    InboxFacts(count=4, oldest=None, checked=4, undated=0, partial=False),
    SCOPE,
    NOW,
    DecisionSettings(),
)
TRIAGE_KINDS = {"waiting": "suggest", "running": "inform", "ready": "ready"}


async def test_an_inbox_item_takes_its_phase_kind_and_at_most_list(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 seeded draws of `test_arbiter.random_input`, each with the item as an inbox
    suggestion in a random phase with random `work_allowed`, under random inbox, prepare and run
    grants, decided by an arbiter that answers the draw's level, any kind and any work at all: the
    arbiter never sees an inbox suggestion's `work_allowed`. A waiting inbox takes "suggest", a
    running run "inform", waiting suggestions "ready" and any other phase "report", each at most
    "list", and work "prepare" only while it waits, is not silent, its `work_allowed` names
    prepare and the inbox grant is not Off. It offers Triage inbox, then Suggest destinations only
    for that work under Ask first."""
    decide = arbiter.decide
    seen: list[tuple[str, ...]] = []
    levels: list[str] = []
    draws: list[tuple[ArbiterInput, random.Random]] = []

    def recording(inp: ArbiterInput) -> Outcome:
        seen.append(inp.candidate.work_allowed)
        draw, rng = draws[-1]
        out = decide(replace(draw, candidate=inp.candidate))
        levels.append(out.level)
        return replace(out, kind=rng.choice(KINDS), work=rng.choice(("none", "prepare", "run")))

    monkeypatch.setattr(arbiter, "decide", recording)
    in_memory(rig, monkeypatch)
    shown: set[tuple[str, str, str]] = set()
    for seed in range(500):
        rng = random.Random(seed)
        draw = random_input(rng)
        phase = rng.choice(TRIAGE_PHASES)
        grant = rng.choice(GRANTS)
        allowed = tuple(work for work in ("prepare", "run") if rng.random() < 0.6)
        item = replace(TRIAGE, facts={**TRIAGE.facts, "phase": phase}, work_allowed=allowed)
        draws.append((draw, rng))
        seen.clear()
        levels.clear()
        settings = replace(
            draw.settings,
            enabled=True,
            grant_inbox_triage=grant,
            grant_prepare=rng.choice(GRANTS),
            grant_run=rng.choice(GRANTS),
        )
        result = await rig.cycle(item, settings=settings, model_ok=False)
        assert seen and set(seen) == {()}, seed
        (done,) = result.committed
        out = done.outcome
        level = "list" if levels[0] not in ("silent", "list") else levels[0]
        prepares = (
            phase == "waiting" and level != "silent" and "prepare" in allowed and grant != "off"
        )
        assert (out.kind, out.level) == (TRIAGE_KINDS.get(phase, "report"), level), seed
        assert out.work == ("prepare" if prepares else "none"), seed
        actions = [(action.type, action.label) for action in done.item.body.actions]
        expected = [("open_triage", "Triage inbox")]
        if prepares and grant == "ask":
            expected.append(("suggest_inbox", "Suggest destinations"))
        assert actions == expected, seed
        shown.add((phase, out.level, out.work))
    # Each branch was reached: a waiting inbox silent, listed with no work and listed to prepare,
    # and every other phase both silent and listed.
    assert {("waiting", "silent", "none"), ("waiting", "list", "none")} <= shown
    assert ("waiting", "list", "prepare") in shown
    for phase in TRIAGE_PHASES[1:]:
        assert {(phase, "silent", "none"), (phase, "list", "none")} <= shown


# ---- learned numbers and attention ------------------------------------------------


async def test_the_context_numbers_reach_the_terms(rig: Rig) -> None:
    """Learned T, C and weights, a topic's offset too, decide in place of the start values and
    show in the episode's terms; an item titled in another script meets the same confidence
    floors."""
    rig.model.answers = dict(MILD)  # a badge: one request, no reversed question
    reach = {"list": 0.25, "badge": 0.5, "nudge": 0.75, "alert": 0.875}
    costs = {"badge": 0.125, "nudge": 0.5, "alert": 2.0}
    weights = {**START_WEIGHTS, "topic.approvals": 0.75}
    result = await rig.cycle(candidate(), reach=reach, costs=costs, weights=weights)
    (episode,) = rig.rows("decision_episodes")
    terms = json.loads(episode["terms"])
    assert terms == result.committed[0].outcome.terms
    assert {level: terms[f"T.{level}"] for level in reach} == reach
    assert {level: terms[f"C.{level}"] for level in ("list", *costs)} == {"list": 0.0, **costs}
    assert terms["p"] == round(p_of(MILD, weights, "approvals"), 4)
    assert terms["p"] > round(p_of(MILD, START_WEIGHTS, "approvals"), 4)  # the topic's offset

    # A kind confidence of 0.65 clears the nudge floor, 0.6, whatever the title's script.
    unsure = ChoiceAnswer("ask", 0.65, {"ask": 0.65, "inform": 0.35})
    rig.model.answers = {**STRONG, "kind": unsure}
    for at, title in enumerate(("Review the plan", OTHER_SCRIPT), start=1):
        found = await rig.cycle(candidate(f"plan{at}", title=title), now=later(at))
        assert found.committed[0].outcome.level == "nudge", title


async def test_attention_sums_alpha_of_new_showings(rig: Rig) -> None:
    """A cycle's attention is Σα of the levels it newly shows, timed "now": list 0, badge 0.1,
    nudge 1, alert 3. An item shown louder adds its new level's α; one that keeps its level, or
    is held, adds nothing."""
    settings = DecisionSettings(enabled=True, alerts=True, never_send=("10-projects/secret/",))
    secret = candidate("secret", related_ref="10-projects/secret/secret.md")  # never sent: list
    budget = candidate("budget")
    task = candidate(
        "task",
        type="tasks_due",
        rules_evidence={"overdue": 0, "today": 1, "soon": 0},
        deadline=NOW + timedelta(hours=2, minutes=30),
    )
    rig.model.script(MILD)  # budget's answers, a badge; the task's are STRONG, a nudge
    first = await rig.cycle(secret, budget, task, settings=settings)
    assert [rig.item(item).level for item in (secret, budget, task)] == ["list", "badge", "nudge"]
    assert first.attention == pytest.approx(0.0 + 0.1 + 1.0)

    # Due within two hours, the task's nudge becomes an alert, shown louder; the rest stay.
    promoted = await rig.cycle(secret, budget, task, settings=settings, now=later(40))
    assert (rig.item(task).level, promoted.attention) == ("alert", 3.0)

    # Typing holds the task back, while a new badge still shows.
    rig.model.script(MILD)
    notes = candidate("notes")
    typing = replace(HERE, typing=True)
    held = await rig.cycle(
        notes, secret, budget, task, settings=settings, presence=typing, now=later(45)
    )
    assert (rig.item(task).timing, rig.item(notes).level) == ("after_focus", "badge")
    assert held.attention == pytest.approx(0.1)


# ---- the day's usage -------------------------------------------------------------------


async def test_usage_counts_every_request_and_each_one_never_send_held_back_from(
    rig: Rig,
) -> None:
    """Each request made adds a call to the scope's day, with its measured tokens and spend. One
    whose state never-send kept evidence out of also adds one held-back request, however many
    entries it kept: the playbook's questions, the reversed question and a failed request alike,
    and again each time a later cycle asks the item anew. A request that sent every entry adds
    none; a failed request is a call without tokens, a skipped one no call."""
    settings = DecisionSettings(enabled=True, never_send=("10-projects/secret/",))
    secrets = (
        Evidence("10-projects/secret/secret.md", "Kept out.", False, ()),
        Evidence("10-projects/secret/budget.md", "Kept out too.", False, ()),
    )

    def with_secrets(name: str, *more: Evidence) -> Candidate:
        return candidate(name, evidence=(*candidate(name).evidence, *more, *secrets))

    plan, budget = with_secrets("plan"), with_secrets("budget")
    result = await rig.cycle(plan, settings=settings)
    tokens = measured(rig.model.requests)
    assert len(tokens) == 2  # the questions and the reversed one, two entries kept from each
    assert rig.episodes.usage_on(DAY) == Usage(2, 0, sum(tokens), 2)
    spend = sum(map(cost_micro_usd, tokens))
    assert result.spent_micro_usd == rig.episodes.spent_on(DAY) == spend

    rig.model.script(ModelError("refused"))
    failed = await rig.cycle(plan, budget, settings=settings, now=later(1))  # plan: reused
    assert (failed.asked, failed.spent_micro_usd) == (1, 0)
    assert rig.episodes.usage_on(DAY) == Usage(3, 0, sum(tokens), 3)

    # A new note changes the plan's state: both its requests count again.
    note = Evidence("00-inbox/note.md", "A new note.", False, ())
    again = await rig.cycle(with_secrets("plan", note), settings=settings, now=later(2))
    assert again.asked == 2
    rig.model.script(MILD)  # a badge: no reversed question
    sent_all = await rig.cycle(candidate("notes"), settings=settings, now=later(3))
    assert sent_all.asked == 1
    usage = rig.episodes.usage_on(DAY)
    assert (usage.calls, usage.shadow_calls, usage.held_back) == (6, 0, 5)

    spent = rig.episodes.spent_on(DAY)
    rig.gate.skip = {False}
    skipped = await rig.cycle(plan, budget, candidate("other"), settings=settings, now=later(4))
    assert skipped.asked == 0
    assert (rig.episodes.usage_on(DAY), rig.episodes.spent_on(DAY)) == (usage, spent)
    assert rig.episodes.usage_on("2026-10-21") == Usage(0, 0, 0, 0)  # the user's day, not UTC's


# ---- the shadow ------------------------------------------------------------------------

SHADOW_ON = DecisionSettings(enabled=True, shadow="latest")
LEARNED = {**START_WEIGHTS, "topic.approvals": 0.75}  # learned weights: the topic's offset moves p


async def test_a_fresh_answer_is_asked_once_in_the_shadow(rig: Rig) -> None:
    """With the shadow on another version, a fresh answer is asked once more, of that version,
    on the same state and questions. Decided on the live inputs, the learned weights and the
    topic's offset included, its summary rides the episode's answers; it leaves no call record
    and counts in the day as a shadow call. It sends the live requests' state, never-send
    applied, so like each of them it counts one held-back request."""
    settings = replace(SHADOW_ON, never_send=("10-projects/secret/",))
    secret = Evidence("10-projects/secret/secret.md", "Kept out.", False, ())
    shadow_model = FakeModel(STRONG)
    shadow = Gate(shadow_model)
    item = candidate(evidence=(*candidate().evidence, secret))
    result = await rig.cycle(item, settings=settings, shadow_ask=shadow, weights=LEARNED)
    main, _reversed = rig.model.requests
    (asked,) = shadow_model.requests
    assert (asked.model, asked.state, asked.questions) == (
        MODEL_IDS["latest"],
        main.state,
        playbook.questions(),
    )
    assert "Kept out." not in canonical_json(asked.state)
    assert shadow.seen == [(item.item_key, False)]
    record = rig.item(item)
    assert (record.level, result.asked, result.failed) == ("nudge", 3, {})
    episode = rig.episode(item)
    # The same answers on the same inputs, the reversed question's agreement and the learned
    # weights included, agree; p is the learned one, the topic's offset in it.
    p = round(p_of(STRONG, LEARNED, "approvals"), 4)
    assert p != round(p_of(STRONG, START_WEIGHTS, "approvals"), 4)
    assert decode_shadow(episode["answers"]) == ShadowSummary(True, True, "nudge", p, "latest")
    assert decode_answers(episode["answers"]) == (dict(STRONG), True)
    assert record.answers_json is not None and decode_shadow(record.answers_json) is None
    assert len(rig.agenda.calls(SCOPE)) == 2  # the live requests only
    tokens = measured([*rig.model.requests, asked])
    live = tokens[:2]
    assert (episode["input_tokens"], episode["cost_micro_usd"]) == (
        sum(live),
        sum(map(cost_micro_usd, live)),
    )
    assert rig.episodes.usage_on(DAY) == Usage(3, 1, sum(tokens), 3)  # each request held back
    spend = sum(map(cost_micro_usd, tokens))
    assert result.spent_micro_usd == rig.episodes.spent_on(DAY) == spend

    # Reused answers are not fresh: no shadow ask.
    await rig.cycle(item, settings=settings, shadow_ask=shadow, weights=LEARNED, now=later(5))
    assert len(shadow_model.requests) == 1

    # A shadow answering otherwise disagrees: its goal link is too weak for a nudge, its kind
    # another.
    inform = ChoiceAnswer("inform", 0.9, {"inform": 0.9, "ask": 0.1})
    shadow_model.answers = {**MILD, "kind": inform}
    budget = candidate("budget")
    await rig.cycle(
        item, budget, settings=settings, shadow_ask=shadow, weights=LEARNED, now=later(10)
    )
    assert (rig.item(budget).level, len(shadow_model.requests)) == ("nudge", 2)
    p = round(p_of(MILD, LEARNED, "approvals"), 4)
    summary = ShadowSummary(False, False, "badge", p, "latest")
    assert decode_shadow(rig.episode(budget)["answers"]) == summary


async def test_a_fresh_answer_records_its_episode_on_an_unchanged_state_and_level(
    rig: Rig,
) -> None:
    """The rules list an item; on the next cycle the model answers on the same state and
    hysteresis keeps the list. That fresh answer still records a model episode, the shadow
    summary in its answers and this cycle's call pointing at it, so the user's label on the
    item reaches the fit. Answers reused on that state and level record nothing new."""
    item = candidate()
    await rig.cycle(item, settings=SHADOW_ON, model_ok=False)
    listed = rig.item(item)
    assert (listed.level, listed.answers_json) == ("list", None)
    assert rig.episode(item)["policy"] == "rules"

    weak = ScoreAnswer(0.3, 0.9, {0: 0.7, 1: 0.3})  # too little at stake to leave the list
    rig.model.answers = {**STRONG, "importance": weak}
    shadow_model = FakeModel(STRONG)
    shadow = Gate(shadow_model)
    result = await rig.cycle(item, settings=SHADOW_ON, shadow_ask=shadow, now=later(1))
    record = rig.item(item)
    assert (record.level, record.state_hash) == ("list", listed.state_hash)
    (done,) = result.committed
    assert (done.policy, done.new_episode, len(shadow_model.requests)) == ("model", True, 1)
    rules_row, model_row = rig.rows("decision_episodes")
    assert rules_row["episode_id"] == listed.episode_id != record.episode_id
    assert (model_row["episode_id"], model_row["policy"], model_row["level"]) == (
        record.episode_id,
        "model",
        "list",
    )
    assert model_row["state_hash"] == listed.state_hash
    assert decode_answers(model_row["answers"]) == ({**STRONG, "importance": weak}, None)
    # The shadow's strong answers, on the same inputs, would move the item to badge.
    p = round(p_of(STRONG, START_WEIGHTS, "approvals"), 4)
    assert decode_shadow(model_row["answers"]) == ShadowSummary(False, True, "badge", p, "latest")
    (call,) = rig.agenda.calls(SCOPE)
    assert call.episode_id == record.episode_id

    # The user labels the item, which the service does on its current episode: the fit reads it.
    rig.episodes.feedback(
        SCOPE,
        episode_id=record.episode_id,
        value="useful",
        reason=None,
        source="page",
        now=later(2),
    )
    rows = DecisionLoopStats(rig.store).learning_rows(SCOPE, since=None)
    assert [row.episode_id for row in learning.labelled(rows)] == [record.episode_id]

    # Reused on that state and level, the answers record nothing new and ask no shadow.
    again = await rig.cycle(item, settings=SHADOW_ON, shadow_ask=shadow, now=later(3))
    (kept,) = again.committed
    assert (kept.policy, kept.new_episode, kept.item.level, again.asked) == (
        "model",
        False,
        "list",
        0,
    )
    assert kept.item.episode_id == record.episode_id
    assert (len(rig.rows("decision_episodes")), len(shadow_model.requests)) == (2, 1)


@pytest.mark.parametrize(
    "change",
    [
        {"settings": DecisionSettings(enabled=True)},  # off, the default
        {"settings": replace(SHADOW_ON, shadow="pinned")},  # the version in use
        {"settings": replace(SHADOW_ON, model_version="latest")},  # the version in use
        {"shadow_ask": None},  # the engine hands in no shadow ask
        {"model_ok": False},  # the rules decide: no answers
    ],
    ids=["off", "same-pinned", "same-latest", "no-shadow-ask", "rules"],
)
async def test_the_shadow_is_skipped_unless_on_with_another_version(
    rig: Rig, change: dict[str, Any]
) -> None:
    shadow_model = FakeModel(STRONG)
    item = candidate()
    await rig.cycle(item, **{"settings": SHADOW_ON, "shadow_ask": Gate(shadow_model), **change})
    assert shadow_model.requests == []
    answers = rig.episode(item)["answers"]
    assert answers is None or decode_shadow(answers) is None
    assert rig.episodes.usage_on(DAY).shadow_calls == 0


async def misfit(item_key: str, request: ModelRequest, *, reversal: bool) -> ModelReply:
    """A shadow reply that answers one question of the ten."""
    return ModelReply(MODEL_IDS["latest"], {"goal_link": NoulAnswer(0.9)}, 3000, 40)


@pytest.mark.parametrize("failure", ["skipped", "refused", "misfit"])
async def test_a_failed_shadow_neither_sticks_nor_stores(rig: Rig, failure: str) -> None:
    """A skipped, failed or misfitting shadow ask stores no summary and leaves the item off the
    failure memory; a request it made still counts."""
    shadow_model = FakeModel(STRONG)
    gate = Gate(shadow_model)
    if failure == "skipped":
        gate.skip = {False}
    elif failure == "refused":  # a code that keeps a failed live ask's item on the rules
        shadow_model.script(ModelError("refused"))
    item = candidate()
    result = await rig.cycle(
        item, settings=SHADOW_ON, shadow_ask=misfit if failure == "misfit" else gate
    )
    assert (rig.item(item).level, result.failed) == ("nudge", {})
    episode = rig.episode(item)
    assert decode_answers(episode["answers"]) == (dict(STRONG), True)
    assert decode_shadow(episode["answers"]) is None
    assert len(rig.agenda.calls(SCOPE)) == 2  # the live requests only
    made = failure != "skipped"
    shadow_tokens = 3000 if failure == "misfit" else 0
    live = sum(measured(rig.model.requests))
    assert rig.episodes.usage_on(DAY) == Usage(2 + made, int(made), live + shadow_tokens, 0)
    assert result.asked == 2 + made
    assert result.spent_micro_usd == rig.episodes.spent_on(DAY)


async def test_a_shadow_summary_that_would_not_fit_is_left_out(rig: Rig) -> None:
    """The summary rides the episode's answers only while the whole text fits `ANSWERS_CHARS`."""

    def padded(pad: int) -> dict[str, Answer]:
        kind = ChoiceAnswer("ask", 0.9, {"ask": 0.9, "inform": 0.1, "x" * pad: 0.0})
        return {**STRONG, "kind": kind}

    full = padded(ANSWERS_CHARS - len(encode_answers(padded(0), reversal=True)))
    text = encode_answers(full, reversal=True)
    assert len(text) == ANSWERS_CHARS  # the answers and the reversal fill the bound
    rig.model.answers = full
    shadow_model = FakeModel(STRONG)
    item = candidate()
    await rig.cycle(item, settings=SHADOW_ON, shadow_ask=Gate(shadow_model))
    assert (rig.item(item).level, len(shadow_model.requests)) == ("nudge", 1)
    assert rig.episode(item)["answers"] == text  # kept, without the summary


# ---- the wording in use and a proposed wording's trial --------------------------


def worded(question: str) -> dict[str, object]:
    """A wording of goal_link that `playbook.check_question` accepts."""
    return {
        "type": "noul",
        "instructions": {"question": question, "focus": playbook.OUTSIDE_SENTENCE},
        "criteria": {"true": "Moves a listed goal", "false": "Touches no listed goal"},
    }


IN_USE = {"goal_link": worded("Does `candidate` serve one of `owner.goals`?")}
PROPOSED = worded("Does `candidate` move one of `owner.goals` forward or hold it back?")
ON_TRIAL = Trial("goal_link", PROPOSED)
TRIED = {"goal_link": NoulAnswer(0.4)}  # the trial model's answer to the proposed wording
# What a trial on STRONG answers stores: the live wording's yes and the proposed one's.
STRONG_TRIED = TrialSummary(live=0.95, trial=0.4)


async def test_wording_and_version_reach_state_and_calls(rig: Rig) -> None:
    """The wording in use replaces the questions it rewords in every request, the shadow's
    too, and its version names the state: a new wording asks each item anew, and the calls and
    the episode name the version they were asked under."""
    item = candidate()
    await rig.cycle(item)
    first = rig.item(item)
    version = playbook.version(IN_USE)
    assert version.startswith(f"{PLAYBOOK}.") and len(version) == 10
    shadow_model = FakeModel(STRONG)
    await rig.cycle(
        item,
        wording=IN_USE,
        playbook_version=version,
        settings=SHADOW_ON,
        shadow_ask=Gate(shadow_model),
        now=later(5),
    )
    main, reversed_kind = rig.model.requests[2:]
    asked = playbook.questions_for("approvals", IN_USE)
    assert main.questions == asked and main.questions["goal_link"] == IN_USE["goal_link"]
    assert main.questions["news"] == playbook.questions()["news"]  # unworded: the playbook's
    assert reversed_kind.questions == REVERSED
    assert [request.questions for request in shadow_model.requests] == [asked]
    record = rig.item(item)
    assert record.state_hash not in (None, first.state_hash)  # asked anew: a new state
    episode = rig.episode(item)
    assert (episode["policy"], episode["policy_version"]) == ("model", f"playbook-{version}")
    calls = [rig.agenda.call(SCOPE, entry.call_id) for entry in rig.agenda.calls(SCOPE)]

    def versions(episode_id: str) -> list[str]:
        return [call.body.playbook_version for call in calls if call.episode_id == episode_id]

    assert (versions(first.episode_id), versions(record.episode_id)) == (
        [PLAYBOOK, PLAYBOOK],
        [version, version],
    )

    # The same wording and version again: the state is the same, its answers are reused.
    await rig.cycle(item, wording=IN_USE, playbook_version=version, now=later(10))
    assert (len(rig.model.requests), rig.item(item).state_hash) == (4, record.state_hash)


async def test_trial_asks_the_proposed_question_once(rig: Rig) -> None:
    """With a wording on trial, a fresh answer asks the proposed question alone once more, of
    the live version and on the same state, through `trial_ask`, after the live requests, the
    reversed one too. Like a shadow ask it leaves no call record and counts in the day as a
    shadow call, held back as the live requests are since it sends their state; the live and
    the proposed wording's yes ride the episode's answers, which the answers themselves ignore.
    Answers reused on a later cycle ask nothing."""
    settings = DecisionSettings(enabled=True, never_send=("10-projects/secret/",))
    secret = Evidence("10-projects/secret/secret.md", "Kept out.", False, ())
    trial_model = FakeModel(TRIED)
    trial_ask = Gate(trial_model)
    item = candidate(evidence=(*candidate().evidence, secret))
    result = await rig.cycle(item, settings=settings, trial=ON_TRIAL, trial_ask=trial_ask)
    main, _reversed = rig.model.requests
    (asked,) = trial_model.requests
    assert (asked.model, asked.state, asked.questions) == (
        MODEL_IDS["pinned"],
        main.state,
        {"goal_link": PROPOSED},
    )
    assert "Kept out." not in canonical_json(asked.state)
    assert trial_ask.seen == [(item.item_key, False)]
    record = rig.item(item)
    assert (record.level, result.asked, result.failed) == ("nudge", 3, {})
    episode = rig.episode(item)
    assert decode_trial(episode["answers"]) == STRONG_TRIED
    assert decode_answers(episode["answers"]) == (dict(STRONG), True)
    assert decode_shadow(episode["answers"]) is None
    assert record.answers_json is not None and decode_trial(record.answers_json) is None
    assert len(rig.agenda.calls(SCOPE)) == 2  # the live requests only
    tokens = measured([*rig.model.requests, asked])
    live = tokens[:2]
    assert (episode["input_tokens"], episode["cost_micro_usd"]) == (
        sum(live),
        sum(map(cost_micro_usd, live)),
    )
    assert rig.episodes.usage_on(DAY) == Usage(3, 1, sum(tokens), 3)  # each request held back
    spend = sum(map(cost_micro_usd, tokens))
    assert result.spent_micro_usd == rig.episodes.spent_on(DAY) == spend

    again = await rig.cycle(
        item, settings=settings, trial=ON_TRIAL, trial_ask=trial_ask, now=later(5)
    )
    assert (len(trial_model.requests), again.asked) == (1, 0)
    assert rig.episodes.usage_on(DAY) == Usage(3, 1, sum(tokens), 3)


async def test_trial_and_shadow_each_ask_once(rig: Rig) -> None:
    """With the shadow on too, a fresh answer asks the shadow version, then the proposed
    wording, each once; the episode's answers carry both, and the day counts both as shadow
    calls."""
    order: list[str] = []
    shadow_model, trial_model = FakeModel(STRONG), FakeModel(TRIED)

    def logged(name: str, model: FakeModel) -> Ask:
        gate = Gate(model)

        async def ask(
            item_key: str, request: ModelRequest, *, reversal: bool
        ) -> ModelReply | ModelError | None:
            order.append(name)
            return await gate(item_key, request, reversal=reversal)

        return ask

    item = candidate()
    result = await rig.cycle(
        item,
        settings=SHADOW_ON,
        shadow_ask=logged("shadow", shadow_model),
        trial=ON_TRIAL,
        trial_ask=logged("trial", trial_model),
    )
    assert order == ["shadow", "trial"]
    assert (len(shadow_model.requests), len(trial_model.requests), result.asked) == (1, 1, 4)
    answers = rig.episode(item)["answers"]
    p = round(p_of(STRONG, START_WEIGHTS, "approvals"), 4)
    assert decode_shadow(answers) == ShadowSummary(True, True, "nudge", p, "latest")
    assert decode_trial(answers) == STRONG_TRIED
    usage = rig.episodes.usage_on(DAY)
    assert (usage.calls, usage.shadow_calls) == (4, 2)


# A wording the check refuses: its focus does not end with the outside-data sentence.
UNCHECKED = {**PROPOSED, "instructions": {"question": "Does it matter?", "focus": "Weigh it."}}


@pytest.mark.parametrize(
    "change",
    [
        {"trial": None},  # nothing on trial
        {"trial_ask": None},  # the engine hands in no trial ask
        {"model_ok": False},  # the rules decide: no answers
        {"trial": Trial("goal_link", UNCHECKED)},
        {"trial": Trial("importance", PROPOSED)},  # its live answer is a score, not yes/no
        {"answers": {**STRONG, "goal_link": ChoiceAnswer("yes", 0.9, {"yes": 0.9})}},
    ],
    ids=["no-trial", "no-trial-ask", "rules", "unchecked", "not-yes-no", "live-not-yes-no"],
)
async def test_trial_needs_a_fresh_yes_no_answer_and_a_checked_wording(
    rig: Rig, change: dict[str, Any]
) -> None:
    trial_model = FakeModel(TRIED)
    if "answers" in change:
        rig.model.answers = change.pop("answers")
    item = candidate()
    await rig.cycle(item, **{"trial": ON_TRIAL, "trial_ask": Gate(trial_model), **change})
    assert trial_model.requests == []
    answers = rig.episode(item)["answers"]
    assert answers is None or decode_trial(answers) is None
    assert rig.episodes.usage_on(DAY).shadow_calls == 0


async def misfit_trial(item_key: str, request: ModelRequest, *, reversal: bool) -> ModelReply:
    """A trial reply that answers another question than the one asked."""
    return ModelReply(MODEL_IDS["pinned"], {"news": NoulAnswer(0.9)}, 3000, 40)


async def unsided_trial(item_key: str, request: ModelRequest, *, reversal: bool) -> ModelReply:
    """A trial reply that answers the asked question, but not yes or no."""
    answer = ChoiceAnswer("yes", 0.9, {"yes": 0.9})
    return ModelReply(MODEL_IDS["pinned"], {"goal_link": answer}, 3000, 40)


@pytest.mark.parametrize("failure", ["skipped", "refused", "misfit", "unsided"])
async def test_trial_failure_neither_sticks_nor_stores(rig: Rig, failure: str) -> None:
    """A skipped, failed or misfitting trial ask stores no trial and leaves the decision and the
    failure memory as they were; a request it made still counts, as a shadow call."""
    trial_model = FakeModel(TRIED)
    gate = Gate(trial_model)
    if failure == "skipped":
        gate.skip = {False}
    elif failure == "refused":  # a code that keeps a failed live ask's item on the rules
        trial_model.script(ModelError("refused"))
    ask = {"misfit": misfit_trial, "unsided": unsided_trial}.get(failure, gate)
    item = candidate()
    result = await rig.cycle(item, trial=ON_TRIAL, trial_ask=ask)
    assert (rig.item(item).level, result.failed) == ("nudge", {})
    episode = rig.episode(item)
    assert decode_answers(episode["answers"]) == (dict(STRONG), True)
    assert decode_trial(episode["answers"]) is None
    assert len(rig.agenda.calls(SCOPE)) == 2  # the live requests only
    made = failure != "skipped"
    trial_tokens = 3000 if failure in ("misfit", "unsided") else 0
    live = sum(measured(rig.model.requests))
    assert rig.episodes.usage_on(DAY) == Usage(2 + made, int(made), live + trial_tokens, 0)
    assert result.asked == 2 + made
    assert result.spent_micro_usd == rig.episodes.spent_on(DAY)


@pytest.mark.parametrize("kept", ["trial", "neither"])
async def test_trial_rides_the_answers_only_while_they_fit(rig: Rig, kept: str) -> None:
    """Past `ANSWERS_CHARS` the shadow summary is left out first, then the trial: the answers
    themselves are always kept."""

    def padded(pad: int) -> dict[str, Answer]:
        kind = ChoiceAnswer("ask", 0.9, {"ask": 0.9, "inform": 0.1, "x" * pad: 0.0})
        return {**STRONG, "kind": kind}

    trial = STRONG_TRIED if kept == "trial" else None
    full = padded(ANSWERS_CHARS - len(encode_answers(padded(0), reversal=True, trial=trial)))
    text = encode_answers(full, reversal=True, trial=trial)
    assert len(text) == ANSWERS_CHARS  # these fill the bound; one member more would not fit
    with pytest.raises(ValueError):
        encode_answers(full, reversal=True, shadow=SHADOW_SUMMARY, trial=STRONG_TRIED)
    rig.model.answers = full
    shadow_model, trial_model = FakeModel(STRONG), FakeModel(TRIED)
    item = candidate()
    await rig.cycle(
        item,
        settings=SHADOW_ON,
        shadow_ask=Gate(shadow_model),
        trial=ON_TRIAL,
        trial_ask=Gate(trial_model),
    )
    assert (len(shadow_model.requests), len(trial_model.requests)) == (1, 1)
    assert (rig.item(item).level, rig.episode(item)["answers"]) == ("nudge", text)


# ---- answers at rest ---------------------------------------------------------------------------


def numbers_only(value: object) -> bool:
    if isinstance(value, dict):
        return all(numbers_only(member) for member in value.values())
    return isinstance(value, int | float) and not isinstance(value, bool)


@pytest.mark.parametrize("reversal", [None, True, False])
def test_stored_answers_round_trip(reversal: bool | None) -> None:
    text = encode_answers(STRONG, reversal=reversal)
    assert decode_answers(text) == (dict(STRONG), reversal)
    assert numbers_only(json.loads(text))


def test_stored_answers_have_one_shape_per_question_type() -> None:
    answers = {
        "news": NoulAnswer(0.25),
        "kind": ChoiceAnswer("ask", 0.75, {"ask": 0.75, "inform": 0.25}),
        "importance": ScoreAnswer(2.0, 0.5, {2: 0.5, 3: 0.5}),
    }
    assert encode_answers(answers, reversal=True) == (
        '{"importance":{"confidence":0.5,"probabilities":{"2":0.5,"3":0.5},"score":2.0},'
        '"kind":{"confidence":0.75,"probabilities":{"ask":0.75,"inform":0.25}},'
        '"news":{"noul":0.25},"reversal":1}'
    )


def test_a_stored_choice_reads_back_as_its_likeliest_option() -> None:
    tie = {
        "kind": ChoiceAnswer("ask", 0.5, {"ask": 0.5, "inform": 0.5}),
        "work": ChoiceAnswer("run", 0.5, {"run": 0.5, "prepare": 0.5}),
        "other": ChoiceAnswer("b", 0.5, {"b": 0.5, "a": 0.5}),
    }
    answers, _ = decode_answers(encode_answers(tie))
    # A tie goes to the option the playbook lists first; options it lacks, in stored order.
    assert answers["kind"] == ChoiceAnswer("inform", 0.5, {"ask": 0.5, "inform": 0.5})
    assert answers["work"] == ChoiceAnswer("prepare", 0.5, {"run": 0.5, "prepare": 0.5})
    assert answers["other"] == ChoiceAnswer("a", 0.5, {"a": 0.5, "b": 0.5})
    unlikely = {"kind": ChoiceAnswer("report", 0.4, {"report": 0.4, "suggest": 0.6})}
    assert decode_answers(encode_answers(unlikely))[0]["kind"] == ChoiceAnswer(
        "suggest", 0.4, {"report": 0.4, "suggest": 0.6}
    )


def test_a_shadow_summary_round_trips_and_answers_ignore_it() -> None:
    text = encode_answers(STRONG, shadow=SHADOW_SUMMARY)
    assert decode_answers(text) == (dict(STRONG), None)  # the summary plays no part in answers
    assert decode_shadow(text) == replace(SHADOW_SUMMARY, p=round(SHADOW_SUMMARY.p, 4))
    assert numbers_only(json.loads(text))

    # It rides alongside "reversal" without disturbing it, and is absent when not given.
    both = encode_answers(STRONG, reversal=True, shadow=SHADOW_SUMMARY)
    assert decode_answers(both) == (dict(STRONG), True)
    assert decode_shadow(both) == replace(SHADOW_SUMMARY, p=round(SHADOW_SUMMARY.p, 4))
    assert decode_shadow(encode_answers(STRONG)) is None


def test_decode_answers_ignores_a_malformed_shadow_member() -> None:
    text = '{"news":{"noul":0.5},"shadow":"garbage"}'
    assert decode_answers(text) == ({"news": NoulAnswer(0.5)}, None)


def test_a_trial_round_trips_and_answers_ignore_it() -> None:
    tried = TrialSummary(live=0.912345, trial=0.123456)
    text = encode_answers(STRONG, trial=tried)
    assert decode_answers(text) == (dict(STRONG), None)  # the trial plays no part in answers
    assert decode_trial(text) == TrialSummary(live=0.9123, trial=0.1235)  # 4 places
    assert decode_shadow(text) is None
    assert numbers_only(json.loads(text))

    # It rides alongside "reversal" and "shadow" without disturbing them, and is absent when
    # not given.
    every = encode_answers(STRONG, reversal=False, shadow=SHADOW_SUMMARY, trial=tried)
    assert decode_answers(every) == (dict(STRONG), False)
    assert decode_shadow(every) == replace(SHADOW_SUMMARY, p=round(SHADOW_SUMMARY.p, 4))
    assert decode_trial(every) == TrialSummary(live=0.9123, trial=0.1235)
    assert decode_trial(encode_answers(STRONG, shadow=SHADOW_SUMMARY)) is None
    assert decode_answers('{"news":{"noul":0.5},"trial":"garbage"}') == (
        {"news": NoulAnswer(0.5)},
        None,
    )


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"news":{"noul":0.5}}',  # no "trial" member at all
        '{"trial":{"live":0.5}}',  # a key missing
        '{"trial":{"live":0.5,"trial":0.5,"extra":1}}',  # an extra key
        '{"trial":{"live":true,"trial":0.5}}',  # a flag, not a number
        '{"trial":{"live":0.5,"trial":"0.5"}}',  # not a number
        '{"trial":{"live":1.5,"trial":0.5}}',  # past 1
        '{"trial":{"live":0.5,"trial":-0.25}}',  # below 0
        '{"trial":{"live":NaN,"trial":0.5}}',  # not finite
        '{"trial":{"live":0.5,"trial":' + "9" * 400 + "}}",  # too large to become a float
        '{"trial":"not an object"}',  # the member itself is not an object
    ],
)
def test_decode_trial_refuses_anything_not_exactly_that_shape(text: str) -> None:
    assert decode_trial(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"news":{"noul":0.5}}',  # no "shadow" member at all
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":0.5}}',  # a key missing
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":0.5,"setting":0,"extra":1}}',  # an extra key
        '{"shadow":{"agree":2,"kind":0,"level":3,"p":0.5,"setting":0}}',  # not a 0/1 flag
        '{"shadow":{"agree":true,"kind":0,"level":3,"p":0.5,"setting":0}}',  # a bool, not an int
        '{"shadow":{"agree":1,"kind":0,"level":5,"p":0.5,"setting":0}}',  # level out of range
        '{"shadow":{"agree":1,"kind":0,"level":-1,"p":0.5,"setting":0}}',  # level negative
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":0.5,"setting":3}}',  # setting out of range
        '{"shadow":{"agree":1,"kind":0,"level":3.0,"p":0.5,"setting":0}}',  # level not an int
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":1.5,"setting":0}}',  # p past 1
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":"0.5","setting":0}}',  # p not a number
        '{"shadow":{"agree":1,"kind":0,"level":3,"p":NaN,"setting":0}}',  # p not finite
        (  # too large to become a float; OverflowError, not None, until fixed
            '{"shadow":{"agree":1,"kind":0,"level":3,"p":' + "9" * 400 + ',"setting":0}}'
        ),
        '{"shadow":"not an object"}',  # the member itself is not an object
    ],
)
def test_decode_shadow_refuses_anything_not_exactly_that_shape(text: str) -> None:
    assert decode_shadow(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"news":0.5}',
        '{"news":{"noul":true}}',
        '{"news":{"noul":"0.5"}}',
        '{"news":{"noul":NaN}}',
        '{"news":{"noul":1e999}}',
        '{"news":{"noul":' + "9" * 400 + "}}",  # too large to become a float, OverflowError else
        '{"news":{"noul":0.5,"more":1}}',
        '{"reversal":2}',
        '{"reversal":true}',
        '{"importance":{"confidence":1,"probabilities":{"x":1},"score":1}}',
        '{"importance":{"confidence":1,"probabilities":{"01":1},"score":1}}',
        '{"kind":{"confidence":1,"probabilities":{}}}',
        '{"kind":{"confidence":1,"probabilities":[1]}}',
    ],
)
def test_decoding_refuses_what_encoding_never_writes(text: str) -> None:
    with pytest.raises(ValueError):
        decode_answers(text)


@pytest.mark.parametrize(
    "answers",
    [
        {"news": NoulAnswer(math.nan)},
        {"news": NoulAnswer(math.inf)},
        {"news": NoulAnswer(True)},
        {"kind": ChoiceAnswer("ask", 0.5, {})},
        {"importance": ScoreAnswer(1.0, 0.5, {-1: 1.0})},
        {"reversal": NoulAnswer(0.5)},
        {"shadow": NoulAnswer(0.5)},
        {"trial": NoulAnswer(0.5)},
        {f"question_{n}": NoulAnswer(0.123456789) for n in range(100)},  # past the stored bound
    ],
)
def test_encoding_refuses_what_storage_cannot_keep(answers: Mapping[str, Answer]) -> None:
    with pytest.raises(ValueError):
        encode_answers(answers)


def test_cost_is_measured_tokens_at_the_price_rounded_up() -> None:
    assert [cost_micro_usd(n) for n in (0, 1, 1000, 2500, 1_000_000)] == [0, 1, 42, 105, 42_000]


@pytest.mark.parametrize(
    ("created_at", "before"),
    [
        (stamp(NOW - timedelta(milliseconds=1)), True),
        (stamp(NOW), False),
        (stamp(NOW + timedelta(hours=1)), False),
        ("not a stamp", False),
        ("2026-10-21T08:59:59.999+0", False),  # 24 characters, yet no stamp
    ],
)
def test_an_item_stored_before_the_engine_began_reads_its_first_stamp(
    created_at: str, before: bool
) -> None:
    """The bound VIKI's own inbox runs keep (`CycleContext.inbox_since`) reads the stored item's
    first stamp; a stamp that does not read says no, so it fails no commit."""
    item = cast(ItemRecord, SimpleNamespace(created_at=created_at))
    assert _stored_before(item, NOW) is before
    assert _stored_before(None, NOW) is False and _stored_before(item, None) is False
