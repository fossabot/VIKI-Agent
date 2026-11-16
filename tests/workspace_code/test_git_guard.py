"""Synthetic repositories exercise the folder baseline at real Git boundaries."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.workspace_code.test_code_checkout import _rig
from tests.workspace_code.test_code_files import link_dir
from tests.workspace_code.test_code_git import _commit, _git, _repo, _setup, needs_git
from viki_agent import winproc
from viki_agent.code import code_diff, code_remote, code_search, worktree_archive, worktree_probe
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.code_commit import CodeCommitter
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import HARDENING, CodeGit
from viki_agent.code.code_integration import CodeIntegration
from viki_agent.code.code_review import CodeReview
from viki_agent.decision_loop.code_look import CodeLookReader


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    empty = tmp_path / "global-config"
    empty.write_bytes(b"")
    real = winproc.run
    # A synthetic host: every child sees this global config unless its own environment, as
    # `winproc.git(isolated_config=True)` builds it, replaces those variables.
    host = {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_SYSTEM": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def run(*args: Any, **kwargs: Any) -> Any:
        # An environment the caller passes is the child's whole environment, as in `winproc.run`:
        # merging `os.environ` back under it would restore a variable the caller left out.
        given = kwargs.get("env")
        kwargs["env"] = {**host, **(os.environ if given is None else given)}
        return real(*args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)


def guarded(tmp_path: Path) -> tuple[Path, CodeGit, str]:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _commit(root, "first")
    old, workspace_id = _setup(tmp_path, root)
    git = CodeGit(
        old._workspaces,
        old._files,
        git_available=lambda: True,
        bot_code_store=old._workspaces._store,
    )
    fingerprint = git.settings_fingerprint(root)
    assert git._bot_code_store is not None
    assert git._bot_code_store.insert_bot_code_baseline(
        {
            "workspace_id": workspace_id,
            "git_fingerprint": fingerprint,
            "created_at": "2026-11-11T00:00:00.000Z",
        }
    )
    return root, git, workspace_id


@needs_git
@pytest.mark.parametrize(
    "action", ["changes", "repository", "output", "answer", "diff", "probe", "remote", "archive"]
)
def test_changed_settings_survive_real_callers(tmp_path: Path, action: str) -> None:
    root, git, workspace_id = guarded(tmp_path)
    _git(root, "config", "guard.synthetic", "changed")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        match action:
            case "changes":
                git.changes(workspace_id)
            case "repository":
                git.repository(workspace_id)
            case "output":
                git.output(root, "status", "--porcelain")
            case "answer":
                git.answer(root, "rev-parse", "HEAD")
            case "diff":
                code_diff.diff(git, git._files, workspace_id, "a.txt")
            case "probe":
                worktree_probe.inspect(git, root)
            case "remote":
                code_remote.remote_oid(git, root, "origin", "main")
            case "archive":
                worktree_archive.snapshot(git, root)


@needs_git
def test_operations_verify_once_and_refresh_after_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    calls: list[Path] = []
    assert git._guard is not None
    verify = git._guard.verify

    def counted(path: Path) -> None:
        calls.append(path)
        verify(path)

    monkeypatch.setattr(git._guard, "verify", counted)
    git.changes(workspace_id)
    assert calls == [root]
    calls.clear()
    with git.operation(root):
        git.head(root)
        git.branch(root)
        assert calls == [root]
        _git(root, "config", "guard.synthetic", "after-wait")
        with (
            pytest.raises(CodeError, match="^git_settings_changed$"),
            git.operation(root, fresh=True),
        ):
            git.head(root)
    assert calls == [root, root]


@needs_git
@pytest.mark.parametrize("conditional", [False, True])
def test_outside_include_is_refused_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conditional: bool
) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    outside = tmp_path / "outside-canary"
    outside.write_text("[guard]\n synthetic = SECRET-CANARY\n", encoding="utf-8")
    nested = root / ".git" / "nested"
    section = '[includeIf "onbranch:never"]' if conditional else "[include]"
    nested.write_text(f'{section}\n path = "{outside.as_posix()}"\n', encoding="utf-8")
    _git(root, "config", "include.path", "nested")
    from viki_agent.code import git_guard

    read = git_guard._read
    opened: list[Path] = []

    def recording(*args: object, **kwargs: object) -> object:
        opened.append(args[1])  # type: ignore[arg-type]
        assert args[1] != outside
        return read(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(git_guard, "_read", recording)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(_workspace_id)
    assert outside not in opened


def rebaseline(git: CodeGit, root: Path, workspace_id: str) -> None:
    store = git._bot_code_store
    assert store is not None
    old = store.bot_code_baseline(workspace_id)
    assert old is not None
    assert store.replace_bot_code_baseline(
        workspace_id,
        old["git_fingerprint"],
        git.settings_fingerprint(root),
        "2026-11-11T00:00:00.000Z",
    )


@needs_git
@pytest.mark.parametrize(
    "section",
    [
        "include",
        'includeIf "onbranch:main"',
        'includeIf "onbranch:never"',
        'includeIf "hasconfig:remote.*.url:https://example.invalid/**"',
    ],
)
def test_native_nested_and_conditional_graph_is_stable(tmp_path: Path, section: str) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / ".git" / "leaf").write_text("[guard]\n value = leaf\n", encoding="utf-8")
    (root / ".git" / "nested").write_text(f"[{section}]\n path = leaf\n", encoding="utf-8")
    _git(root, "config", "include.path", "nested")
    _git(root, "config", "remote.origin.url", "https://example.invalid/repo")
    rebaseline(git, root, workspace_id)
    assert git.changes(workspace_id).state == "ready"
    before = git.settings_fingerprint(root)
    assert git.settings_fingerprint(root) == before
    (root / ".git" / "leaf").write_text("[guard]\n value = changed\n", encoding="utf-8")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
@pytest.mark.parametrize(
    "condition", ["gitdir:/**", "gitdir/i:/**", "worktree:/**", "future:unknown"]
)
def test_unsupported_condition_is_explicit_refusal(tmp_path: Path, condition: str) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    (root / ".git" / "leaf").write_text("[guard]\n value = safe\n", encoding="utf-8")
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f'[includeIf "{condition}"]\n path = leaf\n')
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(root)


@needs_git
@pytest.mark.parametrize("target", ["hooks", "info"])
def test_hooks_and_attributes_junctions_never_read_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    outside = tmp_path / "outside-canary"
    outside.mkdir()
    (outside / "attributes").write_text("SECRET-CANARY\n", encoding="utf-8")
    path = root / ".git" / target
    for child in path.iterdir():
        child.unlink()
    path.rmdir()
    link_dir(path, outside)
    from viki_agent.code import git_guard

    original = git_guard._read
    opened: list[Path] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        opened.append(args[1])
        assert not args[1].is_relative_to(outside)
        assert not args[1].is_relative_to(path)
        return original(*args, **kwargs)

    monkeypatch.setattr(git_guard, "_read", read)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert not any(p.is_relative_to(path) or p.is_relative_to(outside) for p in opened)


@needs_git
def test_graph_swap_uses_only_snapshots_then_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    outside = tmp_path / "outside-canary"
    outside.write_text("SECRET-CANARY INVALID CONFIG", encoding="utf-8")
    from viki_agent.code import git_guard

    original = git_guard._raw
    expanded: list[Path] = []
    swapped = False

    def raw(folder: Path, private_git: Path, *args: str) -> str:
        nonlocal swapped
        assert private_git != root / ".git"
        if "--includes" in args:
            expanded.append(private_git)
            assert outside.as_posix() not in (private_git / "config").read_text(encoding="utf-8")
        output = original(folder, private_git, *args)
        if not swapped and "--no-includes" in args:
            swapped = True
            with (root / ".git" / "config").open("a", encoding="utf-8") as config:
                config.write(f'[include]\n path = "{outside.as_posix()}"\n')
        return output

    monkeypatch.setattr(git_guard, "_raw", raw)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert expanded and swapped


@needs_git
@pytest.mark.parametrize("kind", ["config", "hooks"])
def test_file_count_and_bytes_are_charged_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    if kind == "config":
        (root / ".git" / "extra").write_bytes(b"[guard]\n value = safe\n")
        _git(root, "config", "include.path", "extra")
        monkeypatch.setattr(git_guard, "MAX_FILES", 1)  # config alone exhausts admission.
        blocked = root / ".git" / "extra"
    else:
        blocked = root / ".git" / "hooks" / "huge"
        with blocked.open("wb") as output:
            output.truncate(git_guard.MAX_BYTES + 1)
    read_handle = git_guard.handles._read_handle
    reads: list[int] = []

    def bounded(handle: Any, size: int) -> bytes:
        reads.append(size)
        assert size <= git_guard.MAX_BYTES
        return read_handle(handle, size)

    monkeypatch.setattr(git_guard.handles, "_read_handle", bounded)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert reads and blocked.exists()


@needs_git
def test_parser_expansion_budget_precedes_native_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    # 200 keys under a 30,000-byte section name: about 6 MB of native output from 31 KB.
    (root / ".git" / "config").write_text(
        '[guard "' + "x" * 30_000 + '"]\n' + " v = x\n" * 200, encoding="utf-8"
    )
    native = git_guard._raw
    calls: list[tuple[str, ...]] = []

    def counted(folder: Path, private_git: Path, *args: str) -> str:
        calls.append(args)
        return native(folder, private_git, *args)

    monkeypatch.setattr(git_guard, "_raw", counted)
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(root)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert calls == []


@needs_git
def test_parser_budget_admits_a_long_ordinary_config(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        for index in range(300):
            config.write(
                f'[remote "synthetic-{index}"]\n'
                f"\turl = https://example.invalid/synthetic-{index}.git\n"
                f"\tfetch = +refs/heads/*:refs/remotes/synthetic-{index}/*\n"
            )
    assert (root / ".git" / "config").stat().st_size > 30_000
    rebaseline(git, root, workspace_id)
    assert git.changes(workspace_id).state == "ready"


@needs_git
def test_repeated_include_edges_are_budgeted_before_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    (root / ".git" / "leaf").write_text("[guard]\n value = " + "x" * 200 + "\n", encoding="utf-8")
    for name, target in (("middle", "leaf"), ("top", "middle"), ("root", "top")):
        (root / ".git" / name).write_text(
            "[include]\n" + f" path = {target}\n" * 20, encoding="utf-8"
        )
    _git(root, "config", "include.path", "root")
    native = git_guard._raw
    expanded: list[tuple[str, ...]] = []

    def counted(folder: Path, private_git: Path, *args: str) -> str:
        if "--includes" in args:
            expanded.append(args)
        return native(folder, private_git, *args)

    monkeypatch.setattr(git_guard, "MAX_OUTPUT_BYTES", 1_500_000)
    monkeypatch.setattr(git_guard, "_raw", counted)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert expanded == []


@needs_git
def test_scope_is_separate_in_threads_and_child_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    assert git._guard is not None
    calls: list[Path] = []
    monkeypatch.setattr(git._guard, "verify", calls.append)
    with git.operation(root), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(git.head, root).result()
    assert calls == [root, root]
    calls.clear()

    async def run() -> None:
        async def child() -> str:
            return git.head(root)

        with git.operation(root):
            assert await asyncio.create_task(child())

    asyncio.run(run())
    assert calls == [root, root]


def count_checks(git: CodeGit, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every root the guard verifies from here on, in order."""
    assert git._guard is not None
    calls: list[Path] = []
    verify = git._guard.verify

    def counted(path: Path) -> None:
        calls.append(path)
        verify(path)

    monkeypatch.setattr(git._guard, "verify", counted)
    return calls


