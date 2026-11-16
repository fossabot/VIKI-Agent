"""Profile start-preview through durable REST send, with no real provider or user data."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.types import Message

from tests.profiles.test_profile_document import document
from tests.profiles.test_profile_launch import _server, with_servers
from tests.profiles.test_provider_catalogs import capabilities as tp_capabilities
from tests.profiles.test_provider_catalogs import models as tp_models
from viki_agent import protection
from viki_agent.app.diagnostics import Readiness
from viki_agent.app.settings import SettingsService
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.conversations.commands import CommandService, TerminalState
from viki_agent.drivers.base import StartFacts
from viki_agent.drivers.claude.control import Launch, build_argv
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import Event, ModelOption, SessionStarted
from viki_agent.profiles import profile_launch
from viki_agent.profiles.mcp_config import config_path
from viki_agent.profiles.profile_capabilities import (
    ADAPTER_VERSION,
    FIXTURE_VERSION,
    evidence_version,
)
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.profiles.provider_catalogs import MAX_CATALOGS, ProviderCatalogService
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.server import create_app
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService

WAIT_S = 0.05
# A hang guard, not a sleep: these events fire in single-digit milliseconds once the
# TestClient's server thread finishes real cross-thread work (driver open, native_initialized,
# the dispatch intent write, send). It only stops the test if that work hangs: thirty
# seconds, since a parallel full run on a busy machine has stalled such work past five.
HANG_GUARD_S = 30
# A CLI version the compatibility fixture does not prove, standing for another build.
OTHER_VERSION = "2.1.280"
# A Claude Code release newer than FIXTURE_VERSION: start-checked evidence, filed under "2.x".
NEWER_VERSION = "2.1.309"
# Another release that files under the same "2.x" label, proving a catalog outlives an update.
EARLIER_2X_VERSION = "2.1.300"

BASE = "http://127.0.0.1:8765"
TOKEN = "profile-launch-test"
AUTH = {"authorization": f"Bearer {TOKEN}"}
WRITE = {**AUTH, "origin": BASE}
WS_HEADERS = {"host": "127.0.0.1:8765", "origin": BASE}


def ident() -> str:
    return str(uuid.uuid4())


class SyntheticCatalog(Catalog):
    def __init__(self, home: Path, vault: Path) -> None:
        super().__init__(home, vault)
        self.transcripts: set[str] = set()

    def exists(self, session_id: str) -> bool:
        return session_id in self.transcripts


class FakeDriver:
    pid: int | None = None
    busy = False
    cli_version = FIXTURE_VERSION  # the version this spawn reports; the rig's factory sets it
    facts = StartFacts(None, None, (), None)  # what the start check reads; the factory sets it
    models: list[ModelOption] = []  # the model menu open() reports; the factory sets it

    def __init__(
        self,
        launch: Launch,
        emit: Callable[[Event], None],
        state: AppState,
        catalog: SyntheticCatalog,
    ) -> None:
        self.launch = launch
        self.emit = emit
        self.state = state
        self.catalog = catalog
        self.alive = False
        self.sent = threading.Event()
        self.marker_before_send = False

    async def open(self) -> SessionStarted:
        self.alive = True
        return SessionStarted(
            model="default",
            mode="auto",
            effort="high",
            commands=[],
            agents=[],
            models=self.models,
            cli_version=self.cli_version,
        )

    async def start_facts(self) -> StartFacts:
        return self.facts

    async def send(self, text: str) -> None:
        with self.state.store._connection() as connection:
            marker = connection.execute(
                "SELECT native_started FROM session_profile_bindings WHERE session_id=?",
                (self.launch.session_id,),
            ).fetchone()
        self.marker_before_send = marker == (1,)
        self.catalog.transcripts.add(self.launch.session_id)
        self.sent.set()

    async def interrupt(self) -> None:
        pass

    def respond(self, tool_use_id: str, answer: Any) -> bool:
        return True

    def pending(self) -> list[str]:
        return []

    async def set_model(self, model: str) -> None:
        pass

    async def close(self) -> None:
        self.alive = False


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.vault = tmp_path / "vault"
        self.home = tmp_path / "home"
        self.vault.mkdir()
        self.home.mkdir()
        self.config = Config(
            vault=self.vault,
            claude_home=self.home,
            cli=Path("synthetic-claude.exe"),
            edge=None,
            data_dir=self.data,
        )
        self.state = AppState(self.data / "state.json", Prefs(mode="auto", effort="low"))
        self.workspaces = WorkspaceService(
            self.state.store,
            env={},
            data_dir=self.data,
            home=self.home,
            install_dir=tmp_path / "install",
            git_available=lambda: False,
            hide_previews=lambda: False,
        )
        workspace, _ = self.workspaces.register_existing(ident(), str(self.vault))
        self.workspace_id = workspace.workspace_id
        self.workspaces.select(ident(), workspace.workspace_id)
        self.settings = SettingsService(
            self.state.store,
            provider_defaults=lambda: Prefs(),
            on_chat_defaults=lambda _: None,
        )
        self.profile_id = ident()
        ProfileService(self.state.store).create(self.profile_id, ident(), document())
        self.catalog = SyntheticCatalog(self.home, self.vault)
        self.readiness = Readiness(
            config=self.config, launch_id=ident(), git_available=lambda: False
        )
        self.readiness.observe_version(FIXTURE_VERSION)
        self.readiness.observe_billing([])
        # What a spawn reports, which the readiness check's version need not equal: a CLI can
        # move between the two reads.
        self.started_version = FIXTURE_VERSION
        # The start check's own report: None, a plain post-open() default, until a test sets it.
        self.started_facts = StartFacts(None, None, (), None)
        self.started_models: list[ModelOption] = []
        self.drivers: list[FakeDriver] = []
        self.driver_created = threading.Event()

    def manager(self) -> SessionManager:
        def factory(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> FakeDriver:
            driver = FakeDriver(launch, emit, self.state, self.catalog)
            driver.cli_version = self.started_version
            driver.facts = self.started_facts
            driver.models = self.started_models
            self.drivers.append(driver)
            self.driver_created.set()
            return driver

        return SessionManager(
            config=DriverConfig(cli=("synthetic",), vault=self.vault, env={}),
            state=self.state,
            claude_home=self.home,
            make_driver=factory,
        )

    def app(self, manager: SessionManager):
        return create_app(
            config=self.config,
            token=TOKEN,
            manager=manager,
            catalog=self.catalog,
            static_dir=None,
            settings=self.settings,
            workspaces=self.workspaces,
            readiness=self.readiness,
            vault_poll_s=60,
        )

    def preview(
        self, client: TestClient, session_id: str, command_id: str, revision: int = 1
    ) -> dict[str, Any]:
        response = client.post(
            f"/api/v2/profiles/{self.profile_id}/start-preview",
            headers=WRITE,
            json={
                "command_id": command_id,
                "session_id": session_id,
                "expected_profile_revision": revision,
                "workspace_id": self.workspace_id,
            },
        )
        assert response.status_code == 200
        return response.json()


def _bind(rig: Rig, key: str) -> None:
    """A local copy of `test_provider_catalogs._bind`'s insert, against the rig's own profile."""
    now = "2026-09-21T00:00:00.000Z"
    with rig.state.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_profile_bindings "
            "(session_id,command_id,request_hash,profile_id,profile_revision,workspace_id,"
            "workspace_revision,policy,policy_revision,settings_revision,runtime,capability_key,"
            "launch_spec_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident(),
                ident(),
                "0" * 64,
                rig.profile_id,
                1,
                rig.workspace_id,
                1,
                "ordinary",
                1,
                0,
                "claude",
                key,
                "1" * 64,
                now,
            ),
        )


async def _next_message(ws: WebSocketTestSession, wait_s: float) -> Message:
    with anyio.fail_after(wait_s):
        return await ws._send_rx.receive()


def recv(ws: WebSocketTestSession, wait_s: float = WAIT_S) -> dict[str, Any]:
    """`wait_s` is HANG_GUARD_S only for a frame that follows real work, such as a driver open."""
    return json.loads(ws.portal.call(_next_message, ws, wait_s)["text"])


@contextmanager
def authed(client: TestClient) -> Iterator[WebSocketTestSession]:
    with client.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "v": 2})
        assert recv(ws)["type"] == "vault.sessions"
        yield ws


def open_session(ws: WebSocketTestSession, session_id: str, wait_s: float = HANG_GUARD_S) -> str:
    request_id = ident()
    ws.send_json(
        {
            "type": "session.open",
            "session_id": session_id,
            "request_id": request_id,
            "after_seq": 0,
        }
    )
    snapshot, synced = recv(ws, wait_s), recv(ws, wait_s)
    assert (snapshot["type"], synced["type"]) == ("session.snapshot", "session.synced")
    assert snapshot["request_id"] == synced["request_id"] == request_id
    return snapshot["generation"]


@contextmanager
def terminal_signal(monkeypatch: pytest.MonkeyPatch, command_id: str) -> Iterator[threading.Event]:
    """Fire once `CommandService.finish` commits the named command's terminal projection."""
    done = threading.Event()
    original = CommandService.finish

    def patched(self: CommandService, cid: str, state: TerminalState, reason: str) -> Any:
        result = original(self, cid, state, reason)
        if cid == command_id:
            done.set()
        return result

    monkeypatch.setattr(CommandService, "finish", patched)
    yield done


