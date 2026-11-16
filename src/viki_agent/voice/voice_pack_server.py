"""The voice pack's Voicebox server, run on demand: started by `address()`, stopped when idle.

`PackServer` spawns Voicebox 0.5.0's CUDA server through `winproc.spawn` on a free loopback port,
its output on the null device, and counts it ready once `/health` answers healthy and every
listener on the port is inside the server's Job. Concurrent callers
share one start. A missing exe is refused before any spawn (`voicebox_pack_missing`) and is no
crash. A failed spawn, an exit VIKI did not cause, a ready timeout or a foreign listener is a
crash: the first leaves the server stopped, so the next `address()` starts it again; the second
leaves it failed until `reset()`. The server stops `idle_stop_s` after the last `address()`.
`profile()` finds or creates VIKI's preset profile for a language and voice once per server run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

from viki_agent import winproc
from viki_agent.voice import pack_voices, voice_pack, voicebox, voicebox_http
from viki_agent.voice.voicebox_http import MAX_JSON_BYTES, STATUS_TIMEOUT_S, VoiceboxError

logger = logging.getLogger(__name__)

READY_TIMEOUT_S = 180.0
IDLE_STOP_S = 300.0
POLL_S = 0.5

_HOST = "127.0.0.1"
_ENV_NAMES = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE")
_PRESET = {"voice_type": "preset"}

Spawn = Callable[..., Awaitable[winproc.Child]]
Request = Callable[[str, int, str, str, bytes | None, float, int], tuple[int, bytes]]


def free_port() -> int:
    """A loopback port nothing listened on just now: bound to port 0, read, and closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HOST, 0))
        return sock.getsockname()[1]


def owns_port(child: winproc.Child, port: int) -> bool:
    """Whether something listens on `port` and every listener is inside the child's Job."""
    pids = winproc.tcp_listeners(port)
    return bool(pids) and all(child.job.holds(pid) for pid in pids)


