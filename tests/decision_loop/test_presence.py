"""Tests for `PresenceTracker`: merging windows into one `current()`, the typing and away
holds, the window timeout and cap, and `opened_since`'s chat/note log. The injected `FakeClock`
never moves on its own, so every hold and timeout is proven at its exact boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from tests.decision_loop.fakes import FakeClock
from viki_agent.decision_loop.contracts import AWAY_S, PageFacts, Presence
from viki_agent.decision_loop.presence import (
    MAX_OPENED,
    MAX_WINDOWS,
    PRESENCE_HEARTBEAT_S,
    TYPING_WINDOW_HOLD_S,
    WINDOW_TIMEOUT_S,
    PresenceTracker,
)

PAGE = PageFacts(
    native_waiting=1,
    runs_working=0,
    runs_review=0,
    git_state="clean",
    git_dirty=None,
    routine_id=None,
    next_run=None,
    routines_paused=None,
)
PAGE_2 = replace(PAGE, native_waiting=2)


def presence(**changes: Any) -> Presence:
    """A window reporting nothing notable: unfocused, not typing, no chat, no note, launcher
    closed, no page. `away` is left False, as a real client's own report always is."""
    blank = Presence(
        focused=False,
        typing=False,
        away=False,
        chat_id=None,
        chat_title=None,
        note_ref=None,
        launcher_open=False,
        page=None,
    )
    return replace(blank, **changes)


def make_tracker(clock: FakeClock) -> PresenceTracker:
    return PresenceTracker(monotonic=clock.monotonic)


def test_constants() -> None:
    assert (
        WINDOW_TIMEOUT_S,
        MAX_WINDOWS,
        MAX_OPENED,
        PRESENCE_HEARTBEAT_S,
        TYPING_WINDOW_HOLD_S,
    ) == (60, 16, 16, 15, 18)


def test_before_any_report_the_state_is_away_and_otherwise_blank() -> None:
    rig = make_tracker(FakeClock())
    assert rig.current() == Presence(
        focused=False,
        typing=False,
        away=True,
        chat_id=None,
        chat_title=None,
        note_ref=None,
        launcher_open=False,
        page=None,
    )


def test_report_answers_false_for_nothing_notable_then_true_for_a_focus_change() -> None:
    rig = make_tracker(FakeClock())
    assert rig.report("w1", presence()) is False
    assert rig.report("w1", presence(focused=True)) is True
    assert rig.report("w1", presence(focused=True)) is False  # repeats: no change


def test_a_note_change_alone_is_a_change() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(focused=True, note_ref=("ws-1", "a.md")))
    assert rig.report("w1", presence(focused=True, note_ref=("ws-1", "b.md"))) is True


def test_a_launcher_open_change_alone_is_a_change() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence())
    assert rig.report("w1", presence(launcher_open=True)) is True


def test_focused_any_window_and_chat_note_come_from_the_focused_one() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(chat_id="chat-1"))  # unfocused
    rig.report("w2", presence(focused=True, note_ref=("ws-1", "notes/a.md")))
    current = rig.current()
    assert current.focused is True
    assert current.chat_id is None  # the focused window (w2) has no chat open
    assert current.note_ref == ("ws-1", "notes/a.md")


def test_multiple_focused_windows_prefer_the_most_recently_reported() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(focused=True, chat_id="chat-1"))
    rig.report("w2", presence(focused=True, chat_id="chat-2"))
    assert rig.current().chat_id == "chat-2"


def test_else_the_latest_window_when_none_is_focused() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(chat_id="chat-1"))
    rig.report("w2", presence(chat_id="chat-2"))
    current = rig.current()
    assert current.focused is False
    assert current.chat_id == "chat-2"  # w2 reported later than w1


def test_a_silent_windows_typing_stops_counting_after_typing_window_hold_s() -> None:
    """A window that reports `typing=True` once and then falls silent (never reports again, so
    the client's own 3 s hold and 15 s heartbeat both lapse) keeps counting as typing for
    `TYPING_WINDOW_HOLD_S`, not just the report's own `TYPING_HOLD_S`."""
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(typing=True))
    assert rig.current().typing is True
    clock.advance(TYPING_WINDOW_HOLD_S)
    assert rig.current().typing is True  # the boundary itself is still within the hold
    clock.advance(0.001)
    assert rig.current().typing is False


def test_typing_that_stays_on_is_not_a_change() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    assert rig.report("w1", presence(typing=True)) is True  # starts
    clock.advance(1.0)
    assert rig.report("w1", presence(typing=True)) is False  # still within the hold


def test_a_false_report_ends_typing_at_once() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(typing=True))
    assert rig.current().typing is True
    assert rig.report("w1", presence(typing=False)) is True  # ends at once: a change
    assert rig.current().typing is False


