"""Settings import preview/apply: synthetic specs and temporary app-only SQLite, no network."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest

from viki_agent.app.imports import (
    _MAX_EXECUTABLE_DEPTH,
    MAX_UNBOUNDED_ROWS,
    ImportService,
    ImportServiceError,
    _looks_executable,
)
from viki_agent.app.settings import KEYS, SettingsConflict, SettingsService
from viki_agent.domain import EffectiveSettings, SettingsDocument, SettingSpec
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.state import Prefs
from viki_agent.storage import SessionStore

SPECS = {
    "appearance.theme": SettingSpec(
        key="appearance.theme",
        scope="app",
        kind="enum",
        allowed=("system", "light", "dark"),
        default="system",
        applies="now",
        personal=False,
    ),
    "identity.display_name": SettingSpec(
        key="identity.display_name",
        scope="app",
        kind="string",
        allowed=None,
        default="",
        applies="now",
        personal=True,
        max_len=64,
    ),
    "privacy.hide_previews": SettingSpec(
        key="privacy.hide_previews",
        scope="app",
        kind="bool",
        allowed=None,
        default=False,
        applies="now",
        personal=False,
    ),
}


def envelope(values: dict, workspaces: list | None = None, format_version: int = 1) -> str:
    return json.dumps(
        {
            "viki_export": {
                "kind": "settings",
                "format_version": format_version,
                "exported_at": "2026-09-17T00:00:00.000Z",
                "app_version": "0.1.0",
                "launch_id": "launch-1",
            },
            "settings": {"version": 1, "values": values},
            "workspaces": workspaces or [],
        }
    )


class FakePatch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict]] = []

    def __call__(
        self, command_id: str, expected_revision: int, changes: Mapping[str, object]
    ) -> tuple[EffectiveSettings, bool]:
        self.calls.append((command_id, expected_revision, dict(changes)))
        document = SettingsDocument(
            version=1,
            revision=expected_revision + 1,
            values=dict(changes),
            read_only=False,
            reason=None,
        )
        effective = EffectiveSettings(
            document=document,
            effective=dict(changes),
            sources=dict.fromkeys(changes, "setting"),
            keys=SPECS,
        )
        return effective, False


def service(current: dict | None = None, patch: FakePatch | None = None) -> ImportService:
    return ImportService(
        settings_specs=SPECS,
        current_settings=lambda: snapshot(current),
        patch=patch or FakePatch(),
    )


def snapshot(current: dict | None = None, revision: int = 0) -> SettingsDocument:
    return SettingsDocument(
        version=1, revision=revision, values=current or {}, read_only=False, reason=None
    )


@pytest.mark.parametrize("local_connection", [False, True])
def test_portable_import_refuses_qualified_default_without_changing_local_settings(
    tmp_path: Path, local_connection: bool
) -> None:
    settings = SettingsService(
        SessionStore(tmp_path / "app.sqlite3", lambda: {}),
        provider_defaults=Prefs,
        on_chat_defaults=lambda _: None,
    )
    connection = parse_connection(
        {
            "name": "Synthetic transfer connection",
            "base_url": "https://models.example/v1",
            "api_mode": "responses",
            "model": "example/model",
            "api_key": "synthetic-key",
        }
    )
    if local_connection:
        Connections(settings).write(0, [connection])
    svc = ImportService(
        settings_specs=KEYS,
        current_settings=settings.import_snapshot,
        patch=settings.patch,
    )
    before = settings.import_snapshot()
    preview = svc.preview("settings", envelope({"chat.default_model": connection.ref}))
    assert [(row.key, row.action) for row in preview.changes] == [
        ("chat.default_model", "invalid_value")
    ]
    with pytest.raises(ImportServiceError) as refused:
        svc.apply(
            preview.preview_id,
            command_id=str(uuid.uuid4()),
            expected_revision=preview.settings_revision,
            keys=["chat.default_model"],
        )
    assert refused.value.code == "invalid_selection"
    assert settings.import_snapshot() == before
    native = svc.preview("settings", envelope({"chat.default_model": "opus"}))
    assert native.changes[0].action == "apply"
    svc.apply(
        native.preview_id,
        command_id=str(uuid.uuid4()),
        expected_revision=native.settings_revision,
        keys=["chat.default_model"],
    )
    assert settings.chat_defaults().model == "opus"


def test_preview_flags_versions_duplicates_paths_and_executables() -> None:
    svc = service(current={"appearance.theme": "light"})  # non-default: differing means conflict

    newer = svc.preview("settings", envelope({}, format_version=2))
    assert newer.supported is False
    assert newer.unsupported_reason == "newer_format"
    assert newer.changes == ()
    assert newer.truncated is False

    matching = svc.preview(
        "settings",
        envelope(
            {
                "appearance.theme": "dark",
                "unknown.key": 1,
                "identity.display_name": "x" * 100,
                "hooks": {"pre_run": "rm -rf /"},
            },
            workspaces=[
                {
                    "name": "a",
                    "display_root": r"..\..\..\Windows\System32",
                    "policy": "protected",
                    "portable_id": "dup-1",
                },
                {
                    "name": "b",
                    "display_root": r"\\?\C:\weird",
                    "policy": "ordinary",
                    "portable_id": "dup-1",
                },
            ],
        ),
    )
    assert matching.supported is True
    by_key = {change.key: change.action for change in matching.changes}
    assert by_key["appearance.theme"] == "conflict"  # current is non-default and differs
    assert by_key["unknown.key"] == "unknown_key"
    assert by_key["identity.display_name"] == "invalid_value"  # exceeds max_len
    assert "hooks" not in by_key  # never reported as a change: it is an executable definition
    assert matching.executable_definitions == ({"key": "hooks", "reason": "disabled"},)
    assert matching.duplicate_ids == ("dup-1",)
    assert [row.action for row in matching.workspaces] == [
        "requires_revalidation",
        "requires_revalidation",
    ]
    # Hostile display_root strings pass through as opaque data; no path was ever touched.
    assert matching.workspaces[0].display_root == r"..\..\..\Windows\System32"
    assert matching.truncated is False


def test_import_never_registers_or_downgrades() -> None:
    # This module has no workspace-registration dependency at all: proving that every
    # workspace entry always comes back `requires_revalidation`, whatever it claims, is the
    # whole guarantee available at this layer (registration itself is `WorkspaceService`'s job).
    svc = service()
    preview = svc.preview(
        "settings",
        envelope(
            {},
            workspaces=[
                {
                    "name": "owner-vault",
                    "display_root": "C:\\VIKI",
                    "policy": "protected",
                    "portable_id": "p-1",
                }
            ],
        ),
    )
    assert preview.workspaces[0].action == "requires_revalidation"


def test_missing_viki_export_or_wrong_kind_is_invalid_format() -> None:
    svc = service()
    with pytest.raises(ImportServiceError) as excinfo:
        svc.preview("settings", json.dumps({"settings": {"values": {}}}))
    assert excinfo.value.code == "invalid_format"
    with pytest.raises(ImportServiceError) as excinfo:
        svc.preview("settings", json.dumps({"viki_export": {"kind": "commands"}}))
    assert excinfo.value.code == "invalid_format"


def test_oversize_content_is_too_large_before_parsing() -> None:
    svc = service()
    huge = envelope({"pad": "x" * (260 * 1024)})
    with pytest.raises(ImportServiceError) as excinfo:
        svc.preview("settings", huge)
    assert excinfo.value.code == "too_large"


def test_unknown_keys_are_capped_but_a_real_key_after_them_is_never_dropped() -> None:
    # The document lists 80 keys no spec knows about, then one real one, in that order: a naive
    # `break` on reaching the row cap would lose the real key entirely (it never gets reached).
    svc = service()
    values: dict[str, object] = {f"junk.{i}": i for i in range(MAX_UNBOUNDED_ROWS + 30)}
    values["appearance.theme"] = "dark"
    preview = svc.preview("settings", envelope(values))
    unknown = [c for c in preview.changes if c.action == "unknown_key"]
    assert len(unknown) == MAX_UNBOUNDED_ROWS
    assert preview.truncated is True
    by_key = {c.key: c for c in preview.changes}
    assert by_key["appearance.theme"].action == "apply"


@pytest.mark.parametrize("language", ["ja", "en", "system"])
def test_an_older_export_language_value_is_skipped_without_a_row(language: str) -> None:
    patch = FakePatch()
    svc = service(patch=patch)
    preview = svc.preview(
        "settings", envelope({"locale.language": language, "privacy.hide_previews": True})
    )
    assert preview.supported is True
    assert [(c.key, c.action) for c in preview.changes] == [("privacy.hide_previews", "apply")]
    assert preview.executable_definitions == ()
    svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=0,
        keys=["privacy.hide_previews"],
    )
    assert patch.calls == [
        ("11111111-1111-4111-8111-111111111111", 0, {"privacy.hide_previews": True})
    ]


def test_executable_definitions_are_capped() -> None:
    svc = service()
    # A shebang-prefixed string is flagged by `_looks_executable` regardless of key name, so this
    # reaches the executable cap without relying on the three fixed `_EXECUTABLE_NAMES`.
    values = {f"script.{i}": "#!/bin/sh\necho hi" for i in range(MAX_UNBOUNDED_ROWS + 10)}
    preview = svc.preview("settings", envelope(values))
    assert len(preview.executable_definitions) == MAX_UNBOUNDED_ROWS
    assert preview.truncated is True
    assert preview.changes == ()  # every key here was flagged, never reported as a change


def test_workspace_rows_are_capped_but_a_duplicate_past_the_cap_is_still_flagged() -> None:
    svc = service()
    workspaces = [
        {
            "name": f"w{i}",
            "display_root": f"D:\\{i}",
            "policy": "ordinary",
            "portable_id": f"id-{i}",
        }
        for i in range(MAX_UNBOUNDED_ROWS + 5)
    ]
    # Duplicates the last generated row's id; both copies sit past the display cap.
    workspaces.append(
        {
            "name": "dup",
            "display_root": "D:\\dup",
            "policy": "ordinary",
            "portable_id": f"id-{MAX_UNBOUNDED_ROWS + 4}",
        }
    )
    preview = svc.preview("settings", envelope({}, workspaces=workspaces))
    assert len(preview.workspaces) == MAX_UNBOUNDED_ROWS
    assert preview.truncated is True
    assert preview.duplicate_ids == (f"id-{MAX_UNBOUNDED_ROWS + 4}",)


def test_apply_refuses_keys_outside_the_apply_action_and_patches_only_selected_keys() -> None:
    patch = FakePatch()
    svc = service(current={"appearance.theme": "light"}, patch=patch)  # non-default: conflict
    preview = svc.preview(
        "settings", envelope({"appearance.theme": "dark", "privacy.hide_previews": True})
    )
    by_key = {c.key: c.action for c in preview.changes}
    assert by_key == {"appearance.theme": "conflict", "privacy.hide_previews": "apply"}
    apply_keys = [c.key for c in preview.changes if c.action == "apply"]
    assert apply_keys == ["privacy.hide_previews"]

    with pytest.raises(ImportServiceError) as excinfo:
        svc.apply(
            preview.preview_id,
            command_id="11111111-1111-4111-8111-111111111111",
            expected_revision=0,
            keys=["appearance.theme"],  # this key's action was "conflict", never "apply"
        )
    assert excinfo.value.code == "invalid_selection"
    assert patch.calls == []

    effective, replayed = svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=0,
        keys=apply_keys,
    )
    assert replayed is False
    assert patch.calls == [
        ("11111111-1111-4111-8111-111111111111", 0, {"privacy.hide_previews": True})
    ]
    assert effective.effective == {"privacy.hide_previews": True}


def test_apply_after_expiry_is_preview_expired() -> None:
    clock = [0.0]
    svc = ImportService(
        settings_specs=SPECS,
        current_settings=snapshot,
        patch=FakePatch(),
        clock=lambda: clock[0],
    )
    preview = svc.preview("settings", envelope({"privacy.hide_previews": True}))
    clock[0] += 15 * 60 + 1  # one second past the 15-minute preview lifetime
    with pytest.raises(ImportServiceError) as excinfo:
        svc.apply(
            preview.preview_id,
            command_id="11111111-1111-4111-8111-111111111111",
            expected_revision=0,
            keys=["privacy.hide_previews"],
        )
    assert excinfo.value.code == "preview_expired"


def test_unsupported_format_is_never_applied_even_with_no_keys_selected() -> None:
    svc = service()
    preview = svc.preview("settings", envelope({}, format_version=99))
    with pytest.raises(ImportServiceError) as excinfo:
        svc.apply(
            preview.preview_id,
            command_id="11111111-1111-4111-8111-111111111111",
            expected_revision=0,
            keys=[],
        )
    assert excinfo.value.code == "unsupported_format"


def test_deeply_nested_body_is_invalid_format_not_a_crash() -> None:
    # Built by string repetition, never by recursive construction. The pre-scan refuses this
    # body before json.loads runs.
    depth = 20_000
    nested = "[" * depth + "]" * depth
    content = (
        '{"viki_export":{"kind":"settings","format_version":1,"exported_at":"x",'
        '"app_version":"0.1.0","launch_id":"l"},'
        f'"settings":{{"version":1,"values":{{"deep":{nested}}}}},'
        '"workspaces":[]}'
    )
    assert len(content.encode("utf-8")) < 256 * 1024
    svc = service()
    with pytest.raises(ImportServiceError) as excinfo:
        svc.preview("settings", content)
    assert excinfo.value.code == "invalid_format"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="only Windows' thread stack overflows at this depth; POSIX may parse it",
)
def test_recursion_error_backstop_when_the_pre_scan_misses_a_deep_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "win32"
    monkeypatch.setattr("viki_agent.app.imports._json_too_deep", lambda text: False)
    depth = 20_000
    nested = "[" * depth + "]" * depth
    content = (
        '{"viki_export":{"kind":"settings","format_version":1,"exported_at":"x",'
        '"app_version":"0.1.0","launch_id":"l"},'
        f'"settings":{{"version":1,"values":{{"deep":{nested}}}}},'
        '"workspaces":[]}'
    )
    svc = service()
    with pytest.raises(ImportServiceError) as excinfo:
        svc.preview("settings", content)
    assert excinfo.value.code == "invalid_format"


def _content_at_depth(total_depth: int) -> str:
    """A minimal valid envelope whose deepest bracket/brace nesting is exactly `total_depth`.

    The two-level `viki_export` wrapper already reaches depth 2, so an extra array nested
    `total_depth - 1` levels under an unrelated key lands its innermost bracket at exactly
    `total_depth`.
    """
    inner = total_depth - 1
    nested = "[" * inner + "]" * inner
    return f'{{"viki_export":{{"kind":"settings","format_version":1}},"junk":{nested}}}'


def test_body_at_the_depth_cap_previews_normally() -> None:
    content = _content_at_depth(_MAX_EXECUTABLE_DEPTH)
    preview = service().preview("settings", content)
    assert preview.supported is True


def test_body_one_deeper_than_the_cap_is_invalid_format() -> None:
    content = _content_at_depth(_MAX_EXECUTABLE_DEPTH + 1)
    with pytest.raises(ImportServiceError) as excinfo:
        service().preview("settings", content)
    assert excinfo.value.code == "invalid_format"


def test_brackets_inside_a_string_literal_do_not_count_toward_the_depth_cap() -> None:
    # More than _MAX_EXECUTABLE_DEPTH literal "[" characters sit inside one JSON string value,
    # right after an escaped quote: none is a structural bracket, so the body still previews. A
    # scanner that ignored the escape would end the string at that quote and count every bracket
    # after it.
    value = '"' + "[" * (_MAX_EXECUTABLE_DEPTH + 10)
    content = json.dumps({"viki_export": {"kind": "settings", "format_version": 1}, "note": value})
    preview = service().preview("settings", content)
    assert preview.supported is True


def test_an_escaped_quote_does_not_hide_structural_brackets() -> None:
    # After a string that holds an escaped quote, brackets are structural again.
    deep = "[" * (_MAX_EXECUTABLE_DEPTH + 10) + "]" * (_MAX_EXECUTABLE_DEPTH + 10)
    content = '{"viki_export":{"kind":"settings","format_version":1},"a":"\\"","b":' + deep + "}"
    with pytest.raises(ImportServiceError) as excinfo:
        service().preview("settings", content)
    assert excinfo.value.code == "invalid_format"


def test_looks_executable_flags_rather_than_recurses_past_its_depth_cap() -> None:
    nested: dict[str, object] = {}
    current = nested
    for _ in range(1000):
        current["x"] = {}
        current = current["x"]  # type: ignore[assignment]
    assert _looks_executable(nested) is True  # flagged well before Python's own recursion limit


def test_format_version_excludes_bool_and_folds_non_positive_to_newer_format() -> None:
    svc = service()
    for bad_version in (True, False, 0, -1):
        preview = svc.preview("settings", envelope({}, format_version=bad_version))
        assert preview.supported is False
        assert preview.unsupported_reason == "newer_format"
        if isinstance(bad_version, bool):
            assert preview.format_version is None  # bool is never accepted as a version at all
        else:
            assert preview.format_version == bad_version  # kept for the UI, just marked refused


def test_apply_consumes_the_preview_so_a_second_apply_is_preview_expired() -> None:
    svc = service(patch=FakePatch())
    preview = svc.preview("settings", envelope({"privacy.hide_previews": True}))
    apply_keys = [c.key for c in preview.changes if c.action == "apply"]
    svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=0,
        keys=apply_keys,
    )
    with pytest.raises(ImportServiceError) as excinfo:
        svc.apply(
            preview.preview_id,
            command_id="22222222-2222-4222-8222-222222222222",
            expected_revision=1,
            keys=apply_keys,
        )
    assert excinfo.value.code == "preview_expired"


def test_values_are_validated_by_the_settings_rules_themselves() -> None:
    # The import must never admit what PATCH /settings would refuse, nor store a different
    # spelling: NEL (U+0085) is a control character the old byte-range check let through, and a
    # decomposed "e" + combining acute is stored in NFC form, exactly as a patch would store it.
    svc = service()
    preview = svc.preview(
        "settings",
        envelope({"identity.display_name": "a\u0085b", "appearance.theme": "dark"}),
    )
    by_key = {change.key: change for change in preview.changes}
    assert by_key["identity.display_name"].action == "invalid_value"
    assert by_key["appearance.theme"].action == "apply"

    patch = FakePatch()
    svc = service(patch=patch)
    preview = svc.preview(
        "settings", envelope({"identity.display_name": "Ren\u00e9e".replace("\u00e9", "e\u0301")})
    )
    change = preview.changes[0]
    assert change.action == "apply"
    assert change.incoming == "Ren\u00e9e"  # already the NFC form the settings document keeps
    svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=0,
        keys=["identity.display_name"],
    )
    assert patch.calls[0][2] == {"identity.display_name": "Ren\u00e9e"}


def test_null_is_never_an_importable_value() -> None:
    # `null` means "reset" to the settings service; an export never carries one for a known
    # key, so an import must not turn it into a patch that removes the current value.
    preview = service(current={"appearance.theme": "dark"}).preview(
        "settings", envelope({"appearance.theme": None})
    )
    assert [c.action for c in preview.changes] == ["invalid_value"]


class _Guarded(dict):
    """A cache that refuses every mutation made outside the service's own lock."""

    def __init__(self, lock: threading.Lock) -> None:
        super().__init__()
        self._lock = lock

    def __setitem__(self, key: str, value: object) -> None:
        assert self._lock.locked(), "cache written outside the lock"
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        assert self._lock.locked(), "cache evicted outside the lock"
        super().__delitem__(key)

    def pop(self, key: str, *default: object) -> object:
        assert self._lock.locked(), "cache consumed outside the lock"
        return super().pop(key, *default)


