from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.types import Message, Scope

from viki_agent import catalog as catalog_module
from viki_agent.catalog import Catalog, project_dir
from viki_agent.conversations import conversation_search as search_module
from viki_agent.conversations.artifact_details import ArtifactDetails
from viki_agent.conversations.conversation_search import (
    SEGMENT_CHECK,
    Budget,
    ConversationSearch,
    _folded_segments,
    _may_yield_text,
    _segments,
    match_snapshot,
    match_span,
)
from viki_agent.conversations.conversation_search_api import routes

SID = str(uuid.UUID(int=1000))
RID = str(uuid.UUID(int=2000))
BASE = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer synthetic", "Origin": BASE}
# A seconds-long guard on an event that normally fires in milliseconds: an admission regression
# that lets an over-limit request through must fail fast, not hang the suite.
HANG_GUARD_S = 5
UNBOUNDED = Budget(math.inf, lambda: False, lambda: 0.0)
FIXTURES = Path(__file__).parents[1] / "fixtures"


def make_catalog(tmp_path: Path, texts: list[str]) -> tuple[Catalog, Path]:
    vault = Path("C:/SyntheticVault")
    path = project_dir(tmp_path, vault) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "uuid": str(uuid.UUID(int=i + 1)),
            "parentUuid": str(uuid.UUID(int=i)) if i else None,
            "type": "user",
            "timestamp": f"2026-09-21T00:00:{i % 60:02d}.000Z",
            "message": {"role": "user", "content": text},
        }
        for i, text in enumerate(texts)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return Catalog(tmp_path, vault), path


@pytest.mark.parametrize(
    ("text", "query", "match"),
    [
        ("a Straße z", "STRASSE", "Straße"),
        ("a ｔｅｓｔ z", "test", "ｔｅｓｔ"),
        ("a cafe\u0301 z", "café", "cafe\u0301"),
        ("a ß z", "ss", "ß"),
    ],
)
def test_unicode_snapshot_keeps_original_characters(text: str, query: str, match: str) -> None:
    assert match_snapshot(text, query, UNBOUNDED) == {"before": "a ", "match": match, "after": " z"}


@pytest.mark.parametrize(
    ("text", "query", "span"),
    [
        ("x ｶﾞｲﾄﾞ y", "ガイド", (2, 7)),
        ("x ＡＢＣ y", "abc", (2, 5)),
        ("x Straße y", "strasse", (2, 8)),
        ("x 猫 y", "猫", (2, 3)),
        ("x absent y", "猫", None),
    ],
)
def test_match_span_gives_raw_offsets_of_the_folded_match(
    text: str, query: str, span: tuple[int, int] | None
) -> None:
    assert match_span(text, query, UNBOUNDED) == span


@pytest.mark.parametrize(
    ("text", "query", "match"),
    [
        ("ﾊﾞｸﾞ修正 完了", "完了", "完了"),
        ("ﾃﾞｰﾀﾍﾞｰｽの検索です", "検索", "検索"),
        ("a ¨ needle z", "needle", "needle"),
        ("a\u0316\u0301 needle", "needle", "needle"),
        ("x❤️y", "❤", "❤️"),
        ("xaﾞy", "a", "aﾞ"),
    ],
)
def test_nfkc_boundary_folds_keep_original_source_characters(
    text: str, query: str, match: str
) -> None:
    snapshot = match_snapshot(text, query, UNBOUNDED)
    assert snapshot is not None and snapshot["match"] == match


def test_a_long_whitespace_run_folds_a_bounded_amount_of_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folded_total = 0
    original = search_module._fold

    def counting_fold(value: str) -> str:
        nonlocal folded_total
        folded_total += len(value)
        return original(value)

    monkeypatch.setattr(search_module, "_fold", counting_fold)
    # The match spans the whitespace run, so the fold-tested walk covers the run itself, not
    # just the ASCII tail after it.
    text = "あ" + " " * 5000 + "needle"
    snapshot = match_snapshot(text, "あ needle", UNBOUNDED)
    assert snapshot is not None and snapshot["match"] == text[:256]
    assert folded_total < 100 * len(text)


def test_whitespace_and_sigma_keep_source_text() -> None:
    assert match_snapshot("a\n  b", "a  b", UNBOUNDED) == {
        "before": "",
        "match": "a\n  b",
        "after": "",
    }
    snapshot = match_snapshot("ΟΣ", "ος", UNBOUNDED)
    assert snapshot is not None and snapshot["match"] == "ΟΣ"


def write_transcript(tmp_path: Path, rows: list[dict[str, Any]]) -> Catalog:
    vault = Path("C:/SyntheticVault")
    path = project_dir(tmp_path, vault) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return Catalog(tmp_path, vault)


def test_a_cancelled_search_leaves_the_chain_cached_for_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"] * 3)
    parses = 0
    original = catalog_module._read_entries

    def counting_read(path: Path) -> list[dict[str, Any]]:
        nonlocal parses
        parses += 1
        return original(path)

    monkeypatch.setattr(catalog_module, "_read_entries", counting_read)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    assert service.search(SID, "needle", cancelled)["state"] == "superseded"
    result = service.search(SID, "needle")
    assert result["state"] == "ready" and len(result["hits"]) == 3
    assert parses == 1


