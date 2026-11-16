"""Extra tests for the transcript reader, beside the frozen acceptance suite
(tests/acceptance/test_catalog_accept.py): edges its synthetic transcripts and the one real
golden don't happen to exercise — a broken parentUuid graph, real-world file quirks, and the
in-memory caches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from viki_agent import catalog as catalog_module
from viki_agent import config
from viki_agent.catalog import (
    CHAIN_CACHE,
    PAGE_ENTRIES,
    Catalog,
    _build_chain,
    _read_entries,
    project_dir,
)
from viki_agent.events import Event, SessionRow

VAULT = Path("C:/Vault")
SID = "aaaaaaaa-1111-4111-8111-111111111111"
# Bounds a wait on an event that normally fires within milliseconds; never real work.
HANG_GUARD_S = 5


def _ts(n: int) -> str:
    return f"2026-09-11T00:00:{n:02d}.000Z"


def _uuid(n: int) -> str:
    """A canonical entry uuid. The reader treats anything else as missing, so tests that care
    what it logs or caches need the real shape."""
    return f"{n:08x}-1111-4111-8111-111111111111"


def _prompts(events: list[Event]) -> list[str]:
    return [e.text for e in events if e.TYPE == "user.message"]  # type: ignore[attr-defined]


def _user(uuid: str, parent: str | None, text: str, n: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "entrypoint": "cli",  # a MAIN_ENTRYPOINTS value, so rows aren't "other" and get searched
        "timestamp": _ts(n),
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(uuid: str, parent: str | None, text: str, n: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "entrypoint": "cli",
        "timestamp": _ts(n),
        "message": {"id": uuid, "role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _compact(uuid: str, logical_parent: str | None, n: int) -> dict[str, Any]:
    return {
        "type": "system",
        "uuid": uuid,
        "parentUuid": None,
        "isSidechain": False,
        "subtype": "compact_boundary",
        "timestamp": _ts(n),
        "logicalParentUuid": logical_parent,
        "compactMetadata": {"trigger": "auto", "preTokens": 1000},
    }


def _summary(uuid: str, parent: str, n: int) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "isCompactSummary": True,
        "timestamp": _ts(n),
        "message": {"role": "user", "content": "summary"},
    }


def _write(folder: Path, session_id: str, lines: list[dict[str, Any]]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    text = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    path.write_bytes(text.encode("utf-8"))
    return path


# --- the chain: broken graphs ------------------------------------------------------------------


def test_a_parentuuid_cycle_does_not_hang_and_is_ignored() -> None:
    z = {"type": "progress", "uuid": "z", "parentUuid": "p", "isSidechain": False}
    p = {"type": "progress", "uuid": "p", "parentUuid": "q", "isSidechain": False}
    q = {"type": "progress", "uuid": "q", "parentUuid": "p", "isSidechain": False}
    r = _user("r", None, "hello", 1)
    s = _assistant("s", "r", "hi", 2)
    # z's walk-up loops p <-> q forever without reaching a user/assistant entry; it must give up
    # (via the "stop at a uuid seen before" rule) rather than hang, leaving the real r->s chain.
    chain = _build_chain([z, p, q, r, s])
    assert [e["uuid"] for e in chain] == ["r", "s"]


def test_an_unresolvable_parentuuid_ends_the_chain_walk() -> None:
    # decoy sits earlier in file order but is not mid's real parent (mid's true parent was
    # trimmed from this file, e.g. by a partial write or hand-edit). The walk must stop at mid,
    # exactly as the SDK port does — never splice in decoy just because it happens to precede
    # mid in file order.
    decoy = {"type": "progress", "uuid": "decoy", "parentUuid": None, "isSidechain": False}
    mid = {"type": "progress", "uuid": "mid", "parentUuid": "missing", "isSidechain": False}
    leaf = _assistant("leaf", "mid", "answer", 1)

    chain = _build_chain([decoy, mid, leaf])

    assert [e["uuid"] for e in chain] == ["mid", "leaf"]


def test_history_reaches_back_across_two_compactions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)
    c1, sum1 = _compact("c1", "a1", 3), _summary("sum1", "c1", 4)
    u2, a2 = _user("u2", "sum1", "two", 5), _assistant("a2", "u2", "ans2", 6)
    c2, sum2 = _compact("c2", "a2", 7), _summary("sum2", "c2", 8)
    u3, a3 = _user("u3", "sum2", "three", 9), _assistant("a3", "u3", "ans3", 10)
    _write(folder, SID, [u1, a1, c1, sum1, u2, a2, c2, sum2, u3, a3])

    page = Catalog(home, VAULT).history(SID)

    types = [e.TYPE for e in page.events]
    assert types == [
        "user.message",
        "text.done",
        "compacted",
        "user.message",
        "text.done",
        "compacted",
        "user.message",
        "text.done",
    ]
    prompts = [e.text for e in page.events if e.TYPE == "user.message"]  # type: ignore[attr-defined]
    assert prompts == ["one", "two", "three"]


def test_a_compact_boundary_with_a_missing_logical_parent_stops_the_walk() -> None:
    u1 = _user("u1", None, "unreachable", 1)
    a1 = _assistant("a1", "u1", "unreachable-ans", 2)
    # c1's logicalParentUuid points nowhere in this file (e.g. the real ancestor was trimmed).
    c1 = _compact("c1", "does-not-exist", 3)
    sum1 = _summary("sum1", "c1", 4)
    u2 = _user("u2", "sum1", "reachable", 5)
    a2 = _assistant("a2", "u2", "reachable-ans", 6)

    chain = _build_chain([u1, a1, c1, sum1, u2, a2])

    assert [e["uuid"] for e in chain] == ["c1", "sum1", "u2", "a2"]


# --- real-world file quirks -------------------------------------------------------------------


def test_crlf_line_endings_are_read(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True, exist_ok=True)
    u1, a1 = _user("u1", None, "hello crlf", 1), _assistant("a1", "u1", "hi back", 2)
    text = "\r\n".join(json.dumps(e, ensure_ascii=False) for e in (u1, a1)) + "\r\n"
    (folder / f"{SID}.jsonl").write_bytes(text.encode("utf-8"))

    page = Catalog(home, VAULT).history(SID)

    prompts = [e.text for e in page.events if e.TYPE == "user.message"]  # type: ignore[attr-defined]
    assert prompts == ["hello crlf"]


def test_invalid_utf8_bytes_are_replaced_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    u1 = _user("u1", None, "bad byte here", 1)
    raw = json.dumps(u1, ensure_ascii=False).encode("utf-8")
    marker = b"bad byte here"
    idx = raw.index(marker)
    raw = raw[: idx + 3] + b"\xff" + raw[idx + 3 :]  # an invalid UTF-8 byte inside the text
    path.write_bytes(raw + b"\n")

    entries = _read_entries(path)

    assert len(entries) == 1
    assert "\ufffd" in entries[0]["message"]["content"]


def test_modified_falls_back_to_mtime_when_the_file_has_no_timestamps(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "isSidechain": False,
        "message": {"role": "user", "content": "no timestamp here"},
    }
    path = folder / f"{SID}.jsonl"
    path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = Catalog(home, VAULT).sessions()

    assert len(rows) == 1
    assert rows[0].modified.endswith("Z")
    parsed = datetime.fromisoformat(rows[0].modified.replace("Z", "+00:00"))
    actual_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    assert abs((parsed - actual_mtime).total_seconds()) < 1.0


def test_search_query_with_regex_and_json_special_characters(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1 = _user("u1", None, "cost is $5 (approx.) [ok]? see a.b*c+d", 1)
    a1 = _assistant("a1", "u1", "noted", 2)
    _write(folder, SID, [u1, a1])
    catalog = Catalog(home, VAULT)

    hits = catalog.search("$5 (approx.) [ok]?")

    # the title falls back to the first prompt here, so it matches too (title, then user)
    assert [h.role for h in hits] == ["title", "user"]
    assert all("$5" in h.snippet for h in hits)
    # literal regex/JSON metacharacters (unbalanced bracket, backslash) must never raise, since
    # matching is plain byte containment, never re.
    assert catalog.search("[unclosed(.*+?^${}\\") == []


def test_search_matches_backslash_paths_and_quoted_phrases(tmp_path: Path) -> None:
    # the query text lives in a second turn, never in the title (which falls back to the first
    # prompt, "hello") — so a hit can only come from the raw-line prefilter over transcript text.
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "hello", 1), _assistant("a1", "u1", "hi", 2)
    quoted = 'open C:\\Users\\x\\notes.md and say "hello there"'
    u2, a2 = _user("u2", "a1", quoted, 3), _assistant("a2", "u2", "ok", 4)
    _write(folder, SID, [u1, a1, u2, a2])
    catalog = Catalog(home, VAULT)

    # a Windows path (contains a backslash) and a quoted phrase (contains double quotes): both
    # are stored JSON-escaped (`\\` and `\"`) in the raw JSONL, unlike the plain query text.
    for query in ("C:\\Users", '"hello there"', "hello there"):
        found = catalog.search(query)
        assert [h.role for h in found] == ["user"], query
        assert found[0].session_id == SID


def test_search_allows_a_single_cjk_character_but_not_a_single_ascii_one(tmp_path: Path) -> None:
    # The 2-character floor exists to filter 1-letter Latin noise; a lone CJK character is
    # already a selective query (as opposed to "x"), so it must not be held to the same floor.
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1 = _user("u1", None, "図書館の予約をしたい", 1)
    a1 = _assistant("a1", "u1", "noted", 2)
    _write(folder, SID, [u1, a1])
    catalog = Catalog(home, VAULT)

    assert [h.role for h in catalog.search("図")] == ["title", "user"]
    assert catalog.search("x") == []  # a single ASCII letter returns nothing


def test_a_lone_surrogate_in_text_or_query_does_not_break_search(tmp_path: Path) -> None:
    # JSON.stringify writes "\ud83d" for a JS string cut mid-pair; json.loads gives it back as a
    # lone surrogate, which has no UTF-8 form.
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True)
    u1, a1 = _user("u1", None, "hello", 1), _assistant("a1", "u1", "hi", 2)
    u2 = _user("u2", "a1", "cut emoji \ud83d here", 3)
    lines = "".join(json.dumps(e) + "\n" for e in (u1, a1, u2))  # ASCII: the surrogate escaped
    (folder / f"{SID}.jsonl").write_text(lines, encoding="utf-8")
    catalog = Catalog(home, VAULT)

    assert [h.role for h in catalog.search("emoji")] == ["user"]
    assert catalog.search("\ud83d emoji") == []


def test_a_query_with_no_ascii_letter_still_matches_a_line_full_of_capitals(
    tmp_path: Path,
) -> None:
    """The line scan skips lowercasing each line when neither form it looks for holds an ASCII
    letter — a full second copy of a file that can be hundreds of MB, saved on every query
    written in a script without one. It is only sound because `.lower()` folds A-Z and nothing
    else, so a line of capitals must still match a query that has none, and an ASCII query must
    still fold."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "hello", 1), _assistant("a1", "u1", "hi", 2)
    u2 = _user("u2", "a1", "QRS の 天気予報 XYZ の話", 3)
    a2 = _assistant("a2", "u2", "OK", 4)
    _write(folder, SID, [u1, a1, u2, a2])
    reader = Catalog(home, VAULT)

    assert [h.role for h in reader.search("天気予報")] == ["user"]  # no fold: matched raw
    assert [h.role for h in reader.search("QRS")] == ["user"]  # folded: matched lowercased
    assert [h.role for h in reader.search("qrs")] == ["user"]


