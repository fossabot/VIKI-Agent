"""Tests for the worktree cleanup policy: constants, `tier` and every `decide` outcome."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from viki_agent.code import worktree_rules
from viki_agent.code.worktree_rules import (
    ARCHIVE_DAYS,
    AUTO_BRANCH,
    MAX_UNTRACKED_FILE,
    MAX_UNTRACKED_TOTAL,
    MAX_WALK_ENTRIES,
    REASONS,
    SWEEP_INTERVAL_S,
    WALK_TIMEOUT_S,
    Decision,
    Facts,
    decide,
    tier,
)

NOW = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)
AUTO_NAME = "viki/20261101-ab12"
BASE_ACTIVITY = NOW - timedelta(hours=100)  # past both tiers, so "recent" never fires by default


def _facts(
    *,
    readable: bool = True,
    branch: str | None = "main",
    changes: int = 0,
    unmerged: bool = False,
    submodule: bool = False,
    embedded: bool = False,
    worktree_refs: bool = False,
    hidden: bool = False,
    unpushed: int = 0,
    untracked_max: int = 0,
    untracked_total: int = 0,
    activity: datetime | None = BASE_ACTIVITY,
    bytes: int | None = 1024,
) -> Facts:
    return Facts(
        readable=readable,
        branch=branch,
        changes=changes,
        unmerged=unmerged,
        submodule=submodule,
        embedded=embedded,
        worktree_refs=worktree_refs,
        hidden=hidden,
        unpushed=unpushed,
        untracked_max=untracked_max,
        untracked_total=untracked_total,
        activity=activity,
        bytes=bytes,
    )


def _decide(
    *,
    row_branch: str = "main",
    in_area: bool = True,
    trusted: bool = True,
    live: bool = False,
    locked: bool = False,
    present: bool = True,
    facts: Facts | None,
    now: datetime = NOW,
    copy: Literal["bound", "released"] | None = None,
) -> Decision:
    return decide(
        "w1",
        row_branch,
        in_area=in_area,
        trusted=trusted,
        live=live,
        locked=locked,
        present=present,
        facts=facts,
        now=now,
        copy=copy,
    )


# ---- constants, REASONS, tier ----------------------------------------------------------


def test_constants_match_the_plan() -> None:
    assert SWEEP_INTERVAL_S == 21600
    assert ARCHIVE_DAYS == 30
    assert MAX_UNTRACKED_FILE == 50 * 1024 * 1024
    assert MAX_UNTRACKED_TOTAL == 200 * 1024 * 1024
    assert MAX_WALK_ENTRIES == 1_000_000
    assert WALK_TIMEOUT_S == 30


def test_auto_branch_matches_the_whole_name_only() -> None:
    assert AUTO_BRANCH.fullmatch(AUTO_NAME)
    assert not AUTO_BRANCH.fullmatch("main")
    assert not AUTO_BRANCH.fullmatch(AUTO_NAME + "-extra")
    assert not AUTO_BRANCH.fullmatch("prefix-" + AUTO_NAME)


def test_tier_is_24h_for_an_auto_branch_and_72h_otherwise() -> None:
    assert tier(AUTO_NAME) == timedelta(hours=24)
    assert tier("main") == timedelta(hours=72)
    assert tier("feature/x") == timedelta(hours=72)


def test_a_bot_copy_branch_gets_the_named_tier_and_no_tier_of_its_own() -> None:
    assert tier("viki/bot-20261110-ab12cd") == timedelta(hours=72)
    assert not hasattr(worktree_rules, "BOT_BRANCH")


def test_reasons_covers_every_decide_outcome_plus_the_act_only_four() -> None:
    expected = {
        "unknown",
        "untrusted",
        "live",
        "bot_chat",
        "locked",
        "missing",
        "recent",
        "submodule",
        "embedded_repo",
        "worktree_refs",
        "hidden_changes",
        "in_progress",
        "branch_changed",
        "merged",
        "filtered",
        "large_untracked",
        "dirty",
        "unpushed",
        "busy",
        "changed",
        "in_use",
        "git_refused",
    }
    assert set(REASONS) == expected
    assert len(REASONS) == len(expected) == 22  # no duplicates


# ---- the gates ahead of any disk probe --------------------------------------------------


def test_outside_the_area_keeps_unknown() -> None:
    d = _decide(in_area=False, facts=None)
    assert (d.action, d.reason) == ("keep", "unknown")
    assert d.idle_since is None
    assert d.eligible_at is None
    assert d.bytes is None


def test_untrusted_or_unregistered_folder_keeps_untrusted() -> None:
    d = _decide(trusted=False, facts=None)
    assert (d.action, d.reason) == ("keep", "untrusted")


def test_a_live_viki_session_check_or_preview_keeps_live() -> None:
    d = _decide(live=True, facts=None)
    assert (d.action, d.reason) == ("keep", "live")


def test_a_git_lock_keeps_locked() -> None:
    d = _decide(locked=True, facts=None)
    assert (d.action, d.reason) == ("keep", "locked")


def test_a_gone_folder_removes_missing() -> None:
    d = _decide(present=False, facts=None)
    assert (d.action, d.reason) == ("remove", "missing")
    assert d.idle_since is None
    assert d.eligible_at is None
    assert d.bytes is None


def test_facts_none_keeps_unknown() -> None:
    d = _decide(facts=None)
    assert (d.action, d.reason) == ("keep", "unknown")


def test_unreadable_facts_keep_unknown() -> None:
    d = _decide(facts=Facts())  # readable=False by default
    assert (d.action, d.reason) == ("keep", "unknown")


def test_an_incomplete_walk_keeps_unknown_on_an_otherwise_removable_row() -> None:
    d = _decide(facts=_facts(bytes=None))  # clean, merged, past its tier otherwise removes it
    assert (d.action, d.reason) == ("keep", "unknown")


def test_an_incomplete_walk_keeps_unknown_on_an_otherwise_archivable_row() -> None:
    d = _decide(facts=_facts(changes=3, bytes=None))  # dirty otherwise archives it
    assert (d.action, d.reason) == ("keep", "unknown")


# ---- recency, both tiers, at their exact boundary ----------------------------------------


def test_an_auto_branch_idle_23h59m_stays_recent() -> None:
    activity = NOW - timedelta(hours=23, minutes=59)
    d = _decide(row_branch=AUTO_NAME, facts=_facts(branch=AUTO_NAME, activity=activity))
    assert (d.action, d.reason) == ("keep", "recent")
    assert d.idle_since is not None
    assert d.eligible_at is not None
    assert d.bytes == 1024


def test_an_auto_branch_idle_exactly_24h_is_no_longer_recent() -> None:
    activity = NOW - timedelta(hours=24)
    d = _decide(row_branch=AUTO_NAME, facts=_facts(branch=AUTO_NAME, activity=activity))
    assert (d.action, d.reason) == ("remove", "merged")


def test_a_named_branch_idle_71h59m_stays_recent() -> None:
    activity = NOW - timedelta(hours=71, minutes=59)
    d = _decide(facts=_facts(activity=activity))
    assert (d.action, d.reason) == ("keep", "recent")


def test_a_named_branch_idle_exactly_72h_is_no_longer_recent() -> None:
    activity = NOW - timedelta(hours=72)
    d = _decide(facts=_facts(activity=activity))
    assert (d.action, d.reason) == ("remove", "merged")


def test_idle_since_and_eligible_at_are_stamped_like_now_iso() -> None:
    activity = datetime(2026, 11, 1, 3, 4, 5, 6000, tzinfo=UTC)
    d = _decide(facts=_facts(activity=activity))
    assert d.idle_since == "2026-11-01T03:04:05.006Z"
    assert d.eligible_at == "2026-11-04T03:04:05.006Z"  # +72h named tier


# ---- the remaining keep reasons, each isolated from the others --------------------------


def test_a_submodule_signal_keeps_submodule() -> None:
    d = _decide(facts=_facts(submodule=True))
    assert (d.action, d.reason) == ("keep", "submodule")


def test_an_embedded_repo_keeps_embedded_repo() -> None:
    d = _decide(facts=_facts(embedded=True))
    assert (d.action, d.reason) == ("keep", "embedded_repo")


def test_worktree_refs_keep_worktree_refs() -> None:
    d = _decide(facts=_facts(worktree_refs=True))
    assert (d.action, d.reason) == ("keep", "worktree_refs")


def test_assume_unchanged_or_skip_worktree_entries_keep_hidden_changes() -> None:
    d = _decide(facts=_facts(hidden=True))
    assert (d.action, d.reason) == ("keep", "hidden_changes")


def test_unmerged_or_in_progress_keeps_in_progress() -> None:
    d = _decide(facts=_facts(unmerged=True))
    assert (d.action, d.reason) == ("keep", "in_progress")


def test_a_switched_branch_keeps_branch_changed() -> None:
    d = _decide(row_branch="main", facts=_facts(branch="other"))
    assert (d.action, d.reason) == ("keep", "branch_changed")


def test_a_detached_head_keeps_branch_changed() -> None:
    d = _decide(row_branch="main", facts=_facts(branch=None))
    assert (d.action, d.reason) == ("keep", "branch_changed")


# ---- merged, large_untracked (exact caps) and the final archive split -------------------


def test_no_changes_and_nothing_unpushed_removes_merged() -> None:
    d = _decide(facts=_facts())
    assert (d.action, d.reason) == ("remove", "merged")


def test_untracked_file_exactly_at_the_cap_does_not_trigger() -> None:
    d = _decide(facts=_facts(changes=1, untracked_max=MAX_UNTRACKED_FILE))
    assert (d.action, d.reason) == ("archive", "dirty")


def test_untracked_file_one_byte_over_the_cap_keeps_large_untracked() -> None:
    d = _decide(facts=_facts(changes=1, untracked_max=MAX_UNTRACKED_FILE + 1))
    assert (d.action, d.reason) == ("keep", "large_untracked")


def test_untracked_total_exactly_at_the_cap_does_not_trigger() -> None:
    d = _decide(facts=_facts(changes=1, untracked_total=MAX_UNTRACKED_TOTAL))
    assert (d.action, d.reason) == ("archive", "dirty")


def test_untracked_total_one_byte_over_the_cap_keeps_large_untracked() -> None:
    d = _decide(facts=_facts(changes=1, untracked_total=MAX_UNTRACKED_TOTAL + 1))
    assert (d.action, d.reason) == ("keep", "large_untracked")


def test_changes_with_room_under_the_caps_archives_dirty() -> None:
    d = _decide(facts=_facts(changes=3))
    assert (d.action, d.reason) == ("archive", "dirty")


def test_unpushed_commits_with_no_changes_archives_unpushed() -> None:
    d = _decide(facts=_facts(unpushed=2))
    assert (d.action, d.reason) == ("archive", "unpushed")


@pytest.mark.parametrize(
    ("facts", "verdict"),
    [
        (_facts(changes=1), ("keep", "filtered")),
        (_facts(unpushed=1), ("keep", "filtered")),
        (_facts(changes=1, untracked_max=MAX_UNTRACKED_FILE + 1), ("keep", "filtered")),
        (_facts(), ("remove", "merged")),  # a removal stores nothing, so it needs no filter
        (_facts(changes=1, submodule=True), ("keep", "submodule")),  # an earlier check wins
    ],
)
def test_a_tree_an_archive_could_not_hold_without_its_filter_is_kept(
    facts: Facts, verdict: tuple[str, str]
) -> None:
    d = _decide(facts=replace(facts, filtered=True))
    assert (d.action, d.reason) == verdict


# ---- Bot copies: bound kept, released without the idle window ---------------------------


@pytest.mark.parametrize(
    ("present", "facts"),
    [(True, _facts()), (True, None), (False, None)],
    ids=["clean-idle", "unread", "gone"],
)
def test_a_bound_copy_is_kept_for_its_chat_when_nothing_runs_in_it(
    present: bool, facts: Facts | None
) -> None:
    d = _decide(copy="bound", present=present, facts=facts)
    assert (d.action, d.reason) == ("keep", "bot_chat")


def test_a_live_bound_copy_reads_live_and_an_untrusted_one_untrusted() -> None:
    assert _decide(copy="bound", live=True, facts=None).reason == "live"
    assert _decide(copy="bound", trusted=False, facts=None).reason == "untrusted"
    assert _decide(copy="bound", in_area=False, facts=None).reason == "unknown"


@pytest.mark.parametrize(
    ("facts", "verdict"),
    [
        (_facts(), ("remove", "merged")),
        (_facts(changes=2), ("archive", "dirty")),
        (_facts(unpushed=1), ("archive", "unpushed")),
    ],
)
def test_a_released_copy_skips_only_the_idle_window(facts: Facts, verdict: tuple[str, str]) -> None:
    fresh = replace(facts, activity=NOW - timedelta(minutes=1))
    assert (_decide(facts=fresh).action, _decide(facts=fresh).reason) == ("keep", "recent")
    d = _decide(copy="released", facts=fresh)
    assert (d.action, d.reason) == verdict
    assert d.idle_since is not None and d.eligible_at is not None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"live": True, "facts": None}, "live"),
        ({"locked": True, "facts": None}, "locked"),
        ({"facts": None}, "unknown"),
        ({"facts": Facts()}, "unknown"),
        ({"facts": _facts(bytes=None)}, "unknown"),
        ({"facts": _facts(submodule=True)}, "submodule"),
        ({"facts": _facts(hidden=True)}, "hidden_changes"),
        ({"facts": _facts(branch="other")}, "branch_changed"),
        ({"facts": _facts(changes=1, untracked_max=MAX_UNTRACKED_FILE + 1)}, "large_untracked"),
    ],
)
def test_a_released_copy_keeps_every_other_guard(kwargs: dict[str, Any], reason: str) -> None:
    d = _decide(copy="released", **kwargs)
    assert (d.action, d.reason) == ("keep", reason)


def test_a_released_copy_whose_folder_is_gone_removes_missing() -> None:
    d = _decide(copy="released", present=False, facts=None)
    assert (d.action, d.reason) == ("remove", "missing")
