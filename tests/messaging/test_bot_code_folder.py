"""A Bot's one code folder grant and the release of its copies, on synthetic app state."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.messaging.test_bots import _STAMP, _attempt, _bind_chat, _bot, _id, _routine, _vault
from tests.messaging.test_bots import service as service
from tests.messaging.test_bots_api import BASE, BEARER, HANG_GUARD_S, ORIGIN, WRITE, Rig
from tests.persistence.test_storage_bot_code import _insert
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.messaging import bots, bots_api
from viki_agent.messaging.bot_threads import BotThreadService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
ROOT = Path("synthetic-root")


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every real Git case unavailable."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


class FakeGit:
    """CodeGit's repository and HEAD checks, recorded; `error` is the refusal code."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, object]] = []
        self.before_head: Any = None

    def repository(self, workspace_id: str) -> Path:
        self.calls.append(("repository", workspace_id))
        if self.error is not None:
            raise CodeError(self.error)
        return ROOT

    def head(self, root: Path) -> str:
        self.calls.append(("head", root))
        if self.before_head is not None:
            self.before_head()
        return "a" * 40


def _bind(service: bots.BotService, fake: FakeGit) -> FakeGit:
    service.bind_code_git(cast(CodeGit, fake))
    return fake


def _grants(service: bots.BotService) -> list[tuple[str, str]]:
    with service.store._connection() as connection:
        return connection.execute(
            "SELECT bot_id,workspace_id FROM bot_code_folders ORDER BY bot_id"
        ).fetchall()


def _states(service: bots.BotService) -> dict[str, str]:
    with service.store._connection() as connection:
        return dict(connection.execute("SELECT worktree_id,state FROM bot_code_copies"))


def _copy(
    service: bots.BotService,
    bot_id: str,
    workspace_id: str,
    *,
    session_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    worktree_id = _id()
    with service.store._connection(write=True) as connection:
        _insert(
            connection,
            "worktrees",
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id,
                "path": f"synthetic-worktrees/{worktree_id}",
                "branch": f"viki/bot/{worktree_id[:8]}",
                "base_oid": "a" * 40,
                "state": "ready",
                "created_at": _STAMP,
                "updated_at": _STAMP,
            },
        )
        _insert(
            connection,
            "bot_code_copies",
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id,
                "bot_id": bot_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "state": "bound",
                "created_at": _STAMP,
            },
        )
    return worktree_id


def _group(service: bots.BotService, workspace_id: str, members: list[str]) -> str:
    thread_id = _id()
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bot_threads VALUES (?,?,1,?,1,?,?,?)",
            (thread_id, workspace_id, b"sealed", "a" * 64, _STAMP, _STAMP),
        )
        connection.executemany(
            "INSERT INTO bot_thread_sessions VALUES (?,?)",
            [(thread_id, session_id) for session_id in members],
        )
    return thread_id


# --- service ----------------------------------------------------------------------------------


def test_grant_defaults_off_and_unbound_git_refuses_only_a_folder(
    service: bots.BotService,
) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    assert bot["code_folder"] is None and service.code_git is None
    with pytest.raises(bots.BotError, match="code_folder_git_unavailable"):
        service.set_code_folder(bot["bot_id"], 1, folder)
    assert _grants(service) == [] and service.record(bot["bot_id"])["state_revision"] == 1
    cleared = service.set_code_folder(bot["bot_id"], 1, None)
    assert cleared["state_revision"] == 2 and cleared["code_folder"] is None


def test_bound_git_checks_repository_then_head_and_projects_the_grant(
    service: bots.BotService,
) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    fake = _bind(service, FakeGit())
    assert service.code_git is fake
    granted = service.set_code_folder(bot["bot_id"], 1, folder)
    assert fake.calls == [("repository", folder), ("head", ROOT)]
    assert granted["state_revision"] == 2
    assert granted["code_folder"] == {"workspace_id": folder, "name": "synthetic"}
    assert service.list() == [granted]
    assert _grants(service) == [(bot["bot_id"], folder)]


