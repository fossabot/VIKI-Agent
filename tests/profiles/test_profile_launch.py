"""Synthetic prepared sessions: atomic drafts, immutable binding, and zero eager spawns."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at
from tests.profiles.test_profile_document import document
from tests.test_server import EchoDriver
from viki_agent import protection
from viki_agent.app.settings import SettingsService
from viki_agent.conversations.drafts import DraftService
from viki_agent.domain import ProviderReadiness
from viki_agent.drivers.base import StartError, StartFacts
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.llm.connections import Connections
from viki_agent.profiles import profile_launch
from viki_agent.profiles.profile_capabilities import (
    ADAPTER_VERSION,
    FIXTURE_VERSION,
    baseline,
    evidence_version,
)
from viki_agent.profiles.profile_document import parse_profile_document
from viki_agent.profiles.profile_launch import START_CHECK_CODES, ProfileLaunchService
from viki_agent.profiles.provider_catalogs import ProviderCatalogService
from viki_agent.profiles.service import ProfileError, ProfileService, _context
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError


def ident() -> str:
    return str(uuid.uuid4())


def seed_avatar(store: Any, variants: tuple[int, ...] = (24, 32, 64, 128, 256)) -> str:
    avatar_id, digest, now = ident(), "0" * 64, "2026-09-27T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO avatars VALUES (?,?,?,?,?,?,?)",
            (avatar_id, digest, 256, 1, digest, 6, now),
        )
        for size in variants:
            connection.execute(
                "INSERT INTO avatar_variants VALUES (?,?,?,?)", (avatar_id, size, 1, digest)
            )
    return avatar_id


def with_avatar(avatar_id: str | None) -> dict[str, Any]:
    value = document()
    value["identity"]["avatar_asset_id"] = avatar_id
    return value


NEWER = "2.1.309"  # a Claude Code release newer than FIXTURE_VERSION: start-checked evidence
MENU = [{"value": "m", "label": "M", "description": ""}]


def ready(version: str) -> ProviderReadiness:
    return ProviderReadiness("ready", version, "signed_in", "clear", None, True, ())


def native_started(state: Any, sid: str) -> int:
    with state.store._connection() as connection:
        return connection.execute(
            "SELECT native_started FROM session_profile_bindings WHERE session_id=?", (sid,)
        ).fetchone()[0]


@pytest.fixture
def setup(tmp_path: Path):
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    workspaces = service_at(tmp_path, state.store)
    vault = tmp_path / "vault"
    vault.mkdir()
    workspace, _ = workspaces.register_existing(ident(), str(vault))
    workspaces.select(ident(), workspace.workspace_id)
    profiles = ProfileService(state.store)
    profile_id = ident()
    profiles.create(profile_id, ident(), document())
    settings = SettingsService(
        state.store, provider_defaults=lambda: Prefs(), on_chat_defaults=lambda _: None
    )
    readiness = ProviderReadiness("ready", FIXTURE_VERSION, "signed_in", "clear", None, True, ())
    service = ProfileLaunchService(
        profiles,
        settings,
        workspaces,
        ProviderCatalogService(state.store),
        lambda: readiness,
        vault,
        lambda _: False,
        connections=Connections(settings),
        route_can_start=lambda: True,
    )
    return service, state, vault, profile_id, workspace.workspace_id


def test_prepare_commits_exact_encrypted_draft_and_immutable_retry(setup: Any) -> None:
    service, state, vault, pid, wid = setup
    cid, sid = ident(), ident()
    prepared = service.prepare(pid, cid, sid, 1, wid)
    assert prepared["session_id"] == sid and prepared["launchable"]
    assert state.store.session_origin(sid) == "profile"
    assert state.store.get_session_metadata(service.scope, sid) == {
        "session_id": sid,
        "revision": 1,
        "title": document()["identity"]["name"],
        "pinned": 0,
        "archived": 0,
        "unread": 0,
        "color": None,
    }
    draft = DraftService(state, vault).get(sid)
    assert (draft.text, draft.revision, draft.attachments) == (document()["instructions"], 1, ())
    assert draft.text.encode() not in state.store.path.read_bytes()
    service.profiles.revise(pid, ident(), 1, {**document(), "instructions": "new revision"})
    assert service.prepare(pid, cid, sid, 1, wid) == prepared
    service.validate_launch(sid, state.prefs(sid))
    with pytest.raises(ProfileError, match="command_conflict"):
        service.prepare(pid, cid, ident(), 1, wid)


def test_blockers_and_wrong_workspace_leave_no_session(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    raw = document()
    raw["tools"]["denied"] = ["Bash"]
    service.profiles.revise(pid, ident(), 1, raw)
    sid = ident()
    result = service.prepare(pid, ident(), sid, 2, wid)
    assert result["blockers"] == ["tools.denied"] and not result["launchable"]
    assert "session_id" not in result and state.store.session_origin(sid) is None
    with pytest.raises(ProfileError, match="workspace_restart_required"):
        service.prepare(pid, ident(), sid, 2, ident())


def test_prepared_session_identity_equals_its_pinned_revision(setup: Any) -> None:
    service, _state, _vault, _pid, wid = setup
    avatar_id = seed_avatar(service.store)
    new_pid, sid = ident(), ident()
    service.profiles.create(new_pid, ident(), with_avatar(avatar_id))
    prepared = service.prepare(new_pid, ident(), sid, 1, wid)
    identity = document()["identity"]
    assert prepared["identity"] == {
        "name": identity["name"],
        "avatar_asset_id": avatar_id,
        "accent": identity["accent"],
        "shape": identity["shape"],
        "profile_revision": 1,
    }


def test_inspect_session_keeps_the_pinned_identity_after_a_rename_and_avatar_replace(
    setup: Any,
) -> None:
    service, _state, _vault, pid, wid = setup
    avatar_id = seed_avatar(service.store)
    sid = ident()
    service.profiles.revise(pid, ident(), 1, with_avatar(avatar_id))
    prepared = service.prepare(pid, ident(), sid, 2, wid)
    other_avatar = seed_avatar(service.store)
    renamed = with_avatar(other_avatar)
    renamed["identity"]["name"] = "renamed"
    service.profiles.revise(pid, ident(), 2, renamed)
    assert service.inspect_session(sid)["identity"] == prepared["identity"]


def test_inspect_session_reads_null_avatar_once_its_reference_row_is_deleted(setup: Any) -> None:
    service, _state, _vault, pid, wid = setup
    avatar_id = seed_avatar(service.store)
    sid = ident()
    service.profiles.revise(pid, ident(), 1, with_avatar(avatar_id))
    service.prepare(pid, ident(), sid, 2, wid)
    with service.store._connection(write=True) as connection:
        connection.execute("DELETE FROM profile_avatar_refs")
    assert service.inspect_session(sid)["identity"]["avatar_asset_id"] is None


def test_inspect_session_says_whether_the_native_session_started(setup: Any) -> None:
    # A missing record is a reopenable draft only while it never started: a started session
    # whose transcript is gone refuses to reopen (transcript_missing).
    service, _state, _vault, pid, wid = setup
    sid = ident()
    assert service.prepare(pid, ident(), sid, 1, wid)["started"] is False
    assert service.inspect_session(sid)["started"] is False
    service.native_initialized(sid)
    assert service.inspect_session(sid)["started"] is True


def test_empty_instruction_has_logical_empty_draft(setup: Any) -> None:
    service, state, vault, pid, wid = setup
    service.profiles.revise(pid, ident(), 1, {**document(), "instructions": ""})
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    draft = DraftService(state, vault).get(sid)
    assert draft.text == "" and draft.revision == 0


def test_failed_draft_insert_rolls_back_every_prepared_row(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    sid = ident()
    # Real SQLite trigger failure after sessions/binding/title writes exercises their rollback.
    with sqlite3.connect(state.store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_draft BEFORE INSERT ON drafts "
            "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(StateError):
        service.prepare(pid, ident(), sid, 1, wid)
    with sqlite3.connect(state.store.path) as connection:
        for table in ("sessions", "session_profile_bindings", "session_metadata"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)
            ).fetchone() == (0,)


def assert_nothing_prepared(service: Any, state: Any, sid: str) -> None:
    with state.store._connection() as connection:
        for table in ("sessions", "session_profile_bindings", "session_metadata"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)
            ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM drafts WHERE scope=? AND key=?", (service.scope, sid)
        ).fetchone() == (0,)


def test_prepare_conflict_when_profile_archives_between_catalog_read_and_write(
    setup: Any,
) -> None:
    # _catalog() runs between prepare's reads and its write transaction. A patched
    # _catalog() commits an archive there, simulating a concurrent operation racing the write.
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    read_catalog = service._catalog

    def archive_then_read() -> dict[str, Any]:
        result = read_catalog()
        service.profiles.archive(pid, ident(), 1, True)
        return result

    service._catalog = archive_then_read
    with pytest.raises(ProfileError, match="conflict"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_prepare_conflict_when_workspace_policy_changes_between_catalog_read_and_write(
    setup: Any,
) -> None:
    # Second variant: the policy changes instead of the profile.
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    read_catalog = service._catalog

    def repolicy_then_read() -> dict[str, Any]:
        result = read_catalog()
        service.workspaces.set_policy(ident(), wid, 1, "protected", [])
        return result

    service._catalog = repolicy_then_read
    with pytest.raises(ProfileError, match="conflict"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_prepare_capacity_when_binding_cap_reached(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    monkeypatch.setattr(profile_launch, "MAX_BINDINGS", 0)
    with pytest.raises(ProfileError, match="capacity"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_prepare_capacity_when_title_cap_reached(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    monkeypatch.setattr(profile_launch, "MAX_TITLES", 0)
    with pytest.raises(ProfileError, match="capacity"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_prepare_capacity_when_draft_cap_reached(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    monkeypatch.setattr(profile_launch, "MAX_DRAFT_KEYS", 0)
    with pytest.raises(ProfileError, match="capacity"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_prepare_ignores_stale_catalog_body_hash_when_key_is_unchanged(setup: Any) -> None:
    # prepare's write transaction must check that the bound catalog key still has a row,
    # not that its body_sha256 is unchanged. A concurrent re-observation of the same
    # capabilities/models bumps body_sha256 (and observed_at) while keeping the same key.
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    read_catalog = service._catalog

    def reobserve_then_read() -> dict[str, Any]:
        result = read_catalog()
        service.catalogs.observe(
            "claude", ADAPTER_VERSION, FIXTURE_VERSION, baseline(FIXTURE_VERSION), []
        )
        return result

    service._catalog = reobserve_then_read
    prepared = service.prepare(pid, cid, sid, 1, wid)
    assert prepared["session_id"] == sid and prepared["launchable"]


def test_prepare_conflict_when_catalog_row_is_evicted_between_read_and_write(setup: Any) -> None:
    # _catalog() runs between prepare's reads and its write transaction. A patched _catalog()
    # deletes the returned key's row there, simulating an eviction (the 8-row bound) racing the
    # write. Without the "still has a row" check, the binding would be written with a dangling
    # capability_key: prepare "succeeds" but its review and every start then answer
    # capability_changed, so the session never starts.
    service, state, _vault, pid, wid = setup
    sid, cid = ident(), ident()
    read_catalog = service._catalog

    def evict_then_read() -> dict[str, Any]:
        result = read_catalog()
        with sqlite3.connect(state.store.path) as connection:
            connection.execute(
                "DELETE FROM provider_catalogs WHERE catalog_key=?", (result["catalog_key"],)
            )
        return result

    service._catalog = evict_then_read
    with pytest.raises(ProfileError, match="conflict"):
        service.prepare(pid, cid, sid, 1, wid)
    assert_nothing_prepared(service, state, sid)


def test_a_session_whose_catalog_is_gone_reviews_as_capability_changed(setup: Any) -> None:
    # A full catalog store may evict a bound catalog of another native version. The session's
    # review then answers what its start answers, capability_changed, rather than corrupt.
    service, state, _vault, pid, wid = setup
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 1, wid)
    with sqlite3.connect(state.store.path) as connection:
        connection.execute(
            "DELETE FROM provider_catalogs WHERE catalog_key=?", (prepared["capability_key"],)
        )
    review = service.inspect_session(sid)
    assert review["launchable"] is False and review["fields"] == []
    assert review["blockers"] == ["capability_changed"]
    assert review["warnings"] == ["capability_changed"]
    assert review["identity"] == prepared["identity"]
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(sid, state.prefs(sid))


def test_drift_and_missing_binding_refuse_launch(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    original = state.prefs(sid)
    service.validate_launch(sid, original)
    with pytest.raises(ProfileError, match="launch_changed"):
        service.validate_launch(sid, replace(original, effort="low"))
    service.readiness = lambda: ProviderReadiness(
        "ready", "9.9.9", "signed_in", "clear", None, True, ()
    )
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(sid, original)
    # A binding made under the proven version also answers capability_changed once readiness
    # reports the superseded 2.1.259 (it fails closed).
    service.readiness = lambda: ProviderReadiness(
        "ready", "2.1.259", "signed_in", "clear", None, True, ()
    )
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(sid, original)
    with sqlite3.connect(state.store.path) as connection:
        connection.execute("DELETE FROM session_profile_bindings WHERE session_id=?", (sid,))
    with pytest.raises(ProfileError, match="corrupt"):
        service.validate_launch(sid, original)


async def test_prepared_actor_requires_explicit_send_guard_before_driver(setup: Any) -> None:
    service, state, vault, pid, wid = setup
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    built: list[str] = []
    checked: list[str] = []

    def factory(launch: Any, prefs: Prefs, emit: Any):
        built.append(launch.session_id)
        raise StartError("internal", "synthetic stop before process")

    async def guard(session_id: str, prefs: Prefs) -> None:
        service.validate_launch(session_id, prefs)
        checked.append(session_id)

    async def observed(_: Any) -> None:
        raise AssertionError("no initialize occurred")

    manager = SessionManager(
        config=DriverConfig(vault=vault, cli=(), env={}),
        state=state,
        claude_home=vault / "synthetic-provider",
        make_driver=factory,
    )

    async def initialized(session_id: str, facts: StartFacts | None = None) -> None:
        service.native_initialized(session_id, facts)

    manager.bind_profiles(guard, observed, initialized=initialized)
    actor = manager.attach_prepared(sid)
    assert not await actor.start() and not built and not checked
    assert not await actor.send("visible edited text")
    assert checked == [sid] and built == [sid]
    # A reload attaches the same durable origin without a native start.
    await manager.close(sid)
    reopened = manager.attach_prepared(sid)
    assert not await reopened.start() and built == [sid]


@pytest.mark.parametrize("failure", ["storage", "cancelled"])
async def test_failed_initialized_checkpoint_retires_without_send(setup: Any, failure: str) -> None:
    service, state, vault, pid, wid = setup
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    drivers: list[EchoDriver] = []
    events: list[dict[str, Any]] = []
    sends: list[str] = []

    def factory(launch: Any, prefs: Prefs, emit: Any) -> EchoDriver:
        driver = EchoDriver(launch, emit)

        async def sent(text: str) -> None:
            sends.append(text)

        driver.send = sent
        drivers.append(driver)
        return driver

    async def guard(session_id: str, prefs: Prefs) -> None:
        service.validate_launch(session_id, prefs)

    async def observed(_: Any) -> None:
        pass

    async def initialized(_: str, facts: StartFacts | None = None) -> None:
        if failure == "cancelled":
            raise asyncio.CancelledError
        raise StateError("unavailable")

    manager = SessionManager(
        config=DriverConfig(cli=(), vault=vault, env={}),
        state=state,
        claude_home=vault / "synthetic-provider",
        make_driver=factory,
    )
    manager.bind_profiles(guard, observed, initialized=initialized)
    actor = manager.attach_prepared(sid)
    actor.subscribe(events.append)
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await actor.send("must remain local")
    else:
        assert not await actor.send("must remain local")
    assert len(drivers) == 1 and not drivers[0].alive and not sends
    assert not any(event["type"] == "session.started" for event in events)
    with state.store._connection() as connection:
        assert connection.execute(
            "SELECT native_started FROM session_profile_bindings WHERE session_id=?", (sid,)
        ).fetchone() == (0,)


def test_transcript_collision_and_reopen_marker_are_fail_closed(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    sid = ident()
    service.transcript_exists = lambda _: True
    with pytest.raises(ProfileError, match="session_exists"):
        service.prepare(pid, ident(), sid, 1, wid)
    assert state.store.session_origin(sid) is None
    service.transcript_exists = lambda _: False
    service.prepare(pid, ident(), sid, 1, wid)
    service.reopenable(sid, False)
    with pytest.raises(ProfileError, match="session_exists"):
        service.reopenable(sid, True)
    service.transcript_exists = lambda _: True
    with pytest.raises(ProfileError, match="session_exists"):
        service.validate_launch(sid, state.prefs(sid))
    service.native_initialized(sid)
    service.native_initialized(sid)
    service.reopenable(sid, True)
    with pytest.raises(ProfileError, match="transcript_missing"):
        service.reopenable(sid, False)


def _server(note: str | None = None) -> dict[str, Any]:
    return {
        "name": "echo",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "echo_server"],
        "url": None,
        "note": note,
    }


def with_servers(*servers: dict[str, Any]) -> dict[str, Any]:
    value = document()
    value["tools"]["mcp_configuration"] = {"servers": list(servers)}
    return value


@pytest.fixture
def mcp_setup(setup: Any, tmp_path: Path):
    """A profile with one app server, the capability proven, and an ordinary workspace."""
    service, state, vault, pid, wid = setup
    service.profiles.revise(pid, ident(), 1, with_servers(_server()))
    service.workspaces.set_policy(ident(), wid, 1, "ordinary", [])
    service.bind_mcp_directory(tmp_path / "data" / "mcp")
    return service, state, vault, pid, wid


def mcp_row(result: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in result["fields"] if row["field"] == "tools.mcp_configuration")


@pytest.mark.parametrize("policy", ["protected", "existing_readonly"])
def test_app_servers_are_blocked_outside_an_ordinary_workspace(setup: Any, policy: str) -> None:
    service, state, _vault, pid, wid = setup
    service.profiles.revise(pid, ident(), 1, with_servers(_server()))
    if policy == "protected":
        service.workspaces.set_policy(ident(), wid, 1, "protected", [])
    sid = ident()
    result = service.prepare(pid, ident(), sid, 2, wid)
    assert (mcp_row(result)["status"], mcp_row(result)["reason"]) == (
        "unsupported",
        "mcp_policy_blocked",
    )
    assert result["blockers"] == ["tools.mcp_configuration"] and not result["launchable"]
    assert state.store.session_origin(sid) is None


def test_ordinary_workspace_launches_and_the_preview_names_the_written_file(
    mcp_setup: Any,
) -> None:
    service, state, _vault, pid, wid = mcp_setup
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 2, wid)
    assert prepared["launchable"] and mcp_row(prepared)["status"] == "supported"
    preview = prepared["mcp_config"]
    assert not service.mcp_directory.exists()  # the preview writes nothing
    service.validate_launch(sid, state.prefs(sid))
    path = service.mcp_config_path(sid)
    assert path is not None and Path(path).is_absolute()
    assert Path(path).read_bytes() == preview["json"].encode("ascii")
    assert preview["argv_element"] == f"--mcp-config={path}"
    assert '"viki_echo"' in preview["json"] and "note" not in preview["json"]
    assert service.mcp_config_path(sid) == path  # every spawn resolves again; one file


def test_app_servers_are_unproven_on_an_unproven_cli_version(mcp_setup: Any) -> None:
    # _catalog() observes baseline("2.1.259"), whose mcp_config row is unverified.
    service, state, _vault, pid, wid = mcp_setup
    service.readiness = lambda: ProviderReadiness(
        "ready", "2.1.259", "signed_in", "clear", None, True, ()
    )
    sid = ident()
    result = service.prepare(pid, ident(), sid, 2, wid)
    assert (mcp_row(result)["status"], mcp_row(result)["reason"]) == (
        "unsupported",
        "mcp_config_unproven",
    )
    assert "tools.mcp_configuration" in result["blockers"] and not result["launchable"]
    assert state.store.session_origin(sid) is None


def catalog_rows(state: Any) -> list[tuple[Any, ...]]:
    with state.store._connection() as connection:
        return connection.execute(
            "SELECT rowid,catalog_key,observed_at FROM provider_catalogs ORDER BY rowid"
        ).fetchall()


@pytest.mark.parametrize("models", [[], [{"value": "m", "label": "M", "description": ""}]])
def test_a_catalog_holding_this_builds_rows_is_never_re_observed(
    setup: Any, models: list[dict[str, str]]
) -> None:
    # The rows an ordinary start records (server.py's observer) compare equal to _catalog()'s.
    service, state, _vault, pid, wid = setup
    stored = service.catalogs.observe(
        "claude",
        ADAPTER_VERSION,
        FIXTURE_VERSION,
        baseline(FIXTURE_VERSION, models_observed=bool(models)),
        models,
    )
    before = catalog_rows(state)
    assert service._catalog()["catalog_key"] == service._catalog()["catalog_key"]
    sid = ident()
    assert service.prepare(pid, ident(), sid, 1, wid)["capability_key"] == stored["catalog_key"]
    service.inspect_session(sid)
    service.validate_launch(sid, state.prefs(sid))
    assert catalog_rows(state) == before and len(before) == 1


def old_rows(version: str, *, models_observed: bool = False) -> list[dict[str, Any]]:
    """The rows the build before the mcp_config flip stored: that row unverified."""
    return [
        {**row, "status": "unverified", "reason": "evidence_unavailable", "evidence": None}
        if row["id"] == "mcp_config"
        else row
        for row in baseline(version, models_observed=models_observed)
    ]


def test_a_catalog_an_earlier_build_stored_is_re_observed_with_this_builds_rows(
    mcp_setup: Any,
) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    models = [{"value": "m", "label": "M", "description": ""}]
    rows = old_rows(FIXTURE_VERSION, models_observed=True)
    stale = service.catalogs.observe("claude", ADAPTER_VERSION, FIXTURE_VERSION, rows, models)
    prepared = service.prepare(pid, ident(), ident(), 2, wid)
    assert prepared["launchable"] and mcp_row(prepared)["status"] == "supported"
    latest = service.catalogs.latest("claude", ADAPTER_VERSION, FIXTURE_VERSION)
    assert latest is not None and latest["catalog_key"] == prepared["capability_key"]
    assert latest["catalog_key"] != stale["catalog_key"] and latest["models"] == models


def test_a_full_store_of_bound_catalogs_evicts_the_oldest_and_prepares(
    mcp_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An earlier build bound all eight retained catalogs under FIXTURE_VERSION's one label.
    # With no unbound catalog and no other label to evict first, observe() makes room by evicting
    # the oldest bound catalog of this label instead of refusing with capacity, and that
    # catalog's session answers capability_changed at its next start.
    service, state, _vault, pid, wid = mcp_setup
    plain = ident()
    service.profiles.create(plain, ident(), document())
    oldest_sid = ""
    with monkeypatch.context() as patch:
        patch.setattr(profile_launch, "baseline", old_rows)
        for index in range(8):
            models = [{"value": f"m{index}", "label": f"M{index}", "description": ""}]
            rows = old_rows(FIXTURE_VERSION, models_observed=True)
            service.catalogs.observe("claude", ADAPTER_VERSION, FIXTURE_VERSION, rows, models)
            sid = ident()
            assert service.prepare(plain, ident(), sid, 1, wid)["launchable"]
            if index == 0:
                oldest_sid = sid
    before = catalog_rows(state)
    assert len(before) == 8
    with state.store._connection() as connection:
        oldest_key = connection.execute(
            "SELECT capability_key FROM session_profile_bindings WHERE session_id=?",
            (oldest_sid,),
        ).fetchone()[0]
    assert oldest_key in [row[1] for row in before]
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 2, wid)
    assert prepared["launchable"]
    after_keys = [row[1] for row in catalog_rows(state)]
    assert len(after_keys) == 8
    assert oldest_key not in after_keys and prepared["capability_key"] in after_keys
    new_catalog = service.catalogs.get(prepared["capability_key"])
    assert new_catalog is not None
    assert new_catalog["capabilities"] == baseline(FIXTURE_VERSION, models_observed=True)
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(oldest_sid, state.prefs(oldest_sid))


@pytest.mark.parametrize(
    ("stored", "code"), [(True, "protection"), (True, "corrupt"), (False, "capacity")]
)
def test_other_observation_failures_still_refuse(setup: Any, stored: bool, code: str) -> None:
    service, _state, _vault, _pid, _wid = setup
    if stored:
        service.catalogs.observe(
            "claude", ADAPTER_VERSION, FIXTURE_VERSION, old_rows(FIXTURE_VERSION), []
        )

    def refused(*_: Any) -> dict[str, Any]:
        raise ProfileError(code)

    service.catalogs.observe = refused
    with pytest.raises(ProfileError, match=code):
        service._catalog()


@pytest.mark.parametrize("change", ["removed", "note_changed", "archived"])
def test_a_server_no_longer_in_the_current_revision_is_revoked(mcp_setup: Any, change: str) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    if change == "removed":
        service.profiles.revise(pid, ident(), 2, document())
    elif change == "note_changed":
        service.profiles.revise(pid, ident(), 2, with_servers(_server("edited")))
    else:
        state_revision = service.profiles.get(pid)["state_revision"]
        service.profiles.archive(pid, ident(), state_revision, True)
    with pytest.raises(ProfileError, match="mcp_revoked"):
        service.mcp_config_path(sid)
    assert not service.mcp_directory.exists()


def test_dropping_one_of_two_pinned_servers_revokes_the_session(mcp_setup: Any) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    other = {**_server(), "name": "other"}
    service.profiles.revise(pid, ident(), 2, with_servers(_server(), other))
    sid = ident()
    assert service.prepare(pid, ident(), sid, 3, wid)["launchable"]
    service.profiles.revise(pid, ident(), 3, with_servers(_server()))
    with pytest.raises(ProfileError, match="mcp_revoked"):
        service.mcp_config_path(sid)
    assert not service.mcp_directory.exists()


def test_adding_a_server_keeps_the_pinned_one(mcp_setup: Any) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    other = {**_server(), "name": "other"}
    service.profiles.revise(pid, ident(), 2, with_servers(_server(), other))
    assert service.inspect_session(sid)["warnings"] == []
    path = service.mcp_config_path(sid)
    assert path is not None and Path(path).is_file()


def test_another_blocker_never_hides_a_revoked_server(mcp_setup: Any) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 2, wid)
    # Claude Code moves to a version outside the binding's evidence class.
    service.readiness = lambda: ready("2.1.259")
    blocked = service.inspect_session(sid)
    assert blocked["warnings"] == ["capability_changed"] and not blocked["launchable"]
    assert blocked["mcp_config"] == prepared["mcp_config"] is not None
    service.profiles.revise(pid, ident(), 2, document())
    review = service.inspect_session(sid)
    assert review["warnings"] == ["capability_changed", "mcp_revoked"]
    assert review["blockers"] == ["capability_changed", "mcp_revoked"]
    assert review["mcp_config"] is None and not review["launchable"]


def test_a_code_two_checks_raise_is_listed_once(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client refuses a review whose blockers repeat a code.
    service, _state, _vault, pid, wid = setup
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    service.readiness = lambda: ready("2.1.259")

    def refused(*_: Any) -> None:
        raise ProfileError("capability_changed")

    monkeypatch.setattr(service, "_require_unrevoked", refused)
    review = service.inspect_session(sid)
    assert review["warnings"] == ["capability_changed"]
    assert review["blockers"] == ["capability_changed"] and not review["launchable"]


def test_the_review_of_a_revoked_server_is_blocked_and_previews_nothing(mcp_setup: Any) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    service.profiles.revise(pid, ident(), 2, document())
    review = service.inspect_session(sid)
    assert not review["launchable"] and review["warnings"] == ["mcp_revoked"]
    assert review["blockers"] == ["mcp_revoked"] and review["mcp_config"] is None
    assert not service.mcp_directory.exists()


def test_an_unrelated_revision_keeps_the_pinned_server(mcp_setup: Any) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 2, wid)
    service.profiles.revise(
        pid, ident(), 2, {**with_servers(_server()), "instructions": "a later edit"}
    )
    review = service.inspect_session(sid)
    assert prepared["mcp_config"] is not None
    assert review["launchable"] and review["mcp_config"] == prepared["mcp_config"]
    assert service.mcp_config_path(sid) is not None


def test_a_failed_write_or_an_unbound_folder_is_unavailable(
    mcp_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _state, _vault, pid, wid = mcp_setup
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)

    def refused(directory: Path, data: bytes) -> Path:
        raise OSError(13, "synthetic")

    monkeypatch.setattr(profile_launch, "write_config", refused)
    with pytest.raises(ProfileError, match="mcp_config_unavailable"):
        service.mcp_config_path(sid)
    service.mcp_directory = None
    with pytest.raises(ProfileError, match="mcp_config_unavailable"):
        service.mcp_config_path(sid)
    assert service.inspect_session(sid)["mcp_config"] is None


def test_a_profile_without_servers_resolves_to_none_and_previews_null(
    setup: Any, tmp_path: Path
) -> None:
    service, _state, _vault, pid, wid = setup
    service.bind_mcp_directory(tmp_path / "data" / "mcp")
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 1, wid)
    assert prepared["mcp_config"] is None
    assert service.mcp_config_path(sid) is None
    assert not service.mcp_directory.exists()


def test_a_pinned_value_that_no_longer_conforms_previews_null(mcp_setup: Any) -> None:
    # Only a later schema tightening leaves such a pinned revision: rewrite the stored one.
    service, state, _vault, pid, wid = mcp_setup
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    plain = parse_profile_document(with_servers({**_server(), "name": "Echo"})).canonical
    body = protection.protect(plain, context=_context(pid, 2))
    with state.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE profile_revisions SET body=?,body_bytes=?,body_sha256=? "
            "WHERE profile_id=? AND revision=2",
            (body, len(plain), hashlib.sha256(plain).hexdigest(), pid),
        )
    review = service.inspect_session(sid)
    assert mcp_row(review)["reason"] == "mcp_config_invalid" and not review["launchable"]
    assert review["mcp_config"] is None


@pytest.mark.parametrize("version", [FIXTURE_VERSION, NEWER])
def test_a_start_whose_bound_catalog_is_gone_is_refused(setup: Any, version: str) -> None:
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready(version)
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 1, wid)
    with sqlite3.connect(state.store.path) as connection:
        connection.execute(
            "DELETE FROM provider_catalogs WHERE catalog_key=?", (prepared["capability_key"],)
        )
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid)
    assert caught.value.code == "capability_changed" and native_started(state, sid) == 0


def test_a_minimal_newer_2x_profile_needs_a_start_report(setup: Any) -> None:
    # Its effort row rests on the start check, so even a profile pinning nothing more needs one.
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready(NEWER)
    sid = ident()
    assert service.prepare(pid, ident(), sid, 1, wid)["launchable"]
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid)
    assert caught.value.code == "start_unconfirmed" and native_started(state, sid) == 0
    service.native_initialized(sid, StartFacts(None, "default", (), None))
    assert native_started(state, sid) == 1


def observe_menu(service: Any, version: str) -> dict[str, Any]:
    """What an ordinary chat's start at `version` records: its model menu under its label."""
    return service.catalogs.observe(
        "claude",
        ADAPTER_VERSION,
        evidence_version(version),
        baseline(version, models_observed=True),
        MENU,
    )


