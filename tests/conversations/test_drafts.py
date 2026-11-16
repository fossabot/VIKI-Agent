"""Encrypted draft service/CAS tests in synthetic app data, never provider-owned storage."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection, storage
from viki_agent.conversations import drafts
from viki_agent.conversations.drafts import Draft, DraftConflict, DraftError, DraftService
from viki_agent.conversations.turn_parts import ExcerptPart
from viki_agent.state import AppState, Prefs
from viki_agent.turn_input import Attachment, TurnInput

SID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DraftService:
    # The ACL module has real Windows ACL tests. This suite isolates transaction behavior
    # from the test runner's temporary-directory inheritance; DPAPI itself remains real.
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None, raising=False)
    return DraftService(AppState(tmp_path / "state.json", Prefs()), tmp_path / "synthetic-vault")


def test_absent_draft_and_empty_save_never_create_session_or_draft_rows(
    service: DraftService,
    tmp_path: Path,
) -> None:
    empty = Draft(SID, 0, "", None)
    assert service.get(SID) == empty
    assert service.save(SID, 0, "") == empty
    state = AppState(tmp_path / "state.json", Prefs())
    assert not state.ours(SID)
    assert service.list().drafts == []
    with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM drafts").fetchone() == (0,)


def test_ciphertext_roundtrip_restart_and_scope_separation(
    service: DraftService,
    tmp_path: Path,
) -> None:
    text = "private-synthetic-draft-83ec9b54-naïve\n\0"
    saved = service.save("new", 0, text)
    assert saved.revision == 1 and saved.saved_at is not None
    reopened = DraftService(
        AppState(tmp_path / "state.json", Prefs()), tmp_path / "synthetic-vault"
    )
    assert reopened.get("new") == saved
    other = DraftService(AppState(tmp_path / "state.json", Prefs()), tmp_path / "different-vault")
    assert other.get("new") == Draft("new", 0, "", None)
    assert other.list().drafts == []
    for path in tmp_path.iterdir():
        if path.is_file():
            data = path.read_bytes()
            assert text.encode("utf-8") not in data and text.encode("utf-16le") not in data


@pytest.mark.skipif(sys.platform == "darwin", reason="pins the pre-NFC scope bytes")
def test_draft_scope_keeps_the_old_normcase_bytes_on_windows_and_linux(tmp_path: Path) -> None:
    expected = hashlib.sha256(os.path.normcase(str(tmp_path.resolve())).encode("utf-8")).hexdigest()
    assert drafts.draft_scope(tmp_path) == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS case and normalization insensitivity")
def test_draft_scope_merges_case_and_nfc_nfd_spellings_on_darwin(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    (tmp_path / "Vault").mkdir()
    assert drafts.draft_scope(tmp_path / "Vault") == drafts.draft_scope(tmp_path / "VAULT")
    (tmp_path / "café").mkdir()  # NFC spelling
    assert drafts.draft_scope(tmp_path / "café") == drafts.draft_scope(tmp_path / "cafe\u0301")


def test_cas_retry_noop_clear_and_aba_conflict(service: DraftService) -> None:
    first = service.save(SID, 0, "one")
    assert service.save(SID, 0, "one") == first  # Lost response, one revision behind.
    assert service.save(SID, 1, "one") == first  # No changed content, no new timestamp.
    with pytest.raises(DraftConflict) as conflict:
        service.save(SID, 0, "different")
    assert conflict.value.current_revision == 1 and not hasattr(conflict.value, "text")
    cleared = service.save(SID, 1, "")
    assert cleared.revision == 2 and service.get(SID) == cleared
    assert service.list().drafts == []
    third = service.save(SID, 2, "one")
    assert third.revision == 3
    with pytest.raises(DraftConflict) as stale:
        service.save(SID, 0, "one")
    assert stale.value.current_revision == 3


def test_two_windows_race_to_one_committed_revision(
    service: DraftService,
    tmp_path: Path,
) -> None:
    second = DraftService(AppState(tmp_path / "state.json", Prefs()), tmp_path / "synthetic-vault")
    barrier = threading.Barrier(2)

    def save(window: DraftService, text: str) -> Draft | DraftError:
        barrier.wait()
        try:
            return window.save(SID, 0, text)
        except DraftError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(save, window, text)
            for window, text in (
                (service, "window one"),
                (second, "window two"),
            )
        ]
        results = [future.result() for future in futures]
    saved = [result for result in results if isinstance(result, Draft)]
    rejected = [result for result in results if isinstance(result, DraftError)]
    assert len(saved) == len(rejected) == 1
    assert saved[0].revision == 1
    assert rejected[0].code in {"conflict", "busy"}
    assert service.get(SID) == second.get(SID) == saved[0]


@pytest.mark.parametrize("statement", ["INSERT INTO drafts", "COMMIT"])
def test_failed_write_or_commit_preserves_previous_draft(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    before = service.save(SID, 0, "original")
    connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith(statement):
                raise sqlite3.OperationalError("synthetic private SQL and draft")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(
            storage.sqlite3, "connect", lambda *a, **kw: connect(*a, factory=Failing, **kw)
        )
        with pytest.raises(DraftError) as failure:
            service.save(SID, 1, "must not publish")
        assert failure.value.code == "unavailable" and "private SQL" not in str(failure.value)
    assert service.get(SID) == before


def test_protection_and_acl_failures_never_save_plaintext_or_erase_a_record(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.save(SID, 0, "preserve")

    def failed(*args: Any, **kwargs: Any) -> None:
        raise protection.ProtectionError()

    for method in ("protect", "unprotect", "require_private_directory", "require_private_file"):
        with monkeypatch.context() as patch:
            patch.setattr(protection, method, failed)
            with pytest.raises(DraftError) as failure:
                service.save(SID, 1, "new private text")
            assert failure.value.code == "protection"
        assert service.get(SID) == original


def test_ciphertext_cannot_be_moved_to_another_key_or_revision(
    service: DraftService,
    tmp_path: Path,
) -> None:
    service.save(SID, 0, "same length")
    service.save(OTHER, 0, "other value")
    with contextlib.closing(sqlite3.connect(tmp_path / "app.sqlite3")) as connection:
        connection.execute(
            "UPDATE drafts SET body=(SELECT body FROM drafts WHERE key=?) WHERE key=?", (SID, OTHER)
        )
        connection.commit()
    with pytest.raises(DraftError) as failure:
        service.get(OTHER)
    assert failure.value.code == "protection"
    with contextlib.closing(sqlite3.connect(tmp_path / "app.sqlite3")) as connection:
        connection.execute("UPDATE drafts SET revision=2 WHERE key=?", (SID,))
        connection.commit()
    with pytest.raises(DraftError) as changed_revision:
        service.get(SID)
    assert changed_revision.value.code == "protection"


def test_plaintext_length_and_utf8_are_verified_before_publishing(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.save(SID, 0, "a")
    for invalid in (b"longer", b"\xff"):
        with monkeypatch.context() as patch:
            patch.setattr(protection, "unprotect", lambda *a, result=invalid, **kw: result)
            with pytest.raises(DraftError) as failure:
                service.get(SID)
            assert failure.value.code == "corrupt"


def test_list_is_bounded_ordered_metadata_only_and_skips_tombstones(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("new", OTHER, SID):
        service.save(key, 0, f"private {key}")
    service.save(OTHER, 1, "")
    monkeypatch.setattr(
        protection, "unprotect", lambda *a, **kw: pytest.fail("list decrypted a body")
    )
    first = service.list(limit=1)
    assert [row.key for row in first.drafts] == [SID] and first.next_after == SID
    assert set(dataclasses.asdict(first.drafts[0])) == {"key", "revision", "saved_at", "bytes"}
    last = service.list(after=first.next_after, limit=1)
    assert [row.key for row in last.drafts] == ["new"] and last.next_after is None


def test_scope_quotas_include_tombstone_metadata_without_silent_eviction(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafts, "MAX_NONEMPTY_DRAFTS", 1)
    monkeypatch.setattr(drafts, "MAX_DRAFT_KEYS", 2)
    monkeypatch.setattr(drafts, "MAX_SCOPE_BYTES", 5)
    one = service.save(SID, 0, "12345")
    for key, revision, text in ((OTHER, 0, "a"), (SID, 1, "123456")):
        with pytest.raises(DraftError) as failure:
            service.save(key, revision, text)
        assert failure.value.code == "capacity"
    assert service.get(SID) == one
    assert service.save(SID, 1, "").revision == 2
    assert service.save(OTHER, 0, "a").revision == 1
    service.save(OTHER, 1, "")
    with pytest.raises(DraftError) as full_metadata:
        service.save("new", 0, "a")
    assert full_metadata.value.code == "capacity"
    assert service.get(SID).revision == service.get(OTHER).revision == 2


@pytest.mark.parametrize("key", ["", "NEW", "../new", SID.upper(), "x" * 37, None, []])
def test_invalid_keys_are_refused(service: DraftService, key: Any) -> None:
    if key == SID:  # The fixture UUID contains no a-f; test an uppercase letter explicitly.
        key = "AAAAAAAA-0000-4000-8000-000000000001"
    with pytest.raises(DraftError) as failure:
        service.get(key)
    assert failure.value.code == "invalid_key"


@pytest.mark.parametrize("revision", [-1, True, 1.0, drafts.MAX_REVISION + 1, None])
def test_invalid_revisions_are_refused(service: DraftService, revision: Any) -> None:
    with pytest.raises(DraftError) as failure:
        service.save(SID, revision, "text")
    assert failure.value.code == "invalid_revision"


def test_utf8_size_boundary_and_lone_surrogate(service: DraftService) -> None:
    exact = "界" * (drafts.MAX_TEXT_BYTES // 3) + "a"
    assert len(exact.encode("utf-8")) == drafts.MAX_TEXT_BYTES
    saved = service.save(SID, 0, exact)
    assert service.get(SID) == saved
    for text, code in ((exact + "a", "too_large"), ("\ud800", "invalid_text")):
        with pytest.raises(DraftError) as failure:
            service.save(SID, 1, text)
        assert failure.value.code == code
    assert service.get(SID) == saved


def test_typed_draft_rejects_an_escaped_envelope_before_protection(service: DraftService) -> None:
    # JSON spells each NUL as six bytes.  Text itself is at the public 1 MiB limit, but its
    # durable v2 envelope cannot fit the established encrypted-body bound.
    with pytest.raises(DraftError) as failure:
        service.save(SID, 0, TurnInput("\0" * drafts.MAX_TEXT_BYTES))
    assert failure.value.code == "too_large"
    assert service.get(SID) == Draft(SID, 0, "", None)


def test_legacy_save_counts_an_attachment_only_draft_against_capacity(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafts, "MAX_NONEMPTY_DRAFTS", 1)
    asset_id = "00000000-0000-4000-8000-000000000010"
    attachment = Attachment(asset_id, "image/png", 4, 1, 1, "synthetic.png")
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
    service.save(SID, 0, TurnInput("", (attachment,)))
    with pytest.raises(DraftError) as failure:
        service.save(OTHER, 0, "legacy text")
    assert failure.value.code == "capacity"


ASSET = "00000000-0000-4000-8000-000000000011"
LATER = "00000000-0000-4000-8000-000000000012"


def _image(asset_id: str) -> Attachment:
    return Attachment(asset_id, "image/png", 4, 1, 1, "synthetic.png")


def _put(service: DraftService, asset_id: str) -> None:
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


def _draft_refs(service: DraftService) -> list[tuple[str, str]]:
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        return connection.execute(
            "SELECT asset_id,role FROM asset_refs WHERE owner_kind='draft' ORDER BY role"
        ).fetchall()


def test_legacy_replay_never_answers_a_typed_revision_with_text_only(
    service: DraftService,
) -> None:
    # A typed save commits but its response is lost; the user then removes the image and the
    # legacy form arrives at the old revision. Echoing revision 1 would mark a draft saved that
    # still holds the removed image, so the stale save must conflict instead.
    _put(service, ASSET)
    typed = service.save(SID, 0, TurnInput("hello", (_image(ASSET),)))
    with pytest.raises(DraftConflict) as conflict:
        service.save(SID, 0, "hello")
    assert conflict.value.current_revision == 1
    assert service.get(SID) == typed
    assert _draft_refs(service) == [(ASSET, "attachment.0")]


@pytest.mark.parametrize("local", ["expired", "missing"])
def test_save_refuses_an_attachment_that_is_not_stored_locally(
    service: DraftService, local: str
) -> None:
    _put(service, ASSET)
    saved = service.save(SID, 0, TurnInput("keep", (_image(ASSET),)))
    if local == "expired":
        _put(service, LATER)
        with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
            connection.execute(
                "UPDATE assets SET local_state='expired',local_removed_at=? WHERE asset_id=?",
                ("2026-09-20T00:00:00.000Z", LATER),
            )
            connection.commit()
    with pytest.raises(DraftError) as failure:
        service.save(SID, 1, TurnInput("keep", (_image(ASSET), _image(LATER))))
    assert failure.value.code == "attachment_unavailable"
    assert service.get(SID) == saved
    assert _draft_refs(service) == [(ASSET, "attachment.0")]


@pytest.mark.parametrize(
    "field,value", [("media_type", "image/jpeg"), ("byte_size", 5), ("width", 2), ("height", 2)]
)
def test_save_refuses_metadata_that_disagrees_with_its_asset_row(
    service: DraftService, field: str, value: Any
) -> None:
    _put(service, ASSET)
    saved = service.save(SID, 0, TurnInput("keep", (_image(ASSET),)))
    _put(service, LATER)
    forged = dataclasses.replace(_image(LATER), **{field: value})
    with pytest.raises(DraftError) as failure:
        service.save(SID, 1, TurnInput("keep", (forged,)))
    assert failure.value.code == "invalid_attachment"
    assert service.get(SID) == saved
    assert _draft_refs(service) == [(ASSET, "attachment.0")]


def test_busy_writer_returns_safe_failure_and_keeps_last_committed_draft(
    service: DraftService,
    tmp_path: Path,
) -> None:
    before = service.save(SID, 0, "original")
    with contextlib.closing(
        sqlite3.connect(tmp_path / "app.sqlite3", isolation_level=None)
    ) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(DraftError) as failure:
            service.save(SID, 1, "blocked")
        assert failure.value.code == "busy"
        assert service.get(SID) == before
        writer.execute("ROLLBACK")
    assert service.get(SID) == before


def test_acl_refusal_precedes_every_draft_read_and_write(
    service: DraftService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(path: Path) -> None:
        raise protection.ProtectionError()

    def unexpected_sql(*args: Any, **kwargs: Any) -> None:
        pytest.fail("SQL opened before draft ACL admission")

    monkeypatch.setattr(protection, "require_private_directory", refused)
    monkeypatch.setattr(storage.sqlite3, "connect", unexpected_sql)
    for operation in (lambda: service.get(SID), service.list, lambda: service.save(SID, 0, "text")):
        with pytest.raises(DraftError) as failure:
            operation()
        assert failure.value.code == "protection"


def _check_journal(
    monkeypatch: pytest.MonkeyPatch, journal: Path, look: Callable[[int], None]
) -> list[int]:
    """Replace the file ACL check: each look at the journal runs `look(n)`, others pass."""
    looks: list[int] = []

    def check(path: Path) -> None:
        if path == journal:
            looks.append(len(looks) + 1)
            look(looks[-1])

    monkeypatch.setattr(protection, "require_private_file", check)
    return looks


def _refuse(look: int) -> None:
    raise protection.ProtectionError()


def test_private_store_treats_a_journal_deleted_during_the_check_as_absent(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent DELETE-mode commit removes the journal while its ACL is read.
    journal = Path(f"{service._store.path}-journal")
    journal.write_bytes(b"")

    def vanish(look: int) -> None:
        journal.unlink()
        raise protection.ProtectionError()

    looks = _check_journal(monkeypatch, journal, vanish)
    drafts.require_private_store(service._store)
    assert looks == [1]


def test_private_store_checks_a_journal_created_during_the_check_once_more(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent commit creates the journal after the first look found none: the second look
    # passes. `require_private_file` is patched for the whole test (see the `service` fixture), so
    # this does not exercise the real ACL check; it only proves the second look is taken.
    journal = Path(f"{service._store.path}-journal")
    assert not journal.exists()
    passing_check = protection.require_private_file

    def appear(look: int) -> None:
        if look == 1:
            journal.write_bytes(b"")
            raise protection.ProtectionError()
        passing_check(journal)

    looks = _check_journal(monkeypatch, journal, appear)
    drafts.require_private_store(service._store)
    assert looks == [1, 2]


def test_private_store_passes_a_sidecar_that_vanishes_between_two_failed_looks(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both looks fail the ACL check, but a concurrent delete-pending commit removes the journal
    # between them: the second failure refuses only a file still present, so nothing refuses.
    journal = Path(f"{service._store.path}-journal")
    journal.write_bytes(b"")

    def refuse_then_vanish(look: int) -> None:
        if look == 2:
            journal.unlink()
        raise protection.ProtectionError()

    looks = _check_journal(monkeypatch, journal, refuse_then_vanish)
    drafts.require_private_store(service._store)
    assert looks == [1, 2]


def test_private_store_checks_a_present_sidecar_only_once_when_it_passes(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Path(f"{service._store.path}-journal")
    journal.write_bytes(b"")

    def passes(look: int) -> None:
        return None

    looks = _check_journal(monkeypatch, journal, passes)
    drafts.require_private_store(service._store)
    assert looks == [1]


def test_private_store_still_refuses_a_present_sidecar_whose_acl_check_fails(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Path(f"{service._store.path}-journal")
    journal.write_bytes(b"")
    looks = _check_journal(monkeypatch, journal, _refuse)
    with pytest.raises(protection.ProtectionError):
        drafts.require_private_store(service._store)
    assert looks == [1, 2]


def test_private_store_checks_a_present_sidecar_that_exists_reports_absent(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path.exists() answers False on a denied stat, so it cannot gate the ACL check.
    journal = Path(f"{service._store.path}-journal")
    journal.write_bytes(b"")
    real_exists = Path.exists

    def exists(self: Path, *, follow_symlinks: bool = True) -> bool:
        return False if self == journal else real_exists(self, follow_symlinks=follow_symlinks)

    def check(path: Path) -> None:
        if path == journal:
            raise protection.ProtectionError()

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(protection, "require_private_file", check)
    with pytest.raises(protection.ProtectionError):
        drafts.require_private_store(service._store)


def test_private_store_refuses_a_sidecar_whose_recheck_stat_is_denied(
    service: DraftService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path.exists() answers False on a denied stat; only FileNotFoundError means absent.
    journal = Path(f"{service._store.path}-journal")
    real_lstat = drafts.os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == journal:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    _check_journal(monkeypatch, journal, _refuse)
    monkeypatch.setattr(drafts.os, "lstat", lstat)
    with pytest.raises(protection.ProtectionError):
        drafts.require_private_store(service._store)


@pytest.mark.parametrize("after,limit", [("../new", 1), (None, 0), (None, 101), (None, True)])
def test_page_cursor_and_size_are_bounded(service: DraftService, after: Any, limit: Any) -> None:
    with pytest.raises(DraftError) as failure:
        service.list(after=after, limit=limit)
    assert failure.value.code == "invalid_page"


def test_revision_exhaustion_never_wraps_or_changes_existing_content(
    service: DraftService,
    tmp_path: Path,
) -> None:
    service.save(SID, 0, "last")
    ciphertext = protection.protect(b"last", context=service._context(SID, drafts.MAX_REVISION))
    with contextlib.closing(sqlite3.connect(tmp_path / "app.sqlite3")) as connection:
        connection.execute(
            "UPDATE drafts SET revision=?, body=? WHERE key=?",
            (drafts.MAX_REVISION, ciphertext, SID),
        )
        connection.commit()
    last = service.get(SID)
    assert service.save(SID, drafts.MAX_REVISION, "last") == last
    with pytest.raises(DraftError) as failure:
        service.save(SID, drafts.MAX_REVISION, "different")
    assert failure.value.code == "capacity"
    assert service.get(SID) == last


VAULT_ID = "0f1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
FIRST = ExcerptPart(VAULT_ID, "notes/area/note.md", 10, 20, "a" * 64)
SECOND = ExcerptPart(VAULT_ID, "notes/area/other.md", 1, 3, "b" * 64)


def _row(service: DraftService, key: str) -> tuple[int, bytes]:
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        row = connection.execute("SELECT format,body FROM drafts WHERE key=?", (key,)).fetchone()
    assert row is not None
    return row


def test_two_excerpt_parts_reload_from_a_format_3_row(
    service: DraftService, tmp_path: Path
) -> None:
    saved = service.save(SID, 0, "quote these", parts=(FIRST, SECOND))
    assert saved.parts == (FIRST, SECOND) and _row(service, SID)[0] == 3
    reopened = DraftService(
        AppState(tmp_path / "state.json", Prefs()), tmp_path / "synthetic-vault"
    )
    assert reopened.get(SID) == saved
    # Dropping one excerpt at unchanged text is a change, not a lost response.
    fewer = service.save(SID, 1, "quote these", parts=(FIRST,))
    assert fewer.revision == 2 and fewer.parts == (FIRST,)
    assert service.save(SID, 2, "quote these", parts=(FIRST,)) == fewer


def test_the_v3_draft_context_binds_the_format_digit(service: DraftService) -> None:
    saved = service.save(SID, 0, "quote", parts=(FIRST,))
    stored_format, body = _row(service, SID)
    assert stored_format == 3
    context = f"VIKI draft v3\0{service._scope}\0{SID}\0{saved.revision}".encode("ascii")
    assert service._context(SID, saved.revision, 3) == context
    assert protection.unprotect(body, context=context) == TurnInput("quote", (), (FIRST,)).encode()
    for wrong in (
        context.replace(b"v3", b"v2"),  # the v2 row's own context
        context.replace(b"v3", b"v"),  # the digit dropped
        context.replace(b" v3", b""),
    ):
        with pytest.raises(protection.ProtectionError):
            protection.unprotect(body, context=wrong)
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        connection.execute("UPDATE drafts SET format=2 WHERE key=?", (SID,))
        connection.commit()
    with pytest.raises(DraftError) as failure:
        service.get(SID)
    assert failure.value.code == "protection"


def test_a_text_only_save_keeps_the_released_format_1_body(service: DraftService) -> None:
    saved = service.save(SID, 0, "legacy text")
    stored_format, body = _row(service, SID)
    assert stored_format == 1 and saved.parts == ()
    context = f"VIKI draft v1\0{service._scope}\0{SID}\0{saved.revision}".encode("ascii")
    assert protection.unprotect(body, context=context) == b"legacy text"
    assert service.get(SID) == saved


def test_a_typed_save_without_an_excerpt_keeps_the_envelope_format(service: DraftService) -> None:
    # The body is the JSON envelope, so the row must not claim the text-only format 1.
    saved = service.save(SID, 0, "typed", parts=())
    assert _row(service, SID)[0] == 2 and saved.parts == ()
    assert service.get(SID) == saved
    assert service.save(OTHER, 0, TurnInput("typed")).revision == 1
    assert _row(service, OTHER)[0] == 2


def test_a_legacy_text_save_clears_an_excerpt_draft_instead_of_echoing_it(
    service: DraftService,
) -> None:
    typed = service.save(SID, 0, "quote", parts=(FIRST,))
    with pytest.raises(DraftConflict) as conflict:
        service.save(SID, 0, "quote")  # The stale text-only replay must never echo revision 1.
    assert conflict.value.current_revision == 1
    assert service.get(SID) == typed
    cleared = service.save(SID, 1, "quote")
    assert cleared.revision == 2 and cleared.parts == () and _row(service, SID)[0] == 1
    assert service.get(SID) == cleared
