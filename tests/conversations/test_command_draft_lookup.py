"""Read-only recovery of an accepted command by its exact saved editor revision."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from tests.conversations.test_command_api import AUTH, BASE, CID, SID, assemble, prepare
from viki_agent import protection
from viki_agent.conversations.commands import CommandError, CommandInput, CommandService
from viki_agent.state import AppState, Prefs

LOOKUP = f"/api/v2/sessions/{SID}/draft-command"


async def test_lookup_requires_auth_and_exact_bounded_revision(tmp_path: Path) -> None:
    app, _, manager, drivers, drafts = assemble(tmp_path)
    _, value = prepare(manager, drafts)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        assert (await client.get(LOOKUP + "?revision=1")).status_code == 401
        assert (
            await client.get(
                LOOKUP + "?revision=1", headers={**AUTH, "origin": "https://foreign.invalid"}
            )
        ).status_code == 403
        for query in (
            "",
            "?revision=",
            "?revision=0",
            "?revision=01",
            "?revision=-1",
            "?revision=9007199254740992",
            "?revision=1&revision=1",
            "?revision=1&scope=x",
            "?revision=" + "9" * 300,
        ):
            assert (await client.get(LOOKUP + query, headers=AUTH)).status_code == 400
        assert (
            await client.get("/api/v2/sessions/INVALID/draft-command?revision=1", headers=AUTH)
        ).status_code == 400
        assert (await client.get(LOOKUP + "?revision=1", headers=AUTH)).status_code == 404
        assert drivers == [] and app.state.commands.service.list().commands == []
        assert value["draft_revision"] == 1


async def test_lookup_is_metadata_only_and_survives_draft_change_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, manager, drivers, drafts = assemble(tmp_path)
    _, value = prepare(manager, drafts)
    accepted, created = app.state.commands.service.accept(CommandInput(**value))
    assert created and accepted.text
    drafts.save(SID, 1, "new editor text")
    with monkeypatch.context() as patch:
        patch.setattr(
            protection, "unprotect", lambda *_args, **_kw: pytest.fail("lookup decrypted text")
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
        ):
            response = await client.get(LOOKUP + "?revision=1", headers=AUTH)
            assert response.status_code == 200
            receipt = response.json()
            assert receipt["command_id"] == CID
            assert receipt["session_id"] == SID
            assert receipt["draft_revision"] == 1
            assert receipt["state"] == "unknown"  # no active runner owns a native attempt
            assert "text" not in receipt and "submitted" not in response.text
            assert (await client.get(LOOKUP + "?revision=2", headers=AUTH)).status_code == 404
            head = await client.head(LOOKUP + "?revision=1", headers=AUTH)
            assert head.status_code == 200 and head.content == b""
    assert drivers == []

    reopened, _, replacement_manager, replacement_drivers, _ = assemble(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url=BASE
    ) as client:
        recovered = await client.get(LOOKUP + "?revision=1", headers=AUTH)
        assert recovered.status_code == 200
        assert recovered.json()["command_id"] == CID
        assert recovered.json()["state"] == "unknown"
        assert replacement_manager.get(SID) is None and replacement_drivers == []
    await reopened.state.commands.close()


def test_second_id_for_same_revision_is_refused_and_another_scope_isolated(tmp_path: Path) -> None:
    _, state, manager, _, drafts = assemble(tmp_path)
    actor, value = prepare(manager, drafts)
    service = CommandService(state, tmp_path / "vault", owner=str(uuid.uuid4()))
    first, _ = service.accept(CommandInput(**value))
    another_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    with pytest.raises(CommandError) as duplicate:
        service.accept(CommandInput(**{**value, "command_id": another_id}))
    assert duplicate.value.code == "draft_already_submitted"
    assert service.accept(CommandInput(**value)) == (first, False)
    assert service.find_draft_command(SID, 1).command_id == first.command_id
    assert asdict(service.find_draft_command(SID, 1)).get("text") is None
    assert actor.generation == value["generation"]

    other_vault = tmp_path / "other-vault"
    other_vault.mkdir()
    isolated = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), other_vault, owner=str(uuid.uuid4())
    )
    with pytest.raises(CommandError) as missing:
        isolated.find_draft_command(SID, 1)
    assert missing.value.code == "not_found"


def test_two_command_ids_racing_for_one_saved_revision_commit_only_one_copy(tmp_path: Path) -> None:
    _, state, manager, _, drafts = assemble(tmp_path)
    _, value = prepare(manager, drafts)
    service = CommandService(state, tmp_path / "vault", owner=str(uuid.uuid4()))
    ids = (CID, "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    start = Barrier(2)

    def submit(command_id: str):
        start.wait()
        try:
            return service.accept(CommandInput(**{**value, "command_id": command_id}))
        except CommandError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, cid) for cid in ids]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, tuple) and result[1] for result in results) == 1
    assert len(service.list().commands) == 1
    for result, command_id in zip(results, ids, strict=True):
        if not isinstance(result, tuple):
            assert result in {"draft_already_submitted", "busy"}
            with pytest.raises(CommandError) as retry:
                service.accept(CommandInput(**{**value, "command_id": command_id}))
            assert retry.value.code == "draft_already_submitted"


async def test_http_second_id_uses_safe_distinct_409_without_native_attempt(tmp_path: Path) -> None:
    app, _, manager, drivers, drafts = assemble(tmp_path)
    _, value = prepare(manager, drafts)
    app.state.commands.service.accept(CommandInput(**value))
    second = {**value, "command_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        response = await client.post(
            "/api/v2/commands", headers={**AUTH, "origin": BASE}, json=second
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "draft_already_submitted"
        lookup = await client.get(LOOKUP + "?revision=1", headers=AUTH)
        assert lookup.status_code == 200 and lookup.json()["command_id"] == CID
    assert not drivers and len(app.state.commands.service.list().commands) == 1


def test_lookup_validates_service_input_before_storage(tmp_path: Path) -> None:
    _, state, _, _, _ = assemble(tmp_path)
    service = CommandService(state, tmp_path / "vault", owner=str(uuid.uuid4()))
    for session_id, revision in (("INVALID", 1), (SID, 0), (SID, True), (SID, 1 << 53)):
        with pytest.raises(CommandError) as failure:
            service.find_draft_command(session_id, revision)
        assert failure.value.code == "invalid_command"
