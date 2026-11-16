"""Quick open: a bounded search over the file names Git tracks in a trusted code folder."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from viki_agent.code.code_files import CodeError, is_private
from viki_agent.code.code_git import CodeGit

MAX_QUERY_CHARS = 200
MAX_HITS = 50
MAX_INDEX_BYTES = 33_554_432

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[str, ...]
    truncated: bool


def _unavailable(subcommand: str, exc: Exception) -> CodeError:
    # Only the subcommand and exception type: a message may hold a path or git stderr.
    _LOGGER.warning("code: git %s failed (%s)", subcommand, type(exc).__name__)
    return CodeError("git_unavailable")


def search(git: CodeGit, workspace_id: str, query: str) -> SearchResult:
    """Tracked, non-private paths holding every whitespace-separated token of `query`, case
    folded; paths whose basename holds every token come first, then shorter paths, then by path."""
    query = query.strip()
    if not 1 <= len(query) <= MAX_QUERY_CHARS:
        raise CodeError("invalid_query")
    tokens = query.casefold().split()
    with git.scope():  # one operation: one Git settings check for the query's commands
        root = git.repository(workspace_id)
        try:
            index_path = git.git(root, "rev-parse", "--git-path", "index").rstrip("\r\n")
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            raise _unavailable("rev-parse", exc) from None
        # The index is relative to the root unless absolute (a linked worktree's is elsewhere).
        try:
            size = os.stat(root / index_path).st_size
        except FileNotFoundError:
            return SearchResult((), False)
        except OSError:
            raise CodeError("unavailable") from None
        # With core.splitIndex the index is a stub; the entries sit in a sharedindex.* beside it.
        try:
            shared = (root / index_path).parent.glob("sharedindex.*")
            size += max((path.stat().st_size for path in shared), default=0)
        except OSError:
            raise CodeError("unavailable") from None
        if size > MAX_INDEX_BYTES:
            raise CodeError("too_large")
        try:
            listing = git.git(root, "ls-files", "--cached", "-z")
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            raise _unavailable("ls-files", exc) from None

    ranked: list[tuple[int, int, str]] = []
    for path in listing.split("\0"):
        folded = path.casefold()
        if not path or not all(token in folded for token in tokens) or is_private(path.split("/")):
            continue
        basename = folded.rsplit("/", 1)[-1]
        ranked.append((0 if all(token in basename for token in tokens) else 1, len(path), path))
    ranked.sort()
    return SearchResult(tuple(path for _, _, path in ranked[:MAX_HITS]), len(ranked) > MAX_HITS)
