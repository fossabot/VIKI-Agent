"""Scenario 36 (named-only): stop_task on one background agent, then a Stop mid-turn on the
next turn that spares the other. Drives capture.s36 with fakes only; the real scenario is a
paid live run and is never executed here (capture.py's own NAMED_ONLY keeps it out of the
default run)."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any, cast

TESTS = Path(__file__).parents[1]
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import capture  # noqa: E402

from viki_agent import config  # noqa: E402

from replay import FakeSpawner  # noqa: E402


def _cfg(tmp_path: Path) -> config.Config:
    """A minimal Config: only claude_home is read by _project_dir/_agent_meta_stopped_by_user."""
    return config.Config(
        vault=None,
        claude_home=tmp_path / "claude_home",
        cli=None,
        edge=None,
        data_dir=tmp_path / "data",
    )


def test_scenario_36_is_registered_named_only_and_absent_from_default() -> None:
    assert "36" in capture.SCENARIOS
    assert "36" in capture.NAMED_ONLY
    assert "36" not in capture.REFUSED

    default = [
        key
        for key in capture.SCENARIOS
        if key not in capture.REFUSED and key not in capture.NAMED_ONLY
    ]
    assert "36" not in default

    _title, fn = capture.SCENARIOS["36"]
    source = inspect.getsource(fn)
    assert "stop_task(" in source
    assert "interrupt_when" in source


# --- _agent_meta_stopped_by_user: the one key read from agent-<id>.meta.json -----------------


def test_agent_meta_stopped_by_user_reads_the_matching_file_only(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # Never created. A short root keeps the project folder, whose name repeats the root, under
    # Windows' MAX_PATH.
    root = Path("scratch")
    folder = capture._project_dir(cfg, root) / "sess-1" / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-a.meta.json").write_text(
        json.dumps({"toolUseId": "toolu_a", "stoppedByUser": True, "other": "ignored"}),
        encoding="utf-8",
    )
    (folder / "agent-b.meta.json").write_text(
        json.dumps({"toolUseId": "toolu_b", "stoppedByUser": False}), encoding="utf-8"
    )

    assert capture._agent_meta_stopped_by_user(cfg, root, "sess-1", "toolu_a") is True
    assert capture._agent_meta_stopped_by_user(cfg, root, "sess-1", "toolu_b") is False


def test_agent_meta_stopped_by_user_missing_folder_file_or_match(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # Never created. A short root keeps the project folder, whose name repeats the root, under
    # Windows' MAX_PATH.
    root = Path("scratch")
    assert capture._agent_meta_stopped_by_user(cfg, root, "sess-1", "toolu_a") is None  # no folder

    folder = capture._project_dir(cfg, root) / "sess-1" / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-a.meta.json").write_text("not json", encoding="utf-8")
    assert capture._agent_meta_stopped_by_user(cfg, root, "sess-1", "toolu_a") is None  # bad file
    assert capture._agent_meta_stopped_by_user(cfg, root, "sess-1", None) is None  # no id to match


# --- s36 itself, driven end to end with fakes -------------------------------------------------

HANG_GUARD_S = 5  # the bound for a collect that must finish its frames; only a hang reaches it

SLEEP_A = "Start-Sleep -Seconds 40; Write-Output a-done"
SLEEP_B = "Start-Sleep -Seconds 25; Write-Output b-done"
SLEEP_15 = "Start-Sleep -Seconds 15; Write-Output slept"

AGENT_LAUNCH = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_a",
                "name": "Agent",
                "input": {
                    "subagent_type": "general-purpose",
                    "model": "haiku",
                    "run_in_background": True,
                    "prompt": f"Use the PowerShell tool to run exactly this command: {SLEEP_A}",
                },
            },
            {
                "type": "tool_use",
                "id": "toolu_b",
                "name": "Agent",
                "input": {
                    "subagent_type": "general-purpose",
                    "model": "haiku",
                    "run_in_background": True,
                    "prompt": f"Use the PowerShell tool to run exactly this command: {SLEEP_B}",
                },
            },
        ]
    },
}
TASK_STARTED_A = {
    "type": "system",
    "subtype": "task_started",
    "task_id": "task-a",
    "tool_use_id": "toolu_a",
}
TASK_STARTED_B = {
    "type": "system",
    "subtype": "task_started",
    "task_id": "task-b",
    "tool_use_id": "toolu_b",
}
RESULT_SUCCESS = {"type": "result", "subtype": "success"}
TOOL_USE_PS = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_ps",
                "name": "PowerShell",
                "input": {"command": SLEEP_15},
            }
        ]
    },
}
RESULT_ABORTED = {"type": "result", "subtype": "aborted_tools", "is_error": True}
TASK_UPDATED_A_STOPPED = {  # the CLI's task_updated patch says "killed" for a stopped task
    "type": "system",
    "subtype": "task_updated",
    "task_id": "task-a",
    "patch": {"status": "killed"},
}
TASK_NOTIFICATION_A_STOPPED = {
    "type": "system",
    "subtype": "task_notification",
    "task_id": "task-a",
    "tool_use_id": "toolu_a",
    "status": "stopped",
}
TASK_UPDATED_B_COMPLETED = {
    "type": "system",
    "subtype": "task_updated",
    "task_id": "task-b",
    "patch": {"status": "completed"},
}
TASK_NOTIFICATION_B_DONE = {
    "type": "system",
    "subtype": "task_notification",
    "tool_use_id": "toolu_b",
    "status": "completed",
}
EXIT_FRAME = {"type": capture.EXIT}

TURN1_HAPPY = [AGENT_LAUNCH, TASK_STARTED_A, TASK_STARTED_B, RESULT_SUCCESS]
TURN2_HAPPY = [TOOL_USE_PS, RESULT_ABORTED]
TAIL_HAPPY = [TASK_UPDATED_A_STOPPED, TASK_NOTIFICATION_B_DONE, EXIT_FRAME]


class _FakeControl36:
    def __init__(
        self, stop_error: Exception | None = None, interrupt_error: Exception | None = None
    ) -> None:
        self._stop_error = stop_error
        self._interrupt_error = interrupt_error
        self.stopped_task_ids: list[str] = []
        self.interrupts = 0

    async def send(self, text: str, session_id: str) -> None:
        pass

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self._interrupt_error is not None:
            raise self._interrupt_error

    async def stop_task(self, task_id: str) -> None:
        self.stopped_task_ids.append(task_id)
        if self._stop_error is not None:
            raise self._stop_error


class _FakeLive36:
    def __init__(self, control: _FakeControl36, frames: list[Any]) -> None:
        self.control = control
        self.session_id = "sid-fake"
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.seen: list[Any] = []
        for frame in frames:
            self.inbox.put_nowait(frame)


class _FakeCapture36:
    """Real Capture.collect underneath (it never reads self, per test_capture_mcp.py's own
    _FakeCapture33); turn's own two lines are reimplemented here so both turns, and the final
    bare collect, all get the short test bound instead of capture.py's real timeouts."""

    def __init__(
        self, live: _FakeLive36, cfg: Any, root: Path, bound: float = HANG_GUARD_S
    ) -> None:
        self._live = live
        self.cfg = cfg
        self.root = root
        self._bound = bound

    async def open(self, rec: Any, launch: Any) -> _FakeLive36:
        return self._live

    async def turn(self, live: _FakeLive36, text: str, **kwargs: Any) -> list[Any]:
        await live.control.send(text, live.session_id)
        return await self.collect(live, **kwargs)

    async def collect(self, live: _FakeLive36, **kwargs: Any) -> list[Any]:
        kwargs["timeout"] = self._bound
        return await capture.Capture.collect(cast(capture.Capture, self), live, **kwargs)  # type: ignore[arg-type]

    async def close(self, live: _FakeLive36) -> None:
        pass


