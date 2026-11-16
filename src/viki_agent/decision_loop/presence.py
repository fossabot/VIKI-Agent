"""Merges every browser window's presence report into one picture of where the user is.

Event-loop only: `report` and `current` run synchronously wherever they are called (the
Decision API's presence route, and the engine's cycle), with no lock and no thread, because
every caller already runs on the asyncio loop. Nothing here awaits or sleeps.

A window that goes `WINDOW_TIMEOUT_S` without a new report no longer counts for anything
window-scoped (`focused`, the chosen chat and note, `launcher_open`, `typing`); at most
`MAX_WINDOWS` are kept at once, the stalest dropped first, so a client that opens windows without
closing them cannot grow this past a small, fixed size. A window's own `typing` holds until that
window reports `typing=False`, or until `TYPING_WINDOW_HOLD_S` (one missed client heartbeat,
`PRESENCE_HEARTBEAT_S`, plus the report's own `TYPING_HOLD_S`) after its last `typing=True`
report, or until the window is dropped; merged `typing` is true while any live window is within
its own hold. The wider, per-window hold (rather than one global `TYPING_HOLD_S` hold) matters
because the client only posts `typing=True` once per keystroke burst and then again on its own
heartbeat, not on every keystroke. `away` is not window-scoped: a report's `focused` holds off
`away` for `AWAY_S`, counting from the report itself and surviving every window's drop, since the
hold is unrelated to how long a quiet window takes to be dropped. `page` is a single latest-known
value for the same reason: it describes the app, not a particular window.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from viki_agent.decision_loop.contracts import AWAY_S, TYPING_HOLD_S, PageFacts, Presence

WINDOW_TIMEOUT_S = 60  # a window with no report for this long is dropped
MAX_WINDOWS = 16  # the most windows tracked at once; a new one evicts the stalest over this
MAX_OPENED = 16  # opened_since() never keeps or answers more than this many entries
PRESENCE_HEARTBEAT_S = 15  # the client's presence heartbeat cadence (usePresence.ts HEARTBEAT_MS)
TYPING_WINDOW_HOLD_S = PRESENCE_HEARTBEAT_S + TYPING_HOLD_S  # one missed heartbeat plus the hold

Opened = str | tuple[str, str]  # a chat id, or a note's (workspace_id, ref)


@dataclass
class _Window:
    """One window's last report, plus the bookkeeping `PresenceTracker` needs."""

    seq: int
    last_seen: float
    focused: bool
    chat_id: str | None
    chat_title: str | None
    note_ref: tuple[str, str] | None
    launcher_open: bool
    typing_until: float | None  # None once this window reports typing=False; set fresh on each
    # typing=True report to now + TYPING_WINDOW_HOLD_S, never carried over from an older report


class PresenceTracker:
    """Where the user is, merged from every window's own presence report.

    `monotonic` is read for "now" on every `report` and `current`; tests inject a fake one.
    """

    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._windows: dict[str, _Window] = {}
        self._seq = 0
        self._last_focused_at: float | None = None
        self._page: PageFacts | None = None
        self._focused_chat_id: str | None = None
        self._focused_note_ref: tuple[str, str] | None = None
        self._open_seq = 0
        self._open_log: deque[tuple[int, Opened]] = deque(maxlen=MAX_OPENED)

    def report(self, window: str, presence: Presence) -> bool:
        """Merge one window's report; answer whether `current()` changed in a way the engine
        wakes for: focus, a typing start, the chosen chat or note, launcher open, or away. A
        report that only refreshes an already-held `typing`, repeats everything else, or changes
        only `page`, is not a change."""
        now = self._monotonic()
        self._prune(now)
        before = self._merge(now)
        self._seq += 1
        seq = self._seq
        if presence.focused:
            self._last_focused_at = now
        if presence.page is not None:
            self._page = presence.page
        if window not in self._windows and len(self._windows) >= MAX_WINDOWS:
            stalest, _ = min(self._windows.items(), key=lambda pair: pair[1].seq)
            del self._windows[stalest]
        self._windows[window] = _Window(
            seq=seq,
            last_seen=now,
            focused=presence.focused,
            chat_id=presence.chat_id,
            chat_title=presence.chat_title,
            note_ref=presence.note_ref,
            launcher_open=presence.launcher_open,
            typing_until=now + TYPING_WINDOW_HOLD_S if presence.typing else None,
        )
        self._record_opened()
        after = self._merge(now)
        return (
            before.focused != after.focused
            or before.typing != after.typing
            or before.chat_id != after.chat_id
            or before.note_ref != after.note_ref
            or before.launcher_open != after.launcher_open
            or before.away != after.away
        )

    def current(self) -> Presence:
        """The merged presence: focused if any window is, that focused window's chat and note
        (else the most recently reported window's), typing if any live window is within
        `TYPING_WINDOW_HOLD_S` of its own last `typing=True` report and has not since reported
        `typing=False`, away without a focused report for `AWAY_S` (true before the first
        report), launcher open if any window has it open, and the latest page facts reported."""
        now = self._monotonic()
        self._prune(now)
        return self._merge(now)

    def opened_since(self, token: int) -> tuple[int, list[Opened]]:
        """Chat ids and (workspace_id, ref) notes that became the focused window's chat or note
        after `token`, newest last, at most `MAX_OPENED`; and the token to pass next time. Pass
        0 for every one still kept."""
        opened = [value for seq, value in self._open_log if seq > token]
        return self._open_seq, opened

    # ---- internals -----------------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        stale = [
            wid for wid, win in self._windows.items() if now - win.last_seen >= WINDOW_TIMEOUT_S
        ]
        for wid in stale:
            del self._windows[wid]

    def _focused_windows(self) -> list[_Window]:
        return [win for win in self._windows.values() if win.focused]

    def _merge(self, now: float) -> Presence:
        windows = list(self._windows.values())
        focused_windows = self._focused_windows()
        pool = focused_windows or windows
        chat_id = chat_title = note_ref = None
        if pool:
            source = max(pool, key=lambda win: win.seq)
            chat_id, chat_title, note_ref = source.chat_id, source.chat_title, source.note_ref
        return Presence(
            focused=bool(focused_windows),
            typing=any(win.typing_until is not None and now <= win.typing_until for win in windows),
            away=self._last_focused_at is None or now - self._last_focused_at >= AWAY_S,
            chat_id=chat_id,
            chat_title=chat_title,
            note_ref=note_ref,
            launcher_open=any(win.launcher_open for win in windows),
            page=self._page,
        )

    def _record_opened(self) -> None:
        """Log a new chat or note once it becomes the focused window's, so `opened_since` can
        tell the engine. Losing focus clears the tracked chat and note (not logged), so focus
        returning to the same one later is logged again."""
        focused = self._focused_windows()
        source = max(focused, key=lambda win: win.seq) if focused else None
        new_chat = source.chat_id if source else None
        new_note = source.note_ref if source else None
        if new_chat is not None and new_chat != self._focused_chat_id:
            self._append_opened(new_chat)
        if new_note is not None and new_note != self._focused_note_ref:
            self._append_opened(new_note)
        self._focused_chat_id = new_chat
        self._focused_note_ref = new_note

    def _append_opened(self, value: Opened) -> None:
        self._open_seq += 1
        self._open_log.append((self._open_seq, value))
