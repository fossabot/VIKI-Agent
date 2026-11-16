"""Check definitions, sealing and the shared root gate, on a real store and service."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, protected_at, service_at, store_at
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import protection, storage, winproc
from viki_agent.code import code_checks
from viki_agent.code.code_checks import (
    MAX_CHECKS,
    CheckService,
    CheckView,
    CodeRunError,
    seal,
    trusted_root,
    unseal,
    valid_command,
    valid_name,
    valid_timeout,
)
from viki_agent.session import now_iso
from viki_agent.storage import CheckRow, CheckRunRow, SessionStore
from viki_agent.workspaces import WorkspaceService

WORKTREE = "66666666-6666-4666-8666-666666666666"
OTHER_WORKTREE = "77777777-7777-4777-8777-777777777777"
UNKNOWN = "88888888-8888-4888-8888-888888888888"
CHECK = "99999999-9999-4999-8999-999999999999"


@dataclass
class Env:
    tmp_path: Path
    store: SessionStore
    service: WorkspaceService
    calls: list[str] = field(default_factory=list)  # filesystem and Git calls the spies saw

    def code(self, name: str = "repo", *, trust: bool = True) -> str:
        root = self.tmp_path / name
        root.mkdir()
        if trust:
            return trusted(self.service, root)
        workspace, _ = self.service.register_existing(COMMAND, str(root), kind="code")
        return workspace.workspace_id

    def worktree(
        self,
        workspace_id: str,
        worktree_id: str = WORKTREE,
        *,
        state: str = "ready",
        folder: bool = True,
    ) -> Path:
        # The gate makes no Git call, so a row and a directory are the whole worktree it sees.
        path = self.tmp_path / "worktrees" / worktree_id
        if folder:
            path.mkdir(parents=True)
        stamp = now_iso()
        self.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id,
                "path": str(path),
                "branch": "viki/test",
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=32,
        )
        if state != "creating":
            self.store.update_worktree_state(worktree_id, "creating", state, stamp)
        return path

    def spy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Record the gate's own filesystem probes and every process or Git call: not the
        store's checks of its database file while it reads a row, nor the calls a spied call
        makes inside itself."""
        depth = [0]

        def record(name: str, real: Any) -> Any:
            def spied(*args: Any, **kwargs: Any) -> Any:
                if depth[0] == 0 and not _in_store():
                    self.calls.append(name)
                depth[0] += 1
                try:
                    return real(*args, **kwargs)
                finally:
                    depth[0] -= 1

            return spied

        for method in ("is_dir", "exists", "stat", "lstat", "resolve", "iterdir", "open"):
            real = getattr(pathlib.Path, method)
            monkeypatch.setattr(pathlib.Path, method, record(f"Path.{method}", real))
        for name in ("git", "run", "spawn"):
            monkeypatch.setattr(winproc, name, record(f"winproc.{name}", getattr(winproc, name)))
        monkeypatch.setattr(
            self.service, "current_root", record("current_root", self.service.current_root)
        )

    def refusal(self, workspace_id: str, worktree_id: str | None = None) -> str:
        with pytest.raises(CodeRunError) as refused:
            trusted_root(self.store, self.service, workspace_id, worktree_id)
        return refused.value.code


def _in_store() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_filename == storage.__file__:
            return True
        frame = frame.f_back
    return False


@pytest.fixture
def env(tmp_path: Path) -> Env:
    store = store_at(tmp_path)
    return Env(tmp_path, store, service_at(tmp_path, store))


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service's refusal off Windows is its own test; elsewhere run as on Windows."""
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)


def run_row(check_id: str, workspace_id: str, worktree_id: str | None, run_id: str) -> CheckRunRow:
    return {
        "run_id": run_id,
        "check_id": check_id,
        "workspace_id": workspace_id,
        "worktree_id": worktree_id,
        "head_oid": None,
        "dirty": None,
        "outcome": "running",
        "exit_code": None,
        "duration_ms": None,
        "output_bytes": None,
        "tail": None,
        "started_at": now_iso(),
        "ended_at": None,
    }


