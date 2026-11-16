"""What the transcript reader does when a file, a queue or a clock runs out.

Beside tests/test_catalog.py (the reader's ordinary edges) and the frozen acceptance suite. The
budgets are patched down to KB and hundredths of a second here, so every file is small, no test
sleeps, and every wait that observes work is an Event or a permit timeout of at most 50 ms. The
one unbounded wait is `_blocking_reader`'s resume gate: it holds a parse open until the test's own
`finally` releases it, so it observes nothing and a bound on it would only be a race window.
"""

from __future__ import annotations

import json
import os
import random
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest
from synthetic_transcripts import Written, session_id, write_transcript

from viki_agent import catalog as catalog_module
from viki_agent.catalog import Catalog, HistoryPage, project_dir
from viki_agent.events import Event

VAULT = Path("C:/Vault")
SID = session_id(1)
NEEDLE = "通し番号"  # every generated prompt ends with this and its number


def _prompts(events: list[Event]) -> list[str]:
    return [e.text for e in events if e.TYPE == "user.message"]  # type: ignore[attr-defined]


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    return home, project_dir(home, VAULT)


def _small(folder: Path, sid: str, *, kib: int = 8, seed: int = 7) -> Written:
    """One transcript of about `kib` KiB, in turns of a few KB, under a main entrypoint so its
    row is searchable. Reading it once here is not decoration: the first open of a newly written
    file costs ~200 ms on this machine (the scanner sees it), which would swamp every wait below
    and say nothing about the reader."""
    written = write_transcript(
        folder,
        sid,
        target_bytes=kib * 1024,
        rng=random.Random(seed),
        entrypoint="cli",
        filler=(1, 2),
        started=1_760_000_000 + seed * 86_400,
    )
    written.path.read_bytes()
    return written


def _mtimes(paths: list[Path], newest_first: list[int]) -> None:
    """Give `paths` distinct mtimes; `newest_first` lists their indexes newest first."""
    for rank, index in enumerate(newest_first):
        stamp = 1_800_000_000 - rank * 100
        os.utime(paths[index], (stamp, stamp))


def _ticking(step: float) -> Callable[[], float]:
    """A clock that advances `step` every time it is read: the tests inject one instead of
    waiting for a real deadline."""
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += step
        return now

    return clock


def _stopped() -> Callable[[], float]:
    return lambda: 0.0


# --- single flight: one cold parse per file ------------------------------------------------------


class _WatchedPermit:
    """The parse permit, announcing when a call is about to block on it. Only a caller that lost
    the permit passes a timeout, so `waiting` is set exactly when someone is queued behind a
    parse — the one moment these tests need to observe without polling or sleeping."""

    def __init__(self, inner: threading.BoundedSemaphore, waiting: threading.Event) -> None:
        self._inner = inner
        self._waiting = waiting

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if timeout is not None:
            self._waiting.set()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()


@dataclass
class _Parse:
    paths: list[Path]  # files parsed, in order
    entered: threading.Event  # a cold parse has started and is holding the permit
    waiting: threading.Event  # another call is queued behind it
    resume: threading.Event  # let the blocked parse finish


def _blocking_reader(reader: Catalog, monkeypatch: pytest.MonkeyPatch) -> _Parse:
    """Make the reader's cold parses block until `resume` is set, and announce when one has
    started and when a second call is queued behind it."""
    state = _Parse([], threading.Event(), threading.Event(), threading.Event())
    original = catalog_module._read_entries  # noqa: SLF001 - own module, own test

    def blocking(path: Path) -> list[dict[str, Any]]:
        state.paths.append(path)
        state.entered.set()
        # No bound: every caller releases this in a `finally`, and a bound would instead be the
        # window the follower must start a thread and reach acquire() within — measured at 27 ms
        # worst case under 32 CPU hogs, so a 50 ms bound failed correct single-flight code on a
        # loaded laptop rather than guarding anything.
        state.resume.wait()
        return original(path)

    monkeypatch.setattr(catalog_module, "_read_entries", blocking)
    monkeypatch.setattr(
        reader,
        "_parse_permit",
        _WatchedPermit(reader._parse_permit, state.waiting),  # noqa: SLF001
    )
    return state


def _in_thread(call: Callable[[], HistoryPage], out: list[HistoryPage]) -> threading.Thread:
    thread = threading.Thread(target=lambda: out.append(call()))
    thread.start()
    return thread


