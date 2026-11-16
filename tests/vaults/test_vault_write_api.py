from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.acceptance import test_instruction_save_accept as accept
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault import (
    BACKUP_PREFIX,
    EXCLUDED_DIRS,
    PRIVATE_NAMES,
    STAGE_PREFIX,
    WRITE_ONLY_NAMES,
    is_bot_memory_ref,
)
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PreparedReplacement,
)
from viki_agent.vaults.vault_publication import (
    REQUEST_LEASE_WAIT_S,
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    PublicationRecoveryStatus,
    VaultPublicationService,
)
from viki_agent.vaults.vault_write_api import vault_write_routes
from viki_agent.workspaces import root_key

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
MUTATION = {**AUTH, "Origin": ORIGIN}
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
# A guard on an event that normally fires in milliseconds, never a bound on real work.
HANG_GUARD_S = 5
# The app's whole server, driven over HTTP: the instruction save's acceptance harness.
harness = accept.harness


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.waits: list[float | None] = []
        self.pending: list[PublicationRecoveryStatus] = []
        self.error: Exception | None = None

    async def save(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        content: bytes,
        *,
        expected_sha256: str | None,
        wait_s: float | None = None,
    ) -> PublicationOutcome:
        self.calls.append(
            (workspace_id, policy_revision, root_physical_id, ref, content, expected_sha256)
        )
        self.waits.append(wait_s)
        if self.error is not None:
            raise self.error
        return PublicationOutcome("00000000-0000-4000-8000-000000000002", "saved")

    async def save_instruction(self, *args: object, **kwargs: object) -> PublicationOutcome:
        if self.error is not None:
            raise self.error
        return PublicationOutcome("00000000-0000-4000-8000-000000000002", "saved")

    def recovery_status(self) -> list[PublicationRecoveryStatus]:
        return self.pending


def _app(
    service: object, workers: set[asyncio.Task[Any]] | None = None, **options: float
) -> Starlette:
    tracked = set() if workers is None else workers
    return Starlette(
        routes=vault_write_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=tracked,
            **options,
        )
    )


def _body(**changes: object) -> dict[str, object]:
    return {
        "policy_revision": 3,
        "root_physical_id": "12:34",
        "content": "# Note\n",
        "expected_sha256": None,
        **changes,
    }


def _path(ref: str = "notes/note.md") -> str:
    return f"/api/v2/vaults/{WORKSPACE_ID}/notes/{ref}"


def test_save_requires_auth_and_exact_allowed_origin() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        assert client.put(_path(), json=_body()).status_code == 401
        assert client.put(_path(), headers=AUTH, json=_body()).status_code == 403
        assert (
            client.put(
                _path(), headers={**AUTH, "Origin": "https://evil.invalid"}, json=_body()
            ).status_code
            == 403
        )
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "status": "saved",
        "cleanup_pending": False,
    }
    assert service.calls == [(WORKSPACE_ID, 3, "12:34", "notes/note.md", b"# Note\n", None)]


