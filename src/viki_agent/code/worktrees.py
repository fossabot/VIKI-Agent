"""App-created Git worktrees of a trusted code folder.

Each worktree is one folder `<area>/<worktree_id>` in a private area beside the data folder, made
by `git worktree add -b` from the folder's last commit under the whole-root lease, so no code save
or other worktree operation in that folder runs meanwhile; a Bot's copy (`create(hooks=False)`) is
made with the repository's hooks off. Removal runs `git worktree remove`
(without `--force`) under the same lease, only when `git status` shows no change, and the branch
stays. The cleanup sweep (`sweep`; `plan` is its dry run) decides each `ready` row by
`worktree_rules` and acts at once under the same lease: it archives a tree with changes or
unpushed commits (`worktree_archive`), renames the tree aside to `<area>/.trash-<worktree_id>`,
unregisters it with `git worktree remove`, deletes its branch unless a remote or another worktree
holds it, and deletes the renamed folder after the lease; one it could not delete is tried again
at the start of every sweep, cleanup on or off. A Bot copy still bound to its chat or
Bot thread is kept (`bot_chat`). A released one (`release_copies`, at recovery and at the start
of every sweep, cleanup on or off) goes through the same checks and acts without the idle
window: removed when merged, else archived with its branch kept; its binding is forgotten only
once its row is `removed`. A restore (`restore`; `archives` lists
what it can bring back) adds an archived tree again at its path under the same lease with `git
worktree add --no-checkout`, on its branch while that still names the archived HEAD and no
worktree has it checked out, or on that branch made again when the sweep deleted it, then checks
the archive out into it. A launch reconciles rows a crash left in `creating` or `removing`
against `git worktree list` (a restore cut short keeps its archive) and deletes nothing on disk.
Git runs only in trusted folders and only through `CodeGit.git`, so `HARDENING` applies. Each
phase opens one `CodeGit.operation` before its own catches (`_guarded`), so a folder whose Git
settings no longer match its Bot-copy baseline refuses `git_settings_changed` rather than read as
a Git failure; the background recovery and dry run skip such a folder or row instead. Logs carry
the Git subcommand, the exception type or the refusal code, never a path, branch name or id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from viki_agent.code import worktree_archive
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit, parse_status_v2, status_records
from viki_agent.code.session_roots import SessionRoots
from viki_agent.code.worktree_probe import has_process_inside, inspect
from viki_agent.code.worktree_rules import ARCHIVE_DAYS, Decision, Facts, decide
from viki_agent.code.worktree_rules import SWEEP_INTERVAL_S as SWEEP_INTERVAL_S  # for the launch
from viki_agent.protection import (
    ProtectionError,
    create_private_directory,
    require_private_directory,
)
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore, StateError, WorktreeRow
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

MAX_WORKTREES = 32
MAX_ARCHIVES = 256  # the most `archives` answers, newest first
ADD_TIMEOUT_S = 120.0
REMOVE_TIMEOUT_S = 60.0
LEASE_WAIT_S = 10.0
PLAN_DEADLINE_S = 50  # a dry run's total budget, so one more row still fits under the client's 120s
# Every row state: the sweep's archive retention needs the folders of `removed` rows too.
_EVERY_STATE = ("creating", "ready", "removing", "kept", "removed")

_LOGGER = logging.getLogger(__name__)
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TRASH = re.compile(r"\.trash-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The resolver's and the lease's codes under the names the routes map.
_RENAMED = {
    "ordinary_workspace_required": "trust_required",
    "stale_root": "root_unavailable",
    "git_common_unsafe": "git_common_unavailable",
}
# A refused `revive_worktree` under the names the routes map; any other code is `unavailable`.
_REVIVE_REFUSED = {
    "capacity": "worktree_limit",
    "conflict": "worktree_busy",
    "not_found": "unknown_worktree",
}


class WorktreeError(Exception):
    """A refused worktree operation: only `.code`, never a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Worktree:
    worktree_id: str
    workspace_id: str
    path: str
    branch: str
    base_oid: str
    state: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: WorktreeRow) -> Worktree:
        return cls(**row)


@dataclass(frozen=True)
class Removal:
    worktree: Worktree  # with its state after the removal
    outcome: Literal["removed", "kept"]
    reason: Literal["dirty", "git_refused", "unknown_state"] | None
    changes: int | None


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    locked: bool
    prunable: bool
    branch: str | None  # the full ref of a `branch <ref>` field, None without one
    detached: bool


@dataclass(frozen=True)
class _Folder:
    """One registered code folder's admission and Git bookkeeping, loaded once and shared by
    every row `plan` or the acting sweep decides for it. `listing` is None when `worktree
    list` itself could not be read, so every row of that folder reads as `unknown` rather
    than guess at its state. `root` is the admitted folder the sweep leases, None when the
    folder is not trusted or its Git settings no longer match its baseline."""

    trusted: bool
    listing: dict[str, WorktreeEntry] | None
    root: OrdinaryMutationRoot | None = None


def parse_worktree_list(raw: str) -> dict[str, WorktreeEntry]:
    """`git worktree list --porcelain -z`, keyed by `root_key(path)`. A strict reader: any field
    or shape not listed here raises `ValueError`, so the caller treats the whole listing as
    unknown rather than act on part of it."""
    if not raw.endswith("\0"):
        raise ValueError("unterminated output")
    entries: dict[str, WorktreeEntry] = {}
    record: list[str] = []
    for field in raw.split("\0")[:-1]:
        if field:
            record.append(field)
            continue
        entry = _worktree_entry(record)
        entries[root_key(entry.path)] = entry
        record = []
    if record or not entries:
        raise ValueError("unterminated record")
    return entries


def _listing_key(path: str) -> str:
    """A row's key in `parse_worktree_list`: Git lists a worktree by its real path (8.3 short
    names expanded, junctions resolved), and `realpath` resolves the part of the path that still
    exists even when the worktree's folder is gone."""
    return root_key(os.path.realpath(path))


def _worktree_entry(record: list[str]) -> WorktreeEntry:
    if not record or not record[0].startswith("worktree ") or record[0] == "worktree ":
        raise ValueError("record without worktree")
    locked = prunable = detached = False
    branch: str | None = None
    for field in record[1:]:
        name, _, value = field.partition(" ")
        if (name == "HEAD" and _OID.fullmatch(value)) or field == "bare":
            continue
        if name == "branch" and value:
            branch = value
        elif field == "detached":
            detached = True
        elif name == "locked":
            locked = True
        elif name == "prunable":
            prunable = True
        else:
            raise ValueError("unknown field")
    return WorktreeEntry(record[0][len("worktree ") :], locked, prunable, branch, detached)


def worktree_area(data_dir: Path) -> Path:
    return data_dir.parent / f"{data_dir.name}-worktrees"


