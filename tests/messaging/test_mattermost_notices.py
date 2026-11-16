"""Synthetic receipt and cancellation checks for bounded outbound Bot notices."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_mattermost_bot import BINDING, Rig, take
from viki_agent.messaging.mattermost_bot import _STOP_LINES
from viki_agent.messaging.mattermost_format import MAX_POST_BYTES, chat_text
from viki_agent.messaging.mattermost_http import Binding


async def allowed() -> bool:
    return True


async def checkpoint() -> None:
    resumed = asyncio.Event()
    asyncio.get_running_loop().call_soon(resumed.set)
    await resumed.wait()


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    value = Rig(tmp_path)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()


def destination(rig: Rig) -> str:
    key = rig.bot.destination_key()
    assert key is not None
    return key


async def test_destination_only_after_confirmed_ready_and_excludes_token(rig: Rig) -> None:
    original = destination(rig)
    expected = json.dumps(
        [rig.bot.config.url, BINDING.bot_user_id, BINDING.owner_user_id, BINDING.channel_id],
        separators=(",", ":"),
    )
    assert original == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    rig.bot.config = dataclasses.replace(rig.bot.config, bot_token="other-synthetic-token")
    assert destination(rig) == original
    rig.bot.config = dataclasses.replace(rig.bot.config, url="https://example.test/team")
    assert destination(rig) != original
    rig.bot._ready_sent = False
    assert rig.bot.destination_key() is None
    rig.bot._ready_sent = True
    rig.bot.disarm()
    assert rig.bot.destination_key() is None


async def test_notice_receipt_waits_for_http_and_escapes_once(rig: Rig) -> None:
    rig.http.gate.clear()
    task = asyncio.create_task(
        rig.bot.send_notice(
            "A @all ` <tag> \u202e", destination_key=destination(rig), current=allowed
        )
    )
    message, root = await take(rig.http.sent)
    assert message == chat_text("A @all ` <tag> \u202e") and root == ""
    assert not task.done()
    assert rig.bot._actor is None and not rig.bot._cards and rig.created.empty()
    rig.http.gate.set()
    assert await task == "sent"
    assert rig.bot._output_count == rig.bot._output_bytes == 0


@pytest.mark.parametrize("ready", [False, True])
async def test_unready_or_wrong_destination_refuses_before_guard(rig: Rig, ready: bool) -> None:
    key = destination(rig)
    rig.bot._ready_sent = ready

    async def forbidden() -> bool:
        pytest.fail("Unverified destination must not reach the guard")

    assert (
        await rig.bot.send_notice(
            "Private result", destination_key=key if not ready else "0" * 64, current=forbidden
        )
        == "unavailable"
    )
    assert rig.http.sent.empty()


@pytest.mark.parametrize("result", [False, None, 1])
async def test_guard_requires_exact_true(rig: Rig, result: Any) -> None:
    async def guard() -> Any:
        return result

    assert (
        await rig.bot.send_notice("Held result", destination_key=destination(rig), current=guard)
        == "unavailable"
    )
    assert rig.http.sent.empty() and rig.bot._armed


async def test_guard_failure_is_definite_and_does_not_disarm_bridge(rig: Rig) -> None:
    async def guard() -> bool:
        raise ValueError("Private implementation detail")

    assert (
        await rig.bot.send_notice("Private result", destination_key=destination(rig), current=guard)
        == "failed"
    )
    assert rig.http.sent.empty() and rig.bot._armed
    assert (
        await rig.bot.send_notice("Next result", destination_key=destination(rig), current=allowed)
        == "sent"
    )


@pytest.mark.parametrize("changed", ["epoch", "destination", "prefix", "ready", "disarm"])
async def test_writer_rechecks_binding_and_epoch_after_async_guard(rig: Rig, changed: str) -> None:
    async def guard() -> bool:
        if changed == "epoch":
            rig.bot._epoch += 1
        elif changed == "destination":
            rig.bot._binding = Binding("b" * 26, "o" * 26, "d" * 26)
        elif changed == "prefix":
            rig.bot.config = dataclasses.replace(rig.bot.config, url="https://example.test/other")
        elif changed == "ready":
            rig.bot._ready_sent = False
        else:
            rig.bot.disarm()
        return True

    assert (
        await rig.bot.send_notice("Revoked result", destination_key=destination(rig), current=guard)
        == "unavailable"
    )
    assert rig.http.sent.empty()


async def test_disarm_resolves_notice_waiting_in_guard(rig: Rig) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def guard() -> bool:
        entered.set()
        await release.wait()
        return True

    task = asyncio.create_task(
        rig.bot.send_notice("Queued result", destination_key=destination(rig), current=guard)
    )
    async with asyncio.timeout(0.05):
        await entered.wait()
    rig.bot.disarm()
    async with asyncio.timeout(0.05):
        assert await task == "unavailable"
    assert await take(rig.http.sent) == (chat_text(_STOP_LINES["connection_lost"]), "")
    assert rig.http.sent.empty()


async def test_disarm_resolves_queued_and_dispatched_receipts_distinctly(rig: Rig) -> None:
    key = destination(rig)
    rig.http.gate.clear()
    first = asyncio.create_task(rig.bot.send_notice("First", destination_key=key, current=allowed))
    await take(rig.http.sent)
    second = asyncio.create_task(
        rig.bot.send_notice("Second", destination_key=key, current=allowed)
    )
    await checkpoint()
    assert rig.bot._output_count == 2
    rig.bot.disarm()
    async with asyncio.timeout(0.05):
        assert await first == "unknown"
        assert await second == "unavailable"
    rig.http.gate.set()
    assert await take(rig.http.sent) == (chat_text(_STOP_LINES["connection_lost"]), "")
    assert rig.http.sent.empty()


@pytest.mark.parametrize("waiting_in_guard", [False, True])
async def test_caller_cancellation_cannot_send_a_queued_notice(
    rig: Rig, waiting_in_guard: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def guard() -> bool:
        entered.set()
        if waiting_in_guard:
            await release.wait()
        return True

    if not waiting_in_guard:
        rig.http.gate.clear()
        rig.bot._reply("Existing output", "")
        await take(rig.http.sent)
    task = asyncio.create_task(
        rig.bot.send_notice("Cancelled result", destination_key=destination(rig), current=guard)
    )
    if waiting_in_guard:
        async with asyncio.timeout(0.05):
            await entered.wait()
    else:
        await checkpoint()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    rig.http.gate.set()
    rig.bot._reply("Barrier output", "")
    assert (await take(rig.http.sent))[0] == chat_text("Barrier output")
    assert rig.http.sent.empty() and rig.bot._armed


async def test_caller_cancellation_marks_dispatched_receipt_unknown(rig: Rig) -> None:
    rig.http.gate.clear()
    task = asyncio.create_task(
        rig.bot.send_notice("Dispatched result", destination_key=destination(rig), current=allowed)
    )
    await take(rig.http.sent)
    receipt = rig.bot._active_notice
    assert receipt is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert receipt.receipt.result() == "unknown"
    rig.http.gate.set()
    rig.bot._reply("Barrier output", "")
    assert (await take(rig.http.sent))[0] == chat_text("Barrier output")
    assert rig.http.sent.empty() and rig.bot._armed


async def test_any_error_after_http_invocation_is_unknown_without_retry(rig: Rig) -> None:
    rig.http.failure = True
    assert (
        await rig.bot.send_notice(
            "Unconfirmed result", destination_key=destination(rig), current=allowed
        )
        == "unknown"
    )
    assert (await take(rig.http.sent))[0] == chat_text("Unconfirmed result")
    assert rig.http.sent.empty() and not rig.bot._armed


@pytest.mark.parametrize("char,width", [("x", 1), ("\U0001f642", 4), ("@", 6)])
@pytest.mark.parametrize("extra", [0, 1])
async def test_final_encoded_post_cap_is_exact(rig: Rig, char: str, width: int, extra: int) -> None:
    count, padding = divmod(MAX_POST_BYTES, width)
    text = char * count + "x" * (padding + extra)
    if not extra:
        assert len(chat_text(text).encode("utf-8")) == MAX_POST_BYTES
    result = await rig.bot.send_notice(text, destination_key=destination(rig), current=allowed)
    assert result == ("failed" if extra else "sent")
    if not extra:
        assert len((await take(rig.http.sent))[0].encode("utf-8")) == MAX_POST_BYTES
    assert rig.http.sent.empty() and rig.bot._armed


async def test_invalid_text_does_not_enter_queue(rig: Rig) -> None:
    assert (
        await rig.bot.send_notice("\ud800", destination_key=destination(rig), current=allowed)
        == "failed"
    )
    assert rig.bot._output_count == 0 and rig.http.sent.empty()


async def test_notice_overflow_refuses_without_disarming_existing_bridge(rig: Rig) -> None:
    rig.http.gate.clear()
    rig.bot._enqueue("x" * MAX_POST_BYTES, "")
    await take(rig.http.sent)
    key = destination(rig)
    text = "x" * MAX_POST_BYTES
    tasks = [
        asyncio.create_task(rig.bot.send_notice(text, destination_key=key, current=allowed))
        for _ in range(31)
    ]
    await checkpoint()
    assert rig.bot._output_count == 32 and rig.bot._output_bytes == 384 * 1024
    assert (
        await rig.bot.send_notice("Overflow", destination_key=key, current=allowed) == "unavailable"
    )
    assert rig.bot._armed and rig.bot._output_count == 32
    rig.bot.disarm()
    async with asyncio.timeout(0.05):
        assert await asyncio.gather(*tasks) == ["unavailable"] * 31
    assert await take(rig.http.sent) == (chat_text(_STOP_LINES["connection_lost"]), "")
    rig.http.gate.set()
    assert rig.http.sent.empty()
