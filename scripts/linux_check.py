"""The Linux check: tree snapshots, the runner images, one container per suite, and the CLI.

    uv run --directory <repo> python scripts/linux_check.py
        [--suite check|e2e|desktop|package|compile|oldgit]... [--head REV] [--base REF] [--fresh]

`snapshot_tree` packs what `git add -A` would commit and `snapshot_commit` a commit's tree, each
as a tar. `ensure_image` reuses the local image of a `scripts/linux/Dockerfile` target tagged with
that folder's fingerprint, or builds it. `run_suite` feeds a snapshot to `sh /runner/run.sh
<suite>` in a fresh container, copies the container's /out into the run folder and removes the
container with its anonymous volumes on every path: success, failure, timeout or interrupt.

`main` runs every `--suite` (default: check, e2e and desktop) on the head and, only for a suite
with a confirmed failure (`linux_results.Failures.confirmed`) that is not purely a timeout, a
failed setup or a step that failed without naming a test (`linux_results.no_evidence`: a base
run cannot change the verdict of one either way), on the merge base of `--head` (default:
the working tree) with `--base` (default `origin/main`) — cached by base sha, suite and runner
fingerprint unless `--fresh` forces a rerun — then compares the two with `linux_results.compare`
so only a failure the head adds counts. Exit 0 when nothing is new, 1 for a new failure, 2 when
the check itself could not run.

Whatever keeps the check from running (git, docker, a broken container, a base suite that timed
out, failed its setup or failed a step without naming a test, and so cannot say what main fails)
is a RunnerError; a suite's own report being unreadable (including by a byte no UTF-8 decoder
accepts) is a linux_results.ResultsError; any other unexpected exception is still reported the
same way and mapped to exit 2, so a harness crash is never misread as a new failure. Nothing is
pushed, and `docker run` never pulls.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from time import perf_counter, strftime
from typing import Any, BinaryIO, NamedTuple

import linux_results

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "linux"  # the image build context
OUT = REPO / ".linux-check"
# run_suite gives a suite 90 minutes, then removes its container and returns None.
SUITE_LIMIT_S = 90 * 60
SUITE_LIMIT_MIN = SUITE_LIMIT_S // 60
# Each suite of scripts/linux/run.sh and the Dockerfile target whose image runs it.
SUITE_TARGET = {
    "check": "main",
    "e2e": "main",
    "desktop": "main",
    "package": "main",
    "compile": "compile",
    "oldgit": "oldgit",
}
# The removed CI's suites, plus oldgit; package and compile run on request (ADR-056).
DEFAULT_SUITES = ("check", "e2e", "desktop", "oldgit")


class RunnerError(Exception):
    """The Linux check could not run: git or docker failed, or a container broke."""


class Image(NamedTuple):
    tag: str
    built: bool  # False when an image with this tag was already there


def _run(cmd: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
    """subprocess.run without check; a command that cannot start is a RunnerError."""
    try:
        return subprocess.run(cmd, check=False, **options)
    except OSError as error:
        raise RunnerError(f"{cmd[0]} could not start: {error}") from error


def _open_log(path: Path) -> BinaryIO:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("wb")
    except OSError as error:
        raise RunnerError(f"could not write {path}: {error}") from error


def git(*args: str) -> bytes:
    """git's stdout for a command run in REPO; RunnerError when it fails."""
    done = _run(
        ["git", "-C", str(REPO), "--no-optional-locks", "-c", "core.quotepath=off", *args],
        capture_output=True,
    )
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise RunnerError(f"git {' '.join(args)} failed: {detail}")
    return done.stdout


def snapshot_tree() -> bytes:
    """What `git add -A` would commit, as a tar: every tracked and untracked-not-ignored regular
    file with its bytes and modification time, mode 0o644, owner 0:0 and no owner names. A listed
    path that is gone, a folder (a submodule) or a symlink is left out."""
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        # A set: --cached lists an unmerged path once per stage.
        for name in sorted({os.fsdecode(raw) for raw in listed.split(b"\0") if raw}):
            path = REPO / name
            try:
                status = path.lstat()
                if not stat.S_ISREG(status.st_mode):
                    continue
                data = path.read_bytes()
            except FileNotFoundError, NotADirectoryError:
                continue
            except OSError as error:
                raise RunnerError(f"could not read {name}: {error}") from error
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mtime = int(status.st_mtime)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def snapshot_commit(sha: str) -> bytes:
    """The tree committed at `sha`, as a tar."""
    return git("archive", "--format=tar", sha)


