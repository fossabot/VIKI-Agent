"""inbox_triage.py: the inbox-triage item, its key, its phases, Never send and what the model is
sent."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.decision_loop.fakes import settings_values
from viki_agent.automation.brief import InboxFacts, TaskFacts, VaultFacts
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import contracts, inbox_triage, words
from viki_agent.decision_loop.contracts import (
    DecisionSettings,
    InboxLook,
    InboxRun,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.detectors import Observation, detect
from viki_agent.decision_loop.state import History, Now, Owner, build_state

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SCOPE = "scope-x"
SETTLED = NOW - timedelta(minutes=10)  # the newest capture changed exactly the settle time ago
REFS = (
    "00-inbox/a.md",
    "00-inbox/b/c.md",
    "00-inbox/zebra-crossing.md",
    "00-inbox/e.md",
    "00-inbox/f.md",
)
STAMPS = tuple(stamp(SETTLED - timedelta(hours=n)) for n in range(len(REFS) - 1, -1, -1))
LISTED = tuple(zip(REFS, STAMPS, strict=True))
NEWEST = STAMPS[-1]
# Five captures, the oldest one a day old: the `inbox` item's own rule presses (more than three).
PRESSING = InboxFacts(5, stamp(NOW - timedelta(days=1)), 5, 0, False)
QUIET = InboxFacts(3, stamp(NOW - timedelta(days=1)), 3, 0, False)
NO_LOOK = InboxLook(None, {}, {})
KEY = item_key("inbox_triage", SCOPE, canonical_json([[ref, s] for ref, s in LISTED]))


def _settings(**overrides: Any) -> DecisionSettings:
    return contracts.parse_settings(settings_values(**overrides))


def _candidates(
    look: InboxLook = NO_LOOK,
    listed: tuple[tuple[str, str], ...] = LISTED,
    inbox: InboxFacts = PRESSING,
    now: datetime = NOW,
    settings: DecisionSettings | None = None,
) -> list[contracts.Candidate]:
    return inbox_triage.candidates(
        look, listed, inbox, SCOPE, now, settings if settings is not None else _settings()
    )


def _run(state: str, started_at: str, reason: str | None = None) -> InboxRun:
    return InboxRun("run-1", state, reason, started_at)


def test_a_pressing_inbox_with_an_unanswered_capture_waits_after_ten_minutes() -> None:
    (item,) = _candidates()
    assert item.type == "inbox_triage"
    assert item.item_key == KEY
    assert item.title == words.type_words("inbox_triage")
    assert dict(item.facts) == {"workspace_id": SCOPE, "phase": "waiting"}
    assert dict(item.words) == {}
    assert item.evidence == ()
    assert item.related_chat is None
    assert item.related_ref is None
    assert item.deadline is None
    assert item.shown_elsewhere == ()
    assert dict(item.rules_evidence) == {}
    assert item.origin_session is None
    assert item.work_allowed == ("prepare",)
    # One second short of ten minutes since the newest change: nothing yet.
    assert _candidates(now=NOW - timedelta(seconds=1)) == []
    # An inbox that presses by age alone, with one capture, waits too.
    old = InboxFacts(1, stamp(NOW - timedelta(days=4)), 1, 0, False)
    assert len(_candidates(inbox=old, listed=LISTED[:1])) == 1


def test_answered_quiet_or_empty_inboxes_raise_nothing() -> None:
    answered = InboxLook(None, dict(LISTED), {})
    assert _candidates(answered) == []
    # One capture answered at an older stamp is unanswered again.
    stale = InboxLook(None, {**dict(LISTED), REFS[0]: stamp(SETTLED - timedelta(days=9))}, {})
    assert len(_candidates(stale)) == 1
    assert len(_candidates(InboxLook(None, dict(LISTED[1:]), {}))) == 1
    assert _candidates(inbox=QUIET) == []  # the `inbox` item's own rule does not press
    assert _candidates(listed=()) == []


def test_an_inbox_a_run_already_saw_is_not_run_again() -> None:
    after = stamp(SETTLED + timedelta(seconds=1))
    for run in (_run("done", after), _run("failed", after, "deadline"), _run("failed", NEWEST)):
        assert _candidates(InboxLook(run, {}, {})) == [], run
    # A run that started before the newest change did not see it.
    before = stamp(SETTLED - timedelta(seconds=1))
    assert len(_candidates(InboxLook(_run("done", before), {}, {}))) == 1


def test_a_run_going_raises_no_new_item() -> None:
    going = InboxLook(_run("running", stamp(NOW - timedelta(hours=2))), {}, {})
    assert _candidates(going) == []


def test_one_key_per_listed_state() -> None:
    (item,) = _candidates()
    assert _candidates(inbox=replace(PRESSING, count=9))[0].item_key == item.item_key
    changed = ((REFS[0], stamp(SETTLED - timedelta(days=1))), *LISTED[1:])
    assert _candidates(listed=changed)[0].item_key != item.item_key
    added = (*LISTED, ("00-inbox/g.md", stamp(SETTLED - timedelta(minutes=1))))
    assert _candidates(listed=added)[0].item_key != item.item_key
    assert _candidates(listed=LISTED[:-1])[0].item_key != item.item_key
    other = inbox_triage.candidates(NO_LOOK, LISTED, PRESSING, "other", NOW, _settings())
    assert other[0].item_key != item.item_key


@pytest.mark.parametrize(
    "started, run, phase",
    [
        ("", None, "failed"),  # a start the host refused
        ("", _run("running", NEWEST), "failed"),  # remembered, whatever runs now
        ("run-1", _run("running", NEWEST), "running"),
        ("run-1", _run("done", NEWEST), "ready"),
        ("run-1", _run("failed", NEWEST, "no_suggestions"), "nothing"),
        ("run-1", _run("failed", NEWEST, "deadline"), "failed"),
        ("run-1", _run("failed", NEWEST, None), "failed"),
        ("run-1", None, "failed"),
    ],
)
def test_a_started_key_takes_its_runs_phase(started: str, run: InboxRun | None, phase: str) -> None:
    (item,) = _candidates(InboxLook(run, {}, {KEY: started}))
    assert item.item_key == KEY
    assert item.facts["phase"] == phase
    assert item.work_allowed == ()


def _other_run(state: str, reason: str | None = None) -> InboxRun:
    return InboxRun("run-2", state, reason, stamp(NOW))


@pytest.mark.parametrize(
    "other",
    [
        None,  # another vault's run took the suggester's one latest run
        _other_run("done"),  # another vault's finished run
        _other_run("failed", "deadline"),  # the user's later run in this vault failed
        _other_run("failed", "no_suggestions"),
        _other_run("running"),  # another run goes now
    ],
)
def test_a_started_key_whose_run_is_replaced_reads_the_kept_suggestions(
    other: InboxRun | None,
) -> None:
    kept = {REFS[0]: STAMPS[0]}  # one capture is still answered at its listed stamp
    (item,) = _candidates(InboxLook(other, kept, {KEY: "run-1"}))
    assert item.facts["phase"] == "ready"
    assert item.work_allowed == ()
    # Answered at an older stamp, or not at all: nothing was kept for this state.
    for answered in ({REFS[0]: stamp(SETTLED - timedelta(days=9))}, {}):
        (item,) = _candidates(InboxLook(other, answered, {KEY: "run-1"}))
        assert item.facts["phase"] == "failed", answered


def test_a_started_key_whose_own_run_goes_is_running_even_with_answers_kept() -> None:
    (item,) = _candidates(InboxLook(_run("running", NEWEST), dict(LISTED), {KEY: "run-1"}))
    assert item.facts["phase"] == "running"


def test_a_started_key_stays_even_when_nothing_is_due_any_more() -> None:
    # Answered captures, a quiet inbox and a settled run: the item still reads its phase.
    done = InboxLook(_run("done", NEWEST), dict(LISTED), {KEY: "run-1"})
    assert _candidates(done, inbox=QUIET, now=SETTLED)[0].facts["phase"] == "ready"


def test_while_another_run_goes_an_unstarted_key_raises_nothing() -> None:
    going = InboxLook(_run("running", NEWEST), {}, {"another": "run-1"})
    assert _candidates(going) == []


FOLDER_RULES = ("00-inbox/b/", "00-INBOX/", "00-inbox\\b/")


def _never_send(*entries: str) -> DecisionSettings:
    return replace(_settings(), never_send=entries)


@pytest.mark.parametrize(
    "phase_look", [NO_LOOK, InboxLook(_run("running", NEWEST), {}, {KEY: "r"})]
)
def test_never_send_holds_every_pre_run(phase_look: InboxLook) -> None:
    assert len(_candidates(phase_look, settings=_never_send("30-private/"))) == 1
    # A folder over a listed capture, at any depth or letter case.
    for rule in FOLDER_RULES:
        assert _candidates(phase_look, settings=_never_send(rule)) == [], rule
    # Capture tags are unread, so any tag rule holds the run, whatever the rule says.
    for tag in ("#private", "#", "#health/diet"):
        assert _candidates(phase_look, settings=_never_send(tag)) == [], tag
    assert _candidates(phase_look, settings=_never_send("30-private/", "#private")) == []
    # The prompt also names every project: a rule at or under the projects folder holds the run.
    for rule in ("10-projects/acme/", "10-projects/", "10-PROJECTS/Acme/", "10-projects\\acme/"):
        assert _candidates(phase_look, settings=_never_send(rule)) == [], rule
    assert len(_candidates(phase_look, settings=_never_send("20-areas/"))) == 1
    # A list that could not be read cannot be trusted.
    assert _candidates(phase_look, settings=replace(_settings(), never_send_invalid=True)) == []


def test_an_inbox_triage_item_sends_only_its_kind_to_the_model() -> None:
    now = Now(None, False, False, NOW, 0)
    waiting = InboxLook(None, {}, {})
    looks = {
        "waiting": waiting,
        "running": InboxLook(_run("running", NEWEST), {}, {KEY: "run-1"}),
        "ready": InboxLook(_run("done", NEWEST), {}, {KEY: "run-1"}),
        "nothing": InboxLook(_run("failed", NEWEST, "no_suggestions"), {}, {KEY: "run-1"}),
        "failed": InboxLook(None, {}, {KEY: ""}),
    }
    assert set(looks) == set(contracts.TRIAGE_PHASES)
    for phase, look in looks.items():
        (item,) = _candidates(look)
        assert item.facts["phase"] == phase
        sent = canonical_json(build_state(item, Owner(), now, History(), _settings()).state)
        assert "suggested destinations for inbox captures" in sent, phase
        # A ref holds digits, which `build_state` drops from `words`; its digit-free stem would
        # not be dropped, nor would the phase or the scope.
        private = (
            *REFS,
            *STAMPS,
            "zebra-crossing",
            phase,
            SCOPE,
            item.item_key,
            "workspace_id",
            "run-1",
        )
        for value in private:
            assert value not in sent, (phase, value)


def _observation(**overrides: Any) -> Observation:
    tasks = TaskFacts(0, 0, 0, (), 0, "k", False)
    base: dict[str, Any] = {
        "scope": SCOPE,
        "kind": "vault",
        "vault": VaultFacts(PRESSING, tasks),
        "goals": (),
        "approvals": (),
        "page": None,
        "failures": (),
        "policy": "ordinary",
        "inbox_listed": LISTED,
        "inbox": NO_LOOK,
    }
    base.update(overrides)
    return Observation(**base)


def _detected(obs: Observation, **overrides: Any) -> list[contracts.Candidate]:
    return [c for c in detect(obs, _settings(**overrides), (), NOW) if c.type == "inbox_triage"]


def test_inbox_triage_needs_the_grant_the_inbox_watch_and_a_prepare_policy() -> None:
    obs = _observation()
    for grant in ("ask", "allowed"):
        (item,) = _detected(obs, **{"decision.grant.inbox_triage": grant})
        assert item.work_allowed == ("prepare",), grant
    assert _detected(obs, **{"decision.grant.inbox_triage": "off"}) == []
    assert _detected(obs, **{"decision.watch.inbox": False}) == []
    assert _detected(replace(obs, inbox=None)) == []  # the cycle did not read the host
    assert _detected(replace(obs, vault=None)) == []
    # An Ordinary or a Protected vault may prepare; no other policy lists the item.
    assert len(_detected(replace(obs, policy="protected"))) == 1
    for policy in ("existing_readonly", "folder", "unknown", None):
        assert _detected(replace(obs, policy=policy)) == [], policy
    # The listing is empty when the scan failed: no item.
    assert _detected(replace(obs, inbox_listed=())) == []
