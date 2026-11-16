"""One recorded user choice, one local delivery attempt; HTTP clients do not own its lifetime.

The record and the intent to deliver commit before the driver's future is resolved, and the
delivered body is decoded from the committed row rather than from the request that carried it.
Nothing here replays a recorded choice: a row whose attempt is gone is projected at read time,
never retried.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields, replace
from typing import Any, Literal

from viki_agent.conversations.approvals import (
    ApprovalError,
    ApprovalService,
    Choice,
    ChoiceInput,
    ChoicePage,
    ChoiceRecord,
    ChoiceSummary,
    PermissionChoice,
    TerminalState,
    choice_kind,
)

# The same cancellation-safe commit observer durable commands use. A second copy of
# it would be a second set of rules for the same problem, so this one is shared deliberately.
from viki_agent.conversations.command_runtime import storage_call
from viki_agent.drivers.base import PermissionAnswer, QuestionAnswer
from viki_agent.session import CommandDispatchRejected, SessionActor, SessionManager

MAX_APPROVAL_RUNS = 64
# What one lock collision at step 5 is worth waiting out. Each attempt already waits the store's
# own busy budget (storage.BUSY_TIMEOUT_S), so three of them finish inside half a second.
INTENT_BACKOFF_S = (0.05, 0.1)

logger = logging.getLogger(__name__)

DispatchCode = Literal[
    "no_target", "stale_generation", "stale_request", "closed", "closing", "overloaded"
]
# The projected card item's event type, which is what the choice must be an answer to.
_CARD_KIND = {"permission.requested": "permission", "question.asked": "question"}
_SUMMARY_FIELDS = tuple(field.name for field in fields(ChoiceSummary))


class ApprovalDispatchRejected(RuntimeError):
    """An admission refusal where nothing was recorded, so the card stays answerable."""

    def __init__(self, code: DispatchCode) -> None:
        self.code = code
        super().__init__(code)


def answer_for(choice: Choice) -> PermissionAnswer | QuestionAnswer:
    """The driver answer for a committed choice, built exactly as the socket path built it.

    The answers map comes back from canonical JSON, so its keys are in sorted order rather than
    the browser's. Only the mapping is delivered, so that is invisible to claude; it does show
    in a repr, which is why this stays a plain construction with no reordering of its own.
    """
    if isinstance(choice, PermissionChoice):
        return PermissionAnswer(allow=choice.allow)
    return QuestionAnswer(answers=choice.answers, response=choice.response)


def _refusal(exc: BaseException) -> str:
    """Hard rule 8: a refusal is recorded as its code or its type name and nothing else."""
    return exc.code if isinstance(exc, ApprovalError) else type(exc).__name__


def _summary_of(record: ChoiceRecord) -> ChoiceSummary:
    """The metadata receipt of a decoded row: a `ChoiceRecord` is a summary plus its body."""
    return ChoiceSummary(**{name: getattr(record, name) for name in _SUMMARY_FIELDS})


def _observe_exception(future: asyncio.Future[Any]) -> None:
    # A disconnected caller may never retrieve its receipt. Never log the choice, the card or a
    # storage exception; the authenticated lookup by ID is the recovery channel.
    if not future.cancelled():
        future.exception()


class ApprovalRunner:
    def __init__(self, service: ApprovalService, manager: SessionManager) -> None:
        self.service = service
        self.manager = manager
        self._jobs: set[asyncio.Task[None]] = set()
        # Counted claims, not a set: a duplicate POST that races the first commit claims the
        # same ID and must not drop the winner's claim when it releases its own.
        self._owned: dict[str, int] = {}
        self._closing = False

    async def decide(self, value: ChoiceInput) -> tuple[ChoiceSummary, bool]:
        """Record, deliver once, and answer with the receipt and whether this call created it."""
        if self._closing:
            raise ApprovalDispatchRejected("closing")
        if len(self._jobs) >= MAX_APPROVAL_RUNS:
            raise ApprovalDispatchRejected("overloaded")
        receipt: asyncio.Future[tuple[ChoiceSummary, bool]] = (
            asyncio.get_running_loop().create_future()
        )
        receipt.add_done_callback(_observe_exception)
        job = asyncio.create_task(self._attempt(value, receipt))
        self._jobs.add(job)

        def done(task: asyncio.Task[None]) -> None:
            self._jobs.discard(task)
            if not receipt.done():
                # Cancellation before the coroutine's first step skips its finally block, so
                # the runner settles the waiter here instead of leaving shutdown to drain it.
                # Anything else that reaches here is a failure the attempt could not classify:
                # `internal` says so, where `unavailable` would claim nothing was recorded.
                receipt.set_exception(
                    ApprovalDispatchRejected("closing")
                    if task.cancelled()
                    else ApprovalError("internal")
                )
            _observe_exception(task)

        job.add_done_callback(done)
        # A vanished HTTP client or socket task cannot cancel a recorded choice's one attempt.
        return await asyncio.shield(receipt)

    async def _attempt(
        self, value: ChoiceInput, receipt: asyncio.Future[tuple[ChoiceSummary, bool]]
    ) -> None:
        """One delivery attempt, in the seven steps numbered below. Every terminal observation is
        committed before it is reported."""
        choice_id = value.choice_id
        native = {
            "runtime_id": value.native.runtime_id,
            "request_id": value.native.request_id,
            "turn_id": value.native.turn_id,
        }
        claimed = False
        created = False
        known: ChoiceSummary | None = None  # the newest committed row this attempt has seen
        delivered: Choice | None = None  # the committed body, never the requested one
        outcome: tuple[TerminalState, str] | None = None

        def observe_record(result: tuple[ChoiceRecord, bool]) -> None:
            nonlocal created, known, delivered
            record, created = result
            known, delivered = _summary_of(record), record.choice

        def observe_state(record: ChoiceRecord) -> None:
            nonlocal known
            known = _summary_of(record)

        def retired(actor: SessionActor) -> str | None:
            """Why this record is no longer worth delivering, or None while it still is."""
            if (
                self.manager.get(value.session_id) is not actor
                or actor.generation != value.generation
                or actor.command_context()["closed"]
            ):
                return "runtime_changed"
            if not actor.request_pending(value.tool_use_id, native):
                return "stale_request"
            return None

        try:
            # 1. An identical call replays its receipt; another ID for the same native request
            #    is refused here as well as inside record()'s transaction.
            existing = await storage_call(lambda: self.service.lookup(value))
            if existing is not None:
                receipt.set_result((self._recover(_summary_of(existing)), False))
                return
            try:
                await storage_call(
                    lambda: self.service.find_request(
                        value.session_id, value.native.runtime_id, value.native.request_id
                    )
                )
            except ApprovalError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise ApprovalError("already_decided")

            # 2. The target, on the loop, without an await: nothing may change between these.
            actor = self.manager.get(value.session_id)
            if actor is None:
                raise ApprovalDispatchRejected("no_target")
            if actor.generation != value.generation:
                raise ApprovalDispatchRejected("stale_generation")
            if actor.command_context()["closed"]:
                raise ApprovalDispatchRejected("closed")
            request = actor.displayed_request(value.tool_use_id, native)
            if request is None:
                raise ApprovalDispatchRejected("stale_request")
            if _CARD_KIND.get(request["type"]) != choice_kind(value.choice):
                raise ApprovalError("invalid_choice")

            # 3. Claim before the commit: until the runner owns the ID, a concurrent read could
            #    see the fresh row and project a live choice as an interrupted one. Over-claiming
            #    only suppresses that projection, which is why the claim is deliberately early.
            self._owned[choice_id] = self._owned.get(choice_id, 0) + 1
            claimed = True
            try:
                await storage_call(lambda: self.service.record(value, request), observe_record)
            except ApprovalError as exc:
                if exc.code != "protection":
                    raise
                # record() protects the document before the INSERT and decodes the row after the
                # commit, so this failure is possibly-committed. Refusing it would let the card
                # be answered again under a second ID; recover by ID instead.
                recovered = await storage_call(lambda: self._committed(choice_id))
                if recovered is None:
                    raise
                logger.warning("approval record undecodable: %s", _refusal(exc))
                known, created = recovered, True
                outcome = ("not_delivered", "storage")
                return
            if not created:
                return  # a concurrent identical call won; its attempt owns the delivery

            # 4. The record is only worth delivering to the same live card.
            if (reason := retired(actor)) is not None:
                outcome = ("not_delivered", reason)
                return

            # 5. Intent, then the same revalidation: an uncommitted intent proves the callback
            #    never ran, which is what makes a recovered `recorded` row provably undelivered.
            for delay in (*INTENT_BACKOFF_S, None):  # the last attempt has no retry left
                try:
                    await storage_call(
                        lambda: self.service.mark_delivering(choice_id), observe_state
                    )
                    break
                except Exception as exc:
                    # A `busy` is another writer holding the store past its busy budget, not a
                    # storage fault: the row is still `recorded` and the callback has not run,
                    # so asking again replays nothing. Everything else is terminal here, because
                    # a card the user can never answer again costs the whole turn.
                    if delay is None or not isinstance(exc, ApprovalError) or exc.code != "busy":
                        logger.warning("approval intent refused: %s", _refusal(exc))
                        outcome = ("not_delivered", "storage")
                        return
                await asyncio.sleep(delay)
                if (reason := retired(actor)) is not None:
                    outcome = ("not_delivered", reason)
                    return
            if (reason := retired(actor)) is not None:
                outcome = ("not_delivered", reason)
                return

            # 6. The single local callback, synchronous: `outcome` is set in every branch, so a
            #    cancellation seen with it unset is specific evidence of refusal before handoff.
            if delivered is None:
                # observe_record always sets this when the row it creates commits; reaching here
                # otherwise is a bug elsewhere in this attempt, never a delivery outcome, so the
                # callback is skipped and this can never read as delivered_locally.
                outcome = ("unknown", "internal")
                return
            try:
                accepted = actor.respond(
                    value.tool_use_id,
                    answer_for(delivered),
                    expected_generation=value.generation,
                    current=lambda: self.manager.get(value.session_id) is actor,
                    native_request=native,
                )
            except CommandDispatchRejected:
                # The actor's own guard refused before it touched the future.
                outcome = ("not_delivered", "runtime_changed")
            except Exception as exc:
                # The future may or may not have been resolved: the one honest `unknown`.
                logger.warning("approval delivery failed: %s", _refusal(exc))
                outcome = ("unknown", "internal")
            else:
                outcome = (
                    ("delivered_locally", "") if accepted else ("not_delivered", "stale_request")
                )
        except asyncio.CancelledError:
            if outcome is None:
                outcome = ("not_delivered", "cancelled")
            if not created and not receipt.done():
                receipt.set_exception(ApprovalDispatchRejected("closing"))
            raise
        except Exception as exc:
            # Nothing was recorded under this ID, so the caller gets a typed refusal. Once the
            # record exists, every later failure is a state in the receipt instead.
            if not created and not receipt.done():
                receipt.set_exception(exc)
        finally:
            # 7. Finalize, then release the claim last, so a failed finalization is answered
            #    with the same read-time projection every other reader would see. The release
            #    and the receipt live in their own finally: finalization awaits, and a second
            #    cancellation there must not leak the claim or leave the waiter hanging.
            try:
                if created and outcome is not None:
                    state, reason = outcome
                    try:
                        # The observer records the committed row before a cancellation delivered
                        # mid-write is re-raised, so the receipt never reports a phase older
                        # than the ledger's.
                        await storage_call(
                            lambda: self.service.finish(choice_id, state, reason), observe_state
                        )
                    except Exception as exc:
                        # A failed finalization leaves the row at its persisted phase for the
                        # read-time projection, which never claims delivery it did not observe.
                        logger.warning("approval finalize failed: %s", _refusal(exc))
            finally:
                if claimed:
                    self._release(choice_id)
                if not receipt.done():
                    if known is not None:
                        receipt.set_result((self._recover(known), created))
                    else:
                        # Nothing committed under this ID and no classified refusal: `internal`
                        # is the ambiguous code, never `unavailable`, which would assert that.
                        receipt.set_exception(ApprovalError("internal"))

    def _committed(self, choice_id: str) -> ChoiceSummary | None:
        """The row under this ID without decoding it, or None when nothing was committed."""
        try:
            return self.service.summary(choice_id)
        except ApprovalError as exc:
            if exc.code == "not_found":
                return None
            raise

    def _release(self, choice_id: str) -> None:
        remaining = self._owned[choice_id] - 1
        if remaining:
            self._owned[choice_id] = remaining
        else:
            del self._owned[choice_id]

    def _recover[T: ChoiceSummary](self, row: T) -> T:
        """The read-time projection of a nonterminal row no live attempt owns.

        `recorded` becomes not_delivered because intent commits strictly before the callback, so
        an uncommitted intent proves the callback never ran; `delivering` stays honestly unknown.
        """
        if row.choice_id in self._owned:
            return row
        if row.state == "recorded":
            return replace(row, state="not_delivered", reason="interrupted")
        if row.state == "delivering":
            return replace(row, state="unknown", reason="interrupted")
        return row

    async def get(self, choice_id: str) -> ChoiceRecord:
        return self._recover(await storage_call(lambda: self.service.get(choice_id)))

    async def summary(self, choice_id: str) -> ChoiceSummary:
        return self._recover(await storage_call(lambda: self.service.summary(choice_id)))

    async def list(self, *, after: str | None = None, limit: int = 50) -> ChoicePage:
        page = await storage_call(lambda: self.service.list(after=after, limit=limit))
        return replace(page, approvals=[self._recover(row) for row in page.approvals])

    async def find_request(
        self, session_id: str, runtime_id: str, request_id: str
    ) -> ChoiceSummary:
        return self._recover(
            await storage_call(
                lambda: self.service.find_request(session_id, runtime_id, request_id)
            )
        )

    async def close(self) -> None:
        self._closing = True
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
