"""A registered code folder stays in the export deny set and the redaction roots."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from viki_agent import __main__ as launcher
from viki_agent.config import Config
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService


def test_a_code_folder_is_denied_and_redacted(tmp_path: Path) -> None:
    data, home, cwd = tmp_path / "data", tmp_path / "claude", tmp_path / "cwd"
    vault, code = tmp_path / "vaults" / "notes", tmp_path / "code"
    for folder in (data, home, cwd, vault, code):
        folder.mkdir(parents=True)
    config = Config(vault=cwd, claude_home=home, cli=Path("fake"), edge=None, data_dir=data)
    state = AppState(data / "state.json", Prefs())
    spaces = WorkspaceService(
        state.store,
        env={},
        data_dir=data,
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    spaces.register_existing(str(uuid.uuid4()), str(vault))
    spaces.register_existing(str(uuid.uuid4()), str(code), kind="code")
    active = spaces.resolve_active()

    denied = {os.path.normcase(p) for p in launcher._denied_roots(config, spaces, active)()}
    labels = dict(launcher._redaction_roots(config, spaces, active))

    for root in (vault, code):
        assert (
            os.path.normcase(str(root.resolve())) in denied or os.path.normcase(str(root)) in denied
        )
    assert any(
        os.path.normcase(path)
        in (os.path.normcase(str(code)), os.path.normcase(str(code.resolve())))
        and label.startswith("<workspace-")
        for path, label in labels.items()
    )
