"""A real interrupted SQLite transaction, using synthetic files and a windowless helper."""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.conversations.test_commands_legacy import CID, GEN, OWNER, SID, STAMP, _v5_store
from tests.persistence.test_storage import _downgrade_bot_code
from tests.persistence.test_storage_bot_code import _id, _legacy_rows, version_39
from viki_agent import protection, storage, winproc
from viki_agent.conversations.commands import CommandError, CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService, draft_scope
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError


def _posix_key_input() -> str | None:
    """The parent's key, hex-encoded on one line, for a child that seals or unseals.

    None on Windows: DPAPI needs no key, and `winproc.run` then leaves the child's stdin at
    the null device. On POSIX the child installs this key before any store work, since its
    own process gets no key source (only the pytest process does, via a fixture).
    """
    if sys.platform == "win32":
        return None
    return protection._key().hex() + "\n"


_CRASH = """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=4')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE sessions SET model=replace(model, 'x', 'y')")
os._exit(73)
"""

_MIGRATION_CRASH = """
import os
import sqlite3
import sys
sys.path.insert(0, sys.argv[2])
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore
checkpoint = sys.argv[3]
original_connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if (checkpoint == 'code_before_alter'
            and sql.startswith('ALTER TABLE session_profile_bindings ADD COLUMN')) or (
            checkpoint == 'code_before_commit' and sql == 'COMMIT'
        ):
            os._exit(73)
        if checkpoint == 'drop_v5' and sql == 'BEGIN IMMEDIATE':
            # Spill the rebuilt pages into the file, so only the journal can undo them.
            super().execute('PRAGMA cache_size=4')
        result = super().execute(sql, parameters)
        if (checkpoint == 'ddl' and sql.startswith('CREATE TABLE approvals')) or (
            checkpoint == 'p1_ddl' and sql.startswith('CREATE TABLE workspaces')
        ) or (
            checkpoint == 'drop_v5' and sql == 'DROP TABLE commands_v5'
        ) or (
            checkpoint == 'routine_ddl' and sql.startswith('CREATE TABLE routines ')
        ) or (
            checkpoint == 'receipt_ddl' and sql.startswith('CREATE TABLE routine_commands ')
        ) or (
            checkpoint == 'active_index_ddl'
            and sql.startswith('CREATE INDEX task_attempts_active ')
        ) or (
            checkpoint == 'code_alter'
            and sql.startswith('ALTER TABLE session_profile_bindings ADD COLUMN')
        ) or (
            checkpoint == 'code_routine'
            and sql.startswith("UPDATE session_profile_bindings SET launch_origin='routine'")
        ) or (
            checkpoint == 'code_interactive'
            and sql.startswith("UPDATE session_profile_bindings SET launch_origin='interactive'")
        ) or (
            checkpoint in ('bot_code_folders', 'bot_code_copies', 'bot_code_baselines')
            and sql.startswith('CREATE TABLE ' + checkpoint + ' ')
        ) or (
            checkpoint == 'code_index' and sql.startswith('CREATE UNIQUE INDEX bot_code_')
        ) or (
            checkpoint == 'commit' and sql == 'COMMIT'
        ):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=Connection, **kw)
original_backup = SessionStore._backup
def backup(self, version):
    original_backup(self, version)
    if checkpoint == 'backup':
        os._exit(73)
SessionStore._backup = backup
AppState(__import__('pathlib').Path(sys.argv[1]), Prefs())
os._exit(74)
"""

_DRAFT_CRASH = """
import os
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
if sys.platform != 'win32':
    from viki_agent import protection
    key = bytes.fromhex(sys.stdin.readline().strip())
    protection.set_key_source(lambda: key)
from viki_agent.state import AppState, Prefs
from viki_agent.conversations.drafts import DraftService
state = AppState(Path(sys.argv[1]), Prefs())
service = DraftService(state, Path(sys.argv[1]).parent / 'vault')
original_connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql == 'BEGIN IMMEDIATE':
            super().execute('PRAGMA cache_size=4')
        result = super().execute(sql, parameters)
        if (sys.argv[3] == 'write' and sql.startswith('INSERT INTO drafts')) or (
            sys.argv[3] == 'commit' and sql == 'COMMIT'
        ):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=Connection, **kw)
service.save('new', 1, 'synthetic-next-draft-7fbe4b21' * 20000)
os._exit(74)
"""

