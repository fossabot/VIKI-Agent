"""A Bot copy's nested repository, against real Git in short temp folders: the app's Review, file
view, saved-check snapshot and Commit start no Git inside it, whatever its own config says, while
an ordinary worktree keeps Git's default. Two synthetic vectors prove whether Git worked inside:
a clean filter (it needs one index entry there) and a promisor remote's command (config and a
ref alone). A marker file tells which ran."""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from tests.workspace_code.test_code_checkout import BRANCH, STAMP, WORKTREE, Rig, _head, _rig
from tests.workspace_code.test_code_commit import _approval, _commit
from tests.workspace_code.test_code_git import _git, needs_git
from tests.workspace_code.test_git_guard import isolated_git  # noqa: F401 (autouse: no host config)
from tests.workspace_code.test_session_roots import bot_copy
from viki_agent import winproc
from viki_agent.code import worktree_probe
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit, _outside_nested
from viki_agent.code.code_review import CodeReview
from viki_agent.code.worktree_rules import decide

pytestmark = needs_git

FLAG = "--ignore-submodules=dirty"
VECTORS = ("filter", "remote")


def _nested(path: Path, text: str) -> Path:
    """A repository of its own at `path` with one commit, as a shell could leave it there."""
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        _git(path, "config", key, value)
    (path / "f.txt").write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", text.strip())
    return path


