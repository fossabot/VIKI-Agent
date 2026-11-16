"""diagnostics.py: readiness facts, the diagnostics document and redaction.

Every probe here is synthetic (a fake config, a fake git_available callable, a fake CLI/git
version function): nothing spawns claude.exe or a real PowerShell, and nothing reads a real
credential file.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent import config as config_module
from viki_agent.app import diagnostics
from viki_agent.app.diagnostics import CheckBusy, CheckUnsupported, ProviderChecks, Readiness
from viki_agent.domain import (
    ActiveWorkspace,
    CanonicalRoot,
    GitReadiness,
    Layout,
    PolicyKind,
    ProviderProblem,
    RootIdentity,
    StorageReadiness,
    Workspace,
    WorkspaceKind,
    WorkspaceSource,
)

_READY_STORAGE = StorageReadiness(state="ready", reason=None, private=True)


@dataclass
class _FakeConfig:
    """A structural stand-in for config.Config's readiness fields (see diagnostics._ProviderConfig).
    The tests build this directly instead of importing the real Config."""

    cli: Path | None
    cli_problem: ProviderProblem | None = None
    claude_config_dir_set: bool = False
    browser_kind: str = ""
    cli_source: str = ""


def _workspace(
    *,
    kind: WorkspaceKind = "vault",
    workspace_id: str = "w1",
    name: str = "VIKI",
    root_display: str = "C:\\VIKI",
    policy: PolicyKind = "ordinary",
    layout: Layout = "unknown",
    source: WorkspaceSource = "opened",
) -> Workspace:
    root = CanonicalRoot(
        key=root_display.lower(), display=root_display, physical_id="1:1", aliased=False
    )
    identity = RootIdentity(
        git_root_oids=(),
        git_common_dir=None,
        instruction_hashes={},
        established=False,
        inherits_from=None,
        probed_at="2026-01-01T00:00:00.000Z",
    )
    return Workspace(
        workspace_id=workspace_id,
        kind=kind,
        root=root,
        name=name,
        policy=policy,
        policy_revision=1,
        revision=1,
        source=source,
        layout=layout,
        portable_id=None,
        identity=identity,
        face_dir=None,
        bootstrap="none",
        persistent=kind != "scratch",
        created_at="2026-01-01T00:00:00.000Z",
        updated_at="2026-01-01T00:00:00.000Z",
    )


def _active(
    workspace: Workspace,
    *,
    selected_id: str | None,
    selected_by: str = "registration",
    chat_only: bool = False,
) -> ActiveWorkspace:
    return ActiveWorkspace(
        workspace=workspace,
        cwd=Path(workspace.root.display),
        selected_id=selected_id,
        selected_by=selected_by,  # type: ignore[arg-type]
        chat_only=chat_only,
    )


def test_readiness_exposes_billing_cli_state_and_launch_id_as_read_only_properties() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")),
        launch_id="launch-42",
        git_available=lambda: True,
    )
    assert (readiness.billing, readiness.cli_state, readiness.launch_id) == (
        "unknown",
        "ready",
        "launch-42",
    )
    readiness.observe_billing(["settings.json: apiKeyHelper is set"])
    assert readiness.billing == "blocked"


def test_readiness_matrix_reports_each_fact_independently() -> None:
    # missing CLI
    readiness = Readiness(
        config=_FakeConfig(cli=None, cli_problem="not_found"),
        launch_id="l1",
        git_available=lambda: False,
    )
    caps = readiness.capabilities(
        None, StorageReadiness(state="unavailable", reason="missing", private=None)
    )
    provider = caps.provider["claude"]
    assert provider.cli == "missing"
    assert provider.can_start is False
    assert caps.git.state == "missing"
    assert caps.storage.state == "unavailable"

    # batch shim
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.cmd"), cli_problem="batch_shim"),
        launch_id="l2",
        git_available=lambda: True,
    )
    assert readiness._cli_state() == "unusable"
    assert readiness.may_spawn() is False

    # CLAUDE_CONFIG_DIR override wins even over a resolved exe
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe"), claude_config_dir_set=True),
        launch_id="l3",
        git_available=lambda: True,
    )
    assert readiness._cli_state() == "config_dir_override"
    assert readiness.may_spawn() is False
    assert "config_dir_override" in readiness._problems()

    # a ready CLI, a billing problem, a signed-out session
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l4", git_available=lambda: True
    )
    assert readiness.may_spawn() is True
    readiness.observe_billing(["C:\\home\\.claude\\settings.json: env.ANTHROPIC_API_KEY is set"])
    readiness.observe_start_error("not_logged_in")
    caps = readiness.capabilities(None, _READY_STORAGE)
    provider = caps.provider["claude"]
    assert provider.billing == "blocked"
    assert provider.login == "signed_out"
    assert provider.can_start is False
    assert "billing_env_key" in provider.problems

    # a first-party account observed by a real session start signs it in
    readiness.observe_account({"apiProvider": "firstParty"})
    assert readiness.capabilities(None, caps.storage).provider["claude"].login == "signed_in"


def test_git_overridden_reports_first_regardless_of_the_last_probe() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")),
        launch_id="l",
        git_available=lambda: True,
        git_overridden=lambda: True,
    )
    assert readiness.capabilities(None, _READY_STORAGE).git.state == "overridden"
    readiness.observe_git("2.40.0")  # a fresh probe result never beats the override
    assert readiness.capabilities(None, _READY_STORAGE).git.state == "overridden"


def test_git_state_is_the_current_probe_result_never_the_launch_time_copy() -> None:
    """What the identity probes read through the launcher: the launch-time answer until a `git`
    check re-runs the probe, and never "ready" while the scope override is set."""
    overridden = False
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")),
        launch_id="l",
        git_available=lambda: False,
        git_overridden=lambda: overridden,
    )
    assert readiness.git_state == "missing"
    readiness.observe_git("2.40.0")
    assert readiness.git_state == "ready"
    overridden = True
    assert readiness.git_state == "overridden"


def test_observe_git_empty_version_reports_missing() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    readiness.observe_git("")
    assert readiness.capabilities(None, _READY_STORAGE).git == GitReadiness(
        state="missing", version=""
    )


def test_classify_billing_problem_falls_back_to_other() -> None:
    assert diagnostics._classify_billing_problem("nothing recognisable here") == "other"


def test_classify_prefers_env_when_a_path_contains_the_other_keywords() -> None:
    # the path segment can itself contain "apiKeyHelper" or "federation profile" text; the
    # actual condition reported here is an env key, and that must win over the looser matches
    message = "C:\\Users\\x\\apiKeyHelper-backup\\settings.local.json: env.ANTHROPIC_API_KEY is set"
    assert diagnostics._classify_billing_problem(message) == "billing_env_key"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.1.280", "older"),
        ("2.1.281", "tested"),
        ("2.1.282", "tested"),
        ("2.1.283", "newer"),
        ("2.2", "newer"),
        ("", "unknown"),
        ("abc", "unknown"),
    ],
)
def test_version_status_marks_the_tested_range(version: str, expected: str) -> None:
    assert diagnostics.version_status(version) == expected


def test_billing_problems_are_classified_from_real_config_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This pins the classifier against the real message shapes of config.billing_problems(),
    not a guess at their wording."""
    # Never this machine's real managed-settings paths (test_config.py's autouse fixture does
    # the same for test_config.py's own tests).
    managed = {os_name: tmp_path / "managed" / os_name for os_name in ("win32", "darwin", "linux")}
    monkeypatch.setattr(config_module, "_MANAGED_DIRS", managed)
    monkeypatch.setattr(config_module, "_LEGACY_MANAGED_SETTINGS", tmp_path / "legacy-managed.json")

    claude_home = tmp_path / "home" / ".claude"
    claude_home.mkdir(parents=True)
    (claude_home / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "C:\\helper.cmd"}), encoding="utf-8"
    )
    vault = tmp_path / "vault"
    (vault / ".claude").mkdir(parents=True)
    (vault / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-x"}}), encoding="utf-8"
    )
    anthropic_dir = tmp_path / "anthropic"
    (anthropic_dir / "configs").mkdir(parents=True)
    (anthropic_dir / "configs" / "profile.json").write_text(
        json.dumps({"federation": "acme"}), encoding="utf-8"
    )
    cfg = config_module.Config(
        vault=vault, claude_home=claude_home, cli=Path("absent"), edge=None, data_dir=tmp_path / "d"
    )
    problems = config_module.billing_problems(cfg, {"ANTHROPIC_CONFIG_DIR": str(anthropic_dir)})
    codes = {diagnostics._classify_billing_problem(p) for p in problems}
    assert codes == {"api_key_helper", "billing_env_key", "federation_profile"}