@pytest.fixture
def newer_setup(mcp_setup: Any):
    """A plan profile with a pinned model and one app server, prepared on NEWER with its menu."""
    service, state, _vault, pid, wid = mcp_setup
    service.readiness = lambda: ready(NEWER)
    observe_menu(service, NEWER)
    raw = with_servers(_server())
    raw["runtime"].update(permission_mode="plan", model="m")
    service.profiles.revise(pid, ident(), 2, raw)
    sid = ident()
    return service, state, sid, service.prepare(pid, ident(), sid, 3, wid)


def facts(**changes: Any) -> StartFacts:
    """A start report confirming newer_setup's profile; `changes` replace its fields."""
    return replace(StartFacts(None, "plan", ("m", "other"), ("viki_echo",)), **changes)


def test_a_minimal_profile_prepares_launchable_on_a_newer_2x(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready(NEWER)
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 1, wid)
    assert prepared["launchable"] and prepared["blockers"] == []
    catalog = service.catalogs.get(prepared["capability_key"])
    assert catalog is not None and catalog["native_version"] == "2.x"
    service.validate_launch(sid, state.prefs(sid))


def test_a_plan_model_and_app_server_profile_prepares_launchable_on_a_newer_2x(
    newer_setup: Any,
) -> None:
    service, state, sid, prepared = newer_setup
    assert prepared["launchable"] and prepared["blockers"] == []
    rows = {row["field"]: (row["status"], row["source"]) for row in prepared["fields"]}
    for field in (
        "runtime.model",
        "runtime.effort",
        "runtime.permission_mode",
        "tools.mcp_configuration",
    ):
        assert rows[field] == ("supported", "profile")
    service.validate_launch(sid, state.prefs(sid))


