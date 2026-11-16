"""The memory service: local authority, journalled saves and protected proposals.

Every row runs against a synthetic on-disk vault, a real store, the real sealing and the real
journalled save, so a write shows up as changed bytes and a refusal as an identical tree.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent.conversations.turn_parts import parse_excerpt_part
from viki_agent.domain import CanonicalRoot
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore
from viki_agent.vaults import memory as memory_module
from viki_agent.vaults.memory import (
    Corrected,
    InvalidMemory,
    MemoryList,
    MemoryService,
    MemoryServiceError,
    Proposed,
    Reviewed,
    Written,
)
from viki_agent.vaults.memory_record import Fields, Record, compose, parse
from viki_agent.vaults.proposals import ProposalService
from viki_agent.vaults.vault import VaultError, VaultService
from viki_agent.vaults.vault_excerpts import resolve_excerpts
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    VaultPublicationService,
)
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

# A fixed wall clock, so `reviewed` is an exact string rather than a shape.
NOON = 1_700_000_000_123_456_789
STAMP = "2023-11-14T22:13:20.123Z"
CREATED = "2023-11-01T09:00:00.000Z"
CLAIM = "Prefers tea over coffee in the morning"
STAGE_OR_BACKUP = (".viki-stage-", ".viki-backup-")
LEASE_WAIT_S = 0.02
# Only for the one wait that must end by the lease deadline (AGENTS.md's HANG_GUARD_S exception).
HANG_GUARD_S = 5


class _Workspaces:
    """What `VaultService`, the proposal service and the memory service reach, plus the admission
    `WorkspaceMutationResolver` makes, reduced to the policy and the snapshot."""

    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}
        self.active: str | None = None

    def add(self, root: Path, policy: str) -> str:
        resolved = root.resolve()
        info = os.stat(resolved)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            policy=policy,
            policy_revision=3,
            root=CanonicalRoot(
                key=root_key(resolved),
                display=str(resolved),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        self.active = self.active or workspace_id
        return workspace_id

    def get(self, workspace_id: str) -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root

    def list(self) -> list[Any]:
        return list(self.spaces.values())

    def resolve_active(self) -> Any:
        return SimpleNamespace(workspace=self.spaces[cast(str, self.active)])

    def resolve_root(
        self, workspace_id: str, policy_revision: int, physical_id: str
    ) -> OrdinaryMutationRoot:
        workspace = self.get(workspace_id)
        if workspace.policy != "ordinary":
            raise MutationError("ordinary_workspace_required")
        if (policy_revision, physical_id) != (
            workspace.policy_revision,
            workspace.root.physical_id,
        ):
            raise MutationError("workspace_changed")
        return OrdinaryMutationRoot(
            workspace_id,
            Path(workspace.root.display),
            workspace.root.key,
            physical_id,
            policy_revision,
            None,
        )


# (relative path, size, sha256, mtime_ns); a directory is (path, -1, "", mtime_ns).
Entry = tuple[str, int, str, int]


def tree(root: Path) -> list[Entry]:
    """Every entry under `root` with its bytes and modification time, so even a rewrite shows."""
    entries: list[Entry] = []
    for folder, directories, files in os.walk(root):
        for name in directories:
            path = Path(folder) / name
            entries.append((os.path.relpath(path, root), -1, "", path.stat().st_mtime_ns))
        for name in files:
            path = Path(folder) / name
            data = path.read_bytes()
            entries.append(
                (
                    os.path.relpath(path, root),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    path.stat().st_mtime_ns,
                )
            )
    return sorted(entries)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Saves:
    """The real publication service, recording each save's ref and `create_folders` in order;
    `fail_at` makes the n-th save (1-based) refuse before it starts, the state a crash between
    two saves leaves, and `before` runs just before each save reaches the service."""

    def __init__(self, inner: VaultPublicationService) -> None:
        self.inner = inner
        self.refs: list[str] = []
        self.creates: list[bool] = []
        self.fail_at: int | None = None
        self.before: Callable[[], None] | None = None

    async def save(self, *args: Any, **kwargs: Any) -> PublicationOutcome:
        self.refs.append(args[3])
        self.creates.append(kwargs.get("create_folders", False))
        if len(self.refs) == self.fail_at:
            raise PublicationError("target_busy")
        if self.before is not None:
            self.before()
        return await self.inner.save(*args, **kwargs)


@dataclass
class Harness:
    service: MemoryService
    proposals: ProposalService
    vault: VaultService
    saves: _Saves
    store: SessionStore
    spaces: _Workspaces
    root: Path
    wid: str
    coordinator: MutationCoordinator

    def path(self, memory_id: str) -> Path:
        return self.root / "memory" / f"{memory_id}.md"

    def write(self, ref: str, data: bytes) -> str:
        """Write a file the way another tool, an import or a restore would."""
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return sha(data)

    def record(
        self,
        *,
        status: str = "approved",
        kind: str = "owner",
        scope: str = "vault",
        claim: str = CLAIM,
        bom: bool = False,
    ) -> tuple[str, str]:
        memory_id = str(uuid.uuid4())
        text = compose(Fields(memory_id, kind, scope, status, CREATED), claim)
        data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
        return memory_id, self.write(f"memory/{memory_id}.md", data)

    def parsed(self, memory_id: str) -> Record:
        record = parse(self.path(memory_id).read_bytes().decode("utf-8"))
        assert isinstance(record, Record)
        return record

    def states(self) -> dict[str, str]:
        return {item.fields.id: item.state for item in self.service.list(self.wid).items}

    def admission(self) -> tuple[int, str]:
        workspace = self.spaces.get(self.wid)
        return workspace.policy_revision, workspace.root.physical_id


def _harness(tmp_path: Path, policy: str, *, memory: bool = True) -> Harness:
    root = tmp_path / "vault"
    (root / "memory" if memory else root).mkdir(parents=True)
    spaces = _Workspaces()
    wid = spaces.add(root, policy)
    store = AppState(tmp_path / "state.json", Prefs()).store
    vault = VaultService(cast(WorkspaceService, spaces))
    coordinator = MutationCoordinator()
    saves = _Saves(
        VaultPublicationService(PublicationJournal(store), spaces.resolve_root, coordinator)
    )
    proposals = ProposalService(cast(WorkspaceService, spaces), store, vault, wall=lambda: NOON)
    service = MemoryService(
        cast(WorkspaceService, spaces),
        store,
        vault,
        cast(VaultPublicationService, saves),
        proposals,
        lease_wait_s=LEASE_WAIT_S,
        wall=lambda: NOON,
    )
    return Harness(service, proposals, vault, saves, store, spaces, root, wid, coordinator)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return _harness(tmp_path, "ordinary")


@pytest.fixture
def protected(tmp_path: Path) -> Harness:
    return _harness(tmp_path, "protected")


async def _code(call: Awaitable[Any]) -> MemoryServiceError:
    with pytest.raises(MemoryServiceError) as raised:
        await call
    return raised.value


async def test_memory_gains_no_authority_from_its_own_file(harness: Harness) -> None:
    """Authority is this device's `memory_reviews` hash, never the file's own word."""
    memory_id, revision = harness.record(status="approved")  # imported or restored
    assert harness.states() == {memory_id: "pending"}

    before = tree(harness.root)
    outcome = await harness.service.approve(harness.wid, memory_id, revision, *harness.admission())
    assert outcome == Reviewed(memory_id, revision)
    assert tree(harness.root) == before  # no vault byte written
    assert harness.saves.refs == []
    assert harness.states() == {memory_id: "active"}

    path = harness.path(memory_id)
    path.write_bytes(path.read_bytes().replace(b"coffee", b"Coffee"))  # one changed byte
    assert harness.states() == {memory_id: "pending"}

    # An inference stays pending even with this device's hash for its exact bytes.
    inference, inference_revision = harness.record(status="approved", kind="inference")
    harness.service._review(harness.wid, inference, inference_revision)
    assert harness.states()[inference] == "pending"


async def test_reviews_name_each_approval_with_its_time(harness: Harness) -> None:
    first, first_revision = harness.record()
    second, second_revision = harness.record()
    other = str(uuid.uuid4())
    assert memory_module.reviews(harness.store, harness.wid) == {}
    await harness.service.approve(harness.wid, first, first_revision, *harness.admission())
    await harness.service.approve(harness.wid, second, second_revision, *harness.admission())
    harness.service._review(str(uuid.uuid4()), other, "f" * 64)
    before = tree(harness.root)

    found = memory_module.reviews(harness.store, harness.wid)

    at = harness.service._stamp()
    assert found == {first: (first_revision, at), second: (second_revision, at)}
    assert tree(harness.root) == before


async def test_a_record_with_a_bom_is_hashed_as_its_file(harness: Harness) -> None:
    """The record is parsed from `Note.source`, so its hash is the file's own."""
    memory_id, revision = harness.record(bom=True)

    await harness.service.approve(harness.wid, memory_id, revision, *harness.admission())

    assert harness.states() == {memory_id: "active"}


