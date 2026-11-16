"""The worktree cleanup policy: a deterministic `decide` for one row, no Git or disk I/O.

Pure: no I/O, no clock reads. `worktree_probe` gathers `Facts` from Git and disk; `decide` only
reads what it is given. Plan §4 (`docs/work/worktree-lifecycle.md`) names the decision order and
the constants below verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

AUTO_BRANCH = re.compile(r"viki/\d{8}-[0-9a-f]{4}")
SWEEP_INTERVAL_S = 21600
ARCHIVE_DAYS = 30
MAX_UNTRACKED_FILE = 50 * 1024 * 1024
MAX_UNTRACKED_TOTAL = 200 * 1024 * 1024
MAX_WALK_ENTRIES = 1_000_000
WALK_TIMEOUT_S = 30

# The 18 outcomes `decide` can return, plus the four the act phase (sweep) alone can report.
REASONS = (
    "unknown",
    "untrusted",
    "live",
    "bot_chat",
    "locked",
    "missing",
    "recent",
    "submodule",
    "embedded_repo",
    "worktree_refs",
    "hidden_changes",
    "in_progress",
    "branch_changed",
    "merged",
    "filtered",
    "large_untracked",
    "dirty",
    "unpushed",
    "busy",
    "changed",
    "in_use",
    "git_refused",
)


def tier(branch: str) -> timedelta:
    """The idle window before a worktree on *branch* stops being `recent`: a day for an
    auto-named branch (`viki/yyyymmdd-hex4`), three days for anything named by hand."""
    return timedelta(hours=24) if AUTO_BRANCH.fullmatch(branch) else timedelta(hours=72)


@dataclass(frozen=True)
class Facts:
    """What `worktree_probe` read from one worktree's Git state and disk, or the all-false/zero
    defaults when nothing could be read (`readable=False`).

    `activity` is the newest mtime of every file and folder (links and junctions not followed)
    and of the per-worktree `HEAD`, `index` and `logs/HEAD`. `bytes` is the tree's total disk
    usage; None means the disk walk did not finish (a budget tripped, a folder could not be
    listed, or an entry could not be stat'ed), so `decide` keeps the row `unknown` rather than
    trust a partial scan. `filtered` means an archive could not hold the tree as it is: Git ran
    there without a filter driver (`CodeGit.unfiltered`, a Bot copy) that a changed file is
    under, or that would rewrite files when the archive is checked out again.
    """

    readable: bool = False
    head: str | None = None
    branch: str | None = None
    index_digest: str | None = None
    changes: int = 0
    unmerged: bool = False
    submodule: bool = False
    embedded: bool = False
    worktree_refs: bool = False
    hidden: bool = False
    filtered: bool = False
    unpushed: int = 0
    untracked_max: int = 0
    untracked_total: int = 0
    activity: datetime | None = None
    bytes: int | None = None


@dataclass(frozen=True)
class Decision:
    """One row's cleanup verdict: `action` is `remove`, `archive` or `keep`; `reason` is one of
    `REASONS`. `idle_since` and `eligible_at` are `session.now_iso`-style stamps of the
    worktree's last activity and of when its tier makes it eligible, or None before activity is
    known."""

    worktree_id: str
    action: Literal["remove", "archive", "keep"]
    reason: str
    idle_since: str | None
    eligible_at: str | None
    bytes: int | None


def decide(
    worktree_id: str,
    row_branch: str,
    *,
    in_area: bool,
    trusted: bool,
    live: bool,
    locked: bool,
    present: bool,
    facts: Facts | None,
    now: datetime,
    copy: Literal["bound", "released"] | None = None,
) -> Decision:
    """The cleanup verdict for one row, by Plan §4's decision order: the first matching check
    wins. `copy` is the row's Bot-copy binding: a `bound` copy is kept for its chat even when
    nothing runs in it; a `released` one skips only the idle window (`recent`)."""
    if not in_area:
        return _keep(worktree_id, "unknown")
    if not trusted:
        return _keep(worktree_id, "untrusted")
    if live:
        return _keep(worktree_id, "live")
    if copy == "bound":
        return _keep(worktree_id, "bot_chat")
    if locked:
        return _keep(worktree_id, "locked")
    if not present:
        return Decision(worktree_id, "remove", "missing", None, None, None)  # branch kept
    if facts is None or not facts.readable or facts.activity is None or facts.bytes is None:
        return _keep(worktree_id, "unknown")

    activity = facts.activity
    idle_since = _stamp(activity)
    eligible = activity + tier(row_branch)
    eligible_at = _stamp(eligible)
    size = facts.bytes

    if eligible > now and copy != "released":
        return _keep(worktree_id, "recent", idle_since, eligible_at, size)
    if facts.submodule:
        return _keep(worktree_id, "submodule", idle_since, eligible_at, size)
    if facts.embedded:
        return _keep(worktree_id, "embedded_repo", idle_since, eligible_at, size)
    if facts.worktree_refs:
        return _keep(worktree_id, "worktree_refs", idle_since, eligible_at, size)
    if facts.hidden:
        return _keep(worktree_id, "hidden_changes", idle_since, eligible_at, size)
    if facts.unmerged:
        return _keep(worktree_id, "in_progress", idle_since, eligible_at, size)
    if facts.branch is None or facts.branch != row_branch:
        return _keep(worktree_id, "branch_changed", idle_since, eligible_at, size)
    if facts.changes == 0 and facts.unpushed == 0:
        return Decision(worktree_id, "remove", "merged", idle_since, eligible_at, size)
    if facts.filtered:
        # Past `merged`: a removal stores nothing, so only an archive needs the filter.
        return _keep(worktree_id, "filtered", idle_since, eligible_at, size)
    if facts.untracked_max > MAX_UNTRACKED_FILE or facts.untracked_total > MAX_UNTRACKED_TOTAL:
        return _keep(worktree_id, "large_untracked", idle_since, eligible_at, size)
    reason = "dirty" if facts.changes > 0 else "unpushed"
    return Decision(worktree_id, "archive", reason, idle_since, eligible_at, size)


def _keep(
    worktree_id: str,
    reason: str,
    idle_since: str | None = None,
    eligible_at: str | None = None,
    size: int | None = None,
) -> Decision:
    return Decision(worktree_id, "keep", reason, idle_since, eligible_at, size)


def _stamp(moment: datetime) -> str:
    """*moment* in `session.now_iso`'s format."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
