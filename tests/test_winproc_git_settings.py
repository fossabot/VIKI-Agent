"""Child-only Git configuration isolation, with synthetic startup include canaries."""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from viki_agent import winproc

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
HANG_GUARD_S = 10.0


@pytest.fixture(autouse=True)
def synthetic_parent_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    empty = tmp_path / "empty-config"
    empty.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")


def _snapshot(tmp_path: Path) -> tuple[str, ...]:
    source = tmp_path / "snapshot-config"
    source.write_text("[guard]\n value = snapshot\n", encoding="utf-8")
    return (
        "-c",
        "core.fsmonitor=false",
        "--literal-pathspecs",
        "--no-pager",
        "config",
        "--file",
        str(source),
        "--no-includes",
        "--list",
        "-z",
    )


def _poison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str) -> Path:
    canary = tmp_path / "outside-canary"
    canary.write_text("SYNTHETIC-CANARY invalid config", encoding="utf-8")
    source = tmp_path / "poison-config"
    source.write_text(f'[include]\n path = "{canary.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(source))
    return canary


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("with_index", [False, True])
def test_default_git_argv_and_environment_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, with_index: bool
) -> None:
    parent = {"PATH": "synthetic-bin", "GIT_CONFIG_PARAMETERS": "synthetic-parent"}
    monkeypatch.setattr(winproc.os, "environ", parent)
    monkeypatch.setattr(winproc, "_git_exe", lambda: tmp_path / "git-native")
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="synthetic-output", stderr="")

    monkeypatch.setattr(winproc, "run", run)
    index = tmp_path / "private" / "index" if with_index else None
    options = {"isolated_config": False} if explicit else {}
    assert (
        winproc.git(tmp_path, "status", timeout=0.04, index=index, **options) == "synthetic-output"
    )
    argv = [str(tmp_path / "git-native"), "--no-optional-locks", "-c", "core.quotepath=off"]
    if index is not None:
        argv += ["-c", "core.sparseCheckout=false", "-c", f"core.hooksPath={index.parent}"]
    expected_env = {**parent, "GIT_INDEX_FILE": str(index)} if index is not None else None
    assert seen == [(argv + ["status"], {"cwd": tmp_path, "timeout": 0.04, "env": expected_env})]
    assert parent == {"PATH": "synthetic-bin", "GIT_CONFIG_PARAMETERS": "synthetic-parent"}


@pytest.mark.parametrize("null_source", ["nul", "/dev/null"])  # os.devnull: Windows, POSIX
@pytest.mark.parametrize("with_index", [False, True])
def test_isolation_clears_injection_case_insensitively_in_child_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, null_source: str, with_index: bool
) -> None:
    parent = {
        "PATH": "synthetic-bin",
        "KEEP": "synthetic-preserved",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG": "synthetic-file",
        "git_config_parameters": "synthetic-parameters",
        "Git_Config_Count": "2",
        "gIt_CoNfIg_KeY_0": "include.path",
        "git_config_value_0": "synthetic-target",
        "GIT_CONFIG_KEY_999": "synthetic-key",
        "GIT_CONFIG_VALUE_999": "synthetic-value",
        "git_config_global": "synthetic-global",
        "Git_Config_System": "synthetic-system",
        "git_config_nosystem": "0",
        "GIT_INDEX_FILE": "synthetic-existing-index",
    }
    before = dict(parent)
    monkeypatch.setattr(winproc.os, "environ", parent)
    monkeypatch.setattr(winproc.os, "devnull", null_source)
    monkeypatch.setattr(winproc, "_git_exe", lambda: tmp_path / "git-native")
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append((argv, kwargs["env"]))
        assert parent == before
        # Git before 2.32 ignores GIT_CONFIG_GLOBAL and reads ~/.gitconfig and the XDG
        # config: both point at an empty private folder that exists for this call only.
        home = Path(kwargs["env"]["HOME"])
        assert home.is_dir() and not any(home.iterdir())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(winproc, "run", run)
    index = tmp_path / "private" / "index" if with_index else None
    winproc.git(tmp_path, "config", "--list", index=index, isolated_config=True)
    argv = [str(tmp_path / "git-native"), "--no-optional-locks", "-c", "core.quotepath=off"]
    if index is not None:
        argv += ["-c", "core.sparseCheckout=false", "-c", f"core.hooksPath={index.parent}"]
    expected = {
        "PATH": "synthetic-bin",
        "KEEP": "synthetic-preserved",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_INDEX_FILE": "synthetic-existing-index" if index is None else str(index),
        "GIT_CONFIG_GLOBAL": null_source,
        "GIT_CONFIG_SYSTEM": null_source,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": seen[0][1]["HOME"],
        "XDG_CONFIG_HOME": seen[0][1]["HOME"],
    }
    assert seen == [(argv + ["config", "--list"], expected)] and seen[0][1] is not parent
    assert parent == before
    assert not Path(expected["HOME"]).exists()