def review_of(git: CodeGit) -> CodeReview:
    assert git._bot_code_store is not None
    return CodeReview(git._bot_code_store, git)


def check_snapshot(git: CodeGit, workspace_id: str, worktree_id: str | None) -> object:
    assert git._bot_code_store is not None
    runner = CheckRunner(git._bot_code_store, git._workspaces, git, cast(Any, None))
    return runner._snapshot(workspace_id, worktree_id)


@needs_git
@pytest.mark.parametrize(
    "action",
    ["changes", "diff", "probe", "snapshot", "remote", "review", "view", "search", "check"],
)
def test_owned_operations_have_one_guard_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / "a.txt").write_text("two\n", encoding="utf-8")  # a change for the review to list
    calls = count_checks(git, monkeypatch)
    operations: dict[str, Callable[[], object]] = {
        "changes": lambda: git.changes(workspace_id),
        "diff": lambda: code_diff.diff_at(git, root, "a.txt"),
        "probe": lambda: worktree_probe.inspect(git, root),
        "snapshot": lambda: worktree_archive.snapshot(git, root),
        "remote": lambda: code_remote.select_remote(git, root, "main"),
        "review": lambda: review_of(git).review(workspace_id, None),
        "view": lambda: review_of(git).view(workspace_id, "a.txt", None, integration=False),
        "search": lambda: code_search.search(git, workspace_id, "a"),
        "check": lambda: check_snapshot(git, workspace_id, None),
    }
    assert operations[action]() not in (None, (None, None))
    assert calls == [root]


