"""The rails Git chip in a code folder with a Bot-copy settings baseline: it runs Git only once
the folder's settings check (`CodeGit.operation`) passes, and reads as unknown otherwise."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import OTHER_COMMAND, service_at
from tests.test_server import BEARER
from tests.workspace_code.test_code_git import _commit, _repo, needs_git
from tests.workspace_code.test_code_open import _rails_client
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import rails, winproc
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import HARDENING, CodeGit
from viki_agent.code.git_guard import GitGuard
from viki_agent.events import GitChip, Rails, VaultCounts
from viki_agent.state import AppState, Prefs

UNKNOWN = GitChip(branch="", head="", committed_at="", dirty=None)


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _record_git(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[tuple[str, ...]]:
    """Every Git command run in `root`; the guard's own private parses run elsewhere."""
    calls: list[tuple[str, ...]] = []
    real = winproc.git

    def recording(cwd: str | os.PathLike[str], *args: str, **kwargs: Any) -> str:
        if Path(cwd) == root:
            calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(winproc, "git", recording)
    return calls


def test_a_refused_settings_check_runs_no_git_and_reads_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def refuse(_root: Path) -> Iterator[tuple[str, ...]]:
        raise CodeError("git_settings_changed")
        yield ()

    monkeypatch.setattr(winproc, "git", lambda *_a, **_k: pytest.fail("Git ran"))
    assert rails.git_chip(tmp_path, hardening=HARDENING, operation=refuse) == UNKNOWN


def _baselined(tmp_path: Path, *, baseline: bool = True) -> tuple[Path, AppState]:
    """A trusted, active code folder with one stat-dirty file, and its baseline if asked."""
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_text("aaaa\n", encoding="utf-8")
    _commit(root, "first")
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    spaces = service_at(tmp_path, state.store)
    workspace_id = trusted(spaces, root)
    spaces.select(OTHER_COMMAND, workspace_id)
    if baseline:
        assert state.store.insert_bot_code_baseline(
            {
                "workspace_id": workspace_id,
                "git_fingerprint": GitGuard(spaces, state.store).fingerprint(root),
                "created_at": "2026-11-11T00:00:00.000Z",
            }
        )
    # Same size, newer mtime: status must rehash the file, through any clean filter.
    info = (root / "a.txt").stat()
    os.utime(root / "a.txt", ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    return root, state


def _route_chip(tmp_path: Path, root: Path, state: AppState) -> dict[str, object]:
    spaces = service_at(tmp_path, state.store)
    with _rails_client(tmp_path, spaces, root) as client:
        response = client.get("/api/rails", headers=BEARER)
    assert response.status_code == 200, response.text
    return response.json()["git"]


@needs_git
def test_an_unchanged_baseline_leaves_the_chip_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state = _baselined(tmp_path)
    plain = rails.git_chip(root, hardening=HARDENING)
    assert (plain.state, plain.dirty) == ("known", 0)
    calls = _record_git(monkeypatch, root)
    chip = _route_chip(tmp_path, root, state)
    assert chip == {**plain.__dict__}
    assert [args[len(HARDENING) :] for args in calls] == [
        ("branch", "--show-current"),
        ("rev-parse", "--short", "HEAD"),
        ("log", "-1", "--format=%cI"),
        ("status", "--porcelain"),
    ]


@needs_git
def test_changed_settings_run_no_git_in_the_folder_and_read_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state = _baselined(tmp_path)
    marker = tmp_path / "filter-ran"
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f'[filter "x"]\n\tclean = "echo ran > \'{marker.as_posix()}\'; cat"\n')
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text("* filter=x\n", encoding="utf-8")
    calls = _record_git(monkeypatch, root)

    chip = _route_chip(tmp_path, root, state)

    assert chip == {**UNKNOWN.__dict__}
    assert calls == []
    assert not marker.exists()
    # The filter is live: the chip's own hardened status runs it.
    rails.git_chip(root, hardening=HARDENING)
    assert marker.exists()


@needs_git
def test_without_a_baseline_the_chip_is_unchanged_and_reads_no_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state = _baselined(tmp_path, baseline=False)
    calls = _record_git(monkeypatch, root)
    plain = rails.git_chip(root, hardening=HARDENING)
    plain_calls = calls.copy()
    calls.clear()
    fingerprints: list[Path] = []
    monkeypatch.setattr(GitGuard, "fingerprint", lambda _self, root, *_a: fingerprints.append(root))

    chip = _route_chip(tmp_path, root, state)

    assert chip == {**plain.__dict__}
    assert calls == plain_calls
    assert fingerprints == []


def test_the_route_passes_the_settings_check_only_for_a_code_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    spaces = service_at(tmp_path, state.store)
    spaces.select(OTHER_COMMAND, trusted(spaces, root))
    calls: list[dict[str, Any]] = []

    def record(*_args: object, **kwargs: Any) -> Rails:
        calls.append(kwargs)
        return Rails(
            agents=[],
            gates=[],
            routines=[],
            counts=VaultCounts(knowledge=0, sources=0, inbox=0),
            git=UNKNOWN,
        )

    monkeypatch.setattr(rails, "rails_read", record)
    with _rails_client(tmp_path, spaces, root) as client:
        assert client.get("/api/rails", headers=BEARER).status_code == 200
    [kwargs] = calls
    assert kwargs["hardening"] == HARDENING
    operation = kwargs["operation"]
    assert operation.__func__ is CodeGit.operation
    assert isinstance(operation.__self__, CodeGit)
    assert operation.__self__._guard is not None
