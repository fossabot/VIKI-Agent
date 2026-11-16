"""winproc.tcp_listeners and Job.holds: who listens on a port, and whether that pid is in a Job
(a preview's port ownership)."""

from __future__ import annotations

import ctypes
import errno
import os
import socket
import struct
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from viki_agent import winproc

# A guard on events that normally take milliseconds (a server's listen, a kill).
HANG_GUARD_S = 10.0

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the ownership reads")


@contextmanager
def _listening(host: str, family: socket.AddressFamily) -> Iterator[int]:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen()
        yield sock.getsockname()[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


@windows_only
def test_an_ipv4_listener_is_found_with_its_pid() -> None:
    with _listening("127.0.0.1", socket.AF_INET) as port:
        assert winproc.tcp_listeners(port) == frozenset({os.getpid()})


@windows_only
@pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback")
def test_an_ipv6_listener_is_found_with_its_pid() -> None:
    with _listening("::1", socket.AF_INET6) as port:
        assert winproc.tcp_listeners(port) == frozenset({os.getpid()})


@windows_only
def test_a_bound_socket_that_does_not_listen_and_a_closed_port_have_none() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        assert winproc.tcp_listeners(sock.getsockname()[1]) == frozenset()
    assert winproc.tcp_listeners(_free_port()) == frozenset()


@windows_only
def test_sixty_four_listeners_are_each_found() -> None:
    with ExitStack() as stack:
        ports = [stack.enter_context(_listening("127.0.0.1", socket.AF_INET)) for _ in range(64)]
        for port in ports:
            assert winproc.tcp_listeners(port) == frozenset({os.getpid()})


def _growing_table(answers_122: int, port: int, pid: int) -> tuple[list[int], object]:
    """A stand-in for GetExtendedTcpTable whose IPv4 table grows `answers_122` times before it
    fits: each such call asks for more room. The IPv6 table is empty. Returns the IPv4 calls'
    buffer sizes and the stand-in."""
    row = struct.pack("<6I", 2, 0x0100007F, socket.htons(port), 0, 0, pid)
    sizes: list[int] = []

    def fake(buffer: ctypes.Array[ctypes.c_char], size: ctypes.c_ulong, *args: object) -> int:
        family = args[1]
        need = 4 + len(row) * (len(sizes) + 1) if family == socket.AF_INET else 4
        if family == socket.AF_INET:
            sizes.append(len(buffer))
            if len(sizes) <= answers_122:
                size.value = need
                return 122
            data = struct.pack("<I", 1) + row
        elif len(buffer) < need:
            size.value = need
            return 122
        else:
            data = struct.pack("<I", 0)
        ctypes.memmove(buffer, data, len(data))
        return 0

    return sizes, fake


@windows_only
def test_the_size_retry_follows_a_table_that_keeps_growing(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes, fake = _growing_table(3, port=4321, pid=777)
    monkeypatch.setattr(winproc, "_GetExtendedTcpTable", fake)
    assert winproc.tcp_listeners(4321) == frozenset({777})
    assert sizes == [0, 28, 52, 76]  # the first call with no room, then 3 more as it grew


@windows_only
def test_the_size_retry_gives_up_after_three_more_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes, fake = _growing_table(4, port=4321, pid=777)
    monkeypatch.setattr(winproc, "_GetExtendedTcpTable", fake)
    with pytest.raises(OSError):
        winproc.tcp_listeners(4321)
    assert len(sizes) == 4


@windows_only
def test_another_error_from_the_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "_GetExtendedTcpTable", lambda *_args: 87)
    with pytest.raises(OSError):
        winproc.tcp_listeners(4321)


@pytest.mark.parametrize("bad", [0, 65536, True, -1, 80.0])
def test_a_port_outside_1_to_65535_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        winproc.tcp_listeners(bad)  # type: ignore[arg-type]


def _poll_listeners(port: int, run: winproc.ShellRun) -> frozenset[int]:
    deadline = time.monotonic() + HANG_GUARD_S
    while not (pids := winproc.tcp_listeners(port)):
        assert run.wait(0) is None, "the server exited before it listened"
        assert time.monotonic() < deadline, "nothing listened"
        time.sleep(0.05)  # the server's listen is a real OS event; poll its state
    return pids


@windows_only
def test_a_listener_in_the_job_is_held(tmp_path: Path) -> None:
    port = _free_port()
    command = f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'
    run = winproc.start_shell(command, cwd=tmp_path, env=winproc.scrub_env(os.environ))
    try:
        pids = _poll_listeners(port, run)
        assert all(run.job.holds(pid) for pid in pids)
        assert not run.job.holds(os.getpid())
        run.kill_tree()
        assert run.wait(HANG_GUARD_S) is not None
    finally:
        run.release()
    assert not any(run.job.holds(pid) for pid in pids)


@windows_only
def test_a_pid_that_cannot_be_opened_is_not_held() -> None:
    job = winproc.Job()
    try:
        assert not job.holds(0xFFFFFFFC)  # no such process
    finally:
        job.close()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX refusal")
def test_listener_discovery_refuses_off_windows() -> None:
    with pytest.raises(OSError) as refused:
        winproc.tcp_listeners(8080)
    assert refused.value.errno == errno.ENOSYS
