"""winproc.open_terminal: the OS terminal in its own window, outside any Job.

A real call opens a window on the desktop, so every test patches `_detached` (the seam) and
records what it was given; none starts the terminal."""

from __future__ import annotations

import errno
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from viki_agent import winproc


def _record(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def detached(argv: Any, *, cwd: Any, env: dict[str, str], creationflags: int) -> None:
        calls.append({"argv": list(argv), "cwd": cwd, "env": env, "creationflags": creationflags})

    def no_job(*_: object, **__: object) -> None:
        raise AssertionError("open_terminal constructed a Job")

    monkeypatch.setattr(winproc, "_detached", detached)
    monkeypatch.setattr(winproc, "Job", no_job)
    return calls


@pytest.mark.skipif(sys.platform != "win32", reason="open_terminal is Windows-only")
def test_open_terminal_starts_powershell_in_a_new_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    env = {"PATH": "x", "VIKI_E27": "1"}
    winproc.open_terminal(tmp_path, env)
    shell = str(winproc.system_dir() / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    assert calls == [
        {
            "argv": [shell, "-NoLogo"],
            "cwd": tmp_path,
            "env": env,
            "creationflags": 0x00000010,
        }
    ]
    assert winproc.CREATE_NEW_CONSOLE == 0x00000010
    assert calls[0]["env"] is not env  # a copy, as the Popen call takes it


def test_open_terminal_starts_the_linux_shell_in_the_chosen_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="linux"))
    terminal = tmp_path / "xdg-terminal-exec"
    monkeypatch.setattr(
        winproc, "find_on_path", lambda name, _path: terminal if name == terminal.name else None
    )
    env = {"PATH": str(tmp_path), "SHELL": "/bin/zsh"}
    winproc.open_terminal(tmp_path, env)
    assert calls == [
        {
            "argv": [
                str(terminal),
                "/bin/sh",
                "-c",
                'cd -- "$1" || exit; exec "${SHELL:-/bin/sh}"',
                "viki-terminal",
                str(tmp_path),
            ],
            "cwd": tmp_path,
            "env": env,
            "creationflags": 0,
        }
    ]
    assert calls[0]["env"] is not env


def test_open_terminal_opens_a_macos_command_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="darwin"))
    script = tmp_path / "terminal.command"
    scripts: list[tuple[object, object]] = []

    def command_file(argv: object, cwd: object) -> Path:
        scripts.append((argv, cwd))
        return script

    monkeypatch.setattr(winproc, "_command_file", command_file)
    env = {"PATH": "x"}
    winproc.open_terminal(tmp_path, env)
    assert scripts == [((), tmp_path)]
    assert calls == [
        {
            "argv": ["/usr/bin/open", "-a", "Terminal", str(script)],
            "cwd": tmp_path,
            "env": env,
            "creationflags": 0,
        }
    ]


def test_open_terminal_without_a_linux_launcher_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(OSError) as refused:
        winproc.open_terminal(tmp_path, {})
    assert refused.value.errno == errno.ENOENT
    assert calls == []


def test_a_macos_launcher_failure_removes_its_private_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="darwin"))
    script = tmp_path / "terminal.command"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(winproc, "_command_file", lambda _argv, _cwd: script)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("launcher failed")

    monkeypatch.setattr(winproc, "_detached", fail)
    with pytest.raises(OSError, match="launcher failed"):
        winproc.open_terminal(tmp_path, {})
    assert not script.exists()
