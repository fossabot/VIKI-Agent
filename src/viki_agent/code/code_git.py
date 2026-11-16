"""Read-only Git for a trusted code folder: the repository gate and the changes list.

Git runs only after the folder's policy is `ordinary`, and every call carries `HARDENING`, so a
repository's own `core.fsmonitor` hook never runs, its `log.showSignature` starts no signature
program and a path argument is never a pathspec.

A Bot copy's files are the Bot's to write, so a program Git would start from that folder is the
Bot's too. Each command there also runs with hooks at a private empty folder, without the
filter drivers the configuration defines (Git LFS's own commands, which Git starts from PATH,
stay, and download nothing), without a signing program named by a path inside the copy, with
every transport refused (no fetch of a missing object) but for a push and its remote check,
and, for `status` and `diff`, outside nested repositories (`CodeGit.operation`, `CodeGit.git`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal, cast

from viki_agent import rails, winproc
from viki_agent.code import git_guard
from viki_agent.code.code_files import CodeError, CodeFiles, is_private
from viki_agent.code.git_guard import GitGuard
from viki_agent.storage import SessionStore
from viki_agent.workspaces import WorkspaceError, WorkspaceService

HARDENING = ("-c", "core.fsmonitor=false", "-c", "log.showSignature=false", "--literal-pathspecs")
GIT_TIMEOUT_S = 5.0
MAX_CHANGES = 2000
ARGV_PATH_CHARS = 16_000
# The filter drivers and named transports one Bot copy's commands can switch off: each is a few
# `-c` options on every command line there.
MAX_BOT_COPY_SETTINGS = 32

_LOGGER = logging.getLogger(__name__)
# The query's own setting: an answer without it is a failed read, never "nothing is set".
_ASKED = "viki.asked"
# What a Bot copy's commands ask Git first: each filter driver's commands, the transports a
# configuration allows by name, and the programs a signature would start.
_BOT_COPY_SETTINGS = (
    rf"^({re.escape(_ASKED)}|filter\..*\.(clean|smudge|process)|protocol\..*\.allow"
    r"|gpg\.(program|(openpgp|x509|ssh)\.program|ssh\.defaultkeycommand))$"
)
# Git LFS's own commands, as `git lfs install` and Git for Windows write them. Git starts
# `git-lfs` from PATH, never from the copy, and cannot read or write an LFS file without it.
_LFS = {
    "clean": ("git-lfs clean -- %f",),
    "smudge": ("git-lfs smudge -- %f", "git-lfs smudge --skip -- %f"),
    "process": ("git-lfs filter-process", "git-lfs filter-process --skip"),
}
# What a Bot copy's commands tell Git LFS: a server that is no place, and to leave a file as its
# pointer when the repository's own LFS store lacks its object instead of failing the checkout.
# Git LFS otherwise downloads that object from the server `.lfsconfig` names, a file of the
# copy's own; Git's configuration outranks that file.
_LFS_OFFLINE = ("lfs.url=file:///dev/null/no-download", "lfs.skipdownloaderrors=true")
_TRANSPORTS: tuple[str, ...] = ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext")
_USES_A_REMOTE = ("push", "ls-remote")
# What `winproc.run` puts where Git's output was not UTF-8: the name it stood for is lost.
_LOST = chr(0xFFFD)
_CODE = re.compile(r"[MTADRCU.]{2}")
_SUB = re.compile(r"N\.\.\.|S[C.][M.][U.]")
_SCORE = re.compile(r"[RC]\d+")
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_AB = re.compile(r"\+(\d+) -(\d+)")
# Fields before the path, the record type included: paths may hold spaces.
_FIELDS = {"1": 8, "2": 9, "u": 10, "?": 1}

Kind = Literal["tracked", "untracked", "unmerged", "submodule"]
RecordType = Literal["1", "2", "u", "?"]


@dataclass(frozen=True)
class StatusRecord:
    """One porcelain v2 record. `fields` holds every token before the path, the type first;
    modes and object ids are left to callers to check."""

    type: RecordType
    fields: tuple[str, ...]
    path: str
    orig_path: str | None


@dataclass(frozen=True)
class ChangeEntry:
    path: str
    orig_path: str | None
    index: str
    worktree: str
    kind: Kind
    private: bool


@dataclass(frozen=True)
class Changes:
    state: Literal["ready", "unknown"]
    branch: str | None = None
    head: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: tuple[ChangeEntry, ...] = ()
    truncated: bool = False


def _record(record: str, tokens: Iterator[str]) -> StatusRecord:
    """One `1`, `2`, `u` or `?` record; `tokens` yields a rename's origPath."""
    kind = cast(RecordType, record[0])
    fields = record.split(" ", _FIELDS[kind])
    if record[1:2] != " " or len(fields) != _FIELDS[kind] + 1 or not fields[-1]:
        raise ValueError("malformed record")
    orig_path = None
    if kind != "?":
        if not _CODE.fullmatch(fields[1]) or not _SUB.fullmatch(fields[2]):
            raise ValueError("malformed record")
        if kind == "2":
            orig_path = next(tokens, "")
            if not _SCORE.fullmatch(fields[8]) or not orig_path:
                raise ValueError("malformed record")
    return StatusRecord(kind, tuple(fields[:-1]), fields[-1], orig_path)


