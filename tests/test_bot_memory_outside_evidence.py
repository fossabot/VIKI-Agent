"""Two real runs of capture scenario 41 (a Bot memory folder outside the working folder), kept as
they ended: run 1 did not pass, run 2 passed. Observations of one Claude Code version on one
Windows machine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture  # noqa: E402
import pytest  # noqa: E402

from viki_agent.drivers.claude.control import Launch, build_argv  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "bot-memory-outside.json"
RUNS: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]
RUN1, RUN2 = RUNS


def _product_argv(tmp: Path, *, mode: str, resume: bool) -> list[str]:
    """One turn's launch, built by the product with a temp path and the capture's placeholders."""
    cli = tmp / "claude"
    settings = tmp / ("bot-memory-settings/" + "a" * 64 + ".json")
    launch = Launch("sid", resume=resume, bot_memory_settings=str(settings))
    placeholders = {
        str(cli): "<cli>",
        "--session-id=sid": "--session-id=<session-id>",
        "--resume=sid": "--resume=<session-id>",
        f"--settings={settings}": "--settings=<bot-memory-settings>",
    }
    argv = build_argv([str(cli)], launch, mode=mode, effort="low", model="sonnet")
    # The scenario records the permission mode (a fixed word) as well as flags and placeholders.
    return [
        a if a == mode or capture._ARGV_SHAPE.fullmatch(a) else "<redacted>"
        for a in (placeholders.get(a, a) for a in argv)
    ]


@pytest.mark.parametrize("run", RUNS)
def test_run_identity(run: dict[str, Any]) -> None:
    assert (run["scenario"], run["platform"]) == ("41", "win32")
    assert tuple(map(int, run["native_version"].split("."))) >= (2, 1, 281)
    assert set(run) == {"scenario", "platform", "native_version", "run", "observations", "turns"}


@pytest.mark.parametrize("run", RUNS)
def test_each_turn_argv_matches_the_product(run: dict[str, Any], tmp_path: Path) -> None:
    """Compared in full against build_argv with the capture's placeholders: one `--settings`, no
    `--add-dir`, `--mcp-config` or `--tools`. A launch-flag change makes this evidence stale."""
    for turn in run["turns"]:
        assert turn["argv"].count("--settings=<bot-memory-settings>") == 1
        assert not any(
            a.startswith(("--add-dir", "--mcp-config", "--tools", "--strict-mcp-config"))
            for a in turn["argv"]
        )
        assert turn["argv"] == _product_argv(tmp_path, mode=turn["mode"], resume=turn["resume"])


def test_run1_save_without_an_index_entry_was_not_recalled_by_a_new_chat() -> None:
    assert RUN1["observations"] == {
        "pass": False,
        "folder_absent_before": True,
        "folder_created": True,
        "saved_in_folder": True,
        "fresh_recalled": False,
        "resume_recalled_own": False,
        "turns_succeeded": True,
        "saved_in_working_folder": False,
        "resume_recalled_previous": True,
    }
    assert [(t["mode"], t["resume"], t["success"], t["asks"]) for t in RUN1["turns"]] == [
        ("acceptEdits", False, True, []),
        ("default", False, True, []),
        ("default", False, True, []),
        ("default", True, True, []),
    ]


def test_run2_index_entry_was_recalled_and_resume_read_its_own_folder() -> None:
    assert RUN2["observations"] == {
        "pass": True,
        "folder_absent_before": True,
        "folder_created": True,
        "saved_in_folder": True,
        "index_written": True,
        "fresh_recalled": True,
        "turns_succeeded": True,
        "saved_in_working_folder": False,
        "default_mode_save_landed": True,
        "default_mode_asked": False,
        "fresh_recalled_default_save": True,
        "resume_recalled_own": True,
        "resume_recalled_previous": True,
    }
    assert [(t["mode"], t["resume"], t["success"], t["asks"]) for t in RUN2["turns"]] == [
        ("acceptEdits", False, True, []),
        ("default", False, True, []),
        ("default", False, True, []),
        ("default", False, True, []),
        ("default", True, True, []),
    ]


def test_no_permission_request_was_seen_in_either_mode() -> None:
    modes = {t["mode"] for run in RUNS for t in run["turns"] if not t["asks"]}
    assert modes == {"acceptEdits", "default"}
    assert all(t["asks"] == [] for run in RUNS for t in run["turns"])
