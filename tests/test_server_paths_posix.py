"""Server, export, artifact and redaction paths: POSIX spellings, and the home-path redaction
that applies on every OS."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from viki_agent import server
from viki_agent.app.diagnostics import redact
from viki_agent.app.exports import denied_destination
from viki_agent.conversations.artifacts import _workspace_path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX path spellings")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see /home/alice/x", "see <user>/x"),
        ("see /var/home/alice/x", "see <user>/x"),
        ("see /Users/alice/x", "see <user>/x"),
        ('"/home/alice"', '"<user>"'),
        ("see /usr/share/x", "see /usr/share/x"),
        ("see /data/home/alice/x", "see /data/home/alice/x"),
    ],
)
def test_redact_generalizes_a_posix_home_on_every_os(text: str, expected: str) -> None:
    redacted, count = redact(text, roots=())
    assert redacted == expected
    assert count == (text != expected)


@posix_only
def test_vault_target_reads_a_backslash_as_a_separator(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    note = tmp_path / "30-knowledge" / "x.md"
    note.parent.mkdir()
    note.write_text("x", encoding="utf-8")
    assert server._vault_target(tmp_path, "30-knowledge\\x.md") == note.resolve()
    assert server._vault_target(tmp_path, "30-knowledge\\..\\..\\x.md") is None


@posix_only
def test_exports_deny_a_destination_under_a_denied_root_only(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    denied = tmp_path / "codex"
    assert denied_destination(denied, [denied]) is True
    assert denied_destination(denied / "sub" / "x.json", [denied]) is True
    assert denied_destination(tmp_path / "codex-sibling", [denied]) is False
    # The default macOS volume ignores case, so the deny check does too.
    assert denied_destination(tmp_path / "CODEX" / "x.json", [denied]) is (sys.platform == "darwin")


@posix_only
def test_an_absolute_posix_path_under_the_workspace_is_workspace_relative() -> None:
    assert sys.platform != "win32"
    assert _workspace_path("/w/a/b.md", "/w") == ("a/b.md", "workspace_relative")
    assert _workspace_path("a/b.md", "/w") == ("a/b.md", "workspace_relative")
    assert _workspace_path("/other/a.md", "/w") == ("", "unknown")
    assert _workspace_path("/w/.git/config", "/w") == ("", "unknown")
    assert _workspace_path("/w/a/../b.md", "/w") == ("", "unknown")
    assert _workspace_path("/w/a.md", "w") == ("", "unknown")
    expected = ("a.md", "workspace_relative") if sys.platform == "darwin" else ("", "unknown")
    assert _workspace_path("/W/a.md", "/w") == expected
