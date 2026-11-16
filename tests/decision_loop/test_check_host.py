"""The saved-check host: real git in `tmp_path`, an injected clock, run rows made through the
store, and starts through a real `CheckRunner` with fake shells. Saved checks run on Windows
only, so the flag is patched on; the one real cmd.exe test runs only there."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND
from tests.workspace_code.test_check_runs import (
    WORKTREE,
    Rig,
    _empty_git_environment,  # noqa: F401 (autouse: no GIT_* variable, an empty git home)
    _forbidden,  # pyright: ignore[reportPrivateUsage]
    _head,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
    _settled,  # pyright: ignore[reportPrivateUsage]
    _spawner,  # pyright: ignore[reportPrivateUsage]
    windows_only,
)
from tests.workspace_code.test_code_git import (
    _commit,  # pyright: ignore[reportPrivateUsage]
    _git,  # pyright: ignore[reportPrivateUsage]
    _repo,  # pyright: ignore[reportPrivateUsage]
    needs_git,
)
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import protection, storage, winproc
from viki_agent.code import check_runs, code_checks
from viki_agent.code.check_runs import KEEP_RUNS, CheckRunner
from viki_agent.code.code_checks import CheckService, CodeRunError
from viki_agent.code.code_git import CodeGit
from viki_agent.decision_loop import check_host
from viki_agent.decision_loop.check_host import SavedCheckHost
from viki_agent.decision_loop.contracts import (
    CHECK_HEAD_SETTLE,
    CHECK_LOOK_EVERY,
    CheckFolder,
    CheckLook,
    DecisionLoopError,
)
from viki_agent.storage import CheckRow, SessionStore, StateError

pytestmark = needs_git

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
EVERY = CHECK_LOOK_EVERY
SETTLE = CHECK_HEAD_SETTLE
SECOND = timedelta(seconds=1)
EMPTY = CheckLook((), ())
HANG_GUARD_S = 5  # only for events that fire in milliseconds
_ORDER = itertools.count()


@pytest.fixture(autouse=True)
def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)


@pytest.fixture(autouse=True)
def _patient_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check runs that start or stop together each write the store from their own thread. A slow
    disk can hold its write lock past the store's busy wait, which no test here is about."""
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", HANG_GUARD_S)


def _host(rig: Rig) -> SavedCheckHost:
    return SavedCheckHost(rig.store, rig.workspaces, rig.git, rig.runner())


def _record(
    store: SessionStore,
    check_id: str,
    head: str | None,
    outcome: str = "passed",
    *,
    worktree_id: str | None = None,
) -> str:
    """One run of the check, newer than every run made before; the run's id."""
    check = store.get_check(check_id)
    assert check is not None
    order = next(_ORDER)
    stamp = f"2026-10-01T{order // 3600:02d}:{order // 60 % 60:02d}:{order % 60:02d}.000Z"
    run_id = str(uuid.uuid4())
    store.insert_check_run(
        {
            "run_id": run_id,
            "check_id": check_id,
            "workspace_id": check["workspace_id"],
            "worktree_id": worktree_id,
            "head_oid": head,
            "dirty": None if head is None else 0,
            "outcome": "running",
            "exit_code": None,
            "duration_ms": None,
            "output_bytes": None,
            "tail": None,
            "started_at": stamp,
            "ended_at": None,
        },
        keep=KEEP_RUNS,
    )
    if outcome != "running":
        store.finish_check_run(
            run_id,
            outcome=outcome,
            exit_code={"passed": 0, "failed": 1}.get(outcome),
            duration_ms=1,
            output_bytes=0,
            tail=None,
            ended_at=stamp,
            keep=KEEP_RUNS,
        )
    return run_id


def _checks(rig: Rig, workspace_id: str, count: int) -> list[str]:
    service = CheckService(rig.store, rig.workspaces)
    return [
        service.add_check(workspace_id, f"check {index}", f"echo {index}", 600).check_id
        for index in range(count)
    ]


def _ran(rig: Rig, count: int) -> list[str]:
    """`count` checks of the rig's folder, each run once, passed, at its current HEAD."""
    ids = _checks(rig, rig.workspace_id, count)
    head = _head(rig.folder)
    for check_id in ids:
        _record(rig.store, check_id, head)
    return ids


def _advance(root: Path) -> str:
    """A new commit on top; its id."""
    (root / "a.txt").write_text(f"{next(_ORDER)}\n", encoding="utf-8")
    _commit(root, "next")
    return _head(root)


def _more(rig: Rig, name: str) -> tuple[str, Path, str]:
    """A second trusted folder with one check run at its commit: id, root, check id."""
    root = _repo(rig.short / name)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _commit(root, "first")
    workspace_id = trusted(rig.workspaces, root)
    [check_id] = _checks(rig, workspace_id, 1)
    _record(rig.store, check_id, _head(root))
    return workspace_id, root, check_id


def _spy(monkeypatch: pytest.MonkeyPatch, git: CodeGit) -> list[tuple[Path, tuple[str, ...]]]:
    """Every Git call the reader makes through `CodeGit.git`: the folder and the arguments."""
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real = git.git

    def spy(root: Path, *args: str, **kwargs: Any) -> str:
        calls.append((Path(root), args))
        return real(root, *args, **kwargs)

    monkeypatch.setattr(git, "git", spy)
    return calls


def _looked(calls: list[tuple[Path, tuple[str, ...]]]) -> list[Path]:
    """The folders looked at: one entry per look, which starts with this `rev-parse`."""
    return [root for root, args in calls if "--is-inside-work-tree" in args]


def _snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    found: dict[str, tuple[int, bytes | None]] = {}
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(folder, name)
            body = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            found[path.relative_to(root).as_posix()] = (path.lstat().st_mtime_ns, body)
    return found


# --- the fact ---------------------------------------------------------------------------