def fingerprint() -> str:
    """The first 12 hex characters of a SHA-256 over every file of RUNNER, in order of relative
    path: the path, NUL, the bytes, NUL."""
    digest = hashlib.sha256()
    try:
        names = sorted(p.relative_to(RUNNER).as_posix() for p in RUNNER.rglob("*") if p.is_file())
        for name in names:
            digest.update(name.encode() + b"\0" + (RUNNER / name).read_bytes() + b"\0")
    except OSError as error:
        raise RunnerError(f"could not read the runner files: {error}") from error
    return digest.hexdigest()[:12]


def docker_ready() -> str:
    """The Docker server's version; RunnerError when docker is missing or its server is down."""
    done = _run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True)
    version = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not version:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise RunnerError(f"docker is not ready: {detail}")
    return version


def ensure_image(target: str, run_dir: Path) -> Image:
    """The local image of Dockerfile `target`, tagged `viki-agent-linux:<target>-<fingerprint>`:
    reused when `docker image inspect` finds the tag, else built with its output in
    `run_dir/image-<target>.log`. A failed build is a RunnerError."""
    tag = f"viki-agent-linux:{target}-{fingerprint()}"
    if _run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return Image(tag, built=False)
    log = run_dir / f"image-{target}.log"
    with _open_log(log) as out:
        done = _run(
            ["docker", "build", "--progress=plain", "--target", target, "-t", tag]
            + ["-f", "Dockerfile", "."],
            cwd=RUNNER,
            env={**os.environ, "DOCKER_BUILDKIT": "1"},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    if done.returncode != 0:
        raise RunnerError(f"building the {target} image failed (exit {done.returncode}): see {log}")
    return Image(tag, built=True)


def run_argv(name: str, image: str, suite: str) -> list[str]:
    """`docker run` for one suite: stdin kept open for the tree, an init process, 2 GB of
    /dev/shm, anonymous volumes as /tmp and /home/ubuntu (Docker's disk, not a tmpfs, so the
    e2e scratch under ~/.cache lands on ext4 too) and the named caches; no ports, host network,
    bind mounts, environment or privileges. Only desktop gets `seccomp=unconfined`; compile, the
    compile image's one suite, has caches of its own."""
    compiled = SUITE_TARGET[suite] == "compile"
    prefix = "viki-agent-compile-" if compiled else "viki-agent-"
    argv = ["docker", "run", "-i", "--init", "--shm-size=2g", "--pull=never", "--name", name]
    argv += ["--mount", "type=volume,dst=/tmp"]
    argv += ["--mount", "type=volume,dst=/home/ubuntu"]
    for cache in ("uv", "npm") if compiled else ("uv", "npm", "electron"):
        argv += ["--mount", f"type=volume,src={prefix}{cache}-cache,dst=/cache/{cache}"]
    if suite == "desktop":
        argv += ["--security-opt", "seccomp=unconfined"]
    return [*argv, image, "sh", "/runner/run.sh", suite]


def _remove_container(name: str) -> None:
    """`docker rm -f -v name`, immune to a second Ctrl+C: SIGINT is ignored on this process for
    as long as the removal runs (restored after, even if it raises), and the child starts in its
    own process group so the console's Ctrl+C/Ctrl+Break is not also delivered to it directly.
    Tried twice; if both attempts fail (or cannot even start), one line names the container so
    the user can remove it by hand instead of it running for up to SUITE_LIMIT_S with its
    multi-GB volume. Never raises: this must not replace the error or interrupt that is already
    propagating out of `run_suite`."""
    kwargs: dict[str, Any] = {"capture_output": True, "check": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for _ in range(2):
            try:
                done = subprocess.run(["docker", "rm", "-f", "-v", name], **kwargs)
            except OSError:
                continue
            if done.returncode == 0:
                return
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"could not remove container {name}; remove it by hand", flush=True)


def run_suite(side: str, suite: str, tree: bytes, image: str, run_dir: Path) -> Path | None:
    """Run `suite` in a fresh container of `image` fed `tree` on stdin, with its output in
    `run_dir/<side>-<suite>.log`, and copy the container's /out to `run_dir/<side>-<suite>`,
    which is returned. None when the suite ran past SUITE_LIMIT_S: the docker client is killed
    then, and `docker rm -f` stops the container. RunnerError when the container exits non-zero
    or /out cannot be copied. The container goes with its anonymous volumes on every path."""
    folder = f"{side}-{suite}"
    name = f"viki-agent-linux-check-{run_dir.name}-{folder}"
    log = run_dir / f"{folder}.log"
    with _open_log(log) as out:
        try:
            done = _run(
                run_argv(name, image, suite),
                input=tree,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=SUITE_LIMIT_S,
            )
            if done.returncode != 0:
                raise RunnerError(
                    f"{side} {suite}: the container exited {done.returncode}: see {log}"
                )
            # A relative destination: docker cp would read a drive's colon as container:path.
            copied = _run(
                ["docker", "cp", f"{name}:/out", folder],
                cwd=run_dir,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
            if copied.returncode != 0:
                raise RunnerError(f"{side} {suite}: copying /out failed: see {log}")
        except subprocess.TimeoutExpired:
            return None
        finally:
            # Also when docker run never created it (rm then exits 1); see _remove_container for
            # why a second Ctrl+C or a failed removal must not replace the error or interrupt
            # that brought us here.
            _remove_container(name)
    return run_dir / folder


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The CLI in the module docstring. ``--suite`` is repeatable; with none given, the default
    suites run. A repeat is kept once, in the order first given."""
    parser = argparse.ArgumentParser(description="Run the Linux check suites in Docker.")
    parser.add_argument(
        "--suite",
        action="append",
        choices=list(SUITE_TARGET),
        dest="suites",
        help=f"a suite to run, repeatable (default: {', '.join(DEFAULT_SUITES)})",
    )
    parser.add_argument("--head", metavar="REV", help="a commit-ish; default: the working tree")
    parser.add_argument(
        "--base",
        default="origin/main",
        metavar="REF",
        help="compared against the head (default: origin/main)",
    )
    parser.add_argument("--fresh", action="store_true", help="ignore a cached base result")
    args = parser.parse_args(argv)
    args.suites = list(dict.fromkeys(args.suites or DEFAULT_SUITES))
    return args


def _run_id() -> str:
    """A run folder name unique across processes: a timestamp plus this process's id, since two
    runs started in the same second would otherwise collide (and the second one's cleanup would
    remove the first one's container)."""
    return f"{strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def _another_run_is_active() -> bool:
    """Whether a container from another linux_check run looks to be active, by its name prefix. A
    non-zero exit (an odd docker flag problem, not checked by docker_ready()) is read as "no";
    docker itself not starting still raises RunnerError, same as every other docker call here."""
    done = _run(
        ["docker", "ps", "-q", "--filter", "name=viki-agent-linux-check-"], capture_output=True
    )
    return done.returncode == 0 and bool(done.stdout.strip())


def _side_result(
    side: str, suite: str, tree: bytes, image: str, run_dir: Path
) -> linux_results.Failures:
    """``suite``'s Failures for one side (``"head"`` or ``"base"``): run it in one container and
    read its copied ``/out``, or, when it runs past SUITE_LIMIT_S, a marker id standing in for the
    report a timed-out suite never produced."""
    out = run_suite(side, suite, tree, image, run_dir)
    if out is None:
        marker = linux_results.timeout_id(suite, SUITE_LIMIT_MIN)
        return linux_results.Failures(first=frozenset({marker}), still=frozenset({marker}))
    return linux_results.suite_failures(suite, out)


def _passfail(still: frozenset[str]) -> str:
    return "passed" if not still else f"{len(still)} failed"


def _needs_base(confirmed: frozenset[str]) -> bool:
    """Whether a suite's confirmed-failing ids (`linux_results.Failures.confirmed`) are worth a
    base run at all: there is nothing to compare when none are failing, and a base run cannot
    change the verdict when every one of them is a marker that names no test
    (`linux_results.no_evidence`; `linux_results.compare` always counts those as new). An id
    failing on only one of the suite's two runs is not confirmed, so it never starts a base run
    by itself."""
    return bool(confirmed) and not all(linux_results.no_evidence(id_) for id_ in confirmed)


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise RunnerError(f"could not write {path}: {error}") from error


def _load_base(cache_path: Path) -> linux_results.Failures | None:
    """The cached Failures ``to_json`` wrote to ``cache_path``, or ``None`` when its ``"version"``
    is not ``linux_results.CACHE_VERSION``: a cache written under other rules is a cache miss, not
    an error, so ``_run_check`` reruns the base and overwrites the file with a fresh one. A file
    that cannot even be read as JSON, is not an object, or has a malformed ``first``/``still``
    still fails closed the same way ``from_json`` already does: a ResultsError, not a crash or a
    silent cache miss."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise linux_results.ResultsError(f"unreadable base cache {cache_path}: {error}") from error
    if not isinstance(data, dict):
        raise linux_results.ResultsError(f"malformed base cache, not an object: {cache_path}")
    if data.get("version") != linux_results.CACHE_VERSION:
        return None
    return linux_results.from_json(data)


def _run_check(args: argparse.Namespace) -> int:
    """Run every ``args.suites`` entry per the module docstring and return the process's exit
    code. Raises RunnerError or linux_results.ResultsError when the check itself cannot run;
    ``main`` turns either into exit code 2."""
    docker_ready()
    rev = args.head if args.head is not None else "HEAD"
    head_sha = git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    base_sha = git("merge-base", head_sha, args.base).decode().strip()
    head_short = git("rev-parse", "--short", head_sha).decode().strip()
    base_short = git("rev-parse", "--short", base_sha).decode().strip()
    fp = fingerprint()

    run_dir = OUT / "runs" / _run_id()
    run_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    def emit(line: str) -> None:
        print(line, flush=True)
        lines.append(line)

    if args.head is None:
        status = git("status", "--porcelain").decode("utf-8", "replace")
        differing = [line for line in status.splitlines() if line.strip()]
        note = "(clean)" if not differing else f"({len(differing)} paths differ from HEAD)"
        head_label = f"working tree on {head_short} {note}"
        head_tree = snapshot_tree()
    else:
        head_label = f"commit {head_short}"
        head_tree = snapshot_commit(head_sha)

    emit(f"linux check: head {head_label}, base {base_short} (merge base with {args.base})")
    if _another_run_is_active():
        emit("warning: another linux check looks to be running; containers may compete for load")

    images: dict[str, Image] = {}
    base_tree: bytes | None = None
    new: set[str] = set()
    known: set[str] = set()
    flaky: set[str] = set()

    for suite in args.suites:
        target = SUITE_TARGET[suite]
        image = images.get(target)
        if image is None:
            started = perf_counter()
            image = ensure_image(target, run_dir)
            images[target] = image
            elapsed = perf_counter() - started
            if image.built:
                emit(f"image {target}: built in {elapsed:.0f}s")
            else:
                emit(f"image {target}: reused")

        started = perf_counter()
        head_failures = _side_result("head", suite, head_tree, image.tag, run_dir)
        elapsed = perf_counter() - started
        emit(f"head {suite}: {_passfail(head_failures.confirmed)} ({elapsed:.0f}s)")

        base_failures: linux_results.Failures | None = None
        if _needs_base(head_failures.confirmed):
            cache_path = OUT / "base" / f"{base_sha}-{suite}-{fp}.json"
            loaded = _load_base(cache_path) if not args.fresh and cache_path.exists() else None
            if loaded is not None:
                base_failures = loaded
                emit(f"base {suite}: {_passfail(base_failures.still)} (cached)")
            else:
                if base_tree is None:
                    base_tree = snapshot_commit(base_sha)
                started = perf_counter()
                base_failures = _side_result("base", suite, base_tree, image.tag, run_dir)
                # A base that never ran its tests, or whose step failed without naming one, cannot
                # say what main fails. Comparing against it, or caching it, would settle every
                # later verdict for this base on no evidence.
                base_failed = base_failures.first | base_failures.still
                blind = sorted(filter(linux_results.no_evidence, base_failed))
                if blind:
                    raise RunnerError(
                        f"base {suite} gave no result ({', '.join(blind)}), so what main fails "
                        f"is unknown: see {run_dir / f'base-{suite}.log'}"
                    )
                elapsed = perf_counter() - started
                emit(f"base {suite}: {_passfail(base_failures.still)} ({elapsed:.0f}s)")
                _write_text(cache_path, json.dumps(linux_results.to_json(base_failures)))

        verdict = linux_results.compare(head_failures, base_failures)
        new |= verdict.new
        known |= verdict.known
        flaky |= verdict.flaky

    emit("new failures: " + (", ".join(sorted(new)) if new else "none"))
    if known:
        emit("also failing on main, not counted: " + ", ".join(sorted(known)))
    if flaky:
        emit("flaky, passed on one of its two runs, not counted: " + ", ".join(sorted(flaky)))
    emit(f"logs: {run_dir.relative_to(REPO).as_posix()}")

    _write_text(run_dir / "summary.txt", "\n".join(lines) + "\n")
    return 1 if new else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run_check(args)
    except (RunnerError, linux_results.ResultsError) as error:
        print(f"linux check could not run: {error}")
        return 2
    except Exception as error:
        # Anything else unexpected: a harness crash must never read as a new failure (exit 1).
        print(f"linux check could not run: {type(error).__name__}: {error}")
        return 2


def _route_stop_signals() -> None:
    # Mirrors scripts/check.py: once a POSIX stage runs in its own session, a SIGTERM or SIGHUP to
    # this process's group no longer reaches it; converting them to KeyboardInterrupt keeps
    # run_suite's container cleanup on the same path as Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if sys.platform != "win32":  # SIGHUP doesn't exist on win32; same condition as hasattr here
        signal.signal(signal.SIGHUP, signal.default_int_handler)


if __name__ == "__main__":
    _route_stop_signals()
    sys.exit(main())
