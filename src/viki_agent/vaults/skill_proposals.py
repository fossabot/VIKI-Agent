"""Skill proposals: a `SKILL.md` text the user may install, kept sealed in the app's own store.

The app never writes a skill file. A proposed text is checked with the native reader's own
frontmatter rules (`native_assets.parse_skill_text`), sealed into the store, and reaches
`.claude/skills/<name>/SKILL.md` only through a chat the user sends. Every file read goes through
`NativeAssetInspector`, and each read derives a proposal's state from the installed file's bytes;
no stored field says "applied". Nothing here logs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from viki_agent import protection
from viki_agent.automation.routines import stamp
from viki_agent.profiles import native_assets
from viki_agent.profiles.native_assets import (
    NativeAsset,
    NativeAssetCatalog,
    NativeAssetError,
    NativeAssetInspector,
    SkillHead,
    parse_skill_text,
)
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults.proposals import _diff
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MAX_SKILL_PROPOSALS = 100
MAX_SKILL_BYTES = native_assets.MAX_FILE_BYTES
MAX_SESSIONS = 10
MAX_REASON_CHARS = 200
TRIGGERS = ("button", "long_session", "repeated_request", "correction")
ACTIONS = ("approve", "edit", "reject", "withdraw")

SkillState = Literal["open", "handed", "applied", "rejected", "stale", "source_missing", "unknown"]
# The states that still ask the user for something.
ATTENTION_STATES = frozenset({"open", "stale", "source_missing"})

_CONTEXT = b"VIKI skill proposal v1\0"
# Every control character but a tab and a line feed (C0, DEL and C1), and every character that
# can hide or reorder text: Unicode 17's Default_Ignorable_Code_Point set (zero-width characters,
# bidi controls, fillers, variation selectors, tags and the code points reserved as ignorable),
# plus U+2028/U+2029 and U+FFF9-FFFB, minus U+FE0E/U+FE0F: VS15 and VS16 stay allowed for
# everyday emoji.
_CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
    r"\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0d\ufeff\uffa0\ufff0-\ufffb"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_REVISION = re.compile(r"[0-9a-f]{64}")
# A reason is one line: no control character at all, no Unicode line or paragraph separator, and
# no lone surrogate, which the store cannot encode.
_NOT_IN_REASON = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")

SkillRow = dict[str, Any]
InstalledSkill = tuple[str, str] | Literal["unreadable"] | None


class SkillProposalError(Exception):
    """A typed refusal. `extra` holds only ids, hashes, limits and reason codes, never text."""

    def __init__(self, code: str, **extra: Any) -> None:
        self.code = code
        self.extra = extra
        super().__init__(code)


class SkillProposalService:
    """Create, list, open and act on skill proposals. Writes the store, never a file."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        store: SessionStore,
        inspector: NativeAssetInspector,
        *,
        wall: Callable[[], int] = time.time_ns,
    ) -> None:
        self._workspaces = workspaces
        self._store = store
        self._inspector = inspector
        self._wall = wall

    def create(
        self, workspace_id: str, text: str, trigger: str, sessions: list[str]
    ) -> dict[str, object]:
        """Seal one proposed `SKILL.md` for the active persistent vault.

        The vault's skills are scanned once. A target that a partial scan could have left out is
        refused rather than guessed at, and an installed target's digest becomes the base that
        the proposal changes. One proposal holds each name: a holder the same scan finds applied
        is finished, and the new proposal replaces it.
        """
        active = self._workspaces.resolve_active()
        if active.chat_only or active.workspace.kind != "vault" or not active.workspace.persistent:
            raise SkillProposalError("no_vault")
        if active.workspace.workspace_id != workspace_id:
            raise SkillProposalError("not_active")
        data, head = _checked(text)
        if trigger not in TRIGGERS or not _valid_sessions(sessions):
            raise SkillProposalError("invalid_request")
        proposed_sha256 = hashlib.sha256(data).hexdigest()
        source_id = _source_id(workspace_id)
        catalog = self._catalog(source_id)
        installed = functools.cache(functools.partial(self._installed, source_id, head.name))
        base_sha256 = _base(catalog, head.name, proposed_sha256, installed)
        # The replay check reads one list outside the write transaction; the write's own check,
        # the holder check inside `insert_skill_proposal` or `_replace`'s delete at the applied
        # holder's revision, is what keeps one name to one proposal.
        finished: SkillRow | None = None
        for held in self._store.list_skill_proposals(workspace_id):
            if held["name"] == head.name:
                if self._state(held, catalog, installed) == "applied":
                    finished = held
                    break
                same = held["proposed_sha256"] == proposed_sha256
                raise SkillProposalError(
                    "proposal_exists" if same else "proposal_conflict",
                    proposal_id=held["proposal_id"],
                )
        proposal_id = str(uuid.uuid4())
        now = self._stamp()
        row: SkillRow = {
            "proposal_id": proposal_id,
            "workspace_id": workspace_id,
            "name": head.name,
            "trigger": trigger,
            "sessions": json.dumps(sessions, separators=(",", ":")),
            "base_sha256": base_sha256,
            "proposed_sha256": proposed_sha256,
            "body": self._seal(data, workspace_id, proposal_id),
            "text_bytes": len(data),
            "approved_at": None,
            "rejected_at": None,
            "reason": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            if finished is None:
                self._store.insert_skill_proposal(row, limit=MAX_SKILL_PROPOSALS)
            else:
                self._replace(finished, row)
        except StateError as exc:
            # A held name and a full vault are the user's to resolve; any other error propagates.
            if exc.code == "conflict":
                raise SkillProposalError("proposal_conflict") from None
            if exc.code == "capacity":
                raise SkillProposalError("proposals_full") from None
            raise
        return self._item(row, "open")

    def inbox_items(self) -> list[dict[str, object]]:
        """The active vault's proposals, each state derived from one scan of its skills now."""
        active = self._workspaces.resolve_active()
        if active.chat_only or active.workspace.kind != "vault":
            return []
        workspace_id = active.workspace.workspace_id
        rows = self._store.list_skill_proposals(workspace_id)
        if not rows:
            return []
        source_id = _source_id(workspace_id)
        catalog = self._catalog(source_id)
        return [
            self._item(
                row,
                self._state(
                    row,
                    catalog,
                    functools.cache(functools.partial(self._installed, source_id, row["name"])),
                ),
            )
            for row in rows
        ]

    def detail(self, proposal_id: str) -> dict[str, object]:
        """One proposal with its text, the installed file, a diff between them and the
        context the user decides with: the frontmatter, any personal skill that would run
        instead, and the vault's policy."""
        row = self._row(proposal_id)
        workspace_id, name = row["workspace_id"], row["name"]
        active = self._workspaces.resolve_active()
        if active.chat_only or active.workspace.workspace_id != workspace_id:
            raise SkillProposalError("not_active")
        try:
            policy = self._workspaces.get(workspace_id).policy
        except WorkspaceError:
            raise SkillProposalError("not_active") from None
        text = self._unseal(row)
        try:
            head = parse_skill_text(text)
        except NativeAssetError:
            raise SkillProposalError("unavailable") from None
        source_id = _source_id(workspace_id)
        catalog = self._catalog(source_id)
        read = functools.cache(functools.partial(self._installed, source_id, name))
        try:
            installed = _target(catalog, name, read) if catalog is not None else None
        except SkillProposalError:
            catalog, installed = None, None
        # One read serves both the diff and the state's CRLF check. A target the scan cannot
        # rule out (no catalog, or one that may have left it out) is unreadable, not new.
        current = _read_text(read()) if installed is not None else None
        if current is None and (installed is not None or catalog is None or not _whole(catalog)):
            diff, diff_reason = "", "unreadable"
        else:
            diff, diff_reason = _diff(_relative_id(name), current, text)
        return {
            **self._item(row, self._state(row, catalog, read)),
            "text": text,
            "current_text": current,
            "diff": diff,
            "diff_reason": diff_reason,
            "description": head.description,
            "unsupported_fields": list(head.unsupported_fields),
            "shadowed": _shadowed(self._catalog("claude:user"), name),
            "policy": policy,
        }

    def act(
        self,
        proposal_id: str,
        action: str,
        revision: str,
        *,
        text: str | None = None,
        reason: str | None = None,
    ) -> dict[str, object] | None:
        """Approve, edit, reject or withdraw one proposal at the revision the user saw.

        The first thing wrong is refused, in this order: a malformed request, an unknown
        proposal, a vault that is not open, a revision that moved on, then the action's own
        checks. Each write is the store's compare-and-set at `revision`, so a row that moves on
        between this read and the write is refused as stale and keeps what it holds. A decision
        made meanwhile leaves the revision where it was: an approval that meets a rejection is
        refused as stale too and the rejection stands, while a rejection replaces an approval.
        Approving records the approval only; a chat the user sends writes the file. Answers the
        item the write left, its state derived from the same scan, or None after a withdrawal.
        """
        if not _valid_request(action, revision, text, reason):
            raise SkillProposalError("invalid_request")
        row = self._row(proposal_id)
        active = self._workspaces.resolve_active()
        if active.chat_only or active.workspace.workspace_id != row["workspace_id"]:
            raise SkillProposalError("not_active")
        if row["proposed_sha256"] != revision:
            raise SkillProposalError("stale_revision", current_revision=row["proposed_sha256"])
        if action == "withdraw":
            if not self._store.delete_skill_proposal(proposal_id, revision):
                raise self._stale(proposal_id)
            return None
        source_id = _source_id(row["workspace_id"])
        catalog = self._catalog(source_id)
        installed = functools.cache(functools.partial(self._installed, source_id, row["name"]))
        state = self._state(row, catalog, installed)
        if state == "applied":
            raise SkillProposalError("already_applied")
        now = self._stamp()
        if action == "approve":
            if state not in ("open", "handed"):
                raise SkillProposalError("not_approvable", state=state)
            changes: SkillRow = {"approved_at": now}
        elif action == "reject":
            kept = reason.strip() if reason is not None else None
            changes = {"approved_at": None, "rejected_at": now, "reason": kept}
        else:
            changes = self._edit(row, catalog, text, installed)
        changes["updated_at"] = now
        try:
            written = self._store.update_skill_proposal(proposal_id, revision, changes)
        except StateError as exc:
            # The store's CHECK of one decision per row is the only one an approval can break:
            # a rejection made at this revision after the read.
            if action != "approve" or exc.code != "invalid_input":
                raise
            written = False
        if not written:
            raise self._stale(proposal_id)
        row = {**row, **changes}
        return self._item(row, self._state(row, catalog, installed))

    # --- internals ------------------------------------------------------------------------

    def _row(self, proposal_id: str) -> SkillRow:
        row = self._store.read_skill_proposal(proposal_id) if isinstance(proposal_id, str) else None
        if row is None:
            raise SkillProposalError("unknown_proposal")
        return row

    def _replace(self, finished: SkillRow, row: SkillRow) -> None:
        """Retire an applied proposal and insert `row` under its name, in one write transaction.

        The applied row is deleted at the revision the scan found installed. A row that moved on
        since, withdrawn or already replaced by another create, refuses the write as
        `proposal_conflict` and the store keeps what it holds. The row count stays as it was, so
        no limit is checked.
        """
        with self._store._connection(write=True) as connection:
            if not connection.execute(
                "DELETE FROM skill_proposals WHERE proposal_id=? AND proposed_sha256=?",
                (finished["proposal_id"], finished["proposed_sha256"]),
            ).rowcount:
                raise SkillProposalError("proposal_conflict")
            connection.execute(
                f"INSERT INTO skill_proposals ({','.join(row)}) "
                f"VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )

    def _stale(self, proposal_id: str) -> SkillProposalError:
        """The refusal of a write whose row moved on, naming the revision the row holds now."""
        row = self._store.read_skill_proposal(proposal_id)
        return SkillProposalError(
            "stale_revision", current_revision=row["proposed_sha256"] if row is not None else None
        )

    def _edit(
        self,
        row: SkillRow,
        catalog: NativeAssetCatalog | None,
        text: object,
        installed_text: Callable[[], InstalledSkill],
    ) -> SkillRow:
        """The columns an edit writes: the new text sealed to this row, rebased onto the file the
        scan read now, with the approval and the rejection cleared."""
        data, head = _checked(text)
        if head.name != row["name"]:
            raise SkillProposalError("name_changed")
        proposed_sha256 = hashlib.sha256(data).hexdigest()
        if proposed_sha256 == row["proposed_sha256"]:
            raise SkillProposalError("no_change")
        base_sha256 = _base(catalog, head.name, proposed_sha256, installed_text)
        return {
            "body": self._seal(data, row["workspace_id"], row["proposal_id"]),
            "proposed_sha256": proposed_sha256,
            "base_sha256": base_sha256,
            "text_bytes": len(data),
            "approved_at": None,
            "rejected_at": None,
            "reason": None,
        }

    def _catalog(self, source_id: str) -> NativeAssetCatalog | None:
        """One scan of a source, or None when the source cannot be read."""
        try:
            return self._inspector.inspect(source_id)
        except NativeAssetError, WorkspaceError, OSError:
            return None

    def _installed(self, source_id: str, name: str) -> InstalledSkill:
        """The canonical target's text and raw digest, absent, or a failed inspector read."""
        try:
            return self._inspector.read_skill(source_id, name)
        except NativeAssetError, WorkspaceError, OSError:
            return "unreadable"

    @staticmethod
    def _state(
        row: SkillRow,
        catalog: NativeAssetCatalog | None,
        installed_text: Callable[[], InstalledSkill],
    ) -> SkillState:
        """The first state whose condition holds. A folded target needs a canonical read; an
        exact target needs one only for a digest matching neither hash. CRLF text counts as
        applied when each CRLF turned to LF hashes to the proposal."""
        if catalog is None:
            return "unknown"
        try:
            target = _target(catalog, row["name"], installed_text)
        except SkillProposalError:
            return "unknown"
        proposed, base = row["proposed_sha256"], row["base_sha256"]
        if target is not None and (
            target.digest == proposed
            or (target.digest != base and _lf_sha256(_read_text(installed_text())) == proposed)
        ):
            return "applied"
        if row["rejected_at"] is not None:
            return "rejected"
        if target is None:
            if not _whole(catalog):
                return "unknown"
            if base is not None:
                return "source_missing"
        elif target.digest != base:
            return "stale"
        return "handed" if row["approved_at"] is not None else "open"

    @staticmethod
    def _item(row: SkillRow, state: SkillState) -> dict[str, object]:
        name = row["name"]
        return {
            "kind": "skill_proposal",
            "id": row["proposal_id"],
            "origin": {"workspace_id": row["workspace_id"], "session_id": None},
            "revision": row["proposed_sha256"],
            "state": state,
            "attention": state in ATTENTION_STATES,
            "title": ("New skill: " if row["base_sha256"] is None else "Skill change: ") + name,
            "name": name,
            "relative_id": _relative_id(name),
            "trigger": row["trigger"],
            "sessions": json.loads(row["sessions"]),
            "base_revision": row["base_sha256"],
            "text_bytes": row["text_bytes"],
            "approved_at": row["approved_at"],
            "rejected_at": row["rejected_at"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _context(workspace_id: str, proposal_id: str) -> bytes:
        """The seal is bound to this workspace and this proposal, so no other row unseals it."""
        return _CONTEXT + workspace_id.encode() + b"\0" + proposal_id.encode()

    def _seal(self, data: bytes, workspace_id: str, proposal_id: str) -> bytes:
        try:
            return protection.protect(data, context=self._context(workspace_id, proposal_id))
        except protection.ProtectionError:
            raise SkillProposalError("unavailable") from None

    def _unseal(self, row: SkillRow) -> str:
        try:
            plain = protection.unprotect(
                row["body"], context=self._context(row["workspace_id"], row["proposal_id"])
            )
            return plain.decode("utf-8")
        except protection.ProtectionError, UnicodeDecodeError:
            raise SkillProposalError("unavailable") from None

    def _stamp(self) -> str:
        return stamp(datetime.fromtimestamp(self._wall() / 1_000_000_000, UTC))


def _checked(text: object) -> tuple[bytes, SkillHead]:
    """The text's UTF-8 bytes and frontmatter, or the refusal its first failed check names."""
    if not isinstance(text, str):
        raise SkillProposalError("invalid_request")
    if "\r" in text:
        raise SkillProposalError("invalid_skill", reason="carriage_return")
    if _CONTROL.search(text):
        raise SkillProposalError("invalid_skill", reason="control_character")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise SkillProposalError("invalid_skill", reason="invalid_encoding") from None
    if len(data) > MAX_SKILL_BYTES:
        raise SkillProposalError("too_large", limit=MAX_SKILL_BYTES)
    try:
        return data, parse_skill_text(text)
    except NativeAssetError as exc:
        raise SkillProposalError("invalid_skill", reason=exc.code) from None


def _valid_request(action: object, revision: object, text: object, reason: object) -> bool:
    """A known action at a SHA-256 revision, a text with an edit alone, and a reason with a
    rejection alone: one line of 1 to `MAX_REASON_CHARS` UTF-16 code units once stripped, the
    length the browser measures."""
    if action not in ACTIONS or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        return False
    if (text is not None) != (action == "edit") or not isinstance(text, str | None):
        return False
    # The character check comes first: a lone surrogate does not encode as UTF-16 either.
    return reason is None or (
        action == "reject"
        and isinstance(reason, str)
        and _NOT_IN_REASON.search(reason) is None
        and 1 <= len(reason.strip().encode("utf-16-le")) // 2 <= MAX_REASON_CHARS
    )


def _base(
    catalog: NativeAssetCatalog | None,
    name: str,
    proposed_sha256: str,
    installed_text: Callable[[], InstalledSkill],
) -> str | None:
    """The installed target's digest, which the proposal changes; None for a new skill. A failed
    scan, one that may have left the target out, and a target holding the text already are
    refused. Besides a folded target's identity check, a digest other than the proposal's reads
    `installed_text`: a file whose text as the inspector reads it (a leading BOM dropped), each
    CRLF turned to LF, hashes to the proposal holds the text already."""
    if catalog is None:
        raise SkillProposalError("unavailable")
    installed = _target(catalog, name, installed_text)
    if installed is None:
        if not _whole(catalog):
            raise SkillProposalError("catalog_partial")
        return None
    if (
        installed.digest == proposed_sha256
        or _lf_sha256(_read_text(installed_text())) == proposed_sha256
    ):
        raise SkillProposalError("no_change")
    return installed.digest


def _valid_sessions(sessions: object) -> bool:
    """1 to `MAX_SESSIONS` distinct chat ids, each a canonical UUID."""
    return (
        isinstance(sessions, list)
        and 1 <= len(sessions) <= MAX_SESSIONS
        and all(_canonical_uuid(session) for session in sessions)
        and len(set(sessions)) == len(sessions)
    )


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _source_id(workspace_id: str) -> str:
    return f"claude:workspace:{workspace_id}"


def _relative_id(name: str) -> str:
    return f"skills/{name}/SKILL.md"


def _whole(catalog: NativeAssetCatalog) -> bool:
    """Whether the scan left no entry out, so a skill it does not list is absent. A description
    shortened to `native_assets.MAX_DESCRIPTION_BYTES` makes a scan partial (`oversized`) with
    nothing omitted and its skill still listed; any other reason, or any entry omitted, may stand
    for the target."""
    return not catalog.partial or (
        catalog.partial_reasons == ("oversized",) and catalog.omitted == 0
    )


def _target(
    catalog: NativeAssetCatalog, name: str, installed: Callable[[], InstalledSkill]
) -> NativeAsset | None:
    """The vault's own `skills/<name>/SKILL.md` in a scan, if the scan read it: that exact path,
    else an ASCII case variant whose raw digest a canonical-path read confirms. That read supplies
    filesystem evidence without assuming a volume's case rules from the platform. Other letters
    are not folded: Unicode folds some onto ASCII (U+212A KELVIN SIGN onto `k`) where NTFS keeps
    the folders apart."""
    relative_id = _relative_id(name)
    exact = next((asset for asset in catalog.assets if asset.relative_id == relative_id), None)
    if exact is not None:
        return exact
    folded = relative_id.lower()
    candidate = next(
        (
            asset
            for asset in catalog.assets
            if asset.relative_id.isascii() and asset.relative_id.lower() == folded
        ),
        None,
    )
    if candidate is None:
        return None
    current = installed()
    if current is None:
        return None
    if isinstance(current, str) or current[1] != candidate.digest:
        raise SkillProposalError("unavailable")
    return candidate


def _read_text(installed: InstalledSkill) -> str | None:
    return installed[0] if isinstance(installed, tuple) else None


def _lf_sha256(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _shadowed(catalog: NativeAssetCatalog | None, name: str) -> bool | None:
    """Whether a personal skill of this name runs instead; None when the scan cannot tell."""
    if catalog is None:
        return None
    if any(asset.kind == "skill" and asset.name == name for asset in catalog.assets):
        return True
    return False if _whole(catalog) else None
