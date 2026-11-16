"""The three new routes (`endpoint`'s trust boundary): the cleanup dry run, the archive
listing and restore. Reuses `test_roots_api`'s rig (real services) and spy (gate checks)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.workspace_code.test_roots_api import (
    ALPHA,
    AUTH,
    BEARER,
    ORIGIN,
    WS,
    WT,
    Spy,
    _committed,
    _no_store,
    _refused,
    _rig,
    _spied,
    post,
)
from tests.workspace_code.test_worktree_sweep import _age
from viki_agent.code import roots_api
from viki_agent.code.worktrees import Worktree, WorktreeError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable, and an empty home, so the user's Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


CLEANUP_URL = f"/api/v2/code/{WS}/worktree-cleanup"
ARCHIVES_URL = f"/api/v2/code/{WS}/worktree-archives"
RESTORE_URL = f"/api/v2/worktrees/{WT}/restore"
RESTORE_FIELDS = {"policy_revision": 1, "root_physical_id": "1:2"}
NEW_GETS = [CLEANUP_URL, ARCHIVES_URL]


class CleanupSpy(Spy):
    """Extends `Spy` with the dry run, archive listing and restore the new routes call.
    `cleanup_enabled` is not recorded: its own success is incidental to every archives call."""

    def cleanup_enabled(self) -> bool:
        return True

    def archives(self, *args: Any) -> list[Any]:
        self.calls.append(("archives", args))
        if self.raises is not None:
            raise self.raises
        return []

    async def plan(self, *args: Any) -> list[Any]:
        await self._enter("plan", args)
        return []

    async def restore(self, *args: Any) -> Worktree:
        await self._enter("restore", args)
        return self._worktree(WS, "b", "ready")


# --- the gate: Bearer, Origin, ids, query, over the spy ------------------------------------------


@pytest.mark.parametrize("url", NEW_GETS)
def test_a_get_needs_bearer(url: str) -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        for headers in ({}, {"Authorization": "Bearer other"}):
            _refused(client.get(url, headers=headers), 401, "unauthorized")
    assert spy.calls == []


def test_a_post_needs_bearer() -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        for headers in ({"Origin": ORIGIN}, {"Authorization": "Bearer other", "Origin": ORIGIN}):
            response = client.post(
                RESTORE_URL,
                content=json.dumps(RESTORE_FIELDS),
                headers={**headers, "Content-Type": "application/json"},
            )
            _refused(response, 401, "unauthorized")
    assert spy.calls == []


@pytest.mark.parametrize("url", NEW_GETS)
def test_a_get_may_omit_origin_but_not_name_a_foreign_one(url: str) -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        _no_store(client.get(url, headers=BEARER), 200)
        _no_store(client.get(url, headers=AUTH), 200)
        assert len(spy.calls) == 2
        foreign = client.get(url, headers={**BEARER, "Origin": "https://evil.invalid"})
        _refused(foreign, 403, "forbidden_origin")
    assert len(spy.calls) == 2


def test_a_post_needs_a_listed_origin() -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        absent = client.post(
            RESTORE_URL,
            content=json.dumps(RESTORE_FIELDS),
            headers={**BEARER, "Content-Type": "application/json"},
        )
        _refused(absent, 403, "forbidden_origin")
        foreign = post(
            client, RESTORE_URL, RESTORE_FIELDS, headers={"Origin": "https://evil.invalid"}
        )
        _refused(foreign, 403, "forbidden_origin")
        assert spy.calls == []
        assert post(client, RESTORE_URL, RESTORE_FIELDS).status_code == 200
    assert len(spy.calls) == 1


@pytest.mark.parametrize("query", ["?x=1", "?a&b", "?=", "?%00"])
@pytest.mark.parametrize("url", NEW_GETS)
def test_any_query_key_on_a_get_is_400_invalid_query(url: str, query: str) -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        _refused(client.get(url + query, headers=AUTH), 400, "invalid_query")
    assert spy.calls == []


@pytest.mark.parametrize("query", ["?x=1", "?a&b"])
def test_any_query_key_on_the_restore_post_is_400_invalid_query(query: str) -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, RESTORE_URL + query, RESTORE_FIELDS), 400, "invalid_query")
    assert spy.calls == []


@pytest.mark.parametrize(
    ("build", "unknown"),
    [
        (lambda raw: f"/api/v2/code/{raw}/worktree-cleanup", "unknown_workspace"),
        (lambda raw: f"/api/v2/code/{raw}/worktree-archives", "unknown_workspace"),
        (lambda raw: f"/api/v2/worktrees/{raw}/restore", "unknown_worktree"),
    ],
)
def test_a_non_canonical_path_id_is_404_before_the_query_or_the_body(
    build: Callable[[str], str], unknown: str
) -> None:
    spy = CleanupSpy()
    with TestClient(_spied(spy)) as client:
        for raw in (ALPHA.upper(), f"{{{ALPHA}}}", ALPHA.replace("-", ""), "w"):
            url = build(raw)
            if url.endswith("restore"):
                response = post(client, url + "?x=1", RESTORE_FIELDS)
            else:
                response = client.get(url + "?x=1", headers=AUTH)
            _refused(response, 404, unknown)
    assert spy.calls == []


# --- refusals over the real services -------------------------------------------------------------


def test_cleanup_archives_and_restore_in_an_untrusted_folder_are_409_trust_required(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, trust=False)
    rig.put_worktree(state="removed", folder=False)
    with TestClient(rig.app()) as client:
        for url in (
            f"/api/v2/code/{rig.workspace_id}/worktree-cleanup",
            f"/api/v2/code/{rig.workspace_id}/worktree-archives",
        ):
            _refused(client.get(url, headers=AUTH), 409, "trust_required", rig)
        restore = post(client, f"/api/v2/worktrees/{WT}/restore", rig.remove_fields())
    _refused(restore, 409, "trust_required", rig)


def test_cleanup_archives_and_restore_for_an_unknown_target_are_404(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    with TestClient(rig.app()) as client:
        for url in (
            f"/api/v2/code/{WS}/worktree-cleanup",
            f"/api/v2/code/{WS}/worktree-archives",
        ):
            _refused(client.get(url, headers=AUTH), 404, "unknown_workspace", rig)
        restore = post(client, f"/api/v2/worktrees/{WT}/restore", rig.remove_fields())
    _refused(restore, 404, "unknown_worktree", rig)


@pytest.mark.parametrize("code", ["no_archive", "branch_moved", "branch_in_use"])
def test_each_new_refusal_has_its_status_and_fixed_message(code: str) -> None:
    spy = CleanupSpy(WorktreeError(code))
    with TestClient(_spied(spy)) as client:
        response = post(client, RESTORE_URL, RESTORE_FIELDS)
    error = _refused(response, 409, code)
    assert error["message"] == roots_api._MESSAGES[code]
    assert len(spy.calls) == 1


# --- the three shapes, over the real git-backed service ------------------------------------------


@needs_git
def test_get_cleanup_answers_the_decision_shape_for_a_fresh_worktree(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    with TestClient(rig.app()) as client:
        worktree = post(client, rig.create_url(), rig.create_fields()).json()["worktree"]
        response = client.get(f"/api/v2/code/{rig.workspace_id}/worktree-cleanup", headers=AUTH)
    body = _no_store(response, 200)
    assert list(body) == ["decisions"]
    assert len(body["decisions"]) == 1
    decision = body["decisions"][0]
    assert set(decision) == {
        "worktree_id",
        "action",
        "reason",
        "idle_since",
        "eligible_at",
        "bytes",
    }
    assert (decision["worktree_id"], decision["action"], decision["reason"]) == (
        worktree["worktree_id"],
        "keep",
        "recent",
    )
    rows, _more = rig.store.list_worktrees(100)
    assert rows[0]["state"] == "ready"  # the dry run changed nothing


@needs_git
async def test_archives_lists_a_swept_worktree_and_restore_brings_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    monkeypatch.setattr(rig.service, "_enabled", lambda: True)
    with TestClient(rig.app()) as client:
        worktree = post(client, rig.create_url(), rig.create_fields()).json()["worktree"]
        (Path(worktree["path"]) / "dirty.txt").write_text("x\n", encoding="utf-8")
        _age(Path(worktree["path"]), datetime.now(UTC) - timedelta(hours=25))
        swept = await rig.service.sweep()
        assert [(d.action, d.reason) for d in swept] == [("archive", "dirty")]

        archived = client.get(f"/api/v2/code/{rig.workspace_id}/worktree-archives", headers=AUTH)
        body = _no_store(archived, 200)
        assert list(body) == ["enabled", "archives"]
        assert body["enabled"] is True
        assert len(body["archives"]) == 1
        archive = body["archives"][0]
        assert set(archive) == {"worktree_id", "oid", "archived_at"}
        assert archive["worktree_id"] == worktree["worktree_id"]

        restored = post(
            client, f"/api/v2/worktrees/{worktree['worktree_id']}/restore", rig.remove_fields()
        )
    body = _no_store(restored, 200)
    assert list(body) == ["worktree"]
    made = body["worktree"]
    assert (made["worktree_id"], made["state"]) == (worktree["worktree_id"], "ready")
    assert (Path(worktree["path"]) / "dirty.txt").is_file()
