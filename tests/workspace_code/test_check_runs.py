"""The check runner: real cmd.exe runs in a trusted code folder with a commit, in its worktree and
in a plain folder; the refusals; the record, its sealed tail and the output offsets.

Process tests skip off Windows and poll real OS state (a run settles when its process ends)
under `HANG_GUARD_S`; each ends its own process trees through the runner or the Job."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.app.test_workspaces import service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import protection, winproc
from viki_agent.code import check_runs, code_checks
from viki_agent.code.check_output import Slice
from viki_agent.code.check_runs import KEEP_RUNS, SETTLE_WAIT_S, CheckRunner
from viki_agent.code.code_checks import CheckService, CodeRunError
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import SessionRoots
from viki_agent.storage import CheckRow, CheckRunRow, SessionStore
from viki_agent.workspaces import WorkspaceService

HANG_GUARD_S = 10.0
windows_only = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("git") is None,
    reason="checks run on Windows only, and these need git",
)
WORKTREE = "00000000-0000-4000-8000-000000000001"
UNKNOWN = "88888888-8888-4888-8888-888888888888"
BRANCH = "viki/20261004-e07a"
STAMP = "2026-10-04T00:00:00.000Z"
# A grandchild that reports its pid and lingers, started with `start /b` so it outlives cmd.exe.
_SLEEPER = "import os,time;print(os.getpid(),flush=True);time.sleep(60)"
# A child that leaves a lingering grandchild on the shared stdout, prints its pid and exits 0.
_LEAVER = (
    "import subprocess,sys;"
    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
    "print(p.pid,flush=True)"
)


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable (each makes the snapshot null), and an empty home, so the
    user's global Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal off Windows is its own test; the tests that start nothing run everywhere."""
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)


@dataclass
class Rig:
    store: SessionStore
    workspaces: WorkspaceService
    git: CodeGit
    roots: SessionRoots
    workspace_id: str
    folder: Path
    short: Path  # a short folder for worktrees: Windows MAX_PATH under the default basetemp
    starts: list[winproc.ShellRun] = field(default_factory=list)

    def runner(
        self, *, timeout_s: float = HANG_GUARD_S, start: Callable[..., Any] | None = None
    ) -> CheckRunner:
        return CheckRunner(
            self.store,
            self.workspaces,
            self.git,
            self.roots,
            start=start or self.record,
            timeout_of=lambda _s: timeout_s,
        )

    def record(self, command: str, *, cwd: Path, env: dict[str, str]) -> winproc.ShellRun:
        shell = winproc.start_shell(command, cwd=cwd, env=env)
        self.starts.append(shell)
        return shell

    def check(self, command: str, name: str = "check") -> str:
        service = CheckService(self.store, self.workspaces)
        return service.add_check(self.workspace_id, name, command, 600).check_id

    def row(self, path: Path) -> None:
        self.store.insert_worktree(
            {
                "worktree_id": WORKTREE,
                "workspace_id": self.workspace_id,
                "path": str(path),
                "branch": BRANCH,
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": STAMP,
                "updated_at": STAMP,
            },
            limit=32,
        )
        self.store.update_worktree_state(WORKTREE, "creating", "ready", STAMP)

    def worktree(self) -> Path:
        """A real `git worktree add` of the folder and its `ready` row."""
        path = self.short / "w"
        _git(self.folder, "worktree", "add", "-q", "-b", BRANCH, str(path), "HEAD")
        self.row(path)
        return path


