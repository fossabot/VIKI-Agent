"""The skill proposal routes over a real service, a real native inspector and real sealing.

The proposed text is the user's skill content, so every row that could leak it (a create answer,
an error body, the log) is checked against a marker string.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient

from tests.profiles.test_native_assets import _root, _workspace, _write
from viki_agent.domain import ActiveWorkspace, CanonicalRoot, Workspace
from viki_agent.profiles.native_assets import NativeAssetInspector
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.skill_proposal_api import (
    MAX_REQUEST_BYTES,
    MAX_SKILL_REQUESTS,
    skill_proposal_routes,
)
from viki_agent.vaults.skill_proposals import MAX_SKILL_BYTES, SkillProposalService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
POST = {**AUTH, "Origin": ORIGIN}
SECRET = "SECRET-WEEKLY-STEPS-9f2"
NAME = "weekly-recap"
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _skill(name: str = NAME, body: str = SECRET) -> str:
    return f"---\nname: {name}\ndescription: Runs the weekly recap\n---\n{body}\n"


TEXT = _skill()


class _Workspaces:
    """The one active, persistent vault workspace every call resolves against."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def resolve_active(self) -> ActiveWorkspace:
        w = self.workspace
        return ActiveWorkspace(w, Path(w.root.display), w.workspace_id, "registration", False)

    def get(self, workspace_id: str) -> Workspace:
        if workspace_id != self.workspace.workspace_id:
            raise WorkspaceError("unknown_workspace")
        return self.workspace

    def current_root(self, workspace: Workspace) -> CanonicalRoot:
        return workspace.root


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.vault = tmp_path / "vault"
        self.home = tmp_path / "home"
        self.vault.mkdir()
        self.home.mkdir()
        self.spaces = _Workspaces(_workspace(self.vault))
        state = AppState(tmp_path / "state.json", Prefs())
        self.store = state.store
        inspector = NativeAssetInspector(self.spaces, claude_home=_root(self.home))
        self.service = SkillProposalService(
            cast(WorkspaceService, self.spaces), self.store, inspector
        )
        self.routes = skill_proposal_routes(
            self.service,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set[asyncio.Task[Any]](),
        )
        self.client = TestClient(Starlette(routes=self.routes))

    @property
    def wid(self) -> str:
        return self.spaces.workspace.workspace_id

    @property
    def create_url(self) -> str:
        return f"/api/v2/vaults/{self.wid}/skill-proposals"

    def detail_url(self, proposal_id: str) -> str:
        return f"/api/v2/decisions/skills/{proposal_id}"

    def body(
        self, text: str = TEXT, trigger: str = "button", sessions: list[str] | None = None
    ) -> dict[str, Any]:
        return {"text": text, "trigger": trigger, "sessions": sessions or [SESSION]}

    def create(self, **kwargs: Any) -> Any:
        return self.client.post(self.create_url, headers=POST, json=self.body(**kwargs))

    def act(self, proposal_id: str, action: str, revision: str, **extra: Any) -> Any:
        return self.client.post(
            self.detail_url(proposal_id),
            headers=POST,
            json={"action": action, "revision": revision, **extra},
        )

    def direct(self, path: str, method: str, proposal_id: str, *, body: bytes = b"") -> Response:
        """Drive one route's own endpoint from a hand-built ASGI scope, bypassing the test
        client's URL encoder: it refuses to carry a lone surrogate over the wire at all, but
        that is a client-side nicety, not a guarantee every ASGI front end gives the route."""
        route = next(r for r in self.routes if r.path == path)
        headers = [(b"authorization", b"Bearer secret"), (b"origin", ORIGIN.encode())]
        if body:
            headers.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "method": method,
            "path": "/direct",
            "raw_path": b"/direct",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "headers": headers,
            "path_params": {"proposal_id": proposal_id},
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        return asyncio.run(route.endpoint(Request(scope, receive)))


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_create_answers_201_and_never_echoes_the_text(harness: Harness) -> None:
    created = harness.create()

    assert created.status_code == 201
    proposal = created.json()["proposal"]
    assert uuid.UUID(proposal["id"]) and proposal["state"] == "open"
    assert proposal["kind"] == "skill_proposal" and proposal["name"] == NAME
    assert "text" not in proposal and "current_text" not in proposal
    assert "SECRET" not in created.text
    assert created.headers["cache-control"] == "no-store"


