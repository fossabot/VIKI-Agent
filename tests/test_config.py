"""config: path resolution, refusals, and the billing preflight (all on synthetic trees)."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from viki_agent import compiled, config

_SHIPPED_MANAGED_DIRS = config._MANAGED_DIRS
# Only stops the test if the fix regresses and the read blocks on the FIFO again; a fixed read
# refuses a FIFO record at once, without waiting for a writer.
HANG_GUARD_S = 5


@pytest.fixture(autouse=True)
def _synthetic_managed_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight never reads this machine's real managed settings in a test."""
    managed = {os_name: tmp_path / "managed" / os_name for os_name in ("win32", "darwin", "linux")}
    monkeypatch.setattr(config, "_MANAGED_DIRS", managed)
    monkeypatch.setattr(config, "_LEGACY_MANAGED_SETTINGS", tmp_path / "legacy-managed.json")


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "VIKI"
    (vault / ".claude").mkdir(parents=True)
    (vault / "CLAUDE.md").write_text("# vault", encoding="utf-8")
    return vault


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "VIKI_VAULT": str(_vault(tmp_path)),
        "VIKI_CLAUDE_CLI": sys.executable,
        "LOCALAPPDATA": str(tmp_path / "local"),
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "local"),
        **extra,
    }


def _data_dir(tmp_path: Path) -> Path:
    """Where _env() puts the default data directory on the running OS."""
    if sys.platform == "darwin":
        return tmp_path / "home" / "Library" / "Application Support" / "viki-agent"
    return tmp_path / "local" / "viki-agent"


def _exe(path: Path) -> Path:
    """A fresh placeholder executable at `path`, including its parent directories; marked
    executable so a POSIX host's real `resolve_exe` (checked for real on whichever host runs
    this, whatever `platform` string a test passes to `installed_cli`) accepts it too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if sys.platform != "win32":
        path.chmod(0o700)
    return path


def test_load_resolves_paths_from_the_environment(tmp_path: Path) -> None:
    cfg = config.load(_env(tmp_path))
    assert cfg.vault == (tmp_path / "VIKI").resolve()
    assert cfg.cli == Path(sys.executable)
    assert cfg.data_dir == _data_dir(tmp_path)
    assert cfg.state_file == cfg.data_dir / "state.json"
    assert (cfg.cli_problem, cfg.vault_problem, cfg.claude_config_dir_set) == (None, None, False)


def test_load_defaults_to_the_bundled_cli(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["VIKI_CLAUDE_CLI"]
    assert config.load(env).cli == config.BUNDLED_CLI


def test_the_bundled_cli_is_the_sdk_wheels_executable_for_this_os() -> None:
    assert config.BUNDLED_CLI.name == ("claude.exe" if sys.platform == "win32" else "claude")


def test_repo_is_the_compiled_install_dir() -> None:
    """config._REPO is compiled.INSTALL_DIR itself (ADR-054), not a second computation of the
    same path: a compiled launch's install_dir must reach every reader through one place."""
    assert config._REPO is compiled.INSTALL_DIR


def test_load_never_refuses_a_launch_and_records_what_is_wrong(tmp_path: Path) -> None:
    """The app opens without a vault, without a CLI and with CLAUDE_CONFIG_DIR set.
    Each of those used to be a ConfigError that returned 2 with a message box and no window."""
    env = _env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path))
    env["VIKI_VAULT"] = str(tmp_path / "no-such-folder")

    cfg = config.load(env)

    assert cfg.claude_config_dir_set is True
    assert (cfg.vault, cfg.vault_problem) == (None, "not_found")


@pytest.mark.skipif(sys.platform != "win32", reason="batch shims are a Windows refusal")
def test_a_batch_shim_cli_is_recorded_not_used(tmp_path: Path) -> None:
    """CreateProcess cannot run a .cmd directly, so the launch records it instead of failing."""
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    cfg = config.load(_env(tmp_path, VIKI_CLAUDE_CLI=str(shim)))
    assert (cfg.cli, cfg.cli_problem) == (None, "batch_shim")


