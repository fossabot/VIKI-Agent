"""The window-host link, over os.pipe pairs standing in for the desktop app's end of stdin and
stdout, and once over a child's real stdout."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import logging
import os
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO

import pytest

from viki_agent import winproc
from viki_agent.window import WindowManager
from viki_agent.window_host import IGNORED, MAX_LINE, HostWindow, WindowHost

HANG_GUARD_S = 30  # a child Python starts slowly under a parallel run's load
HOST_PID = 4242
URL = "http://127.0.0.1:8765/?ui=" + "0" * 32 + "#token=" + "t" * 43
OPEN_1 = b'{"type":"open","id":1,"url":"' + URL.encode() + b'"}\n'
CLOSE_1 = b'{"type":"close","id":1}\n'


class Link:
    """A WindowHost over two os.pipe pairs, with the desktop app's end of each: `app` writes the
    host's stdin and `sent` reads its stdout. Its callbacks record each call, then raise `fail`
    when it is set."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO, *, app: BinaryIO, sent: BinaryIO) -> None:
        self.app = app
        self.sent = sent
        self.fail: Exception | None = None
        self.wakes: list[int] = []  # the thread each wake ran on
        self.woke = asyncio.Event()
        self.gone = 0
        self.ended = asyncio.Event()
        self.host = WindowHost(stdin, stdout, host_pid=HOST_PID, wake=self._wake, gone=self._gone)

    def _wake(self) -> None:
        self.wakes.append(threading.get_ident())
        self.woke.set()
        if self.fail is not None:
            raise self.fail

    def _gone(self) -> None:
        self.gone += 1
        self.ended.set()
        if self.fail is not None:
            raise self.fail

    def send(self, data: bytes) -> None:
        self.app.write(data)
        self.app.flush()

    def line(self) -> bytes:
        """The next line the host wrote, read on a thread so that a missing one fails within
        HANG_GUARD_S instead of hanging the test."""
        lines: list[bytes] = []
        reader = threading.Thread(target=lambda: lines.append(self.sent.readline()), daemon=True)
        reader.start()
        reader.join(HANG_GUARD_S)
        assert lines, "the host wrote no line"
        return lines[0]

    def end(self) -> None:
        """End the host's stdin and wait for its reader thread to end."""
        self.app.close()
        if (thread := self.host._thread) is not None:
            thread.join(HANG_GUARD_S)
            assert not thread.is_alive()


@pytest.fixture
def link() -> Iterator[Link]:
    host_in, app_out = os.pipe()
    app_in, host_out = os.pipe()
    # Closed in reverse: the host's stdout first, so that a line() still waiting ends.
    with (
        open(app_in, "rb") as sent,
        open(app_out, "wb") as app,
        open(host_in, "rb") as stdin,
        contextlib.suppress(OSError),  # a host stdout still holding a line it could not send
        open(host_out, "wb") as stdout,
    ):
        link = Link(stdin, stdout, app=app, sent=sent)
        yield link
        link.end()


async def _open(host: WindowHost, url: str = URL) -> HostWindow:
    return await host.spawn([url], cwd=".", env={}, pipes=False)


async def test_each_open_is_one_line_with_a_rising_id(link: Link) -> None:
    first = await _open(link.host)
    second = await _open(link.host, URL + "&session=s")
    assert (first.id, second.id) == (1, 2)
    assert link.line() == OPEN_1
    assert link.line() == b'{"type":"open","id":2,"url":"' + URL.encode() + b'&session=s"}\n'


async def test_magpie_targets_only_the_window_with_its_exact_ui_nonce(link: Link) -> None:
    await _open(link.host)
    assert link.line() == OPEN_1
    dashboard = "http://127.0.0.1:3431/?k=" + "a" * 64
    link.host.show_magpie("0" * 32, dashboard)
    assert link.line() == (b'{"type":"magpie","id":1,"url":"' + dashboard.encode() + b'"}\n')
    link.host.hide_magpie("0" * 32)
    assert link.line() == b'{"type":"magpie_close","id":1}\n'
    for ui in (None, "1" * 32):
        with pytest.raises(ValueError):
            link.host.show_magpie(ui, dashboard)
    link.host._closed(1)
    assert not link.host.has_ui("0" * 32)


