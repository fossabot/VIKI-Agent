"""ClaudeTransport and the control glue: framing, routing, deadlock freedom, ending a process."""

from __future__ import annotations

import asyncio
import json
import json.scanner
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    CLIConnectionError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ToolPermissionContext,
)

from viki_agent import winproc
from viki_agent.drivers.claude.control import ClaudeControl, Launch, build_argv
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.drivers.claude.transport import (
    EXIT,
    FRAME_ERROR,
    ClaudeTransport,
    LineFramer,
    parse_line,
)
from viki_agent.turn_input import Attachment, AttachmentUnavailable, TurnInput

from replay import (
    DEFAULT_INIT,
    SID,
    Entry,
    FakeSpawner,
    expect,
    init_frame,
    out,
    permission_request,
    permission_response_in,
    result_frame,
    text_delta,
    user_in,
)

FAST = {"grace_s": 0.05, "poll_s": 0.05}  # injected timers for in-process tests


class Frames(list[dict[str, Any]]):
    """A sink that records frames and lets a test wait for one without polling."""

    def __init__(self) -> None:
        super().__init__()
        self._changed = asyncio.Event()

    def __call__(self, frame: dict[str, Any], source_bytes: int = 0) -> None:
        self.append(frame)
        self._changed.set()

    async def until(self, frame_type: str, timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not any(f["type"] == frame_type for f in self):
                self._changed.clear()
                await self._changed.wait()

    @property
    def types(self) -> list[str]:
        return [f["type"] for f in self]


# --- framing ------------------------------------------------------------------------------------


def _lines(framer: LineFramer, data: bytes, size: int) -> list[bytes | None]:
    return [line for i in range(0, len(data), size) for line in framer.push(data[i : i + size])]


def test_framer_reassembles_japanese_split_byte_by_byte() -> None:
    data = '{"text":"日本語のテスト"}\n'.encode()
    lines = _lines(LineFramer(1024), data, 1)
    assert len(lines) == 1 and lines[0] is not None
    assert parse_line(lines[0]) == {"text": "日本語のテスト"}


def test_framer_handles_a_5_mib_frame_in_64_kib_chunks() -> None:
    big = "界" * (5 * 1024 * 1024 // 3)
    data = json.dumps({"big": big}, ensure_ascii=False).encode() + b"\n"
    lines = _lines(LineFramer(32 * 1024 * 1024), data, 64 * 1024)
    assert len(lines) == 1 and lines[0] is not None
    assert parse_line(lines[0]) == {"big": big}


def test_framer_drops_oversize_lines_once_and_recovers() -> None:
    framer = LineFramer(10)
    assert _lines(framer, b"x" * 8, 8) == []
    assert _lines(framer, b"x" * 8, 8) == [None]  # crossed the cap mid-line
    assert _lines(framer, b"xx\n" + b'{"a":1}\n', 64) == [b'{"a":1}']


def test_parse_line_tolerates_crlf_noise_and_reports_bad_json() -> None:
    assert parse_line(b'{"a":1}\r') == {"a": 1}
    assert parse_line(b"[SandboxDebug] hello") is None
    assert parse_line(b"   ") is None
    assert parse_line(b'{"a":') == {"type": FRAME_ERROR, "reason": "bad_json"}


def test_nested_json_cannot_abort_the_reader_or_forge_lifecycle_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth = sys.getrecursionlimit() + 100
    nested = b'{"nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    # Python 3.14's C decoder accepts this nesting; the stdlib fallback still uses recursion.
    # These runtime stdlib hooks are intentionally absent from the public typing stubs.
    decoder: Any = json.JSONDecoder()
    scanner: Any = json.scanner
    decoder.scan_once = scanner.py_make_scanner(decoder)
    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", decoder.decode)
        assert parse_line(nested) == {"type": FRAME_ERROR, "reason": "bad_json"}
    for kind in (EXIT, FRAME_ERROR, "viki.card", "viki.overflow", []):
        assert parse_line(json.dumps({"type": kind}).encode()) == {
            "type": FRAME_ERROR,
            "reason": "bad_json",
        }


def test_framer_flush_returns_an_unterminated_tail() -> None:
    framer = LineFramer(64)
    assert list(framer.push(b'{"last":true}')) == []
    assert framer.flush() == b'{"last":true}'


# --- the transport with the SDK's Query, over the in-process fake -----------------------------


async def _allow(
    tool: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    return PermissionResultAllow()


def _control(
    spawner: FakeSpawner,
    sink: Callable[[dict[str, Any]], None],
    can_use_tool: Any = _allow,
    **transport_options: Any,
) -> tuple[ClaudeControl, ClaudeTransport]:
    argv = build_argv(["claude.exe"], Launch(SID), mode="auto", effort="low")
    options = {**FAST, **transport_options}
    transport = ClaudeTransport(
        argv,
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: sink(frame),
        spawner=spawner,
        **options,
    )
    return ClaudeControl(transport, can_use_tool, control_timeout_s=0.05), transport


async def test_initialize_goes_to_query_and_the_turn_to_the_sink() -> None:
    script = [user_in(), out(init_frame()), out(text_delta("Hello there")), out(result_frame())]
    frames = Frames()
    control, _ = _control(FakeSpawner(script), frames)
    info = await control.start()
    assert info["account"] == DEFAULT_INIT["account"]
    await control.send("hello", SID)
    await frames.until("result")
    assert frames.types == ["system", "stream_event", "result"]
    await control.close()


async def test_typed_image_turn_uses_the_public_iterable_query_shape() -> None:
    class CapturingClient:
        request: object | None = None
        session_id: str | None = None

        async def query(self, request: object, *, session_id: str) -> None:
            self.session_id = session_id
            self.request = [item async for item in request]  # type: ignore[union-attr]

    frames = Frames()
    control, _ = _control(FakeSpawner([]), frames)
    client = CapturingClient()
    control._client = client  # type: ignore[assignment]  # isolate public query input construction
    attachment = Attachment(
        "00000000-0000-4000-8000-000000000012", "image/png", 4, 1, 1, "synthetic.png"
    )
    resolved: list[Attachment] = []

    async def resolve(value: Attachment) -> bytes:
        resolved.append(value)
        return b"\x89PNG"

    await control.send(TurnInput("describe this", (attachment,)), SID, resolve)
    assert resolved == [attachment] and client.session_id == SID
    assert client.request == [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw=="},
                    },
                ],
            },
        }
    ]


