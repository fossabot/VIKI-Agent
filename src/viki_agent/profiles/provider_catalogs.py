"""Encrypted, bounded provider capability observations with fail-closed evaluation DTOs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from viki_agent import protection
from viki_agent.conversations.drafts import require_private_store
from viki_agent.profiles.profile_document import Capability, CapabilityCatalog
from viki_agent.profiles.service import ProfileError, _one
from viki_agent.storage import SessionStore

MAX_CATALOGS = 8
MAX_CAPABILITIES = 64
MAX_MODELS = 128
MAX_PLAIN_BYTES = 262_144
MAX_CIPHER_BYTES = 327_680
MAX_DETAIL_BYTES = 4_096
_KEY = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$")
_STATUSES = frozenset({"supported", "unsupported", "unverified"})
_CAPABILITY_KEYS = frozenset({"id", "status", "reason", "evidence"})
_MODEL_KEYS = frozenset({"value", "label", "description"})
_DOCUMENT_KEYS = frozenset(
    {"provider", "adapter_version", "native_version", "observed_at", "capabilities", "models"}
)
_RETURN_KEYS = _DOCUMENT_KEYS | {"catalog_key", "body_sha256"}


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProfileError("invalid_catalog")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProfileError("invalid_catalog") from None
    if size > maximum or (not empty and size == 0):
        raise ProfileError("invalid_catalog")
    return value


def _version(value: object, maximum: int) -> str:
    return _text(value, maximum)


def _timestamp(value: object) -> str:
    text = _text(value, 24)
    try:
        if (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            != text
        ):
            raise ValueError
    except ValueError:
        raise ProfileError("invalid_catalog") from None
    return text


def _capabilities(value: object) -> list[dict[str, str | None]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes)
        or len(value) > MAX_CAPABILITIES
    ):
        raise ProfileError("invalid_catalog")
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _CAPABILITY_KEYS:
            raise ProfileError("invalid_catalog")
        capability_id = _text(item["id"], 256)
        status = item["status"]
        reason, evidence = item["reason"], item["evidence"]
        if (
            not _CODE.fullmatch(capability_id)
            or not isinstance(status, str)
            or status not in _STATUSES
            or any(
                detail is not None and (not isinstance(detail, str) or not _CODE.fullmatch(detail))
                for detail in (reason, evidence)
            )
            or capability_id in seen
        ):
            raise ProfileError("invalid_catalog")
        seen.add(capability_id)
        result.append(
            {"id": capability_id, "status": str(status), "reason": reason, "evidence": evidence}
        )
    return result


def _models(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or len(value) > MAX_MODELS:
        raise ProfileError("invalid_catalog")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _MODEL_KEYS:
            raise ProfileError("invalid_catalog")
        model = {
            "value": _text(item["value"], MAX_DETAIL_BYTES),
            "label": _text(item["label"], MAX_DETAIL_BYTES),
            "description": _text(item["description"], MAX_DETAIL_BYTES, empty=True),
        }
        if model["value"] in seen:
            raise ProfileError("invalid_catalog")
        seen.add(model["value"])
        result.append(model)
    return result


def _document(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DOCUMENT_KEYS:
        raise ProfileError("invalid_catalog")
    return {
        "provider": _version(value["provider"], 32),
        "adapter_version": _version(value["adapter_version"], 64),
        "native_version": _version(value["native_version"], 64),
        "observed_at": _timestamp(value["observed_at"]),
        "capabilities": _capabilities(value["capabilities"]),
        "models": _models(value["models"]),
    }


def _canonical(value: Mapping[str, Any]) -> bytes:
    plain = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    if not 2 <= len(plain) <= MAX_PLAIN_BYTES:
        raise ProfileError("capacity")
    return plain


def _content_key(value: Mapping[str, Any]) -> str:
    stable = {key: item for key, item in value.items() if key != "observed_at"}
    return hashlib.sha256(_canonical(stable)).hexdigest()


def _context(key: str) -> bytes:
    return f"VIKI provider catalog v1\0{key}".encode("ascii")


def _key(value: object) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ProfileError("invalid_catalog")
    return value


def _decode_row(row: Mapping[str, Any], key: str) -> dict[str, Any]:
    if row["format"] != 1 or not isinstance(row["body"], bytes):
        raise ProfileError("corrupt")
    try:
        plain = protection.unprotect(row["body"], context=_context(key))
    except protection.ProtectionError:
        raise ProfileError("protection") from None
    if (
        not 2 <= len(plain) <= MAX_PLAIN_BYTES
        or len(plain) != row["body_bytes"]
        or hashlib.sha256(plain).hexdigest() != row["body_sha256"]
    ):
        raise ProfileError("corrupt")
    try:
        value = _document(json.loads(plain))
    except ValueError, TypeError, RecursionError, ProfileError:
        raise ProfileError("corrupt") from None
    if (
        any(
            value[name] != row[name]
            for name in ("provider", "adapter_version", "native_version", "observed_at")
        )
        or _content_key(value) != key
    ):
        raise ProfileError("corrupt")
    return {"catalog_key": key, "body_sha256": row["body_sha256"], **value}


class ProviderCatalogService:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def get(self, key: str) -> dict[str, Any] | None:
        key = _key(key)
        require_private_store(self.store)
        with self.store._connection() as connection:
            row = _one(
                connection, "SELECT rowid,* FROM provider_catalogs WHERE catalog_key=?", (key,)
            )
        return None if row is None else _decode_row(row, key)

    def latest(
        self, provider: str, adapter_version: str, native_version: str
    ) -> dict[str, Any] | None:
        provider = _version(provider, 32)
        adapter_version = _version(adapter_version, 64)
        native_version = _version(native_version, 64)
        require_private_store(self.store)
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT catalog_key FROM provider_catalogs "
                "WHERE provider=? AND adapter_version=? AND native_version=? "
                "ORDER BY rowid DESC LIMIT 1",
                (provider, adapter_version, native_version),
            ).fetchone()
        return self.get(row[0]) if row else None

    def observe(
        self,
        provider: str,
        adapter_version: str,
        native_version: str,
        capabilities: object,
        models: object,
    ) -> dict[str, Any]:
        value = _document(
            {
                "provider": provider,
                "adapter_version": adapter_version,
                "native_version": native_version,
                "observed_at": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "capabilities": capabilities,
                "models": models,
            }
        )
        plain = _canonical(value)
        key = _content_key(value)
        body_sha256 = hashlib.sha256(plain).hexdigest()
        try:
            cipher = protection.protect(plain, context=_context(key))
        except protection.ProtectionError:
            raise ProfileError("protection") from None
        if not 2 <= len(cipher) <= MAX_CIPHER_BYTES:
            raise ProfileError("capacity")
        require_private_store(self.store)
        with self.store._connection(write=True) as connection:
            existing = _one(
                connection, "SELECT rowid,* FROM provider_catalogs WHERE catalog_key=?", (key,)
            )
            if existing is not None:
                _decode_row(existing, key)
                # A→B→A is a new observation of A, even if its semantic key is unchanged.
                # Receipt order is independent of wall-clock ties or clock corrections.
                connection.execute(
                    "UPDATE provider_catalogs SET "
                    "rowid=(SELECT max(rowid)+1 FROM provider_catalogs),"
                    "body=?,body_bytes=?,body_sha256=?,observed_at=? WHERE catalog_key=?",
                    (cipher, len(plain), body_sha256, value["observed_at"], key),
                )
                return {"catalog_key": key, "body_sha256": body_sha256, **value}
            count = connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0]
            if count > MAX_CATALOGS:
                raise ProfileError("corrupt")
            if count == MAX_CATALOGS:
                victim = connection.execute(
                    "SELECT catalog_key FROM provider_catalogs p WHERE NOT EXISTS "
                    "(SELECT 1 FROM session_profile_bindings b "
                    "WHERE b.capability_key=p.catalog_key) "
                    "ORDER BY p.rowid LIMIT 1"
                ).fetchone()
                if victim is None:
                    # A bound catalog of another native version may go: a start requires the
                    # readiness version, so its sessions refuse `capability_changed` at their next
                    # start, unless this observation is labelled with a stale launch-time version.
                    # When every retained row already shares this native_version, nothing here
                    # qualifies and the step below picks among them instead.
                    victim = connection.execute(
                        "SELECT catalog_key FROM provider_catalogs WHERE native_version<>? "
                        "ORDER BY rowid LIMIT 1",
                        (value["native_version"],),
                    ).fetchone()
                if victim is None:
                    # One evidence label (such as "2.x") can bind every catalog the store
                    # holds, so the oldest bound catalog of this native_version goes too. Its
                    # sessions answer `capability_changed` at their next start, once per
                    # MAX_CATALOGS menus -- the start check, not the stored menu, guards a
                    # pinned model.
                    victim = connection.execute(
                        "SELECT catalog_key FROM provider_catalogs WHERE native_version=? "
                        "ORDER BY rowid LIMIT 1",
                        (value["native_version"],),
                    ).fetchone()
                if victim is None:
                    raise ProfileError("capacity")
                connection.execute("DELETE FROM provider_catalogs WHERE catalog_key=?", victim)
            connection.execute(
                "INSERT INTO provider_catalogs VALUES (?,?,?,?,1,?,?,?,?)",
                (
                    key,
                    value["provider"],
                    value["adapter_version"],
                    value["native_version"],
                    cipher,
                    len(plain),
                    body_sha256,
                    value["observed_at"],
                ),
            )
        return {"catalog_key": key, "body_sha256": body_sha256, **value}


def evaluation_catalog(document: Mapping[str, Any]) -> CapabilityCatalog:
    if set(document) != _RETURN_KEYS:
        raise ProfileError("invalid_catalog")
    key = _key(document["catalog_key"])
    body_sha256 = _key(document["body_sha256"])
    value = _document({name: document[name] for name in _DOCUMENT_KEYS})
    if _content_key(value) != key or hashlib.sha256(_canonical(value)).hexdigest() != body_sha256:
        raise ProfileError("invalid_catalog")
    return CapabilityCatalog(
        provider=value["provider"],
        adapter_version=value["adapter_version"],
        native_version=value["native_version"],
        capabilities=tuple(Capability(**item) for item in value["capabilities"]),
        models=tuple(item["value"] for item in value["models"]),
    )
