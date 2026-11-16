"""Synthetic app profiles: real encrypted storage, immutable revisions and bounded receipts."""

from __future__ import annotations

import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.profiles.test_profile_document import document
from viki_agent.profiles import service as profiles
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError


def ident() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def service(tmp_path: Path) -> ProfileService:
    return ProfileService(AppState(tmp_path / "state.json", Prefs()).store)


def test_revisions_are_encrypted_immutable_and_retryable(service: ProfileService) -> None:
    profile_id, command_id = ident(), ident()
    original = document()
    first = service.create(profile_id, command_id, original)
    assert service.create(profile_id, command_id, original) == first
    assert service.get(profile_id)["document"] == original
    assert original["instructions"].encode() not in service.store.path.read_bytes()
    changed = {**original, "instructions": "synthetic revised instruction"}
    second = service.revise(profile_id, ident(), 1, changed)
    assert second["revision"] == 2
    assert service.get(profile_id, 1)["document"] == original
    assert service.get(profile_id)["document"] == changed
    assert service.create(profile_id, command_id, original) == first
    with pytest.raises(ProfileError, match="command_conflict"):
        service.create(profile_id, command_id, changed)
    assert service.compare(profile_id, 1, 2)["changes"] == [
        {
            "field": "instructions",
            "before": original["instructions"],
            "after": changed["instructions"],
        }
    ]


def test_competing_revisions_admit_exactly_one(service: ProfileService) -> None:
    profile_id = ident()
    service.create(profile_id, ident(), document())

    def revise() -> str:
        try:
            service.revise(profile_id, ident(), 1, document())
            return "accepted"
        except (ProfileError, StateError) as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: revise(), range(2)))
    assert outcomes.count("accepted") == 1
    assert set(outcomes) <= {"accepted", "conflict", "busy"}
    assert service.get(profile_id)["current_revision"] == 2


def test_archive_receipt_and_cross_runtime_duplicate_keep_restrictions(
    service: ProfileService,
) -> None:
    profile_id, target, archive_id = ident(), ident(), ident()
    original = document()
    original["tools"]["denied"] = ["Bash"]
    service.create(profile_id, ident(), original)
    result = service.archive(profile_id, archive_id, 1, True)
    assert service.archive(profile_id, archive_id, 1, True) == result
    assert service.list() == []
    assert service.list(True)[0]["profile_id"] == profile_id
    with pytest.raises(ProfileError, match="conflict"):
        service.revise(profile_id, ident(), 1, original)
    service.duplicate(profile_id, ident(), 1, target, "codex")
    copied = service.get(target)["document"]
    assert copied["tools"] == original["tools"]
    assert copied["runtime"]["provider"] == "codex"
    assert copied["provenance"]["derived_from"] == {"profile_id": profile_id, "revision": 1}
    assert not service.get(target)["archived"]


def test_duplicate_can_select_an_older_immutable_revision(service: ProfileService) -> None:
    source, target = ident(), ident()
    first = document()
    service.create(source, ident(), first)
    service.revise(source, ident(), 1, {**first, "instructions": "new"})
    service.duplicate(source, ident(), 1, target, "codex")
    copied = service.get(target)["document"]
    assert copied["instructions"] == first["instructions"]
    assert copied["provenance"]["derived_from"]["revision"] == 1


def test_corrupt_receipt_cannot_report_success(service: ProfileService) -> None:
    profile_id, command_id = ident(), ident()
    service.create(profile_id, command_id, document())
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE profile_commands SET result_revision=NULL WHERE command_id=?", (command_id,)
        )
    with pytest.raises(ProfileError, match="corrupt"):
        service.create(profile_id, command_id, document())


