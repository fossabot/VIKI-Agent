from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from tests.vaults.test_vault import service
from tests.vaults.test_vault_api import AUTH, ORIGIN, _app
from viki_agent.vaults import vault as module
from viki_agent.vaults import vault_api
from viki_agent.vaults.vault import Overview, OverviewNote, ScanResult, VaultError, VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

OVERVIEW = "/api/v2/vaults/w/overview"
BODY_KEYS = {"notes", "partial", "partial_reasons", "omitted", "read_bytes"}
ROW_KEYS = {
    "ref",
    "state",
    "title",
    "type",
    "status",
    "confidence",
    "tags",
    "created",
    "modified",
    "summary",
    "links",
    "complete",
}
# Guards real thread entry, which normally takes milliseconds; not the overview's own deadline.
HANG_GUARD_S = 5
NOW = 1_760_000_000


def _write(root: Path, ref: str, text: str, mtime: int = NOW) -> None:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    os.utime(path, (mtime, mtime))


def _dated(created: str = "", modified: str = "") -> str:
    dates = {"created": created, "modified": modified}
    header = [f"{key}: {value}" for key, value in dates.items() if value]
    return "\n".join(["---", *header, "---", "# Note", ""])


def test_overview_lists_review_notes_newest_first(tmp_path: Path) -> None:
    review = {
        "40-sources/c.md": _dated(modified="2026-10-01"),
        "30-knowledge/a.md": _dated(created="2026-09-02"),
        "30-knowledge/B.md": _dated("2026-08-01", "2026-09-02"),
        "50-maps/d.md": _dated("2026-07-01", "2026-06-01"),
        "40-sources/g.md": _dated("2026-06-15"),
        "30-knowledge/deep/e.md": "# Undated\n",
        "40-sources/F.md": "",
    }
    hidden = {
        "10-projects/p.md": _dated("2026-12-01"),
        "home.md": _dated("2026-12-01"),
        "30-knowledge/.claude/secret.md": _dated("2026-12-01"),
        "30-knowledge/.git/secret.md": _dated("2026-12-01"),
        "30-knowledge/secret.txt": _dated("2026-12-01"),
    }
    for ref, text in {**review, **hidden}.items():
        _write(tmp_path, ref, text)
    with TestClient(_app(service(tmp_path))) as client:
        response = client.get(OVERVIEW, headers={**AUTH, "Origin": ORIGIN})
    assert response.status_code == 200 and response.content.isascii()
    body = response.json()
    assert set(body) == BODY_KEYS and all(set(row) == ROW_KEYS for row in body["notes"])
    # The newer of created and modified, newest first; a tie and the undated by ref casefold.
    assert [row["ref"] for row in body["notes"]] == [
        "40-sources/c.md",
        "30-knowledge/a.md",
        "30-knowledge/B.md",
        "50-maps/d.md",
        "40-sources/g.md",
        "30-knowledge/deep/e.md",
        "40-sources/F.md",
    ]
    assert [row["links"] for row in body["notes"]] == [None, None, None, 0, None, None, None]
    assert (body["partial"], body["partial_reasons"], body["omitted"]) == (False, [], 0)
    assert body["read_bytes"] == sum(len(text.encode()) for text in review.values())
    assert "secret" not in response.text


VIKI_NOTE = """\
---
type: note
status: evergreen
created: 2026-08-18
modified: 2026-09-01
up: "[[Second Brain MOC]]"
tags: [ai, pkm]
aliases: []
author: viki
confidence: high
---
# Deny rules hold even in bypass mode

## Summary
Deny rules in **settings** still apply when [[Permission modes|bypass mode]] is on.

## Details
Longer text.
"""


