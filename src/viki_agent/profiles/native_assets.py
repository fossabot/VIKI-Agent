"""Read-only, allowlisted inspection of Claude native agents and skills."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from viki_agent.domain import ActiveWorkspace, CanonicalRoot, Workspace
from viki_agent.vaults.vault import VaultError, _is_reparse, _physical_id
from viki_agent.workspaces import WorkspaceError, canonical_root

MAX_ENTRIES = 256
MAX_TOTAL_BYTES = 1024 * 1024
MAX_FILE_BYTES = 128 * 1024
MAX_DEADLINE_SECONDS = 2.0
MAX_DESCRIPTION_BYTES = 2048

PartialReason = Literal[
    "deadline",
    "entry_limit",
    "byte_limit",
    "oversized",
    "unsafe_entry",
    "unreadable",
    "changed",
    "invalid_encoding",
    "malformed_metadata",
]
_DENIED_NAMES = (".env", "secret", "secrets", "credential", "credentials", "settings")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_READ_KEYS = frozenset({"name", "description", "model", "tools"})
# A carriage return, U+0085, U+2028 and U+2029 may end a YAML line where this reader and the
# CLI's retry see none, and YAML admits no other C0 or C1 control, no DEL, and neither U+FFFE nor
# U+FFFF.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufffe\uffff]")
# The YAML 1.2 core schema's null, boolean, integer and float forms: the CLI reads an unquoted
# one as that type, not as text, and refuses an agent whose name or description is one.
_YAML_SCALAR = re.compile(
    r"~|null|Null|NULL|true|True|TRUE|false|False|FALSE"
    r"|[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+"
    r"|[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?"
    r"|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)"
)


class NativeAssetError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NativeAsset:
    source_id: str
    source_scope: Literal["workspace", "user"]
    provider: Literal["claude"]
    relative_id: str
    kind: Literal["agent", "skill"]
    name: str
    description: str | None
    model: str | None
    tools: tuple[str, ...]
    digest: str
    observed_at: str
    supported_fields: tuple[str, ...]
    unsupported_fields: tuple[str, ...]
    launch_supported: Literal[False] = False


@dataclass(frozen=True)
class NativeAssetCatalog:
    source_id: str
    provider: Literal["claude"]
    source_scope: Literal["workspace", "user"]
    assets: tuple[NativeAsset, ...]
    partial: bool
    partial_reasons: tuple[PartialReason, ...]
    scanned_entries: int
    scanned_bytes: int
    omitted: int


class WorkspaceReader(Protocol):
    def resolve_active(self) -> ActiveWorkspace: ...

    def get(self, workspace_id: str) -> Workspace: ...

    def current_root(self, workspace: Workspace) -> CanonicalRoot | None: ...


@dataclass
class _Budget:
    expires_at: float
    entries: int = 0
    bytes: int = 0
    omitted: int = 0
    reasons: set[PartialReason] = field(default_factory=set)

    def before_work(self) -> bool:
        if time.monotonic() >= self.expires_at:
            self.stop("deadline")
            return False
        return True

    def visit(self) -> bool:
        if not self.before_work():
            return False
        if self.entries >= MAX_ENTRIES:
            self.stop("entry_limit")
            return False
        self.entries += 1
        return True

    def reason(self, reason: PartialReason, *, omitted: int = 1) -> None:
        self.reasons.add(reason)
        self.omitted += omitted

    def stop(self, reason: PartialReason) -> None:
        if reason not in self.reasons:
            self.reason(reason)


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_name(name: str) -> bool:
    lowered = name.casefold()
    if not name or name in {".", ".."} or name.startswith(".") or "\\" in name or "/" in name:
        return False
    stem = lowered[:-3] if lowered.endswith(".md") else lowered
    return not any(
        stem == denied or stem.startswith((f"{denied}.", f"{denied}-", f"{denied}_"))
        for denied in _DENIED_NAMES
    )


if sys.platform == "win32":
    from ctypes import wintypes

    from viki_agent.vaults.vault import (
        _ByHandleInfo,
        _handle_info,
        _open_ancestor_chain,
        _open_directory,
        _open_note,
        _ReadFile,
    )

    # A folder is a path whose ancestors are held open without delete sharing.
    _Folder = Path
    _open_folder = _open_ancestor_chain

    @contextmanager
    def _open_subfolder(folder: Path, name: str) -> Iterator[tuple[Path, os.stat_result]]:
        child = folder / name
        with _open_directory(child):
            yield child, child.lstat()

    def _lstat_at(folder: Path, name: str) -> os.stat_result:
        return (folder / name).lstat()

    def _handle_identity(info: _ByHandleInfo) -> tuple[int, int]:
        volume = int(info.volume)
        index = (int(info.index_high) << 32) | int(info.index_low)
        return volume, index

    def _read_exact(folder: Path, name: str, expected: os.stat_result) -> bytes:
        with _open_note(folder / name) as handle:
            before = _handle_info(handle)
            size = (int(before.size_high) << 32) | int(before.size_low)
            if (
                size != expected.st_size
                or _handle_identity(before)[1] != expected.st_ino
                or size > MAX_FILE_BYTES
            ):
                raise NativeAssetError("oversized" if size > MAX_FILE_BYTES else "changed")
            data = bytearray()
            while len(data) < size:
                amount = min(size - len(data), 64 * 1024)
                buffer = ctypes.create_string_buffer(amount)
                read = wintypes.DWORD()
                if not _ReadFile(handle, buffer, amount, ctypes.byref(read), None):
                    raise OSError(ctypes.get_last_error(), "ReadFile")
                if read.value == 0:
                    raise NativeAssetError("changed")
                data.extend(buffer.raw[: read.value])
            after = _handle_info(handle)
            if (
                _handle_identity(after) != _handle_identity(before)
                or ((int(after.size_high) << 32) | int(after.size_low)) != size
            ):
                raise NativeAssetError("changed")
            return bytes(data)

else:
    from viki_agent.vaults.vault import (
        _DIRECTORY_FLAGS,
        _open_dir_at,
        _open_root_fd,
        _read_file_at,
    )

    # A folder is a directory descriptor; every open below it is relative to it, so a rename
    # or a symlink swapped in mid-scan cannot redirect a read.
    _Folder = int

    @contextmanager
    def _open_folder(root: CanonicalRoot, parts: tuple[str, ...]) -> Iterator[int]:
        # The fixed names on the way down are opened without the reader's name check: on macOS
        # that check refuses `.claude`, the very folder this reads.
        with _open_root_fd(root) as folder, ExitStack() as stack:
            for part in parts:
                folder = os.open(part, _DIRECTORY_FLAGS, dir_fd=folder)
                stack.callback(os.close, folder)
            yield folder

    @contextmanager
    def _open_subfolder(folder: int, name: str) -> Iterator[tuple[int, os.stat_result]]:
        child = _open_dir_at(folder, name)
        try:
            yield child, os.fstat(child)
        finally:
            os.close(child)

    def _lstat_at(folder: int, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)

    def _read_exact(folder: int, name: str, expected: os.stat_result) -> bytes:
        try:
            data, info = _read_file_at(folder, name, MAX_FILE_BYTES)
        except VaultError as exc:
            if exc.code == "note_too_large":
                raise NativeAssetError("oversized") from exc
            raise
        if _physical_id(info) != _physical_id(expected) or info.st_size != expected.st_size:
            raise NativeAssetError("changed")
        return data


# The CLI's frontmatter opening, `^---\s*\n`: `---`, any JavaScript white space, a line feed.
_OPENING = re.compile(
    r"---[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n"
)
# Characters YAML gives a meaning at the start of a plain value.
_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
# A key whose line the CLI's retry rewrites, quoting a plain value that holds `: `.
_RETRY_KEY = re.compile(r"[A-Za-z_-]+")
# A quoted scalar YAML reads as the text between its quotes: no backslash, no inner quote of its
# own kind.
_QUOTED = re.compile(r"\"[^\"\\]*\"|'[^']*'")


def _reads_as_written(value: str, requoted: bool) -> bool:
    """Whether YAML reads a key line's value, the text after the colon and its spaces and tabs,
    as written on that line: empty, one closed quoted scalar or a JSON list of strings with
    nothing after it, or a plain value with no indicator at its start, no other white space at
    either end (the CLI trims it from a model or a tool), no trailing `:`, no comment, and `: `
    only where the CLI's retry quotes it.

    No value that passes opens a node the lines below could join. Nothing may follow a quote or a
    list, not even a space or a tab. The CLI's retry reads a list, or a quote holding `: ` or one
    of ``{}[]*&#!|>%@` ``, followed by anything as one string, quotes and brackets included. After
    any other quote a space is refused only to keep the rule simple."""
    if _QUOTED.fullmatch(value):
        return True
    if value.startswith("["):
        if "\\" in value or not value.endswith("]"):
            return False
        try:
            parsed = json.loads(value)
        except ValueError, RecursionError:
            return False
        return isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)
    value = value.rstrip(" \t")
    return not value or (
        value[0] not in _INDICATORS
        and value == value.strip()
        and "\ufeff" not in (value[0], value[-1])
        and not value.endswith(":")
        and not any(mark in value for mark in (":\t", " #", "\t#"))
        and (": " not in value or requoted)
    )


def _frontmatter(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    opening = _OPENING.match(text)
    if opening is None:
        return {}, ()
    # The CLI's frontmatter ends at the first `---` after the opening, mid-line included.
    rest = text[opening.end() :]
    end = rest.find("---")
    lines = rest[:end].split("\n", 256)
    if end == -1 or len(lines) > 256:
        raise NativeAssetError("malformed_metadata")
    values: dict[str, str] = {}
    unsupported: list[str] = []
    for line in lines:
        # A line ends at a line feed. One carriage return at a line's end, the block's last line
        # included, is dropped; any other carriage return is refused as an unsafe character.
        crlf = line.endswith("\r")
        line = line.removesuffix("\r")
        if _UNSAFE.search(line):
            raise NativeAssetError("malformed_metadata")
        # Blank and comment lines are skipped (YAML white space is a space or a tab).
        if not line.strip(" \t") or line.lstrip(" \t").startswith("#"):
            continue
        # Every other line is a key at column 0, a colon, then a space, a tab or the line's end.
        name, colon, value = line.partition(":")
        if (
            not colon
            or value[:1] not in ("", " ", "\t")
            or _FIELD.fullmatch(name) is None
            or name in values
        ):
            raise NativeAssetError("malformed_metadata")
        value = value.lstrip(" \t")
        # After a failed parse the CLI parses again with each plain value holding `: ` quoted,
        # but only on a line whose key is letters, `_` and `-` and that ends in no carriage return.
        requoted = not crlf and _RETRY_KEY.fullmatch(name) is not None
        if not _reads_as_written(value, requoted):
            raise NativeAssetError("malformed_metadata")
        values[name] = value.rstrip(" \t")
        if name not in _READ_KEYS:
            unsupported.append(name)
    return values, tuple(sorted(unsupported, key=str.casefold))


def _plain(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if not value or len(value.encode("utf-8")) > maximum or any(ord(char) < 32 for char in value):
        raise NativeAssetError("malformed_metadata")
    return value


def _tools(value: str | None) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError, RecursionError:
            raise NativeAssetError("malformed_metadata") from None
        if not isinstance(parsed, list):
            raise NativeAssetError("malformed_metadata")
        items: Iterable[object] = parsed
    else:
        items = value.split(",")
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise NativeAssetError("malformed_metadata")
        tool = item.strip().strip("\"'")
        if not tool or not tool.isprintable() or tool in result:
            raise NativeAssetError("malformed_metadata")
        if len(tool.encode("utf-8")) > 128:
            raise NativeAssetError("malformed_metadata")
        result.append(tool)
        if len(result) > 64:
            raise NativeAssetError("malformed_metadata")
    return tuple(result)


SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RESERVED_SKILL_NAMES = frozenset({"synced", "anthropic-skills"})
MAX_SKILL_DESCRIPTION_CHARS = 1536


@dataclass(frozen=True)
class SkillHead:
    name: str
    description: str
    unsupported_fields: tuple[str, ...]


def _valid_skill_name(name: str) -> bool:
    return len(name) <= 64 and SKILL_NAME.fullmatch(name) is not None and _safe_name(name)


def parse_skill_text(text: str) -> SkillHead:
    """Validate a `SKILL.md`'s frontmatter the way a skill proposal must read to be installed."""
    if _OPENING.match(text) is None:
        raise NativeAssetError("missing_frontmatter")
    values, unsupported = _frontmatter(text)
    name = _plain(values.get("name"), 128)
    if name is None:
        raise NativeAssetError("missing_name")
    if not _valid_skill_name(name):
        raise NativeAssetError("invalid_name")
    if name in RESERVED_SKILL_NAMES:
        raise NativeAssetError("reserved_name")
    description = _plain(values.get("description"), MAX_FILE_BYTES)
    if description is None:
        raise NativeAssetError("missing_description")
    if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
        raise NativeAssetError("long_description")
    return SkillHead(name, description, unsupported)


