"""PackInstaller against the real `winproc.spawn` of tests/fake_pack_setup.py, plus `probe_gpu`
against a stubbed `winproc`."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from viki_agent import winproc
from viki_agent.voice import voice_pack
from viki_agent.voice import voice_pack_install as vpi
from viki_agent.voice.voice_pack import Gpu
from viki_agent.voice.voice_pack_install import PackInstaller, PackRefused, _remove_tree
from viki_agent.voice.voice_pack_service import VoicePack

HANG_GUARD_S = 10
FAKE = Path(__file__).parent.parent / "fake_pack_setup.py"


def _lines_env(lines: list[str], *, hold: bool = False, log: Path | None = None) -> dict[str, str]:
    env = {"FAKE_PACK_SETUP_LINES": json.dumps(lines)}
    if hold:
        env["FAKE_PACK_SETUP_HOLD"] = "1"
    if log is not None:
        env["FAKE_PACK_SETUP_LOG"] = str(log)
    return env


@dataclass
class Call:
    argv: list[str]
    cwd: Any
    env: dict[str, str]
    memory_limit_bytes: int | None


class Recorder:
    """winproc.spawn, recording each call; `extra_env` rides along with the real spawn only,
    so recorded `env` still reflects what PackInstaller itself computed."""

    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self.calls: list[Call] = []
        self.children: list[winproc.Child] = []
        self.extra_env = extra_env or {}

    async def __call__(
        self, argv: list[str], *, cwd: Any, env: dict[str, str], memory_limit_bytes: int | None
    ) -> winproc.Child:
        self.calls.append(Call(list(argv), cwd, dict(env), memory_limit_bytes))
        child = await winproc.spawn(
            argv, cwd=cwd, env={**env, **self.extra_env}, memory_limit_bytes=memory_limit_bytes
        )
        self.children.append(child)
        return child


class FakeInstalled:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, data_dir: Path) -> bool:
        self.calls += 1
        return self.value


class FakeProbe:
    """A `probe` stub: synchronous, optionally pausing (from the thread `check()` runs it in)
    until `release` is set, so a test can observe the "checking" status in between."""

    def __init__(self, result: Gpu | str) -> None:
        self.result = result
        self.calls = 0
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> Gpu | str:
        self.calls += 1
        if self.block:
            self.started.set()
            assert self.release.wait(HANG_GUARD_S)
        return self.result


class PendingChild:
    """A setup child with memory-only pipes and counted teardown operations."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.kills = self.waits = self.tree_waits = self.releases = 0
        self.wait_entered = asyncio.Event()
        self.wait_hold: asyncio.Event | None = None
        self.process = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: None),
            stdout=self.stdout,
            stderr=self.stderr,
            wait=self.wait,
        )

    def kill_tree(self) -> None:
        self.kills += 1
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.waits += 1
        self.wait_entered.set()
        if self.wait_hold is not None:
            await self.wait_hold.wait()
        return 0

    async def wait_tree(self, *, poll_s: float) -> None:
        self.tree_waits += 1

    def release(self) -> None:
        self.releases += 1


async def _poll_status(
    installer: PackInstaller,
    cond: Callable[[dict[str, Any]], bool],
    *,
    timeout_s: float = HANG_GUARD_S,
) -> dict[str, Any]:
    async with asyncio.timeout(timeout_s):
        while True:
            current = await installer.status()
            if cond(current):
                return current
            await asyncio.sleep(0.01)


def make(
    data_dir: Path,
    *,
    spawn: Recorder | None = None,
    probe: Callable[[], Gpu | str] | None = None,
    is_installed: Callable[[Path], bool] | None = None,
    remove_tree: Callable[[Path], None] | None = None,
    **kwargs: Any,
) -> tuple[PackInstaller, Recorder]:
    spawn = spawn or Recorder()
    kwargs.setdefault("script", FAKE)
    kwargs.setdefault("platform", "win32")
    kwargs.setdefault("machine", "AMD64")
    if probe is not None:
        kwargs["probe"] = probe
    if is_installed is not None:
        kwargs["is_installed"] = is_installed
    if remove_tree is not None:
        kwargs["remove_tree"] = remove_tree
    return PackInstaller(data_dir, spawn=spawn, **kwargs), spawn


# --- install, progress, cancel --------------------------------------------------------------


