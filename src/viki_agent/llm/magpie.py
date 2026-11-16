"""Bundled Magpie, started on demand and owned by this app process."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import html
import json
import logging
import os
import platform
import secrets
import socket
import stat
import sys
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import httpx2

from viki_agent import compiled, protection, winproc
from viki_agent.api_json import unique_object
from viki_agent.llm.connections import Connection, ConnectionError
from viki_agent.llm.gateway import finish_cleanup

if TYPE_CHECKING:
    from viki_agent.window_host import WindowHost

logger = logging.getLogger(__name__)

VERSION = "0.1.3357"
SOURCE_COMMIT = "8544d5fdb51bea24dece530e7272210de3e0f519"
PINNED = {
    "win32/x64": ("be2ec10921df558b4bd85cd9ee9c353d640cee1bb7699b2ba494f91449ab65cb", 74378240),
    "darwin/arm64": ("99a19a5baf3117b3a66b73a024ccb2cd3c4c1268866ac3b987f9cca35eb9d3d9", 69315186),
    "linux/x64": ("a706225e8f9d412121547a4f51af794493250b1a0b5397aa73aef836b1bde193", 73560226),
    "linux/arm64": ("24dcf7e777ba7f6c3b4b0cfb65e021f4daccc5661522c49c6c4db7715ef028e9", 67829922),
}
GATEWAY_PORT = 3426
WEB_PORT = 3431
BASE_URL = f"http://127.0.0.1:{GATEWAY_PORT}/v1"
STARTUP_S = 30
POLL_S = 0.05
# How long a killed leftover's group gets to end before the ports are probed regardless.
REAP_S = 2
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


class MagpieError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Unidentified(Exception):
    """A process has the recorded pid, but whether it is the recorded one could not be read."""


def bundle_path() -> Path:
    root = compiled.INSTALL_DIR / ("magpie" if compiled.COMPILED else "dist/magpie")
    if compiled.COMPILED:
        return root / "magpie.gz"
    return root / ("magpie.exe" if sys.platform == "win32" else "magpie")


def _arch() -> str | None:
    return {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )


def _payload(source: Path, expected: tuple[str, int], output: BinaryIO | None = None) -> None:
    digest = hashlib.sha256()
    size = 0
    with gzip.open(source, "rb") if source.name == "magpie.gz" else source.open("rb") as stream:
        while chunk := stream.read(min(1024 * 1024, expected[1] + 1 - size)):
            size += len(chunk)
            if size > expected[1]:
                raise MagpieError("missing_bundle")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
    if size != expected[1] or not secrets.compare_digest(digest.hexdigest(), expected[0]):
        raise MagpieError("missing_bundle")


def verified_bundle(binary: Path) -> Path:
    """Verify the original executable bytes in a source or compressed installed bundle."""
    try:
        manifest = binary.with_name("magpie.json")
        manifest_info = manifest.stat(follow_symlinks=False)
        binary_info = binary.stat(follow_symlinks=False)
        if (
            binary.parent.is_symlink()
            or binary.parent.is_junction()
            or binary.is_symlink()
            or binary.is_junction()
            or manifest.is_symlink()
            or manifest.is_junction()
            or not stat.S_ISREG(manifest_info.st_mode)
            or manifest_info.st_nlink != 1
            or manifest_info.st_size > 4096
            or not stat.S_ISREG(binary_info.st_mode)
            or binary_info.st_nlink != 1
        ):
            raise ValueError()
        value = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        arch = _arch()
        expected = PINNED.get(f"{sys.platform}/{arch}")
        name = "magpie.exe" if sys.platform == "win32" else "magpie"
        if expected is None or value != {
            "version": VERSION,
            "source_commit": SOURCE_COMMIT,
            "platform": sys.platform,
            "arch": arch,
            "binary": name,
            "sha256": expected[0],
        }:
            raise ValueError()
        expected_name = "magpie.gz" if compiled.COMPILED else name
        if binary.name != expected_name or (
            compiled.COMPILED and os.path.lexists(binary.with_name(name))
        ):
            raise ValueError()
        size = binary_info.st_size
        if size > expected[1] + 1024 * 1024 or binary.name == name and size != expected[1]:
            raise ValueError()
        _payload(binary, expected)
        return binary
    except OSError, EOFError, ValueError, TypeError, RecursionError, zlib.error:
        raise MagpieError("missing_bundle") from None


def _private_directory(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise MagpieError("unavailable")
    protection.create_private_directory(path)
    protection.require_private_directory(path)


def _runtime_binary(path: Path, expected: tuple[str, int]) -> None:
    """An installed executable is private, regular, unaliased and byte-for-byte pinned."""
    if path.is_symlink() or path.is_junction():
        raise MagpieError("runtime_unavailable")
    protection.require_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != expected[1]
            or sys.platform != "win32"
            and info.st_mode & 0o700 != 0o700
            or hashlib.file_digest(stream, "sha256").hexdigest() != expected[0]
        ):
            raise MagpieError("runtime_unavailable")
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise MagpieError("runtime_unavailable")


def materialize(source: Path, root: Path) -> Path:
    """Create the original executable from an installed gzip without replacing existing files."""
    if source.name != "magpie.gz":
        return source
    try:
        verified_bundle(source)
        arch = _arch()
        expected = PINNED[f"{sys.platform}/{arch}"]
        runtime = root / "runtime"
        _private_directory(runtime)
        target_dir = runtime / f"{VERSION}-{sys.platform}-{arch}"
        _private_directory(target_dir)
        target = target_dir / ("magpie.exe" if sys.platform == "win32" else "magpie")
        if os.path.lexists(target):
            _runtime_binary(target, expected)
            return target
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        created = None
        try:
            fd = os.open(target, flags | getattr(os, "O_BINARY", 0), 0o700)
            with os.fdopen(fd, "wb") as stream:
                created = os.fstat(stream.fileno())
                _payload(source, expected, stream)
                if sys.platform != "win32":
                    os.fchmod(stream.fileno(), 0o700)
                stream.flush()
                os.fsync(stream.fileno())
            _runtime_binary(target, expected)
        except BaseException:
            if created is not None:
                with contextlib.suppress(OSError):
                    current = target.stat(follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        target.unlink()
            raise
        return target
    except OSError, EOFError, protection.ProtectionError, zlib.error:
        raise MagpieError("runtime_unavailable") from None


def _environment(root: Path, key: str) -> dict[str, str]:
    _private_directory(root)
    for leaf in ("config", "cache", "state", "run"):
        _private_directory(root / leaf)
    config = root / "config" / "magpie"
    _private_directory(config)
    settings = config / "settings.json"
    try:
        fd = os.open(settings, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"noAutoUpdate": True, "autoRestart": False}, stream)
    except FileExistsError:
        protection.require_private_file(settings)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("MAGPIE_", "VIKI_"))
    }
    env.update(
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_STATE_HOME=str(root / "state"),
        MAGPIE_ADDR=f"127.0.0.1:{GATEWAY_PORT}",
        MAGPIE_WEB_KEY=key,
        DO_NOT_TRACK="1",
        MAGPIE_NO_STATS="1",
        # Magpie then reads no agent sign-in and serves no subscription account (ADR-079).
        MAGPIE_NO_ACCOUNTS="1",
    )
    return env


def _ports_free() -> None:
    """Refuse occupied dedicated ports, including a separately running Magpie."""
    with contextlib.ExitStack() as stack:
        for port in (GATEWAY_PORT, WEB_PORT):
            probe = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # A Magpie that was just killed leaves the connections it had open in
                # TIME_WAIT for up to a minute, and without this option the bind fails on them
                # though nothing listens there and Magpie's own listener, which sets it too,
                # would start. A listener on this address is refused either way. Known limit,
                # as for the app's own port: on macOS another program's wildcard listener on the
                # same port is not seen.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise MagpieError("port_in_use") from None


def _identity(pid: int) -> str | None:
    """What tells the process now at `pid` from any other that ever has that pid; None once
    nothing runs there, and also when it could not be read."""
    started = winproc.process_start_time(pid)
    if started is None:
        return None
    if sys.platform == "linux":
        # Linux counts a start time from boot, so a pid and its tick can recur after a restart.
        try:
            return f"{_BOOT_ID.read_text(encoding='ascii').strip()}/{started}"
        except OSError, ValueError:
            return None
    return str(started)


def _leads_group(pid: int) -> bool:
    """Whether a process at `pid` leads its own process group, as the Magpie this app started
    does for as long as it runs. False once nothing runs there."""
    if sys.platform == "win32":
        return False  # no record is kept there
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _write_record(record: Path, pid: int) -> None:
    """Name this launch's Magpie on disk, so the next launch can end it if this one is killed
    before it stops Magpie itself. Raises OSError when no record could be written."""
    identity = _identity(pid)
    if identity is None:
        raise OSError("process identity unavailable")
    # A record still here was kept for a leftover nobody could identify. The ports were free
    # for this child, so it is the Magpie the next launch has to find.
    record.unlink(missing_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(record, flags, 0o600), "w", encoding="utf-8") as stream:
        json.dump({"pid": pid, "started": identity}, stream)


def _leftover(record: Path) -> winproc.Job | None:
    """The process group of the Magpie an earlier launch recorded, only while that very process
    still runs. A pid that went to another process has another identity, and nothing is touched.
    Raises `_Unidentified` when a process is there but its identity could not be read."""
    try:
        protection.require_private_file(record)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(record, flags), "rb") as stream:
            data = stream.read(513)
        value = json.loads(data, object_pairs_hook=unique_object)
    except OSError, ValueError, RecursionError, protection.ProtectionError:
        return None
    if len(data) > 512 or not isinstance(value, dict) or set(value) != {"pid", "started"}:
        return None
    pid, started = value["pid"], value["started"]
    # As a group id, 0 names this app's own group and 1 the system's first process.
    if type(pid) is not int or not 1 < pid <= 0x7FFFFFFF or not isinstance(started, str):
        return None
    identity = _identity(pid)
    if identity is None and _leads_group(pid):
        # A read that failed (a slow `ps`, an unreadable /proc) is not a process that ended.
        raise _Unidentified()
    if started != identity:
        return None
    job = winproc.Job()
    job.adopt(pid)
    return job


class Magpie:
    def __init__(
        self,
        data_dir: Path,
        *,
        host: WindowHost | None = None,
        opener: Callable[[str], None] = winproc.open_external,
    ) -> None:
        self.root = data_dir / "magpie"
        self.host = host
        self.opener = opener
        self._child: winproc.Child | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._key = secrets.token_hex(32)
        self._crashed = False
        self._closing = False
        self._page: Path | None = None
        self._available = False
        self._record = self.root / "run" / "child.json"
        self._refused: str | None = None
        self._unidentified = False

    @property
    def running(self) -> bool:
        return self._child is not None and self._child.process.returncode is None

    async def status(self) -> dict[str, object]:
        try:
            await asyncio.to_thread(verified_bundle, bundle_path())
            available = True
        except MagpieError:
            available = False
        self._available = available
        return self._status()

    def _status(self) -> dict[str, object]:
        return {
            "available": self._available,
            "running": self.running,
            "version": VERSION,
            "desktop": self.host is not None,
            "base_url": BASE_URL,
        }

    async def ensure_connection(self, connection: Connection) -> None:
        if connection.base_url == BASE_URL:
            try:
                await self.start()
            except MagpieError:
                raise ConnectionError("endpoint_unreachable") from None

    async def start(self, *, explicit: bool = False) -> None:
        try:
            await self._start(explicit)
        except MagpieError as error:
            # A chat sends a refused request again, so a cause is logged once, not per request.
            if not self._closing and error.code != self._refused:
                self._refused = error.code
                logger.warning("magpie start refused (%s)", error.code)
            raise
        self._refused = None

    async def _start(self, explicit: bool) -> None:
        async with self._lock:
            if self._closing:
                raise MagpieError("unavailable")
            if self.running:
                return
            if self._child is not None:
                self._crashed = True
            if self._crashed and not explicit:
                raise MagpieError("needs_open")
            if self._watcher is not None:
                await self._watcher
            binary = await asyncio.to_thread(verified_bundle, bundle_path())
            self._available = True
            await self._reap()
            await asyncio.to_thread(_ports_free)
            try:
                env = await asyncio.to_thread(_environment, self.root, self._key)
                binary = await asyncio.to_thread(materialize, binary, self.root)
            except OSError, protection.ProtectionError:
                raise MagpieError("data_unavailable") from None
            child = None
            try:
                if self._closing:
                    # stop() ran while the folders were prepared: start nothing at all.
                    raise MagpieError("unavailable")
                child = await winproc.spawn(
                    [str(binary), "web", "--addr", f"127.0.0.1:{WEB_PORT}", "--no-open"],
                    cwd=self.root,
                    env=env,
                    pipes=False,
                )
                self._child = child
                if self._closing:
                    # stop() ran during the spawn, when it had no child to kill.
                    raise MagpieError("unavailable")
                if sys.platform != "win32":
                    # Before readiness: a launch that dies during the wait leaves this child too.
                    try:
                        await asyncio.to_thread(_write_record, self._record, child.pid)
                    except OSError:
                        logger.warning("magpie process record unavailable")
                await self._ready(child)
                self._crashed = False
                self._watcher = asyncio.create_task(self._watch(child))
            except BaseException as exc:
                # A caller that went away is no crash: this app ends the child itself, so the
                # next use of the connection may start Magpie again.
                crashed = not isinstance(exc, asyncio.CancelledError)

                async def discard() -> None:
                    try:
                        if child is not None:
                            child.kill_tree()
                            await child.wait_tree(POLL_S)
                    finally:
                        if child is not None:
                            child.release()
                            with contextlib.suppress(OSError):
                                self._record.unlink(missing_ok=True)
                        self._child = None
                        self._crashed = crashed

                await finish_cleanup(discard())
                if isinstance(exc, OSError | protection.ProtectionError):
                    raise MagpieError("spawn_failed") from None
                raise

    async def _reap(self) -> None:
        if sys.platform == "win32":
            return  # a Windows Job is kill-on-close: Magpie ended there with its launch
        try:
            job = await asyncio.to_thread(_leftover, self._record)
        except _Unidentified:
            # The record is all that lets a launch end that Magpie, so it stays for the next
            # start or launch to read again. A chat repeats a refused start: logged once.
            if not self._unidentified:
                self._unidentified = True
                logger.warning("magpie left by an earlier launch could not be identified")
            return
        self._unidentified = False
        if job is not None:
            job.terminate()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(REAP_S):
                    # Until the group is gone its listeners still hold the two ports.
                    while job.active_processes():
                        await asyncio.sleep(POLL_S)
            job.close()
            logger.warning("magpie left running by an earlier launch was stopped")
        with contextlib.suppress(OSError):
            self._record.unlink(missing_ok=True)

    async def reap_leftover(self) -> None:
        """End the Magpie an earlier launch of this app left running, so its ports and its
        loaded keys do not outlive the app. Nothing is started in its place."""
        async with self._lock:
            if self._child is None:
                await self._reap()

    async def _ready(self, child: winproc.Child) -> None:
        try:
            async with (
                asyncio.timeout(STARTUP_S),
                httpx2.AsyncClient(timeout=0.5, follow_redirects=False, trust_env=False) as client,
            ):
                while child.process.returncode is None and not self._closing:
                    try:
                        client.cookies.clear()
                        root = f"http://127.0.0.1:{WEB_PORT}/"
                        async with client.stream("GET", root) as response:
                            if response.status_code != 401:
                                raise MagpieError("unexpected_dashboard")
                        async with client.stream("GET", self._url()) as response:
                            if (
                                response.status_code != 303
                                or response.headers.get("location") != "/"
                            ):
                                raise MagpieError("unexpected_dashboard")
                        if self._key not in client.cookies.values():
                            raise MagpieError("unexpected_dashboard")
                        async with client.stream("GET", root) as response:
                            if response.status_code == 200 and response.headers.get(
                                "content-type", ""
                            ).startswith("text/html"):
                                return
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(POLL_S)
        except TimeoutError:
            pass
        if self._closing:
            raise MagpieError("unavailable")
        if child.process.returncode is not None:
            # Its output goes to the null device, so the exit code is all a failed start leaves.
            logger.warning("magpie exited during start (%s)", child.process.returncode)
            raise MagpieError("exited")
        raise MagpieError("not_ready")

    async def _watch(self, child: winproc.Child) -> None:
        try:
            code = await child.process.wait()
            if not self._closing:
                logger.warning("magpie exited (%s)", code)
            child.kill_tree()
            await child.wait_tree(POLL_S)
        finally:
            child.release()
            with contextlib.suppress(OSError):
                self._record.unlink(missing_ok=True)
            if self._child is child:
                self._child = None
                self._crashed = not self._closing

    def _url(self) -> str:
        return f"http://127.0.0.1:{WEB_PORT}/?k={self._key}"

    async def open(
        self, ui: str | None, view: dict[str, object] | None = None
    ) -> dict[str, object]:
        if self.host is not None and not self.host.has_ui(ui):
            raise MagpieError("invalid_window")
        if self.host is not None and view is not None and not self.host.prepare_magpie(ui, view):
            return self._status()
        try:
            await self.start(explicit=True)
            if self.host is not None:
                if view is None:
                    self.host.show_magpie(ui, self._url())
                else:
                    self.host.show_magpie(ui, self._url(), str(view["id"]))
            else:
                await asyncio.to_thread(self._open_browser)
            return await self.status()
        except (OSError, protection.ProtectionError) as error:
            logger.warning("magpie dashboard open failed (%s)", type(error).__name__)
            raise MagpieError("open_failed") from None

    def _open_browser(self) -> None:
        page = self.root / "run" / f"open-{secrets.token_hex(16)}.html"
        fd = os.open(page, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(
                '<!doctype html><meta http-equiv="refresh" content="0;url='
                + html.escape(self._url(), quote=True)
                + '">'
            )
        if self._page is not None:
            self._page.unlink(missing_ok=True)
        self._page = page
        self.opener(page.as_uri())

    async def update_view(self, ui: str | None, view: dict[str, object]) -> dict[str, object]:
        if self.host is None:
            raise MagpieError("invalid_window")
        self.host.update_magpie(ui, view)
        return self._status()

    async def hide(self, ui: str | None, view_id: str | None = None) -> dict[str, object]:
        if self.host is not None:
            try:
                self.host.hide_magpie(ui, view_id)
            except OSError, ValueError:
                raise MagpieError("invalid_window") from None
        return self._status()

    def stop(self) -> None:
        """The app's stop has begun: Magpie's tree ends now and no start runs any more. It
        awaits nothing, so it can run before the server drains its open connections; `close()`
        reaps afterwards."""
        self._closing = True
        if self._child is not None:
            self._child.kill_tree()

    async def close(self) -> None:
        # Not behind the lock: a start in progress holds it until Magpie answers or gives up.
        self.stop()
        async with self._lock:
            if self._watcher is not None:
                await self._watcher
            if self._page is not None:
                with contextlib.suppress(OSError):
                    self._page.unlink(missing_ok=True)
