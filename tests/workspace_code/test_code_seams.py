"""Root-taking seams for review: `read_at`, `diff_at`, status records and Git answers."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at
from tests.workspace_code.test_code_diff import _subcommands
from tests.workspace_code.test_code_files import PRIVATE, REFUSED, code, link_dir
from tests.workspace_code.test_code_git import OID, _commit, _count_git, _git, _repo, needs_git
from viki_agent import osfs, winproc
from viki_agent.code import code_diff, code_files, code_git
from viki_agent.code.code_diff import MAX_DIFF_SOURCE_BYTES, FileDiff
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import ChangeEntry, CodeGit, parse_status_v2

ZERO = "0" * 40


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable and an empty home, so the user's Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


def _code_git(tmp_path: Path) -> CodeGit:
    service = service_at(tmp_path)
    return CodeGit(service, CodeFiles(service), git_available=lambda: True)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _checkout_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A folder with `a.txt` committed and a worktree of it whose `a.txt` differs."""
    folder = _repo(tmp_path / "repo")
    (folder / "a.txt").write_bytes(b"alpha\n")
    _commit(folder, "first")
    worktree = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c")) / "wt"
    _git(folder, "worktree", "add", "-q", "-b", "side", str(worktree), "HEAD")
    (worktree / "a.txt").write_bytes(b"beta\n")
    return folder, worktree


# --- the given checkout -----------------------------------------------------------------------


@needs_git
def test_read_at_reads_the_given_checkout(tmp_path: Path) -> None:
    folder, worktree = _checkout_pair(tmp_path)
    assert code_files.read_at(worktree, "a.txt").text == "beta\n"
    assert code_files.read_at(folder, "a.txt").text == "alpha\n"
    base, target = code_files.resolve_at(worktree, "a.txt", want="file")
    assert (base, target) == (worktree.resolve(), (worktree / "a.txt").resolve())


@needs_git
def test_diff_at_reads_the_given_checkout(tmp_path: Path) -> None:
    folder, worktree = _checkout_pair(tmp_path)
    git = _code_git(tmp_path)
    changed = code_diff.diff_at(git, worktree, "a.txt")
    assert changed.text is not None and "-alpha\n+beta\n" in changed.text
    assert code_diff.diff_at(git, folder, "a.txt") == FileDiff("a.txt", None, "no_diff")


# --- refs are checked before anything else ----------------------------------------------------


