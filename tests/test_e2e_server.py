"""The explicit test entry point owns fake-CLI assembly; no process or port is needed here."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent import config, protection
from viki_agent.voice import voice_models

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import e2e_server as harness  # noqa: E402


@pytest.fixture(autouse=True)
def _synthetic_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep scratch's USERPROFILE/HOME changes and ambient fake-CLI variables out of other tests.
    # USERPROFILE stays set because the assembly asks for Path.home() (the deny set's base),
    # and an environment with none at all makes pathlib raise instead of answering.
    monkeypatch.setattr(harness.os, "environ", {"USERPROFILE": str(tmp_path / "home")})
    monkeypatch.setattr(harness.winproc, "run", _forbid_process)
    monkeypatch.setattr(harness.winproc, "spawn", _forbid_process)


def _forbid_process(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("this assembly test must not start a process")


def denied_roots() -> list[Path]:
    """The sentinel deny list the stubbed build_services returns."""
    return []


def _capture_assembly(
    monkeypatch: pytest.MonkeyPatch, cfg: config.Config, active_root: Path
) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    app = SimpleNamespace(router=SimpleNamespace(routes=[]))
    # The vault when one is registered; the scratch root when none is. `_FakeWorkspaces` answers
    # with whichever the test's own config names, so the assertions below read the same path the
    # harness would have served.
    workspaces = _FakeWorkspaces(active_root)
    calls["workspaces"] = workspaces

    def load(env: dict[str, str]) -> config.Config:
        calls["env"] = dict(env)
        return cfg

    def version(argv: tuple[str, ...]) -> str:
        calls["version_argv"] = argv
        return "2.1.259"

    def manager(**kwargs: Any) -> object:
        calls["manager"] = kwargs
        return object()

    def create_app(**kwargs: Any) -> Any:
        calls["app"] = kwargs
        return app

    def server(uvi_config: Any) -> Any:
        calls["uvicorn"] = uvi_config

        def run(**kwargs: Any) -> None:
            calls["run_kwargs"] = kwargs
            calls["ran"] = True

        return SimpleNamespace(run=run)

    def listen(host: str, port: int) -> object:
        # Every ordinary test in this file serves through a bound socket the harness never
        # touches — a fixed sentinel, never a real one, so no test here binds config.PORT.
        calls["listen_args"] = (host, port)
        calls["listen_socket"] = object()
        return calls["listen_socket"]

    def build_services(*args: Any, **kwargs: Any) -> Any:
        # What the launcher's own function builds is pinned by
        # tests/test_main.py::test_build_services_wires_defaults_and_the_spawn_gate; what this
        # file is about is which workspace the harness hands it and where the result goes.
        calls["services_args"] = args
        calls["services"] = kwargs
        return SimpleNamespace(
            settings="synthetic-settings",
            bootstrap="synthetic-bootstrap",
            checks="synthetic-checks",
            diagnostics_document="synthetic-document",
            data="synthetic-data",
            argv_cli=(),
            version="",
            owner="synthetic-owner",
            denied_roots=denied_roots,
        )

    monkeypatch.setattr(harness.config, "load", load)
    monkeypatch.setattr(harness, "cli_version", version)
    monkeypatch.setattr(harness, "SessionManager", manager)
    monkeypatch.setattr(harness, "AppState", lambda *_args: SimpleNamespace(store=object()))
    monkeypatch.setattr(harness, "WorkspaceService", lambda *_args, **_kw: workspaces)
    monkeypatch.setattr(harness, "user_defaults", lambda *_args: None)
    monkeypatch.setattr(harness, "Catalog", lambda *_args: object())
    monkeypatch.setattr(harness, "build_services", build_services)
    monkeypatch.setattr(harness.server, "create_app", create_app)
    monkeypatch.setattr(harness.uvicorn, "Server", server)
    monkeypatch.setattr(harness, "_listen", listen)
    calls["routes"] = app.router.routes
    return calls


class _FakeWorkspaces:
    """WorkspaceService as the harness uses it: register the scratch vault, then select it.

    Real registration canonicalises a root through Win32 and probes its identity; the harness's
    own assembly is what this file is about, and tests/test_workspaces.py owns that behaviour.
    """

    def __init__(self, active_root: Path) -> None:
        self.registered: list[str] = []
        self.selected: list[str] = []
        self._active_root = active_root

    def resolve_active(self) -> Any:
        """`ActiveWorkspace` as the harness reads it: the working directory and the workspace.
        Real resolution creates the private scratch root and is
        owned by tests/test_workspaces.py."""
        return SimpleNamespace(
            cwd=self._active_root,
            workspace=SimpleNamespace(workspace_id="ws-1"),
            chat_only=not self.selected,
        )

    def register_existing(self, command_id: str, raw_path: str) -> tuple[Any, bool]:
        del command_id
        self.registered.append(raw_path)
        return SimpleNamespace(workspace_id="ws-1"), True

    def select(self, command_id: str, workspace_id: str | None) -> None:
        del command_id
        self.selected.append(str(workspace_id))


def _config(scratch: Path) -> config.Config:
    return config.Config(
        vault=scratch / "vault",
        claude_home=scratch / "home" / ".claude",
        cli=Path(sys.executable),
        edge=None,
        data_dir=scratch / "data",
    )


def test_missing_token_refuses_before_config_or_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", "synthetic-fake.py")
    monkeypatch.setattr(harness.config, "load", _forbid_process)
    monkeypatch.setattr(harness, "cli_version", _forbid_process)
    monkeypatch.setattr(harness, "SessionManager", _forbid_process)
    monkeypatch.setattr(harness.uvicorn, "Server", _forbid_process)

    assert harness.main([]) == 2
    assert "VIKI_TEST_TOKEN is required" in capsys.readouterr().err


@pytest.mark.parametrize("script", ["", "synthetic directory/fake.py"])
def test_harness_assembles_the_cli_and_keeps_test_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    cfg = _config(tmp_path)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", script)
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "synthetic.jsonl")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", "synthetic-state")

    assert harness.main([]) == 0

    expected = (str(cfg.cli), script) if script else (str(cfg.cli),)
    driver = calls["manager"]["config"]
    assert driver.cli == calls["version_argv"] == expected
    assert driver.env["FAKE_CLAUDE_SCRIPT"] == "synthetic.jsonl"
    assert driver.env["FAKE_CLAUDE_STATE"] == "synthetic-state"
    assert calls["app"]["token"] == "synthetic-token"
    assert calls["app"]["windows"] is None
    assert calls["app"]["dev"] is False
    # The launch's ledger `owner`, which the reset service is built from, travels with the data
    # services exactly as the launcher sends it; a harness that minted its own would answer a
    # reset with an `owner` matching no row. What `build_services` puts in both places is
    # pinned in tests/test_main.py.
    assert calls["app"]["owner"] == "synthetic-owner"
    assert [route.path for route in calls["routes"]] == [
        "/api/e2e/opened",
        "/api/e2e/control-responses",
        "/api/e2e/previews",
    ]
    # A preview window records its argv instead of starting a browser on the machine running
    # the suite.
    preview_spawn = calls["app"]["preview_spawn"]
    assert preview_spawn is not harness.winproc.spawn
    assert preview_spawn.__qualname__.startswith("_recording_preview_spawn")
    assert calls["ran"] is True
    # The scratch vault is this run's workspace because it is registered and selected, not
    # because it happens to hold a CLAUDE.md: config.load does not ask for one.
    assert calls["workspaces"].registered == [str(cfg.vault)]
    assert calls["workspaces"].selected == ["ws-1"]


def test_voice_flag_installs_fake_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--voice` writes two tiny fake models and re-pins them, so the engine's own checks (size
    from lstat at the start, hash before the spawn) pass against the files it wrote and the app
    runs the scripted worker instead of the real engine."""
    scratch = tmp_path / "scratch"
    cfg = _config(scratch)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setattr(harness, "SCRATCH", scratch)
    monkeypatch.setattr(harness.shutil, "rmtree", lambda path, **_kwargs: None)
    # The harness replaces entries of the real tables in its own process; keep the test's copy.
    monkeypatch.setattr(voice_models, "MODELS", dict(voice_models.MODELS))
    monkeypatch.setattr(voice_models, "VAD", voice_models.VAD)
    real_weights_size = voice_models.MODELS["small-q8_0"].weights.size
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main(["--scratch", "--voice"]) == 0

    assert calls["app"]["voice_worker"] == REPO / "tests" / "fake_voice_worker.py"
    for model_id in ("small-q8_0", "large-v3-turbo-q8_0"):
        folder = voice_models.model_dir(cast(Path, cfg.data_dir), model_id)
        weights = voice_models.MODELS[model_id].weights
        assert (folder / weights.name).read_bytes() == f"fake {model_id}".encode()
        assert (folder / voice_models.VAD.name).read_bytes() == b"fake vad"
        assert voice_models.present(folder, voice_models.model_files(model_id))
        assert voice_models.verify(folder, voice_models.model_files(model_id)) is None
    assert voice_models.MODELS["small-q8_0"].weights.size != real_weights_size
    # The third pinned model stays uninstalled: the settings options list installed ones only.
    assert not voice_models.model_dir(cast(Path, cfg.data_dir), "large-v3-turbo-q5_0").exists()