def test_a_replay_answers_200_with_the_same_id(harness: Harness) -> None:
    first = harness.create()
    replay = harness.create()

    assert first.status_code == 201 and replay.status_code == 200
    assert replay.json()["proposal"]["id"] == first.json()["proposal"]["id"]
    assert replay.json()["replayed"] is True and "SECRET" not in replay.text


@pytest.mark.parametrize("approved", [False, True], ids=["pending", "approved_not_applied"])
def test_a_conflicting_text_for_the_same_name_is_409_naming_the_other_proposal(
    harness: Harness, approved: bool
) -> None:
    first = harness.create().json()["proposal"]
    if approved:
        assert harness.act(first["id"], "approve", first["revision"]).status_code == 200
    other = harness.create(text=_skill(body="SECRET-OTHER"))

    assert other.status_code == 409 and _code(other) == "proposal_conflict"
    assert other.json()["error"]["details"] == {"proposal_id": first["id"]}
    assert "SECRET" not in other.text


def test_an_applied_proposal_makes_way_for_its_next_version(harness: Harness) -> None:
    """Propose, approve and apply (the user's chat writes the file), then propose a new version
    of the same skill: a new proposal over the file as it is now, and the applied one is gone."""
    first = harness.create().json()["proposal"]
    approved = harness.act(first["id"], "approve", first["revision"])
    assert approved.json()["proposal"]["state"] == "handed"
    _write(harness.vault / ".claude" / "skills" / NAME / "SKILL.md", TEXT)

    created = harness.create(text=_skill(body="SECRET-NEXT-STEPS"), trigger="correction")

    assert created.status_code == 201 and "SECRET" not in created.text
    proposal = created.json()["proposal"]
    assert (proposal["title"], proposal["state"]) == (f"Skill change: {NAME}", "open")
    assert proposal["base_revision"] == hashlib.sha256(TEXT.encode()).hexdigest()
    detail = harness.client.get(harness.detail_url(proposal["id"]), headers=AUTH)
    found = detail.json()["proposal"]
    assert (found["base_revision"], found["current_text"]) == (proposal["base_revision"], TEXT)
    assert {f"-{SECRET}", "+SECRET-NEXT-STEPS"} <= set(found["diff"].splitlines())
    gone = harness.client.get(harness.detail_url(first["id"]), headers=AUTH)
    assert gone.status_code == 404 and _code(gone) == "unknown_proposal"


def test_detail_carries_the_text(harness: Harness) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    detail = harness.client.get(harness.detail_url(proposal_id), headers=AUTH)

    assert detail.status_code == 200
    proposal = detail.json()["proposal"]
    assert proposal["text"] == TEXT and SECRET in proposal["text"]
    assert detail.headers["cache-control"] == "no-store"


def test_withdraw_removes_the_proposal(harness: Harness) -> None:
    created = harness.create().json()["proposal"]

    withdrawn = harness.act(created["id"], "withdraw", created["revision"])

    assert withdrawn.status_code == 200 and withdrawn.json() == {"withdrawn": True}
    gone = harness.client.get(harness.detail_url(created["id"]), headers=AUTH)
    assert gone.status_code == 404 and _code(gone) == "unknown_proposal"


def test_unknown_proposal_is_404(harness: Harness) -> None:
    missing = str(uuid.uuid4())

    assert harness.client.get(harness.detail_url(missing), headers=AUTH).status_code == 404
    assert harness.act(missing, "withdraw", "0" * 64).status_code == 404


def test_a_text_over_the_cap_is_413_and_not_echoed(harness: Harness) -> None:
    big = harness.create(text=_skill(body="SECRET" + "x" * MAX_SKILL_BYTES))

    assert big.status_code == 413 and _code(big) == "too_large"
    assert big.json()["error"]["details"] == {"limit": MAX_SKILL_BYTES}
    assert "SECRET" not in big.text