def test_restart_required_true_for_a_vault_active_with_no_persisted_selection() -> None:
    workspace = _workspace(kind="vault", workspace_id="w1")
    active = _active(workspace, selected_id=None, selected_by="builtin")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    caps = readiness.capabilities(active, _READY_STORAGE)
    assert caps.workspace["restart_required"] is True


def test_restart_required_false_for_scratch_with_no_persisted_selection() -> None:
    workspace = _workspace(kind="scratch", workspace_id="scratch-1")
    active = _active(workspace, selected_id=None, selected_by="builtin")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    caps = readiness.capabilities(active, _READY_STORAGE)
    assert caps.workspace["restart_required"] is False
    assert caps.workspace["shared_scratch"] is True


def test_restart_required_reflects_a_selection_mismatch_or_match() -> None:
    workspace = _workspace(kind="vault", workspace_id="w1")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    mismatched = _active(workspace, selected_id="w2")
    assert readiness.capabilities(mismatched, _READY_STORAGE).workspace["restart_required"] is True
    matched = _active(workspace, selected_id="w1")
    assert readiness.capabilities(matched, _READY_STORAGE).workspace["restart_required"] is False


def test_root_available_defaults_true_and_is_overridable() -> None:
    workspace = _workspace(kind="vault", workspace_id="w1")
    active = _active(workspace, selected_id="w1")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    caps = readiness.capabilities(active, _READY_STORAGE)
    assert cast("dict[str, Any]", caps.workspace)["active"]["root_available"] is True
    caps = readiness.capabilities(active, _READY_STORAGE, root_available=False)
    assert cast("dict[str, Any]", caps.workspace)["active"]["root_available"] is False