def test_voice_flag_needs_scratch_and_other_runs_keep_the_real_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fake pins never land in the user's data folder, and a run without the flag passes no
    worker, so `create_app` keeps the real one."""
    cfg = _config(tmp_path)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main(["--voice"]) == 2
    assert "--voice needs --scratch" in capsys.readouterr().err
    assert not (tmp_path / "data" / "voice").exists()
    assert "app" not in calls

    assert harness.main([]) == 0
    assert calls["app"]["voice_worker"] is None


def test_the_recording_preview_spawn_keeps_the_argv_and_a_window_that_ends_when_killed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        argvs: list[list[str]] = []
        spawn = harness._recording_preview_spawn(argvs)  # pyright: ignore[reportPrivateUsage]

        window = await spawn(
            ("edge", "--app=http://localhost:1/"), cwd=tmp_path, env={}, pipes=False
        )

        assert argvs == [["edge", "--app=http://localhost:1/"]]
        waiting = asyncio.create_task(window.wait_tree())
        await asyncio.sleep(0)
        assert not waiting.done()
        window.release()
        window.kill_tree()
        await asyncio.wait_for(waiting, 0.05)

    asyncio.run(scenario())


def test_scratch_wires_its_own_paths_and_fake_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    cfg = _config(scratch)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    cleanup_targets: list[Path] = []
    monkeypatch.setattr(harness, "SCRATCH", scratch)
    monkeypatch.setattr(
        harness.shutil, "rmtree", lambda path, **_kwargs: cleanup_targets.append(path)
    )
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", "inherited-fake.py")
    fixture = tmp_path / "synthetic.jsonl"

    assert harness.main(["--scratch", "--fixture", str(fixture)]) == 0

    assert cleanup_targets == [scratch]
    env = calls["env"]
    assert env["VIKI_VAULT"] == str(cfg.vault)
    assert env["VIKI_DATA_DIR"] == str(cfg.data_dir)
    assert env["HOME"] == str(scratch / "home")
    assert cast(Path, cfg.vault).joinpath("CLAUDE.md").is_file()
    assert cfg.claude_home.is_dir() and cfg.data_dir.is_dir()
    driver = calls["manager"]["config"]
    expected = (sys.executable, str(REPO / "tests" / "fake_claude.py"))
    assert driver.cli == calls["version_argv"] == expected
    assert driver.env["FAKE_CLAUDE_SCRIPT"] == str(fixture.resolve())
    if sys.platform == "win32":
        assert env["USERPROFILE"] == str(scratch / "home")
        # workspaces.denied_roots() derives its refusals from these variables, and on a standard
        # profile %TEMP% lies under %LOCALAPPDATA% — where SCRATCH itself lives. Left pointing at
        # the real profile they would deny the scratch vault its own run just registered.
        local = scratch / "home" / "AppData" / "Local"
        assert env["LOCALAPPDATA"] == str(local)
        assert env["TEMP"] == env["TMP"] == str(local / "Temp")
        assert env["APPDATA"] == str(scratch / "home" / "AppData" / "Roaming")
        assert (local / "Temp").is_dir() and Path(env["APPDATA"]).is_dir()
    else:
        # No Windows profile variables on POSIX: nothing else in the app reads them off this OS.
        assert not {"LOCALAPPDATA", "TEMP", "TMP", "APPDATA"} & env.keys()
    assert calls["workspaces"].registered == [str(cfg.vault)]


def test_no_vault_serves_the_scratch_root_with_no_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-vault` is the first-launch shape: nothing registered, nothing selected, and the
    working directory is the chat-only scratch root `resolve_active` hands back — which is what
    makes `capabilities.workspace.chat_only` true and the onboarding view reachable."""
    scratch = tmp_path / "scratch"
    # config.load answers with no vault, exactly as it does once VIKI_VAULT is withheld.
    cfg = replace(_config(scratch), vault=None)
    scratch_root = scratch / "data" / "scratch"
    calls = _capture_assembly(monkeypatch, cfg, scratch_root)
    monkeypatch.setattr(harness, "SCRATCH", scratch)
    monkeypatch.setattr(harness.shutil, "rmtree", lambda path, **_kwargs: None)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main(["--scratch", "--no-vault"]) == 0

    assert "VIKI_VAULT" not in calls["env"]
    assert calls["workspaces"].registered == []
    assert calls["workspaces"].selected == []
    # The folder still exists: the open-an-existing-folder scenarios need something to open.
    assert (scratch / "vault").is_dir()
    assert calls["manager"]["config"].vault == scratch_root
    assert calls["app"]["config"].vault == scratch_root
    assert calls["services"]["active"].cwd == scratch_root
    assert calls["services"]["active"].chat_only is True
    # Every route the launcher registers is registered here too.
    assert calls["app"]["settings"] == "synthetic-settings"
    assert calls["app"]["bootstrap"] == "synthetic-bootstrap"
    assert calls["app"]["checks"] == "synthetic-checks"
    assert calls["app"]["diagnostics_document"] == "synthetic-document"
    assert calls["app"]["data"] == "synthetic-data"
    assert calls["app"]["workspaces"] is calls["workspaces"]
    assert calls["app"]["denied_roots"] is denied_roots
    assert calls["app"]["app_version"] == harness._app_version()  # pyright: ignore[reportPrivateUsage]
    assert calls["app"]["readiness"].may_spawn() is True


