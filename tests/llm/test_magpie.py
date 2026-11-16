from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest

from viki_agent.llm import magpie
from viki_agent.llm.connections import ConnectionError, parse_connection
from viki_agent.llm.gateway import Gateway

HANG_GUARD_S = 5  # on waits that end in milliseconds unless the code under test hangs
READY = magpie.Magpie._ready  # the real readiness wait, which `rig` replaces


def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    binary = directory / ("magpie.exe" if sys.platform == "win32" else "magpie")
    binary.write_bytes(b"synthetic reviewed executable; never executed")
    machine = platform.machine().lower()
    manifest = {
        "version": magpie.VERSION,
        "source_commit": magpie.SOURCE_COMMIT,
        "platform": sys.platform,
        "arch": "arm64" if machine in ("arm64", "aarch64") else "x64",
        "binary": binary.name,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(
        magpie,
        "PINNED",
        {f"{sys.platform}/{manifest['arch']}": (manifest["sha256"], binary.stat().st_size)},
    )
    binary.with_name("magpie.json").write_text(json.dumps(manifest), encoding="utf-8")
    return binary


WRITE_RECORD = magpie._write_record


class Child:
    pid = 4242

    def __init__(self) -> None:
        self.process = self
        self.returncode: int | None = None
        self.done = asyncio.Event()
        self.killed = self.released = 0

    async def wait(self) -> int:
        await self.done.wait()
        return self.returncode or 0

    async def wait_tree(self, poll_s: float = 0.5) -> None:
        await self.done.wait()

    def kill_tree(self) -> None:
        self.killed += 1
        self.returncode = 1
        self.done.set()

    def release(self) -> None:
        self.released += 1


def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    binary = bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "bundle_path", lambda: binary)
    monkeypatch.setattr(magpie, "_ports_free", lambda: None)
    monkeypatch.setattr(magpie.protection, "create_private_directory", lambda p: p.mkdir())
    monkeypatch.setattr(magpie.protection, "require_private_directory", lambda p: None)
    children: list[Child] = []
    launches: list[dict[str, Any]] = []

    async def spawn(argv, **kwargs):
        launches.append({"argv": argv, **kwargs})
        child = Child()
        children.append(child)
        return child

    async def ready(self, child):
        pass

    monkeypatch.setattr(magpie.winproc, "spawn", spawn)
    monkeypatch.setattr(magpie.Magpie, "_ready", ready)
    # No real process has the stand-in's pid, so on a macOS or Linux host its record could
    # never be written. The tests about the record take those paths through `linux()`.
    monkeypatch.setattr(magpie, "_write_record", lambda record, pid: None)
    return magpie.Magpie(tmp_path), children, launches


async def test_status_verifies_bundle_without_starting_or_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "bundle_path", lambda: binary)
    service = magpie.Magpie(tmp_path)
    assert await service.status() == {
        "available": True,
        "running": False,
        "version": magpie.VERSION,
        "desktop": False,
        "base_url": magpie.BASE_URL,
    }
    assert not service.root.exists()
    binary.write_bytes(b"changed")
    assert (await service.status())["available"] is False
    with pytest.raises(magpie.MagpieError, match="missing_bundle"):
        await service.start()
    assert not service.root.exists()


