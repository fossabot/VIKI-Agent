"""Owner thumbs reactions on Mattermost permission cards; synthetic posts and REST lists."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from tests.messaging.test_desktop_questions import Desk, on, shown
from tests.messaging.test_desktop_questions import ask as desk_ask
from tests.messaging.test_mattermost_bot import BINDING, Driver, Http, Rig, take
from tests.messaging.test_remote_answers import NO_ALLOW, choices, question
from tests.messaging.test_remote_steer import reads
from viki_agent.conversations.approvals import PermissionChoice
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.events import PermissionRequested, RequestCancelled
from viki_agent.messaging.mattermost_bot import _HELP
from viki_agent.messaging.mattermost_format import chat_text
from viki_agent.messaging.mattermost_http import Binding, MattermostHttp, MattermostHttpError
from viki_agent.messaging.mattermost_transport import reaction_candidate

OTHER = "x" * 26
HINT = (
    "A permission request is waiting. Reply allow or deny to answer it; this message was not "
    "sent to Claude."
)
SEVERAL = (
    "Several requests are waiting, so this message answered none of them and was not sent to "
    "Claude. Use the command shown under the request you mean."
)
REACT = (
    " On a permission request you can also react with a thumbs up to allow it once or a thumbs "
    "down to deny it."
)


class ReactionHttp(Http):
    """Distinct post ids for every send, and each post's REST reaction list."""

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[str] = []
        self.lists: dict[str, list[dict[str, Any]]] = {}
        self.reaction_reads: list[str] = []
        self.reading = asyncio.Event()
        self.reaction_gate = asyncio.Event()
        self.reaction_gate.set()
        self.invalid = False

    async def send(self, binding: Binding, message: str, root_id: str = "") -> str:
        self.shown.append(f"r{len(self.shown) + 1:025d}")
        post_id = self.shown[-1]
        await super().send(binding, message, root_id)
        return post_id

    async def reactions(self, post_id: str) -> list[dict[str, Any]]:
        self.reaction_reads.append(post_id)
        self.reading.set()
        await self.reaction_gate.wait()
        if self.invalid:
            self.invalid = False
            raise MattermostHttpError("mattermost_post_invalid")
        return list(self.lists.get(post_id, []))


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    value = Rig(tmp_path)
    value.http = ReactionHttp()
    value.bot.http = cast(MattermostHttp, value.http)
    value.manager.bind_observer(value.bot.observe)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()


def web(rig: Rig) -> ReactionHttp:
    return cast(ReactionHttp, rig.http)


def react(
    rig: Rig, post_id: str, emoji: str = "+1", *, confirmed: bool = True, **changes: Any
) -> None:
    """Put an owner reaction on the WebSocket in the shape `posted` yields, or, when `posted`
    would refuse it, as a raw reaction; with `confirmed`, the post's REST list holds it too."""
    item = {
        "user_id": BINDING.owner_user_id,
        "channel_id": BINDING.channel_id,
        "post_id": post_id,
        "emoji_name": emoji,
        "create_at": 1000,
        "delete_at": 0,
        **changes,
    }
    if confirmed:
        web(rig).lists.setdefault(post_id, []).append(item)
    candidate = reaction_candidate(item, BINDING, 1000)
    rig.incoming.put_nowait(candidate or {"_reaction": True, **item, "_fresh_cutoff_ms": 1000})


async def permission(rig: Rig, driver: Driver, tool_id: str) -> tuple[str, str]:
    """Show an own permission card; return its detail and instruction post ids."""
    driver.ask(tool_id)
    await take(rig.http.sent)
    await take(rig.http.sent)
    return web(rig).shown[-2], web(rig).shown[-1]


def receipt(allow: bool) -> str:
    return f"{'Allowed once.' if allow else 'Denied.'} Choice receipt: delivered_locally."


