"""PackServer: the voice pack's Voicebox server, started on demand, against tests/fake_voicebox.py.

Process tests spawn the fake through the real `winproc.spawn` and poll real OS state under
HANG_GUARD_S. The real `owns_port` runs only on Windows; elsewhere, and where a test decides the
ownership, it is injected.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import uuid
from collections.abc import Awaitable, Collection, Mapping
from pathlib import Path
from typing import Any

import pytest

from viki_agent import winproc
from viki_agent.voice import voice_pack, voice_pack_server, voicebox_http
from viki_agent.voice.voice_pack_server import PackServer
from viki_agent.voice.voicebox_http import VoiceboxError

# A guard on events that normally take well under a second (a start, a kill, a noticed exit).
HANG_GUARD_S = 10.0
FAKE = Path(__file__).parent.parent / "fake_voicebox.py"
PRESET = {"voice_type": "preset", "preset_engine": "qwen_custom_voice"}
KOKORO_PRESET = {"voice_type": "preset", "preset_engine": "kokoro"}

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the ownership reads")


def _owned(child: winproc.Child, port: int) -> bool:
    return True


OWNS = voice_pack_server.owns_port if sys.platform == "win32" else _owned


class Child(winproc.Child):
    """A Child that records how many processes its Job still held when it was released."""

    active_at_release: int | None = None

    def release(self) -> None:
        if self.active_at_release is None:
            self.active_at_release = self.job.active_processes()
        super().release()


class Spawns:
    """winproc.spawn for the fake: records each call and child, and adds the fake's mode and log
    to the environment the server chose."""

    def __init__(self, data: Path, mode: str) -> None:
        self.mode = mode
        self.log = data / "fake.jsonl"
        self.calls: list[dict[str, Any]] = []
        self.children: list[Child] = []

    async def __call__(
        self, argv: list[str], *, cwd: Path, env: Mapping[str, str], pipes: bool
    ) -> winproc.Child:
        self.calls.append({"cwd": cwd, "env": dict(env), "pipes": pipes})
        env = {**env, "FAKE_VOICEBOX_MODE": self.mode, "FAKE_VOICEBOX_LOG": str(self.log)}
        spawned = await winproc.spawn(argv, cwd=cwd, env=env, pipes=pipes)
        child = Child(spawned.process, spawned.job)
        self.children.append(child)
        return child

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def all_reaped(self) -> bool:
        """Every Job was released, and only once its whole tree had ended."""
        return all(child.active_at_release == 0 for child in self.children)


class Requests:
    """voicebox_http.request, recording each (method, path); `answers` replaces some answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.answers: dict[tuple[str, str], tuple[int, bytes] | VoiceboxError] = {}

    def __call__(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        timeout: float,
        limit: int,
    ) -> tuple[int, bytes]:
        self.calls.append((method, path))
        answer = self.answers.get((method, path))
        if answer is None:
            return voicebox_http.request(host, port, method, path, body, timeout, limit)
        if isinstance(answer, VoiceboxError):
            raise answer
        return answer


def install_exe(data: Path) -> Path:
    exe = voice_pack.server_dir(data) / voice_pack.SERVER_EXE
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    exe.chmod(0o755)
    return exe


def fake_command(data_dir: Path, port: int, parent_pid: int) -> list[str]:
    """The real command's flags, run by the fake instead of the pack's exe."""
    return [sys.executable, str(FAKE), *voice_pack_server.command(data_dir, port, parent_pid)[1:]]


def make(data: Path, mode: str = "ok", **kwargs: Any) -> tuple[PackServer, Spawns]:
    install_exe(data)
    spawns = Spawns(data, mode)
    options: dict[str, Any] = {"owns_port": OWNS, "poll_s": 0.02, "ready_timeout_s": HANG_GUARD_S}
    server = PackServer(data, command=fake_command, spawn=spawns, **{**options, **kwargs})
    return server, spawns


async def refused(call: Awaitable[object], code: str) -> None:
    with pytest.raises(VoiceboxError) as caught:
        await call
    assert caught.value.code == code


async def call(port: int, method: str, path: str, body: object = None) -> tuple[int, bytes]:
    """One request to the fake, past the server under test."""
    data = None if body is None else json.dumps(body).encode()
    return await asyncio.to_thread(
        voicebox_http.request,
        "127.0.0.1",
        port,
        method,
        path,
        data,
        voicebox_http.STATUS_TIMEOUT_S,
        voicebox_http.MAX_WAV_BYTES,
    )


