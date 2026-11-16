"""The decision inbox route over a stub service, plus one full-app check that a protected vault's
memory promotion reaches it as a `vault_proposal` item.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.vaults.test_vault_knowledge_integration import HEADERS as BEARER
from tests.vaults.test_vault_knowledge_integration import launch
from viki_agent.domain import ActiveWorkspace
from viki_agent.storage import StateError
from viki_agent.vaults.decision_api import MAX_DECISION_REQUESTS, decision_routes
from viki_agent.vaults.decisions import DecisionError, DecisionService
from viki_agent.workspaces import WorkspaceService

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"


def _workspaces(*, chat_only: bool = False) -> WorkspaceService:
    return cast(
        WorkspaceService,
        SimpleNamespace(resolve_active=lambda: SimpleNamespace(chat_only=chat_only)),
    )


def _service(*, chat_only: bool = False, proposal_items: Any = None) -> DecisionService:
    kwargs = {} if proposal_items is None else {"proposal_items": proposal_items}
    return DecisionService(_workspaces(chat_only=chat_only), **kwargs)


def _app(decisions: object, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=decision_routes(
            cast(DecisionService, decisions),
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set[asyncio.Task[Any]]() if workers is None else workers,
        )
    )


@pytest.fixture
def harness() -> TestClient:
    return TestClient(_app(_service()))


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_the_inbox_answers_only_the_proposal_items() -> None:
    items = [{"kind": "vault_proposal", "id": "a"}, {"kind": "skill_proposal", "id": "b"}]
    client = TestClient(_app(_service(proposal_items=lambda: items)))

    inbox = client.get("/api/v2/decisions", headers=AUTH)

    assert inbox.status_code == 200 and inbox.headers["cache-control"] == "no-store"
    assert inbox.json() == {"items": items}


def test_a_chat_only_workspace_is_422() -> None:
    client = TestClient(_app(_service(chat_only=True)))

    refused = client.get("/api/v2/decisions", headers=AUTH)

    assert refused.status_code == 422 and _code(refused) == "no_vault"


class _Raising:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def inbox(self) -> None:
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DecisionError("no_vault"), 422, "no_vault"),
        (DecisionError("unmapped_code"), 503, "unmapped_code"),
        (StateError("busy"), 503, "unavailable"),
    ],
)
def test_the_status_map_and_the_unmapped_fallback(error: Exception, status: int, code: str) -> None:
    client = TestClient(_app(_Raising(error)))

    refused = client.get("/api/v2/decisions", headers=AUTH)

    assert (refused.status_code, _code(refused)) == (status, code)


def test_a_query_string_is_400(harness: TestClient) -> None:
    refused = harness.get("/api/v2/decisions?x=1", headers=AUTH)

    assert refused.status_code == 400 and _code(refused) == "invalid_request"


def test_missing_or_wrong_bearer_is_401(harness: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        refused = harness.get("/api/v2/decisions", headers=headers)
        assert refused.status_code == 401 and _code(refused) == "unauthorized"
        assert refused.headers["cache-control"] == "no-store"


def test_a_foreign_origin_is_403_but_an_absent_one_is_allowed(harness: TestClient) -> None:
    foreign = {**AUTH, "Origin": "https://evil.invalid"}

    assert harness.get("/api/v2/decisions", headers=foreign).status_code == 403
    assert harness.get("/api/v2/decisions", headers=AUTH).status_code == 200
    assert harness.get("/api/v2/decisions", headers={**AUTH, "Origin": ORIGIN}).status_code == 200


def test_every_refusal_releases_its_permit(harness: TestClient) -> None:
    # The limiter holds four permits; more refusals than that would 429 if one leaked.
    for _ in range(MAX_DECISION_REQUESTS * 3):
        assert harness.get("/api/v2/decisions?x=1", headers=AUTH).status_code == 400
    assert harness.get("/api/v2/decisions", headers=AUTH).status_code == 200


async def test_a_full_limiter_answers_429_and_is_shielded_and_tracked() -> None:
    entered = 0
    lock = threading.Lock()
    all_started = threading.Event()
    release = threading.Event()

    class Blocking:
        def inbox(self) -> dict[str, Any]:
            nonlocal entered
            with lock:
                entered += 1
                if entered == MAX_DECISION_REQUESTS:
                    all_started.set()
            release.wait(5)
            return {"items": []}

    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(Blocking(), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.get("/api/v2/decisions", headers=AUTH))
            for _ in range(MAX_DECISION_REQUESTS)
        ]
        assert await asyncio.to_thread(all_started.wait, 2)
        assert len(workers) == MAX_DECISION_REQUESTS
        overloaded = await client.get("/api/v2/decisions", headers=AUTH)
        assert overloaded.status_code == 429 and _code(overloaded) == "overloaded"
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers


def test_a_memory_promotion_lists_and_opens_with_no_base_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A protected vault's memory promotion is a new-note proposal, so the inbox lists it as a
    `vault_proposal` and its detail (a route `decision_api.py` does not own) meets it with
    `base_revision: null`."""
    with launch(tmp_path, monkeypatch) as app:
        folder = app.vaults / "guarded"
        folder.mkdir()
        (folder / "note.md").write_bytes(b"# A guarded synthetic note\n")
        workspace, _ = app.spaces.register_existing(str(uuid.uuid4()), str(folder))
        workspace = app.spaces.set_policy(
            str(uuid.uuid4()), workspace.workspace_id, workspace.policy_revision, "protected", []
        )
        # The inbox needs an active vault, which a launch resolves once: this one, as if relaunched.
        active = ActiveWorkspace(workspace, folder, workspace.workspace_id, "registration", False)
        monkeypatch.setattr(app.spaces, "resolve_active", lambda: active)
        created = app.create_memory(workspace)
        assert created.status_code == 201, created.text
        promoted = created.json()
        assert promoted["outcome"] == "proposed"

        inbox = app.client.get("/api/v2/decisions", headers=BEARER)
        detail = app.client.get(
            f"/api/v2/decisions/proposals/{promoted['proposal_id']}", headers=BEARER
        )

        assert inbox.status_code == 200, inbox.text
        assert detail.status_code == 200, detail.text
        listed = [item for item in inbox.json()["items"] if item["kind"] == "vault_proposal"]
        opened = detail.json()["proposal"]
        assert len(listed) == 1
        for item in (*listed, opened):
            assert item["id"] == promoted["proposal_id"]
            assert item["origin_kind"] == "memory_promotion"
            assert item["base_revision"] is None
            assert item["ref"] == promoted["ref"]
            assert item["revision"] == promoted["proposed_sha256"]
            assert item["state"] == "open"
        assert opened["current_revision"] is None
