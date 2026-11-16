"""capture.py's scenario 35 (steer spike): registration, named-only status, and the mid-turn
send it records — never run live here (a live run needs a maintainer's go); these feed it
synthetic frames through the real `Capture.collect`."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

TESTS = Path(__file__).parents[1]
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import capture  # noqa: E402


def test_scenario_35_is_registered_and_named_only() -> None:
    assert "35" in capture.SCENARIOS
    assert "35" in capture.NAMED_ONLY
    default = [
        key
        for key in capture.SCENARIOS
        if key not in capture.REFUSED and key not in capture.NAMED_ONLY
    ]
    assert "35" not in default


def test_scenario_35_source_calls_control_send_twice() -> None:
    _title, fn = capture.SCENARIOS["35"]
    assert inspect.getsource(fn).count("control.send(") == 2


class _FakeControl35:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, turn: str, session_id: str) -> None:
        self.sent.append((turn, session_id))


class _FakeLive35:
    def __init__(self, control: _FakeControl35, frames: list[Any]) -> None:
        self.control = control
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.seen: list[Any] = []
        for frame in frames:
            self.inbox.put_nowait(frame)


class _FakeCapture35:
    """Real Capture.collect (it never reads self); a short bound replaces s35's 90 s tail wait."""

    def __init__(self, live: _FakeLive35, cfg: Any, root: Path, bound: float) -> None:
        self.cfg = cfg
        self.root = root
        self._live = live
        self._bound = bound
        self.closed: list[Any] = []

    async def open(self, rec: capture.Recorder, launch: Any) -> _FakeLive35:
        return self._live

    async def collect(self, live: _FakeLive35, **kwargs: Any) -> list[Any]:
        kwargs["timeout"] = self._bound
        return await capture.Capture.collect(cast(capture.Capture, self), live, **kwargs)  # type: ignore[arg-type]

    async def close(self, live: _FakeLive35) -> None:
        self.closed.append(live)


def test_scenario_35_joins_the_running_turn_and_reads_transcript_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steer_text = "After that, also reply with the word steered."
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "attachment", "attachment": {"type": "hook_success"}},
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": steer_text}]},
            "isMeta": False,
        },
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    monkeypatch.setattr(capture, "_transcript", lambda cfg, root, sid: transcript)

    tool_use = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "PowerShell", "input": {}}]},
    }
    tool_result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "slept"}]},
    }
    final_reply = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "done, steered"}]},
    }
    result_frame = {"type": "result", "subtype": "success", "is_error": False}

    control = _FakeControl35()
    live = _FakeLive35(control, [tool_use, tool_result, final_reply, result_frame])
    cap = _FakeCapture35(live, cfg=None, root=tmp_path, bound=0.05)
    rec = capture.Recorder(tmp_path / "35.jsonl", "35")

    _title, fn = capture.SCENARIOS["35"]
    obs = asyncio.run(fn(cast(capture.Capture, cap), rec))
    rec.close()

    # two sends: the PowerShell turn, then the mid-turn steer line, same session id both times.
    assert len(control.sent) == 2
    assert "Start-Sleep -Seconds 8; Write-Output slept" in control.sent[0][0]
    assert control.sent[1] == (steer_text, obs["session_id"])

    assert obs["pass"] is True
    assert obs["result_count"] == 1
    assert obs["after_types"] == ["user", "assistant", "result/success"]
    assert len(obs["after_user_frames"]) == 1
    assert obs["steered_before_first_result"] is True
    assert obs["asked"] == []

    assert obs["queue_operation_entries"] == 1
    assert obs["attachment_entries"] == 1
    assert len(obs["steer_entries"]) == 1
    assert obs["steer_entries"][0]["type"] == "user"
    assert obs["steer_entries"][0]["isMeta"] is False
    assert obs["steer_entries"][0]["keys"] == ["isMeta", "message", "type"]

    assert cap.closed == [live]