def test_list_skips_ordinary_notes_and_shows_every_invalid_record_raw(harness: Harness) -> None:
    candidate, revision = harness.record(status="candidate")
    harness.write("memory/plain.md", b"# Shopping\nMilk\n")  # no marker: an ordinary note
    broken_id = str(uuid.uuid4())
    broken = f"---\nviki_memory: 1\nid: {broken_id}\nid: {broken_id}\n---\nclaim\n"
    harness.write(f"memory/{broken_id}.md", broken.encode())
    elsewhere = str(uuid.uuid4())
    moved = compose(Fields(elsewhere, "owner", "vault", "approved", CREATED), CLAIM)
    renamed = str(uuid.uuid4())
    harness.write(f"memory/{renamed}.md", moved.encode())  # its id is not its file stem
    undecodable = str(uuid.uuid4())
    harness.write(f"memory/{undecodable}.md", b"---\nviki_memory: 1\n\xff\xfe\n")
    nested, _ = harness.record()
    (harness.root / "memory" / "sub").mkdir()
    harness.path(nested).rename(harness.root / "memory" / "sub" / f"{nested}.md")
    harness.write(f"notes/{elsewhere}.md", moved.encode())

    listed = harness.service.list(harness.wid)

    assert [(item.ref, item.sha256, item.state) for item in listed.items] == [
        (f"memory/{candidate}.md", revision, "candidate")
    ]
    assert listed.items[0].claim == CLAIM and listed.items[0].superseded_by is None
    assert sorted(listed.invalid, key=lambda entry: entry.ref) == sorted(
        [
            InvalidMemory(f"memory/{broken_id}.md", "duplicate_key", broken),
            InvalidMemory(f"memory/{renamed}.md", "bad_id", moved),
            InvalidMemory(f"memory/{undecodable}.md", "invalid_encoding", None),
        ],
        key=lambda entry: entry.ref,
    )
    assert all(entry.state == "invalid" for entry in listed.invalid)
    assert listed.partial is False