# --- the root gate ----------------------------------------------------------------------------


def test_the_root_gate_returns_the_folder_or_its_ready_worktree(env: Env) -> None:
    workspace_id = env.code()
    path = env.worktree(workspace_id)
    display = env.service.get(workspace_id, kind="code").root.display
    assert trusted_root(env.store, env.service, workspace_id, None) == Path(display)
    assert trusted_root(env.store, env.service, workspace_id, WORKTREE) == path


def test_the_root_gate_refuses_an_untrusted_folder_before_any_lookup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code(trust=False)
    env.worktree(workspace_id)
    lookups: list[str] = []
    monkeypatch.setattr(env.store, "get_worktree", lambda wid: lookups.append(wid))
    env.spy(monkeypatch)
    assert env.refusal(workspace_id) == "trust_required"
    assert env.refusal(workspace_id, WORKTREE) == "trust_required"
    assert env.calls == [] and lookups == []


def test_the_root_gate_refuses_a_vault_or_an_unknown_id_before_any_lookup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = env.tmp_path / "notes"
    notes.mkdir()
    vault, _ = env.service.register_existing(COMMAND, str(notes))
    env.spy(monkeypatch)
    assert env.refusal(vault.workspace_id) == "unknown_workspace"
    assert env.refusal(UNKNOWN, WORKTREE) == "unknown_workspace"
    assert env.calls == []


def test_the_root_gate_refuses_a_protected_bound_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = env.tmp_path / "VIKI"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    protected_at(env.service, vault)
    copy = env.tmp_path / "copy"
    copy.mkdir()
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    code, _ = env.service.register_existing(COMMAND, str(copy), kind="code")
    assert code.policy == "protected"
    env.spy(monkeypatch)
    assert env.refusal(code.workspace_id) == "trust_required"
    assert env.calls == []


def test_the_root_gate_refuses_a_folder_that_does_not_persist(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code()
    real = env.service.get

    def scratch(wid: str, *, kind: Any = "vault") -> Any:
        return dataclasses.replace(real(wid, kind=kind), persistent=False)

    monkeypatch.setattr(env.service, "get", scratch)
    env.spy(monkeypatch)
    assert env.refusal(workspace_id) == "trust_required"
    assert env.calls == []


def test_the_root_gate_refuses_a_moved_folder_without_git(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code()
    env.worktree(workspace_id)
    (env.tmp_path / "repo").rename(env.tmp_path / "moved")
    env.spy(monkeypatch)
    assert env.refusal(workspace_id) == "root_unavailable"
    assert env.refusal(workspace_id, WORKTREE) == "root_unavailable"
    assert not [call for call in env.calls if call.startswith("winproc.")]


def test_the_root_gate_refuses_an_unknown_or_foreign_worktree_before_the_disk(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code()
    other = env.code("other")
    env.worktree(other, OTHER_WORKTREE)
    env.spy(monkeypatch)
    assert env.refusal(workspace_id, UNKNOWN) == "unknown_worktree"
    assert env.refusal(workspace_id, OTHER_WORKTREE) == "unknown_worktree"
    assert set(env.calls) == {"current_root"}


@pytest.mark.parametrize("state", ["creating", "removing", "kept", "removed"])
def test_the_root_gate_refuses_a_worktree_not_ready_before_the_disk(
    env: Env, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    workspace_id = env.code()
    env.worktree(workspace_id, state=state)
    env.spy(monkeypatch)
    assert env.refusal(workspace_id, WORKTREE) == "worktree_not_ready"
    assert env.calls == ["current_root"]


def test_the_root_gate_refuses_a_ready_worktree_whose_folder_is_gone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code()
    env.worktree(workspace_id, folder=False)
    env.spy(monkeypatch)
    assert env.refusal(workspace_id, WORKTREE) == "worktree_not_ready"
    assert env.calls == ["current_root", "Path.is_dir"]


# --- validators -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("a" * 100, True),
        ("a" * 101, False),
        ("テスト 1", True),
        ("", False),
        ("   ", False),
        ("a\tb", False),
        ("a\rb", False),
        ("a\nb", False),
        ("a\0b", False),
        ("a\x7fb", False),
        ("a\x85b", False),
        ("a\x9fb", False),
        ("a\ud800b", False),
        (7, False),
        (None, False),
    ],
)
def test_valid_name(value: object, ok: bool) -> None:
    assert valid_name(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("x" * 8000, True),
        ("x" * 8001, False),
        ('npm test -- --grep "a b" && echo %X%', True),
        ("a\tb", True),
        ("", False),
        (" \t ", False),
        ("a\rb", False),
        ("a\nb", False),
        ("a\0b", False),
        ("a\x1bb", False),
        ("a\ud800b", False),
        (b"npm test", False),
    ],
)
def test_valid_command(value: object, ok: bool) -> None:
    assert valid_command(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (59, False),
        (60, True),
        (600, True),
        (3600, True),
        (3601, False),
        (True, False),
        (60.0, False),
    ],
)
def test_valid_timeout(value: object, ok: bool) -> None:
    assert valid_timeout(value) is ok


