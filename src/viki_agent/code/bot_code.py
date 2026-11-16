"""Bot code copies: making a copy on a first send, the spawn recheck of a bound copy, and its
description for the lists.

A Bot's main chat or task thread works in its own copy, and the granted Bots of a Bot thread
share one copy per code folder (`bot_code_copies`). `resolve` is the spawn hook. A granted
Bot's main chat or task thread prepared interactively, with no task attempt, gets its copy on
its first spawn only (before Claude Code ever started it, and while it has no copy row of any
state); a granted Bot of a Bot thread gets the thread's copy of its folder, made on the first
turn that needs it. The copy is a worktree made with the repository's hooks off from the
folder's HEAD on `viki/bot-<yyyymmdd>-<hex6>`, its binding reserved with its `creating` row in
one transaction, so the tree is a Bot copy to Git from the start. The folder's first copy
records the Git-settings baseline every later Git action is checked against. One creation runs
per chat or per Bot thread and folder; a spawn that asks meanwhile waits for it, and a
cancelled spawn leaves it running to the end. Then, before every spawn, resume included, it
rechecks the facts that copy was bound under and refuses the spawn rather than pass another
path. `describe` names copies for the worktree lists by stored Bot, chat and thread names.
Errors and logs carry fixed text and codes, never a path, ref, branch or id.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import stat
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from viki_agent.code import roots_api
from viki_agent.code.code_api import GIT_SETTINGS_CHANGED
from viki_agent.code.code_files import CodeError
from viki_agent.code.session_roots import LiveActor, SessionRoots
from viki_agent.code.worktrees import MAX_WORKTREES, WorktreeError, WorktreeService
from viki_agent.domain import Workspace
from viki_agent.drivers.base import StartError
from viki_agent.events import CodeCopy
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.messaging.bots import BotError
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.protection import (
    ProtectionError,
    create_private_directory,
    require_private_directory,
)
from viki_agent.session import now_iso
from viki_agent.storage import BotCodeCopyRow, SessionStore, StateError, WorktreeRow
from viki_agent.vaults.vault_mutation import MutationError, OrdinaryMutationRoot
from viki_agent.workspaces import WorkspaceError, WorkspaceService

_LOGGER = logging.getLogger(__name__)

MSG_UNGRANTED = (
    "This chat works in a copy of a code folder that is no longer this Bot's code folder, so it "
    "can't start. Give the folder back in Bot settings, or start a new conversation."
)
MSG_UNTRUSTED = (
    "This chat's code folder is no longer trusted or was removed from VIKI Agent, so it can't "
    "start. Trust it again in Settings, or start a new conversation."
)
MSG_MISSING = (
    "This chat's code folder isn't where it was registered, so it can't start. "
    "Put the folder back, then send again."
)
MSG_UNAVAILABLE = (
    "This chat's code copy was removed or isn't ready, so it can't start. Start a new conversation."
)
MSG_THREAD_BUSY = (
    "Another Bot in this Bot thread is using its code copy now, so this turn can't start until "
    "that one finishes."
)
MSG_FOLDER_BUSY = (
    "VIKI Agent is busy with this code folder for another task, so this chat's code copy can't "
    "be made yet. Send again when it finishes."
)
MSG_FAILED = (
    "VIKI Agent couldn't make this chat's code copy, so it can't start. The Code view's "
    "Worktrees tab shows anything left behind."
)
MSG_LIMIT = (
    "VIKI Agent keeps at most 32 worktrees, so this chat's code copy can't be made. Remove one "
    "in the Code view's Worktrees tab."
)
# The folder's first copy needs a baseline of Git settings the guard refuses to read (a link or
# a subfolder in a hooks folder, a path outside the folder). Nothing is left behind, so
# `MSG_FAILED` would point at an empty Worktrees tab: the roots routes' sentence for the code.
MSG_SETTINGS_UNREADABLE = roots_api._MESSAGES["git_settings_unavailable"]
_MESSAGES = {
    "ungranted": MSG_UNGRANTED,
    "untrusted": MSG_UNTRUSTED,
    "missing": MSG_MISSING,
    "unavailable": MSG_UNAVAILABLE,
    "thread_busy": MSG_THREAD_BUSY,
    "folder_busy": MSG_FOLDER_BUSY,
    "failed": MSG_FAILED,
    "limit": MSG_LIMIT,
    "git_settings_changed": GIT_SETTINGS_CHANGED,
    "git_settings_unavailable": MSG_SETTINGS_UNREADABLE,
}
# A creation's refusal code (Git, admission, lease, store or worktree) under the fixed sentence
# it shows; every other code is `failed`.
_NOT_MADE = {
    "unknown_workspace": "untrusted",
    "trust_required": "untrusted",
    "ordinary_workspace_required": "untrusted",
    "stale_root": "missing",
    "root_unavailable": "missing",
    "vault_locked": "folder_busy",
    "worktree_limit": "limit",
    "capacity": "limit",
    "git_settings_changed": "git_settings_changed",
    "git_settings_unavailable": "git_settings_unavailable",
}


class BotCopyDescription(TypedDict):
    kind: Literal["session", "task", "thread"]
    bot_name: str | None
    title: str | None
    released: bool


@dataclass(frozen=True)
class _Bound:
    copy: CodeCopy
    worktree_id: str
    others: tuple[str, ...]  # the other sessions of a shared Bot-thread copy


@dataclass(frozen=True)
class _Make:
    """A copy to make: of `folder_id` for `bot_id`, bound to a chat session or a Bot thread."""

    bot_id: str
    folder_id: str
    session_id: str | None
    thread_id: str | None


def _refused(code: str) -> StartError:
    _LOGGER.warning("bot code copy refused: %s", code)
    return StartError("bad_request", _MESSAGES[code])


def _not_made(code: str) -> StartError:
    _LOGGER.warning("bot code copy not made: %s", code)
    return _refused(_NOT_MADE.get(code, "failed"))


def _plain_directory(path: Path) -> bool:
    """A real folder: not a link, a junction or any other reparse point."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class BotCode:
    def __init__(
        self,
        store: SessionStore,
        workspaces: WorkspaceService,
        roots: SessionRoots,
        profiles: ProfileService,
        threads: BotThreadService,
        *,
        actor: Callable[[str], LiveActor | None],
        area: Path,
        worktrees: WorktreeService | None = None,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._roots = roots
        self._profiles = profiles
        self._threads = threads
        self._actor = actor  # the manager's actor of a session, asked on the loop
        self._area = area  # `worktrees.worktree_area`, where every copy is a direct child
        # Its CodeGit checks the folder's Git-settings baseline; without one, no copy is made.
        self._worktrees = worktrees
        # The creation running for a chat session or a Bot thread and folder, by key; touched
        # only on the loop, so two spawns of one chat or thread never make two copies.
        self._making: dict[str, asyncio.Future[None]] = {}

    # --- the spawn hook (session.BotCodeResolver) -------------------------------------------

    async def resolve(self, session_id: str, settings_file: str) -> CodeCopy | None:
        """The copy this spawn adds, made first when this is its first send; None for a session
        with no copy."""
        target = await asyncio.to_thread(self._target, session_id, settings_file)
        if target is not None:
            key, make = target
            making = self._making.get(key)
            if making is None and make is not None:
                making = self._making[key] = asyncio.ensure_future(self._make(make))
                making.add_done_callback(lambda done: self._made(key, done))
            if making is not None:
                # Shielded: a cancelled spawn leaves the creation to finish, never half made.
                await asyncio.shield(making)
        bound = await asyncio.to_thread(self._check, session_id, settings_file)
        if bound is None:
            return None
        # Last, on the loop, with no await before the return: the actor goes live synchronously
        # after this returns, so a removal that blocks and then asks who is live, or another
        # Bot of the thread starting in the same copy, either refuses here or sees this one live.
        if self._roots.blocked(bound.worktree_id):
            raise _refused("unavailable")
        if any((actor := self._actor(other)) is not None and actor.live for other in bound.others):
            raise _refused("thread_busy")
        return bound.copy

    def _check(self, session_id: str, settings_file: str) -> _Bound | None:
        with self._store._connection() as connection:
            owner = connection.execute(
                "SELECT p.profile_id,p.workspace_id,m.bot_id,b.profile_id,b.workspace_id,"
                "EXISTS(SELECT 1 FROM task_attempts t WHERE t.session_id=p.session_id),p.policy "
                "FROM session_profile_bindings p "
                "LEFT JOIN session_bot_memory m ON m.session_id=p.session_id "
                "LEFT JOIN bots b ON b.bot_id=m.bot_id WHERE p.session_id=?",
                (session_id,),
            ).fetchone()
            threads = [
                row[0]
                for row in connection.execute(
                    "SELECT thread_id FROM bot_thread_sessions WHERE session_id=?", (session_id,)
                )
            ]
            released = connection.execute(
                "SELECT workspace_id FROM bot_code_copies "
                "WHERE session_id=? AND state IN ('released','former') LIMIT 1",
                (session_id,),
            ).fetchone()
        if owner is not None and owner[6] != "ordinary":
            # A Bot session prepared in a read-only or protected vault reaches this hook too, and
            # a copy is an ordinary-vault feature: such a session has none and is not refused.
            return None
        bot_id = owner[2] if owner is not None and owner[3] is not None else None
        grant = self._store.bot_code_folder(bot_id) if bot_id is not None else None
        copies: list[BotCodeCopyRow] = []
        if (own := self._store.bound_copy(session_id)) is not None:
            copies.append(own)
        if grant is not None:
            # A granted Bot shares its thread's copy of its own folder; an ungranted one has none.
            for thread_id in threads:
                if shared := self._store.bound_thread_copy(thread_id, grant["workspace_id"]):
                    copies.append(shared)
        if not copies:
            if released is not None:
                # Its copy's owner was deleted or its folder unregistered, its release pending or
                # finished: never the vault instead.
                self._folder(released[0])
                raise _refused("unavailable")
            return None
        if len(copies) > 1:
            raise _refused("unavailable")
        copy = copies[0]
        if not settings_file or not Path(settings_file).is_file():
            raise _refused("unavailable")
        # Durable provenance only: retained routine evidence outranks any interactive marker.
        origin = self._store.profile_launch_origin(session_id)
        if owner is None or origin != "interactive" or owner[5]:
            raise _refused("unavailable")
        if (
            bot_id is None
            or (copy["thread_id"] is None and copy["bot_id"] != bot_id)
            or (owner[3], owner[4]) != (owner[0], owner[1])
        ):
            raise _refused("unavailable")
        try:
            vault = self._workspaces.get(owner[4])
        except WorkspaceError:
            raise _refused("unavailable") from None
        if vault.policy != "ordinary" or not vault.persistent:
            raise _refused("unavailable")
        if grant is None or grant["workspace_id"] != copy["workspace_id"]:
            raise _refused("ungranted")
        folder = self._folder(copy["workspace_id"])
        worktree_id = copy["worktree_id"]
        if not _canonical_uuid(worktree_id):
            raise _refused("unavailable")
        path = self._area / worktree_id
        worktree = self._store.get_worktree(worktree_id)
        if (
            worktree is None
            or worktree["state"] != "ready"
            or worktree["workspace_id"] != copy["workspace_id"]
            or worktree["path"] != str(path)
            or not _plain_directory(path)
        ):
            raise _refused("unavailable")
        try:
            require_private_directory(self._area)
        except OSError, ProtectionError:
            raise _refused("unavailable") from None
        others: tuple[str, ...] = ()
        if copy["thread_id"] is not None:
            others = tuple(
                other
                for other in self._store.sessions_using_worktree(worktree_id)
                if other != session_id
            )
        return _Bound(
            CodeCopy(path=str(path), folder=folder.name, branch=worktree["branch"]),
            worktree_id,
            others,
        )

    def _folder(self, workspace_id: str) -> Workspace:
        """The trusted, persistent code folder still at its registered identity."""
        try:
            folder = self._workspaces.get(workspace_id, kind="code")
        except WorkspaceError:
            raise _refused("untrusted") from None
        if folder.policy != "ordinary" or not folder.persistent:
            raise _refused("untrusted")
        if self._workspaces.current_root(folder) != folder.root:
            raise _refused("missing")
        return folder

    # --- making a copy on a first send -------------------------------------------------------

    def _target(self, session_id: str, settings_file: str) -> tuple[str, _Make | None] | None:
        """The key a granted Bot session's copy is made under (its chat, or its Bot thread and
        folder), and the copy to make when this spawn is the one that makes it. Only durable
        interactive provenance with no task attempt qualifies; a chat's own copy also needs a
        session Claude Code never started and no copy row of any state, so a chat whose copy
        was released, or that started without one, never gets one."""
        with self._store._connection() as connection:
            owner = connection.execute(
                "SELECT m.bot_id,p.workspace_id,p.launch_origin,p.native_started,"
                "EXISTS(SELECT 1 FROM task_attempts t WHERE t.session_id=p.session_id) "
                "FROM session_profile_bindings p "
                "JOIN session_bot_memory m ON m.session_id=p.session_id "
                "JOIN bots b ON b.bot_id=m.bot_id AND b.profile_id=p.profile_id "
                "AND b.workspace_id=p.workspace_id WHERE p.session_id=?",
                (session_id,),
            ).fetchone()
            threads = [
                row[0]
                for row in connection.execute(
                    "SELECT thread_id FROM bot_thread_sessions WHERE session_id=?", (session_id,)
                )
            ]
            copies = connection.execute(
                "SELECT c.state,w.state FROM bot_code_copies c "
                "LEFT JOIN worktrees w ON w.worktree_id=c.worktree_id WHERE c.session_id=?",
                (session_id,),
            ).fetchall()
        if owner is None or len(threads) > 1:
            return None
        bot_id, vault_id, origin, started, attempted = owner
        grant = self._store.bot_code_folder(bot_id)
        if grant is None:
            return None
        folder_id = grant["workspace_id"]
        if threads:
            key = f"{threads[0]}/{folder_id}"
            wanted = self._store.bound_thread_copy(threads[0], folder_id) is None
        else:
            key = session_id
            wanted = not started and not copies
        if origin != "interactive" or attempted:
            return key, None
        if not threads and not started and copies == [("bound", "kept")]:
            raise _refused("failed")  # its first send's add failed and left a tree behind
        if not wanted:
            return key, None
        try:
            vault = self._workspaces.get(vault_id)
        except WorkspaceError:
            return key, None
        if vault.policy != "ordinary" or not vault.persistent:
            return key, None
        if not settings_file or not Path(settings_file).is_file():
            raise _refused("unavailable")
        self._folder(folder_id)  # an untrusted or moved folder refuses: no start without it
        thread_id = threads[0] if threads else None
        return key, _Make(bot_id, folder_id, None if thread_id else session_id, thread_id)

    def _made(self, key: str, done: asyncio.Future[None]) -> None:
        if self._making.get(key) is done:
            del self._making[key]
        if not done.cancelled():
            done.exception()  # retrieved here: the spawn that asked for it may be gone

    async def _make(self, make: _Make) -> None:
        """Reserve, then add the worktree with hooks off under the folder's whole-root lease. A
        refusal before the add undoes the reservation; a failed add that left a folder keeps
        its `kept` row bound, so the chat refuses rather than make a second copy."""
        worktrees = self._worktrees
        if worktrees is None:
            raise _not_made("no_worktrees")  # refused, never started without its copy
        reserved = await asyncio.to_thread(self._reserve, make, worktrees)
        if reserved is None:
            return  # another spawn bound this chat or thread first, or the grant went
        root, row = reserved
        adding = False
        try:
            async with worktrees._lease(root):
                await worktrees._require_new_branch(root.path, row["branch"])
                adding = True
                await worktrees._add(root.path, row, hooks=False)
        except BaseException as exc:
            await asyncio.to_thread(self._unreserve, row["worktree_id"], uncreated=not adding)
            if isinstance(exc, WorktreeError):
                raise _not_made(exc.code) from None
            raise

    def _reserve(
        self, make: _Make, worktrees: WorktreeService
    ) -> tuple[OrdinaryMutationRoot, WorktreeRow] | None:
        """The checks before the lease, the folder's first baseline, then the `creating` row
        reserved with its binding in one shared-cap transaction. None when the grant changed
        meanwhile or another spawn's binding won."""
        folder = self._folder(make.folder_id)
        grant = self._store.bot_code_folder(make.bot_id)
        if grant is None or grant["workspace_id"] != folder.workspace_id:
            return None
        git = worktrees._git
        try:
            root = worktrees._resolve_root(
                folder.workspace_id, folder.policy_revision, folder.root.physical_id, kind="code"
            )
            with git.scope():
                # Read before the check below: a baseline another spawn saves after that check
                # was never compared with these settings, so it must count as new here too.
                first = self._store.bot_code_baseline(folder.workspace_id) is None
                git.repository(folder.workspace_id)  # checks an existing baseline
                if first:
                    # The first copy's: kept by every later copy, and by none of them removed.
                    self._store.insert_bot_code_baseline(
                        {
                            "workspace_id": folder.workspace_id,
                            "git_fingerprint": git.settings_fingerprint(root.path),
                            "created_at": now_iso(),
                        }
                    )
                # A baseline taken just now, here or by another spawn, is the one that won:
                # checked here. One that `repository` already checked is not read again.
                with git.operation(root.path, fresh=first):
                    base_oid = git.head(root.path)
        except (CodeError, MutationError, StateError) as exc:
            raise _not_made(exc.code) from None
        try:
            create_private_directory(self._area)
            require_private_directory(self._area)
        except OSError, ProtectionError:
            raise _not_made("unavailable") from None
        worktree_id, stamp = str(uuid.uuid4()), now_iso()
        row: WorktreeRow = {
            "worktree_id": worktree_id,
            "workspace_id": folder.workspace_id,
            "path": str(self._area / worktree_id),
            "branch": f"viki/bot-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}",
            "base_oid": base_oid,
            "state": "creating",
            "created_at": stamp,
            "updated_at": stamp,
        }
        binding: BotCodeCopyRow = {
            "worktree_id": worktree_id,
            "workspace_id": folder.workspace_id,
            "bot_id": make.bot_id,
            "session_id": make.session_id,
            "thread_id": make.thread_id,
            "state": "bound",
            "created_at": stamp,
        }
        try:
            self._store.insert_bot_copy(row, binding, limit=MAX_WORKTREES)
        except StateError as exc:
            if exc.code == "conflict":
                return None
            raise _not_made(exc.code) from None
        return root, row

    def _unreserve(self, worktree_id: str, *, uncreated: bool) -> None:
        """Undo what a refused creation reserved: its `creating` row when the add never ran,
        then its binding once no worktree row is left. A binding whose row remains stays, so
        no tree that was a Bot copy to Git ever stops being one."""
        try:
            with self._store._connection(write=True) as connection:
                if uncreated:
                    connection.execute(
                        "DELETE FROM worktrees WHERE worktree_id=? AND state='creating'",
                        (worktree_id,),
                    )
                connection.execute(
                    "DELETE FROM bot_code_copies WHERE worktree_id=? "
                    "AND NOT EXISTS(SELECT 1 FROM worktrees WHERE worktree_id=?)",
                    (worktree_id, worktree_id),
                )
        except StateError as exc:
            _LOGGER.warning("bot code copy reservation not undone: %s", exc.code)

    # --- the worktree lists' description (roots_api describe) -------------------------------

    def describe(self, worktree_ids: Sequence[str]) -> dict[str, BotCopyDescription]:
        """One batch: each Bot copy among `worktree_ids` by its stored names. A label the store
        no longer has is None, and a session copy whose Bot is gone, so neither main chat nor
        task thread is known, is left out rather than guessed, as is a `former` copy, whose
        release finished: it is no longer its Bot's."""
        copies = {
            worktree_id: copy
            for worktree_id, copy in self._store.copy_states(list(worktree_ids)).items()
            if copy["state"] != "former"
        }
        if not copies:
            return {}
        bot_ids = sorted({copy["bot_id"] for copy in copies.values()})
        thread_ids = sorted({c["thread_id"] for c in copies.values() if c["thread_id"]})
        with self._store._connection() as connection:
            bots = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT bot_id,profile_id,workspace_id,chat_session_id FROM bots "
                    f"WHERE bot_id IN ({','.join('?' * len(bot_ids))})",
                    bot_ids,
                )
            }
            thread_vaults = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT thread_id,workspace_id FROM bot_threads "
                    f"WHERE thread_id IN ({','.join('?' * len(thread_ids))})",
                    thread_ids,
                )
            }
        names = {bot_id: self._bot_name(profile_id) for bot_id, (profile_id, _, _) in bots.items()}
        titles = self._thread_titles(thread_vaults)
        described: dict[str, BotCopyDescription] = {}
        for worktree_id, copy in copies.items():
            released = copy["state"] == "released"
            name = names.get(copy["bot_id"])
            if copy["thread_id"] is not None:
                described[worktree_id] = BotCopyDescription(
                    kind="thread",
                    bot_name=name,
                    title=titles.get(copy["thread_id"]),
                    released=released,
                )
            elif (bot := bots.get(copy["bot_id"])) is not None and copy["session_id"] is not None:
                described[worktree_id] = BotCopyDescription(
                    kind="session" if bot[2] == copy["session_id"] else "task",
                    bot_name=name,
                    title=self._chat_title(bot[1], copy["session_id"]),
                    released=released,
                )
        return described

    def _bot_name(self, profile_id: str) -> str | None:
        try:
            name = self._profiles.get(profile_id)["document"]["identity"]["name"]
        except ProfileError:
            return None
        return name or None

    def _chat_title(self, vault_id: str, session_id: str) -> str | None:
        try:
            vault = self._workspaces.get(vault_id)
        except WorkspaceError:
            return None
        scope = hashlib.sha256(vault.root.key.encode("utf-8")).hexdigest()
        metadata = self._store.get_session_metadata(scope, session_id)
        return (metadata["title"] or None) if metadata is not None else None

    def _thread_titles(self, thread_vaults: dict[str, str]) -> dict[str, str | None]:
        titles: dict[str, str | None] = {}
        for vault_id in sorted(set(thread_vaults.values())):
            try:
                listed = self._threads.list(vault_id)["threads"]
            except BotError:
                continue  # an unreadable or unregistered vault's titles stay unknown
            for thread in listed:
                if thread_vaults.get(thread["thread_id"]) == vault_id:
                    titles[thread["thread_id"]] = thread["state"]["title"] or None
        return titles
