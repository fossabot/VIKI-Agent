"""Bot code copies through the real Commit, Integrate and Push services and the code-folder
routes, against synthetic repositories in short temp folders with a local bare `origin`: hooks
off for a Bot copy's Git writes (marker hooks prove which ran), live checks over every copy user,
`git_settings_changed` kept through every catch and mapped by all three route families, and the
Git-settings baseline read and its confirmation. No test reads the real global or system Git
config or reaches a network."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_code_checkout import BRANCH, STAMP, WORKTREE, Rig, _head, _rig
from tests.workspace_code.test_code_commit import _approval, _commit
from tests.workspace_code.test_code_git import _commit as _git_commit
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_roots_api import Spy
from tests.workspace_code.test_session_roots import FORK, SESSION, bot_copy
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code.code_api import GIT_SETTINGS_CHANGED, code_routes
from viki_agent.code.code_checkout import GitSettings, busy_in
from viki_agent.code.code_commit import CodeCommitter
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.code_integration import CodeIntegration
from viki_agent.code.code_review import CodeReview
from viki_agent.code.integration_api import integration_routes
from viki_agent.code.roots_api import MAX_ROOT_REQUESTS, root_routes
from viki_agent.code.worktrees import WorktreeError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
HOOKS = ("pre-commit", "post-commit", "post-merge", "pre-push")
SIMILAR = "viki/20261004-ab13"  # a branch named like the copy's, with no worktree row
BEARER = {"Authorization": "Bearer secret"}
AUTH = {**BEARER, "Origin": ORIGIN}
JSON = {**AUTH, "Content-Type": "application/json"}
HANG_GUARD_S = 2.0  # a guard on events that fire at once, never a bound on real work


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable, an empty home, and an empty global and no system config
    for every Git child, so neither the user's nor the host's configuration is read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    empty = home / "empty-config"
    empty.write_bytes(b"")
    host = {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_SYSTEM": str(empty)}
    host["GIT_CONFIG_NOSYSTEM"] = "1"
    real = winproc.run

    def run(*args: Any, **kwargs: Any) -> Any:
        kwargs["env"] = {**os.environ, **host, **(kwargs.get("env") or {})}
        return real(*args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)


def _runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every process started from here on."""
    seen: list[list[str]] = []
    real = winproc.run

    def run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        seen.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)
    return seen


def _read_settings(runs: list[list[str]]) -> bool:
    """Whether a settings fingerprint ran: only its private parses name `--git-dir=`."""
    return any(arg.startswith("--git-dir=") for argv in runs for arg in argv)


def _guarded(tmp_path: Path, *, baseline: bool = True) -> tuple[Rig, Path]:
    """A trusted folder with marker hooks in its own `.git/hooks` and a local bare `origin`,
    its saved baseline taken after both, and a `CodeGit` that checks it."""
    rig = _rig(tmp_path)
    rig.git = CodeGit(
        rig.workspaces,
        CodeFiles(rig.workspaces),
        git_available=lambda: True,
        bot_code_store=rig.store,
    )
    marker = rig.short / "marker"
    for name in HOOKS:
        hook = rig.folder / ".git" / "hooks" / name
        hook.write_text(
            f'#!/bin/sh\necho {name} >> "{marker.as_posix()}"\n', encoding="utf-8", newline="\n"
        )
        hook.chmod(0o755)
    bare = rig.short / "b"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    _git(rig.folder, "remote", "add", "origin", str(bare))
    if baseline:
        assert rig.store.insert_bot_code_baseline(
            {
                "workspace_id": rig.workspace_id,
                "git_fingerprint": rig.git.settings_fingerprint(rig.folder),
                "created_at": STAMP,
            }
        )
    return rig, marker