# --- sealing ----------------------------------------------------------------------------------


def _row(check_id: str, body: bytes) -> CheckRow:
    return {
        "check_id": check_id,
        "workspace_id": UNKNOWN,
        "body": body,
        "timeout_s": 600,
        "created_at": now_iso(),
    }


def test_a_sealed_body_hides_its_json_and_opens_only_under_its_own_id() -> None:
    body = seal(CHECK, "naïve tests", "npm test")
    canonical = json.dumps(
        {"command": "npm test", "name": "naïve tests", "v": 1}, ensure_ascii=False
    ).encode("utf-8")
    assert body != canonical and b"npm test" not in body
    assert unseal(_row(CHECK, body)) == ("naïve tests", "npm test")
    with pytest.raises(CodeRunError) as other:
        unseal(_row(UNKNOWN, body))
    assert other.value.code == "check_unreadable"


@pytest.mark.parametrize(
    "document",
    [
        {"command": "npm test", "name": "t", "v": 2},
        {"command": "npm test", "name": "t", "v": True},
        {"command": "npm test", "name": "t"},
        {"command": "npm test", "name": "t", "v": 1, "extra": 1},
        {"command": "a\nb", "name": "t", "v": 1},
        {"command": "npm test", "name": "", "v": 1},
        ["npm test", "t", 1],
    ],
)
def test_a_body_that_opens_to_anything_but_a_valid_document_is_unreadable(
    document: object,
) -> None:
    body = protection.protect(
        json.dumps(document).encode("utf-8"), context=b"VIKI check v1\0" + CHECK.encode("ascii")
    )
    with pytest.raises(CodeRunError) as refused:
        unseal(_row(CHECK, body))
    assert refused.value.code == "check_unreadable"


@pytest.mark.parametrize("size", [0, code_checks.MAX_BODY_BYTES + 1])
def test_a_sealed_body_outside_its_bounds_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    monkeypatch.setattr(protection, "protect", lambda data, *, context: b"x" * size)
    with pytest.raises(CodeRunError) as refused:
        seal(CHECK, "t", "npm test")
    assert refused.value.code == "unavailable"


def test_a_failed_seal_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(data: bytes, *, context: bytes) -> bytes:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "protect", fail)
    with pytest.raises(CodeRunError) as refused:
        seal(CHECK, "t", "npm test")
    assert refused.value.code == "unavailable"


# --- the service ------------------------------------------------------------------------------


