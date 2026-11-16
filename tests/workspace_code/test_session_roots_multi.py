"""Selected code folders are pinned, revalidated, and rendered as path data."""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.test_catalog_roots import _turn, _write
from tests.workspace_code.test_session_roots import FORK, REVOKE, SESSION, WORKTREE, Env, FakeActor
from viki_agent.catalog import Catalog, project_dir
from viki_agent.code import session_roots
from viki_agent.code.session_roots import (
    MSG_ROOT_MISSING,
    MSG_ROOT_UNTRUSTED,
    RootError,
    SessionRoot,
)
from viki_agent.domain import Workspace, WorkspaceKind
from viki_agent.drivers.base import StartError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    # A short folder: a transcript path repeats a code folder's path, which must stay under
    # Windows MAX_PATH even beneath pytest's default basetemp.
    short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
    store = store_at(short)
    return Env(short, store, service_at(short, store))


@pytest.mark.parametrize("change", ["revoked", "replaced"])
async def test_live_context_refuses_revoked_or_replaced_extra(env: Env, change: str) -> None:
    primary, extra = env.code(), env.code("extra")
    roots = env.roots
    await roots.prepare(SESSION, primary, None, additional_workspace_ids=(extra,))
    env.actors[SESSION] = FakeActor(live=True)
    assert await roots.context(SESSION) is not None
    if change == "revoked":
        env.service.set_policy(REVOKE, extra, 2, "existing_readonly", [])
        message = MSG_ROOT_UNTRUSTED
    else:
        path = env.tmp_path / "extra"
        path.rename(env.tmp_path / "old-extra")
        path.mkdir()
        message = MSG_ROOT_MISSING
    for method in (roots.context, roots.resolve):
        with pytest.raises(StartError) as refusal:
            await method(SESSION)
        assert refusal.value.code == "bad_request" and refusal.value.message == message
        assert (
            extra not in refusal.value.message and env.display(extra) not in refusal.value.message
        )


@pytest.mark.parametrize(
    "bad",
    ["duplicate", "primary", "many", "noncanonical", "path", "list", "null", "integer", "worktree"],
)
async def test_invalid_selection_records_no_primary_root(env: Env, bad: str) -> None:
    primary, extra = env.code(), env.code("extra")
    values: dict[str, Any] = {
        "duplicate": (extra, extra),
        "primary": (primary,),
        "many": tuple(str(uuid.uuid4()) for _ in range(8)),
        "noncanonical": (extra.replace("-", ""),),
        "path": (env.display(extra),),
        "list": [extra],
        "null": None,
        "integer": (1,),
        "worktree": (extra,),
    }
    with pytest.raises(RootError) as refusal:
        await env.roots.prepare(
            SESSION, primary, WORKTREE if bad == "worktree" else None, values[bad]
        )
    assert refusal.value.code == "invalid_body"
    assert env.store.get_session_root(SESSION) is None