def test_load_records_a_missing_cli_without_a_vault_at_all(tmp_path: Path) -> None:
    env = _env(tmp_path, VIKI_CLAUDE_CLI=str(tmp_path / "claude.exe"))
    del env["VIKI_VAULT"]

    cfg = config.load(env)

    assert (cfg.cli, cfg.cli_problem) == (None, "not_found")
    assert (cfg.vault, cfg.vault_problem) == (None, None)  # no override is not a problem


def test_installed_cli_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each OS's install locations, most specific first, PATH last; a candidate whose own
    environment variable is unset is skipped rather than guessed some other way (never
    Path.home())."""
    # win32: USERPROFILE's native install, then LOCALAPPDATA's WinGet link, then its package
    # folder, then APPDATA's npm install - created in reverse priority to prove each one wins
    # over what comes after it in the list.
    user_profile, local_appdata, appdata = (tmp_path / p for p in ("profile", "local", "roaming"))
    win_env = {
        "USERPROFILE": str(user_profile),
        "LOCALAPPDATA": str(local_appdata),
        "APPDATA": str(appdata),
    }
    assert config.installed_cli("win32", win_env) is None
    npm = _exe(
        appdata / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )
    assert config.installed_cli("win32", win_env) == npm
    winget_pkg = _exe(
        local_appdata / "Microsoft" / "WinGet" / "Packages" / config._WINGET_PACKAGE / "claude.exe"
    )
    assert config.installed_cli("win32", win_env) == winget_pkg
    winget_link = _exe(local_appdata / "Microsoft" / "WinGet" / "Links" / "claude.exe")
    assert config.installed_cli("win32", win_env) == winget_link
    native_win = _exe(user_profile / ".local" / "bin" / "claude.exe")
    assert config.installed_cli("win32", win_env) == native_win
    # USERPROFILE unset: the native candidate can never be built, even though a file still sits
    # at that exact path, so WinGet's link wins instead.
    without_profile = {k: v for k, v in win_env.items() if k != "USERPROFILE"}
    assert config.installed_cli("win32", without_profile) == winget_link

    # darwin: ~/.local/bin/claude, then the two fixed Homebrew prefixes.
    mac_home = tmp_path / "mac-home"
    brew_arm, brew_intel = tmp_path / "opt-homebrew" / "claude", tmp_path / "usr-local" / "claude"
    monkeypatch.setattr(config, "_MAC_INSTALLS", (brew_arm, brew_intel))
    mac_env = {"HOME": str(mac_home)}
    assert config.installed_cli("darwin", mac_env) is None
    _exe(brew_intel)
    assert config.installed_cli("darwin", mac_env) == brew_intel
    _exe(brew_arm)
    assert config.installed_cli("darwin", mac_env) == brew_arm
    native_mac = _exe(mac_home / ".local" / "bin" / "claude")
    assert config.installed_cli("darwin", mac_env) == native_mac
    assert config.installed_cli("darwin", {}) == brew_arm  # HOME unset: Homebrew still answers

    # linux: ~/.local/bin/claude only, then PATH.
    linux_home = tmp_path / "linux-home"
    linux_env = {"HOME": str(linux_home)}
    assert config.installed_cli("linux", linux_env) is None
    native_linux = _exe(linux_home / ".local" / "bin" / "claude")
    assert config.installed_cli("linux", linux_env) == native_linux
    path_dir = tmp_path / "path-only"
    on_path = _exe(path_dir / "claude")
    assert config.installed_cli("linux", {"PATH": str(path_dir)}) == on_path


def test_installed_cli_skips_shims_and_relative_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`installed_cli`'s PATH fallback only ever looks up the exact name "claude.exe", so a
    .cmd/.ps1 shim or an extensionless claude beside it is never what gets found; an empty or
    relative PATH entry, including the current directory, answers nothing either."""
    monkeypatch.chdir(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude.cmd").write_text("@echo off", encoding="utf-8")
    (bin_dir / "claude.ps1").write_text("", encoding="utf-8")
    (bin_dir / "claude").write_bytes(b"")  # extensionless: not the win32 name being searched for
    _exe(tmp_path / "claude.exe")  # the current directory: never found either
    env = {"PATH": os.pathsep.join(["", ".", str(bin_dir)])}

    assert config.installed_cli("win32", env) is None

    real = _exe(bin_dir / "claude.exe")
    assert config.installed_cli("win32", env) == real


def test_load_finds_the_users_own_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through config.load(): outside a source checkout, with no override, the app runs the
    user's own installation and records where it came from."""
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", False)
    env = _env(tmp_path)
    del env["VIKI_CLAUDE_CLI"]
    if sys.platform == "win32":
        env["USERPROFILE"] = str(tmp_path / "profile")
        claude = _exe(tmp_path / "profile" / ".local" / "bin" / "claude.exe")
    else:
        claude = _exe(Path(env["HOME"]) / ".local" / "bin" / "claude")

    cfg = config.load(env)

    assert (cfg.cli, cfg.cli_problem, cfg.cli_source) == (claude, None, "installed")


def test_the_sdk_copy_counts_only_in_a_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product build ships no frontend/package.json, so the SDK's own bundled copy —
    present in any dev venv built from this checkout — must never be picked up outside one, even
    though resolve_exe(BUNDLED_CLI) would happily accept it."""
    monkeypatch.setattr(
        config, "_MAC_INSTALLS", (tmp_path / "no-brew-arm", tmp_path / "no-brew-intel")
    )
    env = _env(tmp_path)
    del env["VIKI_CLAUDE_CLI"]

    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", True)
    assert config.load(env).cli_source == "bundled"

    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", False)
    cfg = config.load(env)
    assert cfg.cli_source != "bundled"
    assert (cfg.cli, cfg.cli_problem) == (None, "not_found")  # nothing installed in this env


def test_a_broken_override_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIKI_CLAUDE_CLI never reaches for the SDK's bundled copy or the user's own install,
    broken or not; its source reads "bundled" only when it names BUNDLED_CLI exactly,
    "override" otherwise."""
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", True)  # a bundled copy is real and available
    env = _env(tmp_path, VIKI_CLAUDE_CLI=str(tmp_path / "no-such-claude.exe"))

    cfg = config.load(env)

    assert (cfg.cli, cfg.cli_problem, cfg.cli_source) == (None, "not_found", "override")

    env["VIKI_CLAUDE_CLI"] = str(config.BUNDLED_CLI)
    bundled = config.load(env)
    assert bundled.cli_source == "bundled"


@pytest.mark.parametrize("source_checkout", [True, False])
def test_a_resolving_override_reports_its_own_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_checkout: bool
) -> None:
    """An override that resolves to something other than the SDK's bundled copy reads
    "override", whether or not a source checkout would otherwise prefer the bundled copy -
    _SOURCE_CHECKOUT never reaches the override branch at all."""
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", source_checkout)
    cfg = config.load(_env(tmp_path))  # VIKI_CLAUDE_CLI=sys.executable, never BUNDLED_CLI
    assert cfg.cli_source == "override"
    assert cfg.cli == Path(sys.executable)