async def test_magpie_view_ownership_fences_late_startup_layout_and_close(link: Link) -> None:
    await _open(link.host)
    assert link.line() == OPEN_1
    ui = "0" * 32
    dashboard = "http://127.0.0.1:3431/?k=" + "a" * 64
    old: dict[str, object] = {"id": "a" * 32, "visible": True}
    current: dict[str, object] = {"id": "b" * 32, "visible": True}
    link.host.prepare_magpie(ui, old)
    link.host.hide_magpie(ui, "a" * 32)
    assert json.loads(link.line()) == {"type": "magpie_close", "id": 1, "view_id": "a" * 32}
    link.host.show_magpie(ui, dashboard, "a" * 32)
    link.host.prepare_magpie(ui, current)
    link.host.update_magpie(ui, old)
    link.host.hide_magpie(ui, "a" * 32)
    link.host.hide_magpie(ui)
    latest = {**current, "visible": False}
    link.host.update_magpie(ui, latest)
    assert json.loads(link.line()) == {"type": "magpie_view", "id": 1, "view": latest}
    link.host.show_magpie(ui, dashboard, "b" * 32)
    assert json.loads(link.line()) == {"type": "magpie", "id": 1, "url": dashboard, "view": latest}
    link.host._closed(1)
    assert link.host._magpie_views == {}


async def test_magpie_close_before_open_is_remembered_and_bounded(link: Link) -> None:
    await _open(link.host)
    assert link.line() == OPEN_1
    ui = "0" * 32
    view: dict[str, object] = {"id": "a" * 32}
    link.host.hide_magpie(ui, "a" * 32)
    assert not link.host.prepare_magpie(ui, view)
    assert link.host._magpie_views == {}
    for index in range(129):
        link.host.hide_magpie(ui, f"{index:032x}")
    assert not link.host.prepare_magpie(ui, {"id": "b" * 32})
    link.host._closed(1)
    assert link.host._magpie_canceled == {}


async def test_magpie_external_links_need_valid_http_url_and_live_window(link: Link) -> None:
    await _open(link.host)
    assert link.line() == OPEN_1
    loop = asyncio.get_running_loop()
    opened: list[str] = []
    done = asyncio.Event()

    def opener(url: str) -> None:
        opened.append(url)
        loop.call_soon_threadsafe(done.set)

    link.host._open_url = opener

    def message(url: str, window_id: int = 1):
        return link.host._accepted(
            json.dumps({"type": "magpie_open_url", "id": window_id, "url": url}).encode()
        )

    for url in (
        "file:///private",
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "http://127.0.0.1:8765/#token=secret",
        "http://localhost:8765/",
        "http://[::1]:8765/",
        "https://example.com/\\path",
        "https://example.com/\n",
        "https://example.com:" + "1" * 10,
    ):
        assert message(url) is None
    missing = message("https://example.com/", 2)
    assert missing is not None
    missing()
    assert opened == []
    accepted = message("https://example.com/sign-in")
    assert accepted is not None
    accepted()
    await asyncio.wait_for(done.wait(), HANG_GUARD_S)
    assert opened == ["https://example.com/sign-in"]


async def test_closed_ends_only_its_window(link: Link) -> None:
    first, second = await _open(link.host), await _open(link.host)
    first_closed = asyncio.create_task(first.wait_tree())
    link.send(b'{"type":"closed","id":2}\n')
    await asyncio.wait_for(second.wait_tree(), HANG_GUARD_S)
    await asyncio.sleep(0)  # a wrongly woken first_closed would finish here
    assert not first_closed.done()
    link.send(b'{"type":"closed","id":1}\n')
    await asyncio.wait_for(first_closed, HANG_GUARD_S)


