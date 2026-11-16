"""Commit the approved paths, against real repositories in short temp folders: exactly the approved
paths land, every stale-review and refusal case leaves the index as it was, hooks run
(`core.hooksPath` at a test hooks folder), and no banned option reaches Git. Commits run under the
whole-root lease."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.workspace_code.test_code_checkout import BRANCH, Rig, _head, _rig
from tests.workspace_code.test_code_git import _git
from viki_agent import winproc
from viki_agent.code import code_checkout
from viki_agent.code.code_commit import CodeCommitter, CommitResult
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import HARDENING
from viki_agent.code.code_review import CodeReview
from viki_agent.vaults import vault_mutation

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
SESSION = "33333333-3333-4333-8333-000000000001"
STAMP = "2026-10-04T00:00:00.000Z"
UNKNOWN = "0" * 64
BANNED = {"-a", "-A", "--all", "--no-verify", "-n", "--amend", "--allow-empty"}


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable (each turns the gate into `git_unavailable`), and an empty
    home, so the user's global Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


def _approval(rig: Rig, *paths: str, worktree_id: str | None = None) -> list[tuple[str, str]]:
    """The paths with the digests the review shows; a path without one gets `UNKNOWN`."""
    found = CodeReview(rig.store, rig.git).entries(rig.checkout(worktree_id)).items
    digests = {entry.path: entry.digest for entry in found}
    return [(path, digests.get(path) or UNKNOWN) for path in paths]


def _commit(
    rig: Rig,
    paths: Sequence[tuple[str, str]],
    *,
    message: str = "change",
    branch: str = "main",
    head_oid: str | None = None,
    worktree_id: str | None = None,
    busy: frozenset[str] = frozenset(),
) -> Awaitable[CommitResult]:
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    committer = CodeCommitter(
        CodeReview(rig.store, rig.git),
        rig.git,
        rig.store,
        rig.coordinator,
        rig.resolver,
        busy=busy.__contains__,
    )
    root = rig.folder if worktree_id is None else rig.short / "w"
    return committer.commit(
        rig.workspace_id,
        worktree_id,
        workspace.policy_revision,
        workspace.root.physical_id,
        branch,
        head_oid or _head(root),
        message,
        paths,
    )


async def _refused(awaitable: Awaitable[object]) -> str:
    with pytest.raises(CodeError) as refused:
        await awaitable
    return refused.value.code


def _staged(root: Path) -> list[str]:
    return _git(root, "diff", "--cached", "--name-only").splitlines()


def _hook(rig: Rig, body: str) -> None:
    hooks = rig.short / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _git(rig.folder, "config", "core.hooksPath", str(hooks))


def _spy_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []
    real = winproc.git

    def spy(cwd: str | os.PathLike[str], *args: str, timeout: float = 15.0, **kwargs: Any) -> str:
        seen.append(args)
        return real(cwd, *args, timeout=timeout, **kwargs)

    monkeypatch.setattr(winproc, "git", spy)
    return seen


def _changes(rig: Rig) -> None:
    """A modified, a new and a deleted file to approve, and a tracked and an untracked change to
    leave alone (`commit -a` would take the one, `add -A` both)."""
    folder = rig.folder
    (folder / "t.txt").write_text("tracked\n", encoding="utf-8")
    _git(folder, "add", "t.txt")
    _git(folder, "commit", "-q", "-m", "tracked")
    (folder / "t.txt").write_text("left alone\n", encoding="utf-8")
    (folder / "a.txt").write_text("approved\n", encoding="utf-8")
    (folder / "n.txt").write_text("new\n", encoding="utf-8")
    (folder / "dir" / "b.txt").unlink()
    (folder / "o.txt").write_text("other\n", encoding="utf-8")


# --- the commit ---------------------------------------------------------------------------------


@needs_git
async def test_commit_stages_exactly_the_approved_paths(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    _hook(rig, "touch .git/pre-commit-ran")
    before = _head(rig.folder)
    message = 'First line "quoted"\n\n日本語の本文'

    result = await _commit(rig, _approval(rig, "a.txt", "n.txt", "dir/b.txt"), message=message)

    head = _head(rig.folder)
    assert result == CommitResult(head, (), ())
    assert head != before
    assert _git(rig.folder, "rev-parse", "HEAD~1").strip() == before
    changed = _git(rig.folder, "diff", "--name-status", "--no-renames", before, head)
    assert sorted(changed.splitlines()) == ["A\tn.txt", "D\tdir/b.txt", "M\ta.txt"]
    assert _git(rig.folder, "log", "-1", "--format=%B").rstrip("\n") == message
    assert _staged(rig.folder) == []
    assert _git(rig.folder, "status", "--porcelain").splitlines() == [" M t.txt", "?? o.txt"]
    assert (rig.folder / ".git" / "pre-commit-ran").exists()


@needs_git
async def test_a_worktree_commits_on_its_branch(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id, path = rig.worktree()
    folder_head = _head(rig.folder)
    (path / "a.txt").write_text("in the worktree\n", encoding="utf-8")

    result = await _commit(
        rig,
        _approval(rig, "a.txt", worktree_id=worktree_id),
        branch=BRANCH,
        worktree_id=worktree_id,
    )

    assert result.head_oid == _head(path) != folder_head
    assert _head(rig.folder) == folder_head
    assert _git(path, "status", "--porcelain") == ""


@needs_git
async def test_another_staged_file_is_unrelated_staged(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    _git(rig.folder, "add", "o.txt")
    approval = _approval(rig, "a.txt")
    assert await _refused(_commit(rig, approval)) == "unrelated_staged"
    assert _staged(rig.folder) == ["o.txt"]


# --- stale reviews ------------------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize("move", ["head", "branch", "digest", "unchanged"])
async def test_a_change_after_the_review_is_stale_review(tmp_path: Path, move: str) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    target.write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    reviewed = _head(rig.folder)
    if move == "head":
        _git(rig.folder, "commit", "-q", "--allow-empty", "-m", "moved")
    elif move == "branch":
        _git(rig.folder, "switch", "-q", "-c", "side")
    elif move == "digest":
        target.write_text("edited after the review\n", encoding="utf-8")
    else:
        target.write_text("alpha\n", encoding="utf-8")

    assert await _refused(_commit(rig, approval, head_oid=reviewed)) == "stale_review"
    assert _staged(rig.folder) == []


@needs_git
async def test_a_write_right_after_the_add_is_stale_review_and_the_add_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    target.write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    real = rig.git.git

    def racing(root: Path, *args: str, timeout: float = 5.0) -> str:
        done = real(root, *args, timeout=timeout)
        if args[0] == "add":  # an editor outside the app writes meanwhile
            target.write_text("written after the add\n", encoding="utf-8")
        return done

    monkeypatch.setattr(rig.git, "git", racing)
    assert await _refused(_commit(rig, approval)) == "stale_review"
    assert _head(rig.folder) == before
    assert _staged(rig.folder) == ["a.txt"]


@needs_git
async def test_a_write_right_before_the_add_is_stale_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    target.write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    real = rig.git.git

    def racing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[0] == "add":  # an editor outside the app writes after the digest check
            target.write_text("written before the add\n", encoding="utf-8")
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", racing)
    assert await _refused(_commit(rig, approval)) == "stale_review"
    assert _head(rig.folder) == before
    assert _staged(rig.folder) == ["a.txt"]  # the add stays; nothing is committed


@needs_git
async def test_another_file_staged_right_before_the_add_is_stale_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    (rig.folder / "o.txt").write_text("other\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    real = rig.git.git

    def racing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[0] == "add":  # another program stages after the staged-set check
            _git(rig.folder, "add", "o.txt")
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", racing)
    assert await _refused(_commit(rig, approval)) == "stale_review"
    assert _head(rig.folder) == before


@needs_git
async def test_other_bytes_staged_then_the_reviewed_bytes_written_back_is_stale_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    target.write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    real = rig.git.git

    def racing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[0] != "add":
            return real(root, *args, timeout=timeout)
        target.write_text("unreviewed\n", encoding="utf-8")
        done = real(root, *args, timeout=timeout)  # stages the unreviewed bytes
        target.write_text("approved\n", encoding="utf-8")  # the digests match again
        return done

    monkeypatch.setattr(rig.git, "git", racing)
    assert await _refused(_commit(rig, approval)) == "stale_review"
    assert _head(rig.folder) == before


@needs_git
async def test_a_stage_after_the_add_checks_is_hook_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    target.write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    real = rig.git.git

    def racing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args == ("diff", "--name-only", "-z", "--no-renames", "--", "a.txt"):
            target.write_text("unreviewed\n", encoding="utf-8")  # written and staged, so the
            _git(rig.folder, "add", "a.txt")  # index matches the worktree again
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", racing)
    result = await _commit(rig, approval)
    assert result == CommitResult(_head(rig.folder), ("a.txt",), ())
    assert _git(rig.folder, "show", "HEAD:a.txt") == "unreviewed\n"


@needs_git
async def test_autocrlf_text_commits(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "config", "core.autocrlf", "true")
    (rig.folder / "a.txt").write_bytes(b"crlf\r\nlines\r\n")
    (rig.folder / "c.txt").write_bytes(b"new\r\nfile\r\n")

    result = await _commit(rig, _approval(rig, "a.txt", "c.txt"))

    assert result == CommitResult(_head(rig.folder), (), ())
    staged_lf = hashlib.sha1(b"blob 9\0new\nfile\n").hexdigest()  # the clean conversion's LF
    assert _git(rig.folder, "rev-parse", "HEAD:c.txt").strip() == staged_lf


@needs_git
async def test_a_detached_head_is_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    _git(rig.folder, "switch", "-q", "--detach")
    assert await _refused(_commit(rig, approval)) == "detached_head"


@needs_git
async def test_a_merge_in_progress_is_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    (rig.folder / ".git" / "MERGE_HEAD").write_text(_head(rig.folder) + "\n", encoding="utf-8")
    assert await _refused(_commit(rig, approval)) == "in_progress"
    assert _staged(rig.folder) == []


@needs_git
async def test_no_git_identity_is_refused_before_any_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    monkeypatch.delenv("EMAIL", raising=False)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    _git(rig.folder, "config", "--unset", "user.name")
    _git(rig.folder, "config", "--unset", "user.email")
    _git(rig.folder, "config", "user.useConfigOnly", "true")
    assert await _refused(_commit(rig, approval)) == "identity_missing"
    assert _staged(rig.folder) == []


@needs_git
async def test_an_approved_binary_is_unsupported_path(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "b.bin").write_bytes(b"\0\1\2binary")
    assert await _refused(_commit(rig, _approval(rig, "b.bin"))) == "unsupported_path"
    assert _staged(rig.folder) == []


# --- input checks, before any Git ---------------------------------------------------------------


def _long(index: int, extra: str = "") -> tuple[str, str]:
    """A 400-character path (two parts within the name cap), unique by `index`."""
    return f"{index:02d}{'d' * 197}/{'e' * 200}{extra}", UNKNOWN


@needs_git
async def test_path_count_and_length_caps(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    short = [(f"f{index}.txt", UNKNOWN) for index in range(201)]
    assert await _refused(_commit(rig, short)) == "too_many_paths"
    assert await _refused(_commit(rig, [])) == "too_many_paths"
    assert await _refused(_commit(rig, [("a.txt", UNKNOWN)] * 2)) == "too_many_paths"
    long = [_long(index) for index in range(40)]  # 16,000 characters
    assert await _refused(_commit(rig, [*long[:-1], _long(39, "z")])) == "too_many_paths"
    # At the caps the checks pass; none of these paths is a change.
    assert await _refused(_commit(rig, short[:200])) == "stale_review"
    assert await _refused(_commit(rig, long)) == "stale_review"


@needs_git
async def test_message_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)
    approval = [("a.txt", UNKNOWN)]
    calls = _spy_git(monkeypatch)
    for message in ("", " \n\t ", "a\0b", "x" * 4001, "lone \ud800"):
        assert await _refused(_commit(rig, approval, message=message)) == "invalid_message"
    assert calls == []
    assert await _refused(_commit(rig, approval, message="x" * 4000)) == "stale_review"


@needs_git
async def test_bad_and_private_paths_are_refused_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    head = _head(rig.folder)
    calls = _spy_git(monkeypatch)
    looked: list[str] = []
    monkeypatch.setattr(rig.git, "repository", lambda workspace_id: looked.append(workspace_id))
    for path in ("../a.txt", "a//b.txt", "/a.txt", "a\\b.txt", "CON"):
        refused = await _refused(_commit(rig, [(path, UNKNOWN)], head_oid=head))
        assert refused == "invalid_path", path
    assert await _refused(_commit(rig, [(".git/config", UNKNOWN)], head_oid=head)) == (
        "private_path"
    )
    assert (calls, looked) == ([], [])


@needs_git
async def test_an_unsupported_drive_refuses_and_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    assert await _refused(_commit(rig, approval)) == "unsupported_filesystem"

    assert _head(rig.folder) == before
    assert _staged(rig.folder) == []


# --- hooks --------------------------------------------------------------------------------------


@needs_git
async def test_a_refusing_hook_is_commit_refused_and_the_paths_stay_staged(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    _hook(rig, "exit 1")
    before = _head(rig.folder)
    approval = _approval(rig, "a.txt", "n.txt")
    assert await _refused(_commit(rig, approval)) == "commit_refused"
    assert _head(rig.folder) == before
    assert sorted(_staged(rig.folder)) == ["a.txt", "n.txt"]


@needs_git
async def test_a_commit_that_lands_before_a_timeout_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    before = _head(rig.folder)
    real = rig.git.git

    def timing_out(root: Path, *args: str, timeout: float = 5.0) -> str:
        done = real(root, *args, timeout=timeout)
        if args[0] == "commit":
            raise TimeoutError("commit")
        return done

    monkeypatch.setattr(rig.git, "git", timing_out)
    result = await _commit(rig, approval)
    assert result == CommitResult(_head(rig.folder), (), ())
    assert result.head_oid != before


@needs_git
async def test_a_hook_that_rewrites_an_approved_file_is_hook_changed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    _hook(rig, "printf 'hooked\\n' > a.txt && git add a.txt")
    result = await _commit(rig, _approval(rig, "a.txt", "n.txt"))
    assert result == CommitResult(_head(rig.folder), ("a.txt",), ())
    assert _git(rig.folder, "show", "HEAD:a.txt") == "hooked\n"


@needs_git
async def test_a_hook_that_stages_another_file_is_unexpected(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    _hook(rig, "git add o.txt")
    result = await _commit(rig, _approval(rig, "n.txt", "a.txt"))
    assert result == CommitResult(_head(rig.folder), (), ("o.txt",))


# --- the busy guard and the lease ---------------------------------------------------------------


@needs_git
async def test_a_busy_session_in_the_folder_is_checkout_busy(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": None,
            "display_root": "<repo>",
            "created_at": STAMP,
        }
    )
    approval = _approval(rig, "a.txt")
    assert await _refused(_commit(rig, approval, busy=frozenset({SESSION}))) == "checkout_busy"
    assert _staged(rig.folder) == []
    assert (await _commit(rig, approval)).head_oid == _head(rig.folder)  # idle is allowed


@needs_git
async def test_a_held_root_is_vault_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt")
    monkeypatch.setattr(code_checkout, "LEASE_WAIT_S", 0.05)
    async with rig.coordinator.lease(rig.admit(), ()):
        assert await _refused(_commit(rig, approval)) == "vault_locked"
    assert _staged(rig.folder) == []


# --- argv ---------------------------------------------------------------------------------------


@needs_git
async def test_the_argv_holds_no_banned_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    _changes(rig)
    approval = _approval(rig, "a.txt", "n.txt", "dir/b.txt")
    message = "-a --no-verify\nsecond line"
    seen = _spy_git(monkeypatch)

    await _commit(rig, approval, message=message)

    assert (*HARDENING, "add", "--", "a.txt", "n.txt", "dir/b.txt") in seen
    assert (*HARDENING, "commit", f"--message={message}") in seen
    adds = [args for args in seen if "add" in args]
    commits = [args for args in seen if "commit" in args]
    assert len(adds) == 1 and len(commits) == 1
    for args in seen:
        assert not BANNED & set(args), args
        assert not any(arg.startswith(("--author", "--date", "user.")) for arg in args), args
