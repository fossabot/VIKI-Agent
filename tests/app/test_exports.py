"""Diagnostics/settings export preview and write: synthetic fakes, tmp_path only, no sockets."""

from __future__ import annotations

import builtins
import json
import socket
import sys
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from viki_agent import winproc
from viki_agent.app import diagnostics, exports
from viki_agent.app.exports import ExportError, ExportResult, ExportService, denied_destination
from viki_agent.app.settings import KEYS, SettingsService
from viki_agent.domain import SettingSpec
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore

SPECS = {
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
    "appearance.theme": SettingSpec(
        key="appearance.theme",
        scope="app",
        kind="enum",
        allowed=("system", "light", "dark"),
        default="system",
        applies="now",
        personal=False,
    ),
}

TOKEN = "sk-ant-planted-secret-value-should-never-survive-export"
USER_PATH = "C:\\Users\\synthuser\\secret-notes.txt"
USER_AVATAR_ID = "11111111-2222-3333-4444-555555555555"
ASSISTANT_AVATAR_ID = "66666666-7777-8888-9999-000000000000"
HANG_GUARD_S = 5  # Guard on events normally raised in milliseconds, not a work budget.


def content_text(preview) -> str:
    return preview.content[1]


def write(svc: ExportService, preview_id: str, destination: str) -> ExportResult:
    """One fresh command per call: these tests are about the destination, not replay."""
    result, _replayed = svc.write(
        command_id=str(uuid.uuid4()), preview_id=preview_id, destination=destination
    )
    return result


def make_junction(link: Path, target: Path) -> None:
    done = winproc.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], cwd=str(link.parent), timeout=10.0
    )
    if done.returncode != 0:
        pytest.skip("this volume refuses junctions")


def fake_redact(text: str, roots: Sequence[tuple[str, str]]) -> tuple[str, int]:
    """Drop lines with secret-shaped content and substitute any planted user path."""
    count = 0
    replaced = text.replace(USER_PATH, "<user>") if USER_PATH in text else text
    if replaced != text:
        count += 1
    lines = []
    for line in replaced.splitlines():
        if "sk-ant-" in line or "Bearer " in line or "token=" in line:
            count += 1
            continue
        lines.append(line)
    return "\n".join(lines), count


def service(tmp_path: Path, *, settings_values: dict | None = None) -> ExportService:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        f"2026-09-17 boot ok\nleaked secret: {TOKEN}\nBearer abcdef123456\n", encoding="utf-8"
    )
    return ExportService(
        diagnostics_document=lambda paths, identity: {
            "app": {"version": "0.1.0"},
            "provider": {"claude": {"cli": "ready"}},
        },
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: dict(
            settings_values or {"identity.display_name": "Robin", "appearance.theme": "dark"}
        ),
        settings_specs=SPECS,
        workspaces_export=lambda: [
            {"name": "owner-vault", "policy": "protected", "display_root": r"C:\Synthetic\Vault"}
        ],
        denied_roots=lambda: (tmp_path / "denied",),
        destination_guard=threading.RLock(),
        log_path=log_path,
        app_version="0.1.0",
        launch_id="launch-1",
    )


def test_export_redacts_planted_material_by_default(tmp_path: Path) -> None:
    svc = service(tmp_path)

    default_preview = svc.preview("diagnostics", None)
    text = content_text(default_preview)
    assert TOKEN not in text
    assert "Bearer" not in text
    # The settings/log sections are opt-in and absent by default, not merely redacted: even a
    # harmless, non-personal key must not appear unless explicitly selected.
    assert "settings" not in json.loads(text)
    assert "log_tail" not in json.loads(text)
    assert "appearance.theme" not in text
    assert "identity.display_name" not in text

    selected = svc.preview("diagnostics", {"log_tail": True, "settings": True, "identity": False})
    selected_text = content_text(selected)
    assert TOKEN not in selected_text  # redact() strips the secret line even when included
    document = json.loads(selected_text)
    assert "identity.display_name" not in document["settings"]  # personal key stays out
    assert document["settings"]["appearance.theme"] == "dark"
    assert selected.files[0].redactions >= 1

    settings_preview = svc.preview("settings", None)
    settings_text = content_text(settings_preview)
    assert "identity.display_name" not in settings_text
    assert TOKEN not in settings_text


