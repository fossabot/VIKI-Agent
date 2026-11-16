"""capture.py's app-MCP tooling: the synthetic stdio server and the generated config writer."""

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

from viki_agent import winproc  # noqa: E402
from viki_agent.app import diagnostics  # noqa: E402
from viki_agent.profiles import mcp_config  # noqa: E402


def _line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


def _skip_if_secret_shaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_mcp_config refuses a path holding a run of 32+ word characters as secret_value, and a
    pytest tmp_path can be that long on some machines or checkouts. It writes sys.executable as the
    server's command and the schema screens that too, so the tests pin it to a short name (none of
    them starts a process); only tmp_path can still make them skip."""
    monkeypatch.setattr(sys, "executable", "python")
    if diagnostics._looks_secret(str(tmp_path)):
        pytest.skip("tmp_path looks secret-shaped to the schema's screen")


def test_echo_server_answers_initialize_list_and_call(tmp_path: Path) -> None:
    server = capture.write_echo_server(tmp_path)
    requests = (
        _line(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        + _line({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + _line({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + _line(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hello"}},
            }
        )
        + _line({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        + _line({"jsonrpc": "2.0", "id": 5, "method": "not/a/real/method"})
        + _line(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "日本語 café"}},
            }
        )
    )
    done = winproc.run([sys.executable, str(server)], cwd=tmp_path, input=requests)
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert len(replies) == 6
    assert replies[0]["result"] == {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "viki-echo", "version": "1"},
    }
    assert replies[1]["result"]["tools"] == [
        {
            "name": "echo",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]
    assert replies[2]["result"] == {"content": [{"type": "text", "text": "hello"}]}
    assert replies[3]["result"] == {}
    assert replies[4]["error"]["code"] == -32601
    assert replies[5]["result"] == {"content": [{"type": "text", "text": "日本語 café"}]}


def test_echo_server_reads_the_variant_flag(tmp_path: Path) -> None:
    server = capture.write_echo_server(tmp_path)
    request = _line(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1"}}
    )
    done = winproc.run(
        [sys.executable, str(server), "--variant=project"], cwd=tmp_path, input=request
    )
    reply = json.loads(done.stdout.strip())
    assert reply["result"]["serverInfo"] == {"name": "viki-echo", "version": "project"}


def test_write_mcp_config_under_space_and_japanese_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_if_secret_shaped(tmp_path, monkeypatch)
    server = tmp_path / "echo_server.py"
    server.write_text("", encoding="utf-8")
    folder = tmp_path / "mcp 日本語"
    value = {
        "servers": [
            {
                "name": "echo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server)],
                "url": None,
                "note": None,
            }
        ]
    }
    path = capture.write_mcp_config(folder, server)
    assert path.is_absolute()
    assert path.parent == (folder / "mcp").absolute()
    assert path.name.endswith(".json")
    assert len(path.stem) == 64
    assert path.read_bytes() == mcp_config.config_bytes(value)


def test_main_runs_default_keys_with_no_named_only_or_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves main()'s own default-key filter, not a copy of it: a fake `run` records exactly the
    keys main() decided to pass it."""
    recorded: dict[str, list[str]] = {}

    async def fake_run(keys: list[str]) -> int:
        recorded["keys"] = keys
        return 0

    monkeypatch.setattr(capture, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["capture.py"])

    with pytest.raises(SystemExit) as exc_info:
        capture.main()

    assert exc_info.value.code == 0
    keys = recorded["keys"]
    assert not (set(keys) & set(capture.NAMED_ONLY))
    assert not (set(keys) & set(capture.REFUSED))
    assert {"29", "30", "31", "32", "33"}.isdisjoint(keys)
    assert {"29", "30", "31"} <= set(capture.SCENARIOS)
    for scenario_key, (_title, fn) in capture.SCENARIOS.items():
        if "mcp_config=" in inspect.getsource(fn):
            assert scenario_key in capture.NAMED_ONLY


