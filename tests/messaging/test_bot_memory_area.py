"""The read-only Bot memory routes over `<data>/bot-memory/<workspace>/`, on real files."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BASE, BEARER, _protected_vault_app
from tests.test_server import ORIGIN as APP_ORIGIN
from viki_agent.domain import SCRATCH_WORKSPACE_ID
from viki_agent.messaging.bot_memory import app_memory_area
from viki_agent.messaging.bot_memory_api import BotMemoryArea, bot_memory_routes
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.vault import MAX_NOTE_BYTES
from viki_agent.workspaces import WorkspaceService

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
BOT = "0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b"
INDEX = "---\ntitle: What this Bot keeps\n---\n# Index\n- [topic](topic.md)\n"


class Rig:
    """A registered vault and code folder, the vault's memory area, and the routes over it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        store = AppState(self.data / "state.json", Prefs()).store
        self.spaces = WorkspaceService(
            store,
            env={},
            data_dir=self.data,
            home=tmp_path / "home",
            install_dir=tmp_path / "install",
            git_available=lambda: False,
            hide_previews=lambda: False,
        )
        self.vault_id = self.register(tmp_path / "vault")
        self.code_id = self.register(tmp_path / "code", kind="code")
        self.area = app_memory_area(self.data, self.vault_id)
        self.workers: set[asyncio.Task[Any]] = set()
        self.app = Starlette(
            routes=bot_memory_routes(
                BotMemoryArea(self.spaces, self.data),
                authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
                origins=frozenset({ORIGIN}),
                workers=self.workers,
            )
        )

    def register(self, root: Path, *, kind: Any = "vault") -> str:
        root.mkdir()
        workspace, _ = self.spaces.register_existing(str(uuid.uuid4()), str(root), kind=kind)
        return workspace.workspace_id

    def write(self, ref: str, content: bytes) -> Path:
        path = self.area.joinpath(*ref.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def url(self, tail: str, workspace_id: str | None = None) -> str:
        return f"/api/v2/bot-memory/{workspace_id or self.vault_id}/{tail}"

    def metadata(self, client: TestClient, refs: list[str], **kwargs: Any) -> Any:
        return client.post(self.url("metadata"), headers=AUTH, json={"refs": refs}, **kwargs)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def code(response: Any) -> str:
    return response.json()["error"]["code"]


def make_link(path: Path, target: Path, kind: str) -> None:
    if kind == "file":
        path.write_text("not a folder", encoding="utf-8")
    elif kind == "junction":
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(path))
        else:
            pytest.skip("junctions are Windows only")
    else:
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("Creating directory symlinks requires host privileges")


def test_lists_reads_and_enriches_app_memory(rig: Rig) -> None:
    rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    rig.write(f"{BOT}/topic.md", b"# Topic\nThe user prefers short answers.\n")
    rig.write(f"{BOT}/scratch.txt", b"not a note")
    before = files(rig.data)
    with TestClient(rig.app) as client:
        listing = client.get(rig.url("entries"), headers=AUTH)
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "no-store"
        body = listing.json()
        refs = [entry["ref"] for entry in body["entries"]]
        assert refs == [f"{BOT}/MEMORY.md", f"{BOT}/topic.md"]
        assert body["folder"] == str(rig.area)
        assert (body["partial"], body["partial_reasons"]) == (False, [])

        note = client.get(rig.url(f"notes/{BOT}/MEMORY.md"), headers=AUTH)
        assert note.status_code == 200
        assert note.headers["cache-control"] == "no-store"
        read = note.json()
        assert (read["note"]["ref"], read["note"]["source"]) == (f"{BOT}/MEMORY.md", INDEX)
        assert (read["resolved_links"], read["partial"], read["partial_reasons"]) == ([], False, [])

        enriched = rig.metadata(client, [f"{BOT}/MEMORY.md", f"{BOT}/topic.md"])
        assert enriched.status_code == 200
        items = enriched.json()["items"]
        assert [item["state"] for item in items] == ["ready", "ready"]
        assert items[0]["display_metadata"]["display_title"] == "What this Bot keeps"
    assert files(rig.data) == before
    assert not rig.workers