_IMAGE = Attachment("00000000-0000-4000-8000-000000000013", "image/png", 4, 1, 1, "synthetic.png")


@pytest.mark.parametrize("failure", ["raises", "short", "not_bytes", "no_resolver", "cancelled"])
async def test_an_unavailable_attachment_is_refused_before_query(failure: str) -> None:
    class RefusingClient:
        async def query(self, request: object, *, session_id: str) -> None:
            pytest.fail("query reached with an unavailable attachment")

    control, _ = _control(FakeSpawner([]), Frames())
    control._client = RefusingClient()  # type: ignore[assignment]

    async def resolve(value: Attachment) -> Any:
        if failure == "raises":
            raise OSError("synthetic blob gone")
        if failure == "cancelled":
            raise asyncio.CancelledError
        return b"\x89PN" if failure == "short" else "\x89PNG"

    expected = asyncio.CancelledError if failure == "cancelled" else AttachmentUnavailable
    with pytest.raises(expected):
        await control.send(
            TurnInput("describe this", (_IMAGE,)),
            SID,
            None if failure == "no_resolver" else resolve,
        )


async def test_a_refused_send_disarms_a_stop_asked_while_the_images_were_read() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(value: Attachment) -> bytes:
        entered.set()
        await release.wait()
        raise OSError("synthetic blob gone")

    spawner = FakeSpawner([])
    driver = ClaudeDriver(
        Launch(SID),
        mode="auto",
        effort="low",
        model=None,
        config=DriverConfig(
            cli=("claude.exe",),
            vault=Path.cwd(),
            env={},
            spawner=spawner,
            stop_pickup_s=0.05,
            resolve_attachment=resolve,
        ),
        emit=lambda event: None,
    )
    sending = asyncio.create_task(driver.send_turn(TurnInput("", (_IMAGE,))))
    await asyncio.wait_for(entered.wait(), 0.05)
    await driver.interrupt()  # the CLI has not entered the turn, so the stop is armed
    release.set()
    with pytest.raises(AttachmentUnavailable):
        await asyncio.wait_for(sending, 0.05)
    assert not driver.busy
    assert not driver._stop_wanted and driver._stop_task is None
    assert spawner.children == []


