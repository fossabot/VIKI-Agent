"""Saved Bot-thread membership and send references; conversation bodies stay in their sources."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from viki_agent import protection
from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.messaging.bots import BotError, _id, _revision
from viki_agent.storage import SessionStore
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MAX_THREADS = 20
MAX_BODY = 65536
SEATS = {"chief", "ops", "checker", "research", "writer", "desk"}


def _keys(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BotError("invalid_input")
    return value


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise BotError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BotError("invalid_input") from None
    if any(ord(char) < 32 for char in value):
        raise BotError("invalid_input")
    return value


def thread_state(value: object) -> dict[str, Any]:
    raw = _keys(value, {"title", "members", "messages", "phase"})
    _text(raw["title"], 128)
    if not isinstance(raw["phase"], str) or raw["phase"] not in (
        "idle",
        "running",
        "paused",
        "done",
    ):
        raise BotError("invalid_input")
    members, messages = raw["members"], raw["messages"]
    if not isinstance(members, list) or not 1 <= len(members) <= 64:
        raise BotError("invalid_input")
    if not isinstance(messages, list) or len(messages) > 200:
        raise BotError("invalid_input")
    known: set[str] = set()
    session_ids: set[str] = set()
    for value in members:
        member = _keys(value, {"key", "bot_id", "profile_id", "session_id", "enabled"})
        key = member["key"]
        if not isinstance(key, str):
            raise BotError("invalid_input")
        if key not in SEATS:
            _id(key)
        if key in known or type(member["enabled"]) is not bool:
            raise BotError("invalid_input")
        known.add(key)
        for field in ("bot_id", "profile_id", "session_id"):
            if member[field] is not None:
                _id(member[field])
        if member["bot_id"] is not None and member["profile_id"] is None:
            raise BotError("invalid_input")
        if member["session_id"] is not None:
            if member["session_id"] in session_ids or member["profile_id"] is None:
                raise BotError("invalid_input")
            session_ids.add(member["session_id"])
    ids: set[str] = set()
    for value in messages:
        message = _keys(value, {"id", "at", "member_key", "session_id", "command_id"})
        identity = _id(message["id"])
        if identity in ids:
            raise BotError("invalid_input")
        ids.add(identity)
        try:
            _text(message["at"], 24)
            parse_stamp(message["at"])
        except TypeError, ValueError:
            raise BotError("invalid_input") from None
        _id(message["command_id"])
        if message["member_key"] is not None and (
            not isinstance(message["member_key"], str) or message["member_key"] not in known
        ):
            raise BotError("invalid_input")
        _id(message["session_id"])
    return raw


class BotThreadService:
    def __init__(self, store: SessionStore, workspaces: WorkspaceService) -> None:
        self.store, self.workspaces = store, workspaces

    @staticmethod
    def _context(workspace_id: str, thread_id: str, revision: int) -> bytes:
        return f"viki bot thread v1\0{workspace_id}\0{thread_id}\0{revision}".encode("ascii")

    def _workspace(self, workspace_id: str) -> str:
        _id(workspace_id)
        require_private_store(self.store)
        try:
            workspace = self.workspaces.get(workspace_id)
        except WorkspaceError:
            raise BotError("workspace_missing") from None
        if workspace.kind != "vault" or not workspace.persistent:
            raise BotError("workspace_unavailable")
        return hashlib.sha256(workspace.root.key.encode("utf-8")).hexdigest()

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            revision = _revision(row["revision"])
            plain = protection.unprotect(
                row["body"], context=self._context(row["workspace_id"], row["thread_id"], revision)
            )
            if (
                len(plain) != row["body_bytes"]
                or hashlib.sha256(plain).hexdigest() != row["body_hash"]
            ):
                raise BotError("corrupt")
            state = thread_state(json.loads(plain))
        except ValueError, TypeError, UnicodeError:
            raise BotError("corrupt") from None
        return {
            "thread_id": row["thread_id"],
            "workspace_id": row["workspace_id"],
            "revision": revision,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "state": state,
        }

    def list(self, workspace_id: str) -> dict[str, Any]:
        self._workspace(workspace_id)
        with self.store._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM bot_threads WHERE workspace_id=? "
                "ORDER BY created_at,thread_id LIMIT 21",
                (workspace_id,),
            ).fetchall()
            if len(rows) > MAX_THREADS:
                raise BotError("corrupt")
            return {"threads": [self._decode(row) for row in rows]}

    def save(
        self, workspace_id: str, thread_id: str, expected_revision: object, state: object
    ) -> dict[str, Any]:
        scope = self._workspace(workspace_id)
        _id(thread_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < 9007199254740991:
            raise BotError("invalid_input")
        raw = thread_state(state)
        plain = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(plain) > MAX_BODY:
            raise BotError("invalid_input")
        digest = hashlib.sha256(plain).hexdigest()
        revision = expected_revision + 1
        cipher = protection.protect(plain, context=self._context(workspace_id, thread_id, revision))
        now = stamp(datetime.now(UTC))
        with self.store._connection(write=True) as connection:
            connection.row_factory = sqlite3.Row
            members = {member["key"]: member for member in raw["members"]}
            for member in raw["members"]:
                if member["session_id"] is None:
                    continue
                binding = connection.execute(
                    "SELECT b.profile_id,b.workspace_id,owner.bot_id "
                    "FROM session_profile_bindings b "
                    "LEFT JOIN session_bot_memory owner ON owner.session_id=b.session_id "
                    "WHERE b.session_id=?",
                    (member["session_id"],),
                ).fetchone()
                if (
                    binding is None
                    or binding["workspace_id"] != workspace_id
                    or binding["profile_id"] != member["profile_id"]
                    or (binding["bot_id"] is not None and binding["bot_id"] != member["bot_id"])
                ):
                    raise BotError("invalid_input")
            for message in raw["messages"]:
                member = members.get(message["member_key"])
                source = connection.execute(
                    "SELECT b.profile_id,b.workspace_id,owner.bot_id FROM commands c "
                    "JOIN session_profile_bindings b ON b.session_id=c.session_id "
                    "LEFT JOIN session_bot_memory owner ON owner.session_id=b.session_id "
                    "WHERE c.scope=? AND c.command_id=? AND c.session_id=?",
                    (scope, message["command_id"], message["session_id"]),
                ).fetchone()
                if (
                    source is None
                    or source["workspace_id"] != workspace_id
                    or (
                        member is not None
                        and (
                            source["profile_id"] != member["profile_id"]
                            or (
                                source["bot_id"] is not None
                                and source["bot_id"] != member["bot_id"]
                            )
                        )
                    )
                ):
                    raise BotError("invalid_input")
            row = connection.execute(
                "SELECT * FROM bot_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is not None:
                if row["workspace_id"] != workspace_id:
                    raise BotError("not_found")
                if row["revision"] == revision and row["body_hash"] == digest:
                    return self._decode(row)
                if row["revision"] != expected_revision:
                    raise BotError("conflict")
                connection.execute(
                    "UPDATE bot_threads SET revision=?,body=?,body_bytes=?,"
                    "body_hash=?,updated_at=? "
                    "WHERE thread_id=?",
                    (revision, cipher, len(plain), digest, now, thread_id),
                )
            else:
                if expected_revision != 0:
                    raise BotError("not_found")
                if (
                    connection.execute("SELECT count(*) FROM bot_threads").fetchone()[0]
                    >= MAX_THREADS
                ):
                    raise BotError("capacity")
                connection.execute(
                    "INSERT INTO bot_threads VALUES (?,?,?,?,?,?,?,?)",
                    (thread_id, workspace_id, revision, cipher, len(plain), digest, now, now),
                )
            saved = connection.execute(
                "SELECT * FROM bot_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if saved is None:
                raise BotError("unavailable")
            sources = {
                member["session_id"]
                for member in raw["members"]
                if member["session_id"] is not None
            }
            sources.update(message["session_id"] for message in raw["messages"])
            connection.execute("DELETE FROM bot_thread_sessions WHERE thread_id=?", (thread_id,))
            connection.executemany(
                "INSERT INTO bot_thread_sessions VALUES (?,?)",
                [(thread_id, source) for source in sorted(sources)],
            )
            return self._decode(saved)

    def remove(
        self, workspace_id: str, thread_id: str, expected_revision: object
    ) -> dict[str, Any]:
        self._workspace(workspace_id)
        _id(thread_id)
        revision = _revision(expected_revision)
        with self.store._connection(write=True) as connection:
            changed = connection.execute(
                "DELETE FROM bot_threads WHERE workspace_id=? AND thread_id=? AND revision=?",
                (workspace_id, thread_id, revision),
            ).rowcount
            if not changed:
                raise BotError("conflict")
            # Mark the group's copies released before its membership, their owner, goes.
            connection.execute(
                "UPDATE bot_code_copies SET state='released' WHERE thread_id=? AND state='bound'",
                (thread_id,),
            )
            connection.execute("DELETE FROM bot_thread_sessions WHERE thread_id=?", (thread_id,))
        return {"removed": True}
