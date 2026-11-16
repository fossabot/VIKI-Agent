"""Checks and terminal launches are admitted on each supported desktop platform."""

from __future__ import annotations

import sys

import pytest

from viki_agent.code import code_checks, terminal_launch


@pytest.mark.skipif(sys.platform not in {"win32", "linux", "darwin"}, reason="desktop platforms")
def test_checks_and_terminal_are_supported_on_this_desktop() -> None:
    assert code_checks.CHECKS_SUPPORTED
    assert terminal_launch.TERMINAL_SUPPORTED
