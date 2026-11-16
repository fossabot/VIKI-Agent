"""scripts/linux_check.py: tree snapshots, image reuse, one container per suite (removed on every
path), and the CLI that wires suites, the base cache, comparison and the summary together.

subprocess.run is a fake wherever docker would run: nothing here starts a container. git runs for
real in a temp repo for the snapshot tests, and is faked (like every other linux_check function
below main's own CLI/cache/compare/summary logic) for the flow tests further down.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re
import signal
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from viki_agent import winproc

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import linux_check  # noqa: E402
import linux_results  # noqa: E402

TREE = b"the tree as a tar"
IMAGE = "viki-agent-linux:main-0123456789ab"
STAMP = "20261022-120000"
NAME = f"viki-agent-linux-check-{STAMP}-head-check"
STDOUT = linux_check.subprocess.STDOUT
# _remove_container's per-platform process-group isolation kwarg, reused by every test that
# asserts the exact options a "docker rm" call is made with.
_ISOLATION: dict[str, Any] = (
    {"creationflags": linux_check.subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32"
    else {"start_new_session": True}
)

# Flow fixtures: a head commit, a merge-base commit and a runner fingerprint, all fake.
HEAD_SHA = "1" * 40
BASE_SHA = "2" * 40
HEAD_SHORT = "1111111"
BASE_SHORT = "2222222"
FP = "fakefp000000"

Answer = int | tuple[int, bytes] | BaseException
Calls = list[tuple[list[str], dict[str, Any]]]


def fake_run(monkeypatch: pytest.MonkeyPatch, **answers: Answer) -> Calls:
    """Stand in for subprocess.run: record each call and answer it by its key (`git`, or docker's
    subcommand: `version`, `image`, `build`, `run`, `cp`, `rm`) with an exit code, an exit code
    and stdout, or an exception to raise; any other call exits 0. A call whose stdout is a file
    writes `<key> output` to it, so a test can see which calls wrote a log."""
    calls: Calls = []

    def run(cmd: list[str], **options: Any) -> linux_check.subprocess.CompletedProcess[bytes]:
        calls.append((cmd, options))
        key = cmd[0] if cmd[0] == "git" else cmd[1]
        answer = answers.get(key, 0)
        if isinstance(answer, BaseException):
            raise answer
        code, stdout = answer if isinstance(answer, tuple) else (answer, b"")
        if hasattr(options.get("stdout"), "write"):
            options["stdout"].write(f"{key} output\n".encode())
        return linux_check.subprocess.CompletedProcess(cmd, code, stdout, b"what went wrong")

    monkeypatch.setattr(linux_check.subprocess, "run", run)
    return calls


def test_paths_limit_and_suites() -> None:
    assert linux_check.RUNNER == REPO / "scripts" / "linux"
    assert linux_check.OUT == REPO / ".linux-check"
    assert linux_check.SUITE_LIMIT_S == 5400
    assert linux_check.SUITE_TARGET == {
        "check": "main",
        "e2e": "main",
        "desktop": "main",
        "package": "main",
        "compile": "compile",
        "oldgit": "oldgit",
    }


EXACT_ARGV = {
    "check": (
        f"docker run -i --init --shm-size=2g --pull=never --name {NAME}"
        " --mount type=volume,dst=/tmp"
        " --mount type=volume,dst=/home/ubuntu"
        " --mount type=volume,src=viki-agent-uv-cache,dst=/cache/uv"
        " --mount type=volume,src=viki-agent-npm-cache,dst=/cache/npm"
        " --mount type=volume,src=viki-agent-electron-cache,dst=/cache/electron"
        f" {IMAGE} sh /runner/run.sh check"
    ),
    "desktop": (
        f"docker run -i --init --shm-size=2g --pull=never --name {NAME}"
        " --mount type=volume,dst=/tmp"
        " --mount type=volume,dst=/home/ubuntu"
        " --mount type=volume,src=viki-agent-uv-cache,dst=/cache/uv"
        " --mount type=volume,src=viki-agent-npm-cache,dst=/cache/npm"
        " --mount type=volume,src=viki-agent-electron-cache,dst=/cache/electron"
        " --security-opt seccomp=unconfined"
        f" {IMAGE} sh /runner/run.sh desktop"
    ),
    "compile": (
        f"docker run -i --init --shm-size=2g --pull=never --name {NAME}"
        " --mount type=volume,dst=/tmp"
        " --mount type=volume,dst=/home/ubuntu"
        " --mount type=volume,src=viki-agent-compile-uv-cache,dst=/cache/uv"
        " --mount type=volume,src=viki-agent-compile-npm-cache,dst=/cache/npm"
        f" {IMAGE} sh /runner/run.sh compile"
    ),
    "oldgit": (
        f"docker run -i --init --shm-size=2g --pull=never --name {NAME}"
        " --mount type=volume,dst=/tmp"
        " --mount type=volume,dst=/home/ubuntu"
        " --mount type=volume,src=viki-agent-uv-cache,dst=/cache/uv"
        " --mount type=volume,src=viki-agent-npm-cache,dst=/cache/npm"
        " --mount type=volume,src=viki-agent-electron-cache,dst=/cache/electron"
        f" {IMAGE} sh /runner/run.sh oldgit"
    ),
}


@pytest.mark.parametrize("suite", list(EXACT_ARGV))
def test_run_argv_is_exact(suite: str) -> None:
    assert linux_check.run_argv(NAME, IMAGE, suite) == EXACT_ARGV[suite].split()


# None of these may reach a suite's container, in a space-separated, a long "--flag=value" or (for
# -v/-p) docker's joined-with-no-separator short spelling: splitting on "=" alone, as a mutant
# could still spell a bind mount or a host port that way, undetected.
FORBIDDEN = {
    *("-p", "--publish", "-P", "--publish-all", "--network", "--net"),  # ports, host network
    *("-v", "--volume", "--volumes-from", "-e", "--env", "--env-file"),  # host paths, environment
    *("--privileged", "--cap-add", "--device", "--pid", "--ipc", "--uts", "--userns"),
}
_JOINED_SHORT = ("-v", "-p")  # docker also accepts these joined: "-v/host:/ctr", not just "-v …"


@pytest.mark.parametrize("suite", sorted(linux_check.SUITE_TARGET))
def test_no_suite_gets_a_forbidden_flag(suite: str) -> None:
    argv = linux_check.run_argv(NAME, IMAGE, suite)
    flags = argv[: argv.index(IMAGE)]
    assert argv[len(flags) :] == [IMAGE, "sh", "/runner/run.sh", suite]
    assert not {flag.split("=")[0] for flag in flags} & FORBIDDEN
    assert not any(flag.startswith(prefix) for flag in flags for prefix in _JOINED_SHORT)

    def values(name: str) -> list[str]:
        """Every value `name` is given, in whichever spelling: "name value" (space-separated,
        the only one our own argv produces) or "name=value" (joined), so a mutant cannot hide a
        dangerous value (a bind mount's `--mount=type=bind,...`, say) just by switching
        spellings - the flag-name-only check above would otherwise miss it entirely, since a
        joined "--mount=…" token never equals the bare "--mount" it looks for."""
        found = []
        for at, flag in enumerate(flags):
            if flag == name:
                found.append(flags[at + 1])
            elif flag.startswith(f"{name}="):
                found.append(flag[len(name) + 1 :])
        return found

    mounts = values("--mount")
    assert mounts[0] == "type=volume,dst=/tmp"
    assert mounts[1] == "type=volume,dst=/home/ubuntu"
    cache = r"type=volume,src=viki-agent-(compile-)?[a-z]+-cache,dst=/cache/[a-z]+"
    assert all(re.fullmatch(cache, mount) for mount in mounts[2:])
    assert values("--security-opt") == (["seccomp=unconfined"] if suite == "desktop" else [])


def _git(cwd: Path, *args: str) -> str:
    """Repo setup only: linux_check.git is the path under test."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _read(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    data = tar.extractfile(member)
    assert data is not None
    return data.read()


COMMITTED = {
    ".gitignore": b"frontend/dist/\n",
    "README.md": b"committed\n",
    "frontend/src/main.ts": b"export {};\n",
    "gone.txt": b"deleted from the working tree later\n",
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp repo with one commit of COMMITTED, and linux_check.REPO pointing at it. The
    developer's own git config (global excludes, hooks, signing) and GIT_* variables stay out."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    config = tmp_path / "gitconfig"
    config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    for name, data in COMMITTED.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    user = ["-c", "user.email=check@example.com", "-c", "user.name=check"]
    _git(root, *user, "commit", "-q", "-m", "tree")
    monkeypatch.setattr(linux_check, "REPO", root)
    return root


def test_the_tree_snapshot_is_what_git_add_would_commit(repo: Path) -> None:
    (repo / "README.md").write_bytes(b"edited, not staged\n")
    (repo / "notes.txt").write_bytes(b"untracked, not ignored\n")
    (repo / "notes.txt").chmod(0o755)
    (repo / "frontend" / "dist").mkdir()
    (repo / "frontend" / "dist" / "index.html").write_bytes(b"<!doctype html>\n")  # ignored
    (repo / "gone.txt").unlink()  # still tracked, but gone from the working tree
    (repo / "empty").mkdir()
    with tarfile.open(fileobj=io.BytesIO(linux_check.snapshot_tree())) as tar:
        members = tar.getmembers()
        files = {member.name: _read(tar, member) for member in members}
    assert [member.name for member in members] == [
        ".gitignore",
        "README.md",
        "frontend/src/main.ts",
        "notes.txt",
    ]
    assert "frontend/dist/index.html" not in files
    assert files["README.md"] == b"edited, not staged\n"
    assert files["notes.txt"] == b"untracked, not ignored\n"
    for member in members:
        assert member.isreg()
        assert (member.mode, member.uid, member.gid) == (0o644, 0, 0)
        assert (member.uname, member.gname) == ("", "")
        assert member.mtime == int((repo / member.name).stat().st_mtime)


def test_the_commit_snapshot_is_the_committed_tree(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "README.md").write_bytes(b"edited, not committed\n")
    (repo / "notes.txt").write_bytes(b"untracked\n")
    with tarfile.open(fileobj=io.BytesIO(linux_check.snapshot_commit(sha))) as tar:
        files = {member.name: _read(tar, member) for member in tar.getmembers() if member.isfile()}
    assert files == COMMITTED


def test_a_symlink_in_the_tree_is_left_out_of_the_snapshot(repo: Path) -> None:
    """A symlink to a file outside the repo must never bring that file's content into the
    snapshot. An untracked-not-ignored symlink is enough to be listed; no commit needed."""
    outside = repo.parent / "outside.txt"
    outside.write_bytes(b"must never reach the snapshot\n")
    link = repo / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating a symlink needs a privilege this user/platform does not have")
    with tarfile.open(fileobj=io.BytesIO(linux_check.snapshot_tree())) as tar:
        members = tar.getmembers()
        contents = b"".join(_read(tar, member) for member in members)
    assert "link.txt" not in [member.name for member in members]
    assert b"must never reach the snapshot" not in contents


def test_git_runs_in_the_repo_with_check_py_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, git=(0, b"listed\0"))
    assert linux_check.git("ls-files", "-z") == b"listed\0"
    git = ["git", "-C", str(linux_check.REPO), "--no-optional-locks", "-c", "core.quotepath=off"]
    assert calls == [([*git, "ls-files", "-z"], {"check": False, "capture_output": True})]


def test_a_failing_git_is_a_runner_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(
        linux_check.RunnerError, match=r"^git rev-parse --verify nope\^\{commit\} failed: fatal: "
    ):
        linux_check.git("rev-parse", "--verify", "nope^{commit}")
    fake_run(monkeypatch, git=FileNotFoundError(2, "No such file"))
    with pytest.raises(linux_check.RunnerError, match="^git could not start"):
        linux_check.git("status")


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A build context for linux_check.RUNNER; returns its fingerprint, worked out by hand."""
    folder = tmp_path / "linux"
    (folder / "patches").mkdir(parents=True)
    (folder / "run.sh").write_bytes(b"echo run\n")
    (folder / "Dockerfile").write_bytes(b"FROM scratch\n")
    (folder / "patches" / "a.diff").write_bytes(b"+fix\n")
    monkeypatch.setattr(linux_check, "RUNNER", folder)
    hashed = b"Dockerfile\0FROM scratch\n\0patches/a.diff\0+fix\n\0run.sh\0echo run\n\0"
    return hashlib.sha256(hashed).hexdigest()[:12]


def test_the_fingerprint_covers_every_runner_file_by_name(runner: str) -> None:
    assert linux_check.fingerprint() == runner
    (linux_check.RUNNER / "run.sh").write_bytes(b"echo changed\n")
    assert linux_check.fingerprint() != runner


def test_docker_ready_returns_the_server_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, version=(0, b"24.0.6\n"))
    assert linux_check.docker_ready() == "24.0.6"
    version = ["docker", "version", "--format", "{{.Server.Version}}"]
    assert calls == [(version, {"check": False, "capture_output": True})]


@pytest.mark.parametrize(
    "answer",
    [(1, b""), (0, b"\n"), FileNotFoundError(2, "No such file")],
    ids=["server down", "no version", "no docker"],
)
def test_docker_not_ready_is_a_runner_error(
    answer: Answer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run(monkeypatch, version=answer)
    with pytest.raises(linux_check.RunnerError, match="^docker"):
        linux_check.docker_ready()


def _no_push_or_pull(calls: Calls) -> bool:
    return not any(word in ("push", "pull", "--pull") for cmd, _ in calls for word in cmd)


def test_an_image_with_the_tag_is_reused(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_run(monkeypatch, image=0)
    run_dir = tmp_path / STAMP
    tag = f"viki-agent-linux:main-{runner}"
    assert linux_check.ensure_image("main", run_dir) == linux_check.Image(tag, built=False)
    assert calls == [
        (["docker", "image", "inspect", tag], {"check": False, "capture_output": True})
    ]
    assert not run_dir.exists()  # nothing built, so no image log


def test_a_missing_image_is_built_locally_into_its_log(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_run(monkeypatch, image=1)
    run_dir = tmp_path / STAMP
    tag = f"viki-agent-linux:compile-{runner}"
    assert linux_check.ensure_image("compile", run_dir) == linux_check.Image(tag, built=True)
    (inspect, _), (build, options) = calls
    assert inspect == ["docker", "image", "inspect", tag]
    assert build == [
        *("docker", "build", "--progress=plain", "--target", "compile", "-t", tag),
        *("-f", "Dockerfile", "."),
    ]
    log = run_dir / "image-compile.log"
    assert options["stdout"].name == str(log)
    assert {key: value for key, value in options.items() if key != "stdout"} == {
        "check": False,
        "cwd": linux_check.RUNNER,
        "env": {**os.environ, "DOCKER_BUILDKIT": "1"},
        "stderr": STDOUT,
    }
    assert log.read_bytes() == b"build output\n"
    assert _no_push_or_pull(calls)


def test_a_failed_build_is_a_runner_error_naming_its_log(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_run(monkeypatch, image=1, build=1)
    run_dir = tmp_path / STAMP
    with pytest.raises(linux_check.RunnerError, match=re.escape(str(run_dir / "image-main.log"))):
        linux_check.ensure_image("main", run_dir)
    assert _no_push_or_pull(calls)


def test_a_suite_runs_in_one_container_and_its_out_is_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_run(monkeypatch)
    run_dir = tmp_path / STAMP
    assert linux_check.run_suite("head", "check", TREE, IMAGE, run_dir) == run_dir / "head-check"
    (run, run_options), (copy, copy_options), (remove, remove_options) = calls
    log = run_dir / "head-check.log"
    assert run == linux_check.run_argv(NAME, IMAGE, "check")
    assert run_options["stdout"].name == str(log)
    assert {key: value for key, value in run_options.items() if key != "stdout"} == {
        "check": False,
        "input": TREE,
        "stderr": STDOUT,
        "timeout": linux_check.SUITE_LIMIT_S,
    }
    # A relative destination in run_dir: docker cp would read a drive's colon as container:path.
    assert copy == ["docker", "cp", f"{NAME}:/out", "head-check"]
    assert copy_options["cwd"] == run_dir
    assert remove == ["docker", "rm", "-f", "-v", NAME]
    assert remove_options == {"capture_output": True, "check": False, **_ISOLATION}
    assert log.read_bytes() == b"run output\ncp output\n"


TIMEOUT = linux_check.subprocess.TimeoutExpired(["docker", "run"], linux_check.SUITE_LIMIT_S)
# answers for fake_run, what run_suite returns (a folder name or None) or raises, and the docker
# subcommands it ran, in order.
ENDINGS: dict[str, tuple[dict[str, Answer], str | type[BaseException] | None, list[str]]] = {
    "success": ({}, "head-check", ["run", "cp", "rm"]),
    "run.sh failure": ({"run": 64}, linux_check.RunnerError, ["run", "rm"]),
    "docker failure": ({"run": 125}, linux_check.RunnerError, ["run", "rm"]),
    "copy failure": ({"cp": 1}, linux_check.RunnerError, ["run", "cp", "rm"]),
    "no docker": (
        {"run": FileNotFoundError(2, "No such file")},
        linux_check.RunnerError,
        ["run", "rm"],
    ),
    "timeout": ({"run": TIMEOUT}, None, ["run", "rm"]),
    "interrupt": ({"run": KeyboardInterrupt()}, KeyboardInterrupt, ["run", "rm"]),
}


@pytest.mark.parametrize(
    ("answers", "expected", "verbs"), list(ENDINGS.values()), ids=list(ENDINGS)
)
def test_the_container_is_removed_on_every_path(
    answers: dict[str, Answer],
    expected: str | type[BaseException] | None,
    verbs: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_run(monkeypatch, **answers)
    run_dir = tmp_path / STAMP
    if isinstance(expected, type):
        with pytest.raises(expected):
            linux_check.run_suite("head", "check", TREE, IMAGE, run_dir)
    else:
        outcome = linux_check.run_suite("head", "check", TREE, IMAGE, run_dir)
        assert outcome == (None if expected is None else run_dir / expected)
    assert [cmd[1] for cmd, _ in calls] == verbs
    assert calls[-1][0] == ["docker", "rm", "-f", "-v", NAME]


def test_a_broken_runner_is_a_runner_error_naming_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_run(monkeypatch, run=64)
    run_dir = tmp_path / STAMP
    with pytest.raises(linux_check.RunnerError, match=re.escape(str(run_dir / "base-desktop.log"))):
        linux_check.run_suite("base", "desktop", TREE, IMAGE, run_dir)
    name = f"viki-agent-linux-check-{STAMP}-base-desktop"
    assert calls[0][0] == linux_check.run_argv(name, IMAGE, "desktop")
    assert calls[-1][0] == ["docker", "rm", "-f", "-v", name]


def test_a_failed_removal_does_not_hide_the_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_remove_container retries once and then gives up quietly (no raise) rather than
    replacing the KeyboardInterrupt that is already propagating out of run_suite."""
    calls = fake_run(monkeypatch, run=KeyboardInterrupt(), rm=FileNotFoundError(2, "No such file"))
    with pytest.raises(KeyboardInterrupt):
        linux_check.run_suite("head", "check", TREE, IMAGE, tmp_path / STAMP)
    assert [cmd[1] for cmd, _ in calls] == ["run", "rm", "rm"]  # tried, then retried once
    assert NAME in capsys.readouterr().out


# --- _remove_container is immune to a second Ctrl+C, retries once, reports if it still fails


def test_remove_container_runs_in_its_own_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """So the console's own Ctrl+C/Ctrl+Break is not also delivered to this child directly."""
    calls = fake_run(monkeypatch)
    linux_check._remove_container("some-name")
    assert calls == [
        (
            ["docker", "rm", "-f", "-v", "some-name"],
            {"capture_output": True, "check": False, **_ISOLATION},
        )
    ]


def test_remove_container_ignores_sigint_while_running_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def run(cmd: list[str], **options: Any) -> linux_check.subprocess.CompletedProcess[bytes]:
        seen.append(signal.getsignal(signal.SIGINT))
        return linux_check.subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(linux_check.subprocess, "run", run)
    before = signal.getsignal(signal.SIGINT)
    linux_check._remove_container("some-name")
    assert seen == [signal.SIG_IGN]  # ignored for the whole duration of the call
    assert signal.getsignal(signal.SIGINT) == before  # and restored afterwards


def test_remove_container_retries_once_then_reports_the_container_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = fake_run(monkeypatch, rm=1)  # every attempt exits 1
    linux_check._remove_container("stuck-name")
    assert len(calls) == 2  # tried, then retried once, then gave up
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # one line
    assert "stuck-name" in out


def test_remove_container_succeeds_on_the_retry_and_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts = iter([1, 0])  # fails once, then succeeds

    def run(cmd: list[str], **options: Any) -> linux_check.subprocess.CompletedProcess[bytes]:
        return linux_check.subprocess.CompletedProcess(cmd, next(attempts), b"", b"")

    monkeypatch.setattr(linux_check.subprocess, "run", run)
    linux_check._remove_container("some-name")
    assert capsys.readouterr().out == ""


# --- the CLI, --head/--base/--fresh, the base cache, compare and the summary ---------------


def test_default_suites_are_check_e2e_desktop_and_oldgit() -> None:
    args = linux_check._parse_args([])
    assert args.suites == ["check", "e2e", "desktop", "oldgit"]
    assert (args.head, args.base, args.fresh) == (None, "origin/main", False)


def test_suite_is_repeatable_and_deduped_in_first_seen_order() -> None:
    args = linux_check._parse_args(["--suite", "package", "--suite", "check", "--suite", "package"])
    assert args.suites == ["package", "check"]


def test_head_base_and_fresh_flags_are_read() -> None:
    args = linux_check._parse_args(["--head", "HEAD~1", "--base", "upstream/main", "--fresh"])
    assert (args.head, args.base, args.fresh) == ("HEAD~1", "upstream/main", True)


def test_an_unknown_suite_is_rejected() -> None:
    with pytest.raises(SystemExit):
        linux_check._parse_args(["--suite", "nope"])


def test_run_ids_differ_by_pid_in_the_same_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux_check, "strftime", lambda fmt: "20261022-120000")  # noqa: ARG005
    monkeypatch.setattr(linux_check.os, "getpid", lambda: 111)
    first = linux_check._run_id()
    monkeypatch.setattr(linux_check.os, "getpid", lambda: 222)
    second = linux_check._run_id()
    assert (first, second) == ("20261022-120000-111", "20261022-120000-222")