def _run_s36(
    tmp_path: Path,
    frames: list[Any],
    *,
    stop_error: Exception | None = None,
    interrupt_error: Exception | None = None,
) -> tuple[dict[str, Any], _FakeControl36]:
    control = _FakeControl36(stop_error, interrupt_error)
    live = _FakeLive36(control, frames)
    cfg = _cfg(tmp_path)
    cap = _FakeCapture36(live, cfg, tmp_path / "scratch")
    rec = capture.Recorder(tmp_path / "36.jsonl", "36")
    obs = asyncio.run(capture.s36(cast(capture.Capture, cap), rec))
    rec.close()
    return obs, control


def test_scenario_36_stop_replies_a_stopped_b_completed_passes(tmp_path: Path) -> None:
    frames = TURN1_HAPPY + TURN2_HAPPY + TAIL_HAPPY
    obs, control = _run_s36(tmp_path, frames)

    assert control.stopped_task_ids == ["task-a"]
    assert control.interrupts == 1
    assert obs["stop_outcome"] == "reply"
    assert obs["stop_error"] is None
    assert obs["a_task_updated_statuses"] == ["killed"]
    assert obs["b_task_notification_statuses"] == ["completed"]
    assert obs["b_completed"] is True
    assert obs["turn2_result_subtype"] == "aborted_tools"
    assert obs["a_stopped_by_user"] is None  # no meta.json seeded in this fake run: nothing to read
    assert obs["b_stopped_by_user"] is None
    assert obs["pass"] is True