def fake_clock(reads_at_zero: int) -> Callable[[], float]:
    """A clock that reads 0 this many times, then reads past the 2-second deadline for good."""
    ticks = iter([0.0] * reads_at_zero)
    return lambda: next(ticks, 5.0)


def test_a_search_past_its_deadline_answers_partial_with_the_hits_so_far(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"] * 10)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    # The budget's start and the first three entries read 0; every later read is past 2 s.
    result = service.search(SID, "needle", clock=fake_clock(4))
    assert (result["state"], result["reason"], result["coverage"]) == (
        "partial",
        "time_budget",
        "complete",
    )
    assert len(result["hits"]) == 3


def test_a_slow_chain_wait_does_not_spend_the_search_budget(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"] * 3)
    now = 0.0

    def slow_resolve(sid: str) -> tuple[str, None]:
        nonlocal now
        now += 5.0  # the wait for a cold parse outlasts the whole budget
        return sid, None

    result = ConversationSearch(catalog, slow_resolve).search(SID, "needle", clock=lambda: now)
    assert (result["state"], result["reason"], len(result["hits"])) == ("ready", "", 3)


def test_an_anchor_past_its_deadline_is_a_typed_error_in_either_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["old", "needle", "new"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    hit = service.search(SID, "needle")["hits"][0]
    maps: list[bool] = []
    original = search_module._map_entry

    def spy_map(mapper: Any, entry: dict[str, Any], *, source_revision: str = "") -> Any:
        maps.append(bool(source_revision))  # only the window pass passes a revision
        return original(mapper, entry, source_revision=source_revision)

    monkeypatch.setattr(search_module, "_map_entry", spy_map)
    # The budget's start reads the first tick. One tick: the classification pass's first entry
    # is past the deadline, so only the revision check has mapped anything. Four ticks: the three
    # classification checks pass and the window pass's first entry is past it, unmapped.
    for passing, mapped in ((1, [False]), (4, [False] * 4)):
        maps.clear()
        page = service.anchor(SID, hit["anchor"], clock=fake_clock(passing))
        assert (page["state"], page["reason"], page["events"]) == ("error", "time_budget", [])
        assert maps == mapped


@pytest.mark.parametrize("filler", ["a", "\u3042"])
def test_a_search_is_cancelled_inside_one_huge_text(tmp_path: Path, filler: str) -> None:
    catalog, _ = make_catalog(tmp_path, [filler * 1_000_000 + " needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    result = service.search(SID, "needle", cancelled)
    assert result["state"] == "superseded" and result["hits"] == []
    # One check before the chain and one for the entry: the other three ran inside the text.
    assert calls == 5


@pytest.mark.parametrize(
    "text",
    [
        # Each U+FDFA folds to 18 code points, and NFKC's cost on such text grows quadratically.
        "ﷺ" * (4 * SEGMENT_CHECK),
        # NFKC reorders a run of marks in quadratic time.
        "a" + "̖́" * (2 * SEGMENT_CHECK),
    ],
)
def test_a_hostile_text_is_folded_in_bounded_chunks_between_budget_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    catalog, _ = make_catalog(tmp_path, [text])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    folded: list[int] = []
    original = search_module._fold

    def measuring_fold(value: str) -> str:
        folded.append(len(value))
        return original(value)

    monkeypatch.setattr(search_module, "_fold", measuring_fold)
    # The budget's start and the entry's check read 0; the next check, inside the text, is past it.
    result = service.search(SID, "zz", clock=fake_clock(2))
    assert (result["state"], result["reason"], result["hits"]) == ("partial", "time_budget", [])
    assert max(folded) <= 2 * SEGMENT_CHECK


def test_a_run_of_marks_is_cut_into_bounded_segments() -> None:
    text = "a" + "̖́" * (2 * SEGMENT_CHECK)
    spans = list(_segments(text, UNBOUNDED))
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert max(right - left for left, right in spans) <= SEGMENT_CHECK


def test_the_unit_walk_checks_the_budget_inside_one_long_segment() -> None:
    # Two chunks, so the locate pass reads the clock once; the match then spans a whitespace run
    # that the non-ASCII unit walk keeps as one segment, and only its own checks can stop it.
    text = "é" + "x" + " " * (SEGMENT_CHECK * 3) + "y"
    with pytest.raises(TimeoutError):
        match_snapshot(text, "x y", Budget(2.0, lambda: False, fake_clock(1)))


def whole_text_snapshot(text: str, query: str) -> dict[str, str] | None:
    """The reference: fold the whole text at once, then map the match back unit by unit."""
    needle = search_module._fold(query)
    found = search_module._fold(text).find(needle)
    if found < 0:
        return None
    offset = 0
    start = None
    for left, right in _segments(text, UNBOUNDED):
        offset += len(search_module._fold(text[left:right]))
        if start is None and offset > found:
            start = left
        if start is not None and offset >= found + len(needle):
            return {
                "before": text[max(0, start - 120) : start],
                "match": text[start : min(right, start + 256)],
                "after": text[right : right + 120],
            }
    return None


@pytest.mark.parametrize(
    "text",
    [
        # Numbered, so that a query cut from one place is not found earlier in the text.
        "".join(f"ﾃﾞｰﾀ{i}ﾍﾞｰｽの検索 café Straße ｔｅｓｔ ¨ ΟΣ a\n\t  b " for i in range(120)),
        "".join(f"word{i}, another word. " for i in range(300)),
        "あ" * 1000 + " 　\n" * 700 + "ｶﾞ x y",
    ],
)
def test_chunked_folding_gives_the_whole_text_snapshot(text: str) -> None:
    # Queries cut from the text itself, starting on both sides of the first two chunk cuts, so
    # that a match begins in one chunk and ends in the next.
    snapshots = [
        (match_snapshot(text, query, UNBOUNDED), whole_text_snapshot(text, query))
        for begin in [
            *range(SEGMENT_CHECK - 60, SEGMENT_CHECK + 20),
            *range(2 * SEGMENT_CHECK - 40, 2 * SEGMENT_CHECK + 40, 3),
        ]
        if (query := text[begin : begin + 45].strip())
    ]
    snapshots.append(
        (match_snapshot(text, "あ ガ x", UNBOUNDED), whole_text_snapshot(text, "あ ガ x"))
    )
    assert [chunked for chunked, whole in snapshots if chunked != whole] == []
    assert sum(whole is not None for _chunked, whole in snapshots) > len(snapshots) // 2
    assert match_snapshot(text, "absent", UNBOUNDED) is None


def test_a_conjoining_pair_at_the_first_chunk_cut_still_matches() -> None:
    # U+1100 U+1161 compose to "가" under NFKC; putting the pair right at the first candidate
    # cut (SEGMENT_CHECK code points in) checks that the cut loop still joins them.
    text = "x" * 1023 + "가" + " tail"
    query = "가"
    assert match_snapshot(text, query, UNBOUNDED) == whole_text_snapshot(text, query)
    assert match_snapshot(text, query, UNBOUNDED) is not None


def test_the_ascii_fast_path_gives_the_folded_walks_units() -> None:
    ascii_chars = [chr(code) for code in range(128)]
    for pair in map("".join, itertools.product(ascii_chars, repeat=2)):
        assert list(_segments(pair, UNBOUNDED)) == list(_folded_segments(pair, UNBOUNDED)), pair
    whitespace = " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f"
    for text in [
        "x" + whitespace * 5 + "y",
        whitespace + "a" + whitespace[::-1] * 3 + "b",
        "a" + " " * 40 + "b\t\n" + "\x1f" * 33 + "c" + "d e" * 700,
        *(f"a{space * 40}b{space}c" for space in whitespace),
    ]:
        assert list(_segments(text, UNBOUNDED)) == list(_folded_segments(text, UNBOUNDED))


def test_the_prefilter_skips_only_entries_that_map_to_no_searchable_text(tmp_path: Path) -> None:
    service = ConversationSearch(Catalog(tmp_path, Path("C:/SyntheticVault")), lambda s: (s, None))
    lines = [
        json.loads(line)
        for path in sorted(FIXTURES.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entries = [line.get("frame", line) for line in lines if isinstance(line, dict)]
    entries += [
        {"type": "user", "message": {"content": [{"type": "image", "source": {}}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "typed"}]}},
        {"type": "user", "message": "not an object"},
        {"type": "assistant", "message": {"id": "m", "content": "not a list"}},
        {"type": "assistant", "message": {"id": "m", "content": [{"type": "thinking"}]}},
        {"type": "system", "message": {"content": "system text"}},
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "go"},
        },
        {"type": "attachment", "attachment": {"type": "instructions", "files": []}},
    ]
    entries = [
        entry
        for entry in entries
        if entry.get("type") in {"user", "assistant", "attachment", "system"}
    ]
    assert any(service._texts(entry) for entry in entries)
    assert any(not _may_yield_text(entry) for entry in entries)
    assert [e for e in entries if service._texts(e) and not _may_yield_text(e)] == []


def test_a_folded_steer_is_found_by_in_chat_search(tmp_path: Path) -> None:
    # catalog._build_chain's leaf must be a "user" or "assistant" entry (its own rule, not
    # this package's); a trailing assistant entry anchors one so the attachment ancestor is
    # still walked into the chain, exactly as a steer folded into a real running turn would be.
    rows: list[dict[str, Any]] = [
        {
            "uuid": str(uuid.UUID(int=1)),
            "parentUuid": None,
            "type": "attachment",
            "timestamp": "2026-09-21T00:00:00.000Z",
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "prompt": "find the needle while running",
            },
        },
        {
            "uuid": str(uuid.UUID(int=2)),
            "parentUuid": str(uuid.UUID(int=1)),
            "type": "assistant",
            "timestamp": "2026-09-21T00:00:01.000Z",
            "message": {"id": "m1", "content": []},
        },
    ]
    service = ConversationSearch(write_transcript(tmp_path, rows), lambda sid: (sid, None))
    result = service.search(SID, "needle")
    assert len(result["hits"]) == 1
    assert result["hits"][0]["role"] == "user"


def test_a_lone_oversized_target_stops_mapping_at_the_entry_that_overflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "uuid": str(uuid.UUID(int=1)),
            "parentUuid": None,
            "type": "user",
            "timestamp": "2026-09-21T00:00:00.000Z",
            "message": {"role": "user", "content": "needle"},
        },
        *(
            {
                "uuid": str(uuid.UUID(int=i)),
                "parentUuid": str(uuid.UUID(int=i - 1)),
                "type": "assistant",
                "timestamp": "2026-09-21T00:00:01.000Z",
                "message": {"id": f"m{i}", "content": [{"type": "text", "text": "x" * 200}]},
            }
            for i in range(2, 52)
        ),
    ]
    service = ConversationSearch(write_transcript(tmp_path, rows), lambda sid: (sid, None))
    hit = service.search(SID, "needle")["hits"][0]
    window_maps = 0
    original = search_module._map_entry

    def counting_map(mapper: Any, entry: dict[str, Any], *, source_revision: str = "") -> Any:
        nonlocal window_maps
        window_maps += bool(source_revision)  # only the window pass passes a revision
        return original(mapper, entry, source_revision=source_revision)

    monkeypatch.setattr(search_module, "_map_entry", counting_map)
    monkeypatch.setattr(search_module, "MAX_ANCHOR_BYTES", 1024)
    page = service.anchor(SID, hit["anchor"])
    assert (page["state"], page["reason"]) == ("error", "too_large")
    # The prompt and one reply fit in the 512 bytes left; the second reply overflows them.
    assert window_maps == 3


def test_revision_v2_hashes_the_line_digest_role_and_text(tmp_path: Path) -> None:
    catalog, path = make_catalog(tmp_path, ["a needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    line = path.read_bytes().rstrip(b"\r\n")

    def revision(domain: bytes, source: bytes) -> str:
        digest = hashlib.sha256(domain)
        for value in (source, b"user", b"a needle"):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    anchor = service.search(SID, "needle")["hits"][0]["anchor"]
    v2 = revision(b"viki-conversation-search-v2\0", hashlib.sha256(line).hexdigest().encode())
    assert anchor["source_revision"] == v2
    assert service.search(SID, "needle")["hits"][0]["anchor"] == anchor
    assert service.anchor(SID, anchor)["state"] == "ready"
    # An anchor minted under v1, which hashed the raw line itself, now reads as a changed source.
    v1 = revision(b"viki-conversation-search-v1\0", line)
    page = service.anchor(SID, {**anchor, "source_revision": v1})
    assert (page["state"], page["reason"]) == ("changed", "source_changed")


def test_search_revisions_include_unclipped_text_and_detect_same_uuid_changes(
    tmp_path: Path,
) -> None:
    catalog, path = make_catalog(tmp_path, ["a" * 17000 + " needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    hit = service.search(SID, "needle")["hits"][0]
    assert service.anchor(SID, hit["anchor"])["state"] == "ready"
    path.write_text(path.read_text(encoding="utf-8").replace("needle", "change"), encoding="utf-8")
    # Same size as before: move the mtime so the catalog's (size, mtime_ns) cache sees the
    # rewrite even when both writes land in one clock tick.
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    assert service.anchor(SID, hit["anchor"])["state"] == "changed"


def test_hit_and_ascii_response_budgets_are_truthful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"] * 101)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    result = service.search(SID, "needle")
    assert len(result["hits"]) == 100
    assert (result["state"], result["reason"]) == ("partial", "response_budget")
    monkeypatch.setattr(search_module, "MAX_SEARCH_BYTES", 1000)
    result = service.search(SID, "needle")
    assert len(json.dumps(result)) + 128 <= 1000
    assert result["reason"] == "response_budget"


def test_anchor_shrinks_context_preserving_whole_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["old", "needle", "new"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    hit = service.search(SID, "needle")["hits"][0]
    monkeypatch.setattr(search_module, "MAX_ANCHOR_EVENTS", 1)
    page = service.anchor(SID, hit["anchor"])
    assert page["state"] == "ready" and len(page["events"]) == 1
    assert page["anchor_turn_index"] == 0
    assert page["older_before_uuid"] and page["newer_after_uuid"]
    monkeypatch.setattr(search_module, "MAX_ANCHOR_BYTES", 100)
    assert service.anchor(SID, hit["anchor"])["reason"] == "too_large"


def test_anchor_opens_a_whole_target_turn_over_a_thousand_entries(tmp_path: Path) -> None:
    vault = Path("C:/SyntheticVault")
    path = project_dir(tmp_path, vault) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "uuid": str(uuid.UUID(int=0)),
            "parentUuid": None,
            "type": "user",
            "isSidechain": False,
            "timestamp": "2026-09-21T00:00:00.000Z",
            "message": {"role": "user", "content": "start"},
        },
        *(
            {
                "uuid": str(uuid.UUID(int=i)),
                "parentUuid": str(uuid.UUID(int=i - 1)),
                "type": "progress",
                "isSidechain": False,
                "timestamp": "2026-09-21T00:00:01.000Z",
            }
            for i in range(1, 1201)
        ),
        {
            "uuid": str(uuid.UUID(int=1201)),
            "parentUuid": str(uuid.UUID(int=1200)),
            "type": "assistant",
            "isSidechain": False,
            "timestamp": "2026-09-21T00:00:01.000Z",
            "message": {"id": "m1", "content": [{"type": "text", "text": "needle"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    catalog = Catalog(tmp_path, vault)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    hit = service.search(SID, "needle")["hits"][0]
    page = service.anchor(SID, hit["anchor"])
    assert page["state"] == "ready" and page["reason"] == ""
    assert page["older_before_uuid"] is None and page["newer_after_uuid"] is None


def test_cancelled_search_discards_hits_and_resolver_applies_cutoff(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle", "needle"])
    service = ConversationSearch(catalog, lambda _: (SID, "2026-09-21T00:00:01.000Z"))
    assert len(service.search(RID, "needle")["hits"]) == 1
    assert service.search(SID, "needle", lambda: True)["state"] == "superseded"


def test_private_routes_validate_inputs_and_always_disable_caching(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
            artifacts=ArtifactDetails(catalog, service.resolve),
        )
    )
    with TestClient(app, base_url=BASE) as client:
        url = f"/api/v2/sessions/{SID}/conversation-search?q=needle&request_id={RID}"
        response = client.get(url, headers=HEADERS)
        assert response.status_code == 200 and len(response.json()["hits"]) == 1
        anchor = response.json()["hits"][0]["anchor"]
        assert (
            client.post(
                f"/api/v2/sessions/{SID}/history-anchor",
                headers=HEADERS,
                json={"request_id": RID, "anchor": anchor},
            ).json()["state"]
            == "ready"
        )
        for response in [
            client.get(url),
            client.get(url + "&q=extra", headers=HEADERS),
            client.post(
                f"/api/v2/sessions/{SID}/history-anchor",
                headers=HEADERS,
                content='{"request_id":NaN}',
            ),
            client.post(
                f"/api/v2/sessions/{SID}/history-anchor",
                headers={**HEADERS, "Content-Encoding": "gzip"},
                json={"request_id": RID, "anchor": anchor},
            ),
        ]:
            assert response.status_code in {400, 401}
            assert response.headers["cache-control"] == "no-store"


def test_sidechain_entries_are_excluded_from_search(tmp_path: Path) -> None:
    vault = Path("C:/SyntheticVault")
    path = project_dir(tmp_path, vault) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "uuid": str(uuid.UUID(int=1)),
            "parentUuid": None,
            "type": "user",
            "isSidechain": False,
            "timestamp": "2026-09-21T00:00:00.000Z",
            "message": {"role": "user", "content": "start"},
        },
        {
            "uuid": str(uuid.UUID(int=2)),
            "parentUuid": str(uuid.UUID(int=1)),
            "type": "user",
            "isSidechain": True,
            "timestamp": "2026-09-21T00:00:01.000Z",
            "message": {"role": "user", "content": "needle"},
        },
        {
            "uuid": str(uuid.UUID(int=3)),
            "parentUuid": str(uuid.UUID(int=2)),
            "type": "user",
            "isSidechain": False,
            "timestamp": "2026-09-21T00:00:02.000Z",
            "message": {"role": "user", "content": "end"},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    catalog = Catalog(tmp_path, vault)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    assert service.search(SID, "needle")["hits"] == []


def test_post_history_anchor_without_origin_header_is_forbidden(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
        )
    )
    with TestClient(app, base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/history-anchor",
            headers={"Authorization": "Bearer synthetic"},
            json={
                "request_id": RID,
                "anchor": {
                    "kind": "native_entry",
                    "entry_uuid": SID,
                    "mapped_index": 0,
                    "source_revision": "a" * 64,
                },
            },
        )
        assert response.status_code == 403


def test_authorized_get_with_a_disallowed_origin_is_forbidden(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
        )
    )
    with TestClient(app, base_url=BASE) as client:
        response = client.get(
            f"/api/v2/sessions/{SID}/conversation-search?q=needle&request_id={RID}",
            headers={"Authorization": "Bearer synthetic", "Origin": "https://evil.test"},
        )
        assert response.status_code == 403


def test_a_query_whose_nfkc_form_expands_past_the_raw_bound_is_accepted(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
        )
    )
    # The raw query is 250 characters, inside MAX_QUERY (256); its NFKC form ("ffi" per ligature)
    # is 750, which the dropped post-NFKC re-check used to refuse.
    query = "ﬃ" * 250
    with TestClient(app, base_url=BASE) as client:
        response = client.get(
            f"/api/v2/sessions/{SID}/conversation-search",
            params={"q": query, "request_id": RID},
            headers=HEADERS,
        )
    assert response.status_code == 200


def test_crlf_line_endings_are_read_and_hashed_correctly(tmp_path: Path) -> None:
    vault = Path("C:/SyntheticVault")
    row = {
        "uuid": str(uuid.UUID(int=1)),
        "parentUuid": None,
        "type": "user",
        "isSidechain": False,
        "timestamp": "2026-09-21T00:00:00.000Z",
        "message": {"role": "user", "content": "needle"},
    }
    line = json.dumps(row).encode("utf-8")
    revisions = {}
    for label, ending in [("lf", b"\n"), ("crlf", b"\r\n")]:
        root = tmp_path / label
        path = project_dir(root, vault) / f"{SID}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(line + ending)
        catalog = Catalog(root, vault)
        service = ConversationSearch(catalog, lambda sid: (sid, None))
        hit = service.search(SID, "needle")["hits"][0]
        assert service.anchor(SID, hit["anchor"])["state"] == "ready"
        revisions[label] = hit["anchor"]["source_revision"]
    # The contract hashes the line with its CR/LF terminator removed: a CRLF copy and an LF copy
    # of the same line must hash identically. A hash that left the CR attached would break this.
    assert revisions["lf"] == revisions["crlf"]


def test_lone_surrogate_text_is_searched_without_crashing(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["before \ud800 needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    result = service.search(SID, "needle")
    assert len(result["hits"]) == 1
    assert service.anchor(SID, result["hits"][0]["anchor"])["state"] == "ready"


def test_tail_only_coverage_still_finds_a_hit_in_the_kept_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, path = make_catalog(tmp_path, ["old"] * 5 + ["needle"])
    raw = path.read_bytes()
    monkeypatch.setattr(catalog_module, "FULL_READ_BYTES", len(raw) - 1)
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", len(raw) - raw.rindex(b"\n", 0, -1))
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    result = service.search(SID, "needle")
    assert result["coverage"] == "tail" and len(result["hits"]) == 1


def test_missing_and_unreadable_sessions_are_typed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    missing_id = str(uuid.UUID(int=9999))
    assert service.search(missing_id, "needle") == {
        "hits": [],
        "state": "error",
        "reason": "transcript_missing",
        "coverage": "complete",
    }

    stat = Path.stat

    def denied_stat(self: Path, **kwargs: Any) -> os.stat_result:
        if self.name == f"{SID}.jsonl":
            raise PermissionError(5, "denied")
        return stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    assert service.search(SID, "needle")["reason"] == "read_failed"


async def test_search_requests_over_admission_are_refused_with_429(tmp_path: Path) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
        )
    )
    entered: asyncio.Queue[None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    original = ConversationSearch.search

    def blocked(
        self: ConversationSearch,
        session_id: str,
        query: str,
        cancelled=lambda: False,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        loop.call_soon_threadsafe(entered.put_nowait, None)
        release.wait()
        return original(self, session_id, query, cancelled, clock=clock)

    import viki_agent.conversations.conversation_search as cs_module

    orig = cs_module.ConversationSearch.search
    cs_module.ConversationSearch.search = blocked
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            active = [
                asyncio.create_task(
                    client.get(
                        f"/api/v2/sessions/{SID}/conversation-search"
                        f"?q=needle&request_id={str(uuid.UUID(int=i + 1))}",
                        headers=HEADERS,
                    )
                )
                for i in range(2)
            ]
            try:
                for _ in active:
                    await asyncio.wait_for(entered.get(), 1.0)
                async with asyncio.timeout(HANG_GUARD_S):
                    rejected = await client.get(
                        f"/api/v2/sessions/{SID}/conversation-search?q=needle&request_id={RID}",
                        headers=HEADERS,
                    )
                assert rejected.status_code == 429
            finally:
                release.set()
                await asyncio.gather(*active, return_exceptions=True)
    finally:
        cs_module.ConversationSearch.search = orig


def probe_app(service: ConversationSearch, artifacts: ArtifactDetails | None = None) -> Starlette:
    return Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
            artifacts=artifacts,
        )
    )


def start_request(
    app: Starlette,
    method: str,
    path: str,
    gone: asyncio.Event,
    *,
    query: bytes = b"",
    body: bytes | None = None,
) -> tuple[asyncio.Task[None], list[Message]]:
    """Runs one request through the ASGI app. Its body (if any) arrives at once, and it
    disconnects once `gone` is set."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body or b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    output: list[Message] = []

    async def send(message: Message) -> None:
        output.append(message)

    headers = [
        (b"host", b"127.0.0.1:8765"),
        (b"authorization", b"Bearer synthetic"),
        (b"origin", BASE.encode()),
    ]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": headers,
    }
    return asyncio.create_task(app(scope, receive, send)), output


async def third_of_three_edits_is_admitted(
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    start: Callable[[int, asyncio.Event], tuple[asyncio.Task[None], list[Message]]],
) -> dict[str, Any]:
    """Three debounced edits against a cold chain: the first holds the parse, the second waits
    for it, and both are superseded. The third must be admitted (200) rather than refused,
    whether it is queued behind the still-running parse or arrives after it finishes. Returns the
    third's decoded body.
    test_catalog_budgets.py::test_concurrent_reads_of_one_file_share_one_parse pins that a
    request queued behind a parse is served from that parse rather than starting its own."""
    loop = asyncio.get_running_loop()
    parsing = asyncio.Event()
    queued: asyncio.Queue[None] = asyncio.Queue()
    resume = threading.Event()
    read = catalog_module._read_entries
    permit = catalog._parse_permit

    class AnnouncedPermit:
        """The parse permit, announcing each caller that finds it taken."""

        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            acquired = permit.acquire(blocking, timeout)
            if not blocking and not acquired:
                loop.call_soon_threadsafe(queued.put_nowait, None)
            return acquired

        def release(self) -> None:
            permit.release()

    def held_read(path: Path) -> list[dict[str, Any]]:
        loop.call_soon_threadsafe(parsing.set)
        resume.wait()  # released in the finally below
        return read(path)

    monkeypatch.setattr(catalog, "_parse_permit", AnnouncedPermit())
    monkeypatch.setattr(catalog_module, "_read_entries", held_read)
    # Longer than the guard, so only the cancellation, not the wait's expiry, can free a slot.
    monkeypatch.setattr(catalog_module, "PARSE_WAIT_S", 2 * HANG_GUARD_S)

    gone = [asyncio.Event() for _ in range(3)]
    tasks: list[asyncio.Task[None]] = []
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            first, _ = start(1, gone[0])
            tasks.append(first)
            await parsing.wait()
            second, _ = start(2, gone[1])
            tasks.append(second)
            await queued.get()
        gone[0].set()
        gone[1].set()
        # The first still holds its slot while it parses, so only the second's wait can end now.
        await asyncio.wait({second}, timeout=HANG_GUARD_S)
        third, output = start(3, gone[2])
        tasks.append(third)
        # Admitted and queued behind the parse, or refused at once: whichever comes first.
        arrival = asyncio.create_task(queued.get())
        await asyncio.wait(
            {third, arrival}, timeout=HANG_GUARD_S, return_when=asyncio.FIRST_COMPLETED
        )
        arrival.cancel()
        resume.set()
        async with asyncio.timeout(HANG_GUARD_S):
            await third
        response_start = next(m for m in output if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in output if m["type"] == "http.response.body")
        assert response_start["status"] == 200
        return json.loads(body)
    finally:
        resume.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_disconnected_request_waiting_on_a_cold_parse_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    app = probe_app(ConversationSearch(catalog, lambda sid: (sid, None)))

    def start(number: int, gone: asyncio.Event) -> tuple[asyncio.Task[None], list[Message]]:
        return start_request(
            app,
            "GET",
            f"/api/v2/sessions/{SID}/conversation-search",
            gone,
            query=f"q=needle&request_id={uuid.UUID(int=number)}".encode(),
        )

    body = await third_of_three_edits_is_admitted(catalog, monkeypatch, start)
    assert body["state"] == "ready"


async def test_a_disconnected_anchor_request_waiting_on_a_cold_parse_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_catalog(tmp_path, ["needle"])
    # The anchor comes from a second catalog, so `catalog` itself stays cold.
    warm = ConversationSearch(Catalog(tmp_path, catalog.vault), lambda sid: (sid, None))
    anchor = warm.search(SID, "needle")["hits"][0]["anchor"]
    app = probe_app(ConversationSearch(catalog, lambda sid: (sid, None)))

    def start(number: int, gone: asyncio.Event) -> tuple[asyncio.Task[None], list[Message]]:
        request = {"request_id": str(uuid.UUID(int=number)), "anchor": anchor}
        return start_request(
            app,
            "POST",
            f"/api/v2/sessions/{SID}/history-anchor",
            gone,
            body=json.dumps(request).encode(),
        )

    body = await third_of_three_edits_is_admitted(catalog, monkeypatch, start)
    assert body["state"] == "ready"


async def test_a_disconnected_artifact_detail_request_waiting_on_a_cold_parse_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, path = make_catalog(tmp_path, [])
    ref = write_tool_result_transcript(path)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    app = probe_app(service, ArtifactDetails(catalog, service.resolve))

    def start(number: int, gone: asyncio.Event) -> tuple[asyncio.Task[None], list[Message]]:
        request = {"request_id": str(uuid.UUID(int=number)), "ref": ref, "cursor": 0}
        return start_request(
            app,
            "POST",
            f"/api/v2/sessions/{SID}/artifact-detail",
            gone,
            body=json.dumps(request).encode(),
        )

    body = await third_of_three_edits_is_admitted(catalog, monkeypatch, start)
    assert (body["state"], body["text"]) == ("ready", "artifact body text")


def write_tool_result_transcript(path: Path) -> dict[str, str]:
    """A transcript of one Read call and its result, written to `path`; returns the result's
    artifact reference."""
    entries = [
        {
            "type": "assistant",
            "uuid": str(uuid.UUID(int=1)),
            "parentUuid": None,
            "message": {
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read1",
                        "name": "Read",
                        "input": {"file_path": "n.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": str(uuid.UUID(int=2)),
            "parentUuid": str(uuid.UUID(int=1)),
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "read1", "content": "artifact body text"}
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in entries) + "\n", encoding="utf-8")
    with path.open("rb") as handle:
        raw_line = handle.readlines()[1]
    return {
        "entry_uuid": str(uuid.UUID(int=2)),
        "tool_use_id": "read1",
        "source_revision": hashlib.sha256(raw_line.rstrip(b"\r\n")).hexdigest(),
    }


async def test_artifact_detail_route_pages_and_cancels_on_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, path = make_catalog(tmp_path, [])
    ref = write_tool_result_transcript(path)
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    artifacts = ArtifactDetails(catalog, service.resolve)
    app = Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
            artifacts=artifacts,
        )
    )
    with TestClient(app, base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/artifact-detail",
            headers=HEADERS,
            json={"request_id": RID, "ref": ref, "cursor": 0},
        )
        assert response.status_code == 200
        assert response.json()["text"] == "artifact body text"

    # Disconnect cancellation: a client that goes away mid-computation gets a typed refusal,
    # never a hang or an unhandled crash. The watched request's own cancellation event is the
    # bound method's underlying threading.Event, so waiting on it needs no sleep loop: it fires
    # within milliseconds when the disconnect watcher observes the disconnect this test sends,
    # and the wait is bounded by HANG_GUARD_S so a regression that drops cancellation fails fast
    # instead of hanging. The stall never asserts: an assertion raised inside the route's worker
    # thread is caught by the route's own exception handling and mapped to the same 503 the test
    # expects, which would mask exactly the regression this test exists to catch (a route that no
    # longer passes its own cancellation event to the service). It only ever raises
    # InterruptedError when it truly observed cancellation; otherwise it answers ready, and the
    # test checks what was actually watched afterward, outside the route.
    watched_holder: dict[str, object] = {}

    def stalling_page(
        self: ArtifactDetails,
        session_id: str,
        ref: dict[str, str],
        cursor: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        watched = getattr(cancelled, "__self__", None)
        watched_holder["watched"] = watched
        if (
            cancelled is not None
            and isinstance(watched, threading.Event)
            and watched.wait(HANG_GUARD_S)
            and cancelled()
        ):
            raise InterruptedError
        return {
            "state": "ready",
            "reason": "",
            "text": "not cancelled",
            "next_cursor": None,
            "byte_size": 0,
            "truncated": False,
        }

    monkeypatch.setattr(ArtifactDetails, "page", stalling_page)
    body = json.dumps({"request_id": RID, "ref": ref, "cursor": 0}).encode("utf-8")
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    output: list[Message] = []

    async def send(message: Message) -> None:
        output.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/v2/sessions/{SID}/artifact-detail",
        "raw_path": f"/api/v2/sessions/{SID}/artifact-detail".encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"authorization", b"Bearer synthetic"),
            (b"origin", BASE.encode()),
            (b"content-type", b"application/json"),
        ],
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    # The route must hand the service its own cancellation event, not a stand-in: checked here,
    # outside the route, so this can never itself be the thing masking a dropped cancellation.
    assert isinstance(watched_holder["watched"], threading.Event)
    assert status == 503


class WatchedPermit:
    """The parse permit, announcing when a call is about to block on it: only a caller that lost
    the permit passes a timeout, so `waiting` fires exactly when a read is queued behind the held
    permit (test_catalog_budgets.py's own pattern for the history route, restated here so this
    file and test_artifact_integration.py share it without a cross-file private import)."""

    def __init__(self, inner: threading.BoundedSemaphore, waiting: threading.Event) -> None:
        self._inner = inner
        self._waiting = waiting

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if timeout is not None:
            self._waiting.set()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()


def test_anchor_stops_waiting_for_the_parse_permit_once_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held permit makes `anchor` queue for it (PARSE_WAIT_S); flipping the caller's
    own `cancelled` mid-wait must free it in about one PARSE_WAIT_SLICE_S, not after the whole
    wait. PARSE_WAIT_S is patched well past HANG_GUARD_S so only the cancellation, never the
    wait's own expiry, can explain a prompt "busy"."""
    catalog, _ = make_catalog(tmp_path, ["needle"])
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    monkeypatch.setattr(catalog_module, "PARSE_WAIT_S", 2 * HANG_GUARD_S)
    waiting = threading.Event()
    monkeypatch.setattr(catalog, "_parse_permit", WatchedPermit(catalog._parse_permit, waiting))
    catalog._parse_permit.acquire()  # stands in for a parse already in flight
    cancelled = threading.Event()
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(service.anchor(SID, {}, cancelled.is_set))
    )
    thread.start()
    try:
        assert waiting.wait(HANG_GUARD_S), "the read never queued behind the held permit"
        cancelled.set()
        thread.join(HANG_GUARD_S)
        assert not thread.is_alive(), "cancelling mid-wait did not free the read"
    finally:
        catalog._parse_permit.release()
    assert (result["state"], result["reason"]) == ("error", "busy")