def test_a_bare_command_is_the_first_prompt_as_typed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    daily = "<command-message>daily</command-message>\n<command-name>/daily</command-name>\n"
    u1 = _user("u1", None, daily + "<command-args></command-args>", 1)
    a1 = _assistant("a1", "u1", "done", 2)
    u2, a2 = _user("u2", "a1", "what next?", 3), _assistant("a2", "u2", "this", 4)
    _write(folder, SID, [u1, a1, u2, a2])

    [row] = Catalog(home, VAULT).sessions()

    assert (row.first_prompt, row.title) == ("/daily", "/daily")


def test_non_uuid_session_id_is_treated_as_missing(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "home", VAULT)

    assert catalog.exists("not-a-uuid") is False
    assert catalog.exists("AAAAAAAA-1111-4111-8111-111111111111") is False  # canonical: lowercase

    page = catalog.history("not-a-uuid")
    assert page.events == []
    assert page.next_before_uuid is None


# --- caching -----------------------------------------------------------------------------------


def test_chain_cache_is_reused_then_invalidated_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)
    _write(folder, SID, [u1, a1])
    catalog = Catalog(home, VAULT)

    calls: list[Path] = []
    original = catalog_module._window

    def counting(path: Path, start: int, length: int) -> bytes:
        calls.append(path)
        return original(path, start, length)

    monkeypatch.setattr(catalog_module, "_window", counting)

    catalog.history(SID)
    catalog.history(SID)
    assert len(calls) == 1  # the second call reused the cached chain, no re-read

    u2, a2 = _user("u2", "a1", "two", 3), _assistant("a2", "u2", "ans2", 4)
    _write(folder, SID, [u1, a1, u2, a2])  # grows the file: size and mtime both change

    catalog.history(SID)
    assert len(calls) == 2  # invalidated: read again


