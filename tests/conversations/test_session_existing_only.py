"""Existing-only window subscriptions use synthetic histories and provider doubles."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from tests.profiles.test_profile_launch_api import HANG_GUARD_S, Rig, ident, recv
from tests.profiles.test_profile_launch_api import authed as authed_profile
from tests.test_server import NEW, authed, running
from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.conversations.drafts import draft_scope
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.storage import StateError


class SyntheticCatalog(Catalog):
    def __init__(self, tmp_path: Path, *, exists: bool) -> None:
        super().__init__(tmp_path / "home", tmp_path / "vault")
        self.present = exists

    def exists(self, session_id: str) -> bool:
        return self.present


def opening(ws: WebSocketTestSession, session_id: str = NEW, **fields: Any) -> str:
    request_id = ident()
    ws.send_json(
        {
            "type": "session.open",
            "session_id": session_id,
            "request_id": request_id,
            "existing_only": True,
            **fields,
        }
    )
    return request_id


# Every existing-only open reads storage in a worker thread before it answers.
def assert_refused(
    ws: WebSocketTestSession, session_id: str, request_id: str, reason: str = "unavailable"
) -> None:
    assert recv(ws, HANG_GUARD_S) == {
        "v": 2,
        "type": "session.open.refused",
        "session_id": session_id,
        "generation": "",
        "seq": 0,
        "request_id": request_id,
        "data": {"reason": reason},
    }


def assert_synced(
    ws: WebSocketTestSession, session_id: str, request_id: str, *, wait_s: float = HANG_GUARD_S
) -> str:
    snapshot, synced = recv(ws, wait_s), recv(ws, wait_s)
    assert (snapshot["type"], synced["type"]) == ("session.snapshot", "session.synced")
    assert snapshot["session_id"] == synced["session_id"] == session_id
    assert snapshot["request_id"] == synced["request_id"] == request_id
    assert snapshot["generation"] == synced["generation"]
    return synced["generation"]


@pytest.mark.parametrize("value", [True, False])
def test_existing_only_accepts_only_boolean(value: bool) -> None:
    raw = {"type": "session.open", "session_id": NEW, "request_id": ident()}
    assert server._validate({**raw, "existing_only": value}) is not None


@pytest.mark.parametrize("value", [0, 1, "true", "false", "", None, [], {}])
def test_nonboolean_existing_only_is_dropped(value: Any) -> None:
    raw = {"type": "session.open", "session_id": NEW, "request_id": ident()}
    assert server._validate({**raw, "existing_only": value}) is None


@pytest.mark.parametrize("labelled", [False, True])
@pytest.mark.parametrize("reconnect", [False, True])
def test_unknown_or_label_only_target_refuses_without_creating_or_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labelled: bool, reconnect: bool
) -> None:
    make_driver = Mock(side_effect=AssertionError("an existing-only open must not start"))
    with running(tmp_path, make_driver=make_driver) as app:
        if labelled:
            app.manager.app_state.store.update_session_metadata(
                draft_scope(app.vault),
                NEW,
                lambda _: {
                    "session_id": NEW,
                    "revision": 1,
                    "title": "Synthetic missing conversation",
                    "pinned": 1,
                    "archived": 0,
                    "unread": 0,
                    "color": None,
                },
            )
        create = Mock(wraps=app.manager.create)
        monkeypatch.setattr(app.manager, "create", create)
        with authed(app.client) as ws:
            request_id = opening(ws, **({"generation": ident()} if reconnect else {}))
            assert_refused(ws, NEW, request_id)
        assert app.manager.get(NEW) is None
        create.assert_not_called()
        make_driver.assert_not_called()


@pytest.mark.parametrize("held", [False, True])
def test_existing_history_or_held_actor_subscribes_without_provider_start(
    tmp_path: Path, held: bool
) -> None:
    catalog = SyntheticCatalog(tmp_path, exists=not held)
    make_driver = Mock(side_effect=AssertionError("opening a view must not start"))
    with running(tmp_path, make_driver=make_driver, catalog=catalog) as app:
        previous = app.manager.create(NEW) if held else None
        with authed(app.client) as ws:
            request_id = opening(ws)
            generation = assert_synced(ws, NEW, request_id)
            actor = app.manager.get(NEW)
            assert actor is not None and actor.generation == generation
            assert not actor.live
            if held:
                assert actor is previous
            else:
                assert actor.transcript_expected
        make_driver.assert_not_called()


def test_default_open_still_creates_and_starts_new_session(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        request_id = opening(ws, existing_only=False)
        # The default path performs real store/thread work and starts the synthetic driver.
        assert_synced(ws, NEW, request_id, wait_s=HANG_GUARD_S)
        actor = app.manager.get(NEW)
        assert actor is not None and actor.live


def test_history_disappearing_during_origin_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = SyntheticCatalog(tmp_path, exists=True)
    with running(tmp_path, catalog=catalog) as app:
        read = app.manager.app_state.store.session_origin

        def disappear(session_id: str) -> str | None:
            result = read(session_id)
            catalog.present = False
            return result

        monkeypatch.setattr(app.manager.app_state.store, "session_origin", disappear)
        with authed(app.client) as ws:
            assert_refused(ws, NEW, opening(ws))
        assert app.manager.get(NEW) is None


def test_history_disappearing_at_last_catalog_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = SyntheticCatalog(tmp_path, exists=True)
    reads = 0

    def disappear(session_id: str) -> bool:
        nonlocal reads
        reads += 1
        present = catalog.present
        catalog.present = False
        return present

    monkeypatch.setattr(catalog, "exists", disappear)
    with running(tmp_path, catalog=catalog) as app, authed(app.client) as ws:
        assert_refused(ws, NEW, opening(ws))
        assert app.manager.get(NEW) is None
        assert reads >= 2


def test_source_loss_after_worker_proof_reports_missing_history_without_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = SyntheticCatalog(tmp_path, exists=True)
    make_driver = Mock(side_effect=AssertionError("opening a view must not start a provider"))
    with running(tmp_path, catalog=catalog, make_driver=make_driver) as app:
        real_to_thread = asyncio.to_thread

        async def disappear_after_proof(function: Any, *args: Any, **kwargs: Any) -> Any:
            result = await real_to_thread(function, *args, **kwargs)
            if function.__name__ == "existing_session_source":
                catalog.present = False
            return result

        monkeypatch.setattr(server.asyncio, "to_thread", disappear_after_proof)
        with authed(app.client) as ws:
            generation = assert_synced(ws, NEW, opening(ws))
            actor = app.manager.get(NEW)
            assert actor is not None and not actor.live
            request_id = ident()
            ws.send_json(
                {
                    "type": "history.more",
                    "session_id": NEW,
                    "generation": generation,
                    "request_id": request_id,
                }
            )
            page = recv(ws, HANG_GUARD_S)
            assert page["type"] == "history.page" and page["request_id"] == request_id
            assert page["data"]["error"] == "transcript_missing"
        make_driver.assert_not_called()


@pytest.mark.parametrize("error", [StateError("busy"), OSError("synthetic failure")])
def test_storage_failure_refuses_explicitly_without_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    with running(tmp_path) as app:
        read = Mock(side_effect=error)
        monkeypatch.setattr(app.manager.app_state.store, "session_origin", read)
        with authed(app.client) as ws:
            assert_refused(ws, NEW, opening(ws), "storage_unavailable")
        assert app.manager.get(NEW) is None


def test_rechecks_held_actor_after_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = SyntheticCatalog(tmp_path, exists=True)
    with running(tmp_path, catalog=catalog) as app:
        real_to_thread = asyncio.to_thread
        replacement: list[Any] = []

        async def attach_during_read(function: Any, *args: Any, **kwargs: Any) -> Any:
            result = await real_to_thread(function, *args, **kwargs)
            if not replacement and function.__name__ == "existing_session_source":
                replacement.append(app.manager.create(NEW))
                catalog.present = False
            return result

        monkeypatch.setattr(server.asyncio, "to_thread", attach_during_read)
        with authed(app.client) as ws:
            assert_synced(ws, NEW, opening(ws), wait_s=HANG_GUARD_S)
        assert app.manager.get(NEW) is replacement[0]


@pytest.mark.parametrize("started", [False, True])
def test_unheld_profile_target_requires_matching_prepared_or_history_state(
    tmp_path: Path, started: bool
) -> None:
    rig, session_id = Rig(tmp_path), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url="http://127.0.0.1:8765") as client:
        rig.preview(client, session_id, ident())
        manager._actors.pop(session_id)
        if started:
            with rig.state.store._connection(write=True) as connection:
                connection.execute(
                    "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?",
                    (session_id,),
                )
            rig.catalog.transcripts.add(session_id)
        with authed_profile(client) as ws:
            assert_synced(ws, session_id, opening(ws, session_id))
        actor = manager.get(session_id)
        assert actor is not None and actor.transcript_expected is started and not actor.live
    assert rig.drivers == []


def test_profile_history_disappearing_during_reopenability_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, session_id = Rig(tmp_path), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url="http://127.0.0.1:8765") as client:
        rig.preview(client, session_id, ident())
        manager._actors.pop(session_id)
        with rig.state.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?",
                (session_id,),
            )
        rig.catalog.transcripts.add(session_id)
        read = ProfileLaunchService.reopenable

        def disappear(service: ProfileLaunchService, sid: str, exists: bool) -> None:
            read(service, sid, exists)
            rig.catalog.transcripts.discard(sid)

        monkeypatch.setattr(ProfileLaunchService, "reopenable", disappear)
        with authed_profile(client) as ws:
            assert_refused(ws, session_id, opening(ws, session_id))
        assert manager.get(session_id) is None
    assert rig.drivers == []


def test_started_profile_missing_transcript_refuses_without_provider_start(tmp_path: Path) -> None:
    rig, session_id = Rig(tmp_path), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url="http://127.0.0.1:8765") as client:
        rig.preview(client, session_id, ident())
        manager._actors.pop(session_id)
        with rig.state.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?",
                (session_id,),
            )
        with authed_profile(client) as ws:
            assert_refused(ws, session_id, opening(ws, session_id))
        assert manager.get(session_id) is None
    assert rig.drivers == []


@pytest.mark.parametrize("mutation", ["removed", "started_without_history"])
def test_prepared_binding_changing_during_reopenability_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    rig, session_id = Rig(tmp_path), ident()
    manager = rig.manager()
    with TestClient(rig.app(manager), base_url="http://127.0.0.1:8765") as client:
        rig.preview(client, session_id, ident())
        manager._actors.pop(session_id)
        read = ProfileLaunchService.reopenable
        removed = False

        def disappear(service: ProfileLaunchService, sid: str, exists: bool) -> None:
            nonlocal removed
            read(service, sid, exists)
            if not removed:
                with rig.state.store._connection(write=True) as connection:
                    if mutation == "removed":
                        connection.execute(
                            "DELETE FROM session_profile_bindings WHERE session_id=?", (sid,)
                        )
                    else:
                        connection.execute(
                            "UPDATE session_profile_bindings SET native_started=1 "
                            "WHERE session_id=?",
                            (sid,),
                        )
                removed = True

        monkeypatch.setattr(ProfileLaunchService, "reopenable", disappear)
        with authed_profile(client) as ws:
            assert_refused(
                ws,
                session_id,
                opening(ws, session_id),
                "storage_unavailable" if mutation == "removed" else "unavailable",
            )
        assert manager.get(session_id) is None
    assert rig.drivers == []
