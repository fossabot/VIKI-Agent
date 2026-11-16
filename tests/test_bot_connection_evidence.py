"""Pinned, scrubbed evidence for the synthetic Bot connection capture."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "bot-connection.json"
STRINGS = {"scenario", "platform", "native_version", "model"}
BOOLEANS = {
    "argv_matches_build",
    "model_id_matches",
    "stock_sdk_cli",
    "version_checked",
    "bundled_updater_pinned",
    "private_config_roots",
    "generated_configs_private",
    "host_managed_environment",
    "host_managed_auth",
    "upstream_reached",
    "memory_in_request",
    "mcp_listed",
    "stream_success",
    "capture_pass",
    "initialize_account_metadata_present",
    "initialize_models_metadata_present",
    "app_evidence_pass",
    "no_account_catalog_evidence",
    "routed_signed_out",
    "routed_singleton_menu",
    "native_signed_in",
    "native_model",
}
COUNTS = {
    "launch_count",
    "open_count",
    "model_count",
    "settings_count",
    "mcp_count",
    "routed_account_observer_calls",
    "routed_catalog_observer_calls",
    "native_account_observer_calls",
    "native_catalog_observer_calls",
}
ALLOWED = STRINGS | BOOLEANS | COUNTS
SUCCESS = {
    "argv_matches_build",
    "model_id_matches",
    "stock_sdk_cli",
    "version_checked",
    "bundled_updater_pinned",
    "private_config_roots",
    "generated_configs_private",
    "host_managed_environment",
    "host_managed_auth",
    "upstream_reached",
    "memory_in_request",
    "mcp_listed",
    "stream_success",
    "capture_pass",
    "app_evidence_pass",
    "no_account_catalog_evidence",
    "routed_signed_out",
    "routed_singleton_menu",
    "native_signed_in",
    "native_model",
}


def _version(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def _valid(evidence: dict[str, Any]) -> bool:
    if set(evidence) != ALLOWED:
        return False
    if any(type(evidence[key]) is not str for key in STRINGS):
        return False
    if any(type(evidence[key]) is not bool for key in BOOLEANS):
        return False
    if any(type(evidence[key]) is not int for key in COUNTS):
        return False
    if (evidence["scenario"], evidence["platform"], evidence["model"]) != (
        "39",
        "linux",
        "synthetic-model",
    ):
        return False
    version = _version(evidence["native_version"])
    if version is None or version < (2, 1, 281):
        return False
    if any(not evidence[key] for key in SUCCESS):
        return False
    if any(
        evidence[key] != 1
        for key in ("launch_count", "open_count", "model_count", "settings_count", "mcp_count")
    ):
        return False
    return (
        evidence["initialize_account_metadata_present"]
        and evidence["initialize_models_metadata_present"]
        and evidence["routed_account_observer_calls"] == 0
        and evidence["routed_catalog_observer_calls"] == 0
        and evidence["native_account_observer_calls"] == 2
        and evidence["native_catalog_observer_calls"] == 1
    )


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_bot_connection_fixture_pins_successful_combined_capture() -> None:
    evidence = _fixture()

    assert _valid(evidence)
    assert evidence == {
        "scenario": "39",
        "platform": "linux",
        "native_version": "2.1.281",
        "model": "synthetic-model",
        "argv_matches_build": True,
        "model_id_matches": True,
        "stock_sdk_cli": True,
        "version_checked": True,
        "bundled_updater_pinned": True,
        "private_config_roots": True,
        "generated_configs_private": True,
        "host_managed_environment": True,
        "host_managed_auth": True,
        "upstream_reached": True,
        "memory_in_request": True,
        "mcp_listed": True,
        "stream_success": True,
        "capture_pass": True,
        "initialize_account_metadata_present": True,
        "initialize_models_metadata_present": True,
        "launch_count": 1,
        "open_count": 1,
        "model_count": 1,
        "settings_count": 1,
        "mcp_count": 1,
        "app_evidence_pass": True,
        "no_account_catalog_evidence": True,
        "routed_account_observer_calls": 0,
        "routed_catalog_observer_calls": 0,
        "routed_signed_out": True,
        "routed_singleton_menu": True,
        "native_account_observer_calls": 2,
        "native_catalog_observer_calls": 1,
        "native_signed_in": True,
        "native_model": True,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stream_success", False),
        ("argv_matches_build", False),
        ("model_count", 2),
        ("routed_catalog_observer_calls", 1),
        ("native_catalog_observer_calls", 0),
        ("initialize_models_metadata_present", False),
    ],
)
def test_false_or_inconsistent_capture_evidence_cannot_pass(key: str, value: object) -> None:
    evidence = _fixture()
    evidence[key] = value

    assert not _valid(evidence)


def test_extra_field_cannot_expand_the_fixture_schema() -> None:
    evidence = _fixture()
    evidence["account"] = {"present": True}

    assert not _valid(evidence)


@pytest.mark.parametrize("version", ["2.1.280", "2.1.80", "invalid"])
def test_old_or_unchecked_version_cannot_pass(version: str) -> None:
    evidence = _fixture()
    evidence["native_version"] = version

    assert not _valid(evidence)
