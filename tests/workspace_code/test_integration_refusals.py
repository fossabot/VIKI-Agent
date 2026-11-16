"""The six review and integration routes over the real services refuse an untrusted, a
protected-bound and a vault workspace before any Git call or file read, and a private path before
any Git call; a full commit and integrate open nothing under `.git`; and the review modules hold no
forcing, history-rewriting or hook-skipping Git string."""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import shutil
import tempfile
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import viki_agent
from tests.app.test_workspaces import protected_at
from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_code_checkout import (
    Rig,
    _committed,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _head,
    _rig,
)
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_roots_api import BEARER, JSON
from viki_agent import osfs, winproc
from viki_agent.code.code_commit import CodeCommitter
from viki_agent.code.code_integration import CodeIntegration
from viki_agent.code.code_review import CodeReview
from viki_agent.code.integration_api import integration_routes

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
OID = "a" * 40
DIGEST = "0" * 64
PRIVATE = (".git/config", ".GIT/hooks/x", "a/.git/x", ".env")
ROUTES = ("review", "view", "commit", "integrate", "push", "check")
# Banned Git strings. `code_remote` may hold `--all` for its `remote get-url --all` reads only.
BANNED = {
    "--force",
    "-f",
    "--no-verify",
    "-A",
    "--all",
    "reset",
    "checkout",
    "stash",
    "update-ref",
    # also banned: amend, rebase and clean
    "--amend",
    "rebase",
    "clean",
}
SCANNED = (
    "code_checkout",
    "code_review",
    "code_range",
    "code_remote",
    "code_commit",
    "code_integration",
    "integration_api",
)


def _app(rig: Rig) -> Starlette:
    review = CodeReview(rig.store, rig.git)
    idle = frozenset[str]().__contains__
    return Starlette(
        routes=integration_routes(
            review,
            CodeCommitter(review, rig.git, rig.store, rig.coordinator, rig.resolver, busy=idle),
            CodeIntegration(rig.git, rig.store, rig.coordinator, rig.resolver, busy=idle),
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set(),
        )
    )


def _request(
    client: TestClient, route: str, workspace_id: str, worktree_id: str, path: str = "a.txt"
) -> Any:  # TestClient's response and httpx's are two unrelated types to pyright
    code = f"/api/v2/code/{workspace_id}"
    admission = {"policy_revision": 1, "root_physical_id": "1:2"}
    target = {"remote": "origin", "branch": "main", "head_oid": OID}
    if route == "review":
        return client.get(f"{code}/review", headers=BEARER)
    if route == "view":
        quoted = urllib.parse.quote(path, safe="")
        return client.get(f"{code}/review/file?path={quoted}", headers=BEARER)
    url, body = {
        "commit": (
            f"{code}/commit",
            {
                **admission,
                "worktree_id": None,
                "branch": "main",
                "head_oid": OID,
                "message": "change",
                "paths": [{"path": path, "digest": DIGEST}],
            },
        ),
        "integrate": (
            f"/api/v2/worktrees/{worktree_id}/integrate",
            {**admission, "target_branch": "main", "target_oid": OID, "head_oid": "b" * 40},
        ),
        "push": (f"{code}/push", {**admission, **target}),
        "check": (f"{code}/push/check", target),
    }[route]
    return client.post(url, content=json.dumps(body), headers=JSON)


def _code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["error"]["code"]


def _spy_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = winproc.git

    def spy(cwd: str | os.PathLike[str], *args: str, timeout: float = 15.0) -> str:
        calls.append(args)
        return real(cwd, *args, timeout=timeout)

    monkeypatch.setattr(winproc, "git", spy)
    return calls


def _spy_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = osfs.read_regular

    def spy(path: str | os.PathLike[str], limit: int, *, follow_symlinks: bool = True) -> bytes:
        reads.append(os.fspath(path))
        return real(path, limit, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(osfs, "read_regular", spy)
    return reads


def _row(rig: Rig, workspace_id: str) -> str:
    """A `ready` worktree row of the workspace, with a folder at its path."""
    path = Path(tempfile.mkdtemp(dir=rig.short, prefix="w"))
    return rig.row(path, worktree_id=str(uuid.uuid4()), workspace_id=workspace_id)


def _untrusted(rig: Rig) -> str:
    folder = _committed(rig.short / "untrusted")
    workspace, _ = rig.workspaces.register_existing(str(uuid.uuid4()), str(folder), kind="code")
    assert workspace.policy == "existing_readonly"
    return workspace.workspace_id


def _protected_bound(rig: Rig) -> str:
    vault = rig.short / "VIKI"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    protected_at(rig.workspaces, vault)
    copy = _committed(rig.short / "copy")
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    workspace, _ = rig.workspaces.register_existing(str(uuid.uuid4()), str(copy), kind="code")
    assert workspace.policy == "protected"
    return workspace.workspace_id


def _vault(rig: Rig) -> str:
    notes = rig.short / "notes"
    notes.mkdir()
    return rig.workspaces.register_existing(str(uuid.uuid4()), str(notes))[0].workspace_id


@needs_git
@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (_untrusted, 409, "trust_required"),
        (_protected_bound, 409, "trust_required"),
        (_vault, 404, "unknown_workspace"),
    ],
    ids=["untrusted", "protected-bound", "vault"],
)
def test_an_untrusted_protected_or_vault_workspace_is_refused_before_git_or_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any, status: int, code: str
) -> None:
    rig = _rig(tmp_path)
    workspace_id = make(rig)
    worktree_id = _row(rig, workspace_id)
    calls, reads = _spy_git(monkeypatch), _spy_reads(monkeypatch)
    with TestClient(_app(rig)) as client:
        for route in ROUTES:
            refused = _request(client, route, workspace_id, worktree_id)
            assert _code(refused, status) == code, route
            assert (calls, reads) == ([], []), route


