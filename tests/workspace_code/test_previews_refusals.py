"""The preview routes over the real services: start and open refuse an untrusted, a protected-bound
and a vault folder, a worktree that is not ready and VIKI's own port, with no process started, no
window opened and no Git call; the preview modules hold no shell string and only `preview_windows`
holds the browser's switches; and the chat socket has no preview message."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.workspace_code.test_checks_refusals import SHELL_WORDS, _source, _spy_starts
from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_code_checkout import (
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
from tests.workspace_code.test_preview_windows import EDGE, Spawner
from tests.workspace_code.test_roots_api import JSON
from viki_agent import config, events
from viki_agent.code import previews
from viki_agent.code.preview_windows import PreviewWindows
from viki_agent.code.previews import PreviewService, _Preview
from viki_agent.code.previews_api import preview_routes
from viki_agent.code.session_roots import SessionRoots
from viki_agent.winproc import ShellRun

PREVIEW = "00000000-0000-4000-8000-0000000000a1"
WORKTREE = "00000000-0000-4000-8000-000000000001"
PORT = 5000
SCANNED = ("previews", "preview_windows", "previews_api")
BROWSER_SWITCHES = ("--proxy-server", "--app=")


@pytest.fixture(autouse=True)
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route reaches the root gate on every platform."""
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", True)


def _refused(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    workspace_id: str,
    worktree_id: str | None = None,
    port: int = PORT,
) -> Any:  # TestClient's response and httpx's are two unrelated types to pyright
    """The answer to one start in `workspace_id`, or one open of a live preview there, which
    started no process, opened no window and called no Git."""
    roots = SessionRoots(
        rig.store, rig.workspaces, exists=lambda _sid: False, actor=lambda _sid: None
    )
    spawn = Spawner()
    service = PreviewService(
        rig.store, rig.workspaces, roots, PreviewWindows(EDGE, rig.short / "data", spawn=spawn)
    )
    # A preview that went live before its folder was refused; its shell is never touched.
    live = _Preview(PREVIEW, workspace_id, None, "echo viki", PORT)
    live.shell = cast(ShellRun, object())
    service._previews[PREVIEW] = live
    app = Starlette(
        routes=preview_routes(
            service,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set(),
        )
    )
    calls, starts = _spy_git(monkeypatch), _spy_starts(monkeypatch)
    if route == "start":
        body: dict[str, Any] = {"worktree_id": worktree_id, "command": "echo viki", "port": port}
        url = f"/api/v2/code/{workspace_id}/previews"
    else:
        body, url = {}, f"/api/v2/previews/{PREVIEW}/open"
    with TestClient(app) as client:
        response = client.post(url, content=json.dumps(body), headers=JSON)
    assert (calls, starts, spawn.calls) == ([], [], []), route
    return response


def _code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["error"]["code"]


@needs_git
@pytest.mark.parametrize("route", ["start", "open"])
@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (_untrusted, 409, "trust_required"),
        (_protected_bound, 409, "trust_required"),
        (_vault, 404, "unknown_workspace"),
    ],
    ids=["untrusted", "protected-bound", "vault"],
)
def test_an_untrusted_protected_or_vault_folder_is_refused_before_a_process_or_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, make: Any, status: int, code: str
) -> None:
    rig = _rig(tmp_path)
    assert _code(_refused(rig, monkeypatch, route, make(rig)), status) == code


@needs_git
@pytest.mark.parametrize("state", ["removed", "kept", "removing"])
def test_a_worktree_that_is_not_ready_is_refused_before_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    rig = _rig(tmp_path)
    rig.row(Path(tempfile.mkdtemp(dir=rig.short, prefix="w")), state=state, worktree_id=WORKTREE)
    response = _refused(rig, monkeypatch, "start", rig.workspace_id, WORKTREE)
    assert _code(response, 409) == "worktree_not_ready"


@needs_git
def test_vikis_own_port_is_refused_before_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    response = _refused(rig, monkeypatch, "start", rig.workspace_id, port=config.PORT)
    assert _code(response, 409) == "port_reserved"


# --- what the modules hold ----------------------------------------------------------------------


def _held(module: str, words: tuple[str, ...]) -> list[str]:
    _, strings = _strings(_source(module))
    return [
        node.value
        for node in strings
        if isinstance(node.value, str) and any(word in node.value.casefold() for word in words)
    ]


@pytest.mark.parametrize("module", SCANNED)
def test_the_preview_modules_hold_no_shell_string(module: str) -> None:
    assert _held(module, SHELL_WORDS) == []


def test_only_the_window_module_holds_the_browser_switches() -> None:
    holding = {module for module in SCANNED if _held(module, BROWSER_SWITCHES)}
    assert holding == {"preview_windows"}


def test_the_chat_socket_has_no_preview_message() -> None:
    names = [*events.CLIENT_MESSAGES, *events.EVENTS]
    assert [name for name in names if name.startswith("preview")] == []
