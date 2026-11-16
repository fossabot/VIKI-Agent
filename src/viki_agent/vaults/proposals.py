"""Proposals for a protected vault: the user's replacement text for a note the app may not write.

Hard rule 1 keeps a protected vault app-unwritable, so a note edit there can produce only a
proposal: the proposed bytes are sealed into the app's own store, and `claude` makes the change
itself under the user's hooks and log. No method here opens a file for writing, takes a mutation
lease or reaches `vault_publication`; every vault touch is one bounded open-read-close through
`VaultService.read` (and, when that read meets no folder, one lstat under the same verified root),
and a proposal's state is derived from that read on each call rather than remembered, so a note
changed outside the app is reported, never overwritten.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from viki_agent import protection
from viki_agent.automation.routines import stamp
from viki_agent.domain import Workspace
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults.vault import NOTE_SUFFIXES, Note, VaultError, VaultService, safe_ref
from viki_agent.workspaces import WorkspaceError, WorkspaceService

# The reader's own verified root openers, used only to probe that a folder does not exist.
if sys.platform == "win32":
    from viki_agent.vaults.vault import _open_registered_root
else:
    from viki_agent.vaults.vault import _open_root_fd

MAX_PROPOSAL_BYTES = 1 << 20
MAX_PROPOSALS = 200
# The diff is built with difflib, whose cost grows with the product of the two line counts, so a
# note or a proposal past this many lines reports `diff_reason: "too_large"` instead of a diff.
MAX_DIFF_LINES = 2000

_CONTEXT = b"VIKI proposal v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")

ProposalState = Literal["open", "handed", "applied", "stale", "source_missing", "unknown"]
ProposalOrigin = Literal["note_edit", "memory_promotion"]
_ORIGINS = frozenset({"note_edit", "memory_promotion"})
# The states that still ask the user for something; `handed` is in flight and `applied` is done.
ATTENTION_STATES = frozenset({"open", "stale", "source_missing"})

ProposalRow = dict[str, Any]


class ProposalError(Exception):
    """A typed refusal. `extra` carries only ids, revisions and limits, never proposed text."""

    def __init__(self, code: str, **extra: Any) -> None:
        self.code = code
        self.extra = extra
        super().__init__(code)


class ProposalService:
    """Create, inspect, hand off and withdraw proposals. Writes the store, never a vault."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        store: SessionStore,
        vault: VaultService,
        *,
        wall: Callable[[], int] = time.time_ns,
    ) -> None:
        self._workspaces = workspaces
        self._store = store
        self._vault = vault
        self._wall = wall

    def create(
        self,
        workspace_id: str,
        ref: str,
        proposed_text: str,
        base_revision: str | None,
        *,
        origin: ProposalOrigin = "note_edit",
    ) -> dict[str, object]:
        """Seal one proposed note text against the revision the user edited.

        Only a `protected` workspace takes a proposal: an ordinary vault has its own journalled save
        and a read-only one has nothing. The note is read once, so a text that no longer matches
        `base_revision` is refused rather than proposed against bytes the user never saw. A
        `memory_promotion` (`memory.MemoryService.promote`) may propose a new note: its base is
        None, and a note already at the ref is refused the same way.
        """
        workspace = self._workspace(workspace_id)
        if workspace.policy != "protected":
            raise ProposalError("not_protected", policy=workspace.policy)
        new_note = origin == "memory_promotion" and base_revision is None
        if (
            origin not in _ORIGINS
            or not isinstance(proposed_text, str)
            or not (new_note or _is_sha256(base_revision))
        ):
            raise ProposalError("invalid_request")
        self._check_ref(ref)
        proposed = proposed_text.encode("utf-8")
        if len(proposed) > MAX_PROPOSAL_BYTES:
            raise ProposalError("too_large", limit=MAX_PROPOSAL_BYTES)
        note, failure = self._note(workspace_id, ref)
        if note is None:
            if not (new_note and failure == "source_missing"):
                raise ProposalError(
                    "source_missing" if failure == "source_missing" else "unavailable"
                )
        elif note.sha256 != base_revision:
            raise ProposalError("stale_revision", current_revision=note.sha256)
        proposed_sha256 = hashlib.sha256(proposed).hexdigest()
        if note is not None and proposed_sha256 == note.sha256:
            raise ProposalError("no_change")
        # The replay check and the cap read one list, outside the write transaction: the UNIQUE
        # (workspace_id, ref) and `write_vault_proposal`'s own holder check are what keep one ref
        # to one proposal, so the worst a race costs is one proposal past the cap.
        rows = self._store.list_vault_proposals(workspace_id)
        for row in rows:
            if row["ref"] != ref:
                continue
            if row["proposed_sha256"] == proposed_sha256:
                raise ProposalError("proposal_exists", proposal_id=row["proposal_id"])
            raise ProposalError("proposal_conflict", proposal_id=row["proposal_id"])
        if len(rows) >= MAX_PROPOSALS:
            raise ProposalError("proposals_full", limit=MAX_PROPOSALS)
        proposal_id = str(uuid.uuid4())
        row = {
            "proposal_id": proposal_id,
            "workspace_id": workspace_id,
            "ref": ref,
            "origin": origin,
            "base_sha256": base_revision,
            "proposed_sha256": proposed_sha256,
            "body": self._seal(proposed, workspace_id, proposal_id),
            "text_bytes": len(proposed),
            "handed_at": None,
            "created_at": self._stamp(),
        }
        try:
            self._store.write_vault_proposal(row)
        except StateError as exc:
            # Only a held ref is the user's to resolve; a busy or failed store is the route's 503.
            if exc.code != "conflict":
                raise
            raise ProposalError("proposal_conflict") from None
        return self._item(row, "open")

    def detail(self, proposal_id: str) -> dict[str, object]:
        """One proposal with its unsealed text and a unified diff against the note as it is now."""
        row = self._row(proposal_id)
        note, failure = self._note(row["workspace_id"], row["ref"])
        proposed_text = self._unseal(row)
        diff, diff_reason = _diff(row["ref"], note.source if note else None, proposed_text)
        return {
            **self._item(row, self._state(row, note, failure)),
            "current_revision": note.sha256 if note else None,
            "proposed_text": proposed_text,
            "diff": diff,
            "diff_reason": diff_reason,
        }

    def handoff(self, proposal_id: str) -> dict[str, object]:
        """Record that the proposal was handed to `claude`; the app itself applies nothing."""
        row = self._row(proposal_id)
        note, failure = self._note(row["workspace_id"], row["ref"])
        state = self._state(row, note, failure)
        if state == "applied":
            raise ProposalError("already_applied")
        if state == "stale":
            raise ProposalError("stale_proposal", current_revision=note.sha256 if note else None)
        if state == "source_missing":
            raise ProposalError("source_missing")
        if state == "unknown":
            raise ProposalError("unavailable")
        handed = {**row, "handed_at": self._stamp()}
        if not self._store.mark_vault_proposal_handed(row["proposal_id"], handed["handed_at"]):
            # The user withdrew it while this hand-off was reading the note.
            raise ProposalError("unknown_proposal")
        return self._item(handed, "handed")

    def withdraw(self, proposal_id: str) -> None:
        if not isinstance(proposal_id, str) or not self._store.delete_vault_proposal(proposal_id):
            raise ProposalError("unknown_proposal")

    def inbox_items(self) -> list[dict[str, object]]:
        """Every workspace's proposals, each state re-derived from its note's current revision."""
        items: list[dict[str, object]] = []
        for workspace in self._workspaces.list():
            for row in self._store.list_vault_proposals(workspace.workspace_id):
                note, failure = self._note(workspace.workspace_id, row["ref"])
                items.append(self._item(row, self._state(row, note, failure)))
        return items

    # --- internals ------------------------------------------------------------------------

    def _workspace(self, workspace_id: str) -> Workspace:
        try:
            return self._workspaces.get(workspace_id)
        except WorkspaceError:
            raise ProposalError("unknown_workspace") from None

    def _row(self, proposal_id: str) -> ProposalRow:
        row = self._store.read_vault_proposal(proposal_id) if isinstance(proposal_id, str) else None
        if row is None:
            raise ProposalError("unknown_proposal")
        return row

    @staticmethod
    def _check_ref(ref: str) -> None:
        """`safe_ref` refuses an excluded name at any depth; the suffix is the reader's own set."""
        try:
            parts = safe_ref(ref)
        except VaultError as exc:
            raise ProposalError(exc.code) from None
        if PurePosixPath(parts[-1]).suffix.casefold() not in NOTE_SUFFIXES:
            raise ProposalError("invalid_ref")

    def _note(self, workspace_id: str, ref: str) -> tuple[Note | None, ProposalState | None]:
        """One bounded read: the note, or the state its failure derives."""
        try:
            return self._vault.read(workspace_id, ref), None
        except VaultError as exc:
            missing = exc.code == "missing_note" or (
                exc.code == "unsafe_path" and self._folder_absent(workspace_id, ref)
            )
            return None, "source_missing" if missing else "unknown"

    def _folder_absent(self, workspace_id: str, ref: str) -> bool:
        """Whether the ref's first folder does not exist: the reader reports that as
        `unsafe_path`, as it does an unsafe folder. Only an lstat beneath the reader's own
        verified, held root that finds no entry at all counts; a reparse point, a link, a file or
        an alias is an entry, so it answers False and the reader's refusal stands."""
        # Only the first folder is probed, which covers `memory/<id>.md`; a missing
        # deeper folder still reads `unknown`, since an lstat through an unheld folder could
        # follow a swapped-in link.
        try:
            parts = safe_ref(ref)
            root = self._workspaces.current_root(self._workspaces.get(workspace_id))
            if len(parts) < 2 or root is None:
                return False
            if sys.platform == "win32":
                with _open_registered_root(root) as path:
                    return _no_entry(lambda: os.lstat(path / parts[0]))
            with _open_root_fd(root) as fd:
                return _no_entry(lambda: os.lstat(parts[0], dir_fd=fd))
        except VaultError, WorkspaceError, OSError:
            return False

    @staticmethod
    def _state(row: ProposalRow, note: Note | None, failure: ProposalState | None) -> ProposalState:
        if note is None:
            # A new note's proposal waits on the note's absence, as an edit's waits on its base.
            if failure == "source_missing" and row["base_sha256"] is None:
                return "handed" if row["handed_at"] else "open"
            return failure or "unknown"
        if note.sha256 == row["proposed_sha256"]:
            return "applied"
        if note.sha256 == row["base_sha256"]:
            return "handed" if row["handed_at"] else "open"
        return "stale"

    @staticmethod
    def _item(row: ProposalRow, state: ProposalState) -> dict[str, object]:
        return {
            "kind": "vault_proposal",
            "id": row["proposal_id"],
            "origin": {"workspace_id": row["workspace_id"], "session_id": None},
            "revision": row["proposed_sha256"],
            "state": state,
            "attention": state in ATTENTION_STATES,
            "title": row["ref"],
            "ref": row["ref"],
            "origin_kind": row["origin"],
            "base_revision": row["base_sha256"],
            "text_bytes": row["text_bytes"],
            "handed_at": row["handed_at"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _context(workspace_id: str, proposal_id: str) -> bytes:
        """The seal is bound to this workspace and this proposal, so no other row unseals it."""
        return _CONTEXT + workspace_id.encode() + b"\0" + proposal_id.encode()

    def _seal(self, proposed: bytes, workspace_id: str, proposal_id: str) -> bytes:
        try:
            body = protection.protect(proposed, context=self._context(workspace_id, proposal_id))
        except protection.ProtectionError:
            raise ProposalError("unavailable") from None
        if not isinstance(body, bytes) or not body:
            raise ProposalError("unavailable")
        return body

    def _unseal(self, row: ProposalRow) -> str:
        try:
            plain = protection.unprotect(
                row["body"], context=self._context(row["workspace_id"], row["proposal_id"])
            )
            return plain.decode("utf-8")
        except protection.ProtectionError, UnicodeDecodeError:
            raise ProposalError("unavailable") from None

    def _stamp(self) -> str:
        return stamp(datetime.fromtimestamp(self._wall() / 1_000_000_000, UTC))


def _no_entry(probe: Callable[[], object]) -> bool:
    try:
        probe()
    except FileNotFoundError:
        return True
    return False


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _diff(ref: str, current: str | None, proposed: str) -> tuple[str, str | None]:
    """A unified diff from the note as it is now to the proposal, or the reason there is none."""
    if current is None:
        return "", "no_source"
    before, after = current.splitlines(), proposed.splitlines()
    if len(before) > MAX_DIFF_LINES or len(after) > MAX_DIFF_LINES:
        return "", "too_large"
    return "\n".join(difflib.unified_diff(before, after, f"a/{ref}", f"b/{ref}", lineterm="")), None