class _FakeControl:
    def __init__(
        self,
        statuses: list[dict[str, Any]],
        usage: dict[str, Any],
        config_dir: Path | None = None,
    ) -> None:
        self._statuses = iter(statuses)
        self._usage = usage
        self._config_dir = config_dir
        self.config_existed_at_call: list[bool] = []

    async def mcp_status(self) -> dict[str, Any]:
        if self._config_dir is not None:
            self.config_existed_at_call.append(any(self._config_dir.glob("*.json")))
        return next(self._statuses)

    async def context_usage(self) -> dict[str, Any]:
        return self._usage


class _FakeTransport:
    def stderr_tail(self) -> str:
        return ""


class _FakeLive:
    def __init__(self, control: _FakeControl) -> None:
        self.control = control
        self.transport = _FakeTransport()


class _FakeCapture:
    def __init__(self, run_dir: Path, live: _FakeLive) -> None:
        self.run_dir = run_dir
        self._live = live
        self.root = run_dir
        self.opened_launch: Any = None

    async def open(self, rec: capture.Recorder, launch: Any) -> _FakeLive:
        self.opened_launch = launch
        return self._live

    async def close(self, live: _FakeLive) -> None:
        pass


def test_scenario_29_samples_five_times_and_reads_the_config_only_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_if_secret_shaped(tmp_path, monkeypatch)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    connected: dict[str, Any] = {
        "mcpServers": [
            {
                "name": "viki_echo",
                "status": "connected",
                "scope": "dynamic",
                # the SDK's real shape (McpToolInfo): a dict per tool, not a plain string
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echoes text back to the caller.",
                        "annotations": {"readOnly": True},
                    }
                ],
            }
        ]
    }
    control = _FakeControl(
        [connected] * 6,
        {
            "skills": {
                "totalSkills": 1,
                "includedSkills": 1,
                "tokens": 0,
                "skillFrontmatter": [{"name": "one", "source": "user", "tokens": 0}],
            },
            "systemTools": ["Read"],
        },
        config_dir=tmp_path / "data" / "mcp",
    )
    fake_live = _FakeLive(control)
    cap = _FakeCapture(tmp_path, fake_live)
    rec = capture.Recorder(tmp_path / "29.jsonl", "29")

    obs = asyncio.run(capture.s29(cast(capture.Capture, cap), rec))
    rec.close()

    assert len(obs["samples"]) == 5
    assert obs["after_delete_status"] == "connected"
    assert obs["pass"] is True
    assert obs["skills_shape"] == {
        "totalSkills": "int",
        "includedSkills": "int",
        "tokens": "int",
        "skillFrontmatter": [{"name": "str", "source": "str", "tokens": "int"}, "x1"],
    }
    assert all(s["tools"] == ["echo"] for s in obs["samples"])
    assert not any("description" in json.dumps(s) for s in obs["samples"])
    assert cap.opened_launch.mcp_config is not None
    assert Path(cap.opened_launch.mcp_config).is_absolute()
    # write_mcp_config's one file was written then unlink()ed by the scenario: none remain.
    assert list((tmp_path / "data" / "mcp").iterdir()) == []
    # the config file existed for the five samples and was gone by the sixth (post-delete) call.
    assert control.config_existed_at_call == [True, True, True, True, True, False]


class _FakeTransport32:
    def __init__(self, stderr: str) -> None:
        self._stderr = stderr

    def stderr_tail(self) -> str:
        return self._stderr


class _FakeLive32:
    def __init__(self, stderr: str = "No conversation found.") -> None:
        self.transport = _FakeTransport32(stderr)
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        for _ in range(2):
            self.inbox.put_nowait({"type": "system", "subtype": "hook_started"})
        # a non-hook frame the drain also sees: hook_frames must count only the hook ones.
        self.inbox.put_nowait({"type": "system", "subtype": "status"})


class _FakeCapture32:
    def __init__(self, live: _FakeLive32) -> None:
        self._live = live
        self._open = [live]
        self.closed: list[Any] = []

    async def open(self, rec: capture.Recorder, launch: Any) -> _FakeLive32:
        raise RuntimeError("no conversation found")

    async def close(self, live: _FakeLive32) -> None:
        self.closed.append(live)