def test_another_run_is_active_when_docker_ps_lists_one(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, ps=(0, b"abc123def456\n"))
    assert linux_check._another_run_is_active() is True
    assert calls == [
        (
            ["docker", "ps", "-q", "--filter", "name=viki-agent-linux-check-"],
            {"check": False, "capture_output": True},
        )
    ]


@pytest.mark.parametrize("answer", [(0, b""), 1], ids=["empty", "docker ps failed"])
def test_another_run_is_not_active(answer: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, ps=answer)
    assert linux_check._another_run_is_active() is False


def test_a_timeout_becomes_a_marker_failure_in_both_first_and_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linux_check, "run_suite", lambda *a, **k: None)  # noqa: ARG005
    failures = linux_check._side_result("head", "check", TREE, IMAGE, tmp_path)
    marker = "check: timed out after 90 min"
    assert failures == linux_results.Failures(frozenset({marker}), frozenset({marker}))


def test_an_unreadable_base_cache_is_a_results_error(tmp_path: Path) -> None:
    cache_path = tmp_path / "bad.json"
    cache_path.write_text("not json", encoding="utf-8")
    with pytest.raises(linux_results.ResultsError, match="unreadable base cache"):
        linux_check._load_base(cache_path)


def test_an_undecodable_base_cache_is_a_results_error(tmp_path: Path) -> None:
    """A byte no UTF-8 decoder accepts must fail closed like any other unreadable base cache,
    not crash with an uncaught UnicodeDecodeError."""
    cache_path = tmp_path / "bad.json"
    cache_path.write_bytes(b"\xff\xfe not valid utf-8")
    with pytest.raises(linux_results.ResultsError, match="unreadable base cache"):
        linux_check._load_base(cache_path)