async def test_a_write_names_the_revision_it_read_and_a_legal_transition(
    harness: Harness,
) -> None:
    memory_id, revision = harness.record(status="candidate")
    retired, retired_revision = harness.record(status="retired")
    admission = harness.admission()
    before = tree(harness.root)

    stale = await _code(harness.service.approve(harness.wid, memory_id, "0" * 64, *admission))
    assert (stale.code, stale.extra) == ("stale_revision", {"current_revision": revision})
    for call in (
        harness.service.approve(harness.wid, retired, retired_revision, *admission),
        harness.service.retire(harness.wid, retired, retired_revision, *admission),
        harness.service.narrow_scope(harness.wid, memory_id, revision, *admission),
    ):
        assert (await _code(call)).code == "invalid_transition"
    missing = str(uuid.uuid4())
    assert (
        await _code(harness.service.approve(harness.wid, missing, revision, *admission))
    ).code == "missing_note"
    assert (
        await _code(harness.service.approve(harness.wid, "../x", revision, *admission))
    ).code == "invalid_request"
    assert (
        await _code(harness.service.approve(str(uuid.uuid4()), memory_id, revision, *admission))
    ).code == "unknown_workspace"
    blank = await _code(harness.service.correct(harness.wid, memory_id, revision, "", *admission))
    assert (blank.code, blank.extra) == ("invalid_input", {"reason": "bad_claim"})
    assert tree(harness.root) == before
    assert harness.saves.refs == []