def test_concurrent_reads_of_one_file_share_one_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two windows opening the same cold session used to parse it twice, side by side, each
    peaking at ~3x the file's bytes — for no speed, because the GIL serialises the parse anyway.
    The second caller now waits for the permit and finds the first caller's chain."""
    home, folder = _home(tmp_path)
    written = _small(folder, SID)
    reader = Catalog(home, VAULT)
    parse = _blocking_reader(reader, monkeypatch)

    first: list[HistoryPage] = []
    second: list[HistoryPage] = []
    leader = _in_thread(lambda: reader.history(SID), first)
    try:
        assert parse.entered.wait(0.05), "no parse started"
        follower = _in_thread(lambda: reader.history(SID), second)
        assert parse.waiting.wait(0.05), "the second read never queued behind the first"
        assert reader._parse_waiters == 1  # noqa: SLF001 - own counter, own test
    finally:
        parse.resume.set()
    leader.join(0.05)
    follower.join(0.05)

    assert parse.paths == [written.path]  # one parse, not two
    assert first and second
    assert _prompts(first[0].events) == _prompts(second[0].events) != []
    assert (first[0].status, second[0].status) == ("complete", "complete")


def test_reads_of_different_files_serialize_under_the_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One permit is a global bound, not a per-file one: a second file waits its turn rather than
    holding a second 3x peak beside the first. It must still be read, and nothing may deadlock."""
    home, folder = _home(tmp_path)
    first = _small(folder, SID)
    other = _small(folder, session_id(2), seed=8)
    reader = Catalog(home, VAULT)
    parse = _blocking_reader(reader, monkeypatch)

    leader_pages: list[HistoryPage] = []
    follower_pages: list[HistoryPage] = []
    leader = _in_thread(lambda: reader.history(SID), leader_pages)
    try:
        assert parse.entered.wait(0.05), "no parse started"
        follower = _in_thread(lambda: reader.history(other.session_id), follower_pages)
        assert parse.waiting.wait(0.05), "the second file never queued behind the first"
        assert reader._parse_waiters == 1  # noqa: SLF001
        assert parse.paths == [first.path]  # the second file has not been touched yet
    finally:
        parse.resume.set()
    leader.join(0.05)
    follower.join(0.05)

    assert sorted(parse.paths) == sorted([first.path, other.path])
    assert _prompts(leader_pages[0].events) == first.prompts
    assert _prompts(follower_pages[0].events) == other.prompts


def test_history_reports_busy_instead_of_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing hangs: a wait that expires and a queue that is full both answer `busy`, which the
    browser shows with an explicit reload rather than a spinner that never ends."""
    home, folder = _home(tmp_path)
    _small(folder, SID)
    reader = Catalog(home, VAULT)
    parse = _blocking_reader(reader, monkeypatch)
    reader._parse_permit.acquire()  # noqa: SLF001 - stands in for a parse already in flight
    queued: list[HistoryPage] = []
    thread: threading.Thread | None = None
    try:
        monkeypatch.setattr(catalog_module, "PARSE_WAIT_S", 0.01)
        expired = reader.history(SID)
        assert (expired.status, expired.reason) == ("busy", "")
        assert (expired.events, expired.next_before_uuid) == ([], None)
        assert reader._parse_waiters == 0  # noqa: SLF001 - the expired waiter left the queue

        # And a caller that arrives when the queue is full is told so without joining it.
        monkeypatch.setattr(catalog_module, "PARSE_WAIT_S", 5.0)
        monkeypatch.setattr(catalog_module, "PARSE_WAITERS", 1)
        parse.waiting.clear()  # the expired caller above already queued once
        thread = _in_thread(lambda: reader.history(SID), queued)
        assert parse.waiting.wait(0.05), "the first caller never queued"
        refused = reader.history(SID)
        assert (refused.status, refused.events) == ("busy", [])
    finally:
        parse.resume.set()
        reader._parse_permit.release()  # noqa: SLF001
    assert thread is not None
    thread.join(0.05)

    assert queued and queued[0].status == "complete"  # the queued caller was served, not refused


# --- over-budget transcripts: the newest window only ---------------------------------------------


