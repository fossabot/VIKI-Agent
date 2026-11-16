"""HTTP surface for data locations, exports, imports and reset: a minimal app, tmp_path only.

This test builds a small Starlette app around `data_routes` directly (not `server.create_app`,
which wires it once every data-related service is supplied); auth/origin checks are the same
shape as every other private route in this codebase.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.types import Message

from viki_agent import workspaces
from viki_agent.app import data_api
from viki_agent.app.data_api import LocationsService, ResetService, _measure, data_routes
from viki_agent.app.exports import ExportService
from viki_agent.app.imports import ImportService
from viki_agent.app.settings import KEYS, SettingsConflict, SettingsError, SettingsService
from viki_agent.conversations.approvals import (
    ApprovalError,
    ApprovalService,
    ChoiceInput,
    NativeRequest,
    PermissionChoice,
)
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService, draft_scope
from viki_agent.domain import (
    ActiveWorkspace,
    CanonicalRoot,
    EffectiveSettings,
    RootIdentity,
    SettingsDocument,
    Workspace,
)
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore
from viki_agent.winproc import run as winproc_run

BASE = "http://127.0.0.1:8765"
TOKEN = "data-api-test-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
MUTATION = {**AUTH, "origin": BASE}
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
DRAFT_KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GENERATION_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OWNER_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
ADMITTED_ID = "22222222-2222-4222-8222-222222222222"
FINISHED_ID = "33333333-3333-4333-8333-333333333333"
OTHER_WORKSPACE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"  # workspace_id must be exactly 36 chars
# A launch that has exited. Only one process can hold this store, so `owner <> OWNER_ID` on a
# row means the launch that wrote it is over.
DEAD_OWNER_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
RUNTIME_ID = "44444444-4444-4444-8444-444444444444"
TURN_ID = "55555555-5555-4555-8555-555555555555"
CARD: dict = {
    "type": "permission.requested",
    "data": {"tool_use_id": "toolu_1"},
    "native_request": {"runtime_id": RUNTIME_ID, "request_id": RUNTIME_ID, "turn_id": TURN_ID},
}
_STAMP = "2026-09-17T00:00:00.000Z"


def choice(index: int) -> ChoiceInput:
    """One user choice for its own native request, in the active scope's session."""
    ident = f"66666666-6666-4666-8666-{index:012d}"
    return ChoiceInput(
        choice_id=ident,
        session_id=DRAFT_KEY,
        generation=GENERATION_ID,
        tool_use_id=f"toolu_{index}",
        native=NativeRequest(RUNTIME_ID, ident, TURN_ID),
        choice=PermissionChoice(True),
    )


def workspace_row(workspace_id: str) -> dict:
    return {
        "workspace_id": workspace_id,
        "kind": "vault",
        "canonical_key": f"c:\\synthetic\\{workspace_id}",
        "display_root": f"C:\\Synthetic\\{workspace_id}",
        "physical_id": f"1:{workspace_id}",
        "name": "synthetic",
        "policy": "ordinary",
        "policy_revision": 1,
        "revision": 1,
        "source": "created",
        "layout": "unknown",
        "portable_id": None,
        "identity": "{}",
        "face_dir": None,
        "bootstrap": "none",
        "command_id": None,
        "active": 0,
        "created_at": _STAMP,
        "updated_at": _STAMP,
    }


def make_junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        link.symlink_to(target, target_is_directory=True)
        return
    done = winproc_run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], cwd=str(link.parent), timeout=10.0
    )
    if done.returncode != 0:
        pytest.skip("this volume refuses junctions")


def identity() -> RootIdentity:
    return RootIdentity(
        git_root_oids=(),
        git_common_dir=None,
        instruction_hashes={},
        established=False,
        inherits_from=None,
        probed_at="2026-09-17T00:00:00.000Z",
    )


def workspace(root: Path, *, workspace_id: str, name: str = "vault") -> Workspace:
    canonical = CanonicalRoot(
        key=os.path.normcase(str(root)), display=str(root), physical_id="1:1", aliased=False
    )
    return Workspace(
        workspace_id=workspace_id,
        kind="vault",
        root=canonical,
        name=name,
        policy="protected",
        policy_revision=1,
        revision=1,
        source="migrated",
        layout="unknown",
        portable_id=None,
        identity=identity(),
        face_dir=None,
        bootstrap="complete",
        persistent=True,
        created_at="2026-09-17T00:00:00.000Z",
        updated_at="2026-09-17T00:00:00.000Z",
    )


def effective(revision: int, values: dict) -> EffectiveSettings:
    document = SettingsDocument(
        version=1, revision=revision, values=values, read_only=False, reason=None
    )
    return EffectiveSettings(
        document=document,
        effective=dict(values),
        sources=dict.fromkeys(values, "setting"),
        keys=KEYS,
    )


