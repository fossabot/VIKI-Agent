"""create_app's ToolSources wiring, driven through the route with a fake actor: the lookup reads
the open chat's own turns, this launch's included, and a fork reads its source until its own
file exists. A resolver with the history pages' `until` cut, or one that ignores forks, fails a
test here."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.test_server import BEARER, ORIGIN, running
from viki_agent.catalog import Catalog, project_dir

SID = str(uuid.UUID(int=1000))
FORK = str(uuid.UUID(int=3000))
# The catalog's vault, not running()'s: a short project folder name keeps the paths short.
VAULT = Path("C:/SyntheticVault")
SINCE = "2026-10-10T01:00:00.000Z"  # the live actor's first process start
BEFORE, AFTER = "2026-10-10T00:00:00.000Z", "2026-10-10T01:00:05.000Z"


@dataclass
class FakeActor:
    """The SessionActor fields a resolver reads: the transcript that holds the chat's history
    (a fork's source) and this launch's first process start."""

    history_source: str
    since: str | None


def _tool_use(tool_use_id: str, name: str, **input_fields: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": input_fields}


OLD_TURN = (BEFORE, _tool_use("old_w", "WebFetch", url="https://old.example/a"))


def _write(home: Path, session_id: str, turns: list[tuple[str, dict[str, Any]]]) -> None:
    """One timestamped assistant entry per turn, chained. Uuids follow the position, so a fork's
    copy of its source's turns keeps their uuids and timestamps, as the CLI's copy does."""
    rows: list[dict[str, Any]] = []
    parent: str | None = None
    for i, (timestamp, block) in enumerate(turns):
        uid = str(uuid.UUID(int=i + 1))
        rows.append(
            {
                "type": "assistant",
                "uuid": uid,
                "parentUuid": parent,
                "timestamp": timestamp,
                "message": {"id": f"m{i + 1}", "content": [block]},
            }
        )
        parent = uid
    path = project_dir(home, VAULT) / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    actor: FakeActor,
    ids: list[str],
) -> dict[str, Any]:
    """The route's answer from create_app's own wiring, with `actor` as the open chat."""
    with running(tmp_path, catalog=Catalog(tmp_path / "home", VAULT)) as app:
        monkeypatch.setattr(app.manager, "get", lambda sid: actor if sid == session_id else None)
        response = app.client.post(
            f"/api/v2/sessions/{session_id}/tool-sources",
            headers={**BEARER, "origin": ORIGIN},
            json={"request_id": str(uuid.uuid4()), "tool_use_ids": ids},
        )
    assert response.status_code == 200
    return response.json()


def test_a_live_chat_finds_the_turns_after_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path / "home",
        SID,
        [
            OLD_TURN,
            (AFTER, _tool_use("new_w", "WebFetch", url="https://new.example/b")),
            (AFTER, _tool_use("new_g", "Grep", pattern="needle")),
        ],
    )
    body = _lookup(tmp_path, monkeypatch, SID, FakeActor(SID, SINCE), ["old_w", "new_w", "new_g"])
    assert body["state"] == "ready"
    assert body["items"] == [
        {"tool_use_id": "old_w", "tool": "WebFetch", "url": "https://old.example/a"},
        {"tool_use_id": "new_w", "tool": "WebFetch", "url": "https://new.example/b"},
        {
            "tool_use_id": "new_g",
            "tool": "Grep",
            "pattern": "needle",
            "path": "",
            "path_state": "root",
        },
    ]


def test_a_fork_reads_its_own_file_once_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write(home, SID, [OLD_TURN])
    fork_turn = (AFTER, _tool_use("fork_w", "WebFetch", url="https://f.example"))
    _write(home, FORK, [OLD_TURN, fork_turn])
    body = _lookup(tmp_path, monkeypatch, FORK, FakeActor(SID, SINCE), ["old_w", "fork_w"])
    assert body["state"] == "ready"
    assert [item["tool_use_id"] for item in body["items"]] == ["old_w", "fork_w"]


def test_a_fork_with_no_file_yet_reads_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "home", SID, [OLD_TURN])
    body = _lookup(tmp_path, monkeypatch, FORK, FakeActor(SID, SINCE), ["old_w"])
    assert body["state"] == "ready"
    assert [item["tool_use_id"] for item in body["items"]] == ["old_w"]