def test_refused_refs_reach_no_filesystem_and_no_git(tmp_path: Path) -> None:
    git = _code_git(tmp_path)
    root = tmp_path / "repo"

    def touched(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("a refused ref reached the filesystem or Git")

    cases = [(ref, "invalid_path") for ref in REFUSED] + [(ref, "private_path") for ref in PRIVATE]
    # The context undoes the patches before pytest reports a failure, which itself stats files.
    with pytest.MonkeyPatch.context() as patch:
        for owner, name in (
            (Path, "resolve"),
            (Path, "stat"),
            (Path, "lstat"),
            (os, "stat"),
            (os, "lstat"),
            (osfs, "read_regular"),
            (CodeGit, "git"),
        ):
            patch.setattr(owner, name, touched)
        for ref, expected in cases:
            assert code(code_files.resolve_at, root, ref, want="file") == expected, ref
            assert code(code_files.resolve_at, root, ref, want="dir") == expected, ref
            assert code(code_files.read_at, root, ref) == expected, ref
            assert code(code_diff.diff_at, git, root, ref) == expected, ref
            between = (OID, OID)
            assert code(code_diff.diff_at, git, root, ref, between=between) == expected, ref
        assert code(code_diff.diff_at, git, root, "") == "invalid_path"


@pytest.mark.parametrize(
    "between",
    [
        ("HEAD", OID),
        (OID, "HEAD"),
        (OID[:39], OID),
        (OID + "0", OID),
        (OID.upper(), OID),
        ("-" + OID[1:], OID),
    ],
)
def test_a_bad_between_is_a_value_error_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, between: tuple[str, str]
) -> None:
    git = _code_git(tmp_path)

    def touched(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("a bad between reached Git")

    monkeypatch.setattr(CodeGit, "git", touched)
    with pytest.raises(ValueError):
        code_diff.diff_at(git, tmp_path, "a.txt", between=between)


def test_a_link_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    link_dir(root / "out", outside)
    assert code(code_files.read_at, root, "out/secret.txt") == "invalid_path"
    assert code(code_files.resolve_at, root, "out", want="dir") == "invalid_path"


# --- diffs between two commits ----------------------------------------------------------------


@needs_git
def test_between_diffs_two_commits_and_reads_no_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    (root / "dir").mkdir()
    for name, body in (("a.txt", b"one\n"), ("same.txt", b"same\n"), ("dir/b.txt", b"b\n")):
        (root / name).write_bytes(body)
    (root / "blob.bin").write_bytes(b"\x00\x01")
    _commit(root, "base")
    base = _head(root)
    (root / "a.txt").write_bytes(b"two\n")
    (root / "new.txt").write_bytes(b"new\n")
    (root / "blob.bin").write_bytes(b"\x00\x02")
    _commit(root, "head")
    head = _head(root)
    (root / "a.txt").write_bytes(b"uncommitted\n")
    git = _code_git(tmp_path)

    def touched(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("a between diff read the worktree")

    monkeypatch.setattr(code_diff, "_worktree_size", touched)
    changed = code_diff.diff_at(git, root, "a.txt", between=(base, head))
    assert changed.text is not None and "-one\n+two\n" in changed.text
    assert "uncommitted" not in changed.text
    added = code_diff.diff_at(git, root, "new.txt", between=(base, head))
    assert added.text is not None and "new file mode" in added.text and "+new\n" in added.text
    assert code_diff.diff_at(git, root, "same.txt", between=(base, head)) == FileDiff(
        "same.txt", None, "no_diff"
    )
    assert code_diff.diff_at(git, root, "blob.bin", between=(base, head)) == FileDiff(
        "blob.bin", None, "binary"
    )
    assert code(code_diff.diff_at, git, root, "dir", between=(base, head)) == "not_a_file"


@needs_git
@pytest.mark.parametrize("side", ["base", "head"])
def test_between_a_side_over_the_limit_is_too_large_with_no_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    big, small = b"x" * (MAX_DIFF_SOURCE_BYTES + 1), b"small\n"
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_bytes(big if side == "base" else small)
    _commit(root, "base")
    base = _head(root)
    (root / "a.txt").write_bytes(small if side == "base" else big)
    _commit(root, "head")
    head = _head(root)
    git = _code_git(tmp_path)
    calls = _count_git(monkeypatch)
    result = code_diff.diff_at(git, root, "a.txt", between=(base, head))
    assert result == FileDiff("a.txt", None, "too_large")
    assert "cat-file" in _subcommands(calls) and "diff" not in _subcommands(calls)


# --- status records ---------------------------------------------------------------------------

# One `git status` output with every shape: a link, a gitlink, an unmerged path, a nested
# repository, a rename and a file removed from the index but left on disk.
SHAPES = (
    f"# branch.oid {OID}\0# branch.head main\0"
    f"1 A. N... 000000 120000 120000 {ZERO} {OID} link\0"
    f"1 .M S.M. 160000 160000 160000 {OID} {OID} vendor\0"
    f"u UU N... 100644 100644 100644 100644 {OID} {OID} {OID} both.txt\0"
    "? nested/\0"
    f"2 R. N... 100644 100644 100644 {OID} {OID} R100 new name.txt\0old name.txt\0"
    f"1 D. N... 100644 000000 000000 {OID} {ZERO} kept.txt\0"
    "? kept.txt\0"
)


def test_status_records_keep_every_field_before_the_path() -> None:
    headers, records = code_git.status_records(SHAPES)
    assert headers == {"branch.oid": OID, "branch.head": "main"}
    assert records == (
        code_git.StatusRecord(
            "1", ("1", "A.", "N...", "000000", "120000", "120000", ZERO, OID), "link", None
        ),
        code_git.StatusRecord(
            "1", ("1", ".M", "S.M.", "160000", "160000", "160000", OID, OID), "vendor", None
        ),
        code_git.StatusRecord(
            "u",
            ("u", "UU", "N...", "100644", "100644", "100644", "100644", OID, OID, OID),
            "both.txt",
            None,
        ),
        code_git.StatusRecord("?", ("?",), "nested/", None),
        code_git.StatusRecord(
            "2",
            ("2", "R.", "N...", "100644", "100644", "100644", OID, OID, "R100"),
            "new name.txt",
            "old name.txt",
        ),
        code_git.StatusRecord(
            "1", ("1", "D.", "N...", "100644", "000000", "000000", OID, ZERO), "kept.txt", None
        ),
        code_git.StatusRecord("?", ("?",), "kept.txt", None),
    )


def test_parse_status_v2_reads_the_same_shapes_as_before() -> None:
    changes = parse_status_v2(SHAPES)
    assert (changes.branch, changes.head, changes.truncated) == ("main", OID[:12], False)
    assert changes.entries == (
        ChangeEntry("link", None, "A", ".", "tracked", False),
        ChangeEntry("vendor", None, ".", "M", "submodule", False),
        ChangeEntry("both.txt", None, "U", "U", "unmerged", False),
        ChangeEntry("nested/", None, "?", "?", "untracked", False),
        ChangeEntry("new name.txt", "old name.txt", "R", ".", "tracked", False),
        ChangeEntry("kept.txt", None, "D", ".", "tracked", False),
        ChangeEntry("kept.txt", None, "?", "?", "untracked", False),
    )


def test_status_records_have_no_cap_and_refuse_a_malformed_output() -> None:
    many = "".join(f"? f{n}.txt\0" for n in range(code_git.MAX_CHANGES + 1))
    assert len(code_git.status_records(many)[1]) == code_git.MAX_CHANGES + 1
    assert code_git.status_records("") == ({}, ())
    for raw in (
        "? a.txt",  # unterminated
        "! ignored.txt\0",
        "?a.txt\0",
        "1 .M N...\0",
        f"1 .M Q... 100644 100644 100644 {OID} {OID} a.txt\0",
        f"2 R. N... 100644 100644 100644 {OID} {OID} R100 new.txt\0",  # no origPath
    ):
        with pytest.raises(ValueError):
            code_git.status_records(raw)


# --- Git answers ------------------------------------------------------------------------------


@needs_git
def test_answers_on_a_clean_new_and_detached_repository(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    git = _code_git(tmp_path)
    assert git.branch(root) == "main"
    assert code(git.head, root) == "no_commits"
    (root / "a.txt").write_bytes(b"a\n")
    _commit(root, "first")
    oid = _head(root)
    assert (git.head(root), git.branch(root)) == (oid, "main")
    assert git.output(root, "rev-parse", "HEAD") == f"{oid}\n"
    assert git.answer(root, "cat-file", "-t", "HEAD:a.txt") == "blob\n"
    _git(root, "switch", "-q", "--detach")
    assert (git.head(root), git.branch(root)) == (oid, None)


@needs_git
def test_a_refusal_is_none_or_git_failed_and_logs_no_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _repo(tmp_path / "secret-name-ZZZ")
    (root / "a.txt").write_bytes(b"a\n")
    _commit(root, "first")
    git = _code_git(tmp_path)
    assert git.answer(root, "cat-file", "-t", "HEAD:missing.txt") is None
    with caplog.at_level(logging.WARNING, logger="viki_agent.code.code_git"):
        assert code(git.output, root, "cat-file", "-t", "HEAD:missing.txt") == "git_failed"
    (record,) = caplog.records
    assert record.getMessage() == "code: git cat-file failed (RuntimeError)"
    assert root.name not in caplog.text


def test_a_timeout_is_git_unavailable_and_logs_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "secret-name-ZZZ"
    git = _code_git(tmp_path)

    def timing_out(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError(str(root))

    monkeypatch.setattr(winproc, "git", timing_out)
    for call, subcommand in (
        (lambda: git.output(root, "rev-parse", "HEAD"), "rev-parse"),
        (lambda: git.answer(root, "cat-file", "-t", "HEAD:a.txt"), "cat-file"),
        (lambda: git.head(root), "rev-parse"),
        (lambda: git.branch(root), "symbolic-ref"),
    ):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="viki_agent.code.code_git"):
            assert code(call) == "git_unavailable"
        (record,) = caplog.records
        assert record.getMessage() == f"code: git {subcommand} failed (TimeoutError)"
        assert root.name not in caplog.text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (f"{OID}\n", OID),
        ("ab" * 32 + "\n", "ab" * 32),
        ("nothex\n", "git_failed"),
        ("", "git_failed"),
        (f"{OID}\n{OID}\n", "git_failed"),
        (f"{OID.upper()}\n", "git_failed"),
    ],
)
def test_head_takes_exactly_one_full_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, expected: str
) -> None:
    git = _code_git(tmp_path)
    monkeypatch.setattr(winproc, "git", lambda *_a, **_k: answer)
    if expected == "git_failed":
        assert code(git.head, tmp_path) == expected
    else:
        assert git.head(tmp_path) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("refs/heads/feature/x\n", "feature/x"),
        ("refs/heads/\n", "git_failed"),
        ("refs/tags/v1\n", "git_failed"),
        ("refs/heads/a\nrefs/heads/b\n", "git_failed"),
        ("", "git_failed"),
    ],
)
def test_branch_takes_exactly_one_local_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, expected: str
) -> None:
    git = _code_git(tmp_path)
    monkeypatch.setattr(winproc, "git", lambda *_a, **_k: answer)
    if expected == "git_failed":
        assert code(git.branch, tmp_path) == expected
    else:
        assert git.branch(tmp_path) == expected


def test_output_passes_its_timeout_and_the_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _code_git(tmp_path)
    calls: list[tuple[tuple[str, ...], float]] = []

    def fake(_cwd: object, *args: str, timeout: float = 15.0) -> str:
        calls.append((args, timeout))
        return "ok\n"

    monkeypatch.setattr(winproc, "git", fake)
    assert git.output(tmp_path, "add", "--", "a.txt", timeout=30.0) == "ok\n"
    assert git.output(tmp_path, "status") == "ok\n"
    assert calls == [
        ((*code_git.HARDENING, "add", "--", "a.txt"), 30.0),
        ((*code_git.HARDENING, "status"), code_git.GIT_TIMEOUT_S),
    ]
