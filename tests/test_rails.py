"""Extra tests for rails.py, beside the frozen acceptance suite
(tests/acceptance/test_rails_accept.py): frontmatter edge cases its synthetic agent files don't
happen to exercise, and the read() failure-logging contract on a different broken part.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_server import BEARER, running
from viki_agent import rails, server, winproc
from viki_agent.events import AgentInfo, GitChip, Rails, VaultCounts

SID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _touch_at(path: Path, iso: str) -> None:
    stamp = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (stamp, stamp))


def test_the_rails_route_resolves_the_project_folder_in_the_same_thread_as_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_dir does realpath on macOS and Linux, a blocking call that must not run on the
    event loop: the route runs it inside the thread that reads the rails."""
    threads: dict[str, int] = {}
    loop_thread = {"ident": 0}

    def project_dir(claude_home: Path, vault: Path) -> Path:
        threads["project"] = threading.get_ident()
        return claude_home

    def rails_read(vault: Path, project: Path) -> Rails:
        threads["read"] = threading.get_ident()
        return Rails(
            agents=[],
            gates=[],
            routines=rails.routines(),
            counts=VaultCounts(knowledge=0, sources=0, inbox=0),
            git=GitChip(branch="", head="", committed_at="", dirty=0),
        )

    monkeypatch.setattr(server, "project_dir", project_dir)
    monkeypatch.setattr(rails, "rails_read", rails_read)
    with running(tmp_path) as app:
        loop_thread["ident"] = app.client.portal.call(threading.get_ident)  # type: ignore[union-attr]
        assert app.client.get("/api/rails", headers=BEARER).status_code == 200

    assert threads["project"] == threads["read"] != loop_thread["ident"]


# --- frontmatter edge cases (agents) -----------------------------------------------------------


