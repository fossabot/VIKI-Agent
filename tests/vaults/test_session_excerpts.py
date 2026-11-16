"""Dispatching a turn that carries vault excerpts: the echo order, the clip and the refusals.

The clip rows use the real payload a turn can carry (the 12,000-character block budget beside a
long question, and a 16,000-character excerpt in a reloaded transcript), because the user's
question surviving `stream_state.MAX_TEXT_CHARS` is the whole point of the joined order.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_session import Drivers, GatedDriver, gated, watch
from tests.vaults.test_vault_excerpts import ACTIVE, part, vault
from viki_agent.catalog import Catalog, project_dir
from viki_agent.conversations.stream_state import MAX_TEXT_CHARS
from viki_agent.conversations.turn_parts import EXCERPT_BLOCK_CHARS, render_excerpt_block
from viki_agent.domain import CanonicalRoot
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import Command, Event, SessionStarted, UserMessage
from viki_agent.session import CommandDispatchResult
from viki_agent.state import Prefs
from viki_agent.turn_input import Attachment, TurnInput
from viki_agent.vaults.vault import VaultService

from replay import SID

VAULT = Path("C:/Vault")
QUESTION = "Summarize this"


async def _intent() -> None:
    return None


def _actor(
    tmp_path: Path, driver: GatedDriver | Drivers, *, reader: VaultService | None = None
) -> Any:
    """An actor whose manager has the reader bound, as the launcher binds it."""
    manager = gated(tmp_path, driver)
    if reader is not None:
        manager.bind_excerpts(reader, ACTIVE)
    return manager.open(SID)


def _note(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")


def _sent(driver: GatedDriver) -> list[str]:
    return [call for call in driver.calls if call.startswith("send")]


# --- the echo: the question first, then the excerpt block (order, clip, reload) ------------------


async def test_the_owner_text_comes_first_and_the_block_follows(tmp_path: Path) -> None:
    driver = GatedDriver()
    driver.release.set()
    _note(tmp_path, "note.md", "one\ntwo\nthree\n")
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    seen = watch(actor)
    turn = TurnInput(QUESTION, (), (part("note.md", 1, 2, "one\ntwo"),))
    # Written out rather than through joined_text, so this row pins the order itself.
    expected = f"{QUESTION}\n" + render_excerpt_block("note.md", 1, 2, "one\ntwo")

    async def accept() -> TurnInput:
        return turn

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert result == CommandDispatchResult("unknown", "native_unconfirmed")
    assert _sent(driver) == [f"send {expected}"]
    message = next(event["data"] for event in seen if event["type"] == "user.message")
    assert message["text"] == expected and message["command"] == ""
    assert expected.startswith(QUESTION)
    await actor.close()


async def test_the_clip_eats_the_citation_tail_and_never_the_question(tmp_path: Path) -> None:
    """A turn at the block budget with a long question: the replayed message is clipped to
    MAX_TEXT_CHARS, and what survives still opens with the user's words."""
    body = "a" * 11_900
    _note(tmp_path, "note.md", body + "\n")
    block = render_excerpt_block("note.md", 1, 1, body)
    assert len(block) <= EXCERPT_BLOCK_CHARS
    text = f"{QUESTION}\n" + "あ" * 5_000
    turn = TurnInput(text, (), (part("note.md", 1, 1, body),))
    expected = f"{text}\n{block}"  # the question first, the citation after it
    assert len(expected) > MAX_TEXT_CHARS  # the payload really is past the clip
    driver = GatedDriver()
    driver.release.set()
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    seen = watch(actor)

    async def accept() -> TurnInput:
        return turn

    await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    live = next(event["data"] for event in seen if event["type"] == "user.message")
    assert live["text"] == expected  # the subscriber gets the whole turn
    [snapshot] = actor.recovery("a-stale-generation", 0)
    replayed = next(
        item["data"] for item in snapshot["data"]["events"] if item["type"] == "user.message"
    )
    assert len(replayed["text"]) == MAX_TEXT_CHARS
    assert replayed["text"].startswith(QUESTION)
    assert expected.startswith(replayed["text"])  # only the end of the citation was cut
    await actor.close()