def test_a_sessions_row_is_rebuilt_when_its_file_changes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)
    _write(folder, SID, [u1, a1])
    catalog = Catalog(home, VAULT)
    [before] = catalog.sessions()

    renamed = {"type": "custom-title", "customTitle": "Renamed", "sessionId": SID}
    _write(folder, SID, [u1, a1, renamed, _user("u2", "a1", "two", 30)])
    [after] = catalog.sessions()

    assert (before.title, after.title) == ("one", "Renamed")
    assert after.modified > before.modified


def _append(folder: Path, session_id: str, lines: list[dict[str, Any]]) -> None:
    with (folder / f"{session_id}.jsonl").open("ab") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n")


def test_an_appended_turn_rebuilds_the_cached_row_with_one_bounded_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session the user is working in grows every turn, so its row cache key never matches
    and a newline at the old boundary cannot prove that its prefix is unchanged. One bounded
    scan rebuilds its row, which is the one a reader with no cache computes."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2, version="2.1.1")
    _write(folder, SID, [u1, a1])
    catalog = Catalog(home, VAULT)
    [before] = catalog.sessions()

    scans: list[Path] = []
    original = catalog_module._row_lines  # noqa: SLF001 - own module, own test

    def counting(path: Path, size: int) -> Any:
        scans.append(path)
        return original(path, size)

    monkeypatch.setattr(catalog_module, "_row_lines", counting)
    _append(
        folder,
        SID,
        [
            _user("u2", "a1", "two", 30, version="2.1.2"),
            {"type": "custom-title", "customTitle": "Renamed", "sessionId": SID},
        ],
    )
    [after] = catalog.sessions()

    assert scans == [folder / f"{SID}.jsonl"]
    assert (before.title, after.title) == ("one", "Renamed")
    assert after.first_prompt == "one"  # fixed by the first entry, not by the newest one
    assert after == Catalog(home, VAULT).sessions()[0]  # identical to a full rescan


def test_a_rewritten_transcript_is_scanned_whole_and_never_folded(tmp_path: Path) -> None:
    """A fold is only sound for an append. A file that shrank, and one that grew by being written
    again from the start, both fall through to the ordinary scan."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    path = folder / f"{SID}.jsonl"
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)
    _write(folder, SID, [u1, a1, {"type": "custom-title", "customTitle": "Renamed"}])
    catalog = Catalog(home, VAULT)
    [titled] = catalog.sessions()
    grown = path.stat().st_size

    _write(folder, SID, [_user("u9", None, "shorter", 5)])  # shorter: a rewrite, not an append
    [shrunk] = catalog.sessions()

    long_prompt = "a much longer first prompt, so the file grows while its lines all change"
    _write(folder, SID, [_user("u9", None, long_prompt, 5), _assistant("a9", "u9", "ans", 6)])
    assert path.stat().st_size > grown  # longer than the cached scan, and still not an append
    assert path.read_bytes()[grown - 1 : grown] != b"\n"
    [rewritten] = catalog.sessions()

    assert (titled.title, shrunk.title) == ("Renamed", "shorter")
    assert rewritten == Catalog(home, VAULT).sessions()[0]


def test_an_append_that_completes_a_half_written_line_is_read_whole(tmp_path: Path) -> None:
    """The CLI can be caught mid-write: the scan skips the fragment as corrupt and stops on a byte
    that is not a newline. The completed line must not be folded over — the appended bytes alone
    are half a JSON object and would be skipped again, losing the line for the life of the
    process."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    path = folder / f"{SID}.jsonl"
    _write(folder, SID, [_user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)])
    title_line = json.dumps({"type": "custom-title", "customTitle": "Renamed"}).encode("utf-8")
    with path.open("ab") as fh:
        fh.write(title_line[:20])
    catalog = Catalog(home, VAULT)
    [torn] = catalog.sessions()

    with path.open("ab") as fh:
        fh.write(title_line[20:] + b"\n")
    [whole] = catalog.sessions()

    assert (torn.title, whole.title) == ("one", "Renamed")


def test_growth_past_the_read_budget_stops_folding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over FULL_READ_BYTES a row comes from a head window and a tail window, which is not the
    prefix of anything: nothing may be folded onto it, and nothing may be folded onto the state
    that preceded it."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    path = folder / f"{SID}.jsonl"
    _write(
        folder,
        SID,
        [
            _user("u1", None, "one", 1),
            _assistant("a1", "u1", "ans1", 2),
            _user("u2", "a1", "two", 3),
            _assistant("a2", "u2", "ans2", 4),
        ],
    )
    raw = path.read_bytes()
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", len(raw))
    monkeypatch.setattr(catalog_module, "HEAD_WINDOW_BYTES", 0)
    # A tail window the width of the last line, so the windows an over-budget scan reads hold
    # neither the first prompt nor the entrypoint — which is exactly what folding would forge.
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", len(raw) - raw.rindex(b"\n", 0, -1))
    catalog = Catalog(home, VAULT)
    [inside] = catalog.sessions()

    _append(folder, SID, [{"type": "custom-title", "customTitle": "Renamed"}])
    [over] = catalog.sessions()
    # Read cold while the file is untouched: these rows carry the mtime, not a timestamp.
    [over_cold] = Catalog(home, VAULT).sessions()
    _append(folder, SID, [{"type": "ai-title", "aiTitle": "Ignored while custom-title stands"}])
    [larger] = catalog.sessions()
    [larger_cold] = Catalog(home, VAULT).sessions()

    assert (inside.title, inside.first_prompt) == ("one", "one")
    assert (over, larger) == (over_cold, larger_cold)
    assert (over.title, over.first_prompt) == ("Renamed", "")  # the window holds no first prompt


