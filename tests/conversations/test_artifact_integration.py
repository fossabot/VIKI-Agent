from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_conversation_search import (
    HANG_GUARD_S,
    SID,
    WatchedPermit,
    fake_clock,
    make_catalog,
)
from viki_agent import catalog as catalog_module
from viki_agent.conversations import artifact_details as artifact_details_module
from viki_agent.conversations.artifact_details import DETAIL_BYTES, ArtifactDetails
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import ToolDone, ToolStarted


def tool_entries(content: object) -> list[dict]:
    return [
        {
            "type": "assistant",
            "uuid": "00000000-0000-0000-0000-000000000001",
            "parentUuid": None,
            "message": {
                "id": "message",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read1",
                        "name": "Read",
                        "input": {"file_path": "note.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "00000000-0000-0000-0000-000000000002",
            "parentUuid": "00000000-0000-0000-0000-000000000001",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "read1", "content": content}],
            },
        },
    ]


def test_production_history_artifacts_and_revision_checked_utf8_detail(tmp_path: Path) -> None:
    catalog, path = make_catalog(tmp_path, [])
    content = "日" * 23000
    rows = tool_entries(content)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert all("artifact" not in event.data() for event in catalog.history(SID).events)
    catalog.bind_artifacts()
    events = catalog.history(SID).events
    start = next(event for event in events if isinstance(event, ToolStarted))
    done = next(event for event in events if isinstance(event, ToolDone))
    assert start.artifact and start.artifact.kind == "file"
    assert done.artifact and done.artifact.detail_ref
    assert done.artifact.sha256 == hashlib.sha256(content.encode()).hexdigest()
    ref = done.data()["artifact"]["detail_ref"]
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    first = service.page(SID, ref, 0)
    second = service.page(SID, ref, first["next_cursor"])
    assert first["text"] + second["text"] == content
    assert second["next_cursor"] is None
    with pytest.raises(ValueError, match="invalid_cursor"):
        service.page(SID, ref, 1)
    path.write_text(
        path.read_text(encoding="utf-8").replace("note.md", "other.md"), encoding="utf-8"
    )
    # The result line itself is immutable even when another entry changes.
    assert service.page(SID, ref, 0)["state"] == "ready"
    path.write_text(path.read_text(encoding="utf-8").replace("read1", "read2"), encoding="utf-8")
    # Same size as before: move the mtime so the catalog's (size, mtime_ns) cache sees the
    # rewrite even when both writes land in one clock tick.
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    assert service.page(SID, ref, 0)["state"] == "changed"


def test_under_ceiling_payload_with_a_boundary_multibyte_char_is_never_falsely_truncated(
    tmp_path: Path,
) -> None:
    prefix_len = 65534
    mid = "あ"  # a 3-byte UTF-8 character, placed to straddle the 64 KiB page boundary
    filler_len = (DETAIL_BYTES - 1) - prefix_len - len(mid.encode("utf-8"))
    content = "x" * prefix_len + mid + "x" * filler_len
    payload = content.encode("utf-8")
    assert len(payload) == DETAIL_BYTES - 1
    catalog, path = make_catalog(tmp_path, [])
    rows = tool_entries(content)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    catalog.bind_artifacts()
    events = catalog.history(SID).events
    done = next(event for event in events if isinstance(event, ToolDone))
    ref = done.data()["artifact"]["detail_ref"]
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    received = b""
    cursor = 0
    truncated = False
    for _ in range(200):
        page = service.page(SID, ref, cursor)
        received += page["text"].encode("utf-8")
        truncated = page["truncated"]
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    else:
        pytest.fail("did not reach the last page")
    assert received == payload
    assert truncated is False


