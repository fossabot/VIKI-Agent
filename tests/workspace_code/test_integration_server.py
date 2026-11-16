"""Review, commit, integrate and push wired through `create_app`: a worktree made by its route goes
from review to a local bare remote, and a session with a turn running in it holds the commit. Real
Git in short temp folders; Windows only, as code mutations are."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.test_server import BEARER, EchoDriver
from tests.workspace_code import test_roots_server
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_roots_server import (
    BRANCH,
    JSON_HEADERS,
    SID,
    Built,
    Folders,
    _build,
    _create_worktree,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _folders,
    _open_v2,
    _record,
    authed,
    needs_git,
    running,
)

REVIEW_KEYS = {
    "workspace_id",
    "worktree_id",
    "branch",
    "head_oid",
    "in_progress",
    "entries",
    "truncated",
    "remote",
    "remote_state",
    "remote_oid",
    "pull_request_url",
    "integration",
}
INTEGRATION_KEYS = {
    "target_branch",
    "target_oid",
    "merge_base",
    "fast_forward",
    "ahead",
    "behind",
    "commits",
    "commits_truncated",
    "paths",
    "paths_truncated",
}


class TurnDriver(EchoDriver):
    """An `EchoDriver` whose `busy` a test sets: a turn running, or not."""

    busy = False


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").strip()


def _answer(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _post(client: TestClient, url: str, body: dict[str, Any]) -> Any:
    return client.post(url, content=json.dumps(body), headers=JSON_HEADERS)


def _admission(built: Built, folders: Folders) -> dict[str, Any]:
    workspace = built.spaces.get(folders.code_id, kind="code")
    return {
        "policy_revision": workspace.policy_revision,
        "root_physical_id": workspace.root.physical_id,
    }


def _review(client: TestClient, folders: Folders, worktree_id: str) -> dict[str, Any]:
    url = f"/api/v2/code/{folders.code_id}/review?worktree={worktree_id}"
    review = _answer(client.get(url, headers=BEARER))
    assert set(review) == REVIEW_KEYS
    assert set(review["integration"]) == INTEGRATION_KEYS
    return review


def _commit_body(
    built: Built, folders: Folders, worktree_id: str, review: dict[str, Any]
) -> dict[str, Any]:
    return {
        **_admission(built, folders),
        "worktree_id": worktree_id,
        "branch": review["branch"],
        "head_oid": review["head_oid"],
        "message": "Change a",
        "paths": [
            {"path": entry["path"], "digest": entry["digest"]} for entry in review["entries"]
        ],
    }


@needs_git
def test_a_worktree_change_goes_from_review_to_push_through_the_app(tmp_path: Path) -> None:
    folders = _folders(tmp_path, git=True)
    folder = folders.display
    bare = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c")) / "b"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    _git(folder, "remote", "add", "origin", str(bare))
    base = _head(folder)
    built = _build(folders)
    code = f"/api/v2/code/{folders.code_id}"
    with running(built) as client:
        worktree = _create_worktree(client, built, folders)
        worktree_id, path = worktree["worktree_id"], Path(worktree["path"])
        (path / "a.txt").write_text("beta\n", encoding="utf-8")

        before = _review(client, folders, worktree_id)
        [entry] = before["entries"]
        assert {key: value for key, value in entry.items() if key != "digest"} == {
            "path": "a.txt",
            "change": "modified",
            "kind": "text",
        }
        assert len(entry["digest"]) == 64
        assert {key: before[key] for key in REVIEW_KEYS - {"entries", "integration"}} == {
            "workspace_id": folders.code_id,
            "worktree_id": worktree_id,
            "branch": BRANCH,
            "head_oid": base,
            "in_progress": False,
            "truncated": False,
            "remote": "origin",
            "remote_state": "ready",
            "remote_oid": None,
            "pull_request_url": None,  # a local path is no pull request host
        }
        assert before["integration"] == {
            "target_branch": "main",
            "target_oid": base,
            "merge_base": base,
            "fast_forward": True,
            "ahead": 0,
            "behind": 0,
            "commits": [],
            "commits_truncated": False,
            "paths": [],
            "paths_truncated": False,
        }
        shown = _answer(
            client.get(f"{code}/review/file?path=a.txt&worktree={worktree_id}", headers=BEARER)
        )
        assert set(shown) == {"path", "diff", "content", "reason"}
        assert (shown["path"], shown["content"], shown["reason"]) == ("a.txt", None, None)
        assert "+beta" in shown["diff"]

        committed = _answer(
            _post(client, f"{code}/commit", _commit_body(built, folders, worktree_id, before))
        )
        head = _head(path)
        assert committed == {
            "outcome": "committed",
            "head_oid": head,
            "hook_changed": [],
            "unexpected": [],
        }

        after = _review(client, folders, worktree_id)
        assert (after["head_oid"], after["entries"]) == (head, [])
        integration = after["integration"]
        assert (integration["ahead"], integration["behind"]) == (1, 0)
        assert integration["commits"] == [{"oid": head, "subject": "Change a"}]
        assert integration["paths"] == [{"path": "a.txt", "change": "modified", "kind": "text"}]
        ranged = _answer(
            client.get(
                f"{code}/review/file?path=a.txt&worktree={worktree_id}&range=integration",
                headers=BEARER,
            )
        )
        assert "+beta" in ranged["diff"]

        integrated = _answer(
            _post(
                client,
                f"/api/v2/worktrees/{worktree_id}/integrate",
                {
                    **_admission(built, folders),
                    "target_branch": "main",
                    "target_oid": base,
                    "head_oid": head,
                },
            )
        )
        assert integrated == {"outcome": "integrated", "head_oid": head}
        assert (folder / "a.txt").read_text(encoding="utf-8") == "beta\n"
        assert _git(folder, "status", "--porcelain") == ""

        for branch in (BRANCH, "main"):
            fields = {"remote": "origin", "branch": branch, "head_oid": head}
            pushed = _answer(
                _post(client, f"{code}/push", {**_admission(built, folders), **fields})
            )
            assert pushed == {"outcome": "pushed", "remote_oid": head}
            assert _git(bare, "rev-parse", f"refs/heads/{branch}").strip() == head
            checked = _answer(_post(client, f"{code}/push/check", fields))
            assert checked == {"outcome": "pushed", "remote_oid": head}


@needs_git
def test_a_turn_running_in_the_worktree_holds_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path, git=True)
    # `_build`'s driver factory makes the module's `EchoDriver`.
    monkeypatch.setattr(test_roots_server, "EchoDriver", TurnDriver)
    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        worktree = _create_worktree(client, built, folders)
        worktree_id, path = worktree["worktree_id"], Path(worktree["path"])
        (path / "a.txt").write_text("beta\n", encoding="utf-8")
        _record(client, folders, worktree_id=worktree_id)
        _open_v2(ws, SID)
        body = _commit_body(built, folders, worktree_id, _review(client, folders, worktree_id))
        url = f"/api/v2/code/{folders.code_id}/commit"

        monkeypatch.setattr(TurnDriver, "busy", True)
        refused = _answer(_post(client, url, body), 409)
        assert refused["error"]["code"] == "checkout_busy"
        assert _head(path) == body["head_oid"]

        monkeypatch.setattr(TurnDriver, "busy", False)
        committed = _answer(_post(client, url, body))
    assert committed["outcome"] == "committed" and committed["head_oid"] == _head(path)
    assert [launch.cwd for launch in built.launches] == [path]