def test_scenario_35_tail_timeout_yields_empty_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the result, nothing more arrives: the 90 s tail collect times out and the scenario
    still reports the one result it already has (steer and queue-op counts out of scope here)."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(capture, "_transcript", lambda cfg, root, sid: transcript)

    tool_use = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "PowerShell", "input": {}}]},
    }
    result_frame = {"type": "result", "subtype": "success", "is_error": False}

    control = _FakeControl35()
    live = _FakeLive35(control, [tool_use, result_frame])
    cap = _FakeCapture35(live, cfg=None, root=tmp_path, bound=0.05)
    rec = capture.Recorder(tmp_path / "35.jsonl", "35")

    _title, fn = capture.SCENARIOS["35"]
    obs = asyncio.run(fn(cast(capture.Capture, cap), rec))
    rec.close()

    assert obs["result_count"] == 1
    assert obs["after_types"] == ["result/success"]
    # "steered" never arrived, so pass is False and the ordering check is False, not a crash.
    assert obs["pass"] is False
    assert obs["steered_before_first_result"] is False


# --- scenario 38 (profile-chat steer): a fold probe and a Stop probe, each its own session ------

CANARY_REPLY = object()  # in `after`: _FakeControl38.send() swaps this for an echo of `turn`
# (the real canary, unknown ahead of time), so a test can place the canary-bearing frame
# anywhere in its own `after` list instead of always right after the lifecycle frames.


class _FakeControl38:
    """Records every `send(turn, session_id, steer_id)`. A steer send (`steer_id` given) queues
    the frames that follow it onto the live session's inbox: one `command_lifecycle` frame per
    state in `lifecycle_states`, in order (Claude Code 2.1.281's real run queued the steer
    before marking it started and completed), each naming whatever steer id it was actually
    given (unknown ahead of time, since scenario 38 generates it itself); the echoed reply
    carries `turn` so the canary lands without the test having to know its random value."""

    def __init__(
        self,
        inbox: asyncio.Queue[Any],
        *,
        lifecycle_states: list[str] | None,
        after: list[Any],
        reply_text: str | None = None,
    ) -> None:
        self._inbox = inbox
        self._lifecycle_states = lifecycle_states or []
        self._after = after
        self._reply_text = reply_text  # None echoes `turn` (so it carries the real canary)
        self.sent: list[tuple[str, str, str | None]] = []
        self.interrupted = 0

    async def send(self, turn: str, session_id: str, *, steer_id: str | None = None) -> None:
        self.sent.append((turn, session_id, steer_id))
        if steer_id is None:
            return
        for state in self._lifecycle_states:
            self._inbox.put_nowait(
                {"type": "command_lifecycle", "command_uuid": steer_id, "state": state}
            )
        text = turn if self._reply_text is None else self._reply_text
        self._inbox.put_nowait(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        )
        for frame in self._after:
            if frame is CANARY_REPLY:
                self._inbox.put_nowait(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": turn}]}}
                )
            else:
                self._inbox.put_nowait(frame)

    async def interrupt(self) -> None:
        self.interrupted += 1


class _FakeLive38:
    def __init__(self, seed: list[Any]) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.seen: list[Any] = []
        for frame in seed:
            self.inbox.put_nowait(frame)
        self.control: _FakeControl38 | None = None  # set by the caller, which owns the inbox


class _FakeCapture38:
    """Real Capture.collect; `open()` hands out pre-built lives in call order (fold, then
    stop) and records each call's kwargs. A short bound replaces scenario 38's real 60 s/120 s
    tail timeouts."""

    def __init__(self, lives: list[_FakeLive38], cfg: Any, root: Path, bound: float) -> None:
        self.cfg = cfg
        self.root = root
        self._lives = list(lives)
        self._bound = bound
        self.closed: list[Any] = []
        self.opened_with: list[dict[str, Any]] = []

    async def open(self, rec: capture.Recorder, launch: Any, **kwargs: Any) -> _FakeLive38:
        self.opened_with.append(kwargs)
        return self._lives.pop(0)

    async def collect(self, live: _FakeLive38, **kwargs: Any) -> list[Any]:
        kwargs["timeout"] = self._bound
        return await capture.Capture.collect(cast(capture.Capture, self), live, **kwargs)  # type: ignore[arg-type]

    async def close(self, live: _FakeLive38) -> None:
        self.closed.append(live)


def _init38(version: str) -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "claude_code_version": version}


def _tool_use38() -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "PowerShell", "input": {}}]},
    }


