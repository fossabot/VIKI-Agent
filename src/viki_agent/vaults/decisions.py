"""The decision inbox: the vault and skill proposals waiting on the user.

Nothing in the app reads a vault's `system/retro/*.md` Gate summary rows, here or in `rails`:
retros are decided in the chat that runs /retro. `inbox()` does nothing but gate on the active
workspace and hand back whatever `proposal_items` gives it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from viki_agent.workspaces import WorkspaceService

__all__ = ["DecisionError", "DecisionService"]


class DecisionError(Exception):
    def __init__(self, code: str, **extra: Any) -> None:
        self.code = code
        self.extra = extra
        super().__init__(code)


class DecisionService:
    """The decision inbox of the active vault: the proposal items waiting on the user."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        *,
        proposal_items: Callable[[], Sequence[dict[str, Any]]] = lambda: [],
    ) -> None:
        self._workspaces = workspaces
        self._proposal_items = proposal_items

    def inbox(self) -> dict[str, Any]:
        """The decision inbox: the vault and skill proposals waiting on the user."""
        if self._workspaces.resolve_active().chat_only:
            raise DecisionError("no_vault")
        return {"items": list(self._proposal_items())}