def fetch_command(client: TestClient, command_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v2/commands/{command_id}", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def test_clear_then_save_turns_a_blocked_duplicate_launchable(tmp_path: Path) -> None:
    rig, session_id = Rig(tmp_path), ident()
    profiles = ProfileService(rig.state.store)
    blocked_doc = {**document(), "tools": {**document()["tools"], "required": ["Bash"]}}
    profiles.revise(rig.profile_id, ident(), 1, blocked_doc)
    target_id = ident()
    profiles.duplicate(rig.profile_id, ident(), 2, target_id, "claude")
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        blocked = client.post(
            f"/api/v2/profiles/{target_id}/start-preview",
            headers=WRITE,
            json={
                "command_id": ident(),
                "session_id": session_id,
                "expected_profile_revision": 1,
                "workspace_id": rig.workspace_id,
            },
        )
        assert blocked.status_code == 422
        body = blocked.json()
        assert "tools.required" in body["blockers"] and "identity" not in body
        # Cleared from the duplicate's own fetched document, not a fresh `document()`, so
        # revision 2 keeps the duplicate's own fields, its provenance included.
        cleared_doc = profiles.get(target_id)["document"]
        cleared_doc["tools"]["required"] = []
        expected = copy.deepcopy(cleared_doc)
        profiles.revise(target_id, ident(), 1, cleared_doc)
        assert profiles.get(target_id)["document"] == expected
        launchable = client.post(
            f"/api/v2/profiles/{target_id}/start-preview",
            headers=WRITE,
            json={
                "command_id": ident(),
                "session_id": session_id,
                "expected_profile_revision": 2,
                "workspace_id": rig.workspace_id,
            },
        )
        assert launchable.status_code == 200
        result = launchable.json()
        assert result["launchable"]
        assert result["identity"]["name"] == document()["identity"]["name"]


def test_start_preview_after_eviction_of_another_native_version(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    catalogs = ProviderCatalogService(rig.state.store)
    for index in range(8):
        # A distinct `evidence` string per row, so each observation gets its own content key
        # (the key excludes `observed_at`) instead of re-observing a single bound row.
        row = catalogs.observe(
            "claude",
            ADAPTER_VERSION,
            "native-other",
            tp_capabilities(f"bound-{index}"),
            tp_models(),
        )
        _bind(rig, row["catalog_key"])
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, ident(), ident())
    assert preview["launchable"]
    assert catalogs.latest("claude", ADAPTER_VERSION, FIXTURE_VERSION) is not None


def test_evicted_session_review_answers_capability_changed(tmp_path: Path) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, command_id)
        assert preview["launchable"]
        with rig.state.store._connection(write=True) as connection:
            connection.execute(
                "DELETE FROM provider_catalogs WHERE catalog_key=?", (preview["capability_key"],)
            )
        response = client.get(f"/api/v2/profile-sessions/{session_id}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["launchable"] is False
    assert body["blockers"] == ["capability_changed"]


def test_a_spawn_at_another_version_never_evicts_a_prepared_catalog(tmp_path: Path) -> None:
    """The CLI was `OTHER_VERSION` when the catalogs now bound were observed and every retained
    row is bound; the CLI moved on, so the readiness check holds FIXTURE_VERSION and session S
    is prepared against that version's catalog. An ordinary chat whose spawn still reports the
    old version must not make its observation take S's catalog: starts check the readiness
    version, so the eviction would answer `capability_changed`.
    """
    rig, session_id, ordinary = Rig(tmp_path), ident(), ident()
    rig.started_version = OTHER_VERSION
    catalogs = ProviderCatalogService(rig.state.store)
    for index in range(MAX_CATALOGS - 1):
        # One short of the retained bound, so S's own catalog fills the last place. A distinct
        # `evidence` string per row gives each observation its own content key.
        row = catalogs.observe(
            "claude",
            ADAPTER_VERSION,
            OTHER_VERSION,
            tp_capabilities(f"bound-{index}"),
            tp_models(),
        )
        _bind(rig, row["catalog_key"])
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())
        assert preview["launchable"]
        key = preview["capability_key"]
        with authed(client) as ws:
            generation = open_session(ws, ordinary, HANG_GUARD_S)
            ws.send_json(
                {
                    "type": "turn.send",
                    "session_id": ordinary,
                    "generation": generation,
                    "command_id": ident(),
                    "text": "hi",
                }
            )
            assert rig.driver_created.wait(HANG_GUARD_S)
            # The observation is awaited inside the start, before the send: `sent` is its barrier.
            assert rig.drivers[0].sent.wait(HANG_GUARD_S)
        review = client.get(f"/api/v2/profile-sessions/{session_id}", headers=AUTH)
    assert review.status_code == 200
    assert review.json()["blockers"] == [] and review.json()["launchable"] is True
    assert catalogs.get(key) is not None
    assert catalogs.latest("claude", ADAPTER_VERSION, FIXTURE_VERSION) is not None
    with rig.state.store._connection() as connection:
        retained = connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0]
    assert retained == MAX_CATALOGS  # the skipped observation wrote no row of its own


