from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.vaults.vault_lifecycle import (
    ForgottenOperation,
    OperationOutcome,
    OperationPlan,
    PreviewStep,
    StuckOperation,
)
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.vault_operations import RecoveryEntry
from viki_agent.vaults.vault_ops_api import MAX_REQUEST_BYTES, vault_ops_routes
from viki_agent.vaults.vault_publication import PublicationError, ResolveOutcome

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
EVIL = "https://evil.invalid"
MUTATION = {**AUTH, "Origin": ORIGIN}
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_WORKSPACE = "00000000-0000-4000-8000-000000000009"
OPERATION_ID = "00000000-0000-4000-8000-000000000002"
# Letters, so an upper-case spelling is a different, non-canonical string.
RECOVERY_ID = "e0000000-0000-4000-8000-000000000003"
COMMAND_ID = "c0000000-0000-4000-8000-000000000004"
HASH = "a" * 64
# A guard on a refusal that normally answers in milliseconds, never a bound on real work.
HANG_GUARD_S = 5
# Distinct from any default, so only a pass-through can deliver it.
LEASE_WAIT_S = 0.25

PREVIEW = f"/api/v2/vaults/{WORKSPACE_ID}/operations/preview"
APPLY = f"/api/v2/vaults/{WORKSPACE_ID}/operations"
RECOVERY = f"/api/v2/vaults/{WORKSPACE_ID}/recovery"
FORGET = f"/api/v2/vault-operations/{OPERATION_ID}/resolve"
RESOLVE = f"/api/v2/vault-publications/{OPERATION_ID}/resolve"

_KIND_FIELDS: dict[str, dict[str, str]] = {
    "rename": {"ref": "notes/a.md", "to": "notes/b.md", "expected_sha256": HASH},
    "move": {"ref": "notes/a.md", "to": "other/a.md", "expected_sha256": HASH},
    "archive": {"ref": "notes/a.md", "to": "archive/notes/a.md", "expected_sha256": HASH},
    "delete": {"ref": "notes/a.md", "expected_sha256": HASH},
    "restore": {"recovery_id": RECOVERY_ID, "to": "notes/a.md"},
}


def _op(
    kind: str = "rename", /, *, drop: tuple[str, ...] = (), **changes: object
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "policy_revision": 3,
        "root_physical_id": "12:34",
        "kind": kind,
        **_KIND_FIELDS[kind],
        **changes,
    }
    for key in drop:
        del body[key]
    return body


def _apply(kind: str = "rename", /, **changes: object) -> dict[str, Any]:
    return _op(kind, **{"command_id": COMMAND_ID, **changes})