def test_manifest_cannot_repin_a_changed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = bundle(tmp_path, monkeypatch)
    manifest = binary.with_name("magpie.json")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    binary.write_bytes(b"changed")
    value["sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(magpie.MagpieError, match="missing_bundle"):
        magpie.verified_bundle(binary)


def test_runtime_release_pins_match_build_inputs() -> None:
    path = Path(__file__).resolve().parents[2] / "desktop" / "build" / "magpie-release.json"
    release = json.loads(path.read_text(encoding="utf-8"))
    assert (release["version"], release["source_commit"]) == (magpie.VERSION, magpie.SOURCE_COMMIT)
    assert {
        name: (asset["sha256"], asset["size"]) for name, asset in release["assets"].items()
    } == magpie.PINNED


def compressed_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    binary = bundle(tmp_path, monkeypatch)
    payload = binary.read_bytes()
    source = binary.with_name("magpie.gz")
    source.write_bytes(gzip.compress(payload, mtime=0))
    binary.unlink()
    monkeypatch.setattr(magpie.compiled, "COMPILED", True)
    monkeypatch.setattr(magpie, "bundle_path", lambda: source)
    return source, payload


async def test_compressed_status_does_not_extract_or_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = compressed_bundle(tmp_path, monkeypatch)
    service = magpie.Magpie(tmp_path)
    assert (await service.status())["available"] is True
    assert magpie.verified_bundle(source) == source
    assert not service.root.exists()


def test_installed_payload_materializes_create_only_and_refuses_modified_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, payload = compressed_bundle(tmp_path, monkeypatch)
    root = tmp_path / "private"
    magpie._private_directory(root)
    target = magpie.materialize(source, root)
    assert target.read_bytes() == payload
    assert target.parent.name == f"{magpie.VERSION}-{sys.platform}-{magpie._arch()}"
    magpie.protection.require_private_file(target)
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o700
    original = target.stat()
    assert magpie.materialize(source, root) == target
    assert target.stat().st_mtime_ns == original.st_mtime_ns
    target.write_bytes(b"changed")
    with pytest.raises(magpie.MagpieError, match="runtime_unavailable"):
        magpie.materialize(source, root)
    assert target.read_bytes() == b"changed"


@pytest.mark.parametrize(
    "replacement", [None, b"changed payload", b"", b"larger than the official payload" * 9]
)
def test_installed_compressed_payload_is_bounded_and_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bytes | None
) -> None:
    source, _ = compressed_bundle(tmp_path, monkeypatch)
    source.write_bytes(
        b"broken gzip" if replacement is None else gzip.compress(replacement, mtime=0)
    )
    with pytest.raises(magpie.MagpieError, match="missing_bundle"):
        magpie.verified_bundle(source)
    assert not (tmp_path / "runtime").exists()


def test_runtime_rejects_aliases_and_wrong_installed_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, payload = compressed_bundle(tmp_path, monkeypatch)
    root = tmp_path / "private"
    magpie._private_directory(root)
    target = magpie.materialize(source, root)
    alias = tmp_path / "alias"
    os.link(target, alias)
    with pytest.raises(magpie.MagpieError, match="runtime_unavailable"):
        magpie.materialize(source, root)
    binary = source.with_name("magpie.exe" if sys.platform == "win32" else "magpie")
    binary.write_bytes(payload)
    with pytest.raises(magpie.MagpieError, match="missing_bundle"):
        magpie.verified_bundle(source)


async def test_sidecar_is_on_demand_private_and_owned_until_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    monkeypatch.setenv("MAGPIE_WEB_KEY", "inherited-secret")
    monkeypatch.setenv("MAGPIE_ADDR", "0.0.0.0:1")
    monkeypatch.setenv("MAGPIE_NO_ACCOUNTS", "0")
    monkeypatch.setenv("VIKI_TOKEN", "window-secret")
    await service.start()
    await service.start()
    assert len(launches) == 1 and service.running
    launch = launches[0]
    assert launch["argv"][1:] == ["web", "--addr", "127.0.0.1:3431", "--no-open"]
    assert launch["cwd"] == service.root and launch["pipes"] is False
    env = launch["env"]
    assert env["MAGPIE_ADDR"] == "127.0.0.1:3426"
    assert env["MAGPIE_WEB_KEY"] == service._key
    assert len(service._key) == 64
    assert env["DO_NOT_TRACK"] == env["MAGPIE_NO_STATS"] == "1"
    assert env["MAGPIE_NO_ACCOUNTS"] == "1"
    assert "VIKI_TOKEN" not in env
    assert env["XDG_CONFIG_HOME"] == str(service.root / "config")
    settings = service.root / "config" / "magpie" / "settings.json"
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "noAutoUpdate": True,
        "autoRestart": False,
    }
    await service.hide(None)
    assert service.running
    await service.close()
    assert not service.running and children[0].killed >= 1 and children[0].released == 1


async def test_crashed_process_needs_explicit_open_and_only_exact_route_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})
    form = {
        "name": "Magpie",
        "base_url": "https://models.example/v1",
        "model": "provider/model",
        "api_mode": "anthropic",
        "api_key": "",
    }
    await service.ensure_connection(parse_connection(form))
    assert launches == []
    connection = parse_connection({**form, "base_url": magpie.BASE_URL})
    await service.ensure_connection(connection)
    children[0].kill_tree()
    with pytest.raises(ConnectionError, match="endpoint_unreachable"):
        await service.ensure_connection(connection)
    assert len(launches) == 1
    await service.start(explicit=True)
    assert len(launches) == 2
    await service.close()