def test_a_viki_shaped_note_is_ready_with_every_field(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/Deny rules hold.md", VIKI_NOTE)
    with TestClient(_app(service(tmp_path))) as client:
        response = client.get(OVERVIEW, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["notes"] == [
        {
            "ref": "30-knowledge/Deny rules hold.md",
            "state": "ready",
            "title": "Deny rules hold even in bypass mode",
            "type": "note",
            "status": "evergreen",
            "confidence": "high",
            "tags": ["ai", "pkm"],
            "created": "2026-08-18",
            "modified": "2026-09-01",
            "summary": "Deny rules in settings still apply when bypass mode is on.",
            "links": None,
            "complete": True,
        }
    ]


def test_map_links_count_distinct_resolved_body_targets(tmp_path: Path) -> None:
    for ref in (
        "30-knowledge/Alpha.md",
        "40-sources/Beta.md",
        "50-maps/Home MOC.md",
        "50-maps/Other map.md",
        "50-maps/Twin.md",
        "50-maps/Twin.markdown",
    ):
        _write(tmp_path, ref, "# Target\n")
    topics = [
        "---",
        'up: "[[Home MOC]]"',
        "---",
        "# Topics",
        "[[../30-knowledge/Alpha]] [again](../30-knowledge/alpha.md) [[Other map]]",
        "[[Other map|alias]] [[../40-sources/Beta#Part]] [[Topics]] [[Missing]] [[Twin]]",
        "[site](https://example.com)",
    ]
    _write(tmp_path, "50-maps/Topics.md", "\n".join(topics))
    # A link past the 32 KiB map prefix is never read.
    _write(
        tmp_path, "50-maps/Long.md", "# Long\n[[Other map]]\n" + "x" * 40_000 + "\n[[Home MOC]]\n"
    )
    rows = {note.ref: note for note in service(tmp_path).overview("w").notes}
    # Alpha, Other map and Beta: the frontmatter `up:`, itself, missing, ambiguous and external
    # links are not counted.
    assert (rows["50-maps/Topics.md"].links, rows["50-maps/Topics.md"].complete) == (3, True)
    assert (rows["50-maps/Long.md"].links, rows["50-maps/Long.md"].complete) == (1, False)
    assert rows["50-maps/Home MOC.md"].links == 0
    assert rows["30-knowledge/Alpha.md"].links is None


def test_each_prefix_stops_at_its_cap(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/big.md", "# Big\n" + "x" * 10_000)
    _write(tmp_path, "50-maps/big.md", "# Big map\n" + "x" * 40_000)
    _write(tmp_path, "40-sources/small.md", "# Small\n")
    overview = service(tmp_path).overview("w")
    assert overview.read_bytes == 4096 + 32768 + len("# Small\n")
    assert {note.ref: note.complete for note in overview.notes} == {
        "30-knowledge/big.md": False,
        "50-maps/big.md": False,
        "40-sources/small.md": True,
    }
    assert not overview.partial and overview.omitted == 0


def test_byte_limit_stops_at_the_note_that_crosses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for age, ref in enumerate(("30-knowledge/a.md", "30-knowledge/b.md", "40-sources/c.md")):
        _write(tmp_path, ref, "x" * 6000, NOW - age)
    monkeypatch.setattr(module, "MAX_OVERVIEW_BYTES", 5000)
    overview = service(tmp_path).overview("w")
    # a reads 4,096 bytes, b the 904 left and then stops: b and c are not listed.
    assert [note.ref for note in overview.notes] == ["30-knowledge/a.md"]
    assert (overview.partial, overview.partial_reasons) == (True, ("byte_limit",))
    assert (overview.omitted, overview.read_bytes) == (2, 5000)


def test_item_limit_reads_maps_then_the_newest_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "50-maps/m.md", "# Map\n", NOW - 9)
    _write(tmp_path, "30-knowledge/new.md", "# New\n", NOW)
    _write(tmp_path, "40-sources/mid.md", "# Mid\n", NOW - 1)
    _write(tmp_path, "30-knowledge/old.md", "# Old\n", NOW - 2)
    monkeypatch.setattr(module, "MAX_OVERVIEW_NOTES", 2)
    overview = service(tmp_path).overview("w")
    assert sorted(note.ref for note in overview.notes) == ["30-knowledge/new.md", "50-maps/m.md"]
    assert (overview.partial, overview.partial_reasons, overview.omitted) == (
        True,
        ("item_limit",),
        2,
    )
    assert overview.read_bytes == len("# Map\n") + len("# New\n")


def test_deadline_counts_the_note_it_stops_and_every_remaining_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for age, ref in enumerate(("30-knowledge/a.md", "30-knowledge/b.md", "40-sources/c.md")):
        _write(tmp_path, ref, "# Note\n", NOW - age)
    now, called = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    vault = service(tmp_path)
    prefix = vault._read_prefix

    def observed(root, ref, budget, **kwargs):
        called.append(ref)
        result = prefix(root, ref, budget, **kwargs)
        now[0] = module.OVERVIEW_DEADLINE_SECONDS  # the request is now 3 s old
        return result

    monkeypatch.setattr(vault, "_read_prefix", observed)
    overview = vault.overview("w")
    assert called == ["30-knowledge/a.md", "30-knowledge/b.md"]
    assert [note.ref for note in overview.notes] == ["30-knowledge/a.md"]
    assert (overview.partial, overview.partial_reasons) == (True, ("deadline",))
    assert (overview.omitted, overview.read_bytes) == (2, len("# Note\n"))


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("changed", "changed"),
        ("missing_note", "changed"),
        ("target_busy", "changed"),
        ("unsafe_path", "unreadable"),
        ("private_path", "unreadable"),
        ("note_too_large", "unreadable"),
        ("read_failed", "unreadable"),
        ("unknown_workspace", None),
        ("root_changed", None),
        ("root_unavailable", None),
    ],
)
def test_a_refused_note_is_omitted_and_a_root_failure_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, reason: str | None
) -> None:
    _write(tmp_path, "30-knowledge/a.md", "# A\n", NOW)
    _write(tmp_path, "30-knowledge/b.md", "# B\n", NOW - 1)
    vault = service(tmp_path)
    prefix = vault._read_prefix

    def refused(root, ref, budget, **kwargs):
        if ref == "30-knowledge/a.md":
            raise VaultError(code)
        return prefix(root, ref, budget, **kwargs)

    monkeypatch.setattr(vault, "_read_prefix", refused)
    if reason is None:
        with pytest.raises(VaultError, match=f"^{code}$"):
            vault.overview("w")
        return
    overview = vault.overview("w")
    assert [note.ref for note in overview.notes] == ["30-knowledge/b.md"]
    assert (overview.partial, overview.partial_reasons, overview.omitted) == (True, (reason,), 1)