async def gone(pids: Collection[int]) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while any(winproc.process_start_time(pid) is not None for pid in pids):
            await asyncio.sleep(0.02)


async def spawned(spawns: Spawns) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while not spawns.children:
            await asyncio.sleep(0.01)


async def left_ready(server: PackServer) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while server.status()["state"] == "ready":
            await asyncio.sleep(0.02)


# --- pure helpers -------------------------------------------------------------------------------


def test_server_env_keeps_the_named_variables_and_points_the_model_cache_at_the_pack(
    tmp_path: Path,
) -> None:
    pack = voice_pack.pack_dir(tmp_path)
    model_env = {
        "VOICEBOX_MODELS_DIR": str(pack / "models"),
        "HF_HOME": str(pack / "hf"),
        "HF_HUB_OFFLINE": "1",
    }
    names = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE")
    kept = {name: f"value-{name}" for name in names}
    environ = {**kept, "PATH": "p", "PYTHONPATH": "pp", "CLAUDECODE": "1", "HF_HUB_OFFLINE": "0"}
    assert voice_pack_server.server_env(tmp_path, environ) == {**kept, **model_env}
    assert voice_pack_server.server_env(tmp_path, {}) == model_env


def test_command_runs_the_pack_exe_outside_its_data_dir_and_refuses_a_missing_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        voice_pack_server.command(tmp_path, 49152, 4242)
    exe = install_exe(tmp_path)
    data_dir = voice_pack.state_dir(tmp_path)
    assert voice_pack_server.command(tmp_path, 49152, 4242) == [
        str(exe),
        "--host",
        "127.0.0.1",
        "--port",
        "49152",
        "--data-dir",
        str(data_dir),
        "--parent-pid",
        "4242",
    ]
    assert not exe.is_relative_to(data_dir)


async def test_a_missing_exe_is_refused_before_any_spawn(tmp_path: Path) -> None:
    spawns = Spawns(tmp_path, "ok")
    server = PackServer(tmp_path, spawn=spawns, owns_port=OWNS)  # the real command: no exe
    try:
        for _ in range(3):  # not a crash: it never fails the server
            await refused(server.address(), "voicebox_pack_missing")
        assert server.status() == {"state": "stopped", "error": "missing_exe"}
        assert spawns.calls == []
    finally:
        await server.close()


# --- starting -----------------------------------------------------------------------------------


async def test_address_starts_the_server_once_for_concurrent_callers(tmp_path: Path) -> None:
    server, spawns = make(tmp_path)
    try:
        assert server.status() == {"state": "stopped", "error": None}
        answers = await asyncio.gather(*(server.address() for _ in range(3)))
        assert len(set(answers)) == 1 and answers[0][0] == "127.0.0.1"
        assert len(spawns.children) == 1
        assert server.status() == {"state": "ready", "error": None}
        assert await server.address() == answers[0]  # ready: answered at once
        assert len(spawns.children) == 1
    finally:
        await server.close()
    assert spawns.all_reaped()


async def test_the_server_runs_with_the_designed_argv_cwd_and_env(tmp_path: Path) -> None:
    server, spawns = make(tmp_path)
    try:
        _, port = await server.address()
    finally:
        await server.close()
    pack = voice_pack.pack_dir(tmp_path)
    [spawn_call] = spawns.calls
    assert spawn_call == {
        "cwd": pack,
        "env": voice_pack_server.server_env(tmp_path),
        "pipes": False,
    }
    [line] = spawns.lines()
    assert line == {
        "argv": [
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(pack / "data"),
            "--parent-pid",
            str(os.getpid()),
        ],
        "env": {
            "HF_HUB_OFFLINE": "1",
            "VOICEBOX_MODELS_DIR": str(pack / "models"),
            "HF_HOME": str(pack / "hf"),
        },
    }


async def test_a_cancelled_caller_leaves_the_shared_start_running(tmp_path: Path) -> None:
    server, spawns = make(tmp_path)
    try:
        first = asyncio.ensure_future(server.address())
        await spawned(spawns)
        assert server.status() == {"state": "starting", "error": None}
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert server.status()["state"] == "starting"
        await server.address()
        assert len(spawns.children) == 1
    finally:
        await server.close()