_COMMAND_CRASH = """
import os
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
if sys.platform != 'win32':
    from viki_agent import protection
    key = bytes.fromhex(sys.stdin.readline().strip())
    protection.set_key_source(lambda: key)
from viki_agent.state import AppState, Prefs
from viki_agent.conversations.commands import CommandInput, CommandService
state = AppState(Path(sys.argv[1]), Prefs())
service = CommandService(state, Path(sys.argv[1]).parent / 'vault', owner=sys.argv[3])
value = CommandInput(sys.argv[4], sys.argv[5], sys.argv[6], 1)
phase, boundary = sys.argv[7:9]
if phase != 'accept':
    service.accept(value)
if phase == 'finish':
    service.mark_dispatching(value.command_id)
original_connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql == 'BEGIN IMMEDIATE':
            super().execute('PRAGMA cache_size=4')
        result = super().execute(sql, parameters)
        mutation = sql.startswith('INSERT INTO commands') or sql.startswith('UPDATE commands')
        if (boundary == 'write' and mutation) or (boundary == 'commit' and sql == 'COMMIT'):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=Connection, **kw)
if phase == 'accept':
    service.accept(value)
elif phase == 'intent':
    service.mark_dispatching(value.command_id)
else:
    service.finish(value.command_id, 'unknown', 'native_unconfirmed')
os._exit(74)
"""


def _stopped_copy(tmp_path: Path) -> Path:
    recovery = tmp_path / "recovered"
    recovery.mkdir()
    for path in tmp_path.iterdir():
        if path.is_file():
            shutil.copyfile(path, recovery / path.name)
    # Recovery is deliberately on a copy; this is never a reset of the stopped source.
    with contextlib.closing(
        sqlite3.connect((recovery / "app.sqlite3").as_uri() + "?mode=rw", uri=True)
    ) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    return recovery


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("checkpoint", ["backup", "ddl", "commit"])
def test_schema_upgrade_survives_real_process_death(
    tmp_path: Path, checkpoint: str, version: int
) -> None:
    database = tmp_path / "app.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute(storage._SESSION_SCHEMA_V6)
        if version >= 2:
            connection.execute(storage._DRAFT_SCHEMA)
        if version >= 3:
            connection.execute(storage._COMMAND_SCHEMA)
        connection.execute("INSERT INTO sessions VALUES ('original','plan','high','synthetic')")
        connection.commit()
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _MIGRATION_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    backups = list(tmp_path.glob(f"app.sqlite3.v{version}-backup-*.sqlite3"))
    assert len(backups) == 1
    with contextlib.closing(sqlite3.connect(backups[0].as_uri() + "?mode=ro", uri=True)) as backup:
        assert backup.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert backup.execute("PRAGMA user_version").fetchone() == (version,)
        assert backup.execute("SELECT model FROM sessions").fetchone() == ("synthetic",)
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    recovery = _stopped_copy(tmp_path)
    state = AppState(recovery / "state.json", Prefs())
    assert state.prefs("original") == Prefs(mode="plan", effort="high", model="synthetic")
    with contextlib.closing(sqlite3.connect(state.database_path)) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone() == (storage.SCHEMA_VERSION,)
        assert upgraded.execute("SELECT count(*) FROM drafts").fetchone() == (0,)
        assert upgraded.execute("SELECT count(*) FROM approvals").fetchone() == (0,)
    assert all(path.read_bytes() == contents for path, contents in source.items())


@pytest.mark.parametrize("checkpoint", ["backup", "p1_ddl", "commit"])
def test_process_death_around_the_schema_5_ddl_commit(tmp_path: Path, checkpoint: str) -> None:
    """A stopped v5 upgrade leaves the validated v4 store and its backup, or a valid v5 store."""
    database = tmp_path / "app.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
        connection.execute("PRAGMA user_version=4")
        for ddl in (
            storage._SESSION_SCHEMA_V6,
            storage._DRAFT_SCHEMA,
            storage._COMMAND_SCHEMA,
            storage._APPROVAL_SCHEMA,
        ):
            connection.execute(ddl)
        connection.execute("INSERT INTO sessions VALUES ('original','plan','high','synthetic')")
        connection.commit()
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _MIGRATION_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    (backup,) = tmp_path.glob("app.sqlite3.v4-backup-*.sqlite3")
    with contextlib.closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
        assert saved.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert saved.execute("PRAGMA user_version").fetchone() == (4,)
        assert saved.execute("SELECT model FROM sessions").fetchone() == ("synthetic",)
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as stopped:
        version = stopped.execute("PRAGMA user_version").fetchone()[0]
        objects = stopped.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name IN "
            "('legacy_binding','setting_commands','settings','workspaces','workspaces_active')"
        ).fetchone()[0]
        assert stopped.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        # The stopped store is never half-migrated: a v4 with no schema-5 object, or a whole v5.
        assert (version, objects) in ((4, 0), (5, 5))
    recovery = _stopped_copy(tmp_path)
    state = AppState(recovery / "state.json", Prefs())
    assert state.prefs("original") == Prefs(mode="plan", effort="high", model="synthetic")
    with contextlib.closing(sqlite3.connect(state.database_path)) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone() == (storage.SCHEMA_VERSION,)
        for table in ("workspaces", "settings", "setting_commands", "legacy_binding"):
            assert upgraded.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert all(path.read_bytes() == contents for path, contents in source.items())