def _plant(nested: Path, vector: str, marker: Path) -> None:
    """Config of the nested repository's own that touches `marker` when Git works inside it."""
    with (nested / ".git" / "config").open("a", encoding="utf-8") as config:
        if vector == "filter":
            config.write(f'[filter "evil"]\n\tclean = "touch {marker.as_posix()}; cat"\n')
        else:
            config.write(
                '[remote "evil"]\n\turl = .\n\tpromisor = true\n'
                f'\tuploadpack = "touch {marker.as_posix()}; false"\n'
                "[extensions]\n\tpartialClone = evil\n[core]\n\trepositoryformatversion = 1\n"
            )
    if vector == "filter":
        (nested / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        os.utime(nested / "f.txt", ns=(1, 1))  # same size, other mtime: status runs the filter
    else:
        # A branch at a commit the repository lacks: Git inside asks the promisor remote for it.
        (nested / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n", encoding="utf-8")


def _copy(
    tmp_path: Path, vector: str, *, bot: bool, where: str = "staged"
) -> tuple[Rig, Path, Path]:
    """A baselined folder, one worktree of it (a Bot copy with `bot`) holding a nested repository
    and the marker its planted config touches. `where` is how the copy knows the path: a gitlink
    `staged` in the copy, one in the folder's `head`, so the copy starts with it, or `untracked`."""
    rig = _rig(tmp_path)
    rig.git = CodeGit(
        rig.workspaces, CodeFiles(rig.workspaces), git_available=lambda: True,
        bot_code_store=rig.store,
    )  # fmt: skip
    if where == "head":
        _nested(rig.folder / "nested", "folder\n")
        _git(rig.folder, "add", "nested")
        _git(rig.folder, "commit", "-q", "-m", "gitlink")
    assert rig.store.insert_bot_code_baseline(
        {
            "workspace_id": rig.workspace_id,
            "git_fingerprint": rig.git.settings_fingerprint(rig.folder),
            "created_at": STAMP,
        }
    )
    _, copy = rig.worktree()
    if bot:
        bot_copy(rig.store, rig.workspace_id, WORKTREE)
    nested = _nested(copy / "nested", "copy\n")
    if where == "staged":
        _git(copy, "add", "nested")
    marker = rig.short / "nested-git-ran"
    _plant(nested, vector, marker)
    (copy / "a.txt").write_text("copy\n", encoding="utf-8")
    return rig, copy, marker


def _snapshot(rig: Rig) -> tuple[str | None, int | None]:
    runner = CheckRunner(rig.store, rig.workspaces, rig.git, cast(Any, None))
    return runner._snapshot(rig.workspace_id, WORKTREE)


@pytest.mark.parametrize("vector", VECTORS)
def test_an_ordinary_worktree_keeps_gits_default_and_enters_a_nested_repository(
    tmp_path: Path, vector: str
) -> None:
    rig, _copy_path, marker = _copy(tmp_path, vector, bot=False)
    with contextlib.suppress(CodeError):  # the promisor's command fails, and so the status
        CodeReview(rig.store, rig.git).review(rig.workspace_id, WORKTREE)
    assert marker.exists()


@pytest.mark.parametrize("vector", VECTORS)
@pytest.mark.parametrize("where", ["staged", "head"])
async def test_review_view_snapshot_and_commit_start_no_git_inside_a_bot_copys_nested_repository(
    tmp_path: Path, vector: str, where: str
) -> None:
    rig, copy, marker = _copy(tmp_path, vector, bot=True, where=where)
    review = CodeReview(rig.store, rig.git)

    shown = review.review(rig.workspace_id, WORKTREE)
    # The gitlink keeps its row, so a commit it blocks is explained.
    assert {entry.path: entry.kind for entry in shown.entries} == {
        "a.txt": "text",
        "nested": "submodule",
    }
    assert review.view(rig.workspace_id, "a.txt", WORKTREE, integration=False).diff
    for path in ("nested", "nested/f.txt"):
        with contextlib.suppress(CodeError):  # the folder itself refuses, with no Git inside it
            review.view(rig.workspace_id, path, WORKTREE, integration=False)
    assert _snapshot(rig) == (_head(copy), 1)

    approval = _approval(rig, "a.txt", worktree_id=WORKTREE)
    landing = _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE)
    if where == "staged":
        with pytest.raises(CodeError, match="^unrelated_staged$"):
            await landing
    else:
        result = await landing
        assert _head(copy) == result.head_oid
        assert _git(copy, "show", "--name-only", "--format=", "HEAD").split() == ["a.txt"]
    assert not marker.exists()


def test_every_status_and_diff_in_a_bot_copy_carries_the_option_and_the_folders_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, copy, _marker = _copy(tmp_path, "filter", bot=True)
    native = winproc.git
    seen: list[tuple[Path, tuple[str, ...]]] = []

    def watched(cwd: Any, *args: str, **kwargs: Any) -> str:
        if not any(arg.startswith("--git-dir") for arg in args):  # the guard's private parses
            seen.append((Path(cwd), args))
        return native(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", watched)
    review = CodeReview(rig.store, rig.git)
    review.review(rig.workspace_id, WORKTREE)
    review.view(rig.workspace_id, "a.txt", WORKTREE, integration=False)
    _snapshot(rig)

    def scans(root: Path) -> list[tuple[str, ...]]:
        """The options, up to `--`, of each `status` and `diff` that ran in `root`."""
        ran = [args for cwd, args in seen if cwd == root]
        options = [args[: args.index("--")] if "--" in args else args for args in ran]
        return [args for args in options if {"status", "diff"} & set(args)]

    in_copy = scans(copy)
    # The review's status alone is enough to enter a nested repository: it runs for every
    # review, whether or not its diff follows.
    statuses = [args for args in in_copy if "--untracked-files=all" in args]
    assert statuses and all(FLAG in args for args in statuses)
    assert {"status", "diff"} <= {word for args in in_copy for word in args}
    assert all(FLAG in args for args in in_copy)
    in_folder = scans(rig.folder)
    assert in_folder and not any("--ignore-submodules" in "".join(args) for args in in_folder)


def test_the_option_follows_the_subcommand_and_ignores_a_path_spelled_like_it() -> None:
    assert _outside_nested(("status", "--porcelain")) == ("status", FLAG, "--porcelain")
    assert _outside_nested(("-c", "core.longpaths=true", "diff", "HEAD", "--")) == (
        *("-c", "core.longpaths=true", "diff", FLAG, "HEAD", "--"),
    )
    # After `--` it is a file's name, not the caller's choice of mode.
    assert _outside_nested(("diff", "--", "--ignore-submodules=all")) == (
        *("diff", FLAG, "--", "--ignore-submodules=all"),
    )
    kept: tuple[tuple[str, ...], ...] = (
        ("status", "--ignore-submodules=all"),
        ("rev-parse", "HEAD"),
        ("add", "--", "status"),
        ("-c", "status", "rev-parse"),
        ("-c", "core.longpaths=true"),
        (),
    )
    for args in kept:
        assert _outside_nested(args) == args


@pytest.mark.parametrize("kind", ["staged", "untracked"])
def test_a_released_bot_copy_holding_a_nested_repository_is_kept_not_archived(
    tmp_path: Path, kind: str
) -> None:
    """The archive's `add -A` has no such option, so the cleanup rules must keep these trees."""
    rig, copy, marker = _copy(tmp_path, "filter", bot=True, where=kind)
    facts = worktree_probe.inspect(rig.git, copy)
    assert facts.readable and (facts.submodule if kind == "staged" else facts.embedded)
    decision = decide(
        WORKTREE,
        BRANCH,
        in_area=True,
        trusted=True,
        live=False,
        locked=False,
        present=True,
        facts=facts,
        now=datetime.now(UTC) + timedelta(days=3650),
        copy="released",
    )
    assert (decision.action, decision.reason) == (
        "keep",
        "submodule" if kind == "staged" else "embedded_repo",
    )
    assert not marker.exists()
