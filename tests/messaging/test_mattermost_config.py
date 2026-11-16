"""Optional Mattermost configuration, using only synthetic environment mappings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from viki_agent.messaging.mattermost_config import (
    MattermostConfig,
    MattermostConfigError,
    load_mattermost,
    valid_id,
)

OWNER = "a1" * 13
TOKEN = "synthetic-token-DO-NOT-EXPOSE"
ENV = {
    "VIKI_MATTERMOST_URL": "https://chat.example.test",
    "VIKI_MATTERMOST_OWNER_ID": OWNER,
    "VIKI_MATTERMOST_BOT_TOKEN": TOKEN,
}


def test_disabled_and_invalid_config_never_connects() -> None:
    connections: list[MattermostConfig] = []

    def configure(env: Mapping[str, str]) -> None:
        try:
            config = load_mattermost(env)
        except MattermostConfigError:
            return
        if config is not None:
            connections.append(config)

    configure({})
    configure({"UNRELATED": "synthetic"})
    configure({"VIKI_MATTERMOST_URL": ENV["VIKI_MATTERMOST_URL"]})
    configure({**ENV, "VIKI_MATTERMOST_URL": "http://chat.example.test"})
    assert connections == []
    configure(ENV)
    assert len(connections) == 1


def test_valid_configuration_is_frozen_and_hides_token() -> None:
    config = load_mattermost(ENV)
    assert config == MattermostConfig(ENV["VIKI_MATTERMOST_URL"], OWNER, TOKEN)
    assert config is not None
    assert config.bot_token == TOKEN
    assert TOKEN not in repr(config)
    assert "bot_token" not in repr(config)
    with pytest.raises(FrozenInstanceError):
        setattr(config, "url", "https://elsewhere.example.test")  # noqa: B010


@pytest.mark.parametrize("missing", list(ENV))
def test_partial_configuration_is_refused(missing: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({key: value for key, value in ENV.items() if key != missing})


@pytest.mark.parametrize("name", list(ENV))
@pytest.mark.parametrize("value", ["", " ", "\t", "\n"])
def test_present_empty_or_whitespace_is_not_disabled(name: str, value: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({**ENV, name: value})


@pytest.mark.parametrize(
    "url",
    [
        "http://chat.example.test",
        "//chat.example.test",
        "https:///chat.example.test",
        "https://",
        "https://user@localhost",
        "https://user:password@localhost",
        "https://chat.example.test?query",
        "https://chat.example.test?",
        "https://chat.example.test#fragment",
        "https://chat.example.test#",
        "https://chat.example.test\\other",
        "https://chat.example.test/\\other",
        " https://chat.example.test",
        "https://chat.example.test ",
        "https://chat.example.test/space here",
        "https://chat.example.test/\tpath",
        "https://chat.example.test/\npath",
        "https://chat.example.test/\rpath",
        "https://chat.example.test/\x00path",
        "https://chat.example.test/\x7fpath",
        "https://ch\u00e1t.example.test",
        "https://chat.example.test/\u00e9",
        "https://chat.example.test/%2e",
        "https://chat.example.test/%2f",
        "https://chat.example.test/%252f",
        "https://chat.example.test/.",
        "https://chat.example.test/..",
        "https://chat.example.test/one/../two",
        "https://chat.example.test//prefix",
        "https://chat.example.test/prefix//path",
        "https://chat.example.test:0",
        "https://chat.example.test:65536",
        "https://chat.example.test:",
        "https://chat.example.test:abc",
        "https://chat.example.test:+443",
        "https://chat.example.test:-443",
        "https://chat.example.test:443:443",
        "https://chat.example.test:\uff14\uff14\uff13",
        "https://2130706433",
        "https://127.1",
        "https://0177.0.0.1",
        "https://0x7f000001",
        "https://0x7f.0.0.1",
        "https://127.0.0.01",
        "https://999.0.0.1",
        "https://host_name.example.test",
        "https://-host.example.test",
        "https://host-.example.test",
        "https://host..example.test",
        "https://" + "a" * 64 + ".example.test",
    ],
)
def test_hostile_urls_are_refused(url: str) -> None:
    with pytest.raises(MattermostConfigError) as error:
        load_mattermost({**ENV, "VIKI_MATTERMOST_URL": url})
    assert error.value.args == ("invalid_mattermost_config",)
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "token",
    ["", "leading space", "trailing ", "line\nbreak", "tab\t", "\x00", "\x1f", "\x7f", "\u00e9"],
)
def test_token_is_printable_ascii_without_whitespace(token: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({**ENV, "VIKI_MATTERMOST_BOT_TOKEN": token})


@pytest.mark.parametrize(
    "value", [None, False, 42, [], {}, "", "a" * 25, "a" * 27, "A" * 26, "_" * 26, "a" * 25 + "\n"]
)
def test_noncanonical_ids_are_refused(value: object) -> None:
    assert valid_id(value) is False
    with pytest.raises(MattermostConfigError):
        load_mattermost(cast(Mapping[str, str], {**ENV, "VIKI_MATTERMOST_OWNER_ID": value}))


@pytest.mark.parametrize("value", ["a" * 26, "0" * 26, OWNER])
def test_canonical_ids_are_admitted(value: str) -> None:
    assert valid_id(value) is True


@pytest.mark.parametrize("name", list(ENV))
def test_case_collisions_are_refused(name: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({**ENV, name.lower(): ENV[name]})


@pytest.mark.parametrize("name", list(ENV))
def test_case_mismatched_names_are_refused_even_when_no_canonical_names_exist(name: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({name.lower(): ENV[name]})


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("https://chat.example.test/", "https://chat.example.test"),
        ("HTTPS://CHAT.EXAMPLE.TEST/Team-1/prefix_2/", "https://chat.example.test/Team-1/prefix_2"),
        ("https://chat.example.test:8443/team", "https://chat.example.test:8443/team"),
        ("https://chat.example.test:1", "https://chat.example.test:1"),
        ("https://chat.example.test:65535", "https://chat.example.test:65535"),
        ("https://127.0.0.1", "https://127.0.0.1"),
        ("https://[2001:db8::1]:8443/team", "https://[2001:db8::1]:8443/team"),
        ("https://[::1]/", "https://[::1]"),
        ("https://localhost", "https://localhost"),
    ],
)
def test_valid_urls_are_canonicalized(raw: str, canonical: str) -> None:
    config = load_mattermost({**ENV, "VIKI_MATTERMOST_URL": raw})
    assert config is not None
    assert config.url == canonical


@pytest.mark.parametrize(
    "url",
    [
        "https://[2001:db8::1",
        "https://2001:db8::1",
        "https://[not-an-address]",
        "https://[::1]extra",
        "https://[::1]:",
        "https://[fe80::1%25eth0]",
        "https://[fe80::1%eth0]",
        "https://chat.example.test:000443",
        "https://example.123",
        "https://example.0x123",
        "https://chat.example.test./",
        "https://" + ".".join(["a" * 63] * 4),
        "https://chat.example.test/" + "a" * 512,
        "https://chat.example.test/" + "a" * 2048,
    ],
)
def test_url_host_port_and_prefix_bounds_are_refused(url: str) -> None:
    with pytest.raises(MattermostConfigError):
        load_mattermost({**ENV, "VIKI_MATTERMOST_URL": url})


def test_prefix_and_token_bounds_admit_limit_and_refuse_one_more() -> None:
    config = load_mattermost(
        {**ENV, "VIKI_MATTERMOST_URL": "https://chat.example.test/" + "a" * 511}
    )
    assert config is not None
    assert len(config.url.removeprefix("https://chat.example.test")) == 512
    assert load_mattermost({**ENV, "VIKI_MATTERMOST_BOT_TOKEN": "a" * 4096}) is not None
    with pytest.raises(MattermostConfigError):
        load_mattermost({**ENV, "VIKI_MATTERMOST_BOT_TOKEN": "a" * 4097})


@pytest.mark.parametrize("name", list(ENV))
@pytest.mark.parametrize("value", [None, True, 42, [], {}])
def test_nonstring_values_are_safely_refused(name: str, value: object) -> None:
    with pytest.raises(MattermostConfigError) as error:
        load_mattermost(cast(Mapping[str, str], {**ENV, name: value}))
    assert error.value.args == ("invalid_mattermost_config",)
    assert error.value.__context__ is None


def test_load_does_not_mutate_its_mapping_or_log_values(caplog: pytest.LogCaptureFixture) -> None:
    env = dict(ENV)
    assert load_mattermost(env) is not None
    assert env == ENV
    hostile_url = "https://user:synthetic-password@localhost"
    env["VIKI_MATTERMOST_URL"] = hostile_url
    before = dict(env)
    with pytest.raises(MattermostConfigError) as error:
        load_mattermost(env)
    assert env == before
    assert error.value.args == (error.value.code,)
    assert str(error.value) == error.value.code
    assert hostile_url not in repr(error.value)
    assert "synthetic-password" not in repr(error.value)
    assert TOKEN not in repr(error.value)
    assert error.value.__context__ is None
    assert caplog.records == []
