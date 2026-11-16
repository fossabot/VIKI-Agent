"""Encrypted, app-only conversation archives with self-contained media manifests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from anyio.to_thread import run_sync

from viki_agent import protection
from viki_agent.conversations.drafts import require_private_store
from viki_agent.media.assets import AssetError, DecodedImage
from viki_agent.storage import ConversationArchiveRow, SessionStore, StateError
from viki_agent.turn_input import MAX_TURN_ATTACHMENT_BYTES, Attachment, TurnInputError

MAX_ARCHIVE_BYTES = 8 << 20
MAX_ARCHIVE_CIPHERTEXT_BYTES = MAX_ARCHIVE_BYTES + (64 << 10)
MAX_ARCHIVES = 64
MAX_TURNS = 1000
MAX_MEDIA = 10
MAX_TURN_ID_BYTES = 160
MAX_TITLE_BYTES = 512
MAX_TEXT_BYTES = 1 << 20


class ArchiveError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ArchiveConflict(ArchiveError):
    def __init__(self) -> None:
        super().__init__("conflict")


@dataclass(frozen=True)
class ArchiveSummary:
    archive_id: str
    created_at: str
    byte_size: int


@dataclass(frozen=True)
class ConversationArchive:
    archive_id: str
    created_at: str
    byte_size: int
    archive: dict[str, object]


ImageInspector = Callable[[bytes], Awaitable[DecodedImage]]


def _uuid(value: Any) -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ArchiveError("invalid_archive")


def _utf8(value: Any, *, limit: int, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ArchiveError("invalid_archive")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ArchiveError("invalid_archive") from None
    if size > limit:
        raise ArchiveError("invalid_archive")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 24:
        raise ArchiveError("invalid_archive")
    try:
        canonical = datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    except ValueError:
        raise ArchiveError("invalid_archive") from None
    if canonical.replace("+00:00", "Z") != value:
        raise ArchiveError("invalid_archive")
    return value


def _normalise(raw: Any) -> tuple[dict[str, object], dict[str, bytes]]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "format_version",
        "kind",
        "read_only",
        "session",
        "turns",
        "media",
    }:
        raise ArchiveError("invalid_archive")
    if (
        type(raw["format_version"]) is not int
        or raw["format_version"] != 1
        or raw["kind"] != "viki-conversation-archive"
        or raw["read_only"] is not True
    ):
        raise ArchiveError("invalid_archive")
    session = raw["session"]
    if not isinstance(session, Mapping) or set(session) != {"session_id", "title"}:
        raise ArchiveError("invalid_archive")
    normal_session = {
        "session_id": _uuid(session["session_id"]),
        "title": _utf8(session["title"], limit=MAX_TITLE_BYTES, empty=True),
    }

    values = raw["turns"]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_TURNS:
        raise ArchiveError("invalid_archive")
    turns: list[dict[str, object]] = []
    referenced: dict[str, Attachment] = {}
    identities: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, Mapping) or set(value) not in (
            {"turn_id", "role", "text"},
            {"turn_id", "role", "text", "created_at"},
            {"turn_id", "role", "text", "attachments"},
            {"turn_id", "role", "text", "created_at", "attachments"},
        ):
            raise ArchiveError("invalid_archive")
        turn_id = _utf8(value["turn_id"], limit=MAX_TURN_ID_BYTES)
        role = value["role"]
        if (
            not isinstance(role, str)
            or role not in {"user", "assistant"}
            or (turn_id, role) in identities
        ):
            raise ArchiveError("invalid_archive")
        identities.add((turn_id, role))
        text = _utf8(value["text"], limit=MAX_TEXT_BYTES, empty=True)
        attachments: list[Attachment] = []
        if "attachments" in value:
            raw_attachments = value["attachments"]
            if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
                raise ArchiveError("invalid_archive")
            try:
                attachments = [Attachment(**item) for item in raw_attachments]
            except TypeError, TurnInputError:
                raise ArchiveError("invalid_archive") from None
            if len({item.asset_id for item in attachments}) != len(attachments):
                raise ArchiveError("invalid_archive")
            if sum(item.byte_size for item in attachments) > MAX_TURN_ATTACHMENT_BYTES:
                raise ArchiveError("invalid_archive")
        if not text and not attachments:
            raise ArchiveError("invalid_archive")
        turn: dict[str, object] = {"turn_id": turn_id, "role": role, "text": text}
        if "created_at" in value:
            turn["created_at"] = _timestamp(value["created_at"])
        if attachments:
            turn["attachments"] = [item.data() for item in attachments]
        turns.append(turn)
        for item in attachments:
            prior = referenced.setdefault(item.asset_id, item)
            if prior != item:
                raise ArchiveError("invalid_archive")

    media_values = raw["media"]
    if not isinstance(media_values, list) or len(media_values) > MAX_MEDIA:
        raise ArchiveError("invalid_archive")
    media: list[dict[str, object]] = []
    decoded: dict[str, bytes] = {}
    seen: set[str] = set()
    for value in media_values:
        if not isinstance(value, Mapping) or set(value) not in (
            {"asset_id", "unavailable"},
            {"asset_id", "sha256", "base64"},
        ):
            raise ArchiveError("invalid_archive")
        asset_id = _uuid(value["asset_id"])
        if asset_id in seen:
            raise ArchiveError("invalid_archive")
        seen.add(asset_id)
        if "unavailable" in value:
            if value["unavailable"] is not True:
                raise ArchiveError("invalid_archive")
            media.append({"asset_id": asset_id, "unavailable": True})
            continue
        sha256 = value["sha256"]
        encoded = value["base64"]
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or not isinstance(encoded, str)
            or len(encoded) > MAX_ARCHIVE_BYTES
        ):
            raise ArchiveError("invalid_archive")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError, binascii.Error:
            raise ArchiveError("invalid_archive") from None
        if (
            not data
            or base64.b64encode(data).decode("ascii") != encoded
            or hashlib.sha256(data).hexdigest() != sha256
        ):
            raise ArchiveError("invalid_archive")
        decoded[asset_id] = data
        media.append({"asset_id": asset_id, "sha256": sha256, "base64": encoded})
    if seen != set(referenced):
        raise ArchiveError("invalid_archive")
    media.sort(key=lambda value: str(value["asset_id"]))
    archive: dict[str, object] = {
        "format_version": 1,
        "kind": "viki-conversation-archive",
        "read_only": True,
        "session": normal_session,
        "turns": turns,
        "media": media,
    }
    return archive, decoded


def _encode(archive: dict[str, object]) -> bytes:
    value = json.dumps(archive, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if not 1 <= len(value) <= MAX_ARCHIVE_BYTES:
        raise ArchiveError("too_large")
    return value


class ConversationArchiveService:
    def __init__(self, store: SessionStore, scope: str, inspect_image: ImageInspector) -> None:
        if len(scope) != 64 or any(char not in "0123456789abcdef" for char in scope):
            raise ArchiveError("invalid_archive")
        self._store = store
        self._scope = scope
        self._inspect_image = inspect_image

    def _context(self, archive_id: str) -> bytes:
        return f"VIKI conversation archive v1\0{self._scope}\0{archive_id}".encode("ascii")

    async def _store_call[T](self, call: Callable[[], T]) -> T:
        def checked() -> T:
            try:
                require_private_store(self._store)
                return call()
            except protection.ProtectionError:
                raise ArchiveError("protection") from None
            except StateError as exc:
                raise ArchiveError(exc.code) from None

        return await run_sync(checked)

    def _decode(self, row: ConversationArchiveRow) -> ConversationArchive:
        archive_id = _uuid(row.get("archive_id"))
        created_at = _timestamp(row.get("created_at"))
        size = row.get("byte_size")
        cipher = row.get("body")
        if (
            type(size) is not int
            or not 1 <= size <= MAX_ARCHIVE_BYTES
            or not isinstance(cipher, bytes)
            or not 1 <= len(cipher) <= MAX_ARCHIVE_CIPHERTEXT_BYTES
        ):
            raise ArchiveError("corrupt")
        try:
            plaintext = protection.unprotect(cipher, context=self._context(archive_id))
            raw = json.loads(plaintext.decode("utf-8"))
            archive, _ = _normalise(raw)
            canonical = _encode(archive)
        except protection.ProtectionError, UnicodeError, ValueError, RecursionError, ArchiveError:
            raise ArchiveError("corrupt") from None
        if len(canonical) != size or canonical != plaintext:
            raise ArchiveError("corrupt")
        return ConversationArchive(archive_id, created_at, size, archive)

    async def create(self, archive_id: str, raw: Any) -> tuple[ConversationArchive, bool]:
        archive_id = _uuid(archive_id)
        archive, media = await run_sync(_normalise, raw)
        plaintext = await run_sync(_encode, archive)
        attachments = {
            item["asset_id"]: item
            for turn in archive["turns"]  # type: ignore[union-attr]
            for item in turn.get("attachments", [])  # type: ignore[union-attr]
        }
        for asset_id, data in media.items():
            try:
                inspected = await self._inspect_image(data)
            except AssetError as exc:
                if exc.code == "decode_failed":
                    raise ArchiveError("invalid_media") from None
                raise ArchiveError("unavailable") from None
            expected = attachments[asset_id]
            if (
                inspected.media_type != expected["media_type"]
                or inspected.width != expected["width"]
                or inspected.height != expected["height"]
                or len(data) != expected["byte_size"]
            ):
                raise ArchiveError("invalid_media")
        existing = await self._store_call(
            lambda: self._store.get_conversation_archive(self._scope, archive_id)
        )
        if existing is not None:
            record = await run_sync(self._decode, existing)
            if await run_sync(_encode, record.archive) != plaintext:
                raise ArchiveConflict()
            return record, False
        try:
            cipher = await run_sync(
                lambda: protection.protect(plaintext, context=self._context(archive_id))
            )
        except protection.ProtectionError:
            raise ArchiveError("protection") from None
        if not 1 <= len(cipher) <= MAX_ARCHIVE_CIPHERTEXT_BYTES:
            raise ArchiveError("protection")
        created_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        row: ConversationArchiveRow = {
            "archive_id": archive_id,
            "created_at": created_at,
            "body": cipher,
            "byte_size": len(plaintext),
        }
        try:
            await self._store_call(
                lambda: self._store.insert_conversation_archive(self._scope, row)
            )
        except ArchiveError as exc:
            if exc.code not in {"invalid_input", "capacity"}:
                raise
            winner = await self._store_call(
                lambda: self._store.get_conversation_archive(self._scope, archive_id)
            )
            if winner is not None:
                winner_record = await run_sync(self._decode, winner)
                if await run_sync(_encode, winner_record.archive) == plaintext:
                    return winner_record, False
            if exc.code == "capacity":
                raise
            raise ArchiveConflict() from None
        return ConversationArchive(archive_id, created_at, len(plaintext), archive), True

    async def list(self) -> list[ArchiveSummary]:
        rows = await self._store_call(lambda: self._store.list_conversation_archives(self._scope))
        result: list[ArchiveSummary] = []
        for row in rows:
            size = row.get("byte_size")
            if type(size) is not int or not 1 <= size <= MAX_ARCHIVE_BYTES:
                raise ArchiveError("corrupt")
            result.append(
                ArchiveSummary(
                    _uuid(row.get("archive_id")), _timestamp(row.get("created_at")), size
                )
            )
        return result

    async def get(self, archive_id: str) -> ConversationArchive:
        archive_id = _uuid(archive_id)
        row = await self._store_call(
            lambda: self._store.get_conversation_archive(self._scope, archive_id)
        )
        if row is None:
            raise ArchiveError("not_found")
        return await run_sync(self._decode, row)

    async def remove(self, archive_id: str) -> bool:
        archive_id = _uuid(archive_id)
        return await self._store_call(
            lambda: self._store.remove_conversation_archive(self._scope, archive_id)
        )