def _marks(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").split() if marker.exists() else []


def _saved(rig: Rig) -> str | None:
    found = rig.store.bot_code_baseline(rig.workspace_id)
    return None if found is None else found["git_fingerprint"]


def _ahead(path: Path, text: str = "copy\n") -> str:
    """One more commit in `path` (its hooks may run: callers clear the marker after)."""
    (path / "a.txt").write_text(text, encoding="utf-8")
    _git_commit(path, text.strip())
    return _head(path)


def _integration(rig: Rig, busy: frozenset[str] = frozenset()) -> CodeIntegration:
    return CodeIntegration(
        rig.git, rig.store, rig.coordinator, rig.resolver, busy=busy.__contains__
    )


def _settings(rig: Rig) -> GitSettings:
    return GitSettings(rig.workspaces, rig.store, rig.git, rig.coordinator, rig.resolver)


def _authorized(request: Any) -> bool:
    return request.headers.get("authorization") == "Bearer secret"


def _app(
    rig: Rig,
    *,
    integration: CodeIntegration | None = None,
    spy: Spy | None = None,
    workers: set[asyncio.Task[Any]] | None = None,
) -> Starlette:
    """The three code-folder route families over the real services, as the server mounts them;
    the roots family's own services are a spy."""
    workers = set() if workers is None else workers
    origins = frozenset({ORIGIN})
    review = CodeReview(rig.store, rig.git)
    committer = CodeCommitter(
        review, rig.git, rig.store, rig.coordinator, rig.resolver, busy=lambda _: False
    )
    roots: Any = Spy() if spy is None else spy
    return Starlette(
        routes=[
            *code_routes(
                CodeFiles(rig.workspaces),
                rig.git,
                authorized=_authorized,
                origins=origins,
                workers=workers,
            ),
            *integration_routes(
                review,
                committer,
                integration or _integration(rig),
                authorized=_authorized,
                origins=origins,
                workers=workers,
            ),
            *root_routes(
                roots,
                roots,
                authorized=_authorized,
                origins=origins,
                workers=workers,
                git_settings=_settings(rig),
            ),
        ]
    )


def _admission(rig: Rig) -> dict[str, Any]:
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    return {
        "policy_revision": workspace.policy_revision,
        "root_physical_id": workspace.root.physical_id,
    }


def _settings_url(rig: Rig, workspace_id: str | None = None) -> str:
    return f"/api/v2/code/{workspace_id or rig.workspace_id}/git-settings"


def _confirm_body(rig: Rig, expected: str | None, **changes: Any) -> str:
    return json.dumps({**_admission(rig), "expected_fingerprint": expected, **changes})


def _push_body(rig: Rig, branch: str, head_oid: str) -> str:
    fields = {**_admission(rig), "remote": "origin", "branch": branch, "head_oid": head_oid}
    return json.dumps(fields)


def _refused(rig: Rig, response: Any, status: int, code: str) -> str:
    assert response.status_code == status, response.text
    if not response.url.path.endswith(("/changes", "/diff")):  # code reads set no header
        assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert error["code"] == code
    for secret in (str(rig.folder), rig.folder.as_posix(), str(rig.short), BRANCH):
        assert secret not in response.text
    return error["message"]


async def _code(awaitable: Any) -> str:
    with pytest.raises(CodeError) as refused:
        await awaitable
    return refused.value.code


# --- hooks off for a Bot copy's Git writes ------------------------------------------------------


@needs_git
@pytest.mark.parametrize("bound", [True, False])
async def test_commit_in_a_bot_copy_runs_no_hook_and_an_ordinary_one_keeps_them(
    tmp_path: Path, bound: bool
) -> None:
    rig, marker = _guarded(tmp_path)
    _, path = rig.worktree()
    if bound:
        bot_copy(rig.store, rig.workspace_id, WORKTREE)
    (path / "a.txt").write_text("copy\n", encoding="utf-8")
    approval = _approval(rig, "a.txt", worktree_id=WORKTREE)

    result = await _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE)

    assert _head(path) == result.head_oid and result.hook_changed == ()
    assert _marks(marker) == ([] if bound else ["pre-commit", "post-commit"])
    assert _git(path, "diff", "--cached", "--name-only") == ""


@needs_git
@pytest.mark.parametrize("state", ["bound", "released", None])
async def test_integrate_of_a_bot_copy_fast_forwards_the_folder_with_no_hook(
    tmp_path: Path, state: str | None
) -> None:
    rig, marker = _guarded(tmp_path)
    _, path = rig.worktree()
    if state is not None:
        bot_copy(rig.store, rig.workspace_id, WORKTREE, state=state)
    head = _ahead(path)
    target = _head(rig.folder)
    marker.unlink(missing_ok=True)

    revision, physical_id = _admission(rig).values()
    found = await _integration(rig).integrate(WORKTREE, revision, physical_id, "main", target, head)

    assert found == head == _head(rig.folder)
    assert _marks(marker) == ([] if state else ["post-merge"])


