"""Visible folder context reaches every ordinary turn before its native dispatch intent."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from tests.app.test_workspaces import service_at
from tests.conversations.test_session import Drivers, GatedDriver, gated, watch
from tests.test_server import EchoDriver, _open_v2, authed
from tests.vaults.test_vault_excerpts import ACTIVE, part, vault
from tests.workspace_code.test_roots_server import _build, _folders, _send, running
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code.session_roots import MSG_ROOT_UNTRUSTED, SessionRoots
from viki_agent.conversations.command_runtime import CommandRunner
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService
from viki_agent.conversations.turn_parts import render_excerpt_block
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.control import Launch, build_argv
from viki_agent.events import Command, Event, SessionStarted
from viki_agent.session import (
    CommandDispatchRejected,
    CommandDispatchResult,
    SessionActor,
    SessionManager,
    now_iso,
)
from viki_agent.state import Prefs
from viki_agent.storage import StateError
from viki_agent.turn_input import MAX_TEXT_BYTES, Attachment, TurnInput

from replay import SID

_SECOND = "00000000-0000-4000-8000-000000000012"
_CONTEXT = (
    "Selected code folders for this conversation:\n"
    '{"primary_cwd":"C:/Synthetic/main","additional_folders":["C:/Synthetic/extra"]}\n'
    "Native permissions govern access to these folders."
)


class CaptureDriver(GatedDriver):
    def __init__(self) -> None:
        super().__init__()
        self.turns: list[TurnInput] = []

    async def open(self) -> SessionStarted:
        return replace(await super().open(), commands=[Command(name="capture", description="")])

    async def send_turn(self, turn: TurnInput) -> None:
        self.turns.append(turn)
        self.calls.append("send attachment")


class CaptureDrivers(Drivers):
    def __call__(
        self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]
    ) -> CaptureDriver:
        driver = CaptureDriver()
        driver.release.set()
        self.made.append((driver, launch, prefs, emit))
        return driver


def _image(n: int) -> Attachment:
    return Attachment(f"00000000-0000-4000-8000-{n:012d}", "image/png", 4, 1, 1, f"image-{n}.png")


async def _intent() -> None:
    pass


async def _context(_session_id: str) -> str:
    return _CONTEXT


async def _root(_session_id: str) -> None:
    return None


async def _bound(
    tmp_path: Path, *, excerpts: bool = False
) -> tuple[SessionManager, SessionRoots, CaptureDrivers, SessionActor]:
    drivers = CaptureDrivers()
    manager = gated(tmp_path, drivers)
    spaces = service_at(tmp_path, manager.app_state.store)
    selected = []
    for name in ("primary", "extra", "other"):
        folder = tmp_path / name
        folder.mkdir()
        selected.append(trusted(spaces, folder))
    roots = SessionRoots(manager.app_state.store, spaces, exists=lambda _: False, actor=manager.get)
    await roots.prepare(SID, selected[0], None, tuple(selected[1:]))
    manager.bind_roots(roots.resolve, context=roots.context)
    if excerpts:
        manager.bind_excerpts(vault(tmp_path), ACTIVE)
    return manager, roots, drivers, manager.open(SID)


@pytest.mark.parametrize("text", ["describe both images", ""])
async def test_image_turn_without_excerpts_sends_all_selected_paths(
    tmp_path: Path, text: str
) -> None:
    _, roots, drivers, actor = await _bound(tmp_path)
    seen = watch(actor)
    turn = TurnInput(text, (_image(1), _image(2)))
    context = await roots.context(SID)
    assert context is not None
    expected = f"{text}\n{context}" if text else context

    async def accept() -> TurnInput:
        return turn

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    driver, launch, prefs, _ = drivers.made[0]
    assert isinstance(driver, CaptureDriver)
    assert result == CommandDispatchResult("unknown", "native_unconfirmed")
    assert driver.turns == [TurnInput(expected, turn.attachments)]
    assert turn.text == text and turn.parts == ()
    message = next(event["data"] for event in seen if event["type"] == "user.message")
    assert message["text"] == expected and message["attachments"] == [
        a.data() for a in turn.attachments
    ]
    assert launch.cwd == roots.root_of(SID)
    assert build_argv(["claude.exe"], launch, mode=prefs.mode, effort=prefs.effort) == build_argv(
        ["claude.exe"], Launch(SID, resume=True), mode=prefs.mode, effort=prefs.effort
    )
    await actor.close()


async def test_excerpts_and_images_precede_the_folder_context(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("one\n", encoding="utf-8")
    _, roots, drivers, actor = await _bound(tmp_path, excerpts=True)
    seen = watch(actor)
    turn = TurnInput("owner question", (_image(1),), (part("note.md", 1, 1, "one"),))
    block = render_excerpt_block("note.md", 1, 1, "one")
    expected = f"owner question\n{block}\n{await roots.context(SID)}"

    async def accept() -> TurnInput:
        return turn

    await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    driver = drivers.made[0][0]
    assert isinstance(driver, CaptureDriver)
    assert driver.turns == [TurnInput(expected, turn.attachments)]
    assert driver.turns[0].parts == () and turn.parts
    assert (
        next(event["data"]["text"] for event in seen if event["type"] == "user.message") == expected
    )
    await actor.close()


@pytest.mark.parametrize("path", ["legacy", "durable"])
@pytest.mark.parametrize("text", ["/capture  keep\targs\n", "/dynamic-skill untouched\n"])
async def test_slash_bytes_stay_exact_and_the_next_ordinary_turn_has_context(
    tmp_path: Path, path: str, text: str
) -> None:
    driver = CaptureDriver()
    driver.release.set()
    manager = gated(tmp_path, driver)
    actor = manager.open(SID)  # Binding also reaches an actor created before the bind.
    checked: list[str] = []

    async def context(session_id: str) -> str:
        checked.append(session_id)
        return _CONTEXT

    manager.bind_roots(_root, context=context)

    async def accept() -> str:
        return text

    if path == "legacy":
        assert await actor.send(text)
    else:
        await actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
        )
    assert [call for call in driver.calls if call.startswith("send ")] == [f"send {text}"]
    assert checked == [SID]
    assert await actor.send("next")
    assert driver.calls[-1] == f"send next\n{_CONTEXT}" and checked == [SID, SID]
    await actor.close()


async def test_none_context_preserves_existing_and_future_typed_values(tmp_path: Path) -> None:
    drivers = CaptureDrivers()
    manager = gated(tmp_path, drivers)
    first = manager.open(SID)

    async def no_context(_session_id: str) -> None:
        return None

    manager.bind_roots(_root, context=no_context)
    second = manager.open(_SECOND)
    turn = TurnInput("unchanged\n", (_image(1),))

    async def accept() -> TurnInput:
        return turn

    for actor in (first, second):
        await actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
        )
    for driver, _, _, _ in drivers.made:
        assert isinstance(driver, CaptureDriver) and driver.turns[0] is turn
    await manager.close_all()


@pytest.mark.parametrize("path", ["legacy", "durable"])
@pytest.mark.parametrize("failure", ["start", "state"])
async def test_live_context_refusal_has_no_echo_intent_or_native_send(
    tmp_path: Path, path: str, failure: str
) -> None:
    driver = CaptureDriver()
    driver.release.set()
    manager = gated(tmp_path, driver)
    actor = manager.open(SID)
    assert await actor.start()
    seen = watch(actor)

    async def refuse(_session_id: str) -> str:
        assert actor._lock.locked()
        if failure == "state":
            raise StateError("corrupt")
        raise StartError("bad_request", MSG_ROOT_UNTRUSTED)

    manager.bind_roots(_root, context=refuse)

    async def accept() -> str:
        return "/dynamic-skill args"

    async def forbidden() -> None:
        pytest.fail("intent preceded folder admission")

    if path == "legacy":
        assert await actor.send("/dynamic-skill args") is False
    else:
        assert await actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=forbidden
        ) == CommandDispatchResult("not_dispatched", "start_failed")
    assert seen.types == ["error"] and driver.calls == ["open", "opened"]
    assert seen[0]["data"]["message"] == (
        StateError("corrupt").message if failure == "state" else MSG_ROOT_UNTRUSTED
    )
    assert actor.live
    await actor.close()


@pytest.mark.parametrize("path", ["legacy", "durable"])
async def test_final_utf8_budget_is_checked_before_intent_and_preserves_submission(
    tmp_path: Path, path: str
) -> None:
    driver = CaptureDriver()
    driver.release.set()
    manager = gated(tmp_path, driver)
    manager.bind_roots(_root, context=_context)
    actor = manager.open(SID)
    seen = watch(actor)
    original = "\u00e9" * (MAX_TEXT_BYTES // 2)
    assert TurnInput(original).text_bytes == MAX_TEXT_BYTES
    if path == "legacy":
        assert await actor.send(original) is False
    else:
        vault_root = tmp_path / "vault"
        saved = DraftService(manager.app_state, vault_root).save(SID, 0, original)
        owner, command_id = str(uuid.uuid4()), str(uuid.uuid4())
        service = CommandService(manager.app_state, vault_root, owner=owner)
        runner = CommandRunner(service, manager)
        await runner.submit(CommandInput(command_id, SID, actor.generation, saved.revision))
        await asyncio.gather(*tuple(runner._jobs))
        record = service.get(command_id)
        assert record.state == "not_dispatched" and record.reason == "start_failed"
        assert record.text == original and record.bytes == MAX_TEXT_BYTES
        await runner.close()
    assert "user.message" not in seen.types and "turn.started" not in seen.types
    assert not any(call.startswith("send ") for call in driver.calls)
    error = next(event["data"] for event in seen if event["type"] == "error")
    assert (
        error["message"]
        == "This message is too long, so nothing was sent. Shorten it, then send again."
    )
    await actor.close()


@pytest.mark.parametrize("path", ["legacy", "durable"])
@pytest.mark.parametrize("change", ["effort", "cancel"])
async def test_context_await_preserves_runtime_and_cancellation_guards(
    tmp_path: Path, path: str, change: str
) -> None:
    driver = CaptureDriver()
    driver.release.set()
    manager = gated(tmp_path, driver)
    actor = manager.open(SID)
    assert await actor.start()
    seen = watch(actor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def context(_session_id: str) -> str:
        entered.set()
        await release.wait()
        return _CONTEXT

    async def accept() -> str:
        return "owner question"

    async def forbidden() -> None:
        pytest.fail("intent preceded runtime revalidation")

    manager.bind_roots(_root, context=context)
    task = asyncio.create_task(
        actor.send("owner question")
        if path == "legacy"
        else actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=forbidden
        )
    )
    await asyncio.wait_for(entered.wait(), 0.05)
    if change == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert actor.set_effort("high")
        release.set()
        if path == "legacy":
            with pytest.raises(CommandDispatchRejected) as refusal:
                await task
            assert refusal.value.code == "stale_generation"
        else:
            assert await task == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert "user.message" not in seen.types and "turn.started" not in seen.types
    assert not any(call.startswith("send ") for call in driver.calls)
    await actor.close()


def test_create_app_binds_folder_context_to_real_websocket_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path)
    built = _build(folders)
    extra = folders.display.parent / "extra"
    extra.mkdir()
    extra_id = trusted(built.spaces, extra)
    workspace = built.spaces.get(extra_id, kind="code")
    built.state.store.put_session_root(
        {
            "session_id": SID,
            "workspace_id": folders.code_id,
            "worktree_id": None,
            "display_root": str(folders.display),
            "created_at": now_iso(),
            "additional_roots": [
                {
                    "workspace_id": extra_id,
                    "display_root": workspace.root.display,
                    "physical_id": workspace.root.physical_id,
                }
            ],
        }
    )
    sent: list[str] = []

    async def capture(_driver: EchoDriver, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(EchoDriver, "send", capture)
    with running(built) as client, authed(client) as ws:
        _, target = _open_v2(ws, SID)
        frames = _send(ws, target)
    assert len(sent) == 1 and sent[0].startswith(
        "go on\nSelected code folders for this conversation:\n"
    )
    message = next(frame["data"] for frame in frames if frame["type"] == "user.message")
    assert message["text"] == sent[0]
    assert json.loads(sent[0].splitlines()[2]) == {
        "primary_cwd": str(folders.display),
        "additional_folders": [workspace.root.display],
    }
    assert built.launches[0].cwd == folders.display
