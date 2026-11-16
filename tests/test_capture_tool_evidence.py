"""Unverified running-tool compatibility cannot launch a provider or certify a pass."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture  # noqa: E402

from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION  # noqa: E402


async def test_bot_memory_capture_has_private_settings_before_opening_a_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()

    async def stop_before_provider(rec: object, launch: capture.Launch) -> None:
        assert launch.bot_memory_settings
        path = Path(launch.bot_memory_settings)
        capture.protection.require_private_directory(path.parent)
        capture.protection.require_private_file(path)
        document = json.loads(path.read_text(encoding="ascii"))
        assert set(document) == {"autoMemoryDirectory"}
        assert (Path(document["autoMemoryDirectory"]) / "MEMORY.md").is_file()
        raise RuntimeError("provider deliberately absent")

    open_session = AsyncMock(side_effect=stop_before_provider)
    cap = cast(capture.Capture, SimpleNamespace(root=root, run_dir=tmp_path, open=open_session))
    with pytest.raises(RuntimeError, match="provider deliberately absent"):
        await capture.s37(cap, cast(capture.Recorder, None))
    open_session.assert_awaited_once()


def test_native_bot_memory_evidence_covers_two_bots_a_fresh_chat_and_resume() -> None:
    evidence = json.loads(
        (Path(__file__).parent / "fixtures" / "claude" / "bot-memory.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["native_version"] == "2.1.281" and evidence["scenario"] == "37"
    assert evidence["platform"] == "win32"
    observations = evidence["observations"]
    assert [(item["owner"], item["resume"]) for item in observations] == [
        (0, False),
        (1, False),
        (0, False),
        (0, True),
    ]
    for item in observations:
        assert item["success"] and item["own_canary"] and item["project_instructions"]
        assert not item["other_canary"] and item["tools"] == []


async def test_running_tool_capture_fails_closed_before_opening_a_provider() -> None:
    open_session = AsyncMock(side_effect=AssertionError("provider capture must stay disabled"))
    cap = cast(capture.Capture, SimpleNamespace(open=open_session))

    observed = await capture.s16(cap, cast(capture.Recorder, None))

    assert observed["pass"] is False
    assert observed["running_tool_verified"] is False
    assert observed["incomplete"] is True
    assert "Unverified" in observed["note"]
    open_session.assert_not_awaited()


_CANARY_RE = re.compile(r"STEER_[0-9a-f]+")


def _load_scrubbed_fixture(path: Path) -> list[dict[str, Any]]:
    """One scrubbed capture file's lines (`{scenario, dir, t_ms, frame}`), in arrival order."""
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    return [json.loads(line) for line in lines]


def _out_frames(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["frame"] for e in entries if e["dir"] == "out"]


