"""The replay engine's own guarantees: what it answers itself, and what it must not do once the
fake has exited. Every other suite trusts this double, so its edges are worth pinning here."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from replay import FakeSpawner, Frame, Replayer, init_frame, out, result_frame

TOUCH: Frame = {"type": "fake.touch", "name": "shared.txt", "text": "from the session\n"}
SLEEPING = [out(init_frame()), out({"type": "fake.sleep", "s": 0.2}), out(result_frame())]


class Clock:
    """Stands in for `loop.call_later`: records what a fake.sleep scheduled instead of waiting,
    so the test fires the continuation by hand and never sleeps."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def schedule(self, _delay: float, callback: Callable[[], None]) -> object:
        self.pending.append(callback)
        return None

    def fire(self) -> None:
        self.pending.pop()()


def _replayer(clock: Clock, writes: list[bytes], errs: list[bytes]) -> Replayer:
    return Replayer(
        SLEEPING,
        out=writes.append,
        err=errs.append,
        exit=lambda _code: None,
        schedule=clock.schedule,
    )


def _types(writes: list[bytes]) -> list[str]:
    return [json.loads(line)["type"] for line in writes]


def test_a_pending_fake_sleep_emits_the_rest_of_the_script_when_it_fires() -> None:
    clock = Clock()
    writes: list[bytes] = []
    errs: list[bytes] = []
    replayer = _replayer(clock, writes, errs)

    replayer.boot()
    assert _types(writes) == ["system"]  # the result frame waits behind the sleep

    clock.fire()
    assert _types(writes) == ["system", "result"]
    assert errs == []


def test_a_fake_sleep_that_fires_after_the_fake_exited_writes_nothing() -> None:
    """The in-process fake wires `out` to `StreamReader.feed_data` and exits by calling
    `feed_eof()`. A test that closes stdin mid-sleep and then keeps the loop running used to have
    the pending timer write anyway, tripping asyncio's `assert not self._eof` inside a timer
    callback — logged by the loop's exception handler, failing nothing, and leaving the rest of
    the script silently unplayed."""
    clock = Clock()
    writes: list[bytes] = []
    errs: list[bytes] = []
    replayer = _replayer(clock, writes, errs)

    replayer.boot()
    replayer.stdin_closed()  # what _FakeStdin.close does, while the sleep is still pending
    assert replayer.exited

    clock.fire()
    assert _types(writes) == ["system"]
    assert errs == []


def test_an_unscripted_get_context_usage_answers_in_the_shape_the_cli_uses() -> None:
    """`driver._context_percent` does `int(percentage)`, so a fractional default reported 0 on
    every fake-driven turn. The real CLI reply carries the percentage as an integer."""
    writes: list[bytes] = []
    replayer = Replayer([], out=writes.append, err=writes.append, exit=lambda _code: None)

    replayer.feed(
        {
            "type": "control_request",
            "request_id": "req_1",
            "request": {"subtype": "get_context_usage"},
        }
    )

    usage = json.loads(writes[-1])["response"]["response"]
    assert isinstance(usage["percentage"], int)  # a float here is what int() rounded away
    assert usage["percentage"] > 0


def _touching(frame: Frame, cwd: Path | None) -> tuple[Replayer, list[bytes]]:
    writes: list[bytes] = []
    replayer = Replayer(
        [out(frame)], out=writes.append, err=writes.append, exit=lambda _code: None, cwd=cwd
    )
    return replayer, writes


def test_fake_touch_writes_the_text_bytes_into_the_cwd_and_emits_nothing(tmp_path: Path) -> None:
    replayer, writes = _touching(TOUCH, tmp_path)

    replayer.boot()

    assert (tmp_path / "shared.txt").read_bytes() == b"from the session\n"  # LF, not CRLF
    assert writes == []


@pytest.mark.parametrize("name", ["a/b.txt", "a\\b.txt", "c:b.txt", ".."])
def test_fake_touch_refuses_a_name_that_leaves_the_cwd(tmp_path: Path, name: str) -> None:
    replayer, _writes = _touching({**TOUCH, "name": name}, tmp_path)

    with pytest.raises(RuntimeError):
        replayer.boot()

    assert list(tmp_path.iterdir()) == []


def test_fake_touch_refuses_without_a_cwd() -> None:
    replayer, _writes = _touching(TOUCH, None)

    with pytest.raises(RuntimeError):
        replayer.boot()


async def test_a_fake_spawner_hands_its_cwd_to_the_replayer(tmp_path: Path) -> None:
    spawner = FakeSpawner([out(TOUCH)])

    await spawner(["claude"], tmp_path, {})

    assert (tmp_path / "shared.txt").read_bytes() == b"from the session\n"