def _window_of(path: Path, tail_bytes: int) -> bytes:
    """What a tail window of `tail_bytes` should hold: whole lines only, computed here from the
    rule itself rather than by calling the code under test."""
    raw = path.read_bytes()
    return raw[len(raw) - tail_bytes - 1 :].split(b"\n", 1)[1]


def test_an_oversized_transcript_is_read_from_its_tail_and_marked_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript over FULL_READ_BYTES is read from its newest window. Every page says so, and
    paging back ends at the window's oldest entry rather than pretending the session starts
    there: that note is the difference between "there is nothing older" and "this read could not
    reach it"."""
    home, folder = _home(tmp_path)
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 200_000)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 160_000)
    written = _small(folder, SID, kib=320)
    reader = Catalog(home, VAULT)

    window = _window_of(written.path, 160_000)
    inside = [p for p in written.prompts if p.encode("utf-8") in window]
    assert catalog_module.PAGE_TURNS < len(inside) < len(written.prompts)  # over budget, >1 page

    pages: list[HistoryPage] = []
    before: str | None = None
    while True:
        page = reader.history(SID, before_uuid=before)
        pages.append(page)
        if page.next_before_uuid is None:
            break
        before = page.next_before_uuid

    assert len(pages) > 1
    assert [(p.status, p.reason) for p in pages] == [("partial", "tail_budget")] * len(pages)
    walked = [text for page in reversed(pages) for text in _prompts(page.events)]
    assert walked == inside
    assert written.prompts[-1] in walked and written.prompts[0] not in walked

    # The empty page too. Paging back past the window hands in a before_uuid the window cannot
    # see, and an empty `complete` page there would tell the browser there is nothing older —
    # the one thing the tail-budget note exists to deny.
    beyond = reader.history(SID, before_uuid="99999999-9999-4999-8999-999999999999")
    assert (beyond.events, beyond.next_before_uuid) == ([], None)
    assert (beyond.status, beyond.reason) == ("partial", "tail_budget")
    # Same for a page whose `until` filters every entry it could see.
    filtered = reader.history(SID, until="2000-01-01T00:00:00.000Z")
    assert (filtered.status, filtered.reason, filtered.events) == ("partial", "tail_budget", [])