@pytest.mark.parametrize("append", [False, True], ids=["stable", "appended"])
def test_log_tail_bounds_actual_source_bytes_during_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, append: bool
) -> None:
    svc = service(tmp_path)
    log = tmp_path / "app.log"
    cap = exports.LOG_TAIL_MAX_BYTES
    raw = b"older\n" + b"x" * cap + b"tail\n"
    log.write_bytes(raw)
    consumed: list[int] = []

    @contextmanager
    def opened(path, mode):
        assert Path(path) == log and mode == "rb"
        with builtins.open(path, mode) as source:

            def read(amount: int = -1) -> bytes:
                # Grow after the captured-EOF seek, before the real source-byte read.
                if append:
                    with builtins.open(log, "ab") as growing:
                        growing.write(b"new" * cap)
                data = source.read(amount)
                consumed.append(len(data))
                return data

            yield SimpleNamespace(seek=source.seek, tell=source.tell, read=read)

    monkeypatch.setattr(exports, "open", opened, raising=False)
    preview = svc.preview("diagnostics", {"log_tail": True})
    assert consumed == [cap]
    assert json.loads(content_text(preview))["log_tail"] == ""


@pytest.mark.parametrize("suffix", [b"short-value\nordinary\n", b"short-value"])
def test_log_tail_discards_a_credential_line_cut_after_its_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: bytes
) -> None:
    svc, _store = retained_settings_exporter(tmp_path, {})
    cap = len(suffix)
    monkeypatch.setattr(exports, "LOG_TAIL_MAX_BYTES", cap)
    (tmp_path / "unused.log").write_bytes(b"older\npassword=" + suffix)
    preview = svc.preview("diagnostics", {"log_tail": True})
    expected = "ordinary\n" if b"\n" in suffix else ""
    assert json.loads(content_text(preview))["log_tail"] == expected


def test_log_tail_keeps_an_untrimmed_first_line(tmp_path: Path) -> None:
    svc, _store = retained_settings_exporter(tmp_path, {})
    (tmp_path / "unused.log").write_bytes(b"ordinary\nlast line")
    preview = svc.preview("diagnostics", {"log_tail": True})
    assert json.loads(content_text(preview))["log_tail"] == "ordinary\nlast line"


def test_settings_section_scrubs_planted_secrets_and_paths_in_unknown_keys(tmp_path: Path) -> None:
    # A value under a key this module has never heard of (not in SPECS, so spec-based personal
    # filtering can't catch it) must still lose a planted secret/path: the per-value redact()
    # pass is the only thing standing between an unknown future key and a real leak.
    svc = service(
        tmp_path,
        settings_values={
            "appearance.theme": "dark",
            "custom.unknown_note": f"see {USER_PATH} and key {TOKEN}",
        },
    )
    preview = svc.preview("diagnostics", {"settings": True})
    document = json.loads(content_text(preview))
    note = document["settings"]["custom.unknown_note"]
    assert USER_PATH not in note
    assert TOKEN not in note
    assert preview.files[0].redactions >= 2  # the path substitution and the dropped secret line

    settings_kind = svc.preview("settings", None)
    settings_document = json.loads(content_text(settings_kind))
    settings_note = settings_document["settings"]["values"]["custom.unknown_note"]
    assert USER_PATH not in settings_note
    assert TOKEN not in settings_note