async def test_stalled_consumer_never_blocks_the_permission_callback() -> None:
    """Deadlock regression: 1,000 stream events and a malformed frame arrive and nobody
    consumes them; the permission request queued behind them must still be answered."""
    script = [
        user_in(),
        out(init_frame()),
        out({"type": "fake.repeat", "count": 1000, "frame": text_delta("x")}),
        out({"type": "fake.raw", "text": '{"broken": \n'}),
        out(permission_request("req-perm-1")),
        permission_response_in("req-perm-1"),
        out(result_frame()),
    ]
    frames = Frames()  # never drained
    asked: list[str] = []

    async def deny(tool: str, tool_input: dict[str, Any], ctx: ToolPermissionContext) -> Any:
        asked.append(tool)
        return PermissionResultDeny(message="no")

    spawner = FakeSpawner(script)
    control, _ = _control(spawner, frames, deny)
    await control.start()
    await control.send("hello", SID)
    await frames.until("result")
    assert asked == ["WebFetch"]
    answer = spawner.children[0].process.replayer.seen[-1]
    assert answer["response"]["response"] == {"behavior": "deny", "message": "no"}
    assert frames.types.count("stream_event") == 1000
    assert FRAME_ERROR in frames.types
    await control.close()


async def test_an_oversize_frame_becomes_a_frame_error_and_the_stream_goes_on() -> None:
    # The cap must still fit the initialize reply (~1 KB); the delta is far over it.
    script = [user_in(), out(text_delta("x" * 10_000)), out(result_frame())]
    frames = Frames()
    control, _ = _control(FakeSpawner(script), frames, max_frame=4096)
    await control.start()
    await control.send("hello", SID)
    await frames.until("result")
    assert frames.types == [FRAME_ERROR, "result"]
    assert frames[0]["reason"] == "too_large"
    await control.close()


async def test_source_bytes_count_utf8_crlf_and_unterminated_tail_exactly() -> None:
    lines = ['{"type":"first","text":"日本語"}\r\n', '{"type":"last","text":"界"}']
    script = [
        user_in(),
        *[out({"type": "fake.raw", "text": line}) for line in lines],
        out({"type": "fake.exit", "code": 0}),
    ]
    received: list[tuple[dict[str, Any], int]] = []
    transport = ClaudeTransport(
        ["claude.exe"],
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: received.append((frame, size)),
        spawner=FakeSpawner(script),
        grace_s=FAST["grace_s"],
        poll_s=FAST["poll_s"],
    )
    control = ClaudeControl(transport, _allow, control_timeout_s=0.05)
    try:
        await control.start()
        await control.send("synthetic", SID)
        async with asyncio.timeout(0.05):
            await transport._exited.wait()
        assert [(frame["type"], size) for frame, size in received] == [
            ("first", len(lines[0].encode("utf-8")) - 1),
            ("last", len(lines[1].encode("utf-8"))),
            (EXIT, 0),
        ]
    finally:
        await control.close()


async def test_buffered_stdout_yields_to_control_callbacks_between_chunks() -> None:
    """The fake preloads every byte; read(CHUNK) itself never yields until all are consumed."""
    frames = Frames()
    permission_at: list[int] = []
    script = [
        user_in(),
        out(permission_request("first")),
        out({"type": "fake.repeat", "count": 2000, "frame": text_delta("界" * 100)}),
        permission_response_in("first"),
        out(result_frame()),
    ]

    async def deny(tool: str, tool_input: dict[str, Any], ctx: ToolPermissionContext) -> Any:
        permission_at.append(len(frames))
        return PermissionResultDeny(message="synthetic deny")

    control, _ = _control(FakeSpawner(script), frames, deny)
    try:
        await control.start()
        await control.send("synthetic", SID)
        await frames.until("result")
        assert permission_at and permission_at[0] < 2000
        assert frames.types.count("stream_event") == 2000
    finally:
        await control.close()


