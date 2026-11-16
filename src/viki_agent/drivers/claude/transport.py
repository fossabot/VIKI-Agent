"""ClaudeTransport: owns the claude.exe process and splits its stdout in two.

Control frames (`control_request`, `control_response`, `control_cancel_request`) are yielded to
the SDK's Query, which answers initialize and can_use_tool and resolves our control calls.
Every other frame goes straight to `sink`, with its source-byte count. The driver's bounded
queue never waits for capacity: overflow ends the attempt while this reader keeps draining.
Query's own message buffer (100 frames, drained only by `receive_messages()`, which we never
call) therefore stays empty, so a busy conversation can never stall the control channel.

Outbound, `write()` passes every line through `amend_initialize()`: the SDK's initialize
request gains `"perTaskStopAffordance": true`, which tells claude that an interrupt ends only the
turn and spares background agents, and, for a Bot session, `"appendSystemPrompt"` with that Bot's
instructions, memory rule and Bot rules (ADR-084); every other line goes out byte for byte.

Ending a process: `shutdown()` closes stdin and lets claude flush its transcript while the
reader keeps draining stdout (so the last frames and the EXIT frame still reach the sink);
`close()` (called by the SDK) reaps and releases; `abort()` is the idempotent last resort.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import CLIConnectionError, ProcessError, Transport

from viki_agent import winproc

CHUNK = 64 * 1024
MAX_FRAME = 32 * 1024 * 1024  # the largest frame seen in real transcripts is 1.36 MB
STDERR_LINES = 200
GRACE_S = 5.0
POLL_S = 1.0  # a quiet reader checks this often whether claude has exited
CONTROL_TYPES = frozenset({"control_request", "control_response", "control_cancel_request"})

# Synthetic frames the transport adds to the sink stream. Real claude frames never use "viki.".
EXIT = "viki.exit"  # {"type", "code", "stderr"}: the reader saw claude exit
FRAME_ERROR = "viki.frame_error"  # {"type", "reason": "too_large" | "bad_json"}

Frame = dict[str, Any]
Sink = Callable[[Frame, int], None]


class _Reader(Protocol):
    async def read(self, n: int = -1, /) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes, /) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    def is_closing(self) -> bool: ...


class _Process(Protocol):
    @property
    def stdin(self) -> _Writer | None: ...
    @property
    def stdout(self) -> _Reader | None: ...
    @property
    def stderr(self) -> _Reader | None: ...
    @property
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...


class ChildProcess(Protocol):
    """What the transport needs from a spawned child; `winproc.Child` and test fakes fit."""

    @property
    def process(self) -> _Process: ...
    @property
    def pid(self) -> int: ...
    def kill_tree(self) -> None: ...
    def release(self) -> None: ...


Spawner = Callable[[Sequence[str], Path, Mapping[str, str]], Awaitable[ChildProcess]]


async def _spawn(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> ChildProcess:
    return await winproc.spawn(argv, cwd=cwd, env=env)


class LineFramer:
    """Splits a byte stream into lines. Decoding happens per complete line, so a multi-byte
    UTF-8 character split across reads is never seen half-way. A line longer than `max_line`
    is dropped and reported once as None."""

    def __init__(self, max_line: int) -> None:
        self._buf = bytearray()
        self._max = max_line
        self._discarding = False

    def push(self, chunk: bytes) -> Iterator[bytes | None]:
        start = 0
        while (nl := chunk.find(b"\n", start)) >= 0:
            if self._discarding:
                self._discarding = False  # the oversize line ends here; None was already sent
            else:
                self._buf += chunk[start:nl]
                yield None if len(self._buf) > self._max else bytes(self._buf)
            self._buf.clear()
            start = nl + 1
        if not self._discarding:
            self._buf += chunk[start:]
            if len(self._buf) > self._max:
                self._buf.clear()
                self._discarding = True
                yield None

    def flush(self) -> bytes:
        """Take the unterminated tail (a last line written without a newline)."""
        tail = b"" if self._discarding else bytes(self._buf)
        self._buf.clear()
        self._discarding = False
        return tail


def parse_line(line: bytes) -> Frame | None:
    """One NDJSON line to a frame. Blank and non-JSON lines are noise (None); a line that
    starts like JSON but does not parse becomes a FRAME_ERROR frame."""
    text = line.decode("utf-8", errors="replace").strip()  # strip() also drops a CRLF's \r
    if not text.startswith("{"):
        return None
    try:
        frame = json.loads(text)
    except ValueError, RecursionError:
        return {"type": FRAME_ERROR, "reason": "bad_json"}
    if isinstance(frame, dict):
        kind = frame.get("type")
        if kind is not None and (not isinstance(kind, str) or kind.startswith("viki.")):
            # Only the transport may create lifecycle markers; provider text cannot forge EXIT.
            return {"type": FRAME_ERROR, "reason": "bad_json"}
    return frame if isinstance(frame, dict) else None


def amend_initialize(data: str, append_system_prompt: str | None = None) -> str:
    """Amend one outbound line. A JSON object whose `type` is "control_request" and whose
    `request.subtype` is "initialize" gets `request["perTaskStopAffordance"] = True` and, when
    `append_system_prompt` is given, `request["appendSystemPrompt"]` set to it; it is re-encoded
    with json.dumps (the SDK's own encoding), keeping its line ending. Any other line, or one
    that does not parse, is returned as it came."""
    try:
        frame = json.loads(data)  # a trailing \n or \r\n is JSON whitespace
    except ValueError, RecursionError:
        return data
    request = frame.get("request") if isinstance(frame, dict) else None
    if (
        not isinstance(request, dict)
        or frame.get("type") != "control_request"
        or request.get("subtype") != "initialize"
    ):
        return data
    request["perTaskStopAffordance"] = True
    if append_system_prompt is not None:
        request["appendSystemPrompt"] = append_system_prompt
    return json.dumps(frame) + data[len(data.rstrip("\r\n")) :]


class ClaudeTransport(Transport):
    """One claude.exe process. The SDK's Query reads control frames through `read_messages()`;
    everything else reaches `sink` in stdout order. Single use: one transport, one process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        sink: Sink,
        spawner: Spawner | None = None,  # tests pass a fake; default: winproc.spawn
        grace_s: float = GRACE_S,
        poll_s: float = POLL_S,
        max_frame: int = MAX_FRAME,
        append_system_prompt: str | None = None,
    ) -> None:
        self._argv = list(argv)
        self._append_system_prompt = append_system_prompt
        self._cwd = cwd
        self._env = dict(env)
        self._sink = sink
        self._spawner = spawner or _spawn
        self._grace_s = grace_s
        self._poll_s = poll_s
        self._max_frame = max_frame
        self._child: ChildProcess | None = None
        self._stdin_lock = asyncio.Lock()
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        self._exited = asyncio.Event()  # set once the reader has emitted EXIT
        self._ready = False
        self._closing = False  # we asked claude to exit; its exit is not an error

    @property
    def pid(self) -> int | None:
        return self._child.pid if self._child else None

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def connect(self) -> None:
        if self._child is not None:
            raise CLIConnectionError("a ClaudeTransport runs one process; make a new transport")
        self._child = await self._spawner(self._argv, self._cwd, self._env)
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._child))
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def write(self, data: str) -> None:
        async with self._stdin_lock:
            stdin = self._child.process.stdin if self._child else None
            if not self._ready or stdin is None or stdin.is_closing():
                raise CLIConnectionError("claude is not accepting input")
            try:
                stdin.write(amend_initialize(data, self._append_system_prompt).encode("utf-8"))
                await stdin.drain()
            except (OSError, RuntimeError) as exc:
                self._ready = False
                raise CLIConnectionError(f"writing to claude failed: {exc}") from exc

    def read_messages(self) -> AsyncIterator[Frame]:
        return self._read()

    async def _read(self) -> AsyncIterator[Frame]:
        child = self._require_child()
        try:
            framer = LineFramer(self._max_frame)
            eof = False
            while not eof:
                chunk = await self._next_chunk(child)
                eof = not chunk
                for line in [framer.flush()] if eof else framer.push(chunk):
                    frame = (
                        {"type": FRAME_ERROR, "reason": "too_large"}
                        if line is None
                        else parse_line(line)
                    )
                    if frame is None:
                        continue
                    if frame.get("type") in CONTROL_TYPES:
                        yield frame
                    else:
                        self._sink(frame, len(line) if line is not None else 0)
                if not eof:
                    # A pipe with buffered output need not suspend on read(). Let the pump,
                    # Stop and SDK permission callbacks run at least once per 64 KiB chunk.
                    await asyncio.sleep(0)
            self._ready = False
            code = await self._reap(child)
            self._sink({"type": EXIT, "code": code, "stderr": self.stderr_tail()}, 0)
        finally:
            self._exited.set()  # also when the SDK cancels the reader: nobody may wait on it
        if not self._closing:
            # Raising makes Query fail every pending control call at once instead of timing out.
            # No stderr in the exception: the SDK logs it, and hard rule 8 keeps child output out
            # of logs. The session gets the tail from the EXIT frame instead.
            raise ProcessError("claude exited", exit_code=code)

    async def _next_chunk(self, child: ChildProcess) -> bytes:
        """The next stdout chunk, or b"" once claude is gone. Its exit counts as EOF even when a
        grandchild still holds an inherited copy of the pipe, but only after a whole quiet poll
        period that began with claude already gone, so buffered last frames are never lost."""
        stdout = child.process.stdout
        assert stdout is not None
        while True:
            gone = child.process.returncode is not None
            try:
                async with asyncio.timeout(self._poll_s):
                    return await stdout.read(CHUNK)
            except TimeoutError:
                if gone:
                    return b""

    async def end_input(self) -> None:
        async with self._stdin_lock:
            stdin = self._child.process.stdin if self._child else None
            if stdin is not None and not stdin.is_closing():
                stdin.close()

    async def shutdown(self) -> None:
        """Ask claude to finish: close stdin, wait up to grace_s for it to exit while the reader
        keeps draining, then kill the tree. Returns once the EXIT frame is out (or never came)."""
        if self._child is None:
            return
        self._closing = True
        with suppress(TimeoutError):
            async with asyncio.timeout(self._grace_s):
                await self.end_input()
        try:
            async with asyncio.timeout(self._grace_s):
                await self._exited.wait()
        except TimeoutError:
            self.kill()
            with suppress(TimeoutError):
                async with asyncio.timeout(self._grace_s):
                    await self._exited.wait()

    def kill(self) -> None:
        """Kill the whole process tree now (the last step of a layered stop)."""
        if self._child:
            self._child.kill_tree()

    async def close(self) -> None:
        """Close stdin, reap and release. Called by the SDK during disconnect (after shutdown()
        in a normal close, or on its own when a start fails); every await is bounded."""
        child = self._child
        if child is None:
            return
        self._closing = True
        self._ready = False
        try:
            with suppress(TimeoutError):
                async with asyncio.timeout(self._grace_s):
                    await self.end_input()  # a no-op after shutdown(); lets claude exit by itself
            await self._reap(child)
        finally:
            self.abort()

    def abort(self) -> None:
        """Kill the tree and release the Job right now. Idempotent, so it is safe as the
        `finally` of a close that may itself have been cancelled."""
        self._ready = False
        if self._child:
            self._child.kill_tree()
            self._child.release()
        if self._stderr_task:
            self._stderr_task.cancel()

    async def _reap(self, child: ChildProcess) -> int | None:
        """Wait (bounded) for claude to exit, then end whatever is left in its Job so every pipe
        closes, and let stderr settle. Returns the exit code."""
        if child.process.returncode is None:
            with suppress(TimeoutError):
                async with asyncio.timeout(self._grace_s):
                    await child.process.wait()
        # claude is gone or overdue: on Windows the Job ends everything it started; on POSIX,
        # only what stayed in its group (winproc's module docstring).
        child.kill_tree()
        with suppress(TimeoutError):
            async with asyncio.timeout(self._grace_s):
                await child.process.wait()
        if self._stderr_task:
            await asyncio.wait({self._stderr_task}, timeout=1.0)  # never raises the task's error
        return child.process.returncode

    async def _drain_stderr(self, child: ChildProcess) -> None:
        stderr = child.process.stderr
        if stderr is None:
            return
        framer = LineFramer(CHUNK)
        try:
            while chunk := await stderr.read(CHUNK):
                for line in framer.push(chunk):
                    self._keep_stderr(line)
        except OSError:
            pass
        finally:
            self._keep_stderr(framer.flush())  # a last message without a newline matters most

    def _keep_stderr(self, line: bytes | None) -> None:
        if line and (text := line.decode("utf-8", errors="replace").rstrip()):
            self._stderr.append(text)

    def _require_child(self) -> ChildProcess:
        if self._child is None:
            raise CLIConnectionError("not connected")
        return self._child