def test_over_ceiling_payload_gets_a_final_truncated_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both constants are read at call time, so patching them here scales the ceiling down without
    # writing an 8 MiB fixture.
    monkeypatch.setattr(artifact_details_module, "PAGE_BYTES", 8)
    monkeypatch.setattr(artifact_details_module, "DETAIL_BYTES", 32)
    content = "x" * (32 + 1)
    catalog, path = make_catalog(tmp_path, [])
    rows = tool_entries(content)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    catalog.bind_artifacts()
    events = catalog.history(SID).events
    done = next(event for event in events if isinstance(event, ToolDone))
    ref = done.data()["artifact"]["detail_ref"]
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    received = b""
    cursor = 0
    last = None
    for _ in range(20):
        last = service.page(SID, ref, cursor)
        received += last["text"].encode("utf-8")
        if last["next_cursor"] is None:
            break
        cursor = last["next_cursor"]
    else:
        pytest.fail("did not reach the last page")
    assert last is not None
    assert last["truncated"] is True
    assert last["next_cursor"] is None
    assert len(received) >= 32 - 3
    assert received == content.encode("utf-8")[: len(received)]


def _ref_for(path: Path, entry_uuid: str, tool_use_id: str) -> dict[str, str]:
    with path.open("rb") as handle:
        raw_line = handle.readlines()[1]
    return {
        "entry_uuid": entry_uuid,
        "tool_use_id": tool_use_id,
        "source_revision": hashlib.sha256(raw_line.rstrip(b"\r\n")).hexdigest(),
    }


def test_several_detail_cursors_parse_the_transcript_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifact_details_module, "PAGE_BYTES", 4)
    catalog, path = make_catalog(tmp_path, [])
    rows = tool_entries("x" * 10)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    ref = _ref_for(path, rows[1]["uuid"], "read1")
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    parses: list[Path] = []
    original = catalog_module._read_entries  # noqa: SLF001 - own module, own test

    def counting(read_path: Path) -> list[dict[str, Any]]:
        parses.append(read_path)
        return original(read_path)

    monkeypatch.setattr(catalog_module, "_read_entries", counting)
    cursor = 0
    pages = 0
    for _ in range(20):
        page = service.page(SID, ref, cursor)
        assert page["state"] == "ready"
        pages += 1
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert pages > 1
    assert len(parses) == 1


def test_a_detail_read_past_its_deadline_is_a_typed_error(tmp_path: Path) -> None:
    catalog, path = make_catalog(tmp_path, [])
    rows = tool_entries("payload")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    ref = _ref_for(path, rows[1]["uuid"], "read1")
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    page = service.page(SID, ref, 0, clock=fake_clock(1))
    assert (page["state"], page["reason"]) == ("error", "time_budget")


def test_page_stops_waiting_for_the_parse_permit_once_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held permit makes `page` queue for it (PARSE_WAIT_S); flipping the caller's own
    `cancelled` mid-wait must free it in about one PARSE_WAIT_SLICE_S, not after the whole wait.
    PARSE_WAIT_S is patched well past HANG_GUARD_S so only the cancellation, never the wait's own
    expiry, can explain a prompt "busy"."""
    catalog, _ = make_catalog(tmp_path, [])
    service = ArtifactDetails(catalog, lambda sid: (sid, None))
    monkeypatch.setattr(catalog_module, "PARSE_WAIT_S", 2 * HANG_GUARD_S)
    waiting = threading.Event()
    monkeypatch.setattr(catalog, "_parse_permit", WatchedPermit(catalog._parse_permit, waiting))
    catalog._parse_permit.acquire()  # stands in for a parse already in flight
    cancelled = threading.Event()
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(service.page(SID, {}, 0, cancelled.is_set))
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


def test_malformed_or_ambiguous_results_do_not_claim_success() -> None:
    for content in [None, [{"type": "image", "source": "untrusted"}]]:
        mapper = Mapper(vault="C:/SyntheticVault", mode="default", artifacts_enabled=True)
        before, after = tool_entries(content)
        mapper.entry(before)
        done = next(event for event in mapper.entry(after) if isinstance(event, ToolDone))
        assert done.artifact is None
    mapper = Mapper(vault="C:/SyntheticVault", mode="default", artifacts_enabled=True)
    before, after = tool_entries("payload")
    after["message"]["content"][0]["is_error"] = "false"
    mapper.entry(before)
    done = next(event for event in mapper.entry(after) if isinstance(event, ToolDone))
    assert done.artifact and done.artifact.observation == "unknown"
