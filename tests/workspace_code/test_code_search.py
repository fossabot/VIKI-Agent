"""Quick open, over real repositories in `tmp_path`."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

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
from viki_agent.code import code_search
from viki_agent.code.code_search import MAX_HITS, MAX_QUERY_CHARS, SearchResult, search


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _tracked(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n", encoding="utf-8")
    _commit(root, "files")


@needs_git
def test_tokens_match_across_directories_and_case_and_basename_hits_come_first(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(
        root,
        "main.py",
        "src/app/main.py",
        "src/MAIN_helpers.py",
        "main/util.py",
        "docs/main-notes.md",
        "a/mm.py",
        "b/mm.py",
    )
    git, workspace_id = _setup(tmp_path, root)

    # Every token must be in the path; a path with every token in its basename ranks first,
    # then the shorter path.
    assert search(git, workspace_id, "main py") == SearchResult(
        ("main.py", "src/app/main.py", "src/MAIN_helpers.py", "main/util.py"), False
    )
    # Tokens may sit in different path parts, in any case.
    assert search(git, workspace_id, "  SRC  helpers ").hits == ("src/MAIN_helpers.py",)
    # Equal rank and length fall back to the path.
    assert search(git, workspace_id, "mm").hits == ("a/mm.py", "b/mm.py")
    assert search(git, workspace_id, "no-such-name") == SearchResult((), False)


@needs_git
def test_at_most_50_hits_and_truncated_only_when_more(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, *(f"f{n:02}.txt" for n in range(MAX_HITS + 1)))
    git, workspace_id = _setup(tmp_path, root)

    over = search(git, workspace_id, "f")
    assert (len(over.hits), over.truncated) == (MAX_HITS, True)
    assert over.hits == tuple(f"f{n:02}.txt" for n in range(MAX_HITS))

    _git(root, "rm", "-q", "--cached", "f50.txt")
    assert search(git, workspace_id, "f") == SearchResult(over.hits, False)


@needs_git
def test_private_paths_are_never_hits(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(
        root,
        "config/app.toml",
        "config/.env",
        "keys/id_rsa",
        "keys/ok.txt",
        "certs/a.pem",
        ".ssh/config",
    )
    git, workspace_id = _setup(tmp_path, root)

    assert search(git, workspace_id, "config").hits == ("config/app.toml",)
    assert search(git, workspace_id, "keys").hits == ("keys/ok.txt",)
    for query in (".env", "id_rsa", "a.pem", "ssh", "certs"):
        assert search(git, workspace_id, query).hits == (), query


@needs_git
def test_a_japanese_file_name_matches(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "日本語 メモ.txt", "docs/読書メモ.md", "other.txt")
    git, workspace_id = _setup(tmp_path, root)

    assert search(git, workspace_id, "メモ").hits == ("日本語 メモ.txt", "docs/読書メモ.md")
    assert search(git, workspace_id, "日本語 txt").hits == ("日本語 メモ.txt",)


@needs_git
def test_a_repository_with_no_index_has_no_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    git, workspace_id = _setup(tmp_path, root)
    calls = _count_git(monkeypatch)
    assert search(git, workspace_id, "a") == SearchResult((), False)
    assert not any("ls-files" in args for args in calls)


@needs_git
def test_a_linked_worktree_reads_its_own_index(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "main.txt")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(linked), "-b", "topic")
    (linked / "extra.txt").write_text("x\n", encoding="utf-8")
    _git(linked, "add", "extra.txt")
    git, workspace_id = _setup(tmp_path, linked)

    assert search(git, workspace_id, "txt").hits == ("main.txt", "extra.txt")


@needs_git
def test_an_index_over_the_cap_is_too_large_with_no_ls_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "a.txt")
    git, workspace_id = _setup(tmp_path, root)
    index_size = (root / ".git" / "index").stat().st_size
    calls = _count_git(monkeypatch)

    monkeypatch.setattr(code_search, "MAX_INDEX_BYTES", index_size - 1)
    assert _refusal(lambda: search(git, workspace_id, "a")) == "too_large"
    assert not any("ls-files" in args for args in calls)

    monkeypatch.setattr(code_search, "MAX_INDEX_BYTES", index_size)
    assert search(git, workspace_id, "a").hits == ("a.txt",)
    assert any("ls-files" in args for args in calls)


@needs_git
def test_a_split_index_counts_its_shared_index_against_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    _git(root, "config", "core.splitIndex", "true")
    _tracked(root, *(f"dir/file{n:03}.txt" for n in range(200)))
    _git(root, "update-index", "--split-index")
    git, workspace_id = _setup(tmp_path, root)
    stub_size = (root / ".git" / "index").stat().st_size
    shared = list((root / ".git").glob("sharedindex.*"))
    assert shared
    shared_size = max(path.stat().st_size for path in shared)
    assert shared_size > stub_size
    calls = _count_git(monkeypatch)

    monkeypatch.setattr(code_search, "MAX_INDEX_BYTES", stub_size + shared_size // 2)
    assert _refusal(lambda: search(git, workspace_id, "file")) == "too_large"
    assert not any("ls-files" in args for args in calls)

    monkeypatch.setattr(code_search, "MAX_INDEX_BYTES", stub_size + shared_size)
    assert search(git, workspace_id, "file").hits
    assert any("ls-files" in args for args in calls)


@needs_git
def test_repository_config_runs_nothing_during_a_search(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "a.txt")
    (root / ".gitattributes").write_text("* diff=marker\n", encoding="utf-8")
    _git(root, "add", ".gitattributes")  # before the hook exists: setup must not run it
    marker = tmp_path / "hook-ran"
    hook = f"\"echo ran > '{marker.as_posix()}'\""
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(
            f"[core]\n\tfsmonitor = {hook}\n"
            f"[diff]\n\texternal = {hook}\n"
            f'[diff "marker"]\n\ttextconv = {hook}\n'
        )

    git, workspace_id = _setup(tmp_path, root)
    assert search(git, workspace_id, "a.txt").hits == ("a.txt",)
    assert not marker.exists()

    # The hook is live: a status without the hardening runs it.
    winproc.git(root, "status", "--porcelain")
    assert marker.exists()


@needs_git
@pytest.mark.parametrize("query", ["", "   ", "x" * (MAX_QUERY_CHARS + 1), " " + "x" * 201])
def test_an_invalid_query_is_refused_before_any_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "a.txt")
    git, workspace_id = _setup(tmp_path, root)
    calls = _count_git(monkeypatch)

    assert _refusal(lambda: search(git, workspace_id, query)) == "invalid_query"
    assert calls == []
    # 200 characters, spaces around them stripped first, is valid.
    assert search(git, workspace_id, f" {'x' * MAX_QUERY_CHARS} ") == SearchResult((), False)


@needs_git
def test_an_untrusted_folder_is_refused_with_no_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    _tracked(root, "a.txt")
    git, workspace_id = _setup(tmp_path, root, trust=False)
    calls = _count_git(monkeypatch)

    assert _refusal(lambda: search(git, workspace_id, "a")) == "trust_required"
    assert calls == []


@pytest.mark.parametrize("failing", ["rev-parse", "ls-files"])
def test_a_git_failure_is_git_unavailable_and_logs_no_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    root = tmp_path / "secret-name-ZZZ"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "index").write_bytes(b"")
    git, workspace_id = _setup(tmp_path, root)

    def fake(_cwd: object, *args: str, timeout: float = 15.0) -> str:
        if failing in args and "--is-inside-work-tree" not in args:
            raise RuntimeError(f"git {failing} failed: fatal: {root}")
        return "true\n\n" if "--is-inside-work-tree" in args else ".git/index\n"

    monkeypatch.setattr(winproc, "git", fake)
    with caplog.at_level(logging.WARNING, logger="viki_agent.code.code_search"):
        assert _refusal(lambda: search(git, workspace_id, "a")) == "git_unavailable"
    (record,) = caplog.records
    assert record.getMessage() == f"code: git {failing} failed (RuntimeError)"
    assert root.name not in caplog.text