async def test_protected_memory_is_a_proposal_and_writes_nothing(protected: Harness) -> None:
    """A protected vault takes no memory write; promotion is a sealed proposal."""
    harness = protected
    candidate, candidate_revision = harness.record(status="candidate")
    approved, approved_revision = harness.record(status="approved")
    admission = harness.admission()
    before = tree(harness.root)

    command_id = str(uuid.uuid4())
    outcome, replayed = await harness.service.promote(
        harness.wid, *admission, command_id=command_id, kind="owner", scope="personal", claim=CLAIM
    )

    assert isinstance(outcome, Proposed) and not replayed
    assert outcome.memory_id == command_id  # the command id is the record's id
    assert outcome.ref == f"memory/{outcome.memory_id}.md"
    row = harness.store.read_vault_proposal(outcome.proposal_id)
    assert row is not None and row["origin"] == "memory_promotion"
    assert row["base_sha256"] is None and row["proposed_sha256"] == outcome.proposed_sha256
    assert CLAIM.encode("utf-8") not in row["body"]
    detail = harness.proposals.detail(outcome.proposal_id)
    assert detail["state"] == "open" and detail["origin_kind"] == "memory_promotion"
    record = parse(cast(str, detail["proposed_text"]))
    assert isinstance(record, Record) and record.sha256 == outcome.proposed_sha256
    assert record.fields == Fields(outcome.memory_id, "owner", "personal", "candidate", STAMP)
    assert record.claim == CLAIM

    for call in (
        harness.service.approve(harness.wid, candidate, candidate_revision, *admission),
        harness.service.retire(harness.wid, approved, approved_revision, *admission),
        harness.service.correct(harness.wid, approved, approved_revision, "New claim", *admission),
        harness.service.narrow_scope(harness.wid, approved, approved_revision, *admission),
    ):
        assert (await _code(call)).code == "vault_protected"
    # Approving bytes the file already says are approved records a hash, in every policy.
    assert await harness.service.approve(
        harness.wid, approved, approved_revision, *admission
    ) == Reviewed(approved, approved_revision)

    assert tree(harness.root) == before
    assert not [e for e in tree(harness.root) if Path(e[0]).name.startswith(STAGE_OR_BACKUP)]
    assert harness.saves.refs == []
    assert harness.states() == {candidate: "candidate", approved: "active"}


@pytest.mark.parametrize("method", ["approve", "retire", "correct", "narrow_scope"])
async def test_each_write_method_refuses_a_protected_vault_on_its_own(
    protected: Harness, method: str
) -> None:
    harness = protected
    memory_id, revision = harness.record(status="candidate", scope="personal")
    admission = harness.admission()
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "approve": lambda: harness.service.approve(harness.wid, memory_id, revision, *admission),
        "retire": lambda: harness.service.retire(harness.wid, memory_id, revision, *admission),
        "correct": lambda: harness.service.correct(
            harness.wid, memory_id, revision, "New claim", *admission
        ),
        "narrow_scope": lambda: harness.service.narrow_scope(
            harness.wid, memory_id, revision, *admission
        ),
    }
    before = tree(harness.root)

    assert (await _code(calls[method]())).code == "vault_protected"

    assert tree(harness.root) == before
    assert harness.saves.refs == []


def _reparse_folder(link: Path, target: Path) -> None:
    """A folder that exists as a reparse point: a junction on Windows, a symlink elsewhere."""
    target.mkdir()
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


async def test_a_vault_without_a_memory_folder_lists_nothing_and_takes_a_proposal(
    tmp_path: Path,
) -> None:
    """A vault that never held a memory has none to list, and a protected promotion into
    it proposes the folder's first record rather than failing `unavailable`."""
    harness = _harness(tmp_path, "protected", memory=False)
    before = tree(harness.root)

    assert harness.service.list(harness.wid) == MemoryList((), (), False)
    outcome, _ = await harness.service.promote(
        harness.wid,
        *harness.admission(),
        command_id=str(uuid.uuid4()),
        kind="owner",
        scope="vault",
        claim=CLAIM,
    )

    assert isinstance(outcome, Proposed)
    assert harness.proposals.detail(outcome.proposal_id)["state"] == "open"
    assert tree(harness.root) == before


@pytest.mark.parametrize("shape", ["reparse_point", "file"])
async def test_a_memory_that_exists_but_is_not_a_plain_folder_still_refuses(
    tmp_path: Path, shape: str
) -> None:
    """Only a folder that does not exist counts as absent; one that is unsafe refuses as before."""
    harness = _harness(tmp_path, "protected", memory=False)
    if shape == "reparse_point":
        _reparse_folder(harness.root / "memory", tmp_path / "elsewhere")
    else:
        (harness.root / "memory").write_bytes(b"not a folder\n")

    refused = await _code(
        harness.service.promote(
            harness.wid,
            *harness.admission(),
            command_id=str(uuid.uuid4()),
            kind="owner",
            scope="vault",
            claim=CLAIM,
        )
    )

    assert refused.code == "unavailable"
    assert harness.store.list_vault_proposals(harness.wid) == []
    assert harness.service.list(harness.wid).items == ()