def test_missing_area_lists_nothing(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        listing = client.get(rig.url("entries"), headers=AUTH)
        assert listing.status_code == 200
        assert listing.json() == {
            "entries": [],
            "partial": False,
            "partial_reasons": [],
            "scanned_bytes": 0,
            "visited_items": 0,
            "omitted": 0,
            "folder": str(rig.area),
        }
        note = client.get(rig.url(f"notes/{BOT}/MEMORY.md"), headers=AUTH)
        assert (note.status_code, code(note)) == (404, "missing_note")
        enriched = rig.metadata(client, [f"{BOT}/MEMORY.md"])
        assert (enriched.status_code, code(enriched)) == (404, "missing_note")
        # Only `<data>/bot-memory` there, the area under it still missing.
        rig.area.parent.mkdir()
        assert client.get(rig.url("entries"), headers=AUTH).json()["entries"] == []
    assert not rig.area.exists()


@pytest.mark.parametrize("tail", ["entries", f"notes/{BOT}/MEMORY.md", "metadata"])
def test_routes_need_the_token_and_a_known_origin(rig: Rig, tail: str) -> None:
    rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    with TestClient(rig.app) as client:

        def ask(headers: dict[str, str]) -> Any:
            if tail == "metadata":
                return client.post(rig.url(tail), headers=headers, json={"refs": [f"{BOT}/a.md"]})
            return client.get(rig.url(tail), headers=headers)

        refused = ask({})
        assert (refused.status_code, code(refused)) == (401, "unauthorized")
        assert refused.headers["cache-control"] == "no-store"
        assert ask({"Authorization": "Bearer other"}).status_code == 401
        foreign = ask({**AUTH, "Origin": "https://evil.invalid"})
        assert (foreign.status_code, code(foreign)) == (403, "forbidden_origin")
        assert ask({**AUTH, "Origin": ORIGIN}).status_code == 200


@pytest.mark.parametrize("which", ["unregistered", "code", "scratch", "not_uuid", "uppercase"])
def test_only_a_registered_vault_has_an_area(rig: Rig, which: str) -> None:
    workspace_id = {
        "unregistered": str(uuid.uuid4()),
        "code": rig.code_id,
        "scratch": SCRATCH_WORKSPACE_ID,
        "not_uuid": "x" * 36,
        "uppercase": rig.vault_id.upper(),
    }[which]
    # A real area for that id, so only the registration check can refuse it.
    if which != "not_uuid":
        area = rig.data / "bot-memory" / workspace_id / BOT
        area.mkdir(parents=True)
        (area / "MEMORY.md").write_text("canary", encoding="utf-8")
    with TestClient(rig.app) as client:
        for response in (
            client.get(rig.url("entries", workspace_id), headers=AUTH),
            client.get(rig.url(f"notes/{BOT}/MEMORY.md", workspace_id), headers=AUTH),
            client.post(
                rig.url("metadata", workspace_id), headers=AUTH, json={"refs": [f"{BOT}/MEMORY.md"]}
            ),
        ):
            assert (response.status_code, code(response)) == (404, "unknown_workspace")
            assert "canary" not in response.text
            assert "bot-memory" not in response.text


@pytest.mark.parametrize(
    ("tail", "status", "error"),
    [
        ("notes/%2E%2E/vault/a.md", 400, "invalid_ref"),
        (f"notes/{BOT}/%2E%2E/%2E%2E/a.md", 400, "invalid_ref"),
        ("notes//etc/a.md", 400, "invalid_ref"),
        ("notes/C:/a.md", 400, "invalid_ref"),
        (f"notes/{BOT}%5Ca.md", 400, "invalid_ref"),
        (f"notes/{BOT}/scratch.txt", 400, "unsupported_note"),
        (f"notes/{BOT}/MEMORY.md?q=x", 400, "invalid_query"),
        ("entries?q=memory", 400, "invalid_query"),
        ("entries?x", 400, "invalid_query"),
        (f"notes/{BOT}/.env", 404, "private_path"),
        (f"notes/{BOT}/.claude/a.md", 404, "private_path"),
        (f"notes/{BOT}/credentials.json", 404, "private_path"),
        (f"notes/{BOT}/gone.md", 404, "missing_note"),
    ],
)
def test_refs_and_queries_are_refused(rig: Rig, tail: str, status: int, error: str) -> None:
    rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    rig.write(f"{BOT}/scratch.txt", b"canary")
    rig.write(f"{BOT}/.claude/a.md", b"canary")
    rig.write(f"{BOT}/credentials.json", b"canary")
    (rig.tmp / "vault" / "a.md").write_text("canary", encoding="utf-8")
    with TestClient(rig.app) as client:
        response = client.get(rig.url(tail), headers=AUTH)
    assert (response.status_code, code(response)) == (status, error)
    assert "canary" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"refs": ["../a.md"]},
        {"refs": [f"{BOT}/a.txt"]},
        {"refs": ["/a.md"]},
        {"refs": []},
        {"refs": [f"{BOT}/a.md"], "extra": 1},
    ],
)
def test_metadata_requests_are_checked(rig: Rig, body: dict[str, Any]) -> None:
    rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    with TestClient(rig.app) as client:
        response = client.post(rig.url("metadata"), headers=AUTH, json=body)
        assert (response.status_code, code(response)) == (400, "invalid_metadata_request")
        query = client.post(rig.url("metadata?x=1"), headers=AUTH, json={"refs": ["a/b.md"]})
        assert (query.status_code, code(query)) == (400, "invalid_metadata_request")


