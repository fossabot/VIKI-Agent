"""App state in %LOCALAPPDATA%\\viki-agent\\app.sqlite3 (never the vault): which sessions this
app created, and each session's permission mode, model and effort.

Defaults for new sessions come from the settings the CLI itself merges (read-only): the vault's
`.claude/settings.local.json` over its `.claude/settings.json` over the user's
`~/.claude/settings.json` — `permissions.defaultMode`, `effortLevel`, `model`. The app passes
mode and effort on every spawn, so it must start from the same values the terminal would.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from viki_agent import osfs
from viki_agent.config import _READ_WHOLE_LIMIT, _settings_source_allowed
from viki_agent.drivers.claude.control import EFFORTS, PERMISSION_MODES
from viki_agent.storage import SessionRecord, SessionStore, StateError, check_file

log = logging.getLogger(__name__)
MAX_LEGACY_BYTES = 8 << 20
MAX_MODEL_BYTES = 1024
MAX_SESSION_ID_BYTES = 128
# None is an explicit native-default snapshot; omission keeps legacy callers unchanged.
MODEL_UNSPECIFIED = object()


@dataclass(frozen=True)
class Prefs:
    mode: str = "default"
    effort: str = "high"
    model: str | None = None


def _valid(raw: Mapping[str, Any], base: Prefs) -> Prefs:
    """`base` with each value from `raw` that is allowed; anything else keeps base's value
    (an unsafe or unknown mode never gets through)."""
    mode, effort, model = raw.get("mode"), raw.get("effort"), raw.get("model", base.model)
    return Prefs(
        mode=mode if isinstance(mode, str) and mode in PERMISSION_MODES else base.mode,
        effort=effort if isinstance(effort, str) and effort in EFFORTS else base.effort,
        model=model if isinstance(model, str) and model else None,
    )


def _read_json(path: Path) -> dict[str, Any]:
    """The file's JSON object, or {} when it is absent, unreadable, non-regular or not an object.

    Read through osfs.read_regular at config's own ceiling (the one settings reader that must
    never block or grow without bound): a FIFO named like a settings file refuses at once instead
    of stalling the launch before any window opens, and a settings file linked to a device such
    as /dev/zero refuses instead of being read without limit.

    utf-8-sig, not utf-8: PowerShell writes JSON with a UTF-8 BOM, and plain utf-8 leaves that
    BOM in the decoded string, where json.loads rejects it. A BOM'd settings.json would then read
    as empty and every session would spawn with the built-in defaults instead of the user's mode
    and effort — the one thing this module's docstring promises never happens. The CLI's own
    settings reader uses utf-8-sig for exactly this reason; BOM-less UTF-8 decodes identically.
    """
    try:
        data = json.loads(osfs.read_regular(path, _READ_WHOLE_LIMIT).decode("utf-8-sig"))
    except FileNotFoundError:
        return {}  # the ordinary case: the user simply has no such file
    except (OSError, ValueError, RecursionError) as exc:
        # A file that exists but cannot be read (UTF-16 from PowerShell's Out-File, half-written,
        # locked, or JSON nested too deep for json.loads — RecursionError, not ValueError) used
        # to be indistinguishable from one that is not there. Still never fatal, but it now
        # leaves a trace, so "my defaults were ignored" is diagnosable from the log.
        log.warning("ignoring unreadable %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def user_defaults(
    claude_home: Path, vault: Path | None, *, env: Mapping[str, str] | None = None
) -> Prefs:
    """The user's own defaults, so the app starts sessions the way the terminal does.

    With no vault workspace this reads `claude_home` alone. It never falls back to a relative
    `.claude`, which would be the folder the app happened to be started from — someone else's
    project settings, applied to every chat-only session. Known credential aliases are excluded
    using this provider home and the supplied launch environment (the process's by default).
    """
    merged: dict[str, Any] = {}
    vault_sources = (
        ()
        if vault is None
        else (vault / ".claude" / "settings.json", vault / ".claude" / "settings.local.json")
    )
    for path in (claude_home / "settings.json", *vault_sources):
        if not _settings_source_allowed(path, claude_home, env):
            continue
        settings = _read_json(path)
        permissions = settings.get("permissions")
        if isinstance(permissions, dict) and "defaultMode" in permissions:
            merged["mode"] = permissions["defaultMode"]
        if "effortLevel" in settings:
            merged["effort"] = settings["effortLevel"]
        if "model" in settings:
            merged["model"] = settings["model"]
    return _valid(merged, Prefs())


class AppState:
    def __init__(self, path: Path, defaults: Prefs, *, database_path: Path | None = None) -> None:
        self._path = path.absolute()
        self.defaults = _valid(asdict(defaults), Prefs())
        _check_record("defaults", asdict(self.defaults), "invalid_preferences")
        self.database_path = (database_path or self._path.parent / "app.sqlite3").absolute()
        self._store = SessionStore(self.database_path, self._legacy_sessions)
        self._default_model_available: Callable[[], bool] | None = None

    def bind_default_model_guard(self, available: Callable[[], bool]) -> None:
        self._default_model_available = available

    def set_defaults(self, prefs: Prefs) -> None:
        """Replace what a session with no stored preferences inherits.

        The settings service calls this after a committed change to a `chat.*` key, so a new
        session picks the new default up without a restart. Validation is `_valid` alone: a
        committed setting must never be able to raise out of the settings commit path, and an
        unspawnable mode or effort is dropped back to the built-in value here as everywhere.
        """
        self.defaults = _valid(asdict(prefs), Prefs())

    @property
    def store(self) -> SessionStore:
        """The app-owned transaction store; draft persistence never creates session ownership."""
        return self._store

    def ours(self, session_id: str) -> bool:
        return self._store.get(session_id) is not None

    def prefs(self, session_id: str) -> Prefs:
        raw = self._store.get(session_id)
        return _valid(raw, self.defaults) if raw is not None else self.defaults

    def launch_prefs(self, session_id: str) -> Prefs:
        """Resolve a route only from stored preferences or readable defaults."""
        raw = self._store.get(session_id)
        if raw is not None:
            return _valid(raw, self.defaults)
        if self._default_model_available is not None and not self._default_model_available():
            raise StateError("model_defaults_unavailable")
        if self.defaults.model and self.defaults.model.startswith("viki-llm/"):
            raise StateError("model_choice_required")
        return self.defaults

    def remember(
        self,
        session_id: str,
        *,
        new_session: bool = False,
        expected_model: object = MODEL_UNSPECIFIED,
        **changes: Any,
    ) -> Prefs:
        """Merge into the latest stored preferences and return only after the commit."""
        if (
            not set(changes) <= {"mode", "effort", "model"}
            or (
                "mode" in changes
                and (
                    not isinstance(changes["mode"], str) or changes["mode"] not in PERMISSION_MODES
                )
            )
            or (
                "effort" in changes
                and (not isinstance(changes["effort"], str) or changes["effort"] not in EFFORTS)
            )
            or (
                "model" in changes
                and changes["model"] is not None
                and (not isinstance(changes["model"], str) or not changes["model"])
            )
        ):
            raise StateError("invalid_preferences")
        _check_record(session_id, asdict(replace(self.defaults, **changes)), "invalid_preferences")
        defaults_available = (
            "model" in changes
            or self._default_model_available is None
            or self._default_model_available()
        )

        def changed(raw: SessionRecord | None) -> SessionRecord:
            # Never persist a provider fallback when the saved route cannot be established.
            if raw is None and not defaults_available:
                raise StateError("model_defaults_unavailable")
            if (
                raw is None
                and not new_session
                and changes
                and "model" not in changes
                and self.defaults.model
                and self.defaults.model.startswith("viki-llm/")
            ):
                raise StateError("model_choice_required")
            base = _valid(raw, self.defaults) if raw is not None else self.defaults
            candidate = replace(base, **changes)
            record = asdict(_valid(asdict(candidate), base))
            _check_record(session_id, record, "invalid_preferences")
            if expected_model is not MODEL_UNSPECIFIED and record["model"] != expected_model:
                raise StateError("model_preview_changed")
            return record

        return Prefs(**self._store.update(session_id, changed))

    def _legacy_sessions(self) -> dict[str, SessionRecord]:
        """A missing legacy file is first run. Existing unfamiliar data is never reset."""
        try:
            if not check_file(self._path):
                return {}
            with self._path.open("rb") as source:
                contents = source.read(MAX_LEGACY_BYTES + 1)
            if len(contents) > MAX_LEGACY_BYTES:
                raise StateError("invalid_legacy")
            data = json.loads(contents.decode("utf-8-sig"), object_pairs_hook=_unique)
        except OSError, ValueError, RecursionError:
            raise StateError("invalid_legacy") from None
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or set(data) != {"version", "sessions"}
            or not isinstance(data["sessions"], dict)
        ):
            raise StateError("invalid_legacy")
        result: dict[str, SessionRecord] = {}
        for session_id, raw in data["sessions"].items():
            if not isinstance(raw, dict) or not set(raw) <= {"mode", "effort", "model"}:
                raise StateError("invalid_legacy")
            record = asdict(_valid(raw, self.defaults))
            _check_record(session_id, record, "invalid_legacy")
            result[session_id] = record
        return result


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _check_record(session_id: str, record: SessionRecord, code: str) -> None:
    try:
        if (
            not isinstance(session_id, str)
            or not 0 < len(session_id.encode("utf-8")) <= MAX_SESSION_ID_BYTES
        ):
            raise StateError(code)
        model = record["model"]
        if model is not None and (
            not isinstance(model, str) or not 0 < len(model.encode("utf-8")) <= MAX_MODEL_BYTES
        ):
            raise StateError(code)
    except UnicodeError:
        raise StateError(code) from None
