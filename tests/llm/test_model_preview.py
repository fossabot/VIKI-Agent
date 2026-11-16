from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_server import NEW, EchoDriver, authed, recv, running
from viki_agent import server
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError

FIRST = "viki-llm/11111111-1111-4111-8111-111111111111/model-a"
SECOND = "viki-llm/22222222-2222-4222-8222-222222222222/model-b"


@pytest.mark.parametrize("model", [None, "default", "sonnet", "viki-llm/example/model"])
def test_open_accepts_an_exact_nullable_model_preview(model: str | None) -> None:
    message = {
        "type": "session.open",
        "session_id": NEW,
        "request_id": str(uuid.uuid4()),
        "expected_model": model,
    }
    assert server._validate(message) == ("session.open", message)


@pytest.mark.parametrize("model", [False, 3, {}, [], "", "a" * 1025, "\ud800"])
def test_open_refuses_an_invalid_model_preview(model: object) -> None:
    assert (
        server._validate(
            {
                "type": "session.open",
                "session_id": NEW,
                "request_id": str(uuid.uuid4()),
                "expected_model": model,
            }
        )
        is None
    )


@pytest.mark.parametrize("preview_kind", ["custom", "native", "unset"])
def test_changed_default_refuses_open_before_any_driver_or_subscription_barrier(
    tmp_path: Path, preview_kind: str
) -> None:
    made: list[str | None] = []

    def make_driver(launch, prefs, emit):
        made.append(prefs.model)
        return EchoDriver(launch, emit)

    with running(tmp_path, make_driver=make_driver) as app:
        state = app.manager.app_state
        state.set_defaults(
            replace(
                state.defaults,
                model={"custom": FIRST, "native": "sonnet", "unset": None}[preview_kind],
            )
        )
        preview = state.prefs(NEW).model
        assert not state.ours(NEW)
        state.set_defaults(replace(state.defaults, model=SECOND))

        with authed(app.client) as ws:
            request_id = str(uuid.uuid4())
            ws.send_json(
                {
                    "type": "session.open",
                    "session_id": NEW,
                    "request_id": request_id,
                    "expected_model": preview,
                }
            )
            refused = recv(ws)
            assert refused["type"] == "session.open.refused"
            assert refused["request_id"] == request_id
            assert refused["data"] == {"reason": "model_review_required"}
            assert not made
            assert app.manager.get(NEW) is None
            assert not app.manager.app_state.ours(NEW)

            # Explicit reopening can show the current model without sending the draft.
            ws.send_json(
                {"type": "session.open", "session_id": NEW, "request_id": str(uuid.uuid4())}
            )
            assert recv(ws)["type"] == "session.snapshot"
            assert recv(ws)["type"] == "session.synced"
            assert made == [SECOND]
            assert state.launch_prefs(NEW).model == SECOND


def test_existing_actor_refuses_a_stale_preview_without_removing_its_owner(tmp_path: Path) -> None:
    made: list[str | None] = []

    def make_driver(launch, prefs, emit):
        made.append(prefs.model)
        return EchoDriver(launch, emit)

    with running(tmp_path, make_driver=make_driver) as app:
        actor = app.manager.create(NEW)
        with authed(app.client) as ws:
            ws.send_json(
                {
                    "type": "session.open",
                    "session_id": NEW,
                    "request_id": str(uuid.uuid4()),
                    "expected_model": "sonnet",
                }
            )
            refused = recv(ws)
            assert refused["type"] == "session.open.refused"
            assert refused["data"] == {"reason": "model_review_required"}
            assert app.manager.get(NEW) is actor
            assert not actor.live and not made


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("error", [StateError("unavailable"), ProtectionError()])
def test_unavailable_model_check_gives_no_barrier_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool, error: Exception
) -> None:
    made: list[object] = []

    def make_driver(launch, prefs, emit):
        made.append(prefs.model)
        return EchoDriver(launch, emit)

    with running(tmp_path, make_driver=make_driver) as app:
        state = app.manager.app_state
        state.set_defaults(replace(state.defaults, model="sonnet"))
        actor = app.manager.create(NEW) if held else None

        def unavailable(*args, **kwargs):
            raise error

        monkeypatch.setattr(state, "launch_prefs" if held else "remember", unavailable)
        with authed(app.client) as ws:
            ws.send_json(
                {
                    "type": "session.open",
                    "session_id": NEW,
                    "request_id": str(uuid.uuid4()),
                    "expected_model": "sonnet",
                }
            )
            refused = recv(ws)
            assert refused["type"] == "session.open.refused"
            assert refused["data"] == {"reason": "model_review_required"}
            assert not made
            assert app.manager.get(NEW) is actor