def _result38(subtype: str = "success") -> dict[str, Any]:
    return {"type": "result", "subtype": subtype, "is_error": subtype != "success"}


def _probe38(
    *,
    version: str,
    lifecycle_states: list[str] | None,
    after: list[Any],  # dict frames, or the CANARY_REPLY sentinel
    pending_result: dict[str, Any] | None = None,
    reply_text: str | None = None,
) -> _FakeLive38:
    """A probe's seed frames: its turn's own init and tool_use, plus (for the Stop probe) the
    result already waiting in the inbox when its interrupt lands. `after` is what the steer
    send queues once scenario 38 calls it (the fold's own result, or the Stop steer's own new
    turn and result). `pending_result` omitted puts the Stop probe's own result *after* the
    steer's lifecycle frames instead of before them -- the first live run's shape, where Claude
    Code marked the steer started before the interrupted turn's own result arrived."""
    seed = [_init38(version), _tool_use38()]
    if pending_result is not None:
        seed.append(pending_result)
    live = _FakeLive38(seed)
    live.control = _FakeControl38(
        live.inbox, lifecycle_states=lifecycle_states, after=after, reply_text=reply_text
    )
    return live


def _run_scenario_38(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fold_live: _FakeLive38,
    stop_live: _FakeLive38,
) -> dict[str, Any]:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(capture, "_transcript", lambda cfg, root, sid: transcript)
    monkeypatch.setattr(capture, "STOP_DELAY_S", 0)

    cap = _FakeCapture38([fold_live, stop_live], cfg=None, root=tmp_path, bound=0.05)
    rec = capture.Recorder(tmp_path / "38.jsonl", "38")
    _title, fn = capture.SCENARIOS["38"]
    obs = asyncio.run(fn(cast(capture.Capture, cap), rec))
    rec.close()
    return obs


def test_scenario_38_is_registered_and_named_only() -> None:
    assert "38" in capture.SCENARIOS
    assert "38" in capture.NAMED_ONLY
    default = [
        key
        for key in capture.SCENARIOS
        if key not in capture.REFUSED and key not in capture.NAMED_ONLY
    ]
    assert "38" not in default


def test_scenario_38_passes_with_steer_ids_and_no_prompt_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["fold_pass"] is True
    assert obs["stop_pass"] is True
    assert obs["pass"] is True
    assert obs["native_version"] == capture.FIXTURE_VERSION

    fold_sent = cast(_FakeControl38, fold_live.control).sent
    stop_sent = cast(_FakeControl38, stop_live.control).sent
    assert len(fold_sent) == 2
    assert len(stop_sent) == 2
    assert fold_sent[0][2] is None
    assert stop_sent[0][2] is None
    fold_steer_id, stop_steer_id = fold_sent[1][2], stop_sent[1][2]
    assert fold_steer_id and stop_steer_id and fold_steer_id != stop_steer_id

    dumped = json.dumps(obs, ensure_ascii=False)
    for leaked in (
        "Start-Sleep",
        "Write-Output",
        "Run exactly this PowerShell command",
        "Also reply with the word",
        "STEER_",
    ):
        assert leaked not in dumped