def _patch_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failures: dict[tuple[str, str], linux_results.Failures],
    *,
    busy: bool = False,
    status: bytes = b"",
) -> dict[str, list[Any]]:
    """Patch every linux_check function `main`/`_run_check` calls beneath the CLI parsing, base
    caching, comparison and summary logic under test. `failures[(side, suite)]` stands in for a
    container's results. `run_suite` is faked to return the same `<side>-<suite>` folder name the
    real one does, so the faked `suite_failures` can read the (side, suite) pair back out of it,
    and a pair missing from `failures` raises KeyError instead of silently passing. perf_counter
    is a plain tick counter, so every started/elapsed pair reports exactly 1s."""
    monkeypatch.setattr(linux_check, "REPO", tmp_path)
    monkeypatch.setattr(linux_check, "OUT", tmp_path / ".linux-check")
    monkeypatch.setattr(linux_check, "docker_ready", lambda: "24.0.6")
    monkeypatch.setattr(linux_check, "fingerprint", lambda: FP)
    monkeypatch.setattr(linux_check, "_another_run_is_active", lambda: busy)
    monkeypatch.setattr(linux_check, "_run_id", lambda: STAMP)
    monkeypatch.setattr(linux_check, "snapshot_tree", lambda: b"tree")

    ticks = itertools.count()
    monkeypatch.setattr(linux_check, "perf_counter", lambda: float(next(ticks)))

    recorded: dict[str, list[Any]] = {"run_suite": [], "snapshot_commit": [], "ensure_image": []}

    def fake_snapshot_commit(sha: str) -> bytes:
        recorded["snapshot_commit"].append(sha)
        return f"commit-{sha}".encode()

    def fake_ensure_image(target: str, run_dir: Path) -> linux_check.Image:
        recorded["ensure_image"].append(target)
        return linux_check.Image(f"viki-agent-linux:{target}-{FP}", False)

    def fake_git(*args: str) -> bytes:
        if args[:2] == ("rev-parse", "--verify"):
            return (HEAD_SHA + "\n").encode()
        if args[0] == "merge-base":
            return (BASE_SHA + "\n").encode()
        if args[:2] == ("rev-parse", "--short"):
            short = HEAD_SHORT if args[2] == HEAD_SHA else BASE_SHORT
            return (short + "\n").encode()
        if args == ("status", "--porcelain"):
            return status
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run_suite(
        side: str, suite: str, tree: bytes, image: str, run_dir: Path
    ) -> Path | None:
        recorded["run_suite"].append((side, suite, image))
        return run_dir / f"{side}-{suite}"

    def fake_suite_failures(suite: str, out: Path) -> linux_results.Failures:
        side = out.name.split("-", 1)[0]
        return failures[(side, suite)]

    monkeypatch.setattr(linux_check, "snapshot_commit", fake_snapshot_commit)
    monkeypatch.setattr(linux_check, "ensure_image", fake_ensure_image)
    monkeypatch.setattr(linux_check, "git", fake_git)
    monkeypatch.setattr(linux_check, "run_suite", fake_run_suite)
    monkeypatch.setattr(linux_check.linux_results, "suite_failures", fake_suite_failures)
    return recorded


