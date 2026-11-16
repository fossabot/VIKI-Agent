"""`PUT /api/v2/vaults/{workspace_id}/instructions/{name}`: the instruction editor's save route
(hard rule 1's one exception, ADR-082)."""

from __future__ import annotations

import ast
import hashlib
import logging
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.vault_publication import (
    REQUEST_LEASE_WAIT_S,
    PublicationError,
    PublicationOutcome,
)
from viki_agent.vaults.vault_write_api import vault_write_routes

TOKEN = "token-canary-7f3a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "https://app.invalid"
MUTATION = {**AUTH, "Origin": ORIGIN}
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000002"
HASH = "a" * 64
SRC = Path(__file__).resolve().parents[2] / "src" / "viki_agent"


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.waits: list[float | None] = []
        self.error: Exception | None = None
        self.outcome = PublicationOutcome(OPERATION_ID, "saved")

    async def save(self, *args: object, **kwargs: object) -> PublicationOutcome:
        self.calls.append(("save", *args, kwargs))
        return self.outcome

    async def save_instruction(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        name: str,
        content: bytes,
        *,
        expected_sha256: str,
        wait_s: float | None = None,
    ) -> PublicationOutcome:
        self.waits.append(wait_s)
        self.calls.append(
            (workspace_id, policy_revision, root_physical_id, name, content, expected_sha256)
        )
        if self.error is not None:
            raise self.error
        return self.outcome


def _client(service: _Service) -> TestClient:
    app = Starlette(
        routes=vault_write_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
            origins=frozenset({ORIGIN}),
            workers=set(),
        )
    )
    return TestClient(app)


def _body(**changes: object) -> dict[str, object]:
    return {
        "policy_revision": 3,
        "root_physical_id": "12:34",
        "content": "# Instructions\n",
        "expected_sha256": HASH,
        **changes,
    }


def _path(name: str = "AGENTS.md") -> str:
    return f"/api/v2/vaults/{WORKSPACE_ID}/instructions/{name}"


def test_save_requires_auth_and_exact_allowed_origin() -> None:
    service = _Service()
    with _client(service) as client:
        assert client.put(_path(), json=_body()).status_code == 401
        assert client.put(_path(), headers=AUTH, json=_body()).status_code == 403
        foreign = {**AUTH, "Origin": "https://evil.invalid"}
        assert client.put(_path(), headers=foreign, json=_body()).status_code == 403
        assert service.calls == []
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_exact_bytes_reach_the_service_and_the_answer_names_their_hash(name: str) -> None:
    service = _Service()
    content = "﻿# A\r\n"
    with _client(service) as client:
        response = client.put(_path(name), headers=MUTATION, json=_body(content=content))
    sent = content.encode("utf-8")
    assert sent == b"\xef\xbb\xbf# A\r\n"
    assert service.calls == [(WORKSPACE_ID, 3, "12:34", name, sent, HASH)]
    # The route's lease bound reaches the service, so the save refuses before the client's
    # 15 s abort rather than landing after it.
    assert service.waits == [REQUEST_LEASE_WAIT_S]
    assert response.status_code == 200
    assert response.json() == {
        "operation_id": OPERATION_ID,
        "status": "saved",
        "cleanup_pending": False,
        "sha256": hashlib.sha256(sent).hexdigest(),
    }


def test_no_change_answers_200_with_the_hash() -> None:
    service = _Service()
    service.outcome = PublicationOutcome(OPERATION_ID, "no_change")
    with _client(service) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 200
    assert response.json()["status"] == "no_change"
    assert response.json()["sha256"] == hashlib.sha256(b"# Instructions\n").hexdigest()


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_a_conflict_is_409_with_a_null_hash(cleanup_pending: bool) -> None:
    service = _Service()
    service.outcome = PublicationOutcome(OPERATION_ID, "conflict", cleanup_pending)
    with _client(service) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 409
    assert response.json() == {
        "operation_id": OPERATION_ID,
        "status": "conflict",
        "cleanup_pending": cleanup_pending,
        "sha256": None,
    }


def test_a_null_expected_hash_is_refused_before_the_service() -> None:
    service = _Service()
    with _client(service) as client:
        response = client.put(_path(), headers=MUTATION, json=_body(expected_sha256=None))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_publication"
    assert service.calls == []


def test_the_body_keeps_the_note_routes_checks() -> None:
    service = _Service()
    with _client(service) as client:
        for body in (
            _body(expected_sha256="A" * 64),
            _body(content="bad\u0000text"),
            {**_body(), "name": "CLAUDE.md"},
        ):
            response = client.put(_path(), headers=MUTATION, json=body)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_publication"
        too_large = _body(content="x" * (2 * 1024 * 1024 + 1))
        assert client.put(_path(), headers=MUTATION, json=too_large).status_code == 413
    assert service.calls == []


