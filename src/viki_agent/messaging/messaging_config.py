"""Pure user-supplied Slack and Discord launch configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from viki_agent.messaging.mattermost_config import _authority


class MessagingConfigError(ValueError):
    """A fixed refusal code without configured values."""

    code = "invalid_messaging_config"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str = field(repr=False)
    app_token: str = field(repr=False)
    owner_id: str


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str = field(repr=False)
    owner_id: str
    client_url: str


def valid_slack_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[UW][A-Z0-9]{7,31}", value) is not None


def valid_discord_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
        and int(value) <= 2**64 - 1
    )


def _values(env: Mapping[str, str], variables: tuple[str, ...]) -> tuple[str, ...] | None:
    if any(name.upper() in variables and name not in variables for name in env):
        raise MessagingConfigError
    if not any(name in env for name in variables):
        return None
    if not all(name in env and isinstance(env[name], str) for name in variables):
        raise MessagingConfigError
    return tuple(env[name] for name in variables)


def _token(value: str) -> bool:
    return 1 <= len(value) <= 4096 and all(33 <= ord(char) <= 126 for char in value)


def _client_url(value: str) -> str | None:
    if (
        not 1 <= len(value) <= 2048
        or re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'*+;=%-]+", value) is None
        or re.search(r"%(?![0-9A-Fa-f]{2})", value)
    ):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    authority = _authority(parts.netloc)
    if (
        parts.scheme != "https"
        or authority is None
        or any(char in parts.path + parts.query + parts.fragment for char in "[]")
    ):
        return None
    return parts._replace(scheme="https", netloc=authority).geturl()


def load_slack(env: Mapping[str, str]) -> SlackConfig | None:
    values = _values(env, ("VIKI_SLACK_BOT_TOKEN", "VIKI_SLACK_APP_TOKEN", "VIKI_SLACK_OWNER_ID"))
    if values is None:
        return None
    bot, app, owner = values
    if (
        not _token(bot)
        or not bot.startswith("xoxb-")
        or len(bot) == 5
        or not _token(app)
        or not app.startswith("xapp-")
        or len(app) == 5
        or not valid_slack_id(owner)
    ):
        raise MessagingConfigError
    return SlackConfig(bot, app, owner)


def load_discord(env: Mapping[str, str]) -> DiscordConfig | None:
    values = _values(
        env, ("VIKI_DISCORD_BOT_TOKEN", "VIKI_DISCORD_OWNER_ID", "VIKI_DISCORD_CLIENT_URL")
    )
    if values is None:
        return None
    bot, owner, raw_url = values
    url = _client_url(raw_url)
    if not _token(bot) or not valid_discord_id(owner) or url is None:
        raise MessagingConfigError
    return DiscordConfig(bot, owner, url)