async def test_install_spawns_the_setup_child_and_reports_progress(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    recorder = Recorder(
        _lines_env(
            [json.dumps({"type": "progress", "step": "server", "done": 1, "total": 4})],
            hold=True,
            log=log,
        )
    )
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["progress"] is not None)
        assert status == {
            "state": "installing",
            "reason": None,
            "error": None,
            "progress": {"step": "server", "done": 1, "total": 4},
            "gpu": None,
        }

        [call] = recorder.calls
        assert call.argv == [
            sys.executable,
            "-I",
            "-B",
            str(FAKE),
            "install",
            "--data",
            str(tmp_path),
        ]
        assert call.cwd == tmp_path
        assert call.memory_limit_bytes == vpi.SETUP_MEMORY_BYTES

        logged = json.loads(log.read_text(encoding="ascii").splitlines()[0])
        assert logged == ["install", "--data", str(tmp_path)]
    finally:
        await installer.close()


async def test_compiled_spawns_the_installed_binarys_viki_worker_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "install"
    monkeypatch.setattr(vpi.compiled, "COMPILED", True)
    monkeypatch.setattr(vpi.compiled, "INSTALL_DIR", install_dir)
    recorder = Recorder()
    installer, _ = make(
        tmp_path,
        spawn=recorder,
        is_installed=FakeInstalled(True),
        platform="win32",
        machine="AMD64",
    )
    try:
        with pytest.raises(OSError):
            await installer.install(repair=True)
        [call] = recorder.calls
        assert call.argv == [
            str(install_dir / vpi.compiled.APP_BINARY),
            "--viki-worker",
            "voice-pack-setup",
            "install",
            "--data",
            str(tmp_path),
            "--repair",
        ]
    finally:
        await installer.close()


async def test_proxy_environment_variables_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    recorder = Recorder(_lines_env([json.dumps({"type": "done"})]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(True))
    try:
        await installer.install()
        await _poll_status(installer, lambda s: s["state"] != "installing")
        [call] = recorder.calls
        assert call.env["HTTPS_PROXY"] == "http://proxy.example:8080"
    finally:
        await installer.close()


async def test_cancel_keeps_parts_and_reports_cancelled(tmp_path: Path) -> None:
    pack = voice_pack.pack_dir(tmp_path)
    pack.mkdir(parents=True)
    (pack / "partial.bin").write_bytes(b"partial data")
    recorder = Recorder(
        _lines_env(
            [json.dumps({"type": "progress", "step": "unpack", "done": 1, "total": 2})], hold=True
        )
    )
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))

    await installer.install()
    await _poll_status(installer, lambda s: s["progress"] is not None)
    pid = recorder.children[0].pid

    await installer.cancel()

    status = await installer.status()
    assert status == {
        "state": "install_failed",
        "reason": None,
        "error": "cancelled",
        "progress": None,
        "gpu": None,
    }
    assert winproc.process_start_time(pid) is None
    assert (pack / "partial.bin").exists()
    await installer.close()


@pytest.mark.parametrize("facade", [False, True], ids=["installer", "facade"])
@pytest.mark.parametrize("cancel_waiter", [False, True], ids=["waiting", "caller-cancelled"])
async def test_close_reaps_a_pending_spawn_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, facade: bool, cancel_waiter: bool
) -> None:
    entered, release, close_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    child = PendingChild()
    if cancel_waiter:
        child.wait_hold = asyncio.Event()
    spawns = 0

    async def spawn(*args: Any, **kwargs: Any) -> winproc.Child:
        nonlocal spawns
        spawns += 1
        entered.set()
        await release.wait()
        return cast(winproc.Child, child)

    installer = PackInstaller(
        tmp_path, spawn=spawn, is_installed=FakeInstalled(False), platform="win32", machine="AMD64"
    )
    reader = AsyncMock(wraps=installer._read)
    monkeypatch.setattr(installer, "_read", reader)
    server_closed = False

    async def close_server() -> None:
        nonlocal server_closed
        server_closed = True

    server: Any = SimpleNamespace(close=close_server, reset=lambda: None)
    owner = VoicePack(tmp_path, installer, server) if facade else installer

    async def close() -> None:
        close_entered.set()
        await owner.close()

    installing = asyncio.create_task(owner.install())
    closing: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            await entered.wait()
            closing = asyncio.create_task(close())
            await close_entered.wait()
            returned_before_spawn = closing.done()
            release.set()
            assert not returned_before_spawn
            if cancel_waiter:
                await child.wait_entered.wait()
                installing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await installing
                assert not closing.done()
                assert child.wait_hold is not None
                child.wait_hold.set()
            results = await asyncio.gather(installing, closing, return_exceptions=True)
        if cancel_waiter:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert isinstance(results[0], PackRefused) and results[0].code == "pack_busy"
        assert results[1] is None
        assert not facade or server_closed
        assert (child.kills, child.waits, child.tree_waits, child.releases) == (1, 1, 1, 1)
        assert installer._child is None and installer._reader_task is None
        reader.assert_not_called()
        assert (await installer.status())["state"] != "installing"
        await owner.close()
        with pytest.raises(PackRefused) as caught:
            await owner.install()
        assert caught.value.code == "pack_busy"
        assert spawns == 1
        assert (child.kills, child.waits, child.tree_waits, child.releases) == (1, 1, 1, 1)
    finally:
        release.set()
        if child.wait_hold is not None:
            child.wait_hold.set()
        await asyncio.gather(
            installing, *([closing] if closing is not None else []), return_exceptions=True
        )
        await owner.close()


