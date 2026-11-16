"""Closed messaging contracts and pure launch configuration, using synthetic values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

SLACK = {
    "VIKI_SLACK_BOT_TOKEN": "xoxb-synthetic-bot",
    "VIKI_SLACK_APP_TOKEN": "xapp-synthetic-app",
    "VIKI_SLACK_OWNER_ID": "U1234567",
}
DISCORD = {
    "VIKI_DISCORD_BOT_TOKEN": "synthetic-discord-bot",
    "VIKI_DISCORD_OWNER_ID": "123456789",
    "VIKI_DISCORD_CLIENT_URL": "https://example.test/support",
}


@pytest.mark.parametrize(("provider", "env"), [("slack", SLACK), ("discord", DISCORD)])
def test_partial_and_case_alias_config_is_refused(provider: str, env: dict[str, str]) -> None:
    from viki_agent.messaging import messaging_config as module

    load = module.load_slack if provider == "slack" else module.load_discord
    assert load({}) is None
    assert load({"UNRELATED": "synthetic"}) is None
    for name in env:
        for invalid in (
            {key: value for key, value in env.items() if key != name},
            {**env, name.lower(): env[name]},
            {name.lower(): env[name]},
        ):
            with pytest.raises(module.MessagingConfigError) as error:
                load(invalid)
            assert error.value.args == ("invalid_messaging_config",)
            assert error.value.__context__ is None


@pytest.mark.parametrize(("provider", "env"), [("slack", SLACK), ("discord", DISCORD)])
def test_values_are_bounded_secret_free_and_mapping_is_unchanged(
    provider: str, env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    from viki_agent.messaging import messaging_config as module

    load = module.load_slack if provider == "slack" else module.load_discord
    before = dict(env)
    config = load(env)
    assert config is not None and env == before
    assert config.owner_id == env[f"VIKI_{provider.upper()}_OWNER_ID"]
    for name in env:
        if name.endswith("TOKEN"):
            assert env[name] not in repr(config)
            token = "xoxb-" if name.endswith("BOT_TOKEN") and provider == "slack" else ""
            token = "xapp-" if name.endswith("APP_TOKEN") else token
            assert load({**env, name: token + "a" * (4096 - len(token))}) is not None
            with pytest.raises(module.MessagingConfigError):
                load({**env, name: token + "a" * (4097 - len(token))})
        for invalid in (None, True, 42, [], {}, "", " ", "a\nb", "a\rb", "a\tb", "é", "\x7f"):
            with pytest.raises(module.MessagingConfigError) as error:
                load(cast(Mapping[str, str], {**env, name: invalid}))
            assert str(error.value) == error.value.code
            assert error.value.__context__ is None
    with pytest.raises(FrozenInstanceError):
        setattr(config, "owner_id", "other")  # noqa: B010
    assert env == before and caplog.records == []


@pytest.mark.parametrize("owner", ["U1234567", "W" + "A" * 31])
def test_slack_id_subset_and_token_kinds(owner: str) -> None:
    from viki_agent.messaging.messaging_config import (
        MessagingConfigError,
        load_slack,
        valid_slack_id,
    )

    assert valid_slack_id(owner) and load_slack({**SLACK, "VIKI_SLACK_OWNER_ID": owner})
    for value in ("U123456", "U" + "A" * 32, "u1234567", "T1234567", "U123456_", "U1234567\n"):
        assert not valid_slack_id(value)
        with pytest.raises(MessagingConfigError):
            load_slack({**SLACK, "VIKI_SLACK_OWNER_ID": value})
    for name, value in (("BOT", "xapp-synthetic"), ("APP", "xoxb-synthetic"), ("BOT", "xoxb-")):
        with pytest.raises(MessagingConfigError):
            load_slack({**SLACK, f"VIKI_SLACK_{name}_TOKEN": value})


def test_discord_ids_are_canonical_positive_uint64() -> None:
    from viki_agent.messaging.messaging_config import (
        MessagingConfigError,
        load_discord,
        valid_discord_id,
    )

    for value in ("1", str(2**64 - 1)):
        assert valid_discord_id(value) and load_discord({**DISCORD, "VIKI_DISCORD_OWNER_ID": value})
    for value in ("0", "01", "+1", "-1", "١", "1\n", str(2**64), "9" * 4096):
        assert not valid_discord_id(value)
        with pytest.raises(MessagingConfigError):
            load_discord({**DISCORD, "VIKI_DISCORD_OWNER_ID": value})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://",
        "https:///example.test",
        "https://user:secret" + "@" + "example.test",
        "https://example.test\\path",
        "https://example.test:0",
        "https://example.test:65536",
        "https://example.test:",
        "https://bad_host.test",
        "https://[bad]",
        "https://example.test/%zz",
        "https://example.test/(header)",
        "https://example.test/a,b",
        "https://127.1",
        "https://example.test/|",
        "https://example.test/[]",
    ],
)
def test_client_url_refuses_malformed_urls_and_header_injection(url: str) -> None:
    from viki_agent.messaging.messaging_config import MessagingConfigError, load_discord

    with pytest.raises(MessagingConfigError) as error:
        load_discord({**DISCORD, "VIKI_DISCORD_CLIENT_URL": url})
    assert url not in repr(error.value) and error.value.__context__ is None


def test_client_url_byte_limit_and_contracts_are_immutable() -> None:
    from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost
    from viki_agent.messaging.messaging_config import MessagingConfigError, load_discord

    url = "https://example.test/" + "a" * (2048 - len("https://example.test/"))
    assert load_discord({**DISCORD, "VIKI_DISCORD_CLIENT_URL": url}) is not None
    with pytest.raises(MessagingConfigError):
        load_discord({**DISCORD, "VIKI_DISCORD_CLIENT_URL": url + "a"})
    for url in ("https://example.test/a%20b?help=1#support", "https://[2001:db8::1]/support"):
        config = load_discord({**DISCORD, "VIKI_DISCORD_CLIENT_URL": url})
        assert config is not None and config.client_url == url
    for value in (
        RemoteBinding("slack", ("T1234567", "A1234567"), "U1234567", "W1234567", "D1234567"),
        RemotePost("123", "synthetic", "", 1000, "a" * 64, 32),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, "extra", True)  # noqa: B010
