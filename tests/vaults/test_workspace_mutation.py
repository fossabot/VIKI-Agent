from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests.app.test_workspaces import COMMAND, protected_at, service_at
from tests.workspace_code.test_workspaces_code import folder, trusted
from viki_agent.domain import CanonicalRoot, RootIdentity, Workspace
from viki_agent.vault_storage import VaultBootstrap
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver, admits_note_writes
from viki_agent.workspaces import RowKind, WorkspaceError, WorkspaceService, root_key


def _identity(
    *,
    oid: str | None = None,
    common: str | None = None,
    instruction: str | None = None,
    inherits_from: str | None = None,
) -> RootIdentity:
    return RootIdentity(
        (oid,) if oid else (),
        common,
        {"CLAUDE.md": instruction} if instruction else {},
        bool(oid or common),
        inherits_from,
        "2026-09-21T00:00:00.000Z",
    )


def _workspace(
    root: Path,
    *,
    policy: str = "ordinary",
    identity: RootIdentity | None = None,
) -> Workspace:
    resolved = root.resolve()
    info = resolved.stat()
    return Workspace(
        str(uuid.uuid4()),
        "vault",
        CanonicalRoot(
            root_key(resolved),
            str(resolved),
            f"{info.st_dev}:{info.st_ino}",
            False,
        ),
        "scratch",
        policy,  # type: ignore[arg-type]
        4,
        7,
        "created",
        "unknown",
        None,
        identity or _identity(),
        None,
        "complete",
        True,
        "2026-09-21T00:00:00.000Z",
        "2026-09-21T00:00:00.000Z",
    )


class _Workspaces:
    def __init__(self, workspace: Workspace, *others: Workspace) -> None:
        self.workspace = workspace
        self.others = list(others)
        self.gets = 0

    def get(self, workspace_id: str, *, kind: RowKind | None = "vault") -> Workspace:
        self.gets += 1
        if workspace_id != self.workspace.workspace_id:
            raise AssertionError("unexpected workspace")
        if kind is not None and self.workspace.kind != kind:
            raise WorkspaceError("unknown_workspace")  # as `WorkspaceService.get` does
        return self.workspace

    def current_root(self, workspace: Workspace) -> CanonicalRoot | None:
        return workspace.root

    def list(self, *, kind: str | None = "vault") -> list[Workspace]:
        rows = [self.workspace, *self.others]
        return [row for row in rows if kind is None or row.kind == kind]


class _UnavailableRoot(_Workspaces):
    def current_root(self, workspace: Workspace) -> CanonicalRoot | None:
        return None


def _resolver(workspaces: _Workspaces) -> WorkspaceMutationResolver:
    return WorkspaceMutationResolver(workspaces, git_available=lambda: False)


def test_exact_ordinary_complete_workspace_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    workspaces = _Workspaces(workspace)
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: _identity(),
    )

    admitted = _resolver(workspaces)(
        workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
    )
    assert admitted.workspace_id == workspace.workspace_id
    assert admitted.path == tmp_path.resolve()
    assert workspaces.gets == 2


@pytest.mark.parametrize(
    "change",
    [
        {"policy": "protected"},
        {"persistent": False},
        {"bootstrap": "pending"},
        {"bootstrap": "none"},
    ],
)
def test_nonordinary_or_incomplete_workspace_is_refused(
    tmp_path: Path, change: dict[str, object]
) -> None:
    workspace = replace(_workspace(tmp_path), **change)  # type: ignore[arg-type]
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        _resolver(_Workspaces(workspace))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


@pytest.mark.parametrize("kind", ["vault", "scratch"])
@pytest.mark.parametrize("policy", ["ordinary", "existing_readonly", "protected"])
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("bootstrap", ["complete", "pending", "none"])
def test_note_writes_are_admitted_only_for_an_ordinary_complete_persistent_vault(
    tmp_path: Path, kind: str, policy: str, persistent: bool, bootstrap: str
) -> None:
    workspace = replace(
        _workspace(tmp_path),
        kind=kind,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        persistent=persistent,
        bootstrap=bootstrap,  # type: ignore[arg-type]
    )
    expected = (kind, policy, persistent, bootstrap) == ("vault", "ordinary", True, "complete")
    assert admits_note_writes(workspace) is expected
    # The resolver's gate refuses exactly the workspaces the predicate does not admit.
    refused = False
    try:
        WorkspaceMutationResolver._require_expected(  # pyright: ignore[reportPrivateUsage]
            workspace, workspace.policy_revision, workspace.root.physical_id
        )
    except MutationError as exc:
        assert exc.code == "ordinary_workspace_required"
        refused = True
    assert refused is not expected