def test_settings_export_drops_avatar_ids_with_or_without_identity(tmp_path: Path) -> None:
    # An avatar id is dropped and counted whether or not identity is included: it names an
    # avatar row of this data folder, which an import elsewhere refuses as avatar_missing.
    avatar_values = {
        "identity.display_name": "Robin",
        "identity.assistant_name": "Nova",
        "identity.user_avatar_id": USER_AVATAR_ID,
        "identity.assistant_avatar_id": ASSISTANT_AVATAR_ID,
        "appearance.theme": "dark",
    }
    svc = service(tmp_path, settings_values=avatar_values)

    included = svc.preview("settings", {"identity": True})
    text = content_text(included)
    assert "identity.user_avatar_id" not in text
    assert "identity.assistant_avatar_id" not in text
    assert "identity.display_name" in text  # the other identity keys still export
    assert "identity.assistant_name" in text
    assert json.loads(text)["settings"]["values"]["identity.assistant_name"] == "Nova"

    plain_svc = service(
        tmp_path,
        settings_values={"identity.display_name": "Robin", "appearance.theme": "dark"},
    )
    plain = plain_svc.preview("settings", {"identity": True})
    assert included.files[0].redactions == plain.files[0].redactions + 2

    excluded = svc.preview("settings", None)
    excluded_text = content_text(excluded)
    assert "identity.user_avatar_id" not in excluded_text
    assert "identity.assistant_avatar_id" not in excluded_text


def test_registrations_redact_display_root_unless_paths_included(tmp_path: Path) -> None:
    svc = service(tmp_path)

    without_paths = svc.preview("diagnostics", {"registrations": True})
    document = json.loads(content_text(without_paths))
    assert document["workspaces"][0]["display_root"] == "<redacted>"
    assert document["workspaces"][0]["name"] == "owner-vault"  # non-path fields still useful
    assert without_paths.files[0].redactions >= 1

    with_paths = svc.preview("diagnostics", {"registrations": True, "paths": True})
    with_paths_document = json.loads(content_text(with_paths))
    assert with_paths_document["workspaces"][0]["display_root"] == r"C:\Synthetic\Vault"


@pytest.mark.parametrize("include", [None, {"paths": False}, {"paths": True}])
def test_settings_registrations_honor_path_choice(tmp_path: Path, include: dict | None) -> None:
    preview = service(tmp_path).preview("settings", include)
    row = json.loads(content_text(preview))["workspaces"][0]
    assert row == {
        "name": "owner-vault",
        "policy": "protected",
        "display_root": r"C:\Synthetic\Vault" if include and include.get("paths") else "<redacted>",
    }


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
@pytest.mark.parametrize("paths", [False, True])
def test_registration_strings_scrub_credentials_even_when_paths_included(
    tmp_path: Path, kind: str, paths: bool
) -> None:
    svc, _store = retained_settings_exporter(tmp_path, {})
    rows = [
        {
            "name": "password=short-pass",
            "display_root": USER_PATH,
            "url": "https://owner:short-secret@example.com/",
            "policy": "ordinary",
        },
        {"name": "ordinary", "display_root": "https://owner:short-root@example.com"},
    ]
    svc._workspaces_export = lambda: rows
    preview = svc.preview(kind, {"paths": paths, "registrations": True})
    assert json.loads(content_text(preview))["workspaces"] == [
        {
            "name": "",
            "display_root": rows[0]["display_root"] if paths else "<redacted>",
            "url": "",
            "policy": "ordinary",
        },
        {"name": "ordinary", "display_root": "" if paths else "<redacted>"},
    ]
    assert rows[0]["name"] == "password=short-pass"