def test_body_rejects_duplicate_unknown_and_wrong_typed_fields() -> None:
    service = _Service()
    cases = [
        b'{"policy_revision":3,"policy_revision":4,"root_physical_id":"12:34",'
        b'"content":"x","expected_sha256":null}',
        json.dumps({**_body(), "path": "elsewhere.md"}).encode(),
        json.dumps(_body(policy_revision=True)).encode(),
        json.dumps(_body(root_physical_id="not-an-id")).encode(),
        json.dumps(_body(expected_sha256="A" * 64)).encode(),
        json.dumps(_body(content="bad\u0000markdown")).encode(),
    ]
    with TestClient(_app(service)) as client:
        for body in cases:
            response = client.put(
                _path(),
                headers={**MUTATION, "Content-Type": "application/json"},
                content=body,
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_publication"
    assert service.calls == []


def test_only_utf8_markdown_refs_and_two_mib_content_are_admitted() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        for ref in ("note.txt", "%2E%2E%2Fnote.md", ".git/config.md", "CLAUDE.md"):
            assert client.put(_path(ref), headers=MUTATION, json=_body()).status_code == 400
        exact = "x" * (2 * 1024 * 1024)
        assert client.put(_path(), headers=MUTATION, json=_body(content=exact)).status_code == 200
        too_large = "x" * (2 * 1024 * 1024 + 1)
        response = client.put(_path(), headers=MUTATION, json=_body(content=too_large))
        assert response.status_code == 413
        malformed = client.put(
            _path(),
            headers={**MUTATION, "Content-Type": "application/json"},
            content=b"\xff",
        )
        assert malformed.status_code == 400


def _mixed(name: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(name))


def test_excluded_and_unwritable_refs_are_refused_before_the_service() -> None:
    names = (
        *EXCLUDED_DIRS,
        *PRIVATE_NAMES,
        ".env.synthetic",
        *WRITE_ONLY_NAMES,
        f"{STAGE_PREFIX}x.md",
        f"{BACKUP_PREFIX}x.md",
    )
    excluded = [
        f"{parent}{_mixed(name)}{leaf}"
        for name in names
        for parent in ("", "sub/", "a/b/")
        for leaf in ("/n.md", "")
    ]
    unwritable = [
        "what?.md",
        'say "hi".md',
        "a*b.md",
        "notes/<x>.md",
        "a|b/n.md",
        "x" * 253 + ".md",
        "notes/" + "\U0001f600" * 126 + "x.md",
        ".md",
        "..md",
        "notes/.MD",
        "x.markdown",
        # Reserved device names, trailing dots or spaces, and empty or dot segments.
        "COM1.md",
        "nul.md",
        "a./b.md",
        "a /b.md",
        "a//b.md",
        "a/./b.md",
    ]
    service = _Service()
    with TestClient(_app(service)) as client:
        for ref in (*excluded, *unwritable):
            # The client drops a `.` segment, so that dot goes encoded; the route decodes it.
            encoded = quote(ref, safe="/").replace("/./", "/%2E/")
            response = client.put(_path(encoded), headers=MUTATION, json=_body())
            assert response.status_code == 400, ref
            assert response.json()["error"]["code"] == "invalid_ref", ref
        assert service.calls == []
        leaf = "notes/" + "x" * 252 + ".md"
        assert client.put(_path(leaf), headers=MUTATION, json=_body()).status_code == 200
    assert [call[3] for call in service.calls] == [leaf]


def test_bot_memory_root_is_refused_before_save_service() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        for ref in ("bot-memory/x.md", "BoT-MeMoRy/bot/x.md"):
            response = client.put(_path(ref), headers=MUTATION, json=_body())
            assert response.status_code == 400, ref
            assert response.json()["error"]["code"] == "invalid_ref", ref
        assert service.calls == []
        admitted = client.put(_path("notes/bot-memory/x.md"), headers=MUTATION, json=_body())
    assert admitted.status_code == 200
    assert [call[3] for call in service.calls] == ["notes/bot-memory/x.md"]


def test_bot_memory_ref_matches_only_the_first_segment() -> None:
    assert is_bot_memory_ref("bot-memory")
    assert is_bot_memory_ref("BOT-MEMORY/bot/x.md")
    assert not is_bot_memory_ref("notes/bot-memory/x.md")
    assert not is_bot_memory_ref("bot-memory.md")


def test_unsupported_filesystem_is_a_conflict_not_an_outage() -> None:
    service = _Service()
    service.error = MutationError("unsupported_filesystem")
    with TestClient(_app(service)) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_filesystem"


def test_a_read_only_note_names_the_platform_remedy() -> None:
    """`_failure` keeps the Windows remedy text on Windows and names a POSIX-appropriate one
    everywhere else; the running platform's text is what a test on each OS actually sees."""
    service = _Service()
    service.error = MutationError("read_only_target", stage_retained=True)
    with TestClient(_app(service)) as client:
        response = client.put(_path(), headers=MUTATION, json=_body(expected_sha256="a" * 64))
    assert response.status_code == 409
    if sys.platform == "win32":
        message = "This note is read-only. Clear Read-only in its Properties, then save again."
    else:
        message = "This note is read-only or locked. Make it writable, then save again."
    assert response.json()["error"] == {"code": "read_only_target", "message": message}


def test_a_raised_conflict_is_409_not_an_outage() -> None:
    service = _Service()
    service.error = MutationError("conflict", stage_retained=True)
    with TestClient(_app(service)) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_a_stuck_operation_in_the_vault_is_409_like_the_operations_routes() -> None:
    service = _Service()
    service.error = PublicationError("vault_recovery_required")
    with TestClient(_app(service)) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vault_recovery_required"


def test_conflict_and_recovery_required_return_typed_operation_ids() -> None:
    class Conflict(_Service):
        async def save(self, *args: object, **kwargs: object) -> PublicationOutcome:
            return PublicationOutcome("00000000-0000-4000-8000-000000000002", "conflict", True)

    with TestClient(_app(Conflict())) as client:
        conflict = client.put(_path(), headers=MUTATION, json=_body())
    assert conflict.status_code == 409
    assert conflict.json()["operation_id"] == "00000000-0000-4000-8000-000000000002"

    failed = _Service()
    failed.error = PublicationError("recovery_required", "00000000-0000-4000-8000-000000000003")
    with TestClient(_app(failed)) as client:
        recovery = client.put(_path(), headers=MUTATION, json=_body())
    assert recovery.status_code == 503
    assert recovery.json()["error"] == {
        "code": "recovery_required",
        "message": "The note could not be saved.",
        "operation_id": "00000000-0000-4000-8000-000000000003",
    }


def test_recovery_status_is_authenticated_read_only_and_redacted() -> None:
    service = _Service()
    service.pending = [
        PublicationRecoveryStatus(
            "00000000-0000-4000-8000-000000000002",
            WORKSPACE_ID,
            "notes/note.md",
            "backed_up",
            "recovery_required",
            ("forget",),
        )
    ]
    path = "/api/v2/vault-publications/recovery"
    with TestClient(_app(service)) as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={**AUTH, "Origin": "https://evil.invalid"}).status_code == 403
        )
        response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "publications": [
            {
                "operation_id": "00000000-0000-4000-8000-000000000002",
                "workspace_id": WORKSPACE_ID,
                "ref": "notes/note.md",
                "state": "backed_up",
                "reason": "recovery_required",
                "resolvable": ["forget"],
            }
        ]
    }