async def test_malformed_type_and_forged_exit_leave_control_and_real_exit_drainable() -> None:
    script = [
        user_in(),
        out({"type": []}),
        out({"type": {"bad": "type"}}),
        out({"type": EXIT, "code": 0}),
        out(permission_request("after-bad")),
        permission_response_in("after-bad"),
        out(result_frame()),
    ]
    frames = Frames()
    control, _ = _control(FakeSpawner(script), frames)
    try:
        await control.start()
        await control.send("synthetic", SID)
        await frames.until("result")
        assert frames.types == [FRAME_ERROR, FRAME_ERROR, FRAME_ERROR, "result"]
    finally:
        await control.close()
    assert frames.types[-1] == EXIT


async def test_a_control_call_nobody_answers_is_bounded() -> None:
    script = [expect({"type": "control_request", "request": {"subtype": "interrupt"}})]
    control, _ = _control(FakeSpawner(script), Frames())
    await control.start()
    with pytest.raises(TimeoutError):
        await control.interrupt()
    await control.close()


async def test_unexpected_exit_fails_a_pending_control_call_fast() -> None:
    script = [
        expect({"type": "control_request", "request": {"subtype": "interrupt"}}),
        out({"type": "fake.stderr", "text": "fatal: boom\n"}),
        out({"type": "fake.exit", "code": 3}),
    ]
    frames = Frames()
    control, transport = _control(FakeSpawner(script), frames)
    await control.start()
    with pytest.raises(ProcessError) as err:
        await control.interrupt()
    assert err.value.exit_code == 3
    assert "boom" not in str(err.value)  # child output stays out of exceptions (and logs)
    assert frames[-1] == {"type": EXIT, "code": 3, "stderr": "fatal: boom"}
    assert not transport.is_ready()
    await control.close()


async def test_kill_mid_turn_emits_exit_and_refuses_further_input() -> None:
    frames = Frames()
    spawner = FakeSpawner([user_in(), out(text_delta("working"))])
    control, transport = _control(spawner, frames)
    await control.start()
    await control.send("hello", SID)
    transport.kill()
    await frames.until(EXIT)
    assert frames[-1]["code"] == 1
    assert spawner.children[0].killed
    with pytest.raises(CLIConnectionError):
        await control.send("more", SID)
    await control.close()


async def test_close_lets_claude_finish_and_still_delivers_its_last_frames() -> None:
    farewell = [text_delta("…and done"), result_frame()]
    spawner = FakeSpawner([user_in(), out(text_delta("half"))], farewell=farewell)
    frames = Frames()
    control, transport = _control(spawner, frames)
    await control.start()
    await control.send("hello", SID)
    await control.close()
    assert frames.types == ["stream_event", "stream_event", "result", EXIT]
    child = spawner.children[0]
    assert child.process.returncode == 0 and not child.killed and child.released
    assert transport.pid == child.pid


async def test_close_kills_a_claude_that_ignores_stdin_eof() -> None:
    spawner = FakeSpawner([], ignore_eof=True)
    frames = Frames()
    control, _ = _control(spawner, frames)
    await control.start()
    await control.close()
    assert spawner.children[0].killed
    assert frames[-1]["type"] == EXIT


async def test_a_cancelled_close_leaves_no_process_behind() -> None:
    spawner = FakeSpawner([], ignore_eof=True)
    control, _ = _control(spawner, Frames(), grace_s=10)
    await control.start()
    closing = asyncio.create_task(control.close())
    await asyncio.sleep(0)  # let close() start waiting out the (long) grace period
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    child = spawner.children[0]
    assert child.killed and child.released