def test_preview_cache_is_only_touched_under_the_lock() -> None:
    clock = [0.0]
    svc = ImportService(
        settings_specs=SPECS,
        current_settings=snapshot,
        patch=FakePatch(),
        clock=lambda: clock[0],
    )
    svc._cache = _Guarded(svc._lock)
    preview = svc.preview("settings", envelope({"privacy.hide_previews": True}))
    for _ in range(16):  # one past MAX_PREVIEWS: exercises the oldest-entry eviction
        preview = svc.preview("settings", envelope({"privacy.hide_previews": True}))
    assert len(svc._cache) == 16
    svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=0,
        keys=["privacy.hide_previews"],
    )  # consumes the entry
    assert len(svc._cache) == 15
    clock[0] += 15 * 60 + 1
    with pytest.raises(ImportServiceError):
        svc.apply(
            preview.preview_id,
            command_id="22222222-2222-4222-8222-222222222222",
            expected_revision=0,
            keys=[],
        )  # the expiry sweep
    assert svc._cache == {}


def test_preview_eligibility_and_revision_come_from_one_snapshot() -> None:
    reads: list[None] = []
    patch = FakePatch()

    def current() -> SettingsDocument:
        reads.append(None)
        return snapshot({"appearance.theme": "system"}, revision=7)

    svc = ImportService(settings_specs=SPECS, current_settings=current, patch=patch)
    preview = svc.preview("settings", envelope({"appearance.theme": "dark"}))
    assert reads == [None]
    assert preview.settings_revision == 7
    assert preview.changes[0].current == "system"
    assert preview.changes[0].action == "apply"
    svc.apply(
        preview.preview_id,
        command_id="11111111-1111-4111-8111-111111111111",
        expected_revision=7,
        keys=["appearance.theme"],
    )
    assert patch.calls[0][1:] == (7, {"appearance.theme": "dark"})
    assert reads == [None]


