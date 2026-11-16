"""UI-saved messaging settings, one sealed row per provider."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from viki_agent import protection
from viki_agent.api_json import unique_object
from viki_agent.automation.routines import stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.messaging.mattermost_config import (
    MattermostConfig,
    MattermostConfigError,
    load_mattermost,
)
from viki_agent.messaging.messaging_channel import Provider
from viki_agent.messaging.messaging_config import (
    DiscordConfig,
    MessagingConfigError,
    SlackConfig,
    load_discord,
    load_slack,
)
from viki_agent.storage import SessionStore, StateError

Config = MattermostConfig | SlackConfig | DiscordConfig

FIELDS: dict[Provider, tuple[str, ...]] = {
    "mattermost": ("url", "owner_id", "bot_token"),
    "slack": ("bot_token", "app_token", "owner_id"),
    "discord": ("bot_token", "owner_id", "client_url"),
}
SECRETS = frozenset({"bot_token", "app_token"})
# Internal keys for the launch-time validators, in FIELDS order; never read from the environment.
_KEYS: dict[Provider, tuple[str, ...]] = {
    "mattermost": ("VIKI_MATTERMOST_URL", "VIKI_MATTERMOST_OWNER_ID", "VIKI_MATTERMOST_BOT_TOKEN"),
    "slack": ("VIKI_SLACK_BOT_TOKEN", "VIKI_SLACK_APP_TOKEN", "VIKI_SLACK_OWNER_ID"),
    "discord": ("VIKI_DISCORD_BOT_TOKEN", "VIKI_DISCORD_OWNER_ID", "VIKI_DISCORD_CLIENT_URL"),
}
_LOADERS: dict[Provider, Callable[[Mapping[str, str]], Config | None]] = {
    "mattermost": load_mattermost,
    "slack": load_slack,
    "discord": load_discord,
}
_TYPES: dict[str, type[Config]] = {
    "mattermost": MattermostConfig,
    "slack": SlackConfig,
    "discord": DiscordConfig,
}


class MessagingStoreError(Exception):
    """A fixed code; no saved value, path or storage detail is part of the exception."""

    def __init__(self, code: Literal["unavailable", "corrupt"]) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Saved:
    config: Config
    enabled_workspace: str | None


def build(provider: Provider, values: Mapping[str, str]) -> Config:
    """Validate exactly one provider's fields; a refusal carries no value."""
    names = FIELDS[provider]
    if set(values) != set(names):
        raise MessagingConfigError()
    env = {key: values[name] for key, name in zip(_KEYS[provider], names, strict=True)}
    try:
        config = _LOADERS[provider](env)
    except MattermostConfigError:
        raise MessagingConfigError() from None
    if config is None:  # Every key is present, so a loader never answers "absent" here.
        raise MessagingConfigError()
    return config


def fields(config: Config) -> dict[str, str]:
    """All of a config's fields, secrets included, in FIELDS order."""
    match config:
        case MattermostConfig():
            return {
                "url": config.url,
                "owner_id": config.owner_user_id,
                "bot_token": config.bot_token,
            }
        case SlackConfig():
            return {
                "bot_token": config.bot_token,
                "app_token": config.app_token,
                "owner_id": config.owner_id,
            }
        case DiscordConfig():
            return {
                "bot_token": config.bot_token,
                "owner_id": config.owner_id,
                "client_url": config.client_url,
            }


def public_values(config: Config) -> dict[str, str]:
    return {name: value for name, value in fields(config).items() if name not in SECRETS}


def _context(provider: Provider) -> bytes:
    return b"VIKI messaging settings v1\x00" + provider.encode("ascii")


def _unseal(provider: Provider, body: bytes) -> bytes:
    context = _context(provider)
    try:
        return protection.unprotect(body, context=context)
    except protection.ProtectionError:
        pass
    # Both a bad row and a protection that cannot work at all refuse alike. A probe sealed and
    # opened here under the same context tells them apart: it passes only when protection works.
    try:
        protection.unprotect(protection.protect(b"{}", context=context), context=context)
    except protection.ProtectionError:
        raise MessagingStoreError("unavailable") from None
    raise MessagingStoreError("corrupt")


class MessagingConfigStore:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def load(self, provider: Provider) -> Saved | None:
        try:
            require_private_store(self.store)
            with self.store._connection() as connection:
                row = connection.execute(
                    "SELECT body,enabled_workspace FROM messaging_config WHERE provider=?",
                    (provider,),
                ).fetchone()
        except protection.ProtectionError, StateError, sqlite3.Error:
            raise MessagingStoreError("unavailable") from None
        if row is None:
            return None
        body, workspace = row
        plain = _unseal(provider, body)
        try:
            value = json.loads(plain, object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                raise TypeError("not_an_object")
            config = build(provider, value)
        except ValueError, TypeError, RecursionError:
            raise MessagingStoreError("corrupt") from None
        return Saved(config, workspace)

    def save(self, provider: Provider, config: Config) -> None:
        """Insert or replace the provider's row; a new save is off in every workspace."""
        if type(config) is not _TYPES.get(provider):
            raise ValueError("provider_mismatch")
        plain = json.dumps(
            fields(config), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        try:
            body = protection.protect(plain, context=_context(provider))
            require_private_store(self.store)
            with self.store._connection(write=True) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO messaging_config VALUES (?,1,?,NULL,?)",
                    (provider, body, stamp(datetime.now(UTC))),
                )
        except protection.ProtectionError, StateError, sqlite3.Error:
            raise MessagingStoreError("unavailable") from None

    def remember(self, provider: Provider, workspace_id: str | None) -> None:
        """Record the workspace the bot was turned on in, or None for off; no row, no change."""
        if workspace_id is not None and (
            not isinstance(workspace_id, str) or len(workspace_id) != 36
        ):
            raise ValueError("invalid_workspace")
        try:
            require_private_store(self.store)
            with self.store._connection(write=True) as connection:
                connection.execute(
                    "UPDATE messaging_config SET enabled_workspace=?,updated_at=? WHERE provider=?",
                    (workspace_id, stamp(datetime.now(UTC)), provider),
                )
        except protection.ProtectionError, StateError, sqlite3.Error:
            raise MessagingStoreError("unavailable") from None

    def remove(self, provider: Provider) -> bool:
        try:
            require_private_store(self.store)
            with self.store._connection(write=True) as connection:
                deleted = connection.execute(
                    "DELETE FROM messaging_config WHERE provider=?", (provider,)
                ).rowcount
        except protection.ProtectionError, StateError, sqlite3.Error:
            raise MessagingStoreError("unavailable") from None
        return deleted == 1
