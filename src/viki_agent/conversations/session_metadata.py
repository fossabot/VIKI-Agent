"""Conversation labels owned by the app; none of these operations touches native history."""

from __future__ import annotations

import uuid
from typing import Any

from viki_agent.conversations.drafts import require_private_store
from viki_agent.storage import SessionStore

COLORS = frozenset({"blue", "green", "orange", "purple", "red", "slate"})


class SessionMetadataError(ValueError):
    def __init__(self, code: str, revision: int | None = None) -> None:
        self.code = code
        self.revision = revision
        super().__init__(code)


def session_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise SessionMetadataError("invalid_input")
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise SessionMetadataError("invalid_input")


class SessionMetadataService:
    def __init__(self, store: SessionStore, scope: str) -> None:
        self.store = store
        self.scope = scope

    def list(self) -> list[dict[str, Any]]:
        require_private_store(self.store)
        return [self._wire(row) for row in self.store.list_session_metadata(self.scope)]

    @staticmethod
    def _wire(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "pinned": bool(row["pinned"]),
            "archived": bool(row["archived"]),
            "unread": bool(row["unread"]),
        }

    def patch(self, sid: str, expected_revision: Any, patch: Any) -> dict[str, Any]:
        sid = session_id(sid)
        if (
            type(expected_revision) is not int
            or not 0 <= expected_revision < (1 << 53) - 1
            or not isinstance(patch, dict)
            or not patch
            or set(patch) - {"title", "pinned", "archived", "unread", "color"}
        ):
            raise SessionMetadataError("invalid_input")
        if "title" in patch:
            title = patch["title"]
            if (
                not isinstance(title, str)
                or len(title) > 256
                or any(ord(character) < 32 for character in title)
            ):
                raise SessionMetadataError("invalid_input")
            try:
                title.encode("utf-8")
            except UnicodeError:
                raise SessionMetadataError("invalid_input") from None
        if any(
            type(patch[key]) is not bool for key in ("pinned", "archived", "unread") if key in patch
        ):
            raise SessionMetadataError("invalid_input")
        color = patch.get("color")
        if color is not None and (not isinstance(color, str) or color not in COLORS):
            raise SessionMetadataError("invalid_input")

        def change(row: dict[str, Any] | None) -> dict[str, Any]:
            current = row or {
                "session_id": sid,
                "title": "",
                "pinned": False,
                "archived": False,
                "unread": False,
                "color": None,
                "revision": 0,
            }
            identical = all(current[key] == value for key, value in patch.items())
            if expected_revision != current["revision"]:
                if expected_revision + 1 == current["revision"] and identical:
                    return current
                raise SessionMetadataError("conflict", current["revision"])
            if identical and row is not None:
                return current
            return {**current, **patch, "revision": current["revision"] + 1}

        require_private_store(self.store)
        return self._wire(self.store.update_session_metadata(self.scope, sid, change))

    def delete(self, sid: str, expected_revision: Any) -> bool:
        sid = session_id(sid)
        if type(expected_revision) is not int or not 0 <= expected_revision < (1 << 53) - 1:
            raise SessionMetadataError("invalid_input")

        def guard(row: dict[str, Any] | None) -> None:
            if row is not None and row["revision"] != expected_revision:
                raise SessionMetadataError("conflict", row["revision"])

        require_private_store(self.store)
        return self.store.delete_session_metadata(self.scope, sid, guard)
