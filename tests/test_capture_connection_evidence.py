"""Scenario 39's offline isolation and refusal evidence; no real Claude process runs."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture  # noqa: E402


def _scratch_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = capture._isolated_39
    monkeypatch.setattr(capture, "_isolated_39", lambda cli: original(cli, parent=tmp_path))


def _config(tmp_path: Path) -> capture.config.Config:
    return capture.config.Config(
        vault=None,
        claude_home=tmp_path / "home" / ".claude",
        cli=tmp_path / "fake-cli",
        edge=None,
        data_dir=tmp_path / "data",
        cli_source="override",
    )


def test_mcp_writer_accepts_actual_isolated_echo_path(tmp_path: Path) -> None:
    with capture._isolated_39(tmp_path / "cli", parent=tmp_path) as scratch:
        run_dir = scratch / "data" / "captures" / "20261110-000000-12345678"
        run_dir.mkdir(parents=True)
        server = capture.write_echo_server(run_dir)
        mcp = capture.write_mcp_config(run_dir / "data", server)
        assert server.is_file()
        assert mcp.is_file()
        assert mcp.is_relative_to(scratch)


def test_39_real_config_discovers_only_private_home_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_under(tmp_path, monkeypatch)
    cli = tmp_path / "fake-cli.exe"
    cli.write_text("", encoding="utf-8")
    cli.chmod(0o700)
    with capture._isolated_39(cli) as scratch:
        cfg = capture.config.load()
        assert cfg.cli == cli
        assert cfg.claude_home == scratch / "home" / ".claude"
        assert cfg.data_dir == scratch / "data"
        assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_relative_to(scratch)
        assert Path(os.environ["ANTHROPIC_CONFIG_DIR"]).is_relative_to(scratch)


@pytest.mark.parametrize("keys", [["01", "39"], ["39", "39"], ["39", "01"]])
async def test_39_rejects_mixed_or_repeated_selection_before_discovery(
    keys: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("configuration, CLI or billing reached")

    monkeypatch.setattr(capture.config, "load", forbidden)
    monkeypatch.setattr(capture.config, "_cli", forbidden)
    monkeypatch.setattr(capture.config, "billing_problems", forbidden)
    monkeypatch.setattr(capture.winproc, "run", forbidden)
    assert await capture.run(keys) == 2


async def test_39_load_and_version_use_private_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_under(tmp_path, monkeypatch)
    original = dict(os.environ)
    fake_cli = tmp_path / "claude"
    fake_cli.write_text("", encoding="utf-8")
    monkeypatch.setenv("VIKI_CLAUDE_CLI", str(fake_cli))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "second-inherited-sentinel")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-config-sentinel"))
    observed: list[dict[str, str]] = []

    def check_env() -> None:
        for name in (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "TMP",
            "TEMP",
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_CONFIG_DIR",
        ):
            value = os.environ.get(name)
            assert value and "real-config-sentinel" not in value
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        observed.append(dict(os.environ))

    def fake_cli_resolve(env: Any) -> tuple[Path, None, str]:
        assert env["VIKI_CLAUDE_CLI"] == str(fake_cli)
        return fake_cli, None, "override"

    def fake_load() -> Any:
        check_env()
        return SimpleNamespace(
            cli=fake_cli,
            captures_dir=tmp_path / "captures",
            claude_config_dir_set=True,
            cli_source="override",
        )

    def fake_version(_argv: Any, **kwargs: Any) -> Any:
        check_env()
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == ""
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == os.environ["CLAUDE_CONFIG_DIR"]
        return SimpleNamespace(stdout="2.1.281\n")

    async def fake_scenario(_cap: Any, _rec: Any) -> dict[str, bool]:
        check_env()
        return {"pass": True}

    monkeypatch.setattr(capture.config, "_cli", fake_cli_resolve)
    monkeypatch.setattr(capture.config, "load", fake_load)
    monkeypatch.setattr(capture.winproc, "run", fake_version)
    monkeypatch.setattr(capture, "s39", fake_scenario)
    assert await capture.run(["39"]) == 0
    assert len(observed) == 3
    assert os.environ["ANTHROPIC_API_KEY"] == "inherited-sentinel"
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "real-config-sentinel")
    assert all(
        os.environ.get(key) == value
        for key, value in original.items()
        if key
        not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CONFIG_DIR", "VIKI_CLAUDE_CLI"}
    )


async def test_non_39_selection_keeps_subscription_billing_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = capture.config.Config(
        vault=None,
        claude_home=tmp_path / "private-home",
        cli=tmp_path / "fake-cli",
        edge=None,
        data_dir=tmp_path / "data",
    )
    calls: list[str] = []

    def billing(_cfg: Any) -> list[str]:
        calls.append("billing")
        return ["blocked"]

    monkeypatch.setattr(capture.config, "load", lambda: cfg)
    monkeypatch.setattr(capture.config, "billing_problems", billing)
    assert await capture.run(["01"]) == 2
    assert calls == ["billing"]


async def test_39_old_version_refuses_before_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_under(tmp_path, monkeypatch)
    cli = tmp_path / "fake-cli"
    cli.write_text("", encoding="utf-8")
    cfg = SimpleNamespace(cli=cli, captures_dir=tmp_path / "captures", cli_source="override")
    monkeypatch.setattr(capture.config, "_cli", lambda _env: (cli, None, "override"))
    monkeypatch.setattr(capture.config, "load", lambda: cfg)
    monkeypatch.setattr(
        capture.winproc, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="2.1.280")
    )

    async def forbidden(_cap: Any, _rec: Any) -> Any:
        raise AssertionError("old CLI launched")

    monkeypatch.setattr(capture, "s39", forbidden)
    assert await capture.run(["39"]) == 2


async def test_routed_open_scrubs_inherited_key_and_skips_subscription_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "home" / ".claude"
    anthropic_dir = tmp_path / "home" / ".anthropic"
    config_dir.mkdir(parents=True)
    anthropic_dir.mkdir()
    for name in capture._SCRATCH_ENV_KEYS:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(anthropic_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "second-inherited-sentinel")
    observed: dict[str, Any] = {}

    class FakeTransport:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            observed["argv"] = argv
            observed["env"] = kwargs["env"]

    class FakeControl:
        def __init__(self, _transport: Any, _callback: Any) -> None:
            pass

        async def start(self) -> dict[str, Any]:
            return {}

        async def close(self) -> None:
            pass

    def forbidden(_init: Any) -> None:
        raise AssertionError("subscription billing reached")

    monkeypatch.setattr(capture, "TappedTransport", FakeTransport)
    monkeypatch.setattr(capture, "ClaudeControl", FakeControl)
    monkeypatch.setattr(capture, "check_billing", forbidden)
    cap = capture.Capture(_config(tmp_path), tmp_path, tmp_path, isolation_root=tmp_path)
    gateway = capture.Gateway()
    route = gateway.issue(
        capture.Connection(
            "id", "Synthetic", "http://127.0.0.1:1234", "anthropic", "synthetic-model"
        )
    )
    overlay = route.environment()
    overlay["ANTHROPIC_BASE_URL"] = overlay["ANTHROPIC_BASE_URL"].replace(":8765/", ":1234/")
    settings = tmp_path / "bot-memory-settings" / ("a" * 64 + ".json")
    launch = capture.Launch(
        "sid", bot_memory_settings=str(settings), mcp_config=str(tmp_path / "mcp.json")
    )
    rec = capture.Recorder(tmp_path / "capture.jsonl", "39")
    try:
        live = await cap.open(
            rec,
            launch,
            model="synthetic-model",
            child_overlay=overlay,
            routed=True,
        )
        await cap.close(live)
    finally:
        rec.close()
    route.revoke()
    argv = observed["argv"]
    env = observed["env"]
    assert "--model=synthetic-model" in argv
    assert any(arg.startswith("--settings=") for arg in argv)
    assert any(arg.startswith("--mcp-config=") for arg in argv)
    assert env["ANTHROPIC_AUTH_TOKEN"] == overlay["ANTHROPIC_AUTH_TOKEN"]
    assert env["ANTHROPIC_API_KEY"] == ""
    assert "OPENAI_API_KEY" not in env
    assert "DISABLE_AUTOUPDATER" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
    assert env["ANTHROPIC_CONFIG_DIR"] == str(anthropic_dir)


async def test_routed_open_refuses_config_outside_scratch_before_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in capture._SCRATCH_ENV_KEYS:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path.parent / "outside"))
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("child launched")

    monkeypatch.setattr(capture, "TappedTransport", forbidden)
    cap = capture.Capture(_config(tmp_path), tmp_path, tmp_path, isolation_root=tmp_path)
    gateway = capture.Gateway()
    route = gateway.issue(
        capture.Connection(
            "id", "Synthetic", "http://127.0.0.1:1234", "anthropic", "synthetic-model"
        )
    )
    rec = capture.Recorder(tmp_path / "capture.jsonl", "39")
    try:
        with pytest.raises(ValueError, match="unsafe routed capture environment"):
            await cap.open(
                rec,
                capture.Launch("sid"),
                model="synthetic-model",
                child_overlay=route.environment(),
                routed=True,
            )
    finally:
        rec.close()
    route.revoke()


@pytest.mark.parametrize("route_reached", [False, True])
@pytest.mark.parametrize("metadata_present", [False, True])
async def test_39_raw_metadata_diagnoses_without_overriding_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_reached: bool,
    metadata_present: bool,
) -> None:
    monkeypatch.setattr(capture, "sys", SimpleNamespace(platform="linux"))

    listeners: list[str] = []

    @asynccontextmanager
    async def fake_listener(app: Any) -> AsyncIterator[int]:
        listeners.append("opened")
        if route_reached and len(listeners) == 1:
            canary = next((cap.root / "bot-memory").glob("*/MEMORY.md")).read_text(encoding="utf-8")
            body = json.dumps({"system": canary}).encode()

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": body, "more_body": False}

            request = capture.Request(
                {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []},
                receive,
            )
            await app.routes[0].endpoint(request)
        try:
            yield 1234
        finally:
            listeners.append("closed")

    class FakeControl:
        async def mcp_status(self) -> dict[str, Any]:
            return {"mcpServers": [{"name": "viki_echo", "tools": [{"name": "echo"}]}]}

    class FakeCapture:
        root = tmp_path / "project"
        run_dir = tmp_path / "run"
        isolation_root = tmp_path
        closed = False

        async def open(self, _rec: Any, _launch: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                control=FakeControl(),
                init=(
                    {"account": {"apiProvider": "synthetic"}, "models": [{"value": "native"}]}
                    if metadata_present
                    else {}
                ),
            )

        async def turn(self, _live: Any, _prompt: str) -> list[dict[str, Any]]:
            return [{"type": "result", "subtype": "success"}]

        async def close(self, _live: Any) -> None:
            self.closed = True

    FakeCapture.root.mkdir()
    FakeCapture.run_dir.mkdir()
    revoked: list[str] = []
    original_revoke = capture.Gateway.revoke

    def observe_revoke(gateway: capture.Gateway, route_id: str) -> None:
        revoked.append(route_id)
        original_revoke(gateway, route_id)

    monkeypatch.setattr(capture.Gateway, "revoke", observe_revoke)
    monkeypatch.setattr(capture, "_loopback_app", fake_listener)
    monkeypatch.setattr(capture, "write_mcp_config", lambda _folder, _server: tmp_path / "mcp")
    cap = FakeCapture()
    result = await capture.s39(cap, None)  # type: ignore[arg-type]
    assert result["pass"] is route_reached
    assert result["upstream_reached"] is route_reached
    assert result["memory_in_request"] is route_reached
    assert result["initialize_account_metadata_present"] is metadata_present
    assert result["initialize_models_metadata_present"] is metadata_present
    assert set(result) == {
        "pass",
        "linux",
        "route_id",
        "model",
        "memory_in_request",
        "mcp_listed",
        "host_managed_auth",
        "upstream_reached",
        "stream_success",
        "initialize_account_metadata_present",
        "initialize_models_metadata_present",
    }
    assert all(
        isinstance(value, bool) for key, value in result.items() if key not in {"route_id", "model"}
    )
    assert cap.closed is True
    assert revoked == [result["route_id"]]
    assert listeners == ["opened", "opened", "closed", "closed"]
