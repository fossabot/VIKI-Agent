"""Encrypted unsent editor buffers. Saving a draft never accepts or dispatches a command."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from viki_agent import protection
from viki_agent.conversations.turn_parts import ExcerptPart
from viki_agent.state import AppState
from viki_agent.storage import DraftRecord, SessionStore, StateError
from viki_agent.turn_input import Attachment, TurnInput, TurnInputError, coerce
from viki_agent.workspaces import root_key

MAX_TEXT_BYTES = 1 << 20
MAX_CIPHERTEXT_BYTES = MAX_TEXT_BYTES + (64 << 10)
MAX_REVISION = (1 << 53) - 1
MAX_NONEMPTY_DRAFTS = 100
MAX_SCOPE_BYTES = 16 << 20
MAX_DRAFT_KEYS = 4096
MAX_PAGE_SIZE = 100

_MESSAGES = {
    "invalid_key": "Couldn't check the draft's save location.",
    "invalid_revision": "The draft's revision number isn't valid.",
    "invalid_text": "The draft has characters that can't be saved.",
    "invalid_attachment": "Check the attachment.",
    "invalid_page": "The draft list's paging isn't valid.",
    "too_large": "The draft is too large to save. Shorten the content, then save again.",
    "capacity": "The draft storage limit has been reached. Check your saved drafts.",
    "conflict": "The draft has been updated. Check the saved content.",
    "protection": (
        "Couldn't safely read or write the draft. Check the save location's access rights "
        "and " + protection.ACCOUNT_HINT + "."
    ),
    "corrupt": (
        "Couldn't read the draft's saved data. Keep the original file and check the recovery steps."
    ),
    "unsafe_path": "Couldn't check the draft's save location.",
    "unavailable": "Couldn't read or write the draft. Check the save location.",
}


class DraftError(RuntimeError):
    """A safe service/API error; caller-supplied content and storage exceptions stay private."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message if message is not None else _MESSAGES[code]
        super().__init__(self.message)