def test_typing_on_only_the_clients_heartbeat_reads_as_typing_throughout() -> None:
    """The client posts `typing=True` once when typing starts (debounced) and then only on its
    own 15 s heartbeat while the user keeps typing (frontend usePresence.ts), not on every
    keystroke. Reports landing at 1, 16, 31 and 46 s must read as typing continuously through
    61 s: each report renews the window's hold well before the previous one would have expired
    (18 s > the 15 s gap between reports)."""
    clock = FakeClock()
    rig = make_tracker(clock)
    assert rig.current().typing is False
    elapsed = 0.0
    for report_at in (1, 16, 31, 46):
        clock.advance(report_at - elapsed)
        elapsed = report_at
        rig.report("w1", presence(typing=True))
        assert rig.current().typing is True
    clock.advance(61 - elapsed)
    assert rig.current().typing is True


def test_away_without_a_focused_report_for_away_s() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(focused=True))
    assert rig.current().away is False
    clock.advance(AWAY_S)
    assert rig.current().away is True  # the boundary itself counts as "for AWAY_S"


def test_a_new_focused_report_clears_away_again() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(focused=True))
    clock.advance(AWAY_S)
    assert rig.current().away is True
    rig.report("w1", presence(focused=True))
    assert rig.current().away is False


def test_a_silent_window_is_dropped_after_window_timeout_s() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(launcher_open=True))
    clock.advance(WINDOW_TIMEOUT_S - 0.001)
    assert rig.current().launcher_open is True
    clock.advance(0.001)
    assert rig.current().launcher_open is False


def test_away_survives_a_window_being_dropped() -> None:
    """AWAY_S (300 s) outlives WINDOW_TIMEOUT_S (60 s), so a window that goes stale does not
    make the user "away" by itself: away counts purely from the last focused report."""
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w1", presence(focused=True))
    clock.advance(WINDOW_TIMEOUT_S)  # w1 is now stale and drops
    current = rig.current()
    assert current.focused is False  # no live window is focused any more
    assert current.away is False  # but well inside AWAY_S since the last focused report


def test_windows_are_capped_dropping_the_stalest() -> None:
    clock = FakeClock()
    rig = make_tracker(clock)
    rig.report("w0", presence(focused=True, chat_id="chat-0"))
    for i in range(1, MAX_WINDOWS):
        rig.report(f"w{i}", presence())
    assert rig.current().focused is True  # 16 distinct windows tracked, w0 still among them
    rig.report(f"w{MAX_WINDOWS}", presence())  # the 17th distinct window
    assert rig.current().focused is False  # w0, the stalest, was dropped to make room


def test_page_is_the_latest_reported_value_and_sticky() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(page=PAGE))
    assert rig.current().page == PAGE
    rig.report("w1", presence())  # this report carries no page; the known one stays
    assert rig.current().page == PAGE
    rig.report("w2", presence(page=PAGE_2))
    assert rig.current().page == PAGE_2


def test_a_page_only_change_is_not_a_change() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(page=PAGE))
    assert rig.report("w1", presence(page=PAGE_2)) is False


def test_launcher_open_if_any_window_has_it_open() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence())
    rig.report("w2", presence(launcher_open=True))
    assert rig.current().launcher_open is True


def test_opened_since_lists_new_focused_chats_and_notes_newest_last() -> None:
    rig = make_tracker(FakeClock())
    token, opened = rig.opened_since(0)
    assert opened == []

    rig.report("w1", presence(focused=True, chat_id="chat-a", note_ref=("ws-1", "a.md")))
    token, opened = rig.opened_since(token)
    assert opened == ["chat-a", ("ws-1", "a.md")]  # one report, chat logged before note

    token, opened = rig.opened_since(token)
    assert opened == []  # nothing new since the last check

    rig.report("w1", presence(focused=True, chat_id="chat-b", note_ref=("ws-1", "a.md")))
    _, opened = rig.opened_since(token)
    assert opened == ["chat-b"]  # the note did not change, so only the chat is listed


def test_opened_since_relogs_the_same_chat_after_losing_focus() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(focused=True, chat_id="chat-a"))
    token, _ = rig.opened_since(0)

    rig.report("w1", presence(focused=False, chat_id="chat-a"))  # focus lost
    token, opened = rig.opened_since(token)
    assert opened == []  # losing focus is never logged as an open

    rig.report("w1", presence(focused=True, chat_id="chat-a"))  # focus returns to the same chat
    _, opened = rig.opened_since(token)
    assert opened == ["chat-a"]


def test_opened_since_ignores_an_unfocused_windows_chat() -> None:
    rig = make_tracker(FakeClock())
    rig.report("w1", presence(chat_id="chat-a"))  # never focused
    _, opened = rig.opened_since(0)
    assert opened == []


def test_opened_since_caps_at_max_opened() -> None:
    rig = make_tracker(FakeClock())
    for i in range(MAX_OPENED + 4):
        rig.report("w1", presence(focused=True, chat_id=f"chat-{i}"))
    _, opened = rig.opened_since(0)
    assert opened == [f"chat-{i}" for i in range(4, MAX_OPENED + 4)]
    assert len(opened) == MAX_OPENED