@pytest.mark.parametrize("bad", ["unknown", "vault", "untrusted", "missing", "transient"])
async def test_all_extra_folders_are_admitted_before_recording(
    env: Env, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    primary, first = env.code(), env.code("first")
    if bad == "vault":
        path = env.tmp_path / "notes"
        path.mkdir()
        workspace, _ = env.service.register_existing(COMMAND, str(path))
        last = workspace.workspace_id
    elif bad == "unknown":
        last = str(uuid.uuid4())
    else:
        last = env.code("last", trust=bad != "untrusted")
        if bad == "missing":
            (env.tmp_path / "last").rename(env.tmp_path / "moved")
        elif bad == "transient":
            get = env.service.get
            transient = replace(get(last, kind="code"), persistent=False)

            def get_workspace(wid: str, *, kind: WorkspaceKind | None = "vault") -> Workspace:
                return transient if wid == last else get(wid, kind=kind)

            monkeypatch.setattr(env.service, "get", get_workspace)
    with pytest.raises(RootError) as refusal:
        await env.roots.prepare(SESSION, primary, None, (first, last))
    expected = {
        "unknown": "unknown_workspace",
        "vault": "unknown_workspace",
        "untrusted": "trust_required",
        "missing": "root_unavailable",
        "transient": "trust_required",
    }
    assert refusal.value.code == expected[bad]
    assert env.store.get_session_root(SESSION) is None


async def test_pin_fork_reopen_and_catalog_keep_the_primary_cwd(env: Env) -> None:
    primary, extra, other = env.code(), env.code("extra"), env.code("other")
    roots = env.roots
    selected = (extra, other)
    root = SessionRoot(SESSION, primary, None, selected)
    assert await roots.prepare(SESSION, primary, None, selected) == (root, True)
    assert await roots.prepare(SESSION, primary, None, selected) == (root, False)
    with pytest.raises(RootError) as refusal:
        await roots.prepare(SESSION, primary, None, (other, extra))
    assert refusal.value.code == "session_exists"
    roots.copy_for_fork(SESSION, FORK)
    store = store_at(env.tmp_path)
    reopened = Env(env.tmp_path, store, service_at(env.tmp_path, store)).roots
    source, fork = store.get_session_root(SESSION), store.get_session_root(FORK)
    assert source is not None and fork is not None
    assert fork == {**source, "session_id": FORK, "created_at": fork["created_at"]}
    assert source["additional_roots"] == [
        {
            "workspace_id": wid,
            "display_root": env.display(wid),
            "physical_id": env.service.get(wid, kind="code").root.physical_id,
        }
        for wid in selected
    ]
    rows, more = reopened.list()
    assert not more and set(rows) == {root, SessionRoot(FORK, primary, None, selected)}
    cwd = Path(env.display(primary))
    assert await reopened.resolve(SESSION) == cwd
    assert reopened.root_of(SESSION) == cwd
    assert reopened.transcript_roots() == {SESSION: cwd, FORK: cwd}
    home = env.tmp_path / "catalog-home"
    _write(home, cwd, SESSION, _turn(1, "primary prompt", "reply", "main.py"))
    _write(home, Path(env.display(extra)), SESSION, _turn(2, "other prompt", "reply", "other.py"))
    catalog = Catalog(home, env.tmp_path / "active")
    catalog.bind_roots(reopened)
    assert catalog.root_for(SESSION) == cwd
    assert catalog.transcript_path(SESSION) == project_dir(home, cwd) / f"{SESSION}.jsonl"


@pytest.mark.parametrize(
    "change", ["missing", "unknown", "moved_registration", "identity", "primary"]
)
async def test_live_context_rechecks_pinned_and_registered_roots(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    primary, extra = env.code(), env.code("extra")
    roots = env.roots
    await roots.prepare(SESSION, primary, None, (extra,))
    env.actors[SESSION] = FakeActor(live=True)
    if change == "missing":
        (env.tmp_path / "extra").rename(env.tmp_path / "moved")
    elif change in {"moved_registration", "identity"}:
        workspace = env.service.get(extra, kind="code")
        changes = (
            {"display": str(env.tmp_path / "moved")}
            if change == "moved_registration"
            else {"physical_id": "1:changed"}
        )
        changed = replace(workspace, root=replace(workspace.root, **changes))
        get = env.service.get

        def get_workspace(wid: str, *, kind: WorkspaceKind | None = "vault") -> Workspace:
            return changed if wid == extra else get(wid, kind=kind)

        monkeypatch.setattr(env.service, "get", get_workspace)
        # The refreshed registration itself is current; the pinned snapshot must still match it.
        monkeypatch.setattr(env.service, "current_root", lambda workspace: workspace.root)
    elif change == "unknown":
        env.store.update_workspace(extra, lambda _row, _rows: None)
    else:
        env.service.set_policy(REVOKE, primary, 2, "existing_readonly", [])
    expected = MSG_ROOT_UNTRUSTED if change in {"unknown", "primary"} else MSG_ROOT_MISSING
    for method in (roots.context, roots.resolve):
        with pytest.raises(StartError) as refusal:
            await method(SESSION)
        assert refusal.value.message == expected


async def test_context_leaves_legacy_live_sessions_and_unrecorded_starts_unchanged(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = env.code()
    roots = env.roots
    env.service.select(COMMAND, primary)
    assert await roots.context(SESSION) is None
    assert env.store.get_session_root(SESSION) is None
    await roots.prepare(SESSION, primary, None)
    env.actors[SESSION] = FakeActor(live=True)
    env.service.set_policy(REVOKE, primary, 2, "existing_readonly", [])
    monkeypatch.setattr(env.service, "current_root", lambda _: pytest.fail("legacy revalidation"))
    assert await roots.context(SESSION) is None


async def test_context_checks_at_most_eight_folders_off_loop_without_content_reads(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tuple(env.code(f"repo-{n}") for n in range(8))
    roots = env.roots
    await roots.prepare(SESSION, selected[0], None, selected[1:])
    current = env.service.current_root
    checked: list[str] = []

    def current_root(workspace: Any) -> Any:
        assert threading.current_thread() is not threading.main_thread()
        checked.append(workspace.workspace_id)
        return current(workspace)

    monkeypatch.setattr(env.service, "current_root", current_root)
    real_read_text = Path.read_text
    mountinfo = Path("/proc/self/mountinfo")  # osfs.filesystem_type's own read, on Linux

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == mountinfo:
            return real_read_text(path, *args, **kwargs)
        return pytest.fail("content read")

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", lambda *_a, **_kw: pytest.fail("content read"))
    assert await roots.context(SESSION) is not None
    assert checked == list(selected)


async def test_context_renders_exact_paths_as_json_and_measures_utf8_bytes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary, extra = env.code(), env.code("extra")
    paths = {primary: 'C:/Synthetic/\u00e9"\nprimary', extra: 'C:/Synthetic/extra\t"\\folder'}
    get = env.service.get

    def get_workspace(wid: str, *, kind: WorkspaceKind | None = "vault") -> Workspace:
        workspace = get(wid, kind=kind)
        return replace(
            workspace,
            root=replace(workspace.root, display=paths[wid]),
            name="Authored workspace name",
        )

    monkeypatch.setattr(env.service, "get", get_workspace)
    monkeypatch.setattr(env.service, "current_root", lambda workspace: workspace.root)
    roots = env.roots
    await roots.prepare(SESSION, primary, None, (extra,))
    context = await roots.context(SESSION)
    assert context is not None
    heading, document, permissions = context.splitlines()
    assert heading == "Selected code folders for this conversation:"
    assert json.loads(document) == {
        "primary_cwd": paths[primary],
        "additional_folders": [paths[extra]],
    }
    assert permissions == "Native permissions govern access to these folders."
    assert "Authored workspace name" not in context
    size = len(context.encode("utf-8"))
    assert size <= session_roots.MAX_ROOT_CONTEXT_BYTES == 65536
    monkeypatch.setattr(session_roots, "MAX_ROOT_CONTEXT_BYTES", size)
    assert await roots.context(SESSION) == context
    monkeypatch.setattr(session_roots, "MAX_ROOT_CONTEXT_BYTES", size - 1)
    with pytest.raises(StartError) as refusal:
        await roots.context(SESSION)
    assert refusal.value.code == "bad_request" and refusal.value.message == MSG_ROOT_MISSING
