"""Durable REST sends over synthetic app storage and an in-process driver; no server port."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from starlette.types import Message, Scope

from tests.conversations.test_session import GatedDriver
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.conversations import commands as commands_module
from viki_agent.conversations.command_runtime import CommandRunner
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService
from viki_agent.conversations.turn_parts import ExcerptPart, parse_excerpt_part
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.server import SECURITY_HEADERS, create_app
from viki_agent.session import CommandDispatchRejected, SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError
from viki_agent.turn_input import Attachment, TurnInput

BASE = "http://127.0.0.1:8765"
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ASSET = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
AUTH = {"authorization": "Bearer command-test-token"}
WRITE = {**AUTH, "origin": BASE}
PATH = "/api/v2/commands"


class Driver(GatedDriver):
    def __init__(self) -> None:
        super().__init__()
        self.release.set()
        self.sent = asyncio.Event()
        self.send_gate = asyncio.Event()
        self.send_gate.set()

    async def send(self, text: str) -> None:
        await super().send(text)
        self.sent.set()
        await self.send_gate.wait()


def assemble(tmp_path: Path, *, dev: bool = False):
    data, vault, home = tmp_path / "data", tmp_path / "vault", tmp_path / "home"
    vault.mkdir(exist_ok=True)
    state = AppState(data / "state.json", Prefs())
    drivers: list[Driver] = []

    def make_driver(*_args):
        driver = Driver()
        drivers.append(driver)
        return driver

    manager = SessionManager(
        config=DriverConfig(cli=("must-not-run",), vault=vault, env={}),
        state=state,
        claude_home=home,
        make_driver=make_driver,
    )
    app = create_app(
        config=Config(vault=vault, claude_home=home, cli=Path("absent"), edge=None, data_dir=data),
        token="command-test-token",
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
        dev=dev,
    )
    return app, state, manager, drivers, DraftService(state, vault)


@pytest.fixture
async def rig(tmp_path: Path):
    app, state, manager, drivers, drafts = assemble(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        yield app, client, state, manager, drivers, drafts


def prepare(manager: SessionManager, drafts: DraftService, text: str = "submitted café"):
    actor = manager.open(SID)
    draft = drafts.save(SID, 0, text)
    value = CommandInput(CID, SID, actor.generation, draft.revision)
    return actor, asdict(value)


async def settle(runner: CommandRunner) -> None:
    if runner._jobs:
        await asyncio.gather(*runner._jobs)


async def test_committed_receipt_exact_copy_and_duplicate_after_actor_replaced(rig) -> None:
    app, client, state, manager, drivers, drafts = rig
    actor, value = prepare(manager, drafts)
    target = await client.get(f"/api/v2/sessions/{SID}/target", headers=AUTH)
    assert target.json() == actor.command_context()
    assert not drivers  # target and draft work do not start a provider
    result = await client.post(PATH, headers=WRITE, json=value)
    assert result.status_code == 202 and result.json()["state"] == "accepted"
    assert "text" not in result.json()
    await settle(app.state.commands)
    assert drivers[0].calls.count("send submitted café") == 1
    receipt = await client.get(PATH + "/" + CID, headers=AUTH)
    assert receipt.json()["state"] == "unknown"
    assert receipt.json()["reason"] == "native_unconfirmed"
    assert "text" not in receipt.json()
    drafts.save(SID, 1, "newer editor text")
    submitted = await client.get(PATH + "/" + CID + "/submission", headers=AUTH)
    assert submitted.json()["text"] == "submitted café"
    assert drafts.get(SID).text == "newer editor text"
    await manager.close(SID)
    replacement = manager.open(SID)
    assert replacement.generation != value["generation"]
    again = await client.post(PATH, headers=WRITE, json=value)
    assert again.status_code == 200 and again.json() == receipt.json()
    assert len(drivers) == 1
    different = await client.post(PATH, headers=WRITE, json={**value, "draft_revision": 2})
    assert different.status_code == 409
    assert len(drivers) == 1 and b"submitted" not in state.store.path.read_bytes()
    listing = await client.get(PATH, headers=AUTH)
    assert listing.json()["commands"] == [receipt.json()]


async def test_format_3_receipt_keeps_the_summary_key_set(rig) -> None:
    app, client, _, manager, drivers, drafts = rig
    actor = manager.open(SID)
    part = ExcerptPart(str(uuid.uuid4()), "notes/area/note.md", 10, 20, "a" * 64)
    draft = drafts.save(SID, 0, TurnInput("submitted café", (), (part,)))
    value = asdict(CommandInput(CID, SID, actor.generation, draft.revision))
    result = await client.post(PATH, headers=WRITE, json=value)
    assert result.status_code == 202
    keys = set(result.json())
    await settle(app.state.commands)
    receipt = await client.get(PATH + "/" + CID, headers=AUTH)
    assert set(receipt.json()) == keys
    assert not keys & {"text", "attachments", "parts"}
    submitted = await client.get(PATH + "/" + CID + "/submission", headers=AUTH)
    assert submitted.json()["parts"][0]["ref"] == "notes/area/note.md"


async def test_submission_parts_round_trip_through_parse_excerpt_part(rig) -> None:
    app, client, _, manager, drivers, drafts = rig
    actor = manager.open(SID)
    part = ExcerptPart(str(uuid.uuid4()), "notes/area/note.md", 10, 20, "a" * 64)
    draft = drafts.save(SID, 0, TurnInput("submitted café", (), (part,)))
    value = asdict(CommandInput(CID, SID, actor.generation, draft.revision))
    assert (await client.post(PATH, headers=WRITE, json=value)).status_code == 202
    await settle(app.state.commands)
    submitted = await client.get(PATH + "/" + CID + "/submission", headers=AUTH)
    wire = submitted.json()["parts"]
    assert [parse_excerpt_part(item) for item in wire] == [part]
    assert wire[0]["kind"] == "vault_excerpt"


async def test_unavailable_attachment_answers_409_and_builds_no_driver(rig) -> None:
    app, client, _, manager, drivers, drafts = rig
    attachment = Attachment(ASSET, "image/png", 4, 1, 1, "synthetic.png")
    drafts._store.put_asset(
        {
            "scope": drafts._scope,
            "asset_id": ASSET,
            "media_type": "image/png",
            "byte_size": 4,
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-09-19T00:00:00.000Z",
        }
    )
    actor = manager.open(SID)
    draft = drafts.save(SID, 0, TurnInput("submitted café", (attachment,)))
    with contextlib.closing(sqlite3.connect(drafts._store.path)) as connection:
        connection.execute("DELETE FROM assets WHERE asset_id=?", (ASSET,))
        connection.commit()
    value = asdict(CommandInput(CID, SID, actor.generation, draft.revision))
    result = await client.post(PATH, headers=WRITE, json=value)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "attachment_unavailable"
    assert app.state.commands.service.list().commands == []
    assert not drivers


async def test_unknown_target_stale_generation_and_turn_running_never_accept(rig) -> None:
    app, client, _, manager, drivers, drafts = rig
    assert (await client.get(f"/api/v2/sessions/{SID}/target", headers=AUTH)).status_code == 404
    actor, value = prepare(manager, drafts)
    result = await client.post(PATH, headers=WRITE, json={**value, "generation": str(uuid.uuid4())})
    assert result.status_code == 409 and result.json()["error"]["code"] == "stale_generation"
    await actor.start()
    drivers[0].busy = True
    result = await client.post(PATH, headers=WRITE, json=value)
    assert result.status_code == 409 and result.json()["error"]["code"] == "turn_running"
    assert app.state.commands.service.list().commands == []
    drivers[0].busy = False


async def test_acceptance_precedes_native_call_and_pending_retry_never_sends_twice(rig) -> None:
    app, client, _, manager, drivers, drafts = rig
    actor, value = prepare(manager, drafts)
    await actor.start()
    drivers[0].send_gate.clear()
    response = await client.post(PATH, headers=WRITE, json=value)
    assert response.status_code == 202
    await drivers[0].sent.wait()
    assert app.state.commands.service.get(CID).state == "dispatching"
    duplicate = await client.post(PATH, headers=WRITE, json=value)
    assert duplicate.status_code == 200 and duplicate.json()["state"] == "dispatching"
    assert drivers[0].calls.count("send submitted café") == 1
    drivers[0].send_gate.set()


async def test_shutdown_after_intent_preserves_unknown_and_reopen_never_dispatches(
    tmp_path,
) -> None:
    app, _, manager, drivers, drafts = assemble(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        actor, value = prepare(manager, drafts)
        await actor.start()
        drivers[0].send_gate.clear()
        assert (await client.post(PATH, headers=WRITE, json=value)).status_code == 202
        await drivers[0].sent.wait()
    assert drivers[0].closing.is_set()
    reopened, _, second_manager, second_drivers, _ = assemble(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url=BASE
    ) as client:
        receipt = await client.get(PATH + "/" + CID, headers=AUTH)
        assert receipt.json()["state"] == "unknown"
        assert receipt.json()["reason"] == "cancelled"
        retry = await client.post(PATH, headers=WRITE, json=value)
        assert retry.status_code == 200
        assert not second_drivers and second_manager.get(SID) is None
    await reopened.state.commands.close()


@pytest.mark.parametrize(
    "headers,status", [({}, 401), (AUTH, 403), ({**AUTH, "origin": "https://foreign.invalid"}, 403)]
)
async def test_write_auth_origin_before_body(rig, headers, status) -> None:
    app, *_ = rig
    code, body, consumed = await raw_post(app, headers=headers, chunks=[])
    assert code == status and consumed == 0 and b"command-test-token" not in body


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", PATH),
        ("HEAD", PATH),
        ("GET", PATH + "/" + CID),
        ("HEAD", f"/api/v2/sessions/{SID}/target"),
    ],
)
async def test_reads_require_auth_and_reject_foreign_origin(rig, method, path) -> None:
    _, client, *_ = rig
    assert (await client.request(method, path)).status_code == 401
    assert (
        await client.request(method, path, headers={**AUTH, "origin": "https://foreign.invalid"})
    ).status_code == 403


async def test_head_with_submission_body_never_accepts_and_headers_hold(rig) -> None:
    app, client, _, manager, _, drafts = rig
    _, value = prepare(manager, drafts)
    response = await client.request("HEAD", PATH, headers=AUTH, json=value)
    assert response.status_code == 200 and response.content == b""
    assert app.state.commands.service.list().commands == []
    assert response.headers["cache-control"] == "no-store"
    for key, expected in SECURITY_HEADERS.items():
        assert response.headers[key] == expected
    assert "access-control-allow-origin" not in response.headers
    assert (await client.get(PATH, headers={**AUTH, "host": "foreign.invalid"})).status_code == 400


@pytest.mark.parametrize(
    "body", [b"null", b"\xff", b"[]", b'{"command_id":"a","command_id":"b"}', b"{" + b" " * 2048]
)
async def test_invalid_and_oversized_body_cannot_create_commands(rig, body) -> None:
    app, client, *_ = rig
    result = await client.post(
        PATH, headers={**WRITE, "content-type": "application/json"}, content=body
    )
    assert result.status_code in {400, 413}
    assert app.state.commands.service.list().commands == []


@pytest.mark.parametrize(
    "changes",
    [
        {"draft_revision": True},
        {"draft_revision": 0},
        {"draft_revision": 1 << 53},
        {"session_id": SID.upper()},
        {"text": "unreviewed"},
        {"generation": None},
    ],
)
async def test_exact_input_contract(rig, changes) -> None:
    _, client, _, manager, _, drafts = rig
    _, value = prepare(manager, drafts)
    assert (await client.post(PATH, headers=WRITE, json={**value, **changes})).status_code == 400


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=101",
        "?limit=1&limit=2",
        "?scope=other",
        "?after=bad",
        "?limit=" + "9" * 300,
    ],
)
async def test_bounded_query(rig, query) -> None:
    _, client, *_ = rig
    assert (await client.get(PATH + query, headers=AUTH)).status_code == 400


async def raw_post(app, *, headers: dict[str, str], chunks: list[bytes], slow: bool = False):
    calls = 0
    output: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [
            (k.encode(), v.encode()) for k, v in {"host": "127.0.0.1:8765", **headers}.items()
        ],
    }

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if slow:
            await anyio.sleep_forever()
        assert chunks, "body consumed before admission"
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        output.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in output), calls


async def test_actual_body_limit_and_deadline(rig, monkeypatch) -> None:
    from viki_agent.conversations import command_api

    app, *_ = rig
    headers = {**WRITE, "content-type": "application/json", "content-length": "1"}
    assert (await raw_post(app, headers=headers, chunks=[b" " * 2049]))[0] == 413
    monkeypatch.setattr(command_api, "COMMAND_BODY_TIMEOUT_S", 0.001)
    assert (await raw_post(app, headers=headers, chunks=[], slow=True))[0] == 408


async def test_cancelled_http_acceptance_keeps_worker_capacity_and_finishes(
    rig, monkeypatch
) -> None:
    from viki_agent.conversations import command_runtime

    app, client, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = CommandService.accept

    def blocked(self, submitted):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return original(self, submitted)

    monkeypatch.setattr(CommandService, "accept", blocked)
    monkeypatch.setattr(command_runtime, "MAX_COMMAND_RUNS", 1)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=value))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    try:
        pressure = await client.post(
            PATH, headers=WRITE, json={**value, "command_id": str(uuid.uuid4())}
        )
        assert pressure.status_code == 429
        assert (await client.get(f"/api/v2/sessions/{SID}/target", headers=AUTH)).status_code == 200
    finally:
        release.set()
        await settle(app.state.commands)
    assert drivers[0].calls.count("send submitted café") == 1
    assert (await client.get(PATH + "/" + CID, headers=AUTH)).json()["state"] == "unknown"


@pytest.mark.parametrize(
    "stage,expected", [("accept", "not_dispatched"), ("mark_dispatching", "unknown")]
)
async def test_shutdown_observes_inflight_commit_before_finalizing(
    rig, monkeypatch, stage, expected
) -> None:
    app, client, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = getattr(CommandService, stage)

    def blocked(self, argument):
        result = original(self, argument)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    monkeypatch.setattr(CommandService, stage, blocked)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=value))
    await entered.wait()
    shutdown = asyncio.create_task(app.state.commands.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await shutdown
    assert (await caller).status_code == 202
    receipt = app.state.commands.service.get(CID)
    assert receipt.state == expected and receipt.reason == "cancelled"
    assert not any("send " in call for driver in drivers for call in driver.calls)


async def test_status_write_failure_projects_inactive_record_as_unknown(rig, monkeypatch) -> None:
    app, client, _, manager, _, drafts = rig
    _, value = prepare(manager, drafts)

    def failed(*_args):
        raise OSError("PRIVATE exception must never reach HTTP or logs")

    monkeypatch.setattr(app.state.commands.service, "finish", failed)
    assert (await client.post(PATH, headers=WRITE, json=value)).status_code == 202
    await settle(app.state.commands)
    response = await client.get(PATH + "/" + CID, headers=AUTH)
    assert response.json()["state"] == "unknown" and "PRIVATE" not in response.text
    retry = await client.post(PATH, headers=WRITE, json=value)
    assert retry.json()["state"] == "unknown"


async def test_replacement_during_acceptance_never_dispatches_old_actor(rig, monkeypatch) -> None:
    app, client, _, manager, drivers, drafts = rig
    old, value = prepare(manager, drafts)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = CommandService.accept

    def blocked(self, argument):
        result = original(self, argument)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    monkeypatch.setattr(CommandService, "accept", blocked)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=value))
    await entered.wait()
    try:
        replacement = manager.create(SID)
        assert replacement is not old
    finally:
        release.set()
    assert (await caller).status_code == 202
    await settle(app.state.commands)
    assert app.state.commands.service.get(CID).state == "not_dispatched"
    assert not drivers


@pytest.mark.parametrize("dev", [False, True])
async def test_development_origin_requires_development_assembly(tmp_path, dev) -> None:
    app, _, manager, _, drafts = assemble(tmp_path, dev=dev)
    _, value = prepare(manager, drafts)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        response = await client.post(
            PATH, headers={**AUTH, "origin": "http://127.0.0.1:5173"}, json=value
        )
        assert response.status_code == (202 if dev else 403)


@pytest.mark.parametrize(
    "key,value",
    [("content-type", "text/plain"), ("content-encoding", "gzip"), ("content-length", "-1")],
)
async def test_representation_validation_precedes_intake(rig, key, value) -> None:
    app, *_ = rig
    headers = {**WRITE, "content-type": "application/json", key: value}
    status, _, consumed = await raw_post(app, headers=headers, chunks=[])
    assert status == (400 if key == "content-length" else 415)
    assert consumed == 0


async def test_simultaneous_identical_posts_recover_receipt_after_busy_check(
    rig, monkeypatch
) -> None:
    app, client, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    barrier = threading.Barrier(2)
    original_lookup = CommandService.lookup
    original_send = Driver.send

    def both_miss(self, submitted):
        result = original_lookup(self, submitted)
        if result is None:
            barrier.wait()
        return result

    async def native_busy(self, text):
        self.busy = True
        await original_send(self, text)

    monkeypatch.setattr(CommandService, "lookup", both_miss)
    monkeypatch.setattr(Driver, "send", native_busy)
    first, second = await asyncio.gather(
        client.post(PATH, headers=WRITE, json=value), client.post(PATH, headers=WRITE, json=value)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 202]
    assert first.json()["command_id"] == second.json()["command_id"] == CID
    await settle(app.state.commands)
    assert drivers[0].calls.count("send submitted café") == 1


async def test_shutdown_before_attempt_first_step_settles_waiting_receipt(rig) -> None:
    from viki_agent.session import CommandDispatchRejected

    app, _, _, manager, _, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    caller = asyncio.create_task(runner.submit(CommandInput(**value)))
    # submit runs first, enqueues its attempt, then yields to this close before the new
    # coroutine's first step. A cancelled-before-start coroutine does not run its finally.
    await asyncio.sleep(0)
    await runner.close()
    try:
        with pytest.raises(CommandDispatchRejected):
            await asyncio.wait_for(caller, 0.05)
    finally:
        caller.cancel()


async def test_submit_refused_target_never_accepts_or_finishes(rig) -> None:
    from viki_agent.session import CommandDispatchRejected

    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner, submitted = app.state.commands, CommandInput(**value)
    finished: list[str] = []
    with pytest.raises(CommandDispatchRejected) as refused:
        await runner.submit(submitted, admitted=lambda: False, finished=finished.append)
    await settle(runner)
    assert refused.value.code == "no_target"
    assert runner.service.lookup(submitted) is None
    assert not any("send " in call for driver in drivers for call in driver.calls)
    assert finished == []


async def test_submit_reports_the_accepted_attempts_final_status_once(rig) -> None:
    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    finished: list[str] = []
    record, created = await runner.submit(
        CommandInput(**value), admitted=lambda: True, finished=finished.append
    )
    assert created and record.state == "accepted"
    await settle(runner)
    assert finished == ["unknown"]
    assert runner.service.get(CID).state == "unknown"
    assert drivers[0].calls.count("send submitted café") == 1


async def test_stop_exact_command_joins_held_send_without_closing_admission(rig) -> None:
    app, _, _, manager, drivers, drafts = rig
    actor, value = prepare(manager, drafts)
    await actor.start()
    runner = app.state.commands
    submitted = CommandInput(**value)
    finished: list[str] = []
    driver = drivers[0]
    driver.send_gate.clear()
    driver.close_gate.clear()
    stop: asyncio.Task[None] | None = None
    try:
        record, created = await runner.submit(submitted, finished=finished.append)
        assert created and record.state == "accepted"
        await driver.sent.wait()
        wrong = CommandInput(CID, SID, submitted.generation, submitted.draft_revision + 1)
        await runner.stop(wrong)
        assert not driver.closing.is_set() and finished == []

        stop = asyncio.create_task(runner.stop(submitted))
        await driver.closing.wait()
        assert not stop.done() and finished == []
    finally:
        driver.close_gate.set()
        driver.send_gate.set()
        if stop is not None:
            await stop

    assert finished == ["unknown"]
    assert driver.calls.count("send submitted café") == 1
    assert runner.service.get(CID).state == "unknown"
    await runner.stop(submitted)

    next_sid = str(uuid.uuid4())
    next_actor = manager.open(next_sid)
    next_draft = drafts.save(next_sid, 0, "another command")
    _, created = await runner.submit(
        CommandInput(str(uuid.uuid4()), next_sid, next_actor.generation, next_draft.revision)
    )
    assert created
    await settle(runner)
    assert sum(driver.calls.count("send another command") for driver in drivers) == 1


async def test_stop_before_acceptance_has_no_early_finished_callback(rig, monkeypatch) -> None:
    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    submitted = CommandInput(**value)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = CommandService.accept
    finished: list[str] = []

    def blocked(self, argument):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return original(self, argument)

    monkeypatch.setattr(CommandService, "accept", blocked)
    caller = asyncio.create_task(runner.submit(submitted, finished=finished.append))
    stop: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        stop = asyncio.create_task(runner.stop(submitted))
        await asyncio.sleep(0)
        assert not stop.done() and finished == []
    finally:
        release.set()
        if stop is not None:
            await stop

    _, created = await caller
    assert created and finished == ["not_dispatched"]
    assert not any("send " in call for driver in drivers for call in driver.calls)


async def test_stop_drains_two_exact_duplicate_jobs_before_acceptance(rig, monkeypatch) -> None:
    from viki_agent.conversations import command_runtime

    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner, submitted = app.state.commands, CommandInput(**value)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    lock = threading.Lock()
    lookups = 0
    original = CommandService.lookup

    def held_lookup(self, candidate):
        nonlocal lookups
        result = original(self, candidate)
        with lock:
            lookups += 1
            if lookups == 2:
                loop.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    monkeypatch.setattr(CommandService, "lookup", held_lookup)
    monkeypatch.setattr(command_runtime, "MAX_COMMAND_RUNS", 2)
    finished: list[str] = []
    callers = [
        asyncio.create_task(runner.submit(submitted, finished=finished.append)) for _ in range(2)
    ]
    stop: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        assert len(runner._jobs) == 2
        with pytest.raises(CommandDispatchRejected, match="overloaded"):
            await runner.submit(CommandInput(str(uuid.uuid4()), SID, submitted.generation, 1))
        stop = asyncio.create_task(runner.stop(submitted))
        await asyncio.sleep(0)  # one scheduler turn, not an elapsed-time delay
        assert not stop.done() and len(runner._jobs) == 2
        assert all(job.cancelling() == 1 for job in runner._jobs)
        assert finished == []
    finally:
        release.set()
        if stop is not None:
            await stop
        await asyncio.gather(*callers, return_exceptions=True)
    results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(
        isinstance(result, CommandDispatchRejected) and result.code == "closing"
        for result in results
    )
    assert not runner._jobs and runner.service.lookup(submitted) is None
    assert finished == [] and not drivers


async def test_stop_matches_every_queued_identity_field(rig, monkeypatch) -> None:
    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    submitted = CommandInput(**value)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = CommandService.lookup

    def held_lookup(self, candidate):
        result = original(self, candidate)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    monkeypatch.setattr(CommandService, "lookup", held_lookup)
    finished: list[str] = []
    caller = asyncio.create_task(runner.submit(submitted, finished=finished.append))
    stop: asyncio.Task[None] | None = None
    wrong_stop: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        job = next(iter(runner._jobs))
        assert runner._jobs[job] == submitted
        for wrong in (
            CommandInput(CID, str(uuid.uuid4()), submitted.generation, submitted.draft_revision),
            CommandInput(CID, SID, str(uuid.uuid4()), submitted.draft_revision),
            CommandInput(CID, SID, submitted.generation, submitted.draft_revision + 1),
        ):
            wrong_stop = asyncio.create_task(runner.stop(wrong))
            await asyncio.sleep(0)
            assert wrong_stop.done()
            await wrong_stop
            assert job.cancelling() == 0 and not job.done()
        stop = asyncio.create_task(runner.stop(submitted))
        await asyncio.sleep(0)
        assert job.cancelling() == 1 and not stop.done()
        assert runner._jobs[job] == submitted and finished == []
    finally:
        release.set()
        if wrong_stop is not None:
            await wrong_stop
        if stop is not None:
            await stop
        await asyncio.gather(caller, return_exceptions=True)
    with pytest.raises(CommandDispatchRejected, match="closing"):
        await caller
    assert not runner._jobs and runner.service.lookup(submitted) is None
    assert finished == [] and not drivers


async def test_stop_before_attempt_first_step_settles_its_receipt(rig) -> None:
    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    submitted = CommandInput(**value)
    finished: list[str] = []
    caller = asyncio.create_task(runner.submit(submitted, finished=finished.append))
    # The caller registers its job, then yields on its receipt; this continuation was
    # queued before the new job's first step.
    await asyncio.sleep(0)
    assert len(runner._jobs) == 1
    await runner.stop(submitted)
    with pytest.raises(CommandDispatchRejected, match="closing"):
        await caller
    assert not runner._jobs and runner.service.lookup(submitted) is None
    assert finished == [] and not drivers


@pytest.mark.parametrize("overlap", ["close", "cancel_stop_caller"])
async def test_stop_owns_driver_retirement_through_close_or_caller_cancellation(
    rig, monkeypatch, overlap
) -> None:
    from viki_agent.conversations import command_runtime

    app, _, _, manager, drivers, drafts = rig
    actor, value = prepare(manager, drafts)
    await actor.start()
    runner, submitted = app.state.commands, CommandInput(**value)
    driver = drivers[0]
    driver.send_gate.clear()
    driver.close_gate.clear()
    monkeypatch.setattr(command_runtime, "MAX_COMMAND_RUNS", 1)
    finished: list[str] = []
    record, created = await runner.submit(submitted, finished=finished.append)
    assert created and record.state == "accepted"
    await driver.sent.wait()
    job = next(iter(runner._jobs))
    stop: asyncio.Task[None] | None = None
    close: asyncio.Task[None] | None = None
    try:
        stop = asyncio.create_task(runner.stop(submitted))
        await driver.closing.wait()
        assert job.cancelling() == 1 and not stop.done()
        next_sid = str(uuid.uuid4())
        next_actor = manager.open(next_sid)
        next_draft = drafts.save(next_sid, 0, "another command")
        with pytest.raises(CommandDispatchRejected, match="overloaded"):
            await runner.submit(
                CommandInput(
                    str(uuid.uuid4()), next_sid, next_actor.generation, next_draft.revision
                )
            )
        if overlap == "close":
            close = asyncio.create_task(runner.close())
            await asyncio.sleep(0)
            assert runner._closing and not close.done()
        else:
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
        assert not stop.done() and len(runner._jobs) == 1
        assert job.cancelling() == 1 and finished == []
    finally:
        driver.close_gate.set()
        driver.send_gate.set()
        if stop is not None:
            if overlap == "cancel_stop_caller":
                with pytest.raises(asyncio.CancelledError):
                    await stop
            else:
                await stop
        if close is not None:
            await close
    assert not runner._jobs and finished == ["unknown"]
    assert runner.service.get(CID).state == "unknown"
    assert driver.calls.count("send submitted café") == 1
    assert driver.calls.count("close") == 1
    await runner.stop(submitted)
    if overlap == "close":
        with pytest.raises(CommandDispatchRejected, match="closing"):
            await runner.submit(submitted)


@pytest.mark.parametrize(
    "stage,expected,sends",
    [
        ("accept", "not_dispatched", 0),
        ("mark_dispatching", "unknown", 0),
        ("finish", "unknown", 1),
        ("finish_error", "unknown", 1),
    ],
)
async def test_stop_joins_held_command_worker_and_settles_receipt(
    rig, monkeypatch, stage, expected, sends
) -> None:
    from viki_agent.conversations import command_runtime

    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner, submitted = app.state.commands, CommandInput(**value)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    method = "finish" if stage == "finish_error" else stage
    original = getattr(CommandService, method)

    def held(self, *args):
        result = original(self, *args) if stage != "finish_error" else None
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        if stage == "finish_error":
            raise OSError("synthetic terminal failure")
        return result

    monkeypatch.setattr(CommandService, method, held)
    monkeypatch.setattr(command_runtime, "MAX_COMMAND_RUNS", 1)
    finished: list[str] = []
    caller = asyncio.create_task(runner.submit(submitted, finished=finished.append))
    stop: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        job = next(iter(runner._jobs))
        stop = asyncio.create_task(runner.stop(submitted))
        await asyncio.sleep(0)
        assert job.cancelling() == 1 and not stop.done()
        assert runner._jobs[job] == submitted and finished == []
        with pytest.raises(CommandDispatchRejected, match="overloaded"):
            await runner.submit(CommandInput(str(uuid.uuid4()), SID, submitted.generation, 1))
    finally:
        release.set()
        if stop is not None:
            await stop
        await asyncio.gather(caller, return_exceptions=True)
    receipt, created = await caller
    assert created and receipt.state == "accepted"
    assert not runner._jobs and finished == [expected]
    assert sum(driver.calls.count("send submitted café") for driver in drivers) == sends
    if stage == "finish_error":
        assert runner.service.get(CID).state == "dispatching"
        assert (await runner.get(CID)).state == "unknown"
    else:
        assert runner.service.get(CID).state == expected
    retry, created = await runner.submit(submitted)
    assert not created and retry.state == expected
    await settle(runner)
    assert sum(driver.calls.count("send submitted café") for driver in drivers) == sends


async def test_submit_stale_draft_revision_never_finishes(rig) -> None:
    from viki_agent.conversations.commands import CommandError

    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    finished: list[str] = []
    stale = CommandInput(**{**value, "draft_revision": value["draft_revision"] + 1})
    with pytest.raises(CommandError) as refused:
        await runner.submit(stale, admitted=lambda: True, finished=finished.append)
    await settle(runner)
    assert refused.value.code == "draft_conflict"
    assert finished == []
    assert not any("send " in call for driver in drivers for call in driver.calls)


async def test_submit_target_lost_after_acceptance_finishes_not_dispatched(rig) -> None:
    app, _, _, manager, drivers, drafts = rig
    _, value = prepare(manager, drafts)
    runner = app.state.commands
    checks: list[bool] = []
    finished: list[str] = []

    def admitted() -> bool:
        checks.append(not checks)
        return checks[-1]

    _, created = await runner.submit(
        CommandInput(**value), admitted=admitted, finished=finished.append
    )
    assert created
    await settle(runner)
    assert finished == ["not_dispatched"]
    assert runner.service.get(CID).state == "not_dispatched"
    assert not any("send " in call for driver in drivers for call in driver.calls)
    assert not drivers


@pytest.mark.parametrize("code", ["busy", "incompatible", "recovery_required"])
async def test_storage_state_errors_refuse_with_their_own_code_and_log_it(
    rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: str
) -> None:
    """CommandService._access passes StateError codes through untouched, so these three reach
    the route table from outside the commands' own list. The approvals route answers this same
    passthrough `busy` with 409; here every one of them is 503, which the browser reads by
    status class rather than by code. The divergence is pinned, not endorsed.
    """
    app, client, _, manager, _drivers, drafts = rig
    _actor, value = prepare(manager, drafts)

    def unavailable(*_args: Any) -> None:
        raise StateError(code)

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(commands_module, "require_private_store", unavailable)
    response = await client.post(PATH, headers=WRITE, json=value)
    assert response.status_code == 503 and response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] == StateError(code).message
    assert code in caplog.text and StateError(code).message not in caplog.text
    monkeypatch.undo()
    assert (await client.get(PATH, headers=AUTH)).json()["commands"] == []


async def test_turn_running_and_storage_busy_answer_different_statuses(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actor admission refusal (a turn is running) and a storage lock passthrough (`busy`) are
    different situations and must never answer with the same status again: proven side by side so
    a future rename cannot quietly re-merge them.
    """
    app, client, _, manager, drivers, drafts = rig
    actor, value = prepare(manager, drafts)
    await actor.start()
    drivers[0].busy = True
    dispatch_refused = await client.post(PATH, headers=WRITE, json=value)
    assert dispatch_refused.status_code == 409
    assert dispatch_refused.json()["error"]["code"] == "turn_running"
    drivers[0].busy = False

    def unavailable(*_args: Any) -> None:
        raise StateError("busy")

    monkeypatch.setattr(commands_module, "require_private_store", unavailable)
    storage_locked = await client.post(PATH, headers=WRITE, json=value)
    assert storage_locked.status_code == 503
    assert storage_locked.json()["error"]["code"] == "busy"
    monkeypatch.undo()
    assert app.state.commands.service.list().commands == []