@needs_git
@pytest.mark.parametrize("mapping", ["bound", "released", "ordinary", "similar", "removed"])
def test_push_api_takes_hooks_off_only_for_a_recorded_bot_copy_branch(
    tmp_path: Path, mapping: str
) -> None:
    """The real authenticated route, the folder's own branch: hooks are off only while the
    store maps the branch to a worktree that is not removed and is a Bot copy."""
    rig, marker = _guarded(tmp_path)
    _, path = rig.worktree()
    head = _ahead(path)
    branch = BRANCH
    if mapping in {"bound", "similar"}:
        bot_copy(rig.store, rig.workspace_id, WORKTREE)
    if mapping in {"released", "removed"}:
        bot_copy(rig.store, rig.workspace_id, WORKTREE, state="released")
    if mapping == "removed":  # the row's history stays, the mapping does not
        rig.store.update_worktree_state(WORKTREE, "ready", "removed", STAMP)
    if mapping == "similar":
        _git(rig.folder, "branch", SIMILAR, head)
        branch = SIMILAR
    marker.unlink(missing_ok=True)

    with TestClient(_app(rig)) as client:
        response = client.post(
            f"/api/v2/code/{rig.workspace_id}/push",
            content=_push_body(rig, branch, head),
            headers=JSON,
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"outcome": "pushed", "remote_oid": head}
    assert _git(rig.short / "b", "rev-parse", branch).strip() == head
    assert _marks(marker) == ([] if mapping in {"bound", "released"} else ["pre-push"])


@needs_git
@pytest.mark.parametrize("change", ["bind", "unbind", "settings"])
async def test_push_reads_the_mapping_and_the_settings_again_after_the_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rig, marker = _guarded(tmp_path)
    _, path = rig.worktree()
    head = _ahead(path)
    if change == "unbind":
        bot_copy(rig.store, rig.workspace_id, WORKTREE)
    marker.unlink(missing_ok=True)
    integration = _integration(rig)
    loop = asyncio.get_running_loop()
    targeted = asyncio.Event()
    real = integration._push_target

    def target(*args: Any) -> Any:
        found = real(*args)
        loop.call_soon_threadsafe(targeted.set)
        return found

    monkeypatch.setattr(integration, "_push_target", target)
    transport = httpx.ASGITransport(app=_app(rig, integration=integration))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with rig.coordinator.lease(rig.admit(), (), include_git_common=True):
            call = asyncio.create_task(
                client.post(
                    f"/api/v2/code/{rig.workspace_id}/push",
                    content=_push_body(rig, BRANCH, head),
                    headers=JSON,
                )
            )
            await asyncio.wait_for(targeted.wait(), HANG_GUARD_S)  # checked, now waiting
            if change == "bind":
                bot_copy(rig.store, rig.workspace_id, WORKTREE)
            elif change == "unbind":
                with rig.store._connection(write=True) as connection:
                    connection.execute("DELETE FROM bot_code_copies")
            else:
                _git(rig.folder, "config", "guard.synthetic", "late")
        response = await call

    if change == "settings":
        assert _refused(rig, response, 409, "git_settings_changed") == GIT_SETTINGS_CHANGED
        assert _git(rig.short / "b", "for-each-ref").strip() == ""
    else:
        assert response.status_code == 200, response.text
        assert _git(rig.short / "b", "rev-parse", BRANCH).strip() == head
    assert _marks(marker) == (["pre-push"] if change == "unbind" else [])


# --- live checks --------------------------------------------------------------------------------