def no_patch(*args: object) -> tuple[EffectiveSettings, bool]:
    raise AssertionError("apply not exercised by this test")


def build(
    tmp_path: Path,
    *,
    scratch_in_use: bool = False,
    settings_raises: Exception | None = None,
    patch: Callable[[str, int, Mapping[str, object]], tuple[EffectiveSettings, bool]] = no_patch,
    current_settings: Callable[[], SettingsDocument] = lambda: effective(0, {}).document,
    owner: str = OWNER_ID,
    copies_released: Callable[[], None] | None = None,
    on_settings: Callable[[], None] = lambda: None,
    workers: set[asyncio.Task[object]] | None = None,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")
    scratch_root = data_dir / "scratch" / "chat"
    scratch_root.mkdir(parents=True)
    (scratch_root / "leftover.txt").write_text("x", encoding="utf-8")

    ws = workspace(vault, workspace_id="vault-1")
    active = ActiveWorkspace(
        workspace=ws, cwd=vault, selected_id="vault-1", selected_by="registration", chat_only=False
    )
    state = AppState(data_dir / "state.json", Prefs(), database_path=data_dir / "app.sqlite3")
    store = state.store

    locations = LocationsService(
        workspaces=lambda: [ws],
        data_dir=data_dir,
        claude_home=claude_home,
        scratch_in_use=lambda: scratch_in_use,
    )

    def settings_reset(command_id: str) -> None:
        on_settings()
        if settings_raises is not None:
            raise settings_raises

    reset = ResetService(
        store=store,
        active_workspace=lambda: active,
        settings_reset=settings_reset,
        scratch_root=scratch_root,
        scratch_in_use=lambda: scratch_in_use,
        # The launcher mints one ledger `owner` per launch and hands the same value to both
        # ledgers and to this service; every ledger built below uses it, as the app's would.
        owner=owner,
    )
    exports = ExportService(
        diagnostics_document=lambda paths, ident: {"app": {"version": "0.1.0"}},
        redact=lambda text, roots: (text, 0),
        roots=lambda: (),
        settings_export=lambda: {"appearance.theme": "dark"},
        settings_specs={},
        workspaces_export=lambda: [],
        denied_roots=lambda: (vault, claude_home),
        destination_guard=threading.RLock(),
        log_path=data_dir / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
    )
    imports = ImportService(settings_specs=KEYS, current_settings=current_settings, patch=patch)

    routes = data_routes(
        locations,
        exports,
        imports,
        reset,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({BASE}),
        workers=set() if workers is None else workers,
        copies_released=copies_released,
    )
    app = Starlette(routes=routes)
    return app, store, state, vault, claude_home, data_dir


def test_locations_list_categories_without_credential_paths(tmp_path: Path) -> None:
    app, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    response = client.get("/api/v2/data/locations", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    ids = [category["id"] for category in body["categories"]]
    assert ids == ["vaults", "app_data", "provider", "indexes", "assets"]
    provider_items = next(c for c in body["categories"] if c["id"] == "provider")["items"]
    credentials = next(item for item in provider_items if item["kind"] == "credentials")
    assert credentials["path"] is None
    assert credentials["read_by_app"] is False
    vault_item = next(c for c in body["categories"] if c["id"] == "vaults")["items"][0]
    assert vault_item["bytes"] > 0


def test_locations_list_the_assets_folder_only_when_it_exists(tmp_path: Path) -> None:
    app, *_, data_dir = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    assets = data_dir / "assets"

    body = client.get("/api/v2/data/locations", headers=AUTH).json()
    assert next(c for c in body["categories"] if c["id"] == "assets")["items"] == []

    assets.mkdir()
    (assets / "one.png").write_bytes(b"\0" * 10)
    body = client.get("/api/v2/data/locations", headers=AUTH).json()
    items = next(c for c in body["categories"] if c["id"] == "assets")["items"]
    assert len(items) == 1
    assert items[0]["path"] == str(assets)
    assert items[0]["bytes"] >= 10
    assert items[0]["partial"] is False


def test_every_route_requires_bearer_and_valid_origin(tmp_path: Path) -> None:
    app, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    assert client.get("/api/v2/data/locations").status_code == 401
    assert (
        client.post("/api/v2/exports/preview", headers=AUTH, json={"kind": "settings"}).status_code
        == 403
    )  # authenticated but missing/foreign Origin on a mutating route
    ok = client.post("/api/v2/exports/preview", headers=MUTATION, json={"kind": "settings"})
    assert ok.status_code == 200


def test_reset_matrix_preserves_vault_homes_and_registrations(tmp_path: Path) -> None:
    app, store, state, vault, claude_home, data_dir = build(tmp_path, scratch_in_use=True)
    client = TestClient(app, base_url=BASE)

    def snapshot(root: Path) -> set[tuple[str, int, float]]:
        return {
            (str(p), p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*") if p.is_file()
        }

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("secret", encoding="utf-8")
    before_vault = snapshot(vault)
    before_claude = snapshot(claude_home)
    before_codex = snapshot(codex_home)

    # A row in the active scope (must be deleted) and a row in a foreign scope (must survive):
    # without these seeds the "drafts"/"commands" assertions below could never fail, whatever
    # scope delete_scope_rows was actually called with.
    other_vault = tmp_path / "other-vault"
    active_drafts = DraftService(state, vault)
    foreign_drafts = DraftService(state, other_vault)
    active_drafts.save(DRAFT_KEY, 0, "active scope draft")
    foreign_drafts.save(DRAFT_KEY, 0, "foreign scope draft")

    store.update_workspace(
        OTHER_WORKSPACE_ID, lambda current, rows: workspace_row(OTHER_WORKSPACE_ID)
    )

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={
            "command_id": COMMAND_ID,
            "categories": [
                "settings",
                "drafts",
                "commands",
                "approvals",
                "scratch",
                "edge_profile",
            ],
            "confirmation": "RESET",
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results["scratch"] == {"status": "refused", "reason": "in_use", "count": 0}
    assert results["edge_profile"] == {"status": "refused", "reason": "in_use", "count": 0}
    assert results["settings"] == {"status": "done", "count": 1}
    assert results["drafts"] == {"status": "done", "count": 1}
    assert results["commands"] == {"status": "done", "count": 0}
    assert results["approvals"] == {"status": "done", "count": 0}

    assert snapshot(vault) == before_vault
    assert snapshot(claude_home) == before_claude
    assert snapshot(codex_home) == before_codex
    assert active_drafts.get(DRAFT_KEY).text == ""  # deleted: reset back to the empty default
    assert foreign_drafts.get(DRAFT_KEY).text == "foreign scope draft"  # a different scope
    # ResetService.reset never calls update_workspace/delete on the workspaces table at all;
    # drafts/commands are the only scoped tables it touches, through delete_scope_rows.
    assert store.get_workspace(OTHER_WORKSPACE_ID) == workspace_row(OTHER_WORKSPACE_ID)


def test_commands_reset_keeps_an_admitted_turn_and_reports_what_stayed(tmp_path: Path) -> None:
    app, _store, state, vault, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    drafts = DraftService(state, vault)
    ledger = CommandService(state, vault, owner=OWNER_ID)
    drafts.save(DRAFT_KEY, 0, "the turn the owner just sent")
    ledger.accept(CommandInput(ADMITTED_ID, DRAFT_KEY, GENERATION_ID, 1))
    drafts.save(OTHER_SESSION_ID, 0, "a turn that already ended")
    ledger.accept(CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1))
    ledger.finish(FINISHED_ID, "unknown", "internal")

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["commands"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    # The finished row goes, the admitted one stays, and the result says both — not "done".
    assert response.json()["results"]["commands"] == {"status": "partial", "count": 1, "kept": 1}
    assert ledger.lookup(CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1)) is None
    # The reset ran in the window between the acceptance and the dispatching write. Losing the
    # row there sends nothing and tells the user nothing: the receipt is already resolved.
    assert ledger.mark_dispatching(ADMITTED_ID).text == "the turn the owner just sent"


def seed_dead_approval(store: SessionStore, scope: str, index: int) -> None:
    """One terminal row a previous launch left behind, inserted straight through SQLite."""
    ident = f"77777777-7777-4777-8777-{index:012d}"
    with contextlib.closing(sqlite3.connect(store.path)) as connection:
        connection.execute(
            f"INSERT INTO approvals VALUES ({','.join('?' * 15)})",
            (
                scope,
                ident,
                DRAFT_KEY,
                GENERATION_ID,
                RUNTIME_ID,
                ident,
                TURN_ID,
                "toolu_dead",
                "b" * 64,
                DEAD_OWNER_ID,
                "delivered_locally",
                "",
                b"x",
                _STAMP,
                _STAMP,
            ),
        )
        connection.commit()


def test_approvals_reset_keeps_a_choice_this_launch_is_recording(tmp_path: Path) -> None:
    app, store, state, vault, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    ledger = ApprovalService(state, vault, owner=OWNER_ID)
    live = choice(1)
    ledger.record(live, CARD)  # left `recorded`: part-way through one delivery attempt
    ledger.record(choice(2), CARD)
    ledger.finish(choice(2).choice_id, "delivered_locally", "")
    seed_dead_approval(store, draft_scope(vault), 1)

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["approvals"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    assert response.json()["results"]["approvals"] == {
        "status": "partial",
        "count": 1,
        "kept": 2,
    }
    # The reset landed inside one delivery attempt. Taking the row there would let a second
    # choice be recorded for a request that takes exactly one, and delivered if it is still
    # pending; the attempt has to be able to finish what it recorded.
    assert ledger.mark_delivering(live.choice_id).state == "delivering"
    assert ledger.finish(live.choice_id, "delivered_locally", "").state == "delivered_locally"


def test_a_reset_cannot_reopen_a_request_this_launch_already_answered(tmp_path: Path) -> None:
    app, _store, state, vault, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    ledger = ApprovalService(state, vault, owner=OWNER_ID)
    answered = choice(1)
    ledger.record(answered, CARD)
    # This row deliberately leaves its request unanswerable until the turn is
    # stopped, and the request itself is still pending.
    ledger.finish(answered.choice_id, "not_delivered", "stale_request")

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["approvals"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    assert response.json()["results"]["approvals"] == {"status": "partial", "count": 0, "kept": 1}
    found = ledger.find_request(DRAFT_KEY, RUNTIME_ID, answered.native.request_id)
    assert found.choice_id == answered.choice_id and found.state == "not_delivered"
    second = dataclasses.replace(answered, choice_id=FINISHED_ID)
    with pytest.raises(ApprovalError) as failure:
        ledger.record(second, CARD)
    assert failure.value.code == "already_decided"


def test_commands_reset_clears_a_dead_launch_s_in_flight_rows(tmp_path: Path) -> None:
    app, _store, state, vault, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    drafts = DraftService(state, vault)
    ours = CommandService(state, vault, owner=OWNER_ID)
    dead = CommandService(state, vault, owner=DEAD_OWNER_ID)
    drafts.save(DRAFT_KEY, 0, "the turn the owner just sent")
    ours.accept(CommandInput(ADMITTED_ID, DRAFT_KEY, GENERATION_ID, 1))
    drafts.save(OTHER_SESSION_ID, 0, "a turn the crashed launch admitted")
    dead.accept(CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1))

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["commands"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    # Before the owner column reached the DELETE this answered {"count": 0, "kept": 2}: the
    # crashed launch's row counted against the quota forever and no reset could remove it.
    assert response.json()["results"]["commands"] == {"status": "partial", "count": 1, "kept": 1}
    assert dead.lookup(CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1)) is None
    assert ours.mark_dispatching(ADMITTED_ID).text == "the turn the owner just sent"


RESET_HANG_GUARD_S = 10.0  # a guard on cross-thread handoffs that take milliseconds


def _reset(client: TestClient, *categories: str):  # the client's own Response type
    return client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": list(categories), "confirmation": "RESET"},
    )


def _finished_command(state: AppState, vault: Path, command_id: str = FINISHED_ID) -> None:
    DraftService(state, vault).save(OTHER_SESSION_ID, 0, "a turn that already ended")
    ledger = CommandService(state, vault, owner=OWNER_ID)
    ledger.accept(CommandInput(command_id, OTHER_SESSION_ID, GENERATION_ID, 1))
    ledger.finish(command_id, "unknown", "internal")


def test_a_committed_commands_deletion_notifies_once_on_the_loop_after_commit(
    tmp_path: Path,
) -> None:
    seen: list[tuple[int, bool]] = []
    finished = CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1)

    def released() -> None:
        gone = CommandService(state, vault, owner=OWNER_ID).lookup(finished) is None
        seen.append((threading.get_ident(), gone))

    app, _store, state, vault, *_ = build(tmp_path, copies_released=released)
    _finished_command(state, vault)
    with TestClient(app, base_url=BASE) as client:
        response = _reset(client, "commands", "drafts")
        assert client.portal is not None
        loop_thread = client.portal.call(threading.get_ident)
    assert response.status_code == 200
    assert response.json()["results"]["commands"] == {"status": "done", "count": 1}
    # Once, on the server's loop thread, with the deletion already committed and visible.
    assert seen == [(loop_thread, True)]


def test_a_partial_commands_deletion_still_notifies(tmp_path: Path) -> None:
    calls: list[str] = []
    app, _store, state, vault, *_ = build(tmp_path, copies_released=lambda: calls.append("x"))
    _finished_command(state, vault)
    DraftService(state, vault).save(DRAFT_KEY, 0, "the turn the owner just sent")
    CommandService(state, vault, owner=OWNER_ID).accept(
        CommandInput(ADMITTED_ID, DRAFT_KEY, GENERATION_ID, 1)
    )
    response = _reset(TestClient(app, base_url=BASE), "commands")
    assert response.json()["results"]["commands"] == {"status": "partial", "count": 1, "kept": 1}
    assert calls == ["x"]


def test_no_notification_without_a_committed_commands_deletion(tmp_path: Path) -> None:
    calls: list[str] = []
    app, _store, state, vault, *_ = build(tmp_path, copies_released=lambda: calls.append("x"))
    client = TestClient(app, base_url=BASE)
    # Nothing to delete, other categories only, then requests refused before any reset.
    assert _reset(client, "commands").json()["results"]["commands"]["count"] == 0
    _finished_command(state, vault)
    assert _reset(client, "drafts", "approvals", "settings", "scratch").status_code == 200
    for body in (
        {"command_id": COMMAND_ID, "categories": ["commands"], "confirmation": "yes"},
        {"command_id": COMMAND_ID, "categories": ["commands", "registrations"]},
        {"command_id": "not-a-uuid", "categories": ["commands"], "confirmation": "RESET"},
    ):
        assert client.post("/api/v2/data/reset", headers=MUTATION, json=body).status_code == 400
    assert calls == []


def test_a_refused_commands_deletion_does_not_notify(tmp_path: Path) -> None:
    calls: list[str] = []
    app, _store, state, vault, *_ = build(
        tmp_path, owner="", copies_released=lambda: calls.append("x")
    )
    _finished_command(state, vault)
    assert _reset(TestClient(app, base_url=BASE), "commands").status_code == 400
    assert calls == []


def test_a_later_category_failure_still_notifies_the_committed_deletion(tmp_path: Path) -> None:
    calls: list[str] = []
    app, _store, state, vault, *_ = build(
        tmp_path,
        settings_raises=RuntimeError("synthetic later failure"),
        copies_released=lambda: calls.append("x"),
    )
    _finished_command(state, vault)
    response = _reset(TestClient(app, base_url=BASE), "commands", "settings")
    assert response.status_code == 503
    assert calls == ["x"]
    lookup = CommandService(state, vault, owner=OWNER_ID).lookup
    assert lookup(CommandInput(FINISHED_ID, OTHER_SESSION_ID, GENERATION_ID, 1)) is None


async def test_a_reset_whose_caller_left_still_notifies_once_the_worker_finishes(
    tmp_path: Path,
) -> None:
    entered, gate = threading.Event(), threading.Event()
    calls: list[str] = []
    workers: set[asyncio.Task[object]] = set()

    def hold() -> None:
        entered.set()
        gate.wait(RESET_HANG_GUARD_S)

    app, _store, state, vault, *_ = build(
        tmp_path, copies_released=lambda: calls.append("x"), on_settings=hold, workers=workers
    )
    _finished_command(state, vault)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        request = asyncio.create_task(
            client.post(
                "/api/v2/data/reset",
                headers=MUTATION,
                json={
                    "command_id": COMMAND_ID,
                    "categories": ["commands", "settings"],
                    "confirmation": "RESET",
                },
            )
        )
        assert await asyncio.to_thread(entered.wait, RESET_HANG_GUARD_S)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        # The commands deletion committed; the worker is still in its later category.
        assert calls == [] and len(workers) == 1
        gate.set()
        await asyncio.wait_for(asyncio.gather(*workers), RESET_HANG_GUARD_S)
    assert calls == ["x"]


def test_reset_service_refuses_a_malformed_owner(tmp_path: Path) -> None:
    app, *_ = build(tmp_path, owner="")
    client = TestClient(app, base_url=BASE)
    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["commands"], "confirmation": "RESET"},
    )
    # An owner that matches no row would widen the DELETE back over every in-flight row and
    # report them as deleted; a typed refusal is the only safe answer.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_reset_refuses_unknown_and_registrations_categories(tmp_path: Path) -> None:
    app, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["registrations"], "confirmation": "RESET"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_categories"

    wrong_confirmation = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["drafts"], "confirmation": "yes"},
    )
    assert wrong_confirmation.status_code == 400
    assert wrong_confirmation.json()["error"]["code"] == "invalid_confirmation"


