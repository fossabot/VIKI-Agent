"""Acceptance (frozen): the launcher's helpers."""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path

from viki_agent import __main__ as launcher
from viki_agent import server


# AMENDMENT (2026-09-13): this pinned a
# VIKI_TEST_TOKEN hook in the real launcher: set the variable and the launch served that token
# instead of minting one. Nothing needed it — an end-to-end run gets its server from
# scripts/e2e_server.py, which reads the variable itself — and it survived winproc.scrub_env, so
# one stray export would have made a real launch serve on a constant published in this repo. The
# hook is gone and the test now pins its absence, which is the property the name always claimed.
def test_the_token_is_fresh_per_launch_and_no_variable_can_replace_it() -> None:
    first, second = launcher.token_from({}), launcher.token_from({})
    assert first != second and len(first) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)
    hooked = launcher.token_from({"VIKI_TEST_TOKEN": "e2e-test-token-not-a-secret"})
    assert hooked != "e2e-test-token-not-a-secret" and len(hooked) >= 43


def test_the_window_url_carries_the_token_in_the_fragment() -> None:
    assert launcher.app_url("t0k", dev=False) == "http://127.0.0.1:8765/#token=t0k"
    assert launcher.app_url("t0k", dev=True) == "http://127.0.0.1:5173/#token=t0k"


def test_edge_opens_a_dedicated_app_window() -> None:
    edge, profile = Path("C:/Edge/msedge.exe"), Path("C:/Data/edge")
    assert launcher.edge_argv(edge, profile, "http://127.0.0.1:8765/#token=t") == [
        str(edge),
        f"--user-data-dir={profile}",
        "--disable-extensions",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        "--app=http://127.0.0.1:8765/#token=t",
    ]


def test_vault_skills_are_directories_with_a_skill_file(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    for name in ("capture", "retro", "drafts"):
        (skills / name).mkdir(parents=True)
    for name in ("capture", "retro"):
        (skills / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (skills / "README.md").write_text("x", encoding="utf-8")
    assert launcher.vault_skills(tmp_path) == frozenset({"capture", "retro"})
    assert launcher.vault_skills(tmp_path / "missing") == frozenset()


def test_cli_version_is_the_first_version_number_printed() -> None:
    """AMENDMENT (2026-09-13): cli_version
    now takes the whole argv rather than the exe alone, so the version shown in the status bar
    comes from whatever actually runs the sessions — which, under an end-to-end run, is
    `python fake_claude.py` and not the exe by itself. The contract this test names is
    unchanged; only the argument widened, and the argv case below is new."""
    assert launcher.cli_version([sys.executable]) == platform.python_version()
    assert launcher.cli_version(["C:/no/such/claude.exe"]) == ""
    assert launcher.cli_version([sys.executable, "-X", "utf8"]) == platform.python_version()


def test_production_cli_ignores_test_environment() -> None:
    """AMENDMENT (2026-09-14): test variables cannot add a script to
    production argv, even together. The repository-only e2e assembly owns fake CLI selection."""
    cli = Path("C:/claude/claude.exe")
    assert launcher.cli_argv(cli, {}) == (str(cli),)
    assert launcher.cli_argv(cli, {"VIKI_TEST_CLI_SCRIPT": "fake.py"}) == (str(cli),)
    assert launcher.cli_argv(cli, {"VIKI_TEST_TOKEN": "t"}) == (str(cli),)
    assert launcher.cli_argv(cli, {"VIKI_TEST_CLI_SCRIPT": "fake.py", "VIKI_TEST_TOKEN": "t"}) == (
        str(cli),
    )


def test_websocket_frames_are_capped_at_one_mebibyte() -> None:
    assert server.WS_MAX_SIZE == 1 << 20