@needs_git
@pytest.mark.parametrize("path", PRIVATE)
def test_a_private_path_is_refused_with_no_git_call_and_nothing_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("changed\n", encoding="utf-8")
    (rig.folder / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    index = _git(rig.folder, "ls-files", "--stage")
    calls, reads = _spy_git(monkeypatch), _spy_reads(monkeypatch)
    with TestClient(_app(rig)) as client:
        for route in ("view", "commit"):
            refused = _request(client, route, rig.workspace_id, "", path)
            assert _code(refused, 403) == "private_path", route
    assert (calls, reads) == ([], [])
    assert _git(rig.folder, "ls-files", "--stage") == index
    assert _git(rig.folder, "diff", "--cached", "--name-only") == ""


def _has_git_part(name: str) -> bool:
    return any(part.casefold() == ".git" for part in PurePath(name).parts)


@contextmanager
def _opened(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every path the server process opens or reads meanwhile."""
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    real_read = osfs.read_regular

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(repr(file) if isinstance(file, int) else os.fsdecode(file))
        return real_open(file, *args, **kwargs)

    def spy_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(repr(file) if isinstance(file, int) else os.fsdecode(file))
        return real_io_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path))
        return real_os_open(path, *args, **kwargs)

    def spy_read(path: Any, limit: int, **kwargs: Any) -> bytes:
        opened.append(os.fsdecode(path))
        return real_read(path, limit, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", spy_open)
        patch.setattr(io, "open", spy_io_open)
        patch.setattr(os, "open", spy_os_open)
        patch.setattr(osfs, "read_regular", spy_read)
        yield opened


@needs_git
def test_a_full_commit_and_integrate_open_nothing_under_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree_id, path = rig.worktree()
    (path / "a.txt").write_text("worktree\n", encoding="utf-8")
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    admission = {
        "policy_revision": workspace.policy_revision,
        "root_physical_id": workspace.root.physical_id,
    }
    base = _head(rig.folder)
    code = f"/api/v2/code/{rig.workspace_id}"
    with TestClient(_app(rig)) as client, _opened(monkeypatch) as opened:
        review = client.get(f"{code}/review?worktree={worktree_id}", headers=BEARER).json()
        [entry] = review["entries"]
        body = {
            **admission,
            "worktree_id": worktree_id,
            "branch": review["branch"],
            "head_oid": review["head_oid"],
            "message": "change",
            "paths": [{"path": "a.txt", "digest": entry["digest"]}],
        }
        committed = client.post(f"{code}/commit", content=json.dumps(body), headers=JSON)
        assert committed.status_code == 200, committed.text
        head = committed.json()["head_oid"]
        integrated = client.post(
            f"/api/v2/worktrees/{worktree_id}/integrate",
            content=json.dumps(
                {**admission, "target_branch": "main", "target_oid": base, "head_oid": head}
            ),
            headers=JSON,
        )
        assert integrated.status_code == 200, integrated.text
    assert _head(rig.folder) == head
    assert [name for name in opened if _has_git_part(name)] == []
    # The spies were live: the review's digests read the worktree's file.
    assert any(PurePath(name).name == "a.txt" for name in opened)


# --- the strings the modules hold ---------------------------------------------------------------


def _docstrings(tree: ast.Module) -> set[int]:
    """The ids of every string that is a statement of its own: a docstring, not an argument."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _strings(source: str) -> tuple[ast.Module, list[ast.Constant]]:
    """Every string constant of the source, f-string parts included, docstrings left out."""
    tree = ast.parse(source)
    skipped = _docstrings(tree)
    return tree, [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
    ]


def _offending(value: str) -> bool:
    return value.startswith("+") or any(token in BANNED for token in value.split())


@pytest.mark.parametrize("module", SCANNED)
def test_the_review_modules_hold_no_forcing_or_hook_skipping_git_string(module: str) -> None:
    source = (Path(viki_agent.__file__).parent / "code" / f"{module}.py").read_text(
        encoding="utf-8"
    )
    tree, strings = _strings(source)
    allowed: set[int] = set()
    if module == "code_remote":
        # `--all` only as an argument of a `remote get-url` read.
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            words = [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
            if words[:2] == ["remote", "get-url"]:
                allowed |= {
                    id(arg)
                    for arg in call.args
                    if isinstance(arg, ast.Constant) and arg.value == "--all"
                }
        assert len(allowed) == 2  # its fetch and push URL reads
    found = [
        node.value
        for node in strings
        if isinstance(node.value, str) and _offending(node.value) and id(node) not in allowed
    ]
    assert found == []
    # A `config` call only reads (`--get`).
    for call in ast.walk(tree):
        if isinstance(call, ast.Call):
            words = [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
            if "config" in words:
                assert "--get" in words, words


def test_the_string_scan_sees_a_forcing_argument_in_an_f_string() -> None:
    """The scan's own check: an f-string part and a plain argument are seen, a docstring not."""
    source = '"""reset, checkout."""\ngit.git(root, "push", f"+{oid}:refs/heads/x", "--force")\n'
    _, strings = _strings(source)
    offending = [
        node.value for node in strings if isinstance(node.value, str) and _offending(node.value)
    ]
    assert sorted(offending) == ["+", "--force"]
