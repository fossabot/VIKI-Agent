"""The code-folder file diff, all in `tmp_path`."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

from tests.workspace_code.test_code_files import link_dir
from tests.workspace_code.test_code_git import (
    _commit,
    _count_git,
    _git,
    _refusal,
    _repo,
    _setup,
    needs_git,
)
from viki_agent import winproc
from viki_agent.code import code_diff
from viki_agent.code.code_diff import MAX_DIFF_SOURCE_BYTES, FileDiff, diff
from viki_agent.code.code_git import HARDENING, CodeGit


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _diff(git: CodeGit, workspace_id: str, ref: str) -> FileDiff:
    return diff(git, git._files, workspace_id, ref)


def _subcommands(calls: list[tuple[str, ...]]) -> list[str]:
    return [args[len(HARDENING)] for args in calls]


def _committed(tmp_path: Path, name: str, content: bytes) -> tuple[Path, CodeGit, str]:
    root = _repo(tmp_path / "repo")
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _commit(root, "first")
    git, workspace_id = _setup(tmp_path, root)
    return root, git, workspace_id


# --- diff --------------------------------------------------------------------------------------


@needs_git
def test_a_modified_file_diffs_against_head(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(tmp_path, "src dir/a b.txt", b"one\ntwo\n")
    (root / "src dir" / "a b.txt").write_bytes(b"one\nthree\n")
    result = _diff(git, workspace_id, "src dir/a b.txt")
    assert (result.path, result.reason) == ("src dir/a b.txt", None)
    assert result.text is not None
    assert "-two\n+three\n" in result.text and " one\n" in result.text


@needs_git
def test_a_staged_change_and_a_deleted_file_both_diff_against_head(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(tmp_path, "a.txt", b"a\n")
    (root / "gone.txt").write_bytes(b"gone\n")
    _commit(root, "second")
    (root / "a.txt").write_bytes(b"a2\n")
    _git(root, "add", "a.txt")
    (root / "gone.txt").unlink()
    staged = _diff(git, workspace_id, "a.txt").text
    deleted = _diff(git, workspace_id, "gone.txt").text
    assert staged is not None and "+a2" in staged
    assert deleted is not None and "-gone" in deleted


@needs_git
def test_a_binary_file_is_binary_with_no_text(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(tmp_path, "blob.bin", b"\x00\x01\x02")
    (root / "blob.bin").write_bytes(b"\x00\x01\x03")
    assert _diff(git, workspace_id, "blob.bin") == FileDiff("blob.bin", None, "binary")


@needs_git
def test_an_unchanged_or_untracked_file_is_no_diff(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(tmp_path, "a.txt", b"a\n")
    (root / "new.txt").write_bytes(b"new\n")  # absent from HEAD, so `git diff HEAD` is empty
    for ref in ("a.txt", "new.txt", "missing.txt"):
        assert _diff(git, workspace_id, ref) == FileDiff(ref, None, "no_diff")


@needs_git
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("tracked", [False, True])
def test_a_parent_link_refuses_outside_and_private_targets_before_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private: bool, missing: bool, tracked: bool
) -> None:
    root, git, workspace_id = _committed(tmp_path, "dir/a.txt", b"baseline\n")
    (root / "dir").rename(tmp_path / "original-dir")
    target = root / ".ssh" if private else tmp_path / "outside"
    target.mkdir()
    name = "a.txt" if tracked else "untracked.txt"
    if not missing:
        (target / name).write_bytes(b"outside synthetic sentinel\n")
    link_dir(root / "dir", target)
    calls = _count_git(monkeypatch)

    assert _refusal(lambda: _diff(git, workspace_id, f"dir/{name}")) == (
        "private_path" if private else "invalid_path"
    )
    assert "diff" not in _subcommands(calls)


@needs_git
def test_an_inside_parent_link_keeps_the_ref_and_a_missing_parent_keeps_the_deletion(
    tmp_path: Path,
) -> None:
    root, git, workspace_id = _committed(tmp_path, "dir/a.txt", b"baseline\n")
    target = root / "target"
    (root / "dir").rename(target)
    (target / "a.txt").write_bytes(b"inside changed\n")
    link_dir(root / "dir", target)
    shown = _diff(git, workspace_id, "dir/a.txt")
    assert shown.path == "dir/a.txt" and shown.reason is None
    assert shown.text is not None
    if os.name == "nt":
        assert "--- a/dir/a.txt\n+++ b/dir/a.txt" in shown.text
        assert "-baseline\n+inside changed\n" in shown.text
    else:
        # Git treats a tracked child below a POSIX symlink as deleted; the original ref stays.
        assert shown.text.startswith(
            "diff --git a/dir/a.txt b/dir/a.txt\ndeleted file mode 100644\nindex "
        )
        assert shown.text.endswith("--- a/dir/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-baseline\n")
        assert "+inside changed\n" not in shown.text

    if os.name == "nt":
        (root / "dir").rmdir()
    else:
        (root / "dir").unlink()
    deleted = _diff(git, workspace_id, "dir/a.txt")
    assert deleted.text is not None and "-baseline\n" in deleted.text


@needs_git
def test_historical_diff_does_not_resolve_the_worktree_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = _committed(tmp_path, "dir/a.txt", b"baseline\n")
    base = git.head(root)
    (root / "dir" / "a.txt").write_bytes(b"committed change\n")
    _commit(root, "second")
    head = git.head(root)
    (root / "dir").rename(tmp_path / "original-dir")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_bytes(b"outside synthetic sentinel\n")
    link_dir(root / "dir", outside)
    assert _refusal(lambda: _diff(git, workspace_id, "dir/a.txt")) == "invalid_path"

    def touched(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a historical diff resolved the worktree")

    monkeypatch.setattr(code_diff, "resolve_path_at", touched)
    monkeypatch.setattr(code_diff, "_worktree_size", touched)
    shown = code_diff.diff_at(git, root, "dir/a.txt", between=(base, head))
    assert shown.text is not None and "-baseline\n+committed change\n" in shown.text
    assert "outside synthetic sentinel" not in shown.text


@needs_git
def test_a_repository_with_no_commits_is_no_commits(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_bytes(b"a\n")
    git, workspace_id = _setup(tmp_path, root)
    assert _diff(git, workspace_id, "a.txt") == FileDiff("a.txt", None, "no_commits")


@needs_git
def test_a_private_or_malformed_ref_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = _committed(tmp_path, "a.txt", b"a\n")
    calls = _count_git(monkeypatch)
    for ref in (".env", "keys/server.pem", "dir/.ENV"):
        assert _refusal(lambda ref=ref: _diff(git, workspace_id, ref)) == "private_path"
    for ref in ("", "../a.txt", "a//b", "C:/a.txt"):
        assert _refusal(lambda ref=ref: _diff(git, workspace_id, ref)) == "invalid_path"
    assert calls == []


@needs_git
def test_an_untrusted_folder_runs_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_bytes(b"a\n")
    git, workspace_id = _setup(tmp_path, root, trust=False)
    calls = _count_git(monkeypatch)
    assert _refusal(lambda: _diff(git, workspace_id, "a.txt")) == "trust_required"
    assert calls == []


@needs_git
@pytest.mark.parametrize("side", ["worktree", "head"])
def test_too_large_never_runs_git_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    big, small = b"x" * (MAX_DIFF_SOURCE_BYTES + 1), b"small\n"
    root, git, workspace_id = _committed(tmp_path, "a.txt", big if side == "head" else small)
    (root / "a.txt").write_bytes(small if side == "head" else big)
    calls = _count_git(monkeypatch)
    assert _diff(git, workspace_id, "a.txt") == FileDiff("a.txt", None, "too_large")
    # The head side is found by `cat-file`; the worktree side before any probe.
    assert "diff" not in _subcommands(calls)
    assert ("cat-file" in _subcommands(calls)) is (side == "head")


@needs_git
def test_a_file_of_exactly_the_limit_on_both_sides_still_diffs(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(
        tmp_path, "a.txt", b"a" * (MAX_DIFF_SOURCE_BYTES - 1) + b"\n"
    )
    (root / "a.txt").write_bytes(b"b" * (MAX_DIFF_SOURCE_BYTES - 1) + b"\n")
    result = _diff(git, workspace_id, "a.txt")
    assert (result.reason, result.text is not None) == (None, True)


@needs_git
def test_a_glob_shaped_ref_is_literal_and_never_reaches_a_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = _committed(tmp_path, "a.txt", b"a\n")
    (root / ".env").write_bytes(b"SYNTHETIC=1\n")
    _commit(root, "env")
    (root / "a.txt").write_bytes(b"a2\n")
    (root / ".env").write_bytes(b"SYNTHETIC=2\n")
    calls = _count_git(monkeypatch)
    for ref in ("*.env", ".en?", "[.]env", "*"):
        calls.clear()
        assert _diff(git, workspace_id, ref) == FileDiff(ref, None, "no_diff")
        (diff_call,) = (args for args in calls if args[len(HARDENING)] == "diff")
        assert diff_call[: len(HARDENING)] == HARDENING
        assert diff_call[-2:] == ("--", ref)


@needs_git
def test_a_folder_is_not_a_file_and_runs_no_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = _committed(tmp_path, "dir/a.txt", b"a\n")
    calls = _count_git(monkeypatch)
    assert _refusal(lambda: _diff(git, workspace_id, "dir")) == "not_a_file"
    assert "diff" not in _subcommands(calls)


@needs_git
def test_a_folder_gone_from_the_worktree_is_not_a_file_and_runs_no_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = _committed(tmp_path, "dir/a.txt", b"a\n")
    shutil.rmtree(root / "dir")
    calls = _count_git(monkeypatch)
    assert _refusal(lambda: _diff(git, workspace_id, "dir")) == "not_a_file"
    assert "diff" not in _subcommands(calls)


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failing", "kind"),
    [("rev-parse", "timeout"), ("cat-file", "timeout"), ("diff", "runtime"), ("diff", "timeout")],
)
def test_a_git_failure_is_git_unavailable_and_logs_no_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    kind: str,
) -> None:
    root = tmp_path / "secret-name-ZZZ"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)

    def fake(_cwd: object, *args: str, timeout: float = 15.0) -> str:
        subcommand = args[len(HARDENING)]
        if subcommand == "rev-parse" and "--show-prefix" in args:
            return "true\n\n"
        if subcommand != failing:
            return "blob\n" if "-t" in args else "1\n"
        if kind == "runtime":
            raise RuntimeError(f"git {subcommand} failed: fatal: {root}")
        # A real timeout (winproc.run raises TimeoutExpired), shortened so the test stays fast.
        winproc.run(["git", "--version"], cwd=root, timeout=0.0001)
        raise AssertionError("git --version finished inside 0.1 ms")

    monkeypatch.setattr(winproc, "git", fake)
    with caplog.at_level(logging.WARNING, logger="viki_agent.code.code_diff"):
        assert _refusal(lambda: _diff(git, workspace_id, "a.txt")) == "git_unavailable"
    (record,) = caplog.records
    expected = "RuntimeError" if kind == "runtime" else "TimeoutExpired"
    assert record.getMessage() == f"code: git {failing} failed ({expected})"
    assert root.name not in caplog.text


# --- repository config runs nothing -----------------------------------------------------------


@needs_git
def test_textconv_external_diff_and_fsmonitor_never_run(tmp_path: Path) -> None:
    root, git, workspace_id = _committed(tmp_path, "a.txt", b"a\n")
    (root / "a.txt").write_bytes(b"a2\n")
    markers = {name: tmp_path / f"{name}-ran" for name in ("fsmonitor", "external", "textconv")}

    def hook(name: str) -> str:
        return f"\"echo ran > '{markers[name].as_posix()}'\""

    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(
            f"[core]\n\tfsmonitor = {hook('fsmonitor')}\n"
            f"[diff]\n\texternal = {hook('external')}\n"
            f'[diff "marker"]\n\ttextconv = {hook("textconv")}\n'
        )
    (root / ".gitattributes").write_text("* diff=marker\n", encoding="utf-8")

    result = _diff(git, workspace_id, "a.txt")
    assert result.text is not None and "+a2" in result.text
    assert [name for name, marker in markers.items() if marker.exists()] == []

    # Each hook is live: Git without the hardening and the flags runs it.
    winproc.git(root, "status", "--porcelain")
    winproc.git(root, "diff", "HEAD", "--", "a.txt")
    winproc.git(root, "diff", "--no-ext-diff", "HEAD", "--", "a.txt")
    assert [name for name, marker in markers.items() if not marker.exists()] == []