def test_scenario_38_fold_fails_without_the_lifecycle_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=None, after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["fold"]["lifecycle"] == []
    assert obs["fold_pass"] is False
    assert obs["stop_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_stop_fails_when_the_lifecycle_reads_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["cancelled"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["fold_pass"] is True
    assert obs["stop_pass"] is False
    assert obs["pass"] is False


def test_scenario_38_pass_is_false_when_init_reports_another_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version="2.1.259", lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["fold_pass"] is True
    assert obs["stop_pass"] is True
    assert obs["native_version"] == "2.1.259"
    assert obs["pass"] is False


def test_scenario_38_fold_passes_when_queued_precedes_started_and_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live run (20261106-114407, CLI 2.1.281) queued the steer before marking it started
    and completed; fold_pass must look at every lifecycle entry for this steer id, not just the
    first one in arrival order."""
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["queued", "started", "completed"],
        after=[_result38("success")],
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert [entry["state"] for entry in obs["fold"]["lifecycle"]] == [
        "queued",
        "started",
        "completed",
    ]
    assert obs["fold_pass"] is True
    assert obs["pass"] is True


def test_scenario_38_both_probes_open_default_mode_with_the_8s_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code 2.1.281 refuses a foreground `Start-Sleep 30` at once ("Blocked: ... use
    Monitor", live run 20261106-114407), so both probes must open in `default` mode and send
    the same 8 s step the Fold probe actually completed with."""
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(capture, "_transcript", lambda cfg, root, sid: transcript)
    monkeypatch.setattr(capture, "STOP_DELAY_S", 0)

    cap = _FakeCapture38([fold_live, stop_live], cfg=None, root=tmp_path, bound=0.05)
    rec = capture.Recorder(tmp_path / "38.jsonl", "38")
    _title, fn = capture.SCENARIOS["38"]
    asyncio.run(fn(cast(capture.Capture, cap), rec))
    rec.close()

    assert [kwargs.get("mode") for kwargs in cap.opened_with] == ["default", "default"]
    for live in (fold_live, stop_live):
        first_send = cast(_FakeControl38, live.control).sent[0][0]
        assert "Start-Sleep -Seconds 8; Write-Output slept" in first_send


# The six tests below each break one clause of fold_pass/stop_pass that every fixture above
# happens to satisfy regardless (one fold result, the canary right after the lifecycle frames,
# the Stop probe's aborted result pre-seeded ahead of the steer): every clause could be dropped
# from capture.py and these seven tests would stay green. Each one below isolates a single
# clause by breaking only it, with the other probe left in its ordinary passing shape.


def test_scenario_38_fold_fails_with_a_second_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_result38("success"), _result38("success")],
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert len(obs["fold"]["results"]) == 2
    assert obs["fold_pass"] is False
    assert obs["stop_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_stop_fails_when_started_precedes_the_first_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first live run's shape (2026-11-06): Claude Code read the steer and marked it
    started before the interrupted turn's own result ever arrived. The immediate reply says
    "done" (no canary) and `CANARY_REPLY` places the real canary between the two results, so
    only the started-vs-first-result ordering is wrong -- every other clause still holds."""
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[
            _result38("error_during_execution"),
            CANARY_REPLY,
            _init38(capture.FIXTURE_VERSION),
            _result38("success"),
        ],
        # No pending_result: the Stop probe's own result now arrives inside `after`, behind the
        # steer's lifecycle frame instead of ahead of it.
        reply_text="done",
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["stop"]["lifecycle"][0]["index"] < obs["stop"]["results"][0]["index"]
    assert (
        obs["stop"]["results"][0]["index"]
        < obs["stop"]["canary_at"]
        < obs["stop"]["results"][1]["index"]
    )
    assert obs["stop_pass"] is False
    assert obs["fold_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_fold_fails_without_the_canary_in_any_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_result38("success")],
        reply_text="done",
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["fold"]["canary_at"] is None
    assert obs["fold_pass"] is False
    assert obs["stop_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_stop_fails_without_the_canary_in_any_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_init38(capture.FIXTURE_VERSION), _result38("success")],
        pending_result=_result38("aborted"),
        reply_text="done",
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert obs["stop"]["canary_at"] is None
    assert obs["stop_pass"] is False
    assert obs["fold_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_stop_fails_without_an_init_between_started_and_the_second_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[_result38("success")],  # no second system/init in this batch
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert len(obs["stop"]["inits"]) == 1
    assert obs["stop_pass"] is False
    assert obs["fold_pass"] is True
    assert obs["pass"] is False


def test_scenario_38_stop_fails_with_only_one_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fold_live = _probe38(
        version=capture.FIXTURE_VERSION, lifecycle_states=["started"], after=[_result38("success")]
    )
    stop_live = _probe38(
        version=capture.FIXTURE_VERSION,
        lifecycle_states=["started"],
        after=[],  # the steer's own turn never produces a second result
        pending_result=_result38("aborted"),
    )

    obs = _run_scenario_38(tmp_path, monkeypatch, fold_live, stop_live)

    assert len(obs["stop"]["results"]) == 1
    assert obs["stop_pass"] is False
    assert obs["fold_pass"] is True
    assert obs["pass"] is False