def test_a_tail_window_drops_the_line_it_starts_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window begins mid-line, and a torn write can leave a suffix that parses on its own.
    Half an entry is not an entry: the line the window starts inside is dropped whole.

    Both boundaries are here, because one rule covers them and only the pair tells them apart.
    The read starts one byte before the window, so "everything after the first newline" drops a
    partial first line — and, when the boundary happens to fall exactly on a line start, that
    byte is the newline ending the previous line and the window keeps its own first line. A read
    that did not take the early byte would pass the mid-line case and silently lose a whole entry
    in the aligned one."""
    home, folder = _home(tmp_path)
    folder.mkdir(parents=True)

    def entry(uid: str, parent: str | None, text: str) -> dict[str, Any]:
        tail = "-1111-4111-8111-111111111111"
        return {
            "type": "user",
            "uuid": f"{uid}{tail}",
            "parentUuid": None if parent is None else f"{parent}{tail}",
            "isSidechain": False,
            "entrypoint": "cli",
            "timestamp": "2026-09-11T00:00:01.000Z",
            "message": {"role": "user", "content": text},
        }

    ghost = json.dumps(entry("00000002", "00000001", "half-an-entry"))
    lines = [
        json.dumps(entry("00000001", None, "the oldest turn")),
        "torn-prefix" + ghost,  # one physical line whose suffix is a whole JSON object
        json.dumps(entry("00000003", "00000002", "the newest turn")),
    ]
    path = folder / f"{SID}.jsonl"
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    raw = path.read_bytes()

    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 16)
    # The window starts on the ghost object's own "{", mid-line: kept, that suffix parses on its
    # own and becomes this page's older turn, which is exactly what must not happen.
    tail = len(raw) - raw.index(ghost.encode("utf-8")) - 1
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", tail)

    page = Catalog(home, VAULT).history(SID)

    assert _prompts(page.events) == ["the newest turn"]
    assert (page.status, page.reason, page.next_before_uuid) == ("partial", "tail_budget", None)

    # Now the aligned boundary: the window begins exactly at the newest line's first byte, where
    # there is no partial line and nothing may be dropped. A fresh reader, because the chain
    # cache keys on (size, mtime) and this file has not changed.
    newest = lines[2].encode("utf-8")
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", len(raw) - raw.index(newest))
    aligned = Catalog(home, VAULT).history(SID)

    assert _prompts(aligned.events) == ["the newest turn"]  # not [] — its line is whole
    assert (aligned.status, aligned.reason) == ("partial", "tail_budget")


def test_an_oversized_transcript_still_lists_from_its_head_and_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row needs the first prompt and the entrypoint (the head) and the title, version and
    newest timestamp (the tail). What it cannot have is the middle: a custom title written there
    is missed, which is a known limit of the reader, not a bug this test protects."""
    home, folder = _home(tmp_path)
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 16_000)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 4_000)
    monkeypatch.setattr(catalog_module, "HEAD_WINDOW_BYTES", 4_000)
    written = _small(folder, SID, kib=64)
    lines = written.path.read_text(encoding="utf-8").splitlines()
    renamed = json.dumps(
        {"type": "custom-title", "customTitle": "Renamed in the middle", "sessionId": SID},
        ensure_ascii=False,
    )
    lines.insert(len(lines) // 2, renamed)  # in the middle, where neither window reaches
    written.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = Catalog(home, VAULT).list_sessions()

    assert (result.state, result.omitted) == ("ready", 0)
    (row,) = result.sessions
    assert row.first_prompt == written.prompts[0]  # the head
    assert (row.title, row.cli_version) == (written.title, "2.1.266")  # the tail
    assert (row.entrypoint, row.other) == ("cli", False)
    assert row.modified.startswith("2025-")  # a timestamp from the tail, not the mtime fallback


# --- typed outcomes for one transcript -----------------------------------------------------------


def test_missing_and_unreadable_transcripts_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript that is not there and one that will not open are different answers: the first
    is a session the CLI has not written, the second a failure the user should see."""
    home, folder = _home(tmp_path)
    _small(folder, SID)
    reader = Catalog(home, VAULT)

    absent = reader.history(session_id(9))
    assert (absent.status, absent.reason, absent.events) == ("missing", "", [])
    assert absent.next_before_uuid is None
    assert reader.history("not-a-uuid").status == "missing"

    stat = Path.stat

    def denied_stat(self: Path, **kwargs: Any) -> os.stat_result:
        if self.name == f"{SID}.jsonl":
            raise PermissionError(5, "denied")
        return stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    locked = reader.history(SID)
    assert (locked.status, locked.reason) == ("unreadable", "read_failed")
    assert (locked.events, locked.next_before_uuid) == ([], None)
    monkeypatch.undo()

    def denied_read(path: Path, start: int, length: int) -> bytes:
        raise PermissionError(5, "denied")

    monkeypatch.setattr(catalog_module, "_window", denied_read)
    unread = Catalog(home, VAULT).history(SID)  # stats fine, will not open
    assert (unread.status, unread.reason) == ("unreadable", "read_failed")
    monkeypatch.undo()

    # The narrow race the CLI can win: the file is there when history() stats it and gone by the
    # time the read reaches it. That is still an absence — "could not load this conversation"
    # would send the user looking for a fault in an app that did nothing wrong.
    def vanished_read(path: Path, start: int, length: int) -> bytes:
        raise FileNotFoundError(2, "gone")

    monkeypatch.setattr(catalog_module, "_window", vanished_read)
    raced = Catalog(home, VAULT).history(SID)
    assert (raced.status, raced.reason) == ("missing", "")
    assert (raced.events, raced.next_before_uuid) == ([], None)


# --- the listing as an observation ----------------------------------------------------------------


def test_a_slow_listing_stops_at_its_deadline_and_finishes_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that cannot read everything in time says how many it missed and returns the
    newest it did read — an unknown count is never shown as zero — and the next listing finishes
    the job, because the first one's rows are cached."""
    home, folder = _home(tmp_path)
    paths = [_small(folder, session_id(i), seed=i, kib=2).path for i in range(5)]
    _mtimes(paths, newest_first=[3, 1, 4, 0, 2])
    monkeypatch.setattr(catalog_module, "LIST_DEADLINE_S", 0.05)
    reader = Catalog(home, VAULT)

    cut = reader.list_sessions(clock=_ticking(0.02))

    assert (cut.state, cut.reason) == ("partial", "time_budget")
    assert cut.omitted == 3 and len(cut.sessions) == 2
    # Newest mtime first, so a listing cut short holds the sessions the user is most likely after.
    assert {row.session_id for row in cut.sessions} == {session_id(3), session_id(1)}

    full = reader.list_sessions(clock=_stopped())

    assert (full.state, full.reason, full.omitted) == ("ready", "", 0)
    assert len(full.sessions) == 5


def test_listing_counts_unreadable_files_and_ignores_pruned_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI prunes transcripts while this runs, so a file that vanishes mid-listing is an
    ordinary absence. A file that is there and will not open is not: it is a row the user should
    have and does not, and the count says so."""
    home, folder = _home(tmp_path)
    for i in range(3):
        _small(folder, session_id(i), seed=i, kib=2)
    locked, pruned = f"{session_id(0)}.jsonl", f"{session_id(1)}.jsonl"
    opener = Path.open

    def flaky(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == locked:
            raise PermissionError(5, "denied")
        if self.name == pruned:
            raise FileNotFoundError(2, "gone")
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)

    result = Catalog(home, VAULT).list_sessions()

    assert (result.state, result.reason, result.omitted) == ("partial", "read_failed", 1)
    assert [row.session_id for row in result.sessions] == [session_id(2)]


def test_listing_distinguishes_no_transcripts_from_an_unreadable_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No project folder is a known zero — the vault has never been opened in claude — and the
    list can say "no sessions yet". A folder that will not list is not a zero at all."""
    home, _ = _home(tmp_path)

    empty = Catalog(home, VAULT).list_sessions()
    assert (empty.state, empty.reason, empty.sessions, empty.omitted) == ("ready", "", [], 0)

    def denied(self: Path) -> Iterator[Path]:
        raise PermissionError(5, "denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    blocked = Catalog(home, VAULT).list_sessions()
    assert (blocked.state, blocked.reason, blocked.sessions) == ("error", "project_unreadable", [])


def test_row_cache_is_bounded_and_drops_pruned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row cache lived for the life of the process and never dropped anything, so a project
    the user cleaned out kept paying rent. It is now an LRU of ROW_CACHE rows, and a listing
    drops the rows of files it did not see."""
    home, folder = _home(tmp_path)
    monkeypatch.setattr(catalog_module, "ROW_CACHE", 3)
    paths = [_small(folder, session_id(i), seed=i, kib=2).path for i in range(5)]
    reader = Catalog(home, VAULT)

    assert len(reader.list_sessions().sessions) == 5  # every row travels; only the cache is cut
    assert len(reader._row_cache) == 3  # noqa: SLF001 - own cache, own test

    monkeypatch.setattr(catalog_module, "ROW_CACHE", 5)
    assert len(reader.list_sessions().sessions) == 5
    assert len(reader._row_cache) == 5  # noqa: SLF001

    paths[0].unlink()
    after = reader.list_sessions()

    assert (after.state, after.omitted) == ("ready", 0)  # a pruned file is not a gap
    assert len(after.sessions) == 4
    assert session_id(0) not in reader._row_cache  # noqa: SLF001

    # And the whole project going away drops every row at once, rather than leaving the rail's
    # rows behind a folder that no longer exists until some later listing prunes them one by one.
    for path in paths[1:]:
        path.unlink()
    folder.rmdir()
    gone = reader.list_sessions()

    assert (gone.state, gone.reason, gone.sessions, gone.omitted) == ("ready", "", [], 0)
    assert reader._row_cache == {}  # noqa: SLF001


def test_a_listing_is_partial_exactly_when_it_omitted_something(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rail's only sentence for a partial listing names a number — "incomplete list · N not
    read yet" — so `partial` with nothing omitted has no honest wording, and `ready` with
    something omitted would show a short list as though it were the whole one. The browser reads
    a body that breaks either direction as malformed. Only `error`, which shows no count, is free
    of the rule. Every listing outcome this module can produce is checked here at once."""
    home, folder = _home(tmp_path)
    paths = [_small(folder, session_id(i), seed=i, kib=2).path for i in range(3)]
    _mtimes(paths, newest_first=[2, 0, 1])
    opener = Path.open

    def locked(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == f"{session_id(0)}.jsonl":
            raise PermissionError(5, "denied")
        return opener(self, *args, **kwargs)

    def pruned(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == f"{session_id(0)}.jsonl":
            raise FileNotFoundError(2, "gone")
        return opener(self, *args, **kwargs)

    def unlistable(self: Path) -> Iterator[Path]:
        raise PermissionError(5, "denied")

    outcomes = []
    outcomes.append(("ready", Catalog(home, VAULT).list_sessions()))
    monkeypatch.setattr(catalog_module, "LIST_DEADLINE_S", 0.05)
    outcomes.append(("deadline", Catalog(home, VAULT).list_sessions(clock=_ticking(0.02))))
    monkeypatch.undo()
    monkeypatch.setattr(Path, "open", locked)
    outcomes.append(("unreadable file", Catalog(home, VAULT).list_sessions()))
    monkeypatch.undo()
    monkeypatch.setattr(Path, "open", pruned)
    outcomes.append(("pruned file", Catalog(home, VAULT).list_sessions()))
    monkeypatch.undo()
    monkeypatch.setattr(Path, "iterdir", unlistable)
    outcomes.append(("unlistable project", Catalog(home, VAULT).list_sessions()))
    monkeypatch.undo()
    outcomes.append(("no project folder", Catalog(tmp_path / "elsewhere", VAULT).list_sessions()))

    assert {result.state for _, result in outcomes} == {"ready", "partial", "error"}
    for label, result in outcomes:
        if result.state == "error":
            # `error` shows no count at all, so the only honest number it can carry is none.
            assert (result.omitted, result.sessions) == (0, []), label
            continue
        assert (result.state == "partial") == (result.omitted > 0), (label, result.state)
        assert (result.state == "ready") == (result.omitted == 0), (label, result.state)
        assert (result.reason != "") == (result.omitted > 0), (label, result.reason)
        assert result.omitted >= 0


# --- search: cancellation, deadlines and byte budgets ---------------------------------------------


def _corpus(folder: Path, count: int, *, kib: int = 2) -> None:
    for i in range(count):
        _small(folder, session_id(i), seed=i, kib=kib)


def test_a_superseded_search_stops_between_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user keeps typing while a scan runs. A query they have moved past is stopped at the
    next session boundary, and its hits are dropped rather than painted over the newer ones."""
    home, folder = _home(tmp_path)
    _corpus(folder, 3)
    reader = Catalog(home, VAULT)
    reader.list_sessions()  # warm, so the search's own budgets are the only thing being measured
    flags = iter([False, True, True, True])

    result = reader.run_search(NEEDLE, cancelled=lambda: next(flags))

    assert (result.state, result.reason) == ("partial", "cancelled")
    assert result.hits == []
    assert (result.scanned, result.total) == (1, 3)

    # And when the deadline falls due at the same boundary, `cancelled` still wins. It has to:
    # only `cancelled` means "they are not waiting for this any more", and only that reason
    # reaches the browser as `superseded`. A deadline here would hand the palette a partial
    # result with stale hits to paint under a query that no longer exists.
    monkeypatch.setattr(catalog_module, "SEARCH_DEADLINE_S", 0.05)
    flags = iter([False, True, True, True])
    both = reader.run_search(NEEDLE, cancelled=lambda: next(flags), clock=_ticking(0.02))

    assert (both.state, both.reason) == ("partial", "cancelled")
    assert both.hits == []
    assert (both.scanned, both.total) == (1, 3)
    # The deadline really was due at that boundary: the same clock without a cancel says so.
    assert reader.run_search(NEEDLE, clock=_ticking(0.02)).reason == "time_budget"


def test_search_budgets_yield_partial_with_the_stopping_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each budget names itself, so the palette can say how much of the vault an answer covered
    instead of showing a short list as though it were the whole one."""
    home, folder = _home(tmp_path)
    _corpus(folder, 3)
    reader = Catalog(home, VAULT)
    reader.list_sessions()
    whole = reader.run_search(NEEDLE)
    assert (whole.state, whole.reason) == ("ready", "")
    assert len(whole.hits) > 2 and (whole.scanned, whole.total) == (3, 3)

    monkeypatch.setattr(catalog_module, "SEARCH_DEADLINE_S", 0.05)
    slow = reader.run_search(NEEDLE, clock=_ticking(0.02))
    assert (slow.state, slow.reason) == ("partial", "time_budget")
    assert (slow.scanned, slow.total) == (1, 3)
    assert slow.hits  # what it did read still reaches them
    monkeypatch.undo()

    biggest = max(p.stat().st_size for p in folder.glob("*.jsonl"))
    monkeypatch.setattr(catalog_module, "SEARCH_SCAN_BYTES", biggest + 1)
    capped = reader.run_search(NEEDLE)
    assert (capped.state, capped.reason) == ("partial", "scan_budget")
    assert capped.scanned == 1
    # With both due at the same session the deadline wins, because run_search checks it before
    # the byte budget. It is the reason the user can act on — the byte budget would tell them
    # the vault is too big when in fact this search was simply slow.
    monkeypatch.setattr(catalog_module, "SEARCH_SCAN_BYTES", 1)
    monkeypatch.setattr(catalog_module, "SEARCH_DEADLINE_S", 0.05)
    first = reader.run_search(NEEDLE, clock=_ticking(0.05))
    assert (first.state, first.reason) == ("partial", "time_budget")
    assert (first.scanned, first.hits) == (0, [])
    monkeypatch.undo()

    half = len(json.dumps(asdict(whole), separators=(",", ":"))) // 2
    monkeypatch.setattr(catalog_module, "SEARCH_RESPONSE_BYTES", half)
    trimmed = reader.run_search(NEEDLE)
    assert (trimmed.state, trimmed.reason) == ("partial", "response_budget")
    assert 0 < len(trimmed.hits) < len(whole.hits)
    assert trimmed.hits == whole.hits[: len(trimmed.hits)]  # trailing hits go first
    assert len(json.dumps(asdict(trimmed), separators=(",", ":"))) <= half


def test_search_inherits_window_and_listing_partiality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search is only as complete as what it could read. A transcript scanned from its tail and
    a listing that missed files both make the answer partial, and the budget that stopped the
    scan outranks either."""
    home, folder = _home(tmp_path)
    _corpus(folder, 2)
    reader = Catalog(home, VAULT)
    reader.list_sessions()

    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 512)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 256)
    windowed = reader.run_search(NEEDLE)
    assert (windowed.state, windowed.reason) == ("partial", "file_budget")
    assert (windowed.scanned, windowed.total) == (2, 2)

    monkeypatch.setattr(catalog_module, "SEARCH_DEADLINE_S", 0.05)
    both = reader.run_search(NEEDLE, clock=_ticking(0.02))
    assert (both.state, both.reason) == ("partial", "time_budget")  # the stopper outranks the rest
    monkeypatch.undo()

    fresh = Catalog(home, VAULT)
    opener = Path.open

    def flaky(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == f"{session_id(0)}.jsonl":
            raise PermissionError(5, "denied")
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)
    inherited = fresh.run_search(NEEDLE)
    assert (inherited.state, inherited.reason) == ("partial", "listing_partial")
    assert (inherited.scanned, inherited.total) == (1, 1)


def test_a_transcript_that_goes_away_mid_search_keeps_its_title_hit_and_is_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three things a session whose file disappears between the listing and the scan must still
    get right. Its title match stands: the title is on the row, in memory, and dropping the
    session from the results because of its file would be the reader disagreeing with its own
    list. It is not counted in `scanned`, because "S of T sessions searched" has to mean searched
    — a palette that counts a session nobody could open is telling the user the vault was
    covered when it was not. And the answer says so: `partial`/`read_failed`, the last arm of the
    precedence, so a result short of a session is never handed over as a complete one."""
    home, folder = _home(tmp_path)
    written = [_small(folder, session_id(i), seed=i, kib=2) for i in range(2)]
    reader = Catalog(home, VAULT)
    rows = reader.sessions()
    assert len({row.title for row in rows}) == 2  # each session answers for itself
    query, query_id = written[0].title, written[0].session_id
    before = reader.run_search(query)
    assert [hit.role for hit in before.hits] == ["title"]
    assert (before.state, before.reason) == ("ready", "")
    assert (before.scanned, before.total) == (2, 2)

    # Locked after the listing cached its row: the read fails inside the scan.
    opener = Path.open

    def locked(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == f"{written[0].session_id}.jsonl":
            raise PermissionError(5, "denied")
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    unread = reader.run_search(query)
    assert [(hit.session_id, hit.role) for hit in unread.hits] == [(query_id, "title")]
    assert (unread.state, unread.reason) == ("partial", "read_failed")
    assert (unread.scanned, unread.total) == (1, 2)
    monkeypatch.undo()

    # And pruned outright, so even the stat fails: same answer, from the row alone. The listing's
    # own stat is allowed through, because what is being tested is the file going away in the
    # moment between being listed and being opened.
    stat = Path.stat
    stats = 0

    def gone(self: Path, **kwargs: Any) -> os.stat_result:
        nonlocal stats
        if self.name == f"{written[0].session_id}.jsonl":
            stats += 1
            if stats > 1:
                raise FileNotFoundError(2, "gone")
        return stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", gone)
    pruned = reader.run_search(query)

    assert [(hit.session_id, hit.role) for hit in pruned.hits] == [(query_id, "title")]
    assert (pruned.state, pruned.reason) == ("partial", "read_failed")
    assert (pruned.scanned, pruned.total) == (1, 2)


def test_read_failed_is_the_last_reason_and_every_stronger_one_outranks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_failed` is the weakest reason there is: it says only that the scan finished without
    one session's file. Anything that stopped the scan, or cut the answer down, happened to more
    of the vault than one file did and is the thing worth naming, so each of them still wins with
    the same file unreadable underneath.

    `Path.open` rather than `read_bytes`, because `read_bytes` goes through `open` and a tail
    window does too: patching the one place covers a whole-file read and a windowed one alike,
    which is what lets the file_budget leg below be a real collision rather than a file that
    quietly succeeded."""
    home, folder = _home(tmp_path)
    _corpus(folder, 3)
    reader = Catalog(home, VAULT)
    reader.list_sessions()  # warm, so the listing never opens the file that is about to break
    whole = reader.run_search(NEEDLE)
    assert (whole.state, whole.reason, whole.scanned) == ("ready", "", 3)

    opener = Path.open

    def locked(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == f"{session_id(0)}.jsonl":
            raise PermissionError(5, "denied")
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    missed = reader.run_search(NEEDLE)
    assert (missed.state, missed.reason) == ("partial", "read_failed")
    assert (missed.scanned, missed.total) == (2, 3)
    assert missed.hits  # the two sessions it could read still answer

    # listing_partial outranks it: a listing that omitted a file is a bigger gap than a scan
    # that missed one. (A fresh reader, so the listing has to open the locked file itself.)
    cold = Catalog(home, VAULT)
    assert cold.run_search(NEEDLE).reason == "listing_partial"

    # file_budget outranks it: the same file is unreadable, and every other one is windowed.
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 512)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 256)
    assert reader.run_search(NEEDLE).reason == "file_budget"
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 64 * 1024 * 1024)

    # response_budget outranks it: hits the user cannot be sent are nearer to them than a file.
    half = len(json.dumps(asdict(missed), separators=(",", ":"))) // 2
    monkeypatch.setattr(catalog_module, "SEARCH_RESPONSE_BYTES", half)
    assert reader.run_search(NEEDLE).reason == "response_budget"
    monkeypatch.setattr(catalog_module, "SEARCH_RESPONSE_BYTES", 256 * 1024)

    # And the budgets that stop the scan outright outrank it too.
    monkeypatch.setattr(catalog_module, "SEARCH_DEADLINE_S", 0.05)
    assert reader.run_search(NEEDLE, clock=_ticking(0.02)).reason == "time_budget"


def test_a_search_of_an_unlistable_project_is_an_error_not_an_empty_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "No matches" and "nothing could be read" look the same in a list of hits and mean opposite
    things, so the failure travels as the state and not as an empty result."""
    home, _ = _home(tmp_path)

    def denied(self: Path) -> Iterator[Path]:
        raise PermissionError(5, "denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    result = Catalog(home, VAULT).run_search(NEEDLE)

    assert (result.state, result.reason, result.hits) == ("error", "project_unreadable", [])
    assert (result.scanned, result.total) == (0, 0)


def test_the_frozen_wrappers_still_return_plain_lists(tmp_path: Path) -> None:
    """sessions() and search() are what the server, the acceptance suite and FakeCatalog call.
    They keep their names and their return types; the observation rides beside them."""
    home, folder = _home(tmp_path)
    written = _small(folder, SID)
    reader = Catalog(home, VAULT)

    rows = reader.sessions()
    assert [row.session_id for row in rows] == [SID]
    assert rows == reader.list_sessions().sessions
    hits = reader.search(written.title)
    assert hits and hits == reader.run_search(written.title).hits