def test_a_vault_override_inside_a_denied_root_is_recorded_not_used(tmp_path: Path) -> None:
    """The override runs the same canonicalisation a registration does, so a launch and the
    Workspaces screen can never disagree about whether a root is usable."""
    inside = _data_dir(tmp_path) / "vault"
    inside.mkdir(parents=True)
    cfg = config.load(_env(tmp_path, VIKI_VAULT=str(inside)))
    assert cfg.vault is None
    assert cfg.vault_problem == "denied_location"


def test_data_dir_is_refused_inside_every_protected_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard rule 1: the app's own files never land in a vault, ~/.claude or ~/.codex, and the
    check sits in data_dir() so the launcher can run it before it opens its log file."""
    env = _env(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() on Windows
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # Path.home() on macOS and Linux
    for inside in (
        tmp_path / "VIKI" / "10-projects" / "data",
        tmp_path / "home" / ".claude" / "viki",
        tmp_path / "home" / ".codex" / "viki",
    ):
        with pytest.raises(config.ConfigError, match="inside"):
            config.data_dir({**env, "VIKI_DATA_DIR": str(inside)})
    assert config.data_dir(env) == _data_dir(tmp_path)


def test_the_default_data_dir_follows_each_os_convention() -> None:
    windows = {"LOCALAPPDATA": r"D:\Profiles\u\AppData\Local"}
    assert config._data_dir_for("win32", windows) == Path(windows["LOCALAPPDATA"], "viki-agent")
    assert config._data_dir_for("darwin", {"HOME": "/Users/u"}) == Path(
        "/Users/u/Library/Application Support/viki-agent"
    )
    home = {"HOME": "/home/u"}
    assert config._data_dir_for("linux", home) == Path("/home/u/.local/share/viki-agent")
    xdg = {**home, "XDG_DATA_HOME": "/data/xdg"}
    assert config._data_dir_for("linux", xdg) == Path("/data/xdg/viki-agent")
    # The XDG spec says a relative XDG_DATA_HOME is invalid and is ignored.
    relative = {**home, "XDG_DATA_HOME": "xdg"}
    assert config._data_dir_for("linux", relative) == Path("/home/u/.local/share/viki-agent")


def test_the_browser_override_wins_on_every_os(tmp_path: Path) -> None:
    exe = str(tmp_path / "any-chromium")
    for platform in ("win32", "darwin", "linux"):
        assert config._browser_for(platform, {"VIKI_EDGE": exe}) == (Path(exe), "override")


def test_macos_prefers_edge_then_chrome_then_chromium_in_either_applications_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "Applications"
    monkeypatch.setattr(config, "_MAC_APPLICATIONS", system)
    env = {"HOME": str(tmp_path / "home")}

    def bundle(apps: Path, name: str) -> Path:
        exe = apps / f"{name}.app" / "Contents" / "MacOS" / name
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        return exe

    assert config._browser_for("darwin", env) == (None, "")
    chromium = bundle(system, "Chromium")
    assert config._browser_for("darwin", env) == (chromium, "chromium")
    chrome = bundle(tmp_path / "home" / "Applications", "Google Chrome")
    assert config._browser_for("darwin", env) == (chrome, "chrome")
    edge = bundle(system, "Microsoft Edge")
    assert config._browser_for("darwin", env) == (edge, "edge")


@pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup of executable scripts is POSIX")
def test_linux_takes_the_first_browser_on_path_and_skips_snap_and_flatpak(tmp_path: Path) -> None:
    def script(folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o700)
        return exe

    bin_dir = tmp_path / "bin"
    flatpak = tmp_path / "flatpak" / "exports" / "bin"
    snap_bin = tmp_path / "snap-bin"
    snap_wrapper = tmp_path / "snap-wrapper"
    snap_bin.mkdir()
    (snap_bin / "microsoft-edge").symlink_to(script(tmp_path / "usr", "snap"))
    script(flatpak, "google-chrome")
    # bin_dir before snap_wrapper: once the real chromium-browser below exists, it wins over the
    # wrapper for the same name.
    env = {"PATH": os.pathsep.join(map(str, (snap_bin, flatpak, bin_dir, snap_wrapper)))}

    assert config._browser_for("linux", env) == (None, "")
    snap_wrapper.mkdir()
    wrapper = snap_wrapper / "chromium-browser"
    wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    assert config._browser_for("linux", env) == (None, "")  # Ubuntu's snap wrapper, skipped
    chromium = script(bin_dir, "chromium-browser")
    assert config._browser_for("linux", env) == (chromium, "chromium")  # real script, not skipped
    chrome = script(bin_dir, "google-chrome-stable")
    assert config._browser_for("linux", env) == (chrome, "chrome")
    edge = script(bin_dir, "microsoft-edge-stable")
    assert config._browser_for("linux", env) == (edge, "edge")


def test_confined_recognises_a_snap_path_without_touching_the_filesystem() -> None:
    assert config._confined("/snap/bin/chromium") is True


def test_load_records_the_browser_kind(tmp_path: Path) -> None:
    cfg = config.load(_env(tmp_path, VIKI_EDGE=str(tmp_path / "browser")))
    assert (cfg.edge, cfg.browser_kind) == (tmp_path / "browser", "override")


def _cfg(tmp_path: Path) -> config.Config:
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    return config.Config(
        vault=_vault(tmp_path),
        claude_home=home,
        cli=Path(sys.executable),
        edge=None,
        data_dir=tmp_path / "data",
    )


def test_billing_preflight_is_clear_for_plain_settings(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    plain = {
        "DISABLE_TELEMETRY": "1",
        "ANTHROPIC_MODEL": "opus",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "x",
    }
    (cfg.claude_home / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}, "env": plain}), encoding="utf-8"
    )
    assert config.billing_problems(cfg, env={}) == []


def test_billing_preflight_flags_every_provider_switch(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    switches = [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_MANTLE",
    ]
    (cfg.claude_home / "settings.json").write_text(
        json.dumps({"env": dict.fromkeys(switches, "1")}), encoding="utf-8"
    )
    assert len(config.billing_problems(cfg, env={})) == len(switches)


def test_billing_preflight_flags_api_key_routes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    vault = cast(Path, cfg.vault)
    (cfg.claude_home / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "get-key.cmd"}), encoding="utf-8"
    )
    (vault / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://proxy.invalid"}}), encoding="utf-8"
    )
    anthropic = tmp_path / "anthropic"
    (anthropic / "configs").mkdir(parents=True)
    (anthropic / "configs" / "profile.json").write_text(
        json.dumps({"type": "oidc_federation"}), encoding="utf-8"
    )
    found = config.billing_problems(cfg, env={"ANTHROPIC_CONFIG_DIR": str(anthropic)})
    assert len(found) == 3
    assert any("apiKeyHelper" in p for p in found)
    assert any("ANTHROPIC_BASE_URL" in p for p in found)
    assert any("federation" in p for p in found)


def test_billing_preflight_reads_settings_written_with_a_utf8_bom(tmp_path: Path) -> None:
    """PowerShell writes JSON with a UTF-8 BOM, and plain utf-8 leaves that BOM in the string for
    json.loads to reject — which turned the whole file into "says nothing" and let this preflight
    report a machine configured to bill elsewhere as clear."""
    cfg = _cfg(tmp_path)
    vault = cast(Path, cfg.vault)
    (cfg.claude_home / "settings.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"apiKeyHelper": "get-key.cmd"}).encode("utf-8")
    )
    (vault / ".claude" / "settings.local.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}).encode("utf-8")
    )
    found = config.billing_problems(cfg, env={})
    assert len(found) == 2
    assert any("apiKeyHelper" in p for p in found)
    assert any("CLAUDE_CODE_USE_BEDROCK" in p for p in found)


def test_billing_preflight_reads_no_project_settings_without_a_vault(tmp_path: Path) -> None:
    """With no vault workspace the CLI runs in the shared scratch root, which has no .claude of
    its own. Reading a vault-relative path here would mean reading the process working
    directory's project settings — someone else's folder, applied to every chat-only session."""
    cfg = _cfg(tmp_path)
    vault = cast(Path, cfg.vault)
    (vault / ".claude" / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "get-key.cmd"}), encoding="utf-8"
    )
    assert len(config.billing_problems(cfg, env={})) == 1
    assert config.billing_problems(replace(cfg, vault=None), env={}) == []


def test_a_settings_source_that_cannot_be_decoded_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """utf-8-sig still cannot read UTF-16 (what PowerShell's Out-File emits), so the preflight can
    still go blind on a file that exists. It stays a warning rather than a problem string: a
    problem here refuses the launch, and a stray unparseable .json must never be able to do that
    — but the blind spot shows in the log instead of staying invisible."""
    cfg = _cfg(tmp_path)
    broken = cfg.claude_home / "settings.json"
    broken.write_bytes(json.dumps({"apiKeyHelper": "get-key.cmd"}).encode("utf-16"))
    with caplog.at_level(logging.WARNING, logger="viki_agent.config"):
        assert config.billing_problems(cfg, env={}) == []
    assert any(str(broken) in r.getMessage() for r in caplog.records)


def test_a_settings_source_nested_too_deep_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """json.loads raises RecursionError, not ValueError, on JSON nested this deep. It is the same
    blind spot as a file that cannot be decoded at all: a warning, never a crashed preflight."""
    cfg = _cfg(tmp_path)
    nested = cfg.claude_home / "settings.json"
    nested.write_text("[" * 200_000, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="viki_agent.config"):
        assert config.billing_problems(cfg, env={}) == []
    assert any(str(nested) in r.getMessage() for r in caplog.records)


def test_the_managed_settings_directory_is_each_os_own() -> None:
    assert {
        "win32": Path(r"C:\Program Files\ClaudeCode"),
        "darwin": Path("/Library/Application Support/ClaudeCode"),
        "linux": Path("/etc/claude-code"),
    } == _SHIPPED_MANAGED_DIRS


def test_managed_sources_list_the_file_then_its_drop_ins_and_the_legacy_path_on_windows(
    tmp_path: Path,
) -> None:
    for os_name in ("win32", "darwin", "linux"):
        drop_ins = config._MANAGED_DIRS[os_name] / "managed-settings.d"
        drop_ins.mkdir(parents=True)
        for name in ("20-b.json", "10-a.json", "notes.txt"):
            (drop_ins / name).write_text("{}", encoding="utf-8")
        (drop_ins / "nested.json").mkdir()
    for os_name in ("win32", "darwin", "linux"):
        managed = config._MANAGED_DIRS[os_name]
        expected = [
            managed / "managed-settings.json",
            managed / "managed-settings.d" / "10-a.json",
            managed / "managed-settings.d" / "20-b.json",
        ]
        if os_name == "win32":
            expected.append(tmp_path / "legacy-managed.json")
        assert config._managed_sources(os_name) == expected
    # Any other POSIX system reads Linux's directory.
    assert config._managed_sources("freebsd") == config._managed_sources("linux")


def test_billing_preflight_reads_managed_drop_ins_below_the_size_cap(tmp_path: Path) -> None:
    cfg = replace(_cfg(tmp_path), vault=None)
    managed = config._MANAGED_DIRS.get(sys.platform, config._MANAGED_DIRS["linux"])
    drop_ins = managed / "managed-settings.d"
    drop_ins.mkdir(parents=True)
    (drop_ins / "50-helper.json").write_text(
        json.dumps({"apiKeyHelper": "get-key.sh"}), encoding="utf-8"
    )
    oversized = json.dumps({"apiKeyHelper": "x", "pad": "p" * config._MAX_SCANNED_BYTES})
    (drop_ins / "60-oversized.json").write_text(oversized, encoding="utf-8")

    found = config.billing_problems(cfg, env={})

    assert found == [f"{drop_ins / '50-helper.json'}: apiKeyHelper is set"]


@pytest.mark.skipif(sys.platform == "win32", reason="a plain user cannot always create a symlink")
def test_a_linked_managed_settings_drop_in_is_reported(tmp_path: Path) -> None:
    """The CLI applies managed-settings.d drop-ins even when they are links (every
    environment.etc entry on NixOS is one); the preflight must read them too."""
    cfg = replace(_cfg(tmp_path), vault=None)
    managed = config._MANAGED_DIRS.get(sys.platform, config._MANAGED_DIRS["linux"])
    drop_ins = managed / "managed-settings.d"
    drop_ins.mkdir(parents=True)
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"apiKeyHelper": "get-key.sh"}), encoding="utf-8")
    (drop_ins / "50-linked.json").symlink_to(real)

    found = config.billing_problems(cfg, env={})

    assert found == [f"{drop_ins / '50-linked.json'}: apiKeyHelper is set"]


def test_a_dot_named_drop_in_is_not_reported(tmp_path: Path) -> None:
    """The CLI's drop-in reader skips a dotfile; a disabled drop-in it never applies must not
    block every session start."""
    cfg = replace(_cfg(tmp_path), vault=None)
    managed = config._MANAGED_DIRS.get(sys.platform, config._MANAGED_DIRS["linux"])
    drop_ins = managed / "managed-settings.d"
    drop_ins.mkdir(parents=True)
    (drop_ins / ".disabled.json").write_text(
        json.dumps({"apiKeyHelper": "get-key.sh"}), encoding="utf-8"
    )

    assert config.billing_problems(cfg, env={}) == []


def test_billing_preflight_reads_profile_configs_and_never_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only configs/<profile>.json carries the federation block; credentials/<profile>.json
    beside it holds tokens, and the app never reads credentials."""
    cfg = replace(_cfg(tmp_path), vault=None)
    anthropic = tmp_path / "anthropic"
    federation = json.dumps({"federation_rule_id": "r"})
    for folder in ("configs", "credentials", "configs/nested"):
        (anthropic / folder).mkdir(parents=True)
        (anthropic / folder / "p.json").write_text(federation, encoding="utf-8")
    (anthropic / "profile.json").write_text(federation, encoding="utf-8")
    opened: list[Path] = []
    read_json = config._read_json

    def recording(path: Path) -> dict[str, object]:
        opened.append(path)
        return read_json(path)

    monkeypatch.setattr(config, "_read_json", recording)

    found = config.billing_problems(cfg, env={"ANTHROPIC_CONFIG_DIR": str(anthropic)})

    profile = anthropic / "configs" / "p.json"
    assert found == [f"{profile}: an Anthropic federation profile is configured"]
    assert [p for p in opened if p.is_relative_to(anthropic)] == [profile]


@pytest.mark.skipif(sys.platform == "win32", reason="a plain user cannot always create a symlink")
@pytest.mark.parametrize("link_kind", ["file", "folder"])
def test_billing_preflight_never_opens_a_symlinked_profile_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_kind: str
) -> None:
    """A configs/p.json that links into credentials/ is never opened: the configs/ loop skips a
    path whose resolved target lies under credentials/, even though _scannable itself reads
    symlinks (so a linked managed-settings.d drop-in is still reported). Pinned for both the file
    itself being a link and configs/ itself being a folder link into credentials/: a check that
    only looks at whether the entry itself is a link (`path.is_symlink()`) misses the second
    shape."""
    cfg = replace(_cfg(tmp_path), vault=None)
    anthropic = tmp_path / "anthropic"
    (anthropic / "credentials").mkdir(parents=True)
    (anthropic / "credentials" / "p.json").write_text(
        json.dumps({"federation_rule_id": "r"}), encoding="utf-8"
    )
    if link_kind == "file":
        (anthropic / "configs").mkdir()
        (anthropic / "configs" / "p.json").symlink_to(anthropic / "credentials" / "p.json")
    else:
        (anthropic / "configs").symlink_to(anthropic / "credentials", target_is_directory=True)
    opened: list[Path] = []
    read_json = config._read_json

    def recording(path: Path) -> dict[str, object]:
        opened.append(path)
        return read_json(path)

    monkeypatch.setattr(config, "_read_json", recording)

    found = config.billing_problems(cfg, env={"ANTHROPIC_CONFIG_DIR": str(anthropic)})

    assert found == []
    assert [p for p in opened if p.is_relative_to(anthropic)] == []


