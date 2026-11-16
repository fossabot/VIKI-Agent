"""What ClaudeDriver reports of Claude Code's own start: the facts a profile start is checked
against (`start_facts`) and the version the CLI says it runs.

Replies are the recorded 2.1.281 initialize and status replies (`mcp-inventory.jsonl`) or
synthetic frames; no native provider, transcript or vault.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from tests.profiles.test_inventory import FIXTURE, fixture_status, status_script
from viki_agent.drivers.base import StartFacts, StartFactsDriver
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.events import SessionStarted

from replay import DEFAULT_INIT, SID, Entry, FakeSpawner, expect, load_script, out

# The recorded 2.1.281 reply's model menu, in its order.
MENU = ("default", "opus[1m]", "claude-fable-5-1[1m]", "sonnet", "haiku")


def driver_for(
    script: list[Entry],
    tmp_path: Path,
    *,
    app_servers: bool = True,
    observe: Callable[[str], None] | None = None,
    mode: str = "auto",
) -> tuple[ClaudeDriver, FakeSpawner]:
    """A driver over the in-process fake, launch probe 2.1.281, requesting permission `mode`,
    with an app `--mcp-config` unless `app_servers` is False."""
    spawner = FakeSpawner(script)
    mcp = str((tmp_path / "mcp" / f"{'d' * 64}.json").absolute()) if app_servers else None
    config = DriverConfig(
        cli=("claude.exe",),
        vault=Path("C:/SyntheticVault"),
        env={},
        cli_version="2.1.281",
        spawner=spawner,
        grace_s=0.01,
        poll_s=0.01,
        control_timeout_s=0.05,
    )
    if observe is not None:
        config = replace(config, observe_version=observe)
    driver = ClaudeDriver(
        Launch(SID, mcp_config=mcp),
        mode=mode,
        effort="low",
        model=None,
        config=config,
        emit=lambda _event: None,
    )
    return driver, spawner


def recorded_initialize() -> list[Entry]:
    """The recorded initialize request and its 2.1.281 reply: the fixture's first two lines."""
    return load_script(FIXTURE)[:2]


def initialize(**reply: object) -> list[Entry]:
    """A synthetic initialize exchange whose reply is DEFAULT_INIT with `reply` on top."""
    request = {"subtype": "initialize"}
    response = {"subtype": "success", "request_id": "rec-init", "response": DEFAULT_INIT | reply}
    return [
        expect({"type": "control_request", "request_id": "rec-init", "request": request}),
        out({"type": "control_response", "response": response}),
    ]


def asked(spawner: FakeSpawner) -> list[str]:
    """The control requests the driver wrote, in order."""
    seen = spawner.children[0].process.replayer.seen
    return [f["request"]["subtype"] for f in seen if f.get("type") == "control_request"]


async def started_and_facts(driver: ClaudeDriver) -> tuple[SessionStarted, StartFacts]:
    try:
        return await driver.open(), await driver.start_facts()
    finally:
        await driver.close()


# --- start facts ---------------------------------------------------------------------------


async def test_start_facts_report_the_recorded_initialize_reply(tmp_path: Path) -> None:
    driver, spawner = driver_for(load_script(FIXTURE), tmp_path)
    assert isinstance(driver, StartFactsDriver)
    started, facts = await started_and_facts(driver)
    assert facts == StartFacts(version=None, mode="auto", models=MENU, mcp_servers=("viki_echo",))
    # 2.1.281 sends no claude_code_version, so the session keeps the launch probe's.
    assert started.cli_version == "2.1.281"
    assert asked(spawner) == ["initialize", "mcp_status"]