PASSED = linux_results.Failures(frozenset(), frozenset())


def test_a_green_head_never_touches_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = _patch_flow(monkeypatch, tmp_path, {("head", "check"): PASSED})
    exit_code = linux_check.main(["--suite", "check"])
    assert exit_code == 0
    assert recorded["run_suite"] == [("head", "check", f"viki-agent-linux:main-{FP}")]
    assert recorded["snapshot_commit"] == []  # the base is never snapshotted
    assert not (tmp_path / ".linux-check" / "base").exists()  # nor cached
    out = capsys.readouterr().out
    assert "head check: passed (1s)" in out
    assert "base check" not in out
    assert "new failures: none" in out


def test_a_known_failure_does_not_count_as_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    both = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    _patch_flow(monkeypatch, tmp_path, {("head", "check"): both, ("base", "check"): both})
    exit_code = linux_check.main(["--suite", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "new failures: none" in out
    assert "also failing on main, not counted: check nodeX" in out
    assert "flaky" not in out


def test_a_new_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = linux_results.Failures(frozenset({"check nodeY"}), frozenset({"check nodeY"}))
    _patch_flow(monkeypatch, tmp_path, {("head", "check"): head, ("base", "check"): PASSED})
    exit_code = linux_check.main(["--suite", "check"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "new failures: check nodeY" in out
    assert "also failing" not in out
    assert "flaky" not in out


def test_a_flaky_failure_is_not_counted_and_skips_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flaky_head = linux_results.Failures(frozenset({"check nodeZ"}), frozenset())
    recorded = _patch_flow(monkeypatch, tmp_path, {("head", "check"): flaky_head})
    exit_code = linux_check.main(["--suite", "check"])
    assert exit_code == 0
    assert recorded["run_suite"] == [("head", "check", f"viki-agent-linux:main-{FP}")]
    assert recorded["snapshot_commit"] == []
    out = capsys.readouterr().out
    assert "new failures: none" in out
    assert "flaky, passed on one of its two runs, not counted: check nodeZ" in out
    assert "also failing" not in out


def test_a_rerun_only_head_failure_prints_passed_skips_base_and_is_flaky(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A head id that failed only on its rerun (absent from `first`, so `confirmed` is
    empty) must not make the head line read as failed, and must not start a base run by itself;
    `compare` can only call it flaky or known, so with no base it is flaky."""
    rerun_only = linux_results.Failures(frozenset(), frozenset({"check nodeY"}))
    recorded = _patch_flow(monkeypatch, tmp_path, {("head", "check"): rerun_only})

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 0
    assert recorded["run_suite"] == [("head", "check", f"viki-agent-linux:main-{FP}")]
    assert recorded["snapshot_commit"] == []  # no base: _needs_base saw an empty `confirmed`
    out = capsys.readouterr().out
    assert "head check: passed (1s)" in out
    assert "base check" not in out
    assert "new failures: none" in out
    assert "flaky, passed on one of its two runs, not counted: check nodeY" in out


def test_a_timeout_on_both_sides_still_counts_as_new_and_skips_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A suite that times out on both the head and the base used to share one marker id, so
    `compare` counted it known and the run exited 0. It must also never bother running the
    base at all: a base run cannot change a timeout's verdict."""
    recorded = _patch_flow(monkeypatch, tmp_path, {})
    monkeypatch.setattr(linux_check, "run_suite", lambda *a, **k: None)  # noqa: ARG005

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 1
    assert recorded["snapshot_commit"] == []  # the base is never snapshotted
    out = capsys.readouterr().out
    assert "new failures: check: timed out after 90 min" in out
    assert "also failing on main, not counted" not in out
    assert "base check" not in out  # skipped entirely, not merely cached


def test_a_timeout_mixed_with_a_real_failure_still_runs_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skipping the base run only applies when *every* still-failing id is a timeout; a real
    failure alongside one must still get the base run it needs to tell known from new."""
    timeout_marker = "check: timed out after 90 min"
    mixed_head = linux_results.Failures(
        frozenset({timeout_marker, "check nodeX"}), frozenset({timeout_marker, "check nodeX"})
    )
    base = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    recorded = _patch_flow(
        monkeypatch, tmp_path, {("head", "check"): mixed_head, ("base", "check"): base}
    )

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 1
    assert recorded["snapshot_commit"] == [BASE_SHA]  # the base did run, for nodeX's sake
    out = capsys.readouterr().out
    assert "new failures: check: timed out after 90 min" in out
    assert "also failing on main, not counted: check nodeX" in out


def _setup_failed_report(tmp_path: Path) -> Path:
    """The whole of the `/out` that `scripts/linux/run.sh` leaves when a suite's setup fails."""
    report = tmp_path / "report"
    report.mkdir()
    (report / "steps.jsonl").write_text(
        '{"step": "setup", "exit": 1, "seconds": 3}\n', encoding="utf-8"
    )
    return report


@pytest.mark.parametrize("suite", sorted(linux_check.SUITE_TARGET))
def test_a_failed_setup_on_both_sides_counts_as_new_and_skips_the_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A setup that fails on the head and on the base used to share one marker id, so `compare`
    counted it known and the run exited 0 with none of the suite's tests run, and the base's
    failure was cached. Both sides here read the same failed-setup report through the real
    `suite_failures`, so a base run, if one started, would fail its setup too."""
    read_results = linux_results.suite_failures
    recorded = _patch_flow(monkeypatch, tmp_path, {})
    report = _setup_failed_report(tmp_path)
    monkeypatch.setattr(linux_check, "run_suite", lambda *a, **k: report)  # noqa: ARG005
    monkeypatch.setattr(linux_results, "suite_failures", read_results)

    exit_code = linux_check.main(["--suite", suite])

    assert exit_code == 1
    assert recorded["snapshot_commit"] == []  # the base is never snapshotted
    assert not (tmp_path / ".linux-check" / "base").exists()  # nor cached
    out = capsys.readouterr().out
    assert f"new failures: {suite} setup" in out
    assert "also failing on main, not counted" not in out
    assert f"base {suite}" not in out


def test_a_failed_setup_is_new_even_against_a_cached_base_that_shares_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever a cache file of the current version says, a head that never ran its tests is
    not excused by it."""
    setup_failed = linux_results.Failures(
        frozenset({"package setup"}), frozenset({"package setup"})
    )
    _patch_flow(monkeypatch, tmp_path, {("head", "package"): setup_failed})
    cache_path = tmp_path / ".linux-check" / "base" / f"{BASE_SHA}-package-{FP}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(linux_results.to_json(setup_failed)), encoding="utf-8")

    exit_code = linux_check.main(["--suite", "package"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "new failures: package setup" in out
    assert "(cached)" not in out
    assert "also failing on main, not counted" not in out


def test_a_base_whose_setup_failed_exits_two_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The head ran and fails a real step; the base could not be set up, so it cannot say
    whether main shares that failure. That is neither "known" nor "new": the check could not
    run, and the base's non-result must not be kept for the next run to trust."""
    read_results = linux_results.suite_failures
    head = linux_results.Failures(frozenset({"package dist"}), frozenset({"package dist"}))
    recorded = _patch_flow(monkeypatch, tmp_path, {})
    report = _setup_failed_report(tmp_path)
    monkeypatch.setattr(
        linux_results,
        "suite_failures",
        lambda suite, out: head if out.name == "head-package" else read_results(suite, report),
    )

    exit_code = linux_check.main(["--suite", "package"])

    assert exit_code == 2
    assert recorded["snapshot_commit"] == [BASE_SHA]  # the base did run
    assert not (tmp_path / ".linux-check" / "base").exists()  # and left no cache file
    out = capsys.readouterr().out.splitlines()
    log = tmp_path / ".linux-check" / "runs" / STAMP / "base-package.log"
    assert out[-1] == (
        "linux check could not run: base package gave no result (package setup), "
        f"so what main fails is unknown: see {log}"
    )
    assert not any(line.startswith(("new failures", "also failing")) for line in out)


def test_a_base_that_timed_out_exits_two_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base timeout is no more a result than a failed base setup: cached, it would make
    every later head failure on this base read as new without the base ever running again."""
    head = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    _patch_flow(monkeypatch, tmp_path, {("head", "check"): head})

    def run_suite(side: str, suite: str, tree: bytes, image: str, run_dir: Path) -> Path | None:
        return None if side == "base" else run_dir / f"{side}-{suite}"

    monkeypatch.setattr(linux_check, "run_suite", run_suite)

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 2
    assert not (tmp_path / ".linux-check" / "base").exists()
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith(
        "linux check could not run: base check gave no result (check: timed out after 90 min), "
    )


def _pytest_killed(report: Path) -> None:
    """The pytest stage ended without a lastfailed cache, as when it is killed at its limit."""
    (report / "python.log").write_text(
        "FAILED: pytest -q -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )


def _no_result_line(report: Path) -> None:
    (report / "python.log").write_text("Killed\n", encoding="utf-8")


def _unit_listed_nothing(report: Path) -> None:
    (report / "unit.json").write_text('{"testResults": []}', encoding="utf-8")


# How a step of the check suite can fail without naming a test: the step, what it leaves in
# `/out`, and the id `suite_failures` gives it.
NO_TEST_REPORTS = [
    pytest.param("python", _pytest_killed, "check python: failed with no test listed"),
    pytest.param("python", _no_result_line, "check python: no result line"),
    pytest.param("unit", _unit_listed_nothing, "check unit: failed with no test listed"),
]


def _no_test_report(tmp_path: Path, step: str, leave: Any) -> Path:
    report = tmp_path / "report"
    report.mkdir()
    steps = ("setup", "python", "lint", "typecheck", "unit", "build")
    (report / "steps.jsonl").write_text(
        "".join(json.dumps({"step": name, "exit": int(name == step)}) + "\n" for name in steps),
        encoding="utf-8",
    )
    leave(report)
    return report


@pytest.mark.parametrize(("step", "leave", "marker"), NO_TEST_REPORTS)
def test_a_step_that_named_no_test_on_both_sides_counts_as_new_and_skips_the_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    step: str,
    leave: Any,
    marker: str,
) -> None:
    """A pytest stage killed on main and a collection that crashed on the head leave the same
    id. Both sides here read the same report through the real `suite_failures`, so a base run,
    if one started, would share the marker; it must not excuse the head or be cached."""
    read_results = linux_results.suite_failures
    recorded = _patch_flow(monkeypatch, tmp_path, {})
    report = _no_test_report(tmp_path, step, leave)
    monkeypatch.setattr(linux_check, "run_suite", lambda *a, **k: report)  # noqa: ARG005
    monkeypatch.setattr(linux_results, "suite_failures", read_results)

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 1
    assert recorded["snapshot_commit"] == []  # the base is never snapshotted
    assert not (tmp_path / ".linux-check" / "base").exists()  # nor cached
    out = capsys.readouterr().out
    assert f"new failures: {marker}" in out
    assert "also failing on main, not counted" not in out
    assert "base check" not in out


@pytest.mark.parametrize(("step", "leave", "marker"), NO_TEST_REPORTS)
def test_a_base_step_that_named_no_test_exits_two_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    step: str,
    leave: Any,
    marker: str,
) -> None:
    """The head fails a real test; the base's step died without naming one, so it cannot say
    whether main shares that failure. Cached, it would also stand in for main on every later
    head that dies the same way."""
    read_results = linux_results.suite_failures
    head = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    recorded = _patch_flow(monkeypatch, tmp_path, {})
    report = _no_test_report(tmp_path, step, leave)
    monkeypatch.setattr(
        linux_results,
        "suite_failures",
        lambda suite, out: head if out.name == "head-check" else read_results(suite, report),
    )

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 2
    assert recorded["snapshot_commit"] == [BASE_SHA]  # the base did run
    assert not (tmp_path / ".linux-check" / "base").exists()  # and left no cache file
    out = capsys.readouterr().out.splitlines()
    log = tmp_path / ".linux-check" / "runs" / STAMP / "base-check.log"
    assert out[-1] == (
        f"linux check could not run: base check gave no result ({marker}), "
        f"so what main fails is unknown: see {log}"
    )
    assert not any(line.startswith(("new failures", "also failing")) for line in out)


def test_a_no_test_marker_beside_a_real_failure_is_new_against_a_cached_base_that_shares_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever a cache file of the current version holds, the marker never reads as main's."""
    marker = "check python: failed with no test listed"
    both = linux_results.Failures(
        frozenset({marker, "check nodeX"}), frozenset({marker, "check nodeX"})
    )
    recorded = _patch_flow(monkeypatch, tmp_path, {("head", "check"): both})
    cache_path = tmp_path / ".linux-check" / "base" / f"{BASE_SHA}-check-{FP}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(linux_results.to_json(both)), encoding="utf-8")

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 1
    assert recorded["snapshot_commit"] == []  # the cached base was used, for nodeX's sake
    out = capsys.readouterr().out
    assert f"new failures: {marker}" in out
    assert "also failing on main, not counted: check nodeX" in out


def test_a_cached_base_result_skips_running_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cached = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    head = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    recorded = _patch_flow(monkeypatch, tmp_path, {("head", "check"): head})
    cache_path = tmp_path / ".linux-check" / "base" / f"{BASE_SHA}-check-{FP}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(linux_results.to_json(cached)), encoding="utf-8")

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 0  # "check nodeX" is known: the cached base already had it
    assert recorded["run_suite"] == [("head", "check", f"viki-agent-linux:main-{FP}")]
    assert recorded["snapshot_commit"] == []  # a cache hit never snapshots the base either
    assert "base check: 1 failed (cached)" in capsys.readouterr().out


def test_fresh_ignores_a_cached_base_and_overwrites_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = linux_results.Failures(frozenset({"check stale"}), frozenset({"check stale"}))
    fresh = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    head = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    recorded = _patch_flow(
        monkeypatch, tmp_path, {("head", "check"): head, ("base", "check"): fresh}
    )
    cache_path = tmp_path / ".linux-check" / "base" / f"{BASE_SHA}-check-{FP}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(linux_results.to_json(stale)), encoding="utf-8")

    exit_code = linux_check.main(["--suite", "check", "--fresh"])

    assert exit_code == 0  # nodeX is known to the freshly run base, not the stale one
    assert recorded["run_suite"] == [
        ("head", "check", f"viki-agent-linux:main-{FP}"),
        ("base", "check", f"viki-agent-linux:main-{FP}"),
    ]
    saved = linux_results.from_json(json.loads(cache_path.read_text(encoding="utf-8")))
    assert saved == fresh


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_an_older_cache_is_a_miss_and_gets_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    version: int,
) -> None:
    """An old cache is a miss without --fresh, including results predating step validation and
    those that could still hold a base whose setup failed or whose step named no test."""
    fresh = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    head = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    recorded = _patch_flow(
        monkeypatch, tmp_path, {("head", "check"): head, ("base", "check"): fresh}
    )
    cache_path = tmp_path / ".linux-check" / "base" / f"{BASE_SHA}-check-{FP}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps({"version": version, "first": ["check stale"], "still": ["check stale"]}),
        encoding="utf-8",
    )

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 0  # nodeX is known to the freshly run base
    assert recorded["run_suite"] == [
        ("head", "check", f"viki-agent-linux:main-{FP}"),
        ("base", "check", f"viki-agent-linux:main-{FP}"),
    ]
    out = capsys.readouterr().out
    assert "(cached)" not in out
    saved = linux_results.from_json(json.loads(cache_path.read_text(encoding="utf-8")))
    assert saved == fresh


def test_image_build_is_timed_and_happens_once_per_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = _patch_flow(
        monkeypatch, tmp_path, {("head", "check"): PASSED, ("head", "e2e"): PASSED}
    )

    def fake_ensure_image(target: str, run_dir: Path) -> linux_check.Image:
        recorded["ensure_image"].append(target)
        return linux_check.Image(f"viki-agent-linux:{target}-{FP}", True)

    monkeypatch.setattr(linux_check, "ensure_image", fake_ensure_image)

    exit_code = linux_check.main(["--suite", "check", "--suite", "e2e"])

    assert exit_code == 0
    assert recorded["ensure_image"] == ["main"]  # built once, reused for e2e
    out = capsys.readouterr().out
    assert out.count("image main") == 1
    assert "image main: built in 1s" in out


def test_a_warning_is_printed_once_when_another_run_looks_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_flow(
        monkeypatch,
        tmp_path,
        {("head", "check"): PASSED, ("head", "e2e"): PASSED},
        busy=True,
    )
    linux_check.main(["--suite", "check", "--suite", "e2e"])
    warning = "warning: another linux check looks to be running; containers may compete for load"
    assert capsys.readouterr().out.count(warning) == 1


def test_head_line_says_clean_when_the_working_tree_matches_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_flow(monkeypatch, tmp_path, {("head", "check"): PASSED}, status=b"")
    linux_check.main(["--suite", "check"])
    out = capsys.readouterr().out
    assert (
        f"linux check: head working tree on {HEAD_SHORT} (clean), "
        f"base {BASE_SHORT} (merge base with origin/main)" in out
    )


def test_head_line_counts_paths_differing_from_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_flow(
        monkeypatch,
        tmp_path,
        {("head", "check"): PASSED},
        status=b" M scripts/linux_check.py\n?? notes.txt\n",
    )
    linux_check.main(["--suite", "check"])
    out = capsys.readouterr().out
    assert f"head working tree on {HEAD_SHORT} (2 paths differ from HEAD)" in out


def test_an_explicit_head_commit_skips_the_clean_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded_git: list[tuple[str, ...]] = []
    _patch_flow(monkeypatch, tmp_path, {("head", "check"): PASSED})
    real_fake_git = linux_check.git

    def spying_git(*args: str) -> bytes:
        recorded_git.append(args)
        return real_fake_git(*args)

    monkeypatch.setattr(linux_check, "git", spying_git)

    exit_code = linux_check.main(["--suite", "check", "--head", "deadbeef"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"linux check: head commit {HEAD_SHORT}, base {BASE_SHORT} (merge base" in out
    assert "(clean)" not in out
    assert "differ from HEAD" not in out
    assert ("status", "--porcelain") not in recorded_git


def test_the_exact_summary_lists_new_known_and_flaky_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    check_head = linux_results.Failures(
        frozenset({"check nodeX", "check nodeZ"}), frozenset({"check nodeX"})
    )
    check_base = linux_results.Failures(frozenset({"check nodeX"}), frozenset({"check nodeX"}))
    e2e_head = linux_results.Failures(
        frozenset({"e2e fileY > testY"}), frozenset({"e2e fileY > testY"})
    )
    recorded = _patch_flow(
        monkeypatch,
        tmp_path,
        {
            ("head", "check"): check_head,
            ("base", "check"): check_base,
            ("head", "e2e"): e2e_head,
            ("base", "e2e"): PASSED,
        },
    )

    exit_code = linux_check.main(["--suite", "check", "--suite", "e2e"])

    assert exit_code == 1
    expected = [
        f"linux check: head working tree on {HEAD_SHORT} (clean), "
        f"base {BASE_SHORT} (merge base with origin/main)",
        "image main: reused",
        "head check: 1 failed (1s)",
        "base check: 1 failed (1s)",
        "head e2e: 1 failed (1s)",
        "base e2e: passed (1s)",
        "new failures: e2e fileY > testY",
        "also failing on main, not counted: check nodeX",
        "flaky, passed on one of its two runs, not counted: check nodeZ",
        f"logs: .linux-check/runs/{STAMP}",
    ]
    assert capsys.readouterr().out.splitlines() == expected
    summary = tmp_path / ".linux-check" / "runs" / STAMP / "summary.txt"
    assert summary.read_text(encoding="utf-8") == "\n".join(expected) + "\n"
    assert recorded["ensure_image"] == ["main"]  # one target, built or reused once


def test_a_runner_error_from_docker_ready_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> str:
        raise linux_check.RunnerError("docker is not ready: boom")

    monkeypatch.setattr(linux_check, "docker_ready", boom)
    exit_code = linux_check.main([])
    assert exit_code == 2
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "linux check could not run: docker is not ready: boom"


def test_a_results_error_from_a_suite_report_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_flow(monkeypatch, tmp_path, {})

    def fake_suite_failures(suite: str, out: Path) -> linux_results.Failures:
        raise linux_results.ResultsError(f"missing steps.jsonl for {suite}")

    monkeypatch.setattr(linux_check.linux_results, "suite_failures", fake_suite_failures)

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 2
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "linux check could not run: missing steps.jsonl for check"


@pytest.mark.parametrize("records", [[], [{"step": "setup", "exit": 0}]])
def test_an_incomplete_container_report_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    records: list[dict[str, object]],
) -> None:
    read_results = linux_results.suite_failures
    _patch_flow(monkeypatch, tmp_path, {})
    report = tmp_path / "report"
    report.mkdir()
    (report / "steps.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    monkeypatch.setattr(linux_check, "run_suite", lambda *a, **k: report)  # noqa: ARG005
    monkeypatch.setattr(linux_results, "suite_failures", read_results)
    assert linux_check.main(["--suite", "check"]) == 2
    assert "incomplete or unexpected check steps" in capsys.readouterr().out


def test_an_unexpected_exception_exits_two_not_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash that is neither a RunnerError nor a ResultsError (a bug anywhere in the flow
    this harness did not anticipate) must still exit 2, named by type, never propagate as an
    uncaught traceback that exits 1 and so reads as "a new failure"."""
    _patch_flow(monkeypatch, tmp_path, {})

    def fake_suite_failures(suite: str, out: Path) -> linux_results.Failures:
        raise KeyError("nodeX")

    monkeypatch.setattr(linux_check.linux_results, "suite_failures", fake_suite_failures)

    exit_code = linux_check.main(["--suite", "check"])

    assert exit_code == 2
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "linux check could not run: KeyError: 'nodeX'"


def test_a_keyboard_interrupt_through_main_is_not_turned_into_exit_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main's catch-all must not catch KeyboardInterrupt or SystemExit: a real Ctrl+C must not
    land in the "could not run" exit 2 bucket."""
    _patch_flow(monkeypatch, tmp_path, {})

    def fake_suite_failures(suite: str, out: Path) -> linux_results.Failures:
        raise KeyboardInterrupt

    monkeypatch.setattr(linux_check.linux_results, "suite_failures", fake_suite_failures)

    with pytest.raises(KeyboardInterrupt):
        linux_check.main(["--suite", "check"])