async def test_provider_cli_check_is_unsupported_and_spawns_nothing_under_config_dir_override() -> (
    None
):
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe"), claude_config_dir_set=True),
        launch_id="l",
        git_available=lambda: True,
    )
    calls: list[str] = []
    checks = ProviderChecks(
        readiness=readiness,
        cli_version=lambda: calls.append("cli") or "9.9.9",
        git_version=lambda: "2.40.0",
    )
    with pytest.raises(CheckUnsupported):
        await checks.run("provider_cli")
    assert calls == []  # the probe was never invoked


async def test_checks_serialise_a_second_run_while_one_is_in_flight() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    release = threading.Event()

    def slow_git() -> str:
        release.wait(2)
        return "2.40.0"

    checks = ProviderChecks(readiness=readiness, cli_version=lambda: "1.0.0", git_version=slow_git)
    first = asyncio.create_task(checks.run("git"))
    await asyncio.sleep(0.01)  # let the first run() set _busy before the second is attempted
    with pytest.raises(CheckBusy):
        await checks.run("git")
    release.set()
    result = await first
    assert result.status == "ok"


async def test_check_status_is_failed_when_the_probe_returns_an_empty_version() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: ""
    )
    result = await checks.run("git")
    assert result.status == "failed"
    assert result.result == {"version": ""}
    # the empty probe result is still observed, same as a graceful "not found"
    assert readiness.capabilities(None, _READY_STORAGE).git.state == "missing"


async def test_check_times_out_reports_failed_and_clears_busy() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    release = threading.Event()

    def blocked() -> str:
        release.wait(2)
        return "2.40.0"

    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=blocked, timeout_s=0.01
    )
    result = await checks.run("git")
    assert result.status == "failed"
    assert result.result == {"version": ""}
    assert checks._busy is False
    # a second check can run right away: the timeout did not leave the busy flag set
    second = await checks.run("git")
    assert second.status == "failed"
    release.set()  # let the still-running probe thread finish so it doesn't outlive the test