async def test_approving_a_candidate_or_an_inference_writes_it_and_records_that_hash(
    harness: Harness,
) -> None:
    candidate, revision = harness.record(status="candidate")
    inference, inference_revision = harness.record(status="approved", kind="inference")
    admission = harness.admission()

    written = await harness.service.approve(harness.wid, candidate, revision, *admission)
    promoted = await harness.service.approve(harness.wid, inference, inference_revision, *admission)

    assert isinstance(written, Written) and isinstance(promoted, Written)
    assert written.sha256 == sha(harness.path(candidate).read_bytes())
    assert harness.parsed(candidate).fields == Fields(
        candidate, "owner", "vault", "approved", CREATED, reviewed=STAMP
    )
    assert harness.parsed(inference).fields == Fields(
        inference, "assistant", "vault", "approved", CREATED, reviewed=STAMP
    )
    assert harness.saves.refs == [f"memory/{candidate}.md", f"memory/{inference}.md"]
    assert harness.states() == {candidate: "active", inference: "active"}


async def test_a_held_lease_refuses_vault_locked_once_the_deadline_passes(
    harness: Harness,
) -> None:
    """A memory save behind a long backup is refused, not parked on a request slot."""
    candidate, revision = harness.record(status="candidate")
    before = tree(harness.root)

    root = harness.spaces.resolve_root(harness.wid, *harness.admission())
    async with harness.coordinator.lease(root, (f"memory/{candidate}.md",)):
        async with asyncio.timeout(HANG_GUARD_S):
            refused = await _code(
                harness.service.approve(harness.wid, candidate, revision, *harness.admission())
            )

    assert refused.code == "vault_locked"
    assert tree(harness.root) == before  # no stage file, no rewrite
    assert harness.states() == {candidate: "candidate"}  # no hash recorded either


async def test_a_correction_saves_the_new_record_before_retiring_the_old(
    harness: Harness,
) -> None:
    old, revision = harness.record(status="approved", kind="inference", scope="personal")
    await harness.service.approve(harness.wid, old, revision, *harness.admission())
    revision = sha(harness.path(old).read_bytes())
    harness.saves.refs.clear()
    harness.saves.creates.clear()

    outcome = await harness.service.correct(
        harness.wid, old, revision, "Only tea in the morning", *harness.admission()
    )

    assert isinstance(outcome, Corrected)
    new = outcome.record.memory_id
    assert harness.saves.refs == [f"memory/{new}.md", f"memory/{old}.md"]
    # Only the new record's create may make a folder; the retirement rewrites in place.
    assert harness.saves.creates == [True, False]
    assert outcome.record.created_folders == () and outcome.retired.created_folders == ()
    assert harness.parsed(new).fields == Fields(
        new, "assistant", "personal", "approved", STAMP, reviewed=STAMP, supersedes=old
    )
    assert harness.parsed(new).claim == "Only tea in the morning"
    assert harness.parsed(old).fields.status == "retired"
    assert harness.parsed(old).fields.retired_by == new
    assert outcome.retired.sha256 == sha(harness.path(old).read_bytes())
    listed = harness.service.list(harness.wid)
    assert {item.fields.id: (item.state, item.superseded_by) for item in listed.items} == {
        old: ("retired", None),
        new: ("active", None),
    }


async def test_a_correction_cut_between_its_saves_is_offered_to_finish(harness: Harness) -> None:
    """The new record lands first, so an interruption leaves both, never neither."""
    old, revision = harness.record(status="approved")
    await harness.service.approve(harness.wid, old, revision, *harness.admission())
    harness.saves.fail_at = 2

    cut = await _code(
        harness.service.correct(harness.wid, old, revision, "Correction", *harness.admission())
    )

    new = cast(str, cut.extra["replacement_id"])
    assert cut.code == "target_busy"
    assert harness.parsed(old).fields.status == "approved"
    listed = harness.service.list(harness.wid)
    assert {item.fields.id: (item.state, item.superseded_by) for item in listed.items} == {
        old: ("active", new),
        new: ("active", None),
    }

    harness.saves.fail_at = None
    finished = await harness.service.retire(
        harness.wid, old, revision, *harness.admission(), retired_by=new
    )

    assert isinstance(finished, Written)
    assert harness.parsed(old).fields.retired_by == new
    listed = harness.service.list(harness.wid)
    assert {item.fields.id: (item.state, item.superseded_by) for item in listed.items} == {
        old: ("retired", None),
        new: ("active", None),
    }