@pytest.mark.parametrize("wrong", ["revision", "physical"])
def test_expected_snapshot_must_match(tmp_path: Path, wrong: str) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(MutationError, match="workspace_changed"):
        _resolver(_Workspaces(workspace))(
            workspace.workspace_id,
            workspace.policy_revision + (wrong == "revision"),
            "wrong" if wrong == "physical" else workspace.root.physical_id,
        )


def test_current_root_deny_or_identity_failure_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(MutationError, match="workspace_changed"):
        _resolver(_UnavailableRoot(workspace))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


def test_protected_overlap_or_alias_is_refused(tmp_path: Path) -> None:
    ordinary_root = tmp_path / "ordinary"
    ordinary_root.mkdir()
    workspace = _workspace(ordinary_root)
    protected = replace(
        _workspace(tmp_path, policy="protected"),
        root=replace(_workspace(tmp_path, policy="protected").root, physical_id="other"),
    )
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        _resolver(_Workspaces(workspace, protected))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )

    alias = replace(
        protected,
        root=replace(
            protected.root,
            key="x:\\elsewhere",
            physical_id=workspace.root.physical_id,
        ),
    )
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        _resolver(_Workspaces(workspace, alias))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


@pytest.mark.parametrize("evidence", ["stored", "live"])
def test_protected_identity_evidence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    ordinary_root = tmp_path / "ordinary"
    protected_root = tmp_path / "protected"
    ordinary_root.mkdir()
    protected_root.mkdir()
    shared = "a" * 64
    stored = _identity(instruction=shared) if evidence == "stored" else _identity()
    live = _identity(oid=shared) if evidence == "live" else _identity()
    protected_identity = (
        _identity(instruction=shared) if evidence == "stored" else _identity(oid=shared)
    )
    workspace = _workspace(ordinary_root, identity=stored)
    protected = _workspace(protected_root, policy="protected", identity=protected_identity)
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: live,
    )
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        _resolver(_Workspaces(workspace, protected))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


def test_a_protected_code_folder_sharing_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary_root = tmp_path / "ordinary"
    code_root = tmp_path / "code"
    ordinary_root.mkdir()
    code_root.mkdir()
    shared = _identity(instruction="a" * 64)
    workspace = _workspace(ordinary_root, identity=shared)
    protected_code = replace(
        _workspace(code_root, policy="protected", identity=shared), kind="code"
    )
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: _identity(),
    )
    with pytest.raises(MutationError, match="ordinary_workspace_required"):
        _resolver(_Workspaces(workspace, protected_code))(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


def test_the_root_carries_every_other_protected_workspaces_instruction_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instruction save compares its new bytes with these, name by name, so it never gives
    the vault an identity the resolver then refuses. An ordinary workspace's files are no such
    evidence, and the pairs are not part of the root's own identity."""
    for name in ("ordinary", "vault", "code", "other"):
        (tmp_path / name).mkdir()

    def hashes(**named: str) -> RootIdentity:
        return replace(_identity(), instruction_hashes={f"{n}.md": d for n, d in named.items()})

    workspace = _workspace(tmp_path / "ordinary")
    protected_vault = _workspace(
        tmp_path / "vault", policy="protected", identity=hashes(AGENTS="a" * 64, CLAUDE="b" * 64)
    )
    protected_code = replace(
        _workspace(tmp_path / "code", policy="protected", identity=hashes(CLAUDE="c" * 64)),
        kind="code",
    )
    ordinary_other = _workspace(tmp_path / "other", identity=hashes(AGENTS="d" * 64))
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: _identity(),
    )

    root = _resolver(_Workspaces(workspace, protected_vault, protected_code, ordinary_other))(
        workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
    )

    assert root.protected_instructions == {
        ("AGENTS.md", "a" * 64),
        ("CLAUDE.md", "b" * 64),
        ("CLAUDE.md", "c" * 64),
    }
    assert root == replace(root, protected_instructions=frozenset())


def test_policy_change_during_identity_probe_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    workspaces = _Workspaces(workspace)

    def mutate(*_args: object, **_kwargs: object) -> RootIdentity:
        workspaces.workspace = replace(workspace, policy_revision=5)
        return _identity()

    monkeypatch.setattr("viki_agent.vaults.workspace_mutation.probe_identity", mutate)
    with pytest.raises(MutationError, match="workspace_changed"):
        _resolver(workspaces)(
            workspace.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )


# --- code folders -----------------------------------------------------------------------------


def _resolver_over(service: WorkspaceService) -> WorkspaceMutationResolver:
    return WorkspaceMutationResolver(service, git_available=lambda: False)


