"""Closed profile v1 parsing and truthful, provider-free launch evaluation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from viki_agent.profiles.profile_document import (
    Capability,
    CapabilityCatalog,
    ProfileDocument,
    ProfileDocumentError,
    evaluate_profile,
    parse_profile_document,
)


def document() -> dict[str, Any]:
    return {
        "format_version": 1,
        "identity": {
            "name": "Review helper",
            "purpose": "Reviews a selected change",
            "avatar_asset_id": None,
            "accent": "orange",
            "shape": "rounded",
        },
        "runtime": {
            "provider": "claude",
            "model": None,
            "effort": "high",
            "permission_mode": None,
        },
        "instructions": "Review the change and cite concrete evidence.",
        "workspace": {"allowed_workspace_ids": []},
        "context": {"items": []},
        "native_assets": [],
        "tools": {"required": [], "denied": [], "mcp_configuration": None},
        "memory": {"mode": "provider_inherited", "sources": []},
        "limits": {
            "timeout_seconds": None,
            "max_root_tasks": None,
            "token_budget": None,
            "currency_budget": None,
        },
        "provenance": {"kind": "user", "derived_from": None, "native_source": None},
    }


def parsed(value: dict[str, Any] | None = None):
    return parse_profile_document(value or document())


def error(value: object) -> ProfileDocumentError:
    with pytest.raises(ProfileDocumentError) as caught:
        parse_profile_document(value)
    return caught.value


def catalog(*capabilities: Capability, models: tuple[str, ...] = ()) -> CapabilityCatalog:
    return CapabilityCatalog("claude", "viki-claude-1", "2.1.259", capabilities, models)


def test_parse_canonicalizes_and_deep_freezes_without_changing_text() -> None:
    raw = document()
    profile = parsed(raw)
    assert json.loads(profile.canonical) == raw
    assert profile.canonical == json.dumps(
        raw, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with pytest.raises(TypeError):
        profile.value["instructions"] = "changed"  # type: ignore[index]
    identity = profile.value["identity"]
    with pytest.raises(TypeError):
        identity["name"] = "changed"  # type: ignore[index]
    raw["instructions"] = "changed"
    assert profile.value["instructions"] != raw["instructions"]


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d.update(extra=True), "extra"),
        (lambda d: d["identity"].pop("purpose"), "identity.purpose"),
        (lambda d: d["identity"].update(name=""), "identity.name"),
        (lambda d: d["runtime"].update(effort="enormous"), "runtime.effort"),
        (
            lambda d: d["runtime"].update(permission_mode="bypassPermissions"),
            "runtime.permission_mode",
        ),
        (lambda d: d["limits"].update(token_budget=0), "limits.token_budget"),
        (lambda d: d["limits"].update(timeout_seconds=True), "limits.timeout_seconds"),
        (lambda d: d["tools"].update(required=["Read", "Read"]), "tools.required[1]"),
        (lambda d: d["context"].update(items=[{"path": "C:\\secret"}]), "context.items[0].path"),
        (
            lambda d: d["workspace"].update(
                allowed_workspace_ids=["AAAAAAAA-1111-4111-8111-111111111111"]
            ),
            "workspace.allowed_workspace_ids[0]",
        ),
    ],
)
def test_closed_document_rejects_invalid_values(mutate, field: str) -> None:
    value = document()
    mutate(value)
    assert error(value).field == field


def test_json_rejects_duplicates_nonfinite_surrogates_and_oversize() -> None:
    assert error('{"format_version":1,"format_version":1}').code == "duplicate_key"
    value = document()
    value["limits"]["token_budget"] = float("nan")
    assert error(value).code == "invalid_number"
    value = document()
    value["instructions"] = "\ud800"
    assert error(value).field == "instructions"
    assert error(" " * 327681).code == "document_too_large"


def test_collection_and_utf8_bounds_are_enforced() -> None:
    value = document()
    value["identity"]["name"] = "é" * 65
    assert error(value).field == "identity.name"
    value = document()
    value["native_assets"] = [{} for _ in range(33)]
    assert error(value).field == "native_assets"
    value = document()
    value["tools"]["denied"] = [f"tool-{index}" for index in range(65)]
    assert error(value).field == "tools.denied"


def test_inherited_values_need_only_effort_evidence() -> None:
    without_catalog = evaluate_profile(parsed())
    assert without_catalog.blockers == ("runtime.effort",)
    assert without_catalog.launchable is False

    observed = catalog(Capability("effort_selection", "supported"))
    evaluated = evaluate_profile(parsed(), observed)
    assert evaluated.blockers == ()
    assert evaluated.launchable is True
    assert {row.field for row in evaluated.fields} >= {
        "identity.name",
        "runtime.provider",
        "workspace.allowed_workspace_ids",
        "provenance.kind",
    }


def test_workspace_allowlist_stays_blocked_until_start_workspace_is_resolved() -> None:
    value = document()
    value["workspace"]["allowed_workspace_ids"] = ["aaaaaaaa-1111-4111-8111-111111111111"]
    observed = catalog(Capability("effort_selection", "supported"))
    evaluated = evaluate_profile(parsed(value), observed)
    assert evaluated.blockers == ("workspace.allowed_workspace_ids",)
    row = next(row for row in evaluated.fields if row.field == evaluated.blockers[0])
    assert (row.status, row.reason) == ("unverified", "workspace_resolution_required")


def test_explicit_model_requires_matching_observation() -> None:
    value = document()
    value["runtime"]["model"] = "sonnet"
    profile = parsed(value)
    effort_only = catalog(Capability("effort_selection", "supported"))
    assert evaluate_profile(profile, effort_only).blockers == ("runtime.model",)

    observed = catalog(
        Capability("model_selection", "supported"),
        Capability("effort_selection", "supported"),
        models=("sonnet",),
    )
    assert evaluate_profile(profile, observed).launchable is True


def test_observed_negative_status_and_reason_survive_evaluation() -> None:
    value = document()
    value["runtime"]["model"] = "sonnet"
    observed = catalog(
        Capability("model_selection", "unsupported", "native_missing"),
        Capability("effort_selection", "unverified", "fixture_stale"),
    )
    evaluated = evaluate_profile(parsed(value), observed)
    model = next(row for row in evaluated.fields if row.field == "runtime.model")
    effort = next(row for row in evaluated.fields if row.field == "runtime.effort")
    assert (model.status, model.reason) == ("unsupported", "native_missing")
    assert (effort.status, effort.reason) == ("unverified", "fixture_stale")


def test_plan_mode_requires_and_accepts_applicable_evidence() -> None:
    value = document()
    value["runtime"]["permission_mode"] = "plan"
    effort = Capability("effort_selection", "supported")
    assert "runtime.permission_mode" in evaluate_profile(parsed(value), catalog(effort)).blockers
    assert (
        evaluate_profile(
            parsed(value), catalog(effort, Capability("plan_mode", "supported"))
        ).launchable
        is True
    )


@pytest.mark.parametrize(
    ("section", "key", "value", "blocker"),
    [
        ("tools", "required", ["Read"], "tools.required"),
        ("tools", "denied", ["Write"], "tools.denied"),
        ("tools", "mcp_configuration", {"id": "owned"}, "tools.mcp_configuration"),
        ("memory", "mode", "selected", "memory.mode"),
        ("limits", "timeout_seconds", 30, "limits.timeout_seconds"),
        ("limits", "max_root_tasks", 2, "limits.max_root_tasks"),
        ("limits", "token_budget", 1000, "limits.token_budget"),
        ("limits", "currency_budget", 5, "limits.currency_budget"),
    ],
)
def test_unimplemented_or_invalid_restrictions_stay_blocked_despite_claimed_capability(
    section: str, key: str, value: object, blocker: str
) -> None:
    raw = document()
    raw[section][key] = value
    forged = catalog(
        Capability("effort_selection", "supported"),
        Capability("tool_filtering", "supported"),
        Capability("mcp_config", "supported"),
        Capability("memory_isolation", "supported"),
        Capability("timeout", "supported"),
        Capability("hard_budget", "supported"),
    )
    assert blocker in evaluate_profile(parsed(raw), forged).blockers


def _mcp_config() -> dict[str, Any]:
    return {
        "servers": [
            {
                "name": "echo",
                "transport": "stdio",
                "command": "python",
                "args": [],
                "url": None,
                "note": None,
            }
        ]
    }


def test_mcp_configuration_absent_is_supported_and_inherited() -> None:
    evaluated = evaluate_profile(parsed(), catalog(Capability("mcp_config", "supported")))
    row = next(row for row in evaluated.fields if row.field == "tools.mcp_configuration")
    assert (row.status, row.source) == ("supported", "inherited")


def test_mcp_configuration_supported_only_with_conforming_value_and_catalog_row() -> None:
    value = document()
    value["tools"]["mcp_configuration"] = _mcp_config()
    profile = parsed(value)

    effort = Capability("effort_selection", "supported")
    for rows in ((effort,), (effort, Capability("mcp_config", "unverified"))):
        unproven = evaluate_profile(profile, catalog(*rows))
        row = next(row for row in unproven.fields if row.field == "tools.mcp_configuration")
        assert (row.status, row.reason) == ("unsupported", "mcp_config_unproven")
        assert unproven.launchable is False

    supported = evaluate_profile(
        profile,
        catalog(
            Capability("effort_selection", "supported"),
            Capability("mcp_config", "supported"),
        ),
    )
    row = next(row for row in supported.fields if row.field == "tools.mcp_configuration")
    assert (row.status, row.reason, row.source) == ("supported", None, "profile")
    assert supported.launchable is True


def test_mcp_configuration_schema_failure_is_invalid_not_unproven() -> None:
    value = document()
    value["tools"]["mcp_configuration"] = {"id": "owned"}
    evaluated = evaluate_profile(parsed(value), catalog(Capability("mcp_config", "supported")))
    row = next(row for row in evaluated.fields if row.field == "tools.mcp_configuration")
    assert (row.status, row.reason) == ("unsupported", "mcp_config_invalid")


def test_non_claude_catalog_cannot_make_runtime_launchable() -> None:
    value = document()
    value["runtime"]["provider"] = "codex"
    codex = CapabilityCatalog(
        "codex",
        "adapter-1",
        "1.0",
        (Capability("effort_selection", "supported"),),
    )
    assert evaluate_profile(parsed(value), codex).blockers == (
        "runtime.provider",
        "runtime.effort",
    )


def test_catalog_is_bounded_immutable_and_rejects_duplicate_or_invalid_rows() -> None:
    with pytest.raises(ValueError):
        Capability("effort_selection", "maybe")
    with pytest.raises(ValueError):
        catalog(Capability("x", "supported"), Capability("x", "unsupported"))
    with pytest.raises(ValueError):
        catalog(models=("same", "same"))
    with pytest.raises(ValueError):
        Capability("x", "unsupported", "provider said this whole sentence")
    source = [Capability("effort_selection", "supported")]
    frozen = CapabilityCatalog("claude", "adapter", "native", source)
    source.clear()
    assert frozen.capabilities == (Capability("effort_selection", "supported"),)


def test_huge_integers_and_inconsistent_public_document_construction_are_refused() -> None:
    value = document()
    value["limits"]["token_budget"] = 9007199254740992
    assert error(value).field == "limits.token_budget"
    value = document()
    value["context"]["items"] = [{"revision": 10**5000}]
    assert error(value).field == "context.items[0].revision"
    valid = parsed()
    with pytest.raises(ValueError):
        ProfileDocument(valid.value, b"{}")


def test_mapping_input_is_copied_before_freezing() -> None:
    source = document()
    before = deepcopy(source)
    profile = parsed(source)
    source["identity"]["name"] = "later"
    assert json.loads(profile.canonical) == before