def status_records(raw: str) -> tuple[dict[str, str], tuple[StatusRecord, ...]]:
    """The `# name value` headers and every record of `git status --porcelain=v2 -z` output,
    with no cap. Every record is checked before any is returned, so a malformed one raises
    `ValueError` rather than a partial list."""
    if raw and not raw.endswith("\0"):
        raise ValueError("unterminated output")
    tokens = iter(raw.split("\0")[:-1])
    headers: dict[str, str] = {}
    records: list[StatusRecord] = []
    for record in tokens:
        if record.startswith("# "):
            name, _, value = record[2:].partition(" ")
            headers[name] = value
            continue
        if record[:1] not in _FIELDS:
            raise ValueError("unknown record")
        records.append(_record(record, tokens))
    return headers, tuple(records)


def _entry(record: StatusRecord) -> ChangeEntry:
    path, orig_path = record.path, record.orig_path
    if record.type == "?":
        return ChangeEntry(path, None, "?", "?", "untracked", is_private(path.split("/")))
    code, sub = record.fields[1], record.fields[2]
    kind: Kind = "submodule" if sub[0] == "S" else "unmerged" if record.type == "u" else "tracked"
    # A rename's old name counts too: its diff shows the old file's content.
    private = is_private(path.split("/")) or (
        orig_path is not None and is_private(orig_path.split("/"))
    )
    return ChangeEntry(path, orig_path, code[0], code[1], kind, private)


def parse_status_v2(raw: str) -> Changes:
    """`git status --porcelain=v2 -z --branch` output, through `status_records`. Unknown
    headers (such as `# stash`, which `status.showStash` adds) are skipped."""
    headers, records = status_records(raw)
    entries = tuple(_entry(record) for record in records[:MAX_CHANGES])
    truncated = len(records) > MAX_CHANGES
    oid, name = headers.get("branch.oid"), headers.get("branch.head")
    if oid is not None and oid != "(initial)" and not _OID.fullmatch(oid):
        raise ValueError("malformed branch.oid")
    ahead = behind = None
    if "branch.ab" in headers:
        counts = _AB.fullmatch(headers["branch.ab"])
        if counts is None:
            raise ValueError("malformed branch.ab")
        ahead, behind = int(counts[1]), int(counts[2])
    return Changes(
        state="ready",
        branch=None if name in (None, "(detached)") else name,
        head=oid[:12] if oid is not None and oid != "(initial)" else None,
        detached=name == "(detached)",
        upstream=headers.get("branch.upstream"),
        ahead=ahead,
        behind=behind,
        entries=entries,
        truncated=truncated,
    )


def chunks(paths: Iterable[str], limit: int = ARGV_PATH_CHARS) -> Iterator[list[str]]:
    """Consecutive groups of paths whose `len(path) + 1` sum fits `limit`; a path longer than
    the limit travels alone."""
    group: list[str] = []
    size = 0
    for path in paths:
        if group and size + len(path) + 1 > limit:
            yield group
            group, size = [], 0
        group.append(path)
        size += len(path) + 1
    if group:
        yield group


