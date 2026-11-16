"""Bounded, pre-admitted Git settings snapshots for Bot code-folder baselines."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Concatenate

from viki_agent import osfs, rails, winproc
from viki_agent.code.code_files import CodeError, is_private
from viki_agent.storage import SessionStore
from viki_agent.vaults import vault_mutation as handles
from viki_agent.workspaces import WorkspaceService, chain_has_reparse, root_key

if TYPE_CHECKING:
    from viki_agent.code.code_git import CodeGit


def git_operation[**P, T](
    function: Callable[Concatenate[CodeGit, Path, P], T],
) -> Callable[Concatenate[CodeGit, Path, P], T]:
    """Keep multi-command helpers inside their caller's current operation."""

    @wraps(function)
    def run(git: CodeGit, root: Path, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with git.operation(root):
            return function(git, root, *args, **kwargs)

    return run


MAX_FILES = 256
MAX_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_POINTER_BYTES = 4096
_TRACKED = ("creating", "ready", "removing", "kept")
# The private snapshot's HEAD: `onbranch:` includes activate the same way on every branch.
_PLACEHOLDER_HEAD = b"ref: refs/heads/viki-settings-snapshot\n"


def _changed() -> CodeError:
    """Settings differ from the baseline, or changed while they were being read."""
    return CodeError("git_settings_changed")


def _refused() -> CodeError:
    """Settings the guard cannot read safely: unsafe, unsupported, unreadable or over budget."""
    return CodeError("git_settings_unavailable")


def _unsafe_value(value: str) -> bool:
    """Git expands `~` and `%(prefix)`, and on Windows reads `C:x` against that drive's own
    working directory and `/x` against the current drive; the guard follows none of them."""
    windows = PureWindowsPath(value)
    return (
        not value
        or "\0" in value
        or value.startswith(("~", "%("))
        or (
            sys.platform == "win32"
            and bool(windows.drive or windows.root)
            and not windows.is_absolute()
        )
    )


def _stamp(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


@dataclass(frozen=True)
class _File:
    path: Path
    stamp: tuple[int, int, int, int, int, int]
    data: bytes


@dataclass
class _Budget:
    files: int = 0
    size: int = 0
    paths: set[Path] = field(default_factory=set)

    def charge(self, size: int, path: Path) -> None:
        if path in self.paths:
            return
        if self.files >= MAX_FILES or size < 0 or size > MAX_BYTES - self.size:
            raise _refused()
        self.files += 1
        self.size += size
        self.paths.add(path)


def _read(anchor: handles.OrdinaryMutationRoot, path: Path, budget: _Budget) -> _File:
    """Admit metadata and size before opening; open each component without following links."""
    relative = path.relative_to(anchor.path)
    if chain_has_reparse(path, strict=True):
        raise _refused()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _refused()
    budget.charge(before.st_size, path)
    with handles._guard_parent(anchor, relative.parts[:-1]) as (_parent, parent_handle):
        opened = handles._open_attachment(parent_handle, path.name)
        try:
            if not handles.same_final_path(opened, str(path)):
                raise _refused()
            checked = handles._fingerprint_handle(
                opened, unsafe_code="git_settings_changed", max_bytes=before.st_size
            )
            if checked.physical_id != f"{before.st_dev}:{before.st_ino}":
                raise _changed()
            data = handles._read_handle(opened, checked.byte_size)
            if hashlib.sha256(data).hexdigest() != checked.sha256:
                raise _changed()
        finally:
            handles._CloseHandle(opened)
    if _stamp(path.lstat()) != _stamp(before):
        raise _changed()
    return _File(path, _stamp(before), data)


def _raw(folder: Path, git_dir: Path, *args: str) -> str:
    """Fingerprint processes bypass CodeGit, so verification cannot recurse. Git reads the
    system and global config at startup, includes and all, before even `config --file`, so
    these private parses run with that configuration isolated; `--git-dir` names the private
    repository, so no discovery walk reaches another one."""
    from viki_agent.code.code_git import GIT_TIMEOUT_S, HARDENING

    return winproc.git(
        folder,
        *HARDENING,
        "--no-pager",
        f"--git-dir={git_dir}",
        *args,
        timeout=GIT_TIMEOUT_S,
        isolated_config=True,
    )


def _pointer(path: Path) -> str:
    """A `.git` pointer or `commondir` file as Git reads it: only trailing CR/LF stripped."""
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as opened:
        if not stat.S_ISREG(os.fstat(opened.fileno()).st_mode):
            raise _refused()
        data = opened.read(MAX_POINTER_BYTES + 1)
    if len(data) > MAX_POINTER_BYTES:
        raise _refused()
    return data.decode("utf-8").rstrip("\r\n")


def _discovered_common(root: Path) -> Path | None:
    """The common Git directory Git's discovery from `root` reaches, from the first `.git`
    (a folder, or a linked worktree's pointer) or gitdir-shaped folder up the chain and its
    `commondir`; None when there is none. Reads only those small files: no include, no Git."""
    for folder in (root, *root.parents):
        dot = folder / ".git"
        if dot.is_dir():
            git_dir = dot
        elif dot.exists():
            text = _pointer(dot)
            if not text.startswith("gitdir: "):
                raise _refused()
            git_dir = folder / text.removeprefix("gitdir: ")
        elif (folder / "HEAD").is_file() and (folder / "commondir").is_file():
            git_dir = folder
        else:
            continue
        commondir = git_dir / "commondir"
        return git_dir / _pointer(commondir) if commondir.exists() else git_dir
    return None


def _records(raw: str) -> list[tuple[str, str]]:
    if raw and not raw.endswith("\0"):
        raise _refused()
    result: list[tuple[str, str]] = []
    for entry in raw.split("\0")[:-1]:
        key, separator, value = entry.partition("\n")
        if not key:
            raise _refused()
        result.append((key, value if separator else ""))
    return result


class GitGuard:
    def __init__(self, workspaces: WorkspaceService, store: SessionStore) -> None:
        self.workspaces = workspaces
        self.store = store

    def _workspace_id(self, root: Path) -> str | None:
        key = root_key(root)
        for workspace in self.workspaces.list(kind="code"):
            if workspace.root.key == key:
                return workspace.workspace_id
        for row in self.store.worktrees_in_states(_TRACKED):
            if root_key(row["path"]) == key:
                return row["workspace_id"]
        return None

    def bot_copy(self, root: Path) -> bool:
        """Whether `root` is a tracked worktree made as a Bot copy, its release finished or
        not."""
        key = root_key(root)
        rows = self.store.worktrees_in_states(_TRACKED)
        return bool(
            self.store.copy_states(
                [row["worktree_id"] for row in rows if root_key(row["path"]) == key]
            )
        )

    def _shared_baseline(self, root: Path) -> tuple[str, str] | None:
        """The baselined folder and fingerprint whose repository settings Git uses at `root`, a
        folder or worktree without a baseline of its own: a linked worktree of a baselined
        folder shares its config and hooks. `git_settings_unavailable` when undecidable."""
        baselined: dict[str, tuple[str, str]] = {}
        for workspace in self.workspaces.list(kind="code"):
            baseline = self.store.bot_code_baseline(workspace.workspace_id)
            if baseline is not None:
                common = root_key(Path(workspace.root.display) / ".git")
                baselined[common] = workspace.workspace_id, baseline["git_fingerprint"]
        if not baselined:
            return None
        if rails._has_git_scope_override():
            raise _refused()
        try:
            common = _discovered_common(root)
        except Exception:
            raise _refused() from None
        return None if common is None else baselined.get(root_key(common.resolve(strict=False)))

    def verify(self, root: Path) -> None:
        workspace_id = self._workspace_id(root)
        if workspace_id is None:
            return
        baseline = self.store.bot_code_baseline(workspace_id)
        if baseline is not None:
            expected = baseline["git_fingerprint"]
        else:
            shared = self._shared_baseline(root)
            if shared is None:
                return
            workspace_id, expected = shared
        try:
            fingerprint = self.fingerprint(root, workspace_id)
        except CodeError:
            raise _changed() from None
        if fingerprint != expected:
            raise _changed()

    def fingerprint(self, root: Path, workspace_id: str | None = None) -> str:
        """The current settings' hash, `root` read as a worktree of `workspace_id`'s folder
        (by default the folder `root` belongs to). `git_settings_changed` when they changed
        while being read; `git_settings_unavailable` for anything unsafe, unsupported or
        unreadable."""
        try:
            return self._fingerprint(root, workspace_id or self._workspace_id(root))
        except CodeError as exc:
            if exc.code == "git_settings_changed":
                raise _changed() from None
            raise _refused() from None
        except Exception:
            # Config bodies, paths and native stderr have no place in a refusal or log.
            raise _refused() from None

    def _fingerprint(self, root: Path, workspace_id: str | None) -> str:
        if rails._has_git_scope_override():
            raise _refused()
        if workspace_id is None:
            raise _refused()
        workspace = self.workspaces.get(workspace_id, kind="code")
        current = self.workspaces.current_root(workspace)
        if workspace.policy != "ordinary" or not workspace.persistent or current is None:
            raise _refused()
        main = Path(current.display)
        anchor = handles.OrdinaryMutationRoot.from_trusted_code_folder(workspace)
        common = main / ".git"
        # Metadata admission is confined to the registered ordinary root.
        if (
            workspace.identity.git_common_dir
            and root_key(common) != workspace.identity.git_common_dir
        ):
            raise _refused()
        if chain_has_reparse(common, strict=True) or not common.is_dir():
            raise _refused()
        graph_budget = _Budget()
        hook_budget = _Budget()
        files: dict[Path, _File] = {}
        missing: set[Path] = set()
        directories: dict[Path, tuple[int, int]] = {}
        hook_directories: set[Path] = set()
        linked_pointer: tuple[handles.OrdinaryMutationRoot, _File] | None = None

        def admit(path: Path) -> Path:
            # POSIX Git resolves a link before a later `..`; `abspath` alone drops the pair.
            physical = path.resolve(strict=False)
            path = Path(os.path.abspath(path))
            relative = path.relative_to(main)
            if any(part.casefold() in {".claude", ".codex"} for part in relative.parts):
                raise _refused()
            if is_private(tuple(part for part in relative.parts if part.casefold() != ".git")):
                raise _refused()
            for registered in self.workspaces.list(kind=None):
                if registered.policy == "protected":
                    protected = Path(registered.root.display)
                    if path == protected or path.is_relative_to(protected):
                        raise _refused()
            if chain_has_reparse(path):
                raise _refused()
            if root_key(physical) != root_key(path):
                raise _refused()
            return path

        def read(path: Path, budget: _Budget, *, optional: bool = False) -> _File | None:
            path = admit(path)
            if path in files:
                budget.charge(len(files[path].data), path)
                return files[path]
            try:
                found = _read(anchor, path, budget)
            except FileNotFoundError:
                if not optional:
                    raise
                missing.add(path)
                return None
            files[path] = found
            for parent in path.parents:
                if not parent.is_relative_to(main):
                    break
                info = parent.lstat()
                identity = (info.st_dev, info.st_ino)
                if parent in directories and directories[parent] != identity:
                    raise _changed()
                directories[parent] = identity
            return found

        git_dir = common
        # Git reads a relative `core.hooksPath` or `core.attributesFile` at the operation's
        # root; the guard reads it as the main folder resolves it in every root, and never
        # follows a linked worktree's own folder. A Bot copy runs no hooks
        # (`CodeGit.operation`), and attributes only select drivers the framed config defines.
        # An ordinary worktree's hooks run from its own folder, as before: a tree made as a Bot
        # copy stays one after its release, restored from its archive included
        # (`SessionStore.finish_release`), so no ordinary worktree holds files a Bot wrote
        # through its copy.
        if root_key(root) != root_key(main):
            # A tracked linked worktree's pointer is bounded before its target is used.
            if chain_has_reparse(root, strict=True):
                raise _refused()
            from viki_agent.workspaces import canonical_root

            linked_root = canonical_root(str(root), deny=())
            linked_anchor = handles.OrdinaryMutationRoot(
                workspace_id,
                root,
                linked_root.key,
                linked_root.physical_id,
                workspace.policy_revision,
                str(common),
                "code",
            )
            pointer = _read(linked_anchor, root / ".git", graph_budget)
            linked_pointer = linked_anchor, pointer
            # Git strips only trailing CR/LF from the pointer and `commondir`.
            text = pointer.data.decode("utf-8").rstrip("\r\n")
            if not text.startswith("gitdir: ") or "\n" in text:
                raise _refused()
            git_dir = admit(root / text.removeprefix("gitdir: "))
            if not git_dir.is_relative_to(common / "worktrees"):
                raise _refused()
            commondir = read(git_dir / "commondir", graph_budget)
            if (
                commondir is None
                or admit(git_dir / commondir.data.decode("utf-8").rstrip("\r\n")) != common
            ):
                raise _refused()

        config = read(common / "config", graph_budget)
        if config is None:
            raise _refused()
        worktree = read(git_dir / "config.worktree", graph_budget, optional=True)
        roots = [config.path] + ([worktree.path] if worktree is not None else [])
        nodes: dict[Path, Path] = {}
        records: dict[Path, list[tuple[str, str]]] = {}
        edges: dict[Path, list[tuple[str, str, Path]]] = {}
        active: set[Path] = set()
        parser_cost = 0
        digest = hashlib.sha256()

        def name_of(path: Path) -> str:
            # Each worktree has a per-worktree config of its own: named by role, not by path.
            return "config.worktree" if path == git_dir / "config.worktree" else str(path)

        def frame(name: str, value: bytes) -> None:
            label = name.encode("utf-8")
            digest.update(len(label).to_bytes(8, "big"))
            digest.update(label)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        def collect_hooks() -> None:
            hook_paths = {common / "hooks"}
            for parsed in records.values():
                for key, value in parsed:
                    if key in {"core.hookspath", "core.attributesfile"}:
                        if _unsafe_value(value):
                            raise _refused()
                        target = admit(main / value)
                        if key == "core.hookspath":
                            if len(hook_paths) >= MAX_FILES and target not in hook_paths:
                                raise _refused()
                            hook_paths.add(target)
                        else:
                            read(target, hook_budget, optional=True)
            for path in sorted(hook_paths):
                path = admit(path)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    missing.add(path)
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise _refused()
                directories[path] = (info.st_dev, info.st_ino)
                hook_directories.add(path)
                # Charge every entry while enumerating, before retaining a list or its bytes.
                with os.scandir(path) as entries:
                    for entry in entries:
                        read(Path(entry.path), hook_budget)
            read(common / "info" / "attributes", hook_budget, optional=True)

        with tempfile.TemporaryDirectory(prefix="viki-git-settings-") as temporary:
            folder = Path(temporary)
            private_git = folder / ".git"
            private_git.mkdir(mode=0o700)
            (private_git / "objects").mkdir()
            (private_git / "refs").mkdir()
            with osfs.open_new_private(private_git / "HEAD") as target:
                target.write(_PLACEHOLDER_HEAD)

            def discover(path: Path) -> None:
                nonlocal parser_cost
                if path in active:
                    raise _refused()
                if path in nodes:
                    return
                found = read(path, graph_budget)
                if found is None:
                    raise _refused()
                # Native key expansion repeats a section header's name in every key below it.
                # A line yields at most one entry, and an entry's key and value come from its
                # header line and its own lines, so the output is at most the file plus one
                # longest line and framing per line: charged before capturing parser output.
                lines = found.data.split(b"\n")
                parser_cost += len(found.data) + len(lines) * (max(map(len, lines)) + 128)
                if parser_cost > MAX_OUTPUT_BYTES:
                    raise _refused()
                snapshot = private_git / f"source-{len(nodes)}"
                nodes[path] = snapshot
                with osfs.open_new_private(snapshot) as target:
                    target.write(found.data)
                active.add(path)
                parsed = _records(
                    _raw(
                        folder,
                        private_git,
                        "config",
                        "--file",
                        str(snapshot),
                        "--no-includes",
                        "--list",
                        "-z",
                    )
                )
                records[path] = parsed
                edges[path] = []
                for key, value in parsed:
                    if key == "include.path" or (
                        key.startswith("includeif.") and key.endswith(".path")
                    ):
                        if _unsafe_value(value):
                            raise _refused()
                        target_path = admit(path.parent / value)
                        edges[path].append((key, value, target_path))
                        discover(target_path)
                active.remove(path)

            for path in roots:
                discover(path)

            # Unsupported condition contexts are refused, after every target was admitted.
            # onbranch and hasconfig are evaluated by Git against the private placeholder HEAD
            # and config; every conditional target's bytes are framed either way.
            for parsed in records.values():
                for key, _value in parsed:
                    if (
                        key.startswith("includeif.")
                        and key.endswith(".path")
                        and not key.startswith(
                            ("includeif.onbranch:", "includeif.hasconfig:remote.*.url:")
                        )
                    ):
                        raise _refused()

            collect_hooks()

            costs: dict[Path, int] = {}

            def expanded_cost(path: Path) -> int:
                if path in costs:
                    return costs[path]
                snapshot = nodes[path]
                # Every entry carries its key, value, origin type/path and NUL framing.
                cost = sum(
                    len(key.encode("utf-8"))
                    + len(value.encode("utf-8"))
                    + 2 * max(len(str(snapshot).encode("utf-8")), len(str(path).encode("utf-8")))
                    + 128
                    for key, value in records[path]
                )
                for _key, _value, target_path in edges[path]:
                    cost += expanded_cost(target_path)
                    if cost > MAX_OUTPUT_BYTES:
                        raise _refused()
                costs[path] = cost
                return cost

            if 2 * sum(expanded_cost(path) for path in roots) > MAX_OUTPUT_BYTES:
                raise _refused()
            for path, targets in edges.items():
                for key, value, target_path in dict.fromkeys(targets):
                    _raw(
                        folder,
                        private_git,
                        "config",
                        "--file",
                        str(nodes[path]),
                        "--no-includes",
                        "--fixed-value",
                        "--replace-all",
                        key,
                        str(nodes[target_path]),
                        value,
                    )
            for source, name in (
                (config.path, "config"),
                (worktree.path if worktree else None, "config.worktree"),
            ):
                if source is not None:
                    with osfs.open_new_private(private_git / name) as target:
                        target.write(nodes[source].read_bytes())
            origins = {root_key(snapshot): original for original, snapshot in nodes.items()}
            origins[root_key(private_git / "config")] = config.path
            if worktree:
                origins[root_key(private_git / "config.worktree")] = worktree.path

            def normalize(raw: str) -> bytes:
                fields = raw.split("\0")
                if fields[-1] or len(fields) % 2 != 1:
                    raise _refused()
                result: list[str] = []
                for origin, entry in zip(fields[0:-1:2], fields[1:-1:2], strict=True):
                    if not origin.startswith("file:"):
                        raise _refused()
                    original = origins[root_key(origin.removeprefix("file:"))]
                    key, separator, value = entry.partition("\n")
                    if key == "include.path" or (
                        key.startswith("includeif.") and key.endswith(".path")
                    ):
                        value = str(origins[root_key(value)])
                    result.extend((name_of(original), key + separator + value))
                return "\0".join(result).encode("utf-8")

            local = _raw(
                folder,
                private_git,
                "config",
                "--local",
                "--list",
                "--show-origin",
                "--includes",
                "-z",
            )
            frame("local", normalize(local))
            output = _raw(
                folder,
                private_git,
                "config",
                "--worktree",
                "--list",
                "--show-origin",
                "--includes",
                "-z",
            )
            frame("worktree", normalize(output))

        for path, found in sorted(files.items(), key=lambda item: name_of(item[0])):
            if path == git_dir / "commondir":
                continue  # Linked worktree metadata, not a setting.
            # Bytes and mode only: an identical rewrite (`git config` renames a new file over
            # the old one) is no change; races within this read compare the full stamp.
            frame(name_of(path), str(found.stamp[5]).encode("ascii") + found.data)
        for name in sorted(name_of(path) for path in missing):
            frame("missing", name.encode("utf-8"))
        for path, found in files.items():
            again = _read(anchor, path, _Budget())
            if again.stamp != found.stamp or again.data != found.data:
                raise _changed()
        for path in missing:
            admit(path)
            if path.exists():
                raise _changed()
        for path, identity in directories.items():
            info = admit(path).lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise _changed()
            if path in hook_directories:
                seen: set[Path] = set()
                with os.scandir(path) as entries:
                    for entry in entries:
                        if len(seen) >= MAX_FILES:
                            raise _changed()
                        seen.add(Path(entry.path))
                if seen != {p for p in files if p.parent == path}:
                    raise _changed()
        if linked_pointer is not None:
            linked_anchor, pointer = linked_pointer
            again = _read(linked_anchor, pointer.path, _Budget())
            if again != pointer:
                raise _changed()
        if self.workspaces.current_root(workspace) != current:
            raise _changed()
        return digest.hexdigest()