async def test_four_requests_are_shielded_tracked_and_capacity_bounded() -> None:
    entered = 0
    four_started = asyncio.Event()
    release = asyncio.Event()

    class Blocking(_Service):
        async def save(self, *args: object, **kwargs: object) -> PublicationOutcome:
            nonlocal entered
            entered += 1
            if entered == 4:
                four_started.set()
            await release.wait()
            return PublicationOutcome(str(entered), "saved")

    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(Blocking(), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.put(_path(), headers=MUTATION, json=_body()))
            for _ in range(4)
        ]
        await asyncio.wait_for(four_started.wait(), 5)
        assert len(workers) == 4
        overloaded = await client.put(_path(), headers=MUTATION, json=_body())
        assert overloaded.status_code == 429
        release.set()
        assert all(response.status_code == 200 for response in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert workers == set()


def test_head_on_recovery_answers_like_get_and_refuses_foreign_origin() -> None:
    service = _Service()
    service.pending = [
        PublicationRecoveryStatus(
            "00000000-0000-4000-8000-000000000002",
            WORKSPACE_ID,
            "notes/note.md",
            "backed_up",
            "recovery_required",
        )
    ]
    path = "/api/v2/vault-publications/recovery"
    with TestClient(_app(service)) as client:
        assert client.head(path).status_code == 401
        assert (
            client.head(path, headers={**AUTH, "Origin": "https://evil.invalid"}).status_code == 403
        )
        response = client.head(path, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


INSTRUCTION_PATH = f"/api/v2/vaults/{WORKSPACE_ID}/instructions/AGENTS.md"


def test_the_note_save_waits_for_the_lease_only_until_the_route_deadline() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        assert client.put(_path(), headers=MUTATION, json=_body()).status_code == 200
    with TestClient(_app(service, lease_wait_s=0.25)) as client:
        assert client.put(_path(), headers=MUTATION, json=_body()).status_code == 200
    assert service.waits == [REQUEST_LEASE_WAIT_S, 0.25]
    assert REQUEST_LEASE_WAIT_S < 15  # the vault clients' abort (`vaultApi.ts` `TIMEOUT`)


@pytest.mark.parametrize(
    ("path", "error", "status"),
    [
        (_path(), MutationError("vault_locked"), 423),
        (INSTRUCTION_PATH, MutationError("vault_locked"), 423),
        (INSTRUCTION_PATH, PublicationError("protected_instructions"), 409),
    ],
)
def test_a_held_vault_is_423_and_a_save_that_would_lock_it_is_409(
    path: str, error: MutationError | PublicationError, status: int
) -> None:
    service = _Service()
    service.error = error
    with TestClient(_app(service)) as client:
        response = client.put(path, headers=MUTATION, json=_body(expected_sha256="a" * 64))
    assert response.status_code == status
    assert response.json()["error"]["code"] == error.code


def test_an_unexpected_os_error_logs_its_type_and_number_never_its_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    service = _Service()
    service.error = FileNotFoundError(errno.ENOENT, "synthetic", "/vault-canary/notes/note.md")
    with TestClient(_app(service)) as client:
        response = client.put(_path(), headers=MUTATION, json=_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert [record.getMessage() for record in caplog.records] == [
        "vault publication failed: FileNotFoundError 2"
    ]
    assert "vault-canary" not in caplog.text + "".join(repr(r.__dict__) for r in caplog.records)


# --- the real publication service behind the routes ---------------------------------------------

OLD = b"# old\n"
NEW = "# new\n"
# A note save and an instruction save, each replacing a file at the vault's root.
SAVES = [("plan-canary.md", "notes"), ("AGENTS.md", "instructions")]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _vault(tmp_path: Path) -> tuple[Path, OrdinaryMutationRoot]:
    vault = (tmp_path / "vault").resolve()
    vault.mkdir()
    info = vault.stat()
    root = OrdinaryMutationRoot(
        str(uuid.uuid4()), vault, root_key(vault), f"{info.st_dev}:{info.st_ino}", 7, None
    )
    return vault, root


def _journal(tmp_path: Path) -> PublicationJournal:
    return PublicationJournal(SessionStore(tmp_path / "app" / "state.sqlite3", lambda: {}))


def _files(vault: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in vault.iterdir()}


def _real_client(service: VaultPublicationService) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://t")


def _put_body(root: OrdinaryMutationRoot, content: str, expected: str) -> dict[str, object]:
    return {
        "policy_revision": root.policy_revision,
        "root_physical_id": root.physical_id,
        "content": content,
        "expected_sha256": expected,
    }


class _ShortWait(MutationCoordinator):
    """Records the wait each lease asks for, then gives up after 20 ms, so a request's deadline
    shows here without being waited out."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    @asynccontextmanager
    async def lease(
        self,
        root: OrdinaryMutationRoot,
        refs: Iterable[str],
        *,
        include_git_common: bool = False,
        wait_s: float | None = None,
    ) -> AsyncIterator[MutationLease]:
        self.waits.append(wait_s)
        async with MutationCoordinator.lease(
            self,
            root,
            refs,
            include_git_common=include_git_common,
            wait_s=None if wait_s is None else 0.02,
        ) as lease:
            yield lease


@pytest.mark.parametrize(("ref", "route"), SAVES)
async def test_a_save_behind_a_held_vault_refuses_vault_locked_before_any_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, ref: str, route: str
) -> None:
    """A backup holds the whole vault while it copies. A save sent meanwhile gives up its wait
    before its client does and refuses, so it never lands after the client has reported it
    failed; once the holder lets go, the same save goes through."""
    caplog.set_level(logging.WARNING)
    vault, root = _vault(tmp_path)
    (vault / ref).write_bytes(OLD)
    journal = _journal(tmp_path)
    coordinator = _ShortWait()
    service = VaultPublicationService(journal, lambda *_: root, coordinator)
    path = f"/api/v2/vaults/{root.workspace_id}/{route}/{ref}"
    body = _put_body(root, NEW, _sha(OLD))

    async with _real_client(service) as client:
        async with coordinator.lease(root, ()):  # the whole root, as a backup holds it
            async with asyncio.timeout(HANG_GUARD_S):
                refused = await client.put(path, headers=MUTATION, json=body)
            assert coordinator.waits == [None, REQUEST_LEASE_WAIT_S]
            assert refused.status_code == 423, refused.text
            assert refused.json()["error"]["code"] == "vault_locked"
            assert _files(vault) == {ref: OLD}  # no stage or backup either
            assert journal.list_pending() == []
        saved = await client.put(path, headers=MUTATION, json=body)

    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "saved"
    assert _files(vault) == {ref: NEW.encode()}
    assert caplog.records == []  # a held vault is a refusal, not a failure to log


@pytest.mark.parametrize(("ref", "route"), SAVES)
async def test_a_save_that_stops_partway_logs_its_code_operation_and_cause_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ref: str,
    route: str,
) -> None:
    """The failed step's code, operation and OS error are what a later report needs, and all
    that reaches the log: never the note's name, the vault's path, the text or the token. A
    conflict, which the user resolves, logs nothing."""
    caplog.set_level(logging.WARNING)
    vault, root = _vault(tmp_path)
    (vault / ref).write_bytes(OLD)
    journal = _journal(tmp_path)
    service = VaultPublicationService(journal, lambda *_: root, MutationCoordinator())

    def interrupted(_prepared: PreparedReplacement) -> None:
        raise PermissionError(errno.EACCES, "synthetic", str(vault / ref))

    monkeypatch.setattr(PreparedReplacement, "publish_stage_no_replace", interrupted)
    path = f"/api/v2/vaults/{root.workspace_id}/{route}/{ref}"
    canary = "text-canary-5d1e"
    async with _real_client(service) as client:
        conflict = await client.put(path, headers=MUTATION, json=_put_body(root, canary, "0" * 64))
        failed = await client.put(path, headers=MUTATION, json=_put_body(root, canary, _sha(OLD)))

    [row] = journal.list_pending()
    assert conflict.status_code == 409, conflict.text
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["operation_id"] == row.operation_id
    assert [record.getMessage() for record in caplog.records] == [
        f"vault publication failed: recovery_required {row.operation_id} (PermissionError 13)"
    ]
    logged = caplog.text + "".join(repr(record.__dict__) for record in caplog.records)
    for private in (canary, ref, tmp_path.name, "secret"):
        assert private not in logged, private


def test_an_instruction_save_that_would_lock_its_vault_is_refused(harness: accept.Harness) -> None:
    """Bytes a protected vault records for `AGENTS.md`, saved as an ordinary vault's `AGENTS.md`,
    would make the resolver refuse every later write there, the save that reverts them included.
    The save is refused before any row, and the vault keeps taking writes."""
    shared = "# shared instructions\n"
    protected = harness.create_vault("protected")
    accept.put_file(Path(protected.root.display), "AGENTS.md", shared.encode())
    harness.spaces.inspect_policy(protected.workspace_id)  # records the file's hash
    harness.demote(harness.spaces.get(protected.workspace_id, kind=None), "protected")
    ordinary = harness.create_vault("ordinary")
    vault = Path(ordinary.root.display)
    accept.put_file(vault, "AGENTS.md")
    accept.put_file(vault, "CLAUDE.md")
    before = accept.snapshot(vault)

    refused = harness.instruction(ordinary, "AGENTS.md", shared, accept.sha(accept.OLD))

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "protected_instructions"
    harness.assert_untouched({vault: before})
    # The same bytes under the other name match no protected file, and the vault still saves.
    saved = harness.instruction(ordinary, "CLAUDE.md", shared, accept.sha(accept.OLD))
    assert saved.status_code == 200, saved.text
    note = harness.note(ordinary, "after.md", "x\n", None)
    assert note.status_code == 200, note.text