@pytest.mark.parametrize(
    ("where", "emoji"), [(0, "+1"), (1, "thumbsup"), (0, "-1"), (1, "thumbsdown")]
)
async def test_thumbs_reaction_decides_its_exact_card(rig: Rig, where: int, emoji: str) -> None:
    _, driver = await rig.chat()
    first = await permission(rig, driver, "card")
    second = await permission(rig, driver, "second")
    react(rig, first[where], emoji)
    allow = emoji in {"+1", "thumbsup"}
    assert await take(driver.answers) == ("card", PermissionAnswer(allow))
    message, root = await take(rig.http.sent)
    assert message == receipt(allow) and root == rig.bot._thread and root
    assert choices(rig) == [PermissionChoice(allow)] and driver.pending() == ["second"]
    # Once used, the same reaction and another one on the decided card do nothing.
    react(rig, first[where], emoji)
    react(rig, first[1 - where], "-1", create_at=2000)
    react(rig, second[0], "-1")
    assert await take(driver.answers) == ("second", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == receipt(False)
    assert web(rig).reaction_reads == [first[where], second[0]]
    assert driver.answers.empty() and driver.sent.empty() and rig.bot._armed


async def test_reactions_off_an_own_waiting_permission_card_do_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = Desk(rig, monkeypatch)
    await on(rig)
    _, desk_driver = await desk.chat()
    desk_ask(desk_driver)
    await shown(rig.http.sent)
    desk_posts = web(rig).shown[-2:]
    _, driver = await rig.chat()
    prompt = f"{rig.index:026d}"
    await question(rig, driver, "question")
    question_posts = web(rig).shown[-2:]
    detail, instructions = await permission(rig, driver, "card")
    react(rig, detail, user_id=OTHER)
    react(rig, detail, channel_id=OTHER)
    react(rig, detail, create_at=999)
    react(rig, detail, "eyes")
    for post_id in (web(rig).shown[0], prompt, *question_posts, *desk_posts):
        react(rig, post_id)
    react(rig, instructions, "-1")
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == receipt(False)
    assert web(rig).reaction_reads == [instructions]
    assert driver.pending() == ["question"] and driver.answers.empty()
    assert desk_driver.pending() == ["desk"] and desk_driver.answers.empty()
    assert choices(rig) == [PermissionChoice(False)] and rig.bot._armed


@pytest.mark.parametrize(
    "rest", ["empty", "user", "channel", "post", "emoji", "early", "deleted", "invalid"]
)
async def test_reaction_rest_does_not_confirm_does_nothing(rig: Rig, rest: str) -> None:
    _, driver = await rig.chat()
    detail, instructions = await permission(rig, driver, "card")
    near = {
        "empty": None,
        "invalid": None,
        "user": {"user_id": OTHER},
        "channel": {"channel_id": OTHER},
        "post": {"post_id": instructions},
        "emoji": {"emoji_name": "-1"},
        "early": {"create_at": 999},  # Below the cutoff.
        "deleted": {"delete_at": 1},
    }[rest]
    if near is not None:
        listed = {
            "user_id": BINDING.owner_user_id,
            "channel_id": BINDING.channel_id,
            "post_id": detail,
            "emoji_name": "+1",
            "create_at": 1000,
            "delete_at": 0,
        }
        web(rig).lists[detail] = [{**listed, **near}]
    web(rig).invalid = rest == "invalid"
    react(rig, detail, "+1", confirmed=False)
    react(rig, detail, "-1", create_at=2000)  # Then a confirmed one: the first did nothing.
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == receipt(False)
    assert web(rig).reaction_reads == [detail, detail]
    assert choices(rig) == [PermissionChoice(False)] and rig.bot._armed


async def test_readded_reaction_confirms_with_its_original_create_at(rig: Rig) -> None:
    """Mattermost keeps a re-added reaction's first create_at in REST; the frame carries the new
    one."""
    _, driver = await rig.chat()
    detail, _ = await permission(rig, driver, "card")
    first = {
        "user_id": BINDING.owner_user_id,
        "channel_id": BINDING.channel_id,
        "post_id": detail,
        "emoji_name": "+1",
        "create_at": 1500,
        "delete_at": 0,
    }
    web(rig).lists[detail] = [first]
    react(rig, detail, "+1", confirmed=False, create_at=3000)
    assert await take(driver.answers) == ("card", PermissionAnswer(True))
    message, root = await take(rig.http.sent)
    assert message == receipt(True) and root == rig.bot._thread
    react(rig, detail, "+1", confirmed=False, create_at=4000)  # The card is used: nothing.
    rig.post("!viki help")  # Its reply shows the reaction before it was handled.
    assert (await take(rig.http.sent))[0] == chat_text(_HELP)
    assert web(rig).reaction_reads == [detail]
    assert choices(rig) == [PermissionChoice(True)] and rig.bot._armed


async def test_incomplete_card_refuses_allow_and_accepts_deny(rig: Rig) -> None:
    _, driver = await rig.chat()
    driver.inputs["card"] = None  # Too long to show: the card offers Deny only.
    driver.emit(PermissionRequested(tool_use_id="card", kind="tool", tool="Edit", summary="Tiny"))
    assert "!viki allow" not in (await take(rig.http.sent))[0]
    post = web(rig).shown[-1]
    react(rig, post, "thumbsup")
    message, root = await take(rig.http.sent)
    assert message == NO_ALLOW and root == rig.bot._thread
    assert driver.answers.empty() and driver.pending() == ["card"] and not choices(rig)
    react(rig, post, "thumbsdown")
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == receipt(False)
    assert choices(rig) == [PermissionChoice(False)] and rig.bot._armed


async def test_card_gone_while_confirming_answers_nothing(rig: Rig) -> None:
    actor, driver = await rig.chat()
    detail, _ = await permission(rig, driver, "card")
    web(rig).reaction_gate.clear()
    react(rig, detail)
    try:
        async with asyncio.timeout(0.05):
            await web(rig).reading.wait()
        actor._emit(RequestCancelled(tool_use_id="card"))
    finally:
        web(rig).reaction_gate.set()
    message, root = await take(rig.http.sent)
    assert message == "That request is no longer waiting." and root == rig.bot._thread
    assert driver.answers.empty() and not choices(rig) and rig.bot._armed


async def test_reaction_on_a_card_still_being_posted_does_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await rig.chat()
    release = asyncio.Event()
    send = web(rig).send

    async def held(binding: Binding, message: str, root_id: str = "") -> str:
        if "!viki allow" in message:  # The card's last post waits: it is not shown yet.
            await release.wait()
        return await send(binding, message, root_id)

    monkeypatch.setattr(web(rig), "send", held)
    confirmed = reads(rig, monkeypatch)
    driver.ask("card")
    assert "synthetic hidden argument" in (await take(rig.http.sent))[0]
    detail = web(rig).shown[-1]
    card = next(iter(rig.bot._cards.values()))
    assert detail in card.post_ids and card.deadline is None
    react(rig, detail, "+1")
    post = rig.post("!viki help")
    assert await take(confirmed) == post["id"]  # Every earlier input has been handled.
    assert web(rig).reaction_reads == [] and driver.answers.empty()
    assert driver.pending() == ["card"] and not choices(rig)
    release.set()
    assert "!viki allow" in (await take(rig.http.sent))[0]
    assert (await take(rig.http.sent))[0] == chat_text(_HELP)
    assert driver.answers.empty() and driver.pending() == ["card"] and rig.bot._armed


async def test_mattermost_hints_name_reactions(rig: Rig) -> None:
    _, driver = await rig.chat()
    await permission(rig, driver, "card")
    rig.post("please allow it")
    assert (await take(rig.http.sent))[0] == HINT + REACT
    await permission(rig, driver, "second")
    rig.post("allow")
    assert (await take(rig.http.sent))[0] == SEVERAL + REACT
    assert driver.answers.empty() and not choices(rig) and rig.bot._armed
