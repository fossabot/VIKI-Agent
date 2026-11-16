"""Preview and write for the two export kinds: a diagnostics bundle and a settings file.

Runtime dependencies are injected: `diagnostics_document`/`redact`,
`SettingsService.export_values` and its key specs, and a workspace-portability reader.
Nothing here opens a socket, and every write target is validated against the
caller-supplied denied-root list before any byte is written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from viki_agent import osfs
from viki_agent.app.diagnostics import is_credential_key, redact_secrets
from viki_agent.domain import SettingSpec
from viki_agent.llm.connections import PREFIX
from viki_agent.vaults.vault import VaultError, _open_directory
from viki_agent.workspaces import root_key

PREVIEW_TTL_S = 15 * 60
MAX_PREVIEWS = 16
MAX_RECEIPTS = 64
FORMAT_VERSION = 1
SETTINGS_VERSION = 1
LOG_TAIL_MAX_BYTES = 65536
_REDACTED_PATH = "<redacted>"
# an avatar id names an avatar row here; imports elsewhere refuse (avatar_missing); so never export
_AVATAR_ID_KEYS = frozenset({"identity.user_avatar_id", "identity.assistant_avatar_id"})


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class ExportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ExportFile:
    name: str
    bytes: int
    redactions: int


@dataclass(frozen=True)
class ExportPreview:
    preview_id: str
    kind: str
    files: tuple[ExportFile, ...]
    content: tuple[str, str]  # (file name, file text): exactly one file per kind
    expires_at: str


@dataclass(frozen=True)
class ExportResult:
    path: str
    bytes: int
    sha256: str


# \\?\, \\.\ (and so \\?\UNC\, which starts with \\?\) and bare UNC (\\server\share) all start
# with a double backslash; a plain drive path never does. Refusing the whole class up front means
# denied_destination never has to out-guess resolve()'s extended-length-path quirks.
def is_device_or_unc(text: str) -> bool:
    return text.replace("/", "\\").startswith("\\\\")


def _prefix(path: Path) -> str:
    return root_key(path).rstrip(os.sep) + os.sep


def denied_destination(destination: Path, denied_roots: Sequence[Path]) -> bool:
    """`destination` is already resolved; each root is checked as given and, best effort, as
    resolved. A denied root that is itself a junction (say `~/.codex` pointing at another
    drive) only matches a resolved destination through its own resolved spelling, and a root
    that does not exist still denies by its literal spelling: resolve() of a missing path is
    just its absolute form."""
    target = _prefix(destination)
    for root in denied_roots:
        spellings = {_prefix(root)}
        with contextlib.suppress(OSError):
            spellings.add(_prefix(root.resolve()))
        if any(target.startswith(prefix) for prefix in spellings):
            return True
    return False


class ExportService:
    def __init__(
        self,
        *,
        diagnostics_document: Callable[[bool, bool], Mapping[str, object]],
        redact: Callable[[str, Sequence[tuple[str, str]]], tuple[str, int]],
        roots: Callable[[], Sequence[tuple[str, str]]],
        settings_export: Callable[[], Mapping[str, object]],
        settings_specs: Mapping[str, SettingSpec],
        workspaces_export: Callable[[], Sequence[Mapping[str, object]]],
        denied_roots: Callable[[], Sequence[Path]],
        destination_guard: contextlib.AbstractContextManager[object],
        log_path: Path,
        app_version: str,
        launch_id: str,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._diagnostics_document = diagnostics_document
        self._redact = redact
        self._roots = roots
        self._settings_export = settings_export
        self._settings_specs = settings_specs
        self._workspaces_export = workspaces_export
        self._denied_roots = denied_roots
        self._destination_guard = destination_guard
        self._log_path = log_path
        self._app_version = app_version
        self._launch_id = launch_id
        self._clock = clock
        self._now = now
        self._cache: dict[str, tuple[float, ExportPreview]] = {}
        # (preview_id, result) by command_id: a repeated write is answered with what the first
        # one produced instead of an `exists` refusal for the file it just wrote itself.
        self._receipts: dict[str, tuple[str, ExportResult]] = {}
        # One export lock serializes writes and cache access; split them if preview
        # waits on file I/O become material. Lock order is export, then destination registry.
        self._lock = threading.Lock()

    def preview(self, kind: str, include: Mapping[str, bool] | None) -> ExportPreview:
        if kind not in ("diagnostics", "settings"):
            raise ExportError("invalid_kind", "Unknown export kind.")
        include = include or {}
        if not isinstance(include, Mapping) or any(
            not isinstance(value, bool) for value in include.values()
        ):
            raise ExportError("invalid_include", "Unknown export option.")
        if kind == "settings":
            name, text, redactions = self._settings_file(
                bool(include.get("identity", False)), bool(include.get("paths", False))
            )
        else:
            name, text, redactions = self._diagnostics_file(include)
        # One clock read decides both the cache eviction time and the reported expiry, so they
        # can never drift apart under an injected fake clock in a test.
        expiry = self._clock() + PREVIEW_TTL_S
        preview = ExportPreview(
            preview_id=str(uuid.uuid4()),
            kind=kind,
            files=(ExportFile(name=name, bytes=len(text.encode("utf-8")), redactions=redactions),),
            content=(name, text),
            expires_at=_iso(self._now() + PREVIEW_TTL_S),
        )
        self._store(preview, expiry)
        return preview

    def _redacted_values(self, include_identity: bool) -> tuple[dict[str, object], int]:
        """Strip personal/credential keys, then scrub remaining strings for secrets/paths.

        Spec-driven personal filtering only knows about the declared keys; an unknown or
        future key can still carry a stray path or token, so every string value also passes
        through the free-text redactor before it leaves this module.
        """
        values = dict(self._settings_export())
        redactions = 0
        for key in list(values):
            spec = self._settings_specs.get(key)
            personal = spec is not None and spec.personal
            value = values[key]
            connection_default = (
                key == "chat.default_model" and isinstance(value, str) and value.startswith(PREFIX)
            )
            if key in _AVATAR_ID_KEYS or connection_default or (personal and not include_identity):
                del values[key]
                redactions += 1
        roots = self._roots()
        scrubbed: dict[str, object] = {}
        pending: list[tuple[dict[str, object] | list[object], dict[str, object] | list[object]]] = [
            (values, scrubbed)
        ]

        def scrub(value: object) -> object:
            nonlocal redactions
            if isinstance(value, str):
                text, found = self._redact(value, roots)
                redactions += found
                return text
            if isinstance(value, dict | list):
                target: dict[str, object] | list[object] = {} if isinstance(value, dict) else []
                pending.append((value, target))
                return target
            return value

        # Settings supplies admitted JSON. Copy its containers without adding a Python-stack
        # depth limit or mutating the retained document. Omit sensitive keys rather than
        # renaming them, which could collide with another retained key.
        while pending:
            source, target = pending.pop()
            if isinstance(source, dict) and isinstance(target, dict):
                for key, value in source.items():
                    if is_credential_key(key) or redact_secrets(key)[1]:
                        redactions += 1
                    else:
                        target[key] = scrub(value)
            elif isinstance(source, list) and isinstance(target, list):
                target.extend(scrub(value) for value in source)
        return scrubbed, redactions

    def _settings_file(self, include_identity: bool, include_paths: bool) -> tuple[str, str, int]:
        values, redactions = self._redacted_values(include_identity)
        workspaces, removed = self._registrations(include_paths)
        redactions += removed
        document = {
            "viki_export": {
                "kind": "settings",
                "format_version": FORMAT_VERSION,
                "exported_at": _iso(self._now()),
                "app_version": self._app_version,
                "launch_id": self._launch_id,
            },
            "settings": {"version": SETTINGS_VERSION, "values": values},
            "workspaces": workspaces,
        }
        text = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True)
        return "settings.json", text, redactions

    def _diagnostics_file(self, include: Mapping[str, bool]) -> tuple[str, str, int]:
        # The base diagnostics facts (readiness, versions, storage state) are the point of a
        # support bundle and are always present; log/settings/registrations are opt-in, so a
        # plain preview with no `include` carries nothing that could hold planted material.
        include_paths = bool(include.get("paths", False))
        document: dict[str, object] = dict(
            self._diagnostics_document(include_paths, bool(include.get("identity", False)))
        )
        redactions = 0
        if include.get("log_tail", False):
            raw = self._read_log_tail()
            text, count = self._redact(raw, self._roots())
            document["log_tail"] = text
            redactions += count
        if include.get("settings", False):
            values, removed = self._redacted_values(bool(include.get("identity", False)))
            document["settings"] = values
            redactions += removed
        if include.get("registrations", False):
            workspaces, removed = self._registrations(include_paths)
            redactions += removed
            document["workspaces"] = workspaces
        text = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True)
        return "diagnostics.json", text, redactions

    def _registrations(self, include_paths: bool) -> tuple[list[dict[str, object]], int]:
        workspaces = [dict(row) for row in self._workspaces_export()]
        redactions = 0
        for row in workspaces:
            for key, value in row.items():
                if key == "display_root" and value is not None and not include_paths:
                    row[key] = _REDACTED_PATH
                    redactions += 1
                elif isinstance(value, str):
                    row[key], removed = redact_secrets(value)
                    redactions += removed
        return workspaces, redactions

    def _read_log_tail(self) -> str:
        try:
            with open(self._log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                offset = max(0, size - LOG_TAIL_MAX_BYTES)
                handle.seek(offset)
                data = handle.read(LOG_TAIL_MAX_BYTES)
        except OSError:
            return ""
        # A cut line may have lost the credential marker that would cause its removal.
        # Stay within the source-byte budget by discarding it without another read.
        if offset:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace")

    def write(
        self, *, command_id: str, preview_id: str, destination: str
    ) -> tuple[ExportResult, bool]:
        with self._lock:
            receipt = self._receipts.get(command_id)
            if receipt is not None:
                written_preview, result = receipt
                if written_preview != preview_id:
                    raise ExportError(
                        "command_conflict",
                        "This operation has already been processed with different content.",
                    )
                return result, True
            self._evict_expired()
            entry = self._cache.get(preview_id)
            if entry is None:
                raise ExportError("preview_expired", "The preview expired; run it again.")
            _, preview = entry
            if not isinstance(destination, str) or not destination:
                raise ExportError("invalid_path", "Check the export destination.")
            if is_device_or_unc(destination):
                # An export goes only to a plain drive path; \\?\, \\.\ and UNC forms are refused
                # outright rather than normalised, because normalising them could let a
                # \\?\-prefixed destination slip past the denied-root prefix check.
                raise ExportError(
                    "invalid_path",
                    "Specify an ordinary folder on a local drive as the export destination.",
                )
            dest_dir = Path(destination)
            try:
                resolved = dest_dir.resolve()
                is_dir = dest_dir.is_dir()
            except OSError:
                raise ExportError("invalid_path", "Check the export destination.") from None
            if is_device_or_unc(str(resolved)):
                raise ExportError(
                    "invalid_path",
                    "Specify an ordinary folder on a local drive as the export destination.",
                )
            if not is_dir:
                raise ExportError("invalid_path", "Couldn't find the export destination folder.")
            # This guard is the registry's own lock, acquired and released in this worker.
            # Registration cannot admit this destination between its final check and close.
            name, text = preview.content
            data = text.encode("utf-8")
            target = resolved / name
            try:
                with self._destination_guard, contextlib.ExitStack() as held:
                    if denied_destination(resolved, self._denied_roots()):
                        raise ExportError(
                            "denied_destination",
                            "Exports cannot be written inside a vault, the app data folder, "
                            "~/.claude or ~/.codex.",
                        )
                    opener: Callable[[str, int], int] | None = None
                    if sys.platform == "win32":
                        # Deny rename/reparse replacement of every ancestor through close.
                        for parent in reversed((resolved, *resolved.parents)):
                            held.enter_context(_open_directory(parent))
                    else:
                        directory = os.open(
                            resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        )
                        held.callback(os.close, directory)
                        if root_key(osfs.final_path(directory)) != root_key(resolved):
                            raise OSError("export destination changed")

                        # Creation stays relative to this directory if its name is replaced.
                        def open_relative(_path: str, flags: int) -> int:
                            if root_key(osfs.final_path(directory)) != root_key(resolved):
                                raise OSError("export destination changed")
                            return os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory)

                        opener = open_relative
                    with open(target, "xb", opener=opener) as handle:
                        if sys.platform != "win32" and root_key(
                            osfs.final_path(directory)
                        ) != root_key(resolved):
                            raise OSError("export destination changed")
                        handle.write(data)
            except FileExistsError:
                raise ExportError("exists", "A file already exists at that path.") from None
            except OSError, VaultError:
                raise ExportError(
                    "unavailable", "Couldn't save to the export destination."
                ) from None
            digest = hashlib.sha256(data).hexdigest()
            result = ExportResult(path=str(target), bytes=len(data), sha256=digest)
            if len(self._receipts) >= MAX_RECEIPTS:
                del self._receipts[next(iter(self._receipts))]  # insertion order: the oldest
            self._receipts[command_id] = (preview_id, result)
            return result, False

    def _evict_expired(self) -> None:
        """Caller holds `_lock`."""
        now = self._clock()
        for preview_id in [pid for pid, (expires, _) in self._cache.items() if expires < now]:
            del self._cache[preview_id]

    def _store(self, preview: ExportPreview, expiry: float) -> None:
        with self._lock:
            self._evict_expired()
            if len(self._cache) >= MAX_PREVIEWS:
                oldest = min(self._cache, key=lambda pid: self._cache[pid][0])
                del self._cache[oldest]
            self._cache[preview.preview_id] = (expiry, preview)