def test_invalid_utf8_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "30-knowledge").mkdir()
    (tmp_path / "30-knowledge" / "bad.md").write_bytes(b"# Bad \xff\n")
    _write(tmp_path, "50-maps/good.md", "# Good\n")
    overview = service(tmp_path).overview("w")
    assert [note.ref for note in overview.notes] == ["50-maps/good.md"]
    assert (overview.partial, overview.partial_reasons, overview.omitted) == (
        True,
        ("unreadable",),
        1,
    )


def test_a_control_character_named_note_is_omitted_as_unsafe_entry(tmp_path: Path) -> None:
    # The scan admits DEL/C1 controls in a name (only below-32 and lone surrogates are unsafe
    # there), but the client refuses the whole overview if any row's ref holds one, so such an
    # entry must never become a row here.
    _write(tmp_path, "30-knowledge/plain.md", "# Plain\n")
    try:
        _write(tmp_path, "30-knowledge/odd\x7f.md", "# Odd\n")
    except OSError:
        pytest.skip("this filesystem refuses a DEL-named file")
    overview = service(tmp_path).overview("w")
    assert [note.ref for note in overview.notes] == ["30-knowledge/plain.md"]
    assert (overview.partial, overview.partial_reasons, overview.omitted) == (
        True,
        ("unsafe_entry",),
        1,
    )


def _encoded(notes: tuple[OverviewNote, ...], reasons: list[str], omitted: int) -> bytes:
    body = {
        "notes": [asdict(note) for note in notes],
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "omitted": omitted,
        "read_bytes": 4321,
    }
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class FixedOverview:
    def __init__(self, result: Overview) -> None:
        self.result = result

    def overview(self, workspace_id: str) -> Overview:
        assert workspace_id == "w"
        return self.result