@pytest.mark.parametrize(
    "checkpoint", ["backup", "routine_ddl", "receipt_ddl", "active_index_ddl", "commit"]
)
def test_process_death_around_the_schema_8_ddl_commit(tmp_path: Path, checkpoint: str) -> None:
    """A stopped routine upgrade leaves the validated v7 store and its backup, or a valid v8."""
    database = AppState(tmp_path / "state.json", Prefs()).database_path
    with contextlib.closing(sqlite3.connect(database)) as connection:
        _downgrade_bot_code(connection)
        for table in (
            *storage.DECISION_PROPOSAL_SCHEMAS,
            *storage.MESSAGING_SCHEMAS,
            *storage.BOT_SCHEMAS,
            *storage.BOT_MEMORY_SCHEMAS,
            *storage.BOT_THREAD_SCHEMAS,
            *storage.ROUTINE_SCHEMAS,
            *storage.BOOKMARK_SCHEMAS,
            *storage.DECISION_SCHEMAS,
            *storage.OPERATION_SCHEMAS,
            *storage.MEMORY_SCHEMAS,
            *storage.ROOT_SCHEMAS,
            *storage.CHECK_SCHEMAS,
            *storage.SKILL_SCHEMAS,
            *storage.TASK_SCHEMAS,
            *storage.DECISION_LOOP_SCHEMAS,
        ):
            connection.execute(f"DROP TABLE {table}")
        for table, ddl in (
            ("drafts", storage._DRAFT_SCHEMA_V6),
            ("commands", storage._COMMAND_SCHEMA_V6),
            ("queued_turns", storage._QUEUED_TURN_SCHEMA),
            ("vault_publications", storage.SCHEMAS_BEFORE_13["vault_publications"]),
            ("workspaces", storage._WORKSPACE_SCHEMA),
            ("session_metadata", storage._SESSION_METADATA_SCHEMA_V20),
        ):  # empty tables: pre-10, pre-13 and pre-14 DDL, as test_storage's downgrade helpers
            connection.execute(f"DROP TABLE {table}")
            connection.execute(ddl)
        connection.execute(storage._WORKSPACE_ACTIVE_INDEX)  # dropped with workspaces
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            "INSERT INTO sessions VALUES ('original','plan','high','synthetic','app')"
        )
        connection.commit()
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _MIGRATION_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    (backup,) = tmp_path.glob("app.sqlite3.v7-backup-*.sqlite3")
    with contextlib.closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
        assert saved.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert saved.execute("PRAGMA user_version").fetchone() == (7,)
        assert saved.execute("SELECT model FROM sessions").fetchone() == ("synthetic",)
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    routine_tables = ",".join(f"'{name}'" for name in storage.ROUTINE_SCHEMAS)
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as stopped:
        version = stopped.execute("PRAGMA user_version").fetchone()[0]
        tables = stopped.execute(
            f"SELECT count(*) FROM sqlite_schema WHERE type='table' AND name IN ({routine_tables})"
        ).fetchone()[0]
        assert stopped.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        indexes = stopped.execute(
            "SELECT count(*) FROM sqlite_schema WHERE type='index' AND name='task_attempts_active'"
        ).fetchone()[0]
        # A v7 has neither routine tables nor index; a v8 has all six objects.
        assert (version, tables, indexes) in ((7, 0, 0), (8, 5, 1))
    recovery = _stopped_copy(tmp_path)
    state = AppState(recovery / "state.json", Prefs())
    assert state.prefs("original") == Prefs(mode="plan", effort="high", model="synthetic")
    with contextlib.closing(sqlite3.connect(state.database_path)) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone() == (storage.SCHEMA_VERSION,)
        for table in storage.ROUTINE_SCHEMAS:
            assert upgraded.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert upgraded.execute(
            "SELECT sql FROM sqlite_schema WHERE name='task_attempts_active'"
        ).fetchone() == (storage.TASK_ATTEMPTS_ACTIVE_INDEX,)
    assert all(path.read_bytes() == contents for path, contents in source.items())