@pytest.mark.usefixtures("windows")
def test_added_checks_list_oldest_first_with_the_last_run_of_each_checkout(env: Env) -> None:
    workspace_id = env.code()
    env.worktree(workspace_id)
    service = CheckService(env.store, env.service)
    first = service.add_check(workspace_id, "test", "npm test", 600)
    second = service.add_check(workspace_id, "lint", "npm run lint", 60)
    assert first.readable and (first.name, first.command, first.timeout_s) == (
        "test",
        "npm test",
        600,
    )
    here = run_row(first.check_id, workspace_id, None, "a" * 8 + "-0000-4000-8000-000000000001")
    there = run_row(
        first.check_id, workspace_id, WORKTREE, "a" * 8 + "-0000-4000-8000-000000000002"
    )
    env.store.insert_check_run(here, keep=20)
    env.store.insert_check_run(there, keep=20)
    assert service.list(workspace_id, None) == [(first, here), (second, None)]
    assert service.list(workspace_id, WORKTREE) == [(first, there), (second, None)]


@pytest.mark.usefixtures("windows")
def test_a_corrupted_body_lists_as_unreadable(env: Env) -> None:
    workspace_id = env.code()
    body = bytearray(seal(CHECK, "t", "npm test"))
    body[-1] ^= 1
    stamp = now_iso()
    env.store.insert_check(
        {
            "check_id": CHECK,
            "workspace_id": workspace_id,
            "body": bytes(body),
            "timeout_s": 600,
            "created_at": stamp,
        },
        limit=MAX_CHECKS,
    )
    listed = CheckService(env.store, env.service).list(workspace_id, None)
    assert listed == [(CheckView(CHECK, None, None, 600, stamp, False), None)]


@pytest.mark.usefixtures("windows")
def test_the_thirty_third_check_is_refused(env: Env) -> None:
    workspace_id = env.code()
    service = CheckService(env.store, env.service)
    for index in range(MAX_CHECKS):
        service.add_check(workspace_id, f"check {index}", "echo ok", 60)
    with pytest.raises(CodeRunError) as refused:
        service.add_check(workspace_id, "one more", "echo ok", 60)
    assert refused.value.code == "check_limit"
    assert len(env.store.list_checks(workspace_id)) == MAX_CHECKS


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize(
    ("name", "command", "timeout_s", "code"),
    [
        ("", "npm test", 600, "invalid_name"),
        ("t", "a\nb", 600, "invalid_command"),
        ("t", "npm test", 59, "invalid_timeout"),
    ],
)
def test_add_refuses_invalid_input_before_the_gate(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, command: str, timeout_s: int, code: str
) -> None:
    monkeypatch.setattr(env.store, "get_workspace", _forbidden)
    with pytest.raises(CodeRunError) as refused:
        CheckService(env.store, env.service).add_check(UNKNOWN, name, command, timeout_s)
    assert refused.value.code == code


@pytest.mark.usefixtures("windows")
def test_add_refuses_an_untrusted_folder_and_stores_nothing(env: Env) -> None:
    workspace_id = env.code(trust=False)
    with pytest.raises(CodeRunError) as refused:
        CheckService(env.store, env.service).add_check(workspace_id, "t", "npm test", 600)
    assert refused.value.code == "trust_required"
    assert env.store.list_checks(workspace_id) == []


def _forbidden(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("looked up")


def test_off_windows_the_service_refuses_before_any_lookup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = env.code()
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", False)
    for name in ("get_workspace", "get_worktree", "list_checks", "last_check_runs", "insert_check"):
        monkeypatch.setattr(env.store, name, _forbidden)
    service = CheckService(env.store, env.service)
    for call in (
        lambda: service.list(workspace_id, None),
        lambda: service.add_check(workspace_id, "t", "npm test", 600),
        lambda: service.add_check(workspace_id, "", "", 0),
    ):
        with pytest.raises(CodeRunError) as refused:
            call()
        assert refused.value.code == "unsupported_platform"