def _rig(tmp_path: Path, *, repo: bool = True) -> Rig:
    short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
    folder = short / "repo"
    if repo:
        _repo(folder)
        (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
        _commit(folder, "first")
    else:
        folder.mkdir()
    store = store_at(tmp_path)
    workspaces = service_at(tmp_path, store, git=True)
    workspace_id = trusted(workspaces, folder)
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    roots = SessionRoots(store, workspaces, exists=lambda _sid: False, actor=lambda _sid: None)
    return Rig(store, workspaces, git, roots, workspace_id, folder, short)


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").strip()


async def _settled(runner: CheckRunner, check_id: str) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while runner.running(check_id):
            await asyncio.sleep(0.02)  # a run settles when its real process ends; poll it


async def _run(
    rig: Rig, runner: CheckRunner, check_id: str, worktree_id: str | None = None
) -> tuple[CheckRunRow, CheckRunRow]:
    """The answer of the start, and the row once the run settled."""
    started = await runner.start(check_id, worktree_id)
    await _settled(runner, check_id)
    row = rig.store.get_check_run(started["run_id"])
    assert row is not None
    return started, row


async def _text(runner: CheckRunner, run_id: str) -> str:
    _row, part, done = await runner.output(run_id, 0)
    assert done
    return part.text


async def _until_output(runner: CheckRunner, run_id: str, needle: str) -> Slice:
    async with asyncio.timeout(HANG_GUARD_S):
        while needle not in (part := (await runner.output(run_id, 0))[1]).text:
            await asyncio.sleep(0.02)  # output arrives from a real process
    return part


async def _until_dead(pid: int) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while winproc.process_start_time(pid) is not None:
            await asyncio.sleep(0.05)  # kill-on-close is asynchronous; poll the real OS state


async def _refusal(call: Awaitable[Any]) -> str:
    with pytest.raises(CodeRunError) as refused:
        await call
    return refused.value.code


def _finished(check_id: str, workspace_id: str, index: int) -> CheckRunRow:
    stamp = f"2026-01-01T00:00:{index:02d}.000Z"
    return {
        "run_id": f"00000000-0000-4000-8000-{index:012d}",
        "check_id": check_id,
        "workspace_id": workspace_id,
        "worktree_id": None,
        "head_oid": None,
        "dirty": None,
        "outcome": "passed",
        "exit_code": 0,
        "duration_ms": 1,
        "output_bytes": 0,
        "tail": None,
        "started_at": stamp,
        "ended_at": stamp,
    }


# --- runs and their record ----------------------------------------------------------------------


@windows_only
async def test_a_failed_run_records_its_exit_code_output_and_head(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    check_id = rig.check("echo viki& exit /b 3")
    runner = rig.runner()
    started, row = await _run(rig, runner, check_id)
    assert started["outcome"] == "running" and started["ended_at"] is None
    assert (row["outcome"], row["exit_code"], row["output_bytes"]) == ("failed", 3, 6)
    assert (row["head_oid"], row["dirty"]) == (_head(rig.folder), 0)
    assert row["duration_ms"] is not None and row["ended_at"] is not None
    assert await _text(runner, row["run_id"]) == "viki\r\n"
    # The stored tail is sealed under the run's own id.
    assert row["tail"] is not None and b"viki" not in row["tail"]
    context = b"VIKI check run v1\0" + row["run_id"].encode("ascii")
    assert protection.unprotect(row["tail"], context=context) == b"viki\r\n"


@windows_only
async def test_a_changed_file_records_uncommitted_changes(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("changed\n", encoding="utf-8")
    _started, row = await _run(rig, rig.runner(), rig.check("exit /b 0"))
    assert (row["outcome"], row["exit_code"]) == ("passed", 0)
    assert (row["head_oid"], row["dirty"]) == (_head(rig.folder), 1)


@windows_only
async def test_a_folder_that_is_not_a_repository_records_no_git_state(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    _started, row = await _run(rig, rig.runner(), rig.check("exit /b 0"))
    assert (row["outcome"], row["head_oid"], row["dirty"]) == ("passed", None, None)


@windows_only
async def test_a_worktree_run_runs_in_the_worktree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    path = rig.worktree()
    runner = rig.runner()
    _started, row = await _run(rig, runner, rig.check("cd"), WORKTREE)
    assert row["worktree_id"] == WORKTREE
    assert (row["head_oid"], row["dirty"]) == (_head(path), 0)
    printed = (await _text(runner, row["run_id"])).strip()
    assert os.path.normcase(printed) == os.path.normcase(str(path))


@windows_only
async def test_a_run_past_its_time_limit_is_timed_out_and_its_tree_ended(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    runner = rig.runner(timeout_s=0.05)
    _started, row = await _run(rig, runner, rig.check("ping -n 30 127.0.0.1 >nul"))
    assert (row["outcome"], row["exit_code"]) == ("timed_out", None)
    await _until_dead(rig.starts[0].pid)


@windows_only
async def test_cancel_ends_the_whole_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)

    def held(command: str, *, cwd: Path, env: dict[str, str]) -> winproc.ShellRun:
        # The release would end the grandchild anyway; holding it shows that cancel did.
        shell = rig.record(command, cwd=cwd, env=env)
        monkeypatch.setattr(shell, "release", lambda: None)
        return shell

    command = f'start /b "" "{sys.executable}" -c "{_SLEEPER}" & ping -n 30 127.0.0.1 >nul'
    check_id = rig.check(command)
    runner = rig.runner(start=held)
    run = await runner.start(check_id, None)
    try:
        grandchild = int((await _until_output(runner, run["run_id"], "\n")).text)
        assert (await runner.cancel(run["run_id"]))["outcome"] == "running"
        await _settled(runner, check_id)
        await _until_dead(grandchild)
    finally:
        for shell in rig.starts:
            shell.job.close()  # the release the test held
    row = rig.store.get_check_run(run["run_id"])
    assert row is not None and (row["outcome"], row["exit_code"]) == ("cancelled", None)
    assert (await runner.cancel(run["run_id"]))["outcome"] == "cancelled"  # the stored row


@windows_only
async def test_a_passed_run_ends_what_its_command_left_running(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    runner = rig.runner()
    _started, row = await _run(rig, runner, rig.check(f'"{sys.executable}" -c "{_LEAVER}"'))
    assert row["outcome"] == "passed"
    await _until_dead(int(await _text(runner, row["run_id"])))


@windows_only
async def test_output_continues_from_a_live_offset_after_the_settle(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    check_id = rig.check("echo viki& ping -n 30 127.0.0.1 >nul")
    runner = rig.runner()
    run = await runner.start(check_id, None)
    try:
        live = await _until_output(runner, run["run_id"], "viki")
        assert (live.start, live.next, live.dropped) == (0, 6, False)
        threads = {t.name for t in threading.enumerate() if t.daemon}
        assert {"viki-check-supervisor", "viki-check-reader"} <= threads
    finally:
        await runner.cancel(run["run_id"])
        await _settled(runner, check_id)
    row, rest, done = await runner.output(run["run_id"], live.next)
    assert (rest.start, rest.next, rest.text, done) == (6, 6, "", True)
    assert row["output_bytes"] == 6
    assert await _text(runner, run["run_id"]) == "viki\r\n"


@windows_only
async def test_a_run_sees_no_claude_or_anthropic_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_E07_PROBE", "x")
    monkeypatch.setenv("CLAUDE_CODE_E07_PROBE", "x")
    monkeypatch.setenv("VIKI_E07_PROBE", "x")
    rig = _rig(tmp_path)
    runner = rig.runner()
    _started, row = await _run(rig, runner, rig.check("set"))
    output = await _text(runner, row["run_id"])
    names = [line.partition("=")[0].upper() for line in output.splitlines()]
    assert "VIKI_E07_PROBE" in names
    assert not [name for name in names if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))]


# --- limits, deletes and the record's lifetime --------------------------------------------------


@windows_only
async def test_a_fifth_live_run_and_a_second_run_of_one_check_are_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ids = [rig.check("ping -n 30 127.0.0.1 >nul", f"check {index}") for index in range(5)]
    runner = rig.runner()
    try:
        await runner.start(ids[0], None)
        assert await _refusal(runner.start(ids[0], None)) == "check_running"
        for check_id in ids[1:4]:
            await runner.start(check_id, None)
        assert await _refusal(runner.start(ids[4], None)) == "run_limit"
        assert not runner.running(ids[4])
    finally:
        await runner.close()
    assert len(rig.starts) == 4


@windows_only
async def test_close_settles_live_runs_as_interrupted(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ids = [rig.check("ping -n 30 127.0.0.1 >nul", f"check {index}") for index in range(2)]
    runner = rig.runner()
    runs = [await runner.start(check_id, None) for check_id in ids]
    began = time.monotonic()
    await runner.close()
    assert time.monotonic() - began < SETTLE_WAIT_S
    for run in runs:
        row = rig.store.get_check_run(run["run_id"])
        assert row is not None and (row["outcome"], row["exit_code"]) == ("interrupted", None)
    for shell in rig.starts:
        await _until_dead(shell.pid)


@windows_only
async def test_the_newest_twenty_finished_runs_are_kept(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    check_id = rig.check("exit /b 0")
    for index in range(KEEP_RUNS):
        rig.store.insert_check_run(_finished(check_id, rig.workspace_id, index), keep=100)
    runner = rig.runner()
    started, _row = await _run(rig, runner, check_id)  # its settle trims the oldest
    runs = await runner.runs(check_id)
    assert len(runs) == KEEP_RUNS
    assert runs[0]["run_id"] == started["run_id"]
    assert {r["run_id"] for r in runs[1:]} == {
        _finished(check_id, rig.workspace_id, index)["run_id"] for index in range(1, KEEP_RUNS)
    }


@windows_only
async def test_delete_refuses_a_live_run_and_removes_a_settled_one(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    check_id = rig.check("ping -n 30 127.0.0.1 >nul")
    runner = rig.runner()
    run = await runner.start(check_id, None)
    try:
        assert await _refusal(runner.delete(check_id)) == "check_running"
    finally:
        await runner.cancel(run["run_id"])
        await _settled(runner, check_id)
    await runner.delete(check_id)
    assert rig.store.get_check(check_id) is None
    assert rig.store.get_check_run(run["run_id"]) is None
    assert await _refusal(runner.delete(check_id)) == "unknown_check"
    assert await _refusal(runner.runs(check_id)) == "unknown_check"


# --- refusals that start nothing (every OS) -----------------------------------------------------


def _forbidden(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("called")


@pytest.mark.usefixtures("windows")
async def test_a_blocked_worktree_refuses_without_a_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    path = rig.short / "w"
    path.mkdir()
    rig.row(path)
    check_id = rig.check("exit /b 0")
    runner = rig.runner(start=_forbidden)
    rig.roots.block(WORKTREE)
    assert await _refusal(runner.start(check_id, WORKTREE)) == "worktree_not_ready"
    assert not runner.running(check_id) and not runner.in_worktree(WORKTREE)


@pytest.mark.usefixtures("windows")
async def test_a_start_that_fails_to_spawn_leaves_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    path = rig.short / "w"
    path.mkdir()
    rig.row(path)
    check_id = rig.check("exit /b 0")
    calls: list[str] = []

    def fail(command: str, *, cwd: Path, env: dict[str, str]) -> winproc.ShellRun:
        calls.append(command)
        raise OSError("no")

    runner = rig.runner(start=fail)
    assert await _refusal(runner.start(check_id, WORKTREE)) == "start_failed"
    assert calls == ["exit /b 0"]
    assert not runner.running(check_id) and not runner.in_worktree(WORKTREE)
    assert rig.store.check_runs_of(check_id, 100) == []


class _Exited:
    """A shell whose command has already exited 0 with no output."""

    def read(self) -> bytes:
        return b""

    def wait(self, _timeout: float | None) -> int:
        return 0

    def kill_tree(self) -> None:
        pass

    def release(self) -> None:
        pass


def _spawner(spawned: list[str]) -> Callable[..., Any]:
    def spawn(command: str, *, cwd: Path, env: dict[str, str]) -> _Exited:
        spawned.append(command)
        return _Exited()

    return spawn


@pytest.mark.usefixtures("windows")
async def test_a_check_deleted_before_the_reservation_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    read, release = threading.Event(), threading.Event()
    get_check = rig.store.get_check

    def held(wanted: str) -> CheckRow | None:
        row = get_check(wanted)
        if not read.is_set():  # only the first read, the start's own, waits
            read.set()
            release.wait(HANG_GUARD_S)
        return row

    monkeypatch.setattr(rig.store, "get_check", held)
    spawned: list[str] = []
    runner = rig.runner(start=_spawner(spawned))
    start = asyncio.create_task(runner.start(check_id, None))
    assert await asyncio.to_thread(read.wait, HANG_GUARD_S)
    await runner.delete(check_id)  # nothing is reserved yet, so the delete completes
    release.set()
    assert await _refusal(start) == "unknown_check"
    assert spawned == [] and not runner.running(check_id)
    assert rig.store.check_runs_of(check_id, 100) == []


@pytest.mark.usefixtures("windows")
async def test_trust_revoked_during_the_snapshot_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    spawned: list[str] = []
    runner = rig.runner(start=_spawner(spawned))
    entered, release = threading.Event(), threading.Event()
    snapshot = runner._snapshot

    def held(workspace_id: str, worktree_id: str | None) -> tuple[str | None, int | None]:
        entered.set()
        release.wait(HANG_GUARD_S)
        return snapshot(workspace_id, worktree_id)

    monkeypatch.setattr(runner, "_snapshot", held)
    start = asyncio.create_task(runner.start(check_id, None))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    rig.workspaces.set_policy(str(uuid.uuid4()), rig.workspace_id, 2, "existing_readonly", [])
    release.set()
    assert await _refusal(start) == "trust_required"
    assert spawned == [] and not runner.running(check_id)
    assert rig.store.check_runs_of(check_id, 100) == []


@pytest.mark.usefixtures("windows")
async def test_a_run_alive_past_a_day_records_one_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    readings = iter([0.0, 2 * 86_400.0])  # the spawn, then the wait's end two days later
    monkeypatch.setattr(check_runs, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    runner = rig.runner(start=_spawner([]))
    started = await runner.start(check_id, None)
    [run] = runner._live.values()
    await asyncio.wait_for(run.done.wait(), HANG_GUARD_S)
    row = rig.store.get_check_run(started["run_id"])
    assert row is not None and (row["outcome"], row["duration_ms"]) == ("passed", 86_400_000)


@pytest.mark.usefixtures("windows")
async def test_an_unreadable_check_is_refused_without_a_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    row = rig.store.get_check(check_id)
    assert row is not None
    rig.store.delete_check(check_id)
    rig.store.insert_check(
        {**row, "body": row["body"][:-1] + bytes([row["body"][-1] ^ 1])}, limit=32
    )
    runner = rig.runner(start=_forbidden)
    assert await _refusal(runner.start(check_id, None)) == "check_unreadable"


@pytest.mark.usefixtures("windows")
async def test_unknown_ids_are_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    runner = rig.runner(start=_forbidden)
    assert await _refusal(runner.start(UNKNOWN, None)) == "unknown_check"
    assert await _refusal(runner.runs(UNKNOWN)) == "unknown_check"
    assert await _refusal(runner.delete(UNKNOWN)) == "unknown_check"
    assert await _refusal(runner.cancel(UNKNOWN)) == "unknown_run"
    assert await _refusal(runner.output(UNKNOWN, 0)) == "unknown_run"


@pytest.mark.usefixtures("windows")
async def test_a_stored_tail_that_will_not_open_is_unavailable(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    rig.store.insert_check_run(
        {**_finished(check_id, rig.workspace_id, 1), "output_bytes": 4, "tail": b"junk"}, keep=20
    )
    runner = rig.runner(start=_forbidden)
    run_id = _finished(check_id, rig.workspace_id, 1)["run_id"]
    assert await _refusal(runner.output(run_id, 0)) == "unavailable"


@pytest.mark.usefixtures("windows")
async def test_recover_interrupts_rows_left_running(tmp_path: Path) -> None:
    rig = _rig(tmp_path, repo=False)
    check_id = rig.check("exit /b 0")
    left: CheckRunRow = {
        **_finished(check_id, rig.workspace_id, 1),
        "outcome": "running",
        "exit_code": None,
        "duration_ms": None,
        "output_bytes": None,
        "ended_at": None,
    }
    rig.store.insert_check_run(left, keep=20)
    runner = rig.runner(start=_forbidden)
    assert await runner.recover() == 1
    row = rig.store.get_check_run(left["run_id"])
    assert row is not None and row["outcome"] == "interrupted" and row["ended_at"] is not None
    _row, part, done = await runner.output(left["run_id"], 0)
    assert (part.text, done) == ("", True)


async def test_off_windows_every_method_taking_an_id_refuses_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, repo=False)
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", False)
    for name in ("get_check", "get_check_run", "check_runs_of", "delete_check", "get_worktree"):
        monkeypatch.setattr(rig.store, name, _forbidden)
    runner = rig.runner(start=_forbidden)
    for call in (
        runner.start(UNKNOWN, None),
        runner.start(UNKNOWN, WORKTREE),
        runner.cancel(UNKNOWN),
        runner.output(UNKNOWN, 0),
        runner.runs(UNKNOWN),
        runner.delete(UNKNOWN),
    ):
        assert await _refusal(call) == "unsupported_platform"
    assert not runner.in_worktree(WORKTREE)