def test_a_stale_chain_is_dropped_before_the_file_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case is the session the user is in: claude appended a turn, so the cached
    chain is stale and their next "load older" re-parses. If the stale chain is only replaced
    after the parse, it stays resident beside the file's bytes and the new entries list —
    measured at ~145 MB of an ~850 MB peak on a real 263 MB transcript."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    u1, a1 = _user("u1", None, "one", 1), _assistant("a1", "u1", "ans1", 2)
    _write(folder, SID, [u1, a1])
    reader = Catalog(home, VAULT)

    still_cached: list[bool] = []
    original = catalog_module._read_entries  # noqa: SLF001 - own module, own test

    def watching(path: Path) -> list[dict[str, Any]]:
        still_cached.append(SID in reader._chain_cache)  # noqa: SLF001
        return original(path)

    monkeypatch.setattr(catalog_module, "_read_entries", watching)

    reader.history(SID)
    _write(folder, SID, [u1, a1, _user("u2", "a1", "two", 3)])  # grows: the cache is now stale
    reader.history(SID)

    assert still_cached == [False, False]  # nothing held across either parse


def test_the_chain_cache_is_bounded_by_bytes_and_never_drops_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHAIN_CACHE counts files, which says nothing about memory — a chain retains roughly half
    its file's bytes as live objects, so eight large sessions is over a gigabyte held for the
    life of the process. The newest entry is exempt: it is the session being read, and evicting
    it would make every "load older" click re-parse the largest file in the vault."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    ids = [_uuid(i) for i in range(3)]
    for i, sid in enumerate(ids):
        _write(folder, sid, [_user(f"u{i}", None, "hello " + "pad " * 200, 1)])
    one_file = (folder / f"{ids[0]}.jsonl").stat().st_size

    monkeypatch.setattr(catalog_module, "CHAIN_CACHE_BYTES", one_file * 2)
    reader = Catalog(home, VAULT)
    for sid in ids:
        reader.history(sid)
    # Two files fit the budget, three do not: the oldest goes, though the count cap (8) is idle.
    assert list(reader._chain_cache) == ids[1:]  # noqa: SLF001

    monkeypatch.setattr(catalog_module, "CHAIN_CACHE_BYTES", one_file // 2)
    lean = Catalog(home, VAULT)
    lean.history(ids[0])
    lean.history(ids[1])
    # Each is over the whole budget on its own, and the one just read is still kept.
    assert list(lean._chain_cache) == [ids[1]]  # noqa: SLF001


def test_chain_cache_evicts_the_least_recently_used(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    catalog = Catalog(home, VAULT)
    session_ids = [f"{i:08x}-1111-4111-8111-111111111111" for i in range(CHAIN_CACHE + 1)]
    for i, sid in enumerate(session_ids):
        _write(folder, sid, [_user(f"u{i}", None, f"hello {i}", 1)])
        catalog.history(sid)

    assert len(catalog._chain_cache) == CHAIN_CACHE  # noqa: SLF001 - own cache, own test
    assert session_ids[0] not in catalog._chain_cache  # noqa: SLF001
    assert session_ids[-1] in catalog._chain_cache  # noqa: SLF001


# --- the conversation chain served to search, anchor and detail ---------------------------------


def _mixed_chain(folder: Path) -> Path:
    """A transcript whose main chain holds a non-canonical root, a sidechain entry and a turn past
    `until`, beside a sidechain branch that is never the leaf."""
    return _write(
        folder,
        SID,
        [
            {"type": "progress", "uuid": "not-a-uuid", "parentUuid": None, "isSidechain": False},
            _user(_uuid(1), "not-a-uuid", "one", 1),
            _assistant(_uuid(2), _uuid(1), "ans1", 2),
            _user(_uuid(3), _uuid(2), "aside", 3, isSidechain=True),
            _user(_uuid(4), _uuid(1), "branch", 4, isSidechain=True),
            _user(_uuid(5), _uuid(3), "two", 5),
            _assistant(_uuid(6), _uuid(5), "ans2", 6),
            _user(_uuid(7), _uuid(6), "live", 7),
        ],
    )


def test_the_conversation_chain_has_the_expected_uuids_and_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _mixed_chain(project_dir(home, VAULT))
    reader = Catalog(home, VAULT)

    chain = reader.conversation_chain(SID, until=_ts(7))

    assert chain.status == "complete"
    uuids = [entry["uuid"] for entry in chain.entries]
    assert uuids == [_uuid(1), _uuid(2), _uuid(5), _uuid(6)]
    lines = {json.loads(line)["uuid"]: line for line in path.read_bytes().splitlines()}
    for entry in chain.entries:
        expected = hashlib.sha256(lines[entry["uuid"]]).hexdigest()
        assert entry["_viki_source_revision"] == expected


def test_a_non_canonical_uuid_mid_chain_keeps_the_entries_before_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(
        project_dir(home, VAULT),
        SID,
        [
            _user(_uuid(1), None, "one", 1),
            {
                "type": "progress",
                "uuid": "not-a-uuid",
                "parentUuid": _uuid(1),
                "isSidechain": False,
            },
            _user(_uuid(3), "not-a-uuid", "three", 3),
        ],
    )
    chain = Catalog(home, VAULT).conversation_chain(SID)
    assert [entry["uuid"] for entry in chain.entries] == [_uuid(1), _uuid(3)]


def test_the_conversation_chain_carries_a_failure_status_and_no_entries(tmp_path: Path) -> None:
    reader = Catalog(tmp_path / "home", VAULT)
    assert reader.conversation_chain(SID) == catalog_module.ConversationChain("missing", [])


def test_two_conversation_chain_calls_parse_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _mixed_chain(project_dir(home, VAULT))
    reader = Catalog(home, VAULT)
    parses: list[Path] = []
    original = catalog_module._read_entries  # noqa: SLF001 - own module, own test

    def counting(path: Path) -> list[dict[str, Any]]:
        parses.append(path)
        return original(path)

    monkeypatch.setattr(catalog_module, "_read_entries", counting)

    first = reader.conversation_chain(SID)
    second = reader.conversation_chain(SID, until=_ts(7))

    assert len(parses) == 1
    assert [e["uuid"] for e in first.entries][:-1] == [e["uuid"] for e in second.entries]


class _WatchedPermit:
    """The parse permit, telling the test when a caller starts waiting on it."""

    def __init__(self, permit: threading.BoundedSemaphore, waiting: threading.Event) -> None:
        self._permit = permit
        self._waiting = waiting

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if blocking:
            self._waiting.set()
        return self._permit.acquire(blocking, timeout)

    def release(self) -> None:
        self._permit.release()


def test_a_second_reader_waits_on_the_parse_permit_and_reuses_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _mixed_chain(project_dir(home, VAULT))
    reader = Catalog(home, VAULT)
    parsing, finish, waiting = threading.Event(), threading.Event(), threading.Event()
    parses: list[Path] = []
    original = catalog_module._read_entries  # noqa: SLF001 - own module, own test

    def held(path: Path) -> list[dict[str, Any]]:
        parses.append(path)
        parsing.set()
        assert finish.wait(HANG_GUARD_S)
        return original(path)

    monkeypatch.setattr(catalog_module, "_read_entries", held)
    watched = _WatchedPermit(reader._parse_permit, waiting)  # noqa: SLF001
    reader._parse_permit = watched  # type: ignore[assignment]  # noqa: SLF001
    results: list[catalog_module.ConversationChain] = []

    def read() -> None:
        results.append(reader.conversation_chain(SID))

    leader, follower = threading.Thread(target=read), threading.Thread(target=read)
    leader.start()
    assert parsing.wait(HANG_GUARD_S)
    follower.start()
    assert waiting.wait(HANG_GUARD_S)  # the follower took the waiting path, not "busy"
    finish.set()
    leader.join(HANG_GUARD_S)
    follower.join(HANG_GUARD_S)

    assert len(parses) == 1
    assert [r.status for r in results] == ["complete", "complete"]
    first, second = (r.entries for r in results)
    assert len(first) == len(second) == 5
    assert all(a is b for a, b in zip(first, second, strict=True))  # the cache's own dicts


# --- what a skipped entry says in the log ------------------------------------------------------


def test_a_skipped_entry_is_logged_with_enough_to_find_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The likeliest way this app breaks is the CLI writing a shape the mapper was not written
    against, and the symptom is a page quietly short of turns, never an error. The one line that
    records it has to say which session, which entry and which mapper rule gave up — a constant
    sentence left a log of identical lines nobody could act on."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    good = _user(_uuid(1), None, "one", 1, sessionId=SID)
    reply = _assistant(_uuid(2), _uuid(1), "ans", 2, sessionId=SID)
    bad = _user(_uuid(3), _uuid(2), "unused", 3, sessionId=SID)
    bad["message"] = "not a dict"  # Mapper does message.get("content"): AttributeError
    _write(folder, SID, [good, reply, bad])

    with caplog.at_level(logging.WARNING, logger="viki_agent.catalog"):
        page = Catalog(home, VAULT).history(SID)

    assert _prompts(page.events) == ["one"]  # one bad entry still costs one entry, not the page
    messages = [record.getMessage() for record in caplog.records]
    assert messages  # history maps a user entry twice (turn classifier, then the page)
    for text in messages:
        assert f"session={SID}" in text
        assert f"uuid={_uuid(3)}" in text
        assert "type=user" in text
        assert "AttributeError" in text
        assert "mapper.py:" in text  # the raise site names the rule that broke


def test_a_skipped_entry_never_quotes_the_transcript_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One mapper rule puts transcript text inside the exception itself: Mapper.entry's
    `int(meta.get("preTokens", 0))` raises `invalid literal for int() with base 10: '<value>'`,
    and the value came off the wire. Hard rule 8 keeps prompt text and frame bodies out of
    viki-agent.log, so the handler logs the exception's type and raise site — never str(exc),
    and never exc_info, because a rendered traceback carries the message too. Each record is
    rendered here the way a handler writes it, traceback included, so either slip fails this."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    pasted = "leak-canary-9f3c"  # stands for the web pages the user pastes into a session
    boundary = _compact(_uuid(3), _uuid(2), 3)
    boundary["compactMetadata"] = {"trigger": "auto", "preTokens": pasted}  # int(): ValueError
    _write(
        folder,
        SID,
        [
            _user(_uuid(1), None, "one", 1, sessionId=SID),
            _assistant(_uuid(2), _uuid(1), "ans", 2, sessionId=SID),
            boundary,
            _user(_uuid(4), _uuid(3), "two", 4, sessionId=SID),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="viki_agent.catalog"):
        page = Catalog(home, VAULT).history(SID)

    assert _prompts(page.events) == ["one", "two"]  # the boundary is lost, the page is not
    # Formatted, not getMessage(): a Formatter appends the traceback when a record carries
    # exc_info, so this is what would reach the file, not only the sentence.
    rendered = [logging.Formatter().format(record) for record in caplog.records]
    assert rendered
    for text in rendered:
        assert "ValueError" in text
        assert "mapper.py:" in text
        assert pasted not in text


def test_a_transcripts_own_fields_cannot_forge_a_line_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Everything in a transcript is attacker-influenced (the user pastes web pages into
    sessions), so a uuid carrying a newline would write whole lines of its own into
    viki-agent.log. Shown only when it is a value we recognise, as server.py does with a
    dropped client message's type."""
    forged = "\n2026-09-11 09:00:00 WARNING viki_agent.server nothing to see here"
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    good = _user(_uuid(1), None, "one", 1, sessionId=SID)
    bad = _user(forged, _uuid(1), "unused", 2, sessionId=forged)
    bad["message"] = "not a dict"
    _write(folder, SID, [good, bad])

    with caplog.at_level(logging.WARNING, logger="viki_agent.catalog"):
        Catalog(home, VAULT).history(SID)

    messages = [record.getMessage() for record in caplog.records]
    assert messages
    for text in messages:
        assert "\n" not in text
        assert "session=? uuid=?" in text


# --- a page is bounded by entries as well as turns ---------------------------------------------


def _turn_with_tools(lines: list[dict[str, Any]], index: int, tools: int) -> None:
    """Appends one turn to `lines`: the user's prompt, `tools` tool_use/tool_result pairs, then
    a reply — the shape that makes "20 turns" say nothing about how big a page is."""
    parent = str(lines[-1]["uuid"]) if lines else None
    prompt = _uuid(index * 10_000)
    lines.append(_user(prompt, parent, f"Question {index}", 1))
    parent = prompt
    for n in range(tools):
        use, result = _uuid(index * 10_000 + n * 2 + 1), _uuid(index * 10_000 + n * 2 + 2)
        tool_id = f"toolu_{index}_{n}"
        block = {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "x.md"}}
        lines.append(
            _assistant(use, parent, "", 1)
            | {"message": {"id": use, "role": "assistant", "content": [block]}}
        )
        lines.append(
            _user(result, use, "", 1)
            | {
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
                }
            }
        )
        parent = result
    lines.append(_assistant(_uuid(index * 10_000 + tools * 2 + 1), parent, f"Answer {index}", 1))


def test_a_page_is_cut_by_entries_as_well_as_turns(tmp_path: Path) -> None:
    """PAGE_TURNS counts the user's prompts, and a modern turn is not small: the newest 20 of
    their own carry 4,715 events and 2.35 MB of JSON in one frame, parsed and mounted before
    they have typed anything. The entry bound cuts the page back at a turn boundary."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    lines: list[dict[str, Any]] = []
    for index in range(1, 11):  # 10 turns of 100 tool calls: 202 entries and 202 events each
        _turn_with_tools(lines, index, 100)
    _write(folder, SID, lines)
    reader = Catalog(home, VAULT)

    pages = []
    before: str | None = None
    while True:
        page = reader.history(SID, before_uuid=before)
        pages.append(page)
        if page.next_before_uuid is None:
            break
        before = page.next_before_uuid

    per_turn = 202  # the prompt, 100 tool.started, 100 tool.done, the reply
    assert [len(_prompts(p.events)) for p in pages] == [5, 5]  # not 10, and PAGE_TURNS never bit
    # The turn that crosses the line comes whole, so a page is the bound plus at most one turn.
    assert all(len(p.events) <= PAGE_ENTRIES + per_turn for p in pages)
    walked = [text for page in reversed(pages) for text in _prompts(page.events)]
    assert walked == [f"Question {n}" for n in range(1, 11)]


def test_a_short_newest_turn_does_not_become_a_page_too_small_to_scroll(tmp_path: Path) -> None:
    """The conversation asks for an older page only from a scroll event, and a column that does
    not overflow never fires one. So the entry bound rounds up: a two-line "ok" typed after an
    enormous turn must not open the session as the whole first page, with the rest unreachable."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    lines: list[dict[str, Any]] = []
    _turn_with_tools(lines, 1, 2)
    _turn_with_tools(lines, 2, 600)  # 1,202 entries
    _turn_with_tools(lines, 3, 0)  # "ok": 2 entries
    _write(folder, SID, lines)

    page = Catalog(home, VAULT).history(SID)

    assert _prompts(page.events) == ["Question 2", "Question 3"]


def test_one_turn_over_the_entry_bound_is_still_one_whole_page(tmp_path: Path) -> None:
    """Splitting a turn would put a tool_use on one page and its result on another, which the
    whole-turn rule exists to prevent. So the entry bound yields: it stops a page accumulating
    many large turns, and cannot bound a single enormous one."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    lines: list[dict[str, Any]] = []
    _turn_with_tools(lines, 1, 2)
    _turn_with_tools(lines, 2, 600)  # 1,202 entries on its own
    _write(folder, SID, lines)
    reader = Catalog(home, VAULT)

    page = reader.history(SID)

    assert _prompts(page.events) == ["Question 2"]
    assert len(page.events) > PAGE_ENTRIES
    older = reader.history(SID, before_uuid=page.next_before_uuid)
    assert _prompts(older.events) == ["Question 1"]
    assert older.next_before_uuid is None


def test_a_background_agents_close_survives_a_page_boundary(tmp_path: Path) -> None:
    """A background agent's tool_result says async_launched and leaves its routing row open;
    what closes it is the <task-notification> injected whenever it finishes, however many turns
    later. That entry starts no turn, so the launch and the close can land on different pages —
    and a Mapper only closes a row whose launch it saw. The live path emits subagent.done for
    these same entries, so the history path must too."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    tool_id = "toolu_bg"
    use = {
        "type": "tool_use",
        "id": tool_id,
        "name": "Agent",
        "input": {"subagent_type": "Explore", "description": "find it"},
    }
    launch = _assistant(_uuid(2), _uuid(1), "", 2) | {
        "message": {"id": "msg_1", "role": "assistant", "content": [use]}
    }
    launched = _user(_uuid(3), _uuid(2), "", 3) | {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "launched"}],
        },
        "toolUseResult": {"isAsync": True, "status": "async_launched"},
    }
    lines: list[dict[str, Any]] = [
        _user(_uuid(1), None, "Question 1", 1),
        launch,
        launched,
        _assistant(_uuid(4), _uuid(3), "launched.", 4),
    ]
    parent = _uuid(4)
    for n in range(2, 23):  # 21 more turns, so the launch falls off the newest page
        prompt, reply = _uuid(n * 100), _uuid(n * 100 + 1)
        lines += [
            _user(prompt, parent, f"Question {n}", 5),
            _assistant(reply, prompt, f"Answer {n}", 6),
        ]
        parent = reply
    lines.append(
        _user(
            _uuid(9_999),
            parent,
            f"<task-notification><tool-use-id>{tool_id}</tool-use-id>"
            "<status>completed</status></task-notification>",
            7,
        )
    )
    _write(folder, SID, lines)
    reader = Catalog(home, VAULT)

    page = reader.history(SID)
    older = reader.history(SID, before_uuid=page.next_before_uuid)

    assert "Question 1" not in _prompts(page.events)  # the launch is on the older page
    assert [e.TYPE for e in older.events if e.TYPE.startswith(("subagent.", "tool."))] == [
        "subagent.started"
    ]
    closes = [e for e in page.events if e.TYPE == "subagent.done"]
    assert [e.block_id for e in closes] == [tool_id]  # type: ignore[attr-defined]