def test_a_start_that_confirms_the_profile_marks_it_started(newer_setup: Any) -> None:
    service, state, sid, _prepared = newer_setup
    service.native_initialized(sid, facts())
    assert native_started(state, sid) == 1
    # A respawn on a later 2.x release reports the same evidence class.
    service.native_initialized(sid, facts(version="2.1.312"))
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid, facts(mode="default"))
    # A refused respawn leaves the session started.
    assert caught.value.code == "plan_mode_unconfirmed" and native_started(state, sid) == 1


@pytest.mark.parametrize(
    ("reported", "code"),
    [
        (None, "start_unconfirmed"),
        (facts(mode="default"), "plan_mode_unconfirmed"),
        (facts(mode=None), "plan_mode_unconfirmed"),
        (facts(models=("other",)), "model_unconfirmed"),
        (facts(models=()), "model_unconfirmed"),
        (facts(mcp_servers=None), "mcp_config_unconfirmed"),
        (facts(mcp_servers=("echo", "viki_other")), "mcp_config_unconfirmed"),
        (facts(version=FIXTURE_VERSION), "capability_changed"),
        (facts(version="3.0.0"), "capability_changed"),
    ],
)
def test_a_start_that_does_not_confirm_the_profile_is_refused(
    newer_setup: Any, reported: StartFacts | None, code: str
) -> None:
    service, state, sid, _prepared = newer_setup
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid, reported)
    assert caught.value.code == code and native_started(state, sid) == 0
    assert (code in START_CHECK_CODES) == (code != "capability_changed")


