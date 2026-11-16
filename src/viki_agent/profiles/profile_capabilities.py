"""Conservative evidence for the existing Claude adapter; no discovery subprocesses."""

from __future__ import annotations

from typing import Any

from viki_agent.events import SessionStarted

ADAPTER_VERSION = "viki-claude-1"
FIXTURE_VERSION = "2.1.281"
# A newer 2.x than FIXTURE_VERSION has no recorded capture. Its START_CHECKED rows rest on the
# start check instead: before any text is sent, ProfileLaunchService.native_initialized needs
# Claude Code's own start-up report, confirming the launch's plan mode, pinned model and app
# servers. Every such version shares one catalog label, START_CHECK_VERSION.
START_CHECK_EVIDENCE = "claude-start-check"
START_CHECK_VERSION = "2.x"
START_CHECKED = frozenset({"effort_selection", "plan_mode", "mcp_config", "model_selection"})
_SUPPORTED = {
    "resume",
    "plan_mode",
    "usage_detail",
    "structured_approvals",
    "effort_selection",
    "image_input",
    "mcp_config",
    "steer",  # capture 38 on 2.1.281: tests/fixtures/claude/profile-steer.json
}
# Rows no profile field evaluation reads, so a binding's review does not depend on them.
UNREVIEWED = frozenset({"steer"})
_UNSUPPORTED = {
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


def _start_checked(native_version: str) -> bool:
    """Dot-separated ASCII digits, at most 64 characters, of FIXTURE_VERSION's major and above."""
    parts = native_version.split(".")
    # isascii too: isdigit() also accepts digits such as "²" that int() refuses.
    if len(native_version) > 64 or not all(part.isascii() and part.isdigit() for part in parts):
        return False
    numbers = tuple(int(part) for part in parts)
    fixture = tuple(int(part) for part in FIXTURE_VERSION.split("."))
    return numbers[0] == fixture[0] and numbers > fixture


def evidence_version(native_version: str) -> str:
    """The catalog label for a CLI version: START_CHECK_VERSION for every start-checked one."""
    return START_CHECK_VERSION if _start_checked(native_version) else native_version


def baseline(native_version: str, *, models_observed: bool = False) -> list[dict[str, Any]]:
    rows = []
    names = _SUPPORTED | _UNSUPPORTED | {"skills_listing", "model_selection", "child_stop"}
    proved: frozenset[str] = frozenset()
    evidence: str | None = None
    if native_version == FIXTURE_VERSION:
        proved = frozenset(_SUPPORTED | {"model_selection"})
        evidence = f"claude-{FIXTURE_VERSION}-compatibility"
    elif _start_checked(native_version):
        proved, evidence = START_CHECKED, START_CHECK_EVIDENCE
    for name in sorted(names):
        # model_selection needs an observed model menu too.
        supported = name in proved and (name != "model_selection" or models_observed)
        status = (
            "supported" if supported else "unsupported" if name in _UNSUPPORTED else "unverified"
        )
        reason = (
            None
            if supported
            else "adapter_unsupported"
            if name in _UNSUPPORTED
            else "evidence_unavailable"
        )
        rows.append(
            {
                "id": name,
                "status": status,
                "reason": reason,
                "evidence": evidence if supported else None,
            }
        )
    return rows


def steer_supported(native_version: str) -> bool:
    """Whether baseline()'s steer row is supported on this CLI version."""
    return any(
        row["id"] == "steer" and row["status"] == "supported" for row in baseline(native_version)
    )


def observed_models(started: SessionStarted) -> list[dict[str, str]]:
    # Only the already sanitized initialize menu, never the provider account/config block.
    return [
        {"value": model.value, "label": model.label, "description": model.description}
        for model in started.models
    ]
