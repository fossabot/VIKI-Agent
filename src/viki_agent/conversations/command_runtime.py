"""One admitted native attempt per durable command; HTTP clients do not own its lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from anyio.to_thread import run_sync

from viki_agent.conversations.commands import (
    CommandError,
    CommandInput,
    CommandPage,
    CommandRecord,
    CommandService,
    CommandSummary,
    TerminalState,
)
from viki_agent.session import CommandDispatchRejected, SessionManager
from viki_agent.turn_input import TurnInput

MAX_COMMAND_RUNS = 64


async def storage_call[T](call: Callable[[], T], observe: Callable[[T], None] | None = None) -> T:
    """Observe a commit even if shutdown cancels its waiter while the thread is writing.

    Cancellation is delivered after the result observer updates the attempt's durable phase.
    A second cancellation must not abandon the first write or race its final status write.
    """
    worker = asyncio.create_task(run_sync(call))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
            cancelled = True
    if observe is not None:
        observe(result)
    if cancelled:
        raise asyncio.CancelledError
    return result


def _observe_exception(future: asyncio.Future[Any]) -> None:
    # A disconnected caller may never retrieve its receipt. Never log submitted text or
    # storage exceptions; authenticated lookup is the recovery channel.
    if not future.cancelled():
        future.exception()


class CommandRunner:
    def __init__(self, service: CommandService, manager: SessionManager) -> None:
        self.service = service
        self.manager = manager
        self._jobs: dict[asyncio.Task[None], CommandInput] = {}
        self._owned: set[str] = set()
        self._closing = False

    async def submit(
        self,
        value: CommandInput,
        *,
        admitted: Callable[[], bool] | None = None,
        finished: Callable[[TerminalState], None] | None = None,
    ) -> tuple[CommandRecord, bool]:
        return await self._submit(
            value, lambda: self.service.accept(value), admitted=admitted, finished=finished
        )

    async def _submit(
        self,
        value: CommandInput,
        accept_record: Callable[[], tuple[CommandRecord, bool]],
        *,
        admitted: Callable[[], bool] | None = None,
        finished: Callable[[TerminalState], None] | None = None,
    ) -> tuple[CommandRecord, bool]:
        if self._closing:
            raise CommandDispatchRejected("closing")
        if len(self._jobs) >= MAX_COMMAND_RUNS:
            raise CommandDispatchRejected("overloaded")
        receipt: asyncio.Future[tuple[CommandRecord, bool]] = (
            asyncio.get_running_loop().create_future()
        )
        receipt.add_done_callback(_observe_exception)
        job = asyncio.create_task(self._attempt(value, receipt, accept_record, admitted, finished))
        self._jobs[job] = value

        def done(task: asyncio.Task[None]) -> None:
            self._jobs.pop(task, None)
            if not receipt.done():
                # Cancellation before the coroutine's first step skips its finally block.
                # The runner must still settle the HTTP waiter so shutdown can drain it.
                receipt.set_exception(
                    CommandDispatchRejected("closing")
                    if task.cancelled()
                    else CommandError("unavailable")
                )
            _observe_exception(task)

        job.add_done_callback(done)
        # A vanished HTTP client cannot cancel an accepted send or release its run slot.
        return await asyncio.shield(receipt)

    async def _attempt(
        self,
        value: CommandInput,
        receipt: asyncio.Future[tuple[CommandRecord, bool]],
        accept_record: Callable[[], tuple[CommandRecord, bool]],
        admitted: Callable[[], bool] | None = None,
        finished: Callable[[TerminalState], None] | None = None,
    ) -> None:
        accepted = False
        intent = False
        status: TerminalState = "not_dispatched"
        reason = "internal"

        def accepted_receipt(result: tuple[CommandRecord, bool]) -> None:
            nonlocal accepted
            record, accepted = result
            if accepted:
                self._owned.add(value.command_id)
            else:
                record = self._recover(record)
            if not receipt.done():
                receipt.set_result((record, accepted))

        async def accept() -> TurnInput | None:
            if self._closing:
                raise CommandDispatchRejected("closing")
            check_target()
            record, created = await storage_call(accept_record, accepted_receipt)
            check_target()
            return record.turn_input if created else None

        def committed_intent(_record: CommandRecord) -> None:
            nonlocal intent
            intent = True

        async def dispatch_intent() -> None:
            nonlocal intent
            check_target()
            await storage_call(
                lambda: self.service.mark_dispatching(value.command_id), committed_intent
            )
            try:
                check_target()
            except CommandDispatchRejected:
                # Refused before the driver call, synchronously: the native send never ran,
                # so this attempt is certainly not_dispatched, not unknown.
                intent = False
                raise

        def check_target() -> None:
            if admitted is not None and not admitted():
                raise CommandDispatchRejected("no_target")
            if self.manager.get(value.session_id) is not actor:
                raise CommandDispatchRejected("stale_generation")

        try:
            existing = await storage_call(lambda: self.service.lookup(value))
            if existing is not None:
                receipt.set_result((self._recover(existing), False))
                return
            actor = self.manager.get(value.session_id)
            if actor is None:
                raise CommandDispatchRejected("no_target")
            result = await actor.send_command(
                expected_generation=value.generation,
                accept=accept,
                dispatch_intent=dispatch_intent,
            )
            if result.status != "duplicate":
                status, reason = result.status, result.reason or "internal"
        except asyncio.CancelledError:
            status, reason = ("unknown" if intent else "not_dispatched"), "cancelled"
            if not receipt.done():
                receipt.set_exception(CommandDispatchRejected("closing"))
        except CommandDispatchRejected as exc:
            status = "unknown" if intent else "not_dispatched"
            reason = "runtime_changed"
            if not receipt.done():
                try:
                    # Two initial lookups can both miss before one caller commits. The
                    # second then waits on the actor lock and sees busy/closed/replaced.
                    # Recover the winner's receipt before refusing a duplicate admission.
                    existing = await storage_call(lambda: self.service.lookup(value))
                    if existing is not None:
                        receipt.set_result((self._recover(existing), False))
                    else:
                        receipt.set_exception(exc)
                except Exception as failure:
                    receipt.set_exception(failure)
        except Exception as exc:
            status = "unknown" if intent else "not_dispatched"
            if not receipt.done():
                receipt.set_exception(exc)
        finally:
            if not receipt.done():
                receipt.set_exception(CommandError("unavailable"))
            if accepted:
                try:
                    await storage_call(
                        lambda: self.service.finish(value.command_id, status, reason)
                    )
                except Exception:
                    # The persisted phase is still recoverable. An inactive nonterminal
                    # record is projected as unknown below; it is never put back on a queue.
                    pass
                finally:
                    self._owned.discard(value.command_id)
                    if finished is not None:
                        finished(status)

    def _recover(self, record: CommandRecord) -> CommandRecord:
        if record.state in {"accepted", "dispatching"} and record.command_id not in self._owned:
            return replace(record, state="unknown", reason="internal")
        return record

    async def get(self, command_id: str) -> CommandRecord:
        record = await storage_call(lambda: self.service.get(command_id))
        return self._recover(record)

    async def list(self, *, after: str | None = None, limit: int = 50) -> CommandPage:
        page = await storage_call(lambda: self.service.list(after=after, limit=limit))
        return replace(
            page,
            commands=[
                replace(row, state="unknown", reason="internal")
                if row.state in {"accepted", "dispatching"} and row.command_id not in self._owned
                else row
                for row in page.commands
            ],
        )

    async def find_draft_command(self, session_id: str, draft_revision: int) -> CommandSummary:
        summary = await storage_call(
            lambda: self.service.find_draft_command(session_id, draft_revision)
        )
        if summary.state in {"accepted", "dispatching"} and summary.command_id not in self._owned:
            return replace(summary, state="unknown", reason="internal")
        return summary

    async def _stop_jobs(self, jobs: list[asyncio.Task[None]]) -> None:
        for job in jobs:
            if not job.done() and not job.cancelling():
                job.cancel()
        if jobs:
            waiter = asyncio.gather(*jobs, return_exceptions=True)
            cancelled = False
            while True:
                try:
                    await asyncio.shield(waiter)
                    break
                except asyncio.CancelledError:
                    if waiter.cancelled():
                        raise
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def stop(self, value: CommandInput) -> None:
        await self._stop_jobs([job for job, submitted in self._jobs.items() if submitted == value])

    async def close(self) -> None:
        self._closing = True
        await self._stop_jobs(list(self._jobs))
