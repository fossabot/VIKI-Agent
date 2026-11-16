"""`open.bot_memory`: Open in VS Code for a file of the Bot memory area under the data folder."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.test_server import authed
from tests.test_server import running as bare_running
from tests.workspace_code.test_code_open import App, running
from viki_agent.messaging.bot_memory import app_memory_area

BOT = "0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b"
REF = f"{BOT}/MEMORY.md"


def _area(tmp_path: Path, app: App) -> Path:
    return app_memory_area(tmp_path / "data", app.vault_id)


def _write(area: Path, ref: str, content: bytes = b"# Memory\n") -> Path:
    path = area.joinpath(*ref.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _open(app: App, ws: Any, **fields: Any) -> None:
    ws.send_json({"type": "open.bot_memory", "workspace_id": app.vault_id, "path": REF, **fields})


def _url(path: Path) -> str:
    return f"vscode://file/{path.resolve().as_posix()}"


def test_open_bot_memory_opens_the_file_in_vscode(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        file = _write(_area(tmp_path, app), REF)
        _open(app, ws)
        assert app.opened.get(timeout=3) == _url(file)
        assert app.opened.empty()


def test_open_bot_memory_escapes_what_a_vscode_url_parses_specially(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        file = _write(_area(tmp_path, app), f"{BOT}/q#frag%41.md")
        _open(app, ws, path=f"{BOT}/q#frag%41.md")
        opened = app.opened.get(timeout=3)
    assert opened.endswith(f"{BOT}/q%23frag%2541.md")
    assert opened == _url(file).replace("%", "%25").replace("#", "%23")


def test_a_refused_target_opens_nothing(tmp_path: Path) -> None:
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    with running(tmp_path) as app, authed(app.client) as ws:
        area = _area(tmp_path, app)
        _write(area, REF)
        _write(area, f"{BOT}/notes.txt")
        (area / BOT / "folder.md").mkdir()
        for path in (
            f"{BOT}/missing.md",
            f"{BOT}/notes.txt",
            f"{BOT}/folder.md",
            f"{BOT}/../../outside.md",
            "../outside.md",
            f"{BOT}/..",
            "",
            "/outside.md",
            f"{BOT}\\MEMORY.md",
        ):
            _open(app, ws, path=path)
        _open(app, ws, workspace_id=app.code_id)  # a code folder has no memory area
        _open(app, ws, workspace_id=str(uuid.uuid4()))  # a workspace nobody registered
        _open(app, ws, workspace_id="not-a-uuid")
        _open(app, ws, workspace_id="../" + app.vault_id)
        _open(app, ws)  # the one valid open, also the barrier
        assert app.opened.get(timeout=3) == _url(area / BOT / "MEMORY.md")
        assert app.opened.empty()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_link_in_the_area_opens_nothing(tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with running(tmp_path) as app, authed(app.client) as ws:
        area = _area(tmp_path, app)
        file = _write(area, REF)
        link = area / BOT / "link.md"
        if kind == "hardlink":
            os.link(outside, link)
        else:
            try:
                link.symlink_to(outside)
            except OSError:
                pytest.skip("Creating symlinks requires host privileges")
        _open(app, ws, path=f"{BOT}/link.md")
        _open(app, ws)  # barrier
        assert app.opened.get(timeout=3) == _url(file)
        assert app.opened.empty()


def test_a_missing_memory_folder_opens_nothing(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        _open(app, ws)
        _open(app, ws, path=REF + "x")
        ws.send_json({"type": "open.url", "url": "https://good.example"})  # barrier
        assert app.opened.get(timeout=3) == "https://good.example"
        assert app.opened.empty()


@pytest.mark.parametrize(
    "message",
    [
        {"type": "open.bot_memory", "workspace_id": BOT},
        {"type": "open.bot_memory", "path": REF},
        {"type": "open.bot_memory", "workspace_id": BOT, "path": 5},
        {"type": "open.bot_memory", "workspace_id": 5, "path": REF},
        {"type": "open.bot_memory", "workspace_id": BOT, "path": REF, "line": 3},
    ],
)
def test_a_malformed_message_opens_nothing(tmp_path: Path, message: dict[str, Any]) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        file = _write(_area(tmp_path, app), REF)
        ws.send_json(message)
        _open(app, ws)  # barrier
        assert app.opened.get(timeout=3) == _url(file)
        assert app.opened.empty()


def test_open_bot_memory_without_workspaces_opens_nothing(tmp_path: Path) -> None:
    with bare_running(tmp_path) as bare, authed(bare.client) as ws:
        ws.send_json({"type": "open.bot_memory", "workspace_id": str(uuid.uuid4()), "path": REF})
        ws.send_json({"type": "open.url", "url": "https://good.example"})  # barrier
        assert bare.opened.get(timeout=3) == "https://good.example"
        assert bare.opened.empty()