@pytest.mark.parametrize(
    "checkpoint",
    [
        "backup",
        "code_before_alter",
        "code_alter",
        "code_routine",
        "code_interactive",
        "bot_code_folders",
        "bot_code_copies",
        "bot_code_baselines",
        "code_index",
        "code_before_commit",
        "commit",
    ],
)
def test_v40_migration_survives_real_process_death(tmp_path: Path, checkpoint: str) -> None:
    database = version_39(tmp_path)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        before = _legacy_rows(connection)
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _MIGRATION_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    (backup,) = tmp_path.glob("app.sqlite3.v39-backup-*.sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (39,)
        assert _legacy_rows(connection) == before
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    recovery = _stopped_copy(tmp_path)
    with contextlib.closing(sqlite3.connect(recovery / "app.sqlite3")) as connection:
        version = 40 if checkpoint == "commit" else 39
        assert connection.execute("PRAGMA user_version").fetchone() == (version,)
        verifier = object.__new__(storage.SessionStore)
        verifier._validate(connection, (version,))
        assert _legacy_rows(connection) == before
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(session_profile_bindings)")
        ]
        assert ("launch_origin" in columns) == (version == 40)
    recovered = AppState(recovery / "state.json", Prefs())
    with recovered.store._connection() as connection:
        assert _legacy_rows(connection) == before
        assert connection.execute(
            "SELECT launch_origin FROM session_profile_bindings WHERE session_id=?", (_id(1),)
        ).fetchone() == ("interactive",)
    assert all(path.read_bytes() == contents for path, contents in source.items())