# --- memory: row-building does not retain a full parsed-entries list ---------------------------


def test_sessions_row_build_stays_close_to_the_files_own_size(tmp_path: Path) -> None:
    """Row-building streams the file in one pass and keeps no entries list (see the module
    docstring's memory note): peak allocation while building rows must stay close to the file's
    own size, not multiples of it. A two-copy build (a full `entries` list of parsed dicts,
    plus a `raw.splitlines()` duplicate of the raw bytes) measures ~7-8x file size for a
    transcript this shape; the streaming scan measures ~1x — this pins the difference with a
    generous margin so it isn't sensitive to normal allocator/interpreter variance."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    lines: list[dict[str, Any]] = []
    for i in range(4000):
        n = i % 60
        lines.append(_user(f"u{i}", f"a{i - 1}" if i else None, f"question {i} " + "pad " * 5, n))
        lines.append(_assistant(f"a{i}", f"u{i}", f"answer {i} " + "pad " * 5, n))
    path = _write(folder, SID, lines)
    file_size = path.stat().st_size
    catalog = Catalog(home, VAULT)

    tracemalloc.start()
    try:
        rows = catalog.sessions()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(rows) == 1
    assert peak < file_size * 3


def test_search_scan_stays_close_to_the_files_own_size(tmp_path: Path) -> None:
    """`_search_transcript` scans lines via `_iter_lines` (`io.BytesIO`, not `raw.splitlines()` —
    see the module docstring's memory note): peak allocation while scanning for a match must stay
    close to the file's own size, not multiples of it. Measured for a transcript this shape: ~1x
    with the `io.BytesIO` scan, ~2.2x with a `raw.splitlines()` scan (a second full-size copy of
    `raw`, as a list of line objects) — the 1.5x threshold sits between the two with margin on
    each side, so it isn't sensitive to normal allocator/interpreter variance."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    lines: list[dict[str, Any]] = []
    for i in range(4000):
        n = i % 60
        lines.append(_user(f"u{i}", f"a{i - 1}" if i else None, f"question {i} " + "pad " * 5, n))
        lines.append(_assistant(f"a{i}", f"u{i}", f"answer {i} " + "pad " * 5, n))
    path = _write(folder, SID, lines)
    file_size = path.stat().st_size
    catalog = Catalog(home, VAULT)

    tracemalloc.start()
    try:
        hits = catalog.search("question 3999")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert hits  # the needle is real: a false-positive-free test would hide a broken scan
    assert peak < file_size * 1.5


# --- corrupt lines are never an error state -------------------------------------------


def test_malformed_lines_never_become_an_error_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt lines are the ordinary state of a transcript being written, and also of a tail
    window, which begins inside one by construction. They cost one entry each and nothing else:
    no failed read, no omitted count, no partial listing. A file that is nothing but corruption is
    simply not a session — which is a known answer, not a gap in one."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    good = [
        _user(_uuid(1), None, "one", 1),
        _assistant(_uuid(2), _uuid(1), "ans", 2),
        _user(_uuid(3), _uuid(2), "two", 3),
    ]
    junk_line = "{not json " * 5 + "\n"
    lines = "".join(
        [junk_line] * 40
        + [json.dumps(entry, ensure_ascii=False) + "\n" for entry in good]
        + [junk_line] * 60
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{SID}.jsonl").write_bytes(lines.encode("utf-8"))
    junk = _uuid(99)
    (folder / f"{junk}.jsonl").write_bytes(b"{not json\n" * 10)

    whole = Catalog(home, VAULT)
    listing = whole.list_sessions()
    assert (listing.state, listing.reason, listing.omitted) == ("ready", "", 0)
    assert [row.session_id for row in listing.sessions] == [SID]  # the junk file is no session
    page = whole.history(SID)
    assert (page.status, page.reason) == ("complete", "")
    assert _prompts(page.events) == ["one", "two"]

    raw = (folder / f"{SID}.jsonl").read_bytes()
    # A window that begins 20 bytes into a corrupt line, with more corruption on both sides.
    start = raw.index(json.dumps(good[2], ensure_ascii=False).encode("utf-8")) - 20
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", start)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", len(raw) - start)
    monkeypatch.setattr(catalog_module, "HEAD_WINDOW_BYTES", start)
    windowed = Catalog(home, VAULT)
    assert windowed.list_sessions().state == "ready"
    tail = windowed.history(SID)
    assert (tail.status, tail.reason) == ("partial", "tail_budget")
    assert _prompts(tail.events) == ["two"]  # the window's own good entry, the junk skipped


