"""Git executable lookup uses explicit absolute PATH entries, without cwd fallback."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from viki_agent import winproc


def _git_file(folder: Path, platform: str = sys.platform) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / ("git.exe" if platform == "win32" else "git")
    exe.touch()
    exe.chmod(0o700)
    return exe


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_git_skips_cwd_and_relative_path_entries_without_changing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    server = tmp_path / "server"
    child = tmp_path / "child"
    _git_file(server, platform)
    _git_file(child, platform)
    _git_file(server / "relative", platform)
    first = _git_file(tmp_path / "first", platform)
    _git_file(tmp_path / "second", platform)
    monkeypatch.chdir(server)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform=platform))
    env = {
        "PATH": os.pathsep.join(("", ".", "relative", str(first.parent), str(tmp_path / "second"))),
        "NoDefaultCurrentDirectoryInExePath": "synthetic-existing",
        "GIT_CONFIG_GLOBAL": "synthetic-config",
        "PATHEXT": ".CMD;.BAT;.EXE",
    }
    before = dict(env)
    monkeypatch.setattr(winproc.os, "environ", env)
    calls: list[tuple[list[str], object, object]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((argv, kwargs["cwd"], kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="synthetic-output\n", stderr="")

    monkeypatch.setattr(winproc, "run", run)
    assert winproc.git(child, "status", "--porcelain", timeout=0.04) == "synthetic-output\n"
    assert calls == [
        (
            [
                str(first),
                "--no-optional-locks",
                "-c",
                "core.quotepath=off",
                "status",
                "--porcelain",
            ],
            child,
            0.04,
        )
    ]
    assert env == before


@pytest.mark.parametrize("entry", [None, "", ".", "relative", "C:relative", "\\root-relative"])
def test_git_refuses_missing_empty_or_relative_path_before_validation_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str | None
) -> None:
    _git_file(tmp_path)
    _git_file(tmp_path / "relative")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(winproc.os, "environ", {} if entry is None else {"PATH": entry})
    monkeypatch.setattr(winproc, "run", lambda *args, **kwargs: pytest.fail("launched fallback"))
    monkeypatch.setattr(
        winproc, "resolve_exe", lambda *args: pytest.fail("validated a relative candidate")
    )
    with pytest.raises(FileNotFoundError, match="git"):
        winproc.git(tmp_path, "--version")


@pytest.mark.parametrize("invalid", ["missing", "directory", "denied"])
def test_git_skips_unavailable_candidates_and_preserves_failure_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    native = "git.exe" if sys.platform == "win32" else "git"
    if invalid == "directory":
        (first / native).mkdir()
    second = _git_file(tmp_path / "second")
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(second.parent))))
    resolve = winproc.resolve_exe

    def validate(path: Path) -> Path:
        if invalid == "denied" and path.parent == first:
            raise PermissionError("synthetic denied entry")
        return resolve(path)

    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(returncode=2, stdout="", stderr="synthetic failure\n")

    monkeypatch.setattr(winproc, "resolve_exe", validate)
    monkeypatch.setattr(winproc, "run", run)
    with pytest.raises(RuntimeError, match="^git status failed: synthetic failure$"):
        winproc.git(tmp_path, "status")
    assert seen[0][0] == str(second)


def test_git_missing_absolute_candidates_do_not_fall_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(winproc, "run", lambda *args, **kwargs: pytest.fail("launched fallback"))
    with pytest.raises(FileNotFoundError, match="git"):
        winproc.git(tmp_path, "--version")


def test_git_windows_accepts_quoted_path_entries_and_ignores_batch_pathext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "shims"
    shim.mkdir()
    for name in ("git.cmd", "git.bat", "git"):
        (shim / name).touch()
    exe = _git_file(tmp_path / "path with spaces", "win32")
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(shim), f'"{exe.parent}"')))
    monkeypatch.setenv("PATHEXT", ".CMD;.BAT")
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(winproc, "run", run)
    winproc.git(tmp_path, "--version")
    assert seen[0][0] == str(exe)


UNTRUSTED = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "safe.bareRepository=explicit",
]
SETTINGS_READ = ["config", "--list", "--name-only", "-z"]


def _untrusted_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str, *, read_code: int = 0
) -> tuple[str, dict[str, str], list[tuple[list[str], dict[str, Any]]]]:
    """A git on PATH, a small environment, and a `run` that answers the settings read with
    `listing` and records every launch."""
    exe = str(_git_file(tmp_path / "bin"))
    env = {"PATH": str(tmp_path / "bin"), "SYNTHETIC": "kept"}
    monkeypatch.setattr(winproc.os, "environ", env)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((argv, kwargs))
        if argv[-len(SETTINGS_READ) :] == SETTINGS_READ:
            return SimpleNamespace(returncode=read_code, stdout=listing, stderr="synthetic")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(winproc, "run", run)
    return exe, env, calls


def test_untrusted_git_switches_off_every_filter_driver_and_every_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = "\0".join(
        (
            "filter.lfs.clean",
            "filter.lfs.process",
            "core.autocrlf",
            "filter.clean",  # no driver name: Git ignores it
            "filter.a.b.smudge",  # the driver is `a.b`
            "filter.lfs.required",
            "",
        )
    )
    exe, env, calls = _untrusted_rig(tmp_path, monkeypatch, listing)
    with winproc.untrusted_git():
        assert winproc.git(tmp_path, "status", "--porcelain", timeout=0.04) == "ok\n"
    base = [exe, "--no-optional-locks", "-c", "core.quotepath=off", *UNTRUSTED]
    hardened = {**env, "GIT_ALLOW_PROTOCOL": "none", "GIT_NO_LAZY_FETCH": "1"}
    off = [
        part
        for name in ("lfs", "a.b")
        for setting in ("clean=", "smudge=", "process=", "required=false")
        for part in ("-c", f"filter.{name}.{setting}")
    ]
    assert [(argv, kwargs["env"]) for argv, kwargs in calls] == [
        ([*base, *SETTINGS_READ], hardened),
        ([*base, *off, "status", "--porcelain"], hardened),
    ]
    assert env == {"PATH": str(tmp_path / "bin"), "SYNTHETIC": "kept"}

    # The block ends with its scope, an exception or not: a later call is a plain one again.
    with pytest.raises(LookupError), winproc.untrusted_git():
        raise LookupError
    calls.clear()
    winproc.git(tmp_path, "status")
    assert calls == [
        ([exe, "--no-optional-locks", "-c", "core.quotepath=off", "status"], calls[0][1])
    ]
    assert calls[0][1].get("env") is None


@pytest.mark.parametrize(
    "names",
    [
        ["a=b"],
        ["bad\N{REPLACEMENT CHARACTER}bytes"],
        [f"d{number}" for number in range(winproc.MAX_FILTER_DRIVERS + 1)],
    ],
    ids=["equals-sign", "not-utf-8", "too-many"],
)
def test_untrusted_git_refuses_a_driver_no_option_can_switch_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    listing = "".join(f"filter.{name}.clean\0" for name in names)
    _exe, _env, calls = _untrusted_rig(tmp_path, monkeypatch, listing)
    with winproc.untrusted_git(), pytest.raises(RuntimeError, match="no option can switch off"):
        winproc.git(tmp_path, "status")
    assert [argv[-len(SETTINGS_READ) :] for argv, _ in calls] == [SETTINGS_READ]


def test_untrusted_git_runs_nothing_when_its_settings_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exe, _env, calls = _untrusted_rig(tmp_path, monkeypatch, "", read_code=128)
    with winproc.untrusted_git(), pytest.raises(RuntimeError, match="^git config failed"):
        winproc.git(tmp_path, "status")
    assert len(calls) == 1


@pytest.mark.parametrize(("after", "left"), [(103.0, 2.0), (106.0, 0.0)])
def test_untrusted_git_shares_one_timeout_with_its_settings_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: float, left: float
) -> None:
    """The rail's worst case counts one cap per lookup, so the extra read must not add one."""
    _exe, _env, calls = _untrusted_rig(tmp_path, monkeypatch, "")
    clock = iter((100.0, after))
    monkeypatch.setattr(winproc, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with winproc.untrusted_git():
        winproc.git(tmp_path, "status", timeout=5.0)
    assert [kwargs["timeout"] for _argv, kwargs in calls] == [5.0, left]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bits")
def test_git_posix_skips_nonexecutable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _git_file(tmp_path / "first")
    first.chmod(0o600)
    second = _git_file(tmp_path / "second")
    monkeypatch.setenv("PATH", os.pathsep.join((str(first.parent), str(second.parent))))
    assert winproc._git_exe() == second
    second.chmod(0o600)
    with pytest.raises(FileNotFoundError, match="git"):
        winproc._git_exe()


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows executable search order")
def test_git_actual_process_ignores_server_and_command_cwd_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    server, child, path_bin = (tmp_path / name for name in ("server", "child", "path-bin"))
    for folder in (server, child, path_bin):
        folder.mkdir()
    for folder in (server, child):
        shutil.copyfile(winproc.system_dir() / "whoami.exe", folder / "git.exe")
    expected = path_bin / "git.exe"
    shutil.copyfile(winproc.system_dir() / "where.exe", expected)
    monkeypatch.chdir(server)
    monkeypatch.setenv("PATH", str(path_bin))
    popen = winproc.subprocess.Popen
    images: list[Path] = []
    query = winproc._fn(
        "QueryFullProcessImageNameW",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )

    def observe(*args: Any, **kwargs: Any) -> Any:
        process: Any = popen(*args, **kwargs)
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            assert query(int(process._handle), 0, buffer, ctypes.byref(size))
            images.append(Path(buffer.value))
        except BaseException:
            with process:
                process.kill()
            raise
        return process

    monkeypatch.setattr(winproc.subprocess, "Popen", observe)
    with pytest.raises(RuntimeError):
        winproc.git(child, "--version")
    assert images == [expected]