async def test_unknown_malformed_wrong_key_and_oversized_lines_are_ignored(
    link: Link, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = await _open(link.host), await _open(link.host)
    first_closed = asyncio.create_task(first.wait_tree())
    ignored = [
        OPEN_1,  # a server message, carrying the token
        b'{"type":"closed","id":1\n',
        b"\xff\n",
        b"[" * (MAX_LINE - 1) + b"\n",
        b'["closed",1]\n',
        b'{"type":"closed"}\n',
        b'{"type":"closed","id":1,"url":"' + URL.encode() + b'"}\n',
        b'{"type":"wake","id":1}\n',
        b'{"type":"closed","id":true}\n',
        b'{"type":"closed","id":0}\n',
        b'{"type":"closed","id":"1"}\n',
        b'{"type":"closed","id":1.0}\n',
        b'{"type":"wake"}' + b" " * (MAX_LINE - 15) + b"\n",  # one byte over MAX_LINE
        b" " * (2 * MAX_LINE) + b'{"type":"wake"}\n',  # its tail is no line of its own
    ]
    caplog.set_level(logging.WARNING, logger="viki_agent.window_host")
    link.send(b'{"type":"closed","id":3}\n')  # no such window: taken, then dropped unlogged
    last = b'{"type":"closed","id":2}'
    # The last line is exactly MAX_LINE bytes, its newline included, and is taken.
    link.send(b"".join(ignored) + last + b" " * (MAX_LINE - len(last) - 1) + b"\n")
    await asyncio.wait_for(second.wait_tree(), HANG_GUARD_S)
    await asyncio.sleep(0)
    assert not first_closed.done()
    first_closed.cancel()
    assert link.wakes == []
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert records == [(logging.WARNING, IGNORED)] * len(ignored)
    assert "token" not in caplog.text


async def test_kill_tree_sends_close_and_stays_quiet_once_the_app_stops_reading(
    link: Link,
) -> None:
    window = await _open(link.host)
    assert link.line() == OPEN_1
    window.kill_tree()
    assert link.line() == CLOSE_1
    link.sent.close()  # the app reads no more
    window.kill_tree()
    window.release()
    with pytest.raises(OSError) as raised:
        await _open(link.host)
    assert raised.value.errno == errno.EPIPE
    assert "token" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert list(link.host._windows) == [1]  # the window that could not open is not kept


@pytest.mark.parametrize("argv", [[], [URL, URL], "x", [b"x"]])
async def test_spawn_takes_exactly_one_url(link: Link, argv: Sequence[str]) -> None:
    with pytest.raises(ValueError, match="^a hosted window opens exactly one URL$"):
        await link.host.spawn(argv, cwd=".", env={}, pipes=False)


async def test_wake_runs_on_the_loop_thread(link: Link) -> None:
    await _open(link.host)
    link.send(b'{"type":"wake"}\n')
    await asyncio.wait_for(link.woke.wait(), HANG_GUARD_S)
    assert link.wakes == [threading.get_ident()]


async def test_a_failing_callback_is_logged_by_its_type_only(
    link: Link, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="viki_agent.window_host")
    await _open(link.host)
    link.fail = RuntimeError(URL)
    link.send(b'{"type":"wake"}\n')
    link.app.close()
    await asyncio.wait_for(link.ended.wait(), HANG_GUARD_S)
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert records == [(logging.WARNING, "a window-host callback failed (RuntimeError)")] * 2
    assert "token" not in caplog.text


async def test_the_end_of_stdin_calls_gone_once_and_spawn_then_refuses(link: Link) -> None:
    await _open(link.host)
    link.app.close()
    await asyncio.wait_for(link.ended.wait(), HANG_GUARD_S)
    with pytest.raises(OSError) as raised:
        await _open(link.host)
    assert raised.value.errno == errno.EPIPE
    link.end()  # joins the reader thread: nothing more can arrive
    await asyncio.sleep(0)
    assert link.gone == 1


async def test_a_stdin_read_error_ends_the_link_as_its_end_does() -> None:
    class BrokenStdin(io.BytesIO):
        def readline(self, size: int | None = -1, /) -> bytes:
            raise OSError(errno.EIO, "stdin broke")

    ended = asyncio.Event()
    host = WindowHost(
        BrokenStdin(), io.BytesIO(), host_pid=HOST_PID, wake=lambda: None, gone=ended.set
    )
    await _open(host)
    await asyncio.wait_for(ended.wait(), HANG_GUARD_S)
    with pytest.raises(OSError):
        await _open(host)


def test_a_closed_loop_ends_the_reader_quietly() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    stdin = io.BytesIO(b'{"type":"wake"}\n')
    host = WindowHost(stdin, io.BytesIO(), host_pid=HOST_PID, wake=lambda: None, gone=lambda: None)
    host._read(loop)  # returns rather than raising out of the thread


async def test_holds_claims_only_the_host_pid(link: Link) -> None:
    async def nothing() -> None:
        pass

    windows = WindowManager(
        spawn=link.host.spawn, argv=lambda: [URL], cwd=Path.cwd(), env={}, on_last_closed=nothing
    )
    assert await windows.open()
    claims = [hold(pid) for hold in windows.holds() for pid in (HOST_PID, HOST_PID + 1)]
    assert claims == [True, False]
    await windows.stop()
    assert (link.line(), link.line()) == (OPEN_1, CLOSE_1)  # stopping asks the app to close it


CHILD = f"""
import asyncio, os
from viki_agent.window_host import WindowHost

async def main():
    host = WindowHost.from_stdio({HOST_PID}, wake=lambda: None, gone=lambda: None)
    print("stray", flush=True)
    await host.spawn([{URL!r}], cwd=".", env={{}}, pipes=False)
    os.write(1, b"stray\\n")
    print("stray")

asyncio.run(main())
"""


def test_from_stdio_keeps_stray_output_off_the_link(tmp_path: Path) -> None:
    done = winproc.run([sys.executable, "-c", CHILD], cwd=tmp_path, timeout=HANG_GUARD_S, input="")
    assert (done.returncode, done.stdout, done.stderr) == (0, OPEN_1.decode(), "")