def test_frontmatter_quotes_are_dropped_but_mismatched_quotes_are_left_alone(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write(
        vault / ".claude" / "agents" / "a.md",
        "---\nname: a\ndescription: 'single quoted'\nmodel: \"opus\"\n---\nbody\n",
    )
    _write(
        vault / ".claude" / "agents" / "b.md",
        '---\nname: b\ndescription: "unterminated\n---\nbody\n',
    )
    out = {agent.name: agent for agent in rails.agents(vault, {})}
    assert out["a"].description == "single quoted"
    assert out["a"].model == "opus"
    assert out["b"].description == '"unterminated'  # mismatched quotes: left as-is


def test_agent_name_field_overrides_the_file_stem(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(
        vault / ".claude" / "agents" / "connector.md",
        "---\nname: link-finder\ndescription: Connects notes.\n---\nbody\n",
    )
    assert rails.agents(vault, {"link-finder": "2026-09-01T00:00:00Z"}) == [
        AgentInfo(
            name="link-finder",
            description="Connects notes.",
            tools=[],
            model="",
            last_run="2026-09-01T00:00:00Z",
        )
    ]


def test_agent_frontmatter_without_a_closing_marker_is_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / ".claude" / "agents" / "broken.md", "---\nname: broken\ndescription: x\n")
    assert rails.agents(vault, {}) == []


def test_agent_frontmatter_with_a_yaml_block_scalar_description(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(
        vault / ".claude" / "agents" / "researcher.md",
        "---\nname: researcher\ndescription: >-\n  Deep-dives a topic on the web and writes it\n"
        "  up as evergreen notes.\ntools: Read\n---\nbody\n",
    )
    assert rails.agents(vault, {}) == [
        AgentInfo(
            name="researcher",
            description="Deep-dives a topic on the web and writes it up as evergreen notes.",
            tools=["Read"],
            model="",
            last_run="",
        )
    ]


def test_agent_frontmatter_with_a_yaml_pipe_scalar_description(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(
        vault / ".claude" / "agents" / "librarian.md",
        "---\nname: librarian\ndescription: |\n  Files inbox items.\n---\nbody\n",
    )
    out = rails.agents(vault, {})
    assert out[0].description == "Files inbox items."


def test_agent_frontmatter_with_a_yaml_sequence_for_tools(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(
        vault / ".claude" / "agents" / "connector.md",
        "---\nname: connector\ndescription: Connects notes.\ntools:\n  - Read\n  - Glob\n"
        "  - Grep\n---\nbody\n",
    )
    out = rails.agents(vault, {})
    assert out[0].tools == ["Read", "Glob", "Grep"]


def test_agent_frontmatter_survives_crlf_and_a_bom(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = vault / ".claude" / "agents" / "crlf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\xef\xbb\xbf---\r\n"
        b"name: crlf\r\n"
        b"description: One line\r\n"
        b"  continued.\r\n"
        b"tools: Read\r\n"
        b"---\r\n"
        b"body\r\n"
    )
    assert rails.agents(vault, {}) == [
        AgentInfo(
            name="crlf", description="One line continued.", tools=["Read"], model="", last_run=""
        )
    ]


# --- last_runs ------------------------------------------------------------------------------------


def test_last_runs_picks_the_newest_file_and_ignores_a_non_string_agent_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    older = project / SID / "subagents" / "a.meta.json"
    newer = project / SID / "subagents" / "b.meta.json"
    non_string = project / SID / "subagents" / "c.meta.json"
    _write(older, json.dumps({"agentType": "curator"}))
    _touch_at(older, "2026-09-01T00:00:00+00:00")
    _write(newer, json.dumps({"agentType": "curator"}))
    _touch_at(newer, "2026-09-05T12:00:00+00:00")
    _write(non_string, json.dumps({"agentType": None}))
    _touch_at(non_string, "2026-09-09T00:00:00+00:00")
    assert rails.last_runs(project) == {"curator": "2026-09-05T12:00:00Z"}


def test_last_runs_skips_a_meta_file_nested_too_deep(tmp_path: Path) -> None:
    """json.loads raises RecursionError, not ValueError, on JSON nested this deep. It must be
    skipped the same way any other unparseable meta file is, never crash the rails route."""
    project = tmp_path / "project"
    nested = project / SID / "subagents" / "nested.meta.json"
    kept = project / SID / "subagents" / "kept.meta.json"
    _write(nested, "[" * 200_000)
    _touch_at(nested, "2026-09-09T00:00:00+00:00")
    _write(kept, json.dumps({"agentType": "curator"}))
    _touch_at(kept, "2026-09-05T12:00:00+00:00")
    assert rails.last_runs(project) == {"curator": "2026-09-05T12:00:00Z"}


# --- counts -----------------------------------------------------------------------------------


def test_counts_skips_any_file_with_a_hidden_path_part(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "30-knowledge" / ".obsidian" / "normal.md", "x")  # under a dot directory
    _write(vault / "30-knowledge" / "deep" / ".hidden.md", "x")  # nested dotfile
    _write(vault / "30-knowledge" / "deep" / "ok.md", "x")
    assert rails.counts(vault) == VaultCounts(knowledge=1, sources=0, inbox=0)


def _git(cwd: Path, *args: str) -> None:
    """Repo setup (init, config, commit): the acceptance file's helper, since winproc.git is
    the read-only path."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr


# --- git_chip ---------------------------------------------------------------------------------


def test_git_chip_on_a_fresh_repo_before_any_commit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "trunk")
    assert rails.git_chip(vault) == GitChip(
        branch="trunk", head="", committed_at="", dirty=0, state="known", ref_state="branch"
    )


def _init_repo_with_one_commit(vault: Path) -> None:
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "t@example.com")
    _git(vault, "config", "user.name", "t")
    _write(vault / "Home.md", "# Home\n")
    _git(vault, "add", "Home.md")
    _git(vault, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")


def test_git_chip_keeps_head_committed_at_and_dirty_when_head_is_detached(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _init_repo_with_one_commit(vault)
    _git(vault, "checkout", "-q", "--detach", "HEAD")
    _write(vault / "00-inbox" / "new.md", "x")
    chip = rails.git_chip(vault)
    assert chip.branch == "" and chip.ref_state == "detached"
    assert chip.head != "" and chip.committed_at != ""
    assert chip.dirty == 1 and chip.state == "known"


class _SimulatedTimeout(Exception):
    """Stands in for subprocess.TimeoutExpired: a plain Exception, not a RuntimeError or
    OSError, since rails.py may not import subprocess (ruff TID251)."""


def test_git_chip_degrades_only_the_field_whose_lookup_raises_a_non_standard_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed status read preserves independent metadata without certifying clean."""
    vault = tmp_path / "vault"
    _init_repo_with_one_commit(vault)
    _write(vault / "00-inbox" / "new.md", "x")
    assert rails.git_chip(vault).dirty == 1  # real git sees the uncommitted file
    real_git = winproc.git

    def flaky_git(cwd: Path, *args: str, timeout: float = 15.0) -> str:
        if args and args[0] == "status":
            raise _SimulatedTimeout("git status wedged")
        return real_git(cwd, *args, timeout=timeout)

    monkeypatch.setattr(rails.winproc, "git", flaky_git)
    chip = rails.git_chip(vault)
    assert chip.branch == "main"
    assert chip.head != "" and chip.committed_at != ""
    assert chip.dirty is None and chip.state == "unknown"


def test_git_chip_caps_every_lookup_so_a_wedged_repo_cannot_outlast_the_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rail re-reads the chip every 60 s and each read runs in a pooled thread shared with
    the history and search reads. winproc.git's 15 s default would let four wedged lookups hold
    one of those threads for the whole interval, so the next poll parks another thread on top of
    the last; git_chip passes its own cap to keep the worst case inside one interval."""
    vault = tmp_path / "vault"
    _init_repo_with_one_commit(vault)
    real_git = winproc.git
    timeouts: list[float] = []

    def recording_git(cwd: Path, *args: str, timeout: float = 15.0) -> str:
        timeouts.append(timeout)
        return real_git(cwd, *args, timeout=timeout)

    monkeypatch.setattr(rails.winproc, "git", recording_git)
    chip = rails.git_chip(vault)
    assert chip.branch == "main" and chip.head != ""  # the cap does not blank a healthy repo
    assert len(timeouts) == 4
    assert sum(timeouts) < 60.0  # the rail's refresh interval; winproc's default would hit it


def test_git_chip_logs_a_failed_lookup_with_its_subcommand_and_type_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A vault that is not a repo blanks the whole chip, and a blank chip is simply absent from
    the top bar — so viki-agent.log is the only place that can say git was asked and failed.
    The line carries the subcommand and the exception type and nothing else: comparing the whole
    list is what proves no vault path and no git stderr got in."""
    monkeypatch.setattr(rails, "_LOGGED_GIT_FAILURES", set())  # per launch, not per test
    vault = tmp_path / "vault"
    vault.mkdir()
    with caplog.at_level(logging.WARNING):
        chip = rails.git_chip(vault)
    assert chip == GitChip(branch="", head="", committed_at="", dirty=None, state="not_repository")
    assert [record.getMessage() for record in caplog.records] == [
        "rails: git branch failed (RuntimeError)",
        "rails: git rev-parse failed (RuntimeError)",
        "rails: git log failed (RuntimeError)",
        "rails: git status failed (RuntimeError)",
    ]


def test_git_chip_logs_one_line_per_subcommand_and_exception_type_per_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The rail re-reads the chip every 60 s, so a vault that is not a repo fails four lookups a
    minute for as long as the app runs. viki-agent.log is a size-capped rotating file
    (__main__.py), and logging each failure would evict every other warning from it within
    hours; one line per distinct failure is the evidence, not a stream. A failure mode that
    changes is a different failure, so it gets its own line."""
    monkeypatch.setattr(rails, "_LOGGED_GIT_FAILURES", set())
    vault = tmp_path / "vault"
    vault.mkdir()
    with caplog.at_level(logging.WARNING):
        rails.git_chip(vault)
        rails.git_chip(vault)
        rails.git_chip(vault)
        assert len(caplog.records) == 4  # three polls, four lookups each, four lines

        def wedged_git(cwd: Path, *args: str, timeout: float = 15.0) -> str:
            raise _SimulatedTimeout("git wedged")

        monkeypatch.setattr(rails.winproc, "git", wedged_git)
        rails.git_chip(vault)
    assert [record.getMessage() for record in caplog.records][4:] == [
        "rails: git branch failed (_SimulatedTimeout)",
        "rails: git rev-parse failed (_SimulatedTimeout)",
        "rails: git log failed (_SimulatedTimeout)",
        "rails: git status failed (_SimulatedTimeout)",
    ]


# --- rails_read -------------------------------------------------------------------------------


def test_rails_read_has_no_gates_beside_a_pending_retro_row(tmp_path: Path) -> None:
    """The wire's gates field is always []: a retro report's gate-summary table may carry a
    pending row, but nothing parses the table."""
    vault = tmp_path / "vault"
    _write(
        vault / "system" / "retro" / "2026-09-10.md",
        "# Retro\n\n## Gate summary\n\n| # | Item | Conf. | Owner action |\n"
        "|---|---|---|---|\n| H1 | Needs a decision | medium | approve/reject |\n",
    )
    out = rails.rails_read(vault, tmp_path / "project")
    assert out.gates == []


def test_rails_read_logs_the_exception_type_never_a_path_for_any_failing_part(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "vault"
    _write(vault / ".claude" / "agents", "a file where the folder should be")  # agents() raises
    with caplog.at_level(logging.WARNING):
        out = rails.rails_read(vault, tmp_path / "project")
    assert out.agents == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("agents" in message and "NotADirectoryError" in message for message in messages)
    assert not any(str(vault) in message for message in messages)


def test_rails_read_keeps_the_other_parts_when_git_chip_itself_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """git_chip catches each lookup failure itself and degrades field by field, so nothing it
    does today reaches rails_read's git handler — this is what that handler is kept for, and
    the only way to reach it is to make git_chip raise. A chip that goes down must not take the
    agents, gates and counts beside it down too."""
    vault = tmp_path / "vault"
    _write(vault / "30-knowledge" / "a.md", "x")

    def boom(_: Path, **_kwargs: object) -> GitChip:
        raise MemoryError("no room for a chip")

    monkeypatch.setattr(rails, "git_chip", boom)
    with caplog.at_level(logging.WARNING):
        out = rails.rails_read(vault, tmp_path / "project")
    assert out.git == GitChip(branch="", head="", committed_at="", dirty=None)
    assert out.counts == VaultCounts(knowledge=1, sources=0, inbox=0)
    assert out.agents == [] and out.gates == [] and out.routines == rails.routines()
    messages = [record.getMessage() for record in caplog.records]
    assert "rails: git failed (MemoryError)" in messages
    assert not any(str(vault) in message for message in messages)


@pytest.mark.parametrize("populated", [False, True], ids=["returned-zero", "returned-files"])
def test_count_observation_reports_the_single_existing_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    if populated:
        _write(vault / "30-knowledge" / "one.md", "# one")
        _write(vault / "30-knowledge" / "nested" / "two.txt", "two")
        _write(vault / "30-knowledge" / ".hidden.md", "hidden")
        _write(vault / "30-knowledge" / ".private" / "hidden.md", "hidden")
        _write(vault / "40-sources" / "source.txt", "source")
        _write(vault / "00-inbox" / "item.md", "item")
        (vault / "00-inbox" / "empty-directory").mkdir()
    original = rails.counts
    seen: list[Path] = []

    def observed(root: Path) -> VaultCounts:
        seen.append(root)
        return original(root)

    monkeypatch.setattr(rails, "counts", observed)
    monkeypatch.setattr(
        rails,
        "git_chip",
        lambda *_args, **_kwargs: GitChip(branch="", head="", committed_at="", dirty=0),
    )
    out = rails.rails_read(vault, tmp_path / "project")
    assert seen == [vault]
    assert out.counts_state == "ready"
    assert out.counts == (
        VaultCounts(knowledge=2, sources=1, inbox=1)
        if populated
        else VaultCounts(knowledge=0, sources=0, inbox=0)
    )


def test_failed_count_observation_keeps_raw_fallback_and_other_parts_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "vault"
    agent_list = [AgentInfo(name="Author", description="literal", tools=[], model="", last_run="")]
    chip = GitChip(branch="main", head="abc123", committed_at="", dirty=3)
    seen: list[Path] = []

    def failed(root: Path) -> VaultCounts:
        seen.append(root)
        raise PermissionError(f"synthetic private text at {root}")

    monkeypatch.setattr(rails, "last_runs", lambda _project: {})
    monkeypatch.setattr(rails, "agents", lambda *_args: agent_list)
    monkeypatch.setattr(rails, "git_chip", lambda *_args, **_kwargs: chip)
    monkeypatch.setattr(rails, "counts", failed)
    with caplog.at_level(logging.WARNING):
        out = rails.rails_read(vault, tmp_path / "project")
    assert seen == [vault]
    assert out.counts_state == "error"
    assert out.counts == VaultCounts(knowledge=0, sources=0, inbox=0)
    assert out.agents is agent_list and out.git is chip
    assert out.routines == rails.routines()
    assert [record.getMessage() for record in caplog.records] == [
        "rails: counts failed (PermissionError)"
    ]


@pytest.mark.parametrize("part", ["agents", "git_chip"])
def test_other_part_failure_cannot_invalidate_returned_count_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    vault = tmp_path / "vault"
    _write(vault / "30-knowledge" / "one.md", "one")
    original = rails.counts
    seen: list[Path] = []

    def observed(root: Path) -> VaultCounts:
        seen.append(root)
        return original(root)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic unrelated failure")

    monkeypatch.setattr(rails, "counts", observed)
    monkeypatch.setattr(
        rails,
        "git_chip",
        lambda *_args, **_kwargs: GitChip(branch="", head="", committed_at="", dirty=0),
    )
    monkeypatch.setattr(rails, part, failed)
    out = rails.rails_read(vault, tmp_path / "project")
    assert seen == [vault]
    assert out.counts_state == "ready"
    assert out.counts == VaultCounts(knowledge=1, sources=0, inbox=0)