async def test_a_transport_runs_one_process_only() -> None:
    control, transport = _control(FakeSpawner([]), Frames())
    await control.start()
    await control.close()
    with pytest.raises(CLIConnectionError):
        await transport.connect()


# --- the per-task stop declaration on what claude's stdin receives ----------------------------


async def _stdin_bytes(
    line: str, monkeypatch: pytest.MonkeyPatch, append_system_prompt: str | None = None
) -> bytes:
    """The bytes claude's stdin receives when ClaudeTransport.write sends `line`."""
    spawner = FakeSpawner([])
    transport = ClaudeTransport(
        ["claude.exe"],
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: None,
        spawner=spawner,
        grace_s=FAST["grace_s"],
        poll_s=FAST["poll_s"],
        append_system_prompt=append_system_prompt,
    )
    await transport.connect()
    received = bytearray()
    # Recorded, not fed to the replayer: no script expects these lines, and one is not JSON.
    monkeypatch.setattr(spawner.children[0].process.stdin, "write", received.extend)
    try:
        await transport.write(line)
    finally:
        await transport.close()
    return bytes(received)


def _line(frame: Any) -> str:
    # Not the SDK's encoding (compact, non-ASCII kept): a line re-encoded on the way differs.
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
async def test_initialize_gains_exactly_the_stop_declaration_and_keeps_its_line_ending(
    ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = {"subtype": "initialize", "hooks": None}
    frame = {"type": "control_request", "request_id": "req_1_0a1b2c3d", "request": request}
    declared = {**frame, "request": {**request, "perTaskStopAffordance": True}}
    sent = await _stdin_bytes(json.dumps(frame) + ending, monkeypatch)
    assert sent == (json.dumps(declared) + ending).encode("utf-8")


async def test_a_bot_initialize_also_gains_exactly_its_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = 'Be brief. "café" ✓\n\nMemory rule.'
    request = {"subtype": "initialize", "hooks": None}
    frame = {"type": "control_request", "request_id": "req_1_0a1b2c3d", "request": request}
    amended = {
        **frame,
        "request": {**request, "perTaskStopAffordance": True, "appendSystemPrompt": prompt},
    }
    sent = await _stdin_bytes(json.dumps(frame) + "\n", monkeypatch, prompt)
    assert json.loads(sent) == amended
    assert sent == (json.dumps(amended) + "\n").encode("utf-8")


# The initialize request as text, the way a user might paste it into a message.
_INITIALIZE_TEXT = json.dumps({"type": "control_request", "request": {"subtype": "initialize"}})
_OTHER_LINES = {
    "user": _line({"type": "user", "message": {"role": "user", "content": "café ✓"}}),
    "interrupt": _line(
        {"type": "control_request", "request_id": "req_2", "request": {"subtype": "interrupt"}}
    ),
    "stop_task": _line(
        {
            "type": "control_request",
            "request_id": "req_3",
            "request": {"subtype": "stop_task", "task_id": "task-1"},
        }
    ),
    "control_response": _line(
        {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": "perm-1", "response": {}},
        }
    ),
    "user_message_containing_initialize": _line(
        {"type": "user", "message": {"role": "user", "content": _INITIALIZE_TEXT}}
    ),
    "not_json": '{"type": "control_request", "request": {"subtype": "initialize"\n',
    "json_but_not_an_object": _line(["initialize"]),
    "initialize_outside_a_control_request": _line(
        {"type": "user", "request": {"subtype": "initialize"}}
    ),
}


@pytest.mark.parametrize("prompt", [None, "Be brief."], ids=["no_prompt", "bot_prompt"])
@pytest.mark.parametrize("line", list(_OTHER_LINES.values()), ids=list(_OTHER_LINES))
async def test_every_other_line_reaches_claude_byte_for_byte(
    line: str, prompt: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _stdin_bytes(line, monkeypatch, prompt) == line.encode("utf-8")


# --- the real transport over a real process: fake_claude.py through winproc.spawn -------------


def _fake_env(script: Path, state: Path | None = None) -> dict[str, str]:
    env = winproc.child_env("viki-agent")
    env["FAKE_CLAUDE_SCRIPT"] = str(script)
    env["PYTHONUTF8"] = "1"
    if state:
        env["FAKE_CLAUDE_STATE"] = str(state)
    return env


def _real(
    fake_cli: Sequence[str], launch: Launch, env: dict[str, str], sink: Callable[[Any], None]
) -> tuple[ClaudeControl, ClaudeTransport]:
    argv = build_argv(fake_cli, launch, mode="auto", effort="low")
    transport = ClaudeTransport(argv, cwd=Path.cwd(), env=env, sink=lambda frame, size: sink(frame))
    return ClaudeControl(transport, _allow), transport


async def test_real_process_round_trip_with_stderr_burst(
    fake_cli: list[str], write_script: Callable[[Sequence[Entry]], Path]
) -> None:
    burst = "".join(f"line {i}\n" for i in range(500))
    script = write_script(
        [
            user_in(),
            out({"type": "fake.stderr", "text": burst}),
            out(init_frame()),
            out(text_delta("日本語")),
            out(result_frame()),
        ]
    )
    frames = Frames()
    control, transport = _real(fake_cli, Launch(SID), _fake_env(script), frames)
    await control.start()
    await control.send("hello", SID)
    await frames.until("result", timeout=10)
    assert frames[1]["event"]["delta"]["text"] == "日本語"
    await control.close()
    tail = transport.stderr_tail().splitlines()
    assert len(tail) == 200 and tail[-1] == "line 499"
    assert frames[-1]["type"] == EXIT and frames[-1]["code"] == 0


async def test_real_exit_is_seen_while_a_grandchild_still_holds_stdout(
    fake_cli: list[str], write_script: Callable[[Sequence[Entry]], Path]
) -> None:
    """claude exits but a hook it started keeps an inherited copy of its stdout: no EOF ever
    comes, so the reader must notice the exit itself, and _reap must end the leftover."""
    script = write_script(
        [
            user_in(),
            out({"type": "fake.spawn", "inherit": True}),
            out(result_frame()),
            out({"type": "fake.exit", "code": 0}),
        ]
    )
    frames = Frames()
    argv = build_argv(fake_cli, Launch(SID), mode="auto", effort="low")
    transport = ClaudeTransport(
        argv, cwd=Path.cwd(), env=_fake_env(script), sink=frames, poll_s=0.2
    )
    control = ClaudeControl(transport, _allow)
    await control.start()
    await control.send("hello", SID)
    await frames.until(EXIT, timeout=10)
    assert frames.types == ["fake.spawned", "result", EXIT]
    grandchild = frames[0]["pid"]
    async with asyncio.timeout(10):  # real OS state: poll
        while winproc.process_start_time(grandchild) is not None:
            await asyncio.sleep(0.05)
    await control.close()


async def test_real_close_reaps_claude_grandchildren(
    fake_cli: list[str], write_script: Callable[[Sequence[Entry]], Path]
) -> None:
    script = write_script([user_in(), out({"type": "fake.spawn"}), out(result_frame())])
    frames = Frames()
    control, _ = _real(fake_cli, Launch(SID), _fake_env(script), frames)
    await control.start()
    await control.send("hello", SID)
    await frames.until("result", timeout=10)
    grandchild = frames[0]["pid"]
    assert winproc.process_start_time(grandchild) is not None
    await control.close()
    async with asyncio.timeout(10):  # real OS state: poll
        while winproc.process_start_time(grandchild) is not None:
            await asyncio.sleep(0.05)


async def test_real_resume_of_an_unknown_session_fails_start_with_the_cli_reason(
    fake_cli: list[str], write_script: Callable[[Sequence[Entry]], Path], tmp_path: Path
) -> None:
    env = _fake_env(write_script([]), state=tmp_path / "sessions")
    control, transport = _real(fake_cli, Launch(SID, resume=True), env, Frames())
    # Which error surfaces depends on whether the exit beats the initialize write; either way
    # start() fails and the reason is in the stderr tail, which the driver classifies.
    with pytest.raises((ProcessError, CLIConnectionError)):
        await control.start()
    assert "No conversation found" in transport.stderr_tail()