async def test_dashboard_validates_window_before_start_and_uses_private_browser_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    opened: list[str] = []
    service.opener = opened.append
    service.host = Mock()
    service.host.has_ui.return_value = False  # type: ignore[attr-defined]
    with pytest.raises(magpie.MagpieError, match="invalid_window"):
        await service.open(None)
    assert launches == []
    service.host = None
    await service.open(None)
    assert len(opened) == 1 and service._key not in opened[0]
    assert service._page is not None
    page = service._page
    assert service._key in page.read_text(encoding="utf-8")
    assert "http://127.0.0.1:3431/?k=" in page.read_text(encoding="utf-8")
    await service.close()
    assert not page.exists() and children[0].released == 1


async def test_cancelled_start_reaps_child_and_the_next_use_starts_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})

    async def ready(self, child):
        if child is children[0]:
            raise asyncio.CancelledError()

    monkeypatch.setattr(magpie.Magpie, "_ready", ready)
    with pytest.raises(asyncio.CancelledError):
        await service.start()
    assert len(launches) == 1
    assert children[0].killed == children[0].released == 1
    assert not service.running
    # The caller went away; Magpie did not crash. The connection's next use starts it.
    await service.start()
    assert len(launches) == 2 and service.running
    await service.close()


async def test_a_request_waiting_behind_a_cancelled_start_starts_magpie_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})
    entered, hold = asyncio.Event(), asyncio.Event()

    async def ready(self, child):
        if child is children[0]:
            entered.set()
            await hold.wait()

    monkeypatch.setattr(magpie.Magpie, "_ready", ready)
    first = asyncio.create_task(service.start())
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    waiting = asyncio.create_task(service.start())
    await asyncio.sleep(0)  # the second caller is now queued on the lock
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    async with asyncio.timeout(HANG_GUARD_S):
        await waiting
    assert len(launches) == 2 and service._child is children[1] and service.running
    assert children[0].killed == children[0].released == 1
    await service.close()


async def test_a_cancel_during_the_cleanup_of_a_failed_start_keeps_the_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    reaping, reaped = asyncio.Event(), asyncio.Event()

    async def failed(self, child):
        async def wait_tree(poll_s: float = 0.5) -> None:
            reaping.set()
            await reaped.wait()

        child.wait_tree = wait_tree
        raise magpie.MagpieError("not_ready")

    monkeypatch.setattr(magpie.Magpie, "_ready", failed)
    starting = asyncio.create_task(service.start())
    async with asyncio.timeout(HANG_GUARD_S):
        await reaping.wait()
    starting.cancel()  # lands while the failed child is still being reaped
    reaped.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert children[0].released == 1
    with pytest.raises(magpie.MagpieError, match="needs_open"):
        await service.start()
    assert len(launches) == 1


