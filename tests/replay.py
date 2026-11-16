"""The single replay engine behind every claude test double.

A script is a list of entries `{"dir": "in" | "out", "frame": {...}}`; fixture lines may also
carry `scenario` and `t_ms`, which replay ignores. "in" frames are what the client is expected
to write to claude's stdin, "out" frames are what claude wrote to stdout. After each client
frame, every "out" entry up to the next "in" entry is emitted.

Beyond strict playback the engine:
  * answers `initialize` (and any other control request the script does not expect) itself,
    rewriting request ids so recorded responses match the live requests;
  * rewrites the recorded session id to the live one taken from argv;
  * rejects `--resume=<id>` for sessions it never completed a turn in (like the real CLI),
    when given a state directory.

Test-only directives, usable as "out" frames:
  {"type": "fake.raw", "text": "..."}                  write text verbatim (noise, bad JSON)
  {"type": "fake.stderr", "text": "..."}               write to stderr
  {"type": "fake.exit", "code": N}                     exit immediately
  {"type": "fake.repeat", "count": N, "frame": {...}}  emit one frame N times
  {"type": "fake.spawn", "inherit": false}             start a long-sleeping grandchild and
                                                       emit {"type": "fake.spawned", "pid": N};
                                                       with inherit it holds our stdout too
                                                       (subprocess fake only)
  {"type": "fake.sleep", "s": 0.05}                    pause before the next frame (in-process:
                                                       the client can write meanwhile)
  {"type": "fake.touch", "name": "a.txt", "text": "x"}  write text as UTF-8 to <cwd>/<name> and
                                                       emit nothing; RuntimeError without a
                                                       cwd, or for a name holding / \\ : or
                                                       equal to ".."

Options: `farewell` frames are written when stdin closes (a turn finishing during shutdown);
`ignore_eof` makes a claude that does not exit when stdin closes.

`FakeChild` runs the engine in-process behind the real ClaudeTransport (its `spawner`);
`fake_claude.py` runs it as a real subprocess over stdio.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Frame = dict[str, Any]
Entry = dict[str, Any]

DEFAULT_INIT: Frame = {
    "commands": [
        {"name": "capture", "description": "Capture a note into the inbox", "argumentHint": ""},
        {"name": "compact", "description": "Compact the conversation", "argumentHint": ""},
    ],
    "agents": [{"name": "Explore", "description": "Read-only search agent"}],
    "output_style": "default",
    "available_output_styles": ["default"],
    "models": [
        {"value": "default", "displayName": "Default", "description": "Recommended"},
        {"value": "sonnet", "displayName": "Sonnet", "description": "Everyday tasks"},
    ],
    # The shape of a recorded real reply (it also has email and organization; no tokenSource).
    "account": {"apiProvider": "firstParty", "subscriptionType": "Claude Max"},
    "pid": 4242,
}

_CONTROL_DEFAULTS: dict[str, Frame] = {
    # Three of the 16 keys a recorded real session returned, in its shape: `percentage` is an
    # integer and sonnet 5's window is 1,000,000. A fractional percentage collapsed to 0 through
    # driver._context_percent's int(), so every fake-driven turn — and every screenshot taken
    # against this double — reported "context 0%", a value the real CLI cannot send.
    "get_context_usage": {"totalTokens": 370_000, "maxTokens": 1_000_000, "percentage": 37},
    "mcp_status": {"mcpServers": []},
}


def load_script(path: Path) -> list[Entry]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@dataclass
class FakeArgs:
    """The argv facts a fake needs: the session it writes, and the one it resumes (or forks)."""

    session_id: str | None = None
    resume: str | None = None

    @classmethod
    def parse(cls, argv: Sequence[str]) -> FakeArgs:
        args = cls()
        for token in argv:
            key, _, value = token.partition("=")
            if key == "--session-id":
                args.session_id = value
            elif key == "--resume":
                args.resume = value
        return args

    @property
    def live_session_id(self) -> str | None:
        return self.session_id or self.resume


class Replayer:
    """Plays a script against whatever the client writes. Output goes through callbacks, so
    the same engine drives an in-memory pipe or real stdio."""

    def __init__(
        self,
        script: Sequence[Entry],
        *,
        out: Callable[[bytes], None],
        err: Callable[[bytes], None],
        exit: Callable[[int], None],
        argv: Sequence[str] = (),
        state_dir: Path | None = None,
        spawn: Callable[[bool], int] | None = None,
        farewell: Sequence[Frame] = (),
        ignore_eof: bool = False,
        schedule: Callable[[float, Callable[[], None]], object] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._entries = list(script)
        self._pos = 0
        self._out = out
        self._err = err
        self._exit = exit
        self._spawn = spawn
        self._schedule = schedule  # in-process: loop.call_later; None: fake.sleep blocks
        self._farewell = list(farewell)  # written after stdin closes, before exiting
        self._ignore_eof = ignore_eof  # a claude that never exits on its own
        self._args = FakeArgs.parse(argv)
        self._state_dir = state_dir
        self._cwd = cwd  # where fake.touch writes: the folder the client spawned the fake in
        self._request_ids: dict[str, str] = {}  # recorded control request id -> live id
        self._recorded_sid = next(
            (e["frame"]["session_id"] for e in self._entries if "session_id" in e.get("frame", {})),
            None,
        )
        self.seen: list[Frame] = []  # every frame the client wrote, for assertions
        self.exited = False

    def boot(self) -> None:
        """Start as claude would: refuse an unknown --resume, then emit any leading frames."""
        resume = self._args.resume
        if resume and self._state_dir and not (self._state_dir / f"{resume}.session").exists():
            self._err(f"No conversation found with session ID: {resume}\n".encode())
            self._do_exit(1)
            return
        self._emit_until_next_in()

    def feed(self, frame: Frame) -> None:
        if self.exited:
            return
        self.seen.append(frame)
        kind = frame.get("type")
        expected = self._peek_in()
        if kind == "control_request":
            subtype = frame["request"].get("subtype")
            if expected and _is_request(expected, subtype):
                if recorded := expected.get("request_id"):
                    self._request_ids[recorded] = frame["request_id"]
                self._pos += 1
                self._emit_until_next_in()
            else:
                self._answer_control(frame["request_id"], subtype)
            return
        # A recorded control call this client does not make (e.g. set_permission_mode during a
        # capture) is skipped together with claude's reaction to it.
        while expected is not None and expected.get("type") == "control_request":
            self._pos += 1
            while self._pos < len(self._entries) and self._entries[self._pos]["dir"] == "out":
                self._pos += 1
            expected = self._peek_in()
        if (
            expected is None
            or expected.get("type") != kind
            or (
                kind == "control_response"
                and expected["response"].get("request_id") != frame["response"].get("request_id")
            )
        ):
            self._mismatch(expected, frame)
            return
        self._pos += 1
        self._emit_until_next_in()

    def stdin_closed(self) -> None:
        for frame in self._farewell:
            self._emit(frame)
        if not self._ignore_eof:
            self._do_exit(0)

    # --- internals ----------------------------------------------------------------------------

    def _peek_in(self) -> Frame | None:
        entry = self._entries[self._pos] if self._pos < len(self._entries) else None
        return entry["frame"] if entry and entry["dir"] == "in" else None

    def _emit_until_next_in(self) -> None:
        # A fake.sleep re-enters here through the scheduler, and the fake may have exited in the
        # meantime (stdin closed, kill_tree, fake.exit, a mismatch). Emitting then means feed_data
        # after feed_eof, which asyncio asserts — inside a timer callback, where the assertion goes
        # to the loop's exception handler, gets logged, and fails nothing.
        if self.exited:
            return
        while self._pos < len(self._entries) and self._entries[self._pos]["dir"] == "out":
            frame = self._entries[self._pos]["frame"]
            self._pos += 1
            if frame.get("type") == "fake.sleep":
                if self._schedule is not None:
                    self._schedule(float(frame["s"]), self._emit_until_next_in)
                    return  # the rest follows later; meanwhile the client may write
                time.sleep(float(frame["s"]))
                continue
            self._emit(frame)
            if self.exited:
                return

    def _emit(self, frame: Frame) -> None:
        kind = frame.get("type")
        if kind == "fake.raw":
            self._out(frame["text"].encode())
        elif kind == "fake.stderr":
            self._err(frame["text"].encode())
        elif kind == "fake.exit":
            self._do_exit(int(frame.get("code", 0)))
        elif kind == "fake.repeat":
            line = self._encode(frame["frame"])
            for _ in range(int(frame["count"])):
                self._out(line)
        elif kind == "fake.spawn":
            if self._spawn is None:
                raise RuntimeError("fake.spawn needs the subprocess fake")
            pid = self._spawn(bool(frame.get("inherit", False)))
            self._out(self._encode({"type": "fake.spawned", "pid": pid}))
        elif kind == "fake.touch":
            self._touch(frame["name"], frame["text"])
        else:
            if kind == "control_response":
                frame = json.loads(json.dumps(frame))
                recorded = frame["response"].get("request_id")
                frame["response"]["request_id"] = self._request_ids.get(recorded, recorded)
            self._out(self._encode(frame))
            if kind == "result":
                self._mark_session()

    def _touch(self, name: str, text: str) -> None:
        if self._cwd is None or name == ".." or any(ch in name for ch in "/\\:"):
            raise RuntimeError(f"fake.touch refused {name!r}")
        (self._cwd / name).write_bytes(text.encode("utf-8"))  # bytes: no CRLF translation

    def _encode(self, frame: Frame) -> bytes:
        text = json.dumps(frame, ensure_ascii=False)
        live = self._args.live_session_id
        if live and self._recorded_sid and live != self._recorded_sid:
            text = text.replace(f'"{self._recorded_sid}"', f'"{live}"')
        return (text + "\n").encode("utf-8")

    def _answer_control(self, request_id: str, subtype: str | None) -> None:
        payload = (
            DEFAULT_INIT if subtype == "initialize" else _CONTROL_DEFAULTS.get(subtype or "", {})
        )
        response = {"subtype": "success", "request_id": request_id, "response": payload}
        self._out(self._encode({"type": "control_response", "response": response}))

    def _mark_session(self) -> None:
        live = self._args.live_session_id
        if self._state_dir and live:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            (self._state_dir / f"{live}.session").touch()

    def _mismatch(self, expected: Frame | None, got: Frame) -> None:
        want = "end of script" if expected is None else _describe(expected)
        self._err(f"replay mismatch: expected {want}, got {_describe(got)}\n".encode())
        self._do_exit(98)

    def _do_exit(self, code: int) -> None:
        if not self.exited:
            self.exited = True
            self._exit(code)


def _is_request(expected: Frame, subtype: str | None) -> bool:
    return (
        expected.get("type") == "control_request" and expected["request"].get("subtype") == subtype
    )


def _describe(frame: Frame) -> str:
    kind = frame.get("type", "?")
    if kind == "control_request":
        return f"control_request/{frame['request'].get('subtype')}"
    return str(kind)


# --- in-process double: the real ClaudeTransport over a fake child ---------------------------

_pids = itertools.count(50_000)


class _FakeStdin:
    def __init__(self, proc: FakeProcess) -> None:
        self._proc = proc
        self._buf = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        if self._closing or self._proc.returncode is not None:
            raise ConnectionResetError("stdin closed")
        self._buf += data
        while (nl := self._buf.find(b"\n")) >= 0:
            line, self._buf = bytes(self._buf[:nl]), self._buf[nl + 1 :]
            if line.strip():
                self._proc.replayer.feed(json.loads(line))

    async def drain(self) -> None:
        return None  # like a pipe: a write accepted before the exit drains fine

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._proc.replayer.stdin_closed()

    def is_closing(self) -> bool:
        return self._closing


class FakeProcess:
    def __init__(self, script: Sequence[Entry], argv: Sequence[str], **options: Any) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.replayer = Replayer(
            script,
            out=self.stdout.feed_data,
            err=self.stderr.feed_data,
            exit=self.exit,
            argv=argv,
            schedule=asyncio.get_running_loop().call_later,
            **options,
        )
        self.stdin = _FakeStdin(self)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()


class FakeChild:
    """Stands in for `winproc.Child`: same surface, no OS process. `killed` records whether a
    still-running child had to be killed (ending an exited one is a no-op, as with a Job)."""

    def __init__(self, script: Sequence[Entry], argv: Sequence[str], **options: Any) -> None:
        self.process = FakeProcess(script, argv, **options)
        self.pid = next(_pids)
        self.argv = list(argv)  # what the driver launched: tests check resume/fork/effort flags
        self.killed = False
        self.released = False

    def kill_tree(self) -> None:
        if self.process.returncode is None:
            self.killed = True
            self.process.exit(1)

    def release(self) -> None:
        self.released = True
        self.kill_tree()


class FakeSpawner:
    """A `spawner` for ClaudeTransport that plays `script`; `options` go to the Replayer
    (state_dir, farewell, ignore_eof). Keeps every child it made."""

    def __init__(self, script: Sequence[Entry], **options: Any) -> None:
        self.script = list(script)
        self.options = options
        self.children: list[FakeChild] = []

    async def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> FakeChild:
        child = FakeChild(self.script, argv, cwd=cwd, **self.options)
        self.children.append(child)
        child.process.replayer.boot()
        return child


# --- script builders for synthetic conversations ----------------------------------------------

SID = "11111111-1111-4111-8111-111111111111"


def out(frame: Frame) -> Entry:
    return {"dir": "out", "frame": frame}


def expect(frame: Frame) -> Entry:
    return {"dir": "in", "frame": frame}


def user_in(text: str = "hello") -> Entry:
    message = {"role": "user", "content": text}
    return expect({"type": "user", "message": message, "parent_tool_use_id": None})


def init_frame(session_id: str = SID) -> Frame:
    return {"type": "system", "subtype": "init", "session_id": session_id, "model": "sonnet"}


def text_delta(text: str, session_id: str = SID) -> Frame:
    event = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }
    return {"type": "stream_event", "event": event, "session_id": session_id}


def result_frame(session_id: str = SID, subtype: str = "success") -> Frame:
    return {"type": "result", "subtype": subtype, "is_error": False, "session_id": session_id}


def permission_request(
    request_id: str, tool: str = "WebFetch", tool_use_id: str = "toolu_1"
) -> Frame:
    request = {
        "subtype": "can_use_tool",
        "tool_name": tool,
        "input": {"url": "https://example.com"},
        "tool_use_id": tool_use_id,
    }
    return {"type": "control_request", "request_id": request_id, "request": request}


def permission_response_in(request_id: str) -> Entry:
    return expect({"type": "control_response", "response": {"request_id": request_id}})