@pytest.mark.parametrize("kind", ["symlink", "junction", "hardlink"])
def test_links_inside_the_area_are_neither_listed_nor_read(rig: Rig, kind: str) -> None:
    outside = rig.tmp / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("canary", encoding="utf-8")
    rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    if kind == "hardlink":
        os.link(outside / "secret.md", rig.area / BOT / "linked.md")
        ref = f"{BOT}/linked.md"
    else:
        make_link(rig.area / BOT / "linked", outside, kind)
        ref = f"{BOT}/linked/secret.md"
    with TestClient(rig.app) as client:
        listing = client.get(rig.url("entries"), headers=AUTH).json()
        assert [entry["ref"] for entry in listing["entries"]] == [f"{BOT}/MEMORY.md"]
        assert listing["partial_reasons"] == ["unsafe_entry"]
        note = client.get(rig.url(f"notes/{ref}"), headers=AUTH)
        assert (note.status_code, code(note)) == (400, "unsafe_path")
        assert "canary" not in note.text
        enriched = rig.metadata(client, [ref]).json()["items"][0]
        assert (enriched["state"], enriched["reason"]) == ("unreadable", "unsafe_path")


@pytest.mark.parametrize("kind", ["symlink", "junction", "file"])
@pytest.mark.parametrize("level", ["parent", "area"])
def test_a_link_or_file_at_the_area_levels_is_refused(rig: Rig, level: str, kind: str) -> None:
    outside = rig.tmp / "outside"
    (outside / rig.vault_id / BOT).mkdir(parents=True)
    (outside / rig.vault_id / BOT / "MEMORY.md").write_text("canary", encoding="utf-8")
    (outside / BOT).mkdir()
    (outside / BOT / "MEMORY.md").write_text("canary", encoding="utf-8")
    if level == "parent":
        make_link(rig.area.parent, outside, kind)
    else:
        rig.area.parent.mkdir()
        make_link(rig.area, outside, kind)
    with TestClient(rig.app) as client:
        for response in (
            client.get(rig.url("entries"), headers=AUTH),
            client.get(rig.url(f"notes/{BOT}/MEMORY.md"), headers=AUTH),
            rig.metadata(client, [f"{BOT}/MEMORY.md"]),
        ):
            assert (response.status_code, code(response)) == (400, "unsafe_path")
            assert "canary" not in response.text


def test_a_note_over_the_cap_is_413_and_a_read_note_can_be_renamed_at_once(rig: Rig) -> None:
    rig.write(f"{BOT}/large.md", b"a" * (MAX_NOTE_BYTES + 1))
    read = rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    with TestClient(rig.app) as client:
        large = client.get(rig.url(f"notes/{BOT}/large.md"), headers=AUTH)
        assert (large.status_code, code(large)) == (413, "note_too_large")
        assert client.get(rig.url(f"notes/{BOT}/MEMORY.md"), headers=AUTH).status_code == 200
        os.replace(read, read.with_name("renamed.md"))
        assert rig.metadata(client, [f"{BOT}/renamed.md"]).status_code == 200


def test_open_target_names_only_a_readable_note_in_the_area(rig: Rig) -> None:
    path = rig.write(f"{BOT}/MEMORY.md", INDEX.encode())
    area = BotMemoryArea(rig.spaces, rig.data)
    assert area.open_target(rig.vault_id, f"{BOT}/MEMORY.md") == path.resolve()
    for workspace_id, ref in (
        (rig.vault_id, f"{BOT}/gone.md"),
        (rig.vault_id, f"{BOT}/../{BOT}/MEMORY.md"),
        (rig.vault_id, f"{BOT}/notes.txt"),
        (rig.code_id, f"{BOT}/MEMORY.md"),
        ("x" * 36, f"{BOT}/MEMORY.md"),
    ):
        assert area.open_target(workspace_id, ref) is None


def test_exports_carry_no_memory_text(tmp_path: Path) -> None:
    canary = "canary-" + uuid.uuid4().hex
    app, workspace_id = _protected_vault_app(tmp_path)
    memory = app_memory_area(tmp_path / "data", workspace_id) / BOT
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(f"# Memory\n{canary}\n", encoding="utf-8")
    headers = {**BEARER, "origin": APP_ORIGIN}
    everything = {
        "paths": True,
        "identity": True,
        "log_tail": True,
        "settings": True,
        "registrations": True,
    }
    with TestClient(app, base_url=BASE) as client:
        note = client.get(
            f"/api/v2/bot-memory/{workspace_id}/notes/{BOT}/MEMORY.md", headers=headers
        )
        assert note.status_code == 200
        assert canary in note.json()["note"]["text"]
        texts = [client.get("/api/v2/diagnostics", headers=BEARER).text]
        for kind, include in (("diagnostics", everything), ("settings", everything)):
            preview = client.post(
                "/api/v2/exports/preview", headers=headers, json={"kind": kind, "include": include}
            )
            assert preview.status_code == 200
            texts.append(json.dumps(preview.json()))
    assert all(canary not in text for text in texts)