def valid_branch(name: str) -> bool:
    """A narrow subset of Git's ref-name rules, so a name is never an option, a range or a
    reflog expression."""
    return (
        _BRANCH.fullmatch(name) is not None
        and not name.startswith(("-", "/"))
        and not name.endswith(("/", ".", ".lock"))
        and not any(part in name for part in ("//", "..", "@{"))
        and not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))
        and name != "HEAD"
    )


def _git_failed(subcommand: str, exc: Exception) -> WorktreeError:
    # Only the subcommand and exception type: a message may hold a path or git stderr.
    _LOGGER.warning("worktree: git %s failed (%s)", subcommand, type(exc).__name__)
    return WorktreeError("git_failed")


@contextmanager
def _hooks_off() -> Iterator[tuple[str, ...]]:
    """`-c core.hooksPath=<an empty private folder>`, so no repository hook runs. An `OSError`
    making that folder (no usable temp folder, a full disk) is `WorktreeError("unavailable")`."""
    try:
        tmp = tempfile.TemporaryDirectory(prefix="viki-worktree-", ignore_cleanup_errors=True)
    except OSError:
        raise WorktreeError("unavailable") from None
    with tmp as folder:
        yield ("-c", f"core.hooksPath={folder}")


def _plain_folder(entry: os.DirEntry[str]) -> bool:
    """A real folder: not a link, a junction or any other reparse point."""
    info = entry.stat(follow_symlinks=False)
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _writable_again(function: Callable[..., object], path: str, _exc: BaseException) -> None:
    """`shutil.rmtree`'s `onexc`: make the entry it failed on writable (Windows refuses to
    delete a read-only one), without following a link, and retry that step once; a second
    failure propagates."""
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWRITE, follow_symlinks=False)
    function(path)


def _open_folders(trash: Path) -> None:
    """On POSIX, before `rmtree`: give each folder in `trash` that this user owns user read,
    write and search permission, top-down and without following a link, since deleting an
    entry needs write permission on its folder, and emptying a folder needs read and search
    permission on it. A folder another user owns stays as it is, so `rmtree` fails there."""
    if sys.platform == "win32":
        return
    uid = os.getuid()
    folders = [str(trash)]
    while folders:
        folder = folders.pop()
        info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode):
            continue  # `trash` itself a link: `rmtree` refuses it
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid == uid and (mode & stat.S_IRWXU) != stat.S_IRWXU:
            os.chmod(folder, mode | stat.S_IRWXU, follow_symlinks=False)
        with os.scandir(folder) as entries:
            folders.extend(entry.path for entry in entries if entry.is_dir(follow_symlinks=False))


def _delete_trash(trash: Path) -> None:
    """Delete a tree the sweep renamed aside; a failure leaves it for the next sweep's purge."""
    try:
        _open_folders(trash)
        shutil.rmtree(trash, onexc=_writable_again)
    except Exception as exc:  # OSError, or whatever the retry raised
        _LOGGER.warning("worktree: trash not deleted (%s)", type(exc).__name__)


