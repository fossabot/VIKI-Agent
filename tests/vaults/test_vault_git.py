from __future__ import annotations

import logging
from pathlib import Path

import pytest

from viki_agent import winproc
from viki_agent.vaults import vault_git
from viki_agent.vaults.vault_git import GIT_TIMEOUT_S, MAX_COMMITS, NoteHistory, note_history


def _git(cwd: Path, *args: str) -> None:
    """Repo setup only: winproc.git is the read-only path under test."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr


def _repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _commit(root: Path, name: str, body: str, subject: str) -> None:
    (root / name).write_text(body, encoding="utf-8")
    _git(root, "add", "--", name)
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", subject)


def test_a_three_commit_history_lists_newest_first(tmp_path: Path) -> None:
    root = _repo(tmp_path / "vault")
    for number in (1, 2, 3):
        _commit(root, "a.md", f"v{number}\n", f"edit {number}")
    result = note_history(root, "a.md")
    assert result.state == "ready"
    assert [commit.subject for commit in result.commits] == ["edit 3", "edit 2", "edit 1"]
    assert all(commit.short and "T" in commit.at for commit in result.commits)


def test_a_25_commit_history_is_clipped_to_20(tmp_path: Path) -> None:
    root = _repo(tmp_path / "vault")
    for number in range(25):
        _commit(root, "a.md", f"v{number}\n", f"edit {number}")
    result = note_history(root, "a.md")
    assert len(result.commits) == MAX_COMMITS == 20
    assert result.commits[0].subject == "edit 24"
    assert len(note_history(root, "a.md", limit=2).commits) == 2


def test_a_ref_named_dash_n_reaches_git_as_a_path(tmp_path: Path) -> None:
    root = _repo(tmp_path / "vault")
    _commit(root, "-n", "x\n", "the dash n note")
    _commit(root, "other.md", "y\n", "another note")
    result = note_history(root, "-n")
    assert result.state == "ready"
    assert [commit.subject for commit in result.commits] == ["the dash n note"]


def test_a_glob_ref_matches_nothing_while_a_literal_one_still_answers(tmp_path: Path) -> None:
    """`--literal-pathspecs` keeps `*`, `?` and `[...]` out of git's pathspec matcher, which
    `safe_ref` does not refuse, so a glob cannot report commits touching an excluded name."""
    root = _repo(tmp_path / "vault")
    (root / ".claude").mkdir()
    _commit(root, ".claude/settings.md", "s\n", "the private file")
    _commit(root, "top.md", "t\n", "the note")
    assert note_history(root, "*.md") == NoteHistory((), "ready")
    assert [commit.subject for commit in note_history(root, "top.md").commits] == ["the note"]


def test_a_malformed_line_is_dropped_and_a_long_subject_is_clipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_subject = "s" * 500
    lines = [
        "abc1234\x1f2026-01-02T03:04:05+09:00\x1fgood",
        "no separator",
        "a\x1fb",
        "a\x1fb\x1fc\x1fd",
    ]
    lines.append(f"def5678\x1f2026-01-01T00:00:00+09:00\x1f{long_subject}")
    monkeypatch.setattr(vault_git.winproc, "git", lambda *_a, **_k: "\n".join(lines) + "\n")
    result = note_history(tmp_path, "a.md")
    assert [commit.short for commit in result.commits] == ["abc1234", "def5678"]
    assert result.commits[-1].subject == "s" * 200
    assert result.state == "ready"


def test_a_folder_that_is_not_a_repository_answers_unknown(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    assert note_history(folder, "a.md") == NoteHistory((), "unknown")


def test_a_git_timeout_answers_unknown_under_the_five_second_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float] = []

    def slow_git(cwd: Path, *_args: str, timeout: float) -> str:
        timeouts.append(timeout)
        # A real timeout (winproc.run raises TimeoutExpired), shortened so the test stays fast.
        winproc.run(["git", "--version"], cwd=cwd, timeout=0.0001)
        raise AssertionError("git --version finished inside 0.1 ms")

    monkeypatch.setattr(vault_git.winproc, "git", slow_git)
    assert note_history(tmp_path, "a.md") == NoteHistory((), "unknown")
    assert timeouts == [GIT_TIMEOUT_S]


def test_a_git_scope_variable_in_the_environment_answers_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GIT_DIR` and its kin point Git at another repository, whose commits would read as this
    note's history; the chip and the identity probe stand down the same way."""
    root = _repo(tmp_path / "vault")
    _commit(root, "a.md", "mine\n", "the vault's own edit")
    other = _repo(tmp_path / "other")
    _commit(other, "a.md", "theirs\n", "another repository's edit")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert note_history(root, "a.md") == NoteHistory((), "unknown")


def test_a_failure_logs_the_subcommand_and_type_and_no_ref_or_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ref = "private-name-ZZZ.md"
    with caplog.at_level(logging.WARNING, logger="viki_agent.vaults.vault_git"):
        assert note_history(tmp_path, ref).state == "unknown"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message == "vault: git log failed (RuntimeError)"
    assert ref not in message and str(tmp_path) not in message
    assert ref not in caplog.text and tmp_path.name not in caplog.text