async def test_ready_needs_a_healthy_answer_and_port_ownership(tmp_path: Path) -> None:
    asked: list[int] = []

    def foreign(child: winproc.Child, port: int) -> bool:
        asked.append(port)
        return False

    server, spawns = make(tmp_path, owns_port=foreign)
    try:
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "stopped", "error": "port_taken"}
        assert len(asked) == 1  # asked once /health answered healthy
        assert spawns.all_reaped()
    finally:
        await server.close()


@windows_only
async def test_owns_port_holds_for_the_server_tree_only(tmp_path: Path) -> None:
    server, spawns = make(tmp_path)  # the real ownership check
    try:
        _, port = await server.address()
        [child] = spawns.children
        assert voice_pack_server.owns_port(child, port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            assert not voice_pack_server.owns_port(child, sock.getsockname()[1])  # foreign
        assert not voice_pack_server.owns_port(child, voice_pack_server.free_port())  # none
    finally:
        await server.close()


@windows_only
async def test_a_foreign_ipv6_listener_on_the_same_port_is_not_owned(tmp_path: Path) -> None:
    """owns_port pins `all` over `any`: tcp_listeners(port) reports a listener on any local
    address, IPv4 or IPv6, so a foreign process sharing the port on the pack server's own
    [::1] is enough to refuse ownership, even though 127.0.0.1 is the server's alone."""
    port = voice_pack_server.free_port()
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as foreign:
        foreign.bind(("::1", port))
        foreign.listen()
        server, spawns = make(tmp_path, pick_port=lambda: port)
        try:
            await refused(server.address(), "voicebox_pack_failed")
            assert server.status() == {"state": "stopped", "error": "port_taken"}
        finally:
            await server.close()
        assert spawns.all_reaped()


# --- crashes ------------------------------------------------------------------------------------


async def test_a_ready_timeout_counts_as_a_crash(tmp_path: Path) -> None:
    asked: list[int] = []

    def owns(child: winproc.Child, port: int) -> bool:
        asked.append(port)
        return True

    requests = Requests()
    server, spawns = make(
        tmp_path, "never_ready", owns_port=owns, request=requests, ready_timeout_s=0.4
    )
    try:
        # Only a 200 whose JSON status is "healthy" is ready, so neither answer ever is.
        requests.answers = {("GET", "/health"): (200, b'{"status": "starting"}')}
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "stopped", "error": "start_timeout"}
        requests.answers = {("GET", "/health"): (503, b'{"detail": "starting"}')}
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "failed", "error": "start_timeout"}
        assert asked == []  # ownership is asked only after a healthy answer
        assert len(spawns.children) == 2 and spawns.all_reaped()
    finally:
        await server.close()


async def test_a_second_crash_fails_until_reset(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, "exit_at_start")
    try:
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "stopped", "error": "crashed"}
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "failed", "error": "crashed"}
        await refused(server.address(), "voicebox_pack_failed")
        assert len(spawns.children) == 2  # failed: no third start
        assert spawns.all_reaped()

        server.reset()
        assert server.status() == {"state": "stopped", "error": None}
        spawns.mode = "ok"
        await server.address()
        assert server.status() == {"state": "ready", "error": None}
    finally:
        await server.close()


async def test_idle_stop_resets_the_crash_count(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, "exit_at_start", idle_stop_s=0.2)
    try:
        # One crash: below the failed threshold of 2.
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "stopped", "error": "crashed"}

        # A clean run that goes quiet and stops on its own forgives that old crash.
        spawns.mode = "ok"
        await server.address()
        assert server.status() == {"state": "ready", "error": None}
        await left_ready(server)
        assert server.status() == {"state": "stopped", "error": None}

        # A second crash must not reach "failed": the count was reset by the idle stop.
        spawns.mode = "exit_at_start"
        await refused(server.address(), "voicebox_pack_failed")
        assert server.status() == {"state": "stopped", "error": "crashed"}
    finally:
        await server.close()