def test_no_vault_still_refuses_without_the_test_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness.config, "load", _forbid_process)
    monkeypatch.setattr(harness, "SessionManager", _forbid_process)
    monkeypatch.setattr(harness.uvicorn, "Server", _forbid_process)
    monkeypatch.delenv("VIKI_TEST_TOKEN", raising=False)

    assert harness.main(["--no-vault"]) == 2
    assert "VIKI_TEST_TOKEN is required" in capsys.readouterr().err


def test_a_missing_vault_refuses_on_the_first_line_unless_no_vault_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that means to register a vault and has none says so and stops. Without this the
    registration reaches WorkspaceService with the literal "None" and dies there, after the data
    directory and the store exist and long after any caller stopped reading stderr."""
    monkeypatch.setattr(harness.config, "load", lambda _env: replace(_config(tmp_path), vault=None))
    monkeypatch.setattr(harness, "_listen", lambda *_args: object())  # never the real port
    monkeypatch.setattr(harness, "SessionManager", _forbid_process)
    monkeypatch.setattr(harness.uvicorn, "Server", _forbid_process)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main([]) == 2
    assert "VIKI_VAULT must be set" in capsys.readouterr().err


def test_a_store_that_cannot_be_opened_serves_the_recovery_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--damaged-store` leaves bytes no database starts with, so the launch takes the same
    recovery fork the launcher does instead of exiting with a traceback."""
    scratch = tmp_path / "scratch"
    cfg = _config(scratch)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))

    served = SimpleNamespace(router=SimpleNamespace(routes=[]))

    def recovery_app(**kwargs: Any) -> Any:
        calls["recovery"] = kwargs
        return served

    monkeypatch.setattr(harness.server, "create_recovery_app", recovery_app)
    monkeypatch.setattr(
        harness, "AppState", lambda *_args: (_ for _ in ()).throw(harness.StateError("corrupt"))
    )
    monkeypatch.setattr(harness, "SCRATCH", scratch)
    monkeypatch.setattr(harness.shutil, "rmtree", lambda path, **_kwargs: None)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main(["--scratch", "--damaged-store"]) == 0

    assert (scratch / "data" / "app.sqlite3").read_bytes() == b"not a database"
    assert "app" not in calls  # create_app was never reached
    assert calls["recovery"]["storage_refusal"] == "corrupt"
    assert calls["recovery"]["windows"] is None
    assert calls["recovery"]["token"] == "synthetic-token"
    # The harness's own /api/e2e routes belong to the ordinary path: a recovery app records no
    # frames and opens no URLs, and is served exactly as `create_recovery_app` built it.
    assert served.router.routes == []
    assert calls["ran"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="DPAPI ignores the key source entirely")
