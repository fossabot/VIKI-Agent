"""server.create_app's voice-pack wiring: the default `VoicePack.create(data_dir)`, the pack and
speech-settings routes reaching the real app, and `pack.close()` at shutdown."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_server import BEARER, ORIGIN, running
from viki_agent import server

POST = {**BEARER, "origin": ORIGIN}


class FakePack:
    def __init__(self) -> None:
        self.closed = False
        self.status_view: dict[str, object] = {"state": "not_installed"}

    async def status(self) -> dict[str, object]:
        return self.status_view

    async def check(self) -> dict[str, object]:
        return self.status_view

    async def close(self) -> None:
        self.closed = True


class _VoicePackStub:
    """Monkeypatched in place of `server.VoicePack`: `.create()` hands back one fixed fake, the
    way the real classmethod hands back one real `VoicePack`."""

    def __init__(self, instance: FakePack) -> None:
        self.instance = instance
        self.data_dir: Path | None = None

    def create(self, data_dir: Path) -> FakePack:
        self.data_dir = data_dir
        return self.instance


def test_default_pack_is_built_from_voice_pack_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePack()
    stub = _VoicePackStub(fake)
    monkeypatch.setattr(server, "VoicePack", stub)
    with running(tmp_path) as app:
        response = app.client.get("/api/v2/voice/pack", headers=BEARER)
    assert (response.status_code, response.json()) == (200, {"pack": fake.status_view})
    assert stub.data_dir == tmp_path / "data"


def test_shutdown_closes_the_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePack()
    monkeypatch.setattr(server, "VoicePack", _VoicePackStub(fake))
    with running(tmp_path):
        assert fake.closed is False
    assert fake.closed is True


def test_post_needs_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "VoicePack", _VoicePackStub(FakePack()))
    with running(tmp_path) as app:
        for path in ("/api/v2/voice/pack/check", "/api/v2/voice/pack/warm"):
            assert app.client.post(path, json={}, headers=BEARER).status_code == 403
            foreign = {**BEARER, "origin": "https://evil.example"}
            assert app.client.post(path, json={}, headers=foreign).status_code == 403
            assert app.client.post(path, json={}, headers=POST).status_code == 200


def test_speech_settings_wired_to_the_real_opener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`running()` injects its own fake opener (`opened.put`) exactly as `create_app`'s real
    default parameter would be used; this proves `server.py` threads it into `voice_routes`."""
    monkeypatch.setattr(server, "VoicePack", _VoicePackStub(FakePack()))
    with running(tmp_path) as app:
        response = app.client.post("/api/v2/voice/speech-settings", json={}, headers=POST)
    if sys.platform == "win32":
        assert (response.status_code, response.json()) == (200, {"opened": True})
        assert app.opened.get_nowait() == "ms-settings:speech"
    else:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "unsupported_platform"
        assert app.opened.empty()
