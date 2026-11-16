"""The capture routes over a real listing and a fake suggester.

A capture's preview is the user's vault content, so every response that is not the listing
itself (every error body) is checked against a marker string.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.domain import CanonicalRoot
from viki_agent.storage import StateError
from viki_agent.vaults.capture_api import MAX_REQUEST_BYTES, capture_routes
from viki_agent.vaults.capture_reply import Suggestion
from viki_agent.vaults.capture_suggest import CaptureSuggester, SuggestionRun
from viki_agent.vaults.captures import MAX_CAPTURE_BYTES, CaptureError, CaptureService
from viki_agent.vaults.vault import VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
POST = {**AUTH, "Origin": ORIGIN}
STAMP = "2026-10-10T09:00:00.000Z"
WHY = "a claim worth keeping"
SECRET = "SECRET-CAPTURE-LINE"


class _Workspaces:
    """Only what `CaptureService` and the real `VaultService` reach."""

    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}
        self.active_id: str | None = None

    def add(self, root: Path, *, kind: str = "vault") -> str:
        info = os.stat(root)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            kind=kind,
            root=CanonicalRoot(
                key=root_key(root.resolve()),
                display=str(root.resolve()),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        if self.active_id is None:
            self.active_id = workspace_id
        return workspace_id

    def get(self, workspace_id: str, *, kind: str | None = "vault") -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root

    def resolve_active(self) -> Any:
        assert self.active_id is not None
        return SimpleNamespace(workspace=self.spaces[self.active_id])


class _FakeSuggester:
    """The `CaptureSuggester` surface the routes reach: one running run at a time, from memory."""

    def __init__(self) -> None:
        self._runs: dict[str, SuggestionRun] = {}
        self.table: dict[str, dict[str, Suggestion]] = {}
        self.refuse: Exception | None = None

    def run_for(self, workspace_id: str) -> SuggestionRun | None:
        return self._runs.get(workspace_id)

    def suggestions(self, workspace_id: str) -> Mapping[str, Suggestion]:
        return self.table.get(workspace_id, {})

    async def start(self, workspace_id: str) -> SuggestionRun:
        if self.refuse is not None:
            raise self.refuse
        current = self._runs.get(workspace_id)
        if current is not None and current.state == "running":
            raise CaptureError("suggestion_running", run_id=current.run_id)
        run = SuggestionRun(
            run_id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            session_id=str(uuid.uuid4()),
            state="running",
            started_at=STAMP,
        )
        self._runs[workspace_id] = run
        return run


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "vault"
        self.root.mkdir()
        self.spaces = _Workspaces()
        self.wid = self.spaces.add(self.root)
        vault = VaultService(cast(WorkspaceService, self.spaces))
        self.service = CaptureService(cast(WorkspaceService, self.spaces), vault)
        self.suggester = _FakeSuggester()
        routes = capture_routes(
            self.service,
            cast(CaptureSuggester, self.suggester),
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set[asyncio.Task[Any]](),
        )
        self.client = TestClient(Starlette(routes=routes))

    @property
    def list_url(self) -> str:
        return f"/api/v2/vaults/{self.wid}/captures"

    @property
    def suggest_url(self) -> str:
        return f"/api/v2/vaults/{self.wid}/captures/suggest"

    def note(self, text: str, ref: str = "00-inbox/a.md") -> str:
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path.as_posix()


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_listing_returns_the_documented_shape(harness: Harness) -> None:
    harness.note("a thought\n")

    listed = harness.client.get(harness.list_url, headers=AUTH)

    assert listed.status_code == 200
    body = listed.json()
    assert set(body) == {"captures", "projects", "partial", "omitted", "run"}
    assert body["projects"] == [] and body["partial"] is False and body["omitted"] == 0
    assert body["run"] is None
    capture = body["captures"][0]
    assert set(capture) == {
        "ref",
        "revision",
        "modified",
        "captured",
        "preview",
        "cut",
        "unread",
        "suggestion",
    }
    assert capture["ref"] == "00-inbox/a.md" and capture["preview"] == "a thought\n"
    assert capture["suggestion"] is None and capture["unread"] is None


def test_a_suggestion_is_shown_only_when_its_revision_matches(harness: Harness) -> None:
    first = "first thought\n"
    harness.note(first)
    revision = hashlib.sha256(first.encode("utf-8")).hexdigest()
    harness.suggester.table[harness.wid] = {
        "00-inbox/a.md": Suggestion(
            ref="00-inbox/a.md",
            revision=revision,
            modified=STAMP,
            to="knowledge",
            project=None,
            why=WHY,
            at=STAMP,
        )
    }

    shown = harness.client.get(harness.list_url, headers=AUTH)
    assert shown.json()["captures"][0]["suggestion"] == {
        "to": "knowledge",
        "project": None,
        "why": WHY,
        "at": STAMP,
    }

    harness.note("edited thought\n")  # same ref, new content: revision changes

    stale = harness.client.get(harness.list_url, headers=AUTH)
    assert stale.json()["captures"][0]["suggestion"] is None


def test_a_suggestion_for_an_unread_capture_is_shown_only_while_its_modified_stamp_matches(
    harness: Harness,
) -> None:
    """An unread (too_large or budget) capture has no revision, so its suggestion is bound to the
    scan's `modified` stamp instead; it must keep showing while the file is unchanged and stop
    once that stamp changes, even though the file stays too large."""
    path = harness.note("x" * (MAX_CAPTURE_BYTES + 1))
    first = harness.client.get(harness.list_url, headers=AUTH).json()["captures"][0]
    assert first["unread"] == "too_large" and first["revision"] is None

    harness.suggester.table[harness.wid] = {
        "00-inbox/a.md": Suggestion(
            ref="00-inbox/a.md",
            revision=None,
            modified=first["modified"],
            to="discard",
            project=None,
            why=WHY,
            at=STAMP,
        )
    }

    shown = harness.client.get(harness.list_url, headers=AUTH)
    assert shown.json()["captures"][0]["suggestion"] == {
        "to": "discard",
        "project": None,
        "why": WHY,
        "at": STAMP,
    }

    os.utime(path, (12345, 12345))  # a fixed, different mtime; still over MAX_CAPTURE_BYTES

    stale = harness.client.get(harness.list_url, headers=AUTH)
    assert stale.json()["captures"][0]["unread"] == "too_large"
    assert stale.json()["captures"][0]["suggestion"] is None


def test_a_capture_with_an_mtime_past_year_9999_still_lists_with_200(harness: Harness) -> None:
    """The listing route must not fail just because one file's mtime overflows the epoch
    arithmetic in `captures._modified_stamp`."""
    harness.note("normal\n", "00-inbox/a-normal.md")
    far_future = harness.note("from the far future\n", "00-inbox/z-far-future.md")
    os.utime(far_future, (270_000_000_000.0, 270_000_000_000.0))
    if os.stat(far_future).st_mtime < 253_402_300_800:  # 10000-01-01Z
        pytest.skip(
            "this filesystem cannot store an mtime past year 9999 (ext4 reads back 2446, APFS "
            "2262); test_modified_stamp_clamps_past_year_9999_and_before_year_1 covers the clamp"
        )

    listed = harness.client.get(harness.list_url, headers=AUTH)

    assert listed.status_code == 200
    by_ref = {c["ref"]: c["modified"] for c in listed.json()["captures"]}
    assert by_ref["00-inbox/z-far-future.md"] == "9999-12-31T23:59:59.999Z"
    assert by_ref["00-inbox/a-normal.md"] != by_ref["00-inbox/z-far-future.md"]


def test_post_suggest_starts_a_run_then_409s_with_its_run_id(harness: Harness) -> None:
    started = harness.client.post(harness.suggest_url, headers=POST, json={})
    assert started.status_code == 202
    run = started.json()["run"]
    assert run["state"] == "running"
    assert uuid.UUID(run["run_id"]) and uuid.UUID(run["session_id"])

    again = harness.client.post(harness.suggest_url, headers=POST, json={})
    assert again.status_code == 409 and _code(again) == "suggestion_running"
    assert again.json()["error"]["details"] == {"run_id": run["run_id"]}

    status = harness.client.get(harness.suggest_url, headers=AUTH)
    assert status.json()["run"]["run_id"] == run["run_id"]


def test_get_suggest_with_no_run_is_null(harness: Harness) -> None:
    status = harness.client.get(harness.suggest_url, headers=AUTH)
    assert status.status_code == 200 and status.json() == {"run": None}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1}',
        b"not json",
        b"[]",
        b'{"a":1,"a":2}',
        b"\xff\xfe",
    ],
)
def test_a_malformed_start_body_is_400(harness: Harness, raw: bytes) -> None:
    refused = harness.client.post(
        harness.suggest_url, headers={**POST, "Content-Type": "application/json"}, content=raw
    )
    assert refused.status_code == 400 and _code(refused) == "invalid_request"


def test_an_oversized_start_body_is_413(harness: Harness) -> None:
    raw = b'{"' + b"x" * MAX_REQUEST_BYTES + b'":1}'

    big = harness.client.post(
        harness.suggest_url, headers={**POST, "Content-Type": "application/json"}, content=raw
    )

    assert big.status_code == 413 and _code(big) == "too_large"
    assert big.json()["error"]["details"] == {"limit": MAX_REQUEST_BYTES}


def test_a_body_on_a_get_is_415(harness: Harness) -> None:
    for url in (harness.list_url, harness.suggest_url):
        refused = harness.client.request("GET", url, headers=AUTH, content=b"{}")
        assert refused.status_code == 415 and _code(refused) == "invalid_representation"


def test_a_query_string_is_400_on_every_route(harness: Harness) -> None:
    for call in (
        harness.client.get(harness.list_url + "?x=1", headers=AUTH),
        harness.client.get(harness.suggest_url + "?x=1", headers=AUTH),
        harness.client.post(harness.suggest_url + "?x=1", headers=POST, json={}),
    ):
        assert call.status_code == 400 and _code(call) == "invalid_request"


def test_missing_bearer_is_401_on_every_route(harness: Harness) -> None:
    for call in (
        harness.client.get(harness.list_url),
        harness.client.get(harness.suggest_url),
        harness.client.post(harness.suggest_url, json={}),
        harness.client.get(harness.list_url, headers={"Authorization": "Bearer wrong"}),
    ):
        assert call.status_code == 401 and _code(call) == "unauthorized"


def test_a_foreign_or_absent_origin_on_post_is_403_and_get_allows_absent(
    harness: Harness,
) -> None:
    foreign = {**AUTH, "Origin": "https://evil.invalid"}
    for headers in (foreign, AUTH):
        call = harness.client.post(harness.suggest_url, headers=headers, json={})
        assert call.status_code == 403 and _code(call) == "forbidden_origin"
    assert harness.client.get(harness.list_url, headers=foreign).status_code == 403
    assert harness.client.get(harness.list_url, headers=AUTH).status_code == 200
    assert harness.client.get(harness.suggest_url, headers=AUTH).status_code == 200


def test_unknown_inactive_and_non_vault_workspaces_are_404_or_409(harness: Harness) -> None:
    unknown_id = uuid.uuid4()
    unknown = harness.client.get(f"/api/v2/vaults/{unknown_id}/captures", headers=AUTH)
    assert unknown.status_code == 404 and _code(unknown) == "unknown_workspace"
    poll_unknown = harness.client.get(f"/api/v2/vaults/{unknown_id}/captures/suggest", headers=AUTH)
    assert poll_unknown.status_code == 404 and _code(poll_unknown) == "unknown_workspace"

    ordinary = harness.spaces.add(harness.root, kind="ordinary")
    not_vault = harness.client.get(f"/api/v2/vaults/{ordinary}/captures", headers=AUTH)
    assert not_vault.status_code == 409 and _code(not_vault) == "not_a_vault"
    poll_not_vault = harness.client.get(f"/api/v2/vaults/{ordinary}/captures/suggest", headers=AUTH)
    assert poll_not_vault.status_code == 409 and _code(poll_not_vault) == "not_a_vault"

    other_root = harness.root.parent / "other"
    other_root.mkdir()
    other = harness.spaces.add(other_root)  # harness.wid was added first and stays active
    inactive = harness.client.get(f"/api/v2/vaults/{other}/captures", headers=AUTH)
    assert inactive.status_code == 409 and _code(inactive) == "not_active_vault"
    poll_inactive = harness.client.get(f"/api/v2/vaults/{other}/captures/suggest", headers=AUTH)
    assert poll_inactive.status_code == 409 and _code(poll_inactive) == "not_active_vault"


def test_no_captures_and_unavailable_surface_through_suggest(harness: Harness) -> None:
    harness.suggester.refuse = CaptureError("no_captures")
    empty = harness.client.post(harness.suggest_url, headers=POST, json={})
    assert empty.status_code == 409 and _code(empty) == "no_captures"

    harness.suggester.refuse = StateError("busy")
    broken = harness.client.post(harness.suggest_url, headers=POST, json={})
    assert broken.status_code == 503 and _code(broken) == "unavailable"
    assert "The app store is busy" not in broken.text


def test_every_response_carries_no_store(harness: Harness) -> None:
    for call in (
        harness.client.get(harness.list_url, headers=AUTH),
        harness.client.get(harness.suggest_url, headers=AUTH),
        harness.client.post(harness.suggest_url, headers=POST, json={}),
        harness.client.get(harness.list_url),
        harness.client.get(harness.list_url + "?x=1", headers=AUTH),
    ):
        assert call.headers["cache-control"] == "no-store"


def test_no_vault_text_in_any_error_body(harness: Harness) -> None:
    harness.note(f"{SECRET}\n")
    ok = harness.client.get(harness.list_url, headers=AUTH)
    assert SECRET in ok.text  # sanity: the marker really is in a successful body

    harness.suggester.refuse = StateError("busy")
    bodies = (
        harness.client.post(harness.suggest_url, headers=POST, content=b"nope"),
        harness.client.get(harness.list_url),
        harness.client.post(
            harness.suggest_url, headers={**AUTH, "Origin": "https://evil.invalid"}, json={}
        ),
        harness.client.get(f"/api/v2/vaults/{uuid.uuid4()}/captures", headers=AUTH),
        harness.client.post(harness.suggest_url, headers=POST, json={}),
        harness.client.post(
            harness.suggest_url, headers={**POST, "Content-Type": "text/plain"}, content=b"{}"
        ),
    )
    for body in bodies:
        assert SECRET not in body.text and "app store" not in body.text


def test_every_refusal_releases_its_permit(harness: Harness) -> None:
    # The limiter holds four permits; more refusals than that would 429 if one leaked.
    for _ in range(12):
        bad = harness.client.post(harness.suggest_url, headers=POST, content=b"nope")
        assert bad.status_code == 415
    assert harness.client.get(harness.list_url, headers=AUTH).status_code == 200