async def test_check_probe_exception_is_reported_failed_and_logs_only_the_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )

    calls = 0

    def boom() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(r"C:\Users\synthuser\git\git.exe not found")
        return "2.40.0"

    checks = ProviderChecks(readiness=readiness, cli_version=lambda: "1.0.0", git_version=boom)
    with caplog.at_level(logging.WARNING, logger="viki_agent.app.diagnostics"):
        result = await checks.run("git")
    assert result.status == "failed"
    assert result.result == {"version": ""}
    assert checks._busy is False  # the exception did not leave the check stuck busy

    messages = [record.getMessage() for record in caplog.records]
    assert any("FileNotFoundError" in message for message in messages)
    assert not any("synthuser" in message for message in messages)
    assert not any("Traceback" in message for message in messages)

    # busy really was cleared: a second check runs normally right after
    second = await checks.run("git")
    assert second.status == "ok"


def test_document_reports_versions_readiness_and_storage(tmp_path: Path) -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="launch-1", git_available=lambda: True
    )
    readiness.observe_version("2.1.259")
    readiness.observe_git("2.40.0")
    document: dict[str, Any] = diagnostics.diagnostics_document(
        readiness,
        storage=_READY_STORAGE,
        settings=None,
        workspaces=(),
        data_dir=tmp_path / "data",
        log_path=tmp_path / "data" / "logs" / "viki-agent.log",
        include_paths=True,
        include_identity=True,
    )
    assert document["provider"]["claude"]["version"] == "2.1.259"
    assert document["git"] == {"state": "ready", "version": "2.40.0"}
    assert document["storage"] == {"state": "ready", "reason": None, "private": True}
    assert document["settings"] is None  # reports the absence, not a parse failure


def test_document_carries_source_version_status_and_tested_range(tmp_path: Path) -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe"), cli_source="bundled"),
        launch_id="l",
        git_available=lambda: True,
    )
    readiness.observe_version("2.1.281")
    document = diagnostics.diagnostics_document(
        readiness,
        storage=_READY_STORAGE,
        settings=None,
        workspaces=(),
        data_dir=tmp_path / "data",
        log_path=tmp_path / "data" / "log",
        include_paths=False,
        include_identity=False,
    )
    provider = cast("dict[str, Any]", document)["provider"]["claude"]
    assert provider["source"] == "bundled"
    assert provider["version_status"] == "tested"
    assert provider["tested"] == [diagnostics.CLI_TESTED_MIN, diagnostics.CLI_TESTED_MAX]


def test_os_name_maps_the_three_platform_strings() -> None:
    assert diagnostics._os_name("win32") == "windows"
    assert diagnostics._os_name("darwin") == "macos"
    assert diagnostics._os_name("linux") == "linux"


def test_document_reports_platform_os_browser_and_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics.protection, "keyring_state", lambda: "unavailable")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude"), browser_kind="chrome"),
        launch_id="l",
        git_available=lambda: True,
    )
    document = diagnostics.diagnostics_document(
        readiness,
        storage=_READY_STORAGE,
        settings=None,
        workspaces=(),
        data_dir=tmp_path / "data",
        log_path=tmp_path / "data" / "log",
        include_paths=False,
        include_identity=False,
    )
    assert document["platform"] == {
        "os": diagnostics._os_name(sys.platform),
        "browser": "chrome",
        "keyring": "unavailable",
    }


def test_document_reports_no_browser_as_none(tmp_path: Path) -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    document = diagnostics.diagnostics_document(
        readiness,
        storage=_READY_STORAGE,
        settings=None,
        workspaces=(),
        data_dir=tmp_path / "data",
        log_path=tmp_path / "data" / "log",
        include_paths=False,
        include_identity=False,
    )
    assert cast("dict[str, Any]", document["platform"])["browser"] == "none"