def test_settings_read_only_reset_is_refused_not_done(tmp_path: Path) -> None:
    app, *_ = build(tmp_path, settings_raises=SettingsError("read_only"))
    client = TestClient(app, base_url=BASE)
    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["settings"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    assert response.json()["results"]["settings"] == {
        "status": "refused",
        "reason": "read_only",
        "count": 0,
    }


def test_export_preview_then_write_then_refuses_replay_to_a_denied_root(tmp_path: Path) -> None:
    app, *_, claude_home, data_dir = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    preview = client.post(
        "/api/v2/exports/preview", headers=MUTATION, json={"kind": "settings"}
    ).json()
    assert preview["kind"] == "settings"
    assert preview["files"][0]["name"] == "settings.json"

    denied = client.post(
        "/api/v2/exports",
        headers=MUTATION,
        json={
            "command_id": COMMAND_ID,
            "preview_id": preview["preview_id"],
            "destination": str(claude_home),
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "denied_destination"

    good_dir = tmp_path / "downloads"
    good_dir.mkdir()
    written = client.post(
        "/api/v2/exports",
        headers=MUTATION,
        json={
            "command_id": COMMAND_ID,
            "preview_id": preview["preview_id"],
            "destination": str(good_dir),
        },
    )
    assert written.status_code == 201
    body = written.json()
    assert Path(body["path"]).read_bytes()
    assert len(body["sha256"]) == 64
    assert body["replayed"] is False

    # The same command again is the same result (200, replayed), never `exists` for the file
    # this command itself wrote.
    again = client.post(
        "/api/v2/exports",
        headers=MUTATION,
        json={
            "command_id": COMMAND_ID,
            "preview_id": preview["preview_id"],
            "destination": str(good_dir),
        },
    )
    assert again.status_code == 200
    assert again.json() == {**body, "replayed": True}


def test_import_preview_rejects_malformed_body(tmp_path: Path) -> None:
    app, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    not_an_export = client.post(
        "/api/v2/imports/preview",
        headers=MUTATION,
        json={"kind": "settings", "content": json.dumps({"not": "an export"})},
    )
    assert not_an_export.status_code == 400
    assert not_an_export.json()["error"]["code"] == "invalid_format"

    bad_type = client.post(
        "/api/v2/imports/preview", headers=MUTATION, json={"kind": "settings", "content": 12345}
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["error"]["code"] == "invalid_body"

    newer_format = client.post(
        "/api/v2/imports/preview",
        headers=MUTATION,
        json={
            "kind": "settings",
            "content": json.dumps(
                {
                    "viki_export": {
                        "kind": "settings",
                        "format_version": 2,
                        "exported_at": "x",
                        "app_version": "0.1.0",
                        "launch_id": "l",
                    },
                    "settings": {"version": 1, "values": {}},
                    "workspaces": [],
                }
            ),
        },
    )
    assert newer_format.status_code == 200
    assert newer_format.json()["supported"] is False
    assert newer_format.json()["unsupported_reason"] == "newer_format"


def test_measure_reports_partial_when_entry_or_time_budget_is_exceeded(tmp_path: Path) -> None:
    busy = tmp_path / "busy"
    busy.mkdir()
    for i in range(10):
        (busy / f"f{i}.txt").write_text("x", encoding="utf-8")

    _, over_entry_budget = _measure(busy, max_entries=5)
    assert over_entry_budget is True

    total, within_budget = _measure(busy)
    assert within_budget is False
    assert total == 10  # each file is exactly 1 byte, nothing skipped when the budget is ample

    _, over_time_budget = _measure(busy, timeout_s=0.0)
    assert over_time_budget is True


def test_clear_scratch_empties_files_but_never_follows_a_junction(tmp_path: Path) -> None:
    app, _store, _state, vault, _claude_home, data_dir = build(tmp_path, scratch_in_use=False)
    client = TestClient(app, base_url=BASE)
    scratch_root = data_dir / "scratch" / "chat"  # build() already seeded "leftover.txt" here
    (scratch_root / "another.txt").write_text("y", encoding="utf-8")
    subdir = scratch_root / "subdir"
    subdir.mkdir()
    (subdir / "inner.txt").write_text("z", encoding="utf-8")
    link = scratch_root / "link_to_vault"
    make_junction(link, vault)
    before_vault = {p.name: p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    response = client.post(
        "/api/v2/data/reset",
        headers=MUTATION,
        json={"command_id": COMMAND_ID, "categories": ["scratch"], "confirmation": "RESET"},
    )
    assert response.status_code == 200
    result = response.json()["results"]["scratch"]
    # leftover.txt, another.txt and subdir (one rmtree'd entry) are removed; the junction is not.
    assert result == {"status": "partial", "count": 3}
    assert list(scratch_root.iterdir()) == [link]  # only the un-followed junction survives
    after_vault = {p.name: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert after_vault == before_vault  # the junction's target was never touched


def test_deeply_nested_body_field_is_invalid_body_not_a_crash(tmp_path: Path) -> None:
    # Built by string repetition, not recursive construction, so the *test* never risks its
    # own RecursionError while assembling the fixture -- only the server's json.loads does.
    depth = 20_000
    nested = "[" * depth + "]" * depth
    body = (
        f'{{"command_id":"{COMMAND_ID}","categories":{nested},"confirmation":"RESET"}}'
    ).encode()
    app, *_ = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    response = client.post(
        "/api/v2/data/reset",
        headers={**MUTATION, "content-type": "application/json"},
        content=body,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"


def import_apply(client: TestClient, content_values: dict, **body_overrides: object):
    preview = client.post(
        "/api/v2/imports/preview",
        headers=MUTATION,
        json={
            "kind": "settings",
            "content": json.dumps(
                {
                    "viki_export": {"kind": "settings", "format_version": 1},
                    "settings": {"version": 1, "values": content_values},
                    "workspaces": [],
                }
            ),
        },
    ).json()
    body = {
        "command_id": COMMAND_ID,
        "expected_revision": 0,
        "keys": [c["key"] for c in preview["changes"] if c["action"] == "apply"],
        **body_overrides,
    }
    return client.post(
        f"/api/v2/imports/{preview['preview_id']}/apply", headers=MUTATION, json=body
    )


def test_import_apply_answers_in_the_settings_view_shape(tmp_path: Path) -> None:
    def patch(command_id: str, expected_revision: int, changes: Mapping[str, object]):
        return effective(expected_revision + 1, dict(changes)), False

    app, *_ = build(tmp_path, patch=patch)
    client = TestClient(app, base_url=BASE)
    response = import_apply(client, {"appearance.theme": "dark"})
    assert response.status_code == 200
    body = response.json()
    # The GET/PATCH /settings shape: flattened document, trimmed key specs, no `document`.
    assert body["revision"] == 1
    assert body["values"] == {"appearance.theme": "dark"}
    assert body["keys"]["appearance.theme"]["kind"] == "enum"
    assert "max_len" not in body["keys"]["identity.display_name"]
    assert "document" not in body
    assert body["command_id"] == COMMAND_ID
    assert body["replayed"] is False


def test_import_preview_refuses_rebase_and_allows_a_fresh_selection(tmp_path: Path) -> None:
    settings = SettingsService(
        SessionStore(tmp_path / "settings.sqlite3", lambda: {}),
        provider_defaults=lambda: Prefs(),
        on_chat_defaults=lambda _prefs: None,
    )
    app, *_ = build(tmp_path, patch=settings.patch, current_settings=settings.import_snapshot)
    client = TestClient(app, base_url=BASE)
    content = json.dumps(
        {
            "viki_export": {"kind": "settings", "format_version": 1},
            "settings": {"values": {"appearance.theme": "dark", "privacy.hide_previews": True}},
        }
    )

    def preview():
        response = client.post(
            "/api/v2/imports/preview",
            headers=MUTATION,
            json={"kind": "settings", "content": content},
        )
        assert response.status_code == 200
        return response.json()

    old = preview()
    assert old["settings_revision"] == 0
    settings.patch(COMMAND_ID, 0, {"appearance.theme": "light"})
    request = {
        "command_id": ADMITTED_ID,
        "expected_revision": 1,
        "keys": ["appearance.theme"],
    }
    refused = client.post(
        f"/api/v2/imports/{old['preview_id']}/apply", headers=MUTATION, json=request
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "preview_expired"
    assert settings.import_snapshot().values == {"appearance.theme": "light"}

    fresh = preview()
    assert fresh["settings_revision"] == 1
    assert {row["key"]: row["action"] for row in fresh["changes"]} == {
        "appearance.theme": "conflict",
        "privacy.hide_previews": "apply",
    }
    applied = client.post(
        f"/api/v2/imports/{fresh['preview_id']}/apply",
        headers=MUTATION,
        json={**request, "keys": ["privacy.hide_previews"]},
    )
    assert applied.status_code == 200
    assert applied.json()["revision"] == 2
    assert applied.json()["values"] == {"appearance.theme": "light", "privacy.hide_previews": True}


def test_import_apply_conflict_and_invalid_settings_carry_the_patch_details(
    tmp_path: Path,
) -> None:
    def stale(command_id: str, expected_revision: int, changes: Mapping[str, object]):
        raise SettingsConflict(7, effective(7, {"appearance.theme": "light"}))

    app, *_ = build(tmp_path, patch=stale)
    client = TestClient(app, base_url=BASE)
    response = import_apply(client, {"appearance.theme": "dark"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "stale_revision"
    assert error["current_revision"] == 7
    assert error["current"]["revision"] == 7
    assert error["current"]["values"] == {"appearance.theme": "light"}

    def invalid(command_id: str, expected_revision: int, changes: Mapping[str, object]):
        raise SettingsError("invalid_settings", fields={"command_id": "invalid_type"})

    (tmp_path / "second").mkdir()
    app, *_ = build(tmp_path / "second", patch=invalid)
    client = TestClient(app, base_url=BASE)
    response = import_apply(client, {"appearance.theme": "dark"})
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"command_id": "invalid_type"}


def test_an_untyped_exception_is_a_logged_503_even_when_it_carries_a_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Impostor(RuntimeError):
        code = "stale_revision"  # duck-typing must not buy a typed refusal

    def broken(command_id: str, expected_revision: int, changes: Mapping[str, object]):
        raise Impostor("secret detail that must not reach the browser")

    app, *_ = build(tmp_path, patch=broken)
    client = TestClient(app, base_url=BASE)
    with caplog.at_level("WARNING", logger="viki_agent.app.data_api"):
        response = import_apply(client, {"appearance.theme": "dark"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert "secret detail" not in response.text
    assert any("Impostor" in record.getMessage() for record in caplog.records)
    assert not any("secret detail" in record.getMessage() for record in caplog.records)


def test_locations_count_the_migration_backups_beside_the_database(tmp_path: Path) -> None:
    app, *_, data_dir = build(tmp_path)
    backup = data_dir / "app.sqlite3.v4-backup-11111111-1111-4111-8111-111111111111.sqlite3"
    backup.write_bytes(b"x" * 10)
    (data_dir / "app.sqlite3.v4-backup-unrelated.txt").write_bytes(b"not a backup")
    client = TestClient(app, base_url=BASE)
    items = next(
        c
        for c in client.get("/api/v2/data/locations", headers=AUTH).json()["categories"]
        if c["id"] == "app_data"
    )["items"]
    backups = next(item for item in items if item["kind"] == "backups")
    assert backups["count"] == 1
    assert backups["bytes"] == 10
    assert backups["path"] == str(data_dir)


def test_locations_scratch_path_follows_the_workspaces_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspaces, "SCRATCH_CHAT", ("scratch", "elsewhere"))
    app, *_, data_dir = build(tmp_path)
    client = TestClient(app, base_url=BASE)
    items = next(
        c
        for c in client.get("/api/v2/data/locations", headers=AUTH).json()["categories"]
        if c["id"] == "app_data"
    )["items"]
    scratch = next(item for item in items if item["kind"] == "scratch")
    assert scratch["path"] == str(data_dir / "scratch" / "elsewhere")


def test_body_intake_timeout_is_408(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # TestClient hands the whole body over without ever yielding, so the deadline can only be
    # exercised by driving the ASGI app with a receive() that never delivers it.
    monkeypatch.setattr(data_api, "BODY_TIMEOUT_S", 0.01)
    app, *_ = build(tmp_path)
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v2/exports/preview",
        "raw_path": b"/api/v2/exports/preview",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"content-type", b"application/json"),
            (b"content-length", b"20"),
            *((k.encode(), v.encode()) for k, v in MUTATION.items()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 408
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(body)["error"]["code"] == "intake_timeout"


def test_locations_share_one_time_budget_over_every_measured_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 s per item bounded nothing: registrations accumulate, no route forgets them, and a
    dozen of them would outlast the browser's fixed 30 s timeout while the abandoned walk kept
    one of this route's four permits to the end. The items of one document share one budget, so
    the answer stays bounded however many vaults are registered. Each item gets an equal share
    capped at the per-item limit."""
    budgets: list[float] = []

    def record(
        path: Path, *, max_entries: int = data_api.MEASURE_MAX_ENTRIES, timeout_s: float = 0.0
    ) -> tuple[int, bool]:
        budgets.append(timeout_s)
        return 0, False

    monkeypatch.setattr(data_api, "_measure", record)
    vaults = []
    for index in range(11):
        root = tmp_path / f"vault-{index}"
        root.mkdir()
        vaults.append(workspace(root, workspace_id=f"vault-{index}"))
    locations = LocationsService(
        workspaces=lambda: vaults,
        data_dir=tmp_path / "data",
        claude_home=tmp_path / "home",
        scratch_in_use=lambda: False,
    )

    asyncio.run(locations.document())

    # Every vault, plus logs, the Edge profile, scratch, provider home and app assets.
    shared = data_api.MEASURE_TOTAL_TIMEOUT_S / (len(vaults) + 5)
    assert budgets == [shared] * (len(vaults) + 5)
    assert sum(budgets) <= data_api.MEASURE_TOTAL_TIMEOUT_S

    budgets.clear()
    del vaults[1:]
    asyncio.run(locations.document())
    assert budgets == [min(data_api.MEASURE_TIMEOUT_S, data_api.MEASURE_TOTAL_TIMEOUT_S / 6)] * 6