def test_scenario_36_stop_task_timeout_fails_even_if_a_and_b_otherwise_finish(
    tmp_path: Path,
) -> None:
    frames = TURN1_HAPPY + TURN2_HAPPY + TAIL_HAPPY
    obs, control = _run_s36(tmp_path, frames, stop_error=TimeoutError("stop stalled"))

    assert control.stopped_task_ids == ["task-a"]
    assert obs["stop_outcome"] == "timeout"
    assert obs["stop_error"] is None
    assert obs["pass"] is False


def test_scenario_36_stop_task_error_is_recorded_and_fails(tmp_path: Path) -> None:
    frames = TURN1_HAPPY + TURN2_HAPPY + TAIL_HAPPY
    obs, _ = _run_s36(tmp_path, frames, stop_error=RuntimeError("boom"))

    assert obs["stop_outcome"] == "error"
    assert obs["stop_error"] == "RuntimeError: boom"
    assert obs["pass"] is False


def test_scenario_36_task_started_without_a_task_id_fails_before_any_stop(tmp_path: Path) -> None:
    bad_task_started_a = {
        "type": "system",
        "subtype": "task_started",
        "task_id": None,
        "tool_use_id": "toolu_a",
    }
    frames = [AGENT_LAUNCH, bad_task_started_a, TASK_STARTED_B, RESULT_SUCCESS]
    obs, control = _run_s36(tmp_path, frames)

    assert obs["note"] == "a task_started frame is missing its task_id or tool_use_id"
    assert control.stopped_task_ids == []
    assert obs["pass"] is False


def test_scenario_36_fewer_than_two_task_started_fails_before_any_stop(tmp_path: Path) -> None:
    frames = [AGENT_LAUNCH, TASK_STARTED_A, RESULT_SUCCESS]  # B never starts
    obs, control = _run_s36(tmp_path, frames)

    assert obs["note"] == "both task_started frames never arrived in turn 1"
    assert obs["task_started_count"] == 1
    assert control.stopped_task_ids == []
    assert obs["pass"] is False


def test_scenario_36_cannot_tell_agents_apart_fails_before_any_stop(tmp_path: Path) -> None:
    unmatched_launch = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "Agent",
                    "input": {"prompt": "do work"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "Agent",
                    "input": {"prompt": "do other work"},
                },
            ]
        },
    }
    frames = [unmatched_launch, TASK_STARTED_A, TASK_STARTED_B, RESULT_SUCCESS]
    obs, control = _run_s36(tmp_path, frames)

    assert obs["note"] == "could not tell agent A from agent B"
    assert control.stopped_task_ids == []
    assert obs["pass"] is False


def test_scenario_36_b_not_completed_fails(tmp_path: Path) -> None:
    task_notification_b_failed = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_b",
        "status": "failed",
    }
    tail = [TASK_UPDATED_A_STOPPED, task_notification_b_failed, EXIT_FRAME]
    obs, _ = _run_s36(tmp_path, TURN1_HAPPY + TURN2_HAPPY + tail)

    assert obs["stop_outcome"] == "reply"
    assert obs["a_task_updated_statuses"] == ["killed"]
    assert obs["b_task_notification_statuses"] == ["failed"]
    assert obs["b_completed"] is False
    assert obs["pass"] is False


