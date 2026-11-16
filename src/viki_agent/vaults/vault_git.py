"""Read-only per-note Git history for the vault reader."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent import rails, winproc

MAX_COMMITS = 20
GIT_TIMEOUT_S = 5.0
MAX_SUBJECT_CHARS = 200

_LOGGER = logging.getLogger(__name__)
_FIELD_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class Commit:
    short: str
    at: str
    subject: str


@dataclass(frozen=True)
class NoteHistory:
    commits: tuple[Commit, ...]
    state: Literal["ready", "unknown"]


def note_history(root: Path, ref: str, *, limit: int = MAX_COMMITS) -> NoteHistory:
    """The newest commits touching `ref`. Never raises: a failed git call is `unknown`, and so
    is a Git scope variable in the app's own environment, which could name another repository.
    A vault's own repository settings are nobody's to trust, at any policy, so the read runs in
    `winproc.untrusted_git`."""
    if rails._has_git_scope_override():
        return NoteHistory((), "unknown")
    try:
        # `--` makes a ref such as `-n` a path, never an option, and `--literal-pathspecs` (a main
        # git option, so before the subcommand) makes it a plain path rather than a pathspec, so
        # `*`, `?` and `[...]` in a ref match nothing instead of walking past the name guard.
        with winproc.untrusted_git():
            output = winproc.git(
                root,
                "--literal-pathspecs",
                "log",
                "-n",
                str(limit),
                "--format=%h%x1f%cI%x1f%s",
                "--",
                ref,
                timeout=GIT_TIMEOUT_S,
            )
    except Exception as exc:
        # Only the subcommand and exception type: a message may hold a path, the ref or git stderr.
        _LOGGER.warning("vault: git log failed (%s)", type(exc).__name__)
        return NoteHistory((), "unknown")
    commits: list[Commit] = []
    for line in output.split("\n"):
        fields = line.split(_FIELD_SEPARATOR)
        if len(fields) == 3:
            commits.append(Commit(fields[0], fields[1], fields[2][:MAX_SUBJECT_CHARS]))
    return NoteHistory(tuple(commits), "ready")