def test_a_reloaded_turn_with_a_16000_character_excerpt_opens_with_the_question() -> None:
    """The history half of the same property, at the size the transcript can hold: the mapper
    clips to the same limit, so the label must stay in and the fence may fall out."""
    excerpt = "a" * 16_000
    text = f"{QUESTION}\n" + "あ" * 400  # the question and its 16,000 characters pass the clip
    content = f"{text}\n" + render_excerpt_block("note.md", 1, 1, excerpt)
    assert len(content) > MAX_TEXT_CHARS
    entry = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": "2026-09-11T08:00:00.000Z",
    }

    events = Mapper(vault=str(VAULT), mode="default").entry(entry)

    [message] = events
    assert isinstance(message, UserMessage)
    assert message.command == ""
    assert message.text.startswith(QUESTION)
    assert len(message.text) == MAX_TEXT_CHARS
    assert "[vault excerpt] note.md, lines 1-1, sha256 " in message.text
    assert not message.text.endswith("```")  # the citation's own end is what the clip ate


def test_the_sessions_list_shows_the_owner_words_as_the_first_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True, exist_ok=True)
    content = f"{QUESTION}\n" + render_excerpt_block("note.md", 1, 1, "a" * 16_000)
    entry = {
        "type": "user",
        "uuid": "11111111-1111-4111-8111-111111111111",
        "parentUuid": None,
        "isSidechain": False,
        "entrypoint": "cli",
        "timestamp": "2026-09-11T08:00:00.000Z",
        "message": {"role": "user", "content": content},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    (folder / f"{SID}.jsonl").write_bytes(line.encode("utf-8"))

    [row] = Catalog(home, VAULT).sessions()

    assert row.first_prompt.startswith(QUESTION)
    assert row.title.startswith(QUESTION)


# --- refusals: nothing emitted, nothing sent -----------------------------------------------------


@pytest.mark.parametrize("sha", ["stale", "fresh"])
async def test_a_changed_note_ends_the_attempt_with_nothing_emitted(
    tmp_path: Path, sha: str
) -> None:
    """The same turn, twice: the control run proves the fixture itself dispatches."""
    _note(tmp_path, "note.md", "one\n")
    attached = part("note.md", 1, 1, "one")
    if sha == "stale":
        _note(tmp_path, "note.md", "edited\n")
    driver = GatedDriver()
    driver.release.set()
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    seen = watch(actor)

    async def accept() -> TurnInput:
        return TurnInput(QUESTION, (), (attached,))

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    if sha == "fresh":
        assert result == CommandDispatchResult("unknown", "native_unconfirmed")
        assert _sent(driver) and "user.message" in seen.types
    else:
        assert result == CommandDispatchResult("not_dispatched", "excerpt_stale")
        assert _sent(driver) == []
        assert "user.message" not in seen.types and "turn.started" not in seen.types
    await actor.close()


async def test_blocks_over_the_budget_end_the_attempt_with_nothing_emitted(
    tmp_path: Path,
) -> None:
    body = "a" * 7_000
    _note(tmp_path, "a.md", body + "\n")
    _note(tmp_path, "b.md", body + "\n")
    parts = (part("a.md", 1, 1, body), part("b.md", 1, 1, body))
    assert sum(len(render_excerpt_block("x", 1, 1, body)) for _ in parts) > EXCERPT_BLOCK_CHARS
    driver = GatedDriver()
    driver.release.set()
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    seen = watch(actor)

    async def accept() -> TurnInput:
        return TurnInput(QUESTION, (), parts)

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert result == CommandDispatchResult("not_dispatched", "excerpts_too_large")
    assert _sent(driver) == []
    assert "user.message" not in seen.types and "turn.started" not in seen.types
    await actor.close()


async def test_a_slash_command_carrying_an_excerpt_is_refused(tmp_path: Path) -> None:
    class CommandDriver(GatedDriver):
        """A session that offers /capture, so the dispatch sees a real command."""

        async def open(self) -> SessionStarted:
            started = await super().open()
            return replace(started, commands=[Command(name="capture", description="")])

    driver = CommandDriver()
    driver.release.set()
    _note(tmp_path, "note.md", "one\n")
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    seen = watch(actor)

    async def accept() -> TurnInput:
        return TurnInput("/capture this", (), (part("note.md", 1, 1, "one"),))

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert result == CommandDispatchResult("not_dispatched", "excerpt_with_command")
    assert _sent(driver) == []
    assert "user.message" not in seen.types
    await actor.close()


async def test_an_unbound_reader_refuses_the_turn_rather_than_dropping_its_citations(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    _note(tmp_path, "note.md", "one\n")
    actor = _actor(tmp_path, driver)  # no bind_excerpts
    seen = watch(actor)

    async def accept() -> TurnInput:
        return TurnInput(QUESTION, (), (part("note.md", 1, 1, "one"),))

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert result == CommandDispatchResult("not_dispatched", "excerpt_unavailable")
    assert _sent(driver) == [] and "user.message" not in seen.types
    await actor.close()


# --- where the read runs, and the turns that carry no part ---------------------------------------


async def test_the_note_read_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actor lock is held across the dispatch, so the read must not run on the loop thread."""
    _note(tmp_path, "note.md", "one\n")
    reader = vault(tmp_path)
    threads: list[int] = []
    original = reader.excerpt_at

    def recording(root: CanonicalRoot, workspace_id: str, ref: str, start: int, end: int) -> Any:
        threads.append(threading.get_ident())
        return original(root, workspace_id, ref, start, end)

    monkeypatch.setattr(reader, "excerpt_at", recording)
    driver = GatedDriver()
    driver.release.set()
    actor = _actor(tmp_path, driver, reader=reader)

    async def accept() -> TurnInput:
        return TurnInput(QUESTION, (), (part("note.md", 1, 1, "one"),))

    await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert len(threads) == 1 and threads[0] != threading.get_ident()
    await actor.close()


async def test_an_image_turn_with_an_excerpt_sends_the_joined_text_and_no_part(
    tmp_path: Path,
) -> None:
    class AttachmentDriver(GatedDriver):
        sent: TurnInput | None = None

        async def send_turn(self, turn: TurnInput) -> None:
            self.sent = turn
            self.calls.append("send attachment")

    driver = AttachmentDriver()
    driver.release.set()
    _note(tmp_path, "note.md", "one\n")
    actor = _actor(tmp_path, driver, reader=vault(tmp_path))
    image = Attachment(
        "00000000-0000-4000-8000-000000000021", "image/png", 4, 1, 1, "synthetic.png"
    )
    turn = TurnInput(QUESTION, (image,), (part("note.md", 1, 1, "one"),))
    expected = f"{QUESTION}\n" + render_excerpt_block("note.md", 1, 1, "one")

    async def accept() -> TurnInput:
        return turn

    await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )

    assert driver.sent == TurnInput(expected, (image,))
    assert driver.sent is not None and driver.sent.parts == ()
    await actor.close()


async def test_a_turn_with_no_part_is_sent_exactly_as_it_is_today(tmp_path: Path) -> None:
    """Both existing branches, with no reader bound at all: the text and the typed value that
    reach the driver are the ones they were before excerpts existed."""

    class AttachmentDriver(GatedDriver):
        sent: TurnInput | None = None

        async def send_turn(self, turn: TurnInput) -> None:
            self.sent = turn
            self.calls.append("send attachment")

    class Made(Drivers):
        def __call__(
            self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]
        ) -> GatedDriver:
            driver = AttachmentDriver()
            driver.release.set()
            self.made.append((driver, launch, prefs, emit))
            return driver

    drivers = Made()
    actor = _actor(tmp_path, drivers)
    text = "synthetic café\n・unchanged\n"

    async def accept_text() -> TurnInput:
        return TurnInput(text)

    assert await actor.send_command(
        expected_generation=actor.generation, accept=accept_text, dispatch_intent=_intent
    ) == CommandDispatchResult("unknown", "native_unconfirmed")
    typed = drivers.made[0][0]
    assert isinstance(typed, AttachmentDriver)
    assert _sent(typed) == [f"send {text}"] and typed.sent is None
    await actor.close()

    image = Attachment(
        "00000000-0000-4000-8000-000000000022", "image/png", 4, 1, 1, "synthetic.png"
    )
    turn = TurnInput(text, (image,))
    actor = _actor(tmp_path, drivers)

    async def accept_image() -> TurnInput:
        return turn

    await actor.send_command(
        expected_generation=actor.generation, accept=accept_image, dispatch_intent=_intent
    )
    typed = drivers.made[1][0]
    assert isinstance(typed, AttachmentDriver)
    assert typed.sent is turn  # the committed value itself, not a copy
    await actor.close()