@pytest.mark.parametrize("kind", [None, "code"])
def test_a_trusted_code_folder_resolves_to_a_code_root(
    tmp_path: Path, kind: RowKind | None
) -> None:
    service = service_at(tmp_path)
    root = folder(tmp_path, "repo")
    code = service.get(trusted(service, root), kind="code")
    resolve = _resolver_over(service)
    admitted = resolve(code.workspace_id, code.policy_revision, code.root.physical_id, kind=kind)
    assert (admitted.kind, admitted.workspace_id, admitted.path, admitted.physical_id) == (
        "code",
        code.workspace_id,
        root.resolve(),
        code.root.physical_id,
    )
    # The default reads vault rows only, so every vault writer keeps refusing a code folder.
    with pytest.raises(MutationError, match="workspace_changed"):
        resolve(code.workspace_id, code.policy_revision, code.root.physical_id)


@pytest.mark.parametrize("bound", ["untrusted", "protected"])
def test_an_untrusted_or_protected_code_folder_is_refused(tmp_path: Path, bound: str) -> None:
    service = service_at(tmp_path)
    root = folder(tmp_path, "repo")
    if bound == "protected":
        vault = folder(tmp_path, "VIKI")
        (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
        protected_at(service, vault)
        (root / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    code, _ = service.register_existing(COMMAND, str(root), kind="code")
    assert code.policy == ("protected" if bound == "protected" else "existing_readonly")
    for kind in (None, "code"):
        with pytest.raises(MutationError, match="ordinary_workspace_required"):
            _resolver_over(service)(
                code.workspace_id, code.policy_revision, code.root.physical_id, kind=kind
            )


def test_a_code_folder_sharing_identity_with_a_protected_vault_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_root, vault_root = tmp_path / "code", tmp_path / "protected"
    code_root.mkdir()
    vault_root.mkdir()
    shared = _identity(instruction="a" * 64)
    code = replace(_workspace(code_root, identity=shared), kind="code", bootstrap="none")
    protected = _workspace(vault_root, policy="protected", identity=shared)
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: _identity(),
    )
    snapshot = (code.workspace_id, code.policy_revision, code.root.physical_id)
    assert _resolver(_Workspaces(code))(*snapshot, kind="code").kind == "code"
    for kind in (None, "code"):
        with pytest.raises(MutationError, match="ordinary_workspace_required"):
            _resolver(_Workspaces(code, protected))(*snapshot, kind=kind)


def test_a_vault_resolves_alike_under_either_reading(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    resources = folder(tmp_path, "resources")
    (resources / "welcome.en.md").write_text("# Welcome\n", encoding="utf-8")
    bootstrap = VaultBootstrap(service, resources=resources, home=tmp_path / "home")
    vault, created, _ = bootstrap.create(
        COMMAND, "Notes", str(folder(tmp_path, "vaults")), None, "en", False
    )
    assert created and vault.bootstrap == "complete"
    resolve = _resolver_over(service)
    snapshot = (vault.workspace_id, vault.policy_revision, vault.root.physical_id)
    default = resolve(*snapshot)
    assert default.kind == "vault"
    assert resolve(*snapshot, kind=None) == default


def test_a_stale_code_revision_is_refused(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    code = service.get(trusted(service, folder(tmp_path, "repo")), kind="code")
    with pytest.raises(MutationError, match="workspace_changed"):
        _resolver_over(service)(
            code.workspace_id, code.policy_revision - 1, code.root.physical_id, kind=None
        )


class _UnfilteredWorkspaces(_Workspaces):
    """A reader that ignores `kind`: the resolver must not rely on the reader's filter."""

    def get(self, workspace_id: str, *, kind: RowKind | None = "vault") -> Workspace:
        return super().get(workspace_id, kind=None)


def test_a_code_row_from_a_reader_that_ignores_kind_is_refused_to_vault_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = replace(_workspace(tmp_path), kind="code", bootstrap="none")
    monkeypatch.setattr(
        "viki_agent.vaults.workspace_mutation.probe_identity",
        lambda *_args, **_kwargs: _identity(),
    )
    resolve = _resolver(_UnfilteredWorkspaces(code))
    snapshot = (code.workspace_id, code.policy_revision, code.root.physical_id)
    with pytest.raises(MutationError, match="workspace_changed"):
        resolve(*snapshot)
    with pytest.raises(MutationError, match="workspace_changed"):
        resolve(*snapshot, kind="vault")
    for kind in (None, "code"):
        assert resolve(*snapshot, kind=kind).kind == "code"
