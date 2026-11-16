"""Durable submitted turns. This ledger is not a provider retry queue or receipt."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from viki_agent import protection
from viki_agent.conversations.drafts import (
    MAX_CIPHERTEXT_BYTES,
    MAX_PAGE_SIZE,
    MAX_REVISION,
    MAX_TEXT_BYTES,
    DraftError,
    decode_draft,
    draft_scope,
    require_private_store,
)
from viki_agent.conversations.turn_parts import ExcerptPart
from viki_agent.state import AppState
from viki_agent.storage import CommandRow, DraftRecord, StateError
from viki_agent.turn_input import Attachment, TurnInput, TurnInputError, claude_accepts

MAX_COMMANDS = 4096
MAX_SCOPE_BYTES = 64 << 20
CommandState = Literal["accepted", "dispatching", "unknown", "not_dispatched"]
TerminalState = Literal["unknown", "not_dispatched"]
REASONS = frozenset(
    {
        "native_unconfirmed",
        "start_failed",
        "runtime_changed",
        "cancelled",
        "send_failed",
        "internal",
        "unsupported_attachment",
        "attachment_unavailable",
        "excerpt_with_command",
        "excerpt_wrong_vault",
        "excerpt_stale",
        "excerpt_unavailable",
        "excerpts_too_large",
    }
)
_MESSAGES = {
    "invalid_command": "The submission given isn't valid.",
    "invalid_page": "The paging for the send history isn't valid.",
    "not_found": "Send history entry not found.",
    "draft_conflict": "The draft has been updated. Check the saved content.",
    "draft_already_submitted": "This draft has already been sent. Check the send history.",
    "empty_draft": "There's no draft to send.",
    "unsupported_attachment": "This attachment is larger than Claude Code can accept.",
    "conflict": "This entry in the send history can't be changed. Check its current state.",
    "capacity": (
        "The send history's storage limit has been reached. Check the records you've saved."
    ),
    "protection": (
        "Couldn't safely read or write the submission. "
        "Check the save location's access rights and " + protection.ACCOUNT_HINT + "."
    ),
    "corrupt": (
        "Couldn't read the send history. Keep the original file and check the recovery steps."
    ),
    "unavailable": "Couldn't read or write the send history. Check the save location.",
    "internal": "Couldn't finish processing the send history.",
    "unsafe_path": "Couldn't check the send history's save location.",
}


class CommandError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message if message is not None else _MESSAGES[code]
        super().__init__(self.message)


class CommandConflict(CommandError):
    def __init__(self) -> None:
        super().__init__("conflict")


@dataclass(frozen=True)
class CommandInput:
    command_id: str
    session_id: str
    generation: str
    draft_revision: int


@dataclass(frozen=True)
class CommandIdentity(CommandInput):
    """Storage-only command identity for a direct draft or a claimed queue item."""

    source_draft_id: str


@dataclass(frozen=True)
class CommandSummary:
    command_id: str
    session_id: str
    generation: str
    draft_revision: int
    state: CommandState
    reason: str
    created_at: str
    updated_at: str
    bytes: int


@dataclass(frozen=True)
class CommandRecord(CommandSummary):
    text: str
    attachments: tuple[Attachment, ...] = ()
    parts: tuple[ExcerptPart, ...] = ()

    @property
    def turn_input(self) -> TurnInput:
        return TurnInput(self.text, self.attachments, self.parts)


@dataclass(frozen=True)
class CommandPage:
    commands: list[CommandSummary]
    next_after: str | None


def _uuid(value: Any, code: str = "invalid_command") -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise CommandError(code)


def _input(value: CommandInput) -> CommandIdentity:
    if not isinstance(value, CommandInput):
        raise CommandError("invalid_command")
    for part in (value.command_id, value.session_id, value.generation):
        _uuid(part)
    if type(value.draft_revision) is not int or not 1 <= value.draft_revision <= MAX_REVISION:
        raise CommandError("invalid_command")
    source = value.session_id
    if isinstance(value, CommandIdentity):
        source = _uuid(value.source_draft_id)
    return CommandIdentity(
        value.command_id, value.session_id, value.generation, value.draft_revision, source
    )


def _request_hash(value: CommandInput) -> str:
    # Only immutable identifiers/revision are hashed. Draft plaintext is never hashed here.
    value = _input(value)
    # Schema v6 copied v5 values byte-for-byte.  Direct submissions retain their original
    # four-field hash while a queue claim has its item id as an extra identity component.
    hashed: CommandInput = value
    if value.source_draft_id == value.session_id:
        hashed = CommandInput(
            value.command_id, value.session_id, value.generation, value.draft_revision
        )
    canonical = json.dumps(asdict(hashed), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 24:
        raise CommandError("corrupt")
    try:
        canonical = datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    except ValueError:
        raise CommandError("corrupt") from None
    if canonical.replace("+00:00", "Z") != value:
        raise CommandError("corrupt")
    return value


def _timestamp_page(value: Any) -> str:
    try:
        return _timestamp(value)
    except CommandError:
        raise CommandError("invalid_page") from None


class CommandService:
    def __init__(self, state: AppState, vault: Path, *, owner: str) -> None:
        self._store = state.store
        self._owner = _uuid(owner)
        try:
            self._scope = draft_scope(vault)
        except DraftError as exc:
            raise CommandError(exc.code) from None

    @contextlib.contextmanager
    def _access(self) -> Iterator[None]:
        try:
            require_private_store(self._store)
            yield
        except protection.ProtectionError:
            raise CommandError("protection") from None
        except DraftError as exc:
            raise CommandError(exc.code if exc.code in _MESSAGES else "corrupt") from None
        except StateError as exc:
            raise CommandError(exc.code, exc.message) from None

    def _context(self, value: CommandInput, format_value: int = 1) -> bytes:
        value = _input(value)
        if format_value == 1:
            # v5 ciphertext was carried forward by the v6 migration unchanged.
            return (
                f"VIKI submitted turn v1\0{self._scope}\0{value.command_id}\0{value.session_id}"
                f"\0{value.generation}\0{value.draft_revision}"
            ).encode("ascii")
        return (
            f"VIKI submitted turn v{format_value}\0{self._scope}\0{value.command_id}"
            f"\0{value.session_id}\0{value.source_draft_id}"
            f"\0{value.generation}\0{value.draft_revision}"
        ).encode("ascii")

    def _summary(self, row: CommandRow) -> CommandSummary:
        try:
            value = _input(
                CommandIdentity(
                    *(
                        row[name]
                        for name in (
                            "command_id",
                            "session_id",
                            "generation",
                            "draft_revision",
                            "source_draft_id",
                        )
                    )
                )
            )
            _uuid(row["owner"], "corrupt")
        except CommandError:
            raise CommandError("corrupt") from None
        state, reason = row["state"], row["reason"]
        size = row["text_bytes"]
        if (
            row["request_hash"] != _request_hash(value)
            or state not in {"accepted", "dispatching", "unknown", "not_dispatched"}
            or (reason != "" and reason not in REASONS)
            or (state in {"accepted", "dispatching"} and reason != "")
            or (state in {"unknown", "not_dispatched"} and reason == "")
            or type(size) is not int
            or not 0 <= size <= MAX_TEXT_BYTES
            or row.get("format", 1) not in {1, 2, 3}
        ):
            raise CommandError("corrupt")
        if row["owner"] != self._owner and state in {"accepted", "dispatching"}:
            state, reason = "unknown", "owner_changed"
        return CommandSummary(
            command_id=value.command_id,
            session_id=value.session_id,
            generation=value.generation,
            draft_revision=value.draft_revision,
            state=cast(CommandState, state),
            reason=reason,
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
            bytes=size,
        )

    def _decode(self, row: CommandRow) -> CommandRecord:
        summary = self._summary(row)
        value = CommandIdentity(
            summary.command_id,
            summary.session_id,
            summary.generation,
            summary.draft_revision,
            row["source_draft_id"],
        )
        cipher = row["body"]
        if not isinstance(cipher, bytes) or not 0 < len(cipher) <= MAX_CIPHERTEXT_BYTES:
            raise CommandError("corrupt")
        format_value = row.get("format", 1)
        plaintext = protection.unprotect(cipher, context=self._context(value, format_value))
        if format_value == 1:
            if len(plaintext) != summary.bytes:
                raise CommandError("corrupt")
            try:
                text = plaintext.decode("utf-8")
            except UnicodeError:
                raise CommandError("corrupt") from None
            return CommandRecord(**asdict(summary), text=text)
        try:
            turn = TurnInput.decode(plaintext)
        except TurnInputError:
            raise CommandError("corrupt") from None
        if turn.text_bytes != summary.bytes or not turn.sendable:
            raise CommandError("corrupt")
        return CommandRecord(
            **asdict(summary), text=turn.text, attachments=turn.attachments, parts=turn.parts
        )

    def _same(self, row: CommandRow, value: CommandInput) -> None:
        self._summary(row)
        identity = _input(value)
        if any(row[name] != field for name, field in asdict(identity).items()):
            raise CommandConflict()

    def lookup(self, value: CommandInput) -> CommandRecord | None:
        value = _input(value)
        with self._access():
            row = self._store.get_command(self._scope, value.command_id)
            if row is None:
                return None
            self._same(row, value)
            return self._decode(row)

    def attachment_metadata(self, asset_id: str) -> Attachment | None:
        """Read original attachment metadata without retaining bytes or changing a command."""
        _uuid(asset_id, "invalid_command")
        with self._access():
            row = self._store.asset_source_command(self._scope, asset_id)
            if row is None:
                return None
            return next(
                (item for item in self._decode(row).attachments if item.asset_id == asset_id), None
            )

    def accept(self, value: CommandInput) -> tuple[CommandRecord, bool]:
        value = _input(value)
        snapshot_refs: tuple[tuple[str, str], ...] = ()

        def changed(
            current: CommandRow | None,
            draft: DraftRecord | None,
            usage: tuple[int, int],
            prior: CommandRow | None,
        ) -> CommandRow:
            nonlocal snapshot_refs
            if current is not None:
                self._same(current, value)
                self._decode(current)
                return current
            if prior is not None:
                self._summary(prior)
                raise CommandError("draft_already_submitted")
            if draft is None or draft["revision"] != value.draft_revision:
                raise CommandError("draft_conflict")
            submitted = decode_draft(self._scope, value.session_id, draft)
            submitted_turn = TurnInput(submitted.text, submitted.attachments, submitted.parts)
            if not submitted_turn.sendable:
                raise CommandError("empty_draft")
            if not claude_accepts(submitted_turn):
                raise CommandError("unsupported_attachment")
            snapshot_refs = tuple(
                (attachment.asset_id, f"attachment.{index}")
                for index, attachment in enumerate(submitted.attachments)
            )
            plaintext = submitted.text.encode("utf-8")
            if usage[0] >= MAX_COMMANDS or usage[1] + len(plaintext) > MAX_SCOPE_BYTES:
                raise CommandError("capacity")
            format_value = submitted_turn.wire_format
            # Format 1 keeps the released text-only body; 2 and 3 store the JSON envelope, whose
            # format digit the encryption context binds.
            payload = plaintext if format_value == 1 else submitted_turn.encode()
            cipher = protection.protect(payload, context=self._context(value, format_value))
            if not isinstance(cipher, bytes) or not 0 < len(cipher) <= MAX_CIPHERTEXT_BYTES:
                raise CommandError("protection")
            stamp = _now()
            return {
                **asdict(value),
                "request_hash": _request_hash(value),
                "owner": self._owner,
                "state": "accepted",
                "reason": "",
                "body": cipher,
                "format": format_value,
                "text_bytes": len(plaintext),
                "created_at": stamp,
                "updated_at": stamp,
            }

        with self._access():

            def refs(_draft: DraftRecord | None) -> tuple[tuple[str, str], ...]:
                return snapshot_refs

            row, created = self._store.accept_command(
                self._scope,
                value.command_id,
                value.session_id,
                value.draft_revision,
                value.source_draft_id,
                changed,
                asset_refs=refs,
            )
            return self._decode(row), created

    def get(self, command_id: str) -> CommandRecord:
        command_id = _uuid(command_id)
        with self._access():
            row = self._store.get_command(self._scope, command_id)
            if row is None:
                raise CommandError("not_found")
            return self._decode(row)

    def list(self, *, after: str | None = None, limit: int = 50) -> CommandPage:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise CommandError("invalid_page")
        cursor = _uuid(after, "invalid_page") if after is not None else ""
        with self._access():
            rows = self._store.list_commands(self._scope, cursor, limit + 1)
            summaries = [self._summary(row) for row in rows[:limit]]
            return CommandPage(summaries, summaries[-1].command_id if len(rows) > limit else None)

    def uncertain(self, since: str, limit: int) -> tuple[CommandSummary, ...]:
        """Sends an earlier launch left `unknown` (a send that failed, or one cut off while
        `accepted` or `dispatching`), newest first. `native_unconfirmed` rows are left out.
        Reads only."""
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise CommandError("invalid_page")
        _timestamp_page(since)
        found: list[CommandSummary] = []
        with self._access():
            after, seen = "", 0
            while seen < MAX_COMMANDS:
                rows = self._store.list_commands(self._scope, after, MAX_PAGE_SIZE)
                for row in rows:
                    summary = self._summary(row)
                    if (
                        row["owner"] != self._owner
                        and summary.state == "unknown"
                        and summary.reason != "native_unconfirmed"
                        and summary.updated_at >= since
                    ):
                        found.append(summary)
                seen += len(rows)
                if len(rows) < MAX_PAGE_SIZE:
                    break
                after = rows[-1]["command_id"]
        found.sort(key=lambda item: (item.updated_at, item.command_id), reverse=True)
        return tuple(found[:limit])

    def find_draft_command(self, session_id: str, draft_revision: int) -> CommandSummary:
        session_id = _uuid(session_id)
        if type(draft_revision) is not int or not 1 <= draft_revision <= MAX_REVISION:
            raise CommandError("invalid_command")
        with self._access():
            row = self._store.find_draft_command(
                self._scope, session_id, session_id, draft_revision
            )
            if row is None:
                raise CommandError("not_found")
            return self._summary(row)

    def mark_dispatching(self, command_id: str) -> CommandRecord:
        return self._transition(command_id, "dispatching", "")

    def finish(self, command_id: str, state: TerminalState, reason: str) -> CommandRecord:
        if (
            not isinstance(state, str)
            or state not in {"unknown", "not_dispatched"}
            or not isinstance(reason, str)
            or reason not in REASONS
        ):
            raise CommandError("invalid_command")
        return self._transition(command_id, state, reason)

    def _transition(self, command_id: str, state: CommandState, reason: str) -> CommandRecord:
        command_id = _uuid(command_id)

        def changed(row: CommandRow | None) -> CommandRow:
            if row is None:
                raise CommandError("not_found")
            self._decode(row)
            if row["owner"] != self._owner:
                raise CommandConflict()
            if state in {"unknown", "not_dispatched"} and (row["state"], row["reason"]) == (
                state,
                reason,
            ):
                return row
            if row["state"] not in {"accepted", "dispatching"} or (
                state == "dispatching" and row["state"] != "accepted"
            ):
                raise CommandConflict()
            return {**row, "state": state, "reason": reason, "updated_at": _now()}

        with self._access():
            return self._decode(self._store.update_command(self._scope, command_id, changed))
