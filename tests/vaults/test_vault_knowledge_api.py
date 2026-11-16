from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.vaults import memory_record
from viki_agent.vaults.memory import (
    Corrected,
    InvalidMemory,
    MemoryItem,
    MemoryList,
    MemoryServiceError,
    Proposed,
    Reviewed,
    Written,
)
from viki_agent.vaults.memory_record import Fields, NoteSource, SessionSource
from viki_agent.vaults.vault_attachments import AttachmentError, KeptAttachment
from viki_agent.vaults.vault_backup import BackupError, BackupPreview, BackupResult, BackupSkip
from viki_agent.vaults.vault_knowledge_api import (
    MAX_PATH_CHARS,
    MAX_REQUEST_BYTES,
    vault_knowledge_routes,
)
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.vault_restore import (
    BackupRestoreError,
    BackupRestorePreview,
    BackupRestoreProblem,
    BackupRestoreResult,
)

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
EVIL = "https://evil.invalid"
MUTATION = {**AUTH, "Origin": ORIGIN}
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
# Letters, so an upper-case spelling is a different, non-canonical string.
MEMORY_ID = "a0000000-0000-4000-8000-000000000002"
COMMAND_ID = "c0000000-0000-4000-8000-000000000003"
ASSET_ID = "b0000000-0000-4000-8000-000000000004"
OPERATION_ID = "d0000000-0000-4000-8000-000000000005"
NEW_ID = "e0000000-0000-4000-8000-000000000006"
SESSION_ID = "f0000000-0000-4000-8000-000000000007"
HASH = "a" * 64
OTHER_HASH = "b" * 64
CLAIM = "The owner prefers tea."
# An absolute folder on whichever platform runs the tests: "C:/" or "/".
ABS = os.path.abspath(os.sep).replace("\\", "/")
# The new registration's root, which no answer may name.
ROOT_DISPLAY = f"{ABS}hidden-root/Restored copy"
LONGEST_TAIL = MAX_PATH_CHARS - len(ABS)

VAULT = f"/api/v2/vaults/{WORKSPACE_ID}"
KEEP = f"{VAULT}/attachments"
MEMORIES = f"{VAULT}/memories"
COMPOSE = f"{VAULT}/memories/compose"
MEMORY = f"{VAULT}/memories/{MEMORY_ID}"
BACKUP_PREVIEW = f"{VAULT}/backup/preview"
BACKUP = f"{VAULT}/backup"
RESTORE_PREVIEW = "/api/v2/backup-restore/preview"
RESTORE = "/api/v2/backup-restore"


def _keep(**changes: object) -> dict[str, Any]:
    return {
        "command_id": COMMAND_ID,
        "policy_revision": 3,
        "root_physical_id": "12:34",
        "asset_id": ASSET_ID,
        "name": "cat",
        **changes,
    }


def _draft(**changes: object) -> dict[str, Any]:
    return {
        "kind": "owner",
        "scope": "vault",
        "claim": CLAIM,
        "sources": [{"note": "notes/tea.md", "sha256": HASH}, {"session": SESSION_ID}],
        **changes,
    }


def _create(**changes: object) -> dict[str, Any]:
    return {
        "command_id": NEW_ID,
        "policy_revision": 3,
        "root_physical_id": "12:34",
        **_draft(),
        **changes,
    }


def _patch(action: str = "approve", **changes: object) -> dict[str, Any]:
    extra: dict[str, object] = {"retire": {"retired_by": None}, "correct": {"claim": CLAIM}}.get(
        action, {}
    )
    return {
        "action": action,
        "sha256": HASH,
        "policy_revision": 3,
        "root_physical_id": "12:34",
        **extra,
        **changes,
    }


def _restore(**changes: object) -> dict[str, Any]:
    return {
        "command_id": COMMAND_ID,
        "backup": f"{ABS}Backups/vault backup 20261002-000000",
        "parent": f"{ABS}Restored",
        "folder": "Restored copy",
        **changes,
    }


# (method, path, a valid body, the success status)
ROUTES: list[tuple[str, str, dict[str, Any] | None, int]] = [
    ("POST", KEEP, _keep(), 201),
    ("GET", MEMORIES, None, 200),
    ("POST", MEMORIES, _create(), 201),
    ("POST", COMPOSE, _draft(), 200),
    ("PATCH", MEMORY, _patch(), 200),
    ("POST", BACKUP_PREVIEW, {"destination": f"{ABS}Backups"}, 200),
    ("POST", BACKUP, {"command_id": COMMAND_ID, "destination": f"{ABS}Backups"}, 201),
    ("POST", RESTORE_PREVIEW, {"backup": f"{ABS}Backups/b"}, 200),
    ("POST", RESTORE, _restore(), 201),
]
# Every route whose handler reaches a service (compose is pure).
SERVICE_ROUTES = [route for route in ROUTES if route[1] != COMPOSE]


def _fields(**changes: object) -> Fields:
    base: dict[str, Any] = {
        "id": MEMORY_ID,
        "kind": "owner",
        "scope": "vault",
        "status": "approved",
        "created": "2026-10-02T00:00:00.000Z",
        "sources": (NoteSource("notes/tea.md", HASH),),
    }
    return Fields(**{**base, **changes})


def _written(memory_id: str = MEMORY_ID, created: tuple[str, ...] = ()) -> Written:
    return Written(memory_id, f"memory/{memory_id}.md", OTHER_HASH, OPERATION_ID, created)