def test_an_inherited_plan_mode_must_be_confirmed_too(setup: Any) -> None:
    # The bound mode is the session's, so a plan mode the settings supply is checked as well.
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready(NEWER)
    service.settings.patch(
        ident(), service.settings.read().document.revision, {"chat.default_mode": "plan"}
    )
    sid = ident()
    assert service.prepare(pid, ident(), sid, 1, wid)["launchable"]
    assert state.prefs(sid).mode == "plan"
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid, StartFacts(None, "default", (), None))
    assert caught.value.code == "plan_mode_unconfirmed" and native_started(state, sid) == 0


def test_two_pinned_app_servers_both_need_reporting(mcp_setup: Any) -> None:
    # _require_confirmed's app-server rule is a subset check: an overlap check would accept a
    # report naming just one of two pinned servers.
    service, state, _vault, pid, wid = mcp_setup
    other = {**_server(), "name": "other"}
    service.readiness = lambda: ready(NEWER)
    service.profiles.revise(pid, ident(), 2, with_servers(_server(), other))
    sid = ident()
    assert service.prepare(pid, ident(), sid, 3, wid)["launchable"]
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid, facts(mcp_servers=("viki_echo",)))
    assert caught.value.code == "mcp_config_unconfirmed" and native_started(state, sid) == 0