def bot_copy_status(git: CodeGit, root: Path) -> tuple[str, ...]:
    """For a `status` in a Bot copy that wants no gitlink row at all. `CodeGit.git` already
    keeps every `status` and `diff` there out of nested repositories (`_outside_nested`)."""
    bot_copy = git._guard is not None and git._guard.bot_copy(root)
    return ("--ignore-submodules=all",) if bot_copy else ()


def _subcommand_at(args: tuple[str, ...]) -> int:
    """Where the subcommand sits, after any `-c name=value` before it; `len(args)` without
    one."""
    at = 0
    while at < len(args) and args[at].startswith("-"):
        at += 2 if args[at] == "-c" else 1
    return at


def _outside_nested(args: tuple[str, ...]) -> tuple[str, ...]:
    """A Bot copy's `status` or `diff` with `--ignore-submodules=dirty`, unless it already
    names `all`. To tell whether a gitlink is modified, both otherwise start Git inside its
    folder, a nested repository whose own config (a filter, a remote's command) is Bot-writable
    and outside the guarded settings. `dirty` still reports a gitlink whose commit moved or was
    staged, so a review keeps its row. It is an option because a copy's `.gitmodules` overrides
    the config default; options end at `--`, since a path after it can be spelled like one."""
    at = _subcommand_at(args)
    if at == len(args) or args[at] not in ("status", "diff"):
        return args
    options = args[at + 1 :]
    if "--" in options:
        options = options[: options.index("--")]
    if "--ignore-submodules=all" in options:
        return args
    return (*args[: at + 1], "--ignore-submodules=dirty", *args[at + 1 :])


@dataclass(frozen=True)
class _Verified:
    """One root's standing for the rest of its operation, from its one verification."""

    options: tuple[str, ...] = ()  # before each subcommand there; only a Bot copy has any
    local: tuple[str, ...] = ()  # `options` without the ones that refuse every transport
    pointer: str | None = None  # what a Bot copy's own `.git` said when it was verified
    unfiltered: tuple[str, ...] = ()  # the filter drivers its commands run without
    rewrites: bool = False  # whether one of them also changes a file on checkout


@dataclass
class _Scope:
    """One operation's roots, for the thread and task that opened it."""

    owner: tuple[int, object]
    verified: dict[Path, _Verified]
    folders: ExitStack  # their private hooks folders, kept until the operation ends


def _pointer(root: Path) -> str:
    """What a linked worktree's own `.git` file says, as Git reads it: the Git folder its
    commands use. "" when it cannot be read as one."""
    try:
        return git_guard._pointer(root / ".git")
    except OSError, ValueError, CodeError:  # missing, a folder or a link, or not text
        return ""


def _asked(raw: str) -> dict[str, str]:
    """Each setting's last value in `config -z --get-regexp` output, the one Git uses.
    `ValueError` unless the query's own setting came back."""
    if not raw.endswith("\0"):
        raise ValueError("unterminated output")
    settings: dict[str, str] = {}
    for entry in raw.split("\0")[:-1]:
        key, _, value = entry.partition("\n")
        settings[key] = value
    if settings.pop(_ASKED, None) != "1":
        raise ValueError("unanswered")
    return settings


def _from_the_copy(program: str) -> bool:
    """Whether Git would start `program` from the folder it runs in: a path with a separator
    (or, on Windows, a drive) and no root. A bare name comes from PATH."""
    if sys.platform == "win32":
        path = PureWindowsPath(program)
        return not path.root and bool(path.drive or "/" in program or "\\" in program)
    return "/" in program and not program.startswith("/")


