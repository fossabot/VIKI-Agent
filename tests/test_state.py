"""state.py's own file handling: reading the user's settings, and the state.json handshake.

AppState as the session layer uses it is exercised in tests/conversations/test_session.py;
what is here is the part that touches bytes on disk — encodings the user's tools actually
produce, and a state file written by a different build of this app.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

from viki_agent import state as state_module
from viki_agent.state import AppState, Prefs, user_defaults
from viki_agent.storage import StateError

SID = "0f2ad1c0-7f4a-4b21-9c62-3a1f5d8e0b77"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs and /dev nodes are POSIX")
# Only stops the test if the fix regresses and the read blocks on the FIFO again; a fixed read
# refuses a FIFO record at once, without waiting for a writer.
HANG_GUARD_S = 5


@pytest.fixture(autouse=True)
def _synthetic_anthropic_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defaults guard uses a synthetic credential root, never this host's provider paths."""
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))


def _home_and_vault(tmp_path: Path) -> tuple[Path, Path]:
    home, vault = tmp_path / "home", tmp_path / "vault"
    home.mkdir()
    (vault / ".claude").mkdir(parents=True)
    return home, vault


def test_user_defaults_read_settings_written_with_a_utf8_bom(tmp_path: Path) -> None:
    """PowerShell writes JSON with a BOM and the CLI honours it, so the app must too: a BOM'd
    settings.json read as empty would start every session in a different permission mode from
    the terminal, silently."""
    home, vault = _home_and_vault(tmp_path)
    settings = {"permissions": {"defaultMode": "manual"}, "effortLevel": "low", "model": "opus"}
    (home / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(settings).encode("utf-8"))
    assert user_defaults(home, vault) == Prefs(mode="manual", effort="low", model="opus")


def test_user_defaults_without_a_vault_read_the_provider_home_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat-only launch has no vault workspace. The old signature had nowhere to put that, and
    a `Path()` fallback would have read the folder the app happened to be started from — someone
    else's project settings, applied to every session."""
    home, vault = _home_and_vault(tmp_path)
    (home / "settings.json").write_text(json.dumps({"effortLevel": "low"}), encoding="utf-8")
    (vault / ".claude" / "settings.json").write_text(
        json.dumps({"effortLevel": "max"}), encoding="utf-8"
    )
    monkeypatch.chdir(vault)  # the process working directory is not a settings source

    assert user_defaults(home, None) == Prefs(effort="low")
    assert user_defaults(home, vault) == Prefs(effort="max")


def test_set_defaults_replaces_what_a_new_session_inherits(tmp_path: Path) -> None:
    """A committed change to a chat.* setting refreshes the app's session defaults
    without a restart. An unspawnable value is dropped back to the built-in one, the same way
    every other preference read is scrubbed."""
    state = AppState(tmp_path / "state.json", Prefs(mode="auto", effort="low"))

    state.set_defaults(Prefs(mode="plan", effort="max", model="opus"))
    assert state.prefs(SID) == Prefs(mode="plan", effort="max", model="opus")

    state.set_defaults(Prefs(mode="bypassPermissions", effort="nonsense", model=""))
    assert state.prefs(SID) == Prefs(mode="default", effort="high", model=None)


def test_a_state_file_written_with_a_utf8_bom_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = {"version": 1, "sessions": {SID: {"mode": "plan", "effort": "max", "model": None}}}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    assert AppState(path, Prefs()).prefs(SID) == Prefs(mode="plan", effort="max")


def test_an_unreadable_settings_file_is_empty_but_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """utf-8-sig is not a cure-all: PowerShell's Out-File can emit UTF-16LE, which decodes under
    neither. That still must not stop the app, but it must not be silent either — otherwise
    "my defaults were ignored" has nothing to explain it."""
    home, vault = _home_and_vault(tmp_path)
    broken = home / "settings.json"
    broken.write_bytes(json.dumps({"effortLevel": "max"}).encode("utf-16"))
    with caplog.at_level(logging.WARNING, logger="viki_agent.state"):
        assert user_defaults(home, vault) == Prefs()
    assert any(str(broken) in r.getMessage() for r in caplog.records)