@needs_git
async def test_a_bot_thread_member_busy_in_the_copy_blocks_commit_but_not_integrate(
    tmp_path: Path,
) -> None:
    rig, _marker = _guarded(tmp_path)
    _, path = rig.worktree()
    bot_copy(rig.store, rig.workspace_id, WORKTREE, thread=True)  # members with no root row
    assert rig.store.session_roots_in_worktree(WORKTREE) == []
    for member in (SESSION, FORK):
        assert await busy_in(rig.store, {member}.__contains__, rig.workspace_id, WORKTREE)
    assert not await busy_in(rig.store, {FORK}.__contains__, rig.workspace_id, None)

    (path / "a.txt").write_text("copy\n", encoding="utf-8")
    approval = _approval(rig, "a.txt", worktree_id=WORKTREE)
    refused = _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE, busy=frozenset({FORK}))
    assert await _code(refused) == "checkout_busy"

    # Integrate keeps its own guard: the folder's sessions, not the copy's users.
    head = _ahead(path)
    target = _head(rig.folder)
    revision, physical_id = _admission(rig).values()
    busy = frozenset({FORK})
    found = await _integration(rig, busy).integrate(
        WORKTREE, revision, physical_id, "main", target, head
    )
    assert found == head
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": None,
            "display_root": "<repo>",
            "created_at": STAMP,
        }
    )
    later = _ahead(path, "again\n")
    refused = _integration(rig, frozenset({SESSION})).integrate(
        WORKTREE, revision, physical_id, "main", head, later
    )
    assert await _code(refused) == "checkout_busy"


# --- git_settings_changed survives every catch --------------------------------------------------


def _raise_on(monkeypatch: pytest.MonkeyPatch, rig: Rig, method: str, raises: dict[str, Any]):
    """`rig.git.<method>` raising for the first argument named in `raises`."""
    real = getattr(rig.git, method)

    def wrapped(root: Path, *args: str, **kwargs: Any) -> Any:
        for name, error in raises.items():
            if name in args:
                raise error
        return real(root, *args, **kwargs)

    monkeypatch.setattr(rig.git, method, wrapped)


@needs_git
async def test_commit_keeps_a_settings_change_its_commit_call_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _marker = _guarded(tmp_path)
    _, path = rig.worktree()
    bot_copy(rig.store, rig.workspace_id, WORKTREE)
    (path / "a.txt").write_text("copy\n", encoding="utf-8")
    approval = _approval(rig, "a.txt", worktree_id=WORKTREE)
    _raise_on(monkeypatch, rig, "output", {"commit": CodeError("git_settings_changed")})

    refused = _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE)

    assert await _code(refused) == "git_settings_changed"


@needs_git
async def test_integrate_keeps_a_settings_change_its_merge_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _marker = _guarded(tmp_path)
    _, path = rig.worktree()
    bot_copy(rig.store, rig.workspace_id, WORKTREE)
    head = _ahead(path)
    target = _head(rig.folder)
    _raise_on(monkeypatch, rig, "git", {"merge": CodeError("git_settings_changed")})
    revision, physical_id = _admission(rig).values()

    refused = _integration(rig).integrate(WORKTREE, revision, physical_id, "main", target, head)

    assert await _code(refused) == "git_settings_changed"
    assert _head(rig.folder) == target


