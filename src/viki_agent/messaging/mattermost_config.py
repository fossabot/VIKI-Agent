"""User-supplied Mattermost bot configuration, without environment or network access."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_VARIABLES = ("VIKI_MATTERMOST_URL", "VIKI_MATTERMOST_OWNER_ID", "VIKI_MATTERMOST_BOT_TOKEN")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PREFIX = re.compile(r"(?:/[A-Za-z0-9_-]+)*/?")


class MattermostConfigError(ValueError):
    """A fixed refusal code; configured values are not part of the exception."""

    code = "invalid_mattermost_config"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class MattermostConfig:
    url: str
    owner_user_id: str
    bot_token: str = field(repr=False)


def valid_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9]{26}", value) is not None


def _authority(value: str) -> str | None:
    if value.startswith("["):
        host, bracket, suffix = value[1:].partition("]")
        if not bracket or (suffix and not suffix.startswith(":")):
            return None
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            return None
        separator, port = (":", suffix[1:]) if suffix else ("", "")
    else:
        host, separator, port = value.partition(":")
        host = host.lower()
        if re.fullmatch(r"[0-9.]+", host):
            try:
                if str(ipaddress.IPv4Address(host)) != host:
                    return None
            except ValueError:
                return None
        elif (
            len(host) > 253
            or any(_LABEL.fullmatch(label) is None for label in host.split("."))
            or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1])
        ):
            return None
    if separator:
        if re.fullmatch(r"[0-9]{1,5}", port) is None or not 1 <= int(port) <= 65535:
            return None
        return f"{host}:{int(port)}"
    return host


def _url(value: str) -> str | None:
    if (
        not value
        or len(value) > 2048
        or any(not 33 <= ord(char) <= 126 for char in value)
        or any(char in value for char in "@?#\\%")
    ):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or len(parts.path) > 512 or _PREFIX.fullmatch(parts.path) is None:
        return None
    authority = _authority(parts.netloc)
    if authority is None:
        return None
    return f"https://{authority}{parts.path.removesuffix('/')}"


def load_mattermost(env: Mapping[str, str]) -> MattermostConfig | None:
    """Return None only when absent; refuse invalid fields without echoing any value."""
    if any(name.upper() in _VARIABLES and name not in _VARIABLES for name in env):
        raise MattermostConfigError
    if not any(name in env for name in _VARIABLES):
        return None
    if not all(name in env for name in _VARIABLES):
        raise MattermostConfigError
    url, owner, token = (env[name] for name in _VARIABLES)
    if (
        not isinstance(url, str)
        or not valid_id(owner)
        or not isinstance(token, str)
        or not 1 <= len(token) <= 4096
        or any(not 33 <= ord(char) <= 126 for char in token)
    ):
        raise MattermostConfigError
    canonical_url = _url(url)
    if canonical_url is None:
        raise MattermostConfigError
    return MattermostConfig(canonical_url, owner, token)
