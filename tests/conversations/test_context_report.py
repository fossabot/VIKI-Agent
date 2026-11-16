"""Tests for context_report: the whitelist of a `get_context_usage` reply."""

from __future__ import annotations

import json

import pytest

from tests.profiles.test_inventory import fixture_usage, with_agents_and_tools
from viki_agent.drivers.claude.context_report import (
    MAX_COUNT,
    REPORT_PARTS,
    context_report,
    failed_report,
)

STAMP = "2026-09-28T02:00:00.000Z"


def test_the_fixture_reply_gives_the_report() -> None:
    usage = fixture_usage()
    before = json.dumps(usage, sort_keys=True)
    assert context_report(usage, STAMP) == {
        "observed_at": STAMP,
        "total_tokens": 0,
        "max_tokens": 0,
        "raw_max_tokens": 0,
        "percentage": 0,
        "auto_compact": {"enabled": True, "threshold": 0, "source": "settings"},
        "categories": [
            {"name": "System prompt", "tokens": 0, "kind": "used"},
            {"name": "System tools", "tokens": 0, "kind": "used"},
            {"name": "MCP tools", "tokens": 0, "kind": "used"},
            {"name": "MCP tools (deferred)", "tokens": 0, "kind": "deferred"},
            {"name": "System tools (deferred)", "tokens": 0, "kind": "deferred"},
            {"name": "Memory files", "tokens": 0, "kind": "used"},
            {"name": "Skills", "tokens": 0, "kind": "used"},
            {"name": "Messages", "tokens": 0, "kind": "used"},
            {"name": "Autocompact buffer", "tokens": 0, "kind": "buffer"},
            {"name": "Free space", "tokens": 0, "kind": "free"},
        ],
        "memory_files": [{"name": "CLAUDE.md", "type": "Project", "tokens": 0}],
        "mcp_servers": [{"server": "viki_echo", "tools": 1, "loaded": 0, "loaded_tokens": 0}],
        "skills": {
            "total": 0,
            "included": 0,
            "tokens": 0,
            "items": [
                {
                    "name": "example-skill",
                    "source": "plugin",
                    "plugin": "example-plugin",
                    "tokens": 0,
                }
            ],
        },
        "agents": [],
        "reasons": {},
    }
    # Pure: the reply dict itself is never mutated.
    assert json.dumps(usage, sort_keys=True) == before


def test_deferred_tools_count_but_add_no_tokens() -> None:
    usage = with_agents_and_tools()
    usage["mcpTools"] = [
        {"name": "mcp__s__a", "serverName": "s", "tokens": 100, "isLoaded": True},
        {"name": "mcp__s__b", "serverName": "s", "tokens": 50, "isLoaded": False},
        {"name": "mcp__t__c", "serverName": "t", "tokens": 9, "isLoaded": False},
    ]
    report = context_report(usage, STAMP)
    assert report["mcp_servers"] == [
        {"server": "s", "tools": 2, "loaded": 1, "loaded_tokens": 100},
        {"server": "t", "tools": 1, "loaded": 0, "loaded_tokens": 0},
    ]
    assert report["reasons"] == {}


@pytest.mark.parametrize(
    ("part", "key", "bad_value"),
    [
        ("categories", "categories", [{"name": "x", "tokens": 0, "kind": "nope"}]),
        ("memory_files", "memoryFiles", [{"path": "x"}]),
        (
            "mcp_servers",
            "mcpTools",
            [{"name": "x", "serverName": "s", "isLoaded": True}],  # no tokens
        ),
        (
            "skills",
            "skills",
            {"totalSkills": 0, "includedSkills": 0, "skillFrontmatter": "nope"},
        ),
        ("agents", "agents", [{"agentType": "Explore", "source": "built-in"}]),  # no tokens
    ],
)
def test_a_missing_or_off_shape_part_reads_its_reason(
    part: str, key: str, bad_value: object
) -> None:
    missing = with_agents_and_tools()
    del missing[key]
    report = context_report(missing, STAMP)
    assert report[part] is None
    assert report["reasons"] == {part: "absent"}
    assert all(report[other] is not None for other in REPORT_PARTS if other != part)

    off_shape = with_agents_and_tools()
    off_shape[key] = bad_value
    report = context_report(off_shape, STAMP)
    assert report[part] is None
    assert report["reasons"] == {part: "unrecognized"}
    assert all(report[other] is not None for other in REPORT_PARTS if other != part)


@pytest.mark.parametrize("bad", [True, False, -1, 3.5, MAX_COUNT + 1])
def test_counts_refuse_bools_negatives_floats_and_huge(bad: object) -> None:
    usage = with_agents_and_tools()
    usage["totalTokens"] = bad
    usage["categories"] = [{**usage["categories"][0], "tokens": bad}]
    usage["agents"] = [{**usage["agents"][0], "tokens": bad}]
    report = context_report(usage, STAMP)
    assert report["total_tokens"] is None
    assert report["categories"] is None
    assert report["agents"] is None
    assert report["reasons"] == {"categories": "unrecognized", "agents": "unrecognized"}


@pytest.mark.parametrize("bad_kind", [{"type": "used"}, ["used"]])
def test_an_unhashable_kind_reads_unrecognized_without_raising(bad_kind: object) -> None:
    usage = with_agents_and_tools()
    usage["categories"] = [{**usage["categories"][0], "kind": bad_kind}]
    report = context_report(usage, STAMP)
    assert report["categories"] is None
    assert report["reasons"] == {"categories": "unrecognized"}


def test_nothing_outside_the_whitelist_is_copied() -> None:
    usage = with_agents_and_tools()
    planted = [f"PLANTED-{n}" for n in range(10)]
    usage["categories"] = [{**usage["categories"][0], "color": planted[0]}]
    usage["memoryFiles"] = [
        {
            "path": f"C:\\src\\{planted[1]}\\CLAUDE.md",
            "type": "Project",
            "tokens": 0,
            "content": planted[2],
        }
    ]
    usage["mcpTools"] = [{**usage["mcpTools"][0], "description": planted[3]}]
    usage["skills"] = {
        **usage["skills"],
        "skillFrontmatter": [{**usage["skills"]["skillFrontmatter"][0], "description": planted[4]}],
    }
    usage["agents"] = [{**usage["agents"][0], "description": planted[5], "prompt": planted[6]}]
    usage["gridRows"] = [[{"x": planted[7]}]]
    usage["apiUsage"] = {"input_tokens": 1234, "authorization": f"Bearer {planted[8]}"}
    usage["model"] = planted[9]
    text = json.dumps(context_report(usage, STAMP))
    assert not [value for value in planted if value in text]


def test_the_failed_report_names_one_reason_for_every_part() -> None:
    assert failed_report("timeout") == {
        "observed_at": None,
        "total_tokens": None,
        "max_tokens": None,
        "raw_max_tokens": None,
        "percentage": None,
        "auto_compact": None,
        **dict.fromkeys(REPORT_PARTS),
        "reasons": dict.fromkeys(REPORT_PARTS, "timeout"),
    }