@pytest.mark.parametrize("try_original_revision", [False, True])
def test_changed_settings_cannot_rebase_cached_import_eligibility(
    tmp_path: Path, try_original_revision: bool
) -> None:
    settings = SettingsService(
        SessionStore(tmp_path / "app.sqlite3", lambda: {}),
        provider_defaults=lambda: Prefs(),
        on_chat_defaults=lambda _prefs: None,
    )
    svc = ImportService(
        settings_specs=KEYS, current_settings=settings.import_snapshot, patch=settings.patch
    )
    content = envelope({"appearance.theme": "dark"})
    preview = svc.preview("settings", content)
    assert preview.settings_revision == 0
    assert preview.changes[0].action == "apply"
    settings.patch("11111111-1111-4111-8111-111111111111", 0, {"appearance.theme": "light"})
    if try_original_revision:
        with pytest.raises(SettingsConflict) as stale:
            svc.apply(
                preview.preview_id,
                command_id="22222222-2222-4222-8222-222222222222",
                expected_revision=0,
                keys=["appearance.theme"],
            )
        assert stale.value.current_revision == 1

    with pytest.raises(ImportServiceError) as failure:
        svc.apply(
            preview.preview_id,
            command_id="33333333-3333-4333-8333-333333333333",
            expected_revision=1,
            keys=["appearance.theme"],
        )
    assert failure.value.code == "preview_expired"
    current = settings.import_snapshot()
    assert current.revision == 1
    assert current.values == {"appearance.theme": "light"}
    fresh = svc.preview("settings", content)
    assert fresh.settings_revision == 1
    assert fresh.changes[0].action == "conflict"
    with pytest.raises(ImportServiceError) as selection:
        svc.apply(
            fresh.preview_id,
            command_id="33333333-3333-4333-8333-333333333333",
            expected_revision=1,
            keys=["appearance.theme"],
        )
    assert selection.value.code == "invalid_selection"
    assert settings.import_snapshot() == current


def test_unchanged_import_keeps_settings_cas_and_receipt(tmp_path: Path) -> None:
    settings = SettingsService(
        SessionStore(tmp_path / "app.sqlite3", lambda: {}),
        provider_defaults=lambda: Prefs(),
        on_chat_defaults=lambda _prefs: None,
    )
    svc = ImportService(
        settings_specs=KEYS, current_settings=settings.import_snapshot, patch=settings.patch
    )
    preview = svc.preview(
        "settings", envelope({"appearance.theme": "dark", "privacy.hide_previews": True})
    )
    command_id = "11111111-1111-4111-8111-111111111111"
    result, replayed = svc.apply(
        preview.preview_id,
        command_id=command_id,
        expected_revision=preview.settings_revision,
        keys=["appearance.theme"],
    )
    assert replayed is False
    assert result.document.revision == 1
    assert result.document.values == {"appearance.theme": "dark"}
    repeated, replayed = settings.patch(command_id, 0, {"appearance.theme": "dark"})
    assert replayed is True
    assert repeated.document == result.document