def _steer_stdin_frame(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The one stdin ("in") frame scenario 38's steer wrote: a `user` message with `priority:
    "next"`. Its own `uuid` is the steer id; the scrub leaves it (and the `command_lifecycle`
    frames' matching `command_uuid`) untouched, so this is how a probe's own lifecycle frames
    are told apart from any other command's."""
    for entry in entries:
        if entry["dir"] != "in":
            continue
        frame = entry["frame"]
        if frame.get("type") == "user" and frame.get("priority") == "next":
            return frame
    raise AssertionError("no steer stdin frame (priority next) in this fixture")


def _canary_of(steer_frame: dict[str, Any]) -> str:
    content = steer_frame.get("message", {}).get("content")
    match = _CANARY_RE.search(content) if isinstance(content, str) else None
    assert match, f"no STEER_<hex> canary in the steer's own text: {content!r}"
    return match.group(0)


def _assistant_text(frame: dict[str, Any]) -> str:
    content = (frame.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _indices(frames: list[dict[str, Any]], matches: Callable[[dict[str, Any]], bool]) -> list[int]:
    return [i for i, frame in enumerate(frames) if matches(frame)]


def _assert_inits_report_fixture_version(frames: list[dict[str, Any]], label: str) -> None:
    inits = [f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"]
    assert inits, f"no system/init frame in {label}"
    for init in inits:
        assert init.get("claude_code_version") == FIXTURE_VERSION, (
            f"{label} system/init reports {init.get('claude_code_version')!r}, "
            f"not FIXTURE_VERSION {FIXTURE_VERSION!r}"
        )


def test_profile_steer_evidence_covers_a_fold_and_a_stop() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "claude" / "profile-steer.json"
    evidence = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert evidence["native_version"] == FIXTURE_VERSION
    assert evidence["scenario"] == "38"
    assert evidence["platform"] == "win32"
    assert evidence["pass"] is True
    assert evidence["fold_pass"] is True
    assert evidence["stop_pass"] is True

    fold_entries = _load_scrubbed_fixture(
        Path(__file__).parent / "fixtures" / "claude" / "profile-steer-fold.jsonl"
    )
    fold_out = _out_frames(fold_entries)
    fold_steer = _steer_stdin_frame(fold_entries)
    fold_steer_id = fold_steer["uuid"]
    fold_canary = _canary_of(fold_steer)
    _assert_inits_report_fixture_version(fold_out, "fold")

    fold_result_idx = _indices(fold_out, lambda f: f.get("type") == "result")
    assert len(fold_result_idx) == 1, (
        f"fold must have exactly one result, got {len(fold_result_idx)}"
    )

    fold_lifecycle_idx = _indices(
        fold_out,
        lambda f: f.get("type") == "command_lifecycle" and f.get("command_uuid") == fold_steer_id,
    )
    fold_started_or_completed_idx = [
        i for i in fold_lifecycle_idx if fold_out[i].get("state") in ("started", "completed")
    ]
    assert fold_started_or_completed_idx, "fold steer never reached started or completed"
    assert min(fold_started_or_completed_idx) < fold_result_idx[0], (
        "fold steer's started/completed lifecycle frame must come before its one result"
    )

    fold_canary_idx = _indices(
        fold_out, lambda f: f.get("type") == "assistant" and fold_canary in _assistant_text(f)
    )
    assert fold_canary_idx, "fold canary never reached an assistant text frame"
    assert fold_canary_idx[0] < fold_result_idx[0], "fold canary must arrive before its one result"

    stop_entries = _load_scrubbed_fixture(
        Path(__file__).parent / "fixtures" / "claude" / "profile-steer-stop.jsonl"
    )
    stop_out = _out_frames(stop_entries)
    stop_steer = _steer_stdin_frame(stop_entries)
    stop_steer_id = stop_steer["uuid"]
    stop_canary = _canary_of(stop_steer)
    _assert_inits_report_fixture_version(stop_out, "stop")

    stop_result_idx = _indices(stop_out, lambda f: f.get("type") == "result")
    assert len(stop_result_idx) == 2, (
        f"stop must have exactly two results, got {len(stop_result_idx)}"
    )

    stop_lifecycle_idx = _indices(
        stop_out,
        lambda f: f.get("type") == "command_lifecycle" and f.get("command_uuid") == stop_steer_id,
    )
    stop_started_idx = [i for i in stop_lifecycle_idx if stop_out[i].get("state") == "started"]
    assert stop_started_idx, "stop steer never reached started"

    stop_init_idx = _indices(
        stop_out, lambda f: f.get("type") == "system" and f.get("subtype") == "init"
    )
    stop_init_after_started = [i for i in stop_init_idx if i > stop_started_idx[0]]
    assert stop_init_after_started, "no system/init after the stop steer's started lifecycle frame"

    # Keep the stop order checks: the error result, then started, then init, then success.
    assert stop_out[stop_result_idx[0]].get("subtype") == "error_during_execution"
    assert stop_out[stop_result_idx[1]].get("subtype") == "success"
    assert (
        stop_result_idx[0] < stop_started_idx[0] < stop_init_after_started[0] < stop_result_idx[1]
    )

    stop_canary_idx = _indices(
        stop_out, lambda f: f.get("type") == "assistant" and stop_canary in _assistant_text(f)
    )
    assert stop_canary_idx, "stop canary never reached an assistant text frame"
    assert stop_result_idx[0] < stop_canary_idx[0] < stop_result_idx[1], (
        "stop canary must arrive between its two results"
    )
