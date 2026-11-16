"""Preview windows: a Chromium app window per preview, each with a fresh profile under
`<data>/preview-edge`, its web requests sent to a proxy that refuses them except for the
preview's own port.

Rules:
- At most MAX_WINDOWS windows; each starts through `spawn` (winproc.spawn: a kill-on-close Job
  per window) with the scrubbed environment, no pipes and its profile as its cwd. When a
  window's tree ends: release(), the profile deleted, the owner's on_closed() once.
- The proxy is 127.0.0.1 at the port of a dual-stack socket bound with SO_EXCLUSIVEADDRUSE to
  [::]:0 and never listening, held from the first open() until close_all(): the port is held on
  every IPv4 and IPv6 address, so no other program can bind it and a connection to it is refused.
- sweep() deletes everything under the area except links and junctions, and nothing when the
  area itself is one. The server calls it at launch; close_all() calls it last.
- No log line holds a URL, a path or the argv.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from viki_agent import winproc
from viki_agent.code.code_checks import CodeRunError
from viki_agent.window import Spawn, Window

log = logging.getLogger(__name__)

MAX_WINDOWS = 4
PROFILE_DIR = "preview-edge"
CLOSE_WAIT_S = 5.0


def preview_argv(
    edge: Path, profile: Path, url: str, *, allow: Sequence[str], proxy: str
) -> list[str]:
    # The first seven elements are __main__.edge_argv's, copied rather than imported: __main__
    # imports server, which imports this module.
    return [
        str(edge),
        f"--user-data-dir={profile}",
        "--disable-extensions",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        f"--proxy-server={proxy}",
        # <-loopback> first: in Chromium a later bypass rule wins, and a trailing
        # <-loopback> was measured sending even the preview's own port to the proxy.
        "--proxy-bypass-list=" + ";".join(["<-loopback>", *allow]),
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--app={url}",
    ]


class PreviewWindows:
    def __init__(self, edge: Path | None, data_dir: Path, *, spawn: Spawn = winproc.spawn) -> None:
        self._edge = edge
        self._spawn = spawn
        self.area = data_dir / PROFILE_DIR
        self.proxy: str | None = None  # "127.0.0.1:<port>", set at the first open()
        self._proxy_socket: socket.socket | None = None
        self._slots = 0
        self._closing = False
        self._windows: dict[Window, str] = {}  # each open window and its owner
        self._trackers: set[asyncio.Task[None]] = set()

    @property
    def count(self) -> int:
        return self._slots

    async def open(
        self,
        url: str,
        *,
        allow: Sequence[str],
        owner: str,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        if self._edge is None:
            raise CodeRunError("browser_missing")
        if self._closing:
            raise CodeRunError("unavailable")
        if self._slots >= MAX_WINDOWS:
            raise CodeRunError("window_limit")
        self._slots += 1  # reserved with no await since the checks
        profile: Path | None = None
        try:
            proxy = self._refusing_proxy()  # before any await, so close_all() always sees it
            profile = await asyncio.to_thread(self._new_profile)
            child = await self._spawn(
                preview_argv(self._edge, profile, url, allow=allow, proxy=proxy),
                cwd=profile,
                env=winproc.scrub_env(os.environ),
                pipes=False,
            )
        except BaseException as exc:
            self._slots -= 1
            if profile is not None:
                await asyncio.to_thread(shutil.rmtree, profile, ignore_errors=True)
            if isinstance(exc, OSError):
                log.warning("could not open a preview window (%s)", type(exc).__name__)
                raise CodeRunError("start_failed") from None
            raise
        self._windows[child] = owner
        if self._closing:  # close_all() began during the spawn: its tracker cleans up
            child.kill_tree()
        tracker = asyncio.create_task(self._track(child, profile, on_closed))
        self._trackers.add(tracker)
        tracker.add_done_callback(self._trackers.discard)

    def close_owner(self, owner: str) -> None:
        for child, held_by in list(self._windows.items()):
            if held_by == owner:
                child.kill_tree()

    async def close_all(self) -> None:
        self._closing = True
        for child in list(self._windows):
            child.kill_tree()
        if self._trackers:
            await asyncio.wait(self._trackers, timeout=CLOSE_WAIT_S)
        for child in list(self._windows):
            child.release()
        if self._proxy_socket is not None:
            self._proxy_socket.close()
        await asyncio.to_thread(self.sweep)

    def sweep(self) -> None:
        area = self.area
        if area.is_symlink() or area.is_junction() or not area.is_dir():
            return
        try:
            entries = list(area.iterdir())
        except OSError as exc:
            log.warning("could not list the preview profiles (%s)", type(exc).__name__)
            return
        failed = 0
        for entry in entries:
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue  # rmtree or unlink could act on what it points at
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                failed += 1
        if failed:
            log.warning("could not delete %d preview profile entries", failed)

    def _refusing_proxy(self) -> str:
        if self.proxy is None:
            # One dual-stack socket holds the port on every IPv4 and IPv6 address: a 127.0.0.1
            # bind alone leaves 0.0.0.0 and [::] free, and a listener there would receive every
            # request a preview window sends to the proxy. No fallback: if it cannot be made, the
            # OSError fails the open closed.
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                if sys.platform == "win32":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("::", 0))
            except OSError:
                sock.close()
                raise
            self._proxy_socket = sock
            self.proxy = f"127.0.0.1:{sock.getsockname()[1]}"
        return self.proxy

    def _new_profile(self) -> Path:
        self.area.mkdir(parents=True, exist_ok=True)
        if self.area.is_symlink() or self.area.is_junction():  # a profile would land outside
            raise OSError("the preview profile area is a link")
        profile = self.area / uuid.uuid4().hex
        profile.mkdir()
        return profile

    async def _track(
        self, child: Window, profile: Path, on_closed: Callable[[], None] | None
    ) -> None:
        try:
            await child.wait_tree()
        finally:
            child.release()
            try:
                await asyncio.to_thread(shutil.rmtree, profile)
            except OSError as exc:  # the next sweep removes it
                log.warning("could not delete a preview profile (%s)", type(exc).__name__)
            del self._windows[child]
            self._slots -= 1
            if on_closed is not None:
                try:
                    on_closed()
                except Exception as exc:
                    log.warning("a preview window's close callback failed (%s)", type(exc).__name__)