def test_scratch_mode_seals_with_a_random_key_never_the_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --scratch run is throwaway and gone at exit, so only it may seal with a random key;
    the keychain source (below) must never be reached from here."""
    assert sys.platform != "win32"
    scratch = tmp_path / "scratch"
    cfg = _config(scratch)
    _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setattr(harness, "SCRATCH", scratch)
    monkeypatch.setattr(harness.shutil, "rmtree", lambda path, **_kwargs: None)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")
    calls: list[Path] = []

    def never_the_keychain(data_dir: Path) -> Callable[[], bytes]:
        calls.append(data_dir)
        return lambda: b""

    monkeypatch.setattr(protection, "keychain_source", never_the_keychain)

    before = protection._key_source
    assert harness.main(["--scratch", "--fixture", ""]) == 0

    assert calls == []
    assert protection._key_source is not before
    key_source = protection._key_source
    assert key_source is not None
    assert len(key_source()) == 32


@pytest.mark.skipif(sys.platform == "win32", reason="DPAPI ignores the key source entirely")
def test_non_scratch_mode_installs_the_keychain_source_like_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live or env-driven run without --scratch serves the user's real data directory, so it
    must install the same keychain source `__main__._main` installs — never the random key, or
    rows this run seals become unreadable to the next real launch. And it visits the source
    before the store opens, as the launcher does before anything seals, so a first keychain
    lookup never runs inside a store write transaction."""
    assert sys.platform != "win32"
    cfg = _config(tmp_path)
    _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")
    calls: list[Path] = []
    visits: list[None] = []
    visits_when_the_store_opened: list[int] = []

    def open_store(*_args: Any) -> Any:
        visits_when_the_store_opened.append(len(visits))
        return SimpleNamespace(store=object())

    monkeypatch.setattr(harness, "AppState", open_store)

    def source() -> bytes:
        visits.append(None)
        return b"\x00" * 32

    def keychain_source(data_dir: Path) -> Callable[[], bytes]:
        calls.append(data_dir)
        return source

    monkeypatch.setattr(protection, "keychain_source", keychain_source)

    assert harness.main([]) == 0

    assert calls == [cfg.data_dir]
    assert protection._key_source is source
    assert visits_when_the_store_opened == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX scratch root has no Windows form")