class _Services:
    """One fake for the lifecycle service, the recovery store and the publication service."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None
        self.plan = OperationPlan(
            (PreviewStep(0, "mkdir", "archive", None), PreviewStep(1, "move", "a.md", "b.md")),
            ("archive",),
            30,
        )
        self.outcome = OperationOutcome(COMMAND_ID, "done", False, 2, None, ("archive",))
        self.entries: list[RecoveryEntry] = []
        self.stuck_rows: list[StuckOperation] = []
        self.gate: asyncio.Event | None = None
        self.entered = 0
        self.four_entered = asyncio.Event()

    async def _call(self, *call: object) -> None:
        self.calls.append(call)
        if self.gate is not None:
            self.entered += 1
            if self.entered == 4:
                self.four_entered.set()
            await self.gate.wait()
        if self.error is not None:
            raise self.error

    async def preview(
        self, workspace_id: str, revision: int, physical_id: str, kind: str, **fields: str
    ) -> OperationPlan:
        await self._call("preview", workspace_id, revision, physical_id, kind, fields)
        return self.plan

    async def apply(
        self,
        command_id: str,
        workspace_id: str,
        revision: int,
        physical_id: str,
        kind: str,
        **fields: str,
    ) -> OperationOutcome:
        await self._call("apply", command_id, workspace_id, revision, physical_id, kind, fields)
        return self.outcome

    async def stuck(self, workspace_id: str) -> list[StuckOperation]:
        await self._call("stuck", workspace_id)
        return self.stuck_rows

    async def forget(self, operation_id: str) -> ForgottenOperation:
        await self._call("forget", operation_id)
        return ForgottenOperation(operation_id, (RECOVERY_ID,), True)

    async def resolve(
        self, operation_id: str, choice: str, *, wait_s: float | None = None
    ) -> ResolveOutcome:
        await self._call("resolve", operation_id, choice, wait_s)
        return ResolveOutcome(operation_id, choice, True, False)  # type: ignore[arg-type]

    def sweep(self, now: datetime) -> list[RecoveryEntry]:
        self.calls.append(("sweep", now))
        if self.error is not None:
            raise self.error
        return self.entries


def _app(services: _Services, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=vault_ops_routes(
            services,  # type: ignore[arg-type]
            services,  # type: ignore[arg-type]
            services,  # type: ignore[arg-type]
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
            lease_wait_s=LEASE_WAIT_S,
        )
    )


# Every route, with a body it would accept.
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", PREVIEW, _op()),
    ("POST", APPLY, _apply()),
    ("GET", RECOVERY, None),
    ("POST", FORGET, {"choice": "forget"}),
    ("POST", RESOLVE, {"choice": "drop_previous"}),
]


def _send(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
):
    return client.request(method, path, headers=headers, json=body)


def test_every_route_refuses_an_unauthorized_request_before_anything() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for method, path, body in ROUTES:
            for headers in ({}, {"Origin": EVIL}, {"Authorization": "Bearer wrong"}):
                response = _send(client, method, path, body, headers)
                assert response.status_code == 401, (path, headers)
                assert response.json()["error"]["code"] == "unauthorized"
                assert response.headers["cache-control"] == "no-store"
            # Bearer comes first: a malformed body without the token is still a 401.
            garbled = client.request(
                method,
                path,
                headers={"Content-Type": "application/json"},
                content=b"{",
            )
            assert garbled.status_code == 401
            response = _send(client, method, path, body, {**AUTH, "Origin": EVIL})
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "forbidden_origin"
            assert response.headers["cache-control"] == "no-store"
            if method == "POST":
                missing = _send(client, method, path, body, AUTH)
                assert missing.status_code == 403, path
    assert services.calls == []


def test_every_route_answers_an_authorized_request_with_no_store() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for method, path, body in ROUTES:
            response = _send(client, method, path, body, MUTATION)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-store"
        # A read needs no Origin, as a same-origin GET sends none.
        assert client.get(RECOVERY, headers=AUTH).status_code == 200
        assert client.head(RECOVERY, headers=AUTH).status_code == 200


@pytest.mark.parametrize("kind", list(_KIND_FIELDS))
def test_preview_passes_each_kind_through_and_omits_an_absent_refusal(kind: str) -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(PREVIEW, headers=MUTATION, json=_op(kind))
    assert response.status_code == 200
    assert response.json() == {
        "steps": [
            {"n": 0, "action": "mkdir", "ref": "archive", "to": None},
            {"n": 1, "action": "move", "ref": "a.md", "to": "b.md"},
        ],
        "creates_folders": ["archive"],
        "recovery_days": 30,
    }
    assert services.calls == [
        ("preview", WORKSPACE_ID, 3, "12:34", kind, _KIND_FIELDS[kind]),
    ]


def test_a_preview_refusal_is_a_200_naming_its_code() -> None:
    services = _Services()
    services.plan = OperationPlan((), (), 30, "conflict")
    with TestClient(_app(services)) as client:
        response = client.post(PREVIEW, headers=MUTATION, json=_op())
    assert response.status_code == 200
    assert response.json() == {
        "steps": [],
        "creates_folders": [],
        "recovery_days": 30,
        "refusal": "conflict",
    }


@pytest.mark.parametrize("kind", list(_KIND_FIELDS))
@pytest.mark.parametrize("status", ["done", "failed", "conflict", "recovery_required"])
def test_apply_answers_every_outcome_with_200_and_its_exact_keys(kind: str, status: str) -> None:
    services = _Services()
    reason = None if status == "done" else "target_busy"
    services.outcome = OperationOutcome(COMMAND_ID, status, True, 1, reason, ("a",))  # type: ignore[arg-type]
    with TestClient(_app(services)) as client:
        response = client.post(APPLY, headers=MUTATION, json=_apply(kind))
    assert response.status_code == 200
    assert response.json() == {
        "operation_id": COMMAND_ID,
        "status": status,
        "replayed": True,
        "steps_applied": 1,
        "reason": reason,
        "created_folders": ["a"],
    }
    assert services.calls == [
        ("apply", COMMAND_ID, WORKSPACE_ID, 3, "12:34", kind, _KIND_FIELDS[kind]),
    ]


# Each is refused on preview as it stands, and on apply with a valid `command_id` added.
INVALID_OPERATIONS: list[tuple[str, dict[str, Any]]] = [
    # A request carries exactly one of `ref` or `recovery_id`, matched to its kind.
    ("ref on restore", _op("restore", ref="notes/a.md")),
    ("recovery_id on move", _op("move", recovery_id=RECOVERY_ID)),
    ("restore without recovery_id", _op("restore", drop=("recovery_id",))),
    ("rename without ref", _op("rename", drop=("ref",))),
    ("both on delete", _op("delete", recovery_id=RECOVERY_ID)),
    # `to` is absent for delete and required for the rest.
    ("to on delete", _op("delete", to="notes/b.md")),
    ("move without to", _op("move", drop=("to",))),
    ("archive without to", _op("archive", drop=("to",))),
    ("restore without to", _op("restore", drop=("to",))),
    # A hash for every kind but restore, which carries none.
    ("hash on restore", _op("restore", expected_sha256=HASH)),
    ("rename without hash", _op("rename", drop=("expected_sha256",))),
    ("delete without hash", _op("delete", drop=("expected_sha256",))),
    ("null hash", _op("archive", expected_sha256=None)),
    ("upper hash", _op(expected_sha256="A" * 64)),
    ("short hash", _op(expected_sha256="a" * 63)),
    ("hash with newline", _op(expected_sha256="a" * 64 + "\n")),
    # A strict body: no unknown field and no wrong type.
    ("unknown field", _op(path="elsewhere.md")),
    ("unknown kind", _op("rename", kind="copy")),
    ("upper kind", _op("rename", kind="Rename")),
    ("list kind", _op("rename", kind=["rename"])),
    ("no kind", _op("rename", drop=("kind",))),
    ("no revision", _op(drop=("policy_revision",))),
    ("bool revision", _op(policy_revision=True)),
    ("zero revision", _op(policy_revision=0)),
    ("float revision", _op(policy_revision=3.0)),
    ("huge revision", _op(policy_revision=2**53)),
    ("string revision", _op(policy_revision="3")),
    ("no physical id", _op(drop=("root_physical_id",))),
    ("bad physical id", _op(root_physical_id="not-an-id")),
    ("long physical id", _op(root_physical_id="1:" + "2" * 79)),
    ("numeric ref", _op(ref=7)),
    ("null to", _op(to=None)),
    ("null recovery id", _op("restore", recovery_id=None)),
    ("upper recovery id", _op("restore", recovery_id=RECOVERY_ID.upper())),
    ("braced recovery id", _op("restore", recovery_id="{" + RECOVERY_ID + "}")),
]
# Sent as they stand to both routes.
INVALID_RAW: list[tuple[str, bytes]] = [
    ("array body", b"[]"),
    ("string body", b'"x"'),
    ("empty body", b""),
    ("not json", b"{"),
    ("not utf-8", b"\xff"),
    ("command_id on preview", json.dumps(_apply()).encode()),
    (
        "duplicate key",
        b'{"policy_revision":3,"policy_revision":4,"root_physical_id":"12:34",'
        b'"kind":"delete","ref":"notes/a.md","expected_sha256":"' + HASH.encode() + b'",'
        b'"command_id":"' + COMMAND_ID.encode() + b'"}',
    ),
]


def test_the_strict_body_refuses_before_the_service() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        requests = [
            *((name, PREVIEW, json.dumps(body).encode()) for name, body in INVALID_OPERATIONS),
            *(
                (name, APPLY, json.dumps({**body, "command_id": COMMAND_ID}).encode())
                for name, body in INVALID_OPERATIONS
            ),
            *((name, PREVIEW, body) for name, body in INVALID_RAW),
            *((name, APPLY, body) for name, body in INVALID_RAW if name != "command_id on preview"),
        ]
        for name, path, body in requests:
            response = client.post(
                path,
                headers={**MUTATION, "Content-Type": "application/json"},
                content=body,
            )
            assert response.status_code == 400, (name, path)
            assert response.json()["error"]["code"] == "invalid_operation", (name, path)
            assert response.headers["cache-control"] == "no-store"
        for name, body in {
            "no command_id": _op(),
            "upper command_id": _apply(command_id=COMMAND_ID.upper()),
            "numeric command_id": _apply(command_id=4),
        }.items():
            response = client.post(APPLY, headers=MUTATION, json=body)
            assert response.status_code == 400, name
            assert response.json()["error"]["code"] == "invalid_operation", name
    assert services.calls == []


def test_the_envelope_is_refused_before_the_body_is_read() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for path in (PREVIEW, APPLY, FORGET, RESOLVE):
            body = json.dumps(
                _apply() if path == APPLY else {"choice": "forget"} if "resolve" in path else _op()
            ).encode()
            for headers in (
                {"Content-Type": "text/plain"},
                {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            ):
                response = client.post(path, headers={**MUTATION, **headers}, content=body)
                assert response.status_code == 400, (path, headers)
            query = client.post(f"{path}?x=1", headers=MUTATION, content=body)
            assert query.status_code == 400
            assert query.headers["cache-control"] == "no-store"
    assert services.calls == []


def test_a_body_past_the_bound_is_413_with_its_code() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        padding = "x" * MAX_REQUEST_BYTES
        for path in (PREVIEW, APPLY, FORGET, RESOLVE):
            response = client.post(path, headers=MUTATION, json={"choice": padding})
            assert response.status_code == 413, path
            assert response.json()["error"]["code"] == "too_large"
            assert response.headers["cache-control"] == "no-store"
        # The largest valid request fits: two refs at this OS's own NAME_MAX edge, every
        # character escaped. Windows counts 255 UTF-16 code units per part (510 bytes); off
        # Windows the limit counts 255 UTF-8 bytes per part instead, and "\u00e9" is one code unit
        # but two UTF-8 bytes, so a POSIX part holds only half as many of them.
        part = 255 if sys.platform == "win32" else 127
        long_ref = "\u00e9" * part + "/" + "\u00e9" * (part - 3) + ".md"
        largest = _apply(ref=long_ref, to=long_ref[:-4] + "x.md")
        encoded = json.dumps(largest, ensure_ascii=True).encode()
        assert len(encoded) < MAX_REQUEST_BYTES
        response = client.post(
            APPLY, headers={**MUTATION, "Content-Type": "application/json"}, content=encoded
        )
        assert response.status_code == 200
    assert [call[0] for call in services.calls] == ["apply"]


def test_refs_go_through_the_writer_rule_before_the_service() -> None:
    bad_refs = (
        "CLAUDE.md",
        "notes/agents.md",
        ".git/x.md",
        "../x.md",
        "a//b.md",
        "a/./b.md",
        "/abs.md",
        "a\\b.md",
        "c:x.md",
        "x.txt",
        "COM1.md",
        "x" * 253 + ".md",
        "a\u0001b.md",
    )
    services = _Services()
    with TestClient(_app(services)) as client:
        for ref in bad_refs:
            for path, body in (
                (PREVIEW, _op(ref=ref)),
                (PREVIEW, _op(to=ref)),
                (APPLY, _apply("delete", ref=ref)),
                (APPLY, _apply("restore", to=ref)),
            ):
                response = client.post(path, headers=MUTATION, json=body)
                assert response.status_code == 400, (ref, path)
                assert response.json()["error"]["code"] == "invalid_ref", (ref, path)
        assert services.calls == []
        leaf = "notes/" + "x" * 252 + ".md"
        assert client.post(PREVIEW, headers=MUTATION, json=_op(to=leaf)).status_code == 200
    assert services.calls[0][-1] == {**_KIND_FIELDS["rename"], "to": leaf}


@pytest.mark.parametrize("kind", ("rename", "move", "archive", "delete"))
@pytest.mark.parametrize("route", (PREVIEW, APPLY))
def test_bot_memory_source_is_refused_before_operation_service(kind: str, route: str) -> None:
    services = _Services()
    body = _op(kind, ref="BoT-MeMoRy/bot/x.md")
    if route == APPLY:
        body["command_id"] = COMMAND_ID
    with TestClient(_app(services)) as client:
        response = client.post(route, headers=MUTATION, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_ref"
    assert services.calls == []


@pytest.mark.parametrize("kind", ("rename", "move", "archive", "restore"))
@pytest.mark.parametrize("route", (PREVIEW, APPLY))
def test_bot_memory_destination_is_refused_before_operation_service(kind: str, route: str) -> None:
    services = _Services()
    body = _op(kind, to="bot-memory/bot/x.md")
    if route == APPLY:
        body["command_id"] = COMMAND_ID
    with TestClient(_app(services)) as client:
        response = client.post(route, headers=MUTATION, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_ref"
    assert services.calls == []


def test_nested_bot_memory_destination_remains_admitted() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(
            PREVIEW, headers=MUTATION, json=_op("restore", to="notes/bot-memory/x.md")
        )
    assert response.status_code == 200
    assert services.calls == [
        (
            "preview",
            WORKSPACE_ID,
            3,
            "12:34",
            "restore",
            {"recovery_id": RECOVERY_ID, "to": "notes/bot-memory/x.md"},
        )
    ]


def test_an_unknown_kind_shape_comes_before_a_bad_ref() -> None:
    # A `ref` on a restore is the unknown field, not a ref to judge.
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(PREVIEW, headers=MUTATION, json=_op("restore", ref="CLAUDE.md"))
    assert response.json()["error"]["code"] == "invalid_operation"


def test_a_malformed_workspace_or_operation_id_is_400() -> None:
    services = _Services()
    bad = "00000000-0000-4000-8000-00000000000Z"
    upper = "A" + WORKSPACE_ID[1:]
    with TestClient(_app(services)) as client:
        for path, body, code in (
            (f"/api/v2/vaults/{bad}/operations/preview", _op(), "invalid_operation"),
            (f"/api/v2/vaults/{upper}/operations", _apply(), "invalid_operation"),
            (f"/api/v2/vault-operations/{bad}/resolve", {"choice": "forget"}, "invalid_operation"),
            (
                f"/api/v2/vault-publications/{upper}/resolve",
                {"choice": "forget"},
                "invalid_publication",
            ),
        ):
            response = client.post(path, headers=MUTATION, json=body)
            assert response.status_code == 400, path
            assert response.json()["error"]["code"] == code
        response = client.get(f"/api/v2/vaults/{upper}/recovery", headers=AUTH)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_operation"
    assert services.calls == []


STATUS_MAP = [
    ("invalid_operation", 400),
    ("invalid_ref", 400),
    ("excluded_path", 400),
    ("unknown_operation", 404),
    ("unknown_publication", 404),
    ("recovery_missing", 404),
    ("conflict", 409),
    ("command_conflict", 409),
    ("op_in_progress", 409),
    ("vault_locked", 409),
    ("vault_recovery_required", 409),
    ("unsupported_filesystem", 409),
    ("ordinary_workspace_required", 409),
    ("workspace_changed", 409),
    ("missing_target", 409),
    ("read_only_target", 409),
    ("not_resolvable", 409),
    ("target_busy", 423),
    ("parent_busy", 423),
    ("stage_busy", 423),
    ("operation_busy", 423),
    ("publication_busy", 423),
    ("operation_limit", 429),
    ("recovery_full", 429),
    ("unsafe_target", 503),
    ("unsafe_parent", 503),
    ("folder_create_failed", 503),
    ("stale_root", 503),
    ("stage_failed", 503),
    ("unsafe_stage", 503),
    ("unavailable", 503),
    ("journal_unavailable", 503),
    ("journal_corrupt", 503),
    ("recovery_unavailable", 503),
    ("recovery_corrupt", 503),
    ("recovery_required", 503),
    ("invalid_recovery", 503),
]


@pytest.mark.parametrize(("code", "status"), STATUS_MAP)
def test_every_refusal_is_typed_on_its_status(code: str, status: int) -> None:
    for error in (PublicationError(code), MutationError(code, stage_retained=True)):
        services = _Services()
        services.error = error
        with TestClient(_app(services)) as client:
            for method, path, body in ROUTES:
                response = _send(client, method, path, body, MUTATION)
                assert response.status_code == status, (code, path)
                assert response.json() == {
                    "error": {"code": code, "message": "The vault operation did not run."}
                }, (code, path)
                assert response.headers["cache-control"] == "no-store"


def test_a_refusal_names_its_operation_id_and_nothing_else() -> None:
    services = _Services()
    services.error = PublicationError("unknown_operation", OPERATION_ID)
    with TestClient(_app(services)) as client:
        response = client.post(FORGET, headers=MUTATION, json={"choice": "forget"})
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "unknown_operation",
            "message": "The vault operation did not run.",
            "operation_id": OPERATION_ID,
        }
    }


def test_an_os_or_runtime_failure_is_a_typed_503() -> None:
    for error in (OSError("C:/secret/path.md"), RuntimeError("journal")):
        services = _Services()
        services.error = error
        with TestClient(_app(services)) as client:
            response = client.post(APPLY, headers=MUTATION, json=_apply())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"
        assert "secret" not in response.text


def test_recovery_lists_this_workspace_entries_and_stuck_rows_without_hashes() -> None:
    services = _Services()

    def entry(recovery_id: str, workspace_id: str, ref: str) -> RecoveryEntry:
        return RecoveryEntry(
            recovery_id,
            workspace_id,
            "deleted",
            ref,
            HASH,
            12,
            "2026-10-02T00:00:00.000Z",
            "2026-11-01T00:00:00.000Z",
        )

    services.entries = [
        entry(RECOVERY_ID, WORKSPACE_ID, "notes/gone.md"),
        entry(COMMAND_ID, OTHER_WORKSPACE, "notes/theirs.md"),
    ]
    services.stuck_rows = [
        StuckOperation(OPERATION_ID, "move", "recovery_required", ("a.md", "b/a.md"))
    ]
    with TestClient(_app(services)) as client:
        response = client.get(RECOVERY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            {
                "recovery_id": RECOVERY_ID,
                "kind": "deleted",
                "ref": "notes/gone.md",
                "byte_size": 12,
                "created_at": "2026-10-02T00:00:00.000Z",
                "expires_at": "2026-11-01T00:00:00.000Z",
            }
        ],
        "stuck": [
            {
                "operation_id": OPERATION_ID,
                "kind": "move",
                "state": "recovery_required",
                "paths": ["a.md", "b/a.md"],
            }
        ],
    }
    assert HASH not in response.text
    swept = services.calls[0]
    assert swept[0] == "sweep" and isinstance(swept[1], datetime)
    assert swept[1].tzinfo is not None
    assert services.calls[1] == ("stuck", WORKSPACE_ID)


def test_recovery_refuses_a_query_string() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.get(f"{RECOVERY}?all=1", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_operation"
    assert services.calls == []


def test_forgetting_an_operation_takes_only_forget() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(FORGET, headers=MUTATION, json={"choice": "forget"})
        assert response.status_code == 200
        assert response.json() == {
            "operation_id": OPERATION_ID,
            "recovery_ids": [RECOVERY_ID],
            "stage_retained": True,
        }
        for body in (
            {"choice": "drop_previous"},
            {"choice": "FORGET"},
            {"choice": ["forget"]},
            {"choice": "forget", "operation_id": OPERATION_ID},
            {},
        ):
            refused = client.post(FORGET, headers=MUTATION, json=body)
            assert refused.status_code == 400, body
            assert refused.json()["error"]["code"] == "invalid_operation"
    assert services.calls == [("forget", OPERATION_ID)]


def test_resolving_a_publication_takes_forget_or_drop_previous() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for choice in ("forget", "drop_previous"):
            response = client.post(RESOLVE, headers=MUTATION, json={"choice": choice})
            assert response.status_code == 200
            assert response.json() == {
                "operation_id": OPERATION_ID,
                "choice": choice,
                "stage_retained": True,
                "backup_retained": False,
            }
        for body in ({"choice": "drop"}, {"choice": None}, {"choice": "forget", "x": 1}):
            refused = client.post(RESOLVE, headers=MUTATION, json=body)
            assert refused.status_code == 400, body
            assert refused.json()["error"]["code"] == "invalid_publication"
        duplicate = client.post(
            RESOLVE,
            headers={**MUTATION, "Content-Type": "application/json"},
            content=b'{"choice":"forget","choice":"drop_previous"}',
        )
        assert duplicate.status_code == 400
    assert services.calls == [
        ("resolve", OPERATION_ID, "forget", LEASE_WAIT_S),
        ("resolve", OPERATION_ID, "drop_previous", LEASE_WAIT_S),
    ]


def test_resolving_passes_the_route_deadline_and_maps_vault_locked() -> None:
    services = _Services()
    services.error = MutationError("vault_locked")
    with TestClient(_app(services)) as client:
        response = client.post(RESOLVE, headers=MUTATION, json={"choice": "drop_previous"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vault_locked"
    assert services.calls == [("resolve", OPERATION_ID, "drop_previous", LEASE_WAIT_S)]


async def test_four_requests_across_routes_are_shielded_tracked_and_bounded() -> None:
    services = _Services()
    services.gate = asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(services, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.post(PREVIEW, headers=MUTATION, json=_op())),
            asyncio.create_task(client.post(APPLY, headers=MUTATION, json=_apply())),
            asyncio.create_task(client.post(FORGET, headers=MUTATION, json={"choice": "forget"})),
            asyncio.create_task(client.post(RESOLVE, headers=MUTATION, json={"choice": "forget"})),
        ]
        try:
            await asyncio.wait_for(services.four_entered.wait(), 0.05)
            assert len(workers) == 4
            # Without the limiter this request would park on the gate too.
            overloaded = await asyncio.wait_for(client.get(RECOVERY, headers=AUTH), HANG_GUARD_S)
            assert overloaded.status_code == 429
            assert overloaded.json()["error"]["code"] == "overloaded"
            assert overloaded.headers["cache-control"] == "no-store"
        finally:
            # A failed check must not leave the four parked requests behind.
            services.gate.set()
        assert [response.status_code for response in await asyncio.gather(*calls)] == [200] * 4
        await asyncio.sleep(0)
        assert workers == set()
    assert not any(call[0] == "sweep" for call in services.calls)


def test_no_log_line_names_the_token_a_ref_or_a_hash(caplog: pytest.LogCaptureFixture) -> None:
    services = _Services()
    with caplog.at_level(logging.DEBUG), TestClient(_app(services)) as client:
        client.post(APPLY, headers=MUTATION, json=_apply(ref="private/diary.md"))
        services.error = MutationError("conflict")
        client.post(PREVIEW, headers=MUTATION, json=_op(ref="private/diary.md"))
        client.post(APPLY, headers={"Origin": ORIGIN}, json=_apply(ref="private/diary.md"))
    for secret in ("secret", "diary", HASH):
        assert secret not in caplog.text