def test_stale_revision_refuses_before_git_and_writes_nothing(service: bots.BotService) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    fake = _bind(service, FakeGit())
    before = service.store.path.read_bytes()
    for target in (folder, None):
        with pytest.raises(bots.BotError, match="conflict"):
            service.set_code_folder(bot["bot_id"], 2, target)
    assert fake.calls == [] and service.store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("unknown_workspace", "code_folder_missing"),
        ("root_unavailable", "code_folder_missing"),
        ("trust_required", "code_folder_untrusted"),
        ("not_repository", "code_folder_not_repository"),
        ("not_repository_root", "code_folder_not_repository"),
        ("no_commits", "code_folder_no_commits"),
        ("git_unavailable", "code_folder_git_unavailable"),
        ("git_settings_changed", "git_settings_changed"),
        ("git_failed", "unavailable"),
    ],
)
def test_git_refusals_map_to_fixed_codes_without_a_grant(
    service: bots.BotService, error: str, code: str
) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    _bind(service, FakeGit(error))
    with pytest.raises(bots.BotError) as refused:
        service.set_code_folder(bot["bot_id"], 1, folder)
    assert refused.value.code == code
    assert _grants(service) == [] and service.record(bot["bot_id"])["state_revision"] == 1


@pytest.mark.parametrize(
    "change", ["policy='existing_readonly'", "bootstrap='pending'", "kind='code'"]
)
def test_bot_needs_an_ordinary_vault_before_any_git(service: bots.BotService, change: str) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    fake = _bind(service, FakeGit())
    with service.store._connection(write=True) as connection:
        connection.execute(
            f"UPDATE workspaces SET {change} WHERE workspace_id=?", (bot["workspace_id"],)
        )
    with pytest.raises(bots.BotError, match="code_folder_needs_ordinary"):
        service.set_code_folder(bot["bot_id"], 1, folder)
    assert fake.calls == [] and _grants(service) == []


def test_a_vault_or_unknown_id_is_a_missing_folder(service: bots.BotService) -> None:
    bot = _bot(service)
    _bind(service, FakeGit())
    for target in (bot["workspace_id"], _id()):
        with pytest.raises(bots.BotError, match="code_folder_missing"):
            service.set_code_folder(bot["bot_id"], 1, target)
    assert _grants(service) == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", "untrusted"),
        ("UPDATE workspaces SET physical_id='9:9' WHERE workspace_id=?", "missing"),
        ("DELETE FROM workspaces WHERE workspace_id=?", "missing"),
    ],
)
def test_registration_changed_during_git_refuses_at_commit(
    service: bots.BotService, change: str, code: str
) -> None:
    bot, folder = _bot(service), _vault(service, kind="code")
    fake = _bind(service, FakeGit())

    def change_registration() -> None:
        with service.store._connection(write=True) as connection:
            connection.execute(change, (folder,))

    fake.before_head = change_registration
    with pytest.raises(bots.BotError, match=f"code_folder_{code}"):
        service.set_code_folder(bot["bot_id"], 1, folder)
    assert _grants(service) == [] and service.record(bot["bot_id"])["state_revision"] == 1