def test_a_new_commit_lists_its_pending_checks_after_ten_minutes(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    host = _host(rig)

    assert host.read(NOW) == EMPTY
    assert host.read(NOW + EVERY) == EMPTY
    assert host.read(NOW + SETTLE - SECOND) == EMPTY  # nine minutes and fifty-nine seconds
    assert host.read(NOW + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", head, "waiting", (check_id,)),)
    )


def test_a_head_that_moves_restarts_the_settle(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    _advance(rig.folder)
    host = _host(rig)
    host.read(NOW)
    _git(rig.folder, "commit", "--amend", "-q", "-m", "amended")
    amended = _head(rig.folder)

    assert host.read(NOW + EVERY) == EMPTY  # sees the new HEAD, counted from here
    assert host.read(NOW + SETTLE) == EMPTY
    assert host.read(NOW + EVERY + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", amended, "waiting", (check_id,)),)
    )


@pytest.mark.parametrize(
    "never_counted", ["never run", "run in a worktree", "run without a commit"]
)
def test_only_checks_run_in_the_folder_before_count(tmp_path: Path, never_counted: str) -> None:
    rig = _rig(tmp_path)
    [counted] = _ran(rig, 1)
    [other] = _checks(rig, rig.workspace_id, 1)
    head = _head(rig.folder)
    if never_counted == "run in a worktree":
        rig.worktree()
        _record(rig.store, other, head, worktree_id=WORKTREE)
    elif never_counted == "run without a commit":
        _record(rig.store, other, head)  # an earlier run named a commit; the newest names none
        _record(rig.store, other, None)
    _advance(rig.folder)
    host = _host(rig)

    host.read(NOW)
    listed = host.read(NOW + SETTLE)

    assert [folder.pending for folder in listed.folders or ()] == [(counted,)]


@pytest.mark.parametrize(
    "never_counted", ["never run", "run in a worktree", "run without a commit"]
)
def test_a_folder_with_no_counted_check_lists_nothing(tmp_path: Path, never_counted: str) -> None:
    rig = _rig(tmp_path)
    [check_id] = _checks(rig, rig.workspace_id, 1)
    if never_counted == "run in a worktree":
        rig.worktree()
        _record(rig.store, check_id, _head(rig.folder), worktree_id=WORKTREE)
    elif never_counted == "run without a commit":
        _record(rig.store, check_id, _head(rig.folder))
        _record(rig.store, check_id, None)
    _advance(rig.folder)
    host = _host(rig)

    assert [host.read(NOW), host.read(NOW + SETTLE)] == [EMPTY, EMPTY]


@pytest.mark.parametrize(
    ("outcomes", "phase", "pending"),
    [
        (("running",), "running", ()),
        (("passed",), "passed", ()),
        (("failed",), "failed", ()),
        (("timed_out",), "failed", ()),
        (("cancelled",), "stopped", ()),
        (("interrupted",), "stopped", ()),
        (("passed", None), "waiting", (1,)),
        (("running", None), "running", (1,)),
        (("passed", "failed"), "failed", ()),
        (("failed", "cancelled"), "failed", ()),
        (("passed", "cancelled"), "stopped", ()),
    ],
)
def test_phases_follow_the_runs_at_the_commit(
    tmp_path: Path, outcomes: tuple[str | None, ...], phase: str, pending: tuple[int, ...]
) -> None:
    rig = _rig(tmp_path)
    ids = _ran(rig, len(outcomes))
    head = _advance(rig.folder)
    for check_id, outcome in zip(ids, outcomes, strict=True):
        if outcome is not None:
            _record(rig.store, check_id, head, outcome)
    folder = CheckFolder(rig.workspace_id, "repo", head, phase, tuple(ids[i] for i in pending))
    host = _host(rig)

    # Only a waiting folder waits out the settle.
    assert host.read(NOW) == (EMPTY if phase == "waiting" else CheckLook((folder,)))
    assert host.read(NOW + SETTLE) == CheckLook((folder,))


def test_a_run_in_a_worktree_at_the_commit_does_not_count(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    rig.worktree()
    head = _advance(rig.folder)
    _record(rig.store, check_id, head, worktree_id=WORKTREE)
    host = _host(rig)

    host.read(NOW)

    assert host.read(NOW + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", head, "waiting", (check_id,)),)
    )


def test_a_head_back_at_a_commit_an_earlier_run_named_reads_as_run(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    first = _head(rig.folder)
    second = _advance(rig.folder)
    _record(rig.store, check_id, second)  # the newest run now names the second commit
    _git(rig.folder, "reset", "-q", "--hard", first)
    host = _host(rig)

    assert host.read(NOW) == CheckLook((CheckFolder(rig.workspace_id, "repo", first, "passed"),))


@pytest.mark.parametrize(
    "marker",
    ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"],
)
def test_a_folder_mid_merge_or_rebase_lists_nothing(tmp_path: Path, marker: str) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    path = rig.folder / ".git" / marker
    if marker.startswith("rebase"):
        path.mkdir()
    else:
        path.write_text(head + "\n", encoding="utf-8")
    host = _host(rig)

    assert [host.read(NOW), host.read(NOW + SETTLE)] == [EMPTY, EMPTY]

    # Once it ends the settle counts from the first read that sees a plain HEAD.
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    later = NOW + SETTLE + EVERY
    assert host.read(later) == EMPTY
    assert host.read(later + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", head, "waiting", (check_id,)),)
    )


@pytest.mark.parametrize("shape", ["plain folder", "no commit", "below the top level"])
def test_a_folder_that_is_not_a_checkout_is_a_fact_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, shape: str
) -> None:
    rig = _rig(tmp_path, repo=False)
    if shape == "no commit":
        _git(rig.folder, "init", "-q", "-b", "main")
    elif shape == "below the top level":
        _git(rig.short, "init", "-q", "-b", "main")
    [check_id] = _checks(rig, rig.workspace_id, 1)
    _record(rig.store, check_id, "a" * 40)
    host = _host(rig)

    with caplog.at_level(logging.WARNING):
        assert [host.read(NOW), host.read(NOW + SETTLE)] == [EMPTY, EMPTY]

    assert [r for r in caplog.records if r.name == check_host.__name__] == []


# --- the read ---------------------------------------------------------------------------


def test_one_folder_is_read_per_call_and_each_at_most_every_five_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    _ran(rig, 1)
    second_id, second_root, _ = _more(rig, "second")
    roots = {rig.workspace_id: rig.folder, second_id: second_root}
    first, last = sorted(roots)
    calls = _spy(monkeypatch, rig.git)
    host = _host(rig)

    assert len(host.read(NOW).folders or ()) == 1
    assert _looked(calls) == [roots[first]]
    assert len(host.read(NOW).folders or ()) == 2
    assert _looked(calls) == [roots[first], roots[last]]

    host.read(NOW)
    host.read(NOW + EVERY - SECOND)
    assert len(_looked(calls)) == 2  # both are memoized

    host.read(NOW + EVERY)
    host.read(NOW + EVERY)
    host.read(NOW + EVERY)
    assert _looked(calls)[2:] == [roots[first], roots[last]]  # one per call, then none


def test_every_folder_read_before_stays_answered_until_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At a 15-minute cadence each read looks at one of two waiting folders, in turn, so the
    other's read is older than `every`: both stay answered, and neither item leaves the
    launcher."""
    rig = _rig(tmp_path)
    _ran(rig, 1)
    _advance(rig.folder)
    second_id, second_root, _ = _more(rig, "second")
    _advance(second_root)
    roots = {rig.workspace_id: rig.folder, second_id: second_root}
    calls = _spy(monkeypatch, rig.git)
    host = _host(rig)
    cadence = timedelta(minutes=15)
    host.read(NOW)
    host.read(NOW + cadence)

    for beat in range(2, 6):
        look = host.read(NOW + beat * cadence)
        assert [(f.workspace_id, f.phase) for f in look.folders or ()] == [
            (key, "waiting") for key in sorted(roots)
        ]
    looked = _looked(calls)
    assert len(looked) == 6 and looked[2:] == looked[:2] * 2  # one folder per read, in turn


def test_a_stale_marked_folder_is_read_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    _ran(rig, 1)
    second_id, second_root, _ = _more(rig, "second")
    roots = {rig.workspace_id: rig.folder, second_id: second_root}
    first, last = sorted(roots)
    host = _host(rig)
    host.read(NOW)
    host.read(NOW)
    calls = _spy(monkeypatch, rig.git)

    # Both are due and the first was read earlier, but a start or stop marked the last.
    host._stale.add(last)  # pyright: ignore[reportPrivateUsage]
    host.read(NOW + EVERY)
    assert _looked(calls) == [roots[last]]
    host.read(NOW + EVERY)
    assert _looked(calls) == [roots[last], roots[first]]  # the mark was cleared by its read


def test_a_started_run_shows_at_once_through_a_stale_mark(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    host = _host(rig)
    host.read(NOW)
    host.read(NOW + SETTLE - SECOND)  # not yet settled
    _record(rig.store, check_id, head, "running")
    host._stale.add(rig.workspace_id)  # pyright: ignore[reportPrivateUsage]

    listed = host.read(NOW + SETTLE - SECOND)

    assert [f.phase for f in listed.folders or ()] == ["running"]


def test_off_windows_nothing_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)
    _ran(rig, 1)
    _advance(rig.folder)
    host = _host(rig)
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", False)
    calls = _spy(monkeypatch, rig.git)
    touched: list[str] = []

    def touch(name: str, owner: object) -> None:
        def refuse(*_args: object, **_kwargs: object) -> object:
            touched.append(name)
            raise AssertionError(name)

        monkeypatch.setattr(owner, name, refuse)

    for name in ("list_checks", "last_check_runs", "check_runs_of", "check_run_states"):
        touch(name, rig.store)
    touch("list", rig.workspaces)
    touch("get", rig.workspaces)

    assert host.supported() is False
    assert host.read(NOW) == EMPTY
    assert host.read(NOW + SETTLE) == EMPTY
    assert host.folder(rig.workspace_id, NOW) is None
    assert calls == [] and touched == []


def test_the_read_opens_no_check_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    (rig.folder / "untracked.txt").write_text("loose\n", encoding="utf-8")
    (rig.folder / "a.txt").write_text("edited\n", encoding="utf-8")
    os.utime(rig.folder / "a.txt", (1_700_000_000, 1_700_000_000))
    host = _host(rig)
    calls = _spy(monkeypatch, rig.git)
    runs = rig.store.check_runs_of(check_id, KEEP_RUNS + 1)
    before = _snapshot(rig.folder)
    assert before[".git/index"][1]

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("opened a check")

    monkeypatch.setattr(code_checks, "unseal", refuse)
    monkeypatch.setattr(protection, "unprotect", refuse)
    host.read(NOW)
    listed = host.read(NOW + SETTLE)
    one = host.folder(rig.workspace_id, NOW)

    waiting = CheckFolder(rig.workspace_id, "repo", head, "waiting", (check_id,))
    assert listed == CheckLook((waiting,)) and one == waiting
    assert _snapshot(rig.folder) == before
    assert rig.store.check_runs_of(check_id, KEEP_RUNS + 1) == runs
    assert calls and all(args[0] in ("rev-parse", "symbolic-ref") for _, args in calls)
    assert not any("status" in args for _, args in calls)


@pytest.mark.parametrize("failure", ["git", "runs", "checks", "folders"])
def test_a_failed_read_names_its_failure_and_logs_no_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    host = _host(rig)
    secret = f"boom in {rig.folder}"

    def broken(*_args: object, **_kwargs: object) -> object:
        raise OSError(secret)

    if failure == "git":
        monkeypatch.setattr(winproc, "git", broken)
    elif failure == "runs":
        states = rig.store.check_run_states
        reads = itertools.count()

        def look_broken(workspace_id: str) -> object:
            if next(reads):  # the listing's run read works, the look's fails
                raise OSError(secret)
            return states(workspace_id)

        monkeypatch.setattr(rig.store, "check_run_states", look_broken)
    elif failure == "checks":
        monkeypatch.setattr(rig.store, "list_checks", broken)
    else:
        monkeypatch.setattr(rig.workspaces, "list", broken)

    with caplog.at_level(logging.WARNING):
        look = host.read(NOW)

    expected = ("checks_unknown",) if failure in ("checks", "folders") else ("checks_partial",)
    assert look == CheckLook(None if expected == ("checks_unknown",) else (), expected)
    ours = [r for r in caplog.records if r.name == check_host.__name__]
    assert len(ours) == 1 and ours[0].getMessage().startswith("decision check read failed: ")
    text = " ".join(record.getMessage() for record in caplog.records)
    for hidden in (str(rig.folder), rig.workspace_id, check_id, "boom"):
        assert hidden not in text


def test_a_failed_re_read_does_not_list_the_folder_from_its_earlier_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    _ran(rig, 1)
    host = _host(rig)
    assert len(host.read(NOW).folders or ()) == 1

    def broken(*_args: object, **_kwargs: object) -> object:
        raise OSError("gone")

    monkeypatch.setattr(winproc, "git", broken)

    assert host.read(NOW + EVERY) == CheckLook((), ("checks_partial",))


def test_untrusted_and_removed_folders_run_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    _ran(rig, 1)
    removed_id, removed_root, _ = _more(rig, "removed")
    untrusted = _repo(rig.short / "untrusted")
    (untrusted / "a.txt").write_text("x\n", encoding="utf-8")
    _commit(untrusted, "first")
    rig.workspaces.register_existing(COMMAND, str(untrusted), kind="code")
    calls = _spy(monkeypatch, rig.git)
    host = _host(rig)

    for _ in range(3):
        host.read(NOW)
    assert sorted(_looked(calls)) == sorted([rig.folder, removed_root])  # the untrusted one: none
    assert untrusted not in {root for root, _ in calls}

    rig.workspaces.remove(removed_id)
    after = host.read(NOW + EVERY)

    assert [f.workspace_id for f in after.folders or ()] == [rig.workspace_id]
    assert _looked(calls)[2:] == [rig.folder]  # only the kept folder, once more
    assert removed_root not in {root for root, _ in calls[len(calls) - 4 :]}


def test_folder_reads_one_folder_now_without_the_settle(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    host = _host(rig)

    waiting = CheckFolder(rig.workspace_id, "repo", head, "waiting", (check_id,))
    assert host.folder(rig.workspace_id, NOW) == waiting  # no memo, no ten minutes
    assert host.read(NOW) == EMPTY  # the same commit is not settled for the cycle's read

    _record(rig.store, check_id, head)  # a fresh look each call, not a remembered one
    assert host.folder(rig.workspace_id, NOW) == CheckFolder(
        rig.workspace_id, "repo", head, "passed"
    )
    assert host.folder("00000000-0000-4000-8000-000000000000", NOW) is None


def test_folder_answers_none_where_the_read_lists_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    (rig.folder / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")
    host = _host(rig)

    assert host.folder(rig.workspace_id, NOW) is None  # a merge is under way
    rig.store.delete_check(check_id)
    assert host.folder(rig.workspace_id, NOW) is None  # no check left to run


def test_a_second_read_waits_for_a_read_still_inside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted cycle's read can still run as the next cycle reads."""
    rig = _rig(tmp_path)
    _ran(rig, 1)
    second_id, second_root, _ = _more(rig, "second")
    roots = {rig.workspace_id: rig.folder, second_id: second_root}
    inside, second_inside, release = threading.Event(), threading.Event(), threading.Event()
    looked: list[Path] = []
    real = rig.git.git

    def held(root: Path, *args: str, **kwargs: Any) -> str:
        if "--is-inside-work-tree" in args:
            looked.append(Path(root))
            if len(looked) == 1:
                inside.set()
                release.wait(HANG_GUARD_S)
            else:
                second_inside.set()
        return real(root, *args, **kwargs)

    monkeypatch.setattr(rig.git, "git", held)
    host = _host(rig)
    threads = [threading.Thread(target=host.read, args=(NOW,)) for _ in range(2)]
    threads[0].start()
    assert inside.wait(HANG_GUARD_S)
    threads[1].start()
    # Without the lock the second read starts at once, on the folder the first still holds.
    second_inside.wait(0.05)
    held_read = list(looked)
    release.set()
    for thread in threads:
        thread.join(HANG_GUARD_S)

    assert held_read == [roots[min(roots)]]
    assert looked == [roots[key] for key in sorted(roots)]


# --- start and stop ------------------------------------------------------------


class _Held:
    """A shell whose command runs until it is killed, then exits 1."""

    def __init__(self) -> None:
        self.ended = threading.Event()

    def read(self) -> bytes:
        return b""

    def wait(self, timeout: float | None) -> int | None:
        return 1 if self.ended.wait(timeout) else None

    def kill_tree(self) -> None:
        self.ended.set()

    def release(self) -> None:
        self.ended.set()


def _holding(shells: list[_Held]) -> Callable[..., _Held]:
    def spawn(command: str, *, cwd: Path, env: dict[str, str]) -> _Held:
        shells.append(_Held())
        return shells[-1]

    return spawn


def _hosted(rig: Rig, start: Callable[..., Any]) -> tuple[SavedCheckHost, CheckRunner]:
    runner = rig.runner(timeout_s=HANG_GUARD_S, start=start)
    return SavedCheckHost(rig.store, rig.workspaces, rig.git, runner), runner


def _asked(monkeypatch: pytest.MonkeyPatch, runner: CheckRunner) -> list[tuple[str, str | None]]:
    """Every `CheckRunner.start` call from here on: the check and the worktree."""
    asked: list[tuple[str, str | None]] = []
    real = runner.start

    async def spy(check_id: str, worktree_id: str | None) -> Any:
        asked.append((check_id, worktree_id))
        return await real(check_id, worktree_id)

    monkeypatch.setattr(runner, "start", spy)
    return asked


def _settles(monkeypatch: pytest.MonkeyPatch, store: SessionStore) -> threading.Semaphore:
    """Released once for each run settled in the store from here on."""
    settled = threading.Semaphore(0)
    real = store.finish_check_run

    def finish(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        settled.release()

    monkeypatch.setattr(store, "finish_check_run", finish)
    return settled


async def _wait_settled(settled: threading.Semaphore, count: int = 1) -> None:
    for _ in range(count):
        assert await asyncio.to_thread(settled.acquire, timeout=HANG_GUARD_S)


async def _gone(runner: CheckRunner, run_id: str) -> None:
    """Until the live run has settled and left the runner's live set."""
    run = runner._live[run_id]  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(run.done.wait(), HANG_GUARD_S)


async def _all_gone(runner: CheckRunner) -> None:
    """Until every live run has settled and left the runner's live set."""
    live = list(runner._live.values())  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(asyncio.gather(*(run.done.wait() for run in live)), HANG_GUARD_S)


def _outcome(store: SessionStore, run_id: str) -> str:
    row = store.get_check_run(run_id)
    assert row is not None
    return row["outcome"]


async def test_start_runs_pending_checks_through_the_check_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    ids = _ran(rig, 2)
    head = _advance(rig.folder)
    spawned: list[str] = []
    host, runner = _hosted(rig, _spawner(spawned))
    asked = _asked(monkeypatch, runner)
    settled = _settles(monkeypatch, rig.store)
    monkeypatch.setattr(check_host, "MAX_STARTED_RUNS", 1)
    assert host.read(NOW) == EMPTY  # read, not yet settled

    assert await host.start(rig.workspace_id, head, ids) == 2

    assert asked == [(ids[0], None), (ids[1], None)] and spawned == ["echo 0", "echo 1"]
    await _wait_settled(settled, 2)
    rows = [rig.store.check_runs_of(check_id, 1)[0] for check_id in ids]
    assert [(r["worktree_id"], r["head_oid"], r["outcome"]) for r in rows] == [
        (None, head, "passed")
    ] * 2
    # The start marked the folder stale, so the next read looks again at once.
    assert host.read(NOW) == CheckLook((CheckFolder(rig.workspace_id, "repo", head, "passed"),))
    started = host._started  # pyright: ignore[reportPrivateUsage]
    assert list(started.values()) == [(rig.workspace_id, ids[1])]  # the newest one kept
    assert host.running() == 0


async def test_start_refuses_a_moved_commit_and_skips_checks_already_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    ids = _ran(rig, 2)
    old = _head(rig.folder)
    head = _advance(rig.folder)
    _, _, elsewhere = _more(rig, "second")
    host, runner = _hosted(rig, _spawner([]))
    asked = _asked(monkeypatch, runner)

    with pytest.raises(DecisionLoopError) as moved:
        await host.start(rig.workspace_id, old, ids)
    assert moved.value.code == "not_applicable" and asked == []

    _record(rig.store, ids[0], head)  # the user ran it at this commit since the read
    assert await host.start(rig.workspace_id, head, [*ids, elsewhere]) == 1
    assert asked == [(ids[1], None)]  # nor another folder's check

    (rig.folder / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")
    with pytest.raises(DecisionLoopError) as unreadable:
        await host.start(rig.workspace_id, head, ids)
    assert unreadable.value.code == "not_applicable" and len(asked) == 1


async def test_two_starts_at_once_run_a_check_once_per_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    spawned: list[str] = []
    host, runner = _hosted(rig, _spawner(spawned))
    settled = _settles(monkeypatch, rig.store)
    real_start = runner.start
    calls = 0

    async def start(check_id: str, worktree_id: str | None) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            await _wait_settled(settled)  # the first run ended before this one reached the runner
        return await real_start(check_id, worktree_id)

    monkeypatch.setattr(runner, "start", start)
    counts = await asyncio.gather(
        host.start(rig.workspace_id, head, [check_id]),
        host.start(rig.workspace_id, head, [check_id]),
    )
    await _wait_settled(settled)

    runs = [run for run in rig.store.check_runs_of(check_id, 5) if run["head_oid"] == head]
    assert counts == [1, 0] and len(runs) == 1 and len(spawned) == 1


@pytest.mark.parametrize("refusal", ["check_unreadable", "store failure"])
async def test_a_refused_check_is_not_asked_again_for_that_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """VIKI's own start, given the Stop all count as the cycle passes it."""
    rig = _rig(tmp_path)
    refused, limited, later, owners = _ran(rig, 4)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    if refusal == "check_unreadable":
        unseal = code_checks.unseal

        def broken_unseal(row: CheckRow) -> tuple[str, str]:
            if row["check_id"] == refused:
                raise CodeRunError("check_unreadable")
            return unseal(row)

        monkeypatch.setattr(code_checks, "unseal", broken_unseal)
    else:
        get_check = rig.store.get_check

        def broken_get(check_id: str) -> CheckRow | None:
            if check_id == refused:
                raise StateError("unavailable")
            return get_check(check_id)

        monkeypatch.setattr(rig.store, "get_check", broken_get)
    monkeypatch.setattr(check_runs, "MAX_LIVE_RUNS", 1)
    owner = await runner.start(owners, None)  # the user's own run takes the one slot
    asked = _asked(monkeypatch, runner)
    batch = [refused, limited, later]
    stops = host.stops()

    assert await host.start(rig.workspace_id, head, batch, stops=stops) == 0
    assert [check_id for check_id, _ in asked] == [refused, limited]  # `run_limit` ends it

    monkeypatch.setattr(check_runs, "MAX_LIVE_RUNS", 4)
    assert await host.start(rig.workspace_id, head, batch, stops=stops) == 2
    assert [check_id for check_id, _ in asked[2:]] == [limited, later]  # asked again later

    newer = _advance(rig.folder)  # a new commit asks it again
    assert await host.start(rig.workspace_id, newer, [refused], stops=stops) == 0
    assert [check_id for check_id, _ in asked[4:]] == [refused]

    await runner.cancel(owner["run_id"])
    assert await host.stop_all() == 2
    live = list(runner._live.values())  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(asyncio.gather(*(run.done.wait() for run in live)), HANG_GUARD_S)


async def test_a_folder_whose_checks_were_refused_is_not_looked_at_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [refused] = _ran(rig, 1)
    head = _advance(rig.folder)
    host, runner = _hosted(rig, _forbidden)
    unseal = code_checks.unseal

    def broken_unseal(row: CheckRow) -> tuple[str, str]:
        if row["check_id"] == refused:
            raise CodeRunError("check_unreadable")
        return unseal(row)

    monkeypatch.setattr(code_checks, "unseal", broken_unseal)
    asked = _asked(monkeypatch, runner)
    assert await host.start(rig.workspace_id, head, [refused], stops=host.stops()) == 0
    calls = _spy(monkeypatch, rig.git)

    # A cycle that still holds the item asks again: no Git call, no runner call.
    assert await host.start(rig.workspace_id, head, [refused], stops=host.stops()) == 0
    assert calls == [] and asked == [(refused, None)]


async def test_a_start_that_starts_nothing_does_not_mark_its_folder_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [refused] = _ran(rig, 1)
    head = _advance(rig.folder)
    host, runner = _hosted(rig, _forbidden)

    def broken_unseal(row: CheckRow) -> tuple[str, str]:
        raise CodeRunError("check_unreadable")

    monkeypatch.setattr(code_checks, "unseal", broken_unseal)
    _asked(monkeypatch, runner)
    assert await host.start(rig.workspace_id, head, [refused], stops=host.stops()) == 0
    host._stale.clear()  # pyright: ignore[reportPrivateUsage]

    # Every asked id is refused at this commit: no task is created, so no early read is due.
    assert await host.start(rig.workspace_id, head, [refused], stops=host.stops()) == 0
    assert host._stale == set()  # pyright: ignore[reportPrivateUsage]


async def test_a_spawn_that_failed_for_viki_is_not_retried_but_run_checks_asks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient refusal, here a spawn that fails once (`start_failed`), is remembered for
    VIKI's own start at that commit, so VIKI retries nothing; the user's own Run checks is not
    held by that memory: it asks the runner again, and the check runs."""
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    spawned: list[str] = []
    spawn = _spawner(spawned)
    failures = [OSError("locked")]

    def once_failing(command: str, *, cwd: Path, env: dict[str, str]) -> Any:
        if failures:
            raise failures.pop()
        return spawn(command, cwd=cwd, env=env)

    host, runner = _hosted(rig, once_failing)
    asked = _asked(monkeypatch, runner)
    settled = _settles(monkeypatch, rig.store)

    assert await host.start(rig.workspace_id, head, [check_id], stops=host.stops()) == 0
    assert await host.start(rig.workspace_id, head, [check_id], stops=host.stops()) == 0
    assert asked == [(check_id, None)] and spawned == []  # VIKI asked once at this commit

    assert await host.start(rig.workspace_id, head, [check_id]) == 1  # the user's Run checks
    assert asked == [(check_id, None)] * 2 and spawned == ["echo 0"]
    await _wait_settled(settled)


async def test_the_owners_refused_run_checks_asks_the_runner_at_each_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's Run checks remembers no refusal: each click meets the runner's own answer,
    and the cycle's read still finds the check waiting, so the item still offers Run checks."""
    rig = _rig(tmp_path)
    [refused] = _ran(rig, 1)
    head = _advance(rig.folder)
    host, runner = _hosted(rig, _forbidden)

    def broken_unseal(row: CheckRow) -> tuple[str, str]:
        raise CodeRunError("check_unreadable")

    monkeypatch.setattr(code_checks, "unseal", broken_unseal)
    asked = _asked(monkeypatch, runner)
    host.read(NOW)

    for _ in range(2):
        assert await host.start(rig.workspace_id, head, [refused]) == 0
    assert asked == [(refused, None)] * 2
    assert host.read(NOW + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", head, "waiting", (refused,)),)
    )


@pytest.mark.parametrize(
    ("sibling", "phase"), [("failed", "failed"), ("passed", "stopped"), (None, "stopped")]
)
async def test_a_check_refused_to_viki_reads_as_unfinished_not_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sibling: str | None, phase: str
) -> None:
    """Once VIKI's own start is refused at a commit, here by a spawn that fails, the cycle's read
    no longer lists that check as pending, so the item stops offering a run VIKI will not make: a
    sibling's failure still reports, a pass reads as not finished, and so does a check alone. The
    user's fresh look still finds it pending, so Run checks can still ask the runner."""
    rig = _rig(tmp_path)
    ids = _ran(rig, 1 if sibling is None else 2)
    refused = ids[-1]
    head = _advance(rig.folder)
    if sibling is not None:
        _record(rig.store, ids[0], head, sibling)
    spawn = _spawner([])

    def failing(command: str, *, cwd: Path, env: dict[str, str]) -> Any:
        if command == f"echo {len(ids) - 1}":  # the refused check's command
            raise OSError("locked")
        return spawn(command, cwd=cwd, env=env)

    host, _runner = _hosted(rig, failing)
    waiting = CheckFolder(rig.workspace_id, "repo", head, "waiting", (refused,))
    host.read(NOW)
    assert host.read(NOW + SETTLE) == CheckLook((waiting,))

    assert await host.start(rig.workspace_id, head, [refused], stops=host.stops()) == 0
    # The start marked the folder stale, so this read looks at it again at once.
    assert host.read(NOW + SETTLE) == CheckLook(
        (CheckFolder(rig.workspace_id, "repo", head, phase),)
    )
    assert host.folder(rig.workspace_id, NOW) == waiting


async def test_stop_all_during_a_start_batch_starts_no_further_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    ids = _ran(rig, 3)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    settled = _settles(monkeypatch, rig.store)
    entered, release = threading.Event(), threading.Event()
    snapshot = runner._snapshot  # pyright: ignore[reportPrivateUsage]

    def held(workspace_id: str, worktree_id: str | None) -> tuple[str | None, int | None]:
        if not entered.is_set():  # only the first check's spawn is held
            entered.set()
            release.wait(HANG_GUARD_S)
        return snapshot(workspace_id, worktree_id)

    monkeypatch.setattr(runner, "_snapshot", held)
    starting = asyncio.create_task(host.start(rig.workspace_id, head, ids))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    stopping = asyncio.create_task(host.stop_all())  # the user's Stop all, mid-spawn
    await asyncio.sleep(0)  # a yield, not a wait: Stop all has begun
    assert not stopping.done()  # it waits for the run being spawned
    release.set()

    assert await asyncio.wait_for(stopping, HANG_GUARD_S) == 1  # that run, recorded and cancelled
    assert await asyncio.wait_for(starting, HANG_GUARD_S) == 1  # and nothing after it
    await _wait_settled(settled)
    [run_id] = host._started  # pyright: ignore[reportPrivateUsage]
    assert len(shells) == 1 and shells[0].ended.is_set()
    assert _outcome(rig.store, run_id) == "cancelled"


async def test_a_start_given_a_count_older_than_a_stop_all_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle reads the count, then the user presses Stop all before the cycle reaches its
    start, with nothing of that commit's started yet. The cycle's start holds the commit, so the
    cycle Stop all wakes, given the count as it stands, starts none of it either; the user's own
    Run checks still runs both checks, and VIKI starts afresh on a new commit."""
    rig = _rig(tmp_path)
    ids = _ran(rig, 2)
    head = _advance(rig.folder)
    spawned: list[str] = []
    host, runner = _hosted(rig, _spawner(spawned))
    asked = _asked(monkeypatch, runner)
    settled = _settles(monkeypatch, rig.store)
    before = host.stops()
    calls = _spy(monkeypatch, rig.git)

    assert await host.stop_all() == 0  # the user's Stop all, after the caller read the count
    assert host.stops() == before + 1
    assert await host.start(rig.workspace_id, head, ids, stops=before) == 0
    assert await host.start(rig.workspace_id, head, ids, stops=host.stops()) == 0  # woken
    assert calls == [] and asked == [] and spawned == []

    assert await host.start(rig.workspace_id, head, ids) == 2  # the user's Run checks
    await _wait_settled(settled, 2)
    newer = _advance(rig.folder)
    assert await host.start(rig.workspace_id, newer, ids[:1], stops=host.stops()) == 1
    await _wait_settled(settled)
    assert spawned == ["echo 0", "echo 1", "echo 0"]


async def test_a_stopped_commit_is_not_started_again_by_vikis_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIKI's start runs four of the folder's six checks (`run_limit` ends the batch) and the
    user presses Stop all, which wakes a cycle that reads the two checks left as waiting. VIKI's
    next start at that commit starts nothing, without a look; the user's own Run checks still
    runs them, and VIKI starts afresh on a new commit."""
    rig = _rig(tmp_path)
    ids = _ran(rig, 6)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    assert await host.start(rig.workspace_id, head, ids, stops=host.stops()) == 4
    assert await host.stop_all() == 4
    await _all_gone(runner)
    left = host.folder(rig.workspace_id, NOW)
    assert left is not None and (left.phase, left.pending) == ("waiting", tuple(ids[4:]))
    calls = _spy(monkeypatch, rig.git)
    asked = _asked(monkeypatch, runner)

    assert await host.start(rig.workspace_id, head, left.pending, stops=host.stops()) == 0
    assert calls == [] and asked == [] and len(shells) == 4

    assert await host.start(rig.workspace_id, head, left.pending) == 2  # the user's Run checks
    for shell in shells:
        shell.release()
    await _all_gone(runner)
    newer = _advance(rig.folder)
    assert await host.start(rig.workspace_id, newer, ids[:1], stops=host.stops()) == 1
    assert [check_id for check_id, _ in asked] == [*ids[4:], ids[0]] and len(shells) == 7
    assert await host.stop_all() == 1
    await _all_gone(runner)


@pytest.mark.parametrize("stop", ["stop all", "stop"])
async def test_a_stop_holds_a_commit_whose_checks_were_still_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """The user stops while VIKI's start is still looking at the folder, before anything has
    spawned: that start starts nothing, and VIKI's next start at the commit, given the count as it
    stands, starts nothing either, without a look; the user's own Run checks still runs them."""
    rig = _rig(tmp_path)
    ids = _ran(rig, 2)
    head = _advance(rig.folder)
    spawned: list[str] = []
    host, runner = _hosted(rig, _spawner(spawned))
    settled = _settles(monkeypatch, rig.store)
    entered, release = threading.Event(), threading.Event()
    look = host.folder

    def held(workspace_id: str, now: datetime) -> CheckFolder | None:
        entered.set()
        release.wait(HANG_GUARD_S)
        return look(workspace_id, now)

    monkeypatch.setattr(host, "folder", held)
    starting = asyncio.create_task(host.start(rig.workspace_id, head, ids, stops=host.stops()))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    stopping = host.stop_all() if stop == "stop all" else host.stop(rig.workspace_id)
    assert await stopping == 0  # nothing had spawned, so nothing was live
    release.set()
    assert await asyncio.wait_for(starting, HANG_GUARD_S) == 0
    monkeypatch.setattr(host, "folder", look)
    asked = _asked(monkeypatch, runner)
    calls = _spy(monkeypatch, rig.git)

    assert await host.start(rig.workspace_id, head, ids, stops=host.stops()) == 0
    assert calls == [] and asked == [] and spawned == []
    assert await host.start(rig.workspace_id, head, ids) == 2  # the user's Run checks
    await _wait_settled(settled, 2)


async def test_stop_all_holds_a_commit_whose_checks_were_still_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIKI's start ended at `run_limit` with none of its own started, as when the user's runs
    fill every slot, so its checks wait for a later cycle. Stop all pressed between cycles holds
    that commit: once a slot frees, VIKI's next start starts nothing there, while the user's own
    Run checks still can."""
    rig = _rig(tmp_path)
    *ids, owners = _ran(rig, 3)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    monkeypatch.setattr(check_runs, "MAX_LIVE_RUNS", 1)
    owner = await runner.start(owners, None)  # the user's own run, from the Checks tab
    asked = _asked(monkeypatch, runner)
    assert await host.start(rig.workspace_id, head, ids, stops=host.stops()) == 0
    assert asked == [(ids[0], None)]  # `run_limit`: asked again at a later cycle

    assert await host.stop_all() == 0  # no run of VIKI's to cancel
    await runner.cancel(owner["run_id"])
    await _gone(runner, owner["run_id"])
    monkeypatch.setattr(check_runs, "MAX_LIVE_RUNS", 4)
    assert await host.start(rig.workspace_id, head, ids, stops=host.stops()) == 0
    assert asked == [(ids[0], None)] and len(shells) == 1

    assert await host.start(rig.workspace_id, head, ids) == 2  # the user's Run checks
    assert await host.stop_all() == 2
    await _all_gone(runner)


async def test_the_host_reads_no_run_output_and_counts_its_running_runs_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read, the fresh look, a start and the running count read where each run ran, its
    commit and its outcome, never its sealed output tail, and the count asks the store once for
    every run the host remembers."""
    rig = _rig(tmp_path)
    ids = _ran(rig, 3)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    for name in ("check_runs_of", "last_check_runs", "get_check_run"):
        monkeypatch.setattr(rig.store, name, _forbidden)
    counted: list[int] = []
    count = rig.store.count_running_check_runs

    def counting(run_ids: list[str]) -> int:
        counted.append(len(run_ids))
        return count(run_ids)

    monkeypatch.setattr(rig.store, "count_running_check_runs", counting)
    waiting = CheckFolder(rig.workspace_id, "repo", head, "waiting", tuple(ids))

    host.read(NOW)
    assert host.read(NOW + SETTLE) == CheckLook((waiting,))
    assert host.folder(rig.workspace_id, NOW) == waiting
    assert await host.start(rig.workspace_id, head, ids) == 3
    assert host.running() == 3 and counted == [3]
    assert await host.stop_all() == 3
    await _all_gone(runner)


async def test_a_cancelled_start_leaves_its_run_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    entered, release = threading.Event(), threading.Event()
    snapshot = runner._snapshot  # pyright: ignore[reportPrivateUsage]

    def held(workspace_id: str, worktree_id: str | None) -> tuple[str | None, int | None]:
        entered.set()
        release.wait(HANG_GUARD_S)
        return snapshot(workspace_id, worktree_id)

    monkeypatch.setattr(runner, "_snapshot", held)
    caller = asyncio.create_task(host.start(rig.workspace_id, head, [check_id]))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    caller.cancel()  # the caller leaves while the runner is inside `_launch`
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    tasks = set(host._tasks)  # pyright: ignore[reportPrivateUsage]
    assert tasks  # the start went on without its caller
    await asyncio.wait(tasks, timeout=HANG_GUARD_S)

    assert host._tasks == set()  # pyright: ignore[reportPrivateUsage]
    [run_id] = host._started  # pyright: ignore[reportPrivateUsage]
    assert host.running() == 1 and len(shells) == 1
    assert await host.stop(rig.workspace_id) == 1  # recorded, so Stop reaches it
    await _gone(runner, run_id)
    assert _outcome(rig.store, run_id) == "cancelled"  # its supervisor settled it


async def test_stop_cancels_only_runs_started_through_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    mine, owners = _ran(rig, 2)
    head = _advance(rig.folder)
    second_id, second_root, other = _more(rig, "second")
    second_head = _advance(second_root)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    owner = await runner.start(owners, None)  # the user's own run, from the Checks tab
    assert await host.start(rig.workspace_id, head, [mine]) == 1
    assert await host.start(second_id, second_head, [other]) == 1
    first, second = host._started  # pyright: ignore[reportPrivateUsage]
    assert host.running() == 2

    assert await host.stop(rig.workspace_id) == 1
    await _gone(runner, first)
    assert host.running() == 1

    assert await host.stop_all() == 1  # the first has ended already
    await _gone(runner, second)
    assert host.running() == 0
    assert [_outcome(rig.store, run_id) for run_id in (first, second)] == ["cancelled"] * 2
    assert runner.running(owners) and _outcome(rig.store, owner["run_id"]) == "running"

    await runner.cancel(owner["run_id"])
    await _gone(runner, owner["run_id"])


async def test_off_windows_start_is_unavailable_and_stop_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    [check_id, later] = _ran(rig, 2)
    head = _advance(rig.folder)
    shells: list[_Held] = []
    host, runner = _hosted(rig, _holding(shells))
    assert await host.start(rig.workspace_id, head, [check_id]) == 1
    [run_id] = host._started  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", False)
    asked = _asked(monkeypatch, runner)
    with monkeypatch.context() as patched:
        for name in (
            "list_checks",
            "last_check_runs",
            "check_runs_of",
            "check_run_states",
            "get_check",
        ):
            patched.setattr(rig.store, name, _forbidden)
        patched.setattr(rig.workspaces, "get", _forbidden)

        with pytest.raises(DecisionLoopError) as refused:
            await host.start(rig.workspace_id, head, [later])
        assert refused.value.code == "unavailable" and asked == []
        assert await host.stop(rig.workspace_id) == 0
        assert await host.stop_all() == 0
    assert runner.running(check_id)  # the runner refused `unsupported_platform`

    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)
    assert await host.stop_all() == 1
    await _gone(runner, run_id)


async def test_an_unexpected_start_failure_logs_only_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    [check_id] = _ran(rig, 1)
    head = _advance(rig.folder)
    host, runner = _hosted(rig, _forbidden)
    secret = f"boom in {rig.folder}"

    async def broken(*_args: object) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(runner, "start", broken)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        await host.start(rig.workspace_id, head, [check_id])

    ours = [r.getMessage() for r in caplog.records if r.name == check_host.__name__]
    assert ours == ["decision check start failed: RuntimeError"]
    assert host._tasks == set()  # pyright: ignore[reportPrivateUsage]
    text = " ".join(record.getMessage() for record in caplog.records)
    for hidden in (str(rig.folder), rig.workspace_id, check_id, head, "boom"):
        assert hidden not in text


@windows_only
async def test_a_real_check_runs_through_the_host_on_windows(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    check_id = rig.check("exit /b 0")
    _record(rig.store, check_id, _head(rig.folder))
    head = _advance(rig.folder)
    runner = rig.runner(timeout_s=HANG_GUARD_S)
    host = SavedCheckHost(rig.store, rig.workspaces, rig.git, runner)

    assert await host.start(rig.workspace_id, head, [check_id]) == 1
    await _settled(runner, check_id)

    assert host.folder(rig.workspace_id, NOW) == CheckFolder(
        rig.workspace_id, "repo", head, "passed"
    )
    assert host.running() == 0