def test_document_reports_storage_exactly_as_the_caller_supplied_it(tmp_path: Path) -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=None, cli_problem="not_found"),
        launch_id="l",
        git_available=lambda: False,
    )
    unavailable = StorageReadiness(state="unavailable", reason="missing", private=None)
    document = diagnostics.diagnostics_document(
        readiness,
        storage=unavailable,
        settings=None,
        workspaces=(),
        data_dir=tmp_path / "missing",
        log_path=tmp_path / "missing" / "log",
        include_paths=False,
        include_identity=False,
    )
    assert document["storage"] == {"state": "unavailable", "reason": "missing", "private": None}
    assert "data_dir" not in document  # paths are placeholders/absent outside the in-app screen


def test_document_workspace_entries_respect_include_paths_and_include_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(workspace_id="w1", name="Owner's Notes", root_display="C:\\VIKI")
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    common = {
        "storage": _READY_STORAGE,
        "settings": None,
        "workspaces": [workspace],
        "data_dir": tmp_path / "data",
        "log_path": tmp_path / "data" / "log",
    }
    hidden = diagnostics.diagnostics_document(
        readiness, **common, include_paths=False, include_identity=False
    )
    assert hidden["workspaces"] == [
        {
            "id": "w1",
            "kind": "vault",
            "policy": "ordinary",
            "layout": "unknown",
            "name": "<redacted>",
            "root": "<redacted>",
        }
    ]
    shown = cast(
        "dict[str, Any]",
        diagnostics.diagnostics_document(
            readiness, **common, include_paths=True, include_identity=True
        ),
    )
    assert shown["workspaces"][0]["name"] == "Owner's Notes"
    assert shown["workspaces"][0]["root"] == "C:\\VIKI"
    partial = cast(
        "dict[str, Any]",
        diagnostics.diagnostics_document(
            readiness, **common, include_paths=True, include_identity=False
        ),
    )
    assert partial["workspaces"][0]["name"] == "<redacted>"
    assert partial["workspaces"][0]["root"] == "C:\\VIKI"


