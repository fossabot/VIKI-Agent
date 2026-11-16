"""Released command-wire and v5 ledger compatibility."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

import httpx
from starlette.applications import Starlette

from viki_agent import protection, storage
from viki_agent.conversations.command_api import command_routes
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import draft_scope
from viki_agent.state import AppState, Prefs

SID = "00000000-0000-4000-8000-000000000001"
GEN = "00000000-0000-4000-8000-000000000002"
OWNER = "00000000-0000-4000-8000-000000000003"
CID = "00000000-0000-4000-8000-000000000004"
STAMP = "2026-09-19T00:00:00.000Z"


def _legacy_hash(value: CommandInput) -> str:
    canonical = json.dumps(asdict(value), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _legacy_context(scope: str, value: CommandInput) -> bytes:
    return (
        f"VIKI submitted turn v1\0{scope}\0{value.command_id}\0{value.session_id}"
        f"\0{value.generation}\0{value.draft_revision}"
    ).encode("ascii")


def _v5_store(path: Path, scope: str, value: CommandInput, text: str) -> None:
    cipher = protection.protect(text.encode("utf-8"), context=_legacy_context(scope, value))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
        connection.execute("PRAGMA user_version=5")
        for schema in (
            storage._SESSION_SCHEMA_V6,
            storage._DRAFT_SCHEMA,
            storage._COMMAND_SCHEMA,
            storage._APPROVAL_SCHEMA,
            storage._LEGACY_BINDING_SCHEMA,
            storage._WORKSPACE_SCHEMA,
            storage._WORKSPACE_ACTIVE_INDEX,
            storage._SETTINGS_SCHEMA,
            storage._SETTING_COMMAND_SCHEMA,
        ):
            connection.execute(schema)
        connection.execute(
            "INSERT INTO sessions(session_id,mode,effort,model) VALUES (?,?,?,?)",
            (value.session_id, "auto", "low", None),
        )
        connection.execute(
            "INSERT INTO commands "
            "(scope,command_id,session_id,generation,draft_revision,request_hash,owner,state,reason,"
            "body,text_bytes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scope,
                value.command_id,
                value.session_id,
                value.generation,
                value.draft_revision,
                _legacy_hash(value),
                OWNER,
                "accepted",
                "",
                cipher,
                len(text.encode("utf-8")),
                STAMP,
                STAMP,
            ),
        )


def test_v5_command_cipher_hash_and_draft_lookup_survive_current_migration(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    value = CommandInput(CID, SID, GEN, 1)
    text = "v5 encrypted command na\u00efve"
    scope = draft_scope(vault)
    _v5_store(tmp_path / "app.sqlite3", scope, value, text)

    state = AppState(tmp_path / "state.json", Prefs())
    service = CommandService(state, vault, owner=OWNER)

    record = service.get(CID)
    assert record.text == text
    assert service.lookup(value) == record
    assert service.accept(value) == (record, False)
    assert service.find_draft_command(SID, 1).command_id == CID
    with sqlite3.connect(state.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (storage.SCHEMA_VERSION,)
        assert connection.execute(
            "SELECT origin FROM sessions WHERE session_id=?", (SID,)
        ).fetchone() == ("app",)
        assert connection.execute(
            "SELECT source_draft_id,format,request_hash FROM commands"
        ).fetchone() == (SID, 1, _legacy_hash(value))


class _NeverSubmit:
    called = False

    async def submit(self, value: CommandInput) -> None:
        self.called = True
        raise AssertionError(value)


async def test_public_command_input_has_four_wire_fields_and_rejects_private_source() -> None:
    value = CommandInput(CID, SID, GEN, 1)
    assert [field.name for field in fields(CommandInput)] == [
        "command_id",
        "session_id",
        "generation",
        "draft_revision",
    ]
    assert asdict(value) == {
        "command_id": CID,
        "session_id": SID,
        "generation": GEN,
        "draft_revision": 1,
    }
    runner = _NeverSubmit()
    app = Starlette(
        routes=command_routes(
            runner,  # type: ignore[arg-type]
            authorized=lambda _request: True,
            origins=frozenset({"http://127.0.0.1:8765"}),
            workers=set(),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post(
            "/api/v2/commands",
            headers={"origin": "http://127.0.0.1:8765"},
            json={**asdict(value), "source_draft_id": SID},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"
    assert not runner.called
