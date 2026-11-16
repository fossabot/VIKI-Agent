"""Preview and apply for the settings import format (the only import kind).

Injected dependencies: the settings key specs and
SettingsService.import_snapshot/patch. Value validity comes from `settings._validate_change`
itself, so an import can never admit a value `PATCH /settings` would refuse (NFC form,
control characters, the two non-empty string keys). This module never touches a filesystem path
found in imported content: workspace entries are data only, always `requires_revalidation`
through the real `/vaults/open` flow, never canonicalised or registered here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from viki_agent.api_json import unique_object
from viki_agent.app.settings import RETIRED_KEYS, _FieldError, _validate_change
from viki_agent.domain import EffectiveSettings, SettingsDocument, SettingSpec
from viki_agent.llm.connections import PREFIX

MAX_IMPORT_BYTES = 256 * 1024
PREVIEW_TTL_S = 15 * 60
MAX_PREVIEWS = 16
FORMAT_VERSION = 1
# The three row lists below have no natural ceiling of their own (unlike a real settings key,
# which is bounded by len(settings_specs)): an uploaded document can name any number of unknown
# keys or workspace entries inside the same MAX_IMPORT_BYTES budget. catalog.py's SEARCH_LIMIT
# argues the same invariant for search hits (rows x per-row size, not just request bytes); this
# is that rule applied to the one read path that would otherwise bound only bytes.
MAX_UNBOUNDED_ROWS = 50
# Names a hostile/future payload might use to smuggle behaviour; never applied, only flagged.
_EXECUTABLE_NAMES = {"routines", "profiles", "hooks"}
# A structure nested deeper than this is itself suspicious and is flagged rather than walked:
# recursing to the caller's own depth would risk a RecursionError this far into a request.
_MAX_EXECUTABLE_DEPTH = 50


class ImportServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ImportChange:
    key: str
    current: object
    incoming: object
    action: str  # apply | unchanged | unknown_key | invalid_value | conflict


@dataclass(frozen=True)
class WorkspaceImportRow:
    name: str
    display_root: str
    policy: str
    action: str  # always "requires_revalidation"


@dataclass(frozen=True)
class ImportPreview:
    preview_id: str
    content_hash: str
    settings_revision: int
    format_version: int | None
    supported: bool
    unsupported_reason: str | None
    provenance: Mapping[str, object]
    changes: tuple[ImportChange, ...]
    workspaces: tuple[WorkspaceImportRow, ...]
    executable_definitions: tuple[Mapping[str, object], ...]
    duplicate_ids: tuple[str, ...]
    # True when an unbounded row list (unknown-key changes, executable definitions or
    # workspaces) was cut at MAX_UNBOUNDED_ROWS. data_api.py's hand-built response dict does
    # not carry it, so it is real and tested at this layer but does not reach the UI.
    truncated: bool


def _looks_executable(value: object, depth: int = 0) -> bool:
    """Heuristic only: catches an obviously script-shaped member, not a determined attacker.

    No sandboxing here because nothing from an import is ever executed; this only
    decides what to flag for the person reviewing the preview. `depth` bounds the recursion:
    a structure nested beyond it is flagged outright rather than walked further.
    """
    if depth > _MAX_EXECUTABLE_DEPTH:
        return True
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _EXECUTABLE_NAMES or _looks_executable(inner, depth + 1)
            for key, inner in value.items()
        )
    if isinstance(value, list):
        return any(_looks_executable(item, depth + 1) for item in value)
    if isinstance(value, str):
        return value.lstrip().startswith("#!") or ("\n" in value and len(value) > 200)
    return False


def _json_too_deep(text: str) -> bool:
    """True when `text`'s bracket/brace nesting outside string literals passes
    `_MAX_EXECUTABLE_DEPTH`, checked before `json.loads` runs.

    Python's own JSON scanner recurses once per nesting level, and that recursion is bounded by
    the running thread's C stack, whose size differs by OS and by thread, not by a fixed depth:
    the same body that raises `RecursionError` on Windows can parse on Linux or macOS. Counting
    the nesting in one iterative pass, before the parser ever runs, gives the same refusal on
    every OS. One pass, no recursion.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > _MAX_EXECUTABLE_DEPTH:
                return True
        elif ch == "}" or ch == "]":
            depth -= 1
    return False


class _Invalid(Exception):
    pass


