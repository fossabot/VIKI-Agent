"""VoicePack against fake installer and server objects: the status merge, the one lock serializing
every action, and the speech path staying outside it."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from viki_agent.voice import voice_pack
from viki_agent.voice.voice_pack_install import PackRefused
from viki_agent.voice.voice_pack_service import VoicePack
from viki_agent.voice.voicebox_http import VoiceboxError

HANG_GUARD_S = 10.0
_STATUS_KEYS = {
    "state",
    "reason",
    "error",
    "progress",
    "download_bytes",
    "disk_bytes",
    "need_bytes",
    "free_bytes",
    "gpu",
    "kokoro",
    "speed",
    "fallbacks",
}


class FakeInstaller:
    """Mimics PackInstaller.install(): it reads its own state, yields once, and only then records
    the spawn — the real race a missing lock would let two callers both win."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.state = "not_installed"
        self.error: str | None = None
        self.reason: str | None = None
        self.progress: dict[str, Any] | None = None
        self.gpu: dict[str, Any] | None = None
        self.spawns = 0
        self.closed = False
        self.started = asyncio.Event()
        self.check_hold: asyncio.Event | None = None  # set to pause check() mid-flight
        self.check_entered = asyncio.Event()

    async def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "error": self.error,
            "progress": self.progress,
            "gpu": self.gpu,
        }

    async def check(self, *, again: bool = False) -> None:
        self.order.append(f"installer.check({again})")
        if self.check_hold is not None:
            self.check_entered.set()
            await self.check_hold.wait()

    async def install(self, *, repair: bool = False) -> None:
        self.order.append(f"installer.install(repair={repair})")
        self.started.set()
        state = self.state
        await asyncio.sleep(0)  # the real install() checks state, awaits, then spawns
        if state in ("installing", "removing"):
            raise PackRefused("pack_busy")
        self.spawns += 1
        self.state = "installing"

    async def cancel(self) -> None:
        self.order.append("installer.cancel")

    async def remove(self) -> None:
        self.order.append("installer.remove")

    async def close(self) -> None:
        self.order.append("installer.close")
        self.closed = True


class FakeServer:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.state = "stopped"
        self.error: str | None = None
        self.addr = ("127.0.0.1", 4000)
        self.closed = False
        self.stop_hold: asyncio.Event | None = None  # set to pause stop() mid-flight
        self.stop_entered = asyncio.Event()
        self.run_count = 1  # the current server run; note_speed keys its first call to this

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error}

    async def address(self) -> tuple[str, int]:
        self.order.append("server.address")
        return self.addr

    async def profile(self, language: str, voice: str) -> str:
        self.order.append(f"server.profile({language}, {voice})")
        return f"profile-{language}-{voice}"

    def run(self) -> int:
        return self.run_count

    def reset(self) -> None:
        self.order.append("server.reset")
        self.state, self.error = "stopped", None

    async def stop(self) -> None:
        self.order.append("server.stop")
        self.state = "stopped"
        if self.stop_hold is not None:
            self.stop_entered.set()
            await self.stop_hold.wait()

    async def close(self) -> None:
        self.order.append("server.close")
        self.closed = True


def make(tmp_path: Path) -> tuple[VoicePack, FakeInstaller, FakeServer]:
    order: list[str] = []
    installer, server = FakeInstaller(order), FakeServer(order)
    return VoicePack(tmp_path, installer, server), installer, server


# --- status ---------------------------------------------------------------------------------


async def test_status_key_set_is_exact(tmp_path: Path) -> None:
    pack, _, _ = make(tmp_path)
    assert set(await pack.status()) == _STATUS_KEYS