def test_only_an_active_record_marks_the_one_it_supersedes(harness: Harness) -> None:
    """A candidate or a pending record naming an active one, as another tool may write, is no
    interrupted correction, so the panel never offers to retire the user's record for it."""
    old, revision = harness.record(status="approved")
    harness.service._review(harness.wid, old, revision)
    expected = {old: ("active", None)}
    # A file `approved` with no hash here is pending.
    for status, state in (("candidate", "candidate"), ("approved", "pending")):
        rival = str(uuid.uuid4())
        text = compose(Fields(rival, "assistant", "vault", status, CREATED, supersedes=old), CLAIM)
        harness.write(f"memory/{rival}.md", text.encode("utf-8"))
        expected[rival] = (state, None)

    listed = harness.service.list(harness.wid)

    assert {item.fields.id: (item.state, item.superseded_by) for item in listed.items} == expected


async def test_narrowing_a_personal_record_keeps_only_an_approval_it_had(
    harness: Harness,
) -> None:
    active, revision = harness.record(status="approved", scope="personal")
    await harness.service.approve(harness.wid, active, revision, *harness.admission())
    pending, pending_revision = harness.record(status="approved", scope="personal")

    narrowed = await harness.service.narrow_scope(
        harness.wid, active, revision, *harness.admission()
    )
    await harness.service.narrow_scope(harness.wid, pending, pending_revision, *harness.admission())

    assert isinstance(narrowed, Written)
    assert harness.parsed(active).fields.scope == "vault"
    assert harness.parsed(pending).fields.scope == "vault"
    assert harness.states() == {active: "active", pending: "pending"}


async def test_promoting_in_an_ordinary_vault_writes_a_candidate(harness: Harness) -> None:
    command_id = str(uuid.uuid4())
    outcome, replayed = await harness.service.promote(
        harness.wid,
        *harness.admission(),
        command_id=command_id,
        kind="inference",
        scope="vault",
        claim=CLAIM,
    )

    assert isinstance(outcome, Written) and not replayed
    assert outcome.memory_id == command_id and outcome.operation_id is not None
    assert outcome.ref == f"memory/{outcome.memory_id}.md"
    assert outcome.sha256 == sha(harness.path(outcome.memory_id).read_bytes())
    assert harness.parsed(outcome.memory_id).fields == Fields(
        outcome.memory_id, "inference", "vault", "candidate", STAMP
    )
    assert harness.states() == {outcome.memory_id: "candidate"}
    assert harness.store.list_vault_proposals(harness.wid) == []
    assert harness.saves.creates == [True] and outcome.created_folders == ()


async def test_a_first_promotion_makes_the_memory_folder(tmp_path: Path) -> None:
    """A vault that never held a memory gets `memory/` from its first record's create."""
    harness = _harness(tmp_path, "ordinary", memory=False)

    outcome, _ = await harness.service.promote(
        harness.wid,
        *harness.admission(),
        command_id=str(uuid.uuid4()),
        kind="owner",
        scope="vault",
        claim=CLAIM,
    )

    assert isinstance(outcome, Written) and outcome.created_folders == ("memory",)
    assert harness.path(outcome.memory_id).is_file()
    assert harness.states() == {outcome.memory_id: "candidate"}


def _promote(harness: Harness, command_id: str) -> Awaitable[tuple[Written | Proposed, bool]]:
    return harness.service.promote(
        harness.wid, *harness.admission(), command_id=command_id, kind="owner", scope="vault",
        claim=CLAIM,
    )  # fmt: skip


def _files(root: Path) -> list[Entry]:
    """The files only: a refused create stages and removes a file, which moves its folder's mtime
    and nothing else."""
    return [entry for entry in tree(root) if entry[1] != -1]