@pytest.mark.parametrize("checkpoint", ["drop_v5", "commit"])
def test_process_death_in_the_schema_6_rebuild_keeps_both_v5_rows_decodable(
    tmp_path: Path, checkpoint: str
) -> None:
    """The 5->6 step rebuilds drafts and commands. Dying inside it (after the pages reached the
    file) or at its COMMIT leaves one validated v5 backup and loses neither encrypted row."""
    vault = tmp_path / "vault"
    vault.mkdir()
    scope = draft_scope(vault)
    value = CommandInput(CID, SID, GEN, 1)
    submitted, unsent = "synthetic-v5-command-6c1d", "synthetic-v5-draft-94be"
    database = tmp_path / "app.sqlite3"
    _v5_store(database, scope, value, submitted)
    draft_context = f"VIKI draft v1\0{scope}\0{SID}\0{value.draft_revision}".encode("ascii")
    draft_cipher = protection.protect(unsent.encode("utf-8"), context=draft_context)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO drafts VALUES (?,?,1,?,?,?)",
            (scope, SID, draft_cipher, len(unsent.encode("utf-8")), STAMP),
        )
        connection.commit()
        (command_cipher,) = connection.execute("SELECT body FROM commands").fetchone()
    pre_migration = database.read_bytes()
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _MIGRATION_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    (backup,) = tmp_path.glob("app.sqlite3.v5-backup-*.sqlite3")
    assert not list(tmp_path.glob("app.sqlite3.v6-backup-*.sqlite3"))
    with contextlib.closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
        assert saved.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert saved.execute("PRAGMA user_version").fetchone() == (5,)
        assert saved.execute("SELECT body FROM drafts").fetchone() == (draft_cipher,)
        assert saved.execute("SELECT body FROM commands").fetchone() == (command_cipher,)
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    if checkpoint == "drop_v5":
        # The half-rebuilt pages are in the file; only the stopped-copy rollback may undo them.
        assert database.read_bytes() != pre_migration
        with pytest.raises(StateError) as failure:
            AppState(tmp_path / "state.json", Prefs())
        assert failure.value.code == "recovery_required"
    recovery = _stopped_copy(tmp_path)
    if checkpoint == "drop_v5":
        assert (recovery / "app.sqlite3").read_bytes() == pre_migration
    with contextlib.closing(
        sqlite3.connect((recovery / "app.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as stopped:
        # Never half-migrated: the whole v5 store, or the whole committed v6 step.
        assert stopped.execute("PRAGMA user_version").fetchone() == (
            (5,) if checkpoint == "drop_v5" else (6,)
        )
    recovered = AppState(recovery / "state.json", Prefs())
    assert DraftService(recovered, vault).get(SID).text == unsent
    assert CommandService(recovered, vault, owner=OWNER).get(CID).text == submitted
    assert all(path.read_bytes() == contents for path, contents in source.items())


@pytest.mark.parametrize("checkpoint", ["write", "commit"])
def test_encrypted_draft_commit_boundary_survives_process_death(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    state = AppState(tmp_path / "state.json", Prefs())
    vault = tmp_path / "vault"
    original = "synthetic-old-draft-aa8279e3"
    DraftService(state, vault).save("new", 0, original)
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _DRAFT_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            checkpoint,
        ],
        cwd=tmp_path,
        input=_posix_key_input(),
    )
    assert result.returncode == 73, result.stderr
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    for contents in source.values():
        assert original.encode() not in contents
        assert b"synthetic-next-draft-7fbe4b21" not in contents
    recovery = _stopped_copy(tmp_path)
    # Keep the original logical vault scope even when app-owned state is recovered elsewhere.
    recovered = DraftService(AppState(recovery / "state.json", Prefs()), vault).get("new")
    assert recovered.revision == (2 if checkpoint == "commit" else 1)
    assert recovered.text == (
        "synthetic-next-draft-7fbe4b21" * 20000 if checkpoint == "commit" else original
    )
    assert all(path.read_bytes() == contents for path, contents in source.items())


def test_interrupted_transaction_preserves_source_and_recovers_on_a_copy(tmp_path: Path) -> None:
    legacy = tmp_path / "state.json"
    legacy.write_text('{"version": 1, "sessions": {}}', encoding="utf-8")
    state = AppState(legacy, Prefs())
    with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
        connection.executemany(
            "INSERT INTO sessions (session_id, mode, effort, model) VALUES (?, ?, ?, ?)",
            [(f"synthetic-{i}", "default", "high", "x" * 1024) for i in range(1000)],
        )
        connection.commit()

    # A small SQLite cache forces uncommitted pages to disk. This tests OS process death,
    # not an injected Python exception or a provider process. No test sleeps or live data.
    result = winproc.run(
        [sys.executable, "-I", "-c", _CRASH, str(state.database_path)], cwd=tmp_path
    )
    assert result.returncode == 73, result.stderr
    journal = Path(f"{state.database_path}-journal")
    assert journal.is_file() and journal.stat().st_size > 0
    before = {path.name: path.read_bytes() for path in (legacy, state.database_path, journal)}
    with pytest.raises(StateError) as failure:
        AppState(legacy, Prefs())
    assert failure.value.code == "recovery_required"
    assert {name: (tmp_path / name).read_bytes() for name in before} == before

    # Match the documented recovery: a stopped-app copy, mode=rw (never create a missing
    # database), native rollback, integrity check, then normal application validation.
    recovery = tmp_path / "recovered"
    recovery.mkdir()
    for name in before:
        shutil.copyfile(tmp_path / name, recovery / name)
    database = recovery / "app.sqlite3"
    with contextlib.closing(
        sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)
    ) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute(
            "SELECT count(*) FROM sessions WHERE model=?", ("x" * 1024,)
        ).fetchone() == (1000,)
    assert AppState(recovery / "state.json", Prefs()).prefs("synthetic-0").model == "x" * 1024
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("phase", ["accept", "intent", "finish"])
@pytest.mark.parametrize("boundary", ["write", "commit"])
def test_command_acceptance_and_intent_survive_real_process_death(
    tmp_path: Path, phase: str, boundary: str
) -> None:
    owner, cid, sid, generation, next_owner = (
        f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 6)
    )
    state = AppState(tmp_path / "state.json", Prefs())
    vault = tmp_path / "vault"
    text = "synthetic-immutable-submission-519cb935" * 20000
    DraftService(state, vault).save(sid, 0, text)
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _COMMAND_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            owner,
            cid,
            sid,
            generation,
            phase,
            boundary,
        ],
        cwd=tmp_path,
        input=_posix_key_input(),
    )
    assert result.returncode == 73, result.stderr
    source = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert all(b"synthetic-immutable-submission-519cb935" not in data for data in source.values())
    recovery = _stopped_copy(tmp_path)
    recovered = AppState(recovery / "state.json", Prefs())
    service = CommandService(recovered, vault, owner=next_owner)
    if phase == "accept" and boundary == "write":
        with pytest.raises(CommandError) as failure:
            service.get(cid)
        assert failure.value.code == "not_found"
    else:
        command = service.get(cid)
        assert command.state == "unknown" and command.text == text
        assert service.accept(CommandInput(cid, sid, generation, 1)) == (command, False)
        durable = (
            "unknown"
            if phase == "finish" and boundary == "commit"
            else "dispatching"
            if phase == "finish" or (phase == "intent" and boundary == "commit")
            else "accepted"
        )
        with contextlib.closing(sqlite3.connect(recovered.database_path)) as connection:
            assert connection.execute("SELECT state FROM commands").fetchone() == (durable,)
    assert DraftService(recovered, vault).get(sid).text == text
    assert all(path.read_bytes() == data for path, data in source.items())
