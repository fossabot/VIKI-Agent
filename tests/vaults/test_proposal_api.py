"""The proposal routes over a real store, a real vault reader and real sealing.

The proposed text is the user's note content, so every row that could leak it (a create answer,
an error body, the log) is checked against a marker string.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.domain import CanonicalRoot
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.proposal_api import MAX_REQUEST_BYTES, proposal_routes
from viki_agent.vaults.proposals import MAX_PROPOSAL_BYTES, ProposalService
from viki_agent.vaults.vault import VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
POST = {**AUTH, "Origin": ORIGIN}
SECRET = "SECRET-PROPOSED-LINE\n"
BASE_TEXT = "original line\n"
REF = "notes/a.md"


class _Workspaces:
    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}

    def add(self, root: Path, policy: str = "protected") -> str:
        info = os.stat(root)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            policy=policy,
            root=CanonicalRoot(
                key=root_key(root.resolve()),
                display=str(root.resolve()),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        return workspace_id

    def get(self, workspace_id: str) -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root

    def list(self) -> list[Any]:
        return list(self.spaces.values())


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "vault"
        self.root.mkdir()
        self.spaces = _Workspaces()
        self.wid = self.spaces.add(self.root)
        state = AppState(tmp_path / "state.json", Prefs())
        service = ProposalService(
            cast(WorkspaceService, self.spaces),
            state.store,
            VaultService(cast(WorkspaceService, self.spaces)),
        )
        routes = proposal_routes(
            service,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set[asyncio.Task[Any]](),
        )
        self.client = TestClient(Starlette(routes=routes))

    @property
    def create_url(self) -> str:
        return f"/api/v2/vaults/{self.wid}/proposals"

    def note(self, text: str, ref: str = REF) -> str:
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def body(self, text: str = SECRET, base: str | None = None, ref: str = REF) -> dict[str, str]:
        return {"ref": ref, "text": text, "base_revision": base or self.note(BASE_TEXT, ref)}

    def create(self, **kwargs: Any) -> Any:
        return self.client.post(self.create_url, headers=POST, json=self.body(**kwargs))

    def act(self, proposal_id: str, action: str) -> Any:
        return self.client.post(
            f"/api/v2/decisions/proposals/{proposal_id}", headers=POST, json={"action": action}
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_create_answers_201_with_its_id_and_never_echoes_the_text(harness: Harness) -> None:
    created = harness.create()

    assert created.status_code == 201
    proposal = created.json()["proposal"]
    assert uuid.UUID(proposal["id"]) and proposal["state"] == "open" and proposal["ref"] == REF
    assert "proposed_text" not in proposal and "diff" not in proposal
    assert "SECRET" not in created.text
    assert created.headers["cache-control"] == "no-store"


def test_a_replay_answers_200_with_the_same_id(harness: Harness) -> None:
    first = harness.create()
    replay = harness.create()

    assert first.status_code == 201 and replay.status_code == 200
    assert replay.json()["proposal"]["id"] == first.json()["proposal"]["id"]
    assert replay.json()["replayed"] is True and "SECRET" not in replay.text


def test_a_different_text_for_the_same_note_is_a_409_naming_the_other_proposal(
    harness: Harness,
) -> None:
    first = harness.create()
    other = harness.create(text="SECRET-OTHER\n", base=harness.note(BASE_TEXT))

    assert other.status_code == 409 and _code(other) == "proposal_conflict"
    assert other.json()["error"]["details"] == {"proposal_id": first.json()["proposal"]["id"]}
    assert "SECRET" not in other.text


def test_detail_carries_the_diff_and_the_unsealed_text(harness: Harness) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    detail = harness.client.get(f"/api/v2/decisions/proposals/{proposal_id}", headers=AUTH)

    assert detail.status_code == 200
    proposal = detail.json()["proposal"]
    assert proposal["proposed_text"] == SECRET and proposal["diff_reason"] is None
    assert "-original line" in proposal["diff"] and "+SECRET-PROPOSED-LINE" in proposal["diff"]
    assert detail.headers["cache-control"] == "no-store"


def test_handoff_records_the_hand_off(harness: Harness) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    handed = harness.act(proposal_id, "handoff")

    assert handed.status_code == 200
    assert handed.json()["proposal"]["state"] == "handed" and handed.json()["proposal"]["handed_at"]
    assert "SECRET" not in handed.text


def test_handoff_of_a_note_changed_since_is_409_stale_proposal(harness: Harness) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]
    harness.note("someone else edited\n")

    stale = harness.act(proposal_id, "handoff")

    assert stale.status_code == 409 and _code(stale) == "stale_proposal"
    assert "SECRET" not in stale.text


def test_withdraw_removes_the_proposal(harness: Harness) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    assert harness.act(proposal_id, "withdraw").json() == {"withdrawn": True}

    gone = harness.client.get(f"/api/v2/decisions/proposals/{proposal_id}", headers=AUTH)
    assert gone.status_code == 404 and _code(gone) == "unknown_proposal"
    assert _code(harness.act(proposal_id, "withdraw")) == "unknown_proposal"


def test_a_text_over_the_cap_is_413_and_not_echoed(harness: Harness) -> None:
    text = "SECRET" + "x" * MAX_PROPOSAL_BYTES

    big = harness.create(text=text)

    assert big.status_code == 413 and _code(big) == "too_large"
    assert big.json()["error"]["details"] == {"limit": MAX_PROPOSAL_BYTES}
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
    detail = f"/api/v2/decisions/proposals/{proposal_id}"
    assert harness.client.request("GET", detail, headers=AUTH, content=b"{}").status_code == 415
    form = harness.client.post(detail, headers={**POST, "Content-Type": "text/plain"}, content=b"x")
    assert form.status_code == 415
    assert "SECRET" not in refused.text


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"ref":"a.md","ref":"b.md","text":"t","base_revision":"b"}',
        b'{"ref":"a.md","text":"t"}',
        b'{"ref":"a.md","text":"t","base_revision":"b","extra":1}',
        b'{"ref":1,"text":"t","base_revision":"b"}',
        b'{"ref":"a.md","text":null,"base_revision":"b"}',
        b'{"ref":"a.md","text":"t","base_revision":"short"}',
        b"\xff\xfe",
    ],
)
def test_a_malformed_create_body_is_400(harness: Harness, raw: bytes) -> None:
    refused = harness.client.post(
        harness.create_url, headers={**POST, "Content-Type": "application/json"}, content=raw
    )

    assert refused.status_code == 400 and _code(refused) == "invalid_request"


@pytest.mark.parametrize(
    "body", [{}, {"action": "apply"}, {"action": 1}, {"action": "handoff", "x": 1}]
)
def test_a_malformed_action_body_is_400(harness: Harness, body: dict[str, Any]) -> None:
    proposal_id = harness.create().json()["proposal"]["id"]

    refused = harness.client.post(
        f"/api/v2/decisions/proposals/{proposal_id}", headers=POST, json=body
    )

    assert refused.status_code == 400 and _code(refused) == "invalid_request"


def test_a_bad_ref_and_a_stale_base_are_refused_without_echoing_the_text(
    harness: Harness,
) -> None:
    base = harness.note(BASE_TEXT)
    for body, status, code in (
        (harness.body(ref="../a.md", base=base), 400, "invalid_ref"),
        (harness.body(ref="notes/.git/a.md", base=base), 400, "private_path"),
        (harness.body(ref="notes/a.txt", base=base), 400, "invalid_ref"),
        (harness.body(ref="notes/missing.md", base=base), 404, "source_missing"),
        (harness.body(base="0" * 64), 409, "stale_revision"),
        (harness.body(text=BASE_TEXT, base=base), 409, "no_change"),
    ):
        refused = harness.client.post(harness.create_url, headers=POST, json=body)
        assert (refused.status_code, _code(refused)) == (status, code)
        assert "SECRET" not in refused.text
    stale = harness.client.post(harness.create_url, headers=POST, json=harness.body(base="0" * 64))
    assert stale.json()["error"]["details"] == {"current_revision": base}


def test_only_a_protected_vault_takes_a_proposal(harness: Harness) -> None:
    ordinary = harness.spaces.add(harness.root, "ordinary")

    refused = harness.client.post(
        f"/api/v2/vaults/{ordinary}/proposals", headers=POST, json=harness.body()
    )

    assert refused.status_code == 409 and _code(refused) == "not_protected"
    assert "SECRET" not in refused.text


def test_unknown_workspace_and_proposal_are_404(harness: Harness) -> None:
    unknown = harness.client.post(
        f"/api/v2/vaults/{uuid.uuid4()}/proposals", headers=POST, json=harness.body()
    )
    assert unknown.status_code == 404 and _code(unknown) == "unknown_workspace"
    assert "SECRET" not in unknown.text
    missing = f"/api/v2/decisions/proposals/{uuid.uuid4()}"
    assert harness.client.get(missing, headers=AUTH).status_code == 404
    assert harness.act(str(uuid.uuid4()), "handoff").status_code == 404


def test_missing_bearer_is_401_on_every_route(harness: Harness) -> None:
    detail = f"/api/v2/decisions/proposals/{uuid.uuid4()}"
    calls = (
        harness.client.post(harness.create_url, json=harness.body()),
        harness.client.get(detail),
        harness.client.post(detail, json={"action": "handoff"}),
        harness.client.post(harness.create_url, headers={"Authorization": "Bearer wrong"}),
    )
    for call in calls:
        assert call.status_code == 401 and _code(call) == "unauthorized"
        assert "SECRET" not in call.text


def test_a_foreign_origin_is_403_on_every_route_and_an_absent_one_on_each_post(
    harness: Harness,
) -> None:
    detail = f"/api/v2/decisions/proposals/{uuid.uuid4()}"
    foreign = {**AUTH, "Origin": "https://evil.invalid"}
    for headers in (foreign, AUTH):
        for call in (
            harness.client.post(harness.create_url, headers=headers, json=harness.body()),
            harness.client.post(detail, headers=headers, json={"action": "withdraw"}),
        ):
            assert call.status_code == 403 and _code(call) == "forbidden_origin"
    assert harness.client.get(detail, headers=foreign).status_code == 403
    # Nothing was created by the refused creates, and the GET still accepts an absent Origin.
    assert harness.create().status_code == 201
    assert harness.client.get(detail, headers=AUTH).status_code == 404
    assert harness.client.get(detail, headers={**AUTH, "Origin": ORIGIN}).status_code == 404


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


def test_no_proposal_text_reaches_the_log(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    base = harness.note(BASE_TEXT)

    proposal_id = harness.create().json()["proposal"]["id"]
    harness.create()
    harness.client.post(harness.create_url, headers=POST, json=harness.body(base="0" * 64))
    harness.client.post(harness.create_url, headers=POST, json=harness.body(ref="x.txt", base=base))
    harness.client.post(
        harness.create_url, headers=POST, json=harness.body(text="SECRET" * MAX_PROPOSAL_BYTES)
    )
    harness.act(proposal_id, "handoff")
    harness.client.get(f"/api/v2/decisions/proposals/{proposal_id}", headers=AUTH)
    harness.act(proposal_id, "withdraw")

    assert "SECRET" not in caplog.text