def test_switch_and_clear_keep_existing_copies_bound_to_the_old_folder(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    first, second = _vault(service, kind="code"), _vault(service, kind="code")
    _bind(service, FakeGit())
    service.set_code_folder(bot["bot_id"], 1, first)
    copy = _copy(service, bot["bot_id"], first, session_id=bot["chat_session_id"])
    switched = service.set_code_folder(bot["bot_id"], 2, second)
    assert switched["code_folder"]["workspace_id"] == second
    assert service.store.bound_copy(bot["chat_session_id"]) == {
        "worktree_id": copy,
        "workspace_id": first,
        "bot_id": bot["bot_id"],
        "session_id": bot["chat_session_id"],
        "thread_id": None,
        "state": "bound",
        "created_at": _STAMP,
    }
    assert service.set_code_folder(bot["bot_id"], 3, None)["code_folder"] is None
    assert _grants(service) == [] and _states(service) == {copy: "bound"}


def test_remove_releases_every_chat_copy_of_the_bot_but_not_group_or_other_copies(
    service: bots.BotService,
) -> None:
    bot, other = _bot(service), _bot(service)
    folder = _vault(service, kind="code")
    _bind(service, FakeGit())
    service.set_code_folder(bot["bot_id"], 1, folder)
    service.set_code_folder(other["bot_id"], 1, folder)
    main = _copy(service, bot["bot_id"], folder, session_id=bot["chat_session_id"])
    task = _copy(service, bot["bot_id"], folder, session_id=_id())
    group = _copy(service, bot["bot_id"], folder, thread_id=_id())
    foreign = _copy(service, other["bot_id"], folder, session_id=other["chat_session_id"])
    before = service.store.path.read_bytes()
    with pytest.raises(bots.BotError, match="conflict"):
        service.remove(bot["bot_id"], 1)
    assert service.store.path.read_bytes() == before
    service.remove(bot["bot_id"], 2)
    assert _states(service) == {
        main: "released",
        task: "released",
        group: "bound",
        foreign: "bound",
    }
    assert _grants(service) == [(other["bot_id"], folder)]


def test_workspace_reset_releases_and_ungrants_only_its_bots(service: bots.BotService) -> None:
    bot, other = _bot(service), _bot(service)
    folder = _vault(service, kind="code")
    _bind(service, FakeGit())
    service.set_code_folder(bot["bot_id"], 1, folder)
    service.set_code_folder(other["bot_id"], 1, folder)
    mine = _copy(service, bot["bot_id"], folder, session_id=_id())
    theirs = _copy(service, other["bot_id"], folder, session_id=_id())
    service.reset(bot["workspace_id"])
    assert _states(service) == {mine: "released", theirs: "bound"}
    assert _grants(service) == [(other["bot_id"], folder)]


def test_group_removal_releases_its_copies_before_membership_and_only_after_cas(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    folder = _vault(service, kind="code")
    member = _id()
    thread = _group(service, bot["workspace_id"], [member])
    kept = _group(service, bot["workspace_id"], [member])
    copy = _copy(service, bot["bot_id"], folder, thread_id=thread)
    other = _copy(service, bot["bot_id"], folder, thread_id=kept)
    threads = BotThreadService(service.store, service.workspaces)
    with pytest.raises(bots.BotError, match="conflict"):
        threads.remove(bot["workspace_id"], thread, 2)
    assert _states(service) == {copy: "bound", other: "bound"}
    assert service.store.sessions_using_worktree(copy) == [member]
    assert threads.remove(bot["workspace_id"], thread, 1) == {"removed": True}
    assert _states(service) == {copy: "released", other: "bound"}
    assert service.store.sessions_using_worktree(copy) == []


def test_threads_exclude_known_routine_origin_after_prune_and_keep_unknown_history(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    routine, unknown, interactive = _id(), _id(), _id()
    for session_id in (routine, unknown, interactive):
        _bind_chat(service, {**bot, "chat_session_id": session_id})
    attempt = _attempt(service, _routine(service, bot))
    _bind_chat(service, {**bot, "chat_session_id": attempt.session_id})
    with service.store._connection(write=True) as connection:
        connection.executemany(
            "UPDATE session_profile_bindings SET launch_origin=? WHERE session_id=?",
            [
                ("routine", routine),
                ("routine", attempt.session_id),
                ("interactive", interactive),
            ],
        )
    listed = {row["session_id"] for row in service.threads(bot["bot_id"])["threads"]}
    assert listed == {unknown, interactive}
    with service.store._connection(write=True) as connection:  # attempt retention pruned
        connection.execute("DELETE FROM task_attempts")
    assert service.store.profile_launch_origin(attempt.session_id) == "routine"
    listed = {row["session_id"] for row in service.threads(bot["bot_id"])["threads"]}
    assert listed == {unknown, interactive}
    assert service.store.profile_launch_origin(unknown) == "unknown"


# --- real Git ---------------------------------------------------------------------------------


def _repo(root: Path, *, commit: bool = True) -> Path:
    root.mkdir()
    winproc.git(root, "init", "-q")
    if commit:
        winproc.git(
            root,
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic",
        )
    return root


def _real(service: bots.BotService, *, baseline: bool = False) -> CodeGit:
    code_git = CodeGit(
        service.workspaces,
        CodeFiles(service.workspaces),
        git_available=lambda: True,
        bot_code_store=service.store if baseline else None,
    )
    service.bind_code_git(code_git)
    return code_git


@needs_git
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("committed", None),
        ("empty", "code_folder_no_commits"),
        ("plain", "code_folder_not_repository"),
        ("untrusted", "code_folder_untrusted"),
    ],
)
def test_real_code_git_admits_only_a_trusted_committed_top_level_repository(
    service: bots.BotService, tmp_path: Path, kind: str, code: str | None
) -> None:
    bot = _bot(service)
    _real(service)
    root = tmp_path / "code"
    if kind == "plain":
        root.mkdir()
    else:
        _repo(root, commit=kind != "empty")
    if kind == "untrusted":
        folder = service.workspaces.register_existing(_id(), str(root), kind="code")[0]
        workspace_id = folder.workspace_id
    else:
        workspace_id = trusted(service.workspaces, root)
    if code is None:
        granted = service.set_code_folder(bot["bot_id"], 1, workspace_id)
        assert granted["code_folder"] == {"workspace_id": workspace_id, "name": "code"}
        return
    with pytest.raises(bots.BotError) as refused:
        service.set_code_folder(bot["bot_id"], 1, workspace_id)
    assert refused.value.code == code and _grants(service) == []


@needs_git
def test_baseline_aware_code_git_refuses_changed_settings(
    service: bots.BotService, tmp_path: Path
) -> None:
    bot = _bot(service)
    _real(service, baseline=True)
    workspace_id = trusted(service.workspaces, _repo(tmp_path / "code"))
    assert service.store.insert_bot_code_baseline(
        {"workspace_id": workspace_id, "git_fingerprint": "0" * 64, "created_at": _STAMP}
    )
    with pytest.raises(bots.BotError, match="git_settings_changed"):
        service.set_code_folder(bot["bot_id"], 1, workspace_id)
    assert _grants(service) == []


# --- route ------------------------------------------------------------------------------------


class ReleaseRig(Rig):
    """The Bot routes with the post-commit release callback, which reads committed state."""

    def __init__(self, service: bots.BotService) -> None:
        super().__init__(service)
        self.released: list[dict[str, str]] = []

    def copies_released(self) -> None:
        self.released.append(_states(self.service))

    def app(self) -> Starlette:
        routes = bots_api.bot_routes(
            self.service,
            authorized=lambda request: (
                request.headers.get("authorization") == BEARER["Authorization"]
            ),
            origins=frozenset({ORIGIN}),
            workers=self.workers,
            admission_lock=self.lock,
            prepare_chat=self.prepare_chat,
            chat_status=self.chat_status,
            busy=lambda session_id: session_id in self.busy_chats,
            scheduler_status=lambda: {"state": "running", "attended": True, "paused": False},
            delivery_status=self.delivery_status,
            set_delivery=self.set_delivery,
            revoke=self.revoke,
            wake=self.wake,
            copies_released=self.copies_released,
        )
        return Starlette(routes=routes)


@pytest.fixture
def rig(service: bots.BotService) -> ReleaseRig:
    return ReleaseRig(service)


def test_route_guards_body_and_cas_then_grants_and_clears(rig: ReleaseRig) -> None:
    folder = _vault(rig.service, kind="code")
    fake = _bind(rig.service, FakeGit())
    path = f"{BASE}/{rig.bot['bot_id']}/code-folder"
    body = {"expected_state_revision": 1, "workspace_id": folder}
    with TestClient(rig.app()) as client:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=BEARER, json=body).status_code == 403
        for bad in (
            {"expected_state_revision": 1},
            {**body, "extra": True},
            {**body, "workspace_id": 7},
            {**body, "workspace_id": "../code"},
            {**body, "expected_state_revision": "1"},
        ):
            refused = client.post(path, headers=WRITE, json=bad)
            assert refused.status_code == 400
        assert fake.calls == []
        stale = client.post(path, headers=WRITE, json={**body, "expected_state_revision": 2})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
        granted = client.post(path, headers=WRITE, json=body)
        roster = client.get(BASE, headers=BEARER)
        cleared = client.post(
            path, headers=WRITE, json={"expected_state_revision": 2, "workspace_id": None}
        )
    assert granted.status_code == 200 and granted.headers["cache-control"] == "no-store"
    assert granted.json()["bot"]["code_folder"] == {"workspace_id": folder, "name": "synthetic"}
    assert granted.json()["bot"]["delivery"]["reason"] == "off"
    assert roster.json()["bots"][0]["code_folder"]["workspace_id"] == folder
    assert cleared.status_code == 200 and cleared.json()["bot"]["code_folder"] is None
    assert cleared.json()["bot"]["state_revision"] == 3
    assert rig.released == [] and rig.revoked == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        ("unknown_workspace", 422, "code_folder_missing"),
        ("trust_required", 422, "code_folder_untrusted"),
        ("not_repository", 422, "code_folder_not_repository"),
        ("no_commits", 422, "code_folder_no_commits"),
        ("git_unavailable", 503, "code_folder_git_unavailable"),
        ("git_settings_changed", 409, "git_settings_changed"),
        (None, 503, "code_folder_git_unavailable"),
    ],
)
def test_route_maps_each_grant_refusal_to_a_fixed_status(
    rig: ReleaseRig, error: str | None, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    folder = _vault(rig.service, kind="code")
    if error is not None:
        _bind(rig.service, FakeGit(error))
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/code-folder",
            headers=WRITE,
            json={"expected_state_revision": 1, "workspace_id": folder},
        )
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert folder not in response.text + caplog.text
    assert str(rig.service.store.path.parent.parent) not in response.text + caplog.text