def _bot_copy_options(settings: dict[str, str], hooks: tuple[str, ...], pointer: str) -> _Verified:
    """What every command in a Bot copy runs with, from the settings Git would use there.

    A filter driver's command, a signing program or a transport's helper is a program Git
    starts in the copy's folder, where a relative path or an interpreter's module search finds a
    file the Bot wrote. So each driver's commands are emptied (`required` too: Git then passes
    the file through unchanged, as it does where a filter is not installed), a signing program
    named by a path inside the copy is emptied (the signature fails, and with it the commit),
    and every transport is refused. A driver made of Git LFS's own commands stays, with Git
    LFS told to download nothing (`_LFS_OFFLINE`). `ValueError` for a name `-c` cannot spell
    (an `=`, or bytes that are not UTF-8 and so read back as U+FFFD), or for too many."""
    drivers: dict[str, dict[str, str]] = {}
    transports = list(_TRANSPORTS)
    signers: list[str] = []
    for key, value in settings.items():
        section, _, rest = key.partition(".")
        name, _, variable = rest.rpartition(".")
        if section == "filter":
            drivers.setdefault(name, {})[variable] = value
        elif section == "protocol":
            if name not in transports:
                transports.append(name)
        elif section == "gpg":
            # Git starts each without a shell; only the key command is split on whitespace.
            words = value.split(maxsplit=1) if variable == "defaultkeycommand" else [value]
            if words and _from_the_copy(words[0]):
                signers.append(key)
    unfiltered: list[str] = []
    lfs = False
    rewrites = False
    for name, commands in drivers.items():
        live = {variable: command for variable, command in commands.items() if command}
        if not live:
            continue
        if all(command in _LFS[variable] for variable, command in live.items()):
            lfs = True
            continue
        unfiltered.append(name)
        # `cat` is what a clean-only driver names to keep a file as it is.
        rewrites = rewrites or "process" in live or live.get("smudge", "cat") != "cat"
    named = [*unfiltered, *transports[len(_TRANSPORTS) :]]
    if len(named) > MAX_BOT_COPY_SETTINGS or any("=" in name or _LOST in name for name in named):
        raise ValueError("settings no option can switch off")
    off = [
        f"filter.{name}.{variable}"
        for name in unfiltered
        for variable in ("clean=", "smudge=", "process=", "required=false")
    ]
    if lfs:
        off += _LFS_OFFLINE
    off += [f"{key}=" for key in signers]
    local = (*hooks, *(part for setting in off for part in ("-c", setting)))
    refused = ["protocol.allow=never", *(f"protocol.{name}.allow=never" for name in transports)]
    options = (*local, *(part for setting in refused for part in ("-c", setting)))
    return _Verified(options, local, pointer, tuple(unfiltered), rewrites)


def _unavailable(subcommand: str, exc: Exception, code: str = "git_unavailable") -> CodeError:
    if isinstance(exc, CodeError) and exc.code == "git_settings_changed":
        return exc
    # Only the subcommand and exception type: a message may hold a path or git stderr.
    _LOGGER.warning("code: git %s failed (%s)", subcommand, type(exc).__name__)
    return CodeError(code)