@dataclass(frozen=True)
class _Workspace:
    workspace_id: str
    name: str
    policy: str
    root: str


class _Services:
    """One fake for each of the four services; every call is recorded, then `error` raised."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.entered = 0
        self.four_entered = asyncio.Event()
        self.kept = KeptAttachment("attachments/cat.png", False, 3)
        self.listing = MemoryList((), (), False)
        self.on_loop: bool | None = None
        self.approved: Reviewed | Written = Reviewed(MEMORY_ID, HASH)
        self.promoted: Written | Proposed = _written(NEW_ID, ("memory",))
        self.promote_replayed = False
        self.backup_replayed = False
        self.restore_replayed = False
        self.attachments = _Attachments(self)
        self.memories = _Memories(self)
        self.backups = _Backups(self)
        self.restores = _Restores(self)

    async def hit(self, *call: object) -> None:
        self.calls.append(call)
        if self.gate is not None:
            self.entered += 1
            if self.entered == 4:
                self.four_entered.set()
            await self.gate.wait()
        if self.error is not None:
            raise self.error


class _Attachments:
    def __init__(self, services: _Services) -> None:
        self.s = services

    async def keep(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        asset_id: str,
        name: str,
    ) -> KeptAttachment:
        await self.s.hit("keep", workspace_id, policy_revision, root_physical_id, asset_id, name)
        return self.s.kept


class _Memories:
    def __init__(self, services: _Services) -> None:
        self.s = services

    def list(self, workspace_id: str) -> MemoryList:
        try:
            asyncio.get_running_loop()
            self.s.on_loop = True
        except RuntimeError:
            self.s.on_loop = False
        self.s.calls.append(("list", workspace_id))
        if self.s.error is not None:
            raise self.s.error
        return self.s.listing

    async def approve(self, *args: object) -> Reviewed | Written:
        await self.s.hit("approve", *args)
        return self.s.approved

    async def retire(self, *args: object, retired_by: str | None = None) -> Written:
        await self.s.hit("retire", *args, retired_by)
        return _written()

    async def correct(self, *args: object) -> Corrected:
        await self.s.hit("correct", *args)
        return Corrected(_written(NEW_ID), _written())

    async def narrow_scope(self, *args: object) -> Written:
        await self.s.hit("narrow_scope", *args)
        return _written()

    async def promote(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        *,
        command_id: str,
        kind: str,
        scope: str,
        claim: str,
        sources: tuple[object, ...] = (),
    ) -> tuple[Written | Proposed, bool]:
        await self.s.hit(
            "promote",
            workspace_id,
            policy_revision,
            root_physical_id,
            command_id,
            kind,
            scope,
            claim,
            sources,
        )
        return self.s.promoted, self.s.promote_replayed


class _Backups:
    def __init__(self, services: _Services) -> None:
        self.s = services

    async def preview(self, workspace_id: str, destination: str) -> BackupPreview:
        await self.s.hit("backup_preview", workspace_id, destination)
        return BackupPreview(
            2, 30, ("CLAUDE.md",), (BackupSkip("link", "reparse_point"),), local_recovery=True
        )

    async def run(
        self, command_id: str, workspace_id: str, destination: str
    ) -> tuple[BackupResult, bool]:
        await self.s.hit("backup", command_id, workspace_id, destination)
        return BackupResult("Vault backup 20261002-000000", 2, 30, ()), self.s.backup_replayed


class _Restores:
    def __init__(self, services: _Services) -> None:
        self.s = services

    async def preview(self, backup: str) -> BackupRestorePreview:
        await self.s.hit("restore_preview", backup)
        return BackupRestorePreview(
            "Vault", "protected", True, 2, 30, ("CLAUDE.md",), (), backup_skipped=1
        )

    async def apply(
        self, command_id: str, backup: str, parent: str, folder: str
    ) -> tuple[BackupRestoreResult, bool]:
        await self.s.hit("restore", command_id, backup, parent, folder)
        workspace = _Workspace(NEW_ID, "Restored copy", "protected", ROOT_DISPLAY)
        result = BackupRestoreResult(
            workspace,  # type: ignore[arg-type]
            1,
            10,
            False,
            (BackupRestoreProblem("notes/gone.md", "missing"),),
        )
        return result, self.s.restore_replayed


def _app(services: _Services, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=vault_knowledge_routes(
            services.attachments,  # type: ignore[arg-type]
            services.memories,  # type: ignore[arg-type]
            services.backups,  # type: ignore[arg-type]
            services.restores,  # type: ignore[arg-type]
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
        )
    )


def _send(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
):
    return client.request(method, path, headers=headers, json=body)


def _raw(client: TestClient, method: str, path: str, content: bytes):
    return client.request(
        method, path, headers={**MUTATION, "Content-Type": "application/json"}, content=content
    )


def test_every_route_refuses_an_unauthorized_request_before_anything() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for method, path, body, _ in ROUTES:
            for headers in ({}, {"Origin": EVIL}, {"Authorization": "Bearer wrong"}):
                response = _send(client, method, path, body, headers)
                assert response.status_code == 401, (path, headers)
                assert response.json()["error"]["code"] == "unauthorized"
                assert response.headers["cache-control"] == "no-store"
            # Bearer comes first: a malformed body without the token is still a 401.
            garbled = client.request(
                method, path, headers={"Content-Type": "application/json"}, content=b"{"
            )
            assert garbled.status_code == 401
            response = _send(client, method, path, body, {**AUTH, "Origin": EVIL})
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "forbidden_origin"
            assert response.headers["cache-control"] == "no-store"
            if method != "GET":
                missing = _send(client, method, path, body, AUTH)
                assert missing.status_code == 403, path
                assert missing.json()["error"]["code"] == "forbidden_origin"
    assert services.calls == []


def test_every_route_answers_an_authorized_request_with_no_store() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for method, path, body, status in ROUTES:
            response = _send(client, method, path, body, MUTATION)
            assert response.status_code == status, path
            assert response.headers["cache-control"] == "no-store"
        # A read needs no Origin, as a same-origin GET sends none.
        assert client.get(MEMORIES, headers=AUTH).status_code == 200
        assert client.head(MEMORIES, headers=AUTH).status_code == 200


def test_keep_answers_201_when_created_and_200_when_reused_and_drops_the_command_id() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        created = client.post(KEEP, headers=MUTATION, json=_keep())
        services.kept = KeptAttachment("attachments/cat.png", True, 3)
        reused = client.post(KEEP, headers=MUTATION, json=_keep())
        unnamed = client.post(KEEP, headers=MUTATION, json=_keep(name=""))
        longest = client.post(KEEP, headers=MUTATION, json=_keep(name="n" * 255))
    assert created.status_code == 201
    assert created.json() == {"ref": "attachments/cat.png", "reused": False, "byte_size": 3}
    assert reused.status_code == 200
    assert reused.json() == {"ref": "attachments/cat.png", "reused": True, "byte_size": 3}
    assert unnamed.status_code == longest.status_code == 200
    assert services.calls == [
        ("keep", WORKSPACE_ID, 3, "12:34", ASSET_ID, "cat"),
        ("keep", WORKSPACE_ID, 3, "12:34", ASSET_ID, "cat"),
        ("keep", WORKSPACE_ID, 3, "12:34", ASSET_ID, ""),
        ("keep", WORKSPACE_ID, 3, "12:34", ASSET_ID, "n" * 255),
    ]


def test_keep_takes_no_sha256_and_refuses_a_malformed_field_before_the_service() -> None:
    bodies = [
        _keep(sha256=HASH),  # the server takes the asset row's digest
        {key: value for key, value in _keep().items() if key != "command_id"},
        _keep(command_id=COMMAND_ID.upper()),
        _keep(command_id="not-a-uuid"),
        _keep(asset_id="../asset"),
        _keep(asset_id=ASSET_ID.upper()),
        _keep(name=None),
        _keep(name="n" * 256),
        _keep(policy_revision=True),
        _keep(policy_revision=0),
        _keep(policy_revision=9_007_199_254_740_992),
        _keep(root_physical_id="not-an-id"),
        _keep(root_physical_id=1234),
    ]
    services = _Services()
    with TestClient(_app(services)) as client:
        for body in bodies:
            response = client.post(KEEP, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
    assert services.calls == []


def test_a_protected_vault_is_refused_only_where_a_service_refuses_it() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        # Keep-in-vault and a memory write raise `vault_protected`: 403 with its code.
        for error, method, path, body in (
            (AttachmentError("vault_protected"), "POST", KEEP, _keep()),
            (AttachmentError("vault_read_only"), "POST", KEEP, _keep()),
            (MemoryServiceError("vault_protected"), "PATCH", MEMORY, _patch("retire")),
            (MemoryServiceError("vault_protected"), "PATCH", MEMORY, _patch("approve")),
        ):
            services.error = error
            response = _send(client, method, path, body, MUTATION)
            assert response.status_code == 403, error
            assert response.json() == {
                "error": {"code": error.code, "message": "The vault request did not complete."}
            }
        services.error = None
        # A protected create is a proposal, told apart by its outcome; nothing was written.
        services.promoted = Proposed(NEW_ID, f"memory/{NEW_ID}.md", OPERATION_ID, OTHER_HASH)
        proposed = client.post(MEMORIES, headers=MUTATION, json=_create())
        # Re-approving an approved record only records its hash, in any vault.
        reviewed = client.patch(MEMORY, headers=MUTATION, json=_patch())
        # A backup of a protected vault runs.
        backup = client.post(
            BACKUP, headers=MUTATION, json={"command_id": COMMAND_ID, "destination": f"{ABS}B"}
        )
    assert proposed.status_code == 201
    assert proposed.json() == {
        "memory_id": NEW_ID,
        "ref": f"memory/{NEW_ID}.md",
        "proposal_id": OPERATION_ID,
        "proposed_sha256": OTHER_HASH,
        "outcome": "proposed",
        "replayed": False,
    }
    assert reviewed.status_code == 200
    assert reviewed.json() == {"memory_id": MEMORY_ID, "sha256": HASH, "outcome": "reviewed"}
    assert backup.status_code == 201


def test_memory_list_runs_off_the_loop_and_shows_an_invalid_record_with_its_text() -> None:
    services = _Services()
    record = MemoryItem(
        f"memory/{MEMORY_ID}.md",
        HASH,
        "pending",
        _fields(sources=(NoteSource("notes/tea.md", HASH), SessionSource(SESSION_ID))),
        CLAIM,
        NEW_ID,
    )
    services.listing = MemoryList(
        (record,),
        (
            InvalidMemory("memory/broken.md", "duplicate_key", "---\nviki_memory: 1\n"),
            InvalidMemory("memory/locked.md", "read_failed", None),
        ),
        True,
    )
    with TestClient(_app(services)) as client:
        response = client.get(MEMORIES, headers=AUTH)
        query = client.get(f"{MEMORIES}?all=1", headers=AUTH)
    assert response.status_code == 200
    assert services.on_loop is False
    assert response.json() == {
        "items": [
            {
                "ref": f"memory/{MEMORY_ID}.md",
                "sha256": HASH,
                "state": "pending",
                "fields": {
                    "id": MEMORY_ID,
                    "kind": "owner",
                    "scope": "vault",
                    "status": "approved",
                    "created": "2026-10-02T00:00:00.000Z",
                    "reviewed": None,
                    "creator": None,
                    "sources": [{"note": "notes/tea.md", "sha256": HASH}, {"session": SESSION_ID}],
                    "supersedes": None,
                    "retired_by": None,
                },
                "claim": CLAIM,
                "superseded_by": NEW_ID,
            }
        ],
        "invalid": [
            {
                "ref": "memory/broken.md",
                "reason": "duplicate_key",
                "text": "---\nviki_memory: 1\n",
                "state": "invalid",
            },
            {"ref": "memory/locked.md", "reason": "read_failed", "text": None, "state": "invalid"},
        ],
        "partial": True,
    }
    assert query.status_code == 400
    assert query.json()["error"]["code"] == "invalid_request"
    assert services.calls == [("list", WORKSPACE_ID)]


def test_memory_create_passes_the_draft_and_answers_what_was_written() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(MEMORIES, headers=MUTATION, json=_create())
    assert response.status_code == 201
    assert response.json() == {
        "memory_id": NEW_ID,
        "ref": f"memory/{NEW_ID}.md",
        "sha256": OTHER_HASH,
        "operation_id": OPERATION_ID,
        "created_folders": ["memory"],
        "outcome": "written",
        "replayed": False,
    }
    assert services.calls == [
        (
            "promote",
            WORKSPACE_ID,
            3,
            "12:34",
            NEW_ID,
            "owner",
            "vault",
            CLAIM,
            (NoteSource("notes/tea.md", HASH), SessionSource(SESSION_ID)),
        )
    ]


def test_a_replayed_memory_create_is_200_in_both_shapes_and_a_written_has_no_operation() -> None:
    services = _Services()
    services.promote_replayed = True
    services.promoted = Written(NEW_ID, f"memory/{NEW_ID}.md", OTHER_HASH, None)
    with TestClient(_app(services)) as client:
        written = client.post(MEMORIES, headers=MUTATION, json=_create())
        services.promoted = Proposed(NEW_ID, f"memory/{NEW_ID}.md", OPERATION_ID, OTHER_HASH)
        proposed = client.post(MEMORIES, headers=MUTATION, json=_create())
    assert written.status_code == proposed.status_code == 200
    assert written.json() == {
        "memory_id": NEW_ID,
        "ref": f"memory/{NEW_ID}.md",
        "sha256": OTHER_HASH,
        "operation_id": None,
        "created_folders": [],
        "outcome": "written",
        "replayed": True,
    }
    assert proposed.json() == {
        "memory_id": NEW_ID,
        "ref": f"memory/{NEW_ID}.md",
        "proposal_id": OPERATION_ID,
        "proposed_sha256": OTHER_HASH,
        "outcome": "proposed",
        "replayed": True,
    }


def test_memory_bodies_are_strict_before_the_service() -> None:
    create = [
        {key: value for key, value in _create().items() if key != "sources"},
        {key: value for key, value in _create().items() if key != "command_id"},
        _create(command_id=COMMAND_ID.upper()),
        _create(command_id="not-a-uuid"),
        _create(command_id=7),
        _create(kind=1),
        _create(scope=None),
        _create(claim=["x"]),
        _create(claim="nul\u0000claim"),
        _create(sources={"note": "a.md"}),
        _create(sources=["notes/tea.md"]),
        _create(sources=[{"note": "notes/tea.md"}]),
        _create(sources=[{"note": "notes/tea.md", "sha256": HASH, "extra": "x"}]),
        _create(sources=[{"session": 7}]),
        _create(policy_revision="3"),
    ]
    patch = [
        _patch(command_id=COMMAND_ID),
        _patch(action="delete"),
        {**_patch(), "action": None},
        _patch(claim=CLAIM),  # approve takes no claim
        {key: value for key, value in _patch("retire").items() if key != "retired_by"},
        _patch("retire", retired_by="not-a-uuid"),
        _patch("retire", retired_by=NEW_ID.upper()),
        {key: value for key, value in _patch("correct").items() if key != "claim"},
        _patch("correct", claim="bad\u0001claim"),
        _patch("narrow_scope", retired_by=None),
        _patch(sha256="A" * 64),
        _patch(sha256=None),
        _patch(root_physical_id="1:"),
    ]
    services = _Services()
    with TestClient(_app(services)) as client:
        for body in create:
            response = client.post(MEMORIES, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
        for body in patch:
            response = client.patch(MEMORY, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
        for memory_id in (MEMORY_ID.upper(), "memory", "x" * 36):
            response = client.patch(f"{MEMORIES}/{memory_id}", headers=MUTATION, json=_patch())
            assert response.status_code == 400, memory_id
        for workspace_id in (WORKSPACE_ID[:-1] + "A", WORKSPACE_ID.replace("-", "")):
            response = client.post(
                f"/api/v2/vaults/{workspace_id}/memories", headers=MUTATION, json=_create()
            )
            assert response.status_code == 400, workspace_id
    assert services.calls == []


def test_each_patch_action_reaches_its_service_method_and_answers_its_outcome() -> None:
    services = _Services()
    services.approved = _written()
    with TestClient(_app(services)) as client:
        approve = client.patch(MEMORY, headers=MUTATION, json=_patch())
        retire = client.patch(MEMORY, headers=MUTATION, json=_patch("retire", retired_by=NEW_ID))
        plain = client.patch(MEMORY, headers=MUTATION, json=_patch("retire"))
        correct = client.patch(MEMORY, headers=MUTATION, json=_patch("correct", claim="Coffee."))
        narrow = client.patch(MEMORY, headers=MUTATION, json=_patch("narrow_scope"))
    written = {
        "memory_id": MEMORY_ID,
        "ref": f"memory/{MEMORY_ID}.md",
        "sha256": OTHER_HASH,
        "operation_id": OPERATION_ID,
        "created_folders": [],
        "outcome": "written",
    }
    for response in (approve, retire, plain, narrow):
        assert response.status_code == 200
        assert response.json() == written
    assert correct.status_code == 200
    assert correct.json() == {
        "record": {**written, "memory_id": NEW_ID, "ref": f"memory/{NEW_ID}.md"},
        "retired": written,
        "outcome": "corrected",
    }
    common = (WORKSPACE_ID, MEMORY_ID, HASH)
    assert services.calls == [
        ("approve", *common, 3, "12:34"),
        ("retire", *common, 3, "12:34", NEW_ID),
        ("retire", *common, 3, "12:34", None),
        ("correct", *common, "Coffee.", 3, "12:34"),
        ("narrow_scope", *common, 3, "12:34"),
    ]


def test_compose_returns_the_candidate_text_and_calls_no_service() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        response = client.post(COMPOSE, headers=MUTATION, json=_draft())
        bad = client.post(COMPOSE, headers=MUTATION, json=_draft(kind="rumour"))
        empty = client.post(COMPOSE, headers=MUTATION, json=_draft(claim=" "))
        extra = client.post(COMPOSE, headers=MUTATION, json=_create())
    assert response.status_code == 200
    assert set(response.json()) == {"text"}
    parsed = memory_record.parse(response.json()["text"])
    assert isinstance(parsed, memory_record.Record)
    assert (parsed.fields.kind, parsed.fields.scope, parsed.fields.status) == (
        "owner",
        "vault",
        "candidate",
    )
    assert parsed.claim == CLAIM
    assert parsed.fields.sources == (NoteSource("notes/tea.md", HASH), SessionSource(SESSION_ID))
    assert bad.status_code == empty.status_code == 400
    assert bad.json()["error"] == {
        "code": "invalid_input",
        "message": "The vault request did not complete.",
        "reason": "bad_value",
    }
    assert empty.json()["error"]["reason"] == "bad_claim"
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "invalid_request"
    assert services.calls == []


def test_a_memory_refusal_carries_ids_and_revisions_and_nothing_else() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        services.error = MemoryServiceError("stale_revision", current_revision=OTHER_HASH)
        stale = client.patch(MEMORY, headers=MUTATION, json=_patch())
        services.error = MemoryServiceError(
            "vault_locked", operation_id=None, claim=CLAIM, ref="memory/private.md"
        )
        locked = client.patch(MEMORY, headers=MUTATION, json=_patch("narrow_scope"))
        services.error = MemoryServiceError(
            "conflict", operation_id=OPERATION_ID, replacement_id=NEW_ID, current_revision=7
        )
        half = client.patch(MEMORY, headers=MUTATION, json=_patch("correct"))
        services.error = MemoryServiceError("invalid_record", reason="unknown_key")
        invalid = client.patch(MEMORY, headers=MUTATION, json=_patch())
    message = "The vault request did not complete."
    assert stale.status_code == 409
    assert stale.json() == {
        "error": {"code": "stale_revision", "message": message, "current_revision": OTHER_HASH}
    }
    assert locked.status_code == 409
    assert locked.json() == {"error": {"code": "vault_locked", "message": message}}
    assert half.status_code == 409
    assert half.json() == {
        "error": {
            "code": "conflict",
            "message": message,
            "operation_id": OPERATION_ID,
            "replacement_id": NEW_ID,
        }
    }
    assert invalid.status_code == 409
    assert invalid.json()["error"]["reason"] == "unknown_key"


def test_keep_refusals_name_their_code_and_operation_id_only() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        services.error = AttachmentError("conflict", OPERATION_ID)
        conflict = client.post(KEEP, headers=MUTATION, json=_keep())
        services.error = AttachmentError("asset_unavailable")
        missing = client.post(KEEP, headers=MUTATION, json=_keep())
        services.error = AttachmentError("vault_locked")
        locked = client.post(KEEP, headers=MUTATION, json=_keep())
    message = "The vault request did not complete."
    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {"code": "conflict", "message": message, "operation_id": OPERATION_ID}
    }
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "asset_unavailable", "message": message}}
    assert locked.status_code == 409
    assert locked.json() == {"error": {"code": "vault_locked", "message": message}}


def test_backup_preview_and_run_answer_their_shape_and_a_replay_is_200() -> None:
    services = _Services()
    run = {"command_id": COMMAND_ID, "destination": f"{ABS}Backups"}
    with TestClient(_app(services)) as client:
        preview = client.post(
            BACKUP_PREVIEW, headers=MUTATION, json={"destination": f"{ABS}Backups"}
        )
        first = client.post(BACKUP, headers=MUTATION, json=run)
        services.backup_replayed = True
        replay = client.post(BACKUP, headers=MUTATION, json=run)
    assert preview.status_code == 200
    assert preview.json() == {
        "files": 2,
        "bytes": 30,
        "instruction_files": ["CLAUDE.md"],
        "instruction_files_total": 1,
        "skipped": [{"ref": "link", "reason": "reparse_point"}],
        "skipped_total": 1,
        "local_recovery": True,
    }
    result = {
        "folder": "Vault backup 20261002-000000",
        "files": 2,
        "bytes": 30,
        "skipped": [],
        "skipped_total": 0,
    }
    assert first.status_code == 201
    assert first.json() == {**result, "replayed": False}
    assert replay.status_code == 200
    assert replay.json() == {**result, "replayed": True}
    assert services.calls == [
        ("backup_preview", WORKSPACE_ID, f"{ABS}Backups"),
        ("backup", COMMAND_ID, WORKSPACE_ID, f"{ABS}Backups"),
        ("backup", COMMAND_ID, WORKSPACE_ID, f"{ABS}Backups"),
    ]


def test_backup_bodies_take_no_scope_or_admission_and_no_control_code() -> None:
    previews = [
        {"destination": f"{ABS}Backups", "scope": "notes"},
        {"destination": f"{ABS}Backups", "policy_revision": 3},
        {"destination": ""},
        {"destination": f"{ABS}Back\u0000ups"},
        {"destination": f"{ABS}Back\nups"},
        {"destination": ABS + "x" * (LONGEST_TAIL + 1)},
        {"destination": 7},
        {},
    ]
    runs = [
        {"command_id": COMMAND_ID, "destination": f"{ABS}B", "scope": "all"},
        {"command_id": COMMAND_ID, "destination": f"{ABS}B", "root_physical_id": "12:34"},
        {"command_id": "x", "destination": f"{ABS}B"},
        {"destination": f"{ABS}B"},
    ]
    services = _Services()
    with TestClient(_app(services)) as client:
        for body in previews:
            response = client.post(BACKUP_PREVIEW, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
        for body in runs:
            response = client.post(BACKUP, headers=MUTATION, json=body)
            assert response.status_code == 400, body
        longest = client.post(
            BACKUP_PREVIEW, headers=MUTATION, json={"destination": ABS + "x" * LONGEST_TAIL}
        )
        assert longest.status_code == 200
    assert services.calls == [("backup_preview", WORKSPACE_ID, ABS + "x" * LONGEST_TAIL)]


def test_backup_restore_answers_ids_and_counts_and_never_the_new_root() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        preview = client.post(RESTORE_PREVIEW, headers=MUTATION, json={"backup": f"{ABS}Backups/b"})
        first = client.post(RESTORE, headers=MUTATION, json=_restore())
        services.restore_replayed = True
        replay = client.post(RESTORE, headers=MUTATION, json=_restore())
    assert preview.status_code == 200
    assert preview.json() == {
        "source_name": "Vault",
        "source_policy": "protected",
        "protected": True,
        "files": 2,
        "bytes": 30,
        "instruction_files": ["CLAUDE.md"],
        "instruction_files_total": 1,
        "problems": [],
        "problems_total": 0,
        "backup_skipped": 1,
    }
    result = {
        "workspace": {"workspace_id": NEW_ID, "name": "Restored copy", "policy": "protected"},
        "files": 1,
        "bytes": 10,
        "verified": False,
        "problems": [{"ref": "notes/gone.md", "reason": "missing"}],
        "problems_total": 1,
    }
    assert first.status_code == 201
    assert first.json() == {**result, "replayed": False}
    assert replay.status_code == 200
    assert replay.json() == {**result, "replayed": True}
    assert "hidden-root" not in first.text
    body = _restore()
    assert services.calls[1] == (
        "restore",
        COMMAND_ID,
        body["backup"],
        body["parent"],
        body["folder"],
    )


def test_backup_restore_bodies_are_strict_before_the_service() -> None:
    previews = [
        {"backup": f"{ABS}b", "destination": f"{ABS}b"},
        {"backup": f"{ABS}b\u0000"},
        {"backup": ""},
        {"backup": None},
    ]
    applies = [
        {key: value for key, value in _restore().items() if key != "folder"},
        _restore(policy_revision=3),
        _restore(command_id="1"),
        _restore(folder=""),
        _restore(folder="x" * 256),
        _restore(folder="a\tb"),
        _restore(parent=f"{ABS}p\u007f"),
        _restore(backup=[f"{ABS}b"]),
    ]
    services = _Services()
    with TestClient(_app(services)) as client:
        for body in previews:
            response = client.post(RESTORE_PREVIEW, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
        for body in applies:
            response = client.post(RESTORE, headers=MUTATION, json=body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == "invalid_request", body
    assert services.calls == []


# A path the server would resolve against its own working directory. `C:Backups` is
# drive-relative and `\Backups` is rooted without a drive: neither is absolute.
RELATIVE_PATHS = ["Backups", "./Backups", "../Backups", "C:Backups", "\\Backups", "~/Backups"]


@pytest.mark.parametrize("relative", RELATIVE_PATHS)
def test_each_path_field_refuses_a_relative_path_before_the_service(relative: str) -> None:
    requests = [
        (BACKUP_PREVIEW, {"destination": relative}),  # backup preview: destination
        (BACKUP, {"command_id": COMMAND_ID, "destination": relative}),  # backup run: destination
        (RESTORE_PREVIEW, {"backup": relative}),  # backup restore preview: backup
        (RESTORE, _restore(backup=relative)),  # backup restore: backup
        (RESTORE, _restore(parent=relative)),  # backup restore: parent
    ]
    services = _Services()
    with TestClient(_app(services)) as client:
        for path, body in requests:
            response = client.post(path, headers=MUTATION, json=body)
            assert response.status_code == 400, (path, body)
            assert response.json() == {
                "error": {"code": "invalid_path", "message": "The vault request did not complete."}
            }, (path, body)
    assert services.calls == []


def test_the_envelope_is_refused_before_the_body_is_read() -> None:
    services = _Services()
    with TestClient(_app(services)) as client:
        for method, path, body, _ in ROUTES:
            if body is None:
                continue
            encoded = json.dumps(body).encode()
            for headers in (
                {"Content-Type": "text/plain"},
                {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            ):
                response = client.request(
                    method, path, headers={**MUTATION, **headers}, content=encoded
                )
                assert response.status_code == 400, (path, headers)
                assert response.json()["error"]["code"] == "invalid_request"
            query = client.request(method, f"{path}?x=1", headers=MUTATION, json=body)
            assert query.status_code == 400, path
            pairs = encoded.decode()[1:-1]
            for content in (
                b"{" + pairs.encode() + b"," + pairs.encode() + b"}",  # every key twice
                b"[]",
                b"\xff",
                b"{",
            ):
                response = _raw(client, method, path, content)
                assert response.status_code == 400, (path, content)
                assert response.json()["error"]["code"] == "invalid_request"
    assert services.calls == []


def test_a_body_past_the_bound_is_413_with_its_code() -> None:
    services = _Services()
    padding = "x" * MAX_REQUEST_BYTES
    with TestClient(_app(services)) as client:
        response = client.post(MEMORIES, headers=MUTATION, json=_create(claim=padding))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert services.calls == []


# Every code the four services or their dependencies raise, on its status: every non-ok answer
# is typed.
STATUS_MAP = [
    *(
        (code, 400)
        for code in (
            "invalid_request",
            "invalid_input",
            "invalid_name",
            "invalid_path",
            "invalid_backup",
            "invalid_ref",
            "excluded_path",
            "private_path",
            "not_found",
            "unsafe_path",
        )
    ),
    *(
        (code, 403)
        for code in (
            "vault_protected",
            "vault_read_only",
            "denied_destination",
            "denied_location",
            "protected_overlap",
            "protected_root",
        )
    ),
    *(
        (code, 404)
        for code in ("unknown_workspace", "asset_unavailable", "missing_note", "source_missing")
    ),
    *(
        (code, 409)
        for code in (
            "conflict",
            "stale_revision",
            "invalid_transition",
            "invalid_record",
            "vault_locked",
            "vault_recovery_required",
            "ordinary_workspace_required",
            "workspace_changed",
            "unsupported_filesystem",
            "read_only_target",
            "publication_busy",
            "root_unavailable",
            "overlaps_registration",
            "case_collision",
            "proposal_conflict",
        )
    ),
    *((code, 413) for code in ("too_large", "backup_too_large")),
    ("unsupported_location", 422),
    *((code, 423) for code in ("target_busy", "parent_busy", "stage_busy")),
    *((code, 429) for code in ("publication_limit", "proposals_full")),
    *(
        (code, 503)
        for code in (
            "unavailable",
            "recovery_required",
            "backup_unverified",
            "backup_restore_unverified",
            "unsafe_target",
            "unsafe_parent",
        )
    ),
]


@pytest.mark.parametrize(("code", "status"), STATUS_MAP)
def test_every_refusal_is_typed_on_its_status(code: str, status: int) -> None:
    errors = (
        AttachmentError(code),
        MemoryServiceError(code),
        BackupError(code),
        BackupRestoreError(code),
        MutationError(code, stage_retained=True),
    )
    for error in errors:
        services = _Services()
        services.error = error
        with TestClient(_app(services)) as client:
            for method, path, body, _ in SERVICE_ROUTES:
                response = _send(client, method, path, body, MUTATION)
                assert response.status_code == status, (code, path)
                assert response.json() == {
                    "error": {"code": code, "message": "The vault request did not complete."}
                }, (code, path)
                assert response.headers["cache-control"] == "no-store"


def test_an_os_or_runtime_failure_is_a_typed_503() -> None:
    for error in (OSError("C:/secret/path.md"), RuntimeError("journal")):
        services = _Services()
        services.error = error
        with TestClient(_app(services)) as client:
            for method, path, body, _ in SERVICE_ROUTES:
                response = _send(client, method, path, body, MUTATION)
                assert response.status_code == 503, path
                assert response.json()["error"]["code"] == "unavailable"
                assert "secret" not in response.text


async def test_four_requests_across_routes_are_shielded_tracked_and_bounded() -> None:
    services = _Services()
    services.gate = asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(services, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.post(KEEP, headers=MUTATION, json=_keep())),
            asyncio.create_task(client.patch(MEMORY, headers=MUTATION, json=_patch())),
            asyncio.create_task(
                client.post(BACKUP_PREVIEW, headers=MUTATION, json={"destination": f"{ABS}B"})
            ),
            asyncio.create_task(client.post(RESTORE, headers=MUTATION, json=_restore())),
        ]
        await asyncio.wait_for(services.four_entered.wait(), 0.05)
        assert len(workers) == 4
        overloaded = await client.post(COMPOSE, headers=MUTATION, json=_draft())
        assert overloaded.status_code == 429
        assert overloaded.json()["error"]["code"] == "overloaded"
        assert overloaded.headers["cache-control"] == "no-store"
        services.gate.set()
        statuses = [response.status_code for response in await asyncio.gather(*calls)]
        assert statuses == [201, 200, 200, 201]
        await asyncio.sleep(0)
        assert workers == set()


def test_no_log_line_names_the_token_a_ref_a_claim_or_a_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    services = _Services()
    secrets = ("secret", "diary", "tea", "Private", HASH)
    with caplog.at_level(logging.DEBUG), TestClient(_app(services)) as client:
        client.post(MEMORIES, headers=MUTATION, json=_create(claim="My diary says tea."))
        client.post(BACKUP_PREVIEW, headers=MUTATION, json={"destination": f"{ABS}Private"})
        services.error = MemoryServiceError("conflict", operation_id=OPERATION_ID)
        client.patch(MEMORY, headers=MUTATION, json=_patch("correct", claim="diary"))
        services.error = OSError(f"{ABS}Private/diary.md")
        client.post(RESTORE, headers=MUTATION, json=_restore(parent=f"{ABS}Private"))
        client.post(MEMORIES, headers={"Origin": ORIGIN}, json=_create(claim="diary"))
    for secret in secrets:
        assert secret not in caplog.text


@pytest.mark.parametrize("kind", ["backup_preview", "backup", "restore_preview", "restore"])
@pytest.mark.parametrize("alphabet", ["x", "\U0001f600"])
def test_backup_diagnostics_fit_the_client_without_changing_the_full_result(
    kind: str, alphabet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _Services()
    prefix = "/".join((alphabet * 70, *(alphabet * 60 for _ in range(7))))
    refs = tuple(f"{prefix}/{index:04}/CLAUDE.md" for index in range(1_000))
    assert len(refs[0]) == 512
    skipped = tuple(
        BackupSkip(f"{alphabet * 34}{index:04}.md", "too_large") for index in range(2_001)
    )
    problems = tuple(BackupRestoreProblem(ref, "missing") for ref in refs)
    if kind == "backup_preview":
        result = BackupPreview(1_000, 99, refs, skipped, True)
        service, method, route = services.backups, "preview", BACKUP_PREVIEW
        request = {"destination": f"{ABS}B"}
        lists = ("instruction_files", "skipped")
    elif kind == "backup":
        result = BackupResult("Vault backup", 7, 99, skipped)
        service, method, route = services.backups, "run", BACKUP
        request = {"command_id": COMMAND_ID, "destination": f"{ABS}B"}
        lists = ("skipped",)
    elif kind == "restore_preview":
        result = BackupRestorePreview("Vault", "protected", True, 1_000, 99, refs, problems, 2_001)
        service, method, route = services.restores, "preview", RESTORE_PREVIEW
        request = {"backup": f"{ABS}B"}
        lists = ("instruction_files", "problems")
    else:
        result = BackupRestoreResult(
            _Workspace(NEW_ID, "Restored copy", "protected", ROOT_DISPLAY),  # type: ignore[arg-type]
            7,
            99,
            False,
            problems,
        )
        service, method, route = services.restores, "apply", RESTORE
        request = _restore()
        lists = ("problems",)
    original = asdict(result)

    async def answer(*_args: object):
        return (result, True) if kind in {"backup", "restore"} else result

    monkeypatch.setattr(service, method, answer)
    with TestClient(_app(services)) as client:
        response = client.post(route, headers=MUTATION, json=request)
        replay = client.post(route, headers=MUTATION, json=request)
    assert response.status_code == replay.status_code == 200
    assert len(response.content) <= 256 * 1024
    assert response.content == replay.content
    body = response.json()
    for name in lists:
        assert body[f"{name}_total"] == len(original[name])
        assert len(body[name]) <= 2_000
        assert body[name] == json.loads(json.dumps(original[name][: len(body[name])]))
    assert any(len(body[name]) < len(original[name]) for name in lists)
    if kind == "restore_preview":
        assert body["problems"]  # problems get the byte budget before instruction refs
    for key, value in original.items():
        if key not in {*lists, "workspace"}:
            assert body[key] == value
    if kind in {"backup", "restore"}:
        assert body["replayed"] is True
    assert (
        asdict(result) == original
    )  # projection must not alter receipts or the complete manifest data


def test_backup_skip_row_count_and_unrepresentable_refs_are_reported_as_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _Services()
    rows = tuple(BackupSkip(f"{index}.md", "unreadable") for index in range(2_001))

    async def answer(*_args: object) -> BackupPreview:
        return BackupPreview(7, 99, (), rows, False)

    monkeypatch.setattr(services.backups, "preview", answer)
    with TestClient(_app(services)) as client:
        first = client.post(BACKUP_PREVIEW, headers=MUTATION, json={"destination": f"{ABS}B"})
        rows = (BackupSkip("x" * 513, "unsafe_name"), *rows)
        second = client.post(BACKUP_PREVIEW, headers=MUTATION, json={"destination": f"{ABS}B"})
    assert len(first.content) < 256 * 1024
    assert len(first.json()["skipped"]) == 2_000
    assert first.json()["skipped_total"] == 2_001
    assert second.json()["skipped"] == []
    assert second.json()["skipped_total"] == 2_002
    assert (second.json()["files"], second.json()["bytes"]) == (7, 99)
