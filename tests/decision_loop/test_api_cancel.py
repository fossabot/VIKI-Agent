"""A Decision request cancelled while its service call waits. The call runs to its end under
`asyncio.shield` and keeps its permit until then; whether it then fails or answers, nothing
reaches the event loop's exception handler and no log line holds a failure's text, because the
admitted task maps each failure to its response itself."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest

from tests.decision_loop.test_api import CAPACITY, answered, call, opened, refusal, refused_with
from tests.decision_loop.test_service import HANG_GUARD_S, KEY
from viki_agent.decision_loop import api
from viki_agent.decision_loop.contracts import DecisionLoopError
from viki_agent.decision_loop.missions import MissionError
from viki_agent.storage import StateError


class HeldPage:
    """The service method GET page awaits. A call takes `error` and `gate` as they stand when it
    starts, waits at that gate when there is one, counts itself in `ended`, then raises the error
    or answers. `full` is set once `hold`'s count of calls wait at its gate."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.gate: asyncio.Event | None = None
        self.full = asyncio.Event()
        self.expected = 0
        self.waiting = 0
        self.ended = 0

    def hold(self, count: int) -> asyncio.Event:
        """Hold the calls that start from now at a new gate, which it returns."""
        self.gate, self.full = asyncio.Event(), asyncio.Event()
        self.expected, self.waiting = count, 0
        return self.gate

    async def page(self) -> dict[str, Any]:
        error, gate = self.error, self.gate
        if gate is not None:
            self.waiting += 1
            if self.waiting == self.expected:
                self.full.set()
            await gate.wait()
        self.ended += 1
        if error is not None:
            raise error
        return {"answered": "page"}


# How the held call ends after its request is cancelled: the failure it raises (None: it answers),
# the response the admitted task maps that end to, and the routes' own log lines (one fixed line,
# for the unexpected failure alone, whose text holds the key).
Outcome = tuple[Callable[[], Exception] | None, int, dict[str, Any], list[str]]
OUTCOMES: dict[str, Outcome] = {
    "an answer": (None, 200, {"answered": "page"}, []),
    "an unexpected failure": (
        lambda: RuntimeError(f"agenda failed near {KEY}"),
        503,
        refusal("unavailable"),
        ["decision route failed"],
    ),
    "a store failure": (lambda: StateError("busy"), 503, refusal("unavailable"), []),
    "a loop refusal": (
        lambda: DecisionLoopError("not_applicable"),
        409,
        refusal("not_applicable"),
        [],
    ),
    "a mission refusal": (lambda: MissionError("busy"), 429, refusal("busy"), []),
}


@pytest.mark.parametrize("outcome", OUTCOMES.values(), ids=list(OUTCOMES))
async def test_a_cancelled_requests_call_runs_to_its_end_and_reports_nothing(
    outcome: Outcome, caplog: pytest.LogCaptureFixture
) -> None:
    raised, status, body, lines = outcome
    stub = HeldPage(None if raised is None else raised())
    first = stub.hold(1)
    workers: set[asyncio.Task[Any]] = set()
    loop = asyncio.get_running_loop()
    reports: list[dict[str, Any]] = []

    def report(running: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        reports.append(context)
        running.default_exception_handler(context)  # logged as well, so caplog sees its text

    previous = loop.get_exception_handler()
    loop.set_exception_handler(report)
    try:
        with caplog.at_level(logging.DEBUG):
            async with opened(stub, workers) as client:
                request = asyncio.create_task(call(client, "GET", "page"))
                await asyncio.wait_for(stub.full.wait(), HANG_GUARD_S)
                [worker] = workers
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert workers == {worker} and not worker.done()  # the shield: its call goes on

                # The call keeps its permit: three more requests fill the house, a fifth is busy.
                stub.error = None
                rest = stub.hold(CAPACITY - 1)
                held = [
                    asyncio.create_task(call(client, "GET", "page")) for _ in range(CAPACITY - 1)
                ]
                await asyncio.wait_for(stub.full.wait(), HANG_GUARD_S)
                stub.gate = None  # a call admitted from now on answers at once
                refused_with(await call(client, "GET", "page"), 429, refusal("busy"))

                first.set()  # the call ends after its request's cancel
                await asyncio.wait({worker}, timeout=HANG_GUARD_S)
                # Nothing reached the loop's exception handler, and the one log line is the
                # routes' fixed line for an unexpected failure.
                assert reports == []
                kept = [record for record in caplog.records if record.name != "httpx"]
                assert [(record.name, record.levelno, record.getMessage()) for record in kept] == [
                    (api.__name__, logging.WARNING, line) for line in lines
                ]
                assert KEY not in caplog.text
                # The call ran to its end, and the admitted task ended with its mapped response.
                assert stub.ended == 1 and not worker.cancelled()
                assert worker.exception() is None
                mapped = worker.result()
                assert (mapped.status_code, json.loads(bytes(mapped.body))) == (status, body)

                # Its end gave the permit back: a fourth request is admitted beside the three held.
                answered(await call(client, "GET", "page"))
                rest.set()
                for response in await asyncio.gather(*held):
                    answered(response)
            assert not workers
    finally:
        loop.set_exception_handler(previous)
