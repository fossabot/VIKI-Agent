"""Sealed model connections in the app settings document; no provider-home access."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit

from viki_agent import protection
from viki_agent.api_json import unique_object

if TYPE_CHECKING:
    from viki_agent.app.settings import SettingsService

KEY = "chat.llm_connections"
PREFIX = "viki-llm/"
MAX_CONNECTIONS = 8
MODES = ("chat_completions", "responses", "anthropic")
ApiMode = Literal["chat_completions", "responses", "anthropic"]
_CONTEXT = b"VIKI model connections v1\x00"
_MODEL = re.compile(r"[^\s\x00-\x1f\x7f-\x9f]{1,512}")


class ConnectionError(Exception):
    def __init__(self, code: str = "invalid_connection") -> None:
        self.code = code
        super().__init__(code)


def model_ref(connection_id: str, model: str) -> str:
    return f"{PREFIX}{connection_id}/{model}"


def split_model(value: str | None) -> tuple[str, str] | None:
    if value is None or not value.startswith(PREFIX):
        return None
    connection_id, separator, model = value[len(PREFIX) :].partition("/")
    try:
        valid_id = str(uuid.UUID(connection_id)) == connection_id
    except ValueError:
        valid_id = False
    if not separator or not valid_id or _MODEL.fullmatch(model) is None:
        raise ConnectionError()
    return connection_id, model


def validate_model(value: object) -> str:
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None or value.startswith(PREFIX):
        raise ConnectionError()
    try:
        if len(value.encode("utf-8")) > 978:
            raise ConnectionError()
    except UnicodeError:
        raise ConnectionError() from None
    return value


def validate_url(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ConnectionError()
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value:
        raise ConnectionError()
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("https", "http")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.port == 0
        ):
            raise ConnectionError()
        if parts.scheme == "http":
            loopback = parts.hostname == "localhost"
            with contextlib.suppress(ValueError):
                loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
            if not loopback:
                raise ConnectionError("https_required")
    except ValueError:
        raise ConnectionError() from None
    return value.rstrip("/")


@dataclass(frozen=True)
class Connection:
    id: str
    name: str
    base_url: str
    api_mode: ApiMode
    model: str
    api_key: str = field(default="", repr=False)

    @property
    def ref(self) -> str:
        return model_ref(self.id, self.model)

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
            "model": self.model,
            "model_ref": self.ref,
            "has_api_key": bool(self.api_key),
        }

    def url(self, path: str) -> str:
        base = self.base_url
        if not urlsplit(base).path:
            base += "/v1"
        return f"{base}/{path}"


def parse_connection(value: object, *, connection_id: str | None = None) -> Connection:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "base_url",
        "api_mode",
        "model",
        "api_key",
    }:
        raise ConnectionError()
    name, mode, model, key = (
        value.get("name"),
        value.get("api_mode"),
        value.get("model"),
        value.get("api_key"),
    )
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 80
        or any(ord(char) < 32 for char in name)
        or mode not in MODES
        or not isinstance(key, str)
        or len(key) > 4096
        or any(not 33 <= ord(char) <= 126 for char in key)
    ):
        raise ConnectionError()
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise ConnectionError() from None
    return Connection(
        connection_id or str(uuid.uuid4()),
        name.strip(),
        validate_url(value.get("base_url")),
        cast(ApiMode, mode),
        validate_model(model),
        key,
    )


class Connections:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings

    def read(self) -> tuple[int, list[Connection]]:
        document = self.settings.import_snapshot()
        if document.read_only:
            raise ConnectionError("read_only")
        value = (document.values or {}).get(KEY)
        if value is None:
            return document.revision, []
        try:
            if not isinstance(value, str) or len(value) > 60000:
                raise ValueError()
            plain = protection.unprotect(base64.b64decode(value, validate=True), context=_CONTEXT)
            rows = json.loads(plain, object_pairs_hook=unique_object)
            if not isinstance(rows, list) or len(rows) > MAX_CONNECTIONS:
                raise ValueError()
            result = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise ValueError()
                connection_id = row.pop("id")
                if str(uuid.UUID(connection_id)) != connection_id:
                    raise ValueError()
                result.append(parse_connection(row, connection_id=connection_id))
            if len({row.id for row in result}) != len(result):
                raise ValueError()
            return document.revision, result
        except protection.ProtectionError, ValueError, TypeError, RecursionError:
            raise ConnectionError("connections_unavailable") from None

    def resolve(self, ref: str) -> Connection:
        route = split_model(ref)
        if route is None:
            raise ConnectionError()
        _, rows = self.read()
        for row in rows:
            if row.id == route[0] and row.model == route[1]:
                return row
        raise ConnectionError("connection_missing")

    def write(self, revision: int, rows: list[Connection], *, clear_default: bool = False) -> None:
        if len(rows) > MAX_CONNECTIONS or len({row.id for row in rows}) != len(rows):
            raise ConnectionError("connection_limit")
        try:
            plain = json.dumps([asdict(row) for row in rows], ensure_ascii=True).encode("ascii")
            sealed = base64.b64encode(protection.protect(plain, context=_CONTEXT)).decode("ascii")
        except protection.ProtectionError:
            raise ConnectionError("connections_unavailable") from None
        if len(sealed) > 60000:
            raise ConnectionError("connection_limit")
        changes: dict[str, object] = {KEY: sealed if rows else None}
        if clear_default:
            changes["chat.default_model"] = None
        self.settings.patch(str(uuid.uuid4()), revision, changes)
