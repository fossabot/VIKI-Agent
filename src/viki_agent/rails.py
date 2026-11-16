"""Read-only data for the right rail and the status bar: the agents roster with their real last
runs, the planned routines, the vault counts and the git chip. Nothing here writes, holds a
file open, or reads outside the vault and the transcript project folder. Every function blocks:
the server calls them in a thread.

Rules (tests/acceptance/test_rails_accept.py pins them):
- agents(vault, last_runs): one AgentInfo per `<vault>/.claude/agents/*.md`, sorted by name.
  The frontmatter is the block between a first line `---` and the next `---` line, read as
  plain `key: value` lines (no YAML library; a value's surrounding quotes are dropped, and a
  bare YAML block-scalar indicator — `>`, `>-`, `|`, `|-`, ... — alone on the value's own line
  counts as an empty value, so its indented body folds in the same way as any wrapped plain
  value): `name` (the file stem when absent), `description` (one line: newlines collapsed to
  single spaces, stripped), `tools` (a plain comma-separated value, or an indented YAML `- item`
  sequence collected into that same comma-separated form, split and stripped, [] when absent),
  `model` ("" when absent). A file without frontmatter is skipped. `last_run` comes from
  `last_runs` by name ("" when the agent never ran).
- last_runs(project_dir): {agentType: ISO 8601 UTC, second precision, "Z" suffix} of the newest
  file per agentType over every `<project_dir>/*/subagents/**/*.meta.json` whose JSON object
  has a string `agentType`; unreadable or malformed files are skipped; a missing folder gives {}.
- routines(): the three planned rows, in this order, all planned=True until a schedule exists:
  ("07:30", "/daily · morning brief"), ("22:00", "curator · nightly"),
  ("Sun 18:00", "/weekly + /retro").
- counts(vault): files (not directories; any path part starting with "." excluded) anywhere under
  `30-knowledge/`, `40-sources/` and `00-inbox/`; a missing folder counts 0.
- git_chip(vault): through winproc.git, independently read the current branch, short HEAD,
  HEAD commit date, and status. Only a successful status read gives a dirty count, including
  zero. Failed status is unknown, never clean. A successful blank branch with known HEAD is
  detached; an unborn repository retains its branch without a HEAD. Each call is capped at
  5 s. A non-repository is reported only after Git availability and marker-free ancestry
  are established; ambiguity remains unknown. Failure warnings contain only the subcommand
  and exception type, once per kind. Without `hardening` (a vault, at any policy) every
  lookup runs in `winproc.untrusted_git`, and status stays out of submodules. Given
  `operation` (a code folder's `CodeGit.operation`), every lookup runs inside it, and a
  refused settings check gives the empty chip.
- read(vault, project_dir): a Rails from the four parts, each computed on its own; a part that
  raises is logged at WARNING (the exception type, never a path) and replaced by its empty
  value ([], routines(), VaultCounts(0, 0, 0), the empty chip), so one bad file never blanks
  the whole rail. `gates` is always [] — retired, but kept on the wire for the frontend's Rails
  type and fakes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from viki_agent import winproc
from viki_agent.events import AgentInfo, GitChip, Rails, Routine, VaultCounts

__all__ = ["agents", "counts", "git_chip", "last_runs", "rails_read", "routines"]

_LOGGER = logging.getLogger(__name__)

# Log each failed Git subcommand and exception type once per launch. A 60 s rail refresh
# should not fill the rotating log with repeated expected failures; no path or stderr enters
# the message. A changed failure type still produces one new observation.
_LOGGED_GIT_FAILURES: set[tuple[str, str]] = set()

_GIT_SCOPE_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_EXEC_PATH",
    }
)


def _has_git_scope_override() -> bool:
    # The presence is enough; do not read values because they may contain private paths.
    return any(
        (upper := name.upper()) in _GIT_SCOPE_VARIABLES or upper.startswith("GIT_CONFIG")
        for name in os.environ
    )


# --- shared file helpers ----------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """UTF-8 text of `path` (CRLF is normalised by text-mode read; a leading BOM is stripped
    here, since plain "utf-8" decoding leaves it in). "" when the file can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.removeprefix("﻿")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# A bare YAML block-scalar indicator on a value's own line: `>`, `>-`, `>+`, `|`, `|-`, `|2`, ...
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+\-0-9]*$")


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    """The block between a first line `---` and the next `---` line, as plain `key: value`
    lines: an indented continuation line is space-joined onto the previous key — except that a
    YAML block-scalar indicator (`>`, `|`, ...) alone on the key's line first resets the value to
    "", so its folded body joins in exactly the same way, and an indented `- item` continuation
    under an empty value is instead collected into the same comma-separated form a plain
    `tools: a, b` line would produce. A value's surrounding quotes are dropped. None when the
    opening or closing marker is missing (no frontmatter); {} for a present-but-empty block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    fields: dict[str, str] = {}
    key: str | None = None
    sequence_keys: set[str] = set()
    for line in lines[1:end]:
        if key is not None and line.startswith((" ", "\t")):
            item = line.strip()
            if item.startswith("- ") and (key in sequence_keys or fields[key] == ""):
                sequence_keys.add(key)
                piece = item[2:].strip()
                fields[key] = piece if fields[key] == "" else f"{fields[key]}, {piece}"
            else:
                fields[key] = f"{fields[key]} {item}".strip()
            continue
        if ":" not in line:
            key = None
            continue
        raw_key, _, raw_value = line.partition(":")
        key = raw_key.strip()
        value = raw_value.strip()
        # The continuation branch above folds the indented body into an empty value exactly
        # like any wrapped plain value, so a block-scalar indicator need only be discarded here.
        fields[key] = "" if _BLOCK_SCALAR_RE.match(value) else value
    return {key: _strip_quotes(value) for key, value in fields.items()}


def agents(vault: Path, last_runs: dict[str, str]) -> list[AgentInfo]:
    folder = vault / ".claude" / "agents"
    try:
        paths = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".md"]
    except FileNotFoundError:
        return []
    out: list[AgentInfo] = []
    for path in paths:
        fields = _parse_frontmatter(_read_text(path))
        if fields is None:
            continue
        name = fields.get("name") or path.stem
        tools = [tool.strip() for tool in fields.get("tools", "").split(",") if tool.strip()]
        out.append(
            AgentInfo(
                name=name,
                description=fields.get("description", ""),
                tools=tools,
                model=fields.get("model", ""),
                last_run=last_runs.get(name, ""),
            )
        )
    out.sort(key=lambda agent: agent.name)
    return out


def _iso_seconds(epoch: float) -> str:
    stamp = datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def last_runs(project_dir: Path) -> dict[str, str]:
    try:
        paths = list(project_dir.glob("*/subagents/**/*.meta.json"))
    except FileNotFoundError:
        return {}
    newest: dict[str, float] = {}
    for path in paths:
        try:
            data = json.loads(_read_text(path))
        except ValueError, RecursionError:
            continue
        if not isinstance(data, dict):
            continue
        agent_type = data.get("agentType")
        if not isinstance(agent_type, str):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest.get(agent_type, float("-inf")):
            newest[agent_type] = mtime
    return {agent_type: _iso_seconds(mtime) for agent_type, mtime in newest.items()}


def routines() -> list[Routine]:
    return [
        Routine(when="07:30", name="/daily · morning brief", planned=True),
        Routine(when="22:00", name="curator · nightly", planned=True),
        Routine(when="Sun 18:00", name="/weekly + /retro", planned=True),
    ]


def counts(vault: Path) -> VaultCounts:
    def count_files(folder: Path) -> int:
        if not folder.is_dir():
            return 0
        return sum(
            1
            for p in folder.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(folder).parts)
        )

    return VaultCounts(
        knowledge=count_files(vault / "30-knowledge"),
        sources=count_files(vault / "40-sources"),
        inbox=count_files(vault / "00-inbox"),
    )


def git_chip(
    vault: Path,
    *,
    hardening: tuple[str, ...] = (),
    operation: Callable[[Path], AbstractContextManager[tuple[str, ...]]] | None = None,
) -> GitChip:
    if _has_git_scope_override():
        return GitChip(branch="", head="", committed_at="", dirty=None)
    if operation is not None:
        # A code folder's `CodeGit.operation`: once the folder has a Bot-copy baseline, no Git
        # runs in it until its settings check passes; `status` alone would run a clean filter a
        # Bot added. A refusal reads as unknown, like any other failed lookup.
        try:
            with operation(vault) as hooks:
                return git_chip(vault, hardening=(*hardening, *hooks))
        except Exception as exc:
            _warn_once("settings check", exc)
            return GitChip(branch="", head="", committed_at="", dirty=None)

    # A vault's own repository settings are nobody's to trust, at any policy, so its lookups run
    # untrusted, and status stays out of submodules: their own settings name filter drivers
    # this read does not switch off. A code folder is read once trusted, with its `hardening`.
    untrusted = not hardening
    submodules = ("--ignore-submodules=all",) if untrusted else ()

    # Each normal lookup has a 5 s cap. A repo without detectable metadata needs one more
    # capped Git availability check, so the worst case is 25 s within the 60 s rail refresh.
    # These calls run in the read worker pool and must not strand the next poll.
    def try_git(*args: str) -> str | None:
        try:
            return winproc.git(vault, *hardening, *args, timeout=5.0).strip()
        except Exception as exc:
            _warn_once(args[0], exc)
            return None

    with winproc.untrusted_git() if untrusted else nullcontext():
        # --show-current succeeds with an empty value on detached HEAD and still names an
        # unborn branch. A failed branch lookup remains distinct from that successful empty one.
        branch_read = try_git("branch", "--show-current")
        head_read = try_git("rev-parse", "--short", "HEAD")
        date_read = try_git("log", "-1", "--format=%cI")
        status = try_git("status", "--porcelain", *submodules)
        absent = (
            status is None
            and branch_read is None
            and head_read is None
            and date_read is None
            and _proved_not_repository(vault)
        )
    branch = branch_read or ""
    head = head_read or ""
    ref_state = "branch" if branch else "detached" if branch_read == "" and head else "unknown"
    state = "known" if status is not None else "not_repository" if absent else "unknown"
    return GitChip(
        branch=branch,
        head=head,
        committed_at=date_read or "",
        dirty=len(status.splitlines()) if status is not None else None,
        state=state,
        ref_state=ref_state,
    )


def _warn_once(subcommand: str, exc: Exception) -> None:
    # Record only the fixed subcommand and exception type. Exception messages may include a
    # private path or Git stderr; neither belongs in the app log.
    key = (subcommand, type(exc).__name__)
    if key not in _LOGGED_GIT_FAILURES:
        _LOGGED_GIT_FAILURES.add(key)
        _LOGGER.warning("rails: git %s failed (%s)", *key)


def _proved_not_repository(vault: Path) -> bool:
    """A failed Git command alone cannot distinguish no repo from a broken or slow repo."""
    if _has_git_scope_override():
        return False
    try:
        if vault.resolve(strict=True) != vault.absolute():
            return False
        for folder in (vault, *vault.parents):
            try:
                (folder / ".git").lstat()
            except FileNotFoundError:
                continue
            return False  # directory, worktree file, or even a broken marker: uncertain
    except OSError:
        return False
    try:
        winproc.git(vault, "--version", timeout=5.0)
    except Exception:
        return False
    return True


def rails_read(
    vault: Path,
    project_dir: Path,
    *,
    hardening: tuple[str, ...] = (),
    operation: Callable[[Path], AbstractContextManager[tuple[str, ...]]] | None = None,
) -> Rails:
    """Everything the rails show; see the module rules (`read` in the text above)."""
    agent_list: list[AgentInfo] = []
    count = VaultCounts(knowledge=0, sources=0, inbox=0)
    counts_state: Literal["ready", "error"] = "error"
    chip = GitChip(branch="", head="", committed_at="", dirty=None)
    try:
        agent_list = agents(vault, last_runs(project_dir))
    except Exception as exc:
        _LOGGER.warning("rails: agents failed (%s)", type(exc).__name__)
    try:
        count = counts(vault)
        counts_state = "ready"
    except Exception as exc:
        _LOGGER.warning("rails: counts failed (%s)", type(exc).__name__)
    try:
        chip = git_chip(vault, hardening=hardening, operation=operation)
    except Exception as exc:
        # Keep the other rails when an unexpected Git observation step raises outside
        # try_git. The fallback remains unknown and cannot certify a clean vault.
        _LOGGER.warning("rails: git failed (%s)", type(exc).__name__)
    return Rails(
        agents=agent_list,
        gates=[],
        routines=routines(),
        counts=count,
        git=chip,
        counts_state=counts_state,
    )
