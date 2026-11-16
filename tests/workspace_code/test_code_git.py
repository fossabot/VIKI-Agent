"""The code-folder Git gate and changes list, all in `tmp_path`."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.app.test_workspaces import COMMAND, service_at
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import (
    GIT_TIMEOUT_S,
    HARDENING,
    MAX_CHANGES,
    ChangeEntry,
    Changes,
    CodeGit,
    parse_status_v2,
)
from viki_agent.workspaces import WorkspaceService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
OID = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _git(cwd: Path, *args: str) -> str:
    """Repo setup only: `CodeGit` is the path under test."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _setup(tmp_path: Path, root: Path, *, trust: bool = True) -> tuple[CodeGit, str]:
    service: WorkspaceService = service_at(tmp_path)
    if trust:
        workspace_id = trusted(service, root)
    else:
        workspace_id = service.register_existing(COMMAND, str(root), kind="code")[0].workspace_id
    return CodeGit(service, CodeFiles(service), git_available=lambda: True), workspace_id


def _count_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = winproc.git

    def counting(cwd: str | os.PathLike[str], *args: str, timeout: float = 15.0) -> str:
        calls.append(args)
        return real(cwd, *args, timeout=timeout)

    monkeypatch.setattr(winproc, "git", counting)
    return calls


def _fake_git(
    monkeypatch: pytest.MonkeyPatch, status: Callable[[], str]
) -> list[tuple[tuple[str, ...], float]]:
    """A repository top level whose `status` answers `status()`."""
    calls: list[tuple[tuple[str, ...], float]] = []

    def fake(_cwd: object, *args: str, timeout: float = 15.0) -> str:
        calls.append((args, timeout))
        return "true\n\n" if "rev-parse" in args else status()

    monkeypatch.setattr(winproc, "git", fake)
    return calls


def _refusal(call: Callable[[], object]) -> str:
    with pytest.raises(CodeError) as refused:
        call()
    return refused.value.code


# --- changes --------------------------------------------------------------------------------


