"""Acceptance (frozen): the rails data on synthetic trees, plus one opt-in read of the real
vault (`-m vault`, structure only)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from viki_agent import config, rails, winproc
from viki_agent.events import AgentInfo, GitChip, Routine, VaultCounts

SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _touch_at(path: Path, iso: str) -> None:
    stamp = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (stamp, stamp))


# --- agents ---------------------------------------------------------------------------------------


def test_agents_come_from_the_frontmatter_sorted_by_name_with_their_last_runs(
    tmp_path: Path,
) -> None:
    vault, project = tmp_path / "vault", tmp_path / "project"
    _write(
        vault / ".claude" / "agents" / "researcher.md",
        "---\nname: researcher\ndescription: Web-facing research —\n  fetches sources.\n"
        "tools: Read, Glob, Grep, WebFetch, WebSearch\nmodel: opus\n---\n\n"
        "You are VIKI's researcher.\n",
    )
    _write(
        vault / ".claude" / "agents" / "librarian.md",
        '---\ndescription: "Files inbox items."\ntools: Read, Write\n---\n\nYou file.\n',
    )
    _write(vault / ".claude" / "agents" / "notes.md", "# Not an agent: no frontmatter\n")
    _write(vault / ".claude" / "agents" / "README.txt", "ignored: not .md\n")
    old = project / SID / "subagents" / "agent-1.meta.json"
    _write(old, json.dumps({"agentType": "researcher", "description": "x"}))
    _touch_at(old, "2026-09-10T08:00:00+00:00")
    new = project / SID / "subagents" / "workflows" / "wf_1" / "agent-2.meta.json"
    _write(new, json.dumps({"agentType": "researcher", "workflowPhase": "Research"}))
    _touch_at(new, "2026-09-11T23:31:19+00:00")
    _write(project / SID / "subagents" / "agent-3.meta.json", "{not json")
    _write(project / SID / "subagents" / "agent-4.meta.json", json.dumps({"agentType": 7}))
    misplaced = project / SID / "agent-5.meta.json"  # not under subagents/
    _write(misplaced, json.dumps({"agentType": "librarian"}))

    runs = rails.last_runs(project)
    assert runs == {"researcher": "2026-09-11T23:31:19Z"}
    assert rails.last_runs(tmp_path / "nowhere") == {}
    assert rails.agents(vault, runs) == [
        AgentInfo(
            name="librarian",
            description="Files inbox items.",
            tools=["Read", "Write"],
            model="",
            last_run="",
        ),
        AgentInfo(
            name="researcher",
            description="Web-facing research — fetches sources.",
            tools=["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
            model="opus",
            last_run="2026-09-11T23:31:19Z",
        ),
    ]
    assert rails.agents(tmp_path / "no-vault", {}) == []


# AMENDMENT (2026-10-20): the gates feature retired;
# this section's own frozen pins retire with it, and so do the gate asserts (and the docstring's
# gates wording) of test_real_vault_rails_have_the_expected_structure below. No other frozen test
# here is touched.


# --- routines, counts, git ------------------------------------------------------------------------


def test_routines_are_the_planned_three() -> None:
    assert rails.routines() == [
        Routine(when="07:30", name="/daily · morning brief", planned=True),
        Routine(when="22:00", name="curator · nightly", planned=True),
        Routine(when="Sun 18:00", name="/weekly + /retro", planned=True),
    ]


def test_counts_are_files_under_the_three_folders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    for name in ("a.md", "b.md", "deep/c.md", ".hidden.md"):
        _write(vault / "30-knowledge" / name, "x")
    _write(vault / "40-sources" / "s.md", "x")
    (vault / "40-sources" / "empty-dir").mkdir()
    (vault / "00-inbox").mkdir()
    assert rails.counts(vault) == VaultCounts(knowledge=3, sources=1, inbox=0)
    assert rails.counts(tmp_path / "no-vault") == VaultCounts(knowledge=0, sources=0, inbox=0)


def _git(cwd: Path, *args: str) -> str:
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_git_chip_reads_head_branch_and_dirt_or_reports_no_repository(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    assert rails.git_chip(vault) == GitChip(
        branch="", head="", committed_at="", dirty=None, state="not_repository"
    )
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "t@example.com")
    _git(vault, "config", "user.name", "t")
    assert rails.git_chip(vault) == GitChip(
        branch="main", head="", committed_at="", dirty=0, state="known", ref_state="branch"
    )
    _write(vault / "Home.md", "# Home\n")
    _git(vault, "add", "Home.md")
    _git(vault, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
    _write(vault / "00-inbox" / "new.md", "x")
    chip = rails.git_chip(vault)
    assert chip.branch == "main" and chip.dirty == 1
    assert chip.head == _git(vault, "rev-parse", "--short", "HEAD")
    assert chip.committed_at == _git(vault, "log", "-1", "--format=%cI")
    assert datetime.fromisoformat(chip.committed_at).year >= 2026


def test_read_keeps_the_other_parts_when_one_breaks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "vault"
    _write(vault / ".claude" / "agents", "a file where the folder should be")  # agents() raises
    _write(vault / "30-knowledge" / "a.md", "x")
    with caplog.at_level(logging.WARNING):
        out = rails.rails_read(vault, tmp_path / "project")
    assert out.agents == [] and out.gates == [] and out.routines == rails.routines()
    assert out.counts == VaultCounts(knowledge=1, sources=0, inbox=0)
    assert out.git == GitChip(
        branch="", head="", committed_at="", dirty=None, state="not_repository"
    )
    assert any("agents" in r.getMessage() for r in caplog.records)
    assert not any(str(vault) in r.getMessage() for r in caplog.records)  # no paths in logs


@pytest.mark.vault
def test_real_vault_rails_have_the_expected_structure() -> None:
    """Structure only, never content: the real agents and counts read cleanly."""
    # AMENDMENT (2026-10-20): the retired gates feature's asserts left this test.
    # AMENDMENT (2026-11-15): the real vault is the one VIKI_VAULT names; unset, this check skips.
    cfg = config.load()
    root = config.vault_path(os.environ)
    if root is None or not root.is_dir():
        pytest.skip("set VIKI_VAULT to a vault to run this check")
    out = rails.rails_read(root, cfg.claude_home / "projects" / "unused")
    assert {a.name for a in out.agents} >= {"researcher", "librarian", "curator", "connector"}
    assert all(a.description and a.tools for a in out.agents)
    assert out.counts.knowledge > 50 and out.counts.sources > 50
    assert out.git.branch and out.git.head
    assert datetime.fromisoformat(out.git.committed_at) < datetime.now(UTC)