def test_the_anthropic_directory_follows_each_os_and_the_override() -> None:
    env = {"APPDATA": r"D:\Roaming", "HOME": "/home/u"}
    assert config._anthropic_dir("win32", env) == Path(r"D:\Roaming", "Anthropic")
    assert config._anthropic_dir("darwin", env) == Path("/home/u/.config/anthropic")
    assert config._anthropic_dir("linux", env) == Path("/home/u/.config/anthropic")
    override = {**env, "ANTHROPIC_CONFIG_DIR": "/srv/anthropic"}
    for os_name in ("win32", "darwin", "linux"):
        assert config._anthropic_dir(os_name, override) == Path("/srv/anthropic")
        # Nothing locates the directory, so nothing is scanned.
        assert config._anthropic_dir(os_name, {}) is None


def test_the_anthropic_directory_prefers_xdg_config_home_off_windows() -> None:
    """Off Windows the bundled CLI's own order puts XDG_CONFIG_HOME/anthropic before
    HOME/.config/anthropic, with no requirement that the value be absolute."""
    env = {"XDG_CONFIG_HOME": "/xdg", "HOME": "/home/u"}
    for os_name in ("darwin", "linux"):
        assert config._anthropic_dir(os_name, env) == Path("/xdg/anthropic")
    # A whitespace-only value is not "set": the HOME fallback still applies.
    blank = {"XDG_CONFIG_HOME": "   ", "HOME": "/home/u"}
    for os_name in ("darwin", "linux"):
        assert config._anthropic_dir(os_name, blank) == Path("/home/u/.config/anthropic")
    # Windows never reads XDG_CONFIG_HOME: it has no such convention.
    win_env = {**env, "APPDATA": r"D:\Roaming"}
    assert config._anthropic_dir("win32", win_env) == Path(r"D:\Roaming", "Anthropic")


