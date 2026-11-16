"""The checks routes over the real services: list, create and start refuse an untrusted, a
protected-bound and a vault workspace, and a worktree that is not ready, with no process started and
no Git call; the check modules hold no shell string; only the form's route creates a check; and the
chat socket has no check message."""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import viki_agent
from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_code_checkout import (
    STAMP,
    Rig,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _rig,
)
from tests.workspace_code.test_integration_refusals import (
    _protected_bound,
    _spy_git,
    _strings,
    _untrusted,
    _vault,
    needs_git,
)
from tests.workspace_code.test_roots_api import BEARER, JSON
from viki_agent import events, winproc
from viki_agent.code import code_checks
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.checks_api import check_routes
from viki_agent.code.code_checks import CheckService
from viki_agent.code.session_roots import SessionRoots

CHECK = "00000000-0000-4000-8000-0000000000c1"
WORKTREE = "00000000-0000-4000-8000-000000000001"
SHELL_WORDS = ("cmd.exe", "powershell", "/d /s /c")
SCANNED = ("code_checks", "check_runs", "check_output", "checks_api")


@pytest.fixture(autouse=True)
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route reaches the root gate on every platform."""
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)


def _app(rig: Rig) -> Starlette:
    roots = SessionRoots(
        rig.store, rig.workspaces, exists=lambda _sid: False, actor=lambda _sid: None
    )
    return Starlette(
        routes=check_routes(
            CheckService(rig.store, rig.workspaces),
            CheckRunner(rig.store, rig.workspaces, rig.git, roots),
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set(),
        )
    )


def _spy_starts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    starts: list[str] = []

    def spy(command: str, **_kwargs: Any) -> winproc.ShellRun:
        starts.append(command)
        raise AssertionError("a refused request started a process")

    monkeypatch.setattr(winproc, "start_shell", spy)
    return starts


def _check(rig: Rig, workspace_id: str) -> str:
    """A readable check of the workspace, stored as the form would store it."""
    body = code_checks.seal(CHECK, "check", "echo viki")
    rig.store.insert_check(
        {
            "check_id": CHECK,
            "workspace_id": workspace_id,
            "body": body,
            "timeout_s": 600,
            "created_at": STAMP,
        },
        limit=code_checks.MAX_CHECKS,
    )
    return CHECK


def _request(
    client: TestClient, route: str, workspace_id: str, worktree_id: str | None
) -> Any:  # TestClient's response and httpx's are two unrelated types to pyright
    if route == "list":
        query = "" if worktree_id is None else f"?worktree={worktree_id}"
        return client.get(f"/api/v2/code/{workspace_id}/checks{query}", headers=BEARER)
    if route == "create":
        body = {"name": "check", "command": "echo viki", "timeout_s": 600}
        url = f"/api/v2/code/{workspace_id}/checks"
    else:
        body = {"worktree_id": worktree_id}
        url = f"/api/v2/checks/{CHECK}/runs"
    return client.post(url, content=json.dumps(body), headers=JSON)


def _refused(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    workspace_id: str,
    worktree_id: str | None = None,
) -> Any:
    """The answer to one request for the check of `workspace_id`, which started nothing."""
    _check(rig, workspace_id)
    calls, starts = _spy_git(monkeypatch), _spy_starts(monkeypatch)
    with TestClient(_app(rig)) as client:
        response = _request(client, route, workspace_id, worktree_id)
    assert (calls, starts) == ([], []), route
    return response


def _code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["error"]["code"]


@needs_git
@pytest.mark.parametrize("route", ["list", "create", "start"])
@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (_untrusted, 409, "trust_required"),
        (_protected_bound, 409, "trust_required"),
        (_vault, 404, "unknown_workspace"),
    ],
    ids=["untrusted", "protected-bound", "vault"],
)
def test_an_untrusted_protected_or_vault_workspace_is_refused_before_git_or_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, make: Any, status: int, code: str
) -> None:
    rig = _rig(tmp_path)
    assert _code(_refused(rig, monkeypatch, route, make(rig)), status) == code


@needs_git
@pytest.mark.parametrize("route", ["list", "start"])
@pytest.mark.parametrize("state", ["removing", "kept", "removed", "missing"])
def test_a_worktree_that_is_not_ready_is_refused_before_git_or_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, state: str
) -> None:
    rig = _rig(tmp_path)
    folder = Path(tempfile.mkdtemp(dir=rig.short, prefix="w"))
    if state == "missing":  # a ready row whose folder is gone
        folder.rmdir()
    rig.row(folder, state="ready" if state == "missing" else state, worktree_id=WORKTREE)
    response = _refused(rig, monkeypatch, route, rig.workspace_id, WORKTREE)
    assert _code(response, 409) == "worktree_not_ready"


# --- what the modules hold ----------------------------------------------------------------------


def _source(module: str) -> str:
    return (Path(viki_agent.__file__).parent / "code" / f"{module}.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("module", SCANNED)
def test_the_check_modules_hold_no_shell_string(module: str) -> None:
    _, strings = _strings(_source(module))
    found = [
        node.value
        for node in strings
        if isinstance(node.value, str)
        and any(word in node.value.casefold() for word in SHELL_WORDS)
    ]
    assert found == []


def _modules_naming(name: str) -> set[str]:
    """The `src/viki_agent` modules that define, call or reference `name`."""
    found: set[str] = set()
    for path in Path(viki_agent.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                (isinstance(node, ast.Name) and node.id == name)
                or (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.FunctionDef) and node.name == name)
            ):
                found.add(path.stem)
    return found


def test_only_the_form_route_creates_a_check() -> None:
    assert _modules_naming("insert_check") == {"storage", "code_checks"}
    assert _modules_naming("add_check") == {"code_checks", "checks_api"}


def test_the_chat_socket_has_no_check_message() -> None:
    assert [name for name in events.CLIENT_MESSAGES if name.startswith("check")] == []
