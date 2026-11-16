"""Durable REST approvals over synthetic app storage and an in-process driver; no server port.

Every test answers a card the actor really raised, so the binding, the projected request and the
driver callback are the real ones. Nothing here touches the user's vault or app data.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from starlette.types import Message, Scope

from tests.conversations.test_session import GatedDriver
from viki_agent import protection
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.conversations import approval_api, approval_runtime
from viki_agent.conversations import approvals as approvals_module
from viki_agent.conversations.approval_runtime import ApprovalRunner
from viki_agent.conversations.approvals import ApprovalService, PermissionChoice
from viki_agent.drivers.base import Answer, PermissionAnswer, QuestionAnswer
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import (
    Event,
    PermissionRequested,
    Question,
    QuestionAsked,
    QuestionOption,
    RequestAnswered,
    RequestCancelled,
)
from viki_agent.server import create_app
from viki_agent.session import SessionActor, SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError

BASE = "http://127.0.0.1:8765"
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TOOL = "toolu_01SyntheticCard"
TOKEN = "approval-test-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
WRITE = {**AUTH, "origin": BASE}
PATH = "/api/v2/approvals"
ALLOW: dict[str, Any] = {"kind": "permission", "allow": True}
DENY: dict[str, Any] = {"kind": "permission", "allow": False}
ANSWER: dict[str, Any] = {
    "kind": "question",
    "answers": {"Tea or coffee?": "Tea"},
    "response": "",
}
STATE_CODES = [("busy", 409), ("incompatible", 503), ("recovery_required", 503)]
# A missing lifecycle event must fail rather than hang; this is not a sleep.
HANG_GUARD_S = 5


class _AnswerDriver(GatedDriver):
    """Opens at once; raises cards on demand and resolves them as the real driver does."""

    def __init__(self, emit: Callable[[Event], None]) -> None:
        super().__init__()
        self.release.set()
        self._emit = emit
        self.requests: set[str] = set()
        self.answers: list[tuple[str, Answer]] = []

    def ask(self, tool_use_id: str = TOOL, *, question: bool = False) -> None:
        self.requests.add(tool_use_id)
        self._emit(
            QuestionAsked(
                tool_use_id=tool_use_id,
                questions=[
                    Question(
                        header="Choice",
                        question="Tea or coffee?",
                        options=[QuestionOption(label="Tea"), QuestionOption(label="Coffee")],
                    )
                ],
            )
            if question
            else PermissionRequested(
                tool_use_id=tool_use_id, kind="tool", tool="Read", summary="synthetic card"
            )
        )

    def respond(self, tool_use_id: str, answer: Answer) -> bool:
        self.answers.append((tool_use_id, answer))
        if tool_use_id not in self.requests:
            return False
        self.requests.remove(tool_use_id)
        self._emit(RequestAnswered(tool_use_id=tool_use_id))
        return True

    def pending(self) -> list[str]:
        return list(self.requests)


def assemble(tmp_path: Path, *, dev: bool = False):
    data, vault, home = tmp_path / "data", tmp_path / "vault", tmp_path / "home"
    vault.mkdir(exist_ok=True)
    state = AppState(data / "state.json", Prefs())
    drivers: list[_AnswerDriver] = []

    def make_driver(_launch: Any, _prefs: Any, emit: Callable[[Event], None]) -> _AnswerDriver:
        driver = _AnswerDriver(emit)
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
        token=TOKEN,
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
        dev=dev,
    )
    return app, state, manager, drivers


@pytest.fixture
async def rig(tmp_path: Path):
    app, state, manager, drivers = assemble(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        yield app, client, state, manager, drivers


class Card:
    """One pending card plus what the session's subscribers saw while it was raised."""

    def __init__(
        self,
        actor: SessionActor,
        driver: _AnswerDriver,
        seen: list[dict[str, Any]],
        envelope: dict[str, Any],
    ) -> None:
        self.actor, self.driver, self.seen, self.envelope = actor, driver, seen, envelope

    @property
    def answers(self) -> list[Answer]:
        return [answer for _tool, answer in self.driver.answers]

    @property
    def native(self) -> dict[str, str]:
        return self.envelope["native_request"]

    def body(self, choice: dict[str, Any] = ALLOW, *, choice_id: str = CID) -> dict[str, Any]:
        return {
            "choice_id": choice_id,
            "session_id": SID,
            "generation": self.actor.generation,
            "tool_use_id": self.envelope["data"]["tool_use_id"],
            "native_request": self.native,
            "choice": choice,
        }

    @property
    def lookup(self) -> str:
        return (
            f"/api/v2/sessions/{SID}/approval"
            f"?runtime_id={self.native['runtime_id']}&request_id={self.native['request_id']}"
        )


async def raise_card(
    manager: SessionManager, drivers: list[_AnswerDriver], *, question: bool = False
) -> Card:
    actor = manager.open(SID)
    seen: list[dict[str, Any]] = []
    actor.subscribe(seen.append)
    await actor.start()
    drivers[-1].ask(question=question)
    envelope = next(e for e in seen if e["type"] in {"permission.requested", "question.asked"})
    return Card(actor, drivers[-1], seen, envelope)


