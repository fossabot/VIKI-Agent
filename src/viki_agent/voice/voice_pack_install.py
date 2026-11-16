"""Manages the voice-pack setup child process, and the one-per-run NVIDIA GPU probe.

The setup child (`voice_pack_setup.py`, owned elsewhere) is started as
``python -I -B voice_pack_setup.py install --data <dir> [--repair]`` (compiled, instead
``<app> --viki-worker voice-pack-setup install --data <dir> [--repair]``) and speaks a line
protocol on stdout: one ASCII JSON object per line, each either a ``progress`` step, a terminal
``done``, or a terminal ``error`` naming one of `viki_agent.voice_pack.SETUP_CODES`.
`PackInstaller` owns one such child at a time, reads that protocol strictly (a violation kills
the child), and answers
`status()` from its own state plus `voice_pack.installed()` and a cached GPU probe.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import stat
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from viki_agent import compiled, winproc
from viki_agent.media import media_worker
from viki_agent.voice import voice_pack
from viki_agent.voice.voice_engine import _drop, _line_object, _reap

SETUP_SCRIPT = Path(__file__).with_name("voice_pack_setup.py")
PROBE_TIMEOUT_S = 15.0
SETUP_MEMORY_BYTES = 1 << 30

_LINE_ERRORS = (ValueError, TypeError, RecursionError)
_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_PROGRESS_KEYS = frozenset({"type", "step", "done", "total"})


class PackRefused(Exception):
    """An `install()` the caller must not retry as-is. `.code` is "pack_unsupported" or
    "pack_busy"."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def probe_gpu() -> voice_pack.Gpu | str:
    """The local NVIDIA GPU, or why it could not be probed. Blocking: call via a thread."""
    try:
        system_dir = winproc.system_dir()
        exe = system_dir / "nvidia-smi.exe"
        if not exe.is_file():
            return "no_nvidia"
        done = winproc.run(
            [
                str(exe),
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            cwd=system_dir,
            timeout=PROBE_TIMEOUT_S,
        )
    except Exception:
        return "probe_failed"
    if done.returncode != 0:
        return "no_nvidia"
    return voice_pack.parse_gpu(done.stdout) or "probe_failed"


def _setup_env(environ: Mapping[str, str]) -> dict[str, str]:
    env = media_worker._worker_env(sys.platform, environ)
    env.update({k: v for k, v in environ.items() if k.upper() in _PROXY_NAMES})
    return env


def _remove_tree(path: Path) -> None:
    """Delete `path` recursively. Refuses a reparse point or anything but a directory; a path
    that is already gone is fine."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if reparse or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"refusing to remove a reparse point or non-directory: {path}")
    shutil.rmtree(path)


class PackInstaller:
    def __init__(
        self,
        data_dir: Path,
        *,
        python: str = sys.executable,
        script: Path = SETUP_SCRIPT,
        spawn: Callable[..., Awaitable[winproc.Child]] = winproc.spawn,
        probe: Callable[[], voice_pack.Gpu | str] = probe_gpu,
        is_installed: Callable[[Path], bool] = voice_pack.installed,
        remove_tree: Callable[[Path], None] = _remove_tree,
        platform: str = sys.platform,
        machine: str = platform.machine(),
    ) -> None:
        self._data_dir = data_dir
        self._python = python
        self._script = script
        self._spawn = spawn
        self._probe = probe
        self._is_installed = is_installed
        self._remove_tree = remove_tree
        self._platform = platform
        self._machine = machine
        self._probe_result: voice_pack.Gpu | str | None = None
        self._checking = False
        self._child: winproc.Child | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._progress: dict[str, Any] | None = None
        self._error: str | None = None
        self._removing = False
        self._remove_task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._closed = False
        self._install_lock = asyncio.Lock()

    def _platform_unsupported(self) -> bool:
        return voice_pack.unsupported(self._platform, self._machine, None) == "platform"

    def _gpu_field(self) -> dict[str, Any] | None:
        probe = self._probe_result
        if not isinstance(probe, voice_pack.Gpu):
            return None
        return {
            "name": probe.name,
            "memory_mib": probe.memory_mib,
            "driver": f"{probe.driver[0]}.{probe.driver[1]}",
        }

    def _status_dict(
        self,
        state: str,
        *,
        reason: str | None = None,
        error: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "reason": reason,
            "error": error,
            "progress": progress,
            "gpu": self._gpu_field(),
        }

    async def status(self) -> dict[str, Any]:
        if self._platform_unsupported():
            return self._status_dict("unsupported", reason="platform")
        if self._removing:
            return self._status_dict("removing")
        if self._child is not None:
            return self._status_dict("installing", progress=self._progress)
        if self._error is not None:
            return self._status_dict("install_failed", error=self._error)
        if await asyncio.to_thread(self._is_installed, self._data_dir):
            return self._status_dict("installed")
        if self._checking:
            return self._status_dict("checking")
        if self._probe_result is None:
            return self._status_dict("unchecked")
        reason = voice_pack.unsupported(self._platform, self._machine, self._probe_result)
        if reason is not None:
            return self._status_dict("unsupported", reason=reason)
        return self._status_dict("not_installed")

    async def check(self, *, again: bool = False) -> None:
        if self._platform_unsupported():
            return
        if await asyncio.to_thread(self._is_installed, self._data_dir):
            return
        if self._probe_result is not None and not again:
            return
        self._checking = True
        try:
            self._probe_result = await asyncio.to_thread(self._probe)
        finally:
            self._checking = False

    async def install(self, *, repair: bool = False) -> None:
        # cancel/close must account for a child still inside the awaited spawn.
        async with self._install_lock:
            if self._closed:
                raise PackRefused("pack_busy")
            state = (await self.status())["state"]
            if self._closed:
                raise PackRefused("pack_busy")
            if state == "unsupported":
                raise PackRefused("pack_unsupported")
            if state in ("installing", "removing"):
                raise PackRefused("pack_busy")
            if repair and state not in ("installed", "install_failed"):
                raise PackRefused("pack_busy")
            self._error = None
            self._cancel_requested = False
            argv = compiled.worker_argv(
                "voice-pack-setup",
                ["install", "--data", str(self._data_dir), *(["--repair"] if repair else [])],
                python=self._python,
                script=self._script,
            )
            env = _setup_env(os.environ)
            child = await self._spawn(
                argv, cwd=self._data_dir, env=env, memory_limit_bytes=SETUP_MEMORY_BYTES
            )
            process = child.process
            assert process.stdin is not None
            assert process.stdout is not None and process.stderr is not None
            process.stdin.close()
            self._progress = None
            draining = asyncio.create_task(_drop(process.stderr))
            if self._closed or self._cancel_requested:
                self._error = "cancelled"
                # A cancelled install caller must leave teardown owned for close to await.
                self._reader_task = asyncio.create_task(_reap(child, draining))
                await asyncio.shield(self._reader_task)
                raise PackRefused("pack_busy")
            self._child = child
            self._reader_task = asyncio.create_task(self._read(child, draining))

    async def _read(self, child: winproc.Child, draining: asyncio.Task[None]) -> None:
        process = child.process
        assert process.stdout is not None
        error: str | None = None
        try:
            while line := await process.stdout.readline():
                data = _line_object(line)
                kind = data.get("type")
                if kind == "progress":
                    if (
                        set(data) != _PROGRESS_KEYS
                        or data["step"] not in voice_pack.STEPS
                        or type(data["done"]) is not int
                        or type(data["total"]) is not int
                        or not 0 <= data["done"] <= data["total"]
                    ):
                        raise ValueError("progress")
                    self._progress = {
                        "step": data["step"],
                        "done": data["done"],
                        "total": data["total"],
                    }
                    continue
                if kind == "done":
                    if set(data) != {"type"}:
                        raise ValueError("done")
                    if not await asyncio.to_thread(self._is_installed, self._data_dir):
                        error = "setup_failed"
                    break
                if kind == "error":
                    if set(data) != {"type", "code"} or data["code"] not in voice_pack.SETUP_CODES:
                        raise ValueError("error")
                    error = data["code"]
                    break
                raise ValueError("type")
            else:
                error = "cancelled" if self._cancel_requested else "setup_failed"
        except _LINE_ERRORS:
            child.kill_tree()
            error = "protocol_error"
        self._error = error
        self._child = None
        self._progress = None
        await _reap(child, draining)

    async def cancel(self) -> None:
        self._cancel_requested = True
        async with self._install_lock:
            if self._child is not None:
                self._child.kill_tree()
            reader = self._reader_task
            if reader is not None:
                await asyncio.shield(reader)
                if self._reader_task is reader:
                    self._reader_task = None

    async def remove(self) -> None:
        if self._closed:
            raise PackRefused("pack_busy")
        await self.cancel()
        if self._closed:
            raise PackRefused("pack_busy")
        self._removing = True
        try:
            self._remove_task = asyncio.ensure_future(
                asyncio.to_thread(self._remove_tree, voice_pack.pack_dir(self._data_dir))
            )
            await self._remove_task
            self._error = None
        except Exception:
            self._error = "remove_failed"
        finally:
            self._removing = False
            self._remove_task = None

    async def close(self) -> None:
        self._closed = True
        await self.cancel()
        if self._remove_task is not None:
            await asyncio.wait([self._remove_task])
