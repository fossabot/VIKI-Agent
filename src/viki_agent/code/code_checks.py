"""User-written checks: their definitions, sealed at rest, and the root gate every check run
and terminal passes.

A check's name and command are only ever text the user typed into VIKI's form. They are stored
sealed under a per-row context, so a body opens only under its own check id. `trusted_root` is
the shared gate: it reads the workspace and worktree rows before any filesystem call, and makes
no Git call at all.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from viki_agent import protection
from viki_agent.code.session_roots import _trusted
from viki_agent.session import now_iso
from viki_agent.storage import CheckRow, CheckRunRow, SessionStore, StateError
from viki_agent.workspaces import WorkspaceError, WorkspaceService, chain_has_reparse

CHECKS_SUPPORTED = sys.platform in {"win32", "darwin", "linux"}  # read at call time
MAX_CHECKS = 32
MAX_NAME_CHARS = 100
MAX_COMMAND_CHARS = 8000
MIN_TIMEOUT_S = 60
MAX_TIMEOUT_S = 3600
MAX_BODY_BYTES = 65536


class CodeRunError(Exception):
    """A refused check or terminal request; `code` is one the routes map to a status."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate
        return False
    return True


def valid_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_NAME_CHARS
        and not value.isspace()
        and not any(ch < "\x20" or "\x7f" <= ch <= "\x9f" for ch in value)
        and _utf8(value)
    )


def valid_command(value: object) -> bool:
    # One shell command line: no C0 control but TAB (so no CR, LF or NUL).
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_COMMAND_CHARS
        and not value.isspace()
        and not any(ch < "\x20" and ch != "\t" for ch in value)
        and _utf8(value)
    )


def valid_timeout(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S
    )


def trusted_root(
    store: SessionStore, workspaces: WorkspaceService, workspace_id: str, worktree_id: str | None
) -> Path:
    """The trusted code folder's root, or its ready worktree's path, as `SessionRoots._prepare`
    admits them; refuses before any filesystem call when the rows already say no."""
    try:
        workspace = workspaces.get(workspace_id, kind="code")
    except WorkspaceError:
        raise CodeRunError("unknown_workspace") from None
    if not _trusted(workspace):
        raise CodeRunError("trust_required")
    if workspaces.current_root(workspace) != workspace.root:
        raise CodeRunError("root_unavailable")
    if worktree_id is None:
        return Path(workspace.root.display)
    worktree = store.get_worktree(worktree_id)
    if worktree is None or worktree["workspace_id"] != workspace_id:
        raise CodeRunError("unknown_worktree")
    if worktree["state"] != "ready" or not Path(worktree["path"]).is_dir():
        raise CodeRunError("worktree_not_ready")
    # A ready row must not redirect the cwd into a different root.
    try:
        if chain_has_reparse(Path(worktree["path"]), strict=True):
            raise CodeRunError("worktree_not_ready")
    except OSError:
        raise CodeRunError("worktree_not_ready") from None
    return Path(worktree["path"])


def _context(check_id: str) -> bytes:
    return b"VIKI check v1\0" + check_id.encode("ascii")


def seal(check_id: str, name: str, command: str) -> bytes:
    canonical = json.dumps(
        {"command": command, "name": name, "v": 1},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        sealed = protection.protect(canonical, context=_context(check_id))
    except protection.ProtectionError:
        raise CodeRunError("unavailable") from None
    if not 1 <= len(sealed) <= MAX_BODY_BYTES:
        raise CodeRunError("unavailable")
    return sealed


def unseal(row: CheckRow) -> tuple[str, str]:
    """The check's (name, command); a body that will not open, or opens to anything but a
    valid version-1 document, is `check_unreadable`."""
    try:
        plain = protection.unprotect(row["body"], context=_context(row["check_id"]))
        document = json.loads(plain.decode("utf-8"))
    except protection.ProtectionError, ValueError:
        raise CodeRunError("check_unreadable") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"command", "name", "v"}
        or type(document["v"]) is not int
        or document["v"] != 1
        or not valid_name(document["name"])
        or not valid_command(document["command"])
    ):
        raise CodeRunError("check_unreadable")
    return document["name"], document["command"]


@dataclass(frozen=True)
class CheckView:
    check_id: str
    name: str | None  # None, with command, when the body will not open here
    command: str | None
    timeout_s: int
    created_at: str
    readable: bool


def _view(row: CheckRow) -> CheckView:
    try:
        name, command = unseal(row)
    except CodeRunError:
        return CheckView(row["check_id"], None, None, row["timeout_s"], row["created_at"], False)
    return CheckView(row["check_id"], name, command, row["timeout_s"], row["created_at"], True)


class CheckService:
    """Synchronous: callers run it in a thread."""

    def __init__(self, store: SessionStore, workspaces: WorkspaceService) -> None:
        self._store = store
        self._workspaces = workspaces

    def list(
        self, workspace_id: str, worktree_id: str | None
    ) -> list[tuple[CheckView, CheckRunRow | None]]:
        """The folder's checks, oldest first, each with its newest run in that checkout."""
        if not CHECKS_SUPPORTED:
            raise CodeRunError("unsupported_platform")
        trusted_root(self._store, self._workspaces, workspace_id, worktree_id)
        rows = self._store.list_checks(workspace_id)
        last = self._store.last_check_runs(workspace_id, worktree_id)
        return [(_view(row), last.get(row["check_id"])) for row in rows]

    def add_check(self, workspace_id: str, name: str, command: str, timeout_s: int) -> CheckView:
        if not CHECKS_SUPPORTED:
            raise CodeRunError("unsupported_platform")
        if not valid_name(name):
            raise CodeRunError("invalid_name")
        if not valid_command(command):
            raise CodeRunError("invalid_command")
        if not valid_timeout(timeout_s):
            raise CodeRunError("invalid_timeout")
        trusted_root(self._store, self._workspaces, workspace_id, None)
        check_id = str(uuid.uuid4())
        row: CheckRow = {
            "check_id": check_id,
            "workspace_id": workspace_id,
            "body": seal(check_id, name, command),
            "timeout_s": timeout_s,
            "created_at": now_iso(),
        }
        try:
            self._store.insert_check(row, limit=MAX_CHECKS)
        except StateError as exc:
            if exc.code == "capacity":
                raise CodeRunError("check_limit") from None
            raise
        return CheckView(check_id, name, command, timeout_s, row["created_at"], True)
