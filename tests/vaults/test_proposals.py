"""The protected-proposal service: sealed user text, derived states and no vault write at all.

Every row runs against a real on-disk vault, a real store and the real sealing, so a write path
added to `proposals.py` shows up as a changed byte or modification time in `tree()`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent import protection
from viki_agent.domain import CanonicalRoot
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults import proposals
from viki_agent.vaults.proposals import (
    _CONTEXT,
    MAX_DIFF_LINES,
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSALS,
    ProposalError,
    ProposalService,
)
from viki_agent.vaults.vault import VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

SECRET = "Buy crème fraîche\nThen take a walk\n"
BASE_TEXT = "The original note\n"
STAGE_OR_BACKUP = (".viki-stage-", ".viki-backup-")
# A fixed wall clock, so `created_at` and `handed_at` are exact strings rather than shapes.
NOON = 1_700_000_000_123_456_789
STAMP = "2023-11-14T22:13:20.123Z"


class _Workspaces:
    """Only what `VaultService.read` and the service's policy check reach."""

    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}
        self.available = True

    def add(self, root: Path, policy: str = "protected") -> str:
        info = os.stat(root)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            policy=policy,
            root=CanonicalRoot(
                key=root_key(root.resolve()),
                display=str(root.resolve()),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        return workspace_id

    def get(self, workspace_id: str) -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root if self.available else None

    def list(self) -> list[Any]:
        return list(self.spaces.values())


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


@dataclass
class Harness:
    service: ProposalService
    store: SessionStore
    spaces: _Workspaces
    root: Path
    wid: str
    clock: list[int]
    roots: list[Path] = field(default_factory=list)

    def note(self, ref: str, text: str, *, root: Path | None = None) -> str:
        """Write a note the way an editor outside the app would, and answer its SHA-256."""
        path = (root or self.root) / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def create(self, ref: str = "notes/a.md", text: str = SECRET) -> dict[str, object]:
        return self.service.create(self.wid, ref, text, self.note(ref, BASE_TEXT))

    def row(self, proposal_id: object) -> dict[str, Any]:
        row = self.store.read_vault_proposal(cast(str, proposal_id))
        assert row is not None
        return row

    def unchanged(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run one call with every vault tree hashed before and after it."""
        before = {root: tree(root) for root in self.roots}
        try:
            return call(*args)
        finally:
            for root in self.roots:
                after = tree(root)
                assert after == before[root], root
                assert not [
                    entry for entry in after if Path(entry[0]).name.startswith(STAGE_OR_BACKUP)
                ], root


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "vault"
    root.mkdir()
    spaces = _Workspaces()
    state = AppState(tmp_path / "state.json", Prefs())
    clock = [NOON]
    service = ProposalService(
        cast(WorkspaceService, spaces),
        state.store,
        VaultService(cast(WorkspaceService, spaces)),
        wall=lambda: clock[0],
    )
    return Harness(service, state.store, spaces, root, spaces.add(root), clock, roots=[root])


def _code(call: Callable[..., Any], *args: Any) -> str:
    with pytest.raises(ProposalError) as raised:
        call(*args)
    return raised.value.code


def test_create_seals_a_proposal_against_a_protected_vault(harness: Harness) -> None:
    base = harness.note("notes/a.md", BASE_TEXT)

    item = harness.unchanged(harness.service.create, harness.wid, "notes/a.md", SECRET, base)

    assert item["kind"] == "vault_proposal"
    assert item["state"] == "open" and item["attention"] is True
    assert item["ref"] == "notes/a.md" and item["title"] == "notes/a.md"
    assert item["origin"] == {"workspace_id": harness.wid, "session_id": None}
    assert item["origin_kind"] == "note_edit" and item["created_at"] == STAMP
    assert item["base_revision"] == base and item["handed_at"] is None
    assert item["revision"] == hashlib.sha256(SECRET.encode("utf-8")).hexdigest()
    assert item["text_bytes"] == len(SECRET.encode("utf-8"))
    body = harness.row(item["id"])["body"]
    assert isinstance(body, bytes) and SECRET.encode("utf-8") not in body


def test_detail_unseals_the_body_and_diffs_it_against_the_note(harness: Harness) -> None:
    item = harness.create()

    detail = harness.unchanged(harness.service.detail, item["id"])

    assert detail["proposed_text"] == SECRET
    assert detail["state"] == "open" and detail["current_revision"] == item["base_revision"]
    diff = cast(str, detail["diff"])
    assert diff.splitlines()[:2] == ["--- a/notes/a.md", "+++ b/notes/a.md"]
    assert "-The original note" in diff and "+Buy crème fraîche" in diff
    assert detail["diff_reason"] is None


@pytest.mark.parametrize("policy", ["ordinary", "existing_readonly"])
def test_a_vault_that_is_not_protected_takes_no_proposal(harness: Harness, policy: str) -> None:
    other = harness.spaces.add(harness.root, policy)
    base = harness.note("notes/a.md", BASE_TEXT)

    with pytest.raises(ProposalError) as raised:
        harness.unchanged(harness.service.create, other, "notes/a.md", SECRET, base)

    assert raised.value.code == "not_protected" and raised.value.extra == {"policy": policy}


def test_an_unknown_workspace_and_an_unknown_proposal(harness: Harness) -> None:
    missing = str(uuid.uuid4())

    assert (
        _code(harness.service.create, missing, "notes/a.md", SECRET, "0" * 64)
        == "unknown_workspace"
    )
    for call in (harness.service.detail, harness.service.handoff, harness.service.withdraw):
        assert _code(harness.unchanged, call, missing) == "unknown_proposal"


@pytest.mark.parametrize(
    ("text", "base"),
    [
        (SECRET, "nothex" + "0" * 58),
        (SECRET, "0" * 63),
        (SECRET, "A" * 64),
        (SECRET, None),
        (1, "0" * 64),
    ],
)
def test_text_that_is_not_a_string_and_a_revision_that_is_not_a_sha_are_refused(
    harness: Harness, text: object, base: object
) -> None:
    assert _code(harness.service.create, harness.wid, "notes/a.md", text, base) == "invalid_request"


@pytest.mark.parametrize(
    "ref", ["", "a//b.md", "../x.md", "/x.md", "a\\b.md", "notes/a.txt", "a.md/"]
)
def test_an_unsafe_ref_or_one_the_reader_does_not_take_is_invalid(
    harness: Harness, ref: str
) -> None:
    assert _code(harness.service.create, harness.wid, ref, SECRET, "0" * 64) == "invalid_ref"


@pytest.mark.parametrize(
    "ref", [".claude/x.md", "a/b/.Git/c.md", "a/Credentials.json/n.md", "a/.env.prod/n.md"]
)
def test_a_private_name_at_any_depth_cannot_be_proposed_against(harness: Harness, ref: str) -> None:
    assert _code(harness.service.create, harness.wid, ref, SECRET, "0" * 64) == "private_path"


def test_an_instruction_file_may_be_proposed_against_because_nothing_is_written(
    harness: Harness,
) -> None:
    """`excluded_part(write=False)`: the save's write-only set guards a write, and this is none."""
    base = harness.note("CLAUDE.md", BASE_TEXT)

    item = harness.unchanged(harness.service.create, harness.wid, "CLAUDE.md", SECRET, base)

    assert item["ref"] == "CLAUDE.md" and item["state"] == "open"


def test_a_proposal_over_the_byte_cap_is_refused_before_any_read(harness: Harness) -> None:
    with pytest.raises(ProposalError) as raised:
        harness.service.create(
            harness.wid, "notes/absent.md", "x" * (MAX_PROPOSAL_BYTES + 1), "0" * 64
        )

    assert raised.value.code == "too_large"
    assert raised.value.extra == {"limit": MAX_PROPOSAL_BYTES}


def test_a_missing_note_and_an_unreadable_root(harness: Harness) -> None:
    harness.note("notes/other.md", BASE_TEXT)

    assert (
        _code(harness.service.create, harness.wid, "notes/absent.md", SECRET, "0" * 64)
        == "source_missing"
    )

    harness.spaces.available = False
    assert (
        _code(harness.service.create, harness.wid, "notes/other.md", SECRET, "0" * 64)
        == "unavailable"
    )


def test_a_note_changed_since_the_edit_is_stale_and_names_its_revision(harness: Harness) -> None:
    current = harness.note("notes/a.md", "The current note\n")

    with pytest.raises(ProposalError) as raised:
        harness.service.create(harness.wid, "notes/a.md", SECRET, "0" * 64)

    assert raised.value.code == "stale_revision"
    assert raised.value.extra == {"current_revision": current}


def test_a_memory_promotion_proposes_a_new_note_against_its_absence(harness: Harness) -> None:
    """The second origin: `memory.MemoryService.promote` proposes a record no file holds yet,
    even into a folder that does not exist yet."""
    item = harness.unchanged(
        lambda: harness.service.create(
            harness.wid, "memory/new.md", SECRET, None, origin="memory_promotion"
        )
    )
    proposal_id = cast(str, item["id"])

    assert item["origin_kind"] == "memory_promotion" and item["base_revision"] is None
    assert harness.row(proposal_id)["base_sha256"] is None
    detail = harness.unchanged(harness.service.detail, proposal_id)
    assert detail["state"] == "open" and detail["proposed_text"] == SECRET
    assert detail["diff_reason"] == "no_source"
    assert harness.unchanged(harness.service.handoff, proposal_id)["state"] == "handed"

    harness.note("memory/new.md", SECRET)  # claude applied it, inside the vault
    assert harness.service.detail(proposal_id)["state"] == "applied"
    harness.note("memory/new.md", BASE_TEXT)
    assert harness.service.detail(proposal_id)["state"] == "stale"

    taken = harness.note("memory/taken.md", BASE_TEXT)
    with pytest.raises(ProposalError) as raised:
        harness.service.create(
            harness.wid, "memory/taken.md", SECRET, None, origin="memory_promotion"
        )
    assert (raised.value.code, raised.value.extra) == (
        "stale_revision",
        {"current_revision": taken},
    )
    # A note edit still names its base, and there is no third origin.
    assert _code(harness.service.create, harness.wid, "notes/b.md", SECRET, None) == (
        "invalid_request"
    )
    with pytest.raises(ProposalError) as raised:
        harness.service.create(harness.wid, "notes/a.md", SECRET, taken, origin=cast(Any, "x"))
    assert raised.value.code == "invalid_request"


def test_a_proposal_equal_to_the_note_is_no_change(harness: Harness) -> None:
    base = harness.note("notes/a.md", SECRET)

    assert _code(harness.service.create, harness.wid, "notes/a.md", SECRET, base) == "no_change"


def test_a_replay_answers_the_same_proposal_id_and_a_rewrite_conflicts(harness: Harness) -> None:
    item = harness.create()
    base = cast(str, item["base_revision"])

    with pytest.raises(ProposalError) as replay:
        harness.service.create(harness.wid, "notes/a.md", SECRET, base)
    with pytest.raises(ProposalError) as conflict:
        harness.service.create(harness.wid, "notes/a.md", SECRET + "One more line\n", base)

    assert replay.value.code == "proposal_exists"
    assert replay.value.extra == {"proposal_id": item["id"]}
    assert conflict.value.code == "proposal_conflict"
    assert conflict.value.extra == {"proposal_id": item["id"]}
    assert len(harness.store.list_vault_proposals(harness.wid)) == 1


def test_the_two_hundredth_proposal_is_the_last(harness: Harness) -> None:
    for index in range(MAX_PROPOSALS):
        harness.store.write_vault_proposal(
            {
                "proposal_id": str(uuid.uuid4()),
                "workspace_id": harness.wid,
                "ref": f"notes/filler-{index}.md",
                "origin": "note_edit",
                "base_sha256": None,
                "proposed_sha256": f"{index:064x}",
                "body": b"sealed",
                "text_bytes": 6,
                "handed_at": None,
                "created_at": STAMP,
            }
        )
    base = harness.note("notes/a.md", BASE_TEXT)

    with pytest.raises(ProposalError) as raised:
        harness.service.create(harness.wid, "notes/a.md", SECRET, base)

    assert raised.value.code == "proposals_full"
    assert raised.value.extra == {"limit": MAX_PROPOSALS}


def test_a_failed_seal_is_unavailable_and_carries_no_text(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(data: bytes, *, context: bytes) -> bytes:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "protect", refuse)
    base = harness.note("notes/a.md", BASE_TEXT)

    with pytest.raises(ProposalError) as raised:
        harness.unchanged(harness.service.create, harness.wid, "notes/a.md", SECRET, base)

    assert raised.value.code == "unavailable" and raised.value.extra == {}
    assert SECRET not in str(raised.value)
    assert harness.store.list_vault_proposals(harness.wid) == []


def _store_raises(monkeypatch: pytest.MonkeyPatch, harness: Harness, code: str) -> None:
    def refuse(*args: Any) -> None:
        raise StateError(code)

    monkeypatch.setattr(harness.store, "write_vault_proposal", refuse)
    monkeypatch.setattr(harness.store, "mark_vault_proposal_handed", refuse)


@pytest.mark.parametrize("code", ["busy", "unavailable", "recovery_required"])
def test_a_store_fault_on_create_reaches_the_route_not_invalid_request(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    base = harness.note("notes/a.md", BASE_TEXT)
    _store_raises(monkeypatch, harness, code)

    with pytest.raises(StateError) as raised:
        harness.service.create(harness.wid, "notes/a.md", SECRET, base)

    assert raised.value.code == code


@pytest.mark.parametrize("code", ["busy", "unavailable", "recovery_required"])
def test_a_store_fault_on_handoff_reaches_the_route(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    proposal_id = cast(str, harness.create()["id"])
    _store_raises(monkeypatch, harness, code)

    with pytest.raises(StateError) as raised:
        harness.service.handoff(proposal_id)

    assert raised.value.code == code
    assert harness.row(proposal_id)["handed_at"] is None


def test_the_states_are_re_derived_from_the_note_on_every_call(harness: Harness) -> None:
    item = harness.create()
    proposal_id = cast(str, item["id"])
    assert harness.service.detail(proposal_id)["state"] == "open"

    harness.clock[0] = NOON + 1_000_000_000
    assert harness.service.handoff(proposal_id)["handed_at"] == "2023-11-14T22:13:21.123Z"
    assert harness.service.detail(proposal_id)["state"] == "handed"

    harness.note("notes/a.md", SECRET)  # claude applied it, inside the vault
    assert harness.service.detail(proposal_id)["state"] == "applied"

    harness.note("notes/a.md", "Someone else rewrote this\n")
    assert harness.service.detail(proposal_id)["state"] == "stale"

    (harness.root / "notes" / "a.md").unlink()
    gone = harness.service.detail(proposal_id)
    assert gone["state"] == "source_missing" and gone["attention"] is True
    assert gone["diff"] == "" and gone["diff_reason"] == "no_source"

    harness.spaces.available = False
    unknown = harness.service.detail(proposal_id)
    assert unknown["state"] == "unknown" and unknown["attention"] is False


def test_a_handoff_refuses_every_state_it_may_not_record(harness: Harness) -> None:
    item = harness.create()
    proposal_id = cast(str, item["id"])

    stale_revision = harness.note("notes/a.md", "Someone else rewrote this\n")
    with pytest.raises(ProposalError) as stale:
        harness.unchanged(harness.service.handoff, proposal_id)
    harness.note("notes/a.md", SECRET)
    applied = _code(harness.unchanged, harness.service.handoff, proposal_id)
    (harness.root / "notes" / "a.md").unlink()
    missing = _code(harness.service.handoff, proposal_id)
    harness.spaces.available = False
    unavailable = _code(harness.service.handoff, proposal_id)

    assert stale.value.code == "stale_proposal"
    assert stale.value.extra == {"current_revision": stale_revision}
    assert (applied, missing, unavailable) == ("already_applied", "source_missing", "unavailable")
    assert harness.row(proposal_id)["handed_at"] is None


def test_a_withdraw_inside_the_hand_off_s_note_read_stays_withdrawn(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-off reads the note between its row read and its write, so a withdraw lands there."""
    proposal_id = cast(str, harness.create()["id"])
    vault = harness.service._vault
    read = vault.read

    def withdraw_mid_read(workspace_id: str, ref: str) -> Any:
        note = read(workspace_id, ref)
        harness.service.withdraw(proposal_id)  # the user's withdraw commits in the window
        return note

    monkeypatch.setattr(vault, "read", withdraw_mid_read)

    assert _code(harness.unchanged, harness.service.handoff, proposal_id) == "unknown_proposal"
    assert harness.store.read_vault_proposal(proposal_id) is None
    assert harness.service.inbox_items() == []


def test_a_withdrawn_proposal_leaves_the_inbox(harness: Harness) -> None:
    item = harness.create()
    proposal_id = cast(str, item["id"])

    assert harness.unchanged(harness.service.withdraw, proposal_id) is None

    assert harness.store.read_vault_proposal(proposal_id) is None
    assert harness.service.inbox_items() == []
    assert _code(harness.service.withdraw, proposal_id) == "unknown_proposal"


def test_only_this_proposal_s_own_context_unseals_its_body(harness: Harness) -> None:
    first = harness.create("notes/a.md")
    second = harness.create("notes/b.md")  # the same text under another proposal id
    elsewhere = harness.spaces.add(harness.root)
    body = cast(bytes, harness.row(first["id"])["body"])
    own = _CONTEXT + harness.wid.encode() + b"\0" + cast(str, first["id"]).encode()

    assert protection.unprotect(body, context=own).decode("utf-8") == SECRET

    for wrong in (
        _CONTEXT + harness.wid.encode() + b"\0" + cast(str, second["id"]).encode(),
        _CONTEXT + elsewhere.encode() + b"\0" + cast(str, first["id"]).encode(),
        _CONTEXT + harness.wid.encode() + cast(str, first["id"]).encode(),
        harness.wid.encode() + b"\0" + cast(str, first["id"]).encode(),
    ):
        with pytest.raises(protection.ProtectionError):
            protection.unprotect(body, context=wrong)


def test_a_body_sealed_for_another_proposal_is_unavailable_never_plaintext(
    harness: Harness,
) -> None:
    first = harness.create("notes/a.md")
    second = harness.create("notes/b.md")
    harness.store.write_vault_proposal(
        {**harness.row(second["id"]), "body": harness.row(first["id"])["body"]}
    )

    with pytest.raises(ProposalError) as raised:
        harness.service.detail(cast(str, second["id"]))

    assert raised.value.code == "unavailable" and SECRET not in str(raised.value)
    assert [item["state"] for item in harness.service.inbox_items()] == ["open", "open"]


@pytest.mark.parametrize(
    ("before", "after"),
    [("x\n" * (MAX_DIFF_LINES + 1), "y\n"), ("x\n", "y\n" * (MAX_DIFF_LINES + 1))],
)
def test_a_diff_past_the_line_cap_says_so_instead(
    harness: Harness, before: str, after: str
) -> None:
    base = harness.note("notes/a.md", before)
    item = harness.service.create(harness.wid, "notes/a.md", after, base)

    detail = harness.service.detail(cast(str, item["id"]))

    assert detail["diff"] == "" and detail["diff_reason"] == "too_large"
    assert detail["proposed_text"] == after


def test_the_inbox_lists_every_workspace_s_proposals(harness: Harness, tmp_path: Path) -> None:
    second_root = tmp_path / "second"
    second_root.mkdir()
    harness.roots.append(second_root)
    second = harness.spaces.add(second_root)
    base = harness.note("notes/b.md", "Another note\n", root=second_root)
    first = harness.create("notes/a.md")
    harness.service.create(second, "notes/b.md", SECRET, base)

    items = harness.unchanged(harness.service.inbox_items)

    assert [item["origin"] for item in items] == [
        {"workspace_id": harness.wid, "session_id": None},
        {"workspace_id": second, "session_id": None},
    ]
    assert items[0]["id"] == first["id"]
    assert all(item["state"] == "open" and "proposed_text" not in item for item in items)


def test_the_vault_tree_is_byte_identical_after_every_method(harness: Harness) -> None:
    """Hard rule 1 proved here: no method writes a vault, so no byte and no mtime moves."""
    base = harness.note("notes/a.md", BASE_TEXT)
    before = tree(harness.root)

    item = harness.unchanged(harness.service.create, harness.wid, "notes/a.md", SECRET, base)
    proposal_id = cast(str, item["id"])
    harness.unchanged(harness.service.detail, proposal_id)
    harness.unchanged(harness.service.inbox_items)
    harness.unchanged(harness.service.handoff, proposal_id)
    harness.unchanged(harness.service.detail, proposal_id)
    for refusal, args in (
        (harness.service.create, (harness.wid, "notes/a.md", SECRET, base)),
        (harness.service.create, (harness.wid, ".claude/x.md", SECRET, base)),
        (harness.service.create, (harness.wid, "notes/absent.md", SECRET, base)),
        (harness.service.detail, (str(uuid.uuid4()),)),
    ):
        with pytest.raises(ProposalError):
            harness.unchanged(refusal, *args)
    harness.unchanged(harness.service.withdraw, proposal_id)

    assert tree(harness.root) == before


def test_the_module_imports_no_writer_and_calls_nothing_that_writes() -> None:
    """The static half of the same claim, read from the syntax tree rather than the prose."""
    module = ast.parse(Path(cast(str, proposals.__file__)).read_text(encoding="utf-8"))
    imported = {
        name.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in node.names
    } | {node.module or "" for node in ast.walk(module) if isinstance(node, ast.ImportFrom)}
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
    }

    assert not {
        name for name in imported if "vault_publication" in name or "vault_mutation" in name
    }
    assert not called & {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "replace",
        "rename",
    }
