"""UI-saved messaging settings stay sealed, provider-bound and value-free in every refusal."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from viki_agent import protection
from viki_agent.automation.routines import parse_stamp
from viki_agent.messaging import messaging_store
from viki_agent.messaging.messaging_channel import Provider
from viki_agent.messaging.messaging_config import MessagingConfigError
from viki_agent.messaging.messaging_store import (
    FIELDS,
    SECRETS,
    MessagingConfigStore,
    MessagingStoreError,
    Saved,
    build,
    fields,
    public_values,
)
from viki_agent.state import AppState, Prefs

VALUES: dict[Provider, dict[str, str]] = {
    "mattermost": {
        "url": "https://Chat.Example.TEST:443/team/",
        "owner_id": "abcdefghijklmnopqrstuvwxyz",
        "bot_token": "syntheticmattermosttoken01",
    },
    "slack": {
        "bot_token": "xoxb-synthetic-bot-token",
        "app_token": "xapp-synthetic-app-token",
        "owner_id": "U12345678",
    },
    "discord": {
        "bot_token": "synthetic-discord-token",
        "owner_id": "123456789012345678",
        "client_url": "https://client.example.test/support",
    },
}
CANONICAL: dict[Provider, dict[str, str]] = {
    **VALUES,
    "mattermost": {**VALUES["mattermost"], "url": "https://chat.example.test:443/team"},
}
PROVIDERS: tuple[Provider, ...] = ("mattermost", "slack", "discord")
WORKSPACE = str(uuid.uuid4())


@pytest.fixture
def settings(tmp_path: Path) -> MessagingConfigStore:
    return MessagingConfigStore(AppState(tmp_path / "state.json", Prefs()).store)


def row(settings: MessagingConfigStore, provider: Provider) -> tuple[bytes, str | None, str]:
    with settings.store._connection() as connection:
        found = connection.execute(
            "SELECT body,enabled_workspace,updated_at FROM messaging_config WHERE provider=?",
            (provider,),
        ).fetchone()
    assert found is not None
    return found


def write(settings: MessagingConfigStore, sql: str, values: tuple[object, ...]) -> None:
    with settings.store._connection(write=True) as connection:
        connection.execute(sql, values)


def context(provider: Provider) -> bytes:
    return b"VIKI messaging settings v1\x00" + provider.encode("ascii")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_round_trips_sealed(
    settings: MessagingConfigStore, provider: Provider
) -> None:
    config = build(provider, VALUES[provider])
    assert list(fields(config)) == list(FIELDS[provider])
    assert fields(config) == CANONICAL[provider]
    assert public_values(config) == {
        name: value for name, value in CANONICAL[provider].items() if name not in SECRETS
    }
    assert settings.load(provider) is None

    settings.save(provider, config)

    assert settings.load(provider) == Saved(config, None)
    body, workspace, updated_at = row(settings, provider)
    assert workspace is None
    parse_stamp(updated_at)
    for value in VALUES[provider].values():
        assert value.encode() not in body
    canonical = json.dumps(CANONICAL[provider], sort_keys=True, separators=(",", ":")).encode()
    assert protection.unprotect(body, context=context(provider)) == canonical
    assert all(settings.load(other) is None for other in PROVIDERS if other != provider)


def test_save_resets_and_remember_sets_and_clears_the_workspace(
    settings: MessagingConfigStore,
) -> None:
    settings.remember("slack", WORKSPACE)
    with settings.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM messaging_config").fetchone() == (0,)
    assert settings.load("slack") is None

    config = build("slack", VALUES["slack"])
    settings.save("slack", config)
    settings.remember("slack", WORKSPACE)
    assert settings.load("slack") == Saved(config, WORKSPACE)
    settings.remember("slack", None)
    assert settings.load("slack") == Saved(config, None)

    settings.remember("slack", WORKSPACE)
    replacement = build("slack", {**VALUES["slack"], "owner_id": "W87654321"})
    settings.save("slack", replacement)
    assert settings.load("slack") == Saved(replacement, None)


@pytest.mark.parametrize(
    "workspace",
    ["", "short", WORKSPACE + "0", 36, b"0" * 36],
    ids=["empty", "short", "37-characters", "int", "bytes"],
)
def test_remember_refuses_anything_but_a_36_character_id(
    settings: MessagingConfigStore, workspace: object
) -> None:
    settings.save("slack", build("slack", VALUES["slack"]))
    with pytest.raises(ValueError):
        settings.remember("slack", workspace)  # type: ignore[arg-type]
    assert settings.load("slack") == Saved(build("slack", VALUES["slack"]), None)


def test_remove_deletes_only_once(settings: MessagingConfigStore) -> None:
    settings.save("discord", build("discord", VALUES["discord"]))
    settings.save("mattermost", build("mattermost", VALUES["mattermost"]))
    assert settings.remove("discord") is True
    assert settings.remove("discord") is False
    assert settings.load("discord") is None
    assert settings.load("mattermost") is not None


def test_save_refuses_another_providers_config(settings: MessagingConfigStore) -> None:
    with pytest.raises(ValueError):
        settings.save("discord", build("slack", VALUES["slack"]))
    assert settings.load("discord") is None


def test_flipped_byte_reads_corrupt(settings: MessagingConfigStore) -> None:
    settings.save("mattermost", build("mattermost", VALUES["mattermost"]))
    body = bytearray(row(settings, "mattermost")[0])
    body[len(body) // 2] ^= 0x01
    write(settings, "UPDATE messaging_config SET body=? WHERE provider=?", (body, "mattermost"))
    with pytest.raises(MessagingStoreError) as caught:
        settings.load("mattermost")
    assert caught.value.code == "corrupt" and str(caught.value) == "corrupt"


@pytest.mark.parametrize("reseal", [False, True], ids=["copied", "resealed"])
def test_slack_body_in_the_discord_row_reads_corrupt(
    settings: MessagingConfigStore, reseal: bool
) -> None:
    settings.save("slack", build("slack", VALUES["slack"]))
    settings.save("discord", build("discord", VALUES["discord"]))
    body = row(settings, "slack")[0]
    if reseal:
        # Even past the context binding, Slack's field set is not Discord's.
        plain = json.dumps(VALUES["slack"], sort_keys=True, separators=(",", ":")).encode()
        body = protection.protect(plain, context=context("discord"))
    write(settings, "UPDATE messaging_config SET body=? WHERE provider=?", (body, "discord"))
    with pytest.raises(MessagingStoreError) as caught:
        settings.load("discord")
    assert caught.value.code == "corrupt"


@pytest.mark.parametrize(
    "plain",
    [
        b"not json",
        b"[]",
        b'"text"',
        b"\xff\xfe",
        b"[" * 30_000 + b"]" * 30_000,
        b'{"bot_token":"xoxb-a","bot_token":"xoxb-b","app_token":"xapp-a","owner_id":"U12345678"}',
        b'{"bot_token":"xoxb-a","app_token":"xapp-a","owner_id":12345678}',
        b'{"bot_token":"xoxb-a","app_token":"xapp-a"}',
        b'{"bot_token":"xoxb-a","app_token":"xapp-a","owner_id":"U12345678","url":"x"}',
        b'{"bot_token":"xoxp-a","app_token":"xapp-a","owner_id":"U12345678"}',
    ],
    ids=[
        "not_json",
        "array",
        "string",
        "not_utf8",
        "deep",
        "duplicate",
        "number",
        "missing",
        "extra",
        "invalid",
    ],
)
def test_sealed_body_of_the_wrong_shape_reads_corrupt(
    settings: MessagingConfigStore, plain: bytes
) -> None:
    settings.save("slack", build("slack", VALUES["slack"]))
    body = protection.protect(plain, context=context("slack"))
    write(settings, "UPDATE messaging_config SET body=? WHERE provider=?", (body, "slack"))
    with pytest.raises(MessagingStoreError) as caught:
        settings.load("slack")
    assert caught.value.code == "corrupt"


def test_failing_unprotect_reads_unavailable(
    settings: MessagingConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.save("slack", build("slack", VALUES["slack"]))

    def refuse(data: bytes, *, context: bytes) -> bytes:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "unprotect", refuse)
    with pytest.raises(MessagingStoreError) as caught:
        settings.load("slack")
    assert caught.value.code == "unavailable" and str(caught.value) == "unavailable"


@pytest.mark.parametrize("operation", ["load", "save", "remember", "remove"])
def test_unsafe_store_reads_unavailable(
    settings: MessagingConfigStore, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    config = build("slack", VALUES["slack"])
    settings.save("slack", config)

    def refuse(store: object) -> None:
        raise protection.ProtectionError()

    monkeypatch.setattr(messaging_store, "require_private_store", refuse)
    calls = {
        "load": lambda: settings.load("slack"),
        "save": lambda: settings.save("slack", config),
        "remember": lambda: settings.remember("slack", WORKSPACE),
        "remove": lambda: settings.remove("slack"),
    }
    with pytest.raises(MessagingStoreError) as caught:
        calls[operation]()
    assert caught.value.code == "unavailable"
    monkeypatch.undo()
    assert settings.load("slack") == Saved(config, None)


INVALID: list[tuple[Provider, str, str]] = [
    ("mattermost", "url", "http://leak-marker.example.test"),
    ("mattermost", "owner_id", "Leak-Marker-Not-An-Owner-Id"),
    ("mattermost", "bot_token", "leak marker token"),
    ("slack", "bot_token", "xoxp-leak-marker"),
    ("slack", "app_token", "xoxb-leak-marker"),
    ("slack", "owner_id", "leak-marker"),
    ("discord", "bot_token", "leak marker token"),
    ("discord", "owner_id", "0leakmarker"),
    ("discord", "client_url", "http://leak-marker.example.test/support"),
]


@pytest.mark.parametrize(("provider", "name", "value"), INVALID)
def test_build_refuses_each_invalid_field_without_its_value(
    provider: Provider, name: str, value: str
) -> None:
    with pytest.raises(MessagingConfigError) as caught:
        build(provider, {**VALUES[provider], name: value})
    assert str(caught.value) == "invalid_messaging_config"
    assert "leak" not in repr(caught.value).lower()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_build_refuses_a_missing_or_extra_field(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loaders' VIKI_* keys are internal: a set environment variable fills no field.
    for key, name in zip(messaging_store._KEYS[provider], FIELDS[provider], strict=True):
        monkeypatch.setenv(key, VALUES[provider][name])
    missing = dict(VALUES[provider])
    del missing[FIELDS[provider][0]]
    for values in ({}, missing, {**VALUES[provider], "extra": "value"}):
        with pytest.raises(MessagingConfigError):
            build(provider, values)


def test_loaded_config_repr_hides_tokens(settings: MessagingConfigStore) -> None:
    for provider in PROVIDERS:
        settings.save(provider, build(provider, VALUES[provider]))
        saved = settings.load(provider)
        assert saved is not None
        shown = repr(saved)
        for name in SECRETS & set(FIELDS[provider]):
            assert VALUES[provider][name] not in shown
