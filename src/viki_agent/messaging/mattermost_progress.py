"""Schedules the best-effort Mattermost progress calls of a chat turn, one call at a time.

A call that fails ends that turn's progress and is never retried; nothing here raises into the
caller. The bridge says when the chat is `idle` and wakes the worker when that changes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial

from .mattermost_http import ProgressHttp
from .remote_status import Outcome

STATUS_INTERVAL_S = 20.0
TYPING_INTERVAL_S = 5.0
MAX_TYPING_CALLS = 360
MAX_SEEN = 8
MAX_CLOSING = 4
_MAX_MARKED = 64

_OUTCOME_EMOJI = {
    "done": "white_check_mark",
    "error": "warning",
    "stopped": "black_square_for_stop",
}


@dataclass
class _Turn:
    thread: str
    prompt: str | None
    typing_due: float
    status_due: float
    eyes: deque[str] = field(default_factory=deque)
    typing_calls: int = 0
    status_id: str | None = None
    status_text: str | None = None
    failed: bool = False


@dataclass
class _Closing:
    turn: _Turn
    outcome: Outcome
    final: str
    calls: deque[Callable[[], Awaitable[None]]] | None = None


class MattermostProgress:
    def __init__(
        self,
        http: ProgressHttp,
        *,
        line: Callable[[], str],
        waiting: Callable[[], bool],
        idle: Callable[[], bool],
        clock: Callable[[], float],
        status_interval_s: float = STATUS_INTERVAL_S,
        typing_interval_s: float = TYPING_INTERVAL_S,
    ) -> None:
        self._http = http
        self._line = line
        self._waiting = waiting
        self._idle = idle
        self._clock = clock
        self._status_interval_s = status_interval_s
        self._typing_interval_s = typing_interval_s
        self._turn: _Turn | None = None
        self._closing: deque[_Closing] = deque(maxlen=MAX_CLOSING)
        self._accepted: deque[str] = deque()
        self._marked: deque[str] = deque(maxlen=_MAX_MARKED)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def turn_started(self, thread: str, prompt: str | None) -> None:
        now = self._clock()
        turn = _Turn(thread, prompt, now, now + self._status_interval_s)
        if prompt is not None and prompt not in self._accepted and prompt not in self._marked:
            turn.eyes.append(prompt)
        self._turn = turn
        self._wake.set()

    def accepted(self, post_id: str) -> None:
        if (
            len(self._accepted) < MAX_SEEN
            and post_id not in self._accepted
            and post_id not in self._marked
        ):
            self._accepted.append(post_id)
            self._wake.set()

    def turn_ended(self, outcome: Outcome, final: str, prompt: str | None = None) -> None:
        """Close the open turn; with none open, close `prompt` alone (a stop before its turn
        started). Its closing calls are computed only when the worker reaches them, after any 👀
        call in flight has landed."""
        turn, self._turn = self._turn, None
        if turn is None and prompt is not None:
            turn = _Turn("", prompt, 0.0, 0.0)
        if turn is not None:
            self._closing.append(_Closing(turn, outcome, final))
            self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._http.close()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            delay = await self.step()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def step(self) -> float | None:
        """Make every call that is due, one at a time; return the seconds to the next one."""
        while self._ask(self._idle, False):
            now = self._clock()
            if (
                await self._closing_call()
                or await self._eyes_call()
                or await self._typing_call(now)
                or await self._status_call(now)
            ):
                continue
            return self._delay(now)
        return None

    @staticmethod
    def _ask(question: Callable[[], bool], default: bool) -> bool:
        try:
            return question()
        except Exception:
            return default

    async def _closing_call(self) -> bool:
        while self._closing:
            closing = self._closing[0]
            if closing.calls is None:
                closing.calls = self._closing_calls(closing)
            if not closing.calls:
                self._closing.popleft()
                continue
            try:
                await closing.calls.popleft()()
            except Exception:
                closing.calls.clear()
            return True
        return False

    def _closing_calls(self, closing: _Closing) -> deque[Callable[[], Awaitable[None]]]:
        calls: deque[Callable[[], Awaitable[None]]] = deque()
        # Read the id now: a status create still in flight when the turn ended has set it by then.
        status_id = closing.turn.status_id
        if status_id is not None:
            calls.append(partial(self._http.edit, status_id, closing.final))
        prompt = closing.turn.prompt
        if prompt is not None:
            if prompt in self._accepted:
                self._accepted.remove(prompt)
            if prompt in self._marked:
                self._marked.remove(prompt)
                calls.append(partial(self._http.unreact, prompt))
            calls.append(partial(self._http.react, prompt, _OUTCOME_EMOJI[closing.outcome]))
        return calls

    async def _eyes_call(self) -> bool:
        turn = self._turn
        if self._accepted:
            post_id = self._accepted.popleft()
            with contextlib.suppress(Exception):
                await self._http.react(post_id, "eyes")
                self._marked.append(post_id)
            return True
        if turn is None or turn.failed or not turn.eyes:
            return False
        post_id = turn.eyes.popleft()
        try:
            await self._http.react(post_id, "eyes")
            self._marked.append(post_id)
        except Exception:
            turn.failed = True
        return True

    async def _typing_call(self, now: float) -> bool:
        turn = self._turn
        if (
            turn is None
            or turn.failed
            or turn.typing_calls >= MAX_TYPING_CALLS
            or now < turn.typing_due
            or self._ask(self._waiting, True)
        ):
            return False
        turn.typing_calls += 1
        turn.typing_due = now + self._typing_interval_s
        try:
            await self._http.typing(turn.thread)
        except Exception:
            turn.failed = True
        return True

    async def _status_call(self, now: float) -> bool:
        turn = self._turn
        if turn is None or turn.failed or now < turn.status_due:
            return False
        turn.status_due = now + self._status_interval_s
        try:
            text = self._line()
            if turn.status_id is None:
                turn.status_id = await self._http.create(text, turn.thread)
                turn.status_text = text
            elif text != turn.status_text:
                await self._http.edit(turn.status_id, text)
                turn.status_text = text
        except Exception:
            turn.failed = True
        return True

    def _delay(self, now: float) -> float | None:
        turn = self._turn
        if turn is None or turn.failed:
            return None
        due = [turn.status_due]
        if turn.typing_calls < MAX_TYPING_CALLS and not self._ask(self._waiting, True):
            due.append(turn.typing_due)
        return max(0.0, min(due) - now)