def test_scenario_32_records_hook_frames_drained_before_no_conversation_found(
    tmp_path: Path,
) -> None:
    """A missing --resume id fails cap.open; the scenario reads the Live cap._open left behind
    (system/hook_started frames already queued) and the stderr that names the failure."""
    live = _FakeLive32()
    cap = _FakeCapture32(live)
    rec = capture.Recorder(tmp_path / "32.jsonl", "32")

    obs = asyncio.run(capture.s32(cast(capture.Capture, cap), rec))
    rec.close()

    assert obs["pass"] is True
    assert obs["no_conversation"] is True
    assert obs["hook_frames"] == 2
    assert obs["open_exception"] == "RuntimeError"
    assert cap.closed == [live]


def test_scenario_32_fails_without_no_conversation_found(tmp_path: Path) -> None:
    cap = _FakeCapture32(_FakeLive32("something else went wrong"))
    rec = capture.Recorder(tmp_path / "32.jsonl", "32")

    obs = asyncio.run(capture.s32(cast(capture.Capture, cap), rec))
    rec.close()

    assert obs["no_conversation"] is False
    assert obs["pass"] is False


def _status(state: str) -> dict[str, Any]:
    return {"mcpServers": [{"name": "viki_echo", "status": state, "scope": "dynamic"}]}


def _run_mcp_scenario(
    fn: Any,
    key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[dict[str, Any]],
) -> dict[str, Any]:
    _skip_if_secret_shaped(tmp_path, monkeypatch)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    cap = _FakeCapture(tmp_path, _FakeLive(_FakeControl(statuses, {})))
    rec = capture.Recorder(tmp_path / f"{key}.jsonl", key)
    obs = asyncio.run(fn(cast(capture.Capture, cap), rec))
    rec.close()
    return obs


def test_scenario_29_fails_when_no_sample_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = [_status("pending")] * 5 + [_status("connected")]
    obs = _run_mcp_scenario(capture.s29, "29", tmp_path, monkeypatch, statuses)

    assert obs["after_delete_status"] == "connected"
    assert obs["pass"] is False


def test_scenario_29_fails_when_not_connected_after_the_config_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = [_status("connected")] * 5 + [_status("failed")]
    obs = _run_mcp_scenario(capture.s29, "29", tmp_path, monkeypatch, statuses)

    assert obs["after_delete_status"] == "failed"
    assert obs["pass"] is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [({"mcpServers": []}, True), ({"mcpServers": {}}, False), ({}, False)],
)
def test_scenario_30_passes_only_for_a_list_of_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: dict[str, Any], expected: bool
) -> None:
    obs = _run_mcp_scenario(capture.s30, "30", tmp_path, monkeypatch, [status])

    assert obs["pass"] is expected


@pytest.mark.parametrize(("state", "expected"), [("connected", True), ("failed", False)])
def test_scenario_31_passes_only_when_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, expected: bool
) -> None:
    obs = _run_mcp_scenario(capture.s31, "31", tmp_path, monkeypatch, [_status(state)])

    assert obs["status"] == state
    assert obs["pass"] is expected


_DELTA = {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "x"}}}


HANG_GUARD_S = 5  # the bound for a collect that must finish its frames; only a hang reaches it


class _FakeControl33:
    def __init__(
        self, interrupt_error: Exception | None = None, interrupt_hangs: bool = False
    ) -> None:
        self._interrupt_error = interrupt_error
        self._interrupt_hangs = interrupt_hangs
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self._interrupt_hangs:
            await asyncio.Event().wait()
        if self._interrupt_error is not None:
            raise self._interrupt_error


class _FakeLive33:
    def __init__(self, control: _FakeControl33, frames: list[Any]) -> None:
        self.control = control
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.seen: list[Any] = []
        for frame in frames:
            self.inbox.put_nowait(frame)


