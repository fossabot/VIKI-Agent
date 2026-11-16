"""The remote a code folder's branch pushes to, and the pull request page for it.

A remote's URL can hold a user name or a token. It lives only in `Remote.url`, kept out of
`repr`; it is never returned, logged or put into an error. Only `compare_url`'s output leaves,
and it holds the host and path alone.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit
from viki_agent.code.worktrees import valid_branch

REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
LS_REMOTE_TIMEOUT_S = 30.0
DEFAULT_REMOTE = "origin"

RemoteState = Literal["ready", "none", "unsupported"]

_LOGGER = logging.getLogger(__name__)
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_LS_LINE = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64})\t([^\t]+)")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HOST = r"(?P<host>(?i:github\.com|gitlab\.com))"
# Lazy, so a trailing `.git` and `/` are left for the optional parts; the segments are checked
# afterwards.
_PATH = r"(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?"
_FORMS = (
    re.compile(rf"https://(?:[^@/?#\s]*@)?{_HOST}(?::443)?/{_PATH}/?"),
    re.compile(rf"git@{_HOST}:{_PATH}"),
    re.compile(rf"ssh://git@{_HOST}(?::[0-9]{{1,5}})?/{_PATH}/?"),
)
_NOT_SEGMENTS = (".", "..", "-")


@dataclass(frozen=True)
class Remote:
    name: str
    url: str = field(repr=False)


def _check_branch(branch: str) -> None:
    # A branch name reaches a ref argument: only names VIKI can name get that far.
    if not valid_branch(branch):
        raise CodeError("unsupported_branch")


def _check_remote(remote: str) -> None:
    if REMOTE_NAME.fullmatch(remote) is None:
        raise CodeError("remote_unsupported")


def _push_failed(exc: Exception) -> CodeError:
    if isinstance(exc, CodeError) and exc.code == "git_settings_changed":
        return exc
    # Only the subcommand and exception type: a message may hold the remote's URL.
    _LOGGER.warning("code: git ls-remote failed (%s)", type(exc).__name__)
    return CodeError("push_failed")


def _one_line(answer: str | None) -> str | None:
    lines = [] if answer is None else answer.splitlines()
    return lines[0] if len(lines) == 1 else None


def select_remote(git: CodeGit, root: Path, branch: str) -> tuple[Remote | None, RemoteState]:
    """The remote `branch` pushes to: its `branch.<b>.remote`, else `origin`. `ready` only for
    one fetch URL that the push URLs repeat exactly and a remote that is not a mirror."""
    _check_branch(branch)
    with git.operation(root):
        return _select_remote(git, root, branch)


def _select_remote(git: CodeGit, root: Path, branch: str) -> tuple[Remote | None, RemoteState]:
    answer = git.answer(root, "config", "--get", f"branch.{branch}.remote")
    configured = answer is not None
    name = DEFAULT_REMOTE if answer is None else _one_line(answer)
    if name is None or REMOTE_NAME.fullmatch(name) is None:
        return None, "unsupported"
    fetch = git.answer(root, "remote", "get-url", "--all", name)
    if fetch is None:
        return None, "unsupported" if configured else "none"
    push = git.answer(root, "remote", "get-url", "--push", "--all", name)
    mirror = git.answer(root, "config", "--type=bool", "--get", f"remote.{name}.mirror")
    urls = fetch.splitlines()
    if len(urls) != 1 or not urls[0] or push is None or push.splitlines() != urls:
        return None, "unsupported"
    if mirror is not None and mirror.strip() == "true":
        return None, "unsupported"
    return Remote(name, urls[0]), "ready"


def tracking_oid(git: CodeGit, root: Path, remote: str, branch: str) -> str | None:
    """The local `refs/remotes/<remote>/<branch>` commit, None when it does not exist."""
    _check_remote(remote)
    _check_branch(branch)
    answer = git.answer(
        root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}^{{commit}}"
    )
    if answer is None:
        return None
    oid = _one_line(answer)
    if oid is None or _OID.fullmatch(oid) is None:
        raise CodeError("git_failed")
    return oid


def remote_oid(git: CodeGit, root: Path, remote: str, branch: str) -> str | None:
    """The commit the remote's `refs/heads/<branch>` is at, None when it is absent. Any failure
    to ask (unreachable, timeout, a line that is not `<oid> TAB <ref>`) is `push_failed`."""
    _check_remote(remote)
    _check_branch(branch)
    ref = f"refs/heads/{branch}"
    try:
        raw = git.git(root, "ls-remote", remote, ref, timeout=LS_REMOTE_TIMEOUT_S)
    except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
        raise _push_failed(exc) from None
    found: str | None = None
    for line in raw.splitlines():
        match = _LS_LINE.fullmatch(line)
        if match is None:
            raise _push_failed(ValueError("malformed line"))
        # The pattern also matches a longer ref that ends in it: keep the exact one.
        if match[2] == ref:
            if found is not None:
                raise _push_failed(ValueError("duplicate ref"))
            found = match[1]
    return found


def compare_url(url: str, branch: str, base: str) -> str | None:
    """The host's page to open a pull request from `branch`, built from github.com's or
    gitlab.com's host and path alone; None for any other URL. No user name or token reaches
    the result."""
    if branch == base or not valid_branch(branch) or not valid_branch(base):
        return None
    found = next((match for form in _FORMS if (match := form.fullmatch(url))), None)
    if found is None:
        return None
    path = found["path"]
    segments = path.split("/")
    if any(s in _NOT_SEGMENTS or _SEGMENT.fullmatch(s) is None for s in segments):
        return None
    if found["host"].lower() == "github.com":
        if len(segments) != 2:
            return None
        return f"https://github.com/{path}/compare/{base}...{branch}?quick_pull=1"
    if not 2 <= len(segments) <= 5:
        return None
    source = urllib.parse.quote(branch, safe="")
    return (
        f"https://gitlab.com/{path}/-/merge_requests/new?merge_request%5Bsource_branch%5D={source}"
    )