# --- the reader's protocol ------------------------------------------------------------------


async def test_done_confirms_install_and_clears_any_error(tmp_path: Path) -> None:
    recorder = Recorder(_lines_env([json.dumps({"type": "done"})]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(True))
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
        assert status["state"] == "installed"
        assert status["error"] is None
    finally:
        await installer.close()


async def test_done_while_not_installed_reports_setup_failed(tmp_path: Path) -> None:
    recorder = Recorder(_lines_env([json.dumps({"type": "done"})]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
        assert status["state"] == "install_failed"
        assert status["error"] == "setup_failed"
    finally:
        await installer.close()


async def test_eof_without_done_or_error_reports_setup_failed(tmp_path: Path) -> None:
    recorder = Recorder(
        _lines_env([json.dumps({"type": "progress", "step": "model", "done": 1, "total": 2})])
    )
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
        assert status["state"] == "install_failed"
        assert status["error"] == "setup_failed"
    finally:
        await installer.close()


async def test_error_line_reports_its_code(tmp_path: Path) -> None:
    recorder = Recorder(_lines_env([json.dumps({"type": "error", "code": "disk_full"})]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
        assert status == {
            "state": "install_failed",
            "reason": None,
            "error": "disk_full",
            "progress": None,
            "gpu": None,
        }
    finally:
        await installer.close()


PROTOCOL_VIOLATIONS = [
    pytest.param("not json at all", id="not-json"),
    pytest.param(
        json.dumps({"type": "progress", "step": "server", "done": 1, "total": 2, "extra": "x"}),
        id="progress-extra-key",
    ),
    pytest.param(
        json.dumps({"type": "progress", "step": "server", "done": 5, "total": 2}),
        id="progress-done-over-total",
    ),
    pytest.param(
        json.dumps({"type": "progress", "step": "bogus", "done": 1, "total": 2}),
        id="progress-step-outside-steps",
    ),
    pytest.param(
        json.dumps({"type": "progress", "step": "server", "done": True, "total": 2}),
        id="progress-bool-done",
    ),
    pytest.param(json.dumps({"type": "error", "code": "not_a_real_code"}), id="error-unknown-code"),
    pytest.param('{"type":"done","x":1}', id="done-extra-key"),
    pytest.param(
        '{"type": "progress", "step": "server", "step": "model", "done": 1, "total": 2}',
        id="duplicate-key",
    ),
]


@pytest.mark.parametrize("line", PROTOCOL_VIOLATIONS)
async def test_a_protocol_violation_kills_the_child_and_reports_protocol_error(
    tmp_path: Path, line: str
) -> None:
    recorder = Recorder(_lines_env([line]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    status: dict[str, Any] | None = None
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
    finally:
        pid = recorder.children[0].pid
        await installer.close()  # awaits the reader task, so _reap() has fully run by now
    assert status is not None
    assert status["state"] == "install_failed"
    assert status["error"] == "protocol_error"
    assert winproc.process_start_time(pid) is None


async def _run_custom_setup_script(tmp_path: Path, code: str) -> tuple[dict[str, Any], int]:
    """Like `test_a_protocol_violation...` above, but the child is a throwaway script instead of
    `fake_pack_setup.py`: its output can't always travel through `FAKE_PACK_SETUP_LINES` (an
    env var, capped at 32,767 characters on Windows, and the fake only ever emits valid ASCII)."""
    script = tmp_path / "custom_setup.py"
    script.write_text(code, encoding="ascii")
    recorder = Recorder()
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False), script=script)
    status: dict[str, Any] | None = None
    try:
        await installer.install()
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
    finally:
        pid = recorder.children[0].pid
        await installer.close()
    assert status is not None
    return status, pid


async def test_a_non_ascii_line_is_a_protocol_violation(tmp_path: Path) -> None:
    status, pid = await _run_custom_setup_script(
        tmp_path, "import sys\nsys.stdout.buffer.write('caf\\u00e9'.encode('utf-8') + b'\\n')\n"
    )
    assert status["state"] == "install_failed"
    assert status["error"] == "protocol_error"
    assert winproc.process_start_time(pid) is None


async def test_a_line_over_64kib_is_a_protocol_violation(tmp_path: Path) -> None:
    status, pid = await _run_custom_setup_script(
        tmp_path, "import sys\nsys.stdout.buffer.write(b'a' * 70_000 + b'\\n')\n"
    )
    assert status["state"] == "install_failed"
    assert status["error"] == "protocol_error"
    assert winproc.process_start_time(pid) is None


# --- refusals --------------------------------------------------------------------------------


async def test_install_refuses_on_an_unsupported_platform_and_check_skips_the_probe(
    tmp_path: Path,
) -> None:
    probe = FakeProbe("no_nvidia")
    installer, recorder = make(
        tmp_path, probe=probe, is_installed=FakeInstalled(False), platform="darwin", machine="arm64"
    )

    status = await installer.status()
    assert status["state"] == "unsupported"
    assert status["reason"] == "platform"

    await installer.check()
    assert probe.calls == 0

    with pytest.raises(PackRefused) as exc:
        await installer.install()
    assert exc.value.code == "pack_unsupported"
    assert recorder.calls == []


async def test_install_refuses_while_already_installing(tmp_path: Path) -> None:
    recorder = Recorder(
        _lines_env(
            [json.dumps({"type": "progress", "step": "server", "done": 0, "total": 1})], hold=True
        )
    )
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(False))
    try:
        await installer.install()
        await _poll_status(installer, lambda s: s["state"] == "installing")

        with pytest.raises(PackRefused) as exc:
            await installer.install()
        assert exc.value.code == "pack_busy"
    finally:
        await installer.close()


async def test_install_refuses_while_removing(tmp_path: Path) -> None:
    remover = threading.Event()
    release = threading.Event()

    def slow_remove(path: Path) -> None:
        remover.set()
        assert release.wait(HANG_GUARD_S)

    installer, _ = make(tmp_path, is_installed=FakeInstalled(False), remove_tree=slow_remove)
    task = asyncio.ensure_future(installer.remove())
    try:
        await asyncio.to_thread(remover.wait, HANG_GUARD_S)
        assert (await installer.status())["state"] == "removing"

        with pytest.raises(PackRefused) as exc:
            await installer.install()
        assert exc.value.code == "pack_busy"
    finally:
        release.set()
        await task


async def test_repair_is_refused_until_installed_or_failed_then_runs_with_the_flag(
    tmp_path: Path,
) -> None:
    installed = FakeInstalled(False)
    installer, recorder = make(tmp_path, is_installed=installed)

    with pytest.raises(PackRefused) as exc:
        await installer.install(repair=True)
    assert exc.value.code == "pack_busy"

    # A first, failing install moves the state to install_failed.
    recorder.extra_env = _lines_env([json.dumps({"type": "error", "code": "network"})])
    await installer.install()
    await _poll_status(installer, lambda s: s["state"] != "installing")
    assert (await installer.status())["state"] == "install_failed"

    # install_failed is one of the states repair is allowed from.
    recorder.extra_env = _lines_env([json.dumps({"type": "done"})])
    installed.value = True
    await installer.install(repair=True)
    await _poll_status(installer, lambda s: s["state"] != "installing")
    assert (await installer.status())["state"] == "installed"
    assert recorder.calls[-1].argv[-4:] == ["install", "--data", str(tmp_path), "--repair"]
    await installer.close()


# --- the GPU probe, cached on the instance ----------------------------------------------------


async def test_check_probes_once_and_caches_until_again(tmp_path: Path) -> None:
    gpu = Gpu("Fake GPU", 8192, (560, 10))  # below MIN_DRIVER: unsupported("driver_old")
    probe = FakeProbe(gpu)
    installer, _ = make(tmp_path, probe=probe, is_installed=FakeInstalled(False))

    assert (await installer.status())["state"] == "unchecked"
    assert (await installer.status())["gpu"] is None

    probe.block = True
    task = asyncio.ensure_future(installer.check())
    await asyncio.to_thread(probe.started.wait, HANG_GUARD_S)
    assert (await installer.status())["state"] == "checking"
    probe.release.set()
    await task

    status = await installer.status()
    assert status["state"] == "unsupported"
    assert status["reason"] == "driver_old"
    assert status["gpu"] == {"name": "Fake GPU", "memory_mib": 8192, "driver": "560.10"}
    assert probe.calls == 1

    await installer.check()
    assert probe.calls == 1  # cached: no re-probe
    await installer.check(again=True)
    assert probe.calls == 2


async def test_check_skips_the_probe_once_installed(tmp_path: Path) -> None:
    probe = FakeProbe("no_nvidia")
    installer, _ = make(tmp_path, probe=probe, is_installed=FakeInstalled(True))

    await installer.check()

    assert probe.calls == 0
    assert (await installer.status())["state"] == "installed"


# --- remove ------------------------------------------------------------------------------------


async def test_remove_deletes_the_pack_dir(tmp_path: Path) -> None:
    pack = voice_pack.pack_dir(tmp_path)
    pack.mkdir(parents=True)
    (pack / "marker.txt").write_text("x", encoding="ascii")
    installer, _ = make(tmp_path, is_installed=FakeInstalled(False))

    await installer.remove()

    assert not pack.exists()
    assert (await installer.status())["error"] is None


@pytest.mark.parametrize("is_installed", [False, True])
async def test_remove_reports_remove_failed(tmp_path: Path, is_installed: bool) -> None:
    def boom(path: Path) -> None:
        raise OSError("nope")

    installer, _ = make(tmp_path, is_installed=FakeInstalled(is_installed), remove_tree=boom)

    await installer.remove()

    status = await installer.status()
    assert status["state"] == "install_failed"
    assert status["error"] == "remove_failed"


async def test_a_failed_repair_with_the_pack_still_installed_reports_install_failed(
    tmp_path: Path,
) -> None:
    recorder = Recorder(_lines_env([json.dumps({"type": "error", "code": "network"})]))
    installer, _ = make(tmp_path, spawn=recorder, is_installed=FakeInstalled(True))
    try:
        await installer.install(repair=True)
        status = await _poll_status(installer, lambda s: s["state"] != "installing")
        assert status["state"] == "install_failed"
        assert status["error"] == "network"
    finally:
        await installer.close()


def test_remove_tree_refuses_a_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "not_a_dir"
    target.write_text("x", encoding="ascii")

    with pytest.raises(ValueError):
        _remove_tree(target)
    assert target.exists()


def test_remove_tree_is_fine_with_a_missing_path(tmp_path: Path) -> None:
    _remove_tree(tmp_path / "missing")  # no error


# --- probe_gpu, against a stubbed winproc ------------------------------------------------------


def test_probe_gpu_reports_no_nvidia_when_the_exe_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vpi.winproc, "system_dir", lambda: tmp_path)
    assert vpi.probe_gpu() == "no_nvidia"


def test_probe_gpu_reports_no_nvidia_on_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nvidia-smi.exe").write_bytes(b"")
    monkeypatch.setattr(vpi.winproc, "system_dir", lambda: tmp_path)
    monkeypatch.setattr(
        vpi.winproc, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="")
    )
    assert vpi.probe_gpu() == "no_nvidia"


def test_probe_gpu_reports_probe_failed_on_garbage_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nvidia-smi.exe").write_bytes(b"")
    monkeypatch.setattr(vpi.winproc, "system_dir", lambda: tmp_path)
    monkeypatch.setattr(
        vpi.winproc, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="garbage")
    )
    assert vpi.probe_gpu() == "probe_failed"


def test_probe_gpu_reports_probe_failed_on_any_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nvidia-smi.exe").write_bytes(b"")
    monkeypatch.setattr(vpi.winproc, "system_dir", lambda: tmp_path)

    def boom(argv: list[str], **kw: Any) -> None:
        raise OSError("boom")

    monkeypatch.setattr(vpi.winproc, "run", boom)
    assert vpi.probe_gpu() == "probe_failed"


def test_probe_gpu_runs_nvidia_smi_with_the_right_query_and_parses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nvidia-smi.exe").write_bytes(b"")
    monkeypatch.setattr(vpi.winproc, "system_dir", lambda: tmp_path)
    calls: list[tuple[list[str], Any, float]] = []

    def fake_run(argv: list[str], *, cwd: Any, timeout: float) -> SimpleNamespace:
        calls.append((argv, cwd, timeout))
        return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 4090, 24564, 572.47\n")

    monkeypatch.setattr(vpi.winproc, "run", fake_run)

    assert vpi.probe_gpu() == Gpu("NVIDIA GeForce RTX 4090", 24564, (572, 47))
    [(argv, cwd, timeout)] = calls
    assert argv == [
        str(tmp_path / "nvidia-smi.exe"),
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ]
    assert cwd == tmp_path
    assert timeout == vpi.PROBE_TIMEOUT_S