def test_readiness_never_opens_credential_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broad regression guard: patches every plausible file-reading entry point (open, io.open,
    os.open, Path.read_text/read_bytes) and plants a .credentials.json a naive implementation
    could reach for, then proves none of the patched functions ever saw a path naming it."""
    opened: list[str] = []

    def record(path: object) -> None:
        opened.append(str(path))

    real_open = builtins.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        record(file)
        return real_open(file, *args, **kwargs)

    real_io_open = io.open

    def spy_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        record(file)
        return real_io_open(file, *args, **kwargs)

    real_os_open = os.open

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        record(path)
        return real_os_open(path, *args, **kwargs)

    real_read_text = Path.read_text

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        record(self)
        return real_read_text(self, *args, **kwargs)

    real_read_bytes = Path.read_bytes

    def spy_read_bytes(self: Path, *args: Any, **kwargs: Any) -> Any:
        record(self)
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    claude_home = tmp_path / "home" / ".claude"
    claude_home.mkdir(parents=True)
    (claude_home / ".credentials.json").write_text('{"planted": true}', encoding="utf-8")
    data_dir = tmp_path / "data"
    (data_dir / ".credentials.json").parent.mkdir(parents=True, exist_ok=True)
    opened.clear()  # the planted-file setup above wrote through the same patched functions

    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    readiness.observe_billing(["settings.json: apiKeyHelper is set"])
    readiness.observe_account({"apiProvider": "firstParty"})
    readiness.observe_start_error("billing")
    diagnostics.diagnostics_document(
        readiness,
        storage=_READY_STORAGE,
        settings=None,
        workspaces=[_workspace()],
        data_dir=data_dir,
        log_path=data_dir / "log",
        include_paths=True,
        include_identity=True,
    )
    assert not any("credential" in path.lower() for path in opened)


def test_redact_removes_planted_token_home_path_and_prompt_line() -> None:
    # Built from halves so the committed text never holds a bearer header the scrubber flags.
    token = "sk-ant-oat01-" + "planted-token-value-000000000000"
    text = (
        "starting session\n"
        "Authorization: Bearer " + token + "\n"
        'planted prompt: "delete everything in C:\\Users\\synthuser\\Documents"\n'
        "home: C:\\Users\\synthuser\\AppData\\Local\\viki-agent\n"
        "vault: C:\\VIKI\\notes.md\n"
        "ok\n"
    )
    redacted, count = diagnostics.redact(text, roots=[("C:\\VIKI", "<vault:1>")])
    assert count > 0
    assert token not in redacted
    assert "Bearer " not in redacted
    assert "synthuser" not in redacted
    assert "<vault:1>\\notes.md" in redacted
    assert "ok\n" in redacted  # an unrelated line survives untouched


def test_redact_handles_slash_case_and_json_escaped_root_spellings() -> None:
    text = (
        "c:\\viki\\notes.md\n"
        "C:/VIKI/notes.md\n"
        "C:\\\\VIKI\\\\notes.md\n"  # a JSON-escaped path, literal double backslashes
        "unrelated line\n"
    )
    redacted, count = diagnostics.redact(text, roots=[("C:\\VIKI", "<vault:1>")])
    assert count >= 3
    assert "viki" not in redacted.lower()
    assert redacted.count("<vault:1>") == 3
    assert "unrelated line" in redacted


def test_redact_handles_unc_and_lowercase_user_paths() -> None:
    text = (
        "\\\\localhost\\C$\\Users\\synthuser\\file.txt\n"
        "\\\\server\\share\\Users\\synthuser\\file.txt\n"
        "c:/users/synthuser/documents\n"
    )
    redacted, count = diagnostics.redact(text, roots=[])
    assert count == 3
    assert "synthuser" not in redacted
    assert "<user>" in redacted


@pytest.mark.parametrize(
    "line",
    [
        '"api_key": "short-marker"',
        "password = short-pass",
        "TOKEN: short-token",
        "clientSecret=short-secret",
        "access_token: short-access",
        "refresh-token=short-refresh",
        "VIKI_SLACK_BOT_TOKEN=short-bot",
        "VIKI_SLACK_APP_TOKEN=short-app",
        "Cookie: session=short-cookie",
        "bEaReR short-bearer",
        "sK-aNt-short-key",
        "anthropic_key=short-env",
        "oAuTh short-oauth",
        "https://user:short-pass@example.com/path",
        "ssh://user@example.com/path",
    ],
)
def test_redact_drops_explicit_short_credentials_and_url_userinfo(line: str) -> None:
    redacted, count = diagnostics.redact(f"before\n{line}\nafter\n", roots=[])
    assert redacted == "before\nafter\n"
    assert count == 1


@pytest.mark.parametrize(
    "line",
    [
        '"token_count": 7',
        "password_policy=strict",
        "the password policy needs a review",
        "https://example.com/path/@someone",
        "https://example.com/path?contact=somebody@example.com",
        "owner@example.com",
        "a." * 4096 + "://safe.example.com",
    ],
)
def test_redact_keeps_benign_credential_words_and_urls(line: str) -> None:
    assert diagnostics.redact(line, roots=[]) == (line, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see /media/alice/USB/x", "see <user>/USB/x"),
        ("see /run/media/alice/USB/x", "see <user>/USB/x"),
        ("see /srv/media/alice/x", "see /srv/media/alice/x"),
        ("see /data/run/media/alice/x", "see /data/run/media/alice/x"),
    ],
)
def test_redact_generalizes_a_removable_media_user_folder(text: str, expected: str) -> None:
    redacted, count = diagnostics.redact(text, roots=[])
    assert redacted == expected
    assert count == (text != expected)


def test_redact_removes_username_from_a_real_traceback() -> None:
    try:
        raise FileNotFoundError(r"C:\Users\synthuser\.claude\.credentials.json")
    except FileNotFoundError:
        rendered = traceback.format_exc()
    assert "synthuser" in rendered  # sanity: the planted name is really there before redaction
    redacted, count = diagnostics.redact(rendered, roots=[])
    assert count > 0
    assert "synthuser" not in redacted
