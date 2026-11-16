"""What Decision sees of one vault between chats: stuck saves and operations, deleted notes waiting
in the recovery store, and memories whose cited notes changed.

Read-only and bounded by the readers it calls: the two journals' `list_pending`, the recovery
store's `entries` and `usage` (no sweep, no folder), and `memory_drift`. `read` never raises. Each
part is read alone, so one failure leaves the others standing: the failed part answers None and
its word joins `failures` (`saves_unknown`, `recovery_unknown`, `memory_unknown`; `memory_drift`
adds `memory_partial`). Memory is read only when asked, as the caller knows the vault's policy.
The reader keeps, per workspace and in memory only for its lifetime, what `memory_drift` has read
of the memory records, so a later read skips the records whose file and approval are unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from viki_agent.automation.routines import parse_stamp
from viki_agent.decision_loop.contracts import DeletedNote, SaveRow, VaultLook
from viki_agent.decision_loop.memory_drift import Seen, memory_drift
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault import ScanResult, VaultService
from viki_agent.vaults.vault_operations import OperationJournal, RecoveryStore
from viki_agent.vaults.vault_publication import PublicationJournal


class VaultLookReader:
    def __init__(
        self,
        publications: PublicationJournal,
        operations: OperationJournal,
        recovery: RecoveryStore,
        store: SessionStore,
        reader: VaultService,
    ) -> None:
        self._publications = publications
        self._operations = operations
        self._recovery = recovery
        self._store = store
        self._reader = reader
        # Cycles run one at a time under the engine lock, so the maps need no lock.
        self._seen: dict[str, dict[str, Seen]] = {}

    def read(self, workspace_id: str, scan: ScanResult | None, memory: bool) -> VaultLook:
        failures: list[str] = []
        saves = _part(failures, "saves_unknown", lambda: self._saves(workspace_id))
        deleted = _part(failures, "recovery_unknown", lambda: self._deleted(workspace_id))
        usage = _part(failures, "recovery_unknown", self._recovery.usage)
        drift: tuple[str, ...] | None = None
        if memory:
            found = (
                None
                if scan is None
                else _part(
                    failures,
                    "memory_unknown",
                    lambda: memory_drift(
                        self._store,
                        self._reader,
                        workspace_id,
                        scan,
                        seen=self._seen.setdefault(workspace_id, {}),
                    ),
                )
            )
            if found is None:
                _fail(failures, "memory_unknown")
            else:
                drift = found[0]
                failures.extend(found[1])
        return VaultLook(saves, deleted, usage, drift, tuple(failures))

    def _saves(self, workspace_id: str) -> tuple[SaveRow, ...]:
        publications = (
            SaveRow(row.operation_id, False, False, _moment(row.updated_at))
            for row in self._publications.list_pending()
            if row.workspace_id == workspace_id
        )
        operations = (
            SaveRow(
                row.operation_id,
                True,
                row.state == "recovery_required",
                _moment(row.updated_at),
            )
            for row in self._operations.list_pending()
            if row.workspace_id == workspace_id
        )
        return (*publications, *operations)

    def _deleted(self, workspace_id: str) -> tuple[DeletedNote, ...]:
        return tuple(
            DeletedNote(entry.recovery_id, _moment(entry.expires_at))
            for entry in self._recovery.entries(workspace_id)
            if entry.kind == "deleted"
        )


def _part[T](failures: list[str], word: str, read: Callable[[], T]) -> T | None:
    """One part's value, or None with its word when reading it raised anything."""
    try:
        return read()
    except Exception:
        _fail(failures, word)
        return None


def _fail(failures: list[str], word: str) -> None:
    # `deleted` and `usage` share a word: name it once.
    if word not in failures:
        failures.append(word)


def _moment(value: str) -> datetime | None:
    try:
        return parse_stamp(value)
    except ValueError:
        return None
