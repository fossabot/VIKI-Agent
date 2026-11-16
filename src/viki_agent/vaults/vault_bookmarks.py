"""Per-workspace note bookmarks: app-owned rows in the store, never a vault read or write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from viki_agent.automation.routines import stamp
from viki_agent.state import AppState
from viki_agent.vaults.vault import VaultError, safe_ref
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MAX_BOOKMARKS = 200


class BookmarkError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Bookmark:
    ref: str
    created_at: str


class BookmarkService:
    def __init__(self, state: AppState, workspaces: WorkspaceService) -> None:
        self._store = state.store
        self._workspaces = workspaces

    def _check(self, workspace_id: str, ref: str | None = None) -> None:
        try:
            self._workspaces.get(workspace_id)
        except WorkspaceError:
            raise BookmarkError("unknown_workspace") from None
        if ref is not None:
            try:
                # Also refuses an excluded name at any depth (`excluded_part`, write=False).
                safe_ref(ref)
            except VaultError as exc:
                raise BookmarkError(exc.code) from None

    def list(self, workspace_id: str) -> tuple[Bookmark, ...]:
        self._check(workspace_id)
        with self._store._connection() as connection:
            rows = connection.execute(
                "SELECT ref, created_at FROM vault_bookmarks WHERE workspace_id=?",
                (workspace_id,),
            ).fetchall()
        return tuple(
            Bookmark(*row) for row in sorted(rows, key=lambda row: (row[0].casefold(), row[0]))
        )

    def add(self, workspace_id: str, ref: str) -> Bookmark:
        self._check(workspace_id, ref)
        with self._store._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT ref, created_at FROM vault_bookmarks WHERE workspace_id=? AND ref=?",
                (workspace_id, ref),
            ).fetchone()
            if existing is not None:
                return Bookmark(*existing)
            # Counted in the same write transaction as the insert, so two adds cannot both pass.
            count = connection.execute(
                "SELECT count(*) FROM vault_bookmarks WHERE workspace_id=?", (workspace_id,)
            ).fetchone()[0]
            if count >= MAX_BOOKMARKS:
                raise BookmarkError("bookmarks_full")
            created = stamp(datetime.now(UTC))
            connection.execute(
                "INSERT INTO vault_bookmarks VALUES (?,?,?)", (workspace_id, ref, created)
            )
        return Bookmark(ref, created)

    def remove(self, workspace_id: str, ref: str) -> bool:
        self._check(workspace_id, ref)
        with self._store._connection(write=True) as connection:
            return (
                connection.execute(
                    "DELETE FROM vault_bookmarks WHERE workspace_id=? AND ref=?",
                    (workspace_id, ref),
                ).rowcount
                > 0
            )