async def test_status_uses_server_state_and_error_when_installed(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    server.state, server.error = "ready", None
    status = await pack.status()
    assert (status["state"], status["error"]) == ("ready", None)

    server.state, server.error = "failed", "start_timeout"
    status = await pack.status()
    assert (status["state"], status["error"]) == ("failed", "start_timeout")


async def test_status_uses_installer_state_and_error_when_not_installed(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    server.state, server.error = "ready", None  # must be ignored outside "installed"

    installer.state, installer.error = "install_failed", "hash_mismatch"
    status = await pack.status()
    assert (status["state"], status["error"]) == ("install_failed", "hash_mismatch")

    installer.state, installer.error = "installing", None
    status = await pack.status()
    assert (status["state"], status["error"]) == ("installing", None)


async def test_status_carries_reason_progress_and_gpu_from_the_installer(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    installer.state = "unsupported"
    installer.reason = "low_vram"
    installer.progress = {"step": "model", "done": 1, "total": 2}
    installer.gpu = {"name": "x", "memory_mib": 1024, "driver": "570.65"}
    status = await pack.status()
    assert status["reason"] == "low_vram"
    assert status["progress"] == {"step": "model", "done": 1, "total": 2}
    assert status["gpu"] == {"name": "x", "memory_mib": 1024, "driver": "570.65"}


async def test_status_byte_constants_and_free_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, _, _ = make(tmp_path)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(voice_pack.shutil, "disk_usage", lambda _path: usage)
    status = await pack.status()
    assert status["download_bytes"] == voice_pack.DOWNLOAD_BYTES
    assert status["disk_bytes"] == voice_pack.DISK_BYTES
    assert status["need_bytes"] == voice_pack.NEED_BYTES
    # A fixed free count, since the real one moves while other processes write.
    assert status["free_bytes"] == voice_pack.free_bytes(tmp_path)


async def test_status_never_spawns_anything(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    for state in ("not_installed", "installed", "install_failed", "checking"):
        installer.state = state
        await pack.status()
    assert installer.order == []
    assert server.order == []


# --- actions ------------------------------------------------------------------------------------


async def test_check_calls_installer_check_again(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.check()
    assert installer.order == ["installer.check(True)"]


async def test_install_calls_install_then_resets_the_server(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    status = await pack.install()
    assert installer.order == ["installer.install(repair=False)", "server.reset"]
    assert status["state"] == "installing"  # the server was reset back to stopped meanwhile


async def test_repair_stops_resets_then_installs_with_repair(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.repair()
    assert installer.order == ["server.stop", "server.reset", "installer.install(repair=True)"]


async def test_cancel_calls_installer_cancel(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.cancel()
    assert installer.order == ["installer.cancel"]


async def test_remove_stops_the_server_first(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.remove()
    assert installer.order == ["server.stop", "installer.remove", "server.reset"]


async def test_retry_resets_the_server(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.retry()
    assert installer.order == ["server.reset"]


async def test_install_refusal_propagates_and_releases_the_lock(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    installer.state = "installing"  # another install already under way
    with pytest.raises(PackRefused) as caught:
        await pack.install()
    assert caught.value.code == "pack_busy"
    assert installer.order == ["installer.install(repair=False)"]  # no server.reset after a refusal
    await pack.cancel()  # the lock was released despite the exception
    assert installer.order[-1] == "installer.cancel"


async def test_two_concurrent_installs_spawn_once(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    async with asyncio.timeout(HANG_GUARD_S):
        results = await asyncio.gather(pack.install(), pack.install(), return_exceptions=True)
    refused = [r for r in results if isinstance(r, PackRefused)]
    ok = [r for r in results if isinstance(r, dict)]
    assert installer.spawns == 1
    assert len(refused) == 1 and refused[0].code == "pack_busy"
    assert len(ok) == 1


# --- speech, outside the action lock -------------------------------------------------------------


async def test_address_is_pack_missing_while_installing(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installing"
    with pytest.raises(VoiceboxError) as caught:
        await pack.address()
    assert caught.value.code == "voicebox_pack_missing"
    assert server.order == []  # the server was never asked


async def test_address_returns_the_server_address_when_installed(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    assert await pack.address() == server.addr
    assert server.order == ["server.address"]


async def test_profile_delegates_to_the_server(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    assert await pack.profile("ja", "Ono_Anna") == "profile-ja-Ono_Anna"
    assert server.order == ["server.profile(ja, Ono_Anna)"]


async def test_profile_is_pack_missing_while_not_installed(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "not_installed"
    with pytest.raises(VoiceboxError) as caught:
        await pack.profile("ja", "Ono_Anna")
    assert caught.value.code == "voicebox_pack_missing"
    assert server.order == []  # the server was never asked


async def test_address_still_works_after_a_failed_add_over_an_installed_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed or cancelled "Add the fast voice" leaves the installer at "install_failed", but
    the previously installed pack's files are still on disk, so speech must keep working."""
    pack, installer, server = make(tmp_path)
    installer.state, installer.error = "install_failed", "network"
    monkeypatch.setattr(voice_pack, "installed", lambda data_dir: True)
    assert await pack.address() == server.addr
    assert server.order == ["server.address"]


async def test_address_still_refuses_after_a_failed_fresh_install(tmp_path: Path) -> None:
    """Unlike a failed add over a working pack, a failed fresh install (or failed repair) has no
    complete pack on disk, so `voice_pack.installed()` is false here and it still refuses."""
    pack, installer, _ = make(tmp_path)
    installer.state, installer.error = "install_failed", "network"
    with pytest.raises(VoiceboxError) as caught:
        await pack.address()
    assert caught.value.code == "voicebox_pack_missing"


@pytest.mark.parametrize("action", ["remove", "repair"])
async def test_address_is_pack_missing_while_held(tmp_path: Path, action: str) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    server.stop_hold = asyncio.Event()
    task = asyncio.ensure_future(getattr(pack, action)())
    async with asyncio.timeout(HANG_GUARD_S):
        await server.stop_entered.wait()

    with pytest.raises(VoiceboxError) as caught:
        await pack.address()
    assert caught.value.code == "voicebox_pack_missing"
    assert server.order == ["server.stop"]  # held: no address() reached the server

    server.stop_hold.set()
    await task


@pytest.mark.parametrize("action", ["remove", "repair"])
async def test_profile_is_pack_missing_while_held(tmp_path: Path, action: str) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    server.stop_hold = asyncio.Event()
    task = asyncio.ensure_future(getattr(pack, action)())
    async with asyncio.timeout(HANG_GUARD_S):
        await server.stop_entered.wait()

    with pytest.raises(VoiceboxError) as caught:
        await pack.profile("ja", "Ono_Anna")
    assert caught.value.code == "voicebox_pack_missing"
    assert server.order == ["server.stop"]  # held: profile() never reached the server either

    server.stop_hold.set()
    await task


@pytest.mark.parametrize("action", ["remove", "repair"])
async def test_profile_is_pack_missing_when_held_during_the_kokoro_readiness_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """profile()'s own `_held` check runs before the Kokoro-readiness thread hop; remove() or
    repair() can take the hold during that hop, so profile() must re-check `_held` after it too,
    or it would reach the server for a pack mid remove/repair."""
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    entered = threading.Event()
    gate = threading.Event()

    def slow_kokoro_installed(data_dir: Path) -> bool:
        entered.set()
        gate.wait(HANG_GUARD_S)
        return True

    monkeypatch.setattr(voice_pack, "kokoro_installed", slow_kokoro_installed)
    server.stop_hold = asyncio.Event()

    profiling = asyncio.ensure_future(pack.profile("en", "af_bella"))
    await asyncio.to_thread(entered.wait, HANG_GUARD_S)  # profile() sits in kokoro_ready()'s hop

    task = asyncio.ensure_future(getattr(pack, action)())
    async with asyncio.timeout(HANG_GUARD_S):
        await server.stop_entered.wait()  # action() has set _held and is stopping the server
    gate.set()  # let kokoro_ready() return True while the pack is still held

    with pytest.raises(VoiceboxError) as caught:
        await asyncio.wait_for(profiling, HANG_GUARD_S)
    assert caught.value.code == "voicebox_pack_missing"
    assert server.order == ["server.stop"]  # profile() never reached the server

    server.stop_hold.set()
    await task


async def test_check_does_not_block_install_or_cancel(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    installer.check_hold = asyncio.Event()
    checking = asyncio.ensure_future(pack.check())
    async with asyncio.timeout(HANG_GUARD_S):
        await installer.check_entered.wait()

    # Neither action waits on the in-flight check(): both run to completion right away, while
    # check() is still blocked holding nothing that either one needs.
    await asyncio.wait_for(pack.install(), HANG_GUARD_S)
    await asyncio.wait_for(pack.cancel(), HANG_GUARD_S)
    assert not checking.done()
    assert installer.order[0] == "installer.check(True)"
    assert "installer.install(repair=False)" in installer.order
    assert "installer.cancel" in installer.order

    installer.check_hold.set()
    await checking


async def test_close_closes_the_server_then_the_installer(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    await pack.close()
    assert installer.order == ["server.close", "installer.close"]
    assert installer.closed and server.closed


@pytest.mark.parametrize("action", ["check", "install", "repair", "cancel", "remove", "retry"])
async def test_closed_pack_refuses_later_actions(tmp_path: Path, action: str) -> None:
    pack, installer, _ = make(tmp_path)
    await pack.close()
    before = list(installer.order)
    with pytest.raises(PackRefused) as caught:
        await getattr(pack, action)()
    assert caught.value.code == "pack_busy"
    assert installer.order == before


async def test_close_refuses_an_install_already_waiting_for_the_action_lock(tmp_path: Path) -> None:
    pack, installer, _ = make(tmp_path)
    entered = asyncio.Event()

    async def install() -> dict[str, Any]:
        entered.set()
        return await pack.install()

    async with pack._lock:
        installing = asyncio.create_task(install())
        await entered.wait()
        await pack.close()
    with pytest.raises(PackRefused) as caught:
        await installing
    assert caught.value.code == "pack_busy"
    assert installer.order == ["server.close", "installer.close"]


@pytest.mark.parametrize("action", ["address", "profile"])
async def test_closed_pack_refuses_speech(tmp_path: Path, action: str) -> None:
    pack, installer, _ = make(tmp_path)
    installer.state = "installed"
    await pack.close()
    args = ["ja", "Ono_Anna"] if action == "profile" else []
    with pytest.raises(VoiceboxError) as caught:
        await getattr(pack, action)(*args)
    assert caught.value.code == "voicebox_pack_failed"
    assert installer.order == ["server.close", "installer.close"]


async def test_close_still_closes_the_installer_when_server_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, installer, server = make(tmp_path)

    async def fail() -> None:
        raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(server, "close", fail)
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await pack.close()
    assert installer.closed


# --- kokoro readiness, speed and fallbacks -------------------------------------------------


async def test_status_kokoro_speed_and_fallbacks_default_empty(tmp_path: Path) -> None:
    pack, _, _ = make(tmp_path)
    status = await pack.status()
    assert status["kokoro"] is False  # no files on a fresh tmp_path
    assert status["speed"] == {"kokoro": None, "qwen": None}
    assert status["fallbacks"] == []


async def test_kokoro_ready_reports_whether_kokoro_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, _, _ = make(tmp_path)
    assert await pack.kokoro_ready() is False
    monkeypatch.setattr(voice_pack, "kokoro_installed", lambda data_dir: True)
    assert await pack.kokoro_ready() is True


async def test_a_kokoro_voice_without_files_is_model_missing(tmp_path: Path) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    with pytest.raises(VoiceboxError) as caught:
        await pack.profile("en", "af_bella")
    assert caught.value.code == "voicebox_model_missing"
    assert server.order == []  # refused before any start


async def test_a_kokoro_voice_with_files_delegates_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, installer, server = make(tmp_path)
    installer.state = "installed"
    monkeypatch.setattr(voice_pack, "kokoro_installed", lambda data_dir: True)
    assert await pack.profile("en", "af_bella") == "profile-en-af_bella"
    assert server.order == ["server.profile(en, af_bella)"]


async def test_note_speed_skips_each_runs_first_synthesis(tmp_path: Path) -> None:
    pack, _, server = make(tmp_path)
    server.run_count = 1
    pack.note_speed("kokoro", 0.05)  # first call this run: dropped (model load)
    assert (await pack.status())["speed"] == {"kokoro": None, "qwen": None}
    pack.note_speed("kokoro", 0.0253)
    assert (await pack.status())["speed"]["kokoro"] == 0.025
    pack.note_speed("kokoro", 0.030)
    assert (await pack.status())["speed"]["kokoro"] == 0.03

    server.run_count = 2  # a fresh server run: the next sample for this model drops again
    pack.note_speed("kokoro", 4.0)
    assert (await pack.status())["speed"]["kokoro"] == 0.03  # unchanged: dropped
    pack.note_speed("kokoro", 4.2)
    assert (await pack.status())["speed"]["kokoro"] == 4.2
    assert (await pack.status())["speed"]["qwen"] is None  # a different model: untouched


async def test_note_fallback_keeps_the_last_three_newest_first(tmp_path: Path) -> None:
    pack, _, _ = make(tmp_path)
    for code in ["voicebox_timeout", "voicebox_busy", "voicebox_off", "voicebox_refused"]:
        pack.note_fallback(code)
    status = await pack.status()
    assert [item["code"] for item in status["fallbacks"]] == [
        "voicebox_refused",
        "voicebox_off",
        "voicebox_busy",
    ]
    assert all(item["at"].endswith("Z") and "T" in item["at"] for item in status["fallbacks"])


# --- create ---------------------------------------------------------------------------------


async def test_create_wires_the_real_installer_and_server(tmp_path: Path) -> None:
    pack = VoicePack.create(tmp_path)
    try:
        assert set(await pack.status()) == _STATUS_KEYS
    finally:
        await pack.close()
