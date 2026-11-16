"""Settings aliases use synthetic roots; known credential targets stay unopened."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from viki_agent import config, osfs, state


@dataclass(frozen=True)
class Rig:
    cfg: config.Config
    env: dict[str, str]
    sources: dict[str, Path]
    credentials: Path


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    home, vault, managed = tmp_path / "home" / ".claude", tmp_path / "vault", tmp_path / "managed"
    anthropic = tmp_path / "anthropic"
    sources = {
        "home": home / "settings.json",
        "project": vault / ".claude" / "settings.json",
        "local": vault / ".claude" / "settings.local.json",
        "managed": managed / "managed-settings.json",
        "drop-in": managed / "managed-settings.d" / "50-settings.json",
        "federation": anthropic / "configs" / "profile.json",
    }
    # Only the reader's normal sources are supplied; none use the host's provider directories.
    monkeypatch.setattr(
        config, "_MANAGED_DIRS", dict.fromkeys(("win32", "darwin", "linux"), managed)
    )
    monkeypatch.setattr(config, "_LEGACY_MANAGED_SETTINGS", tmp_path / "legacy-managed.json")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(anthropic))
    return Rig(
        config.Config(vault, home, None, None, tmp_path / "data"),
        {"ANTHROPIC_CONFIG_DIR": str(anthropic)},
        sources,
        anthropic / "credentials",
    )


def _directory_alias(alias: Path, target: Path) -> None:
    alias.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(alias))
    else:
        alias.symlink_to(target, target_is_directory=True)


def _read(rig: Rig, reader: str) -> object:
    if reader == "billing":
        return config.billing_problems(rig.cfg, rig.env)
    return state.user_defaults(rig.cfg.claude_home, rig.cfg.vault)


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    opened: list[Path] = []
    original = osfs.read_regular

    def recording(path: str | Path, limit: int, *, follow_symlinks: bool = True) -> bytes:
        opened.append(Path(path).resolve())
        return original(path, limit, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(osfs, "read_regular", recording)
    return opened


_READERS_AND_SOURCES = [
    (reader, source)
    for reader in ("billing", "defaults")
    for source in ("home", "project", "local")
] + [("billing", source) for source in ("managed", "drop-in", "federation")]
_CANARY = {"model": "synthetic-canary", "apiKeyHelper": "synthetic", "federation_rule_id": "r"}


@pytest.mark.parametrize(("reader", "source"), _READERS_AND_SOURCES)
def test_settings_directory_alias_never_opens_anthropic_credentials(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, reader: str, source: str
) -> None:
    target = rig.credentials / rig.sources[source].name
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_CANARY), encoding="utf-8")
    _directory_alias(rig.sources[source].parent, rig.credentials)
    assert rig.sources[source].resolve() == target.resolve()
    opened = _record_reads(monkeypatch)

    result = _read(rig, reader)

    assert target.resolve() not in opened
    assert result == ([] if reader == "billing" else state.Prefs())


@pytest.mark.parametrize(("reader", "source"), _READERS_AND_SOURCES)
@pytest.mark.parametrize("credential", ["claude", "anthropic"])
def test_settings_file_alias_never_opens_a_known_credential_target(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, reader: str, source: str, credential: str
) -> None:
    """Model file-link resolution on Windows too, where symlink privileges may be unavailable."""
    alias = rig.sources[source]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.write_text("{}", encoding="utf-8")
    target = (
        rig.cfg.claude_home / ".credentials.json"
        if credential == "claude"
        else rig.credentials / "profile.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_CANARY), encoding="utf-8")
    original_resolve, original_read = Path.resolve, osfs.read_regular
    opened: list[Path] = []

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(target if path == alias else path, strict=strict)

    def read(path: str | Path, limit: int, *, follow_symlinks: bool = True) -> bytes:
        resolved = Path(path).resolve()
        opened.append(resolved)
        return original_read(resolved, limit, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(osfs, "read_regular", read)

    result = _read(rig, reader)

    assert target.resolve() not in opened
    assert result == ([] if reader == "billing" else state.Prefs())


@pytest.mark.parametrize(("reader", "source"), _READERS_AND_SOURCES)
def test_ordinary_settings_directory_alias_is_still_read(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, source: str
) -> None:
    target = tmp_path / "ordinary-settings" / rig.sources[source].name
    target.parent.mkdir()
    target.write_text(json.dumps(_CANARY), encoding="utf-8")
    _directory_alias(rig.sources[source].parent, target.parent)
    opened = _record_reads(monkeypatch)

    result = _read(rig, reader)

    assert target.resolve() in opened
    if reader == "defaults":
        assert result == state.Prefs(model="synthetic-canary")
    else:
        label = (
            "an Anthropic federation profile is configured"
            if source == "federation"
            else "apiKeyHelper is set"
        )
        assert result == [f"{rig.sources[source]}: {label}"]


def test_defaults_credential_guard_uses_the_explicit_launch_environment(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rig.credentials / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_CANARY), encoding="utf-8")
    _directory_alias(rig.sources["project"].parent, rig.credentials)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "other-anthropic"))
    opened = _record_reads(monkeypatch)

    result = state.user_defaults(rig.cfg.claude_home, rig.cfg.vault, env=rig.env)

    assert target.resolve() not in opened
    assert result == state.Prefs()


@pytest.mark.parametrize("reader", ["billing", "defaults"])
def test_an_unresolved_settings_alias_is_skipped_before_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    alias = rig.sources["project"]
    original = Path.resolve

    def unresolved(path: Path, strict: bool = False) -> Path:
        if path == alias:
            raise OSError("synthetic resolution failure")
        return original(path, strict=strict)

    opened: list[Path] = []
    monkeypatch.setattr(Path, "resolve", unresolved)
    monkeypatch.setattr(
        osfs, "read_regular", lambda path, _limit: opened.append(Path(path)) or b"{}"
    )

    assert _read(rig, reader) == ([] if reader == "billing" else state.Prefs())
    assert alias not in opened
