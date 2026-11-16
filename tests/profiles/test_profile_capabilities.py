"""Capability evidence baseline: what the proven CLI version supports, and what does not."""

from __future__ import annotations

import claude_agent_sdk
import pytest

from viki_agent.profiles.profile_capabilities import (
    FIXTURE_VERSION,
    baseline,
    evidence_version,
    steer_supported,
)

_LABEL = f"claude-{FIXTURE_VERSION}-compatibility"
_PROVEN = {
    "resume",
    "plan_mode",
    "usage_detail",
    "structured_approvals",
    "effort_selection",
    "image_input",
    "mcp_config",
    "steer",
}
_START_LABEL = "claude-start-check"
_START_CHECKED = {"effort_selection", "plan_mode", "mcp_config", "model_selection"}
_LONGEST = "2.1." + "9" * 60  # 64 characters, the longest version a start check accepts
_ADAPTER_UNSUPPORTED = {
    "fork_at_turn",
    "worktree_execution",
    "tool_filtering",
    "memory_isolation",
    "read_scope",
    "write_scope",
    "egress_scope",
    "timeout",
    "hard_budget",
}


def _by_id(version: str, *, models_observed: bool = False) -> dict[str, dict[str, object]]:
    return {row["id"]: row for row in baseline(version, models_observed=models_observed)}


def test_proven_version_supports_exactly_the_evidenced_capabilities() -> None:
    rows = _by_id(FIXTURE_VERSION)
    supported = {name for name, row in rows.items() if row["status"] == "supported"}
    assert supported == _PROVEN
    for name in _PROVEN:
        assert rows[name]["reason"] is None
        assert rows[name]["evidence"] == _LABEL


def test_proven_version_with_models_observed_adds_model_selection() -> None:
    rows = _by_id(FIXTURE_VERSION, models_observed=True)
    supported = {name for name, row in rows.items() if row["status"] == "supported"}
    assert supported == _PROVEN | {"model_selection"}
    assert rows["model_selection"]["evidence"] == _LABEL


@pytest.mark.parametrize("version", ["2.1.282", "2.1.309", "2.2.0"])
def test_a_newer_2x_version_supports_exactly_the_start_checked_rows(version: str) -> None:
    # Claude Code itself confirms these settings as each chat starts (the launch's start check),
    # so no recorded capture of the release is needed; every other row reads as unproven.
    unproven = _by_id("2.1.259", models_observed=True)
    for models_observed, expected in (
        (True, _START_CHECKED),
        (False, _START_CHECKED - {"model_selection"}),
    ):
        rows = _by_id(version, models_observed=models_observed)
        assert {name for name, row in rows.items() if row["status"] == "supported"} == expected
        for name, row in rows.items():
            assert row == (
                {"id": name, "status": "supported", "reason": None, "evidence": _START_LABEL}
                if name in expected
                else unproven[name]
            )
        # One row set for the whole class, so its catalog and binding survive an update.
        assert baseline(version, models_observed=models_observed) == baseline(
            "2.1.309", models_observed=models_observed
        )


@pytest.mark.parametrize(
    "version",
    [
        "2.1.259",
        "3.0.0",
        "1.9.999",
        "",
        "2.1.x",
        "2.1.281-beta",
        _LONGEST + "9",
        "2.1.²",  # isdigit() accepts it, int() does not
        "２.１.３００",  # full-width digits
    ],
)
def test_other_versions_support_nothing_and_keep_their_own_label(version: str) -> None:
    rows = _by_id(version, models_observed=True)
    assert not any(row["status"] == "supported" for row in rows.values())
    assert evidence_version(version) == version


def test_every_newer_2x_version_shares_one_evidence_label() -> None:
    for version in ("2.1.282", "2.1.309", "2.2.0", _LONGEST):
        assert evidence_version(version) == "2.x"
    assert evidence_version(FIXTURE_VERSION) == FIXTURE_VERSION


@pytest.mark.parametrize("name", ["image_input", "mcp_config"])
def test_capability_is_supported_on_the_proven_version(name: str) -> None:
    # mcp_config's evidence: the recorded --mcp-config probe on 2.1.281.
    # A newer 2.x proves mcp_config by the start check alone (the start-checked rows above).
    assert _by_id(FIXTURE_VERSION)[name] == {
        "id": name,
        "status": "supported",
        "reason": None,
        "evidence": _LABEL,
    }
    assert _by_id("2.1.259")[name] == {
        "id": name,
        "status": "unverified",
        "reason": "evidence_unavailable",
        "evidence": None,
    }


def test_image_input_stays_unverified_on_a_newer_2x_version() -> None:
    # The start check confirms no image input.
    for version in ("2.1.282", "2.1.309"):
        assert _by_id(version, models_observed=True)["image_input"] == {
            "id": "image_input",
            "status": "unverified",
            "reason": "evidence_unavailable",
            "evidence": None,
        }


@pytest.mark.parametrize("version", [FIXTURE_VERSION, "2.1.259", "2.1.282", "2.1.309", "2.2.0", ""])
def test_steer_is_supported_only_on_the_proven_version(version: str) -> None:
    # Capture 38 recorded a profile chat's steer on 2.1.281 alone (profile-steer.json); a newer
    # 2.x's start check confirms nothing about it.
    proven = version == FIXTURE_VERSION
    assert _by_id(version, models_observed=True)["steer"] == (
        {"id": "steer", "status": "supported", "reason": None, "evidence": _LABEL}
        if proven
        else {
            "id": "steer",
            "status": "unverified",
            "reason": "evidence_unavailable",
            "evidence": None,
        }
    )
    assert steer_supported(version) is proven


def test_the_sdk_pin_and_fixture_version_move_together() -> None:
    # The SDK pin and FIXTURE_VERSION move together: evidence for a CLI the app no longer ships
    # would block every profile launch while every other test stayed green.
    assert (claude_agent_sdk.__version__, FIXTURE_VERSION) == ("0.2.159", "2.1.281")


def test_child_stop_is_unverified_pending_evidence() -> None:
    # No longer adapter_unsupported (removed from _UNSUPPORTED): the Claude CLI can stop a run,
    # but this adapter has no recorded evidence of it yet, on the proven version or any other.
    expected = {
        "id": "child_stop",
        "status": "unverified",
        "reason": "evidence_unavailable",
        "evidence": None,
    }
    assert _by_id(FIXTURE_VERSION)["child_stop"] == expected
    assert _by_id("2.1.282")["child_stop"] == expected


def test_unsupported_capabilities_stay_adapter_unsupported_on_every_version() -> None:
    for version in (FIXTURE_VERSION, "2.1.259"):
        rows = _by_id(version)
        for name in _ADAPTER_UNSUPPORTED:
            assert rows[name]["status"] == "unsupported"
            assert rows[name]["reason"] == "adapter_unsupported"
            assert rows[name]["evidence"] is None