async def settle(runner: ApprovalRunner) -> None:
    if runner._jobs:
        await asyncio.gather(*runner._jobs, return_exceptions=True)


def gate(
    monkeypatch: pytest.MonkeyPatch, stage: str, *, after: bool
) -> tuple[asyncio.Event, threading.Event]:
    """Hold the attempt inside one storage call, before or after that call commits.

    The wait is bounded because an assertion that fails before its `release.set()` would
    otherwise leave a worker thread blocked forever and hang the run instead of failing it.
    A passing test always releases first, so the bound is never reached.
    """
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = getattr(ApprovalService, stage)

    def blocked(self: ApprovalService, *args: Any) -> Any:
        if not after:
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5.0)
            return original(self, *args)
        result = original(self, *args)
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=5.0)
        return result

    monkeypatch.setattr(ApprovalService, stage, blocked)
    return entered, release


def trace(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    """Record the order of the two commits and the one local callback."""
    for stage in ("record", "mark_delivering"):
        original = getattr(ApprovalService, stage)

        def traced(self: ApprovalService, *args: Any, _name=stage, _call=original) -> Any:
            result = _call(self, *args)
            order.append(_name)
            return result

        monkeypatch.setattr(ApprovalService, stage, traced)
    original_respond = _AnswerDriver.respond

    def traced_respond(self: _AnswerDriver, tool_use_id: str, answer: Answer) -> bool:
        order.append("respond")
        return original_respond(self, tool_use_id, answer)

    monkeypatch.setattr(_AnswerDriver, "respond", traced_respond)


async def test_record_and_intent_precede_the_single_local_callback(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    order: list[str] = []
    trace(monkeypatch, order)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert order == ["record", "mark_delivering", "respond"]
    assert response.status_code == 202
    assert response.json()["state"] == "delivered_locally" and response.json()["reason"] == ""
    assert app.state.approvals.service.summary(CID).state == "delivered_locally"
    assert card.answers == [PermissionAnswer(allow=True)]
    # The answering window closes its card on this event and on nothing else:
    # the App sends no socket message and never removes a card optimistically.
    assert [e["type"] for e in card.seen].count("request.answered") == 1


async def test_delivered_answer_is_decoded_from_the_committed_record(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    original = ApprovalService.record

    def opposite(self: ApprovalService, value: Any, request: Any) -> Any:
        # The committed row is the only body the seam may deliver. Posting Allow once while the
        # row decodes as Deny can be told apart nowhere but at the callback itself.
        record, created = original(self, value, request)
        return replace(record, choice=PermissionChoice(False)), created

    monkeypatch.setattr(ApprovalService, "record", opposite)
    assert (await client.post(PATH, headers=WRITE, json=card.body(ALLOW))).status_code == 202
    assert card.answers == [PermissionAnswer(allow=False)]


async def test_question_answers_reach_the_driver_from_the_record(rig) -> None:
    _app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers, question=True)
    response = await client.post(PATH, headers=WRITE, json=card.body(ANSWER))
    assert response.status_code == 202 and response.json()["state"] == "delivered_locally"
    assert card.answers == [QuestionAnswer(answers={"Tea or coffee?": "Tea"}, response="")]
    stored = await client.get(f"{PATH}/{CID}/choice", headers=AUTH)
    assert stored.json()["choice"] == ANSWER
    assert stored.json()["request"]["type"] == "question.asked"
    assert stored.json()["request"]["native_request"] == card.native


async def test_second_choice_id_for_same_request_is_already_decided_without_dispatch(rig) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    assert (await client.post(PATH, headers=WRITE, json=card.body())).status_code == 202
    second = await client.post(
        PATH, headers=WRITE, json=card.body(DENY, choice_id=str(uuid.uuid4()))
    )
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"
    assert len(card.answers) == 1
    assert len(app.state.approvals.service.list().approvals) == 1


async def test_identical_choice_id_returns_existing_receipt_without_second_callback(rig) -> None:
    _app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    first = await client.post(PATH, headers=WRITE, json=card.body())
    second = await client.post(PATH, headers=WRITE, json=card.body())
    assert first.status_code == 202 and second.status_code == 200
    assert first.json() == second.json()
    assert len(card.answers) == 1


async def test_conflicting_input_under_same_id_is_409_and_delivers_nothing_new(rig) -> None:
    _app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    assert (await client.post(PATH, headers=WRITE, json=card.body(ALLOW))).status_code == 202
    conflict = await client.post(PATH, headers=WRITE, json=card.body(DENY))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"
    assert card.answers == [PermissionAnswer(allow=True)]


async def test_cancel_before_record_leaves_no_row(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "lookup", after=False)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    shutdown = asyncio.create_task(app.state.approvals.close())
    # sleep(0) so the queued shutdown task reaches its gather before the thread is released.
    await asyncio.sleep(0)
    release.set()
    await shutdown
    response = await caller
    assert response.status_code == 503 and response.json()["error"]["code"] == "closing"
    assert app.state.approvals.service.list().approvals == []
    assert card.answers == []


async def test_shutdown_after_record_is_not_delivered_cancelled(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "mark_delivering", after=False)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    assert app.state.approvals.service.summary(CID).state == "recorded"
    shutdown = asyncio.create_task(app.state.approvals.close())
    await asyncio.sleep(0)
    release.set()
    await shutdown
    assert (await caller).status_code == 202
    receipt = app.state.approvals.service.summary(CID)
    assert (receipt.state, receipt.reason) == ("not_delivered", "cancelled")
    assert card.answers == []


async def test_shutdown_after_intent_is_not_delivered_cancelled_with_specific_evidence(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "mark_delivering", after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    assert app.state.approvals.service.summary(CID).state == "delivering"
    shutdown = asyncio.create_task(app.state.approvals.close())
    await asyncio.sleep(0)
    release.set()
    await shutdown
    response = await caller
    assert response.status_code == 202
    # Intent is committed, so a conservative coordinator would stop at unknown. The callback is
    # synchronous and provably never ran, which is the stronger claim the row must carry.
    assert response.json()["state"] == "not_delivered" and response.json()["reason"] == "cancelled"
    assert app.state.approvals.service.summary(CID).state == "not_delivered"
    assert card.answers == []


@pytest.mark.parametrize("stage", ["record", "mark_delivering"])
async def test_replacement_after_record_or_intent_never_delivers_to_new_actor(
    rig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, stage, after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    try:
        replacement = manager.create(SID)
        assert replacement is not card.actor
    finally:
        release.set()
    assert (await caller).status_code == 202
    await settle(app.state.approvals)
    receipt = app.state.approvals.service.summary(CID)
    assert (receipt.state, receipt.reason) == ("not_delivered", "runtime_changed")
    assert all(driver.answers == [] for driver in drivers)


async def test_closed_but_managed_actor_is_closed_or_runtime_changed_never_unknown(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "mark_delivering", after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    try:
        # SessionManager.close marks an actor closed while it is still managed; closing the
        # actor directly reproduces that window without racing the manager's own await.
        await card.actor.close()
    finally:
        release.set()
    assert (await caller).json()["state"] == "not_delivered"
    assert app.state.approvals.service.summary(CID).reason == "runtime_changed"
    assert card.answers == []
    assert manager.get(SID) is card.actor and card.actor.command_context()["closed"]
    # A choice for any other card of this still-managed actor is refused as `closed` before the
    # binding is even consulted, so nothing is recorded and the answer cannot read as unknown.
    other = {
        **card.body(choice_id=str(uuid.uuid4())),
        "native_request": {name: str(uuid.uuid4()) for name in card.native},
    }
    refused = await client.post(PATH, headers=WRITE, json=other)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "closed"
    assert len(app.state.approvals.service.list().approvals) == 1


async def test_cancelled_http_caller_keeps_its_runner_attempt_slot_and_it_finishes(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This pins `ApprovalRunner`'s own attempt-slot capacity (`MAX_APPROVAL_RUNS`) surviving an
    HTTP disconnect. The route's own worker capacity (`MAX_APPROVAL_REQUESTS`, four intake/read
    permits) is a different bound, pinned separately below."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    monkeypatch.setattr(approval_runtime, "MAX_APPROVAL_RUNS", 1)
    entered, release = gate(monkeypatch, "record", after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    try:
        pressure = await client.post(
            PATH, headers=WRITE, json=card.body(choice_id=str(uuid.uuid4()))
        )
        assert pressure.status_code == 429 and pressure.json()["error"]["code"] == "overloaded"
        assert (await client.get(card.lookup, headers=AUTH)).status_code == 200
    finally:
        release.set()
        await settle(app.state.approvals)
    assert len(card.answers) == 1
    assert (await client.get(f"{PATH}/{CID}", headers=AUTH)).json()["state"] == "delivered_locally"


async def test_disconnected_intake_workers_keep_their_permit_until_the_attempt_finishes(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four intake/read workers (`MAX_APPROVAL_REQUESTS`) keep their permit after
    an HTTP disconnect. Four callers are cancelled while held inside `record`; capacity must stay
    exhausted until their attempts actually finish, not the moment the caller goes away."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    driver = card.driver
    tool_ids = [TOOL, "toolu_02", "toolu_03", "toolu_04"]
    for tool_use_id in tool_ids[1:]:
        driver.ask(tool_use_id=tool_use_id)
    native_requests = {
        tool_use_id: next(
            e
            for e in card.seen
            if e["type"] == "permission.requested" and e["data"]["tool_use_id"] == tool_use_id
        )["native_request"]
        for tool_use_id in tool_ids
    }
    bodies = [
        {
            "choice_id": str(uuid.uuid4()),
            "session_id": SID,
            "generation": card.actor.generation,
            "tool_use_id": tool_use_id,
            "native_request": native_requests[tool_use_id],
            "choice": ALLOW,
        }
        for tool_use_id in tool_ids
    ]

    lock = threading.Lock()
    # The four permits stay held together, but the attempts' storage writes (record, intent,
    # finalization) take turns: the property is about permits, and four writers contending for
    # the store's lock would test its busy budget against the disk's commit time instead.
    write_lock = threading.Lock()
    entered_count = 0
    all_entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_record = ApprovalService.record

    def blocked(self: ApprovalService, *args: Any) -> Any:
        nonlocal entered_count
        with lock:
            entered_count += 1
            if entered_count == len(bodies):
                loop.call_soon_threadsafe(all_entered.set)
        release.wait(timeout=5.0)
        with write_lock:
            return original_record(self, *args)

    def one_writer(call: Callable[..., Any]) -> Callable[..., Any]:
        def serialized(self: ApprovalService, *args: Any) -> Any:
            with write_lock:
                return call(self, *args)

        return serialized

    monkeypatch.setattr(ApprovalService, "record", blocked)
    for stage in ("mark_delivering", "finish"):
        monkeypatch.setattr(ApprovalService, stage, one_writer(getattr(ApprovalService, stage)))
    callers = [asyncio.create_task(client.post(PATH, headers=WRITE, json=body)) for body in bodies]
    await asyncio.wait_for(all_entered.wait(), timeout=5.0)

    # all four intake workers are occupied: a fifth caller is refused, never queued
    over = await client.get(PATH, headers=AUTH)
    assert over.status_code == 429 and over.json()["error"]["code"] == "overloaded"

    for caller in callers:
        caller.cancel()
    for caller in callers:
        with pytest.raises(asyncio.CancelledError):
            await caller

    # the disconnect did not free a permit: still no room for a new caller
    still_over = await client.get(PATH, headers=AUTH)
    assert still_over.status_code == 429 and still_over.json()["error"]["code"] == "overloaded"

    release.set()
    await settle(app.state.approvals)
    for body in bodies:
        summary = await client.get(f"{PATH}/{body['choice_id']}", headers=AUTH)
        assert summary.json()["state"] == "delivered_locally"
    assert len(driver.answers) == 4

    # capacity frees only once every attempt has actually finished
    freed = await client.get(PATH, headers=AUTH)
    for _ in range(1000):
        if freed.status_code == 200:
            break
        await asyncio.sleep(0)
        freed = await client.get(PATH, headers=AUTH)
    assert freed.status_code == 200


@pytest.mark.parametrize("stage", ["record", "mark_delivering"])
async def test_shutdown_observes_inflight_commit_before_finalizing(
    rig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, stage, after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    shutdown = asyncio.create_task(app.state.approvals.close())
    await asyncio.sleep(0)
    assert not shutdown.done()  # the in-flight commit is observed before anything is finalized
    release.set()
    await shutdown
    assert (await caller).status_code == 202
    receipt = app.state.approvals.service.summary(CID)
    assert (receipt.state, receipt.reason) == ("not_delivered", "cancelled")
    assert card.answers == []


async def test_shutdown_during_finish_reports_the_state_that_committed(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`finish` is the third commit of an attempt and it is observed like the other two: a
    cancellation delivered while it is writing must not report the phase the row has left."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "finish", after=True)
    caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    shutdown = asyncio.create_task(app.state.approvals.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await shutdown
    answered = await caller
    assert answered.status_code == 202 and len(card.answers) == 1
    # The ledger says delivered; the receipt must say the same, not `unknown/interrupted`.
    assert app.state.approvals.service.summary(CID).state == "delivered_locally"
    assert (answered.json()["state"], answered.json()["reason"]) == ("delivered_locally", "")


async def test_a_busy_intent_is_retried_and_still_delivers_exactly_once(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`busy` is a lock collision, not a storage fault: the row is still `recorded` and the
    callback has not run, so a bounded retry replays nothing and saves the turn."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    monkeypatch.setattr(approval_runtime, "INTENT_BACKOFF_S", (0.0, 0.0))
    original = ApprovalService.mark_delivering
    calls: list[str] = []

    def collide(self: ApprovalService, choice_id: str) -> Any:
        calls.append(choice_id)
        if len(calls) == 1:
            raise approvals_module.ApprovalError("busy", StateError("busy").message)
        return original(self, choice_id)

    monkeypatch.setattr(ApprovalService, "mark_delivering", collide)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert len(calls) == 2
    assert response.status_code == 202
    assert (response.json()["state"], response.json()["reason"]) == ("delivered_locally", "")
    assert app.state.approvals.service.summary(CID).state == "delivered_locally"
    assert card.answers == [PermissionAnswer(allow=True)]


async def test_a_lasting_busy_intent_is_not_delivered_and_the_card_stays_unanswered(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry is bounded. When it runs out the card is dead — no second choice is ever
    accepted for the same native request — so this pins the cost of exhausting it."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    monkeypatch.setattr(approval_runtime, "INTENT_BACKOFF_S", (0.0, 0.0))
    calls: list[str] = []

    def busy(_self: ApprovalService, choice_id: str) -> Any:
        calls.append(choice_id)
        raise approvals_module.ApprovalError("busy", StateError("busy").message)

    monkeypatch.setattr(ApprovalService, "mark_delivering", busy)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert len(calls) == 3  # two backoffs, three attempts
    assert response.status_code == 202
    assert (response.json()["state"], response.json()["reason"]) == ("not_delivered", "storage")
    assert app.state.approvals.service.summary(CID).state == "not_delivered"
    assert card.answers == [] and card.driver.pending() == [TOOL]
    monkeypatch.undo()
    second = await client.post(PATH, headers=WRITE, json=card.body(choice_id=str(uuid.uuid4())))
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"


async def test_intent_failure_is_not_delivered_storage_and_never_leaks_its_cause(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intent that did not commit proves the callback never ran, so the
    ledger says `not_delivered/storage` — never `unknown`, which would claim it might have."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def failed(_self: ApprovalService, _choice_id: str) -> Any:
        # A locked store surfaces as sqlite3.OperationalError, not only as a typed refusal.
        raise OSError("PRIVATE exception must never reach HTTP or logs")

    monkeypatch.setattr(ApprovalService, "mark_delivering", failed)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == 202 and "PRIVATE" not in response.text
    assert (response.json()["state"], response.json()["reason"]) == ("not_delivered", "storage")
    summary = app.state.approvals.service.summary(CID)
    assert (summary.state, summary.reason) == ("not_delivered", "storage")
    assert card.answers == [] and card.driver.pending() == [TOOL]
    assert (await client.get(f"{PATH}/{CID}", headers=AUTH)).json() == response.json()


async def test_a_raising_callback_is_unknown_internal_and_keeps_the_cards_authority(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver may or may not have seen the answer, so `unknown` is the one
    honest reading. `not_delivered` here would deny a delivery that may have happened."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def failed(_self: _AnswerDriver, _tool_use_id: str, _answer: Answer) -> bool:
        raise RuntimeError("PRIVATE exception must never reach HTTP or logs")

    monkeypatch.setattr(_AnswerDriver, "respond", failed)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == 202 and "PRIVATE" not in response.text
    assert (response.json()["state"], response.json()["reason"]) == ("unknown", "internal")
    summary = app.state.approvals.service.summary(CID)
    assert (summary.state, summary.reason) == ("unknown", "internal")
    # The exception left the card's authority alone: the actor never marked it answered.
    assert card.actor.request_pending(TOOL, card.native)


async def test_finish_failure_projects_inactive_rows_as_interrupted(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def failed(*_args: Any) -> Any:
        raise OSError("PRIVATE exception must never reach HTTP or logs")

    monkeypatch.setattr(app.state.approvals.service, "finish", failed)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == 202 and "PRIVATE" not in response.text
    assert len(card.answers) == 1
    # The row keeps the phase it reached; every reader projects the same honest answer.
    assert app.state.approvals.service.summary(CID).state == "delivering"
    assert response.json()["state"] == "unknown" and response.json()["reason"] == "interrupted"
    assert (await client.get(f"{PATH}/{CID}", headers=AUTH)).json() == response.json()
    assert (await client.get(PATH, headers=AUTH)).json()["approvals"] == [response.json()]


async def test_decide_settles_a_stuck_attempt_as_internal_not_unavailable(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the attempt task ever finished without resolving the receipt (a hypothetical bug in
    `_attempt`), `decide()`'s own done callback must settle it as `internal`, never
    `unavailable`: `unavailable` asserts nothing was recorded, which this callback cannot know."""
    app, _client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    runner = app.state.approvals

    async def stuck(_value: Any, _receipt: Any) -> None:
        return  # finishes without ever touching the receipt

    monkeypatch.setattr(runner, "_attempt", stuck)
    with pytest.raises(approvals_module.ApprovalError) as excinfo:
        await runner.decide(
            approvals_module.ChoiceInput(
                choice_id=str(uuid.uuid4()),
                session_id=SID,
                generation=card.actor.generation,
                tool_use_id=card.envelope["data"]["tool_use_id"],
                native=approvals_module.NativeRequest(**card.native),
                choice=PermissionChoice(True),
            )
        )
    assert excinfo.value.code == "internal"


async def test_attempt_settles_as_internal_not_unavailable_when_nothing_committed(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `BaseException` that neither `except Exception` nor `except CancelledError` catches can
    escape the attempt before anything commits. The last-resort settle in `_attempt`'s own
    `finally` must still say `internal` ("could not classify itself"), never `unavailable`
    ("nothing was recorded" is a claim this branch is not in a position to make)."""
    app, _client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    class _Unclassifiable(BaseException):
        pass

    def explode(*_args: Any) -> Any:
        raise _Unclassifiable("neither Exception nor CancelledError")

    monkeypatch.setattr(manager, "get", explode)
    with pytest.raises(approvals_module.ApprovalError) as excinfo:
        await app.state.approvals.decide(
            approvals_module.ChoiceInput(
                choice_id=str(uuid.uuid4()),
                session_id=SID,
                generation=card.actor.generation,
                tool_use_id=card.envelope["data"]["tool_use_id"],
                native=approvals_module.NativeRequest(**card.native),
                choice=PermissionChoice(True),
            )
        )
    assert excinfo.value.code == "internal"


async def test_reopened_app_projects_previous_launch_rows_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _state, manager, drivers = assemble(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        card = await raise_card(manager, drivers)

        def failed(*_args: Any) -> Any:
            raise OSError("a launch that ended without finalizing its row")

        with monkeypatch.context() as patch:
            patch.setattr(app.state.approvals.service, "finish", failed)
            created = await client.post(PATH, headers=WRITE, json=card.body())
        assert created.status_code == 202 and len(card.answers) == 1
        body = card.body()
    reopened, _state, second_manager, second_drivers = assemble(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url=BASE
    ) as client:
        receipt = await client.get(f"{PATH}/{CID}", headers=AUTH)
        assert receipt.json()["state"] == "unknown" and receipt.json()["reason"] == "owner_changed"
        retry = await client.post(PATH, headers=WRITE, json=body)
        assert retry.status_code == 200 and retry.json() == receipt.json()
        assert not second_drivers and second_manager.get(SID) is None
    await reopened.state.approvals.close()


@pytest.mark.parametrize(
    "question,choice", [(True, ALLOW), (False, ANSWER)], ids=["question-card", "permission-card"]
)
async def test_choice_kind_must_match_the_pending_card(rig, question: bool, choice) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers, question=question)
    response = await client.post(PATH, headers=WRITE, json=card.body(choice))
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_choice"
    assert app.state.approvals.service.list().approvals == []
    assert card.driver.pending() == [TOOL] and card.answers == []


async def test_post_for_an_already_cancelled_card_is_stale_request_with_no_row(rig) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    card.driver._emit(RequestCancelled(tool_use_id=TOOL))
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == 409 and response.json()["error"]["code"] == "stale_request"
    assert app.state.approvals.service.list().approvals == []
    assert card.answers == []


async def test_racing_windows_record_once_without_an_interrupted_projection(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    entered, release = gate(monkeypatch, "record", after=True)
    first = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
    await entered.wait()
    # The row is committed and its attempt is live: a second window's lookup must see a
    # recorded choice, never the interrupted row of an attempt nobody owns.
    live = await client.get(card.lookup, headers=AUTH)
    assert live.status_code == 200 and live.json()["state"] == "recorded"
    second = await client.post(
        PATH, headers=WRITE, json=card.body(DENY, choice_id=str(uuid.uuid4()))
    )
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"
    release.set()
    assert (await first).status_code == 202
    await settle(app.state.approvals)
    assert len(card.answers) == 1
    assert len(app.state.approvals.service.list().approvals) == 1


async def test_duplicate_post_racing_the_first_commit_keeps_the_winners_claim(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_owned` counts claims rather than a bare set: a duplicate POST that races the first
    commit for the same `choice_id` must not drop the winner's claim when the loser releases its
    own. Fails if `_owned` becomes an uncounted set (the loser's release would then free the
    winner's still-live row for an interrupted projection)."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    both_in_record = threading.Barrier(2, timeout=5.0)
    intent = threading.Event()
    original_record = ApprovalService.record

    def record(self: ApprovalService, *args: Any) -> Any:
        # Neither commits until both have claimed. One of the two then waits for the other's
        # intent to commit, so the claims race but the two transactions never contend for the
        # store's lock: which of them answers `busy` would depend on the disk's commit time.
        if both_in_record.wait() == 0:
            intent.wait(timeout=5.0)
        return original_record(self, *args)

    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original_mark = ApprovalService.mark_delivering

    def mark(self: ApprovalService, choice_id: str) -> Any:
        result = original_mark(self, choice_id)
        intent.set()
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=5.0)
        return result

    monkeypatch.setattr(ApprovalService, "record", record)
    monkeypatch.setattr(ApprovalService, "mark_delivering", mark)
    body = card.body()
    first = asyncio.create_task(client.post(PATH, headers=WRITE, json=body))
    second = asyncio.create_task(client.post(PATH, headers=WRITE, json=dict(body)))
    await entered.wait()  # the winner committed its intent and is about to call the driver
    done, pending = await asyncio.wait(
        {first, second}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == 1 and len(pending) == 1  # only the loser has answered
    loser = done.pop().result()
    assert loser.status_code == 200, loser.text  # replay of the committed row
    # The loser released its own claim. The winner still holds one and is still delivering.
    live = await client.get(f"{PATH}/{CID}", headers=AUTH)
    release.set()
    assert (await pending.pop()).status_code == 202
    await settle(app.state.approvals)
    assert len(card.answers) == 1
    assert live.json()["state"] == "delivering", live.json()


@pytest.mark.parametrize("code,status", STATE_CODES)
async def test_storage_state_errors_refuse_with_their_own_code(
    rig, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def unavailable(*_args: Any) -> None:
        raise StateError(code)

    # _access turns a StateError into an ApprovalError with the same code, so these three reach
    # the route table from outside the approvals' own list and must still name a definite refusal.
    monkeypatch.setattr(approvals_module, "require_private_store", unavailable)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] == StateError(code).message
    assert card.answers == []
    monkeypatch.undo()
    assert app.state.approvals.service.list().approvals == []


@pytest.mark.parametrize(
    "headers,status", [({}, 401), (AUTH, 403), ({**AUTH, "origin": "https://foreign.invalid"}, 403)]
)
async def test_write_auth_origin_before_body(rig, headers, status) -> None:
    app, *_ = rig
    code, payload, consumed = await raw_post(app, headers=headers, chunks=[])
    assert code == status and consumed == 0 and TOKEN.encode() not in payload


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", PATH),
        ("HEAD", PATH),
        ("GET", f"{PATH}/{CID}"),
        ("GET", f"{PATH}/{CID}/choice"),
        ("GET", f"/api/v2/sessions/{SID}/approval"),
    ],
)
async def test_reads_require_auth_and_reject_foreign_origin(rig, method, path) -> None:
    _app, client, *_ = rig
    assert (await client.request(method, path)).status_code == 401
    assert (
        await client.request(method, path, headers={**AUTH, "origin": "https://foreign.invalid"})
    ).status_code == 403


# The oversized body is named rather than inlined: pytest puts a parameter's repr in
# PYTEST_CURRENT_TEST, and 64 KiB of it is longer than an environment variable may be.
_BODIES: dict[str, tuple[bytes, dict[str, str], int]] = {
    "null": (b"null", {}, 400),
    "not-utf8": (b"\xff", {}, 400),
    "array": (b"[]", {}, 400),
    "duplicate-key": (b'{"choice_id":"a","choice_id":"b"}', {}, 400),
    "oversized": (b" " * (approval_api.MAX_APPROVAL_BODY_BYTES + 1), {}, 413),
    "not-json": (b"{}", {"content-type": "text/plain"}, 415),
    "encoded": (b"{}", {"content-encoding": "gzip"}, 415),
}


@pytest.mark.parametrize("case", list(_BODIES))
async def test_invalid_oversized_and_wrong_representation_bodies(rig, case: str) -> None:
    app, client, *_ = rig
    content, headers, status = _BODIES[case]
    result = await client.post(
        PATH, headers={**WRITE, "content-type": "application/json", **headers}, content=content
    )
    assert result.status_code == status
    assert app.state.approvals.service.list().approvals == []


@pytest.mark.parametrize(
    "changes",
    [
        {"choice_id": CID.upper()},
        {"tool_use_id": 7},
        {"native_request": {"runtime_id": SID, "request_id": SID}},
        {"native_request": {"runtime_id": SID, "request_id": SID, "turn_id": "x"}},
        {"extra": "unreviewed"},
        {"choice": {"kind": "permission", "allow": "yes"}},
        {"choice": {"kind": "other"}},
    ],
)
async def test_exact_input_contract(rig, changes: dict[str, Any]) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)
    response = await client.post(PATH, headers=WRITE, json={**card.body(), **changes})
    assert response.status_code == 400
    assert app.state.approvals.service.list().approvals == []


@pytest.mark.parametrize(
    "query", ["?limit=0", "?limit=101", "?limit=1&limit=2", "?scope=other", "?after=bad"]
)
async def test_bounded_page_query(rig, query: str) -> None:
    _app, client, *_ = rig
    assert (await client.get(PATH + query, headers=AUTH)).status_code == 400


async def test_oversized_query_is_invalid_input_for_post_and_invalid_page_for_get(rig) -> None:
    """The 256-byte query bound answers one code per route kind, not one condition with two
    codes for the same failure: a write names its own input, a read names its read condition."""
    _app, client, *_ = rig
    long_query = "?" + "a" * 300
    posted = await client.post(PATH + long_query, headers=WRITE, json={})
    assert posted.status_code == 400 and posted.json()["error"]["code"] == "invalid_input"
    got = await client.get(PATH + long_query, headers=AUTH)
    assert got.status_code == 400 and got.json()["error"]["code"] == "invalid_page"


@pytest.mark.parametrize(
    "query",
    [
        "",
        f"?runtime_id={SID}",
        f"?runtime_id={SID}&request_id={SID}&extra=1",
        f"?runtime_id={SID}&runtime_id={SID}&request_id={SID}",
        f"?runtime_id=bad&request_id={SID}",
        f"?runtime_id={SID}&request_id={SID.upper()}",
    ],
)
async def test_lookup_requires_exact_two_uuid_query(rig, query: str) -> None:
    _app, client, *_ = rig
    response = await client.get(f"/api/v2/sessions/{SID}/approval{query}", headers=AUTH)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"


async def test_lookup_without_a_record_is_not_found_and_needs_no_actor(rig) -> None:
    _app, client, _state, manager, _drivers = rig
    query = f"?runtime_id={SID}&request_id={CID}"
    response = await client.get(f"/api/v2/sessions/{SID}/approval{query}", headers=AUTH)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert manager.get(SID) is None


async def test_failure_logs_name_only_types(
    rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def failed(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("PRIVATE approval marker choice-7d1e")

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(app.state.approvals.service, "lookup", failed)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == 503 and "PRIVATE" not in response.text
    assert "OSError" in caplog.text
    assert "PRIVATE" not in caplog.text and "choice-7d1e" not in caplog.text


@pytest.mark.parametrize("code,status", STATE_CODES)
async def test_typed_storage_refusals_log_their_code(
    rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: str, status: int
) -> None:
    """Every real storage or DPAPI failure arrives as an `ApprovalError`, which is the branch
    the log has to cover: without it the log stays empty while every card answer is refused."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def unavailable(*_args: Any) -> None:
        raise StateError(code)

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(approvals_module, "require_private_store", unavailable)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    assert response.status_code == status
    # A 4xx is the user's own answer being refused; only the storage side reaches the log.
    assert (code in caplog.text) is (status >= 500)
    assert "PRIVATE" not in caplog.text and StateError(code).message not in caplog.text


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


async def test_actual_body_limit_and_deadline(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    app, *_ = rig
    headers = {**WRITE, "content-type": "application/json", "content-length": "1"}
    oversized = [b" " * (approval_api.MAX_APPROVAL_BODY_BYTES + 1)]
    assert (await raw_post(app, headers=headers, chunks=oversized))[0] == 413
    monkeypatch.setattr(approval_api, "APPROVAL_BODY_TIMEOUT_S", 0.001)
    assert (await raw_post(app, headers=headers, chunks=[], slow=True))[0] == 408


async def test_protection_failure_after_the_commit_answers_with_a_receipt_not_a_refusal(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record() protects before the INSERT and decodes after the commit, so a decryption
    failure there is possibly-committed. A 4xx/5xx would tell the browser nothing was recorded
    and free the card for a second ID, which the per-request uniqueness rule then refuses."""
    app, client, _state, manager, drivers = rig
    card = await raise_card(manager, drivers)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "unprotect", fail)
    response = await client.post(PATH, headers=WRITE, json=card.body())
    monkeypatch.undo()
    assert response.status_code == 202 and response.json()["state"] == "not_delivered"
    assert card.answers == []  # an undecodable row has no body to deliver
    # A transition validates on plain columns, so the honest terminal state still commits; only
    # the decoded record finish() would have returned is lost with the body.
    summary = app.state.approvals.service.summary(CID)
    assert (summary.state, summary.reason) == ("not_delivered", "storage")
    second = await client.post(PATH, headers=WRITE, json=card.body(choice_id=str(uuid.uuid4())))
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"


async def test_lifespan_shutdown_settles_a_choice_the_app_is_still_delivering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan closes approval admission itself; without that the attempt would keep the
    driver's future alive past the point where the app stops owning the session."""
    app, _state, manager, drivers = assemble(tmp_path)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    closing = asyncio.Event()
    original_close = app.state.approvals.close

    async def traced_close() -> None:
        closing.set()
        # The real close cancels attempts before its first suspension.
        await original_close()

    monkeypatch.setattr(app.state.approvals, "close", traced_close)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        card = await raise_card(manager, drivers)
        entered, release = gate(monkeypatch, "mark_delivering", after=True)
        caller = asyncio.create_task(client.post(PATH, headers=WRITE, json=card.body()))
        await entered.wait()
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        try:
            await asyncio.wait_for(closing.wait(), timeout=HANG_GUARD_S)
        finally:
            release.set()
        await shutdown
        assert (await caller).status_code == 202
    receipt = app.state.approvals.service.summary(CID)
    assert (receipt.state, receipt.reason) == ("not_delivered", "cancelled")
    # close_all() then denies what is left. That denial is the app's, not the user's, and it
    # is deliberately not recorded.
    assert card.answers == [PermissionAnswer(allow=False)]
    assert len(app.state.approvals.service.list().approvals) == 1


async def test_lifespan_closes_approvals_before_manager_denies_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A choice already recorded must reach its own finalization before `close_all()` denies the
    cards still open, so `approvals.close()` must run first."""
    app, _state, manager, _drivers = assemble(tmp_path)
    order: list[str] = []
    original_approvals_close = app.state.approvals.close
    original_close_all = manager.close_all

    async def traced_approvals_close() -> None:
        order.append("approvals")
        await original_approvals_close()

    async def traced_close_all() -> None:
        order.append("manager")
        await original_close_all()

    monkeypatch.setattr(app.state.approvals, "close", traced_approvals_close)
    monkeypatch.setattr(manager, "close_all", traced_close_all)
    async with app.router.lifespan_context(app):
        pass
    assert order == ["approvals", "manager"]