def test_a_dot_named_profile_config_is_reported(tmp_path: Path) -> None:
    """The CLI reads the active profile by name, and a profile name may start with a dot
    (`^[A-Za-z0-9_.-]+$`); the dot skip that used to live in the shared `_scannable` must not
    hide a dot-named profile config the way it rightly hides a disabled drop-in."""
    cfg = replace(_cfg(tmp_path), vault=None)
    anthropic = tmp_path / "anthropic"
    (anthropic / "configs").mkdir(parents=True)
    (anthropic / "configs" / ".work.json").write_text(
        json.dumps({"federation_rule_id": "r"}), encoding="utf-8"
    )

    found = config.billing_problems(cfg, env={"ANTHROPIC_CONFIG_DIR": str(anthropic)})

    profile = anthropic / "configs" / ".work.json"
    assert found == [f"{profile}: an Anthropic federation profile is configured"]


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs need os.mkfifo")
def test_a_settings_source_that_is_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    """A FIFO named exactly like a settings file must not stall the billing preflight, which runs
    before any window opens."""
    assert sys.platform != "win32"
    cfg = replace(_cfg(tmp_path), vault=None)
    fifo = cfg.claude_home / "settings.json"
    os.mkfifo(fifo)

    outcome: list[object] = []

    def call() -> None:
        try:
            outcome.append(config.billing_problems(cfg, env={}))
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread below
            outcome.append(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(HANG_GUARD_S)
    if thread.is_alive():
        # Release the blocked reader before failing, so a failing run never leaves a hung thread.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        thread.join(HANG_GUARD_S)
        pytest.fail("the read blocked on the FIFO instead of refusing it at once")

    assert outcome[0] == []


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS case-insensitivity is macOS only")
def test_data_dir_refuses_a_case_variant_of_a_protected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APFS is case-insensitive by default: a data directory spelled with a different case than
    ~/.claude must still be refused as inside it."""
    assert sys.platform == "darwin"
    env = _env(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    inside = tmp_path / "home" / ".CLAUDE" / "viki"
    with pytest.raises(config.ConfigError, match="inside"):
        config.data_dir({**env, "VIKI_DATA_DIR": str(inside)})


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS case-insensitivity is macOS only")
def test_billing_preflight_skips_a_case_variant_spelling_of_credentials(tmp_path: Path) -> None:
    """A link that spells credentials/ with a different case must still be recognised as the
    protected directory: APFS resolves it to the same file, but a plain string comparison of
    the two spellings would not."""
    assert sys.platform == "darwin"
    cfg = replace(_cfg(tmp_path), vault=None)
    anthropic = tmp_path / "anthropic"
    (anthropic / "credentials").mkdir(parents=True)
    (anthropic / "credentials" / "p.json").write_text(
        json.dumps({"federation_rule_id": "r"}), encoding="utf-8"
    )
    (anthropic / "configs").mkdir()
    (anthropic / "configs" / "p.json").symlink_to(anthropic / "CREDENTIALS" / "p.json")

    found = config.billing_problems(cfg, env={"ANTHROPIC_CONFIG_DIR": str(anthropic)})

    assert found == []
