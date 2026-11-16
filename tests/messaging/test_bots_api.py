"""Private Bot routes with synthetic storage, callback adapters and no provider connection."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.messaging.test_bots import _bot, _id, _routine
from tests.messaging.test_bots import service as bot_service
from tests.profiles.test_profile_document import document
from viki_agent.conversations.command_runtime import storage_call
from viki_agent.messaging import bots_api
from viki_agent.messaging.bots import BotError, BotService
from viki_agent.profiles.service import ProfileError

ORIGIN = "http://127.0.0.1:8765"
BEARER = {"Authorization": "Bearer bot-test-token"}
WRITE = {**BEARER, "Origin": ORIGIN}
BASE = "/api/v2/bots"
HANG_GUARD_S = 5  # Event hang guard, not a timer bound on the operation under test.
service = bot_service


class Rig:
    def __init__(self, service: BotService) -> None:
        self.service = service
        self.bot = _bot(service)
        self.workers: set[asyncio.Task[Any]] = set()
        self.lock = asyncio.Lock()
        self.prepared: dict[str, str] = {}
        self.inspected: list[str] = []
        self.revoked: list[str] = []
        self.busy_chats: set[str] = set()
        self.enabled: set[str] = set()
        self.woken = 0
        self.prepare_calls = 0
        self.status = "unprepared"
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None
        self.prepare_error: Exception | None = None

    def preview(self, bot: dict[str, Any]) -> dict[str, Any]:
        session_id = bot["chat_session_id"]
        generation = self.prepared.setdefault(session_id, _id())
        return {
            "launchable": True,
            "fields": [],
            "blockers": [],
            "warnings": [],
            "prefs": {"mode": "default", "effort": "high", "model": None},
            "session_id": session_id,
            "profile_id": bot["profile_id"],
            "profile_revision": 1,
            "generation": generation,
            "workspace_id": bot["workspace_id"],
            "workspace_revision": 1,
            "policy": "ordinary",
            "policy_revision": 1,
            "settings_revision": 0,
            "runtime": "claude",
            "capability_key": "a" * 64,
            "draft": {
                "key": session_id,
                "revision": 0,
                "text": "",
                "saved_at": None,
                "attachments": [],
            },
        }

    async def prepare_chat(self, bot: dict[str, Any]) -> dict[str, Any]:
        self.prepare_calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.preview(bot)

    async def chat_status(self, bot: dict[str, Any]) -> dict[str, Any]:
        self.inspected.append(bot["chat_session_id"])
        blockers = ["transcript_missing"] if self.status == "transcript_missing" else []
        return {"state": self.status, "blockers": blockers}

    def delivery_status(self, bot: dict[str, Any]) -> dict[str, Any]:
        enabled = bot["bot_id"] in self.enabled
        return {"enabled": enabled, "available": True, "reason": "ready" if enabled else "off"}

    async def set_delivery(self, bot_id: str, expected: int, enabled: bool) -> dict[str, Any]:
        self.revoke(bot_id)
        result = await storage_call(lambda: self.service.bump_revision(bot_id, expected))
        if enabled:
            self.enabled.add(bot_id)
        return result

    def revoke(self, bot_id: str) -> None:
        self.revoked.append(bot_id)
        self.enabled.discard(bot_id)

    def wake(self) -> None:
        self.woken += 1

    def app(self) -> Starlette:
        return Starlette(
            routes=bots_api.bot_routes(
                self.service,
                authorized=lambda request: (
                    request.headers.get("authorization") == BEARER["Authorization"]
                ),
                origins=frozenset({ORIGIN}),
                workers=self.workers,
                admission_lock=self.lock,
                prepare_chat=self.prepare_chat,
                chat_status=self.chat_status,
                busy=lambda session_id: session_id in self.busy_chats,
                scheduler_status=lambda: {"state": "running", "attended": True, "paused": False},
                delivery_status=self.delivery_status,
                set_delivery=self.set_delivery,
                revoke=self.revoke,
                wake=self.wake,
            )
        )


@pytest.fixture
def rig(service: BotService) -> Rig:
    return Rig(service)


def test_roster_has_exact_safe_wire_and_never_inspects_or_prepares_chat(rig: Rig) -> None:
    with TestClient(rig.app()) as client:
        response = client.get(BASE, headers=BEARER)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["scheduler"] == {"state": "running", "attended": True, "paused": False}
    assert result["bots"][0]["bot_id"] == rig.bot["bot_id"]
    assert result["bots"][0]["delivery"] == {"enabled": False, "available": True, "reason": "off"}
    assert rig.inspected == [] and rig.prepare_calls == 0
    assert "instructions" not in response.text and "request_hash" not in response.text


def test_detail_checks_only_selected_chat_and_reports_missing_transcript(rig: Rig) -> None:
    _bot(rig.service)
    rig.status = "transcript_missing"
    with TestClient(rig.app()) as client:
        response = client.get(f"{BASE}/{rig.bot['bot_id']}", headers=BEARER)
    assert response.status_code == 200
    assert response.json()["chat_status"] == {
        "state": "transcript_missing",
        "blockers": ["transcript_missing"],
    }
    assert rig.inspected == [rig.bot["chat_session_id"]] and rig.prepare_calls == 0


@pytest.mark.parametrize("escaped", ['"', "\0"])
def test_roster_preserves_64_bots_with_legal_json_escaped_identity_text(
    rig: Rig, escaped: str
) -> None:
    value = document()
    value["identity"].update(name=escaped * 128, purpose=escaped * 2048)
    rig.service.profiles.revise(rig.bot["profile_id"], _id(), 1, value)
    bot_ids = {rig.bot["bot_id"]}
    for _ in range(63):
        profile_id = _id()
        rig.service.profiles.create(profile_id, _id(), value)
        bot_ids.add(
            _bot(rig.service, profile_id=profile_id, workspace_id=rig.bot["workspace_id"])["bot_id"]
        )
    with TestClient(rig.app()) as client:
        response = client.get(BASE, headers=BEARER)
    assert response.status_code == 200
    assert (256 << 10) < len(response.content) <= (2 << 20)
    result = response.json()["bots"]
    assert {row["bot_id"] for row in result} == bot_ids
    assert len(result) == 64
    assert all(row["profile"]["identity"] == value["identity"] for row in result)
    with pytest.raises(BotError, match="capacity"):
        _bot(rig.service, profile_id=rig.bot["profile_id"], expected_profile_revision=2)


def test_list_budget_does_not_expand_detail_or_mutation_responses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bots_api, "MAX_RESPONSE", 32)
    with TestClient(rig.app()) as client:
        assert client.get(BASE, headers=BEARER).status_code == 200
        assert client.get(f"{BASE}/{rig.bot['bot_id']}", headers=BEARER).status_code == 503
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/new-chat",
            headers=WRITE,
            json={"expected_state_revision": 1},
        )
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("method", "suffix", "headers", "status"),
    [
        ("GET", "", {}, 401),
        ("POST", "/chat", {}, 401),
        ("GET", "", {**BEARER, "Origin": "https://foreign.test"}, 403),
        ("POST", "/chat", BEARER, 403),
        ("DELETE", "", BEARER, 403),
        ("POST", "/chat", {**WRITE, "Origin": "https://foreign.test"}, 403),
        ("GET", "?x=1", BEARER, 400),
        ("POST", "/chat?x=1", WRITE, 400),
    ],
)
def test_auth_origin_and_query_precede_every_callback(
    rig: Rig, method: str, suffix: str, headers: dict[str, str], status: int
) -> None:
    with TestClient(rig.app()) as client:
        response = client.request(
            method,
            f"{BASE}/{rig.bot['bot_id']}{suffix}",
            headers=headers,
            json={"expected_state_revision": 1} if method != "GET" else None,
        )
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert rig.prepare_calls == 0 and not rig.inspected and not rig.revoked


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b"{",
        b'{"expected_state_revision":1,"extra":true}',
        b'{"expected_state_revision":1,"expected_state_revision":1}',
        b'{"expected_state_revision":true}',
        b'{"expected_state_revision":0}',
        b'{"expected_state_revision":1.5}',
        b'{"expected_state_revision":9007199254740992}',
        b"x" * 4097,
        b"\xff",
    ],
)
def test_bad_json_and_revision_cannot_prepare_or_revoke(rig: Rig, body: bytes) -> None:
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat",
            headers={**WRITE, "Content-Type": "application/json"},
            content=body,
        )
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"
    assert rig.prepare_calls == 0 and not rig.revoked


@pytest.mark.parametrize(
    "header",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json;charset=utf-16"},
        {"Content-Length": "4097"},
        {"Content-Length": "nonsense"},
        {"Transfer-Encoding": "chunked"},
    ],
)
def test_body_header_boundary_precedes_mutation(rig: Rig, header: dict[str, str]) -> None:
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/new-chat",
            headers={**WRITE, **header},
            json={"expected_state_revision": 1},
        )
    assert response.status_code == 400 and not rig.revoked
    assert rig.service.record(rig.bot["bot_id"])["state_revision"] == 1


def test_false_content_length_does_not_hide_actual_oversized_body(rig: Rig) -> None:
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat",
            headers={**WRITE, "Content-Type": "application/json", "Content-Length": "0"},
            content=b" " * 4097,
        )
    assert response.status_code == 400 and rig.prepare_calls == 0


def test_create_returns_201_then_200_replay(rig: Rig) -> None:
    bot = rig.bot
    rig.service.remove(bot["bot_id"], 1)
    body = {
        "bot_id": bot["bot_id"],
        "profile_id": bot["profile_id"],
        "workspace_id": bot["workspace_id"],
        "expected_profile_revision": 1,
    }
    with TestClient(rig.app()) as client:
        created = client.post(BASE, headers=WRITE, json=body)
        replay = client.post(BASE, headers=WRITE, json=body)
    assert created.status_code == 201 and not created.json()["replayed"]
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert created.json()["bot"]["chat_session_id"] == replay.json()["bot"]["chat_session_id"]


def test_stale_and_busy_new_chat_refuse_without_replacing_id(rig: Rig) -> None:
    rig.busy_chats.add(rig.bot["chat_session_id"])
    with TestClient(rig.app()) as client:
        busy = client.post(
            f"{BASE}/{rig.bot['bot_id']}/new-chat",
            headers=WRITE,
            json={"expected_state_revision": 1},
        )
        stale = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 2}
        )
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    assert rig.service.record(rig.bot["bot_id"])["chat_session_id"] == rig.bot["chat_session_id"]
    assert not rig.revoked and rig.prepare_calls == 0


def test_prepared_and_blocked_chat_wrap_existing_preview_exactly(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(rig.app()) as client:
        prepared = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 1}
        )
    assert prepared.status_code == 200
    assert prepared.json() == {
        "bot_id": rig.bot["bot_id"],
        "state_revision": 1,
        "preview": rig.preview(rig.bot),
    }
    blocked = {
        "launchable": False,
        "fields": [],
        "blockers": ["capability_unverified"],
        "profile_id": rig.bot["profile_id"],
        "profile_revision": 1,
    }

    async def block(_bot_row: dict[str, Any]) -> dict[str, Any]:
        return blocked

    monkeypatch.setattr(rig, "prepare_chat", block)
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 1}
        )
    assert response.status_code == 422 and response.json()["preview"] == blocked


@pytest.mark.parametrize("field", ["profile_id", "workspace_id", "session_id", "generation"])
def test_wrong_prepared_identity_or_missing_generation_fails_closed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    async def wrong(bot: dict[str, Any]) -> dict[str, Any]:
        return {**rig.preview(bot), field: None if field == "generation" else _id()}

    monkeypatch.setattr(rig, "prepare_chat", wrong)
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 1}
        )
    assert response.status_code == 503 and "preview" not in response.json()


def test_delivery_toggle_cas_and_remove_revoke_before_pause_write(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _routine(rig.service, rig.bot)
    remove = rig.service.remove

    def inspected_remove(bot_id: str, revision: int) -> dict[str, Any]:
        assert bot_id not in rig.enabled and bot_id in rig.revoked
        return remove(bot_id, revision)

    monkeypatch.setattr(rig.service, "remove", inspected_remove)
    with TestClient(rig.app()) as client:
        enabled = client.post(
            f"{BASE}/{rig.bot['bot_id']}/delivery",
            headers=WRITE,
            json={"expected_state_revision": 1, "enabled": True},
        )
        stale = client.post(
            f"{BASE}/{rig.bot['bot_id']}/delivery",
            headers=WRITE,
            json={"expected_state_revision": 1, "enabled": True},
        )
        removed = client.request(
            "DELETE",
            f"{BASE}/{rig.bot['bot_id']}",
            headers=WRITE,
            json={"expected_state_revision": 2},
        )
    assert enabled.status_code == 200 and enabled.json()["bot"]["delivery"]["enabled"]
    assert stale.status_code == 409 and rig.bot["bot_id"] not in rig.enabled
    assert removed.status_code == 200 and removed.json()["paused_routine_ids"] == [routine_id]
    assert rig.service.routines.get(routine_id)["state"] == "paused"


def test_reset_revokes_selected_workspace_without_disarming_other_bot(rig: Rig) -> None:
    other = _bot(rig.service)
    rig.enabled.update({rig.bot["bot_id"], other["bot_id"]})
    with TestClient(rig.app()) as client:
        response = client.post(
            BASE + "/reset", headers=WRITE, json={"workspace_id": rig.bot["workspace_id"]}
        )
    assert response.status_code == 200
    assert response.json()["removed_bot_ids"] == [rig.bot["bot_id"]]
    assert rig.revoked == [rig.bot["bot_id"]] and other["bot_id"] in rig.enabled


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (BotError("not_found"), 404, "not_found"),
        (ProfileError("transcript_missing"), 404, "transcript_missing"),
        (ProfileError("workspace_restart_required"), 409, "workspace_restart_required"),
        (BotError("profile_archived"), 422, "profile_archived"),
        (BotError("capacity"), 429, "capacity"),
        (BotError("corrupt"), 503, "corrupt"),
        (BotError("synthetic-secret-value"), 503, "unavailable"),
        (RuntimeError("synthetic-secret-value"), 503, "unavailable"),
    ],
)
def test_fixed_error_mapping_omits_exception_values(
    rig: Rig, error: Exception, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.prepare_error = error
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 1}
        )
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "synthetic-secret-value" not in response.text + caplog.text


def test_roster_and_chat_actual_serialized_response_caps(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(bots_api, "MAX_LIST_RESPONSE", 32)
        with TestClient(rig.app()) as client:
            assert client.get(BASE, headers=BEARER).status_code == 503

    async def oversized(bot: dict[str, Any]) -> dict[str, Any]:
        result = rig.preview(bot)
        result["draft"]["text"] = "\u2603" * 750000
        assert (
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > bots_api.MAX_CHAT_RESPONSE
        )
        return result

    monkeypatch.setattr(rig, "prepare_chat", oversized)
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/chat", headers=WRITE, json={"expected_state_revision": 1}
        )
    assert response.status_code == 503 and len(response.content) < 256


async def test_concurrent_open_serializes_adapter_and_reuses_reserved_chat(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.gate, rig.entered = asyncio.Event(), asyncio.Event()
    second_prepared, second_admitted = asyncio.Event(), asyncio.Event()

    class Workers(set[asyncio.Task[Any]]):
        def add(self, worker: asyncio.Task[Any]) -> None:
            super().add(worker)
            if len(self) == 2:
                second_admitted.set()

    rig.workers = Workers()
    prepare = rig.prepare_chat
    monkeypatch.setattr(rig.service, "get", lambda _bot_id: rig.bot)

    async def guarded(bot: dict[str, Any]) -> dict[str, Any]:
        if rig.prepare_calls == 1:
            second_prepared.set()
        return await prepare(bot)

    monkeypatch.setattr(rig, "prepare_chat", guarded)
    transport = httpx.ASGITransport(app=rig.app())
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        first = asyncio.create_task(
            client.post(
                f"{BASE}/{rig.bot['bot_id']}/chat",
                headers=WRITE,
                json={"expected_state_revision": 1},
            )
        )
        async with asyncio.timeout(HANG_GUARD_S):
            await rig.entered.wait()
        second = asyncio.create_task(
            client.post(
                f"{BASE}/{rig.bot['bot_id']}/chat",
                headers=WRITE,
                json={"expected_state_revision": 1},
            )
        )
        async with asyncio.timeout(HANG_GUARD_S):
            await second_admitted.wait()
        try:
            with pytest.raises(TimeoutError), anyio.fail_after(0.02):
                await second_prepared.wait()
        finally:
            rig.gate.set()
            responses = await asyncio.gather(first, second)
    assert [response.status_code for response in responses] == [200, 200]
    assert len(rig.prepared) == 1
    assert (
        responses[0].json()["preview"]["generation"] == responses[1].json()["preview"]["generation"]
    )
    assert not rig.workers


async def test_capacity_tracks_locked_workers_until_owned_operations_finish(rig: Rig) -> None:
    rig.gate, rig.entered = asyncio.Event(), asyncio.Event()
    admitted = asyncio.Event()

    class Workers(set[asyncio.Task[Any]]):
        def add(self, worker: asyncio.Task[Any]) -> None:
            super().add(worker)
            if len(self) == bots_api.MAX_REQUESTS:
                admitted.set()

    rig.workers = Workers()
    transport = httpx.ASGITransport(app=rig.app())
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        calls = [
            asyncio.create_task(
                client.post(
                    f"{BASE}/{rig.bot['bot_id']}/chat",
                    headers=WRITE,
                    json={"expected_state_revision": 1},
                )
            )
            for _ in range(bots_api.MAX_REQUESTS)
        ]
        try:
            async with asyncio.timeout(HANG_GUARD_S):
                await admitted.wait()
            response = await client.get(BASE, headers=BEARER)
            assert response.status_code == 429 and len(rig.workers) == bots_api.MAX_REQUESTS
        finally:
            rig.gate.set()
            results = await asyncio.gather(*calls)
        assert [result.status_code for result in results] == [200] * bots_api.MAX_REQUESTS
    assert not rig.workers


async def test_caller_cancellation_keeps_owned_mutation_until_completion(rig: Rig) -> None:
    rig.gate, rig.entered = asyncio.Event(), asyncio.Event()
    transport = httpx.ASGITransport(app=rig.app())
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        caller = asyncio.create_task(
            client.post(
                f"{BASE}/{rig.bot['bot_id']}/chat",
                headers=WRITE,
                json={"expected_state_revision": 1},
            )
        )
        async with asyncio.timeout(HANG_GUARD_S):
            await rig.entered.wait()
        worker = next(iter(rig.workers))
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert not worker.done() and rig.lock.locked()
        rig.gate.set()
        response = await worker
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert not rig.workers and not rig.lock.locked()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_incomplete_intake_cannot_revoke(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, disconnect: bool
) -> None:
    monkeypatch.setattr(bots_api, "INTAKE_TIMEOUT_S", 0.01)

    async def receive() -> Any:
        if disconnect:
            raise ClientDisconnect
        await asyncio.Event().wait()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"{BASE}/{rig.bot['bot_id']}/new-chat",
            "query_string": b"",
            "path_params": {"bot_id": rig.bot["bot_id"], "action": "new-chat"},
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii"))
                for key, value in {**WRITE, "Content-Type": "application/json"}.items()
            ],
        },
        receive=receive,
    )
    route = rig.app().routes[-1]
    assert isinstance(route, Route)
    response = await route.endpoint(request)
    assert response.status_code == (400 if disconnect else 408) and not rig.revoked


def test_storage_reads_run_outside_request_event_loop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_threads: list[int] = []
    read_threads: list[int] = []
    listing = rig.service.list

    def list_off_loop() -> list[dict[str, Any]]:
        read_threads.append(threading.get_ident())
        return listing()

    monkeypatch.setattr(rig.service, "list", list_off_loop)
    original = rig.delivery_status

    def on_loop(bot: dict[str, Any]) -> dict[str, Any]:
        caller_threads.append(threading.get_ident())
        return original(bot)

    monkeypatch.setattr(rig, "delivery_status", on_loop)
    with TestClient(rig.app()) as client:
        assert client.get(BASE, headers=BEARER).status_code == 200
    assert read_threads and caller_threads and read_threads[0] != caller_threads[0]