class DraftConflict(DraftError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__("conflict")


@dataclass(frozen=True)
class Draft:
    key: str
    revision: int
    text: str
    saved_at: str | None
    attachments: tuple[Attachment, ...] = ()
    parts: tuple[ExcerptPart, ...] = ()


@dataclass(frozen=True)
class DraftSummary:
    key: str
    revision: int
    saved_at: str
    bytes: int


@dataclass(frozen=True)
class DraftPage:
    drafts: list[DraftSummary]
    next_after: str | None


def _key(value: Any, *, code: str = "invalid_key") -> str:
    if value == "new":
        return value
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise DraftError(code)


def _metadata(record: DraftRecord) -> tuple[int, int, str]:
    revision, size, saved_at = record["revision"], record["text_bytes"], record["saved_at"]
    if (
        type(revision) is not int
        or not 1 <= revision <= MAX_REVISION
        or type(size) is not int
        or not 0 <= size <= MAX_TEXT_BYTES
        or not isinstance(saved_at, str)
        or len(saved_at) != 24
    ):
        raise DraftError("corrupt")
    try:
        canonical = datetime.fromisoformat(saved_at).isoformat(timespec="milliseconds")
    except ValueError:
        raise DraftError("corrupt") from None
    if canonical.replace("+00:00", "Z") != saved_at:
        raise DraftError("corrupt")
    return revision, size, saved_at


def draft_scope(vault: Path) -> str:
    try:
        canonical = root_key(vault.resolve()).encode("utf-8")
    except OSError, RuntimeError, UnicodeError:
        raise DraftError("unsafe_path") from None
    return hashlib.sha256(canonical).hexdigest()


def require_private_store(store: SessionStore) -> None:
    protection.require_private_directory(store.path.parent)
    protection.require_private_file(store.path)
    for suffix in ("-journal", "-wal", "-shm"):
        path = Path(f"{store.path}{suffix}")
        # A concurrent commit may delete the journal mid-check, or create it after the check
        # found none. An absent sidecar holds nothing to protect; a present one gets a second
        # look, and only a second failure on a file still present refuses.
        for last in (False, True):
            try:
                protection.require_private_file(path)
                break
            except protection.ProtectionError:
                if not _present(path):
                    break
                if last:
                    raise


def _present(path: Path) -> bool:
    # Only a fresh FileNotFoundError means absent: Path.exists() also answers False on a
    # denied stat, and an unreadable file must still refuse.
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def decode_draft(scope: str, key: str, record: DraftRecord) -> Draft:
    revision, size, saved_at = _metadata(record)
    ciphertext = record["body"]
    if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise DraftError("corrupt")
    format_value = record.get("format", 1)
    if format_value not in {1, 2, 3}:
        raise DraftError("corrupt")
    context = f"VIKI draft v{format_value}\0{scope}\0{key}\0{revision}".encode("ascii")
    plaintext = protection.unprotect(ciphertext, context=context)
    if len(plaintext) > MAX_CIPHERTEXT_BYTES:
        raise DraftError("corrupt")
    if format_value == 1:
        if len(plaintext) != size or len(plaintext) > MAX_TEXT_BYTES:
            raise DraftError("corrupt")
        try:
            text = plaintext.decode("utf-8")
        except UnicodeError:
            raise DraftError("corrupt") from None
        return Draft(key, revision, text, saved_at)
    try:
        turn = TurnInput.decode(plaintext)
    except TurnInputError:
        raise DraftError("corrupt") from None
    if turn.text_bytes != size:
        raise DraftError("corrupt")
    return Draft(key, revision, turn.text, saved_at, turn.attachments, turn.parts)


class DraftService:
    def __init__(self, state: AppState, vault: Path) -> None:
        self._store = state.store
        self._scope = draft_scope(vault)

    def _private(self) -> None:
        require_private_store(self._store)

    def _context(self, key: str, revision: int, format_value: int = 1) -> bytes:
        return f"VIKI draft v{format_value}\0{self._scope}\0{key}\0{revision}".encode("ascii")

    def _decode(self, key: str, record: DraftRecord) -> Draft:
        return decode_draft(self._scope, key, record)

    def get(self, key: str) -> Draft:
        key = _key(key)
        try:
            self._private()
            record = self._store.get_draft(self._scope, key)
            return self._decode(key, record) if record is not None else Draft(key, 0, "", None)
        except protection.ProtectionError:
            raise DraftError("protection") from None
        except StateError as exc:
            raise DraftError(exc.code, exc.message) from None

    def list(self, *, after: str | None = None, limit: int = 50) -> DraftPage:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise DraftError("invalid_page")
        cursor = _key(after, code="invalid_page") if after is not None else ""
        try:
            self._private()
            rows = self._store.list_drafts(self._scope, cursor, limit + 1)
            summaries: list[DraftSummary] = []
            for row in rows[:limit]:
                key = _key(row["key"], code="corrupt")
                revision, size, saved_at = _metadata(row)
                summaries.append(DraftSummary(key, revision, saved_at, size))
            return DraftPage(summaries, summaries[-1].key if len(rows) > limit else None)
        except protection.ProtectionError:
            raise DraftError("protection") from None
        except StateError as exc:
            raise DraftError(exc.code, exc.message) from None

    def save(
        self,
        key: str,
        expected_revision: int,
        text: str | TurnInput,
        attachments: tuple[Attachment, ...] | None = None,
        parts: tuple[ExcerptPart, ...] | None = None,
    ) -> Draft:
        key = _key(key)
        if type(expected_revision) is not int or not 0 <= expected_revision <= MAX_REVISION:
            raise DraftError("invalid_revision")
        typed = attachments is not None or parts is not None
        legacy = isinstance(text, str) and not typed
        if isinstance(text, str):
            try:
                raw_text = text.encode("utf-8")
            except UnicodeError:
                raise DraftError("invalid_text") from None
            if len(raw_text) > MAX_TEXT_BYTES:
                raise DraftError("too_large")
        try:
            if typed and not isinstance(text, str):
                raise TurnInputError("invalid text")
            turn = TurnInput(str(text), attachments or (), parts or ()) if typed else coerce(text)
        except TurnInputError as exc:
            raise DraftError(
                "invalid_text" if str(exc) == "invalid text" else "invalid_attachment"
            ) from None
        plaintext = turn.text.encode("utf-8")

        if not legacy:
            # v2 stores the canonical JSON envelope, whose JSON escaping can make control
            # characters much larger than the text byte count.  Keep the same encryption
            # headroom as the text-only format and report an input limit, never a protection
            # failure after an otherwise valid request has entered the transaction.
            encoded = turn.encode()
            if len(encoded) > MAX_TEXT_BYTES:
                raise DraftError("too_large")
            return self._save_input(key, expected_revision, turn, encoded)

        def changed(record: DraftRecord | None, usage: tuple[int, int, int]) -> DraftRecord | None:
            current = self._decode(key, record) if record is not None else Draft(key, 0, "", None)
            if expected_revision != current.revision:
                if (
                    expected_revision + 1 == current.revision
                    and turn.text == current.text
                    and not current.attachments
                    and not current.parts
                ):
                    # The previous save committed, but its response was lost. A revision that
                    # holds attachments or excerpts is never echoed as text-only: the editor
                    # would then send images or quotes the user has already removed.
                    return record
                raise DraftConflict(current.revision)
            if turn.text == current.text and not current.attachments and not current.parts:
                return record  # Includes the absent empty buffer; no row/revision is created.
            if current.revision == MAX_REVISION:
                raise DraftError("capacity")
            keys, nonempty, total_bytes = usage
            previous_size = len(current.text.encode("utf-8"))
            previous_nonempty = bool(previous_size or current.attachments)
            if (
                (record is None and keys >= MAX_DRAFT_KEYS)
                or nonempty - previous_nonempty + turn.sendable > MAX_NONEMPTY_DRAFTS
                or total_bytes - previous_size + len(plaintext) > MAX_SCOPE_BYTES
            ):
                raise DraftError("capacity")
            revision = current.revision + 1
            ciphertext = protection.protect(
                plaintext, context=self._context(key, revision, turn.wire_format)
            )
            if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
                raise DraftError("protection")
            return {
                "revision": revision,
                "body": ciphertext,
                "format": turn.wire_format,
                "text_bytes": len(plaintext),
                "saved_at": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            }

        try:
            self._private()
            record = self._store.update_draft(self._scope, key, changed)
            # No plaintext gets published until the transaction context has committed.
            return (
                Draft(key, record["revision"], turn.text, record["saved_at"])
                if record
                else Draft(key, 0, "", None)
            )
        except protection.ProtectionError:
            raise DraftError("protection") from None
        except StateError as exc:
            raise DraftError(exc.code, exc.message) from None

    def _save_input(
        self, key: str, expected_revision: int, turn: TurnInput, encoded: bytes
    ) -> Draft:
        refs = tuple(
            (item.asset_id, f"attachment.{index}") for index, item in enumerate(turn.attachments)
        )
        # A typed save stores the JSON envelope, so its row is never format 1 -- not even for a
        # turn that carries neither an attachment nor an excerpt.
        format_value = max(2, turn.wire_format)

        def changed(
            record: DraftRecord | None, usage: tuple[int, int, int], metadata: list[dict[str, Any]]
        ) -> DraftRecord | None:
            current = self._decode(key, record) if record is not None else Draft(key, 0, "", None)
            if len(metadata) != len(turn.attachments) or any(
                row["asset_id"] != item.asset_id
                or row["media_type"] != item.media_type
                or row["byte_size"] != item.byte_size
                or row["width"] != item.width
                or row["height"] != item.height
                for row, item in zip(metadata, turn.attachments, strict=True)
            ):
                raise DraftError("invalid_attachment")
            if expected_revision != current.revision:
                if expected_revision + 1 == current.revision and (
                    current.text == turn.text
                    and current.attachments == turn.attachments
                    and current.parts == turn.parts
                ):
                    return record
                raise DraftConflict(current.revision)
            if (
                current.text == turn.text
                and current.attachments == turn.attachments
                and current.parts == turn.parts
            ):
                return record
            if current.revision == MAX_REVISION:
                raise DraftError("capacity")
            keys, nonempty, total_bytes = usage
            previous_size = len(current.text.encode("utf-8"))
            previous_nonempty = bool(previous_size or current.attachments)
            if (
                (record is None and keys >= MAX_DRAFT_KEYS)
                or nonempty - previous_nonempty + turn.sendable > MAX_NONEMPTY_DRAFTS
                or total_bytes - previous_size + turn.text_bytes > MAX_SCOPE_BYTES
            ):
                raise DraftError("capacity")
            revision = current.revision + 1
            ciphertext = protection.protect(
                encoded, context=self._context(key, revision, format_value)
            )
            if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
                raise DraftError("protection")
            return {
                "revision": revision,
                "body": ciphertext,
                "format": format_value,
                "text_bytes": turn.text_bytes,
                "saved_at": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            }

        try:
            self._private()
            record = self._store.update_draft_input(self._scope, key, refs, changed)
            return (
                Draft(
                    key,
                    record["revision"],
                    turn.text,
                    record["saved_at"],
                    turn.attachments,
                    turn.parts,
                )
                if record
                else Draft(key, 0, "", None)
            )
        except protection.ProtectionError:
            raise DraftError("protection") from None
        except StateError as exc:
            raise DraftError(exc.code, exc.message) from None