@needs_git
@pytest.mark.parametrize("scope", ["global", "system"])
def test_native_private_parse_avoids_poisoned_startup_includes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, scope: str
) -> None:
    canary = _poison(tmp_path, monkeypatch, scope)
    args = _snapshot(tmp_path)
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match="outside-canary"):
        winproc.git(tmp_path, *args)
    assert winproc.git(tmp_path, *args, isolated_config=True) == "guard.value\nsnapshot\0"
    with pytest.raises(RuntimeError, match="outside-canary"):
        winproc.git(tmp_path, *args)
    assert dict(os.environ) == before
    assert canary.read_text(encoding="utf-8") == "SYNTHETIC-CANARY invalid config"
    assert not caplog.text


@needs_git
@pytest.mark.parametrize("kind", ["pairs", "parameters", "file"])
def test_native_injected_configuration_is_absent_from_isolated_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "pairs":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "guard.injected")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "synthetic-parent")
    elif kind == "parameters":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'guard.injected=synthetic-parent'")
    else:
        source = tmp_path / "legacy-config"
        source.write_text("[guard]\n injected = synthetic-parent\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG", str(source))
    before = dict(os.environ)
    args = _snapshot(tmp_path)
    assert winproc.git(tmp_path, "config", "--get", "guard.injected") == "synthetic-parent\n"
    with pytest.raises(RuntimeError):
        winproc.git(tmp_path, "config", "--get", "guard.injected", isolated_config=True)
    assert winproc.git(tmp_path, *args, isolated_config=True) == "guard.value\nsnapshot\0"
    assert dict(os.environ) == before


@needs_git
def test_native_isolation_hides_home_and_xdg_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "synthetic-home"
    home.mkdir()
    (home / ".gitconfig").write_text("[guard]\n home = synthetic-home\n", encoding="utf-8")
    canary = home / "poison"
    canary.write_text("SYNTHETIC-CANARY invalid config", encoding="utf-8")
    xdg = tmp_path / "synthetic-xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text("[guard]\n xdg = synthetic-xdg\n", encoding="utf-8")
    (xdg / "git" / "attributes").write_text("* synthetic-xdg\n", encoding="utf-8")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = tmp_path / "repo"
    repo.mkdir()
    winproc.git(repo, "init", "-q", isolated_config=True)
    source = tmp_path / "home-include"
    source.write_text("[include]\n path = ~/poison\n", encoding="utf-8")
    homes: list[str] = []
    native = winproc.run

    def run(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("env"):
            homes.append(kwargs["env"]["HOME"])
        return native(*args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)
    before = dict(os.environ)
    # The installed Git honours GIT_CONFIG_GLOBAL, so these show the child's own HOME and
    # XDG_CONFIG_HOME through `~` expansion and the default attributes file instead.
    for key, value in (("guard.home", "synthetic-home"), ("guard.xdg", "synthetic-xdg")):
        assert winproc.git(repo, "config", "--get", key) == f"{value}\n"
        with pytest.raises(RuntimeError):
            winproc.git(repo, "config", "--get", key, isolated_config=True)
    listing = ("config", "--file", str(source), "--includes", "--list")
    with pytest.raises(RuntimeError, match="poison"):
        winproc.git(tmp_path, *listing)
    assert winproc.git(tmp_path, *listing, isolated_config=True) == "include.path=~/poison\n"
    attribute = ("check-attr", "synthetic-xdg", "--", "file")
    assert winproc.git(repo, *attribute) == "file: synthetic-xdg: set\n"
    assert winproc.git(repo, *attribute, isolated_config=True) == (
        "file: synthetic-xdg: unspecified\n"
    )
    assert homes and all(not Path(path).exists() for path in homes)
    assert str(home) not in homes and dict(os.environ) == before
    assert canary.read_text(encoding="utf-8") == "SYNTHETIC-CANARY invalid config"


@needs_git
def test_native_isolation_and_private_index_work_together(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    winproc.git(repo, "init", "-q", isolated_config=True)
    (repo / "synthetic.txt").write_text("synthetic\n", encoding="utf-8")
    private = tmp_path / "private" / "index"
    private.parent.mkdir()
    winproc.git(repo, "add", "synthetic.txt", index=private, isolated_config=True)
    assert private.is_file() and not (repo / ".git" / "index").exists()
    assert winproc.git(repo, "ls-files", index=private, isolated_config=True) == "synthetic.txt\n"


@needs_git
def test_native_ordinary_and_isolated_calls_overlap_without_parent_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _poison(tmp_path, monkeypatch, "global")
    args = _snapshot(tmp_path)
    before = dict(os.environ)
    barrier = threading.Barrier(2)
    native = winproc.run

    def run(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=HANG_GUARD_S)
        assert dict(os.environ) == before
        return native(*args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)

    def ordinary() -> None:
        with pytest.raises(RuntimeError, match="outside-canary"):
            winproc.git(tmp_path, *args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        control = pool.submit(ordinary)
        isolated = pool.submit(winproc.git, tmp_path, *args, isolated_config=True)
        assert isolated.result(timeout=HANG_GUARD_S) == "guard.value\nsnapshot\0"
        assert control.result(timeout=HANG_GUARD_S) is None
    assert dict(os.environ) == before