def test_a_settings_file_nested_too_deep_is_empty_but_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """json.loads raises RecursionError, not ValueError, on JSON nested this deep. It is the same
    blind spot as a file that cannot be decoded at all: a warning, never a crashed read."""
    home, vault = _home_and_vault(tmp_path)
    nested = home / "settings.json"
    nested.write_text("[" * 200_000, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="viki_agent.state"):
        assert user_defaults(home, vault) == Prefs()
    assert any(str(nested) in r.getMessage() for r in caplog.records)


def test_a_missing_settings_file_says_nothing_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Most users have no settings.local.json and no state.json yet; absent is normal, and a
    warning for it would train the user to ignore the ones that matter."""
    with caplog.at_level(logging.WARNING, logger="viki_agent.state"):
        assert user_defaults(tmp_path / "missing", tmp_path / "missing") == Prefs()
        assert not AppState(tmp_path / "missing" / "state.json", Prefs()).ours(SID)
    assert caplog.records == []


@pytest.mark.parametrize("version", [2, "1", None, True, 1.0])
def test_a_state_file_from_another_version_is_preserved_and_refused(
    tmp_path: Path, version: object
) -> None:
    """Unknown metadata cannot be reinterpreted or silently replaced on the next save."""
    path = tmp_path / "state.json"
    payload: dict[str, object] = {"sessions": {SID: {"mode": "plan", "effort": "max"}}}
    if version is not None:
        payload["version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(StateError, match="state.json"):
        AppState(path, Prefs(mode="auto", effort="low"))
    assert path.read_bytes() == before
    assert not (tmp_path / "app.sqlite3").exists()


@posix_only
def test_user_defaults_refuses_a_fifo_settings_file_without_blocking(tmp_path: Path) -> None:
    """A FIFO named exactly like a settings file must not stall the launch, which reads user
    defaults before any window opens and again on every settings read."""
    assert sys.platform != "win32"
    home, vault = _home_and_vault(tmp_path)
    fifo = home / "settings.json"
    os.mkfifo(fifo)

    outcome: list[object] = []

    def call() -> None:
        try:
            outcome.append(user_defaults(home, vault))
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

    assert outcome[0] == Prefs()


@posix_only
def test_user_defaults_refuses_a_settings_file_linked_to_a_device(tmp_path: Path) -> None:
    """A settings file linked to /dev/zero must not be read without limit: the old plain
    `read_text()` grows without bound and exhausts memory instead of refusing at once."""
    assert sys.platform != "win32"
    import resource

    home, vault = _home_and_vault(tmp_path)
    (home / "settings.json").symlink_to("/dev/zero")
    # macOS refuses setrlimit(RLIMIT_AS, ...) outright ("current limit exceeds maximum limit"),
    # so the ceiling is set only on Linux; the refusal under test is the same bounded-read code
    # on both, and the read still returns Prefs() there without the ceiling.
    if sys.platform != "darwin":
        previous = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, previous[1]))  # a 1 GiB ceiling
        try:
            assert user_defaults(home, vault) == Prefs()
        finally:
            resource.setrlimit(resource.RLIMIT_AS, previous)
    else:
        assert user_defaults(home, vault) == Prefs()


def test_saved_state_is_read_back_without_a_second_writable_json_store(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    AppState(path, Prefs(mode="auto", effort="low")).remember(SID, mode="plan")
    assert not path.exists()
    assert AppState(path, Prefs(mode="auto", effort="low")).prefs(SID) == Prefs(
        mode="plan", effort="low"
    )


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "null",
        "{}",
        "{",
        '{"version":1,"sessions":[]}',
        '{"version":1,"sessions":{"a":{ "mode":"plan"}, "b":null}}',
        '{"version":1,"sessions":{"a":{},"a":{}}}',
        '{"version":1,"sessions":{"a":{"unknown":"preserve"}}}',
        '{"version":1,"sessions":{"a":{"model":"\\ud800"}}}',
    ],
)
def test_malformed_legacy_state_never_becomes_a_partial_import(tmp_path: Path, text: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StateError) as failure:
        AppState(path, Prefs())
    assert failure.value.code == "invalid_legacy"
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / "app.sqlite3").exists()


def test_oversized_legacy_source_is_preserved_without_unbounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_module, "MAX_LEGACY_BYTES", 256)
    path = tmp_path / "state.json"
    text = '{"version":1,"sessions":{}}' + " " * 256
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StateError) as failure:
        AppState(path, Prefs())
    assert failure.value.code == "invalid_legacy"
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / "app.sqlite3").exists()
