"""Opt-in Bot harness assembly uses synthetic roots and the normal vault bootstrap."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.llm.test_connections import FORM
from tests.test_driver_reports import initialize
from viki_agent import __main__ as launcher
from viki_agent import config
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.llm.gateway import Gateway
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION
from viki_agent.session import SessionManager
from viki_agent.state import Prefs
from viki_agent.vaults import starter

from replay import SID, FakeSpawner

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import e2e_server as harness  # noqa: E402

FIXTURE = "tests/fixtures/claude/text-two-turns.jsonl"


def _forbid(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("this assembly test must not start a process or bind a port")


@pytest.fixture(autouse=True)
def _synthetic_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(harness.os, "environ", {"USERPROFILE": str(home), "HOME": str(home)})
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")
    monkeypatch.setattr(harness, "SCRATCH", tmp_path / "viki-e2e")
    monkeypatch.setattr(harness.winproc, "run", _forbid)
    monkeypatch.setattr(harness.winproc, "spawn", _forbid)


@pytest.mark.parametrize(
    ("options", "extra_env", "error"),
    [
        (["--bots-team"], {}, "--bots-team needs --bots."),
        (["--bots-team", "--scratch", "--fixture", FIXTURE], {}, "--bots-team needs --bots."),
        (["--bots"], {}, "--bots needs --scratch"),
        (["--bots", "--scratch", "--no-vault"], {}, "--bots cannot use"),
        (["--bots", "--scratch", "--damaged-store"], {}, "--bots cannot use"),
        (["--bots", "--scratch", "--dirty"], {}, "--bots cannot use"),
        (["--bots", "--scratch"], {}, "synthetic fixture"),
        (["--bots", "--scratch", "--fixture", "tests/fake_claude.py"], {}, "synthetic fixture"),
        (
            ["--bots", "--scratch", "--fixture", "tests/fixtures/claude/missing.jsonl"],
            {},
            "synthetic fixture",
        ),
        (
            ["--bots", "--scratch", "--fixture", FIXTURE],
            {"VIKI_CLAUDE_CLI": "claude.exe"},
            "synthetic CLI",
        ),
        (
            ["--bots", "--scratch", "--fixture", FIXTURE],
            {"VIKI_TEST_CLI_SCRIPT": "different-fake.py"},
            "synthetic CLI",
        ),
    ],
)
def test_bot_mode_refuses_before_socket_scratch_or_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    extra_env: dict[str, str],
    error: str,
) -> None:
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(harness, "_listen", _forbid)
    monkeypatch.setattr(harness, "_scratch", _forbid)
    monkeypatch.setattr(harness.config, "load", _forbid)

    assert harness.main(options) == 2
    assert error in capsys.readouterr().err


def test_bot_mode_refuses_external_fixture_before_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    external = tmp_path / "fixture.jsonl"
    external.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(harness, "_listen", _forbid)
    monkeypatch.setattr(harness, "_scratch", _forbid)
    monkeypatch.setattr(harness.config, "load", _forbid)

    assert harness.main(["--bots", "--scratch", "--fixture", str(external)]) == 2
    assert "synthetic fixture" in capsys.readouterr().err
    assert external.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("root", [Path("viki-e2e"), Path("/").absolute(), Path("different")])
def test_bot_mode_refuses_unsafe_scratch_root_before_cleanup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], root: Path
) -> None:
    monkeypatch.setattr(harness, "SCRATCH", root)
    monkeypatch.setattr(harness, "_listen", _forbid)
    monkeypatch.setattr(harness, "_scratch", _forbid)
    monkeypatch.setattr(harness.config, "load", _forbid)

    assert harness.main(["--bots", "--scratch", "--fixture", FIXTURE]) == 2
    assert "scratch root" in capsys.readouterr().err


def _assembly(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    real_load = harness.config.load

    def load(env: dict[str, str]) -> config.Config:
        calls["env"] = dict(env)
        return real_load(env)

    def manager(**kwargs: Any) -> Any:
        calls["driver"] = kwargs["config"]
        calls["state"] = kwargs["state"]
        assert kwargs["config"].vault == Path(calls["env"]["VIKI_VAULT"])
        return SimpleNamespace(live=lambda: [])

    real_services = harness.build_services

    def services(*args: Any, **kwargs: Any) -> Any:
        calls["active"] = kwargs["active"]
        calls["spaces"] = kwargs["spaces"]
        assert args[0].vault == kwargs["active"].cwd
        result = real_services(*args, **kwargs)
        calls["version_after_services"] = kwargs["readiness"].version
        return result

    def app(**kwargs: Any) -> Any:
        calls["app"] = kwargs
        return SimpleNamespace(router=SimpleNamespace(routes=[]))

    monkeypatch.setattr(harness, "_listen", lambda *_args: object())
    monkeypatch.setattr(harness.config, "load", load)
    monkeypatch.setattr(harness.config, "_browser_for", lambda *_args: (None, ""))
    monkeypatch.setattr(harness, "SessionManager", manager)
    monkeypatch.setattr(harness, "cli_version", lambda _argv: "2.1.259")
    monkeypatch.setattr(launcher, "cli_version", lambda _argv: "3.14.0")
    monkeypatch.setattr(harness, "build_services", services)
    monkeypatch.setattr(harness, "Catalog", lambda *_args: object())
    monkeypatch.setattr(harness.server, "create_app", app)
    monkeypatch.setattr(harness, "_serve", lambda *_args: 0)
    return calls


@pytest.mark.parametrize("mode", ["ordinary", "bots", "model-connections"])
def test_real_bootstrap_and_version_seam_are_opt_in(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    bots = mode == "bots"
    current_version = mode != "ordinary"
    calls = _assembly(monkeypatch)
    monkeypatch.setenv("VIKI_CLAUDE_CLI", sys.executable)
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", str(REPO / "tests" / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", "inherited-state")
    options = ["--scratch", "--fixture", FIXTURE]
    if mode != "ordinary":
        options.append(f"--{mode}")

    assert harness.main(options) == 0

    parent = harness.SCRATCH / "workspaces" if bots else harness.SCRATCH
    vault = parent / "vault"
    active = calls["active"]
    assert active.cwd == vault and active.chat_only is False
    workspace = calls["spaces"].get(active.workspace.workspace_id)
    assert workspace.bootstrap == ("complete" if bots else "none")
    assert workspace.policy == ("ordinary" if bots else "existing_readonly")
    assert workspace.source == ("created" if bots else "opened")
    assert calls["app"]["config"].vault == vault
    assert calls["app"]["config"].claude_home == harness.SCRATCH / "home" / ".claude"
    assert calls["app"]["config"].data_dir == harness.SCRATCH / "data"
    assert calls["version_after_services"] == "3.14.0"
    assert calls["driver"].cli == (sys.executable, str(REPO / "tests" / "fake_claude.py"))
    assert calls["driver"].cli_version == (FIXTURE_VERSION if current_version else "2.1.259")
    assert calls["app"]["readiness"].version == (FIXTURE_VERSION if current_version else "3.14.0")
    assert calls["driver"].env["FAKE_CLAUDE_SCRIPT"] == str((REPO / FIXTURE).resolve())
    assert ("FAKE_CLAUDE_STATE" in calls["driver"].env) is (not bots)
    assert (vault / "CLAUDE.md").is_file() is (not bots)
    assert (vault / "Welcome.md").is_file() is bots
    if bots:
        assert parent.resolve() == parent.absolute()
        assert parent.is_relative_to(harness.SCRATCH)
        assert workspace.name == "Bot test vault" and workspace.portable_id is None
        assert sorted(path.name for path in vault.iterdir()) == ["Welcome.md"]


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (["--model-connections"], "--scratch"),
        (["--model-connections", "--fixture", FIXTURE], "--scratch"),
        (["--model-connections", "--scratch"], "synthetic fixture"),
        (
            ["--model-connections", "--scratch", "--fixture", "tests/fake_claude.py"],
            "synthetic fixture",
        ),
        (
            [
                "--model-connections",
                "--scratch",
                "--fixture",
                "tests/fixtures/claude/missing.jsonl",
            ],
            "synthetic fixture",
        ),
    ],
)
def test_model_connections_require_synthetic_inputs_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    error: str,
) -> None:
    monkeypatch.setattr(harness, "_listen", _forbid)
    monkeypatch.setattr(harness, "_scratch", _forbid)
    monkeypatch.setattr(harness.config, "load", _forbid)
    assert harness.main(options) == 2
    assert error in capsys.readouterr().err


async def test_model_connection_replay_version_is_authoritative_for_the_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _assembly(monkeypatch)
    managers: list[SessionManager] = []

    def manager(**kwargs: Any) -> SessionManager:
        result = SessionManager(**kwargs)
        managers.append(result)
        return result

    monkeypatch.setattr(harness, "SessionManager", manager)
    assert harness.main(["--scratch", "--fixture", FIXTURE, "--model-connections"]) == 0
    actual = managers[0]
    connections = Connections(calls["app"]["settings"])
    row = parse_connection(FORM)
    connections.write(0, [row])
    gateway = Gateway()
    actual.bind_model_connections(connections, gateway)
    actual._config = replace(
        actual._config,
        spawner=FakeSpawner(initialize(claude_code_version="2.1.259")),
        grace_s=0.01,
        poll_s=0.01,
        control_timeout_s=0.05,
    )
    driver = actual._claude_driver(Launch(SID), Prefs(model=row.ref), lambda _event: None)
    try:
        assert isinstance(driver, ClaudeDriver)
        assert driver._config.cli_version == FIXTURE_VERSION
        assert driver._config.observe_version is None
        assert (await driver.open()).cli_version == "2.1.259"
        assert actual._config.cli_version == FIXTURE_VERSION
        assert calls["app"]["readiness"].version == FIXTURE_VERSION
    finally:
        await driver.close()
    # A second route still passes admission after the legacy initialize reply.
    next_driver = actual._claude_driver(Launch(SID), Prefs(model=row.ref), lambda _event: None)
    await next_driver.close()
    assert not gateway._routes


def test_bot_mode_taken_port_preserves_scratch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "_listen", lambda *_args: None)
    monkeypatch.setattr(harness, "_scratch", _forbid)
    monkeypatch.setattr(harness.config, "load", _forbid)

    assert harness.main(["--bots", "--scratch", "--fixture", FIXTURE]) == 3
    assert "port 8765 is taken" in capsys.readouterr().err


def test_bots_team_bootstraps_the_team_kanban_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _assembly(monkeypatch)
    monkeypatch.setenv("VIKI_CLAUDE_CLI", sys.executable)
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", str(REPO / "tests" / "fake_claude.py"))

    assert harness.main(["--scratch", "--fixture", FIXTURE, "--bots", "--bots-team"]) == 0

    vault = harness.SCRATCH / "workspaces" / "vault"
    assert calls["active"].cwd == vault
    assert (vault / ".claude" / "skills" / "kanban" / "SKILL.md").is_file()
    assert (vault / ".claude" / "agents" / "checker.md").is_file()
    assert (vault / "kanban").is_dir()
    assert {skill.name for skill in (vault / ".claude" / "skills").iterdir()} == {
        target.split("/")[2] for target in starter.FILES if target.startswith(".claude/skills/")
    }