def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the module's macOS and Linux paths on any host; process identity is a stand-in."""
    monkeypatch.setattr(magpie, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(magpie, "verified_bundle", lambda path: path)
    monkeypatch.setattr(
        magpie.protection, "create_private_directory", lambda p: p.mkdir(exist_ok=True)
    )
    monkeypatch.setattr(magpie.protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(magpie, "_identity", lambda pid: f"boot/{pid}")
    monkeypatch.setattr(magpie, "_leads_group", lambda pid: False)
    monkeypatch.setattr(magpie, "_write_record", WRITE_RECORD)


class Group:
    """A stand-in process group that ends two polls after it is killed."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.left = 2

    def adopt(self, pid: int) -> None:
        self.calls.append(pid)

    def terminate(self) -> None:
        self.calls.append("terminate")

    def active_processes(self) -> int:
        self.calls.append("poll")
        self.left = max(self.left - 1, 0)
        return self.left

    def close(self) -> None:
        self.calls.append("close")


def leave_record(service: magpie.Magpie, text: str) -> Path:
    service._record.parent.mkdir(parents=True)
    service._record.write_text(text, encoding="utf-8")
    return service._record


async def test_stop_kills_at_once_and_no_later_start_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    await service.start()
    service.stop()  # nothing awaited: the launcher's stop hook runs before the drain
    assert children[0].killed == 1 and not service.running
    with pytest.raises(magpie.MagpieError, match="unavailable"):
        await service.start(explicit=True)
    assert len(launches) == 1
    await service.close()
    assert children[0].released == 1


async def test_stop_while_a_start_prepares_its_folders_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, launches = rig(tmp_path, monkeypatch)

    def environment(root: Path, key: str) -> dict[str, str]:
        service.stop()
        return {}

    monkeypatch.setattr(magpie, "_environment", environment)
    with pytest.raises(magpie.MagpieError, match="unavailable"):
        await service.start(explicit=True)
    assert launches == []


async def test_stop_during_a_spawn_leaves_no_child_and_close_does_not_wait_for_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    spawning, spawned = asyncio.Event(), asyncio.Event()

    async def spawn(argv, **kwargs):
        spawning.set()
        await spawned.wait()
        children.append(Child())
        return children[-1]

    async def never_ready(self, child):
        await asyncio.Event().wait()

    monkeypatch.setattr(magpie.winproc, "spawn", spawn)
    monkeypatch.setattr(magpie.Magpie, "_ready", never_ready)
    starting = asyncio.create_task(service.start())
    async with asyncio.timeout(HANG_GUARD_S):
        await spawning.wait()
    closing = asyncio.create_task(service.close())  # finds no child to kill yet
    await asyncio.sleep(0)
    spawned.set()
    with pytest.raises(magpie.MagpieError, match="unavailable"):
        async with asyncio.timeout(HANG_GUARD_S):
            await starting
    async with asyncio.timeout(HANG_GUARD_S):
        await closing
    assert children[0].killed == children[0].released == 1
    assert service._child is None


async def test_stop_during_readiness_ends_the_wait_as_a_stop_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    polled = asyncio.Event()

    def refuse(request: httpx2.Request) -> httpx2.Response:
        polled.set()
        raise httpx2.ConnectError("not listening yet")

    original = httpx2.AsyncClient
    monkeypatch.setattr(magpie.Magpie, "_ready", READY)
    monkeypatch.setattr(magpie, "POLL_S", 0.001)
    monkeypatch.setattr(
        magpie.httpx2,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx2.MockTransport(refuse), **kwargs),
    )
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        starting = asyncio.create_task(service.start())
        async with asyncio.timeout(HANG_GUARD_S):
            await polled.wait()
        service.stop()
        with pytest.raises(magpie.MagpieError, match="unavailable"):
            async with asyncio.timeout(HANG_GUARD_S):
                await starting
    assert children[0].killed >= 1 and children[0].released == 1
    assert caplog.records == []


async def test_a_started_child_is_recorded_until_it_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    seen: list[object] = []

    async def ready(self, child):
        # Written before readiness: a launch that dies during the wait leaves this child too.
        seen.append(json.loads(service._record.read_text(encoding="utf-8")))

    monkeypatch.setattr(magpie.Magpie, "_ready", ready)
    await service.start()
    assert seen == [{"pid": Child.pid, "started": f"boot/{Child.pid}"}]
    await service.close()
    assert children[0].released == 1 and not service._record.exists()


async def test_a_failed_start_leaves_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)

    async def failed(self, child):
        assert service._record.exists()
        raise magpie.MagpieError("not_ready")

    monkeypatch.setattr(magpie.Magpie, "_ready", failed)
    with pytest.raises(magpie.MagpieError, match="not_ready"):
        await service.start()
    assert children[0].released == 1 and not service._record.exists()


async def test_windows_keeps_no_record_and_reaps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    monkeypatch.setattr(magpie, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(magpie, "_leftover", lambda record: pytest.fail("no leftover on Windows"))
    await service.reap_leftover()
    await service.start()
    assert service.running and not service._record.exists()
    await service.close()


async def test_a_leftover_of_this_app_is_killed_before_the_ports_are_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, children, launches = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    group = Group()
    monkeypatch.setattr(magpie.winproc, "Job", lambda: group)
    monkeypatch.setattr(magpie, "POLL_S", 0.001)
    monkeypatch.setattr(magpie, "_ports_free", lambda: group.calls.append("ports"))
    leave_record(service, json.dumps({"pid": 777, "started": "boot/777"}))
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        await service.start()
    # The ports are probed only once the killed group has ended; then the new child's record
    # takes the place of the leftover's.
    assert group.calls == [777, "terminate", "poll", "poll", "close", "ports"]
    assert [record.getMessage() for record in caplog.records] == [
        "magpie left running by an earlier launch was stopped"
    ]
    assert json.loads(service._record.read_text(encoding="utf-8"))["pid"] == Child.pid
    assert len(launches) == 1
    await service.close()


async def test_a_leftover_that_will_not_end_does_not_hold_the_start_for_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    group = Group()
    group.left = 10**9
    monkeypatch.setattr(magpie.winproc, "Job", lambda: group)
    monkeypatch.setattr(magpie, "POLL_S", 0.001)
    monkeypatch.setattr(magpie, "REAP_S", 0.01)
    leave_record(service, json.dumps({"pid": 777, "started": "boot/777"}))
    async with asyncio.timeout(HANG_GUARD_S):
        await service.reap_leftover()
    assert group.calls[-1] == "close" and not service._record.exists()


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"pid": 777, "started": "boot/778"}),  # the pid went to another process
        json.dumps({"pid": 777, "started": 777}),
        json.dumps({"pid": "777", "started": "boot/777"}),
        json.dumps({"pid": True, "started": "boot/True"}),
        json.dumps({"pid": 1, "started": "boot/1"}),
        json.dumps({"pid": 0, "started": "boot/0"}),
        json.dumps({"pid": -777, "started": "boot/-777"}),
        json.dumps({"pid": 2**31, "started": f"boot/{2**31}"}),
        json.dumps({"pid": 777, "started": "boot/777", "argv": []}),
        '{"pid": 777, "pid": 777, "started": "boot/777"}',
        json.dumps([777, "boot/777"]),
        json.dumps({"pid": 777, "started": "boot/777"}) + " " * 600,
        "not json",
        "",
    ],
)
async def test_a_record_that_does_not_prove_the_same_process_kills_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    monkeypatch.setattr(magpie.winproc, "Job", lambda: pytest.fail("nothing may be killed"))
    record = leave_record(service, text)
    await service.reap_leftover()
    assert not record.exists()  # stale: the next start writes its own


async def test_a_record_names_nothing_once_its_process_is_gone_or_the_file_is_not_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    monkeypatch.setattr(magpie.winproc, "Job", lambda: pytest.fail("nothing may be killed"))
    record = leave_record(service, json.dumps({"pid": 777, "started": "boot/777"}))
    assert magpie._leftover(record.with_name("absent.json")) is None
    monkeypatch.setattr(magpie, "_identity", lambda pid: None)
    assert magpie._leftover(record) is None
    monkeypatch.setattr(magpie, "_identity", lambda pid: f"boot/{pid}")

    def refuse(path: Path) -> None:
        raise magpie.protection.ProtectionError()

    monkeypatch.setattr(magpie.protection, "require_private_file", refuse)
    assert magpie._leftover(record) is None


async def test_a_leftover_whose_identity_cannot_be_read_keeps_its_record_until_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A `ps` past its time limit or an unreadable /proc is not a process that ended. The
    record is all that lets a launch end that Magpie, so it stays, nothing is killed on a guess,
    and a later start ends it once its identity reads again."""
    service, _, launches = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    group = Group()
    monkeypatch.setattr(magpie.winproc, "Job", lambda: group)
    monkeypatch.setattr(magpie, "POLL_S", 0.001)
    monkeypatch.setattr(magpie, "_identity", lambda pid: None)
    monkeypatch.setattr(magpie, "_leads_group", lambda pid: pid == 777)

    def taken() -> None:
        raise magpie.MagpieError("port_in_use")  # the leftover still holds them

    monkeypatch.setattr(magpie, "_ports_free", taken)
    record = leave_record(service, json.dumps({"pid": 777, "started": "boot/777"}))
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        await service.reap_leftover()
        for _ in range(2):  # a chat sends its refused request again
            with pytest.raises(magpie.MagpieError, match="port_in_use"):
                await service.start()
    assert record.exists() and group.calls == [] and launches == []
    assert [r.getMessage() for r in caplog.records] == [
        "magpie left by an earlier launch could not be identified",
        "magpie start refused (port_in_use)",
    ]
    monkeypatch.setattr(magpie, "_identity", lambda pid: f"boot/{pid}")
    monkeypatch.setattr(magpie, "_ports_free", lambda: None)
    await service.start()
    assert group.calls[:2] == [777, "terminate"]
    assert json.loads(record.read_text(encoding="utf-8"))["pid"] == Child.pid
    await service.close()


async def test_a_record_kept_for_an_unidentified_process_gives_way_to_the_next_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ports were free, so whatever runs at the kept pid is not the Magpie holding them:
    the child that starts now is the one the next launch has to find."""
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    monkeypatch.setattr(magpie.winproc, "Job", lambda: pytest.fail("nothing may be killed"))
    monkeypatch.setattr(magpie, "_identity", lambda pid: None if pid == 777 else f"boot/{pid}")
    monkeypatch.setattr(magpie, "_leads_group", lambda pid: pid == 777)
    record = leave_record(service, json.dumps({"pid": 777, "started": "boot/777"}))
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        await service.start()
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "pid": Child.pid,
        "started": f"boot/{Child.pid}",
    }
    assert "magpie process record unavailable" not in caplog.text
    await service.close()


async def test_a_child_whose_identity_cannot_be_read_runs_unrecorded_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    monkeypatch.setattr(magpie, "_identity", lambda pid: None)
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        await service.start()
    assert service.running and not service._record.exists()
    assert [r.getMessage() for r in caplog.records] == ["magpie process record unavailable"]
    await service.close()


@pytest.mark.parametrize(
    ("host", "option"),
    [("linux", "SO_REUSEADDR"), ("darwin", "SO_REUSEADDR"), ("win32", "SO_EXCLUSIVEADDRUSE")],
)
def test_the_port_probe_binds_the_way_magpie_itself_will(
    monkeypatch: pytest.MonkeyPatch, host: str, option: str
) -> None:
    """Off Windows, a Magpie stopped a moment ago leaves its closed connections in TIME_WAIT,
    which refuse a plain bind though nothing listens and Magpie itself would start; on Windows
    the probe must not share a port another program holds. No real port is bound."""
    calls: list[object] = []

    class Probe:
        def __enter__(self) -> Probe:
            return self

        def __exit__(self, *exc: object) -> None:
            calls.append("close")

        def setsockopt(self, level: int, name: str, value: int) -> None:
            calls.append((name, value))

        def bind(self, address: tuple[str, int]) -> None:
            calls.append(address)
            if address[1] == fails_on:
                raise OSError("in use")

    fake = SimpleNamespace(
        AF_INET=2,
        SOCK_STREAM=1,
        SOL_SOCKET=1,
        SO_REUSEADDR="SO_REUSEADDR",
        SO_EXCLUSIVEADDRUSE="SO_EXCLUSIVEADDRUSE",
        socket=lambda family, kind: Probe(),
    )
    monkeypatch.setattr(magpie, "socket", fake)
    monkeypatch.setattr(magpie, "sys", SimpleNamespace(platform=host))
    fails_on = 0
    magpie._ports_free()
    gateway, web = ("127.0.0.1", magpie.GATEWAY_PORT), ("127.0.0.1", magpie.WEB_PORT)
    assert calls == [(option, 1), gateway, (option, 1), web, "close", "close"]
    calls.clear()
    fails_on = magpie.WEB_PORT
    with pytest.raises(magpie.MagpieError, match="port_in_use"):
        magpie._ports_free()
    assert calls == [(option, 1), gateway, (option, 1), web, "close", "close"]


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows Job ends Magpie with its launch")
async def test_a_real_leftover_whose_start_time_cannot_be_read_is_kept_not_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = magpie.Magpie(tmp_path)
    service._record.parent.mkdir(parents=True)
    child = await magpie.winproc.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=os.environ,
        pipes=False,
    )
    try:
        magpie._write_record(service._record, child.pid)
        monkeypatch.setattr(magpie.winproc, "process_start_time", lambda pid: None)
        await service.reap_leftover()
        assert service._record.exists()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await child.process.wait()  # not killed on a guess
        child.kill_tree()
        async with asyncio.timeout(HANG_GUARD_S):
            await child.process.wait()
        await service.reap_leftover()
        assert not service._record.exists()  # nothing runs at that pid any more
    finally:
        child.kill_tree()
        child.release()


async def test_a_running_magpie_of_this_launch_is_never_reaped_as_a_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    linux(monkeypatch)
    await service.start()
    monkeypatch.setattr(magpie.winproc, "Job", lambda: pytest.fail("this launch's own child"))
    await service.reap_leftover()
    assert service.running and children[0].killed == 0 and service._record.exists()
    await service.close()


def test_identity_tells_a_process_from_a_later_one_with_its_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot = tmp_path / "boot_id"
    boot.write_text("11111111-2222\n", encoding="ascii")
    monkeypatch.setattr(magpie, "_BOOT_ID", boot)
    monkeypatch.setattr(magpie.winproc, "process_start_time", lambda pid: "8123")
    monkeypatch.setattr(magpie, "sys", SimpleNamespace(platform="linux"))
    assert magpie._identity(777) == "11111111-2222/8123"
    boot.write_text("33333333-4444\n", encoding="ascii")  # the same tick after a restart
    assert magpie._identity(777) == "33333333-4444/8123"
    boot.unlink()
    assert magpie._identity(777) is None
    monkeypatch.setattr(magpie, "sys", SimpleNamespace(platform="darwin"))
    assert magpie._identity(777) == "8123"
    monkeypatch.setattr(magpie.winproc, "process_start_time", lambda pid: None)
    assert magpie._identity(777) is None


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows Job ends Magpie with its launch")
async def test_a_real_leftover_process_is_killed_only_under_its_own_identity(
    tmp_path: Path,
) -> None:
    service = magpie.Magpie(tmp_path)
    service._record.parent.mkdir(parents=True)
    child = await magpie.winproc.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=os.environ,
        pipes=False,
    )
    try:
        magpie._write_record(service._record, child.pid)
        value = json.loads(service._record.read_text(encoding="utf-8"))
        assert value["pid"] == child.pid
        service._record.write_text(
            json.dumps({**value, "started": value["started"] + "0"}), encoding="utf-8"
        )
        await service.reap_leftover()
        assert not service._record.exists()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await child.process.wait()  # another identity: the process is left alone
        magpie._write_record(service._record, child.pid)
        await service.reap_leftover()
        async with asyncio.timeout(HANG_GUARD_S):
            assert await child.process.wait() != 0
        assert not service._record.exists()
    finally:
        child.kill_tree()
        child.release()


async def test_a_refused_start_is_logged_once_per_cause_by_code_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})
    connection = parse_connection(
        {
            "name": "Magpie",
            "base_url": magpie.BASE_URL,
            "model": "provider/model",
            "api_mode": "anthropic",
            "api_key": "",
        }
    )

    def taken() -> None:
        raise magpie.MagpieError("port_in_use")

    def messages() -> list[str]:
        return [record.getMessage() for record in caplog.records]

    monkeypatch.setattr(magpie, "_ports_free", taken)
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        for _ in range(3):  # a chat sends its refused request again
            with pytest.raises(ConnectionError, match="endpoint_unreachable"):
                await service.ensure_connection(connection)
        assert messages() == ["magpie start refused (port_in_use)"]
        monkeypatch.setattr(magpie, "_ports_free", lambda: None)
        await service.start()
        children[0].kill_tree()
        watcher = service._watcher
        assert watcher is not None
        async with asyncio.timeout(HANG_GUARD_S):
            await watcher
        monkeypatch.setattr(magpie, "_ports_free", taken)
        with pytest.raises(magpie.MagpieError, match="port_in_use"):
            await service.start(explicit=True)
        # A start that worked in between makes the same cause news again.
        assert messages() == [
            "magpie start refused (port_in_use)",
            "magpie exited (1)",
            "magpie start refused (port_in_use)",
        ]
    assert all(record.levelname == "WARNING" for record in caplog.records)
    assert service._key not in caplog.text and "127.0.0.1" not in caplog.text


async def test_each_start_failure_has_its_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)

    def folder(root: Path, key: str) -> dict[str, str]:
        raise magpie.protection.ProtectionError()

    async def spawn(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    caplog.set_level("WARNING", logger="viki_agent.llm.magpie")
    monkeypatch.setattr(magpie, "_environment", folder)
    with pytest.raises(magpie.MagpieError, match="data_unavailable"):
        await service.start(explicit=True)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})
    monkeypatch.setattr(magpie.winproc, "spawn", spawn)
    with pytest.raises(magpie.MagpieError, match="spawn_failed"):
        await service.start(explicit=True)
    # The path the spawn named stays out of the log.
    assert [record.getMessage() for record in caplog.records] == [
        "magpie start refused (data_unavailable)",
        "magpie start refused (spawn_failed)",
    ]
    assert children == []


async def test_readiness_tells_an_exit_from_a_timeout_from_another_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = magpie.Magpie(tmp_path)
    listening = [True]

    def respond(request: httpx2.Request) -> httpx2.Response:
        if listening[0]:
            return httpx2.Response(200)  # a page that never asked for Magpie's key
        raise httpx2.ConnectError("nothing listens")

    original = httpx2.AsyncClient
    monkeypatch.setattr(
        magpie.httpx2,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx2.MockTransport(respond), **kwargs),
    )
    with pytest.raises(magpie.MagpieError, match="unexpected_dashboard"):
        await service._ready(Child())  # type: ignore[arg-type]
    listening[0] = False
    monkeypatch.setattr(magpie, "STARTUP_S", 0.01)
    monkeypatch.setattr(magpie, "POLL_S", 0.001)
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        with pytest.raises(magpie.MagpieError, match="not_ready"):
            await service._ready(Child())  # type: ignore[arg-type]
        assert caplog.records == []
        exited = Child()
        exited.returncode = 3
        with pytest.raises(magpie.MagpieError, match="exited"):
            await service._ready(exited)  # type: ignore[arg-type]
    assert [record.getMessage() for record in caplog.records] == ["magpie exited during start (3)"]


async def test_an_exit_is_logged_with_its_code_unless_the_app_is_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, children, _ = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(magpie, "_environment", lambda root, key: {})
    with caplog.at_level("WARNING", logger="viki_agent.llm.magpie"):
        await service.start()
        children[0].returncode = 7
        children[0].done.set()
        watcher = service._watcher
        assert watcher is not None
        async with asyncio.timeout(HANG_GUARD_S):
            await watcher
        assert [record.getMessage() for record in caplog.records] == ["magpie exited (7)"]
        await service.start(explicit=True)
        await service.close()  # the app's own kill is no news
    assert [record.getMessage() for record in caplog.records] == ["magpie exited (7)"]


async def test_a_dashboard_that_cannot_be_shown_is_logged_by_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _ = rig(tmp_path, monkeypatch)

    def opener(url: str) -> None:
        raise PermissionError("synthetic detail")

    service.opener = opener
    caplog.set_level("WARNING", logger="viki_agent.llm.magpie")
    with pytest.raises(magpie.MagpieError, match="open_failed"):
        await service.open(None)
    assert [record.getMessage() for record in caplog.records] == [
        "magpie dashboard open failed (PermissionError)"
    ]
    await service.close()


async def test_readiness_requires_authenticated_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = magpie.Magpie(tmp_path)
    seen: list[httpx2.Request] = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            assert not request.url.query and "cookie" not in request.headers
            return httpx2.Response(401)
        if len(seen) == 2:
            assert request.url.params["k"] == service._key
            return httpx2.Response(
                303,
                headers={
                    "location": "/",
                    "set-cookie": f"magpie-web-3431={service._key}; Path=/; HttpOnly",
                },
            )
        assert service._key in request.headers["cookie"]
        return httpx2.Response(200, headers={"content-type": "text/html"}, content="dashboard")

    original = httpx2.AsyncClient
    monkeypatch.setattr(
        magpie.httpx2,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx2.MockTransport(respond), **kwargs),
    )
    await service._ready(Child())  # type: ignore[arg-type]
    assert len(seen) == 3 and seen[0].url.port == magpie.WEB_PORT


async def test_gateway_hook_runs_before_discovery_and_inference() -> None:
    sequence: list[str] = []
    connection = parse_connection(
        {
            "name": "Magpie",
            "base_url": magpie.BASE_URL,
            "api_mode": "anthropic",
            "model": "provider/model",
            "api_key": "",
        }
    )

    async def before(row):
        assert row is connection
        sequence.append("start")

    def respond(request):
        sequence.append(request.method)
        if request.method == "GET":
            return httpx2.Response(200, json={"data": [{"id": connection.model}]})
        return httpx2.Response(503)

    gateway = Gateway(transport=httpx2.MockTransport(respond), before_request=before)
    assert await gateway.models(connection) == [connection.model]
    with pytest.raises(ConnectionError, match="upstream_failed"):
        _ = [part async for part in gateway.generate(connection, {"messages": []})]
    assert sequence == ["start", "GET", "start", "POST"]