def test_a_static_binding_needs_no_report_but_never_moves_class(setup: Any) -> None:
    # 2.1.281's rows rest on a recorded capture: its start passes without a report.
    service, state, _vault, pid, wid = setup
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    with pytest.raises(ProfileError) as caught:
        service.native_initialized(sid, StartFacts(NEWER, "default", (), None))
    assert caught.value.code == "capability_changed" and native_started(state, sid) == 0
    service.native_initialized(sid)
    assert native_started(state, sid) == 1
    service.native_initialized(sid, StartFacts(FIXTURE_VERSION, "default", (), None))
    service.readiness = lambda: ready(NEWER)
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(sid, state.prefs(sid))


def test_a_static_binding_also_needs_its_pinned_model_confirmed_when_reported(setup: Any) -> None:
    # baseline()'s model_selection row only needs a non-empty menu, not the pinned model's
    # presence in it, and a static binding outlives a menu change too, so the start checks
    # the pinned model against the menu Claude Code reports.
    service, state, _vault, pid, wid = setup
    observe_menu(service, FIXTURE_VERSION)  # MENU's one value is "m"
    raw = document()
    raw["runtime"]["model"] = "m"
    service.profiles.revise(pid, ident(), 1, raw)
    sid = ident()
    assert service.prepare(pid, ident(), sid, 2, wid)["launchable"]
    for menu in ((), ("other",)):
        with pytest.raises(ProfileError) as caught:
            service.native_initialized(sid, StartFacts(None, "default", menu, None))
        assert caught.value.code == "model_unconfirmed" and native_started(state, sid) == 0
    service.native_initialized(sid, StartFacts(None, "default", ("m", "other"), None))
    assert native_started(state, sid) == 1