def test_export_opens_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("export must never open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    svc = service(tmp_path)
    preview = svc.preview("settings", {"identity": True})
    destination = tmp_path / "out"
    destination.mkdir()
    result = write(svc, preview.preview_id, str(destination))
    assert Path(result.path).is_file()


def test_export_refuses_denied_destinations_and_existing_files(tmp_path: Path) -> None:
    svc = service(tmp_path)
    denied = tmp_path / "denied"
    denied.mkdir()
    preview = svc.preview("settings", None)

    with pytest.raises(ExportError) as excinfo:
        write(svc, preview.preview_id, str(denied))
    assert excinfo.value.code == "denied_destination"

    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    result = write(svc, preview.preview_id, str(ok_dir))
    assert Path(result.path).read_bytes()

    with pytest.raises(ExportError) as excinfo:
        write(svc, preview.preview_id, str(ok_dir))
    assert excinfo.value.code == "exists"  # never overwrite

    missing = tmp_path / "does-not-exist"
    with pytest.raises(ExportError) as excinfo:
        write(svc, preview.preview_id, str(missing))
    assert excinfo.value.code == "invalid_path"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive, device and junction paths")
def test_write_refuses_device_and_unc_forms_and_every_denied_variant(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    # `denied_roots` names one plain drive path; every case below must still refuse a
    # destination that, one way or another, points inside it -- and a sibling that merely
    # shares its name as a string prefix must stay allowed.
    vault = tmp_path / "vault"
    vault.mkdir()
    sibling = tmp_path / "vault2"  # "denied2"-style sibling: shares the string prefix, not denied
    sibling.mkdir()

    svc = ExportService(
        diagnostics_document=lambda paths, identity: {},
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs=SPECS,
        workspaces_export=lambda: [],
        denied_roots=lambda: (vault,),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
    )

    def refused(destination: str) -> str:
        preview = svc.preview("settings", None)
        with pytest.raises(ExportError) as excinfo:
            write(svc, preview.preview_id, destination)
        return excinfo.value.code

    vault_str = str(vault)
    assert refused(f"\\\\?\\{vault_str}") == "invalid_path"
    assert refused(f"\\\\.\\{vault_str}") == "invalid_path"
    assert refused(f"\\\\?\\UNC\\{vault_str}") == "invalid_path"
    assert refused(vault_str.replace("\\", "/")) == "denied_destination"  # forward slashes
    assert refused(vault_str + "\\") == "denied_destination"  # trailing separator
    assert refused(vault_str.upper()) == "denied_destination"  # case-insensitive
    assert refused(str(sibling / ".." / "vault")) == "denied_destination"  # ".."

    link = tmp_path / "link_to_vault"
    make_junction(link, vault)
    assert refused(str(link)) in ("denied_destination", "invalid_path")

    # The sibling must still be a perfectly good destination.
    preview = svc.preview("settings", None)
    result = write(svc, preview.preview_id, str(sibling))
    assert Path(result.path).is_file()


def test_write_after_preview_expiry_is_preview_expired(tmp_path: Path) -> None:
    clock = [0.0]
    svc = ExportService(
        diagnostics_document=lambda paths, identity: {},
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs=SPECS,
        workspaces_export=lambda: [],
        denied_roots=lambda: (),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
        clock=lambda: clock[0],
    )
    preview = svc.preview("settings", None)
    clock[0] += 15 * 60 + 1
    with pytest.raises(ExportError) as excinfo:
        write(svc, preview.preview_id, str(tmp_path))
    assert excinfo.value.code == "preview_expired"


def test_expires_at_is_derived_from_the_injected_clock_and_wall_clock(tmp_path: Path) -> None:
    clock = [1000.0]
    wall = [2_000_000_000.0]  # an arbitrary fixed epoch, deterministic regardless of real time
    svc = ExportService(
        diagnostics_document=lambda paths, identity: {},
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs=SPECS,
        workspaces_export=lambda: [],
        denied_roots=lambda: (),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
        clock=lambda: clock[0],
        now=lambda: wall[0],
    )
    preview = svc.preview("settings", None)
    expected = (
        datetime.fromtimestamp(wall[0] + 15 * 60, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    assert preview.expires_at == expected
    # Advancing only the wall clock (not the monotonic one) must not change a previously
    # computed expires_at -- it was captured once, from the same read that set the cache TTL.
    wall[0] += 3600
    clock[0] += 1
    still_valid = write(svc, preview.preview_id, str(tmp_path))
    assert Path(still_valid.path).is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_denied_root_that_is_a_junction_denies_both_its_spellings(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    # The deny list names `~/.codex`-style roots by their literal spelling. When such a root is
    # itself a junction, the resolved destination only ever carries the target's spelling, so
    # the root must deny under both -- otherwise the export lands in the junction's real target.
    real = tmp_path / "real_codex"
    real.mkdir()
    link = tmp_path / "dot_codex"
    make_junction(link, real)
    svc = ExportService(
        diagnostics_document=lambda paths, identity: {},
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs=SPECS,
        workspaces_export=lambda: [],
        denied_roots=lambda: (link,),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
    )
    preview = svc.preview("settings", None)
    for destination in (link, real):
        with pytest.raises(ExportError) as excinfo:
            write(svc, preview.preview_id, str(destination))
        assert excinfo.value.code == "denied_destination", destination
    assert not list(real.iterdir())  # nothing was written through either spelling

    # A root that does not exist still denies by its literal spelling.
    missing = tmp_path / "missing_codex"
    assert denied_destination(missing / "sub", [missing]) is True
    assert denied_destination(tmp_path / "elsewhere", [missing]) is False


def test_write_replays_the_same_command_and_refuses_a_different_preview(tmp_path: Path) -> None:
    svc = service(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    preview = svc.preview("settings", None)
    command_id = str(uuid.uuid4())

    first, replayed = svc.write(
        command_id=command_id, preview_id=preview.preview_id, destination=str(out)
    )
    assert replayed is False
    again, replayed = svc.write(
        command_id=command_id, preview_id=preview.preview_id, destination=str(out)
    )
    assert replayed is True
    assert again == first  # the file it wrote itself is never reported as `exists`
    assert Path(first.path).read_bytes()

    other = svc.preview("settings", {"identity": True})
    with pytest.raises(ExportError) as excinfo:
        svc.write(command_id=command_id, preview_id=other.preview_id, destination=str(out))
    assert excinfo.value.code == "command_conflict"


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


def test_preview_cache_is_only_touched_under_the_lock(tmp_path: Path) -> None:
    # Requests run on separate worker threads; every insert, eviction (full cache and expiry)
    # must happen while the service lock is held.
    clock = [0.0]
    svc = ExportService(
        diagnostics_document=lambda paths, identity: {},
        redact=fake_redact,
        roots=lambda: (),
        settings_export=lambda: {},
        settings_specs=SPECS,
        workspaces_export=lambda: [],
        denied_roots=lambda: (),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "app.log",
        app_version="0.1.0",
        launch_id="launch-1",
        clock=lambda: clock[0],
    )
    svc._cache = _Guarded(svc._lock)
    preview = svc.preview("settings", None)
    for _ in range(16):  # one past MAX_PREVIEWS: exercises the oldest-entry eviction
        preview = svc.preview("settings", None)
    assert len(svc._cache) == 16
    clock[0] += 15 * 60 + 1  # everything expires: exercises the expiry sweep inside write()
    with pytest.raises(ExportError):
        write(svc, preview.preview_id, str(tmp_path))
    assert svc._cache == {}


def retained_settings_exporter(
    tmp_path: Path, values: dict[str, object]
) -> tuple[ExportService, SessionStore]:
    store = AppState(tmp_path / "data" / "state.json", Prefs()).store
    document = json.dumps(values, ensure_ascii=True)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO settings VALUES ('app',2,1,?,?)",
            (document, "2026-01-01T00:00:00.000Z"),
        )
    settings = SettingsService(store, provider_defaults=Prefs, on_chat_defaults=lambda _: None)
    assert settings.read().document.reason == "newer_version"
    return ExportService(
        diagnostics_document=lambda _paths, _identity: {},
        redact=lambda text, roots: diagnostics.redact(text, roots=roots),
        roots=lambda: (),
        settings_export=settings.export_values,
        settings_specs=KEYS,
        workspaces_export=lambda: (),
        denied_roots=lambda: (),
        destination_guard=threading.RLock(),
        log_path=tmp_path / "unused.log",
        app_version="synthetic",
        launch_id="synthetic",
    ), store


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
@pytest.mark.parametrize("include_identity", [False, True])
def test_export_omits_nonportable_connection_default_before_redaction(
    tmp_path: Path, kind: str, include_identity: bool
) -> None:
    values: dict[str, object] = {
        "chat.default_model": "viki-llm/00000000-0000-4000-8000-000000000001/example/model",
        "chat.llm_connections": "synthetic-sealed-bundle",
        "appearance.theme": "dark",
    }
    svc, store = retained_settings_exporter(tmp_path, values)
    before = store.get_settings()
    preview = svc.preview(kind, {"settings": True, "identity": include_identity})
    payload = json.loads(content_text(preview))["settings"]
    if kind == "settings":
        payload = payload["values"]
    assert payload == {"appearance.theme": "dark"}
    assert preview.files[0].redactions == 2
    assert store.get_settings() == before


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
def test_native_model_default_remains_portable(tmp_path: Path, kind: str) -> None:
    svc, _store = retained_settings_exporter(tmp_path, {"chat.default_model": "opus"})
    preview = svc.preview(kind, {"settings": True})
    payload = json.loads(content_text(preview))["settings"]
    if kind == "settings":
        payload = payload["values"]
    assert payload == {"chat.default_model": "opus"}


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
def test_nested_retained_settings_redact_values_and_keep_source_bytes(
    tmp_path: Path, kind: str
) -> None:
    values = {
        "future.flat": TOKEN,
        "future.path": USER_PATH,
        "future.nested": {
            "entries": [TOKEN, {"path": USER_PATH, "ordinary": "keep"}],
            "scalars": [None, True, 7, 1.5],
            "empty": [{}, []],
        },
    }
    svc, store = retained_settings_exporter(tmp_path, values)
    before = store.get_settings()
    preview = svc.preview(kind, {"settings": True})
    payload = json.loads(content_text(preview))["settings"]
    if kind == "settings":
        payload = payload["values"]
    assert payload == {
        "future.flat": "",
        "future.path": "<user>\\secret-notes.txt",
        "future.nested": {
            "entries": ["", {"path": "<user>\\secret-notes.txt", "ordinary": "keep"}],
            "scalars": [None, True, 7, 1.5],
            "empty": [{}, []],
        },
    }
    assert preview.files[0].redactions == 4
    assert store.get_settings() == before
    assert svc._settings_export() == values


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
def test_retained_settings_omit_sensitive_keys_without_collisions_or_source_changes(
    tmp_path: Path, kind: str
) -> None:
    values = {
        "future.api_key": "short-top-level",
        "future.text": [
            "password=short-pass",
            "bEaReR short-bearer",
            "https://user:short-url@example.com",
        ],
        "future.nested": [
            {
                "api_key": "short-marker",
                "password": "short-pass",
                "TOKEN": "short-token",
                "clientSecret": {"nested": "short-client"},
                "bot_token": "short-bot",
                "app_token": "short-app",
                "env.VIKI_SLACK_BOT_TOKEN": "short-env",
                "cookie": "short-cookie",
                "token_count": 7,
                "password_policy": "strict",
                "": "empty key stays",
                "<redacted>": "placeholder key stays",
                "ordinary": "keep",
                TOKEN: "secret-bearing key",
                "https://user:short-pass@example.com": "userinfo key",
            }
        ],
    }
    svc, store = retained_settings_exporter(tmp_path, values)
    before = store.get_settings()
    preview = svc.preview(kind, {"settings": True, "identity": True})
    payload = json.loads(content_text(preview))["settings"]
    if kind == "settings":
        payload = payload["values"]
    assert payload == {
        "future.text": ["", "", ""],
        "future.nested": [
            {
                "token_count": 7,
                "password_policy": "strict",
                "": "empty key stays",
                "<redacted>": "placeholder key stays",
                "ordinary": "keep",
            }
        ],
    }
    assert preview.files[0].redactions == 14
    assert store.get_settings() == before
    assert svc._settings_export() == values


@pytest.mark.parametrize("kind", ["settings", "diagnostics"])
def test_admitted_deep_settings_do_not_gain_a_redaction_recursion_limit(
    tmp_path: Path, kind: str
) -> None:
    nested: object = "ordinary leaf"
    for _ in range(600):
        nested = [nested]
    svc, store = retained_settings_exporter(tmp_path, {"future.deep": nested})
    before = store.get_settings()
    preview = svc.preview(kind, {"settings": True})
    payload = json.loads(content_text(preview))["settings"]
    if kind == "settings":
        payload = payload["values"]
    leaf = payload["future.deep"]
    for _ in range(600):
        assert isinstance(leaf, list) and len(leaf) == 1
        leaf = leaf[0]
    assert leaf == "ordinary leaf"
    assert preview.files[0].redactions == 0
    assert store.get_settings() == before


@pytest.mark.parametrize("same_preview", [False, True])
def test_concurrent_writes_keep_one_command_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_preview: bool
) -> None:
    svc = service(tmp_path)
    first = svc.preview("settings", None)
    second = first if same_preview else svc.preview("diagnostics", None)
    command_id = str(uuid.uuid4())
    entered, release = threading.Event(), threading.Event()
    started, finished = threading.Event(), threading.Event()

    @contextmanager
    def paused_open(path, mode="r", *args, **kwargs):
        with builtins.open(path, mode, *args, **kwargs) as handle:
            if mode == "xb" and Path(path).name == "settings.json":
                entered.set()
                assert release.wait(HANG_GUARD_S)
            yield handle

    def write_second() -> tuple[ExportResult, bool]:
        started.set()
        try:
            return svc.write(
                command_id=command_id, preview_id=second.preview_id, destination=str(tmp_path)
            )
        finally:
            finished.set()

    monkeypatch.setattr(exports, "open", paused_open, raising=False)
    with ThreadPoolExecutor(max_workers=2) as workers:
        one = workers.submit(
            svc.write,
            command_id=command_id,
            preview_id=first.preview_id,
            destination=str(tmp_path),
        )
        try:
            assert entered.wait(HANG_GUARD_S)
            two = workers.submit(write_second)
            assert started.wait(0.05)
            assert not finished.wait(0.05)
        finally:
            release.set()
        result, replayed = one.result(timeout=HANG_GUARD_S)
        assert replayed is False
        if same_preview:
            assert two.result(timeout=HANG_GUARD_S) == (result, True)
        else:
            with pytest.raises(ExportError) as refused:
                two.result(timeout=HANG_GUARD_S)
            assert refused.value.code == "command_conflict"
    assert {path.name for path in tmp_path.glob("*.json")} == {"settings.json"}
    assert svc._receipts == {command_id: (first.preview_id, result)}


@pytest.mark.parametrize("move_inside_denied", [False, True])
def test_export_does_not_follow_a_destination_replaced_during_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, move_inside_denied: bool
) -> None:
    svc = service(tmp_path)
    destination, moved, denied = (tmp_path / name for name in ("export", "moved", "denied"))
    destination.mkdir()
    denied.mkdir()
    if move_inside_denied:
        moved = denied / "moved"
    preview = svc.preview("settings", None)
    original = builtins.open
    attempted = False

    def replace_before_open(path, mode="r", *args, **kwargs):
        nonlocal attempted
        if mode == "xb" and not attempted:
            attempted = True
            try:
                destination.rename(moved)
            except OSError:
                pass  # A pinned Windows directory refuses replacement.
            else:
                if sys.platform == "win32":
                    import _winapi

                    _winapi.CreateJunction(str(denied), str(destination))
                else:
                    destination.symlink_to(denied, target_is_directory=True)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(exports, "open", replace_before_open, raising=False)
    try:
        write(svc, preview.preview_id, str(destination))
    except ExportError as refused:
        assert refused.code == "unavailable"
    assert not list(denied.rglob("settings.json"))