def test_capacity_rolls_back_profile_revision_and_receipt(
    service: ProfileService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id, command_id = ident(), ident()
    monkeypatch.setattr(profiles, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(ProfileError, match="capacity"):
        service.create(profile_id, command_id, document())
    assert service.list() == []
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM profile_commands").fetchone()[0] == 0
    monkeypatch.setattr(profiles, "MAX_TOTAL_BYTES", 32 << 20)
    service.create(profile_id, command_id, document())
    monkeypatch.setattr(profiles, "MAX_REVISIONS", 1)
    with pytest.raises(ProfileError, match="capacity"):
        service.revise(profile_id, ident(), 1, document())
    assert service.get(profile_id)["current_revision"] == 1


def test_misbound_ciphertext_and_newer_format_are_never_rewritten(service: ProfileService) -> None:
    first, second = ident(), ident()
    service.create(first, ident(), document())
    service.create(second, ident(), document())
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE profile_revisions SET body=(SELECT body FROM profile_revisions "
            "WHERE profile_id=?) WHERE profile_id=?",
            (first, second),
        )
    with pytest.raises(ProfileError, match="protection"):
        service.get(second)
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE profile_revisions SET format=2 WHERE profile_id=?", (first,))
    before = service.store.path.read_bytes()
    with pytest.raises(ProfileError, match="newer_version"):
        service.get(first)
    with pytest.raises(ProfileError, match="newer_version"):
        service.revise(first, ident(), 1, document())
    assert service.store.path.read_bytes() == before


def test_failed_receipt_insert_rolls_back_definition_and_is_retryable(
    service: ProfileService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql.startswith("INSERT INTO profile_commands"):
                raise sqlite3.OperationalError("synthetic disk full")
            return super().execute(sql, parameters)

    profile_id, command_id = ident(), ident()
    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: real_connect(*args, factory=Failing, **kwargs),
        )
        with pytest.raises(StateError):
            service.create(profile_id, command_id, document())
    assert service.list() == []
    assert service.create(profile_id, command_id, document())["revision"] == 1


def seed_avatar(service: ProfileService, variants: tuple[int, ...] = (24, 32, 64, 128, 256)) -> str:
    avatar_id, digest, now = ident(), "0" * 64, "2026-09-27T00:00:00.000Z"
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO avatars VALUES (?,?,?,?,?,?,?)",
            (avatar_id, digest, 256, 1, digest, 6, now),
        )
        for size in variants:
            connection.execute(
                "INSERT INTO avatar_variants VALUES (?,?,?,?)", (avatar_id, size, 1, digest)
            )
    return avatar_id


def with_avatar(avatar_id: str | None) -> dict:
    value = document()
    value["identity"]["avatar_asset_id"] = avatar_id
    return value


def references(service: ProfileService) -> list[tuple[str, int, str]]:
    with service.store._connection() as connection:
        return connection.execute(
            "SELECT profile_id,profile_revision,avatar_id FROM profile_avatar_refs "
            "ORDER BY created_at,profile_id,profile_revision"
        ).fetchall()


def test_each_revision_with_an_avatar_writes_one_reference(service: ProfileService) -> None:
    first, second = seed_avatar(service), seed_avatar(service)
    profile_id, target, command_id = ident(), ident(), ident()
    created = service.create(profile_id, command_id, with_avatar(first))
    service.revise(profile_id, ident(), 1, with_avatar(second))
    service.duplicate(profile_id, ident(), 2, target, "codex")
    service.archive(profile_id, ident(), 1, True)
    assert sorted(references(service)) == sorted(
        [(profile_id, 1, first), (profile_id, 2, second), (target, 1, second)]
    )
    assert service.get(profile_id, 1)["document"]["identity"]["avatar_asset_id"] == first
    assert service.get(target)["document"]["identity"]["avatar_asset_id"] == second
    with service.store._connection(write=True) as connection:
        connection.execute("DELETE FROM avatar_variants WHERE avatar_id=?", (first,))
    # A replayed command answers from its receipt, before the avatar is looked up again.
    assert service.create(profile_id, command_id, with_avatar(first)) == created


def test_an_uncommitted_avatar_writes_no_revision_and_no_receipt(service: ProfileService) -> None:
    profile_id = ident()
    with pytest.raises(ProfileError, match="avatar_missing"):
        service.create(profile_id, ident(), with_avatar(ident()))
    assert service.list() == []
    service.create(profile_id, ident(), document())
    partial = seed_avatar(service, (24, 32, 64, 128))
    with pytest.raises(ProfileError, match="avatar_missing"):
        service.revise(profile_id, ident(), 1, with_avatar(partial))
    assert service.get(profile_id)["current_revision"] == 1
    assert references(service) == []
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM profile_commands").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM profile_revisions").fetchone()[0] == 1


def test_a_reference_that_disagrees_with_its_document_is_corrupt(service: ProfileService) -> None:
    avatar_id, other = seed_avatar(service), seed_avatar(service)
    profile_id, plain = ident(), ident()
    service.create(profile_id, ident(), with_avatar(avatar_id))
    service.create(plain, ident(), document())
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE profile_avatar_refs SET avatar_id=?", (other,))
        connection.execute(
            "INSERT INTO profile_avatar_refs VALUES (?,1,?,?)",
            (plain, other, "2026-09-27T00:00:00.000Z"),
        )
    for target in (profile_id, plain):
        with pytest.raises(ProfileError, match="corrupt"):
            service.get(target)


def test_a_revision_without_its_reference_reads_its_document(service: ProfileService) -> None:
    avatar_id, profile_id = seed_avatar(service), ident()
    service.create(profile_id, ident(), with_avatar(avatar_id))
    with service.store._connection(write=True) as connection:
        connection.execute("DELETE FROM profile_avatar_refs")
    assert service.get(profile_id)["document"] == with_avatar(avatar_id)


def test_a_failed_avatar_reference_insert_rolls_back_the_revision_and_is_retryable(
    service: ProfileService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql.startswith("INSERT INTO profile_avatar_refs"):
                raise sqlite3.OperationalError("synthetic disk full")
            return super().execute(sql, parameters)

    avatar_id = seed_avatar(service)
    profile_id, command_id = ident(), ident()
    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: real_connect(*args, factory=Failing, **kwargs),
        )
        with pytest.raises(StateError):
            service.create(profile_id, command_id, with_avatar(avatar_id))
    assert service.list() == []
    with service.store._connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM profile_commands WHERE command_id=?", (command_id,)
            ).fetchone()[0]
            == 0
        )
    assert references(service) == []
    assert service.create(profile_id, command_id, with_avatar(avatar_id))["revision"] == 1
    assert references(service) == [(profile_id, 1, avatar_id)]