@needs_git
def test_changes_list_every_kind_with_its_codes_branch_and_ahead_behind(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "bom.txt"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    (root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    _commit(root, "first")
    _git(root, "branch", "base")
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    _commit(root, "ahead")
    _git(root, "switch", "-q", "base")
    (root / "e.txt").write_text("e\n", encoding="utf-8")
    _commit(root, "behind")
    _git(root, "switch", "-q", "main")
    _git(root, "branch", "-q", "--set-upstream-to=base")

    (root / "a.txt").write_text("a3\n", encoding="utf-8")
    (root / "b.txt").write_text("b2\n", encoding="utf-8")
    _git(root, "add", "b.txt")
    _git(root, "mv", "c.txt", "c renamed.txt")
    (root / "d.txt").unlink()
    (root / "crlf.txt").write_bytes(b"one\r\nthree\r\n")
    (root / "bom.txt").write_bytes(b"\xef\xbb\xbfbom.txt\n")
    (root / "日本語 メモ.txt").write_text("メモ\n", encoding="utf-8")
    _git(root, "add", "日本語 メモ.txt")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    (root / ".env").write_text("SYNTHETIC=1\n", encoding="utf-8")

    git, workspace_id = _setup(tmp_path, root)
    changes = git.changes(workspace_id)

    assert changes.state == "ready" and changes.truncated is False
    assert (changes.branch, changes.detached, changes.upstream) == ("main", False, "base")
    assert changes.head == _git(root, "rev-parse", "HEAD").strip()[:12]
    assert (changes.ahead, changes.behind) == (1, 1)
    listed = {entry.path: entry for entry in changes.entries}
    assert {path: (e.index, e.worktree, e.kind) for path, e in listed.items()} == {
        "a.txt": (".", "M", "tracked"),
        "b.txt": ("M", ".", "tracked"),
        "c renamed.txt": ("R", ".", "tracked"),
        "d.txt": (".", "D", "tracked"),
        "crlf.txt": (".", "M", "tracked"),
        "bom.txt": (".", "M", "tracked"),
        "日本語 メモ.txt": ("A", ".", "tracked"),
        "new.txt": ("?", "?", "untracked"),
        ".env": ("?", "?", "untracked"),
    }
    assert listed["c renamed.txt"].orig_path == "c.txt"
    assert all(e.orig_path is None for path, e in listed.items() if path != "c renamed.txt")
    assert [path for path, e in listed.items() if e.private] == [".env"]


@needs_git
def test_a_submodule_is_kind_submodule(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "top.txt").write_text("top\n", encoding="utf-8")
    _commit(root, "top")
    inner = _repo(root / "sub")
    (inner / "x.txt").write_text("x\n", encoding="utf-8")
    _commit(inner, "inner")
    _git(root, "add", "sub")  # the nested repository becomes a gitlink

    git, workspace_id = _setup(tmp_path, root)
    (entry,) = git.changes(workspace_id).entries
    assert (entry.path, entry.index, entry.kind) == ("sub", "A", "submodule")


@needs_git
def test_a_new_repository_has_no_head_and_a_detached_one_no_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    git, workspace_id = _setup(tmp_path, root)
    empty = git.changes(workspace_id)
    assert (empty.state, empty.branch, empty.head, empty.entries) == ("ready", "main", None, ())
    assert (empty.upstream, empty.ahead, empty.behind) == (None, None, None)

    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(root, "first")
    _git(root, "switch", "-q", "--detach")
    detached = git.changes(workspace_id)
    assert (detached.branch, detached.detached) == (None, True)
    assert detached.head == _git(root, "rev-parse", "HEAD").strip()[:12]


@needs_git
def test_a_subfolder_root_is_not_repository_root(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "inner").mkdir()
    (root / "inner" / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(root, "first")
    git, workspace_id = _setup(tmp_path, root / "inner")
    assert _refusal(lambda: git.changes(workspace_id)) == "not_repository_root"


@needs_git
def test_a_plain_folder_is_not_a_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)
    assert _refusal(lambda: git.repository(workspace_id)) == "not_repository"


def test_a_first_line_other_than_true_is_not_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)
    monkeypatch.setattr(winproc, "git", lambda *_a, **_k: "false\n")
    assert _refusal(lambda: git.repository(workspace_id)) == "not_repository"


def test_a_rev_parse_failure_beside_a_dot_git_is_git_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    git, workspace_id = _setup(tmp_path, root)

    def failing(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("git rev-parse failed: fatal")

    monkeypatch.setattr(winproc, "git", failing)
    assert _refusal(lambda: git.repository(workspace_id)) == "git_unavailable"


def test_a_malformed_record_answers_unknown_with_no_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)
    calls = _fake_git(monkeypatch, lambda: "? good.txt\0" + "1 .M N...\0")
    assert git.changes(workspace_id) == Changes(state="unknown")
    # Every call carries the hardening before its subcommand, under the five-second cap.
    assert [args[: len(HARDENING) + 1] for args, _ in calls] == [
        (*HARDENING, "rev-parse"),
        (*HARDENING, "status"),
    ]
    assert {timeout for _, timeout in calls} == {GIT_TIMEOUT_S}


def test_over_2000_entries_are_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)
    _fake_git(monkeypatch, lambda: "".join(f"? f{n}.txt\0" for n in range(MAX_CHANGES + 1)))
    changes = git.changes(workspace_id)
    assert (changes.state, len(changes.entries), changes.truncated) == ("ready", MAX_CHANGES, True)
    assert changes.entries[-1].path == f"f{MAX_CHANGES - 1}.txt"


def test_a_git_scope_override_is_git_unavailable_with_no_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)
    calls = _count_git(monkeypatch)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert _refusal(lambda: git.changes(workspace_id)) == "git_unavailable"
    monkeypatch.delenv("GIT_DIR")
    missing = CodeGit(git._workspaces, git._files, git_available=lambda: False)
    assert _refusal(lambda: missing.changes(workspace_id)) == "git_unavailable"
    assert calls == []


@pytest.mark.parametrize("failure", ["runtime", "timeout"])
def test_a_status_failure_is_git_unavailable_and_logs_no_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    root = tmp_path / "secret-name-ZZZ"
    root.mkdir()
    git, workspace_id = _setup(tmp_path, root)

    def status() -> str:
        if failure == "runtime":
            raise RuntimeError(f"git status failed: fatal: {root}")
        # A real timeout (winproc.run raises TimeoutExpired), shortened so the test stays fast.
        winproc.run(["git", "--version"], cwd=root, timeout=0.0001)
        raise AssertionError("git --version finished inside 0.1 ms")

    _fake_git(monkeypatch, status)
    with caplog.at_level(logging.WARNING, logger="viki_agent.code.code_git"):
        assert _refusal(lambda: git.changes(workspace_id)) == "git_unavailable"
    (record,) = caplog.records
    expected = "RuntimeError" if failure == "runtime" else "TimeoutExpired"
    assert record.getMessage() == f"code: git status failed ({expected})"
    assert root.name not in caplog.text


# --- untrusted runs nothing -------------------------------------------------------------------