def test_retried_preview_with_a_transcript_reopens_instead_of_attaching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, preview_id = Rig(tmp_path), ident(), ident()
    first_manager = rig.manager()
    with TestClient(rig.app(first_manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, preview_id)
        first_send = ident()
        with terminal_signal(monkeypatch, first_send) as done:
            send_prepared(client, session_id, preview["generation"], first_send)
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert rig.drivers[0].sent.wait(HANG_GUARD_S)
            assert done.wait(HANG_GUARD_S)

    # A second manager (an app reload) holds no actor for the session, but its transcript now
    # exists: the retried start-preview (same command id) must reopen it, not attach it fresh.
    rig.driver_created.clear()
    second_manager = rig.manager()
    with TestClient(rig.app(second_manager), base_url=BASE) as client:
        retried = rig.preview(client, session_id, preview_id)
        assert retried["session_id"] == session_id and retried["launchable"]
        with authed(client) as ws:
            generation = open_session(ws, session_id, HANG_GUARD_S)
            ws.send_json(
                {
                    "type": "turn.send",
                    "session_id": session_id,
                    "generation": generation,
                    "command_id": ident(),
                    "text": "hi",
                }
            )
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert rig.drivers[-1].sent.wait(HANG_GUARD_S)
    assert rig.drivers[-1].launch.resume is True


def test_profile_session_origin_is_read_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview retry, a fork refusal and a reopen after reload each read the durable origin
    in a worker thread: a SQLite read on the event loop stalls every other session."""
    rig, session_id, command_id, ordinary = Rig(tmp_path), ident(), ident(), ident()
    on_loop: list[str] = []
    read = rig.state.store.session_origin

    def spy(sid: str) -> str | None:
        try:
            asyncio.get_running_loop()
            on_loop.append(sid)
        except RuntimeError:
            pass
        return read(sid)

    monkeypatch.setattr(rig.state.store, "session_origin", spy)
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client, authed(client) as ws:
        preview = rig.preview(client, session_id, command_id)
        assert rig.preview(client, session_id, command_id) == preview
        generation = open_session(ws, session_id, HANG_GUARD_S)
        fork_command = ident()
        ws.send_json(
            {
                "type": "session.fork",
                "session_id": session_id,
                "generation": generation,
                "command_id": fork_command,
                "fork_id": ident(),
            }
        )
        receipt = recv(ws, HANG_GUARD_S)
        assert (receipt["type"], receipt["command_id"]) == ("control.receipt", fork_command)
        assert receipt["data"] == {"status": "rejected", "reason": "profile_locked"}
        open_session(ws, ordinary, HANG_GUARD_S)
        refused = client.post(
            f"/api/v2/profiles/{rig.profile_id}/start-preview",
            headers=WRITE,
            json={
                "command_id": ident(),
                "session_id": ordinary,
                "expected_profile_revision": 1,
                "workspace_id": rig.workspace_id,
            },
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "session_exists"
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client, authed(client) as ws:
        open_session(ws, session_id, HANG_GUARD_S)
    assert on_loop == []
    assert all(driver.launch.session_id == ordinary for driver in rig.drivers)


def test_preview_retry_and_session_open_across_reload_never_spawn(tmp_path: Path) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    first_manager = rig.manager()
    with TestClient(rig.app(first_manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, command_id)
        assert preview["launchable"] and preview["draft"]["revision"] == 1
        assert rig.preview(client, session_id, command_id) == preview
        with authed(client) as ws:
            assert open_session(ws, session_id) == preview["generation"]
        assert rig.drivers == []

    second_manager = rig.manager()
    with TestClient(rig.app(second_manager), base_url=BASE) as client, authed(client) as ws:
        open_session(ws, session_id)
        assert rig.drivers == []


def test_durable_send_checkpoints_before_native_send_and_reopens_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, preview_id, command_id = Rig(tmp_path), ident(), ident(), ident()
    first_manager = rig.manager()
    with TestClient(rig.app(first_manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, preview_id)
        with terminal_signal(monkeypatch, command_id) as done:
            response = client.post(
                "/api/v2/commands",
                headers=WRITE,
                json={
                    "command_id": command_id,
                    "session_id": session_id,
                    "generation": preview["generation"],
                    "draft_revision": 1,
                },
            )
            assert response.status_code == 202
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert len(rig.drivers) == 1 and rig.drivers[0].sent.wait(HANG_GUARD_S)
            assert rig.drivers[0].marker_before_send
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("unknown", "native_unconfirmed")

    second_manager = rig.manager()
    with TestClient(rig.app(second_manager), base_url=BASE) as client, authed(client) as ws:
        open_session(ws, session_id)
        assert len(rig.drivers) == 1


@pytest.mark.parametrize("drift", ["policy", "catalog"])
def test_launch_drift_is_bad_request_and_terminal_not_dispatched(
    tmp_path: Path, drift: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())
        if drift == "policy":
            rig.workspaces.set_policy(ident(), rig.workspace_id, 1, "protected", [])
        else:
            # A version-class move, not a new model menu: once a binding needs only its own
            # stored rows to match the current baseline, a re-keyed catalog at the same label
            # does not disturb it.
            rig.readiness.observe_version("2.1.259")
        with terminal_signal(monkeypatch, command_id) as done:
            accepted = client.post(
                "/api/v2/commands",
                headers=WRITE,
                json={
                    "command_id": command_id,
                    "session_id": session_id,
                    "generation": preview["generation"],
                    "draft_revision": 1,
                },
            )
            assert accepted.status_code == 202
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        assert rig.drivers == []


def test_launch_refused_for_billing_names_settings_not_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())
        rig.readiness.observe_billing(["x"])
        with terminal_signal(monkeypatch, command_id) as done:
            accepted = client.post(
                "/api/v2/commands",
                headers=WRITE,
                json={
                    "command_id": command_id,
                    "session_id": session_id,
                    "generation": preview["generation"],
                    "draft_revision": 1,
                },
            )
            assert accepted.status_code == 202
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        message = errors[-1]["data"]["message"]
        assert "Settings" in message
        assert "Agents" not in message
        assert rig.drivers == []


@pytest.mark.parametrize(
    ("code", "way_on"),
    [
        ("connection_missing", "Choose an available model connection in Providers."),
        ("connections_unavailable", "Check model connections in Providers."),
        ("connection_invalid", "Choose a valid model connection in Providers."),
        ("connection_bot_only", "Start this profile from its Bot."),
    ],
)
def test_profile_connection_guard_has_specific_path_free_way_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, way_on: str
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())

        def refused(_self: ProfileLaunchService, _sid: str, _prefs: Prefs) -> None:
            raise ProfileError(code)

        monkeypatch.setattr(ProfileLaunchService, "validate_launch", refused)
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        assert fetch_command(client, command_id)["reason"] == "start_failed"
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"] == {
            "code": "bad_request",
            "message": f"Profile launch refused ({code}). {way_on}",
            "recoverable": True,
        }
        assert rig.drivers == []


def test_launch_refused_for_missing_workspace_root_names_settings_not_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())
        shutil.rmtree(rig.vault)
        with terminal_signal(monkeypatch, command_id) as done:
            accepted = client.post(
                "/api/v2/commands",
                headers=WRITE,
                json={
                    "command_id": command_id,
                    "session_id": session_id,
                    "generation": preview["generation"],
                    "draft_revision": 1,
                },
            )
            assert accepted.status_code == 202
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        message = errors[-1]["data"]["message"]
        assert "Settings" in message
        assert "Agents" not in message
        assert rig.drivers == []


def mcp_rig(tmp_path: Path) -> Rig:
    """A Rig whose profile lists one app server (revision 2) in an ordinary workspace."""
    rig = Rig(tmp_path)
    ProfileService(rig.state.store).revise(rig.profile_id, ident(), 1, with_servers(_server()))
    rig.workspaces.set_policy(ident(), rig.workspace_id, 1, "ordinary", [])
    return rig


def send_prepared(client: TestClient, session_id: str, generation: str, command_id: str) -> None:
    response = client.post(
        "/api/v2/commands",
        headers=WRITE,
        json={
            "command_id": command_id,
            "session_id": session_id,
            "generation": generation,
            "draft_revision": 1,
        },
    )
    assert response.status_code == 202


@pytest.mark.usefixtures("desktop_umask")
def test_bound_send_passes_the_previewed_mcp_config_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id, ordinary = mcp_rig(tmp_path), ident(), ident(), ident()
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"]
        config = preview["mcp_config"]
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert rig.drivers[0].sent.wait(HANG_GUARD_S)
            assert done.wait(HANG_GUARD_S)
        # An ordinary chat beside the bound one never carries the profile's servers.
        rig.driver_created.clear()
        with authed(client) as ws:
            generation = open_session(ws, ordinary, HANG_GUARD_S)
            ws.send_json(
                {
                    "type": "turn.send",
                    "session_id": ordinary,
                    "generation": generation,
                    "command_id": ident(),
                    "text": "hi",
                }
            )
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert rig.drivers[1].sent.wait(HANG_GUARD_S)
    launch = rig.drivers[0].launch
    assert launch.mcp_config is not None
    path = Path(launch.mcp_config)
    digest = hashlib.sha256(config["json"].encode("ascii")).hexdigest()
    assert path == (rig.data / "mcp").absolute() / f"{digest}.json"
    assert config["argv_element"] == f"--mcp-config={path}"
    assert path.read_bytes() == config["json"].encode("ascii")
    argv = build_argv(("claude",), launch, mode="auto", effort="high")
    assert [arg for arg in argv if arg.startswith("--mcp-config")] == [config["argv_element"]]
    if sys.platform == "win32":
        protection.require_private_directory(path.parent)
        protection.require_private_file(path)
    else:
        assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert rig.drivers[1].launch.session_id == ordinary
    assert rig.drivers[1].launch.mcp_config is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("server_removed", "mcp_revoked"),
        ("archived", "mcp_revoked"),
        ("policy", "launch_changed"),
        ("write_failed", "mcp_config_unavailable"),
    ],
)
def test_bound_send_refuses_before_any_driver_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, code: str
) -> None:
    rig, session_id, command_id = mcp_rig(tmp_path), ident(), ident()
    profiles = ProfileService(rig.state.store)
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"] and preview["mcp_config"] is not None
        if change == "server_removed":
            profiles.revise(rig.profile_id, ident(), 2, document())
        elif change == "archived":
            state_revision = profiles.get(rig.profile_id)["state_revision"]
            profiles.archive(rig.profile_id, ident(), state_revision, True)
        elif change == "policy":
            rig.workspaces.set_policy(ident(), rig.workspace_id, 2, "protected", [])
        else:

            def refused(directory: Path, data: bytes) -> Path:
                raise OSError(13, "synthetic")

            monkeypatch.setattr(profile_launch, "write_config", refused)
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        message = errors[-1]["data"]["message"]
        assert f"({code})" in message
        if code == "mcp_config_unavailable":
            assert "Send again." in message
        elif change == "server_removed":
            assert "Open the profile in Agents and continue from there." in message
    assert rig.drivers == []
    assert not (rig.data / "mcp").exists()


def test_bound_send_refuses_when_mcp_storage_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = mcp_rig(tmp_path), ident(), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"] and preview["mcp_config"] is not None

        def broken(_self: ProfileLaunchService, _sid: str) -> str | None:
            raise protection.ProtectionError()

        monkeypatch.setattr(ProfileLaunchService, "mcp_config_path", broken)
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors and errors[-1]["data"]["code"] == "internal"
        assert errors[-1]["data"]["message"] == "Profile storage is unavailable."
    assert rig.drivers == []


UNAVAILABLE_MESSAGE = (
    "Profile launch refused (mcp_config_unavailable). Send again. If it fails again, restart "
    "VIKI Agent; if it still fails, check that the data folder's mcp folder is private to you."
)


@pytest.mark.parametrize("planted", ["foreign_file", "partial_file", "folder_not_private"])
@pytest.mark.usefixtures("desktop_umask")
def test_an_unwritable_mcp_config_names_the_restart_and_the_private_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    """Each of the three errors behind `mcp_config_unavailable` reaches the user with the way on
    that covers it: sending again, a restart, and the folder's privacy."""
    rig, session_id, command_id = mcp_rig(tmp_path), ident(), ident()
    manager = rig.manager()
    folder = rig.data / "mcp"
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"] and preview["mcp_config"] is not None
        data = preview["mcp_config"]["json"].encode("ascii")
        # Planted after the launch's sweep, which removes a generated name it finds.
        path, other = folder, b""
        if planted == "folder_not_private":
            # A regular file where the private folder belongs: `require_private_directory`
            # refuses it, so `write_config` raises ProtectionError before any write.
            folder.write_bytes(other)
        else:
            assert protection.create_private_directory(folder)
            # Either way the generated name holds other bytes, so the read-back refuses EEXIST:
            # a file the app's writer did not put there, and a write that died before its end.
            path = config_path(folder, data)
            other = b"{}" if planted == "foreign_file" else data[:-1]
            path.write_bytes(other)
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        assert errors[-1]["data"]["message"] == UNAVAILABLE_MESSAGE
    assert rig.drivers == []
    assert path.read_bytes() == other  # the refused launch wrote nothing over what was there


# The start check, the catalog label and the refusal text on a newer, start-checked 2.x.


def plan_document() -> dict[str, Any]:
    """`document()` with its permission mode pinned to plan; nothing else pinned."""
    raw = document()
    raw["runtime"]["permission_mode"] = "plan"
    return raw


def test_a_plan_profile_confirmed_by_the_start_report_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    rig.readiness.observe_version(NEWER_VERSION)
    ProfileService(rig.state.store).revise(rig.profile_id, ident(), 1, plan_document())
    rig.started_facts = StartFacts(None, "plan", (), None)
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"]
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert rig.driver_created.wait(HANG_GUARD_S)
            assert rig.drivers[0].sent.wait(HANG_GUARD_S)
            assert rig.drivers[0].marker_before_send
            assert done.wait(HANG_GUARD_S)


def test_a_plan_profile_not_confirmed_by_the_start_report_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    rig.readiness.observe_version(NEWER_VERSION)
    ProfileService(rig.state.store).revise(rig.profile_id, ident(), 1, plan_document())
    rig.started_facts = StartFacts(None, "default", (), None)
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident(), revision=2)
        assert preview["launchable"]
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        assert errors[-1]["data"]["message"] == (
            "Profile launch refused (plan_mode_unconfirmed). Claude Code did not confirm this "
            "profile's settings when it started, so nothing was sent. Edit the profile in "
            "Agents, then start again."
        )
    assert len(rig.drivers) == 1 and not rig.drivers[0].sent.is_set()