def test_a_line_nested_deeper_than_json_can_parse_costs_one_entry(tmp_path: Path) -> None:
    """Past the interpreter's recursion allowance json.loads raises RecursionError, which is not a
    ValueError: a single ~40 KB line of nested brackets — a tool result that echoed one — used to
    take the listing, the search and the history of that session down with it, so /api/sessions
    and /api/search answered 500 on every poll until the user found the file by hand. It is
    corruption like any other: one entry lost, no error state."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True, exist_ok=True)
    good = [
        _user(_uuid(1), None, "one", 1),
        _assistant(_uuid(2), _uuid(1), "ans", 2),
        _user(_uuid(3), _uuid(2), "two", 3),
    ]
    # Valid JSON, and the needle sits inside it, so the search's line prefilter hands this line to
    # json.loads instead of skipping it — the only way to reach the scan's third parse site.
    deep = "[" * 20_000 + '"deepneedle"' + "]" * 20_000
    lines = [json.dumps(good[0], ensure_ascii=False), deep] + [
        json.dumps(entry, ensure_ascii=False) for entry in good[1:]
    ]
    (folder / f"{SID}.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    reader = Catalog(home, VAULT)
    listing = reader.list_sessions()
    assert (listing.state, listing.reason, listing.omitted) == ("ready", "", 0)
    assert [(row.session_id, row.title) for row in listing.sessions] == [(SID, "one")]
    page = reader.history(SID)
    assert (page.status, page.reason) == ("complete", "")
    assert _prompts(page.events) == ["one", "two"]
    found = reader.run_search("deepneedle")
    assert (found.state, found.hits) == ("ready", [])
    assert [hit.snippet for hit in reader.run_search("two").hits] == ["two"]


# --- this app's entrypoint counts as "ours" ------------------------------------------------------


def test_main_entrypoints_read_this_app_as_ours(tmp_path: Path) -> None:
    """Sessions this app starts carry "viki-agent" and read as "ours", never as "other". An
    entrypoint that was never this app (the SDK driver) still reads as "other", so the list
    cannot widen into hiding genuinely foreign sessions."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    entrypoints = ("viki-agent", "sdk-py")
    for n, entrypoint in enumerate(entrypoints):
        u1 = _user(f"u{n}", None, f"one {n}", 1, entrypoint=entrypoint)
        a1 = _assistant(f"a{n}", f"u{n}", "ans", 2, entrypoint=entrypoint)
        _write(folder, _uuid(n), [u1, a1])

    rows = {row.entrypoint: row.other for row in Catalog(home, VAULT).sessions()}

    assert rows["viki-agent"] is False
    assert rows["sdk-py"] is True