class CodeGit:
    def __init__(
        self,
        workspaces: WorkspaceService,
        files: CodeFiles,
        *,
        git_available: Callable[[], bool],
        bot_code_store: SessionStore | None = None,
    ) -> None:
        self._workspaces = workspaces
        self._files = files
        self._git_available = git_available
        self._bot_code_store = bot_code_store
        self._guard = GitGuard(workspaces, bot_code_store) if bot_code_store is not None else None
        self._operation: ContextVar[_Scope | None] = ContextVar("code_git_operation", default=None)

    @contextmanager
    def scope(self) -> Iterator[_Scope]:
        """One operation over every root its commands run in, such as a folder and one of its
        worktrees: each root is verified once, before its first command, and stays verified
        until the outermost scope of this thread and task ends. Nothing is kept after that, and
        a child thread or task opens a scope of its own: it may outlive this one."""
        try:
            task: object = asyncio.current_task()
        except RuntimeError:
            task = None
        owner = (threading.get_ident(), task)
        current = self._operation.get()
        if current is not None and current.owner == owner:
            yield current
            return
        with ExitStack() as folders:
            opened = _Scope(owner, {}, folders)
            token = self._operation.set(opened)
            try:
                yield opened
            finally:
                self._operation.reset(token)

    @contextmanager
    def _verified(self, root: Path, fresh: bool) -> Iterator[_Verified]:
        with self.scope() as scope:
            found = None if fresh else scope.verified.get(root)
            # A Bot can rewrite its copy's own `.git` without a permission prompt, and Git
            # follows it to another configuration: read again before each later command, so a
            # change is verified again (and refused) rather than used by the rest of the
            # operation.
            if found is not None and found.pointer is not None and _pointer(root) != found.pointer:
                found = None
            if found is None:
                from viki_agent.code.code_checkout import hooks_off  # it imports this module

                scope.verified.pop(root, None)  # a recheck that refuses leaves it unverified
                bot_copy = self._guard is not None and self._guard.bot_copy(root)
                hooks = scope.folders.enter_context(hooks_off(bot_copy))
                # Read before the check, so a rewrite during it differs at the next command.
                pointer = _pointer(root) if bot_copy else ""
                if self._guard is not None:
                    self._guard.verify(root)
                found = self._read_copy(root, hooks, pointer) if bot_copy else _Verified()
                scope.verified[root] = found
            yield found

    def _read_copy(self, root: Path, hooks: tuple[str, ...], pointer: str) -> _Verified:
        """A verified Bot copy's options, from one read of the settings Git would use there
        (`_bot_copy_options`). The read starts no program: `config` opens neither the index nor
        the work tree. A failed, unparsable or unanswerable one is `git_unavailable`: no
        command runs there with a program left on."""
        try:
            raw = winproc.git(
                root, *HARDENING, *hooks, "-c", f"{_ASKED}=1",
                "config", "-z", "--get-regexp", _BOT_COPY_SETTINGS, timeout=GIT_TIMEOUT_S,
            )  # fmt: skip
            return _bot_copy_options(_asked(raw), hooks, pointer)
        except Exception as exc:  # RuntimeError, ValueError or TimeoutExpired (TID251)
            raise _unavailable("config", exc) from None

    @contextmanager
    def operation(self, root: Path, *, fresh: bool = False) -> Iterator[tuple[str, ...]]:
        """One verification of `root` per operation (`scope`); `fresh` rechecks after a mutation
        lease wait, and a Bot copy whose own `.git` was rewritten since is rechecked before its
        next command. Yields the options every command in `root` runs with, before its
        subcommand: none outside a Bot copy, so a folder's hooks and filters run as its owner
        set them. In a Bot copy, whose files are Bot-writable and outside the guarded settings,
        they put hooks at a private empty folder, empty each filter driver's commands (but Git
        LFS's own) and a signing program named by a path inside the copy, and refuse every
        transport (`_bot_copy_options`). The decision precedes verification. A binding goes in
        only with a new `creating` worktree row (`SessionStore.insert_bot_copy`), so an existing
        path cannot become a Bot copy in between; a binding deleted in between leaves them on.
        No folder, or no answer about the settings, no command: `git_unavailable`."""
        with self._verified(root, fresh) as verified:
            yield verified.options

    def unfiltered(self, root: Path) -> tuple[tuple[str, ...], bool]:
        """The filter drivers `root`'s commands run without (none outside a Bot copy), and
        whether one of them also changes a file on checkout. What Git reads or writes for a
        path under one of them is the file's own bytes, not what the driver would make of
        them."""
        with self._verified(root, False) as verified:
            return verified.unfiltered, verified.rewrites

    def settings_fingerprint(self, root: Path) -> str:
        """Safely inspect current settings without comparing or creating a baseline."""
        if self._guard is None:
            raise CodeError("git_unavailable")
        return self._guard.fingerprint(root)

    @contextmanager
    def workspace_operation(self, workspace_id: str) -> Iterator[None]:
        """Resolve an ordinary code folder before starting a multi-command operation."""
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
        except WorkspaceError:
            raise CodeError("unknown_workspace") from None
        if workspace.policy != "ordinary":
            raise CodeError("trust_required")
        with self.operation(Path(workspace.root.display)):
            yield

    def git(
        self, root: Path, *args: str, timeout: float = GIT_TIMEOUT_S, index: Path | None = None
    ) -> str:
        """`winproc.git` with `HARDENING` and the operation's options, kept out of nested
        repositories in a Bot copy. A push or `ls-remote` there keeps its transport, and with
        it the programs its remote's settings name: both run only in a registered code folder,
        which a copy can be. `index`, a private temporary index, goes on only when given, so a
        stand-in for `winproc.git` without it still answers every other call."""
        with self._verified(root, False) as verified:
            options = verified.options
            if options:  # only a Bot copy's operation has any
                args = _outside_nested(args)
                if "".join(args[_subcommand_at(args) :][:1]) in _USES_A_REMOTE:
                    options = verified.local
            if index is None:
                return winproc.git(root, *HARDENING, *options, *args, timeout=timeout)
            return winproc.git(root, *HARDENING, *options, *args, timeout=timeout, index=index)

    def output(
        self, root: Path, *args: str, timeout: float = GIT_TIMEOUT_S, index: Path | None = None
    ) -> str:
        """Git's output: a non-zero exit is `git_failed`, a timeout `git_unavailable`. `index`
        goes on to `git` only when given."""
        try:
            if index is None:
                return self.git(root, *args, timeout=timeout)
            return self.git(root, *args, timeout=timeout, index=index)
        except RuntimeError as exc:
            raise _unavailable(args[0], exc, "git_failed") from None
        except Exception as exc:  # TimeoutExpired, which TID251 bans naming
            raise _unavailable(args[0], exc) from None

    def answer(self, root: Path, *args: str) -> str | None:
        """The output of a question Git answers by its exit code: None when it exits non-zero.
        A timeout is `git_unavailable`, not an answer."""
        try:
            return self.git(root, *args)
        except RuntimeError:
            return None
        except Exception as exc:  # TimeoutExpired, which TID251 bans naming
            raise _unavailable(args[0], exc) from None

    def head(self, root: Path) -> str:
        """HEAD's full commit id; `no_commits` without one."""
        found = self.answer(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if found is None:
            raise CodeError("no_commits")
        lines = found.splitlines()
        if len(lines) != 1 or not _OID.fullmatch(lines[0]):
            raise CodeError("git_failed")
        return lines[0]

    def branch(self, root: Path) -> str | None:
        """The checked-out branch's name, None on a detached head. Callers apply
        `worktrees.valid_branch`."""
        found = self.answer(root, "symbolic-ref", "--quiet", "HEAD")
        if found is None:
            return None
        lines = found.splitlines()
        if len(lines) != 1 or not lines[0].startswith("refs/heads/") or lines[0] == "refs/heads/":
            raise CodeError("git_failed")
        return lines[0].removeprefix("refs/heads/")

    def repository(self, workspace_id: str) -> Path:
        """The root of a trusted code folder that is itself a repository's top level. The
        trust check comes before any filesystem call or Git command."""
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
        except WorkspaceError:
            raise CodeError("unknown_workspace") from None
        if workspace.policy != "ordinary":
            raise CodeError("trust_required")
        root = self._files.root(workspace_id)
        if rails._has_git_scope_override() or not self._git_available():
            raise CodeError("git_unavailable")
        try:
            output = self.git(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            if isinstance(exc, RuntimeError) and not (root / ".git").exists():
                raise CodeError("not_repository") from None
            raise _unavailable("rev-parse", exc) from None
        lines = output.splitlines()
        if not lines or lines[0] != "true":
            raise CodeError("not_repository")
        if len(lines) > 1 and lines[1]:
            raise CodeError("not_repository_root")
        return root

    def changes(self, workspace_id: str) -> Changes:
        with self.workspace_operation(workspace_id):
            return self._changes(workspace_id)

    def _changes(self, workspace_id: str) -> Changes:
        root = self.repository(workspace_id)
        try:
            raw = self.git(
                root,
                "status",
                "--porcelain=v2",
                "-z",
                "--branch",
                "--untracked-files=normal",
                *bot_copy_status(self, root),
            )
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            raise _unavailable("status", exc) from None
        try:
            return parse_status_v2(raw)
        except ValueError:
            return Changes(state="unknown")