def server_env(data_dir: Path, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The server's whole environment: a few OS variables and the pack's offline model cache."""
    env = {name: environ[name] for name in _ENV_NAMES if name in environ}
    env["VOICEBOX_MODELS_DIR"] = str(voice_pack.models_dir(data_dir))
    env["HF_HOME"] = str(voice_pack.hf_home(data_dir))
    env["HF_HUB_OFFLINE"] = "1"
    return env


def command(data_dir: Path, port: int, parent_pid: int) -> list[str]:
    """The server's argv; FileNotFoundError when the pack's exe is missing."""
    exe = winproc.resolve_exe(voice_pack.server_dir(data_dir) / voice_pack.SERVER_EXE)
    return [
        str(exe),
        "--host",
        _HOST,
        "--port",
        str(port),
        "--data-dir",
        str(voice_pack.state_dir(data_dir)),
        "--parent-pid",
        str(parent_pid),
    ]


async def _reap(child: winproc.Child) -> None:
    """Kill one server's tree, wait until all of it has ended, then release its Job."""
    try:
        child.kill_tree()
        await child.wait_tree(poll_s=0.05)
    except Exception as exc:
        logger.warning("voice pack server reap failed: %s", type(exc).__name__)
    finally:
        child.release()


class PackServer:
    def __init__(
        self,
        data_dir: Path,
        *,
        command: Callable[[Path, int, int], list[str]] = command,
        env: Mapping[str, str] | None = None,
        spawn: Spawn = winproc.spawn,
        request: Request = voicebox_http.request,
        owns_port: Callable[[winproc.Child, int], bool] = owns_port,
        pick_port: Callable[[], int] = free_port,
        ready_timeout_s: float = READY_TIMEOUT_S,
        idle_stop_s: float = IDLE_STOP_S,
        poll_s: float = POLL_S,
        parent_pid: int = os.getpid(),
    ) -> None:
        self._data_dir = data_dir
        self._command = command
        self._env = env
        self._spawn = spawn
        self._request = request
        self._owns_port = owns_port
        self._pick_port = pick_port
        self._ready_timeout_s = ready_timeout_s
        self._idle_stop_s = idle_stop_s
        self._poll_s = poll_s
        self._parent_pid = parent_pid
        self._child: winproc.Child | None = None
        self._port: int | None = None  # set while the server is ready
        self._error: str | None = None
        self._crashes = 0
        self._run_count = 0  # successful starts; note_speed keys each model's first sample to it
        self._closed = False
        self._starting: asyncio.Task[str | None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._reaping: asyncio.Task[None] | None = None
        self._profiles: dict[tuple[str, str], str] = {}  # (language, voice) -> id, this run
        self._profile_lock = asyncio.Lock()
        self._idle: asyncio.TimerHandle | None = None
        self._generation = 0  # an idle stop armed before the latest address() is stale
        self._tasks: set[asyncio.Task[None]] = set()

    def status(self) -> dict[str, str | None]:
        if self._crashes >= 2:
            state = "failed"
        elif self._port is not None:
            state = "ready"
        elif self._starting is not None and not self._starting.done():
            state = "starting"
        else:
            state = "stopped"
        return {"state": state, "error": self._error}

    def run(self) -> int:
        """How many times this server has started successfully (a fresh process, a fresh model
        load), for `VoicePack.note_speed` to key each model's dropped first sample to."""
        return self._run_count

    # --- starting -------------------------------------------------------------------------------

    async def address(self) -> tuple[str, int]:
        """The ready server's (host, port), started first if needed. Concurrent callers share one
        start, and a cancelled caller leaves it running."""
        self._cancel_idle()  # no idle stop during a start: the timer is armed again once ready
        if self._closed or self._crashes >= 2:
            raise VoiceboxError("voicebox_pack_failed")
        if self._port is None:
            if self._starting is None or self._starting.done():
                self._starting = asyncio.ensure_future(self._start())
            try:
                code = await asyncio.shield(self._starting)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                code = "voicebox_pack_failed"  # stop() cancelled the start
            if code is not None:
                raise VoiceboxError(code)
        port = self._port
        if port is None:  # the server ended again before this caller resumed
            raise VoiceboxError("voicebox_pack_failed")
        self._arm_idle()
        return _HOST, port

    async def _start(self) -> str | None:
        """Spawn the server and wait until it is ready: None, or the error code for its callers."""
        await self._reaped()  # the last server's tree has ended before the next one starts
        port = self._pick_port()
        try:
            argv = self._command(self._data_dir, port, self._parent_pid)
        except FileNotFoundError:
            self._error = "missing_exe"
            return "voicebox_pack_missing"
        env = server_env(self._data_dir) if self._env is None else self._env
        try:
            child = await self._spawn(
                argv, cwd=voice_pack.pack_dir(self._data_dir), env=env, pipes=False
            )
        except OSError:
            await self._crash("crashed")
            return "voicebox_pack_failed"
        self._child = child
        try:
            error = await self._ready(child, port)
        except BaseException:
            self._take()  # cancelled by stop(), which may have taken the child already
            raise
        if error is not None:
            await self._crash(error)
            return "voicebox_pack_failed"
        self._port, self._error = port, None
        self._run_count += 1
        self._watcher = asyncio.ensure_future(self._watch(child))
        self._arm_idle()  # for a start whose every caller was cancelled
        return None

    async def _ready(self, child: winproc.Child, port: int) -> str | None:
        """None once /health answers healthy and the child's tree owns the port, else the crash."""
        exited = asyncio.ensure_future(child.process.wait())
        try:
            async with asyncio.timeout(self._ready_timeout_s):
                while not exited.done():
                    if await self._healthy(port):
                        owned = await asyncio.to_thread(self._owns_port, child, port)
                        return None if owned else "port_taken"
                    await asyncio.wait([exited], timeout=self._poll_s)
        except TimeoutError:
            return "start_timeout"
        finally:
            exited.cancel()
        return "crashed"

    async def _healthy(self, port: int) -> bool:
        try:
            code, data = await self._call(port, "GET", "/health", None)
        except VoiceboxError:
            return False
        health = voicebox._json(data) if code == 200 else None
        return isinstance(health, dict) and health.get("status") == "healthy"

    async def _call(
        self, port: int, method: str, path: str, body: bytes | None
    ) -> tuple[int, bytes]:
        """(status, body) of one call to the server; `voicebox_pack_failed` for any transport
        failure."""
        try:
            # As in Voicebox._call: the reader thread stops at request's own deadline.
            async with asyncio.timeout(STATUS_TIMEOUT_S):
                return await asyncio.to_thread(
                    self._request, _HOST, port, method, path, body, STATUS_TIMEOUT_S, MAX_JSON_BYTES
                )
        except TimeoutError, VoiceboxError:
            raise VoiceboxError("voicebox_pack_failed") from None

    # --- crashes and stops ----------------------------------------------------------------------

    async def _watch(self, child: winproc.Child) -> None:
        """Notice an exit of the ready server; stop() cancels this before it kills."""
        await child.process.wait()
        await self._crash("crashed")

    async def _crash(self, code: str) -> None:
        logger.warning("voice pack server crashed: %s", code)
        self._cancel_idle()  # a stale idle stop must not later reset this crash away
        self._crashes += 1
        self._error = code
        self._take()
        await self._reaped()

    def _take(self) -> None:
        """End the current run: forget its port and profiles, and hand its child to a reap task
        of its own."""
        child, self._child, self._port = self._child, None, None
        self._profiles.clear()
        if child is not None:
            self._reaping = asyncio.ensure_future(_reap(child))

    async def _reaped(self) -> None:
        """Wait for the latest reap; cancelling the waiter leaves the reap running."""
        if self._reaping is not None:
            await asyncio.wait([self._reaping])

    def reset(self) -> None:
        """Forget the crashes: a failed server is stopped again, and the next address() starts
        it."""
        self._crashes, self._error = 0, None

    async def stop(self) -> None:
        """Cancel a start, then kill the server, wait until its whole tree has ended and release
        its Job."""
        tasks = [task for task in (self._starting, self._watcher) if task is not None]
        for task in tasks:
            task.cancel()
        self._take()
        if tasks:
            await asyncio.wait(tasks)
        await self._reaped()

    async def close(self) -> None:
        """Stop the server and refuse every later start."""
        self._closed = True
        self._cancel_idle()
        await self.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- idle stop ------------------------------------------------------------------------------

    def _cancel_idle(self) -> None:
        self._generation += 1
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._closed:
            return
        generation = self._generation
        self._idle = asyncio.get_running_loop().call_later(
            self._idle_stop_s, lambda: self._track(self._idle_stop(generation))
        )

    def _track(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _idle_stop(self, generation: int) -> None:
        if generation == self._generation:  # else an address() came after this was armed
            await self.stop()
            self.reset()  # a clean, quiet run forgives whatever crashed hours ago

    # --- profiles -------------------------------------------------------------------------------

    async def profile(self, language: str, voice: str) -> str:
        """The id of VIKI's preset profile for `language` and `voice`, found or created once per
        run. `voice` must be a Qwen3 preset, or a Kokoro voice pinned to `language`."""
        valid = voice in pack_voices.PRESETS or pack_voices.KOKORO_VOICES.get(voice) == language
        if language not in voice_pack.LANGUAGES or not valid:
            raise VoiceboxError("voicebox_profile_missing")
        async with self._profile_lock:
            _, port = await self.address()
            run = self._child
            key = (language, voice)
            profile_id = self._profiles.get(key)
            if profile_id is None:
                profile_id = await self._find_or_create(port, language, voice)
                if self._child is run:  # else a crash or stop ended the run and its cache
                    self._profiles[key] = profile_id
            return profile_id

    async def _find_or_create(self, port: int, language: str, voice: str) -> str:
        engine = (
            "kokoro" if pack_voices.KOKORO_VOICES.get(voice) == language else "qwen_custom_voice"
        )
        # Voicebox needs unique names: naming the voice too means a later pin of another voice
        # gets a profile of its own instead of colliding with the old one's name.
        want = {
            "name": f"VIKI {language} {voice}",
            **_PRESET,
            "preset_engine": engine,
            "preset_voice_id": voice,
        }
        code, data = await self._call(port, "GET", "/profiles", None)
        if code != 200:
            raise VoiceboxError("voicebox_refused")
        items = voicebox._json(data)
        if not isinstance(items, list):
            raise VoiceboxError("voicebox_invalid")
        for item in items:
            if isinstance(item, dict) and all(item.get(key) == want[key] for key in want):
                found = item.get("id")
                if isinstance(found, str) and voicebox._canonical_uuid(found):
                    return found
        body = json.dumps({**want, "language": language}).encode()
        code, data = await self._call(port, "POST", "/profiles", body)
        if code != 200:
            raise VoiceboxError("voicebox_refused")
        created = voicebox._json(data)
        profile_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(profile_id, str) or not voicebox._canonical_uuid(profile_id):
            raise VoiceboxError("voicebox_invalid")
        return profile_id