async def test_only_a_named_dynamic_server_is_reported(tmp_path: Path) -> None:
    echo = fixture_status()["mcpServers"][0]
    servers = [
        echo,
        {**echo, "name": "viki_plugin", "source": "plugin"},  # a plugin server's scope is dynamic
        {"name": "viki_unsourced", "status": "connected", "scope": "dynamic"},
        {**echo, "name": 3},
        "viki_text",
    ]
    status = status_script({"response": {"mcpServers": servers}})
    driver, _ = driver_for([*recorded_initialize(), *status], tmp_path)
    _, facts = await started_and_facts(driver)
    assert facts.mcp_servers == ("viki_echo",)


@pytest.mark.parametrize(
    ("status", "warnings"),
    [
        (status_script(None), ["MCP status request timed out"]),
        (status_script({"error": "no status"}, subtype="error"), []),
        (status_script({"response": {"mcpServers": "none"}}), []),
    ],
)
async def test_an_unreadable_status_reports_no_servers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, status: list[Entry], warnings: list[str]
) -> None:
    caplog.set_level(logging.WARNING)
    driver, _ = driver_for([*recorded_initialize(), *status], tmp_path)
    _, facts = await started_and_facts(driver)
    assert facts == StartFacts(version=None, mode="auto", models=MENU, mcp_servers=None)
    assert [record.getMessage() for record in caplog.records] == warnings


async def test_a_launch_without_app_servers_asks_for_no_status(tmp_path: Path) -> None:
    driver, spawner = driver_for(load_script(FIXTURE), tmp_path, app_servers=False)
    _, facts = await started_and_facts(driver)
    assert facts.mcp_servers is None
    assert asked(spawner) == ["initialize"]


async def test_the_reported_mode_is_kept_even_when_a_different_mode_was_requested(
    tmp_path: Path,
) -> None:
    # No test told the start facts' mode from the mode the app requested: driver_for always
    # launched with "auto" and the recorded reply also reports "auto".
    driver, _ = driver_for(load_script(FIXTURE), tmp_path, mode="default")
    _, facts = await started_and_facts(driver)
    assert facts.mode == "auto"


async def test_a_plan_launch_over_a_reply_with_no_reported_mode_reads_none(
    tmp_path: Path,
) -> None:
    driver, _ = driver_for(initialize(), tmp_path, app_servers=False, mode="plan")
    _, facts = await started_and_facts(driver)
    assert facts.mode is None


# --- the reported version ------------------------------------------------------------------


async def test_a_reported_version_names_the_session_and_is_observed_once(tmp_path: Path) -> None:
    observed: list[str] = []
    script = initialize(claude_code_version="2.1.304")
    driver, _ = driver_for(script, tmp_path, app_servers=False, observe=observed.append)
    started, facts = await started_and_facts(driver)
    assert started.cli_version == facts.version == "2.1.304"
    assert observed == ["2.1.304"]


@pytest.mark.parametrize(
    "reported",
    [
        "2.1.281",  # the launch probe's own version
        "2.1.304-beta",
        "v2.1.304",
        "2.1.304\n",
        " 2.1.304",
        "",
        "2",
        "2.1.3.0.4",
        "1234567.1",
        "\u0662.\u0661.\u0663\u0660\u0664",  # Arabic-Indic digits: not ASCII
        2.1,
        None,
    ],
)
async def test_an_equal_or_invalid_version_report_calls_no_observer(
    tmp_path: Path, reported: object
) -> None:
    observed: list[str] = []
    script = initialize(claude_code_version=reported)
    driver, _ = driver_for(script, tmp_path, app_servers=False, observe=observed.append)
    started, facts = await started_and_facts(driver)
    assert started.cli_version == "2.1.281" and observed == []
    assert facts.version == ("2.1.281" if reported == "2.1.281" else None)


async def test_a_failing_version_observer_does_not_fail_the_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    def fail(version: str) -> None:
        raise RuntimeError(version)

    script = initialize(claude_code_version="2.1.304")
    driver, _ = driver_for(script, tmp_path, app_servers=False, observe=fail)
    started, _ = await started_and_facts(driver)
    assert started.cli_version == "2.1.304"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "CLI version observer failed")
    ]