async def test_a_retried_create_meets_its_own_record_and_writes_nothing_twice(
    harness: Harness,
) -> None:
    """The command id is the record's id, so a retry after a lost answer finds the record
    its first try made; there is no second record and no vault byte moves."""
    command_id = str(uuid.uuid4())
    first, first_replayed = await _promote(harness, command_id)
    assert isinstance(first, Written) and first.memory_id == command_id
    # The user has since edited the record; the replay names what is there now.
    edited = harness.write(f"memory/{command_id}.md", harness.path(command_id).read_bytes() + b"\n")
    before = _files(harness.root)

    second, replayed = await _promote(harness, command_id)

    assert isinstance(first, Written) and not first_replayed and first.operation_id is not None
    assert second == Written(command_id, f"memory/{command_id}.md", edited, None)
    assert replayed
    assert _files(harness.root) == before
    assert [item.fields.id for item in harness.service.list(harness.wid).items] == [command_id]
    assert harness.saves.refs == [f"memory/{command_id}.md"] * 2  # the retry's own save refused


@pytest.mark.parametrize("shape", ["another_id", "not_a_record", "broken_marker"])
async def test_a_file_at_the_ref_that_is_not_the_commands_record_is_a_conflict(
    harness: Harness, shape: str
) -> None:
    """Only a record naming the command's own id is a replay; any other file there, or one
    that does not parse, is a genuine conflict and is left as it is."""
    command_id = str(uuid.uuid4())
    other = compose(Fields(str(uuid.uuid4()), "owner", "vault", "approved", CREATED), CLAIM)
    own = compose(Fields(command_id, "owner", "vault", "candidate", CREATED), CLAIM)
    data = {
        "another_id": other.encode("utf-8"),
        "not_a_record": b"# A note the owner wrote\n",
        "broken_marker": own.replace("kind: owner", "kind: rumour", 1).encode("utf-8"),
    }[shape]
    harness.write(f"memory/{command_id}.md", data)
    before = _files(harness.root)

    refused = await _code(_promote(harness, command_id))

    assert refused.code == "conflict"
    assert _files(harness.root) == before


@pytest.mark.parametrize("later", [False, True])
async def test_a_retried_protected_create_replays_its_open_proposal(
    protected: Harness, later: bool
) -> None:
    """The retry's text has its own `created` stamp unless it lands on the same instant, so
    the proposal at the ref is told a replay by its origin, not by its hash."""
    command_id = str(uuid.uuid4())
    first, _ = await _promote(protected, command_id)
    if later:
        protected.service._wall = lambda: NOON + 5_000_000_000
    before = tree(protected.root)

    second, replayed = await _promote(protected, command_id)

    assert isinstance(first, Proposed) and replayed
    assert second == first
    assert len(protected.store.list_vault_proposals(protected.wid)) == 1
    assert tree(protected.root) == before


async def test_a_proposal_that_is_not_a_memory_promotion_is_not_a_replay(
    protected: Harness,
) -> None:
    """A row at the ref from another origin is a conflict; only the minting command's
    `memory_promotion` is a replay."""
    command_id = str(uuid.uuid4())
    ref = f"memory/{command_id}.md"
    protected.store.write_vault_proposal(
        {
            "proposal_id": str(uuid.uuid4()),
            "workspace_id": protected.wid,
            "ref": ref,
            "origin": "note_edit",
            "base_sha256": "a" * 64,
            "proposed_sha256": "b" * 64,
            "body": b"sealed",
            "text_bytes": 6,
            "handed_at": None,
            "created_at": STAMP,
        }
    )

    refused = await _code(_promote(protected, command_id))

    assert refused.code == "proposal_conflict"
    assert len(protected.store.list_vault_proposals(protected.wid)) == 1


@pytest.mark.parametrize("command_id", ["x", "A0000000-0000-4000-8000-000000000002", ""])
async def test_a_create_needs_a_canonical_command_id(harness: Harness, command_id: str) -> None:
    before = tree(harness.root)

    refused = await _code(_promote(harness, command_id))

    assert refused.code == "invalid_request"
    assert tree(harness.root) == before and harness.saves.refs == []