def test_an_envelope_over_the_intake_bound_is_413_and_not_echoed(harness: Harness) -> None:
    raw = json.dumps({**harness.body(), "text": "SECRET" + "x" * MAX_REQUEST_BYTES}).encode()

    big = harness.client.post(
        harness.create_url, headers={**POST, "Content-Type": "application/json"}, content=raw
    )

    assert big.status_code == 413 and _code(big) == "too_large"
    assert "SECRET" not in big.text


def test_a_bad_representation_is_415(harness: Harness) -> None:
    url = harness.create_url
    raw = json.dumps(harness.body()).encode()
    for headers in (
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ):
        refused = harness.client.post(url, headers={**POST, **headers}, content=raw)
        assert refused.status_code == 415 and _code(refused) == "invalid_representation"
    missing = harness.client.post(url, headers=POST, content=b"")
    assert missing.status_code == 415
    proposal_id = harness.create().json()["proposal"]["id"]
    detail_url = harness.detail_url(proposal_id)
    assert harness.client.request("GET", detail_url, headers=AUTH, content=b"{}").status_code == 415
    form = harness.client.post(
        detail_url, headers={**POST, "Content-Type": "text/plain"}, content=b"x"
    )
    assert form.status_code == 415
    assert "SECRET" not in refused.text


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"text":"t","text":"u","trigger":"button","sessions":[]}',
        b'{"text":"t","trigger":"button"}',
        b'{"text":"t","trigger":"button","sessions":[],"extra":1}',
        b'{"text":1,"trigger":"button","sessions":[]}',
        b'{"text":"t","trigger":1,"sessions":[]}',
        b'{"text":"t","trigger":"button","sessions":"x"}',
        b"\xff\xfe",
    ],
)
def test_a_malformed_create_body_is_400(harness: Harness, raw: bytes) -> None:
    refused = harness.client.post(
        harness.create_url, headers={**POST, "Content-Type": "application/json"}, content=raw
    )

    assert refused.status_code == 400 and _code(refused) == "invalid_request"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"action": "approve"},
        {"revision": "0" * 64},
        {"action": "approve", "revision": "0" * 64, "text": "t", "reason": "r"},
        {"action": "approve", "revision": "0" * 64, "extra": 1},
        {"action": 1, "revision": "0" * 64},
        {"action": "approve", "revision": 1},
        {"action": "approve", "revision": "0" * 64, "text": 1},
    ],
)
def test_a_malformed_act_body_is_400(harness: Harness, body: dict[str, Any]) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    refused = harness.client.post(harness.detail_url(proposal_id), headers=POST, json=body)

    assert refused.status_code == 400 and _code(refused) == "invalid_request"


def test_a_non_canonical_proposal_id_is_400_on_both_routes(harness: Harness) -> None:
    # An empty id is left out: Starlette's own router never matches an empty path segment, so
    # that case 404s before any route code runs, rather than exercising the id guard.
    for bad in ("not-a-uuid", "12345678-1234-1234-1234-12345678901", "0" * 64):
        got = harness.client.get(harness.detail_url(bad), headers=AUTH)
        assert (got.status_code, _code(got)) == (400, "invalid_request"), bad
        acted = harness.act(bad, "withdraw", "0" * 64)
        assert (acted.status_code, _code(acted)) == (400, "invalid_request"), bad


def test_a_lone_surrogate_proposal_id_is_400_not_a_crash(harness: Harness) -> None:
    # A row lookup hands this id to SQLite as-is; unguarded, encoding it to UTF-8 for the bind
    # parameter raises UnicodeEncodeError deep inside `detail`/`act` instead of answering 400.
    surrogate = "\ud800"
    url = "/api/v2/decisions/skills/{proposal_id}"

    detail = harness.direct(url, "GET", surrogate)
    assert detail.status_code == 400
    assert json.loads(bytes(detail.body))["error"]["code"] == "invalid_request"

    body = json.dumps({"action": "withdraw", "revision": "0" * 64}).encode()
    acted = harness.direct(url, "POST", surrogate, body=body)
    assert acted.status_code == 400
    assert json.loads(bytes(acted.body))["error"]["code"] == "invalid_request"