def test_scratch_root_lives_under_the_real_cache_directory() -> None:
    """A regression to /tmp — refused only by the fixed POSIX deny set (workspaces.SYSTEM_DENY),
    and only in the Linux e2e run — fails here first."""
    assert sys.platform != "win32"
    assert harness.SCRATCH.is_absolute()
    assert harness.SCRATCH.parts[-2:] == (".cache", "viki-e2e")
    assert not harness.SCRATCH.is_relative_to(Path("/tmp"))
    assert not harness.SCRATCH.is_relative_to(Path("/private"))


def test_a_taken_port_refuses_before_the_fake_server_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live app or another harness run already holds config.PORT: `_listen` answers None, and
    the fake `uvicorn.Server` — standing in for the lifespan that would sweep that process's own
    data folder — must never be constructed."""
    cfg = _config(tmp_path)
    _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setattr(harness, "_listen", lambda *_args: None)
    monkeypatch.setattr(harness.uvicorn, "Server", _forbid_process)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main([]) == 3

    captured = capsys.readouterr()
    assert "port 8765 is taken" in captured.err
    assert captured.out == ""  # "listening" only follows a successful bind


def test_a_taken_port_refuses_before_anything_is_cleared_opened_or_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket is taken at the top of `main`: a run that starts while another holds the port
    must not delete that run's scratch folder, load its config or open its store first."""
    cfg = _config(tmp_path)
    _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    touched: list[str] = []
    monkeypatch.setattr(harness, "_listen", lambda *_args: None)
    monkeypatch.setattr(harness, "_scratch", lambda *_args, **_kw: touched.append("_scratch"))
    monkeypatch.setattr(harness.config, "load", lambda _env: touched.append("config.load"))
    monkeypatch.setattr(harness, "AppState", lambda *_args: touched.append("AppState"))
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main(["--scratch"]) == 3

    assert touched == []


def test_the_bound_socket_is_the_one_uvicorn_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket `_listen` binds is handed straight to `run`, never a second bind uvicorn would
    otherwise do on its own."""
    cfg = _config(tmp_path)
    calls = _capture_assembly(monkeypatch, cfg, cast(Path, cfg.vault))
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-token")

    assert harness.main([]) == 0

    assert calls["listen_args"] == (harness.config.HOST, harness.config.PORT)
    assert calls["run_kwargs"] == {"sockets": [calls["listen_socket"]]}