def test_a_static_binding_with_no_facts_still_needs_no_model_report(setup: Any) -> None:
    # The static binding with facts=None still passes.
    service, state, _vault, pid, wid = setup
    observe_menu(service, FIXTURE_VERSION)
    raw = document()
    raw["runtime"]["model"] = "m"
    service.profiles.revise(pid, ident(), 1, raw)
    sid = ident()
    service.prepare(pid, ident(), sid, 2, wid)
    service.native_initialized(sid)
    assert native_started(state, sid) == 1


def test_a_2x_binding_holds_across_updates_and_menu_changes(setup: Any) -> None:
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready("2.1.300")
    sid = ident()
    assert service.prepare(pid, ident(), sid, 1, wid)["launchable"]
    prefs = state.prefs(sid)
    service.readiness = lambda: ready(NEWER)
    service.validate_launch(sid, prefs)
    observe_menu(service, NEWER)  # an ordinary chat records a new model menu
    service.validate_launch(sid, prefs)
    assert service.inspect_session(sid)["warnings"] == []
    for version in (FIXTURE_VERSION, "2.1.259", "3.0.0"):
        service.readiness = lambda version=version: ready(version)
        with pytest.raises(ProfileError, match="capability_changed"):
            service.validate_launch(sid, prefs)


def test_a_newer_2x_reuses_the_menu_an_earlier_2x_observed(setup: Any) -> None:
    service, state, _vault, _pid, _wid = setup
    observed = observe_menu(service, "2.1.300")
    service.readiness = lambda: ready(NEWER)
    catalog = service._catalog()
    assert catalog["catalog_key"] == observed["catalog_key"] and catalog["native_version"] == "2.x"
    assert catalog["models"] == MENU and len(catalog_rows(state)) == 1