def test_response_limit_keeps_the_newest_rows_that_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = tuple(
        OverviewNote(
            f"30-knowledge/{index:02}.md",
            "ready",
            "Milk " * 20,
            "note",
            "draft",
            "high",
            ["ai"],
            "2026-10-01",
            None,
            "Summary",
            None,
            True,
        )
        for index in range(12)
    )
    fake = FixedOverview(Overview(notes, True, ("deadline", "unreadable"), 4321, 9))
    cut = ["deadline", "response_limit", "unreadable"]
    full = _encoded(notes, ["deadline", "unreadable"], 9)
    eleven, ten = _encoded(notes[:11], cut, 10), _encoded(notes[:10], cut, 11)
    with TestClient(_app(fake)) as client:
        for cap, expected in (
            (len(full), full),
            (len(full) - 1, eleven),
            (len(eleven), eleven),
            (len(eleven) - 1, ten),
        ):
            monkeypatch.setattr(vault_api, "MAX_OVERVIEW_RESPONSE", cap)
            response = client.get(OVERVIEW, headers=AUTH)
            assert response.status_code == 200
            assert response.content == expected and len(response.content) <= cap
        monkeypatch.setattr(vault_api, "MAX_OVERVIEW_RESPONSE", len(_encoded((), cut, 21)) - 1)
        refused = client.get(OVERVIEW, headers=AUTH)
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "read_failed"


class RecordingOverview:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def overview(self, workspace_id: str) -> Overview:
        self.calls.append(workspace_id)
        return Overview((), False, (), 0, 0)


def test_auth_origin_and_any_query_are_refused_before_the_read() -> None:
    fake = RecordingOverview()
    with TestClient(_app(fake)) as client:
        assert client.get(OVERVIEW).status_code == 401
        foreign = client.get(OVERVIEW, headers={**AUTH, "Origin": "https://evil.invalid"})
        assert foreign.status_code == 403
        for query in ("x=1", "q=a", "refresh"):
            response = client.get(f"{OVERVIEW}?{query}", headers=AUTH)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_query"
        assert fake.calls == []
        allowed = client.get(OVERVIEW, headers={**AUTH, "Origin": ORIGIN})
    assert allowed.status_code == 200
    assert allowed.json() == {
        "notes": [],
        "partial": False,
        "partial_reasons": [],
        "omitted": 0,
        "read_bytes": 0,
    }
    assert fake.calls == ["w"]


def test_an_unknown_workspace_is_404() -> None:
    def missing(_id: str) -> object:
        raise WorkspaceError("unknown_workspace")

    owner = SimpleNamespace(get=missing, current_root=lambda _workspace: None)
    with TestClient(_app(VaultService(cast(WorkspaceService, owner)))) as client:
        response = client.get(OVERVIEW, headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_workspace"


async def test_a_held_overview_refuses_a_second_and_takes_no_shared_slot() -> None:
    entered, started, release = threading.Semaphore(0), threading.Event(), threading.Event()

    class SlowVault:
        def scan(self, _workspace_id: str) -> ScanResult:
            entered.release()
            assert release.wait(HANG_GUARD_S)
            return ScanResult((), False, (), 0, 0, 0)

        def overview(self, _workspace_id: str) -> Overview:
            started.set()
            assert release.wait(HANG_GUARD_S)
            return Overview((), False, (), 0, 0)

    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(SlowVault(), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        entries = [
            asyncio.create_task(client.get("/api/v2/vaults/w/entries", headers=AUTH))
            for _ in range(4)
        ]
        try:
            for _ in entries:
                assert await asyncio.to_thread(entered.acquire, True, HANG_GUARD_S)
            # The shared four are busy, yet the overview is admitted on its own slot.
            held = asyncio.create_task(client.get(OVERVIEW, headers=AUTH))
            assert await asyncio.to_thread(started.wait, HANG_GUARD_S)
            second = await client.get(OVERVIEW, headers=AUTH)
        finally:
            release.set()
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "overloaded"
        assert (await held).status_code == 200
        assert [response.status_code for response in await asyncio.gather(*entries)] == [200] * 4
        await asyncio.sleep(0)
        assert not workers
        assert (await client.get(OVERVIEW, headers=AUTH)).status_code == 200
