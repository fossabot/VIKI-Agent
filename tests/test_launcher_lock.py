"""The launcher's refusal when the POSIX single-instance lock finds its runtime folder is not
private to this account: _main logs the reason and gives up with MSG_RUNTIME_DIR, exit code 2,
before anything else is built. On Windows the same error means a mutex this account may not
open, so it takes the ordinary failed-start path instead. Each case patches the platform the
launcher reads, so every case runs on every OS."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from viki_agent import __main__ as launcher


def _stub_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str) -> None:
    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(launcher.winproc, "scrub_env", lambda env: dict(env))
    monkeypatch.setattr(launcher.config, "data_dir", lambda env: tmp_path)
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: None)
    monkeypatch.setattr(launcher, "_raise_nofile_limit", lambda: None)
    monkeypatch.setattr(launcher.config, "load", lambda env: object())

    def _raise(name: str) -> Any:
        raise PermissionError("the runtime directory is not private")

    monkeypatch.setattr(launcher.winproc, "single_instance", _raise)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_runtime_folder_refusal_gives_up_with_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _stub_everything(monkeypatch, tmp_path, platform)
    before = dict(os.environ)
    later: list[str] = []

    assert launcher._main([], later) == 2

    assert later == [launcher.MSG_RUNTIME_DIR]
    assert dict(os.environ) == before  # scrub_env's copy deletes nothing


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_main_shows_the_runtime_folder_message_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _stub_everything(monkeypatch, tmp_path, platform)
    shown: list[str] = []
    monkeypatch.setattr(launcher, "_alert", shown.append)

    assert launcher.main([]) == 2

    assert shown == [launcher.MSG_RUNTIME_DIR]


def test_a_windows_permission_error_fails_the_start_without_the_runtime_folder_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_everything(monkeypatch, tmp_path, "win32")
    shown: list[str] = []
    monkeypatch.setattr(launcher, "_alert", shown.append)

    with pytest.raises(PermissionError):
        launcher.main([])

    assert shown == []
