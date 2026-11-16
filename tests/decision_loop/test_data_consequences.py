"""Pin deletion confirmations to their effects on stored decision data."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.decision_loop.fakes import WALL
from tests.decision_loop.test_service import (
    DAY,
    HANG_GUARD_S,
    KEY,
    SCOPE,
    STRONG,
    TASKS,
    Rig,
    act,
    data_query,
    decided,
    draft_item,
    filed,
    filed_episode,
    five_marked_wrong,
    item_key,
    kept_call,
    look_row,
    observation,
    put,
    recorded,
    retro_states,
    shown,
    task,
)
from viki_agent.decision_loop.contracts import INBOX_TRIAGE, MESSAGE_DRAFTS, SAVED_CHECKS
from viki_agent.decision_loop.cycle import ShadowSummary, TrialSummary
from viki_agent.decision_loop.stats import DecisionLoopStats


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


async def test_delete_all_decision_history_removes_and_keeps(rig: Rig) -> None:
    await rig.service.save_key(KEY)
    rig.model.answers = dict(STRONG)
    rig.observation = observation(task("Model decision"))
    await rig.engine.recheck()
    recorded(rig, "Decision")
    draft_item(rig, "Open suggestion", candidate="inbox_triage")
    look_row(rig, "Started mission", "nothing", at=WALL)
    muted = put(rig, "Muted topic", level="badge")
    rig.store.feedback(
        SCOPE,
        episode_id=muted.episode_id,
        value="mute",
        reason=None,
        source="page",
        now=WALL,
    )
    rig.values["decision.grant.prepare"] = "allowed"
    settings = dict(rig.values)
    rig.store.add_usage(
        SCOPE,
        DAY,
        calls=1,
        shadow_calls=1,
        tokens=25,
        held_back=1,
        micro_usd=12,
        now=WALL,
    )
    with rig.state._connection() as connection:
        kept_preferences = [
            tuple(row)
            for row in connection.execute(
                "SELECT name,value,updated_at FROM decision_preferences "
                "WHERE scope=? AND name LIKE 'mute:%' ORDER BY name",
                (SCOPE,),
            )
        ]
    chats = rig.rows("decision_missions")
    usage, spend = rig.store.usage_on(DAY), rig.store.spent_on(DAY)
    assert rig.rows("decision_episodes") and rig.rows("decision_calls")
    assert any(row["state"] == "open" for row in rig.rows("decision_items"))
    assert chats and usage.calls > 0 and usage.tokens > 0 and spend > 0
    assert rig.store.preferences(SCOPE, WALL).muted == {"tasks_due"}
    assert kept_preferences and kept_preferences[0][1] == "1"

    await rig.service.delete_all()

    assert rig.rows("decision_episodes") == [] and rig.rows("decision_calls") == []
    assert rig.rows("decision_items") == []  # the open suggestion is removed too
    with rig.state._connection() as connection:
        after_preferences = [
            tuple(row)
            for row in connection.execute(
                "SELECT name,value,updated_at FROM decision_preferences "
                "WHERE scope=? AND name LIKE 'mute:%' ORDER BY name",
                (SCOPE,),
            )
        ]
    assert after_preferences == kept_preferences
    assert rig.values == settings
    assert rig.rows("decision_missions") == chats
    assert (rig.store.usage_on(DAY), rig.store.spent_on(DAY)) == (usage, spend)


async def test_delete_all_brings_a_not_useful_item_back_open(rig: Rig) -> None:
    item = put(rig, "Not useful")
    await rig.service.feedback(shown(item.item_key, "wrong", "bad_timing"))
    await rig.service.delete_all()
    rig.observation = observation(task("Not useful"))
    await rig.engine.recheck()
    restored = rig.agenda.item(SCOPE, item.item_key)
    assert restored is not None and restored.state == "open"


async def test_delete_all_recounts_the_pauses(rig: Rig) -> None:
    draft_item(rig, "Paused draft", "wrong")
    before = rig.service._stats.draft_rows(SCOPE, WALL - timedelta(days=30))
    assert before
    await rig.service.delete_all()
    after = rig.service._stats.draft_rows(SCOPE, WALL - timedelta(days=30))
    assert after == []


async def test_delete_all_frees_todays_counts(rig: Rig) -> None:
    draft_item(rig, "Message", candidate="context_handoff")
    draft_item(rig, "Saved check", candidate="commit_checks")
    draft_item(rig, "Inbox suggestion", candidate="inbox_triage")
    stats: DecisionLoopStats = rig.service._stats
    groups = (MESSAGE_DRAFTS, SAVED_CHECKS, INBOX_TRIAGE)
    before = [stats.drafts_today(SCOPE, DAY, (), types=group)[0] for group in groups]
    assert before == [1, 1, 1]
    await rig.service.delete_all()
    after = [stats.drafts_today(SCOPE, DAY, (), types=group)[0] for group in groups]
    assert after == [0, 0, 0]


async def test_delete_all_moves_todays_looks_into_the_drafts_count(rig: Rig) -> None:
    look_row(rig, "Looked today", "nothing", at=WALL)
    before = (await rig.service.page())["missions"]
    assert before["second_look"]["today"] == 1
    assert before["today"]["prepare"] == 0
    await rig.service.delete_all()
    after = (await rig.service.page())["missions"]
    assert after["second_look"]["today"] == 0
    assert after["today"]["prepare"] == 1


async def test_delete_all_leaves_no_shadow_or_trial_rows(rig: Rig) -> None:
    recorded(
        rig,
        "Compared",
        p=0.7,
        answers=STRONG,
        shadow=ShadowSummary(agree=True, kind_agree=False, level="badge", p=0.8, setting="latest"),
        trial=TrialSummary(0.6, 0.8),
    )
    stats: DecisionLoopStats = rig.service._stats
    assert stats.shadow_rows(SCOPE, since=None)
    assert stats.trial_rows(SCOPE, since=None)
    await rig.service.delete_all()
    assert stats.shadow_rows(SCOPE, since=None) == []
    assert stats.trial_rows(SCOPE, since=None) == []


async def test_delete_all_then_acting_on_a_change_records_no_label(rig: Rig) -> None:
    changes = await filed(rig, TASKS)
    row = changes[TASKS]
    await rig.service.delete_all()
    result = await act(rig, row, "approve")
    assert result["change"]["state"] == "approved"
    assert rig.rows("decision_episodes") == []


async def delete_decisions(rig: Rig, *ids: str) -> dict[str, int]:
    return await rig.service.delete(data_query("decisions", delete=True, ids=list(ids)))


async def delete_suggestions(rig: Rig, *ids: str) -> dict[str, int]:
    return await rig.service.delete(data_query("suggestions", delete=True, ids=list(ids)))


async def delete_calls(rig: Rig, *ids: str) -> dict[str, int]:
    return await rig.service.delete(data_query("calls", delete=True, ids=list(ids)))


async def test_deleting_decisions_resets_what_viki_learned(rig: Rig) -> None:
    removed = recorded(rig, "Removed", level="list", feedback="useful")
    kept = recorded(rig, "Kept", level="list", feedback="useful", at=WALL - timedelta(seconds=1))
    rig.store.set_json(SCOPE, "learned", {"weights": [1]}, WALL)
    stats: DecisionLoopStats = rig.service._stats
    assert rig.store.get_json(SCOPE, "learned")
    assert {row.episode_id for row in stats.learning_rows(SCOPE, since=None)} == {removed, kept}

    assert await delete_decisions(rig, removed) == {"deleted": 1, "kept": 0}

    assert rig.store.get_json(SCOPE, "learned") is None
    assert [row.episode_id for row in stats.learning_rows(SCOPE, since=None)] == [kept]


async def test_deleted_decisions_leave_what_the_model_hears(rig: Rig) -> None:
    removed = recorded(rig, "Removed", level="list", feedback="wrong", p=0.8, answers=STRONG)
    recorded(rig, "Kept", level="list", feedback="wrong", p=0.7, answers=STRONG)
    stats: DecisionLoopStats = rig.service._stats
    before = stats.owner_rows(SCOPE, WALL - timedelta(days=1))
    assert len(before.seen) == len(before.marked) == len(before.feedback) == 2
    removed_key = item_key("tasks_due", TASKS, "Removed")
    assert removed_key in {key for key, _level in before.seen}

    assert await delete_decisions(rig, removed) == {"deleted": 1, "kept": 0}

    after = stats.owner_rows(SCOPE, WALL - timedelta(days=1))
    kept_key = item_key("tasks_due", TASKS, "Kept")
    assert len(after.seen) == len(after.marked) == len(after.feedback) == 1
    assert after.seen == ((kept_key, "list"),)
    assert after.marked == (kept_key,)
    assert after.feedback == ((kept_key, "list", "wrong"),)


async def test_deleted_decisions_leave_the_last_days_showings(rig: Rig) -> None:
    removed = recorded(rig, "Removed", level="list")
    kept = recorded(rig, "Kept", level="list", at=WALL - timedelta(seconds=1))
    assert rig.store.observe(SCOPE, event="shown", episode_ids=[removed, kept], now=WALL) == 2
    rig.store.add_showing(SCOPE, "a" * 64, WALL)
    rig.store.add_showing(SCOPE, "b" * 64, WALL)
    assert rig.store.showings_since(SCOPE, WALL - timedelta(days=1)) == 2
    assert rig.store.exposures_since(SCOPE, WALL - timedelta(days=1))

    assert await delete_decisions(rig, removed) == {"deleted": 1, "kept": 0}

    assert rig.store.showings_since(SCOPE, WALL - timedelta(days=1)) == 2
    exposures = rig.store.exposures_since(SCOPE, WALL - timedelta(days=1))
    assert exposures == {item_key("tasks_due", TASKS, "Kept"): 1}


async def test_deleted_decisions_leave_the_version_and_wording_comparisons(rig: Rig) -> None:
    removed = recorded(
        rig,
        "Removed",
        p=0.7,
        answers=STRONG,
        shadow=ShadowSummary(agree=True, kind_agree=True, level="list", p=0.8, setting="latest"),
        trial=TrialSummary(0.6, 0.8),
    )
    kept = recorded(
        rig,
        "Kept",
        at=WALL - timedelta(seconds=1),
        p=0.6,
        answers=STRONG,
        shadow=ShadowSummary(agree=True, kind_agree=True, level="list", p=0.7, setting="latest"),
        trial=TrialSummary(0.5, 0.7),
    )
    stats: DecisionLoopStats = rig.service._stats
    assert len(stats.shadow_rows(SCOPE, since=None)) == 2
    assert len(stats.trial_rows(SCOPE, since=None)) == 2

    assert await delete_decisions(rig, removed) == {"deleted": 1, "kept": 0}

    assert [row.episode_id for row in stats.shadow_rows(SCOPE, since=None)] == [kept]
    assert [row.episode_id for row in stats.trial_rows(SCOPE, since=None)] == [kept]


async def test_acting_on_a_change_whose_decision_is_gone_records_no_label(rig: Rig) -> None:
    changes = await filed(rig, TASKS)
    row = changes[TASKS]
    episode_id = filed_episode(rig, TASKS)["episode_id"]
    assert await delete_decisions(rig, episode_id) == {"deleted": 1, "kept": 0}
    result = await act(rig, row, "approve")

    assert result["change"]["state"] == "approved"
    assert rig.rows("decision_episodes") == []


async def test_deleted_answers_recount_the_draft_check_and_inbox_pauses(rig: Rig) -> None:
    check = draft_item(rig, "Paused check", "wrong", candidate="context_handoff")
    inbox = draft_item(rig, "Paused inbox", "wrong", candidate="inbox_triage")
    for item in (check, inbox):
        rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)
    stats: DecisionLoopStats = rig.service._stats
    since = WALL - timedelta(days=30)
    checks_before = stats.draft_rows(SCOPE, since, types=MESSAGE_DRAFTS)
    inbox_before = stats.draft_rows(SCOPE, since, types=INBOX_TRIAGE, closed="")
    assert {row.reason for row in checks_before} == {"rejected:other"}
    assert {row.reason for row in inbox_before} == {"rejected:other"}

    assert await delete_decisions(rig, check.episode_id, inbox.episode_id) == {
        "deleted": 2,
        "kept": 0,
    }

    checks_after = stats.draft_rows(SCOPE, since, types=MESSAGE_DRAFTS)
    inbox_after = stats.draft_rows(SCOPE, since, types=INBOX_TRIAGE, closed="")
    assert [row.reason for row in checks_after] == ["nothing"]
    assert [row.reason for row in inbox_after] == [""]


async def test_deleting_decisions_drops_the_beats_self_improvement_candidates(rig: Rig) -> None:
    await five_marked_wrong(rig)
    await rig.engine._beat()
    await rig.engine.recheck()
    assert retro_states(rig) == ["open"]
    ids = [
        row["episode_id"]
        for row in rig.rows("decision_episodes")
        if row["candidate"] != "retro_due"
    ]

    result = await rig.service.delete(data_query("decisions", delete=True, ids=ids))

    assert result == {"deleted": len(ids), "kept": 0}
    assert SCOPE not in rig.engine._improve
    assert rig.engine.wakes[-1] == "change"


async def test_deleted_decisions_leave_the_page_and_the_weekly_check(rig: Rig) -> None:
    removed = recorded(rig, "Removed", level="silent", at=WALL - timedelta(days=8))
    kept = recorded(rig, "Kept", level="silent", at=WALL - timedelta(days=8, seconds=1))
    stats: DecisionLoopStats = rig.service._stats
    before_parts = stats.page_parts(SCOPE, WALL)
    before_audit = stats.audit(SCOPE, WALL)
    assert before_parts["data"]["episodes"] == 2 and len(before_audit) == 2

    assert await delete_decisions(rig, removed) == {"deleted": 1, "kept": 0}

    after_parts = stats.page_parts(SCOPE, WALL)
    after_audit = stats.audit(SCOPE, WALL)
    assert after_parts["data"]["episodes"] == 1
    assert [row["episode_id"] for row in after_parts["recent"]] == [kept]
    assert [row["episode_id"] for row in after_audit] == [kept]


async def test_a_deleted_suggestion_found_again_comes_back_open(rig: Rig) -> None:
    item, kept = await decided(rig)
    rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)
    dismissed = rig.agenda.item(SCOPE, item.item_key)
    assert dismissed is not None
    assert dismissed.state == "dismissed"

    assert await delete_suggestions(rig, item.item_key) == {"deleted": 1, "kept": 0}

    await rig.engine.recheck()
    restored = rig.agenda.item(SCOPE, item.item_key)
    assert restored is not None and restored.state == "open"
    control = rig.agenda.item(SCOPE, kept.item_key)
    assert control is not None
    assert control.state == "open"


async def test_deleted_suggestions_leave_the_titles_the_model_hears(rig: Rig) -> None:
    removed = put(rig, "Removed title")
    kept = put(rig, "Kept title")
    rig.agenda.set_state(SCOPE, removed.item_key, "dismissed", WALL)
    before = rig.engine._owner(rig.observation, SCOPE, WALL - timedelta(days=1))
    assert "Removed title" in str(before)
    assert "Kept title" in str(before)

    assert await delete_suggestions(rig, removed.item_key) == {"deleted": 1, "kept": 0}

    after = rig.engine._owner(rig.observation, SCOPE, WALL - timedelta(days=1))
    assert "Removed title" not in str(after)
    assert "Kept title" in str(after)
    assert rig.agenda.item(SCOPE, kept.item_key) is not None


async def test_deleted_suggestions_recount_the_pauses(rig: Rig) -> None:
    removed = draft_item(rig, "Paused inbox item", "wrong", candidate="inbox_triage")
    keep = draft_item(rig, "Kept inbox item", "wrong", candidate="inbox_triage")
    before = rig.service._stats.draft_rows(SCOPE, WALL - timedelta(days=30), types=INBOX_TRIAGE)
    assert len(before) >= 2
    removed_item = rig.agenda.item(SCOPE, removed.item_key)
    assert removed_item is not None
    rig.agenda.set_state(SCOPE, removed.item_key, "acted", WALL)

    assert await delete_suggestions(rig, removed.item_key) == {"deleted": 1, "kept": 0}

    after = rig.service._stats.draft_rows(SCOPE, WALL - timedelta(days=30), types=INBOX_TRIAGE)
    assert len(after) == len(before) - 1
    assert rig.agenda.item(SCOPE, keep.item_key) is not None


async def test_deleted_suggestions_free_todays_counts(rig: Rig) -> None:
    removed = draft_item(rig, "Today inbox", candidate="inbox_triage")
    kept = draft_item(rig, "Control inbox", candidate="inbox_triage")
    stats: DecisionLoopStats = rig.service._stats
    assert stats.drafts_today(SCOPE, DAY, (), types=INBOX_TRIAGE)[0] == 2
    rig.agenda.set_state(SCOPE, removed.item_key, "dismissed", WALL)

    assert await delete_suggestions(rig, removed.item_key) == {"deleted": 1, "kept": 0}

    assert stats.drafts_today(SCOPE, DAY, (), types=INBOX_TRIAGE)[0] == 1
    assert rig.agenda.item(SCOPE, kept.item_key) is not None


async def test_a_deleted_call_cannot_be_opened(rig: Rig) -> None:
    item = put(rig, "Call target")
    removed = rig.agenda.record_call(SCOPE, kept_call(item.item_key, held_back=0, tokens=10), WALL)
    other = rig.agenda.record_call(
        SCOPE, kept_call(item.item_key, held_back=0, tokens=20), WALL - timedelta(seconds=1)
    )
    assert len((await rig.service.records(data_query("calls")))["rows"]) == 2

    assert await delete_calls(rig, removed) == {"deleted": 1, "kept": 0}
    from viki_agent.decision_loop.contracts import DecisionLoopError

    with pytest.raises(DecisionLoopError) as unknown:
        await rig.service.call(removed)

    assert unknown.value.code == "unknown_item"
    assert (await rig.service.call(other))["call_id"] == other


async def test_the_page_shows_the_newest_call_left(rig: Rig) -> None:
    item = put(rig, "Call target")
    older = rig.agenda.record_call(
        SCOPE, kept_call(item.item_key, held_back=0, tokens=10), WALL - timedelta(seconds=1)
    )
    newer = rig.agenda.record_call(SCOPE, kept_call(item.item_key, held_back=0, tokens=20), WALL)
    assert (await rig.service.page())["privacy"]["last_call"]["call_id"] == newer

    assert await delete_calls(rig, newer) == {"deleted": 1, "kept": 0}

    assert (await rig.service.page())["privacy"]["last_call"]["call_id"] == older


async def test_chats_viki_started_stay(rig: Rig) -> None:
    decision_id = recorded(rig, "Decision", level="list", feedback="useful")
    item = put(rig, "Suggestion")
    rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)
    call_id = rig.agenda.record_call(SCOPE, kept_call(item.item_key, held_back=0, tokens=1), WALL)
    look_row(rig, "Mission", "nothing", at=WALL)
    before = rig.rows("decision_missions")
    assert before

    await delete_decisions(rig, decision_id)
    await delete_suggestions(rig, item.item_key)
    await delete_calls(rig, call_id)

    assert rig.rows("decision_missions") == before


async def test_settings_and_stopped_topics_stay(rig: Rig) -> None:
    decision_id = recorded(rig, "Decision", level="list")
    rig.store.feedback(
        SCOPE,
        episode_id=decision_id,
        value="mute",
        reason=None,
        source="page",
        now=WALL,
    )
    rig.values["decision.enabled"] = False
    before_settings = dict(rig.values)
    before_muted = rig.store.preferences(SCOPE, WALL).muted
    item = put(rig, "Suggestion")
    rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)

    await delete_decisions(rig, decision_id)
    await delete_suggestions(rig, item.item_key)

    assert rig.values == before_settings
    assert rig.store.preferences(SCOPE, WALL).muted == before_muted == {"tasks_due"}


async def test_todays_calls_and_spending_stay(rig: Rig) -> None:
    item = put(rig, "Call target")
    removed = rig.agenda.record_call(SCOPE, kept_call(item.item_key, held_back=0, tokens=10), WALL)
    kept = rig.agenda.record_call(
        SCOPE,
        kept_call(item.item_key, held_back=0, tokens=20),
        WALL - timedelta(seconds=1),
    )
    rig.store.add_usage(
        SCOPE, DAY, calls=2, shadow_calls=0, tokens=30, held_back=0, micro_usd=42, now=WALL
    )
    before = (rig.store.usage_on(DAY), rig.store.spent_on(DAY))
    assert before[0].calls == 2 and before[1] == 42

    assert await delete_calls(rig, removed) == {"deleted": 1, "kept": 0}

    assert (rig.store.usage_on(DAY), rig.store.spent_on(DAY)) == before
    assert rig.agenda.call(SCOPE, kept).call_id == kept


async def test_deleted_suggestions_keep_their_decisions_without_why(rig: Rig) -> None:
    item = put(rig, "Suggestion")
    rig.agenda.set_state(SCOPE, item.item_key, "dismissed", WALL)
    kept = put(rig, "Control")
    before_decisions = rig.rows("decision_episodes")
    assert len(before_decisions) == 2

    assert await delete_suggestions(rig, item.item_key) == {"deleted": 1, "kept": 0}

    assert rig.rows("decision_episodes") == before_decisions
    assert rig.agenda.item(SCOPE, item.item_key) is None
    assert rig.agenda.item(SCOPE, kept.item_key) is not None