async def test_a_crash_cancels_a_pending_idle_timer_so_it_cannot_later_erase_it(
    tmp_path: Path,
) -> None:
    """An idle timer armed during a ready run must not outlive a crash of that same run: if it
    fired later anyway, the new idle-stop reset would wrongly forgive this crash too."""
    server, spawns = make(tmp_path, "crash_after_ready", idle_stop_s=1.0)
    try:
        await server.address()
        assert server.status() == {"state": "ready", "error": None}
        await left_ready(server)  # the fake exits on its own after ~0.3 s: a real crash
        assert server.status() == {"state": "stopped", "error": "crashed"}

        await asyncio.sleep(1.2)  # past the 1.0 s the stale timer, if still armed, would fire at
        assert server.status() == {"state": "stopped", "error": "crashed"}
    finally:
        await server.close()


async def test_an_exit_after_ready_is_a_crash_that_clears_the_profiles(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, "crash_after_ready")
    try:
        await server.address()
        before = await server.profile("en", "Ryan")  # well inside the fake's 0.3 s
        await left_ready(server)
        assert server.status() == {"state": "stopped", "error": "crashed"}
        spawns.mode = "ok"
        after = await server.profile("en", "Ryan")  # a new run: looked up again, not cached
        assert after != before
        assert len(spawns.children) == 2
        assert spawns.children[0].active_at_release == 0  # reaped before the next start
        assert server.status() == {"state": "ready", "error": None}
    finally:
        await server.close()


# --- stopping -----------------------------------------------------------------------------------


async def test_idle_stop_ends_the_server_without_counting_a_crash(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, idle_stop_s=0.2)  # shorter than a start: the start still ends
    try:
        for _ in range(3):  # two idle stops would fail the server if they counted as crashes
            await server.address()
            assert server.status() == {"state": "ready", "error": None}
            await left_ready(server)
            assert server.status() == {"state": "stopped", "error": None}
        assert len(spawns.children) == 3
    finally:
        await server.close()
    assert spawns.all_reaped()


async def test_close_leaves_no_process_and_refuses_later_starts(tmp_path: Path) -> None:
    server, spawns = make(tmp_path)
    try:
        _, port = await server.address()
        [child] = spawns.children
        tree = {child.pid, *(winproc.tcp_listeners(port) if sys.platform == "win32" else ())}
        await server.close()
        assert spawns.all_reaped()
        await gone(tree)
        await refused(server.address(), "voicebox_pack_failed")
        await refused(server.profile("en", "Ryan"), "voicebox_pack_failed")
        assert len(spawns.children) == 1
        assert server.status() == {"state": "stopped", "error": None}
    finally:
        await server.close()


async def test_close_cancels_a_start_and_leaves_no_process(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, "never_ready")
    try:
        caller = asyncio.ensure_future(server.address())
        await spawned(spawns)
        await server.close()
        await refused(caller, "voicebox_pack_failed")
        assert spawns.all_reaped()
        await gone([spawns.children[0].pid])
        assert server.status() == {"state": "stopped", "error": None}  # no crash counted
    finally:
        await server.close()


# --- profiles -----------------------------------------------------------------------------------


async def test_profile_is_created_once_per_run_and_a_matching_one_is_reused(
    tmp_path: Path,
) -> None:
    requests = Requests()
    server, _ = make(tmp_path, request=requests)
    try:
        ja = await server.profile("ja", "Ono_Anna")
        assert await server.profile("ja", "Ono_Anna") == ja  # cached for this run
        lookups = [request for request in requests.calls if request[1] == "/profiles"]
        assert lookups == [("GET", "/profiles"), ("POST", "/profiles")]
        _, port = await server.address()
        status, listed = await call(port, "GET", "/profiles")
        expected = {
            "name": "VIKI ja Ono_Anna",
            "language": "ja",
            **PRESET,
            "preset_voice_id": "Ono_Anna",
        }
        assert (status, json.loads(listed)) == (200, [{"id": ja, **expected}])
        speech = {"profile_id": ja, "text": "hi", "language": "ja", "engine": None}
        status, wav = await call(port, "POST", "/generate/stream", speech)
        assert status == 200 and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"

        await server.stop()
        _, port = await server.address()  # a new run of the fake: no profiles yet
        en = {"name": "VIKI en Ryan", "language": "en", **PRESET, "preset_voice_id": "Ryan"}
        _, created = await call(port, "POST", "/profiles", en)
        await call(port, "POST", "/profiles", {**en, "name": "Ryan"})  # not VIKI's name
        assert await server.profile("en", "Ryan") == json.loads(created)["id"]
        _, listed = await call(port, "GET", "/profiles")
        assert len(json.loads(listed)) == 2  # reused: nothing created
    finally:
        await server.close()


