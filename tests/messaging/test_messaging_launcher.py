"""Messaging launch variables are never read; every process handoff's scrub boundary."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.messaging.test_messaging_config import DISCORD, SLACK
from tests.test_main import FakeServer, _cfg, _fake_edge, _reaching_the_server
from viki_agent import __main__ as launcher
from viki_agent import winproc
from viki_agent.state import Prefs
from viki_agent.storage import StateError

PREFIXES = ("VIKI_MATTERMOST_", "VIKI_SLACK_", "VIKI_DISCORD_")
UNKNOWN = {
    "vIkI_sLaCk_future": "synthetic-secret",
    "VIKI_DISCORD_UNKNOWN": "synthetic-secret",
    "viki_mattermost_partial": "synthetic-secret",
}


def clean(env: Any) -> None:
    assert not any(name.upper().startswith(PREFIXES) for name in env)


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
async def test_all_messaging_prefixes_removed_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    original = {"PATH": "synthetic", "OWNER_HOOK": "keep", **SLACK, **DISCORD, **UNKNOWN}
    env = winproc.scrub_env(original)
    clean(env)
    assert original["VIKI_SLACK_BOT_TOKEN"] == SLACK["VIKI_SLACK_BOT_TOKEN"]
    assert env == {"PATH": "synthetic", "OWNER_HOOK": "keep"}
    # The process runner is stubbed; Git lookup still needs an absolute PATH.
    env["PATH"] = str(tmp_path)
    monkeypatch.setattr(winproc.os, "environ", env)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(winproc, "_OPENER", ("synthetic-open",), raising=False)
    monkeypatch.setattr(
        winproc, "Job", lambda: SimpleNamespace(adopt=lambda _: None, close=lambda: None)
    )
    monkeypatch.setattr(
        winproc.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(winproc, "resolve_exe", lambda path: Path(path))
    monkeypatch.setattr(winproc, "system_dir", lambda: tmp_path)
    monkeypatch.setattr(winproc, "powershell_argv", lambda: ["synthetic-powershell"])
    seen: list[dict[str, str]] = []

    class Process:
        pid, returncode = 1, 0

        def __init__(self, args: Any, **kwargs: Any) -> None:
            self.args = args
            inherited = dict(kwargs.get("env", winproc.os.environ))
            clean(inherited)
            seen.append(inherited)

        def __enter__(self) -> Process:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def communicate(self, **_: Any) -> tuple[str, str]:
            return "", ""

        def wait(self) -> int:
            return 0

    async def start(*args: Any, **kwargs: Any) -> Process:
        return Process(args, **kwargs)

    monkeypatch.setattr(winproc.subprocess, "Popen", Process)
    monkeypatch.setattr(winproc.asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(
        winproc.os, "startfile", lambda _: seen.append(dict(winproc.os.environ)), raising=False
    )
    child = await winproc.spawn(["synthetic-cli"], cwd=tmp_path, env=winproc.child_env("sdk-py"))
    child.release()
    winproc.run(["synthetic-probe"], cwd=tmp_path)
    winproc.git(tmp_path, "--version")
    winproc.open_external("https://synthetic.invalid")
    if platform == "win32":
        winproc.open_console(
            ["synthetic-cli", "auth", "login", "--claudeai"], cwd=tmp_path, env=env
        )
        winproc.open_terminal(tmp_path, env)
        winproc.start_shell("synthetic-check-or-preview", cwd=tmp_path, env=env).release()
    assert len(seen) == (7 if platform == "win32" else 4)
    assert all(child["OWNER_HOOK"] == "keep" for child in seen)
    assert seen[0]["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert seen[0]["CLAUDE_AGENT_SDK_CLIENT_APP"] == "viki-agent"
    for inherited in seen:
        clean(inherited)


@pytest.mark.parametrize("configured", ["valid", "partial", "mixed", "invalid", "absent"])
@pytest.mark.parametrize("branch", ["normal", "recovery", "second", "refused"])
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_launch_ignores_messaging_variables_and_scrubs_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configured: str,
    branch: str,
    platform: str,
) -> None:
    git_bin = tmp_path / "git-bin"
    git_bin.mkdir()
    git_exe = git_bin / ("git.exe" if os.name == "nt" else "git")
    git_exe.touch()
    git_exe.chmod(0o700)
    env = {"PATH": str(git_bin), "OWNER_HOOK": "keep", **UNKNOWN}
    if configured != "absent":
        env.update({**SLACK, **DISCORD})
    if configured == "partial":
        del env["VIKI_SLACK_APP_TOKEN"]
        del env["VIKI_DISCORD_OWNER_ID"]
    if configured == "mixed":
        env["viki_slack_bot_token"] = SLACK["VIKI_SLACK_BOT_TOKEN"]
        env["viki_discord_owner_id"] = DISCORD["VIKI_DISCORD_OWNER_ID"]
    if configured == "invalid":
        env["VIKI_SLACK_BOT_TOKEN"] = "synthetic-invalid-secret"
        env["VIKI_DISCORD_CLIENT_URL"] = (
            "https://synthetic-user:synthetic-password" + "@" + "example.test"
        )
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path / "home"))
    # The launcher's PATH additions depend on which folders this host has; they are pinned in
    # tests/test_winproc.py, and this test is about the messaging variables.
    monkeypatch.setattr(winproc, "prepend_user_bin", lambda _env: None)
    cfg = replace(_cfg(tmp_path), cli=Path("synthetic-cli"))
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform=platform, argv=[], stderr=None))
    monkeypatch.setattr(launcher, "_STOP_SIGNALS", (), raising=False)
    monkeypatch.setattr(launcher, "_server", lambda conf, **_: FakeServer(conf))
    monkeypatch.setattr(launcher.config, "data_dir", lambda _: tmp_path / "data")
    monkeypatch.setattr(launcher, "_raise_nofile_limit", lambda: None)
    monkeypatch.setattr(launcher.protection, "visit_key_source", lambda: None)
    monkeypatch.setattr(launcher.protection, "keychain_source", lambda _: None)
    monkeypatch.setattr(launcher.protection, "set_key_source", lambda _: None)
    monkeypatch.setattr(launcher, "user_defaults", lambda *_, **__: Prefs())
    monkeypatch.setattr(
        winproc,
        "single_instance",
        lambda _: None if branch == "second" else SimpleNamespace(release=lambda: None),
    )
    monkeypatch.setattr(launcher, "_wake_running_instance", lambda: True)
    monkeypatch.setattr(launcher, "_browser_left_open", lambda _: False)
    monkeypatch.setattr(winproc.Signal, "create", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(launcher, "_alert", lambda _: clean(os.environ))
    inherited: list[dict[str, str]] = []

    def run(*_: Any, **__: Any) -> Any:
        clean(os.environ)
        inherited.append(dict(os.environ))
        return SimpleNamespace(stdout="2.1.79", stderr="", returncode=0)

    monkeypatch.setattr(winproc, "run", run)
    monkeypatch.setattr(launcher, "git_version", lambda: winproc.git(tmp_path, "--version"))
    if branch == "recovery":
        monkeypatch.setattr(
            launcher, "AppState", lambda *_: (_ for _ in ()).throw(StateError("corrupt"))
        )
    if branch == "refused":
        monkeypatch.setattr(launcher.config, "load", lambda _: replace(cfg, edge=None))
    _, spawned = _fake_edge(monkeypatch)
    assert launcher.main(["--dev"]) == (5 if branch == "refused" else 0)
    clean(os.environ)
    assert os.environ == {"PATH": str(git_bin), "OWNER_HOOK": "keep"}
    for secret in (
        SLACK["VIKI_SLACK_BOT_TOKEN"],
        SLACK["VIKI_SLACK_APP_TOKEN"],
        DISCORD["VIKI_DISCORD_BOT_TOKEN"],
        "synthetic-password",
        "synthetic-invalid-secret",
    ):
        assert secret not in caplog.text
    if branch in {"second", "refused"}:
        assert not calls
        return
    made = calls["create_recovery_app" if branch == "recovery" else "create_app"]
    for key in ("mattermost", "slack_config", "discord_config"):
        assert key not in made
    clean(made["windows"]._env)
    if branch == "normal":
        clean(made["manager"]._config.env)
    assert inherited

    async def browser() -> None:
        await made["windows"].start()
        clean(spawned["env"])
        await made["windows"].stop()

    monkeypatch.setattr(launcher, "_keep_icon", lambda _: 0)
    monkeypatch.setattr(launcher, "_redirect_url", lambda *_: "file:///synthetic.html")
    asyncio.run(browser())
