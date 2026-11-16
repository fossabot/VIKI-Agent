"""Read-only admission of registered ordinary workspaces for vault mutation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from viki_agent.domain import CanonicalRoot, RootIdentity, Workspace, WorkspaceKind
from viki_agent.vaults.vault_mutation import MutationError, OrdinaryMutationRoot
from viki_agent.workspaces import RowKind, WorkspaceError, _overlaps, probe_identity


class WorkspaceReader(Protocol):
    def get(self, workspace_id: str, *, kind: RowKind | None = "vault") -> Workspace: ...

    def current_root(self, workspace: Workspace) -> CanonicalRoot | None: ...

    def list(self, *, kind: WorkspaceKind | None = "vault") -> list[Workspace]: ...


def admits_note_writes(workspace: Workspace) -> bool:
    """The stored half of the note-write gate; the resolver adds the live root checks."""
    return (
        workspace.kind == "vault"
        and workspace.policy == "ordinary"
        and workspace.persistent
        and workspace.bootstrap == "complete"
    )


def admits_code_writes(workspace: Workspace) -> bool:
    """The stored half of the code-write gate; a code folder has no bootstrap to complete."""
    return workspace.kind == "code" and workspace.policy == "ordinary" and workspace.persistent


def _shares_identity(candidate: RootIdentity, protected: RootIdentity) -> bool:
    if candidate.git_root_oids and set(candidate.git_root_oids) & set(protected.git_root_oids):
        return True
    if candidate.git_common_dir and candidate.git_common_dir == protected.git_common_dir:
        return True
    return any(
        digest == protected.instruction_hashes.get(name)
        for name, digest in candidate.instruction_hashes.items()
    )


class WorkspaceMutationResolver:
    """Resolve an exact policy snapshot without changing stored workspace evidence."""

    def __init__(
        self,
        workspaces: WorkspaceReader,
        *,
        git_available: Callable[[], bool],
    ) -> None:
        self._workspaces = workspaces
        self._git_available = git_available

    def __call__(
        self,
        workspace_id: str,
        policy_revision: int,
        physical_id: str,
        *,
        kind: RowKind | None = "vault",
    ) -> OrdinaryMutationRoot:
        """`kind` picks the rows read: the default, vaults only, keeps code ids from vault
        writers; `None` reads a stored row of either kind."""
        workspace = self._get(workspace_id, kind)
        self._require_expected(workspace, policy_revision, physical_id)
        current = self._workspaces.current_root(workspace)
        if current is None or current != workspace.root:
            raise MutationError("workspace_changed")

        live_identity = probe_identity(current, git_available=self._git_available())
        protected_instructions: set[tuple[str, str]] = set()
        for other in self._workspaces.list(kind=None):  # a protected code folder binds too
            if other.policy != "protected" or other.workspace_id == workspace_id:
                continue
            if (
                current.physical_id == other.root.physical_id
                or current.key == other.root.key
                or _overlaps(current.key, other.root.key)
                or workspace.identity.inherits_from == other.workspace_id
                or _shares_identity(workspace.identity, other.identity)
                or _shares_identity(live_identity, other.identity)
            ):
                raise MutationError("ordinary_workspace_required")
            # The hashes `_shares_identity` compares by name, for a save about to change one.
            protected_instructions.update(other.identity.instruction_hashes.items())

        latest = self._get(workspace_id, kind)
        if latest != workspace or self._workspaces.current_root(latest) != current:
            raise MutationError("workspace_changed")
        if latest.kind == "code":
            root = OrdinaryMutationRoot.from_trusted_code_folder(latest)
        else:
            root = OrdinaryMutationRoot.from_admitted_workspace(latest)
        return replace(root, protected_instructions=frozenset(protected_instructions))

    def _get(self, workspace_id: str, kind: RowKind | None) -> Workspace:
        try:
            workspace = self._workspaces.get(workspace_id, kind=kind)
        except WorkspaceError as exc:
            raise MutationError("workspace_changed") from exc
        # The reader's filter is not the only guard: a vault writer never gets a code root.
        if kind is not None and workspace.kind != kind:
            raise MutationError("workspace_changed")
        return workspace

    @staticmethod
    def _require_expected(workspace: Workspace, policy_revision: int, physical_id: str) -> None:
        if (
            workspace.workspace_id == ""
            or workspace.policy_revision != policy_revision
            or workspace.root.physical_id != physical_id
        ):
            raise MutationError("workspace_changed")
        admits = admits_code_writes if workspace.kind == "code" else admits_note_writes
        if not admits(workspace):
            raise MutationError("ordinary_workspace_required")