def test_route_maps_a_bot_without_an_ordinary_vault(rig: ReleaseRig) -> None:
    folder = _vault(rig.service, kind="code")
    _bind(rig.service, FakeGit())
    with rig.service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE workspaces SET policy='protected' WHERE workspace_id=?",
            (rig.bot["workspace_id"],),
        )
    with TestClient(rig.app()) as client:
        response = client.post(
            f"{BASE}/{rig.bot['bot_id']}/code-folder",
            headers=WRITE,
            json={"expected_state_revision": 1, "workspace_id": folder},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "code_folder_needs_ordinary"


async def test_route_waits_for_the_admission_lock_before_git(rig: ReleaseRig) -> None:
    folder = _vault(rig.service, kind="code")
    fake = _bind(rig.service, FakeGit())
    admitted = asyncio.Event()

    class Workers(set[asyncio.Task[Any]]):
        def add(self, worker: asyncio.Task[Any]) -> None:
            super().add(worker)
            admitted.set()

    rig.workers = Workers()
    transport = httpx.ASGITransport(app=rig.app())
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await rig.lock.acquire()
        try:
            call = asyncio.create_task(
                client.post(
                    f"{BASE}/{rig.bot['bot_id']}/code-folder",
                    headers=WRITE,
                    json={"expected_state_revision": 1, "workspace_id": folder},
                )
            )
            async with asyncio.timeout(HANG_GUARD_S):
                await admitted.wait()
            with anyio.move_on_after(0.02):
                await asyncio.shield(call)
            assert not call.done() and fake.calls == []
        finally:
            rig.lock.release()
        response = await call
    assert response.status_code == 200 and len(fake.calls) == 2


def test_release_callback_runs_after_each_committed_deletion_only(rig: ReleaseRig) -> None:
    service = rig.service
    folder = _vault(service, kind="code")
    main = _copy(service, rig.bot["bot_id"], folder, session_id=rig.bot["chat_session_id"])
    thread = _group(service, rig.bot["workspace_id"], [_id()])
    shared = _copy(service, rig.bot["bot_id"], folder, thread_id=thread)
    reset_bot = _bot(service)
    reset_copy = _copy(service, reset_bot["bot_id"], folder, session_id=_id())
    thread_path = f"/api/v2/bot-threads/{rig.bot['workspace_id']}/{thread}"
    with TestClient(rig.app()) as client:
        stale = client.request("DELETE", thread_path, headers=WRITE, json={"expected_revision": 2})
        assert stale.status_code == 409 and rig.released == []
        assert client.request(
            "DELETE", thread_path, headers=WRITE, json={"expected_revision": 1}
        ).json() == {"removed": True}
        assert rig.released == [{main: "bound", shared: "released", reset_copy: "bound"}]
        bot_path = f"{BASE}/{rig.bot['bot_id']}"
        stale = client.request(
            "DELETE", bot_path, headers=WRITE, json={"expected_state_revision": 2}
        )
        assert stale.status_code == 409 and len(rig.released) == 1
        assert (
            client.request(
                "DELETE", bot_path, headers=WRITE, json={"expected_state_revision": 1}
            ).status_code
            == 200
        )
        assert rig.released[-1] == {main: "released", shared: "released", reset_copy: "bound"}
        reset = client.post(
            BASE + "/reset", headers=WRITE, json={"workspace_id": reset_bot["workspace_id"]}
        )
    assert reset.status_code == 200 and len(rig.released) == 3
    assert rig.released[-1] == {main: "released", shared: "released", reset_copy: "released"}
