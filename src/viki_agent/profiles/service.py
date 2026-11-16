"""Encrypted, immutable app profile revisions and transactional mutation receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from viki_agent import protection
from viki_agent.conversations.drafts import require_private_store
from viki_agent.profiles.mcp_config import McpConfigError, check_configuration
from viki_agent.profiles.profile_document import (
    ProfileDocument,
    ProfileDocumentError,
    parse_profile_document,
)
from viki_agent.storage import SessionStore

MAX_PROFILES = 64
MAX_REVISIONS = 256
MAX_TOTAL_BYTES = 32 << 20
MAX_RECEIPTS = 65536
MAX_INTEGER = (1 << 53) - 1


class ProfileError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _id(value: object) -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ProfileError("invalid_id")


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ProfileError("invalid_revision")
    return value


def _one(
    connection: sqlite3.Connection, sql: str, values: tuple[Any, ...]
) -> dict[str, Any] | None:
    cursor = connection.execute(sql, values)
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row, strict=True)) if row else None


def _context(profile_id: str, revision: int) -> bytes:
    return f"VIKI profile v1\0{profile_id}\0{revision}".encode("ascii")


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    if row["format"] != 1:
        raise ProfileError("newer_version")
    if not isinstance(row["body"], bytes) or not 1 <= len(row["body"]) <= 327680:
        raise ProfileError("corrupt")
    try:
        plain = protection.unprotect(
            row["body"], context=_context(_id(row["profile_id"]), _revision(row["revision"]))
        )
        if (
            len(plain) != row["body_bytes"]
            or hashlib.sha256(plain).hexdigest() != row["body_sha256"]
        ):
            raise ProfileError("corrupt")
        document = parse_profile_document(plain)
        if document.canonical != plain:
            raise ProfileError("corrupt")
        return json.loads(plain)
    except protection.ProtectionError:
        raise ProfileError("protection") from None
    except ValueError, TypeError, KeyError, ProfileDocumentError:
        raise ProfileError("corrupt") from None


def avatar_committed(connection: sqlite3.Connection, avatar_id: str) -> bool:
    """True when the avatar row and all five of its variants are committed."""
    return (
        connection.execute(
            "SELECT count(*) FROM avatars a JOIN avatar_variants v ON v.avatar_id=a.avatar_id "
            "WHERE a.avatar_id=?",
            (avatar_id,),
        ).fetchone()[0]
        == 5
    )


def _avatar_ref(
    connection: sqlite3.Connection, profile_id: str, revision: int, document: Mapping[str, Any]
) -> str | None:
    row = connection.execute(
        "SELECT avatar_id FROM profile_avatar_refs WHERE profile_id=? AND profile_revision=?",
        (profile_id, revision),
    ).fetchone()
    if row is None:
        return None  # A revision written before avatar references existed has no avatar.
    if row[0] != document["identity"]["avatar_asset_id"]:
        raise ProfileError("corrupt")
    return row[0]


def _mcp(document: ProfileDocument) -> ProfileDocument:
    """Refuse a write whose MCP configuration is outside the app's closed schema. Parsing stays
    tolerant, so a stored revision that no longer conforms still reads."""
    configuration = cast(Mapping[str, Any], document.value["tools"])["mcp_configuration"]
    if configuration is not None:
        try:
            check_configuration(configuration)
        except McpConfigError as error:
            raise ProfileDocumentError(error.code, error.field) from None
    return document


class ProfileService:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def get(self, profile_id: str, revision: int | None = None) -> dict[str, Any]:
        profile_id = _id(profile_id)
        if revision is not None:
            revision = _revision(revision)
        require_private_store(self.store)
        with self.store._connection() as connection:
            profile = _one(connection, "SELECT * FROM profiles WHERE profile_id=?", (profile_id,))
            if profile is None:
                raise ProfileError("not_found")
            row = _one(
                connection,
                "SELECT * FROM profile_revisions WHERE profile_id=? AND revision=?",
                (profile_id, revision or profile["current_revision"]),
            )
            if row is None:
                raise ProfileError("not_found")
            document = _decode(row)
            _avatar_ref(connection, profile_id, row["revision"], document)
        return {
            **profile,
            "archived": bool(profile["archived"]),
            "revision": row["revision"],
            "document": document,
        }

    def list(self, archived: bool = False) -> list[dict[str, Any]]:
        if type(archived) is not bool:
            raise ProfileError("invalid_input")
        require_private_store(self.store)
        with self.store._connection() as connection:
            cursor = connection.execute(
                "SELECT p.*,r.revision,r.format,r.body,r.body_bytes,r.body_sha256 "
                "FROM profiles p LEFT JOIN profile_revisions r "
                "ON r.profile_id=p.profile_id AND r.revision=p.current_revision "
                "WHERE p.archived=? ORDER BY p.created_at,p.profile_id LIMIT 65",
                (int(archived),),
            )
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        if len(rows) > MAX_PROFILES:
            raise ProfileError("corrupt")
        result = []
        for row in rows:
            if row["revision"] is None:
                raise ProfileError("corrupt")
            document = _decode(row)
            item = {
                key: row[key]
                for key in (
                    "profile_id",
                    "revision",
                    "current_revision",
                    "state_revision",
                    "created_at",
                    "updated_at",
                )
            }
            result.append(
                {
                    **item,
                    "archived": bool(row["archived"]),
                    "identity": document["identity"],
                    "runtime": document["runtime"],
                    "provenance": document["provenance"],
                }
            )
        return result

    def create(self, profile_id: str, command_id: str, document: object) -> dict[str, Any]:
        return self._change(
            "create", profile_id, command_id, _mcp(parse_profile_document(document))
        )

    def revise(
        self, profile_id: str, command_id: str, expected_revision: int, document: object
    ) -> dict[str, Any]:
        return self._change(
            "revise",
            profile_id,
            command_id,
            _mcp(parse_profile_document(document)),
            expected_revision=_revision(expected_revision),
        )

    def archive(
        self, profile_id: str, command_id: str, expected_state_revision: int, archived: bool
    ) -> dict[str, Any]:
        if type(archived) is not bool:
            raise ProfileError("invalid_input")
        return self._change(
            "archive",
            profile_id,
            command_id,
            None,
            expected_state_revision=_revision(expected_state_revision),
            archived=archived,
        )

    def duplicate(
        self,
        source_id: str,
        command_id: str,
        expected_revision: int,
        target_id: str,
        target_runtime: str,
    ) -> dict[str, Any]:
        source = self.get(source_id, _revision(expected_revision))
        document = source["document"]
        document["runtime"]["provider"] = target_runtime
        document["provenance"] = {
            "kind": "duplicate",
            "derived_from": {"profile_id": source_id, "revision": expected_revision},
            "native_source": None,
        }
        return self._change(
            "duplicate",
            target_id,
            command_id,
            _mcp(parse_profile_document(document)),
            source=(_id(source_id), expected_revision),
        )

    def compare(self, profile_id: str, left: int, right: int) -> dict[str, Any]:
        before = self.get(profile_id, _revision(left))["document"]
        after = self.get(profile_id, _revision(right))["document"]
        changes: list[dict[str, Any]] = []

        def visit(a: Any, b: Any, field: str) -> None:
            if isinstance(a, dict) and isinstance(b, dict):
                for key in sorted(a.keys() | b.keys()):
                    visit(a.get(key), b.get(key), f"{field}.{key}" if field else key)
            elif a != b:
                changes.append({"field": field, "before": a, "after": b})

        visit(before, after, "")
        return {"profile_id": profile_id, "left": left, "right": right, "changes": changes}

    def _change(
        self,
        operation: str,
        profile_id: str,
        command_id: str,
        document: ProfileDocument | None,
        *,
        expected_revision: int | None = None,
        expected_state_revision: int | None = None,
        archived: bool = False,
        source: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        profile_id, command_id = _id(profile_id), _id(command_id)
        require_private_store(self.store)
        request = json.dumps(
            [
                operation,
                profile_id,
                expected_revision,
                expected_state_revision,
                archived,
                source,
                document.canonical.decode() if document else None,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request.encode()).hexdigest()
        revision = (expected_revision + 1) if expected_revision is not None else 1
        if revision > MAX_INTEGER:
            raise ProfileError("capacity")
        cipher = b""
        if document:
            try:
                cipher = protection.protect(
                    document.canonical, context=_context(profile_id, revision)
                )
            except protection.ProtectionError:
                raise ProfileError("protection") from None
            if not 1 <= len(cipher) <= 327680:
                raise ProfileError("capacity")
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.store._connection(write=True) as connection:
            receipt = _one(
                connection, "SELECT * FROM profile_commands WHERE command_id=?", (command_id,)
            )
            if receipt:
                if (
                    receipt["operation"],
                    receipt["target_profile_id"],
                    receipt["request_hash"],
                ) != (operation, profile_id, request_hash):
                    raise ProfileError("command_conflict")
                if any(
                    type(receipt[key]) is not int or not 1 <= receipt[key] <= MAX_INTEGER
                    for key in ("result_revision", "result_state_revision")
                ):
                    raise ProfileError("corrupt")
                return {
                    "profile_id": profile_id,
                    "revision": receipt["result_revision"],
                    "state_revision": receipt["result_state_revision"],
                }
            if (
                connection.execute("SELECT count(*) FROM profile_commands").fetchone()[0]
                >= MAX_RECEIPTS
            ):
                raise ProfileError("capacity")
            current = _one(connection, "SELECT * FROM profiles WHERE profile_id=?", (profile_id,))
            state_revision = 1
            if operation in {"create", "duplicate"}:
                if current:
                    raise ProfileError("conflict")
                if (
                    connection.execute("SELECT count(*) FROM profiles").fetchone()[0]
                    >= MAX_PROFILES
                ):
                    raise ProfileError("capacity")
                if source:
                    original = connection.execute(
                        "SELECT 1 FROM profile_revisions WHERE profile_id=? AND revision=?", source
                    ).fetchone()
                    if original is None:
                        raise ProfileError("conflict")
                connection.execute(
                    "INSERT INTO profiles VALUES (?,?,?,0,?,?)", (profile_id, 1, 1, now, now)
                )
            else:
                if current is None:
                    raise ProfileError("not_found")
                state_revision = current["state_revision"]
                if operation == "archive":
                    if state_revision != expected_state_revision:
                        raise ProfileError("conflict")
                    if state_revision >= MAX_INTEGER:
                        raise ProfileError("capacity")
                    state_revision += 1
                    revision = current["current_revision"]
                    connection.execute(
                        "UPDATE profiles SET archived=?,state_revision=?,updated_at=? "
                        "WHERE profile_id=?",
                        (int(archived), state_revision, now, profile_id),
                    )
                else:
                    if current["current_revision"] != expected_revision or current["archived"]:
                        raise ProfileError("conflict")
                    current_format = connection.execute(
                        "SELECT format FROM profile_revisions WHERE profile_id=? AND revision=?",
                        (profile_id, expected_revision),
                    ).fetchone()
                    if current_format is None:
                        raise ProfileError("corrupt")
                    if current_format[0] != 1:
                        raise ProfileError("newer_version")
                    connection.execute(
                        "UPDATE profiles SET current_revision=?,updated_at=? WHERE profile_id=?",
                        (revision, now, profile_id),
                    )
            if document:
                identity = cast(Mapping[str, Any], document.value["identity"])
                avatar_id = identity["avatar_asset_id"]
                if avatar_id is not None and not avatar_committed(connection, avatar_id):
                    raise ProfileError("avatar_missing")
                count = connection.execute(
                    "SELECT count(*) FROM profile_revisions WHERE profile_id=?", (profile_id,)
                ).fetchone()[0]
                total = connection.execute(
                    "SELECT coalesce(sum(body_bytes),0) FROM profile_revisions"
                ).fetchone()[0]
                if count >= MAX_REVISIONS or total + len(document.canonical) > MAX_TOTAL_BYTES:
                    raise ProfileError("capacity")
                connection.execute(
                    "INSERT INTO profile_revisions VALUES (?,?,1,?,?,?,?)",
                    (
                        profile_id,
                        revision,
                        cipher,
                        len(document.canonical),
                        hashlib.sha256(document.canonical).hexdigest(),
                        now,
                    ),
                )
                if avatar_id is not None:
                    connection.execute(
                        "INSERT INTO profile_avatar_refs VALUES (?,?,?,?)",
                        (profile_id, revision, avatar_id, now),
                    )
            connection.execute(
                "INSERT INTO profile_commands VALUES (?,?,?,?,?,?,?)",
                (command_id, operation, profile_id, request_hash, revision, state_revision, now),
            )
        return {"profile_id": profile_id, "revision": revision, "state_revision": state_revision}