async def test_each_voice_gets_its_own_profile_in_one_run(tmp_path: Path) -> None:
    """The cache key is (language, voice): a second voice for the same language gets a profile of
    its own, and a Kokoro voice is created with preset_engine "kokoro"."""
    server, _ = make(tmp_path)
    try:
        ryan = await server.profile("en", "Ryan")
        bella = await server.profile("en", "af_bella")
        assert ryan != bella
        assert await server.profile("en", "Ryan") == ryan  # each cached separately
        assert await server.profile("en", "af_bella") == bella

        _, port = await server.address()
        _, listed = await call(port, "GET", "/profiles")
        items = {item["id"]: item for item in json.loads(listed)}
        assert len(items) == 2
        assert items[ryan] == {
            "id": ryan,
            "name": "VIKI en Ryan",
            "language": "en",
            **PRESET,
            "preset_voice_id": "Ryan",
        }
        assert items[bella] == {
            "id": bella,
            "name": "VIKI en af_bella",
            "language": "en",
            **KOKORO_PRESET,
            "preset_voice_id": "af_bella",
        }
    finally:
        await server.close()


async def test_profile_refuses_before_any_spawn_for_an_invalid_language_or_voice(
    tmp_path: Path,
) -> None:
    server, spawns = make(tmp_path)
    try:
        # Voicebox speaks "he", the pack does not.
        await refused(server.profile("he", "Ryan"), "voicebox_profile_missing")
        # af_bella is a Kokoro voice pinned to "en", not "ja".
        await refused(server.profile("ja", "af_bella"), "voicebox_profile_missing")
        # Neither a Qwen3 preset nor a Kokoro voice at all.
        await refused(server.profile("en", "Nobody"), "voicebox_profile_missing")
        assert spawns.children == []
    finally:
        await server.close()


async def test_profile_refusal_codes(tmp_path: Path) -> None:
    requests = Requests()
    server, spawns = make(tmp_path, request=requests)
    try:
        get, post = ("GET", "/profiles"), ("POST", "/profiles")
        upper_id = json.dumps({"id": str(uuid.uuid4()).upper()}).encode()
        cases: list[tuple[tuple[str, str], tuple[int, bytes] | VoiceboxError, str]] = [
            (get, (500, b'{"detail": "boom"}'), "voicebox_refused"),
            (get, VoiceboxError("voicebox_unreachable"), "voicebox_pack_failed"),
            (get, VoiceboxError("voicebox_timeout"), "voicebox_pack_failed"),
            (get, (200, b'{"not": "a list"}'), "voicebox_invalid"),
            (post, (400, b'{"detail": "taken"}'), "voicebox_refused"),
            (post, VoiceboxError("voicebox_invalid"), "voicebox_pack_failed"),
            (post, (200, b'{"id": "not-a-uuid"}'), "voicebox_invalid"),
            (post, (200, upper_id), "voicebox_invalid"),
        ]
        for key, answer, code in cases:
            requests.answers = {key: answer}
            await refused(server.profile("en", "Ryan"), code)
        requests.answers = {}
        _, port = await server.address()
        await call(
            port, "POST", "/profiles", {"name": "VIKI ko Sohee", **PRESET, "preset_voice_id": "x"}
        )
        refusal = server.profile("ko", "Sohee")
        await refused(refusal, "voicebox_refused")  # its name is taken by a mismatch
        assert server.status() == {"state": "ready", "error": None}  # a refusal is no crash
        assert len(spawns.children) == 1
    finally:
        await server.close()


async def test_run_counts_successful_starts_only(tmp_path: Path) -> None:
    server, spawns = make(tmp_path, "exit_at_start")
    try:
        assert server.run() == 0
        await refused(server.address(), "voicebox_pack_failed")  # a crash: no successful start
        assert server.run() == 0

        spawns.mode = "ok"
        await server.address()
        assert server.run() == 1
        await server.address()  # already ready: still one start
        assert server.run() == 1

        await server.stop()
        await server.address()
        assert server.run() == 2
    finally:
        await server.close()
