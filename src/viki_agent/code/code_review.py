"""The review of a checkout: its changed paths, each with what VIKI may do with it, and
the view of one path.

Only a `text` entry can be committed from VIKI, and only it carries a digest: the SHA-256 of
its path, its HEAD blob id and the SHA-256 of its worktree bytes. The commit recomputes the
digests under the lease, so a path that changed after its review is refused. Every other kind
names why VIKI leaves the path to a terminal. A review reads a file's bytes only after an
`lstat` of each part of its path finds no link. Complete previews admit at most
`MAX_PREVIEW_BYTES` a file and `MAX_REVIEW_READ_BYTES` in all; bounded reads charge their actual
bytes, including a possible extra byte to detect growth, and stop once the total is exhausted.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from viki_agent.code.code_checkout import Checkout, checkout, in_progress
from viki_agent.code.code_diff import diff_at
from viki_agent.code.code_files import (
    MAX_PREVIEW_BYTES,
    CodeError,
    is_private,
    read_at,
    split_ref,
)
from viki_agent.code.code_git import CodeGit, StatusRecord, status_records
from viki_agent.code.code_range import (
    _UNFILTERED,
    MAX_BLOB_BYTES,
    Change,
    Integration,
    Kind,
    TreeEntry,
    _binary_by_path,
    filters,
    integration,
    name_kind,
    tree_entries,
)
from viki_agent.code.code_remote import RemoteState, compare_url, select_remote, tracking_oid
from viki_agent.storage import SessionStore
from viki_agent.vaults import vault_mutation

MAX_REVIEW_ENTRIES = 500
MAX_REVIEW_READ_BYTES = 33_554_432

_READ_KINDS: dict[str, Kind] = {
    "too_large": "too_large",
    "binary": "binary",
    "unsupported_encoding": "binary",
}
_REFUSED_KINDS: dict[str, Kind] = {
    "private_path": "private",
    "invalid_path": "other",
    "not_a_file": "other",
    "not_found": "other",
}


@dataclass(frozen=True)
class ReviewEntry:
    path: str
    change: Change
    kind: Kind
    digest: str | None  # `text` only


@dataclass(frozen=True)
class Entries:
    items: tuple[ReviewEntry, ...]
    truncated: bool
    unmerged: bool


@dataclass(frozen=True)
class Review:
    workspace_id: str
    worktree_id: str | None
    branch: str | None
    head_oid: str
    in_progress: bool
    entries: tuple[ReviewEntry, ...]
    truncated: bool
    remote: str | None
    remote_state: RemoteState
    remote_oid: str | None
    pull_request_url: str | None
    integration: Integration | None


@dataclass(frozen=True)
class ReviewView:
    path: str
    diff: str | None
    content: str | None
    reason: str | None


def digest(path: str, head_blob: str | None, worktree_sha256: str | None) -> str:
    """The stale-review anchor of a `text` path."""
    anchor = f"{path}\0{head_blob or '-'}\0{worktree_sha256 or 'absent'}"
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()


def _on_disk(root: Path, path: str) -> tuple[os.stat_result | None, bool]:
    """The `lstat` of the path's last part (None when it is absent), and whether a part on the
    way is a link or reparse point, where the walk stops."""
    current = root
    info: os.stat_result | None = None
    for part in path.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError, NotADirectoryError:
            return None, False
        except OSError:
            raise CodeError("unavailable") from None
        if vault_mutation._is_reparse(info):
            return info, True
    return info, False


def _listed_on_disk(records: Sequence[StatusRecord]) -> bool:
    """Whether Git sees the path on disk: for a name settled without touching the filesystem."""
    return any(record.type == "?" for record in records) or "D" not in records[0].fields[1]


def _git_kind(
    path: str,
    records: Sequence[StatusRecord],
    entry: TreeEntry | None,
    present: bool,
    binary: dict[str, bool],
    attribute: str,
    linked: bool,
) -> Kind | None:
    """The kind Git's answers settle, in the review's order after the name; None when the
    worktree bytes decide. A HEAD tree at `path` settles "other" only when `path` is not itself
    a link on disk: a tracked folder replaced by a link falls through to the caller's
    `kind is None and linked` -> "symlink"."""
    if any(record.type == "u" for record in records):
        return "unmerged"
    tracked = [record for record in records if record.type != "?"]
    modes = {mode for record in tracked for mode in record.fields[3:6]}
    if "160000" in modes or any(record.fields[2].startswith("S") for record in tracked):
        return "submodule"
    if "120000" in modes:
        return "symlink"
    if (
        (tracked and len(tracked) != len(records))
        or (entry is not None and entry.type == "tree" and not linked)
        or (entry is None and not present)
        or (tracked and path not in binary)
    ):
        return "other"
    if binary.get(path):
        return "binary"
    if entry is not None and (entry.size or 0) > MAX_BLOB_BYTES:
        return "too_large"
    if attribute not in _UNFILTERED:
        return "filtered"
    return None


class CodeReview:
    def __init__(self, store: SessionStore, git: CodeGit) -> None:
        self._store = store
        self._git = git

    def entries(self, checkout: Checkout, *, only: Collection[str] | None = None) -> Entries:
        """The checkout's changed paths, first seen first: the first `MAX_REVIEW_ENTRIES`, or
        with `only` exactly those of them that are changes, uncapped."""
        root = checkout.root
        raw = self._git.output(
            root, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--no-renames"
        )
        try:
            _, records = status_records(raw)
        except ValueError:
            raise CodeError("git_failed") from None
        grouped: dict[str, list[StatusRecord]] = {}
        for record in records:
            grouped.setdefault(record.path, []).append(record)
        unmerged = any(record.type == "u" for record in records)
        if only is None:
            paths = list(grouped)[:MAX_REVIEW_ENTRIES]
            truncated = len(grouped) > MAX_REVIEW_ENTRIES
        else:
            wanted = set(only)
            paths = [path for path in grouped if path in wanted]
            truncated = False
        if not paths:
            return Entries((), truncated, unmerged)

        # A nested repository (`? dir/`) and a name the reader refuses are settled by the
        # path alone, before any filesystem call on it.
        settled: dict[str, Kind] = {}
        for path in paths:
            named = "nested_repository" if path.endswith("/") else name_kind(path)
            if named is not None:
                settled[path] = named
        rest = [path for path in paths if path not in settled]
        head = self._head_entries(root, [path for path in paths if not path.endswith("/")])
        binary: dict[str, bool] = {}
        attributes: dict[str, str] = {}
        if rest:
            binary = _binary_by_path(
                self._git.output(
                    root,
                    *("diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv"),
                    *("HEAD", "--"),
                )
            )
            attributes = filters(self._git, root, rest)

        items: list[ReviewEntry] = []
        spent = 0

        def charge(amount: int) -> None:
            nonlocal spent
            spent += amount

        for path in paths:
            entry = head.get(path)
            if path in settled:
                present = _listed_on_disk(grouped[path])
                items.append(ReviewEntry(path, _change(entry, present), settled[path], None))
                continue
            info, linked = _on_disk(root, path)
            present = info is not None
            change = _change(entry, present)
            kind = _git_kind(path, grouped[path], entry, present, binary, attributes[path], linked)
            if kind is None and linked:
                kind = "symlink"
            sha256: str | None = None
            if kind is None and info is not None:
                size = info.st_size
                remaining = MAX_REVIEW_READ_BYTES - spent
                if size > MAX_PREVIEW_BYTES or size > remaining or remaining <= 0:
                    kind = "too_large"
                else:
                    kind, sha256 = _read(root, path, max_bytes=remaining, on_read=charge)
            head_blob = entry.oid if entry is not None else None
            items.append(
                ReviewEntry(
                    path,
                    change,
                    kind or "text",
                    digest(path, head_blob, sha256) if kind is None else None,
                )
            )
        return Entries(tuple(items), truncated, unmerged)

    def _head_entries(self, root: Path, paths: list[str]) -> dict[str, TreeEntry]:
        """The HEAD entries of `paths`. `ls-tree` descends into a folder when a path under it
        is asked too, and then leaves the folder's own entry out, so a path that is a folder
        of another asked path (a tree in HEAD replaced by a file on disk) waits for a later
        round. The deepest paths go first, so each round asks at least one."""
        found: dict[str, TreeEntry] = {}
        while paths:
            folders = {
                "/".join(parts[:depth])
                for parts in (path.split("/") for path in paths)
                for depth in range(1, len(parts))
            }
            asked = [path for path in paths if path not in folders]
            found.update(tree_entries(self._git, root, "HEAD", asked))
            paths = [path for path in paths if path in folders]
        return found

    def review(self, workspace_id: str, worktree_id: str | None) -> Review:
        remote_name: str | None = None
        state: RemoteState = "none"
        remote_oid: str | None = None
        page: str | None = None
        # One operation: the folder and the worktree each get one Git settings check, however
        # many commands the review runs in them.
        with self._git.scope():
            found = checkout(self._store, self._git, workspace_id, worktree_id)
            entries = self.entries(found)
            progress = in_progress(self._git, found.root) or entries.unmerged
            integrated = None
            if worktree_id is not None:
                integrated = integration(self._git, found.folder, found.head_oid)
            if found.branch is not None:
                remote, state = select_remote(self._git, found.root, found.branch)
                if remote is not None:
                    remote_name = remote.name
                    remote_oid = tracking_oid(self._git, found.root, remote.name, found.branch)
                    if integrated is not None and integrated.target_branch is not None:
                        page = compare_url(remote.url, found.branch, integrated.target_branch)
        return Review(
            workspace_id=workspace_id,
            worktree_id=worktree_id,
            branch=found.branch,
            head_oid=found.head_oid,
            in_progress=progress,
            entries=entries.items,
            truncated=entries.truncated,
            remote=remote_name,
            remote_state=state,
            remote_oid=remote_oid,
            pull_request_url=page,
            integration=integrated,
        )

    def view(
        self, workspace_id: str, path: str, worktree_id: str | None, *, integration: bool
    ) -> ReviewView:
        """One path of a review: its diff against HEAD when HEAD holds it, else its content;
        with `integration`, its change from the folder's head to the worktree's head."""
        parts = split_ref(path)
        if not parts:
            raise CodeError("invalid_path")
        if is_private(parts):
            raise CodeError("private_path")
        if integration and worktree_id is None:
            raise ValueError("an integration view needs a worktree")
        with self._git.scope():  # one operation, as `review`
            found = checkout(self._store, self._git, workspace_id, worktree_id)
            if integration:
                between = (self._git.head(found.folder), found.head_oid)
                shown = diff_at(self._git, found.folder, path, between=between)
                return ReviewView(path, shown.text, None, shown.reason)
            if self._git.answer(found.root, "cat-file", "-e", f"HEAD:{path}") is not None:
                shown = diff_at(self._git, found.root, path)
                return ReviewView(path, shown.text, None, shown.reason)
            text = read_at(found.root, path)
            return ReviewView(path, None, text.text, text.reason)


def _change(entry: TreeEntry | None, present: bool) -> Change:
    if entry is not None:
        return "modified" if present else "deleted"
    return "added"


def _read(
    root: Path, path: str, *, max_bytes: int, on_read: Callable[[int], None]
) -> tuple[Kind | None, str | None]:
    """The kind the worktree bytes give (None for text) and their SHA-256."""
    try:
        text = read_at(root, path, max_bytes=max_bytes, on_read=on_read)
    except CodeError as refused:
        if refused.code not in _REFUSED_KINDS:
            raise  # `unavailable`
        return _REFUSED_KINDS[refused.code], None
    if text.reason is not None:
        return _READ_KINDS[text.reason], None
    return None, text.sha256