# --- opt-in: timing against the real vault (-m vault) -------------------------------------------


@pytest.mark.vault
def test_real_vault_timing() -> None:
    """Timing only, never content: a cold sessions list, the newest session's last page, one
    search, each within a bound the UI can wait for."""
    cfg = config.load()
    root = config.vault_path(os.environ)
    if root is None or not root.is_dir():
        pytest.skip("set VIKI_VAULT to a vault to run this check")
    catalog = Catalog(cfg.claude_home, root)

    def seconds(call: Callable[[], object]) -> float:
        start = time.perf_counter()
        call()
        return time.perf_counter() - start

    rows: list[SessionRow] = []
    assert seconds(lambda: rows.extend(catalog.sessions())) < 3.0
    assert rows
    assert seconds(lambda: catalog.history(rows[0].session_id)) < 1.5
    assert seconds(lambda: catalog.search("markdown")) < 1.5


@pytest.mark.parametrize("field", ["first_prompt", "custom_title", "entrypoint"])
def test_an_aligned_rewrite_plus_append_matches_a_cold_row(tmp_path: Path, field: str) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    original = [_user("u1", None, "old", 1)]
    replacement = [_user("u1", None, "new", 1)]
    if field == "custom_title":
        original.append({"type": "custom-title", "customTitle": "old title"})
        replacement.append({"type": "custom-title", "customTitle": "new title"})
    if field == "entrypoint":
        replacement[0]["entrypoint"] = "api"  # same byte length, outside MAIN_ENTRYPOINTS
    path = _write(folder, SID, original)
    boundary = path.stat().st_size
    reader = Catalog(home, VAULT)
    reader.sessions()
    _write(folder, SID, replacement)
    assert path.stat().st_size == boundary
    _append(folder, SID, [_assistant("a1", "u1", "later reply", 2)])
    assert path.read_bytes()[boundary - 1 : boundary] == b"\n"

    cold = Catalog(home, VAULT)
    assert reader.sessions() == cold.sessions()
    assert reader.search("new") == cold.search("new")