def test_only_the_two_exact_names_and_a_canonical_id_are_admitted() -> None:
    service = _Service()
    names = (
        "agents.md",
        "Agents.md",
        "AGENTS.MD",
        "claude.md",
        "AGENTS.override.md",
        "CLAUDE.local.md",
        "GEMINI.md",
        "ＡGENTS.md",
        "AGENTS.md.",
        "AGENTS.md ",
        "note.md",
    )
    with _client(service) as client:
        for name in names:
            response = client.put(_path(quote(name)), headers=MUTATION, json=_body())
            assert response.status_code == 400, name
            assert response.json()["error"]["code"] == "invalid_ref", name
        for workspace_id in ("00000000-0000-4000-8000-00000000000A", "not-a-uuid"):
            path = f"/api/v2/vaults/{workspace_id}/instructions/AGENTS.md"
            response = client.put(path, headers=MUTATION, json=_body())
            assert response.status_code == 400, workspace_id
            assert response.json()["error"]["code"] == "invalid_ref", workspace_id
        for nested in ("sub/AGENTS.md", "sub%2FAGENTS.md", "%2E%2E/AGENTS.md"):
            response = client.put(_path(nested), headers=MUTATION, json=_body())
            assert not 200 <= response.status_code < 300, nested
    assert service.calls == []


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_the_note_route_still_refuses_both_names_with_a_hash(name: str) -> None:
    service = _Service()
    with _client(service) as client:
        for ref in (name, f"sub/{name}"):
            response = client.put(
                f"/api/v2/vaults/{WORKSPACE_ID}/notes/{ref}", headers=MUTATION, json=_body()
            )
            assert response.status_code == 400, ref
            assert response.json()["error"]["code"] == "invalid_ref", ref
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MutationError("ordinary_workspace_required"), 409),
        (MutationError("read_only_target", stage_retained=True), 409),
        (MutationError("unsupported_filesystem"), 409),
        (MutationError("missing_target"), 503),
        (MutationError("excluded_path", stage_retained=True), 400),
        (MutationError("target_busy"), 423),
        (PublicationError("vault_recovery_required"), 409),
        (PublicationError("publication_limit"), 429),
        (PublicationError("recovery_required", OPERATION_ID), 503),
    ],
)
def test_service_refusals_answer_like_the_note_route(
    error: MutationError | PublicationError, status: int
) -> None:
    service = _Service()
    service.error = error
    with _client(service) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == error.code
    assert "sha256" not in response.json()
    if isinstance(error, PublicationError) and error.operation_id is not None:
        assert response.json()["error"]["operation_id"] == OPERATION_ID


def test_a_save_and_a_refusal_log_no_content_or_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "content-canary-91b2"
    service = _Service()
    with _client(service) as client:
        saved = client.put(_path(), headers=MUTATION, json=_body(content=canary))
        service.error = MutationError("read_only_target", stage_retained=True)
        refused = client.put(_path("CLAUDE.md"), headers=MUTATION, json=_body(content=canary))
    assert (saved.status_code, refused.status_code) == (200, 409)
    logged = caplog.text + "".join(repr(record.__dict__) for record in caplog.records)
    assert canary not in logged
    assert TOKEN not in logged


def test_save_instruction_has_one_caller_the_route() -> None:
    """Every use of the name, an aliased bound method or a `getattr` string included, is the
    route's one call; the definition is the service's; and no second route reaches it."""
    uses: list[str] = []
    definitions: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and node.attr == "save_instruction") or (
                isinstance(node, ast.Constant) and node.value == "save_instruction"
            ):
                uses.append(module)
            elif (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "save_instruction"
            ):
                definitions.append(module)
    assert uses == ["vaults/vault_write_api.py"]
    assert definitions == ["vaults/vault_publication.py"]
    routes = vault_write_routes(
        _Service(),  # type: ignore[arg-type]
        authorized=lambda _request: True,
        origins=frozenset({ORIGIN}),
        workers=set(),
    )
    assert [route.path for route in routes] == [
        "/api/v2/vault-publications/recovery",
        "/api/v2/vaults/{workspace_id}/notes/{ref:path}",
        "/api/v2/vaults/{workspace_id}/instructions/{name}",
    ]
    # The Origin check in `gated` covers PUT and GET only, so a second method would skip it.
    assert routes[-1].methods == {"PUT"}