# --- the tail frames: a_stopped and b_completed both read either source (task_notification
# or task_updated), since a background task may end only through task_updated and its
# task_notification may be suppressed ---------------------------------------------------------


def test_scenario_36_killed_update_and_stopped_notification_both_mark_a_stopped(
    tmp_path: Path,
) -> None:
    tail = [
        TASK_UPDATED_A_STOPPED,
        TASK_NOTIFICATION_A_STOPPED,
        TASK_UPDATED_B_COMPLETED,
        TASK_NOTIFICATION_B_DONE,
        EXIT_FRAME,
    ]
    obs, _ = _run_s36(tmp_path, TURN1_HAPPY + TURN2_HAPPY + tail)

    assert obs["a_task_updated_statuses"] == ["killed"]
    assert obs["a_task_notification_statuses"] == ["stopped"]
    assert obs["b_completed"] is True
    assert obs["pass"] is True


def test_scenario_36_killed_update_alone_marks_a_stopped_when_the_notification_is_suppressed(
    tmp_path: Path,
) -> None:
    tail = [TASK_UPDATED_A_STOPPED, TASK_UPDATED_B_COMPLETED, TASK_NOTIFICATION_B_DONE, EXIT_FRAME]
    obs, _ = _run_s36(tmp_path, TURN1_HAPPY + TURN2_HAPPY + tail)

    assert obs["a_task_updated_statuses"] == ["killed"]
    assert obs["a_task_notification_statuses"] == []
    assert obs["b_completed"] is True
    assert obs["pass"] is True


def test_scenario_36_b_completes_by_task_updated_alone_with_no_notification(
    tmp_path: Path,
) -> None:
    tail = [
        TASK_UPDATED_A_STOPPED,
        TASK_NOTIFICATION_A_STOPPED,
        TASK_UPDATED_B_COMPLETED,
        EXIT_FRAME,
    ]
    obs, _ = _run_s36(tmp_path, TURN1_HAPPY + TURN2_HAPPY + tail)

    assert obs["b_task_notification_statuses"] == []
    assert obs["b_task_updated_statuses"] == ["completed"]
    assert obs["b_completed"] is True
    assert obs["pass"] is True


def test_scenario_36_a_stopped_by_notification_alone_with_no_update(
    tmp_path: Path,
) -> None:
    tail = [
        TASK_NOTIFICATION_A_STOPPED,
        TASK_UPDATED_B_COMPLETED,
        TASK_NOTIFICATION_B_DONE,
        EXIT_FRAME,
    ]
    obs, _ = _run_s36(tmp_path, TURN1_HAPPY + TURN2_HAPPY + tail)

    assert obs["a_task_updated_statuses"] == []
    assert obs["a_task_notification_statuses"] == ["stopped"]
    assert obs["b_completed"] is True
    assert obs["pass"] is True


# --- TappedTransport.write: the tap must show what claude receives, declaration included -------


async def test_tapped_transport_records_the_stop_declaration_on_initialize(tmp_path: Path) -> None:
    """`write` used to tap the raw line before `declare_per_task_stop` ran, so a capture showed
    initialize without `perTaskStopAffordance` although the CLI received it."""
    rec = capture.Recorder(tmp_path / "tap.jsonl", "test")
    transport = capture.TappedTransport(
        ["claude.exe"],
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: None,
        tap=rec,
        spawner=FakeSpawner([]),
        grace_s=0.05,
        poll_s=0.05,
    )
    await transport.connect()
    request = {"subtype": "initialize", "hooks": None}
    line = json.dumps({"type": "control_request", "request_id": "req_1", "request": request}) + "\n"
    try:
        await transport.write(line)
    finally:
        await transport.close()
        rec.close()
    lines = (tmp_path / "tap.jsonl").read_text(encoding="utf-8").splitlines()
    taps = [(entry["dir"], entry["frame"]) for entry in (json.loads(line) for line in lines)]
    assert taps == [
        (
            "in",
            {
                "type": "control_request",
                "request_id": "req_1",
                "request": {**request, "perTaskStopAffordance": True},
            },
        )
    ]
