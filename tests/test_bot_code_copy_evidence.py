"""Two real runs of capture scenario 40, kept as they ended. Neither passed: no run observed a
file written or a commit made in the Bot's code copy by the native process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture  # noqa: E402
import pytest  # noqa: E402

from viki_agent.drivers.claude.control import Launch, build_argv  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "bot-code-copy.json"
RUNS: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]
RUN1, RUN2 = RUNS
# Both runs observed these and nothing about a write or commit in the copy.
COMMON = {
    "native_login": True,
    "not_logged_in": False,
    "mcp_listed": True,
    "turn_success": True,
    "original_untouched": True,
    "hooks_off": True,
    "cwd_scratch": True,
    "add_dir_skill_listed": True,
    "pass": False,
    "exact_approved_once": False,
    "exact_command_only": False,
    "copy_file": False,
    "copy_commit": False,
    "synthetic_author": False,
}
RUN1_KEYS = {k: v for k, v in COMMON.items() if k != "exact_approved_once"}


def _product_argv(tmp: Path) -> list[str]:
    """Scenario 40's launch, built by the product with temp paths and its placeholder mapping.
    The capture's own `open` calls build_argv with mode default, effort low, model sonnet."""
    cli, settings, mcp, copy = (
        tmp / n
        for n in (
            "claude",
            "bot-memory-settings/" + "a" * 64 + ".json",
            "m.json",
            "x-worktrees/12345678-1234-4234-8234-123456789abc",
        )
    )
    launch = Launch(
        "sid", bot_memory_settings=str(settings), mcp_config=str(mcp), code_worktree=str(copy)
    )
    placeholders = {
        str(cli): "<cli>",
        "--session-id=sid": "--session-id=<session-id>",
        f"--add-dir={copy}": "--add-dir=<code-copy>",
        f"--settings={settings}": "--settings=<bot-memory-settings>",
        f"--mcp-config={mcp}": "--mcp-config=<app-mcp-config>",
    }
    argv = build_argv([str(cli)], launch, mode="default", effort="low", model="sonnet")
    redacted = (
        a if capture._ARGV_SHAPE.fullmatch(a) else "<redacted>"
        for a in (placeholders.get(a, a) for a in argv)
    )
    return list(redacted)


@pytest.mark.parametrize("run", RUNS)
def test_run_identity(run: dict[str, Any]) -> None:
    assert (run["scenario"], run["platform"]) == ("40", "linux")
    assert tuple(map(int, run["native_version"].split("."))) >= (2, 1, 281)
    assert run["synthetic_author"] == capture._GIT_AUTHOR


@pytest.mark.parametrize("run", RUNS)
def test_argv_carries_each_flag_once_and_matches_the_product(
    run: dict[str, Any], tmp_path: Path
) -> None:
    """Compared in full: every element, in order, against build_argv for the same launch with
    the capture's placeholders and redaction. A launch-flag change makes this evidence stale."""
    argv = run["argv"]
    for flag in ("--add-dir=<code-copy>", "--settings=<bot-memory-settings>"):
        assert argv.count(flag) == 1
    assert argv.count("--mcp-config=<app-mcp-config>") == 1
    assert sum(a.startswith("--mcp-config") for a in argv) == 1
    assert not any(a.startswith(("--tools", "--strict-mcp-config")) for a in argv)
    assert argv == _product_argv(tmp_path)


def test_run1_observed_memory_but_never_the_exact_command() -> None:
    """Run 1 (older summary shape): no file written and no commit made in the copy."""
    assert set(RUN1) == {
        "run",
        "scenario",
        "platform",
        "native_version",
        "synthetic_author",
        "argv",
        "observations",
    }
    assert RUN1["observations"] == {**RUN1_KEYS, "memory_loaded": True}
    assert RUN1["observations"]["pass"] is False


def test_run2_model_made_no_tool_call_so_nothing_was_written_or_committed() -> None:
    """Run 2 (current shape): no file written and no commit made in the copy; no tool call."""
    assert set(RUN2["observations"]) == set(capture._EVIDENCE_40)
    assert set(RUN2["diagnosis"]) == set(capture._DIAGNOSIS_40)
    assert RUN2["observations"] == {**COMMON, "memory_loaded": False}
    assert RUN2["diagnosis"] == {
        "asks": 0,
        "bash_asks": 0,
        "exact_asks": 0,
        "near_asks": 0,
        "script_asks": 0,
        "tool_calls": 0,
        "bash_calls": 0,
        "approved_errored": False,
        "note_exists": False,
        "commits_after_base": 0,
    }