@needs_git
def test_integrate_recovery_reads_keep_a_settings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a failed merge the recovery reads HEAD and status; a change they meet is kept."""
    rig, _marker = _guarded(tmp_path)
    integration = _integration(rig)
    changed = CodeError("git_settings_changed")
    _raise_on(monkeypatch, rig, "answer", {"rev-parse": changed})
    _raise_on(monkeypatch, rig, "output", {"status": changed})
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        integration._head_or_none(rig.folder)
    with pytest.raises(CodeError, match="^git_settings_changed$"):
        integration._clean_or_false(rig.folder)
    other = CodeError("git_failed")
    _raise_on(monkeypatch, rig, "answer", {"rev-parse": other})
    _raise_on(monkeypatch, rig, "output", {"status": other})
    assert integration._head_or_none(rig.folder) is None
    assert integration._clean_or_false(rig.folder) is False


@needs_git
@pytest.mark.parametrize("failure", ["push", "timeout"])
async def test_push_keeps_a_settings_change_through_its_error_catches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rig, _marker = _guarded(tmp_path)
    head = _head(rig.folder)
    changed = CodeError("git_settings_changed")
    if failure == "push":
        _raise_on(monkeypatch, rig, "git", {"push": changed})
    else:  # a timeout, then the remote read meets the change
        _raise_on(monkeypatch, rig, "git", {"push": TimeoutError(), "ls-remote": changed})
    revision, physical_id = _admission(rig).values()

    refused = _integration(rig).push(
        rig.workspace_id, revision, physical_id, "origin", "main", head
    )

    assert await _code(refused) == "git_settings_changed"
    assert _git(rig.short / "b", "for-each-ref").strip() == ""


# --- every route family maps the change ---------------------------------------------------------


@needs_git
def test_every_route_family_answers_409_with_the_fixed_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig, _marker = _guarded(tmp_path)
    rig.worktree()
    head = _head(rig.folder)
    _git(rig.folder, "config", "guard.synthetic", "changed")
    ws = rig.workspace_id
    admission = _admission(rig)
    commit = {
        **admission,
        "worktree_id": None,
        "branch": "main",
        "head_oid": head,
        "message": "change",
        "paths": [{"path": "a.txt", "digest": "d" * 64}],
    }
    integrate = {**admission, "target_branch": "main", "target_oid": head, "head_oid": head}
    push = {**admission, "remote": "origin", "branch": "main", "head_oid": head}
    check = {"remote": "origin", "branch": "main", "head_oid": head}
    gets = [
        f"/api/v2/code/{ws}/changes",
        f"/api/v2/code/{ws}/diff?path=a.txt",
        f"/api/v2/code/{ws}/review",
        f"/api/v2/code/{ws}/review/file?path=a.txt",
    ]
    posts = [
        (f"/api/v2/code/{ws}/commit", commit),
        (f"/api/v2/worktrees/{WORKTREE}/integrate", integrate),
        (f"/api/v2/code/{ws}/push", push),
        (f"/api/v2/code/{ws}/push/check", check),
    ]
    spy = Spy(CodeError("git_settings_changed"))
    with caplog.at_level(logging.DEBUG), TestClient(_app(rig, spy=spy)) as client:
        answers = [client.get(url, headers=AUTH) for url in gets]
        answers += [client.post(url, content=json.dumps(body), headers=JSON) for url, body in posts]
        create = {**admission, "branch": "viki/20261004-cd34"}
        create_url = f"/api/v2/code/{ws}/worktrees"
        answers.append(client.post(create_url, content=json.dumps(create), headers=JSON))
        spy.raises = WorktreeError("git_settings_changed")
        answers.append(client.post(create_url, content=json.dumps(create), headers=JSON))
        # The baseline read answers what is stored, without Git, while the folder is refused.
        stored = client.get(_settings_url(rig), headers=AUTH)
    for answer in answers:
        assert _refused(rig, answer, 409, "git_settings_changed") == GIT_SETTINGS_CHANGED
    assert stored.json() == {"fingerprint": _saved(rig)}
    assert _head(rig.folder) == head
    for record in caplog.records:
        assert rig.short.name not in record.getMessage() and BRANCH not in record.getMessage()


# --- the Git-settings baseline: read and confirm ------------------------------------------------


@needs_git
def test_the_read_answers_only_the_stored_fingerprint_and_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _marker = _guarded(tmp_path, baseline=False)
    runs = _runs(monkeypatch)
    with TestClient(_app(rig)) as client:
        absent = client.get(_settings_url(rig), headers=BEARER)
        rig.store.insert_bot_code_baseline(
            {"workspace_id": rig.workspace_id, "git_fingerprint": "e" * 64, "created_at": STAMP}
        )
        present = client.get(_settings_url(rig), headers=AUTH)
    assert absent.status_code == present.status_code == 200
    assert absent.headers["cache-control"] == "no-store"
    assert absent.json() == {"fingerprint": None}
    assert present.json() == {"fingerprint": "e" * 64}
    assert runs == []


@needs_git
def test_the_routes_refuse_before_any_git_or_store_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _marker = _guarded(tmp_path)
    saved = _saved(rig)
    assert saved is not None
    _git(rig.folder, "config", "guard.synthetic", "changed")
    other = rig.short / "other"
    other.mkdir()
    other_id = trusted(rig.workspaces, other)
    runs = _runs(monkeypatch)
    url = _settings_url(rig)
    good = _confirm_body(rig, saved)
    json_only = {"Content-Type": "application/json"}
    with TestClient(_app(rig)) as client:
        cases = [
            (client.get(url), 401, "unauthorized"),
            (client.post(url, content=good, headers={**json_only, "Origin": ORIGIN}), 401, ""),
            (client.post(url, content=good, headers={**BEARER, **json_only}), 403, ""),
            (client.get(url, headers={**BEARER, "Origin": "https://evil.invalid"}), 403, ""),
            (client.get(f"{url}?fingerprint=1", headers=AUTH), 400, "invalid_query"),
            (client.post(f"{url}?x=1", content=good, headers=JSON), 400, "invalid_query"),
            (client.get(_settings_url(rig, rig.workspace_id.upper()), headers=AUTH), 404, ""),
            (client.get(_settings_url(rig, str(uuid.uuid4())), headers=AUTH), 404, ""),
            (
                client.post(_settings_url(rig, str(uuid.uuid4())), content=good, headers=JSON),
                404,
                "",
            ),
            (client.post(url, content=good, headers=AUTH), 400, "invalid_body"),
            (client.post(url, content=good + " x", headers=JSON), 400, "invalid_body"),
            (client.post(url, content="x" * 5000, headers=JSON), 413, "too_large"),
        ]
        bodies = [
            _confirm_body(rig, saved.upper()),
            _confirm_body(rig, saved[:-1]),
            _confirm_body(rig, None),
            _confirm_body(rig, saved, workspace_id=rig.workspace_id),
            _confirm_body(rig, saved, fingerprint=saved),
            _confirm_body(rig, saved, policy_revision=True),
            json.dumps({**_admission(rig)}),
            json.dumps([saved]),
        ]
        cases += [
            (client.post(url, content=body, headers=JSON), 400, "invalid_body") for body in bodies
        ]
    for response, status, code in cases:
        assert response.status_code == status, response.text
        assert response.headers["cache-control"] == "no-store"
        if code:
            assert response.json()["error"]["code"] == code
        assert "fingerprint" not in response.json()
    assert runs == []
    # Another trusted folder cannot be confirmed with this folder's admission: admission's own
    # Git may run, the settings are never read.
    with TestClient(_app(rig)) as client:
        crossed = client.post(_settings_url(rig, other_id), content=good, headers=JSON)
    _refused(rig, crossed, 409, "workspace_changed")
    assert not _read_settings(runs)
    assert _saved(rig) == saved


@needs_git
def test_confirm_replaces_only_the_reviewed_baseline_and_actions_resume(
    tmp_path: Path,
) -> None:
    rig, _marker = _guarded(tmp_path)
    saved = _saved(rig)
    _git(rig.folder, "config", "guard.synthetic", "changed")
    url = _settings_url(rig)
    with TestClient(_app(rig)) as client:
        refused = client.get(f"/api/v2/code/{rig.workspace_id}/changes", headers=AUTH)
        _refused(rig, refused, 409, "git_settings_changed")
        reviewed = client.get(url, headers=AUTH).json()["fingerprint"]
        assert reviewed == saved
        confirmed = client.post(url, content=_confirm_body(rig, reviewed), headers=JSON)
        assert confirmed.status_code == 200, confirmed.text
        current = confirmed.json()
        assert set(current) == {"fingerprint"} and current["fingerprint"] != saved
        assert current["fingerprint"] == _saved(rig) == rig.git.settings_fingerprint(rig.folder)
        assert client.get(f"/api/v2/code/{rig.workspace_id}/changes", headers=AUTH).is_success
        # The dialog that still shows the old baseline cannot confirm over the new one.
        stale = client.post(url, content=_confirm_body(rig, reviewed), headers=JSON)
        _refused(rig, stale, 409, "baseline_changed")
    assert _saved(rig) == current["fingerprint"]


@needs_git
def test_confirm_never_creates_a_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, _marker = _guarded(tmp_path, baseline=False)
    current = rig.git.settings_fingerprint(rig.folder)
    runs = _runs(monkeypatch)
    with TestClient(_app(rig)) as client:
        refused = client.post(_settings_url(rig), content=_confirm_body(rig, current), headers=JSON)
    _refused(rig, refused, 409, "baseline_changed")
    assert _saved(rig) is None
    assert not _read_settings(runs)


@needs_git
@pytest.mark.parametrize("change", ["untrusted", "revision"])
def test_confirm_refuses_after_a_trust_or_root_change(tmp_path: Path, change: str) -> None:
    rig, _marker = _guarded(tmp_path)
    saved = _saved(rig)
    _git(rig.folder, "config", "guard.synthetic", "changed")
    if change == "untrusted":
        rig.workspaces.set_policy(str(uuid.uuid4()), rig.workspace_id, 2, "existing_readonly", [])
        body = _confirm_body(rig, saved)  # the current revision: trust itself refuses
    else:
        body = _confirm_body(rig, saved, policy_revision=_admission(rig)["policy_revision"] + 1)
    with TestClient(_app(rig)) as client:
        response = client.post(_settings_url(rig), content=body, headers=JSON)
        read = client.get(_settings_url(rig), headers=AUTH)
    assert response.status_code == 409, response.text
    expected = "trust_required" if change == "untrusted" else "workspace_changed"
    assert response.json()["error"]["code"] == expected
    assert read.status_code == (409 if change == "untrusted" else 200)
    assert _saved(rig) == saved


@needs_git
@pytest.mark.parametrize("unsafe", ["outside_include", "oversized_hook"])
def test_confirm_refuses_settings_it_cannot_admit_safely(tmp_path: Path, unsafe: str) -> None:
    rig, _marker = _guarded(tmp_path)
    saved = _saved(rig)
    if unsafe == "outside_include":
        outside = rig.short / "outside"
        outside.write_text("[guard]\n synthetic = outside\n", encoding="utf-8")
        _git(rig.folder, "config", "include.path", outside.as_posix())
    else:
        (rig.folder / ".git" / "hooks" / "big").write_bytes(b"#" * (4 * 1024 * 1024 + 1))
    with TestClient(_app(rig)) as client:
        response = client.post(_settings_url(rig), content=_confirm_body(rig, saved), headers=JSON)
    message = _refused(rig, response, 503, "git_settings_unavailable")
    assert message == "VIKI Agent can't read this folder's Git settings safely."
    assert _saved(rig) == saved


@needs_git
async def test_two_confirms_of_one_review_land_once(tmp_path: Path) -> None:
    rig, _marker = _guarded(tmp_path)
    saved = _saved(rig)
    _git(rig.folder, "config", "guard.synthetic", "changed")
    transport = httpx.ASGITransport(app=_app(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = _confirm_body(rig, saved)
        answers = await asyncio.gather(
            *(client.post(_settings_url(rig), content=body, headers=JSON) for _ in range(2))
        )
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    lost = next(answer for answer in answers if answer.status_code == 409)
    assert lost.json()["error"]["code"] == "baseline_changed"
    assert _saved(rig) == rig.git.settings_fingerprint(rig.folder) != saved


@needs_git
async def test_the_settings_routes_share_the_roots_capacity(tmp_path: Path) -> None:
    rig, _marker = _guarded(tmp_path)
    spy = Spy()
    spy.gate, spy.entered = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(rig, spy=spy, workers=workers))
    create = json.dumps({**_admission(rig), "branch": "viki/20261004-cd34"})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(
                client.post(
                    f"/api/v2/code/{rig.workspace_id}/worktrees", content=create, headers=JSON
                )
            )
            for _ in range(MAX_ROOT_REQUESTS)
        ]
        await asyncio.wait_for(spy.entered.wait(), HANG_GUARD_S)
        read = await client.get(_settings_url(rig), headers=AUTH)
        confirm = await client.post(
            _settings_url(rig), content=_confirm_body(rig, _saved(rig)), headers=JSON
        )
        spy.gate.set()
        await asyncio.gather(*calls)
    _refused(rig, read, 429, "overloaded")
    _refused(rig, confirm, 429, "overloaded")


def test_without_a_settings_service_the_routes_are_absent(tmp_path: Path) -> None:
    spy: Any = Spy()
    routes = root_routes(spy, spy, authorized=_authorized, origins=frozenset(), workers=set())
    assert not any("git-settings" in getattr(route, "path", "") for route in routes)