@needs_git
def test_an_untrusted_or_unknown_folder_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git, workspace_id = _setup(tmp_path, root, trust=False)
    calls = _count_git(monkeypatch)
    for call in (git.repository, git.changes):
        assert _refusal(lambda call=call: call(workspace_id)) == "trust_required"
        assert _refusal(lambda call=call: call("no-such-id")) == "unknown_workspace"
    (tmp_path / "notes").mkdir()
    vault = git._workspaces.register_existing(COMMAND, str(tmp_path / "notes"))[0]
    assert _refusal(lambda: git.changes(vault.workspace_id)) == "unknown_workspace"
    assert calls == []
    # The reader still answers.
    assert [entry.name for entry in git._files.tree(workspace_id).entries] == ["a.txt"]


# --- repository config runs nothing -----------------------------------------------------------


@needs_git
def test_fsmonitor_hook_never_runs(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(root, "first")
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    marker = tmp_path / "hook-ran"
    hook = f"\"echo ran > '{marker.as_posix()}'\""
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(
            f"[core]\n\tfsmonitor = {hook}\n"
            f"[diff]\n\texternal = {hook}\n"
            f'[diff "marker"]\n\ttextconv = {hook}\n'
        )
    (root / ".gitattributes").write_text("* diff=marker\n", encoding="utf-8")

    git, workspace_id = _setup(tmp_path, root)
    changes = git.changes(workspace_id)
    assert changes.state == "ready"
    assert {entry.path for entry in changes.entries} == {"a.txt", ".gitattributes"}
    assert not marker.exists()

    # The hook is live: a status without the hardening runs it.
    winproc.git(root, "status", "--porcelain")
    assert marker.exists()


# --- parse_status_v2 --------------------------------------------------------------------------


def test_parse_reads_headers_and_every_record_shape() -> None:
    raw = (
        f"# branch.oid {OID}\0# branch.head topic\0# branch.upstream origin/topic\0"
        "# branch.ab +2 -0\0# stash 3\0"
        f"1 .M N... 100644 100644 100644 {OID} {OID} dir/a b.txt\0"
        f"2 R. N... 100644 100644 100644 {OID} {OID} R100 new name.txt\0old name.txt\0"
        f"u UU N... 100644 100644 100644 100644 {OID} {OID} {OID} both.txt\0"
        f"1 .M S.M. 160000 160000 160000 {OID} {OID} vendor\0"
        "? keys/server.pem\0"
    )
    assert parse_status_v2(raw) == Changes(
        state="ready",
        branch="topic",
        head=OID[:12],
        detached=False,
        upstream="origin/topic",
        ahead=2,
        behind=0,
        entries=(
            ChangeEntry("dir/a b.txt", None, ".", "M", "tracked", False),
            ChangeEntry("new name.txt", "old name.txt", "R", ".", "tracked", False),
            ChangeEntry("both.txt", None, "U", "U", "unmerged", False),
            ChangeEntry("vendor", None, ".", "M", "submodule", False),
            ChangeEntry("keys/server.pem", None, "?", "?", "untracked", True),
        ),
        truncated=False,
    )


def test_parse_marks_a_rename_from_a_private_name_private() -> None:
    raw = f"2 R. N... 100644 100644 100644 {OID} {OID} R90 notes.txt\0.env\0"
    (entry,) = parse_status_v2(raw).entries
    assert (entry.orig_path, entry.private) == (".env", True)


def test_parse_reads_detached_initial_and_empty_output() -> None:
    detached = parse_status_v2("# branch.oid (initial)\0# branch.head (detached)\0")
    assert (detached.branch, detached.head, detached.detached) == (None, None, True)
    assert parse_status_v2("") == Changes(state="ready")


@pytest.mark.parametrize(
    "raw",
    [
        "? a.txt",  # unterminated
        "! ignored.txt\0",
        "x\0",
        "?a.txt\0",
        "? \0",
        "1 .M N...\0",
        f"1 .X N... 100644 100644 100644 {OID} {OID} a.txt\0",
        f"1 .M Q... 100644 100644 100644 {OID} {OID} a.txt\0",
        f"2 R. N... 100644 100644 100644 {OID} {OID} R100 new.txt\0",  # no origPath
        f"2 R. N... 100644 100644 100644 {OID} {OID} X100 new.txt\0old.txt\0",
        f"u UU N... 100644 100644 100644 100644 {OID} {OID} both.txt\0",
        "# branch.ab ahead\0",
        "# branch.oid nothex\0",
    ],
)
def test_parse_refuses_a_malformed_output(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_status_v2(raw)


def test_parse_checks_records_past_the_cap() -> None:
    raw = "".join(f"? f{n}.txt\0" for n in range(MAX_CHANGES + 1)) + "! late\0"
    with pytest.raises(ValueError):
        parse_status_v2(raw)