@needs_git
@pytest.mark.parametrize("action", ["review", "view", "integration", "check"])
def test_a_worktrees_operation_checks_the_folder_and_the_tree_once_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    worktree_id = track(git, root, workspace_id, copy, bot=True)
    (copy / "a.txt").write_text("two\n", encoding="utf-8")
    _commit(copy, "second")
    (copy / "a.txt").write_text("three\n", encoding="utf-8")
    calls = count_checks(git, monkeypatch)
    review = review_of(git)
    operations: dict[str, Callable[[], object]] = {
        "review": lambda: review.review(workspace_id, worktree_id),
        "view": lambda: review.view(workspace_id, "a.txt", worktree_id, integration=False),
        "integration": lambda: review.view(workspace_id, "a.txt", worktree_id, integration=True),
        "check": lambda: check_snapshot(git, workspace_id, worktree_id),
    }
    assert operations[action]() not in (None, (None, None))
    assert calls == [root, copy]


@needs_git
def test_a_scope_checks_each_root_once_and_nothing_outlives_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    calls = count_checks(git, monkeypatch)
    native = winproc.git
    hooks: set[str] = set()

    def watched(cwd: Any, *args: str, **kwargs: Any) -> str:
        hooks.update(arg for arg in args if arg.startswith("core.hooksPath="))
        return native(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", watched)
    with git.scope():
        for _ in range(2):
            git.head(root)
            git.head(copy)
        assert calls == [root, copy]
        # One private hooks folder serves the copy's commands for the whole operation.
        (folder,) = (Path(option.removeprefix("core.hooksPath=")) for option in hooks)
        assert folder.is_dir()
        _git(root, "config", "guard.synthetic", "changed-inside")
        git.head(root)  # verified for this operation: not read again
    assert calls == [root, copy] and not folder.exists()
    # The next operation starts from nothing: it reads the settings and refuses.
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(root)
    assert calls == [root, copy, root]


@needs_git
def test_a_refused_recheck_leaves_the_root_unverified(tmp_path: Path) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    with git.scope():
        git.head(root)
        _git(root, "config", "guard.synthetic", "after-wait")
        with (
            pytest.raises(CodeError, match="^git_settings_changed$"),
            git.operation(root, fresh=True),
        ):
            pytest.fail("the recheck let its body run")
        with pytest.raises(CodeError, match="^git_settings_changed$"):
            git.head(root)


@needs_git
@pytest.mark.parametrize(
    ("broken", "refusal"),
    [
        ("folder", "root_unavailable"),
        ("git", "git_unavailable"),
        ("tree", "worktree_not_ready"),
    ],
)
def test_one_scope_keeps_the_refusals_that_come_before_a_roots_first_command(
    tmp_path: Path, broken: str, refusal: str
) -> None:
    """The settings check waits for a root's first command, so what stops the operation before
    that command still answers for itself, never as changed Git settings."""
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    worktree_id = track(git, root, workspace_id, copy)
    if broken == "folder":
        root.rename(tmp_path / "moved")
    elif broken == "git":
        git._git_available = lambda: False
    else:
        copy.rename(tmp_path / "moved-copy")
    with pytest.raises(CodeError, match=f"^{refusal}$"):
        review_of(git).review(workspace_id, worktree_id)


@needs_git
@pytest.mark.parametrize("action", ["commit", "push", "check"])
async def test_a_mutations_reads_before_its_lease_have_one_guard_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    rig = _rig(tmp_path)
    git = rig.git = CodeGit(
        rig.workspaces, CodeFiles(rig.workspaces), git_available=lambda: True,
        bot_code_store=rig.store,
    )  # fmt: skip
    assert rig.store.insert_bot_code_baseline(
        {
            "workspace_id": rig.workspace_id,
            "git_fingerprint": git.settings_fingerprint(rig.folder),
            "created_at": STAMP,
        }
    )
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    admission = (rig.workspace_id, workspace.policy_revision, workspace.root.physical_id)
    services = (rig.store, rig.coordinator, rig.resolver)
    integration = CodeIntegration(git, *services, busy=lambda _: False)
    head = _git(rig.folder, "rev-parse", "HEAD").strip()
    calls = count_checks(git, monkeypatch)
    if action == "commit":
        committer = CodeCommitter(CodeReview(rig.store, git), git, *services, busy=lambda _: False)
        committer._target(rig.workspace_id, None, *admission[1:])
    else:
        # No remote: both refuse after the same reads a ready remote would get.
        with pytest.raises(CodeError, match="^no_remote$"):
            if action == "push":
                integration._push_target(*admission, "origin", "main", head)
            else:
                integration._check(rig.workspace_id, "origin", "main", head)
    assert calls == [rig.folder]


@needs_git
def test_no_baseline_preserves_git_and_changed_refusal_does_not_log_raw_data(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    plain = CodeGit(git._workspaces, git._files, git_available=lambda: True)
    _git(root, "config", "guard.secret", "SECRET-CANARY")
    assert plain.changes(workspace_id).state == "ready"
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.output(root, "status", "--porcelain")
    assert str(root) not in caplog.text and "SECRET-CANARY" not in caplog.text


@needs_git
@pytest.mark.parametrize("state", ["creating", "ready", "removing", "kept"])
def test_main_and_linked_copy_share_settings_and_baseline_survives_removal(
    tmp_path: Path, state: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    _git(root, "worktree", "add", "-q", "-b", "copy", str(copy))
    store = git._bot_code_store
    assert store is not None
    worktree_id = "00000000-0000-4000-8000-000000000001"
    stamp = "2026-11-11T00:00:00.000Z"
    store.insert_worktree(
        {
            "worktree_id": worktree_id,
            "workspace_id": workspace_id,
            "path": str(copy),
            "branch": "copy",
            "base_oid": _git(root, "rev-parse", "HEAD").strip(),
            "state": "creating",
            "created_at": stamp,
            "updated_at": stamp,
        },
        limit=32,
    )
    if state != "creating":
        store.update_worktree_state(worktree_id, "creating", state, stamp)
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    assert git.head(copy)
    _git(root, "config", "guard.synthetic", "while-retained")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(copy)
    rebaseline(git, root, workspace_id)
    _git(root, "worktree", "remove", str(copy))
    store.delete_worktree(worktree_id, expected_state=state)
    assert store.bot_code_baseline(workspace_id) is not None
    _git(root, "config", "guard.synthetic", "after-removal")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
@pytest.mark.parametrize("kind", ["files", "bytes"])
def test_final_hook_collection_enforces_actual_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    hooks = root / ".git" / "hooks"
    for path in hooks.iterdir():
        path.unlink()
    count, size = (257, 1) if kind == "files" else (5, 1024 * 1024)
    for index in range(count):
        with (hooks / f"synthetic-{index}").open("wb") as target:
            target.truncate(size)
    read_handle = git_guard.handles._read_handle
    allocated: list[int] = []

    def read(handle: Any, size: int) -> bytes:
        allocated.append(size)
        return read_handle(handle, size)

    monkeypatch.setattr(git_guard.handles, "_read_handle", read)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    if kind == "files":
        assert allocated.count(1) == 256
    else:
        assert allocated.count(1024 * 1024) == 4


@needs_git
@pytest.mark.parametrize(
    "action",
    [
        "output",
        "answer",
        "repository",
        "changes",
        "probe",
        "diff",
        "remote",
        "archive",
        "parents",
        "inspect",
    ],
)
def test_wrappers_propagate_guard_refusal_from_the_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)

    def refuse(*_args: Any, **_kwargs: Any) -> str:
        raise CodeError("git_settings_changed")

    monkeypatch.setattr(git, "git", refuse)
    operations: dict[str, Callable[[], object]] = {
        "output": lambda: git.output(root, "status"),
        "answer": lambda: git.answer(root, "rev-parse", "HEAD"),
        "repository": lambda: git.repository(workspace_id),
        "changes": lambda: git.changes(workspace_id),
        "probe": lambda: code_diff._probe(git, root, "cat-file", "-s", "HEAD:a.txt"),
        "diff": lambda: code_diff.diff_at(git, root, "a.txt"),
        "remote": lambda: code_remote.remote_oid(git, root, "origin", "main"),
        "archive": lambda: worktree_archive._run(git, root, "status"),
        "parents": lambda: worktree_archive.parents(git, root, "HEAD"),
        "inspect": lambda: worktree_probe.inspect(git, root),
    }
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        operations[action]()


@needs_git
def test_native_expansion_only_names_private_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / ".git" / "leaf").write_text("[guard]\n value = safe\n", encoding="utf-8")
    _git(root, "config", "include.path", "leaf")
    rebaseline(git, root, workspace_id)
    native = winproc.git
    expanded: list[Path] = []
    isolation: list[tuple[bool, bool]] = []

    def watched(cwd: Any, *args: str, **kwargs: Any) -> str:
        private = any(value.startswith("--git-dir=") for value in args)
        isolation.append((private, kwargs.get("isolated_config", False)))
        if "--includes" in args:
            directory = Path(
                next(
                    value.removeprefix("--git-dir=")
                    for value in args
                    if value.startswith("--git-dir=")
                )
            )
            expanded.append(directory)
            assert directory != root / ".git"
            if "--file" in args:
                assert Path(args[args.index("--file") + 1]).is_relative_to(directory)
        return native(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", watched)
    assert git.settings_fingerprint(root)
    assert expanded and all(directory != root / ".git" for directory in expanded)
    # Every private parse isolates the host's configuration; the action itself does not.
    assert isolation and all(private and isolated for private, isolated in isolation)
    isolation.clear()
    assert git.changes(workspace_id).state == "ready"
    assert (False, False) in isolation
    assert all(private == isolated for private, isolated in isolation)


@needs_git
def test_fingerprint_does_not_scan_global_includes(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    outside = tmp_path / "outside-canary"
    outside.write_text("SECRET-CANARY invalid config", encoding="utf-8")
    (tmp_path / "global-config").write_text(
        f'[include]\n path = "{outside.as_posix()}"\n', encoding="utf-8"
    )
    # Control: a native process without isolation opens the canary and fails on it.
    with pytest.raises(RuntimeError, match="bad config"):
        winproc.git(tmp_path, "config", "--file", str(root / ".git" / "config"), "--list")
    assert git.settings_fingerprint(root)
    # Ordinary Code Git actions keep the host's configuration, poisoned include and all.
    with pytest.raises(CodeError, match="^git_unavailable$"):
        git.changes(workspace_id)


@needs_git
@pytest.mark.parametrize("target", ["hooks/synthetic", "info/attributes"])
def test_hook_and_attribute_content_changes_refuse(tmp_path: Path, target: str) -> None:
    root, git, workspace_id = guarded(tmp_path)
    path = root / ".git" / target
    path.write_bytes(b"before\n")
    rebaseline(git, root, workspace_id)
    assert git.head(root)
    path.write_bytes(b"after\n")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
@pytest.mark.parametrize("failure", ["unsafe", "budget", "read"])
def test_raw_fingerprint_unavailability_differs_from_baseline_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    if failure == "unsafe":
        with (root / ".git" / "config").open("a", encoding="utf-8") as output:
            output.write("[include]\n path = ~/outside-canary\n")
    elif failure == "budget":
        monkeypatch.setattr(git_guard, "MAX_FILES", 0)
    else:

        def unreadable(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("SECRET-CANARY raw path")

        monkeypatch.setattr(git_guard, "_read", unreadable)
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(root)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
def test_settings_changing_while_read_is_a_change_not_unavailability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    native = git_guard._raw
    changed = False

    def raw(folder: Path, private_git: Path, *args: str) -> str:
        nonlocal changed
        output = native(folder, private_git, *args)
        if not changed:
            changed = True
            with (root / ".git" / "config").open("a", encoding="utf-8") as config:
                config.write("[guard]\n\tvalue = while-read\n")
        return output

    monkeypatch.setattr(git_guard, "_raw", raw)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.settings_fingerprint(root)
    assert changed


@needs_git
@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative paths are Windows paths")
@pytest.mark.parametrize("key", ["include.path", "core.hooksPath"])
def test_drive_relative_values_are_refused(tmp_path: Path, key: str) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / ".git" / "leaf").write_text("[guard]\n value = safe\n", encoding="utf-8")
    # Git reads `C:leaf` against drive C's own working directory, not this folder.
    _git(root, "config", key, f"{root.drive}leaf")
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(root)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
def test_background_consumers_read_changed_settings_as_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    _git(root, "config", "guard.secret", "SECRET-CANARY")
    workspace = git._workspaces.get(workspace_id, kind="code")
    now = datetime(2026, 11, 11, tzinfo=UTC)
    assert CodeLookReader(git._workspaces, git).read(now).failures == ("code_partial",)
    store = git._bot_code_store
    assert store is not None
    runner = CheckRunner(store, git._workspaces, git, cast(Any, None))
    assert runner._snapshot(workspace_id, None) == (None, None)
    assert CodeLookReader(git._workspaces, git)._look_at(workspace, now).failed
    assert str(root) not in caplog.text and "SECRET-CANARY" not in caplog.text


STAMP = "2026-11-11T00:00:00.000Z"


def track(git: CodeGit, root: Path, workspace_id: str, copy: Path, *, bot: bool = False) -> str:
    """A linked worktree the app tracks, by its id; `bot` binds it to a Bot as a Bot copy."""
    _git(root, "worktree", "add", "-q", "-b", copy.name, str(copy))
    store = git._bot_code_store
    assert store is not None
    row = {
        "worktree_id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "path": str(copy),
        "branch": copy.name,
        "base_oid": _git(root, "rev-parse", "HEAD").strip(),
        "state": "creating",
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    store.insert_worktree(cast(Any, row), limit=32)
    if bot:  # bound as `BotCode` binds it, without a Bot of its own (no grant to check)
        with store._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO bot_code_copies VALUES (?,?,?,?,NULL,'bound',?)",
                (row["worktree_id"], workspace_id, str(uuid.uuid4()), str(uuid.uuid4()), STAMP),
            )
    store.update_worktree_state(row["worktree_id"], "creating", "ready", STAMP)
    return row["worktree_id"]


def register(git: CodeGit, folder: Path) -> str:
    """`folder` registered and trusted as a code folder of its own."""
    service = git._workspaces
    workspace, _ = service.register_existing(str(uuid.uuid4()), str(folder), kind="code")
    required = service.inspect_policy(workspace.workspace_id).acknowledgements_required
    service.set_policy(str(uuid.uuid4()), workspace.workspace_id, 1, "ordinary", required)
    return workspace.workspace_id


def fake_git_dir(root: Path, copy: Path, fake_gitdir: Path, marker: Path) -> None:
    """Copy-written Git metadata whose config runs a filter that touches `marker`."""
    fake_common = fake_gitdir.parent.parent
    shutil.copytree(root / ".git", fake_common, ignore=shutil.ignore_patterns("worktrees"))
    fake_gitdir.mkdir(parents=True)
    for name in ("HEAD", "index"):
        shutil.copy2(root / ".git" / "worktrees" / copy.name / name, fake_gitdir / name)
    (fake_gitdir / "commondir").write_text(
        os.path.relpath(fake_common, fake_gitdir) + "\n", encoding="utf-8"
    )
    with (fake_common / "config").open("a", encoding="utf-8") as config:
        config.write(
            "[guard]\n\tevil = synthetic-in-effect\n"
            f'[filter "evil"]\n\tclean = "touch {marker.as_posix()}; cat"\n'
        )
    (copy / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    os.utime(copy / "a.txt", ns=(1, 1))  # same size, other mtime: status runs the filter


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resolves a link before a later `..`")
@pytest.mark.parametrize("spelling", ["dotdot", "space"])
def test_copy_pointer_that_git_resolves_elsewhere_is_refused(tmp_path: Path, spelling: str) -> None:
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "area" / "copy"
    copy.parent.mkdir()
    track(git, root, workspace_id, copy, bot=True)
    assert git.head(copy)
    marker = tmp_path / "filter-ran"
    real = root / ".git" / "worktrees" / "copy"
    if spelling == "dotdot":
        # Git: copy/evil/../../.. is copy; lexically it is tmp_path, naming the real git dir.
        (copy / "d1" / "d2" / "d3").mkdir(parents=True)
        (copy / "evil").symlink_to("d1/d2/d3")
        fake_git_dir(root, copy, copy / "repo" / ".git" / "worktrees" / "copy", marker)
        (copy / ".git").write_text("gitdir: evil/../../../repo/.git/worktrees/copy\n")
    else:
        # Git strips only CR/LF, so it opens the space-suffixed name, a link to the copy's.
        fake_git_dir(root, copy, copy / "spare" / "worktrees" / "copy", marker)
        Path(f"{real} ").symlink_to(copy / "spare" / "worktrees" / "copy")
        (copy / ".git").write_text(f"gitdir: {real} \n")
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(copy)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.output(copy, "status", "--porcelain")
    assert not marker.exists()


@needs_git
def test_linked_worktree_registered_as_its_own_folder_shares_the_baseline(
    tmp_path: Path,
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    other = tmp_path / "other"
    _git(root, "worktree", "add", "-q", "-b", "other", str(other))
    linked = register(git, other)
    independent_root = _repo(tmp_path / "independent")
    (independent_root / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(independent_root, "first")
    independent = register(git, independent_root)
    nested = tmp_path / "nested"
    track(git, other, linked, nested)  # a worktree row of the linked folder's registration
    assert git.changes(linked).state == "ready"
    assert git.head(nested)
    _git(root, "config", "guard.synthetic", "changed-after-baseline")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(linked)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.output(nested, "status", "--porcelain")
    # A repository of its own keeps the no-baseline behaviour.
    assert git.changes(independent).state == "ready"
    # A pointer the guard cannot follow leaves the question undecided: refused.
    (other / ".git").unlink()  # hidden on Windows: replaced rather than truncated
    (other / ".git").write_bytes(b"not a pointer\n")
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.changes(linked)


@needs_git
@pytest.mark.parametrize("bot", [False, True])
def test_relative_hooks_path_reads_as_the_main_folder_in_a_linked_worktree(
    tmp_path: Path, bot: bool
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / "myhooks").mkdir()
    (root / "myhooks" / "README").write_text("main hooks folder\n", encoding="utf-8")
    _git(root, "config", "core.hooksPath", "myhooks")
    rebaseline(git, root, workspace_id)
    assert git.head(root)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=bot)
    marker = tmp_path / "copy-hook-ran"
    (copy / "myhooks").mkdir()
    hook = copy / "myhooks" / "reference-transaction"
    hook.write_bytes(f'#!/bin/sh\necho ran > "{marker.as_posix()}"\n'.encode())
    hook.chmod(0o755)
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    git.git(copy, "update-ref", "refs/heads/probe", "HEAD")
    # A Bot copy runs no hooks; an ordinary worktree runs its own, as Git does.
    assert marker.exists() is not bot


def plant_hooks(folder: Path, marker: Path) -> None:
    """Hooks that append their name to `marker` when Git runs them."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("pre-commit", "post-commit", "reference-transaction"):
        hook = folder / name
        hook.write_bytes(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n".encode())
        hook.chmod(0o755)


@needs_git
def test_bot_copy_of_a_husky_folder_runs_no_hooks(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    ran = tmp_path / "main-hook-ran"
    plant_hooks(root / ".husky" / "_", ran)
    _git(root, "config", "core.hooksPath", ".husky/_")
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    planted = tmp_path / "planted-hook-ran"
    plant_hooks(copy / ".husky" / "_", planted)
    ran.unlink(missing_ok=True)  # the setup's own `worktree add`
    (copy / "b.txt").write_text("b\n", encoding="utf-8")
    git.output(copy, "add", "b.txt")
    git.output(copy, "commit", "-q", "-m", "in the copy")
    git.output(copy, "status", "--porcelain")
    with git.operation(copy):  # a nested command keeps its operation's hooks off
        git.git(copy, "update-ref", "refs/heads/probe", "HEAD")
    assert not ran.exists() and not planted.exists()
    # The main folder keeps its hooks.
    git.git(root, "update-ref", "refs/heads/probe-main", "HEAD")
    assert ran.read_text(encoding="utf-8").split() == ["reference-transaction"] * 2
    (root / ".husky" / "_" / "pre-commit").write_bytes(b"#!/bin/sh\nexit 0\n")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(copy)


@needs_git
def test_ordinary_worktrees_of_a_husky_folder_keep_their_own_hooks(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    ran = tmp_path / "main-hook-ran"
    plant_hooks(root / ".husky" / "_", ran)
    _git(root, "config", "core.hooksPath", ".husky/_")
    _git(root, "config", "core.attributesFile", ".husky/attributes")
    rebaseline(git, root, workspace_id)
    worktree = tmp_path / "worktree"
    track(git, root, workspace_id, worktree)  # an ordinary Code-view worktree
    other = tmp_path / "other"
    _git(root, "worktree", "add", "-q", "-b", "other", str(other))
    linked = register(git, other)  # a linked worktree registered as its own folder
    worktree_ran = tmp_path / "worktree-hook-ran"
    other_ran = tmp_path / "other-hook-ran"
    plant_hooks(worktree / ".husky" / "_", worktree_ran)
    plant_hooks(other / ".husky" / "_", other_ran)
    ran.unlink(missing_ok=True)  # the setup's own `worktree add`
    (worktree / "b.txt").write_text("b\n", encoding="utf-8")
    git.output(worktree, "add", "b.txt")
    git.output(worktree, "commit", "-q", "-m", "in the worktree")
    assert {"pre-commit", "post-commit"} <= set(worktree_ran.read_text(encoding="utf-8").split())
    assert git.changes(linked).state == "ready"
    git.git(other, "update-ref", "refs/heads/probe-other", "HEAD")
    assert other_ran.read_text(encoding="utf-8").split() == ["reference-transaction"] * 2
    # The main folder keeps its own hooks and was not run by the worktrees.
    assert not ran.exists()
    git.git(root, "update-ref", "refs/heads/probe-main", "HEAD")
    assert ran.read_text(encoding="utf-8").split() == ["reference-transaction"] * 2


@needs_git
@pytest.mark.parametrize("bot", [False, True])
def test_only_bot_copies_turn_hooks_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot: bool
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    ran = tmp_path / "hook-ran"
    plant_hooks(root / "hooks", ran)
    _git(root, "config", "core.hooksPath", (root / "hooks").as_posix())
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=bot)
    ran.unlink(missing_ok=True)
    native = winproc.git
    calls: list[tuple[str, ...]] = []

    def watched(cwd: Any, *args: str, **kwargs: Any) -> str:
        if "--git-dir" not in "".join(args):  # the guard's private parses
            calls.append(args)
        return native(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", watched)
    git.git(copy, "update-ref", "refs/heads/probe", "HEAD")
    git.git(root, "rev-parse", "HEAD")
    assert ran.exists() is not bot
    *asked, in_copy, in_main = calls
    assert in_main == (*HARDENING, "rev-parse", "HEAD")
    if bot:
        option, hooks = in_copy[len(HARDENING) : len(HARDENING) + 2]
        assert option == "-c" and hooks.startswith("core.hooksPath=")
        assert not Path(hooks.removeprefix("core.hooksPath=")).exists()
        assert in_copy[-3:] == ("update-ref", "refs/heads/probe", "HEAD")
        # The copy's settings are read once before its command, with hooks already off.
        (settings,) = asked
        assert settings[: len(HARDENING) + 2] == in_copy[: len(HARDENING) + 2]
        assert "config" in settings and "update-ref" not in settings
    else:
        assert asked == []
        assert in_copy == (*HARDENING, "update-ref", "refs/heads/probe", "HEAD")
    # No private hooks folder: a Bot copy's command refuses; elsewhere nothing changes.
    from viki_agent.code import code_checkout

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("no temporary folder")

    # Only the hooks folder's module: the guard's own private folders still work.
    monkeypatch.setattr(code_checkout, "tempfile", SimpleNamespace(TemporaryDirectory=unavailable))
    calls.clear()
    if bot:
        with pytest.raises(CodeError, match="^git_unavailable$"):
            git.output(copy, "rev-parse", "HEAD")
        assert calls == []
    else:
        assert git.output(copy, "rev-parse", "HEAD")


@needs_git
def test_relative_attributes_file_in_a_bot_copy_reads_the_main_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / "attrs").write_text("* text\n", encoding="utf-8")
    _git(root, "config", "core.attributesFile", "attrs")
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    (copy / "attrs").write_text("* diff=planted\n", encoding="utf-8")
    from viki_agent.code import git_guard

    original = git_guard._read
    opened: list[Path] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        opened.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(git_guard, "_read", read)
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    assert root / "attrs" in opened and copy / "attrs" not in opened
    assert git.head(copy)
    (root / "attrs").write_text("* binary\n", encoding="utf-8")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(copy)


def sparse(root: Path) -> None:
    """A cone-mode sparse checkout, which turns on per-worktree config."""
    (root / "dir").mkdir()
    (root / "dir" / "d.txt").write_text("d\n", encoding="utf-8")
    _commit(root, "dir")
    _git(root, "sparse-checkout", "set", "--cone", "dir")


@needs_git
def test_per_worktree_config_is_compared_by_its_role(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    sparse(root)
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    copied = root / ".git" / "worktrees" / "copy" / "config.worktree"
    assert copied.read_bytes() == (root / ".git" / "config.worktree").read_bytes()
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    assert git.head(copy)
    # A Bot's own per-worktree setting in its copy is still a change.
    _git(copy, "config", "--worktree", "guard.bot", "written-in-copy")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(copy)
    assert git.head(root)


@needs_git
def test_hook_mode_change_refuses(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(b"#!/bin/sh\nexit 0\n")
    hook.chmod(0o644)
    rebaseline(git, root, workspace_id)
    assert git.head(root)
    # The executable bit on POSIX; the read-only attribute on Windows. Same bytes either way.
    hook.chmod(0o755 if sys.platform != "win32" else stat.S_IREAD)
    assert hook.read_bytes() == b"#!/bin/sh\nexit 0\n"
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
def test_identical_bytes_rewrite_is_not_a_change(tmp_path: Path) -> None:
    root, git, _workspace_id = guarded(tmp_path)
    config = root / ".git" / "config"
    before = config.read_bytes()
    _git(root, "config", "user.name", "t")  # the value `_repo` set: a new file, same bytes
    assert config.read_bytes() == before
    assert git.head(root)


@needs_git
def test_onbranch_include_does_not_depend_on_the_branch(tmp_path: Path) -> None:
    root, git, workspace_id = guarded(tmp_path)
    (root / ".git" / "leaf").write_text("[guard]\n value = leaf\n", encoding="utf-8")
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write('[includeIf "onbranch:main"]\n path = leaf\n')
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    assert git.settings_fingerprint(copy) == git.settings_fingerprint(root)
    assert git.head(copy)
    _git(root, "switch", "-q", "-c", "other")
    assert git.head(root)
    (root / ".git" / "leaf").write_text("[guard]\n value = edited\n", encoding="utf-8")
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.head(copy)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)


@needs_git
@pytest.mark.parametrize("prefix", ["~", "%(prefix)"])
def test_home_and_prefix_values_are_refused_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    # A valid config at the literal path, so only the refusal keeps it out of the graph.
    planted = root / ".git" / prefix / "x"
    planted.parent.mkdir()
    planted.write_text("[guard]\n value = planted\n", encoding="utf-8")
    _git(root, "config", "include.path", f"{prefix}/x")
    original = git_guard._read
    opened: list[Path] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        opened.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(git_guard, "_read", read)
    with pytest.raises(CodeError, match="^git_settings_unavailable$"):
        git.settings_fingerprint(root)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.changes(workspace_id)
    assert opened and planted not in opened


@needs_git
@pytest.mark.parametrize("swap", ["hook", "pointer", "folder"])
def test_swaps_during_the_read_are_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    from viki_agent.code import git_guard

    hooks = root / ".git" / "hooks"
    for path in hooks.iterdir():
        path.unlink()
    rebaseline(git, root, workspace_id)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    target = copy if swap == "pointer" else root
    assert git.settings_fingerprint(target)
    native = git_guard._raw
    swapped = False

    def raw(folder: Path, private_git: Path, *args: str) -> str:
        nonlocal swapped
        if not swapped and "--includes" in args:  # after admission, hooks and pointer reads
            swapped = True
            if swap == "hook":
                (hooks / "post-checkout").write_bytes(b"#!/bin/sh\n")
            elif swap == "pointer":
                pointer = copy / ".git"  # hidden on Windows: rewritten in place
                spelling = pointer.read_bytes().replace(b"\n", b"\r\n")
                with pointer.open("r+b") as handle:
                    handle.write(spelling)
            else:
                hooks.rename(hooks.with_name("hooks-before"))
                hooks.mkdir()
        return native(folder, private_git, *args)

    monkeypatch.setattr(git_guard, "_raw", raw)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        git.settings_fingerprint(target)
    assert swapped


@needs_git
@pytest.mark.parametrize("bot", [False, True])
def test_status_skips_nested_repositories_only_in_bot_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot: bool
) -> None:
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=bot)
    nested = _repo(copy / "nested")
    (nested / "f.txt").write_text("nested\n", encoding="utf-8")
    _commit(nested, "nested")
    _git(copy, "add", "nested")  # a gitlink in the copy's index
    marker = tmp_path / "nested-filter-ran"
    with (nested / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f'[filter "evil"]\n\tclean = "touch {marker.as_posix()}; cat"\n')
    (nested / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    os.utime(nested / "f.txt", ns=(1, 1))
    registered = register(git, copy)
    native = winproc.git
    statuses: list[list[str]] = []

    def watched(cwd: Any, *args: str, **kwargs: Any) -> str:
        if "status" in args:
            statuses.append([arg for arg in args if arg.startswith("--ignore-submodules")])
        return native(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", watched)
    facts = worktree_probe.inspect(git, copy)
    assert facts.readable and facts.submodule
    assert git.changes(registered).state == "ready"
    # The cleanup's look keeps a gitlink's row, which alone shows one staged for deletion, and
    # the changes list hides gitlinks; neither enters the nested repository.
    in_copy = [["--ignore-submodules=dirty"], ["--ignore-submodules=all"]]
    assert statuses == (in_copy if bot else [[], []])
    # An ordinary worktree keeps Git's default recursion, the nested filter included.
    assert marker.exists() is not bot
    statuses.clear()
    git.changes(workspace_id)
    assert statuses == [[]]


@needs_git
def test_a_copy_pointer_rewritten_inside_an_operation_is_checked_before_the_next_command(
    tmp_path: Path,
) -> None:
    """One check serves an operation's commands, but a Bot can rewrite its copy's `.git` between
    two of them, and Git would follow it to settings nothing compared."""
    root, git, workspace_id = guarded(tmp_path)
    copy = tmp_path / "copy"
    track(git, root, workspace_id, copy, bot=True)
    marker = tmp_path / "filter-ran"
    planted = copy / "spare" / ".git" / "worktrees" / "copy"
    fake_git_dir(root, copy, planted, marker)
    with git.scope():
        assert git.head(copy)
        pointer = copy / ".git"
        pointer.unlink()  # hidden on Windows: replaced rather than truncated
        pointer.write_text(f"gitdir: {planted.as_posix()}\n", encoding="utf-8")
        with pytest.raises(CodeError, match="^git_settings_changed$"):
            git.output(copy, "status", "--porcelain")
    assert not marker.exists()