class NativeAssetInspector:
    def __init__(
        self,
        workspaces: WorkspaceReader,
        *,
        claude_home: CanonicalRoot | None,
    ) -> None:
        self._workspaces = workspaces
        self._claude_home = claude_home

    def inspect(self, source_id: str) -> NativeAssetCatalog:
        root, prefix, scope, workspace = self._source(source_id)
        budget = _Budget(time.monotonic() + MAX_DEADLINE_SECONDS)
        assets: list[NativeAsset] = []
        observed = _stamp()
        self._scan_agents(root, prefix, source_id, scope, observed, budget, assets)
        if budget.before_work():
            self._scan_skills(root, prefix, source_id, scope, observed, budget, assets)
        try:
            with _open_folder(root, ()):
                pass
        except (VaultError, OSError) as exc:
            raise NativeAssetError("source_changed") from exc
        if workspace is not None:
            active = self._workspaces.resolve_active()
            latest = self._workspaces.get(workspace.workspace_id)
            if (
                active.chat_only
                or active.workspace.workspace_id != workspace.workspace_id
                or latest != workspace
                or self._workspaces.current_root(latest) != root
            ):
                raise NativeAssetError("source_changed")
        reasons = tuple(
            reason
            for reason in (
                "deadline",
                "entry_limit",
                "byte_limit",
                "oversized",
                "unsafe_entry",
                "unreadable",
                "changed",
                "invalid_encoding",
                "malformed_metadata",
            )
            if reason in budget.reasons
        )
        return NativeAssetCatalog(
            source_id,
            "claude",
            scope,
            tuple(sorted(assets, key=lambda asset: asset.relative_id.casefold())),
            bool(reasons),
            reasons,  # type: ignore[arg-type]
            budget.entries,
            budget.bytes,
            budget.omitted,
        )

    def read_skill(self, source_id: str, name: str) -> tuple[str, str] | None:
        """A present skill's raw `SKILL.md` text and its sha256 digest, or None if absent.

        `name` is checked before any read: the same rule `parse_skill_text` holds a parsed
        skill's own name to. The folder and file are then opened exactly as `_scan_skills`
        opens one candidate, so a reparse point, a wrong entry type or a hard-linked file all
        refuse the same way a bulk scan would omit them.
        """
        if not _valid_skill_name(name):
            raise NativeAssetError("invalid_name")
        root, prefix, _scope, workspace = self._source(source_id)
        try:
            with _open_folder(root, (*prefix, "skills")) as folder:
                info = _lstat_at(folder, name)
                if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                    raise NativeAssetError("unsafe_entry")
                with _open_subfolder(folder, name) as (child, current):
                    if _physical_id(current) != _physical_id(info):
                        raise NativeAssetError("changed")
                    file_info = _lstat_at(child, "SKILL.md")
                    if (
                        _is_reparse(file_info)
                        or not stat.S_ISREG(file_info.st_mode)
                        or file_info.st_nlink != 1
                    ):
                        raise NativeAssetError("unsafe_entry")
                    if file_info.st_size > MAX_FILE_BYTES:
                        raise NativeAssetError("oversized")
                    data = _read_exact(child, "SKILL.md", file_info)
        except VaultError, OSError:
            return None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise NativeAssetError("invalid_encoding") from None
        self._confirm_source_unchanged(root, workspace)
        return text, hashlib.sha256(data).hexdigest()

    def _confirm_source_unchanged(self, root: CanonicalRoot, workspace: Workspace | None) -> None:
        """The same re-check `inspect` makes before returning: the root still opens, and an
        active workspace source has not been swapped, dropped or redirected meanwhile."""
        try:
            with _open_folder(root, ()):
                pass
        except (VaultError, OSError) as exc:
            raise NativeAssetError("source_changed") from exc
        if workspace is not None:
            active = self._workspaces.resolve_active()
            latest = self._workspaces.get(workspace.workspace_id)
            if (
                active.chat_only
                or active.workspace.workspace_id != workspace.workspace_id
                or latest != workspace
                or self._workspaces.current_root(latest) != root
            ):
                raise NativeAssetError("source_changed")

    def _source(
        self, source_id: str
    ) -> tuple[CanonicalRoot, tuple[str, ...], Literal["workspace", "user"], Workspace | None]:
        if source_id == "claude:user":
            if self._claude_home is None:
                raise NativeAssetError("source_unavailable")
            return self._claude_home, (), "user", None
        prefix = "claude:workspace:"
        if not source_id.startswith(prefix):
            raise NativeAssetError("invalid_source")
        raw_id = source_id[len(prefix) :]
        try:
            workspace_id = str(uuid.UUID(raw_id))
        except ValueError:
            raise NativeAssetError("invalid_source") from None
        if workspace_id != raw_id:
            raise NativeAssetError("invalid_source")
        active = self._workspaces.resolve_active()
        workspace = self._workspaces.get(workspace_id)
        if (
            active.chat_only
            or active.workspace.workspace_id != workspace_id
            or workspace.kind != "vault"
            or not workspace.persistent
            or workspace.source not in {"created", "opened", "migrated"}
        ):
            raise NativeAssetError("source_unavailable")
        root = self._workspaces.current_root(workspace)
        if root is None:
            raise NativeAssetError("source_unavailable")
        return root, (".claude",), "workspace", workspace

    def _scan_agents(
        self,
        root: CanonicalRoot,
        prefix: tuple[str, ...],
        source_id: str,
        scope: Literal["workspace", "user"],
        observed: str,
        budget: _Budget,
        assets: list[NativeAsset],
    ) -> None:
        self._scan_directory(
            root,
            (*prefix, "agents"),
            ("agents",),
            lambda item: item.name.casefold().endswith(".md") and _safe_name(item.name),
            "agent",
            source_id,
            scope,
            observed,
            budget,
            assets,
        )

    def _scan_skills(
        self,
        root: CanonicalRoot,
        prefix: tuple[str, ...],
        source_id: str,
        scope: Literal["workspace", "user"],
        observed: str,
        budget: _Budget,
        assets: list[NativeAsset],
    ) -> None:
        parts = (*prefix, "skills")
        relative_parts = ("skills",)
        try:
            with _open_folder(root, parts) as folder, os.scandir(folder) as entries:
                for entry in entries:
                    if not budget.visit():
                        return
                    if not _safe_name(entry.name):
                        continue
                    try:
                        info = _lstat_at(folder, entry.name)
                        if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                            if _is_reparse(info):
                                budget.reason("unsafe_entry")
                            continue
                        with _open_subfolder(folder, entry.name) as (child, current):
                            if _physical_id(current) != _physical_id(info):
                                budget.reason("changed")
                                continue
                            self._inspect_candidate(
                                child,
                                "SKILL.md",
                                PurePosixPath(*relative_parts, entry.name, "SKILL.md").as_posix(),
                                entry.name,
                                "skill",
                                source_id,
                                scope,
                                observed,
                                budget,
                                assets,
                            )
                    except FileNotFoundError:
                        continue
                    except VaultError, OSError:
                        budget.reason("unsafe_entry")
        except VaultError, OSError:
            self._directory_failure(root, parts, budget)

    def _scan_directory(
        self,
        root: CanonicalRoot,
        parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
        admitted: Callable[[os.DirEntry[str]], bool],
        kind: Literal["agent", "skill"],
        source_id: str,
        scope: Literal["workspace", "user"],
        observed: str,
        budget: _Budget,
        assets: list[NativeAsset],
    ) -> None:
        try:
            with _open_folder(root, parts) as folder, os.scandir(folder) as entries:
                for entry in entries:
                    if not budget.visit():
                        return
                    if not admitted(entry):
                        continue
                    self._inspect_candidate(
                        folder,
                        entry.name,
                        PurePosixPath(*relative_parts, entry.name).as_posix(),
                        None,
                        kind,
                        source_id,
                        scope,
                        observed,
                        budget,
                        assets,
                    )
        except VaultError, OSError:
            self._directory_failure(root, parts, budget)

    @staticmethod
    def _directory_failure(root: CanonicalRoot, parts: tuple[str, ...], budget: _Budget) -> None:
        """Record why a whole folder was left out.

        `omitted` counts entries left out one by one. A folder left out whole (linked, unreadable
        or changed) records its reason without a count: the reader never learns how many entries
        it held. A stop at the deadline or the entry limit counts one, through `_Budget.stop`.
        """
        path = Path(root.display).joinpath(*parts)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            budget.reason("unreadable", omitted=0)
            return
        budget.reason(
            "unsafe_entry" if not stat.S_ISDIR(info.st_mode) or _is_reparse(info) else "changed",
            omitted=0,
        )

    def _inspect_candidate(
        self,
        folder: _Folder,
        file_name: str,
        relative_id: str,
        fallback_name: str | None,
        kind: Literal["agent", "skill"],
        source_id: str,
        scope: Literal["workspace", "user"],
        observed: str,
        budget: _Budget,
        assets: list[NativeAsset],
    ) -> None:
        if not budget.before_work():
            return
        try:
            info = _lstat_at(folder, file_name)
        except FileNotFoundError:
            return
        except OSError:
            budget.reason("unreadable")
            return
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            budget.reason("unsafe_entry")
            return
        if info.st_size > MAX_FILE_BYTES:
            budget.reason("oversized")
            return
        if info.st_size > MAX_TOTAL_BYTES - budget.bytes:
            budget.reason("byte_limit")
            return
        budget.bytes += info.st_size
        try:
            data = _read_exact(folder, file_name, info)
            text = data.decode("utf-8-sig")
            values, unsupported = _frontmatter(text)
            if kind == "agent":
                # the CLI reads the raw value: a quoted `"42"` is text, an unquoted 42 is a number
                if any(
                    _YAML_SCALAR.fullmatch(values.get(key, "")) for key in ("name", "description")
                ):
                    raise NativeAssetError("malformed_metadata")
                name = _plain(values.get("name"), 128)
                if (
                    name is None
                    or name.startswith("-")
                    or ":" in unicodedata.normalize("NFKC", name)
                ):
                    raise NativeAssetError("malformed_metadata")
                description = _plain(values.get("description"), MAX_FILE_BYTES)
                if description is None:
                    raise NativeAssetError("malformed_metadata")
            else:
                name = _plain(values.get("name", fallback_name), 128)
                if name is None:
                    raise NativeAssetError("malformed_metadata")
                description = _plain(values.get("description"), MAX_FILE_BYTES)
            cut = False
            if description is not None and len(encoded := description.encode("utf-8")) > (
                MAX_DESCRIPTION_BYTES
            ):
                description = encoded[:MAX_DESCRIPTION_BYTES].decode("utf-8", "ignore")
                cut = True
            model = _plain(values.get("model"), 128)
            tools = _tools(values.get("tools"))
        except UnicodeDecodeError:
            budget.reason("invalid_encoding")
            return
        except NativeAssetError as error:
            budget.reason(error.code)  # type: ignore[arg-type]
            return
        except VaultError, OSError:
            budget.reason("changed")
            return
        supported = tuple(
            field for field in ("name", "description", "model", "tools") if field in values
        )
        assets.append(
            NativeAsset(
                source_id,
                scope,
                "claude",
                relative_id,
                kind,
                name,
                description,
                model,
                tools,
                hashlib.sha256(data).hexdigest(),
                observed,
                supported,
                unsupported,
            )
        )
        if cut:
            budget.reason("oversized", omitted=0)


def claude_user_root(claude_home: Path, *, config_dir_set: bool) -> CanonicalRoot | None:
    """The read-only `claude:user` source's root: the CLI's home `.claude` folder.

    None when `CLAUDE_CONFIG_DIR` is set, since the CLI then reads another folder instead. The
    deny set passed to `canonical_root` is empty on purpose: that set exists to guard this
    folder against being chosen as a writable root, and the inspector here only ever reads it.
    """
    if config_dir_set:
        return None
    try:
        return canonical_root(str(claude_home), deny=())
    except WorkspaceError:
        return None