def test_missing_bearer_is_401_on_every_route(harness: Harness) -> None:
    detail_url = harness.detail_url(str(uuid.uuid4()))
    calls = (
        harness.client.post(harness.create_url, json=harness.body()),
        harness.client.get(detail_url),
        harness.client.post(detail_url, json={"action": "withdraw", "revision": "0" * 64}),
        harness.client.post(harness.create_url, headers={"Authorization": "Bearer wrong"}),
    )
    for call in calls:
        assert call.status_code == 401 and _code(call) == "unauthorized"
        assert "SECRET" not in call.text


def test_a_foreign_origin_is_403_on_every_route_and_an_absent_one_on_each_get(
    harness: Harness,
) -> None:
    detail_url = harness.detail_url(str(uuid.uuid4()))
    foreign = {**AUTH, "Origin": "https://evil.invalid"}
    for headers in (foreign, AUTH):
        for call in (
            harness.client.post(harness.create_url, headers=headers, json=harness.body()),
            harness.client.post(
                detail_url, headers=headers, json={"action": "withdraw", "revision": "0" * 64}
            ),
        ):
            assert call.status_code == 403 and _code(call) == "forbidden_origin"
    assert harness.client.get(detail_url, headers=foreign).status_code == 403
    # Nothing was created by the refused creates, and the GET still accepts an absent Origin.
    assert harness.create().status_code == 201
    assert harness.client.get(detail_url, headers=AUTH).status_code == 404
    assert harness.client.get(detail_url, headers={**AUTH, "Origin": ORIGIN}).status_code == 404


def test_every_refusal_releases_its_permit(harness: Harness) -> None:
    # The limiter holds four permits; more refusals than that would 429 if one leaked.
    for _ in range(12):
        refused = harness.client.post(harness.create_url, headers=POST, content=b"x")
        assert refused.status_code == 415
        bad = harness.client.post(
            harness.create_url,
            headers={**POST, "Content-Type": "application/json"},
            content=b"nope",
        )
        assert bad.status_code == 400
    assert harness.create().status_code == 201


async def test_a_full_limiter_answers_429_and_is_shielded_and_tracked() -> None:
    """Four requests in flight hold every permit, so a fifth is refused at once."""
    assert MAX_SKILL_REQUESTS == 4
    entered = 0
    lock = threading.Lock()
    all_started = threading.Event()
    release = threading.Event()

    class Blocking:
        def detail(self, proposal_id: str) -> dict[str, Any]:
            nonlocal entered
            with lock:
                entered += 1
                if entered == MAX_SKILL_REQUESTS:
                    all_started.set()
            release.wait(5)
            return {"id": proposal_id}

    workers: set[asyncio.Task[Any]] = set()
    routes = skill_proposal_routes(
        cast(SkillProposalService, Blocking()),
        authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
        origins=frozenset({ORIGIN}),
        workers=workers,
    )
    url = f"/api/v2/decisions/skills/{uuid.uuid4()}"
    transport = httpx.ASGITransport(app=Starlette(routes=routes))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.get(url, headers=AUTH)) for _ in range(MAX_SKILL_REQUESTS)
        ]
        assert await asyncio.to_thread(all_started.wait, 2)
        assert len(workers) == MAX_SKILL_REQUESTS
        overloaded = await client.get(url, headers=AUTH)
        assert overloaded.status_code == 429 and _code(overloaded) == "overloaded"
        assert overloaded.headers["cache-control"] == "no-store"
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers


def test_no_proposal_text_reaches_the_log(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    created = harness.create().json()["proposal"]
    harness.create()
    harness.create(text=_skill(name="other-skill", body="SECRET-TWO"))
    harness.client.get(harness.detail_url(created["id"]), headers=AUTH)
    harness.act(created["id"], "withdraw", created["revision"])

    assert "SECRET" not in caplog.text
