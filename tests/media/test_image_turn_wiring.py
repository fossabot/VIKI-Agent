"""Proof that `create_app` really wires image turns and history media, not just by reading
`server.py`.

The scripted subprocess fake (`tests/fake_claude.py`) only checks a written frame's `type`
(`replay.Replayer.feed`), so it cannot itself prove which bytes reached it. What `create_app`
alone can prove — and what a deleted binding alone can break — is that the two resolvers it
installs really are the asset service's own bound methods: `manager`'s `DriverConfig` gets
`assets.resolve`, and `catalog` gets `assets.find_by_digest`. The tests below fail for each of
these breakages: a deleted `bind_assets`, a deleted `bind_history_media`, and
`SessionManager._claude_driver` handing `ClaudeDriver` a config with no resolver.

The first test proves `create_app` binds `manager._config` itself, and that the bound methods
are this launch's own asset service, not a stand-in with the same signature (a bound method
compares equal only to the same function on the same instance). Comparing against an
`AssetService` `create_app` never received would add nothing past that, since a bare
`None != other.resolve` already tells those apart with no wiring proof at all.
The second test carries the proof one hop further, to what the session layer actually hands
`ClaudeDriver`: `SessionManager._claude_driver`, built through the manager's own factory
(constructing a `ClaudeDriver` spawns nothing).

The third test closes the gap the first paragraph names: an in-process `FakeChild` session
(no real `claude.exe`, `tests/replay.py`) actually sends a typed turn with one image,
and `Replayer.seen` holds the exact bytes the attachment resolver handed the driver, not merely
a frame whose `type` happens to be right.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.media.assets import AssetService
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.turn_input import Attachment, TurnInput

from replay import FakeSpawner

TOKEN = "wiring-token"


def _build_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SessionManager, Catalog, AssetService]:
    """Assemble `create_app` the way the launcher does, capturing the one `AssetService` it
    builds internally so the test can compare identities against what it bound."""
    vault = tmp_path / "vault"
    vault.mkdir()
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    data_dir = tmp_path / "data"

    real_asset_service = server.AssetService
    built: list[AssetService] = []

    def spy(*args: Any, **kwargs: Any) -> AssetService:
        instance = real_asset_service(*args, **kwargs)
        built.append(instance)
        return instance

    monkeypatch.setattr(server, "AssetService", spy)

    state = AppState(data_dir / "state.json", Prefs(mode="auto", effort="low"))
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=vault, env={}), state=state, claude_home=home
    )
    catalog = Catalog(home, vault)
    config = Config(vault=vault, claude_home=home, cli=Path("fake"), edge=None, data_dir=data_dir)

    server.create_app(config=config, token=TOKEN, manager=manager, catalog=catalog, static_dir=None)

    assert len(built) == 1, "create_app must build exactly one AssetService"
    return manager, catalog, built[0]


def test_create_app_binds_the_image_resolver_and_the_history_media_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, catalog, assets = _build_app(tmp_path, monkeypatch)
    # The driver config every new session gets is built from the asset service's own bound
    # `resolve` method, not a stand-in with the same signature: a bound method compares equal
    # only to the same function on the same instance.
    assert manager._config.resolve_attachment == assets.resolve
    # The catalog's history-media correlator is that identical asset service's `find_by_digest`,
    # so a history image is resolved against this launch's own local assets.
    assert catalog._history_media == assets.find_by_digest


def test_the_session_layer_hands_a_new_driver_the_bound_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`manager._config` carrying the bound resolver (the test above) is one hop short of the
    real send path: nothing proved that `_claude_driver`, the session layer's own factory, hands
    that config to the `ClaudeDriver` it builds, rather than some other config that dropped the
    resolver along the way. Building a driver through the factory spawns no process."""
    manager, _catalog, assets = _build_app(tmp_path, monkeypatch)
    driver = manager._claude_driver(
        Launch("s-wiring"), Prefs(mode="auto", effort="low"), lambda _event: None
    )
    assert isinstance(driver, ClaudeDriver)
    # A bound method compares equal only to the same function on the same instance, so this
    # fails the moment `_claude_driver` hands `ClaudeDriver` any config but the manager's own.
    assert driver._config.resolve_attachment == assets.resolve


async def test_the_wire_carries_the_exact_image_bytes_with_the_text_block_first(
    tmp_path: Path,
) -> None:
    """An in-process `FakeChild` session (no real `claude.exe`) sends a typed turn with one
    image; `Replayer.seen` proves the frame the CLI would receive puts the text block first and
    carries the asset's exact bytes, base64-decoded — not merely a frame whose `type` is right."""
    raw = bytes(range(256)) * 4  # synthetic; every byte value present, so a truncation shows
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    spawner = FakeSpawner([])

    async def resolve(_attachment: Attachment) -> bytes:
        return raw

    config = DriverConfig(
        cli=("fake",),
        vault=tmp_path / "vault",
        env={},
        spawner=spawner,
        resolve_attachment=resolve,
        grace_s=0.05,
        poll_s=0.05,
        control_timeout_s=0.05,
    )
    state = AppState(tmp_path / "state.json", Prefs(mode="auto", effort="low"))
    manager = SessionManager(config=config, state=state, claude_home=home)
    driver = manager._claude_driver(
        Launch("s-image-wire"), Prefs(mode="auto", effort="low"), lambda _event: None
    )
    assert isinstance(driver, ClaudeDriver)
    await driver.open()
    attachment = Attachment(
        "00000000-0000-4000-8000-000000000099", "image/png", len(raw), 1, 1, "synthetic.png"
    )
    await driver.send_turn(TurnInput("describe this", (attachment,)))

    seen = spawner.children[0].process.replayer.seen
    user_frames = [frame for frame in seen if frame.get("type") == "user"]
    assert len(user_frames) == 1
    content = user_frames[0]["message"]["content"]
    assert content == [
        {"type": "text", "text": "describe this"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(raw).decode("ascii"),
            },
        },
    ]
    await driver.close()
