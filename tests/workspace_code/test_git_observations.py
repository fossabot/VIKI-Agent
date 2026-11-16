"""Synthetic Git observations: a failed lookup must never certify a clean checkout."""

from __future__ import annotations

from pathlib import Path

import pytest

from viki_agent import rails, winproc


def _git(cwd: Path, *args: str) -> str:
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(vault: Path) -> None:
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")


def test_unborn_and_detached_are_observed_without_treating_missing_head_as_status_failure(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _repo(vault)
    unborn = rails.git_chip(vault)
    assert (unborn.state, unborn.ref_state, unborn.branch, unborn.head, unborn.dirty) == (
        "known",
        "branch",
        "main",
        "",
        0,
    )
    (vault / "note.md").write_text("hello", encoding="utf-8")
    assert rails.git_chip(vault).dirty == 1
    _git(vault, "add", "note.md")
    _git(vault, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
    _git(vault, "checkout", "-q", "--detach", "HEAD")
    detached = rails.git_chip(vault)
    assert detached.state == "known" and detached.ref_state == "detached"
    assert detached.branch == "" and detached.head and detached.committed_at
    assert detached.dirty == 0


def test_nonrepository_needs_git_and_marker_free_ancestry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    chip = rails.git_chip(vault)
    assert chip.state == "not_repository" and chip.dirty is None
    assert chip.ref_state == "unknown" and not chip.branch and not chip.head


def test_nested_folder_in_parent_repo_is_not_classified_as_nonrepository(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    _repo(parent)
    vault = parent / "vault"
    vault.mkdir()
    chip = rails.git_chip(vault)
    assert chip.state == "known" and chip.ref_state == "branch"
    assert chip.branch == "main" and chip.dirty == 0


def test_worktree_git_file_is_not_mistaken_for_marker_absence(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _repo(primary)
    (primary / "note.md").write_text("hello", encoding="utf-8")
    _git(primary, "add", "note.md")
    _git(primary, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    assert (linked / ".git").is_file()
    chip = rails.git_chip(linked)
    assert chip.state == "known" and chip.branch == "linked" and chip.dirty == 0


def test_failed_commands_with_repo_marker_remain_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _repo(vault)

    def broken_git(_cwd: Path, *_args: str, timeout: float = 15.0) -> str:
        raise TimeoutError("synthetic")

    monkeypatch.setattr(rails.winproc, "git", broken_git)
    chip = rails.git_chip(vault)
    assert chip.state == "unknown" and chip.dirty is None and chip.ref_state == "unknown"


def test_parent_and_worktree_markers_prevent_false_nonrepository_after_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    _repo(parent)
    nested = parent / "vault"
    nested.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../missing", encoding="utf-8")

    def failed_git(_cwd: Path, *_args: str, timeout: float = 15.0) -> str:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(rails.winproc, "git", failed_git)
    for vault in (nested, linked):
        chip = rails.git_chip(vault)
        assert chip.state == "unknown" and chip.dirty is None


def test_missing_git_cannot_prove_nonrepository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []

    def missing_git(_cwd: Path, *args: str, timeout: float = 15.0) -> str:
        calls.append(args[0])
        raise FileNotFoundError("synthetic")

    monkeypatch.setattr(rails.winproc, "git", missing_git)
    chip = rails.git_chip(vault)
    assert chip.state == "unknown" and chip.dirty is None
    assert calls == ["branch", "rev-parse", "log", "status", "--version"]


def test_git_environment_override_prevents_nonrepository_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("GIT_DIR", "synthetic-other-repository")
    calls: list[str] = []

    def misleading_git(_cwd: Path, *args: str, timeout: float = 15.0) -> str:
        calls.append(args[0])
        return {
            "branch": "main",
            "rev-parse": "abc1234",
            "log": "2026-09-14T00:00:00+09:00",
            "status": "",
        }[args[0]]

    monkeypatch.setattr(rails.winproc, "git", misleading_git)
    chip = rails.git_chip(vault)
    assert chip.state == "unknown" and chip.dirty is None
    assert chip.branch == "" and chip.head == "" and chip.committed_at == ""
    assert calls == []  # even a successful clean result could describe another repository


def test_independent_head_fields_survive_status_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _repo(vault)
    (vault / "note.md").write_text("hello", encoding="utf-8")
    _git(vault, "add", "note.md")
    _git(vault, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
    real_git = winproc.git

    def failed_status(cwd: Path, *args: str, timeout: float = 15.0) -> str:
        if args[0] == "status":
            raise TimeoutError("synthetic")
        return real_git(cwd, *args, timeout=timeout)

    monkeypatch.setattr(rails.winproc, "git", failed_status)
    chip = rails.git_chip(vault)
    assert chip.state == "unknown" and chip.dirty is None
    assert chip.ref_state == "branch" and chip.branch == "main" and chip.head and chip.committed_at