def _normalised(key: str, value: object) -> object:
    """A portable setting value, or `_Invalid`.

    Imports exclude resets and connection references, which name local sealed rows, and
    `chat.other_sign_in`, which decides what account sessions bill: only the Chat page sets it.
    """
    try:
        normalised = _validate_change(key, value)
    except _FieldError:
        raise _Invalid from None
    if (
        normalised is None
        or key == "chat.other_sign_in"
        or (
            key == "chat.default_model"
            and isinstance(normalised, str)
            and normalised.startswith(PREFIX)
        )
    ):
        raise _Invalid
    return normalised


class ImportService:
    def __init__(
        self,
        *,
        settings_specs: Mapping[str, SettingSpec],
        current_settings: Callable[[], SettingsDocument],
        patch: Callable[[str, int, Mapping[str, object]], tuple[EffectiveSettings, bool]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._specs = settings_specs
        self._current_settings = current_settings
        self._patch = patch
        self._clock = clock
        self._cache: dict[str, tuple[float, ImportPreview, dict[str, object]]] = {}
        # Every request runs on its own worker thread; the cache is the only shared state.
        self._lock = threading.Lock()

    def preview(self, kind: str, content: str) -> ImportPreview:
        if kind != "settings":
            raise ImportServiceError("invalid_format", "Not a VIKI settings export.")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ImportServiceError("too_large", "The import data is too large.")
        if _json_too_deep(content):
            raise ImportServiceError("invalid_format", "Not a VIKI settings export.")
        try:
            document = json.loads(content, object_pairs_hook=unique_object)
        except ValueError, RecursionError:
            # Backstop: _json_too_deep already refuses a body nested past _MAX_EXECUTABLE_DEPTH,
            # so a RecursionError here should never fire; any other decode error is still
            # malformed input, not a 500.
            raise ImportServiceError("invalid_format", "Not a VIKI settings export.") from None
        if not isinstance(document, dict) or "viki_export" not in document:
            raise ImportServiceError("invalid_format", "Not a VIKI settings export.")
        header = document["viki_export"]
        if not isinstance(header, dict) or header.get("kind") != "settings":
            raise ImportServiceError("invalid_format", "Not a VIKI settings export.")

        raw_version = header.get("format_version")
        format_version = (
            raw_version
            if isinstance(raw_version, int) and not isinstance(raw_version, bool)
            else None
        )
        # Only format_version 1 is supported; 0, negatives, non-ints and bools all fold into
        # the same "newer_format" refusal as an actually-newer version.
        supported = format_version is not None and 1 <= format_version <= FORMAT_VERSION
        provenance = {
            "exported_at": header.get("exported_at"),
            "app_version": header.get("app_version"),
            "launch_id": header.get("launch_id"),
        }
        settings = self._current_settings()
        changes, executable, apply_values, changes_truncated = self._changes(
            document, supported, settings.values or {}
        )
        workspaces, duplicate_ids, workspaces_truncated = self._workspaces(document)

        preview = ImportPreview(
            preview_id=str(uuid.uuid4()),
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            settings_revision=settings.revision,
            format_version=format_version,
            supported=supported,
            unsupported_reason=None if supported else "newer_format",
            provenance=provenance,
            changes=tuple(changes),
            workspaces=tuple(workspaces),
            executable_definitions=tuple(executable),
            duplicate_ids=tuple(duplicate_ids),
            truncated=changes_truncated or workspaces_truncated,
        )
        self._store(preview, apply_values)
        return preview

    def _changes(
        self, document: Mapping[str, object], supported: bool, current: Mapping[str, object]
    ) -> tuple[list[ImportChange], list[Mapping[str, object]], dict[str, object], bool]:
        changes: list[ImportChange] = []
        executable: list[Mapping[str, object]] = []
        apply_values: dict[str, object] = {}
        if not supported:
            return changes, executable, apply_values, False
        settings_block = document.get("settings")
        values = settings_block.get("values") if isinstance(settings_block, Mapping) else None
        if not isinstance(values, Mapping):
            return changes, executable, apply_values, False
        # Every key backed by a real spec produces at most one row each, so that population is
        # already bounded by len(self._specs). Only `unknown_key` and `executable` rows are
        # unbounded by the input itself; each is capped on its own counter so a run of junk keys
        # can never crowd out a real one that happens to sit later in the document.
        unknown_count = 0
        truncated = False
        for key, incoming in values.items():
            if key in RETIRED_KEYS:
                continue  # an older export's retired key: ignored, never a row
            if key in _EXECUTABLE_NAMES or _looks_executable(incoming):
                if len(executable) < MAX_UNBOUNDED_ROWS:
                    executable.append({"key": key, "reason": "disabled"})
                else:
                    truncated = True
                continue
            spec = self._specs.get(key)
            if spec is None:
                unknown_count += 1
                if unknown_count <= MAX_UNBOUNDED_ROWS:
                    changes.append(ImportChange(key, current.get(key), incoming, "unknown_key"))
                else:
                    truncated = True
                continue
            try:
                incoming = _normalised(key, incoming)
            except _Invalid:
                changes.append(ImportChange(key, current.get(key), incoming, "invalid_value"))
                continue
            current_value = current.get(key, spec.default)
            if current_value == incoming:
                changes.append(ImportChange(key, current_value, incoming, "unchanged"))
                continue
            if current_value != spec.default:
                changes.append(ImportChange(key, current_value, incoming, "conflict"))
                continue
            changes.append(ImportChange(key, current_value, incoming, "apply"))
            apply_values[key] = incoming
        return changes, executable, apply_values, truncated

    def _workspaces(
        self, document: Mapping[str, object]
    ) -> tuple[list[WorkspaceImportRow], list[str], bool]:
        rows: list[WorkspaceImportRow] = []
        seen: dict[str, int] = {}
        raw = document.get("workspaces")
        truncated = False
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, Mapping):
                    continue
                # Duplicate detection runs over every entry regardless of the row cap below: a
                # duplicate past row MAX_UNBOUNDED_ROWS is still a duplicate the user should see.
                for id_key in ("portable_id", "workspace_id"):
                    value = entry.get(id_key)
                    if isinstance(value, str) and value:
                        seen[value] = seen.get(value, 0) + 1
                if len(rows) < MAX_UNBOUNDED_ROWS:
                    rows.append(
                        WorkspaceImportRow(
                            name=str(entry.get("name", "")),
                            display_root=str(entry.get("display_root", "")),
                            policy=str(entry.get("policy", "")),
                            action="requires_revalidation",
                        )
                    )
                else:
                    truncated = True
        duplicates = sorted(value for value, count in seen.items() if count > 1)
        return rows, duplicates, truncated

    def apply(
        self,
        preview_id: str,
        *,
        command_id: str,
        expected_revision: int,
        keys: Sequence[str],
    ) -> tuple[EffectiveSettings, bool]:
        with self._lock:
            self._evict_expired()
            entry = self._cache.get(preview_id)
        if entry is None:
            raise ImportServiceError("preview_expired", "The preview expired; run it again.")
        _, preview, apply_values = entry
        if not preview.supported:
            raise ImportServiceError(
                "unsupported_format", "This export was written by a newer version."
            )
        # Eligibility was reviewed against this exact revision. The patch's transactional CAS
        # still checks for later writes; a caller cannot rebase this cached selection onto one.
        if expected_revision != preview.settings_revision:
            raise ImportServiceError("preview_expired", "The preview expired; run it again.")
        for key in keys:
            if key not in apply_values:
                raise ImportServiceError(
                    "invalid_selection", 'Only rows marked "apply" can be selected.'
                )
        changes = {key: apply_values[key] for key in keys}
        result = self._patch(command_id, expected_revision, changes)
        # Single-use once it actually applies: a second apply must re-preview, never replay the
        # same cached selection against a settings document that has since moved on.
        with self._lock:
            self._cache.pop(preview_id, None)
        return result

    def _evict_expired(self) -> None:
        """Caller holds `_lock`."""
        now = self._clock()
        for preview_id in [pid for pid, (expires, _, _) in self._cache.items() if expires < now]:
            del self._cache[preview_id]

    def _store(self, preview: ImportPreview, apply_values: dict[str, object]) -> None:
        with self._lock:
            self._evict_expired()
            if len(self._cache) >= MAX_PREVIEWS:
                oldest = min(self._cache, key=lambda pid: self._cache[pid][0])
                del self._cache[oldest]
            self._cache[preview.preview_id] = (
                self._clock() + PREVIEW_TTL_S,
                preview,
                apply_values,
            )