@pytest.mark.parametrize("method", ["approve", "retire", "narrow_scope", "correct"])
async def test_a_rewrite_never_recreates_a_folder_that_vanished(
    harness: Harness, method: str
) -> None:
    """A save against an expected hash does not make folders: with `memory/` gone between the
    read and the save, the rewrite refuses and the folder stays gone."""
    memory_id, revision = harness.record(status="candidate", scope="personal")
    admission = harness.admission()
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "approve": lambda: harness.service.approve(harness.wid, memory_id, revision, *admission),
        "retire": lambda: harness.service.retire(harness.wid, memory_id, revision, *admission),
        "narrow_scope": lambda: harness.service.narrow_scope(
            harness.wid, memory_id, revision, *admission
        ),
        "correct": lambda: harness.service.correct(
            harness.wid, memory_id, revision, "Correction", *admission
        ),
    }
    folder = harness.root / "memory"
    if method == "correct":  # the retirement, second, after the new record's create
        harness.saves.before = lambda: (
            shutil.rmtree(folder) if len(harness.saves.refs) == 2 else None
        )
    else:
        harness.saves.before = lambda: shutil.rmtree(folder)

    refused = await _code(calls[method]())

    assert refused.code == "unsafe_parent"
    assert not folder.exists()
    assert harness.saves.creates[-1] is False


async def test_a_retirement_makes_an_attached_excerpt_stale(harness: Harness) -> None:
    memory_id, revision = harness.record(status="approved")
    await harness.service.approve(harness.wid, memory_id, revision, *harness.admission())
    ref = f"memory/{memory_id}.md"
    lines = harness.path(memory_id).read_bytes().count(b"\n")
    part = parse_excerpt_part(harness.vault.excerpt(harness.wid, ref, 1, lines).part)
    assert len(resolve_excerpts(harness.vault, harness.wid, [part])) == 1

    await harness.service.retire(harness.wid, memory_id, revision, *harness.admission())

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(harness.vault, harness.wid, [part])
    assert raised.value.code == "excerpt_stale"


@pytest.mark.parametrize("budget", [3000, 4096])
@pytest.mark.parametrize("invalid_encoding", [False, True])
def test_list_charges_actual_successful_and_failed_reads(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    invalid_encoding: bool,
) -> None:
    refs = [f"memory/{index:02}.md" for index in range(3)]
    for ref in refs:
        harness.write(ref, b"x")
    original_scan, original_read = harness.vault.scan, harness.vault.read
    marker = b"---\nviki_memory: 1\n---\n"
    grown = marker + b"x" * (2048 - len(marker))
    read_refs: list[str] = []
    read_bytes = 0

    def scan(workspace_id: str) -> Any:
        listing = original_scan(workspace_id)
        assert sum(entry.bytes for entry in listing.entries) == 3
        for index, ref in enumerate(refs):
            body = b"\xff" + grown[1:] if invalid_encoding and index == 0 else grown
            harness.write(ref, body)
        return listing

    def read(workspace_id: str, ref: str) -> Any:
        nonlocal read_bytes
        read_refs.append(ref)
        try:
            note = original_read(workspace_id, ref)
        except VaultError as exc:
            read_bytes += exc.extra.get("read_bytes", 0)
            raise
        read_bytes += note.byte_size
        return note

    monkeypatch.setattr(memory_module, "MAX_SCAN_BYTES", budget)
    monkeypatch.setattr(harness.vault, "scan", scan)
    monkeypatch.setattr(harness.vault, "read", read)

    listed = harness.service.list(harness.wid)

    assert read_refs == refs[:2]  # a growth detection read is individually bounded
    assert read_bytes == 4096
    assert listed.partial is True and listed.items == ()
    assert len(listed.invalid) == budget // 2048
    retained = sum(len(item.text.encode("utf-8")) for item in listed.invalid if item.text)
    assert retained <= budget
    if invalid_encoding:
        assert listed.invalid[0] == InvalidMemory(refs[0], "invalid_encoding", None)


def test_list_uses_freed_bytes_when_notes_shrink_after_scan(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [f"memory/{index:02}.md" for index in range(3)]
    marker = b"---\nviki_memory: 1\n---\n"
    for ref in refs:
        harness.write(ref, marker + b"x" * (2048 - len(marker)))
    original_scan = harness.vault.scan

    def scan(workspace_id: str) -> Any:
        listing = original_scan(workspace_id)
        for ref in refs:
            harness.write(ref, marker + b"x" * (1024 - len(marker)))
        return listing

    monkeypatch.setattr(memory_module, "MAX_SCAN_BYTES", 4096)
    monkeypatch.setattr(harness.vault, "scan", scan)

    listed = harness.service.list(harness.wid)

    assert [item.ref for item in listed.invalid] == refs
    assert listed.partial is False
