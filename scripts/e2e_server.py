"""The real server, without the launcher's window, for the end-to-end run.

Why this exists rather than `viki-agent` itself: the launcher owns an Edge window, and an
end-to-end run needs playwright to own the browser instead. It uses the same DriverConfig,
env scrub and create_app as the launcher. Fake-CLI argv is assembled here, outside the
production launcher. The window manager is omitted (`windows=None`), which makes the process
serve until it is killed instead of shutting down when a window closes.

It is a test harness and is never installed as a script. It refuses to run without
VIKI_TEST_TOKEN, so it can only ever serve a token a test chose — never a real launch's.

    VIKI_TEST_TOKEN=... VIKI_TEST_CLI_SCRIPT=<abs tests/fake_claude.py>
    VIKI_CLAUDE_CLI=<python.exe> VIKI_VAULT=<a scratch vault> VIKI_DATA_DIR=<a scratch dir>
    FAKE_CLAUDE_SCRIPT=<abs .jsonl>  uv run python scripts/e2e_server.py

`--scratch --fixture <file.jsonl>` fills all of that in itself except the token, which stays
the caller's to choose. That exists so the node side of the end-to-end run needs nothing but
`spawn` — no temp directories, no path arithmetic, no fake-CLI wiring of its own — and so that
what an end-to-end run reads is a scratch home and a scratch vault rather than the user's:
`claude_home` is `Path.home()/".claude"` with no override, so the only way to keep a test out of
their real transcripts is to give the process its own USERPROFILE (Windows) or HOME (POSIX) before
config.load runs.

`--bots` requires that scratch mode and a repository fixture. It creates an ordinary vault
through the launcher's bootstrap service and labels the synthetic driver/readiness with the
reviewed fixture version; default harness runs retain their read-only existing vault.

`--model-connections` also requires scratch mode and a repository fixture. It uses the fixture
version for ordinary model-connection replays, without the Bot vault bootstrap.

Binds config.PORT itself, as the launcher does, at the top of `main` once the token is checked:
before the scratch folder is cleared, the config loaded, the store opened or anything registered,
so an already-taken port refuses with nothing touched instead of starting a lifespan against the
data folder of whichever process (a live VIKI Agent, or another harness run) holds it. Prints
"listening" on stdout once everything is built, just before serving the bound socket; on a taken
port it prints one line to stderr instead, and nothing to stdout, and exits 3. The caller should
still poll an authenticated route once it sees "listening", which proves the lifespan has finished
and that the server answering holds this run's token.

Two things a test cannot otherwise see are made visible here, and only here (`create_app` itself
is untouched, and this file is never installed as a script):

  GET /api/e2e/opened             the URLs `open.url` handed the opener, in order
  GET /api/e2e/control-responses  the control_response frames the server wrote to claude's stdin
  GET /api/e2e/previews           the argv of every preview window the server asked for, in order

`--voice` (with `--scratch`) makes dictation runnable without a speech engine: it writes a fake
weights file and a fake VAD file for `small-q8_0` and `large-v3-turbo-q8_0` under the scratch data
folder, re-pins those two models and the VAD to the files it wrote (this process only), and gives
`create_app` `tests/fake_voice_worker.py` as the worker. `large-v3-turbo-q8_0` is installed so a
cuda pair would resolve; no cuda backend is, so this machine's `auto` runs the CPU pair.

All three take the same Bearer token as the real REST routes. The first also replaces the real
`winproc.open_external`, so a test that clicks a link in a reply records the call instead of
launching a browser on the machine running the suite. The second is off unless
VIKI_E2E_RECORD_FRAMES is set, because it puts a recorder in the byte path of every turn. The
third comes with a spawn that starts no browser: a preview window is a record that ends when
the server kills it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import uvicorn  # noqa: E402
from anyio.from_thread import run_sync as run_on_loop  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

from viki_agent import (  # noqa: E402
    config,
    server,
    winproc,  # noqa: E402
)
from viki_agent.__main__ import (  # noqa: E402
    _RESOURCES,
    LOG_NAME,
    STATIC_DIR,
    _app_version,
    _diagnostics_document,
    _listen,
    build_services,
    cli_version,
    vault_skills,
)
from viki_agent.app.diagnostics import ProviderChecks, Readiness  # noqa: E402
from viki_agent.catalog import Catalog  # noqa: E402
from viki_agent.drivers.claude.driver import DriverConfig  # noqa: E402
from viki_agent.drivers.claude.transport import ChildProcess, Spawner  # noqa: E402
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION  # noqa: E402
from viki_agent.session import SessionManager  # noqa: E402
from viki_agent.state import AppState, user_defaults  # noqa: E402
from viki_agent.storage import StateError  # noqa: E402
from viki_agent.vault_storage import VaultBootstrap  # noqa: E402
from viki_agent.voice import voice_models  # noqa: E402
from viki_agent.window import Spawn, Window  # noqa: E402
from viki_agent.workspaces import WorkspaceService  # noqa: E402

Frame = dict[str, Any]

# Set by the one end-to-end file that asserts on what the user's click actually sent. Reading
# it at start-up (not per request) keeps every other file's spawn path exactly as it was.
RECORD_ENV = "VIKI_E2E_RECORD_FRAMES"


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    token = winproc.scrub_env(os.environ).get("VIKI_TEST_TOKEN")
    if not token:
        print("VIKI_TEST_TOKEN is required: this harness never mints a token.", file=sys.stderr)
        return 2
    if args.voice and not args.scratch:
        # Fake model files must never land in the user's data folder.
        print("--voice needs --scratch.", file=sys.stderr)
        return 2
    if args.bots_team and not args.bots:
        print("--bots-team needs --bots.", file=sys.stderr)
        return 2
    if args.bots and (refusal := _bot_mode_error(args, winproc.scrub_env(os.environ))):
        print(refusal, file=sys.stderr)
        return 2
    if args.model_connections and (
        refusal := _synthetic_replay_error(
            args, winproc.scrub_env(os.environ), "--model-connections"
        )
    ):
        print(refusal, file=sys.stderr)
        return 2
    # The launcher's own order: own the port before anything is deleted, opened or registered, so
    # a taken port refuses with nothing touched — no scratch folder cleared, no store opened.
    sock = _listen(config.HOST, config.PORT)
    if sock is None:
        print(f"port {config.PORT} is taken: another VIKI or harness is running", file=sys.stderr)
        return 3
    if args.scratch:
        _scratch(
            args.fixture,
            dirty=args.dirty,
            no_vault=args.no_vault,
            damaged_store=args.damaged_store,
            bots=args.bots,
        )
    elif args.fixture:
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(Path(args.fixture).resolve())
    env = winproc.scrub_env(os.environ)
    cfg = config.load(env)
    if sys.platform != "win32":
        # Windows seals with DPAPI and needs no key source; POSIX needs one before anything in
        # this process seals or unseals. Only a --scratch run is throwaway and gone at exit, so
        # only it may seal with a random key that a real keychain never sees; any other run
        # (npm run live, or the env-driven mode without --scratch) serves the user's real data
        # directory and must install the same keychain source __main__._main does, or rows sealed
        # under the keychain key become unreadable and rows this run seals become unreadable next.
        from viki_agent import protection

        if args.scratch:
            key = secrets.token_bytes(32)
            protection.set_key_source(lambda: key)
        else:
            protection.set_key_source(protection.keychain_source(cfg.data_dir))
            # Visited now, as the launcher visits it before anything seals: never for the first
            # time inside a store write transaction.
            protection.visit_key_source()
    # A real launch opens without a CLI and says so in the shell; this harness has no shell to
    # say it in, and every end-to-end run needs the fake CLI. A vault is required in the same way,
    # unless `--no-vault` says the run means to do without one: that is how a run reaches the
    # first-launch surfaces (onboarding, the chat-only banner), and the working directory then
    # comes from the scratch workspace like any other launch with nothing selected.
    if cfg.cli is None:
        print(
            f"VIKI_CLAUDE_CLI ({cfg.cli_problem or 'unset'}) must be usable.",
            file=sys.stderr,
        )
        return 2
    if cfg.vault is None and not args.no_vault:
        # Without `--no-vault` this run means to register one, and registering "None" dies deep
        # inside WorkspaceService with a sentence written for the app's UI — after the data
        # directory and the store already exist and long after any caller stopped reading
        # stderr. A run that cannot do what it was asked says so on the first line instead.
        print("VIKI_VAULT must be set, or pass --no-vault.", file=sys.stderr)
        return 2
    voice_worker = _fake_voice(cfg.data_dir) if args.voice else None
    # Only this explicit test entry point may put a Python script behind the executable.
    # The production launcher ignores both test variables, even when inherited together.
    script = env.get("VIKI_TEST_CLI_SCRIPT")
    argv_cli = (str(cfg.cli), script) if script else (str(cfg.cli),)
    written: list[Frame] = []
    opened: list[str] = []
    previews: list[list[str]] = []
    launch_id = str(uuid.uuid4())
    log_file = cfg.data_dir / "logs" / LOG_NAME
    readiness = Readiness(
        config=cfg,
        launch_id=launch_id,
        # A screenshot run must not shell out to git for an identity probe, and `--dirty`'s own
        # repository is read by rails.py, which runs git itself. `--git` lets a code folder's Git
        # routes, and the workspace service's identity probes, run real git.
        git_available=lambda: args.git,
    )
    try:
        state = AppState(cfg.state_file, user_defaults(cfg.claude_home, cfg.vault))
    except StateError as exc:
        # The same fork the launcher takes: a store that cannot be opened serves the
        # read-only recovery shell instead of nothing. `--damaged-store` is the only way an
        # end-to-end run reaches it.
        return _serve(
            _recovery_app(cfg, token, readiness, log_file=log_file, refusal=exc.code), sock
        )
    # A registration is what makes the scratch vault this run's workspace: the CLI does not
    # decide it, and config.load() does not ask the folder for a CLAUDE.md.
    spaces = WorkspaceService(
        state.store,
        env=env,
        data_dir=cfg.data_dir,
        home=Path.home(),
        install_dir=REPO,
        # A worktree needs the Git common dir the identity probe records, and screenshot runs
        # pass no `--git`, so their identity probes still run no git.
        git_available=lambda: args.git,
        hide_previews=lambda: False,
    )
    if args.bots:
        workspace, _created, _files = VaultBootstrap(
            spaces, resources=_RESOURCES, home=Path.home()
        ).create(
            str(uuid.uuid4()),
            "Bot test vault",
            str(SCRATCH / "workspaces"),
            "vault",
            "en",
            False,
            template="viki" if args.bots_team else "blank",
        )
        spaces.select(str(uuid.uuid4()), workspace.workspace_id)
    elif not args.no_vault:
        workspace, _created = spaces.register_existing(str(uuid.uuid4()), str(cfg.vault))
        spaces.select(str(uuid.uuid4()), workspace.workspace_id)
    # Resolved in both modes, exactly as the launcher does it: with a selection this is the
    # registered vault, without one it is the chat-only scratch root the app creates itself.
    active = spaces.resolve_active()
    vault = active.cwd
    cfg = replace(cfg, vault=vault)
    manager = SessionManager(
        config=DriverConfig(
            cli=argv_cli,
            vault=vault,
            # The fake CLI is a Python script and needs its own script path back; child_env
            # scrubs the launcher's own, so the one variable the fake reads is put back here
            # and nowhere near a real launch.
            env={**winproc.child_env(config.ENTRYPOINT), **_fake_env(env)},
            cli_version=(
                FIXTURE_VERSION if args.bots or args.model_connections else cli_version(argv_cli)
            ),
            skills=vault_skills(vault),
            spawner=_recording_spawner(written) if env.get(RECORD_ENV) else None,
        ),
        state=state,
        claude_home=cfg.claude_home,
    )
    # The services and routes the launcher registers, built by the launcher's own function so an
    # end-to-end run reads the same services the app does. `argv_cli` and
    # `version` are the launcher's gated argv and are deliberately not used for the manager
    # above: the fake CLI's script argument is this harness's own business.
    services = build_services(
        cfg,
        env,
        state=state,
        spaces=spaces,
        active=active,
        readiness=readiness,
        launch_id=launch_id,
        log_file=log_file,
        scratch_in_use=lambda: active.chat_only and run_on_loop(lambda: bool(manager.live())),
    )
    if args.bots or args.model_connections:
        # build_services probes the executable alone, not the harness's fake-script argv.
        # Both consumers need the same reviewed test label; native init fixtures stay untouched.
        readiness.observe_version(FIXTURE_VERSION)
    app = server.create_app(
        config=cfg,
        token=token,
        manager=manager,
        catalog=Catalog(cfg.claude_home, vault),
        static_dir=STATIC_DIR,
        dev=False,
        # Left to its default this is winproc.open_external, so the first end-to-end test to
        # click a link in a reply would open it in the real browser of whoever ran the suite.
        # Appending is also the only way a test can tell an open.url that reached the server
        # from one the page swallowed.
        opener=opened.append,
        windows=None,
        # Left to its default this is winproc.spawn, and the first Open preview would start a
        # real browser window on the machine running the suite.
        preview_spawn=_recording_preview_spawn(previews),
        settings=services.settings,
        workspaces=spaces,
        bootstrap=services.bootstrap,
        readiness=readiness,
        checks=services.checks,
        diagnostics_document=services.diagnostics_document,
        data=services.data,
        owner=services.owner,
        denied_roots=services.denied_roots,
        app_version=_app_version(),
        voice_worker=voice_worker,
    )
    # Before the static Mount at "/", which matches everything and would answer these first.
    app.router.routes[:0] = _test_routes(token, opened, written, previews)
    return _serve(app, sock)


def _recovery_app(
    cfg: config.Config,
    token: str,
    readiness: Readiness,
    *,
    log_file: Path,
    refusal: str,
) -> Any:
    """The read-only shell a store that could not be opened gets, for `--damaged-store`.

    No manager, no catalog, no socket, no test routes: this app has nothing to record. Both
    probes answer "" rather than shelling out — a recovery shell starts nothing, and a harness
    must not run `claude --version` or `git --version` to say so.
    """
    checks = ProviderChecks(readiness=readiness, cli_version=lambda: "", git_version=lambda: "")
    return server.create_recovery_app(
        config=cfg,
        token=token,
        readiness=readiness,
        checks=checks,
        # The refusal has to reach the document too, or every --damaged-store screenshot
        # reports `recovery_required` whatever actually refused the store.
        diagnostics_document=_diagnostics_document(
            readiness, cfg, None, None, None, log_file, storage_refusal=refusal
        ),
        storage_refusal=refusal,
        static_dir=STATIC_DIR,
        dev=False,
        windows=None,
    )


def _serve(app: Any, sock: socket.socket) -> int:
    """Print the readiness line and serve the socket `main` bound, until killed.

    `main` owns the socket first (the launcher's own `_listen`), so a port already held by a live
    app or another harness run was refused there, before any scratch folder, store or workspace
    was touched, instead of starting the ASGI lifespan — and any startup sweep it runs — against
    that other process's own data folder. "listening" means bound: the caller may poll the moment
    it sees the line.
    """
    print("listening", flush=True)
    uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.HOST,  # run() below serves the bound socket, so uvicorn never binds
            # this host itself
            port=config.PORT,
            ws_max_size=server.WS_MAX_SIZE,
            access_log=False,
            log_level="warning",
        )
    ).run(sockets=[sock])
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="", help="replay script for the fake CLI (.jsonl)")
    parser.add_argument(
        "--dirty",
        action="store_true",
        help="make the scratch vault a git repo with one uncommitted file",
    )
    parser.add_argument(
        "--scratch",
        action="store_true",
        help="serve a throwaway vault, home and data dir instead of the owner's",
    )
    parser.add_argument(
        "--bots",
        action="store_true",
        help="with --scratch: bootstrap an ordinary vault for synthetic Bot tests",
    )
    parser.add_argument(
        "--bots-team",
        action="store_true",
        help="with --bots: bootstrap the Start with VIKI template, so the vault holds the team"
        " Kanban pack",
    )
    parser.add_argument(
        "--model-connections",
        action="store_true",
        help="with --scratch --fixture: use the fixture version for model-connection replays",
    )
    parser.add_argument(
        "--no-vault",
        action="store_true",
        help="register and select nothing, so the run starts chat-only like a first launch",
    )
    parser.add_argument(
        "--damaged-store",
        action="store_true",
        help="leave an unreadable app.sqlite3 behind, so the launch serves the recovery shell",
    )
    parser.add_argument(
        "--git",
        action="store_true",
        help="report git ready, so a code folder's Git routes run real git",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="with --scratch: install fake dictation models and run the scripted voice worker",
    )
    return parser.parse_args(argv)


# --- the fake dictation engine ----------------------------------------------------------------

FAKE_VOICE_MODELS = ("small-q8_0", "large-v3-turbo-q8_0")


def _fake_voice(data_dir: Path) -> Path:
    """Install the fake model files, re-pin them, and return the scripted worker's path.

    The engine checks the files by size from `lstat` at the status and start, and by SHA-256
    before the spawn, so each pin is replaced by the size and hash of what is written here.
    `tests/fake_voice_worker.py` treats any model id that is not a `fake-*` mode as `fake-ok`.
    """

    def pin(name: str, content: bytes) -> voice_models.PinnedFile:
        return voice_models.PinnedFile(name, len(content), hashlib.sha256(content).hexdigest())

    vad = pin(voice_models.VAD.name, b"fake vad")
    voice_models.VAD = vad
    for model_id in FAKE_VOICE_MODELS:
        model = voice_models.MODELS[model_id]
        weights = f"fake {model_id}".encode()
        folder = voice_models.model_dir(data_dir, model_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / model.weights.name).write_bytes(weights)
        (folder / vad.name).write_bytes(b"fake vad")
        voice_models.MODELS[model_id] = replace(model, weights=pin(model.weights.name, weights))
    return REPO / "tests" / "fake_voice_worker.py"


# --- what a test may look at ------------------------------------------------------------------


def _test_routes(
    token: str, opened: list[str], written: list[Frame], previews: list[list[str]]
) -> list[Route]:
    """Read-only views of what left this process: the URLs it opened, the frames it wrote, the
    preview windows it asked for.

    A real launch cannot serve these. They are added to the app object here rather than in
    `server.create_app`, this script is not in pyproject's `[project.scripts]`, and it exits
    before serving anything unless VIKI_TEST_TOKEN is set. They still ask for that token, so a
    page the app's own Edge window wandered onto cannot read either of them.
    """

    def authorised(request: Request) -> bool:
        prefix = "Bearer "
        header = request.headers.get("authorization", "")
        if not header.startswith(prefix):
            return False
        return secrets.compare_digest(header[len(prefix) :].encode(), token.encode())

    def reply(request: Request, payload: dict[str, Any]) -> Response:
        if not authorised(request):
            body = {"error": "unauthorized"}
            return Response(json.dumps(body), status_code=401, media_type="application/json")
        return Response(json.dumps(payload), media_type="application/json")

    async def opened_route(request: Request) -> Response:
        return reply(request, {"urls": list(opened)})

    async def control_responses_route(request: Request) -> Response:
        return reply(request, {"frames": list(written)})

    async def previews_route(request: Request) -> Response:
        return reply(request, {"argvs": [list(argv) for argv in previews]})

    return [
        Route("/api/e2e/opened", opened_route, methods=["GET"]),
        Route("/api/e2e/control-responses", control_responses_route, methods=["GET"]),
        Route("/api/e2e/previews", previews_route, methods=["GET"]),
    ]


class _RecordedWindow:
    """A preview window that is only a record: its tree ends when the server kills it."""

    def __init__(self) -> None:
        self._killed = asyncio.Event()

    async def wait_tree(self) -> None:
        await self._killed.wait()

    def kill_tree(self) -> None:
        self._killed.set()

    def release(self) -> None:
        pass


def _recording_preview_spawn(argvs: list[list[str]]) -> Spawn:
    """`window.Spawn`'s shape, keeping each argv it is given and starting no process."""

    async def spawn(
        argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> Window:
        argvs.append(list(argv))
        return _RecordedWindow()

    return spawn


def _recording_spawner(written: list[Frame]) -> Spawner:
    """`winproc.spawn`, with a copy of every control_response the server writes kept aside.

    The replay engine that stands in for claude matches a control_response on its `request_id`
    alone, so an end-to-end run proves only that *a* answer for the pending request arrived —
    an inverted allow/deny, or an answers map keyed by the wrong thing, replays identically.
    The frames themselves are the missing evidence, and the only place both halves exist at
    once is this process: the replayer runs inside the fake CLI's own subprocess, where no
    route can reach it.
    """

    async def spawn(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> ChildProcess:
        return _RecordingChild(await winproc.spawn(argv, cwd=cwd, env=env), written)

    return spawn


class _RecordingStdin:
    """claude's stdin, plus a copy of the control_response frames written through it.

    Only control_response frames: they are what a card's answer becomes, and skipping the rest
    keeps the prompt text the user typed out of a list served over HTTP.
    """

    def __init__(self, writer: asyncio.StreamWriter, written: list[Frame]) -> None:
        self._writer = writer
        self._written = written
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._record(data)
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    def _record(self, data: bytes) -> None:
        # Per complete line: a frame split across two writes must not be parsed in halves, and
        # a line this misreads is evidence lost, never a turn broken — hence json.loads under a
        # ValueError that simply drops the line.
        self._buf += data
        while (newline := self._buf.find(b"\n")) >= 0:
            line, self._buf = bytes(self._buf[:newline]), self._buf[newline + 1 :]
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, dict) and frame.get("type") == "control_response":
                self._written.append(frame)


class _RecordingProcess:
    """The five members `ClaudeTransport` reads off a process, with stdin taped."""

    def __init__(self, process: asyncio.subprocess.Process, written: list[Frame]) -> None:
        self._process = process
        self.stdin = None if process.stdin is None else _RecordingStdin(process.stdin, written)

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self._process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        return self._process.stderr

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()


class _RecordingChild:
    """`winproc.Child` as the transport's `ChildProcess` protocol sees it. The Job, the kill and
    the release stay the real child's: nothing about how a session ends changes here."""

    def __init__(self, child: winproc.Child, written: list[Frame]) -> None:
        self._child = child
        self.process = _RecordingProcess(child.process, written)

    @property
    def pid(self) -> int:
        return self._child.pid

    def kill_tree(self) -> None:
        self._child.kill_tree()

    def release(self) -> None:
        self._child.release()


# One fixed root rather than a fresh mkdtemp per run: the caller kills this process, and a kill
# on Windows runs no cleanup, so a fresh directory each time would only pile up. Clearing it on
# the way in also means a run never inherits the last run's state.json. On Windows this is the
# per-user temp directory; on POSIX the fixed deny set (workspaces.SYSTEM_DENY) refuses /tmp and
# /private, so this sits under the real home's cache directory instead, read here — before HOME
# is replaced below.
SCRATCH = (
    Path(tempfile.gettempdir()) / "viki-e2e"
    if sys.platform == "win32"
    else Path.home() / ".cache" / "viki-e2e"
)


def _bot_scratch_is_safe() -> bool:
    # The only recursive cleanup target is the explicitly named harness root, without aliases.
    return (
        SCRATCH.is_absolute()
        and SCRATCH.name == "viki-e2e"
        and SCRATCH.resolve() == SCRATCH.absolute()
    )


def _bot_mode_error(args: argparse.Namespace, env: Mapping[str, str]) -> str | None:
    if not args.scratch:
        return "--bots needs --scratch."
    if args.no_vault or args.damaged_store or args.dirty:
        return "--bots cannot use --no-vault, --damaged-store or --dirty."
    return _synthetic_replay_error(args, env, "--bots")


def _synthetic_replay_error(
    args: argparse.Namespace, env: Mapping[str, str], option: str
) -> str | None:
    if not args.scratch:
        return f"{option} needs --scratch."
    fixture = Path(args.fixture).resolve()
    if (
        not args.fixture
        or not fixture.is_relative_to(REPO / "tests" / "fixtures" / "claude")
        or fixture.suffix != ".jsonl"
        or not fixture.is_file()
    ):
        return f"{option} needs a synthetic fixture under tests/fixtures/claude."
    for key, expected in (
        ("VIKI_CLAUDE_CLI", Path(sys.executable)),
        ("VIKI_TEST_CLI_SCRIPT", REPO / "tests" / "fake_claude.py"),
    ):
        if not expected.is_file() or (
            env.get(key) and Path(env[key]).resolve() != expected.resolve()
        ):
            return f"{option} needs the repository synthetic CLI setup."
    if not _bot_scratch_is_safe():
        return f"{option} needs an absolute, unaliased viki-e2e scratch root."
    return None


def _scratch(
    fixture: str,
    dirty: bool = False,
    no_vault: bool = False,
    damaged_store: bool = False,
    bots: bool = False,
) -> None:
    """A vault, a home and a data dir that belong to nobody, wired into os.environ.

    Written before config.load and before the env scrub, because both read os.environ — and
    because `claude_home` comes from Path.home(), which only USERPROFILE (Windows) or HOME
    (POSIX) can move.

    `dirty` makes the scratch vault a git repository with one uncommitted file. It is the
    only way to reach the surfaces that talk about uncommitted changes at all: rails.py reports
    `git.dirty` from a real `git status`, so with no repository it is always 0 and both the
    row chip and the confirmation before closing such a session are unreachable.

    `no_vault` withholds VIKI_VAULT so the launch has no vault of its own. The folder is still
    created and still made dirty when asked, because the open-an-existing-folder scenarios need
    something to open.

    `damaged_store` leaves bytes no SQLite database starts with where the app store goes, so the
    launch below takes the recovery fork instead of opening it.

    On POSIX only HOME is rewired: there are no per-account TEMP/LOCALAPPDATA/APPDATA variables
    to point elsewhere, and nothing else in the app reads them off this OS.
    """
    if bots and not _bot_scratch_is_safe():
        raise ValueError("unsafe Bot scratch root")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    parent = SCRATCH / "workspaces" if bots else SCRATCH
    if bots:
        parent.mkdir(parents=True, exist_ok=True)
        resolved = parent.resolve()
        if resolved != parent.absolute() or not resolved.is_relative_to(SCRATCH.resolve()):
            raise ValueError("unsafe Bot workspace parent")
    vault, home, data = parent / "vault", SCRATCH / "home", SCRATCH / "data"
    common_env = {
        "VIKI_DATA_DIR": str(data),
        "VIKI_CLAUDE_CLI": sys.executable,
        "VIKI_TEST_CLI_SCRIPT": str(REPO / "tests" / "fake_claude.py"),
        **({} if no_vault else {"VIKI_VAULT": str(vault)}),
    }
    if sys.platform == "win32":
        appdata = home / "AppData"
        for path in (vault, home / ".claude", appdata / "Local" / "Temp", appdata / "Roaming"):
            path.mkdir(parents=True, exist_ok=True)
        os.environ.update(
            {
                "USERPROFILE": str(home),
                "HOME": str(home),
                # workspaces.denied_roots() derives its refusals from exactly these variables, and
                # on a standard profile %TEMP% lies under %LOCALAPPDATA% — where SCRATCH itself
                # lives. Left pointing at the real profile they would deny the scratch vault.
                "TEMP": str(appdata / "Local" / "Temp"),
                "TMP": str(appdata / "Local" / "Temp"),
                "LOCALAPPDATA": str(appdata / "Local"),
                "APPDATA": str(appdata / "Roaming"),
                **common_env,
            }
        )
    else:
        for path in (vault, home / ".claude"):
            path.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home)
        os.environ.update(common_env)
    # Exercise real private-directory admission in this disposable test directory: the
    # owner-only DACL on Windows, the mode bits on POSIX.
    from viki_agent.protection import create_private_directory

    create_private_directory(data)
    if bots:
        # Bootstrap needs an empty destination. Discard inherited fake resume state so this
        # run cannot write outside its scratch root.
        os.environ.pop("FAKE_CLAUDE_STATE", None)
    else:
        # Legacy runs keep the existing-vault shape used by their acceptance assertions.
        (vault / "CLAUDE.md").write_text("# scratch vault (end-to-end run)\n", encoding="utf-8")
    if no_vault:
        # A previous run of this same scratch root may have left one behind, and config.load
        # reads os.environ, not just what was updated here.
        os.environ.pop("VIKI_VAULT", None)
    if damaged_store:
        (data / "app.sqlite3").write_bytes(b"not a database")
    if dirty:
        _dirty_repo(vault)
    if fixture:
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(Path(fixture).resolve())


def _dirty_repo(vault: Path) -> None:
    """A real repository with a real uncommitted change, because rails.py runs real git.

    Identity is set on this repository only, so it works on a machine with no global git
    config and touches none of the user's. A failure is swallowed rather than raised: a
    screenshot run on a box without git is worth less, not worth nothing.
    """
    git = ["git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", str(vault)]
    try:
        for step in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "e2e@example.invalid"],
            ["config", "user.name", "e2e"],
            ["add", "-A"],
            ["commit", "-q", "-m", "scratch"],
        ):
            subprocess.run([*git, *step], check=True, capture_output=True)
        note = vault / "30-knowledge" / "note.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("uncommitted\n", encoding="utf-8")
    except OSError, subprocess.SubprocessError:
        pass


def _fake_env(env: dict[str, str]) -> dict[str, str]:
    """What tests/fake_claude.py reads, and nothing else."""
    keys = ("FAKE_CLAUDE_SCRIPT", "FAKE_CLAUDE_STATE")
    return {key: env[key] for key in keys if env.get(key)}


if __name__ == "__main__":
    raise SystemExit(main())
