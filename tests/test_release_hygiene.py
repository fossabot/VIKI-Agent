"""Private local files stay out of Git candidates and fail before content scanning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import test_fixtures_clean as privacy

from viki_agent import winproc


def test_gitignore_covers_local_credentials_and_app_state(tmp_path: Path) -> None:
    winproc.git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_bytes((privacy.REPO / ".gitignore").read_bytes())
    private = [
        ".env",
        "frontend/.env.production.local",
        "scratch/production.env",
        "scratch/private.pem",
        "scratch/private.key",
        "scratch/signing.p12",
        "scratch/signing.pfx",
        "scratch/signing.p8",
        "scratch/app.sqlite3",
        "scratch/app.sqlite3-journal",
        "scratch/app.sqlite3.v4-backup-synthetic.sqlite3",
        "scratch/private.db-wal",
        "scratch/state.json",
        "scratch/sealing-key-id",
        "scratch/." + "credentials.json",
        "scratch/session.log",
    ]
    public = ["config.py", "notes.md", "frontend/src/components/Sessions/Session.tsx"]
    ignored = winproc.git(
        tmp_path,
        "-c",
        f"core.excludesFile={os.devnull}",
        "check-ignore",
        "--no-index",
        "--",
        *private,
        *public,
    ).splitlines()
    assert ignored == private


def test_force_added_private_file_refuses_before_payload_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winproc.git(tmp_path, "init", "-q")
    private = tmp_path / ".env.local"
    private.write_text("SYNTHETIC_VALUE=sample\n", encoding="utf-8")
    winproc.git(tmp_path, "add", "-f", "--", private.name)
    monkeypatch.setattr(privacy, "REPO", tmp_path)
    monkeypatch.setattr(privacy, "MUST_REACH", ())
    opened: list[Path] = []
    read_bytes = Path.read_bytes

    def read(path: Path) -> bytes:
        opened.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(AssertionError, match="env.local"):
        privacy.test_committed_files_carry_no_owner_data()
    assert private not in opened