def test_a_binding_reviewed_with_other_rows_is_refused(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same evidence label, but an earlier build's rows: not what this build would review.
    service, state, _vault, pid, wid = setup
    sid = ident()
    with monkeypatch.context() as patch:
        patch.setattr(profile_launch, "baseline", old_rows)
        assert service.prepare(pid, ident(), sid, 1, wid)["launchable"]
    with pytest.raises(ProfileError, match="capability_changed"):
        service.validate_launch(sid, state.prefs(sid))


def old_steer_rows(version: str, *, models_observed: bool = False) -> list[dict[str, Any]]:
    """The rows the build before the steer flip stored: that row adapter_unsupported."""
    return [
        {**row, "status": "unsupported", "reason": "adapter_unsupported", "evidence": None}
        if row["id"] == "steer"
        else row
        for row in baseline(version, models_observed=models_observed)
    ]


@pytest.mark.parametrize("version", [FIXTURE_VERSION, NEWER])
def test_a_binding_stored_with_the_old_steer_row_still_validates(
    setup: Any, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    # No profile field reads the steer row, so a chat prepared before the flip keeps starting;
    # any other row change still answers capability_changed (the test above).
    service, state, _vault, pid, wid = setup
    service.readiness = lambda: ready(version)
    sid = ident()
    with monkeypatch.context() as patch:
        patch.setattr(profile_launch, "baseline", old_steer_rows)
        assert service.prepare(pid, ident(), sid, 1, wid)["launchable"]
    service.validate_launch(sid, state.prefs(sid))
    review = service.inspect_session(sid)
    assert review["warnings"] == [] and review["launchable"]
    stored = service.catalogs.get(review["capability_key"])
    assert stored is not None and stored["capabilities"] == old_steer_rows(version)