class _FakeCapture33:
    """Real Capture.collect (it never reads self), `bound` seconds in place of s33's 180 s."""

    def __init__(self, live: _FakeLive33, bound: float) -> None:
        self._live = live
        self._bound = bound

    async def open(self, rec: capture.Recorder, launch: Any) -> _FakeLive33:
        return self._live

    async def turn(self, live: _FakeLive33, text: str) -> list[Any]:
        return []

    async def collect(self, live: _FakeLive33, **kwargs: Any) -> list[Any]:
        kwargs["timeout"] = self._bound
        return await capture.Capture.collect(cast(capture.Capture, self), live, **kwargs)  # type: ignore[arg-type]

    async def close(self, live: _FakeLive33) -> None:
        pass


def _run_s33(
    tmp_path: Path,
    frames: list[Any],
    interrupt_error: Exception | None = None,
    bound: float = HANG_GUARD_S,
    interrupt_hangs: bool = False,
) -> tuple[dict[str, Any], _FakeControl33]:
    control = _FakeControl33(interrupt_error, interrupt_hangs)
    cap = _FakeCapture33(_FakeLive33(control, frames), bound)
    rec = capture.Recorder(tmp_path / "33.jsonl", "33")
    obs = asyncio.run(capture.s33(cast(capture.Capture, cap), rec))
    rec.close()
    return obs, control


def test_scenario_33_delta_ack_and_aborted_result_pass(tmp_path: Path) -> None:
    ack = {"type": "system", "subtype": "status"}
    result = {"type": "result", "subtype": "error_during_execution", "is_error": True}
    obs, control = _run_s33(tmp_path, [_DELTA, ack, result])

    assert control.interrupts == 1
    assert obs["pass"] is True
    assert obs["interrupt_sent"] is True
    assert obs["interrupt_ok"] is True
    assert obs["interrupt_error"] is None
    assert obs["collect_timed_out"] is False
    assert obs["result_subtype"] == "error_during_execution"


def test_scenario_33_process_exit_without_a_result_passes(tmp_path: Path) -> None:
    obs, _ = _run_s33(tmp_path, [_DELTA, {"type": capture.EXIT}])

    assert obs["pass"] is True
    assert obs["process_exited"] is True
    assert obs["result_subtype"] is None


def test_scenario_33_neither_result_nor_exit_fails(tmp_path: Path) -> None:
    """A collect stopped by another route (here the timeout after the interrupt) proves nothing."""
    obs, _ = _run_s33(tmp_path, [_DELTA], bound=0.02)

    assert obs["pass"] is False
    assert obs["interrupt_sent"] is True
    assert obs["interrupt_ok"] is True
    assert obs["collect_timed_out"] is True


def test_scenario_33_interrupt_cut_off_by_the_collect_bound_is_not_ok(tmp_path: Path) -> None:
    """The interrupt call never returns, so the collect's bound ends it: sent, but not ok."""
    obs, control = _run_s33(tmp_path, [_DELTA], bound=0.02, interrupt_hangs=True)

    assert control.interrupts == 1
    assert obs["pass"] is False
    assert obs["interrupt_sent"] is True
    assert obs["interrupt_ok"] is False
    assert obs["interrupt_error"] is None
    assert obs["collect_timed_out"] is True


def test_scenario_33_interrupt_timeout_names_the_error(tmp_path: Path) -> None:
    obs, control = _run_s33(tmp_path, [_DELTA], TimeoutError("interrupt stalled"))

    assert control.interrupts == 1
    assert obs["pass"] is False
    assert obs["interrupt_sent"] is True
    assert obs["interrupt_ok"] is False
    assert obs["interrupt_error"] == "TimeoutError: interrupt stalled"
    assert obs["collect_timed_out"] is False


def test_scenario_33_collect_timeout_before_any_delta(tmp_path: Path) -> None:
    obs, control = _run_s33(tmp_path, [{"type": "system", "subtype": "status"}], bound=0.02)

    assert control.interrupts == 0
    assert obs["pass"] is False
    assert obs["interrupt_sent"] is False
    assert obs["interrupt_ok"] is False
    assert obs["interrupt_error"] is None
    assert obs["collect_timed_out"] is True
