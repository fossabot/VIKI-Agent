"""project_plans.py: the stalled-project and overdue-tasks offers, never-send, what the model is
sent, and the finding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from viki_agent.automation.brief import Goal, TaskFacts, TaskItem
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import contracts, project_plans, words
from viki_agent.decision_loop.contracts import (
    DecisionSettings,
    Finding,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.state import History, Now, Owner, build_state

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
SCOPE = "scope-x"
SESSION = "fe07f4bf-5447-4a8d-9d58-b68385409b99"
MISSION = "m-7a1b2c3d"
SETTINGS = DecisionSettings()
OLD = NOW - contracts.PROJECT_STALL


def _goal(
    slug: str = "orchid",
    *,
    touched: datetime | None = OLD,
    title: str | None = "Orchid",
    tags: tuple[str, ...] | None = (),
    ref: str | None = None,
) -> Goal:
    return Goal(f"10-projects/{slug}/{slug}.md" if ref is None else ref, title, tags, touched)


def _task(
    text: str = "Order seeds",
    *,
    ago: int = 3,
    slug: str = "orchid",
    tags: tuple[str, ...] | None = (),
    ref: str | None = None,
    line: int = 1,
) -> TaskItem:
    return TaskItem(
        ref or f"10-projects/{slug}/tasks.md", line, text, TODAY - timedelta(days=ago), tags
    )


def _moved(item: TaskItem, ref: str) -> TaskItem:
    return TaskItem(ref, item.line, item.text, item.due, item.tags)


def _tasks(*items: TaskItem) -> TaskFacts:
    return TaskFacts(len(items), 0, 0, items, 1, "k", False)


def _stalled(*goals: Goal, settings: DecisionSettings = SETTINGS) -> list[contracts.Candidate]:
    return project_plans.stalled_projects(goals, SCOPE, NOW, settings)


def _overdue(*items: TaskItem, settings: DecisionSettings = SETTINGS) -> list[contracts.Candidate]:
    return project_plans.overdue_tasks(_tasks(*items), SCOPE, TODAY, settings)


def _never(*entries: str, invalid: bool = False) -> DecisionSettings:
    return DecisionSettings(never_send=entries, never_send_invalid=invalid)


# ---- stalled projects --------------------------------------------------------------------------


def test_a_project_is_stalled_at_14_days_and_not_a_minute_sooner() -> None:
    on_time = _goal("a", touched=NOW - timedelta(days=14))
    almost = _goal("b", touched=NOW - timedelta(days=13, hours=23, minutes=59))
    unknown = _goal("c", touched=None)
    fresh = _goal("d", touched=NOW)
    offers = _stalled(on_time, almost, unknown, fresh)
    assert [o.facts["ref"] for o in offers] == ["10-projects/a/a.md"]


def test_stalled_projects_list_most_recently_touched_first_then_by_ref() -> None:
    older = _goal("older", touched=NOW - timedelta(days=30))
    newer = _goal("newer", touched=NOW - timedelta(days=15))
    tie_b = _goal("b", touched=NOW - timedelta(days=20))
    tie_a = _goal("a", touched=NOW - timedelta(days=20))
    offers = _stalled(older, tie_b, newer, tie_a)
    assert [o.facts["ref"] for o in offers[:3]] == [
        "10-projects/newer/newer.md",
        "10-projects/a/a.md",
        "10-projects/b/b.md",
    ]


def test_at_most_three_stalled_projects_are_offered() -> None:
    goals = [_goal(f"p{i}", touched=NOW - timedelta(days=15 + i)) for i in range(5)]
    offers = _stalled(*goals)
    assert len(offers) == contracts.MAX_PROJECT_PLANS == 3
    assert [o.facts["ref"] for o in offers] == [f"10-projects/p{i}/p{i}.md" for i in range(3)]


def test_a_held_back_project_does_not_take_one_of_the_three_places() -> None:
    goals = [_goal(f"p{i}", touched=NOW - timedelta(days=15 + i)) for i in range(5)]
    offers = _stalled(*goals, settings=_never("10-projects/p0/"))
    assert [o.facts["ref"] for o in offers] == [f"10-projects/p{i}/p{i}.md" for i in (1, 2, 3)]


def test_a_stalled_projects_key_names_its_ref_and_its_touched_instant() -> None:
    goal = _goal()
    (offer,) = _stalled(goal)
    assert offer.item_key == item_key("stalled_project", goal.ref, stamp(OLD))
    (same,) = _stalled(_goal())
    assert same.item_key == offer.item_key
    (later,) = _stalled(_goal(touched=OLD - timedelta(seconds=1)))
    assert later.item_key != offer.item_key
    (other,) = _stalled(_goal("rose"))
    assert other.item_key != offer.item_key


def test_a_stalled_project_offer_carries_its_facts_and_nothing_for_the_model() -> None:
    (offer,) = _stalled(_goal())
    assert offer.type == "stalled_project"
    assert offer.title == words.type_words("stalled_project")
    assert dict(offer.facts) == {
        "workspace_id": SCOPE,
        "ref": "10-projects/orchid/orchid.md",
        "project": "Orchid",
    }
    assert dict(offer.words) == {}
    assert offer.evidence == ()
    assert offer.related_chat is None
    assert offer.related_ref is None
    assert offer.deadline is None
    assert offer.shown_elsewhere == ()
    assert dict(offer.rules_evidence) == {}
    assert offer.origin_session is None
    assert offer.work_allowed == ("prepare",)


@pytest.mark.parametrize("title", [None, ""])
def test_a_project_without_a_title_is_named_by_its_folder(title: str | None) -> None:
    (offer,) = _stalled(_goal("garden", title=title))
    assert offer.facts["project"] == "garden"


@pytest.mark.parametrize(
    "ref",
    ["../orchid/orchid.md", "~/orchid/orchid.md", "10-projects/orchid/orchid.txt", "/a/b.md", ""],
)
def test_a_project_whose_ref_is_not_admitted_gets_no_offer(ref: str) -> None:
    assert _stalled(_goal(ref=ref)) == []


# ---- overdue tasks -----------------------------------------------------------------------------


def test_a_task_is_worth_checking_at_3_days_past_due_and_not_at_2() -> None:
    assert len(_overdue(_task(ago=3))) == 1
    assert len(_overdue(_task(ago=10))) == 1
    assert _overdue(_task(ago=2)) == []
    assert _overdue(_task(ago=0)) == []
    assert _overdue(_task(ago=-5)) == []


def test_a_file_gets_one_offer_whatever_the_number_of_its_overdue_tasks() -> None:
    offers = _overdue(_task("A", ago=9), _task("B", ago=5, line=2), _task("C", ago=1, line=3))
    assert len(offers) == 1


def test_files_are_offered_in_the_order_their_first_item_appears() -> None:
    offers = _overdue(
        _task("A", slug="rose", ago=9), _task("B", slug="orchid", ago=8), _task("C", slug="rose")
    )
    assert [o.facts["ref"] for o in offers] == [
        "10-projects/rose/tasks.md",
        "10-projects/orchid/tasks.md",
    ]


def test_an_overdue_files_key_names_its_ref_and_its_overdue_tasks_in_sorted_order() -> None:
    first, second = _task("Order seeds", ago=9), _task("Water", ago=4, line=2)
    (offer,) = _overdue(first, second)
    expected = item_key(
        "overdue_tasks",
        "10-projects/orchid/tasks.md",
        *sorted([f"{first.due.isoformat()} Order seeds", f"{second.due.isoformat()} Water"]),
    )
    assert offer.item_key == expected
    (reversed_offer,) = _overdue(second, first)
    assert reversed_offer.item_key == offer.item_key
    (extra,) = _overdue(first, second, _task("Prune", ago=5, line=3))
    assert extra.item_key != offer.item_key
    (redated,) = _overdue(first, _task("Water", ago=5, line=2))
    assert redated.item_key != offer.item_key
    (other_file,) = _overdue(*(_moved(t, "10-projects/rose/tasks.md") for t in (first, second)))
    assert other_file.item_key != offer.item_key


def test_a_task_not_yet_overdue_is_left_out_of_the_files_key() -> None:
    late = _task("Order seeds", ago=9)
    (alone,) = _overdue(late)
    (with_soon,) = _overdue(late, _task("Water", ago=1, line=2))
    assert with_soon.item_key == alone.item_key


def test_an_overdue_offer_carries_its_facts_and_nothing_for_the_model() -> None:
    (offer,) = _overdue(_task())
    assert offer.type == "overdue_tasks"
    assert offer.title == words.type_words("overdue_tasks")
    assert dict(offer.facts) == {
        "workspace_id": SCOPE,
        "ref": "10-projects/orchid/tasks.md",
        "project": "orchid",
    }
    assert dict(offer.words) == {}
    assert offer.evidence == ()
    assert offer.related_chat is None
    assert offer.related_ref is None
    assert offer.deadline is None
    assert offer.shown_elsewhere == ()
    assert dict(offer.rules_evidence) == {}
    assert offer.origin_session is None
    assert offer.work_allowed == ("prepare",)


def test_a_tasks_file_whose_ref_is_not_admitted_gets_no_offer_and_leaves_the_others() -> None:
    bad = _task(ref="~/orchid/tasks.md")
    good = _task("Water", slug="rose", line=2)
    offers = _overdue(bad, good)
    assert [o.facts["ref"] for o in offers] == ["10-projects/rose/tasks.md"]


# ---- never-send --------------------------------------------------------------------------------


def test_never_send_keeps_a_project_or_tasks_file_from_a_plan() -> None:
    held_goal = "10-projects/orchid/"
    assert _stalled(_goal(), settings=_never(held_goal)) == []
    assert _overdue(_task(), settings=_never(held_goal)) == []
    # By path segment, not by prefix, and ignoring case.
    assert len(_stalled(_goal(), settings=_never("10-projects/orc/"))) == 1
    assert _stalled(_goal(), settings=_never("10-PROJECTS/Orchid/")) == []
    # A tag on the project, on a task, or written inline in the title or the task's text.
    tag = _never("#private")
    assert _stalled(_goal(tags=("private",)), settings=tag) == []
    assert _stalled(_goal(title="Orchid #private"), settings=tag) == []
    assert _overdue(_task(tags=("#private",)), settings=tag) == []
    assert _overdue(_task("Order seeds #private/seeds"), settings=tag) == []
    # Tags nobody can read are held back under a tag rule, and only under one.
    assert _stalled(_goal(tags=None), settings=tag) == []
    assert _overdue(_task(tags=None), settings=tag) == []
    assert len(_stalled(_goal(tags=None), settings=_never("secret/"))) == 1
    assert len(_overdue(_task(tags=None), settings=_never("secret/"))) == 1
    # A tag the rule does not name leaves the offer.
    assert len(_stalled(_goal(tags=("garden",)), settings=tag)) == 1
    assert len(_overdue(_task(tags=("garden",)), settings=tag)) == 1


def test_an_unusable_never_send_list_lists_no_plan_at_all() -> None:
    assert _stalled(_goal(), settings=_never(invalid=True)) == []
    assert _overdue(_task(), settings=_never(invalid=True)) == []
    assert _stalled(_goal(), settings=_never("notes")) == []  # neither a folder nor a tag
    assert _overdue(_task(), settings=_never("notes")) == []


def test_one_held_back_task_leaves_out_its_whole_file_and_only_that_file() -> None:
    clean = _task("Order seeds", ago=9)
    secret = _task("Call #private", ago=5, line=2)
    other = _task("Water", slug="rose", ago=4)
    offers = _overdue(clean, secret, other, settings=_never("#private"))
    assert [o.facts["ref"] for o in offers] == ["10-projects/rose/tasks.md"]


def test_a_held_back_task_that_is_not_overdue_holds_its_file() -> None:
    soon = _task("Call doctor #private", ago=1, line=2)
    assert _overdue(_task("Order seeds", ago=9), soon, settings=_never("#private")) == []
    assert len(_overdue(_task("Order seeds", ago=9), soon, settings=_never("#other"))) == 1


def test_a_never_send_folder_inside_a_project_holds_its_plans() -> None:
    garden, rose = _goal("garden"), _goal("rose")
    late_garden, late_rose = _task(slug="garden"), _task(slug="rose")
    inside = _never("10-projects/garden/journal/")
    assert [o.facts["ref"] for o in _stalled(garden, rose, settings=inside)] == [rose.ref]
    offers = _overdue(late_garden, late_rose, settings=inside)
    assert [o.facts["ref"] for o in offers] == [late_rose.ref]
    # Another project's folder holds neither, and the project's own folder holds both.
    other = _never("10-projects/rose/journal/")
    assert [o.facts["ref"] for o in _stalled(garden, settings=other)] == [garden.ref]
    assert [o.facts["ref"] for o in _overdue(late_garden, settings=other)] == [late_garden.ref]
    own = _never("10-projects/garden/")
    assert _stalled(garden, settings=own) == []
    assert _overdue(late_garden, settings=own) == []
    # Case is folded as everywhere else in never-send.
    assert _stalled(garden, settings=_never("10-PROJECTS/Garden/Journal/")) == []


# ---- what the model is sent --------------------------------------------------------------------


def test_a_project_plan_sends_only_its_kind_to_the_model() -> None:
    (stalled,) = _stalled(_goal())
    (overdue,) = _overdue(_task())
    found = project_plans.finding(
        Finding(MISSION, SCOPE, SESSION, "stalled_project", "flaw", "Orchid")
    )
    sends = (
        (stalled, "project with no recent change"),
        (overdue, "overdue tasks to check"),
        (found, "project with no recent change"),
    )
    now = Now(None, False, False, NOW, 0)
    for candidate, type_words in sends:
        sent = canonical_json(build_state(candidate, Owner(), now, History(), SETTINGS).state)
        assert type_words in sent
        for private in (
            "orchid",
            "Orchid",
            "10-projects",
            "tasks.md",
            "Order seeds",
            SESSION,
            MISSION,
            SCOPE,
            "flaw",
            "found",
        ):
            assert private not in sent, private


# ---- finding -----------------------------------------------------------------------------------


def _found(look: str = "overdue_tasks", title: str = "Orchid") -> Finding:
    return Finding(MISSION, SCOPE, SESSION, look, "other", title)


def test_a_finding_names_the_plan_the_chat_and_the_project() -> None:
    item = project_plans.finding(_found())
    assert item.type == "overdue_tasks"
    assert item.item_key == item_key("overdue_tasks", MISSION, "found")
    assert item.title == words.type_words("overdue_tasks")
    assert dict(item.facts) == {
        "workspace_id": SCOPE,
        "found": "other",
        "session": SESSION,
        "project": "Orchid",
    }
    assert dict(item.words) == {}
    assert item.evidence == ()
    assert item.related_chat == SESSION
    assert item.related_ref is None
    assert item.deadline is None
    assert item.shown_elsewhere == ()
    assert dict(item.rules_evidence) == {"found": "other"}
    assert item.origin_session is None
    assert item.work_allowed == ()


def test_a_finding_is_keyed_by_its_type_and_mission_and_keeps_a_blank_project_blank() -> None:
    stalled = project_plans.finding(_found("stalled_project", ""))
    assert stalled.item_key == item_key("stalled_project", MISSION, "found")
    assert stalled.title == words.type_words("stalled_project")
    assert stalled.facts["project"] == ""
    assert stalled.item_key != project_plans.finding(_found()).item_key
