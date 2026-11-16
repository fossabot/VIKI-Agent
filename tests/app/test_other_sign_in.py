"""`chat.other_sign_in`: a launch-time switch that lets native sessions start on whatever sign-in
the user's Claude Code is set up with. Off, every billing gate answers as before; on, a native
start skips the subscription check while a model-connection route keeps its preflight refusal.
Synthetic stores and the replay engine only; no real `claude`."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.test_main import (  # noqa: F401 - the autouse fixtures guard every main() call here
    _cfg,
    _preserve_os_environ,
    _private_instance_names,
    _reaching_the_server,
    alerts,
    nofile_raises,
)
from viki_agent import __main__ as launcher
from viki_agent.app import diagnostics
from viki_agent.app.diagnostics import Readiness
from viki_agent.app.exports import ExportService
from viki_agent.app.imports import ImportService, ImportServiceError
from viki_agent.app.settings import KEYS, SettingsService
from viki_agent.domain import StorageReadiness
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude import driver as driver_module
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

from replay import SID, FakeSpawner, expect, out

KEY = "chat.other_sign_in"
_READY = StorageReadiness(state="ready", reason=None, private=True)
PREFLIGHT = ["settings.json: apiKeyHelper is set"]
ROUTE_MODEL = "viki-llm/00000000-0000-4000-8000-000000000001/example/model"
NO_SUBSCRIPTION = {"apiProvider": "firstParty", "subscriptionType": ""}
BEDROCK = {"apiProvider": "bedrock", "subscriptionType": ""}


def _readiness(*, other: bool, preflight: bool, refused: bool) -> Readiness:
    config = SimpleNamespace(
        cli=Path("claude.exe"), cli_problem=None, claude_config_dir_set=False, cli_source=""
    )
    readiness = Readiness(config=cast(Any, config), launch_id="launch", git_available=lambda: False)
    readiness.observe_billing(PREFLIGHT if preflight else [])
    if refused:
        readiness.observe_start_error("billing")
    if other:  # off is the default: an off row needs nothing the switch added
        readiness.observe_sign_in_choice(True)
    return readiness


# --- the setting ---------------------------------------------------------------------------------


def test_the_key_is_a_personal_next_launch_bool_that_defaults_on() -> None:
    keys = list(KEYS)
    assert keys.index(KEY) == keys.index("chat.llm_connections") + 1
    spec = KEYS[KEY]
    assert (spec.kind, spec.allowed, spec.default) == ("bool", None, True)
    assert (spec.scope, spec.applies, spec.personal) == ("app", "next_launch", True)


def test_the_key_round_trips_and_resets_to_on(tmp_path: Path) -> None:
    service = SettingsService(
        AppState(tmp_path / "state.json", Prefs()).store,
        provider_defaults=Prefs,
        on_chat_defaults=lambda _prefs: None,
    )
    assert service.flag(KEY) is True and launcher._other_sign_in(service) is True
    saved, _replayed = service.patch(str(uuid.uuid4()), 0, {KEY: False})
    assert saved.effective[KEY] is False and saved.sources[KEY] == "setting"
    assert launcher._other_sign_in(service) is False
    reset, _replayed = service.patch(str(uuid.uuid4()), saved.document.revision, {KEY: None})
    assert reset.effective[KEY] is True and reset.sources[KEY] == "default"
    assert launcher._other_sign_in(service) is True


@pytest.mark.parametrize(("answer", "allowed"), [(None, False), (False, False), (True, True)])
def test_only_a_readable_true_turns_the_switch_on(answer: bool | None, allowed: bool) -> None:
    """`flag` answers None for a read-only document or a failed store: that reads as off."""
    asked: list[str] = []

    def flag(key: str) -> bool | None:
        asked.append(key)
        return answer

    assert launcher._other_sign_in(cast(Any, SimpleNamespace(flag=flag))) is allowed
    assert asked == [KEY]


def test_a_newer_settings_document_reads_as_off(tmp_path: Path) -> None:
    store = AppState(tmp_path / "state.json", Prefs()).store
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO settings VALUES ('app',2,1,?,?)",
            (json.dumps({KEY: True}), "2026-01-01T00:00:00.000Z"),
        )
    service = SettingsService(store, provider_defaults=Prefs, on_chat_defaults=lambda _: None)
    assert service.flag(KEY) is None
    assert launcher._other_sign_in(service) is False


@pytest.mark.parametrize("identity", [False, True])
def test_a_settings_export_carries_the_switch_only_with_identity(
    tmp_path: Path, identity: bool
) -> None:
    exports = ExportService(
        diagnostics_document=lambda _paths, _identity: {},
        redact=lambda text, roots: diagnostics.redact(text, roots=roots),
        roots=lambda: (),
        settings_export=lambda: {KEY: True, "appearance.theme": "dark"},
        settings_specs=KEYS,
        workspaces_export=lambda: (),
        denied_roots=lambda: (),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "unused.log",
        app_version="synthetic",
        launch_id="synthetic",
    )
    preview = exports.preview("settings", {"identity": identity})
    values = json.loads(preview.content[1])["settings"]["values"]
    expected: dict[str, object] = {"appearance.theme": "dark"}
    if identity:
        expected[KEY] = True
    assert values == expected


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("incoming", [False, True])
def test_a_settings_import_never_changes_the_switch(
    tmp_path: Path, current: bool, incoming: bool
) -> None:
    """Only the Chat page sets it: an import file, even this app's own export with Identity,
    shows the key as an invalid row that cannot be selected, in either direction."""
    service = SettingsService(
        AppState(tmp_path / "state.json", Prefs()).store,
        provider_defaults=Prefs,
        on_chat_defaults=lambda _prefs: None,
    )
    service.patch(str(uuid.uuid4()), 0, {KEY: current})
    imports = ImportService(
        settings_specs=KEYS, current_settings=service.import_snapshot, patch=service.patch
    )
    document = {
        "viki_export": {"kind": "settings", "format_version": 1},
        "settings": {"values": {KEY: incoming, "appearance.theme": "dark"}},
    }
    preview = imports.preview("settings", json.dumps(document))
    rows = {change.key: change.action for change in preview.changes}
    assert rows == {KEY: "invalid_value", "appearance.theme": "apply"}
    with pytest.raises(ImportServiceError) as refused:
        imports.apply(
            preview.preview_id,
            command_id=str(uuid.uuid4()),
            expected_revision=preview.settings_revision,
            keys=[KEY],
        )
    assert refused.value.code == "invalid_selection"
    imports.apply(
        preview.preview_id,
        command_id=str(uuid.uuid4()),
        expected_revision=preview.settings_revision,
        keys=["appearance.theme"],
    )
    assert service.flag(KEY) is current


# --- launch: read once ---------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "allowed"), [(None, False), (False, False), (True, True)])
def test_the_launch_reads_the_switch_once_before_sessions_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool | None, allowed: bool
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    answers: dict[str, bool | None] = {KEY: answer}
    asked: list[str] = []
    real_flag = SettingsService.flag

    def flag(self: SettingsService, key: str) -> bool | None:
        if key != KEY:
            return real_flag(self, key)
        asked.append(key)
        return answers[key]

    monkeypatch.setattr(launcher.SettingsService, "flag", flag)

    assert launcher.main([]) == 0

    readiness = calls["create_app"]["readiness"]
    assert asked == [KEY] and readiness.other_sign_in is allowed
    # A change after the launch waits for the next one.
    answers[KEY] = not allowed
    assert readiness.other_sign_in is allowed and asked == [KEY]


# --- readiness ------------------------------------------------------------------------------------


@pytest.mark.parametrize("refused", [False, True], ids=["no_refusal", "refused"])
@pytest.mark.parametrize("preflight", [False, True], ids=["clean", "preflight"])
@pytest.mark.parametrize("other", [False, True], ids=["off", "on"])
def test_readiness_lifts_only_the_native_block_while_the_switch_is_on(
    other: bool, preflight: bool, refused: bool
) -> None:
    readiness = _readiness(other=other, preflight=preflight, refused=refused)
    blocked = not other and (preflight or refused)
    provider = readiness.capabilities(None, _READY).provider["claude"]
    assert readiness.billing == provider.billing == ("blocked" if blocked else "clear")
    assert provider.can_start is not blocked
    # The raw preflight and its problem codes stay visible either way.
    assert readiness.billing_preflight_blocked is preflight
    assert ("api_key_helper" in provider.problems) is preflight


def test_an_unchecked_billing_state_stays_unknown_with_the_switch_on() -> None:
    config = SimpleNamespace(
        cli=Path("claude.exe"), cli_problem=None, claude_config_dir_set=False, cli_source=""
    )
    readiness = Readiness(config=cast(Any, config), launch_id="launch", git_available=lambda: False)
    assert readiness.other_sign_in is False
    readiness.observe_sign_in_choice(True)
    assert readiness.billing == "unknown"


# --- session start gate ---------------------------------------------------------------------------


def _manager(tmp_path: Path, readiness: Readiness, script: list[dict[str, Any]]) -> SessionManager:
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    config = DriverConfig(
        cli=("claude.exe",),
        vault=tmp_path / "vault",
        env={},
        cli_version="2.1.300",
        spawner=FakeSpawner(script),
        grace_s=0.05,
        poll_s=0.05,
        control_timeout_s=0.05,
    )
    return SessionManager(
        config=config,
        state=AppState(tmp_path / "state.json", Prefs()),
        claude_home=home,
        readiness=readiness,
    )


@pytest.mark.parametrize("refused", [False, True], ids=["no_refusal", "refused"])
@pytest.mark.parametrize("preflight", [False, True], ids=["clean", "preflight"])
@pytest.mark.parametrize("route", [False, True], ids=["native", "route"])
@pytest.mark.parametrize("other", [False, True], ids=["off", "on"])
async def test_the_start_gate_follows_the_truth_table(
    tmp_path: Path, other: bool, route: bool, preflight: bool, refused: bool
) -> None:
    readiness = _readiness(other=other, preflight=preflight, refused=refused)
    readiness.observe_version("2.1.300")
    manager = _manager(tmp_path, readiness, [])
    prefs = Prefs(model=ROUTE_MODEL if route else None)
    refuses = preflight if route else not other and (preflight or refused)
    try:
        made = manager._claude_driver(Launch(SID), prefs, lambda _event: None)
    except StartError as error:
        code = error.code
    else:
        code = None
        assert isinstance(made, ClaudeDriver)
        if other:  # the off rows' driver check is the account test below
            assert made._config.subscription_only is False
    if refuses:
        assert code == "billing"
    elif route:
        # Past the billing gate: this test binds no connection store, so the route stops there.
        assert code == "bad_request"
    else:
        assert code is None


# --- driver: the initialize reply's account -------------------------------------------------------


def _initialize_with(account: dict[str, str]) -> list[dict[str, Any]]:
    reply = {"account": account, "commands": []}
    return [
        expect(
            {
                "type": "control_request",
                "request_id": "rec-init",
                "request": {"subtype": "initialize"},
            }
        ),
        out(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": "rec-init", "response": reply},
            }
        ),
    ]


@pytest.mark.parametrize("preflight", [False, True], ids=["clean", "preflight"])
@pytest.mark.parametrize("account", [NO_SUBSCRIPTION, BEDROCK], ids=["no_subscription", "bedrock"])
@pytest.mark.parametrize("other", [False, True], ids=["off", "on"])
async def test_a_native_start_checks_the_subscription_only_while_the_switch_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    other: bool,
    account: dict[str, str],
    preflight: bool,
) -> None:
    checked: list[object] = []
    real_check = driver_module.check_billing

    def check_billing(info: Any) -> None:
        checked.append(info.get("account"))
        real_check(info)

    monkeypatch.setattr(driver_module, "check_billing", check_billing)
    readiness = _readiness(other=other, preflight=preflight, refused=False)
    manager = _manager(tmp_path, readiness, _initialize_with(account))
    actor = manager.create(SID)
    events: list[dict[str, Any]] = []
    actor.subscribe(events.append)

    started = await actor.start()

    errors = [e["data"] for e in events if e["type"] == "error"]
    if other:
        assert started is True and errors == [] and checked == []
        assert readiness.billing == "clear"
    else:
        assert started is False and errors[0]["code"] == "billing"
        # A preflight conflict refuses before any process; otherwise the driver's check does.
        assert checked == ([] if preflight else [account])
        assert readiness.billing == "blocked"
    await manager.close_all()