class WorktreeService:
    def __init__(
        self,
        store: SessionStore,
        workspaces: WorkspaceService,
        git: CodeGit,
        coordinator: MutationCoordinator,
        resolve_root: WorkspaceMutationResolver,
        roots: SessionRoots,
        *,
        area: Path,
        in_use: Callable[[str], bool] = lambda _worktree_id: False,
        enabled: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._git = git
        self._coordinator = coordinator
        self._resolve_root = resolve_root
        self._roots = roots
        self._area = area
        self._in_use = in_use  # a live check or terminal in the worktree, asked on the loop
        self._enabled = enabled  # the cleanup setting, asked fresh each sweep (P7 wires it)
        self._now = now
        # Ids a removal of this service holds, touched only on the loop: a second removal of
        # one id must not lift the first one's spawn block, since `unblock` is not counted.
        self._removing: set[str] = set()

    def list(self, limit: int = 256) -> tuple[list[Worktree], bool]:
        rows, more = self._store.list_worktrees(limit)
        return [Worktree.from_row(row) for row in rows], more

    @contextmanager
    def _guarded(self, root: Path, *, fresh: bool = False) -> Iterator[tuple[str, ...]]:
        """One Git operation on `root`: its settings are checked against the folder's Bot-copy
        baseline here, before the phase's own catches, so a mismatch is `git_settings_changed`
        and never `git_failed` or `no_commits`. `fresh` checks again after a lease wait. Yields
        the options `root`'s own commands run with (`CodeGit.operation`), for a command that
        another folder runs on it."""
        try:
            with self._git.operation(root, fresh=fresh) as options:
                yield options
        except CodeError as exc:
            raise WorktreeError(exc.code) from None

    async def create(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        branch: str,
        *,
        hooks: bool = True,
    ) -> Worktree:
        """`hooks=False`, for a Bot's copy, runs no repository hook during the add."""
        if not valid_branch(branch):
            raise WorktreeError("invalid_branch")
        root, row = await asyncio.to_thread(
            self._prepare, workspace_id, policy_revision, root_physical_id, branch
        )
        adding = False
        try:
            async with self._lease(root):
                await self._require_new_branch(root.path, branch)
                adding = True  # from here a failure may leave a folder behind
                return await self._add(root.path, row, hooks=hooks)
        except BaseException:
            if not adding:
                await asyncio.to_thread(self._discard, row["worktree_id"])
            raise

    def _prepare(
        self, workspace_id: str, policy_revision: int, root_physical_id: str, branch: str
    ) -> tuple[OrdinaryMutationRoot, WorktreeRow]:
        """The checks before the lease, then the `creating` row that reserves a slot."""
        try:
            self._git.repository(workspace_id)
        except CodeError as exc:
            raise WorktreeError(exc.code) from None
        try:
            root = self._resolve_root(workspace_id, policy_revision, root_physical_id, kind="code")
        except MutationError as exc:
            raise WorktreeError(_RENAMED.get(exc.code, exc.code)) from None
        with self._guarded(root.path):
            try:
                base_oid = self._git.git(
                    root.path, "rev-parse", "--verify", "HEAD^{commit}"
                ).strip()
            except Exception:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
                raise WorktreeError("no_commits") from None
        if not _OID.fullmatch(base_oid):
            raise WorktreeError("git_failed")
        try:
            create_private_directory(self._area)
            require_private_directory(self._area)
        except OSError, ProtectionError:
            raise WorktreeError("unavailable") from None
        worktree_id = str(uuid.uuid4())
        stamp = now_iso()
        row: WorktreeRow = {
            "worktree_id": worktree_id,
            "workspace_id": workspace_id,
            "path": str(self._area / worktree_id),
            "branch": branch,
            "base_oid": base_oid,
            "state": "creating",
            "created_at": stamp,
            "updated_at": stamp,
        }
        try:
            self._store.insert_worktree(row, limit=MAX_WORKTREES)
        except StateError as exc:
            raise WorktreeError(
                "worktree_limit" if exc.code == "capacity" else "unavailable"
            ) from None
        return root, row

    @asynccontextmanager
    async def _lease(self, root: OrdinaryMutationRoot) -> AsyncIterator[None]:
        """The whole root and its Git common dir, with admission checked after the wait."""
        try:
            async with self._coordinator.lease(
                root, (), include_git_common=True, wait_s=LEASE_WAIT_S
            ):
                current = await asyncio.to_thread(
                    self._resolve_root,
                    root.workspace_id,
                    root.policy_revision,
                    root.physical_id,
                    kind="code",
                )
                if current != root:
                    raise MutationError("workspace_changed")
                yield
        except MutationError as exc:
            raise WorktreeError(_RENAMED.get(exc.code, exc.code)) from None

    async def _require_new_branch(self, root: Path, branch: str) -> None:
        if (await asyncio.to_thread(self._branch_refs, root, branch)).strip():
            raise WorktreeError("branch_exists")

    def _branch_refs(self, root: Path, branch: str) -> str:
        with self._guarded(root, fresh=True):  # the first Git call after the lease wait
            try:
                return self._git.git(
                    root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"
                )
            except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
                raise _git_failed("for-each-ref", exc) from None

    async def _add(self, root: Path, row: WorktreeRow, *, hooks: bool) -> Worktree:
        try:
            await asyncio.to_thread(self._git_add, root, row, hooks)
        except WorktreeError:  # changed settings or no hooks folder: refused before the add
            await asyncio.to_thread(self._settle_failed_add, row)
            raise
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            await asyncio.to_thread(self._settle_failed_add, row)
            raise _git_failed("worktree add", exc) from None
        stamp = now_iso()
        try:
            await asyncio.to_thread(
                self._store.update_worktree_state, row["worktree_id"], "creating", "ready", stamp
            )
        except StateError:
            raise WorktreeError("unavailable") from None
        return Worktree.from_row({**row, "state": "ready", "updated_at": stamp})

    def _git_add(self, root: Path, row: WorktreeRow, hooks: bool) -> None:
        """`git worktree add`, with long paths on (Windows) and, unless `hooks`, the checkout
        hooks off."""
        args = (
            *worktree_archive.LONG_PATHS, "worktree", "add", "-b", row["branch"], row["path"],
            row["base_oid"],
        )  # fmt: skip
        with self._guarded(root):
            if hooks:
                self._git.git(root, *args, timeout=ADD_TIMEOUT_S)
                return
            with _hooks_off() as off:
                self._git.git(root, *off, *args, timeout=ADD_TIMEOUT_S)

    def _settle_failed_add(self, row: WorktreeRow) -> None:
        """A folder the failed add left stays, recorded as `kept`; without one the row goes."""
        try:
            if Path(row["path"]).exists(follow_symlinks=False):
                self._store.update_worktree_state(row["worktree_id"], "creating", "kept", now_iso())
            else:
                self._store.delete_worktree(row["worktree_id"], expected_state="creating")
        except StateError as exc:
            _LOGGER.warning("worktree: failed add not recorded (%s)", exc.code)

    def _discard(self, worktree_id: str) -> None:
        try:
            self._store.delete_worktree(worktree_id, expected_state="creating")
        except StateError as exc:
            _LOGGER.warning("worktree: refused create not discarded (%s)", exc.code)

    # --- remove ---------------------------------------------------------------------------------

    async def remove(
        self, worktree_id: str, policy_revision: int, root_physical_id: str
    ) -> Removal:
        row, root = await asyncio.to_thread(
            self._removal_target, worktree_id, policy_revision, root_physical_id
        )
        read = row["state"]
        if worktree_id in self._removing:
            raise WorktreeError("worktree_busy")
        # Blocked on the loop before `live_in`: a spawn that passed its blocked check earlier is
        # already live, as no await separates that check from the driver.
        self._removing.add(worktree_id)
        self._roots.block(worktree_id)
        try:
            if await self._roots.live_in(worktree_id) or self._in_use(worktree_id):
                raise WorktreeError("worktree_in_use")
            if sys.platform != "win32" and await asyncio.to_thread(
                has_process_inside, Path(row["path"])
            ):
                raise WorktreeError("worktree_in_use")
            try:
                await asyncio.to_thread(
                    self._store.update_worktree_state, worktree_id, read, "removing", now_iso()
                )
            except StateError as exc:
                code = "unknown_worktree" if exc.code == "not_found" else "worktree_busy"
                raise WorktreeError(code) from None
            entered = False
            try:
                async with self._lease(root):
                    entered = True
                    return await asyncio.to_thread(self._settle_removal, root.path, row, read)
            except BaseException:
                if not entered:
                    await asyncio.to_thread(self._put_back, worktree_id, read)
                raise
        finally:
            self._roots.unblock(worktree_id)
            self._removing.discard(worktree_id)

    def _removal_target(
        self,
        worktree_id: str,
        policy_revision: int,
        root_physical_id: str,
        states: tuple[str, ...] = ("ready", "kept"),
    ) -> tuple[WorktreeRow, OrdinaryMutationRoot]:
        """The row, in one of `states`, and its folder admitted at the caller's pinned
        revision."""
        row = self._store.get_worktree(worktree_id)
        if row is None:
            raise WorktreeError("unknown_worktree")
        if row["state"] not in states:
            raise WorktreeError("worktree_not_ready")
        try:
            self._git.repository(row["workspace_id"])
        except CodeError as exc:
            raise WorktreeError(exc.code) from None
        try:
            root = self._resolve_root(
                row["workspace_id"], policy_revision, root_physical_id, kind="code"
            )
        except MutationError as exc:
            raise WorktreeError(_RENAMED.get(exc.code, exc.code)) from None
        return row, root

    def _settle_removal(self, code_root: Path, row: WorktreeRow, read: str) -> Removal:
        """Under the lease: keep a worktree with any change in the state `read` it had before
        this removal (a `kept` one, such as a half-restored tree, stays `kept`, so no sweep
        archives it over its archive), else let Git remove it."""
        # POSIX can remove an open cwd. Recheck after the lease wait, before reading or
        # changing Git state, and keep the row's original state when a process entered meanwhile.
        if sys.platform != "win32" and has_process_inside(Path(row["path"])):
            self._put_back(row["worktree_id"], read)
            raise WorktreeError("worktree_in_use")
        try:
            with self._guarded(code_root, fresh=True):
                return self._settle_guarded_removal(code_root, row, read)
        except WorktreeError as exc:
            if exc.code == "git_settings_changed":  # nothing changed: the row goes back
                self._put_back(row["worktree_id"], read)
            raise

    def _settle_guarded_removal(self, code_root: Path, row: WorktreeRow, read: str) -> Removal:
        tree = Path(row["path"])
        options: tuple[str, ...] = ()
        if tree.exists(follow_symlinks=False):
            with self._guarded(tree) as options:
                try:
                    raw = self._git.git(
                        tree, "status", "--porcelain=v2", "-z", "--untracked-files=normal"
                    )
                    changes = parse_status_v2(raw)
                except Exception as exc:  # ValueError, RuntimeError or TimeoutExpired (TID251)
                    _git_failed("status", exc)
                    return self._finish(row, "kept", "kept", "unknown_state", None)
            if changes.entries:
                return self._finish(row, read, "kept", "dirty", len(changes.entries))
        elif not self._still_listed(code_root, row):
            # Folder gone and registration pruned: `worktree remove` would refuse forever.
            return self._finish(row, "removed", "removed", None, None)
        try:
            # Git checks the tree is clean with a `status` of its own, started inside the tree
            # with this command's settings: a Bot copy's options keep its programs off there too.
            self._git.git(
                code_root, *options, "worktree", "remove", row["path"], timeout=REMOVE_TIMEOUT_S
            )
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            _git_failed("worktree remove", exc)
            return self._finish(row, "kept", "kept", "git_refused", None)
        return self._finish(row, "removed", "removed", None, None)

    def _still_listed(self, code_root: Path, row: WorktreeRow) -> bool:
        """Whether Git lists the worktree; an unreadable listing or a path that cannot be
        resolved counts as listed."""
        try:
            listed = parse_worktree_list(
                self._git.git(code_root, "worktree", "list", "--porcelain", "-z")
            )
        except Exception as exc:  # ValueError, RuntimeError or TimeoutExpired (TID251)
            _git_failed("worktree list", exc)
            return True
        try:
            key = _listing_key(row["path"])
        except OSError as exc:
            _LOGGER.warning("worktree: path not resolved (%s)", type(exc).__name__)
            return True
        return key in listed

    def _finish(
        self,
        row: WorktreeRow,
        state: str,
        outcome: Literal["removed", "kept"],
        reason: Literal["dirty", "git_refused", "unknown_state"] | None,
        changes: int | None,
    ) -> Removal:
        stamp = now_iso()
        try:
            self._store.update_worktree_state(row["worktree_id"], "removing", state, stamp)
        except StateError as exc:
            # The launch's recovery settles a row left in `removing`.
            _LOGGER.warning("worktree: removal not recorded (%s)", exc.code)
            raise WorktreeError("unavailable") from None
        worktree = Worktree.from_row({**row, "state": state, "updated_at": stamp})
        return Removal(worktree, outcome, reason, changes)

    def _put_back(self, worktree_id: str, state: str) -> None:
        try:
            self._store.update_worktree_state(worktree_id, "removing", state, now_iso())
        except StateError as exc:
            _LOGGER.warning("worktree: refused removal not put back (%s)", exc.code)

    # --- launch recovery --------------------------------------------------------------------------

    async def recover(self) -> None:
        """Settle rows a crash left in `creating` or `removing`, deleting or moving nothing on
        disk, then `release_copies`. Logs, never raises."""
        try:
            await asyncio.to_thread(self._recover)
        except Exception as exc:
            _LOGGER.warning("worktree: recovery failed (%s)", type(exc).__name__)
        await self.release_copies()

    def _recover(self) -> None:
        groups: dict[str, list[WorktreeRow]] = {}
        for row in self._store.worktrees_in_states(("creating", "removing")):
            groups.setdefault(row["workspace_id"], []).append(row)
        for workspace_id, group in groups.items():
            try:
                code_root = self._git.repository(workspace_id)
                with self._git.operation(code_root):
                    self._recover_folder(code_root, group)
            except CodeError as exc:  # untrusted, not a repository, or its settings changed
                _LOGGER.warning("worktree: recovery skipped a folder (%s)", exc.code)

    def _recover_folder(self, code_root: Path, group: list[WorktreeRow]) -> None:
        try:
            listed = parse_worktree_list(
                self._git.git(code_root, "worktree", "list", "--porcelain", "-z")
            )
        except Exception as exc:  # ValueError, RuntimeError or TimeoutExpired (TID251)
            _git_failed("worktree list", exc)
            return
        try:
            archived = {
                archive.worktree_id
                for archive in worktree_archive.list_archives(self._git, code_root)
            }
        except worktree_archive.ArchiveError:
            return  # logged there
        for row in group:
            try:
                key = _listing_key(row["path"])
            except OSError as exc:
                _LOGGER.warning("worktree: recovery skipped a row (%s)", type(exc).__name__)
                continue
            self._reconcile(row, listed.get(key), row["worktree_id"] in archived)

    def _reconcile(self, row: WorktreeRow, entry: WorktreeEntry | None, archived: bool) -> None:
        present = Path(row["path"]).exists(follow_symlinks=False)
        try:
            if row["state"] == "creating" and archived:
                # A restore cut short: never `ready`, so no sweep archives a half-restored tree
                # over the archive, and never deleted, so the archive stays listed.
                state = "kept" if entry is not None or present else "removed"
            elif row["state"] == "creating" and entry is None and not present:
                self._store.delete_worktree(row["worktree_id"], expected_state="creating")
                return
            elif row["state"] == "creating" and entry is not None and not entry.locked:
                state = "ready"
            elif entry is not None or present:
                state = "kept"
            else:
                state = "removed"
            self._store.update_worktree_state(row["worktree_id"], row["state"], state, now_iso())
        except StateError as exc:
            _LOGGER.warning("worktree: recovery skipped a row (%s)", exc.code)

    # --- dry run ----------------------------------------------------------------------------

    def cleanup_enabled(self) -> bool:
        return self._enabled()

    async def plan(self, workspace_id: str | None = None) -> list[Decision]:
        """Every `ready` row's cleanup verdict, scoped to one folder's rows when given its id.
        Read-only: no lease, rename, write, or Git call outside an admitted folder. Past
        `PLAN_DEADLINE_S` seconds, every row not yet reached answers `keep unknown` without
        being inspected, so one more row still fits under the caller's own timeout."""
        if workspace_id is not None:
            try:
                await asyncio.to_thread(self._git.repository, workspace_id)
            except CodeError as exc:
                raise WorktreeError(exc.code) from None
        rows = await asyncio.to_thread(self._store.worktrees_in_states, ("ready",))
        ready = [  # newest first
            row
            for row in reversed(rows)
            if workspace_id is None or row["workspace_id"] == workspace_id
        ]
        copies = await self._copy_states(ready)
        folders: dict[str, _Folder] = {}
        decisions: list[Decision] = []
        start = time.monotonic()
        for row in ready:
            if time.monotonic() - start > PLAN_DEADLINE_S:
                decisions.append(Decision(row["worktree_id"], "keep", "unknown", None, None, None))
                continue
            folder = folders.get(row["workspace_id"])
            if folder is None:
                folder = await asyncio.to_thread(self._load_folder, row["workspace_id"])
                folders[row["workspace_id"]] = folder
            decision, _facts = await self._decide_row(row, folder, copies.get(row["worktree_id"]))
            decisions.append(decision)
        return decisions

    async def _copy_states(
        self, rows: list[WorktreeRow]
    ) -> dict[str, Literal["bound", "released"]]:
        """The Bot-copy binding state of each of `rows` bound or pending release; a `former`
        copy's tree, released before and restored since, is the user's to keep or sweep."""
        found = await asyncio.to_thread(
            self._store.copy_states, [row["worktree_id"] for row in rows]
        )
        return {
            worktree_id: state
            for worktree_id, copy in found.items()
            if (state := copy["state"]) != "former"
        }

    def _load_folder(self, workspace_id: str) -> _Folder:
        """This row's folder, admitted fresh from its current registration (a dry run has no
        caller-pinned revision to check against), then one `worktree list` for the whole
        folder. Either failure is reported through the returned `_Folder`, never raised."""
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
            root = self._resolve_root(
                workspace_id,
                workspace.policy_revision,
                workspace.root.physical_id,
                kind="code",
            )
        except WorkspaceError, MutationError:
            return _Folder(trusted=False, listing=None)
        try:
            with self._git.operation(root.path):
                try:
                    listing = parse_worktree_list(
                        self._git.git(root.path, "worktree", "list", "--porcelain", "-z")
                    )
                except Exception as exc:  # ValueError, RuntimeError or TimeoutExpired (TID251)
                    _git_failed("worktree list", exc)
                    listing = None
        except CodeError as exc:  # its settings changed: every row unknown, nothing acted on
            _LOGGER.warning("worktree: skipped a folder (%s)", exc.code)
            return _Folder(trusted=True, listing=None)
        return _Folder(trusted=True, listing=listing, root=root)

    async def _decide_row(
        self,
        row: WorktreeRow,
        folder: _Folder,
        copy: Literal["bound", "released"] | None = None,
    ) -> tuple[Decision, Facts | None]:
        """One row's cleanup verdict: the cheap checks first, `worktree_probe.inspect` in a
        thread only when the decision order still needs it. A gone folder that Git still lists
        on a detached HEAD or on another branch than the row's keeps `branch_changed`: that
        HEAD and its reflog live only in the registration `missing` would remove. `copy` is
        the row's Bot-copy binding state (`worktree_rules.decide`). Shared by `plan` and the
        acting sweep, so it never leases, renames or writes."""
        worktree_id = row["worktree_id"]
        path = Path(row["path"])
        in_area = path.parent == self._area
        live = locked = present = False
        entry: WorktreeEntry | None = None
        facts: Facts | None = None
        if in_area and folder.trusted and folder.listing is not None:
            live = await self._roots.live_in(worktree_id) or self._in_use(worktree_id)
            if not live and copy != "bound":
                resolved, entry = self._listed(folder.listing, path)
                locked = entry is not None and entry.locked
                if not resolved:
                    present = True  # the path did not resolve: stop here, so `decide` reads unknown
                elif not locked:
                    present = path.exists(follow_symlinks=False)
                    if present:
                        try:
                            facts = await asyncio.to_thread(inspect, self._git, path)
                        except CodeError as exc:  # its settings changed: `decide` reads unknown
                            _LOGGER.warning("worktree: skipped a row (%s)", exc.code)
        elif in_area and folder.trusted:
            present = True  # the listing failed: stop here, so `decide` reads it as unknown
        decision = decide(
            worktree_id,
            row["branch"],
            in_area=in_area,
            trusted=folder.trusted,
            live=live,
            locked=locked,
            present=present,
            facts=facts,
            now=self._now(),
            copy=copy,
        )
        if (
            decision.reason == "missing"
            and entry is not None
            and (entry.detached or entry.branch != f"refs/heads/{row['branch']}")
        ):
            decision = replace(decision, action="keep", reason="branch_changed")
        return decision, facts

    def _listed(
        self, listing: dict[str, WorktreeEntry], path: Path
    ) -> tuple[bool, WorktreeEntry | None]:
        """Whether `path` resolved, and Git's own listing entry for it (None while unlisted). A
        path that cannot be resolved, `_decide_row` keeps as unknown (fail closed)."""
        try:
            key = _listing_key(str(path))
        except OSError as exc:
            _LOGGER.warning("worktree: path not resolved (%s)", type(exc).__name__)
            return False, None
        return True, listing.get(key)

    # --- cleanup sweep ----------------------------------------------------------------------

    async def run_sweeps(self, interval_s: float) -> None:
        """Sweep at once, then again every `interval_s` seconds, until cancelled. Each round
        reads the setting first (`sweep`); a failed round is logged by exception type only and
        the next one still runs."""
        while True:
            try:
                await self.sweep()
            except Exception as exc:  # any failure: the loop outlives one bad round
                _LOGGER.warning("worktree: sweep failed (%s)", type(exc).__name__)
            await asyncio.sleep(interval_s)

    async def sweep(self) -> list[Decision]:
        """Cleanup on or off, the folders an earlier round renamed aside and could not delete
        are tried again (a release makes them with cleanup off too), then `release_copies`. With
        cleanup off it answers [] there, before any other Git or disk call. Else every other
        `ready` row's verdict follows, as `plan` gives it, acted on at once for a removal or an
        archive; then each folder a row of any state names drops its expired archives.

        An act takes `remove`'s guards (the spawn block, a live re-check, the `ready` to
        `removing` compare-and-set, the whole-root lease) and goes on only while a fresh look
        still matches the inspection. An archive is saved and checked against a second
        snapshot before the tree is renamed aside, the last live check (Windows refuses the
        rename while a process has its cwd or an open file inside; POSIX asks
        `has_process_inside` first). The renamed folder is deleted after the lease."""
        await asyncio.to_thread(self._purge_trash)
        released = await self.release_copies()
        if not await asyncio.to_thread(self._enabled):
            return []
        rows = await asyncio.to_thread(self._store.worktrees_in_states, _EVERY_STATE)
        copies = await self._copy_states([row for row in rows if row["state"] == "ready"])
        folders: dict[str, _Folder] = {}
        decisions = released
        for row in rows:
            folder = folders.get(row["workspace_id"])
            if folder is None:
                folder = await asyncio.to_thread(self._load_folder, row["workspace_id"])
                folders[row["workspace_id"]] = folder
            copy = copies.get(row["worktree_id"])
            # A released copy is `release_copies`' alone, so it is never acted on twice.
            if row["state"] == "ready" and copy != "released":
                decisions.append(await self._sweep_row(row, folder, copy))
        for folder in folders.values():
            await self._retain(folder)
        return decisions

    async def release_copies(self) -> list[Decision]:
        """Settle every released Bot copy, cleanup on or off: a `ready` one by the sweep's own
        checks and act (`_sweep_row`) without its idle window, so a merged copy is removed and
        any other change or unintegrated commit archived with its branch kept; then finish the
        release of each copy whose row is `removed` (or gone): its binding stays as a `former`
        copy (`SessionStore.finish_release`), so a restore of its archive brings the tree back
        still a Bot copy to Git. Every other copy stays pending for the next round, its reason
        logged as a code. Logs, never raises."""
        try:
            return await self._release_copies()
        except Exception as exc:
            _LOGGER.warning("worktree: release failed (%s)", type(exc).__name__)
            return []

    async def _release_copies(self) -> list[Decision]:
        folders: dict[str, _Folder] = {}
        decisions: list[Decision] = []
        for copy in await asyncio.to_thread(self._store.released_copies):
            worktree_id = copy["worktree_id"]
            row = await asyncio.to_thread(self._store.get_worktree, worktree_id)
            if row is not None and row["state"] == "ready":
                folder = folders.get(row["workspace_id"])
                if folder is None:
                    folder = await asyncio.to_thread(self._load_folder, row["workspace_id"])
                    folders[row["workspace_id"]] = folder
                decision = await self._sweep_row(row, folder, "released")
                decisions.append(decision)
                if decision.action == "keep":
                    _LOGGER.warning("worktree: release kept a copy (%s)", decision.reason)
                row = await asyncio.to_thread(self._store.get_worktree, worktree_id)
            if row is None or row["state"] == "removed":
                await asyncio.to_thread(self._finish_release, worktree_id)
            elif row["state"] != "ready":  # `creating`/`removing` settle at launch; `kept` waits
                _LOGGER.warning("worktree: release waits for a copy (%s)", row["state"])
        return decisions

    def _finish_release(self, worktree_id: str) -> None:
        try:
            self._store.finish_release(worktree_id)
        except StateError as exc:  # finished next round: its row stays `removed`
            _LOGGER.warning("worktree: release not finished (%s)", exc.code)

    def _purge_trash(self) -> None:
        """Delete the `<area>/.trash-<uuid>` folders an earlier sweep renamed aside but could not
        delete; a link, a junction or any other name stays as it is, and so does the folder of
        a removal still under way."""
        try:
            with os.scandir(self._area) as entries:
                found = [
                    Path(entry.path)
                    for entry in entries
                    if _TRASH.fullmatch(entry.name) and _plain_folder(entry)
                ]
        except FileNotFoundError:
            return  # no worktree was ever made
        except OSError as exc:
            _LOGGER.warning("worktree: trash not listed (%s)", type(exc).__name__)
            return
        for trash in found:
            # Another task's act renames its folder back when `git worktree remove` refuses.
            if trash.name.removeprefix(".trash-") not in self._removing:
                _delete_trash(trash)

    async def _sweep_row(
        self,
        row: WorktreeRow,
        folder: _Folder,
        copy: Literal["bound", "released"] | None = None,
    ) -> Decision:
        """One `ready` row: its verdict, acted on at once when it removes or archives."""
        decision, facts = await self._decide_row(row, folder, copy)
        root = folder.root
        if decision.action == "keep" or root is None:
            return decision
        worktree_id = row["worktree_id"]
        if worktree_id in self._removing:
            return replace(decision, action="keep", reason="busy")
        # As in `remove`: blocked on the loop before `live_in`, so a spawn is refused or live.
        self._removing.add(worktree_id)
        self._roots.block(worktree_id)
        trash: Path | None = None
        try:
            if await self._roots.live_in(worktree_id) or self._in_use(worktree_id):
                return replace(decision, action="keep", reason="live")
            try:
                await asyncio.to_thread(
                    self._store.update_worktree_state, worktree_id, "ready", "removing", now_iso()
                )
            except StateError:
                return replace(decision, action="keep", reason="busy")
            entered = False
            try:
                async with self._lease(root):
                    entered = True
                    # A released Bot copy's archive keeps its branch, unintegrated commits and all.
                    keep_branch = copy == "released" and decision.action == "archive"
                    decision, trash = await asyncio.to_thread(
                        self._act, root.path, row, decision, facts, keep_branch
                    )
            except WorktreeError as exc:
                if entered:
                    raise
                await asyncio.to_thread(self._put_back, worktree_id, "ready")
                reason = "busy" if exc.code == "vault_locked" else "unknown"
                return replace(decision, action="keep", reason=reason)
            except BaseException:
                if not entered:
                    await asyncio.to_thread(self._put_back, worktree_id, "ready")
                raise
        finally:
            self._roots.unblock(worktree_id)
            self._removing.discard(worktree_id)
        if trash is not None:
            await asyncio.to_thread(_delete_trash, trash)
        return decision

    def _act(
        self,
        code_root: Path,
        row: WorktreeRow,
        decision: Decision,
        facts: Facts | None,
        keep_branch: bool,
    ) -> tuple[Decision, Path | None]:
        """Under the lease, on a row this sweep moved to `removing`: carry out `decision` only
        while every check still holds, and settle the row. Answers the final verdict and the
        renamed-aside folder to delete once the lease is released. Git settings that no longer
        match the folder's baseline, checked again after the lease wait, or met by the tree's
        own look before anything is renamed, put the row back and keep it `unknown`."""
        try:
            with self._guarded(code_root, fresh=True):
                return self._act_checked(code_root, row, decision, facts, keep_branch)
        except WorktreeError as exc:  # a settings refusal, raised before any rename or removal
            _LOGGER.warning("worktree: sweep skipped a row (%s)", exc.code)
            return self._kept(row, decision, "unknown"), None

    def _act_checked(
        self,
        code_root: Path,
        row: WorktreeRow,
        decision: Decision,
        facts: Facts | None,
        keep_branch: bool,
    ) -> tuple[Decision, Path | None]:
        if decision.reason == "missing":
            return self._remove_missing(code_root, row, decision), None
        tree = Path(row["path"])
        head = facts.head if facts is not None else None
        if (
            facts is None
            or head is None
            or not self._unchanged(tree, row, facts, clean=decision.action == "remove")
        ):
            return self._kept(row, decision, "changed"), None
        if decision.action == "archive":
            try:
                snap = worktree_archive.snapshot(self._git, tree)
                if (snap.head, snap.index_digest) != (head, facts.index_digest):
                    return self._kept(row, decision, "changed"), None
                worktree_archive.save(
                    self._git, code_root, row["worktree_id"], snap, branch=row["branch"]
                )
                if worktree_archive.snapshot(self._git, tree) != snap:
                    return self._kept(row, decision, "changed"), None  # the archive stays
            except worktree_archive.ArchiveError:
                return self._kept(row, decision, "git_refused"), None
        if has_process_inside(tree):
            return self._kept(row, decision, "in_use"), None
        trash = self._area / f".trash-{row['worktree_id']}"
        try:
            with _hooks_off() as hooks:  # made before the rename, so its failure renames nothing
                try:
                    os.rename(tree, trash)
                except OSError:
                    return self._kept(row, decision, "in_use"), None
                try:
                    self._git.git(
                        code_root, *hooks, "worktree", "remove", row["path"],
                        timeout=REMOVE_TIMEOUT_S,
                    )  # fmt: skip
                except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
                    _git_failed("worktree remove", exc)
                    try:
                        os.rename(trash, tree)
                    except OSError as back:
                        _LOGGER.warning("worktree: rename back failed (%s)", type(back).__name__)
                        return self._kept(row, decision, "git_refused", "kept"), None
                    return self._kept(row, decision, "git_refused"), None
        except WorktreeError as exc:  # `_hooks_off` made no folder
            _git_failed("worktree remove", exc)
            return self._kept(row, decision, "git_refused"), None
        if not keep_branch:
            with suppress(worktree_archive.ArchiveError):  # logged there; the branch stays
                worktree_archive.delete_branch(self._git, code_root, row["branch"], head)
        self._removed(row["worktree_id"])
        return decision, trash

    def _remove_missing(self, code_root: Path, row: WorktreeRow, decision: Decision) -> Decision:
        """A row whose folder is gone: unregister it while Git still lists it; the branch stays."""
        if Path(row["path"]).exists(follow_symlinks=False):
            return self._kept(row, decision, "changed")
        if self._still_listed(code_root, row):
            try:
                with _hooks_off() as hooks:
                    self._git.git(
                        code_root, *hooks, "worktree", "remove", row["path"],
                        timeout=REMOVE_TIMEOUT_S,
                    )  # fmt: skip
            except Exception as exc:  # RuntimeError, TimeoutExpired (TID251) or `_hooks_off`'s
                _git_failed("worktree remove", exc)
                return self._kept(row, decision, "git_refused")
        self._removed(row["worktree_id"])
        return decision

    def _unchanged(self, tree: Path, row: WorktreeRow, facts: Facts, *, clean: bool) -> bool:
        """Whether a fresh look still matches the inspection: the inspected HEAD on the row's
        branch, no status path (none at all when `clean`) and none of the per-worktree `HEAD`,
        `index` and `logs/HEAD` newer than the inspected activity."""
        if facts.activity is None:
            return False
        try:
            headers, records = status_records(
                self._git.output(
                    tree, *worktree_archive.LONG_PATHS, "status", "--porcelain=v2", "-z",
                    "--branch", "--untracked-files=all",
                )
            )  # fmt: skip
            found = self._git.output(
                tree, "rev-parse", "--git-path", "HEAD", "--git-path", "index",
                "--git-path", "logs/HEAD",
            ).splitlines()  # fmt: skip
        except CodeError as exc:
            if exc.code == "git_settings_changed":
                raise  # `_act` keeps the row `unknown` and logs the code
            return False
        except ValueError:
            return False
        if (
            headers.get("branch.oid") != facts.head
            or headers.get("branch.head") != row["branch"]
            or (clean and records)
            or len(found) != 3
        ):
            return False
        for path in [*(tree / record.path for record in records), *(tree / name for name in found)]:
            try:
                moment = datetime.fromtimestamp(path.lstat().st_mtime, tz=UTC)
            except FileNotFoundError:
                continue  # a deleted file, or no reflog
            except OSError:
                return False
            if moment > facts.activity:
                return False
        return True

    def _kept(
        self, row: WorktreeRow, decision: Decision, reason: str, state: str = "ready"
    ) -> Decision:
        """Put a row this sweep moved to `removing` back to `state`, and keep it for `reason`."""
        self._put_back(row["worktree_id"], state)
        return replace(decision, action="keep", reason=reason)

    def _removed(self, worktree_id: str) -> None:
        try:
            self._store.update_worktree_state(worktree_id, "removing", "removed", now_iso())
        except StateError as exc:
            # The launch's recovery settles a row left in `removing`.
            _LOGGER.warning("worktree: sweep not recorded (%s)", exc.code)

    async def _retain(self, folder: _Folder) -> None:
        """Under the folder's lease, drop each archive older than `ARCHIVE_DAYS` whose row is
        `removed` or absent; a refused lease leaves them for the next sweep."""
        if folder.root is None:
            return
        try:
            async with self._lease(folder.root):
                await asyncio.to_thread(self._drop_expired, folder.root.path)
        except WorktreeError as exc:
            _LOGGER.warning("worktree: retention skipped a folder (%s)", exc.code)

    def _drop_expired(self, code_root: Path) -> None:
        # Checked again after the lease wait; a refusal is `_retain`'s to log.
        with self._guarded(code_root, fresh=True):
            self._drop_expired_checked(code_root)

    def _drop_expired_checked(self, code_root: Path) -> None:
        cutoff = self._now() - timedelta(days=ARCHIVE_DAYS)
        try:
            archives = worktree_archive.list_archives(self._git, code_root)
        except worktree_archive.ArchiveError:
            return  # logged there
        for archive in archives:
            if datetime.fromisoformat(archive.archived_at) >= cutoff:
                continue
            try:
                row = self._store.get_worktree(archive.worktree_id)
            except StateError:
                continue
            if row is not None and row["state"] != "removed":
                continue
            with suppress(worktree_archive.ArchiveError):  # logged there; tried again next sweep
                worktree_archive.drop(
                    self._git, code_root, worktree_archive.PREFIX + archive.worktree_id, archive.oid
                )

    # --- restore ----------------------------------------------------------------------------

    def archives(self, workspace_id: str) -> list[worktree_archive.Archive]:
        """The archives a restore can bring back in this trusted folder: those whose row is this
        folder's and `removed`, newest first, at most `MAX_ARCHIVES`."""
        try:
            code_root = self._git.repository(workspace_id)
        except CodeError as exc:
            raise WorktreeError(exc.code) from None
        with self._guarded(code_root):
            try:
                found = worktree_archive.list_archives(self._git, code_root)
            except worktree_archive.ArchiveError:
                raise WorktreeError("git_failed") from None  # logged there
        listed: list[worktree_archive.Archive] = []
        for archive in sorted(found, key=lambda each: each.archived_at, reverse=True):
            row = self._store.get_worktree(archive.worktree_id)
            if row is None or row["workspace_id"] != workspace_id or row["state"] != "removed":
                continue
            listed.append(archive)
            if len(listed) == MAX_ARCHIVES:
                break
        return listed

    async def restore(
        self, worktree_id: str, policy_revision: int, root_physical_id: str
    ) -> Worktree:
        """Bring a `removed` row's archived tree back at its path, on its branch, with the
        staged, unstaged and untracked changes the archive holds, as a checkout writes them;
        then drop the archive ref. Refuses `no_archive` without an archive of
        `worktree_archive.save`'s shape and `worktree_limit` with no slot free; under the
        lease, `worktree_busy`, `branch_in_use` and `branch_moved` (`_restore_args`) put the
        row back to `removed`."""
        row, root, oid, head = await asyncio.to_thread(
            self._restore_target, worktree_id, policy_revision, root_physical_id
        )
        try:
            await asyncio.to_thread(
                self._store.revive_worktree, worktree_id, now_iso(), limit=MAX_WORKTREES
            )
        except StateError as exc:
            raise WorktreeError(_REVIVE_REFUSED.get(exc.code, "unavailable")) from None
        entered = False
        try:
            async with self._lease(root):
                entered = True
                return await asyncio.to_thread(self._settle_restore, root.path, row, oid, head)
        except BaseException:
            if not entered:
                await asyncio.to_thread(self._settle_revived, worktree_id, "removed")
            raise

    def _restore_target(
        self, worktree_id: str, policy_revision: int, root_physical_id: str
    ) -> tuple[WorktreeRow, OrdinaryMutationRoot, str, str]:
        """The checks before the lease: `_removal_target`'s admission of a `removed` row; its
        archive, resolved once to a commit so the ref cannot move between the checks, with
        the HEAD it was taken from (its first parent); and the private area, which must hold
        the row's path (a row recorded before the data folder moved refuses `unavailable`)."""
        row, root = self._removal_target(
            worktree_id, policy_revision, root_physical_id, states=("removed",)
        )
        with self._guarded(root.path):
            try:
                archive = next(
                    (
                        found
                        for found in worktree_archive.list_archives(self._git, root.path)
                        if found.worktree_id == worktree_id
                    ),
                    None,
                )
                shape = (
                    None
                    if archive is None
                    else worktree_archive.parents(self._git, root.path, archive.oid)
                )
            except worktree_archive.ArchiveError:
                raise WorktreeError("git_failed") from None
        if archive is None or shape is None:
            raise WorktreeError("no_archive")
        if Path(row["path"]).parent != self._area:
            raise WorktreeError("unavailable")
        try:
            create_private_directory(self._area)
            require_private_directory(self._area)
        except OSError, ProtectionError:
            raise WorktreeError("unavailable") from None
        return row, root, archive.oid, shape[0]

    def _settle_restore(self, code_root: Path, row: WorktreeRow, oid: str, head: str) -> Worktree:
        """Under the lease, on a row this restore moved to `creating`: add the tree and check
        the archive `oid` out into it. A refusal, or an add that left no folder, puts the row
        back to `removed`; any failure from then on keeps the tree as `kept`, never `ready`,
        so no sweep archives a half-restored tree over the archive."""
        entered = False
        try:
            with self._guarded(code_root, fresh=True):
                entered = True
                return self._restore_tree(code_root, row, oid, head)
        except WorktreeError:
            if not entered:  # changed settings: refused before any change
                self._settle_revived(row["worktree_id"], "removed")
            raise

    def _restore_tree(self, code_root: Path, row: WorktreeRow, oid: str, head: str) -> Worktree:
        worktree_id = row["worktree_id"]
        try:
            args = self._restore_args(code_root, row, head)
        except WorktreeError:
            self._settle_revived(worktree_id, "removed")
            raise
        try:
            with _hooks_off() as hooks:
                self._git.git(
                    code_root, *hooks, "worktree", "add", "--no-checkout", *args,
                    timeout=ADD_TIMEOUT_S,
                )  # fmt: skip
        except WorktreeError:  # `_hooks_off` made no folder: refused before the add
            self._settle_revived(worktree_id, "removed")
            raise
        except Exception as exc:  # RuntimeError, TimeoutExpired (TID251) or OSError
            left = os.path.lexists(row["path"])
            self._settle_revived(worktree_id, "kept" if left else "removed")
            raise _git_failed("worktree add", exc) from None
        try:
            with self._guarded(Path(row["path"])):  # the new tree's own check, before its catch
                try:
                    worktree_archive.restore_files(self._git, Path(row["path"]), oid)
                except Exception as exc:  # ArchiveError, or whatever else stopped it part way
                    _LOGGER.warning(
                        "worktree: restore kept a partial tree (%s)", type(exc).__name__
                    )
                    raise WorktreeError("git_failed") from None
        except WorktreeError:
            self._settle_revived(worktree_id, "kept")
            raise
        stamp = now_iso()
        try:
            self._store.update_worktree_state(worktree_id, "creating", "ready", stamp)
        except StateError:
            # The archive stays, so the launch's recovery settles the row as `kept`.
            raise WorktreeError("unavailable") from None
        try:
            worktree_archive.drop(self._git, code_root, worktree_archive.PREFIX + worktree_id, oid)
        except Exception as exc:  # ArchiveError (logged there too) or OSError: the ref stays
            _LOGGER.warning("worktree: archive not dropped (%s)", type(exc).__name__)
        return Worktree.from_row({**row, "state": "ready", "updated_at": stamp})

    def _restore_args(self, code_root: Path, row: WorktreeRow, head: str) -> tuple[str, ...]:
        """`worktree add --no-checkout`'s arguments for the row's tree: its branch made again at
        `head` when it is gone, else the branch itself while it names `head` and no worktree,
        the main folder included, has it checked out. Refuses `worktree_busy` while anything
        sits at the path, `branch_in_use` while a worktree has the branch checked out, else
        `branch_moved` when it names another commit."""
        path, branch = row["path"], row["branch"]
        if os.path.lexists(path):
            raise WorktreeError("worktree_busy")
        try:
            raw = self._git.output(
                code_root,
                "for-each-ref",
                "--format=%(objectname)%00%(worktreepath)",
                f"refs/heads/{branch}",
            )
        except CodeError as exc:
            raise WorktreeError(exc.code) from None
        if not raw:
            return ("-b", branch, path, head)
        current, _, holder = raw.removesuffix("\n").partition("\0")
        if not _OID.fullmatch(current) or "\n" in holder:
            raise WorktreeError("git_failed")
        if current == head and not holder:
            return (path, branch)
        raise WorktreeError("branch_in_use" if holder else "branch_moved")

    def _settle_revived(self, worktree_id: str, state: str) -> None:
        """Move a row this restore set to `creating` on to `state`; a failure is logged and
        the launch's recovery settles the row."""
        try:
            self._store.update_worktree_state(worktree_id, "creating", state, now_iso())
        except StateError as exc:
            _LOGGER.warning("worktree: restore not recorded (%s)", exc.code)