@pytest.mark.parametrize("operation", ["list", "search"])
@pytest.mark.parametrize("change", ["grow", "shrink"])
def test_a_changed_read_is_bounded_and_reports_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, change: str
) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    path = _write(
        folder, SID, [_user("u1", None, "initial", 1), _assistant("a1", "u1", "reply", 2)]
    )
    size = path.stat().st_size
    reader = Catalog(home, VAULT)
    if operation == "search":
        reader.sessions()  # the race belongs to the body read, after the listing's cache hit
    entries = (
        [_user("u1", None, "initial", 1), _assistant("a1", "u1", "x" * 8192 + " needle", 2)]
        if change == "grow"
        else [_user("u2", None, "replacement needle", 3)]
    )
    changed = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 1024)
    if change == "grow":
        assert size < catalog_module.FULL_READ_BYTES < len(changed)
    else:
        assert len(changed) < size
    starts: list[int] = []
    requested: list[int] = []
    received: list[int] = []
    handles: list[Any] = []
    opener = Path.open
    changed_once = False

    class ObservedRead:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> ObservedRead:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.handle.close()

        def seek(self, start: int) -> int:
            starts.append(start)
            return self.handle.seek(start)

        def read(self, length: int = -1) -> bytes:
            requested.append(length)
            raw = self.handle.read(length)
            received.append(len(raw))
            return raw

    def racing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal changed_once
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            if not changed_once:
                changed_once = True
                path.write_bytes(changed)
            handle = opener(self, *args, **kwargs)
            handles.append(handle)
            return ObservedRead(handle)
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    result = reader.list_sessions() if operation == "list" else reader.run_search("needle")

    assert starts == [0]
    assert requested == [size]  # one captured-size read, including when the new file is larger
    assert received == [min(size, len(changed))]
    assert handles and all(handle.closed for handle in handles)
    assert (result.state, result.reason) == ("partial", "read_failed")
    if operation == "list":
        assert result.omitted == 1 and result.sessions == []  # type: ignore[union-attr]
    else:
        assert result.hits == [] and result.scanned == 0  # type: ignore[union-attr]
        assert result.total == 1  # type: ignore[union-attr]
    cached = reader._row_cache.get(SID)  # noqa: SLF001 - no torn observation may be cached
    fresh = path.stat()
    assert cached is None or cached[:2] != (fresh.st_size, fresh.st_mtime_ns)
    monkeypatch.undo()
    assert reader.list_sessions().state == "ready"  # an explicit later observation succeeds
    assert reader.run_search("needle").hits


def test_an_unchanged_row_cache_hit_opens_no_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    path = _write(project_dir(home, VAULT), SID, [_user("u1", None, "one", 1)])
    reader = Catalog(home, VAULT)
    before = reader.sessions()
    opener = Path.open

    def no_transcript_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        assert self != path
        return opener(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_transcript_open)
    assert reader.sessions() == reader.sessions() == before


def test_search_reserves_the_tail_boundary_probe_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    path = _write(project_dir(home, VAULT), SID, [_user("u1", None, "one", 1)])
    reader = Catalog(home, VAULT)
    reader.sessions()
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", 16)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 32)
    monkeypatch.setattr(catalog_module, "SEARCH_SCAN_BYTES", 32)
    reads: list[tuple[int, int]] = []
    window = catalog_module._window

    def counted(path: Path, start: int, length: int) -> bytes:
        reads.append((start, length))
        return window(path, start, length)

    monkeypatch.setattr(catalog_module, "_window", counted)
    result = reader.run_search("needle")
    assert (result.state, result.reason, result.scanned) == ("partial", "scan_budget", 0)
    assert reads == []
    monkeypatch.setattr(catalog_module, "SEARCH_SCAN_BYTES", 33)
    allowed = reader.run_search("needle")
    assert (allowed.state, allowed.reason, allowed.scanned) == ("partial", "file_budget", 1)
    assert reads == [(path.stat().st_size - 33, 33)]