def test_an_ordinary_start_stores_its_catalog_under_the_label_for_later_versions(
    tmp_path: Path,
) -> None:
    """2.1.300 and NEWER_VERSION both file under the start-checked "2.x" label, so an
    ordinary chat's menu observed at one is what the models route returns at the other."""
    rig, ordinary = Rig(tmp_path), ident()
    rig.readiness.observe_version(EARLIER_2X_VERSION)
    rig.started_version = EARLIER_2X_VERSION
    rig.started_models = [ModelOption(value="m", label="M", description="")]
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client, authed(client) as ws:
        generation = open_session(ws, ordinary, HANG_GUARD_S)
        ws.send_json(
            {
                "type": "turn.send",
                "session_id": ordinary,
                "generation": generation,
                "command_id": ident(),
                "text": "hi",
            }
        )
        assert rig.driver_created.wait(HANG_GUARD_S)
        assert rig.drivers[0].sent.wait(HANG_GUARD_S)
        catalogs = ProviderCatalogService(rig.state.store)
        assert (
            catalogs.latest("claude", ADAPTER_VERSION, evidence_version(EARLIER_2X_VERSION))
            is not None
        )
        rig.readiness.observe_version(NEWER_VERSION)
        response = client.get("/api/v2/providers/claude/models", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["native_version"] == NEWER_VERSION
    assert body["status"] == "supported"
    assert body["models"] == [{"value": "m", "label": "M", "description": ""}]


def test_a_reported_fixture_version_on_a_2x_binding_is_capability_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id, command_id = Rig(tmp_path), ident(), ident()
    rig.readiness.observe_version(NEWER_VERSION)
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url=BASE) as client:
        preview = rig.preview(client, session_id, ident())
        assert preview["launchable"]
        rig.started_facts = StartFacts(FIXTURE_VERSION, None, (), None)
        with terminal_signal(monkeypatch, command_id) as done:
            send_prepared(client, session_id, preview["generation"], command_id)
            assert done.wait(HANG_GUARD_S)
        terminal = fetch_command(client, command_id)
        assert (terminal["state"], terminal["reason"]) == ("not_dispatched", "start_failed")
        actor = manager.get(session_id)
        assert actor is not None
        errors = [event for event in actor.events_after(0) if event["type"] == "error"]
        assert errors[-1]["data"]["code"] == "bad_request"
        assert errors[-1]["data"]["message"] == (
            "Profile launch refused (capability_changed). Open the profile in Agents and "
            "continue from there."
        )
    assert len(rig.drivers) == 1 and not rig.drivers[0].sent.is_set()
