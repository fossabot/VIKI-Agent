"""Submitted-copy durability and one-attempt claims, with synthetic data and real DPAPI."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from viki_agent import protection
from viki_agent.conversations import commands
from viki_agent.conversations.command_runtime import CommandRunner
from viki_agent.conversations.commands import (
    CommandConflict,
    CommandError,
    CommandIdentity,
    CommandInput,
    CommandService,
)
from viki_agent.conversations.drafts import DraftService
from viki_agent.conversations.turn_parts import ExcerptPart
from viki_agent.session import CommandDispatchResult, DispatchReason
from viki_agent.state import AppState, Prefs
from viki_agent.turn_input import (
    CLAUDE_MAX_ATTACHMENT_BYTES,
    CLAUDE_MAX_DIMENSION,
    Attachment,
    TurnInput,
)

SID = "00000000-0000-4000-8000-000000000001"
GEN = "00000000-0000-4000-8000-000000000002"
OWNER = "00000000-0000-4000-8000-000000000003"
CID = "00000000-0000-4000-8000-000000000004"
OTHER = "00000000-0000-4000-8000-000000000005"
INPUT = CommandInput(CID, SID, GEN, 1)
SECRET = "synthetic-submitted-secret-8a1d4f naïve\n\0"


@pytest.fixture
def service(tmp_path: Path) -> CommandService:
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    DraftService(state, tmp_path / "vault").save(SID, 0, SECRET)
    return CommandService(state, tmp_path / "vault", owner=OWNER)


def drafts(tmp_path: Path) -> DraftService:
    return DraftService(AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "vault")


def test_acceptance_copies_encrypted_draft_without_changing_editor_or_session(
    service: CommandService, tmp_path: Path
) -> None:
    record, created = service.accept(INPUT)
    assert created and record.state == "accepted" and record.text == SECRET
    assert record.bytes == len(SECRET.encode()) and record.draft_revision == 1
    assert record.created_at == record.updated_at and record.created_at.endswith("Z")
    assert drafts(tmp_path).get(SID).text == SECRET
    assert not AppState(tmp_path / "data" / "state.json", Prefs()).ours(SID)
    for path in (tmp_path / "data").iterdir():
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            assert SECRET.encode("utf-16le") not in path.read_bytes()
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        cipher, digest = connection.execute("SELECT body,request_hash FROM commands").fetchone()
        assert cipher != SECRET.encode()
        assert digest == commands._request_hash(INPUT)  # Identifiers/revision only, not text.


def test_duplicate_and_lookup_recover_immutable_copy_after_draft_clear(
    service: CommandService, tmp_path: Path
) -> None:
    original, _ = service.accept(INPUT)
    drafts(tmp_path).save(SID, 1, "")
    assert service.accept(INPUT) == (original, False)
    assert service.lookup(INPUT) == original
    assert service.get(CID) == original
    with pytest.raises(CommandConflict):
        service.accept(dataclasses.replace(INPUT, draft_revision=2))
    with pytest.raises(CommandConflict):
        service.lookup(dataclasses.replace(INPUT, session_id=OTHER))
    assert service.lookup(dataclasses.replace(INPUT, command_id=OTHER)) is None


def test_attachment_command_snapshots_input_and_keeps_the_asset_referenced(
    service: CommandService, tmp_path: Path
) -> None:
    asset_id = "00000000-0000-4000-8000-000000000006"
    attachment = Attachment(
        asset_id=asset_id,
        media_type="image/png",
        byte_size=4,
        width=1,
        height=1,
        name="synthetic.png",
        source="drop",
        modified_at="2026-09-19T00:00:00.000Z",
    )
    service._store.put_asset(
        {
            "scope": service._scope,
            "asset_id": asset_id,
            "media_type": "image/png",
            "byte_size": 4,
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-09-19T00:00:00.000Z",
        }
    )
    editor = drafts(tmp_path)
    saved = editor.save(SID, 1, TurnInput("", (attachment,)))
    value = CommandInput(OTHER, SID, GEN, saved.revision)
    record, created = service.accept(value)
    assert created and record.text == "" and record.bytes == 0
    assert record.attachments == (attachment,)
    # Editing the draft through the legacy text API drops its draft ref only. The admitted
    # command remains an immutable snapshot and continues to protect its asset from collection.
    editor.save(SID, saved.revision, "new text")
    assert service.get(OTHER).attachments == (attachment,)
    assert service.attachment_metadata(asset_id) == attachment
    assert not service._store.delete_asset_if_unreferenced(service._scope, asset_id)
    reopened = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "vault", owner=OTHER
    )
    before = service._store.path.read_bytes()
    assert reopened.attachment_metadata(asset_id) == attachment
    assert service._store.path.read_bytes() == before
    elsewhere = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "other-vault", owner=OTHER
    )
    assert elsewhere.attachment_metadata(asset_id) is None


ASSET = "00000000-0000-4000-8000-000000000007"


def _put_image(service: CommandService, byte_size: int = 4, width: int = 1) -> Attachment:
    service._store.put_asset(
        {
            "scope": service._scope,
            "asset_id": ASSET,
            "media_type": "image/png",
            "byte_size": byte_size,
            "sha256": "0" * 64,
            "width": width,
            "height": 1,
            "created_at": "2026-09-19T00:00:00.000Z",
        }
    )
    return Attachment(ASSET, "image/png", byte_size, width, 1, "synthetic.png")


def _query(service: CommandService, sql: str) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        return connection.execute(sql).fetchall()


@pytest.mark.parametrize("local", ["expired", "missing"])
def test_submission_refuses_an_attachment_that_is_not_stored_locally(
    service: CommandService, tmp_path: Path, local: str
) -> None:
    saved = drafts(tmp_path).save(SID, 1, TurnInput("look", (_put_image(service),)))
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        if local == "expired":
            connection.execute(
                "UPDATE assets SET local_state='expired',local_removed_at=?",
                ("2026-09-20T00:00:00.000Z",),
            )
        else:
            connection.execute("DELETE FROM assets")  # The store never enables foreign keys.
        connection.commit()
    with pytest.raises(CommandError) as failure:
        service.accept(dataclasses.replace(INPUT, draft_revision=saved.revision))
    assert failure.value.code == "attachment_unavailable"
    assert _query(service, "SELECT count(*) FROM commands") == [(0,)]
    assert _query(service, "SELECT owner_kind,asset_id FROM asset_refs") == [("draft", ASSET)]
    assert drafts(tmp_path).get(SID) == saved


@pytest.mark.parametrize(
    "byte_size,width", [(CLAUDE_MAX_ATTACHMENT_BYTES + 1, 1), (4, CLAUDE_MAX_DIMENSION + 1)]
)
def test_submission_refuses_an_attachment_over_the_claude_envelope(
    service: CommandService, tmp_path: Path, byte_size: int, width: int
) -> None:
    image = _put_image(service, byte_size, width)
    saved = drafts(tmp_path).save(SID, 1, TurnInput("", (image,)))
    with pytest.raises(CommandError) as failure:
        service.accept(dataclasses.replace(INPUT, draft_revision=saved.revision))
    assert failure.value.code == "unsupported_attachment"
    assert _query(service, "SELECT count(*) FROM commands") == [(0,)]
    assert _query(service, "SELECT owner_kind FROM asset_refs") == [("draft",)]


@pytest.mark.parametrize("state", ["accepted", "dispatching", "unknown", "not_dispatched"])
def test_restart_projects_nonterminal_old_owner_as_unknown_without_writing(
    service: CommandService, tmp_path: Path, state: str
) -> None:
    original, _ = service.accept(INPUT)
    if state == "dispatching":
        original = service.mark_dispatching(CID)
    elif state in {"unknown", "not_dispatched"}:
        original = service.finish(CID, state, "internal")  # type: ignore[arg-type]
    reopened = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "vault", owner=OTHER
    )
    before = service._store.path.read_bytes()
    recovered = reopened.get(CID)
    assert recovered.text == SECRET
    if state in {"accepted", "dispatching"}:
        assert (recovered.state, recovered.reason) == ("unknown", "owner_changed")
    else:
        assert recovered == original
    assert reopened.list().commands[0].state == recovered.state
    assert reopened.accept(INPUT) == (recovered, False)
    assert service._store.path.read_bytes() == before
    with pytest.raises(CommandConflict):
        reopened.mark_dispatching(CID)
    with pytest.raises(CommandConflict):
        reopened.finish(CID, "unknown", "internal")


def test_dispatch_claim_is_once_and_terminal_receipt_is_immutable(service: CommandService) -> None:
    service.accept(INPUT)
    assert service.mark_dispatching(CID).state == "dispatching"
    with pytest.raises(CommandConflict):
        service.mark_dispatching(CID)
    final = service.finish(CID, "unknown", "native_unconfirmed")
    assert service.finish(CID, "unknown", "native_unconfirmed") == final
    with pytest.raises(CommandConflict):
        service.finish(CID, "not_dispatched", "send_failed")
    with pytest.raises(CommandConflict):
        service.mark_dispatching(CID)
    assert service.accept(INPUT) == (final, False)


@pytest.mark.parametrize("claimed", [False, True])
def test_proven_no_call_can_finish_not_dispatched(service: CommandService, claimed: bool) -> None:
    service.accept(INPUT)
    if claimed:
        service.mark_dispatching(CID)
    assert service.finish(CID, "not_dispatched", "runtime_changed").state == "not_dispatched"


@pytest.mark.parametrize(
    "field,value",
    [
        ("command_id", "../../bad"),
        ("session_id", "new"),
        ("generation", GEN.upper().replace("a", "A") + "x"),
        ("draft_revision", 0),
        ("draft_revision", True),
        ("draft_revision", 1.0),
        ("draft_revision", 1 << 53),
    ],
)
def test_lookup_and_accept_validate_even_without_an_actor(
    service: CommandService, field: str, value: Any
) -> None:
    bad = dataclasses.replace(INPUT, **{field: value})
    for call in (service.lookup, service.accept):
        with pytest.raises(CommandError) as failure:
            call(bad)
        assert failure.value.code == "invalid_command"
    assert service.list().commands == []


def test_scope_and_missing_draft_and_revision_are_explicit(
    service: CommandService, tmp_path: Path
) -> None:
    for value in (
        dataclasses.replace(INPUT, session_id=OTHER),
        dataclasses.replace(INPUT, draft_revision=2),
    ):
        with pytest.raises(CommandError) as failure:
            service.accept(value)
        assert failure.value.code == "draft_conflict"
    drafts(tmp_path).save(SID, 1, "")
    with pytest.raises(CommandError) as failure:
        service.accept(dataclasses.replace(INPUT, draft_revision=2))
    assert failure.value.code == "empty_draft"
    with pytest.raises(CommandError) as missing:
        service.get(CID)
    assert missing.value.code == "not_found"
    other = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "other-vault", owner=OWNER
    )
    assert other.list().commands == [] and other.lookup(INPUT) is None


def test_two_clients_race_to_one_acceptance_and_one_dispatch_claim(service: CommandService) -> None:
    barrier = threading.Barrier(2)

    def accept():
        barrier.wait()
        try:
            return service.accept(INPUT)
        except CommandError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (pool.submit(accept), pool.submit(accept))]
    assert sum(isinstance(value, tuple) and value[1] for value in outcomes) == 1
    assert all(not isinstance(value, CommandError) or value.code == "busy" for value in outcomes)
    assert service.accept(INPUT)[1] is False
    barrier.reset()

    def claim():
        barrier.wait()
        try:
            return service.mark_dispatching(CID)
        except CommandError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [future.result() for future in (pool.submit(claim), pool.submit(claim))]
    assert sum(not isinstance(value, CommandError) for value in claims) == 1
    assert all(
        not isinstance(value, CommandError) or value.code in {"busy", "conflict"}
        for value in claims
    )


@pytest.mark.parametrize("boundary", ["INSERT INTO commands", "COMMIT"])
def test_failed_acceptance_publishes_no_receipt_and_preserves_draft(
    service: CommandService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.startswith(boundary):
                raise sqlite3.OperationalError("synthetic-private-error-body")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw))
        with pytest.raises(CommandError) as failure:
            service.accept(INPUT)
        assert failure.value.code == "unavailable" and "synthetic" not in str(failure.value)
    assert service.lookup(INPUT) is None and drafts(tmp_path).get(SID).text == SECRET


@pytest.mark.parametrize("state", ["dispatching", "unknown"])
def test_failed_transition_commit_preserves_last_durable_state(
    service: CommandService, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    original_record, _ = service.accept(INPUT)
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("synthetic failure")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw))
        with pytest.raises(CommandError):
            if state == "dispatching":
                service.mark_dispatching(CID)
            else:
                service.finish(CID, "unknown", "internal")
    assert service.get(CID) == original_record


@pytest.mark.parametrize(
    "helper", ["protect", "unprotect", "require_private_directory", "require_private_file"]
)
def test_protection_failure_has_no_plaintext_fallback_or_command(
    service: CommandService, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    def fail(*a: Any, **kw: Any):
        raise protection.ProtectionError()

    with monkeypatch.context() as patch:
        patch.setattr(protection, helper, fail)
        with pytest.raises(CommandError) as failure:
            service.accept(INPUT)
        assert failure.value.code == "protection"
    assert service.lookup(INPUT) is None


def test_list_is_metadata_only_and_paginated(
    service: CommandService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.accept(INPUT)
    drafts(tmp_path).save(SID, 1, "second metadata")
    service.accept(dataclasses.replace(INPUT, command_id=OTHER, draft_revision=2))
    monkeypatch.setattr(
        protection, "unprotect", lambda *a, **kw: pytest.fail("list decrypted text")
    )
    first = service.list(limit=1)
    assert [row.command_id for row in first.commands] == [CID] and first.next_after == CID
    assert "text" not in dataclasses.asdict(first.commands[0])
    second = service.list(after=first.next_after, limit=1)
    assert [row.command_id for row in second.commands] == [OTHER] and second.next_after is None


@pytest.mark.parametrize("quota", ["MAX_COMMANDS", "MAX_SCOPE_BYTES"])
def test_quota_refuses_without_eviction_and_duplicates_still_recover(
    service: CommandService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quota: str
) -> None:
    monkeypatch.setattr(commands, quota, 1 if quota == "MAX_COMMANDS" else len(SECRET.encode()))
    first, _ = service.accept(INPUT)
    drafts(tmp_path).save(SID, 1, "quota followup")
    with pytest.raises(CommandError) as failure:
        service.accept(dataclasses.replace(INPUT, command_id=OTHER, draft_revision=2))
    assert failure.value.code == "capacity"
    assert service.accept(INPUT) == (first, False)
    assert len(service.list().commands) == 1


@pytest.mark.parametrize(
    "column,value", [("generation", OTHER), ("draft_revision", 2), ("body", b"bad")]
)
def test_tampered_identity_or_ciphertext_is_never_recovered(
    service: CommandService, column: str, value: Any
) -> None:
    service.accept(INPUT)
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        connection.execute(f"UPDATE commands SET {column}=?", (value,))
        connection.commit()
    with pytest.raises(CommandError) as failure:
        service.get(CID)
    assert failure.value.code in {"corrupt", "protection"}


def test_exact_plaintext_limit_and_cipher_context(service: CommandService, tmp_path: Path) -> None:
    text = "x" * (1 << 20)
    drafts(tmp_path).save(SID, 1, text)
    record, _ = service.accept(dataclasses.replace(INPUT, draft_revision=2))
    assert record.text == text and record.bytes == 1 << 20
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        cipher = connection.execute("SELECT body FROM commands").fetchone()[0]
    with pytest.raises(protection.ProtectionError):
        protection.unprotect(cipher, context=service._context(INPUT))  # Wrong submitted revision.


@pytest.mark.parametrize(
    "kwargs", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"after": "new"}]
)
def test_page_bounds(service: CommandService, kwargs: dict[str, Any]) -> None:
    with pytest.raises(CommandError) as failure:
        service.list(**kwargs)
    assert failure.value.code == "invalid_page"


def test_acceptance_holds_the_draft_revision_until_submitted_copy_commits(
    service: CommandService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = drafts(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = protection.protect

    def held(data: bytes, *, context: bytes) -> bytes:
        if context.startswith(b"VIKI submitted turn"):
            entered.set()
            release.wait()
        return original(data, context=context)

    monkeypatch.setattr(protection, "protect", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.accept, INPUT)
        try:
            assert entered.wait(0.05)
            with pytest.raises(Exception) as busy:
                editor.save(SID, 1, "new typing")
            assert getattr(busy.value, "code", "") == "busy"
        finally:
            release.set()
        receipt, created = future.result()
    assert created and receipt.text == SECRET
    editor.save(SID, 1, "new typing")
    assert service.get(CID).text == SECRET


def test_response_decryption_failure_after_commit_still_has_recoverable_submission(
    service: CommandService, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = protection.unprotect
    calls = 0

    def fail_receipt(data: bytes, *, context: bytes) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:  # Draft decrypted inside the transaction; receipt after commit fails.
            raise protection.ProtectionError()
        return original(data, context=context)

    with monkeypatch.context() as patch:
        patch.setattr(protection, "unprotect", fail_receipt)
        with pytest.raises(CommandError) as failure:
            service.accept(INPUT)
        assert failure.value.code == "protection"
    receipt = service.get(CID)
    assert receipt.state == "accepted" and receipt.text == SECRET
    assert service.accept(INPUT) == (receipt, False)


def test_ciphertext_from_another_command_cannot_replace_submitted_copy(
    service: CommandService,
    tmp_path: Path,
) -> None:
    service.accept(INPUT)
    drafts(tmp_path).save(SID, 1, "second ciphertext")
    service.accept(dataclasses.replace(INPUT, command_id=OTHER, draft_revision=2))
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        cipher = connection.execute(
            "SELECT body FROM commands WHERE command_id=?", (OTHER,)
        ).fetchone()[0]
        connection.execute("UPDATE commands SET body=? WHERE command_id=?", (cipher, CID))
        connection.commit()
    with pytest.raises(CommandError) as failure:
        service.get(CID)
    assert failure.value.code == "protection"


@pytest.mark.parametrize("plaintext", [b"x", b"\xff" * len(SECRET.encode())])
def test_decrypted_length_and_utf8_are_checked(
    service: CommandService, monkeypatch: pytest.MonkeyPatch, plaintext: bytes
) -> None:
    service.accept(INPUT)
    monkeypatch.setattr(protection, "unprotect", lambda *a, **kw: plaintext)
    with pytest.raises(CommandError) as failure:
        service.get(CID)
    assert failure.value.code == "corrupt"


VAULT_ID = "0f1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
PART = ExcerptPart(VAULT_ID, "notes/area/note.md", 10, 20, "a" * 64)


def test_a_command_accepted_from_a_format_3_draft_keeps_its_excerpts(
    service: CommandService, tmp_path: Path
) -> None:
    saved = drafts(tmp_path).save(SID, 1, "quote this", parts=(PART,))
    value = dataclasses.replace(INPUT, draft_revision=saved.revision)
    record, created = service.accept(value)
    assert created and record.text == "quote this" and record.parts == (PART,)
    assert record.turn_input == TurnInput("quote this", (), (PART,))
    assert _query(service, "SELECT format FROM commands") == [(3,)]
    assert service.accept(value) == (record, False)  # The stored v3 body decodes on replay.
    reopened = CommandService(
        AppState(tmp_path / "data" / "state.json", Prefs()), tmp_path / "vault", owner=OWNER
    )
    assert reopened.get(CID) == record


def test_the_v3_submitted_context_binds_the_format_digit(
    service: CommandService, tmp_path: Path
) -> None:
    saved = drafts(tmp_path).save(SID, 1, "quote this", parts=(PART,))
    value = dataclasses.replace(INPUT, draft_revision=saved.revision)
    service.accept(value)
    body = _query(service, "SELECT body FROM commands")[0][0]
    context = (
        f"VIKI submitted turn v3\0{service._scope}\0{CID}\0{SID}\0{SID}\0{GEN}\0{saved.revision}"
    ).encode("ascii")
    assert service._context(value, 3) == context
    assert (
        protection.unprotect(body, context=context) == TurnInput("quote this", (), (PART,)).encode()
    )
    for wrong in (
        context.replace(b"v3", b"v2"),  # the v2 row's own context
        context.replace(b"v3", b"v"),  # the digit dropped
        context.replace(b" v3", b""),
    ):
        with pytest.raises(protection.ProtectionError):
            protection.unprotect(body, context=wrong)
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        connection.execute("UPDATE commands SET format=2 WHERE command_id=?", (CID,))
        connection.commit()
    with pytest.raises(CommandError) as failure:
        service.get(CID)
    assert failure.value.code == "protection"


def test_queue_claimed_command_row_still_reads(service: CommandService) -> None:
    """The queue-claim acceptance path is gone, but a command row it once wrote must still
    decode: a claim kept the claimed item's id as `source_draft_id` (never the session id) and
    sealed its body under the v2 context keyed on that identity. The hash and context below are
    independent literals, not `commands._request_hash`/`CommandService._context` (the functions
    this pins): a regression in either is caught here instead of silently passing because both
    sides of the row used the same, equally regressed function."""
    identity = CommandIdentity(CID, SID, GEN, 1, CID)
    legacy_hash = hashlib.sha256(
        json.dumps(dataclasses.asdict(identity), sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    context = f"VIKI submitted turn v2\0{service._scope}\0{CID}\0{SID}\0{CID}\0{GEN}\0{1}".encode(
        "ascii"
    )
    turn = TurnInput("legacy queue claim text")
    cipher = protection.protect(turn.encode(), context=context)
    stamp = "2026-10-24T00:00:00.000Z"

    def changed(*_args: Any) -> dict[str, Any]:
        return {
            **dataclasses.asdict(identity),
            "request_hash": legacy_hash,
            "owner": OWNER,
            "state": "accepted",
            "reason": "",
            "body": cipher,
            "format": 2,
            "text_bytes": turn.text_bytes,
            "created_at": stamp,
            "updated_at": stamp,
        }

    service._store.accept_command(service._scope, CID, SID, 1, CID, changed)
    assert service.get(CID).text == turn.text


def test_a_text_only_command_keeps_the_released_format_1_body(service: CommandService) -> None:
    record, _ = service.accept(INPUT)
    stored_format, body = _query(service, "SELECT format,body FROM commands")[0]
    assert stored_format == 1 and record.parts == ()
    context = (
        f"VIKI submitted turn v1\0{service._scope}\0{CID}\0{SID}\0{GEN}\0{INPUT.draft_revision}"
    ).encode("ascii")
    assert service._context(INPUT) == context
    assert protection.unprotect(body, context=context) == SECRET.encode("utf-8")


EXCERPT_REASONS = [
    "excerpt_with_command",
    "excerpt_wrong_vault",
    "excerpt_stale",
    "excerpt_unavailable",
    "excerpts_too_large",
]


def test_every_dispatch_reason_the_session_can_return_is_a_recordable_command_reason() -> None:
    assert set(get_args(DispatchReason)) <= commands.REASONS
    assert all(len(reason) <= 32 for reason in commands.REASONS)  # The column's CHECK.


class _RefusingActor:
    def __init__(self, reason: DispatchReason) -> None:
        self.reason: DispatchReason = reason

    async def send_command(self, *, expected_generation: str, accept: Any, dispatch_intent: Any):
        await accept()
        await dispatch_intent()
        return CommandDispatchResult("not_dispatched", self.reason)


class _Manager:
    def __init__(self, actor: _RefusingActor) -> None:
        self.actor = actor

    def get(self, _session_id: str) -> _RefusingActor:
        return self.actor


@pytest.mark.parametrize("reason", EXCERPT_REASONS)
async def test_a_refused_excerpt_ends_not_dispatched_with_its_own_reason(
    service: CommandService, reason: str
) -> None:
    # The runner's real finish() is the whole point: it rejects a reason outside REASONS, and
    # the runner swallows that, leaving the row at dispatching and later projected as internal.
    runner = CommandRunner(service, cast(Any, _Manager(_RefusingActor(cast(Any, reason)))))
    done = asyncio.Event()
    await runner.submit(INPUT, finished=lambda _status: done.set())
    await asyncio.wait_for(done.wait(), 5)  # Hang guard; the attempt ends in milliseconds.
    for record in (service.get(CID), await runner.get(CID)):
        assert (record.state, record.reason) == ("not_dispatched", reason)
    assert _query(service, "SELECT state,reason FROM commands") == [("not_dispatched", reason)]


def test_uncertain_lists_earlier_launches_unknown_sends_only(
    service: CommandService, tmp_path: Path
) -> None:
    def stamp(n: int) -> str:
        return f"2026-10-01T00:00:0{n}.000Z"

    # (owner, state, reason, updated_at) per row; OTHER is an earlier launch.
    shapes = {
        "failed": (OTHER, "unknown", "send_failed", stamp(2)),
        "cut": (OTHER, "dispatching", "", stamp(3)),
        "native": (OTHER, "unknown", "native_unconfirmed", stamp(4)),
        "unsent": (OTHER, "not_dispatched", "internal", stamp(4)),
        "mine": (OWNER, "unknown", "send_failed", stamp(4)),
        "old": (OTHER, "unknown", "internal", stamp(0)),
        "edge": (OTHER, "unknown", "send_failed", stamp(1)),  # exactly `since`: kept
    }
    ids: dict[str, str] = {}
    for n, name in enumerate(shapes, start=10):
        sid = f"00000000-0000-4000-8000-{n:012x}"
        ids[name] = f"00000000-0000-4000-8000-{n + 100:012x}"
        drafts(tmp_path).save(sid, 0, name)
        service.accept(CommandInput(ids[name], sid, GEN, 1))
        owner, state, reason, updated = shapes[name]
        with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
            connection.execute(
                "UPDATE commands SET owner=?,state=?,reason=?,updated_at=? WHERE command_id=?",
                (owner, state, reason, updated, ids[name]),
            )
            connection.commit()
    before = service._store.path.read_bytes()

    found = service.uncertain(stamp(1), 10)

    # Newest first by time, though the ids run the other way: failed < cut < edge.
    assert [(item.command_id, item.state, item.reason) for item in found] == [
        (ids["cut"], "unknown", "owner_changed"),
        (ids["failed"], "unknown", "send_failed"),
        (ids["edge"], "unknown", "send_failed"),
    ]
    assert [item.command_id for item in service.uncertain(stamp(1), 1)] == [ids["cut"]]
    assert service._store.path.read_bytes() == before
    for since, limit in (("yesterday", 1), (stamp(1), 0), (stamp(1), commands.MAX_PAGE_SIZE + 1)):
        with pytest.raises(CommandError) as failure:
            service.uncertain(since, limit)
        assert failure.value.code == "invalid_page"
