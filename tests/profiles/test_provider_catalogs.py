"""Synthetic provider observations stay bounded, encrypted and binding-safe."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.profiles.test_profile_document import document
from viki_agent.profiles.profile_document import CapabilityCatalog
from viki_agent.profiles.provider_catalogs import ProviderCatalogService, evaluation_catalog
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.state import AppState, Prefs


def ident() -> str:
    return str(uuid.uuid4())


def capabilities(evidence: str = "synthetic-observation") -> list[dict[str, str | None]]:
    return [
        {
            "id": "plan_mode",
            "status": "supported",
            "reason": None,
            "evidence": evidence,
        },
        {
            "id": "tool_filtering",
            "status": "unsupported",
            "reason": "filtering_unproved",
            "evidence": None,
        },
    ]


def models(value: str = "model-a") -> list[dict[str, str]]:
    return [{"value": value, "label": f"Label {value}", "description": "Synthetic model"}]


@pytest.fixture
def service(tmp_path: Path) -> ProviderCatalogService:
    return ProviderCatalogService(AppState(tmp_path / "state.json", Prefs()).store)


def test_observation_is_content_addressed_encrypted_and_evaluable(
    service: ProviderCatalogService,
) -> None:
    observed = service.observe("claude", "adapter-1", "native-1", capabilities(), models())
    key = observed["catalog_key"]
    assert len(key) == 64
    assert service.get(key) == observed
    assert service.latest("claude", "adapter-1", "native-1") == observed
    catalog = evaluation_catalog(observed)
    assert isinstance(catalog, CapabilityCatalog)
    assert catalog.models == ("model-a",)
    assert [(row.id, row.status) for row in catalog.capabilities] == [
        ("plan_mode", "supported"),
        ("tool_filtering", "unsupported"),
    ]
    with service.store._connection() as connection:
        row = connection.execute(
            "SELECT body,body_sha256 FROM provider_catalogs WHERE catalog_key=?", (key,)
        ).fetchone()
    assert row[1] == observed["body_sha256"]
    assert b"Synthetic model" not in row[0]


def test_reobserving_old_content_makes_it_current(service: ProviderCatalogService) -> None:
    first = service.observe("claude", "adapter-1", "native-1", capabilities(), models("a"))
    second = service.observe("claude", "adapter-1", "native-1", capabilities(), models("b"))
    current = service.latest("claude", "adapter-1", "native-1")
    assert current is not None and current["catalog_key"] == second["catalog_key"]
    refreshed = service.observe("claude", "adapter-1", "native-1", capabilities(), models("a"))
    assert refreshed["catalog_key"] == first["catalog_key"]
    assert service.latest("claude", "adapter-1", "native-1") == refreshed
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM provider_catalogs").fetchone() == (2,)


@pytest.mark.parametrize(
    ("caps", "catalog_models"),
    [
        (capabilities() * 33, models()),
        (capabilities() + capabilities(), models()),
        ([{**capabilities()[0], "status": "maybe"}], models()),
        ([{**capabilities()[0], "status": []}], models()),
        (capabilities(), models() * 129),
        (capabilities(), models() + models()),
        (capabilities(), [{**models()[0], "label": "x" * 4097}]),
    ],
)
def test_invalid_or_unbounded_dtos_do_not_write(
    service: ProviderCatalogService,
    caps: object,
    catalog_models: object,
) -> None:
    with pytest.raises(ProfileError, match="invalid_catalog"):
        service.observe("claude", "adapter-1", "native-1", caps, catalog_models)
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0] == 0


def _bind(service: ProviderCatalogService, profile_id: str, key: str) -> None:
    now = "2026-09-21T00:00:00.000Z"
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_profile_bindings "
            "(session_id,command_id,request_hash,profile_id,profile_revision,workspace_id,"
            "workspace_revision,policy,policy_revision,settings_revision,runtime,capability_key,"
            "launch_spec_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident(),
                ident(),
                "0" * 64,
                profile_id,
                1,
                ident(),
                1,
                "ordinary",
                1,
                0,
                "claude",
                key,
                "1" * 64,
                now,
            ),
        )


def test_ninth_observation_evicts_only_oldest_unreferenced(
    service: ProviderCatalogService,
) -> None:
    profile_id = ident()
    ProfileService(service.store).create(profile_id, ident(), document())
    rows = [
        service.observe(
            "claude", "adapter-1", "native-1", capabilities(f"synthetic-{index}"), models()
        )
        for index in range(8)
    ]
    retained = rows[0]["catalog_key"]
    _bind(service, profile_id, retained)
    newest = service.observe(
        "claude", "adapter-1", "native-1", capabilities("synthetic-ninth"), models()
    )
    assert service.get(retained) is not None
    assert service.get(rows[1]["catalog_key"]) is None
    assert service.get(newest["catalog_key"]) == newest
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0] == 8


def test_full_bound_set_of_one_native_version_evicts_its_oldest_catalog(
    service: ProviderCatalogService,
) -> None:
    profile_id = ident()
    ProfileService(service.store).create(profile_id, ident(), document())
    rows = [
        service.observe("claude", "adapter-1", "native-1", capabilities(f"bound-{index}"), models())
        for index in range(8)
    ]
    for row in rows:
        _bind(service, profile_id, row["catalog_key"])
    newest = service.observe("claude", "adapter-1", "native-1", capabilities("ninth"), models())
    assert service.get(rows[0]["catalog_key"]) is None
    assert all(service.get(row["catalog_key"]) == row for row in rows[1:])
    assert service.get(newest["catalog_key"]) == newest
    assert service.latest("claude", "adapter-1", "native-1") == newest
    assert _binding_keys(service) == [row["catalog_key"] for row in rows]
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0] == 8


def _binding_keys(service: ProviderCatalogService) -> list[str]:
    with service.store._connection() as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT capability_key FROM session_profile_bindings ORDER BY rowid"
            )
        ]


def test_full_bound_set_evicts_oldest_catalog_of_another_native_version(
    service: ProviderCatalogService,
) -> None:
    profile_id = ident()
    ProfileService(service.store).create(profile_id, ident(), document())
    rows = [
        service.observe("claude", "adapter-1", "native-1", capabilities(f"bound-{index}"), models())
        for index in range(8)
    ]
    for row in rows:
        _bind(service, profile_id, row["catalog_key"])
    newest = service.observe("claude", "adapter-1", "native-2", capabilities("new"), models())
    assert service.get(rows[0]["catalog_key"]) is None
    assert all(service.get(row["catalog_key"]) == row for row in rows[1:])
    assert service.get(newest["catalog_key"]) == newest
    assert _binding_keys(service) == [row["catalog_key"] for row in rows]
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM provider_catalogs").fetchone()[0] == 8


def test_unbound_catalog_goes_before_a_bound_one_of_another_native_version(
    service: ProviderCatalogService,
) -> None:
    profile_id = ident()
    ProfileService(service.store).create(profile_id, ident(), document())
    rows = [
        service.observe("claude", "adapter-1", "native-1", capabilities(f"row-{index}"), models())
        for index in range(8)
    ]
    _bind(service, profile_id, rows[0]["catalog_key"])
    newest = service.observe("claude", "adapter-1", "native-2", capabilities("new"), models())
    assert service.get(rows[0]["catalog_key"]) == rows[0]
    assert service.get(rows[1]["catalog_key"]) is None
    assert service.get(newest["catalog_key"]) == newest


def test_full_bound_set_keeps_catalogs_of_the_observed_native_version(
    service: ProviderCatalogService,
) -> None:
    profile_id = ident()
    ProfileService(service.store).create(profile_id, ident(), document())
    versions = ["native-2", "native-1", *["native-2"] * 6]
    rows = [
        service.observe("claude", "adapter-1", version, capabilities(f"bound-{index}"), models())
        for index, version in enumerate(versions)
    ]
    for row in rows:
        _bind(service, profile_id, row["catalog_key"])
    newest = service.observe("claude", "adapter-1", "native-2", capabilities("new"), models())
    assert service.get(rows[1]["catalog_key"]) is None
    assert service.get(rows[0]["catalog_key"]) == rows[0]
    assert all(service.get(row["catalog_key"]) == row for row in rows[2:])
    assert service.get(newest["catalog_key"]) == newest
    assert _binding_keys(service) == [row["catalog_key"] for row in rows]


def test_tampered_ciphertext_never_returns_partial_catalog(
    service: ProviderCatalogService,
) -> None:
    observed = service.observe("claude", "adapter-1", "native-1", capabilities(), models())
    with service.store._connection(write=True) as connection:
        body = connection.execute(
            "SELECT body FROM provider_catalogs WHERE catalog_key=?", (observed["catalog_key"],)
        ).fetchone()[0]
        connection.execute(
            "UPDATE provider_catalogs SET body=? WHERE catalog_key=?",
            (body[:-1] + bytes([body[-1] ^ 1]), observed["catalog_key"]),
        )
    with pytest.raises(ProfileError, match="protection|corrupt"):
        service.get(observed["catalog_key"])
