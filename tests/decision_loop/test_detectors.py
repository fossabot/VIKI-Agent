"""Tests for `detectors.gather`/`detect`: the active workspace and page turned into candidates,
each switch's gate, the related_ref/evidence coupling, and that every produced candidate
still renders through `templates.render` on the rules path."""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from tests.decision_loop.fakes import settings_values
from viki_agent.automation.brief import (
    Goal,
    InboxFacts,
    InboxItem,
    TaskFacts,
    TaskItem,
    TickedFile,
    VaultFacts,
)
from viki_agent.automation.kanban import Card, KanbanRead
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import (
    contracts,
    detectors,
    improve,
    looks,
    project_plans,
    rules,
    second_looks,
    templates,
    words,
)
from viki_agent.decision_loop.changes import ChangeProposals, ChangeRecord, Held
from viki_agent.decision_loop.contracts import canonical_json
from viki_agent.decision_loop.detectors import Observation, detect, detects_changes, gather
from viki_agent.decision_loop.seats import (
    SeatAsk,
    SeatHold,
    SeatOffer,
    SeatPlan,
    SeatReport,
    SeatWorking,
)
from viki_agent.decision_loop.state import BuiltState, History, Now, Owner, build_state, state_text
from viki_agent.decision_loop.vault_watch import LinkPair, NoteFacts, SupersededPair, VaultChanges
from viki_agent.domain import CanonicalRoot
from viki_agent.profiles.native_assets import NativeAsset, NativeAssetCatalog, NativeAssetError
from viki_agent.vaults import captures
from viki_agent.vaults.decisions import DecisionService
from viki_agent.vaults.vault import (
    MetadataBatch,
    Note,
    NoteEntry,
    ScanResult,
    VaultError,
    VaultService,
)
from viki_agent.workspaces import WorkspaceService, root_key

WID = "ws-1"
TASKS_REF = "10-projects/alpha/tasks.md"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
TODAY = NOW.date()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def workspaces(root: Path, *, kind: str = "vault", chat_only: bool = False) -> WorkspaceService:
    """A fake `WorkspaceService` whose active workspace is `root`, the same shape
    `tests/automation/test_jev_service.py` builds."""
    info = os.stat(root)
    canonical = CanonicalRoot(
        key=root_key(root.resolve()),
        display=str(root.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    workspace = SimpleNamespace(root=canonical, policy="ordinary", persistent=True)
    active = SimpleNamespace(
        workspace=SimpleNamespace(workspace_id=WID, kind=kind, policy="ordinary", persistent=True),
        chat_only=chat_only,
    )
    owner = SimpleNamespace(
        get=lambda _id: workspace,
        current_root=lambda _workspace: workspace.root,
        resolve_active=lambda: active,
    )
    return cast(WorkspaceService, owner)


class BrokenWorkspaces:
    """A `WorkspaceService` whose active workspace can never be resolved."""

    def resolve_active(self) -> object:
        raise RuntimeError("no active workspace")


class BrokenReader:
    """A `VaultService` whose scan always fails, so `read_vault_facts` falls back to empty,
    all-partial facts and `gather`'s own `reader.scan` call for goals fails too."""

    def scan(self, workspace_id: str, *, deadline: float = 5.0) -> ScanResult:
        raise VaultError("unavailable")

    def read(self, workspace_id: str, ref: str) -> Note:
        raise VaultError("unavailable")


class FakeDecisions:
    """`DecisionService.inbox` reduced to its items, or a failure."""

    def __init__(
        self, items: Sequence[Mapping[str, Any]] = (), *, error: Exception | None = None
    ) -> None:
        self.items = list(items)
        self.error = error
        self.calls = 0

    def inbox(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"items": self.items}


def settings(**overrides: Any) -> contracts.DecisionSettings:
    return contracts.parse_settings(settings_values(**overrides))


def page(**overrides: Any) -> contracts.PageFacts:
    """A page with nothing waiting, nothing running, git unknown and no routine, unless
    overridden."""
    base: dict[str, Any] = {
        "native_waiting": 0,
        "runs_working": 0,
        "runs_review": 0,
        "git_state": "unknown",
        "git_dirty": None,
        "routine_id": None,
        "next_run": None,
        "routines_paused": None,
    }
    base.update(overrides)
    return contracts.PageFacts(**base)


def mission(
    mission_id: str,
    *,
    state: str = "done",
    reason: str = "",
    title: str = "Draft the weekly review",
) -> contracts.MissionRecord:
    at = stamp(NOW)
    return contracts.MissionRecord(
        mission_id=mission_id,
        scope=WID,
        session_id=f"session-{mission_id}",
        item_key="k",
        work="run",
        state=state,
        reason=reason,
        title=title,
        brief="",
        created_at=at,
        deadline_at=at,
        finished_at=at,
    )


def proposal(ref: str, workspace_id: str, *, attention: bool = True) -> dict[str, Any]:
    """A vault proposal as `ProposalService.inbox_items` lists it (the keys `detect` reads)."""
    return {
        "kind": "vault_proposal",
        "origin": {"workspace_id": workspace_id, "session_id": None},
        "attention": attention,
        "title": ref,
        "ref": ref,
    }


def built_state(candidate: contracts.Candidate, *never_send: str) -> BuiltState:
    """`candidate`'s state under these never-send entries, with nothing else around it."""
    return build_state(
        candidate,
        Owner(),
        Now(active=None, focus=False, typing=False, time=NOW, nudges_this_hour=0),
        History(),
        replace(settings(), never_send=never_send),
    )


def tasks_observation(*items: TaskItem) -> Observation:
    """A minimal vault `Observation` carrying only these kept tasks; the aggregate counts are
    derived from each item's own due date against `TODAY`."""
    overdue = sum(1 for item in items if item.due < TODAY)
    today_count = sum(1 for item in items if item.due == TODAY)
    soon = len(items) - overdue - today_count
    tasks = TaskFacts(
        overdue=overdue,
        today=today_count,
        soon=soon,
        items=items,
        files=1,
        files_key="k",
        partial=False,
    )
    inbox = InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=False)
    return Observation(
        scope=WID,
        kind="vault",
        vault=VaultFacts(inbox=inbox, tasks=tasks),
        goals=(),
        approvals=(),
        page=None,
        failures=(),
    )


def rich_vault() -> VaultFacts:
    task_item = TaskItem(
        ref=TASKS_REF, line=1, text="Renew the passport", due=TODAY, tags=("alpha",)
    )
    old = InboxItem(
        ref="00-inbox/old.md",
        title="Old capture",
        captured=stamp(NOW - timedelta(days=2)),
        excerpt="old body",
        tags=("idea",),
    )
    new = InboxItem(
        ref="00-inbox/new.md",
        title="New capture",
        captured=stamp(NOW - timedelta(hours=1)),
        excerpt="new body",
        tags=None,
    )
    tasks = TaskFacts(
        overdue=0, today=1, soon=0, items=(task_item,), files=1, files_key="k", partial=False
    )
    inbox = InboxFacts(
        count=2, oldest=old.captured, checked=2, undated=0, partial=False, items=(old, new)
    )
    return VaultFacts(inbox=inbox, tasks=tasks)


def rich_page() -> contracts.PageFacts:
    return page(
        native_waiting=1,
        runs_working=2,
        runs_review=1,
        git_state="known",
        git_dirty=3,
        routine_id="routine-1",
        next_run=NOW + timedelta(hours=1),
        routines_paused=False,
    )


RICH_APPROVALS = (
    {
        "kind": "vault_proposal",
        "ref": "30-knowledge/some-note.md",
        "title": "some-note.md",
        "attention": True,
    },
    {"kind": "skill_proposal", "attention": False},
)


def rich_observation(**overrides: Any) -> Observation:
    base: dict[str, Any] = {
        "scope": WID,
        "kind": "vault",
        "vault": rich_vault(),
        "goals": (),
        "approvals": RICH_APPROVALS,
        "page": rich_page(),
        "failures": (),
    }
    base.update(overrides)
    return Observation(**base)


# A skill hint, the chat's skill-suggestion facts with the vault's own skills, one extra
# link-bearing capture and the vault-change facts, layered onto `rich_observation()` so every
# candidate type except the two beat-cached ones (`retro_due`, `question_tune`, from
# `improve.from_rows`) fires at once; left out of `rich_vault`/`rich_page` themselves so the
# existing inbox tests' exact evidence tuples (two items, neither a link) stay untouched.
_SKILL_HINT = contracts.SkillHint(
    trigger="long_session", sessions=("s-1", "s-2"), titles=("Chat A", "Chat B")
)
_SUGGEST_HINT = contracts.SuggestHint(
    session="s-1", title="Sprint retro for the mobile team", used=("inbox",)
)
_SKILLS = (
    improve.skill_words("inbox", "File the inbox"),
    improve.skill_words("sprint-retro", "Run a sprint retro with the team"),
)
_LINK_CAPTURE = InboxItem(
    ref="00-inbox/link.md",
    title="Link capture",
    captured=stamp(NOW - timedelta(days=3)),
    excerpt="https://example.com/a",
    tags=None,
)


def _note(ref: str, *, tags: tuple[str, ...] | None = (), opening: str = "An opening") -> NoteFacts:
    return NoteFacts(ref=ref, title="A title", tags=tags, opening=opening, linked_stems=())


_CHANGE_A = _note("30-knowledge/alpha.md", tags=("x",))
_CHANGE_B = _note("30-knowledge/beta.md", tags=("y",))
_CHANGE_OLDER = _note("30-knowledge/old-note.md")
_CHANGE_NEWER = _note("30-knowledge/new-note.md")
_TICKED_FILE = TickedFile(
    ref="10-projects/alpha/tasks.md", count=5, digest="e" * 64, tags=("alpha",)
)
_CHANGES = VaultChanges(
    changed=(),
    links=(LinkPair(_CHANGE_A, _CHANGE_B, "Alpha mentions beta"),),
    superseded=(SupersededPair(_CHANGE_OLDER, _CHANGE_NEWER),),
    archive={_TICKED_FILE.ref: "90-archive/alpha tasks done 2026.md"},
    partial=False,
)


def card(
    name: str,
    status: str | None = "blocked",
    *,
    age: timedelta = timedelta(0),
    tags: tuple[str, ...] = (),
    title: str = "",
) -> Card:
    """A Kanban card at `kanban/<name>.md`, last changed `age` before `NOW`."""
    return Card(f"kanban/{name}.md", title or f"Card {name}", status, tags, NOW - age)


# One card that needs the user and one todo card gone stale, so both card types fire.
_KANBAN = KanbanRead(
    (card("blocked"), card("todo", "todo", age=timedelta(days=4))), (), complete=True
)


# A row that needs recovery and a drifted memory, so both vault Look types fire.
_VAULT_LOOK = contracts.VaultLook(
    saves=(contracts.SaveRow("op-1", True, True, NOW),),
    deleted=(),
    usage=(0, 0),
    drift=("memory-1",),
)
# One of each app Look type; `detect` never lists them (the cycle awaits the app reader).
_APP_LOOK = contracts.AppLook(
    waiting=(contracts.WaitingChat("s-1", "tool-1", "permission", NOW - timedelta(minutes=9)),),
    heavy=(contracts.HeavyServer("s-1", "files"),),
    sends=(contracts.UncertainSend("c-1", "s-1", NOW - timedelta(hours=2)),),
    bridges=(contracts.StoppedBridge("telegram", False, None),),
)
# One folder idle long enough for the code look.
_CODE_LOOK = contracts.CodeLook(
    (contracts.CodeFolder("code-1", "app", NOW - contracts.CODE_LOOK_IDLE - timedelta(hours=1)),)
)


def observation_with_improve_hints() -> Observation:
    vault = rich_vault()
    vault = replace(vault, inbox=replace(vault.inbox, items=(*vault.inbox.items, _LINK_CAPTURE)))
    vault = replace(vault, tasks=replace(vault.tasks, ticked=(_TICKED_FILE,)))
    return rich_observation(
        page=replace(rich_page(), skill=_SKILL_HINT, suggest=_SUGGEST_HINT),
        vault=vault,
        changes=_CHANGES,
        skills=_SKILLS,
        kanban=_KANBAN,
        look=_VAULT_LOOK,
    )


# ---- gather ----------------------------------------------------------------------------------


def test_gather_reads_vault_facts_goals_and_approvals_for_a_vault_workspace(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / TASKS_REF, f"- [ ] Renew the passport \U0001f4c5 {TODAY.isoformat()}\n")
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        '---\ntype: project\nstatus: active\ntags: ["Work"]\n---\n# Alpha\nOverview.\n',
    )
    write(vault / "00-inbox" / "a.md", f"---\ncaptured: {stamp(NOW)}\n---\nbody\n")
    owner = workspaces(vault)
    decisions = FakeDecisions([{"ref": "x", "title": "x", "attention": True}])
    obs = gather(owner, VaultService(owner), cast(DecisionService, decisions), None, TODAY)
    assert obs.scope == WID
    assert obs.kind == "vault"
    assert obs.vault is not None
    assert len(obs.vault.tasks.items) == 1
    assert len(obs.vault.inbox.items) == 1
    assert [goal.ref for goal in obs.goals] == ["10-projects/alpha/alpha.md"]
    assert len(obs.approvals) == 1
    assert obs.failures == ()
    assert decisions.calls == 1


class CountingReader(VaultService):
    """A real `VaultService` that counts the blocking calls `gather` makes through it."""

    def __init__(self, owner: WorkspaceService) -> None:
        super().__init__(owner)
        self.calls: dict[str, int] = {"scan": 0, "read": 0, "metadata": 0}

    def scan(self, workspace_id: str, *, deadline: float = 5.0) -> ScanResult:
        self.calls["scan"] += 1
        return super().scan(workspace_id, deadline=deadline)

    def read(self, workspace_id: str, ref: str) -> Note:
        self.calls["read"] += 1
        return super().read(workspace_id, ref)

    def metadata(self, workspace_id: str, refs: list[str]) -> MetadataBatch:
        self.calls["metadata"] += 1
        return super().metadata(workspace_id, refs)


def listed_entries(*refs: str) -> ScanResult:
    entries = tuple(NoteEntry(ref, ref, 1, 1_700_000_000.0 + n) for n, ref in enumerate(refs))
    return ScanResult(entries, False, (), 0, 0, 0)


class ScanReader(BrokenReader):
    """A `VaultService` whose scan answers a fixed result and whose reads find nothing."""

    def __init__(self, scan_result: ScanResult) -> None:
        self.scan_result = scan_result

    def scan(self, workspace_id: str, *, deadline: float = 5.0) -> ScanResult:
        return self.scan_result


def test_gather_lists_the_inbox_from_the_shared_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first 100 `00-inbox` entries by casefold, nested ones included, as (ref, the
    listing's own modified stamp), from the scan `gather` already makes: no scan or read added."""
    vault = tmp_path / "vault"
    for n in range(100):
        write(vault / "00-inbox" / f"c{n:03d}.md", "body\n")
    write(vault / "00-inbox" / "a" / "b.md", "nested\n")  # the 101st capture
    write(vault / "30-knowledge" / "x.md", "not a capture\n")
    owner = workspaces(vault)
    reader = CountingReader(owner)
    obs = gather(owner, reader, cast(DecisionService, FakeDecisions()), None, TODAY)
    refs = [ref for ref, _ in obs.inbox_listed]
    assert len(refs) == 100
    assert refs == sorted(refs, key=str.casefold)
    assert refs[0] == "00-inbox/a/b.md"  # nested, and sorted among the flat ones
    assert "00-inbox/c098.md" in refs and "00-inbox/c099.md" not in refs  # cut after 100
    assert all(not ref.startswith("30-knowledge/") for ref in refs)
    scan = VaultService(owner).scan(WID)
    modified = {entry.ref: entry.modified for entry in scan.entries}
    stamps = dict(obs.inbox_listed)
    assert stamps["00-inbox/a/b.md"] == captures._modified_stamp(modified["00-inbox/a/b.md"])
    # The listing adds no call of its own: the same gather without it counts the same.
    monkeypatch.setattr(detectors, "_inbox_listing", lambda _scan: ())
    plain = CountingReader(owner)
    gather(owner, plain, cast(DecisionService, FakeDecisions()), None, TODAY)
    assert reader.calls == plain.calls
    assert reader.calls["scan"] >= 1


def test_gather_lists_the_inbox_folder_in_any_letter_case_and_nothing_else() -> None:
    owner = workspaces(Path("."))
    scan = listed_entries("00-Inbox/Z.md", "00-inbox/a.md", "00-INBOX/m/n.md", "30-knowledge/k.md")
    obs = gather(
        owner,
        cast(VaultService, ScanReader(scan)),
        cast(DecisionService, FakeDecisions()),
        None,
        TODAY,
    )
    assert [ref for ref, _ in obs.inbox_listed] == [
        "00-inbox/a.md",
        "00-INBOX/m/n.md",
        "00-Inbox/Z.md",
    ]


def test_a_failed_scan_lists_no_inbox(tmp_path: Path) -> None:
    owner = workspaces(tmp_path)
    obs = gather(
        owner,
        cast(VaultService, BrokenReader()),
        cast(DecisionService, FakeDecisions()),
        None,
        TODAY,
    )
    assert obs.inbox_listed == ()
    assert "goals_unknown" in obs.failures


@pytest.mark.parametrize("kind,chat_only", [("vault", True), ("code", False)])
def test_gather_skips_vault_reads_outside_a_vault_workspace(
    tmp_path: Path, kind: str, chat_only: bool
) -> None:
    vault = tmp_path / "vault"
    write(vault / TASKS_REF, f"- [ ] solo \U0001f4c5 {TODAY.isoformat()}\n")
    owner = workspaces(vault, kind=kind, chat_only=chat_only)
    decisions = FakeDecisions([{"ref": "x", "attention": True}])
    obs = gather(owner, VaultService(owner), cast(DecisionService, decisions), None, TODAY)
    assert obs.kind == ("chat_only" if chat_only else "folder")
    assert obs.vault is None
    assert obs.goals == ()
    assert obs.approvals == ()
    assert obs.policy is None  # no vault change and no work outside a vault workspace
    assert decisions.calls == 0  # never called: jev_service's own gate


def test_gather_never_raises_when_the_workspace_cannot_be_resolved() -> None:
    obs = gather(
        cast(WorkspaceService, BrokenWorkspaces()),
        cast(VaultService, BrokenReader()),
        cast(DecisionService, FakeDecisions()),
        None,
        TODAY,
    )
    assert (obs.scope, obs.kind, obs.vault, obs.failures) == (
        "app",
        "chat_only",
        None,
        ("workspace_unknown",),
    )
    assert obs.policy is None


def test_gather_reads_the_vault_policy_as_stored_now(tmp_path: Path) -> None:
    """Each gather reads the vault's policy from its registration (`get`), not the
    launch's snapshot, so a policy changed mid-launch applies from the next cycle."""
    vault = tmp_path / "vault"
    vault.mkdir()
    owner = workspaces(vault)
    decisions = cast(DecisionService, FakeDecisions())
    first = gather(owner, VaultService(owner), decisions, None, TODAY)
    assert (first.policy, first.failures) == ("ordinary", ())
    stored: Any = owner.get(WID)
    stored.policy = "existing_readonly"  # the snapshot from `resolve_active` stays ordinary
    second = gather(owner, VaultService(owner), decisions, None, TODAY)
    assert (second.policy, second.failures) == ("existing_readonly", ())


def test_gather_keeps_an_unstored_vaults_policy_and_reads_a_failed_one_as_none(
    tmp_path: Path,
) -> None:
    """A `VIKI_VAULT` vault that is not stored keeps its own policy without reading a
    registration; a stored one whose registration cannot be read gives None, the strictest,
    and "policy_unknown"."""
    vault = tmp_path / "vault"
    vault.mkdir()
    owner = workspaces(vault)
    reader = VaultService(owner)  # the policy read alone fails below, not the vault reads

    def unreadable(_id: str) -> object:
        raise RuntimeError("store down")

    active: Any = owner.resolve_active()
    broken = cast(WorkspaceService, SimpleNamespace(resolve_active=lambda: active, get=unreadable))
    decisions = cast(DecisionService, FakeDecisions())
    failed = gather(broken, reader, decisions, None, TODAY)
    assert failed.policy is None and "policy_unknown" in failed.failures

    active.workspace.persistent = False
    active.workspace.policy = "protected"
    unstored = gather(broken, reader, decisions, None, TODAY)
    assert unstored.policy == "protected" and "policy_unknown" not in unstored.failures


def test_gather_records_a_failure_code_per_unreadable_part(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    owner = workspaces(vault)
    decisions = FakeDecisions(error=RuntimeError("store down"))
    obs = gather(
        owner, cast(VaultService, BrokenReader()), cast(DecisionService, decisions), None, TODAY
    )
    assert obs.vault is not None
    assert obs.vault.tasks.partial is True
    assert obs.vault.inbox.partial is True
    assert set(obs.failures) == {
        "tasks_partial",
        "inbox_partial",
        "goals_unknown",
        "changes_unknown",
        "proposals_unknown",
    }
    assert obs.changes is None


def test_gather_leaves_the_vault_change_read_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gather` reads no vault change itself. It leaves `read_changes` over its own scan and
    ticked tasks, run once with what the change rows hold back."""
    vault = tmp_path / "vault"
    write(vault / "30-knowledge" / "a.md", "body\n")
    owner = workspaces(vault)
    found = VaultChanges((), (), (), {}, False)
    calls: list[tuple[object, ...]] = []

    def spy(*args: object) -> VaultChanges:
        calls.append(args)
        return found

    monkeypatch.setattr(detectors, "read_changes", spy)
    reader = VaultService(owner)
    obs = gather(owner, reader, cast(DecisionService, FakeDecisions()), None, TODAY)
    assert obs.changes is None and calls == []
    assert obs.vault is not None and obs.read_changes is not None

    held = Held(frozenset(), frozenset(), room=True)
    assert obs.read_changes(held) is found
    [(read_with, scope, _scan, today, ticked, held_with)] = calls
    assert (read_with, scope, today, ticked, held_with) == (
        reader,
        WID,
        TODAY,
        obs.vault.tasks.ticked,
        held,
    )


def test_gather_leaves_the_vault_look_to_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gather` binds the Look reader to the vault, the shared scan (None when it failed) and
    whether the memory part may read, and calls none of it."""
    vault = tmp_path / "vault"
    write(vault / "30-knowledge" / "a.md", "body\n")
    found = contracts.VaultLook(saves=(), deleted=(), usage=(0, 0), drift=())
    calls: list[tuple[str, ScanResult | None, bool]] = []

    def spy(scope: str, scan: ScanResult | None, memory: bool) -> contracts.VaultLook:
        calls.append((scope, scan, memory))
        return found

    def gather_with(owner: WorkspaceService, reader: VaultService, **kwargs: Any) -> Observation:
        return gather(owner, reader, cast(DecisionService, FakeDecisions()), None, TODAY, **kwargs)

    owner = workspaces(vault)
    reader = VaultService(owner)
    scans: list[ScanResult] = []
    real_scan = reader.scan

    def keep_scan(workspace_id: str, **kwargs: Any) -> ScanResult:
        scans.append(real_scan(workspace_id, **kwargs))
        return scans[-1]

    monkeypatch.setattr(reader, "scan", keep_scan)
    obs = gather_with(owner, reader, look=spy)
    assert (obs.look, calls) == (None, [])  # nothing ran inside gather
    assert obs.read_look is not None
    assert obs.read_look() is found
    assert calls == [(WID, scans[0], True)]

    stored: Any = owner.get(WID)
    stored.policy = "existing_readonly"
    readonly = gather_with(owner, reader, look=spy)
    assert readonly.read_look is not None and readonly.read_look() is found
    assert calls[-1][2] is False  # the memory part reads only in an ordinary vault

    stored.policy = "protected"
    protected = gather_with(owner, reader, look=spy)
    assert protected.read_look is not None and protected.read_look() is found
    assert calls[-1][2] is False

    def unreadable(_id: str) -> object:
        raise RuntimeError("store down")

    active: Any = owner.resolve_active()
    broken = cast(WorkspaceService, SimpleNamespace(resolve_active=lambda: active, get=unreadable))
    unknown = gather_with(broken, reader, look=spy)  # the policy read failed: None
    assert unknown.policy is None
    assert unknown.read_look is not None and unknown.read_look() is found
    assert calls[-1][2] is False

    failed = gather_with(
        workspaces(vault), cast(VaultService, BrokenReader()), look=spy
    )  # the shared scan failed: still a read, now without a scan
    assert failed.read_look is not None and failed.read_look() is found
    assert calls[-1] == (WID, None, True)
    assert len(calls) == 5

    assert gather_with(owner, reader).read_look is None  # no reader bound


@pytest.mark.parametrize("kind,chat_only", [("vault", True), ("code", False)])
def test_gather_leaves_no_vault_look_outside_a_vault_workspace(
    tmp_path: Path, kind: str, chat_only: bool
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    owner = workspaces(vault, kind=kind, chat_only=chat_only)

    def never(scope: str, scan: ScanResult | None, memory: bool) -> contracts.VaultLook:
        raise AssertionError("no Look read outside a vault workspace")

    obs = gather(
        owner, VaultService(owner), cast(DecisionService, FakeDecisions()), None, TODAY, look=never
    )
    assert (obs.look, obs.read_look) == (None, None)


# ---- gather: the vault's own skills, for a skill suggestion ----------------------------------


def skill_asset(
    name: str, description: str | None = "", *, kind: Literal["agent", "skill"] = "skill"
) -> NativeAsset:
    """A catalog entry as `NativeAssetInspector.inspect` lists one."""
    return NativeAsset(
        source_id=f"claude:workspace:{WID}",
        source_scope="workspace",
        provider="claude",
        relative_id=f"skills/{name}/SKILL.md" if kind == "skill" else f"agents/{name}.md",
        kind=kind,
        name=name,
        description=description,
        model=None,
        tools=(),
        digest="0" * 64,
        observed_at=stamp(NOW),
        supported_fields=("name", "description"),
        unsupported_fields=(),
    )


class FakeCatalog:
    """`NativeAssetInspector.inspect` reduced to the assets it lists, a partial scan or a
    failure; `calls` records each source id asked for."""

    def __init__(
        self, *assets: NativeAsset, partial: bool = False, error: Exception | None = None
    ) -> None:
        self.assets = assets
        self.partial = partial
        self.error = error
        self.calls: list[str] = []

    def __call__(self, source_id: str) -> NativeAssetCatalog:
        self.calls.append(source_id)
        if self.error is not None:
            raise self.error
        return NativeAssetCatalog(
            source_id=source_id,
            provider="claude",
            source_scope="workspace",
            assets=self.assets,
            partial=self.partial,
            partial_reasons=("deadline",) if self.partial else (),
            scanned_entries=len(self.assets),
            scanned_bytes=0,
            omitted=1 if self.partial else 0,
        )


def gather_skills(
    tmp_path: Path,
    catalog: FakeCatalog | None,
    facts: contracts.PageFacts | None,
    *,
    kind: str = "vault",
    chat_only: bool = False,
) -> Observation:
    """`gather` over an empty vault folder with `catalog` bound, as the server binds it."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    owner = workspaces(vault, kind=kind, chat_only=chat_only)
    decisions = cast(DecisionService, FakeDecisions())
    return gather(owner, VaultService(owner), decisions, facts, TODAY, catalog=catalog)


_SKILL_CODES = {"skills_unknown", "skills_partial"}


def test_gather_reads_the_vaults_own_skills_once_for_a_chat_to_suggest_in(tmp_path: Path) -> None:
    catalog = FakeCatalog(
        skill_asset("sprint-retro", "Run a sprint retro"),
        skill_asset("reviewer", "Reviews a change", kind="agent"),
        skill_asset("Sprint Retro"),
        skill_asset("sprint_retro"),
        skill_asset("-sprint"),
        skill_asset("sprint-"),
        skill_asset("sprint--retro"),
        skill_asset("振り返り"),
        skill_asset("x" * 65),
        skill_asset("a" * 64, None),
        skill_asset("inbox", "File the inbox"),
    )
    obs = gather_skills(tmp_path, catalog, page(suggest=_SUGGEST_HINT))
    assert catalog.calls == [f"claude:workspace:{WID}"]
    assert obs.skills == (
        improve.skill_words("sprint-retro", "Run a sprint retro"),
        improve.skill_words("a" * 64, ""),
        improve.skill_words("inbox", "File the inbox"),
    )
    assert not _SKILL_CODES & set(obs.failures)


@pytest.mark.parametrize(
    ("kind", "chat_only", "facts", "bound"),
    [
        pytest.param("code", False, page(suggest=_SUGGEST_HINT), True, id="folder"),
        pytest.param("vault", True, page(suggest=_SUGGEST_HINT), True, id="chat_only"),
        pytest.param("vault", False, None, True, id="no page"),
        pytest.param("vault", False, page(), True, id="no suggest"),
        pytest.param("vault", False, page(suggest=_SUGGEST_HINT), False, id="no catalog"),
    ],
)
def test_gather_reads_no_skills_unless_a_vault_chat_is_in_view(
    tmp_path: Path, kind: str, chat_only: bool, facts: contracts.PageFacts | None, bound: bool
) -> None:
    catalog = FakeCatalog(skill_asset("sprint-retro"))
    obs = gather_skills(tmp_path, catalog if bound else None, facts, kind=kind, chat_only=chat_only)
    assert catalog.calls == []
    assert obs.skills == ()
    assert not _SKILL_CODES & set(obs.failures)


def test_gather_reads_no_skills_when_the_workspace_cannot_be_resolved() -> None:
    catalog = FakeCatalog(skill_asset("sprint-retro"))
    obs = gather(
        cast(WorkspaceService, BrokenWorkspaces()),
        cast(VaultService, BrokenReader()),
        cast(DecisionService, FakeDecisions()),
        page(suggest=_SUGGEST_HINT),
        TODAY,
        catalog=catalog,
    )
    assert (catalog.calls, obs.skills, obs.failures) == ([], (), ("workspace_unknown",))


@pytest.mark.parametrize(
    "error",
    [
        NativeAssetError("source_unavailable"),
        NativeAssetError("source_changed"),
        OSError("unreadable"),
        RuntimeError("anything else"),
    ],
)
def test_gather_records_skills_unknown_when_the_catalog_fails(
    tmp_path: Path, error: Exception
) -> None:
    catalog = FakeCatalog(skill_asset("sprint-retro"), error=error)
    obs = gather_skills(tmp_path, catalog, page(suggest=_SUGGEST_HINT))
    assert catalog.calls == [f"claude:workspace:{WID}"]
    assert obs.skills == ()
    assert _SKILL_CODES & set(obs.failures) == {"skills_unknown"}
    assert obs.vault is not None  # the rest of the cycle still read


def test_gather_keeps_a_partial_catalogs_skills_and_says_so(tmp_path: Path) -> None:
    catalog = FakeCatalog(skill_asset("sprint-retro", "Run a sprint retro"), partial=True)
    obs = gather_skills(tmp_path, catalog, page(suggest=_SUGGEST_HINT))
    assert obs.skills == (improve.skill_words("sprint-retro", "Run a sprint retro"),)
    assert _SKILL_CODES & set(obs.failures) == {"skills_partial"}


POLICIES = ("ordinary", "existing_readonly", "protected", None)


def test_detects_changes_answers_detects_own_gate() -> None:
    """The engine reads the change rows only while `detect` could build a vault change:
    `detects_changes` holds exactly when one of the three types can come out, which is only in
    an ordinary vault."""
    for policy, improve_on, limit, vault_changes, tasks in itertools.product(
        POLICIES, (True, False), (0, 3), (True, False), (True, False)
    ):
        obs = replace(observation_with_improve_hints(), policy=policy)
        chosen = settings(
            **{
                "decision.watch.improve": improve_on,
                "decision.limit.changes": limit,
                "decision.watch.vault_changes": vault_changes,
                "decision.watch.tasks": tasks,
            }
        )
        built = {candidate.type for candidate in detect(obs, chosen, (), NOW)} & _CHANGE_TYPES
        assert detects_changes(chosen, policy) == bool(built), (policy, chosen)
        if policy != "ordinary":
            assert not built


def test_work_follows_the_vault_policy() -> None:
    """Every candidate's `work_allowed` is cut to what the vault's policy allows
    (`POLICY_WORK`): prepare and run in an ordinary vault, prepare alone in the protected one,
    none in a read-only vault or when the policy went unread; order and every other field are
    kept."""
    ordinary = detect(observation_with_improve_hints(), settings(), (), NOW)
    assert ("prepare", "run") in {candidate.work_allowed for candidate in ordinary}
    # The vault changes and the vault Look items come out only in an ordinary vault.
    ordinary_only = _CHANGE_TYPES | set(contracts.VAULT_LOOKS)
    kept = [replace(c, work_allowed=()) for c in ordinary if c.type not in ordinary_only]
    expected = {
        "existing_readonly": (),
        "protected": ("prepare",),
        None: (),
    }
    for policy, work in expected.items():
        obs = replace(observation_with_improve_hints(), policy=policy)
        found = detect(obs, settings(), (), NOW)
        assert [replace(c, work_allowed=()) for c in found] == kept, policy
        tasks_due = [c.work_allowed for c in found if c.type == "tasks_due"]
        assert tasks_due and set(tasks_due) == {work}, policy
        assert {c.work_allowed for c in found} <= {work, ()}, policy


def test_drafts_stay_in_every_vault_but_vault_changes_need_an_ordinary_one() -> None:
    """The drafts the user sends come out in every vault; the three vault-change
    types only in an ordinary one."""
    drafts = {"skill_offer", "skill_suggest", "learn_source"}
    for policy in ("ordinary", "existing_readonly", "protected"):
        obs = replace(observation_with_improve_hints(), policy=policy)
        types = {candidate.type for candidate in detect(obs, settings(), (), NOW)}
        assert drafts <= types, policy
        assert (types >= _CHANGE_TYPES) if policy == "ordinary" else not (types & _CHANGE_TYPES)


# ---- detect: tasks_due (the one topic that fans out) -----------------------------------------


def test_tasks_due_is_one_candidate_per_kept_task_sharing_the_aggregate_evidence() -> None:
    today_item = TaskItem(
        ref=TASKS_REF, line=1, text="Renew the passport", due=TODAY, tags=("alpha",)
    )
    overdue_item = TaskItem(
        ref=TASKS_REF, line=2, text="Call the dentist", due=TODAY - timedelta(days=1), tags=None
    )
    obs = tasks_observation(today_item, overdue_item)
    produced = detect(obs, settings(), (), NOW)
    assert [c.type for c in produced] == ["tasks_due", "tasks_due"]
    by_text = {c.title: c for c in produced}
    # Shared, aggregate rules_evidence (the judgement is topic-wide, like jev's).
    assert by_text["Renew the passport"].rules_evidence == {"overdue": 1, "today": 1, "soon": 0}
    assert (
        by_text["Call the dentist"].rules_evidence == by_text["Renew the passport"].rules_evidence
    )
    # Per-item facts/words/evidence/related_ref/deadline/work.
    today_candidate = by_text["Renew the passport"]
    assert today_candidate.facts["overdue"] is False
    assert today_candidate.words["due"] == "today"
    assert today_candidate.words["deadline"] == "today"
    assert "due" not in today_candidate.words["due"]
    assert today_candidate.related_ref == TASKS_REF
    assert today_candidate.deadline == datetime.combine(TODAY, time.max, tzinfo=NOW.tzinfo)
    assert today_candidate.work_allowed == ("prepare", "run")
    assert today_candidate.evidence == (
        contracts.Evidence(TASKS_REF, "Renew the passport", False, ("alpha",)),
    )
    overdue_candidate = by_text["Call the dentist"]
    assert overdue_candidate.facts["overdue"] is True


def test_tasks_due_item_key_depends_on_ref_and_text_and_is_stable() -> None:
    a = TaskItem(ref=TASKS_REF, line=1, text="Call the dentist", due=TODAY, tags=None)
    b = TaskItem(ref=TASKS_REF, line=2, text="Call the vet", due=TODAY, tags=None)
    obs = tasks_observation(a, b)
    first = detect(obs, settings(), (), NOW)
    second = detect(obs, settings(), (), NOW)
    assert [c.item_key for c in first] == [c.item_key for c in second]
    assert len({c.item_key for c in first}) == 2  # same ref, different text -> different key


def test_tasks_due_absent_without_kept_tasks_or_with_its_watch_off() -> None:
    assert detect(tasks_observation(), settings(), (), NOW) == []
    item = TaskItem(ref=TASKS_REF, line=1, text="Renew", due=TODAY, tags=None)
    obs = tasks_observation(item)
    assert detect(obs, settings(**{"decision.watch.tasks": False}), (), NOW) == []


def health_task(tmp_path: Path, tasks_head: str, overview: str | bytes) -> contracts.Candidate:
    """The one candidate `gather` and `detect` find in a vault holding only a task due today in
    10-projects/health/tasks.md (after `tasks_head`) and its overview (bytes are written raw)."""
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "health" / "tasks.md",
        f"{tasks_head}- [ ] Book the follow-up \U0001f4c5 {TODAY.isoformat()}\n",
    )
    overview_path = vault / "10-projects" / "health" / "health.md"
    if isinstance(overview, bytes):
        overview_path.write_bytes(overview)
    else:
        write(overview_path, overview)
    owner = workspaces(vault)
    obs = gather(owner, VaultService(owner), cast(DecisionService, FakeDecisions()), None, TODAY)
    [candidate] = detect(obs, settings(), (), NOW)
    return candidate


def test_a_tasks_files_own_tag_blocks_its_task_whatever_the_overview_says(
    tmp_path: Path,
) -> None:
    due = health_task(tmp_path, "---\ntags: [private]\n---\n", "---\ntags: [work]\n---\n# H\n")
    assert due.type == "tasks_due"
    assert due.evidence[0].tags == ("private", "work")
    blocked = built_state(due, "#private")
    assert (blocked.blocked, blocked.state) == (True, {})
    assert built_state(due, "#other").blocked is False


def test_unknown_task_tags_reach_the_evidence_and_a_tag_rule_blocks_the_task(
    tmp_path: Path,
) -> None:
    # The overview is not UTF-8, so it cannot be read: the task's tags are unknown (None, not ()).
    due = health_task(tmp_path, "---\ntags: [work]\n---\n", b"\xff\xfe not utf-8")
    assert due.type == "tasks_due"
    assert due.evidence[0].tags is None
    assert built_state(due).blocked is False
    assert built_state(due, "#private").blocked is True


# ---- detect: inbox (one aggregate candidate) --------------------------------------------------


def test_inbox_aggregates_with_the_oldest_kept_item_as_related_ref() -> None:
    obs = rich_observation()
    [candidate] = [c for c in detect(obs, settings(), (), NOW) if c.type == "inbox"]
    assert candidate.related_ref == "00-inbox/old.md"
    assert candidate.evidence == (
        contracts.Evidence("00-inbox/old.md", "old body", False, ("idea",)),
        contracts.Evidence("00-inbox/new.md", "new body", False, None),
    )
    # The evidence at `related_ref` carries its tags, so a tag never-send rule never blocks this
    # candidate outright.
    at_related_ref = [e for e in candidate.evidence if e.ref == candidate.related_ref]
    assert at_related_ref and at_related_ref[0].tags == ("idea",)
    assert candidate.words["age"]
    assert not any(ch.isdigit() for ch in candidate.words["age"])
    assert candidate.facts == {"workspace_id": WID, "partial": False}


def test_inbox_without_a_dated_item_has_no_related_ref_but_is_still_present() -> None:
    inbox = InboxFacts(count=3, oldest=None, checked=3, undated=3, partial=False)
    tasks = TaskFacts(overdue=0, today=0, soon=0, items=(), files=0, files_key="k", partial=False)
    obs = Observation(WID, "vault", VaultFacts(inbox=inbox, tasks=tasks), (), (), None, ())
    [candidate] = detect(obs, settings(), (), NOW)
    assert candidate.type == "inbox"
    assert candidate.related_ref is None
    assert candidate.evidence == ()
    assert "age" not in candidate.words


def test_inbox_absent_when_empty_or_watch_off() -> None:
    empty = Observation(
        WID,
        "vault",
        VaultFacts(
            inbox=InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=False),
            tasks=TaskFacts(
                overdue=0, today=0, soon=0, items=(), files=0, files_key="k", partial=False
            ),
        ),
        (),
        (),
        None,
        (),
    )
    assert detect(empty, settings(), (), NOW) == []
    obs = rich_observation()
    off = detect(obs, settings(**{"decision.watch.inbox": False}), (), NOW)
    assert "inbox" not in {c.type for c in off}


# ---- detect: the remaining aggregate topics ---------------------------------------------------


def test_uncommitted_needs_vault_and_a_known_dirty_count() -> None:
    present = detect(rich_observation(), settings(), (), NOW)
    [uncommitted] = [c for c in present if c.type == "uncommitted"]
    assert uncommitted.rules_evidence == {"dirty": 3}
    assert uncommitted.evidence == ()
    assert uncommitted.related_ref is None
    no_page = detect(rich_observation(page=None), settings(), (), NOW)
    assert "uncommitted" not in {c.type for c in no_page}
    unknown_git = detect(rich_observation(page=page(git_state="unknown")), settings(), (), NOW)
    assert "uncommitted" not in {c.type for c in unknown_git}
    clean = detect(rich_observation(page=page(git_state="known", git_dirty=0)), settings(), (), NOW)
    assert "uncommitted" not in {c.type for c in clean}


def test_agent_runs_facts_are_booleans_and_it_is_shown_on_the_runs_surface() -> None:
    [candidate] = [
        c for c in detect(rich_observation(), settings(), (), NOW) if c.type == "agent_runs"
    ]
    assert candidate.rules_evidence == {"working": 2, "review": 1}
    assert candidate.facts == {"workspace_id": WID, "working": True, "review": True}
    assert candidate.shown_elsewhere == ("runs",)
    no_page = detect(rich_observation(page=None), settings(), (), NOW)
    assert "agent_runs" not in {c.type for c in no_page}


@pytest.mark.parametrize(
    ("delta", "word"),
    [
        (timedelta(hours=1), "soon"),
        (rules.ROUTINE_SOON, "soon"),
        (rules.ROUTINE_SOON + timedelta(seconds=1), "later"),
    ],
)
def test_routine_word_matches_the_rules_own_threshold(delta: timedelta, word: str) -> None:
    obs = rich_observation(page=page(routine_id="r-1", next_run=NOW + delta, routines_paused=False))
    [candidate] = [c for c in detect(obs, settings(), (), NOW) if c.type == "routine"]
    assert candidate.words["next_run"] == word
    assert candidate.facts == {"workspace_id": WID, "routine_id": "r-1", "paused": False}


def test_routine_absent_without_a_routine_on_the_page() -> None:
    obs = rich_observation(page=page())
    assert "routine" not in {c.type for c in detect(obs, settings(), (), NOW)}


# ---- detect: self-improvement (skill_offer, learn_source; improve.py) --------------------


def test_detect_includes_skill_offer_from_the_page_hint() -> None:
    obs = rich_observation(page=replace(rich_page(), skill=_SKILL_HINT))
    [candidate] = [c for c in detect(obs, settings(), (), NOW) if c.type == "skill_offer"]
    assert candidate.title == "Chat A"
    assert candidate.related_chat == "s-1"
    assert candidate.evidence == (contracts.Evidence(None, "Chat B", False, ()),)


def test_detect_includes_learn_source_from_a_captured_link() -> None:
    obs = observation_with_improve_hints()
    [candidate] = [c for c in detect(obs, settings(), (), NOW) if c.type == "learn_source"]
    assert candidate.related_ref == "00-inbox/link.md"
    assert candidate.evidence == (
        contracts.Evidence("00-inbox/link.md", "https://example.com/a", False, None),
    )


def test_skill_offer_and_learn_source_need_a_vault_workspace() -> None:
    hinted = observation_with_improve_hints()
    on_types = {c.type for c in detect(hinted, settings(), (), NOW)}
    assert {"skill_offer", "learn_source"} <= on_types
    folder = replace(hinted, kind="folder")
    off_types = {c.type for c in detect(folder, settings(), (), NOW)}
    assert "skill_offer" not in off_types
    assert "learn_source" not in off_types


def test_watch_improve_off_removes_every_self_improvement_candidate() -> None:
    obs = observation_with_improve_hints()
    on_types = {c.type for c in detect(obs, settings(), (), NOW)}
    assert {"skill_offer", "skill_suggest", "learn_source"} <= on_types
    off_types = {
        c.type for c in detect(obs, settings(**{"decision.watch.improve": False}), (), NOW)
    }
    assert "skill_offer" not in off_types
    assert "learn_source" not in off_types
    # The three vault-change candidates sit under `watch_improve` too (on top of their own
    # extra switch), so this one switch also removes them; everything else is untouched.
    turned_off = {
        "skill_offer",
        "skill_suggest",
        "learn_source",
        "note_link",
        "note_superseded",
        "tasks_ticked",
    }
    assert turned_off & off_types == set()
    assert on_types - turned_off <= off_types


def test_detect_puts_skill_suggest_right_after_skill_offer() -> None:
    obs = observation_with_improve_hints()
    produced = detect(obs, settings(), (), NOW)
    types = [c.type for c in produced]
    assert types[types.index("skill_offer") + 1] == "skill_suggest"
    [candidate] = [c for c in produced if c.type == "skill_suggest"]
    assert candidate.title == "A skill for this chat"
    assert candidate.facts == {"session": "s-1", "skill": "sprint-retro"}
    assert candidate.related_chat == "s-1"
    folder = {c.type for c in detect(replace(obs, kind="folder"), settings(), (), NOW)}
    assert "skill_suggest" not in folder


def test_detect_tokenizes_only_the_title_not_the_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gather` already tokenizes each skill (`improve.skill_words`, in its worker thread);
    `detect`, which the engine calls directly on the event loop (engine.py:552), must only
    tokenize the chat's title through `pick_skill` -- never a skill's own name or description
    again there."""
    skills = (
        improve.skill_words("inbox", "File the inbox"),
        improve.skill_words("sprint-retro", "Run a sprint retro with the team"),
        improve.skill_words("weekly", "GTD weekly review"),
    )
    obs = rich_observation(page=replace(rich_page(), suggest=_SUGGEST_HINT), skills=skills)
    calls = 0
    original = improve.match_tokens

    def counting(text: str) -> frozenset[str]:
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(improve, "match_tokens", counting)
    detect(obs, settings(), (), NOW)
    assert calls == 1


_CHANGE_TYPES = {"note_link", "note_superseded", "tasks_ticked"}


def test_changes_need_their_switches() -> None:
    # The three vault-change candidates need `watch_improve` and `limit_changes > 0` like every
    # self-improvement type, plus their own extra switch -- `watch_vault_changes` for the note
    # pair, `watch_tasks` for ticked tasks -- so each can be turned off on its own without
    # silencing the other self-improvement types (mutant target: removing any one of these
    # gates lets its type through under the switch meant to turn it off).
    obs = observation_with_improve_hints()
    on_types = {c.type for c in detect(obs, settings(), (), NOW)}
    assert on_types >= _CHANGE_TYPES

    no_improve = {
        c.type for c in detect(obs, settings(**{"decision.watch.improve": False}), (), NOW)
    }
    assert not (_CHANGE_TYPES & no_improve)

    no_limit = {c.type for c in detect(obs, settings(**{"decision.limit.changes": 0}), (), NOW)}
    assert not (_CHANGE_TYPES & no_limit)
    # Turning the limit off never touches any other topic's presence.
    assert on_types - _CHANGE_TYPES <= no_limit

    no_vault_changes = {
        c.type for c in detect(obs, settings(**{"decision.watch.vault_changes": False}), (), NOW)
    }
    assert "note_link" not in no_vault_changes
    assert "note_superseded" not in no_vault_changes
    assert "tasks_ticked" in no_vault_changes  # gated by watch_tasks, not watch_vault_changes

    no_tasks = {c.type for c in detect(obs, settings(**{"decision.watch.tasks": False}), (), NOW)}
    assert "tasks_ticked" not in no_tasks
    assert {"note_link", "note_superseded"} <= no_tasks


def test_approvals_present_from_native_alone_or_from_pending_proposals_alone() -> None:
    chat_only_native = Observation(WID, "chat_only", None, (), (), page(native_waiting=1), ())
    [native_only] = detect(chat_only_native, settings(), (), NOW)
    assert native_only.type == "approvals"
    assert native_only.rules_evidence == {"native": 1, "proposals": 0}
    assert native_only.evidence == ()

    only_not_attention = Observation(
        WID, "vault", None, (), ({"ref": "x", "attention": False},), page(), ()
    )
    assert detect(only_not_attention, settings(), (), NOW) == []

    item = {"ref": "r", "title": "t", "attention": True, "origin": {"workspace_id": WID}}
    pending = Observation(WID, "vault", None, (), (item,), page(), ())
    [from_proposal] = detect(pending, settings(), (), NOW)
    assert from_proposal.rules_evidence == {"native": 0, "proposals": 1}
    assert from_proposal.evidence == (contracts.Evidence("r", "t", False, None),)
    assert from_proposal.shown_elsewhere == ("approvals",)


def test_approvals_evidence_is_the_active_workspaces_but_the_count_is_every_workspaces() -> None:
    # The inbox lists every workspace's vault proposals, and each evidence label opens its ref in
    # the active workspace: another vault's proposal, or one naming no workspace, is never one.
    approvals = (
        proposal("system/memory/user.md", "ws-2"),
        proposal("30-knowledge/mine.md", WID),
        proposal("30-knowledge/settled.md", WID, attention=False),
        {"kind": "vault_proposal", "attention": True, "title": "odd.md", "ref": "odd.md"},
    )
    obs = Observation(WID, "vault", None, (), approvals, page(), ())
    [candidate] = detect(obs, settings(), (), NOW)
    assert candidate.rules_evidence == {"native": 0, "proposals": 3}
    assert candidate.evidence == (
        contracts.Evidence("30-knowledge/mine.md", "30-knowledge/mine.md", False, None),
    )


def change_item(kind: str, ref: str, other: str) -> dict[str, object]:
    """An open change row of WID as the Approvals inbox lists it (`ChangeProposals.item`)."""
    at = stamp(NOW)
    record = ChangeRecord(
        proposal_id=str(uuid.uuid4()),
        scope=WID,
        kind=kind,
        ref=ref,
        other=other,
        archive=None,
        item_key="0" * 64,
        episode_id=str(uuid.uuid4()),
        state="open",
        revision="1" * 64,
        created_at=at,
        updated_at=at,
        approved_at=None,
        rejected_at=None,
        reason=None,
    )
    return ChangeProposals.item(record)


def test_a_change_rows_second_note_stays_out_under_never_send() -> None:
    """A change row's title names a second note its ref does not cover (B of a link, the
    newer note of a superseded pair), so a never-send folder on that note could not hold the
    title back. A change row's evidence carries its ref alone, folder-checked as every ref is;
    other rows keep their titles."""
    alpha, payroll = "30-knowledge/alpha planning.md", "people/payroll planning.md"
    approvals = (
        change_item("note_link", alpha, payroll),
        change_item("note_superseded", alpha, payroll),
        change_item("note_link", payroll, alpha),
        proposal("30-knowledge/mine.md", WID),
    )
    assert "payroll planning" in str(approvals[0]["title"])  # the title names B
    obs = Observation(WID, "vault", None, (), approvals, page(), ())
    [candidate] = detect(obs, settings(), (), NOW)
    excerpts = [entry.excerpt for entry in candidate.evidence]
    assert excerpts == ["", "", "", "30-knowledge/mine.md"]

    built = built_state(candidate, "people/")
    sent = state_text(built.state).lower()
    assert not built.blocked and built.held_back == 1  # the row whose own ref is under people/
    assert "payroll" not in sent
    assert alpha in sent and "30-knowledge/mine.md" in sent


# ---- detect: mission_report (unconditional; no watch switch) ----------------------------------


def test_mission_report_is_one_per_ended_mission_regardless_of_watch_settings() -> None:
    off = settings(
        **{
            f"decision.watch.{name}": False
            for name in ("tasks", "inbox", "vault_changes", "approvals", "chats", "routines")
        }
    )
    empty = Observation(WID, "vault", None, (), (), None, ())
    produced = detect(empty, off, (mission("m-1"), mission("m-2", state="failed")), NOW)
    assert [c.type for c in produced] == ["mission_report", "mission_report"]
    assert [c.item_key for c in produced] == [
        contracts.item_key("mission_report", "m-1"),
        contracts.item_key("mission_report", "m-2"),
    ]
    first, second = produced
    assert first.related_chat == "session-m-1"
    assert first.origin_session == "session-m-1"
    assert first.facts["state"] == "done"
    assert second.facts["state"] == "failed"
    assert first.work_allowed == ()


def test_a_mission_report_sends_fixed_words_never_the_missions_own_title() -> None:
    # A mission's title is its item's own (a task's text, for tasks_due), and the report has no
    # related_ref or evidence for never-send to judge, so that title must not ride along.
    secret = "Book the dentist follow-up with Dr Sato"
    empty = Observation(WID, "vault", None, (), (), None, ())
    [report] = detect(empty, settings(), (mission("m-1", title=secret),), NOW)
    assert report.title == words.type_words("mission_report")
    sent = built_state(report, "10-projects/health/")
    assert sent.blocked is False
    assert "dentist" not in state_text(sent.state)


def test_mission_report_carries_the_mission_reason_in_facts() -> None:
    # A prepare mission's first waiting plan card is reported still "running", reason "ready";
    # `templates._what` needs that reason, not just the state, to word it right (once dropped
    # here, a ready report read as a finished one).
    empty = Observation(WID, "vault", None, (), (), None, ())
    [ready] = detect(empty, settings(), (mission("m-1", state="running", reason="ready"),), NOW)
    assert ready.facts["state"] == "running"
    assert ready.facts["reason"] == "ready"
    [stopped] = detect(
        empty, settings(), (mission("m-2", state="stopped", reason="deadline"),), NOW
    )
    assert stopped.facts["state"] == "stopped"
    assert stopped.facts["reason"] == "deadline"


def test_a_ready_report_has_its_own_item_key_distinct_from_the_final_report() -> None:
    # The "ready" report and the mission's later, real end report must be two separate items,
    # never one overwriting the other (the user should still see both).
    empty = Observation(WID, "vault", None, (), (), None, ())
    [ready] = detect(empty, settings(), (mission("m-1", state="running", reason="ready"),), NOW)
    [done] = detect(empty, settings(), (mission("m-1", state="done"),), NOW)
    assert ready.item_key != done.item_key
    assert done.item_key == contracts.item_key("mission_report", "m-1")
    # Stable: asking again for the same still-waiting mission gives back the same key.
    [ready_again] = detect(
        empty, settings(), (mission("m-1", state="running", reason="ready"),), NOW
    )
    assert ready_again.item_key == ready.item_key


def test_a_nothing_report_adds_the_chat_title_to_facts() -> None:
    # Reason "nothing" (a second look that found nothing) names its chat in `facts["chat"]`,
    # the same blank-means-"your chat" convention `second_looks.finding` uses.
    empty = Observation(WID, "vault", None, (), (), None, ())
    [named] = detect(
        empty, settings(), (mission("m-1", reason="nothing", title="Garden plan"),), NOW
    )
    assert named.facts["reason"] == "nothing"
    assert named.facts["chat"] == "Garden plan"
    [blank] = detect(empty, settings(), (mission("m-2", reason="nothing", title=""),), NOW)
    assert blank.facts["chat"] == ""
    [untitled] = detect(
        empty,
        settings(),
        (mission("m-3", reason="nothing", title=contracts.UNTITLED_CHAT),),
        NOW,
    )
    assert untitled.facts["chat"] == ""
    # Any other reason gets no "chat" fact at all.
    [other] = detect(empty, settings(), (mission("m-4", reason="deadline"),), NOW)
    assert "chat" not in other.facts


# ---- detect: switches, chat-only, and ordering -------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "absent_type"),
    [
        ("decision.watch.tasks", "tasks_due"),
        ("decision.watch.inbox", "inbox"),
        ("decision.watch.vault_changes", "uncommitted"),
        ("decision.watch.approvals", "approvals"),
        ("decision.watch.chats", "agent_runs"),
        ("decision.watch.routines", "routine"),
    ],
)
def test_each_watch_switch_off_removes_only_its_own_candidates(flag: str, absent_type: str) -> None:
    obs = rich_observation()
    on_types = {c.type for c in detect(obs, settings(), (), NOW)}
    off_types = {c.type for c in detect(obs, settings(**{flag: False}), (), NOW)}
    assert absent_type in on_types
    assert absent_type not in off_types
    assert on_types - {absent_type} <= off_types


def test_a_chat_only_observation_yields_only_approvals_agent_runs_routine_and_mission_report() -> (
    None
):
    obs = Observation(
        scope="app",
        kind="chat_only",
        vault=None,
        goals=(),
        approvals=RICH_APPROVALS,
        page=rich_page(),
        failures=(),
    )
    produced = detect(obs, settings(), (mission("m-1"),), NOW)
    assert {c.type for c in produced} == {"approvals", "agent_runs", "routine", "mission_report"}


def test_detect_output_is_deterministic_and_follows_candidates_order() -> None:
    obs = observation_with_improve_hints()
    first = detect(obs, settings(), (mission("m-1"),), NOW)
    second = detect(obs, settings(), (mission("m-1"),), NOW)
    assert first == second
    assert {"skill_offer", "learn_source"} <= {c.type for c in first}
    order = [contracts.CANDIDATES.index(c.type) for c in first]
    assert order == sorted(order)


# ---- detect: Kanban cards ----------------------------------------------------


def kanban_observation(
    *cards: Card,
    unread: tuple[str, ...] = (),
    complete: bool = True,
    open_cards: Mapping[str, str] | None = None,
) -> Observation:
    return Observation(
        WID,
        "vault",
        None,
        (),
        (),
        None,
        (),
        kanban=KanbanRead(cards, unread, complete),
        open_cards=open_cards or {},
    )


def card_candidates(obs: Observation, **overrides: Any) -> list[contracts.Candidate]:
    return [c for c in detect(obs, settings(**overrides), (), NOW) if c.type in contracts.CARDS]


def card_key(card_type: str, name: str) -> str:
    return contracts.item_key(card_type, f"kanban/{name}.md")


def names(obs: Observation) -> list[str]:
    """The card names `detect` lists, in order."""
    return [
        cast(str, c.related_ref).removeprefix("kanban/").removesuffix(".md")
        for c in card_candidates(obs)
    ]


def test_cards_by_status() -> None:
    obs = kanban_observation(
        card("a", "blocked", tags=("x", "owner/", "Owner/b", "owner/Research", "owner/writer")),
        card("b", "review"),
        card("c", "working", age=timedelta(days=2)),
        card("d", "todo", age=timedelta(days=5), tags=("owner/research",)),
        card("e", "done", age=timedelta(days=9)),
        card("f", "Blocked"),
        card("g", "WORKING", age=timedelta(days=9)),
        card("h", None, age=timedelta(days=9)),
        card("i", "later", age=timedelta(days=9)),
        unread=("kanban/j.md",),
    )
    produced = card_candidates(obs)
    assert [(c.type, c.related_ref) for c in produced] == [
        ("card_needs_you", "kanban/a.md"),
        ("card_needs_you", "kanban/b.md"),
        ("card_stale", "kanban/c.md"),
        ("card_stale", "kanban/d.md"),
    ]
    assert produced[0] == contracts.Candidate(
        item_key=card_key("card_needs_you", "a"),
        type="card_needs_you",
        title="Card a",
        facts={"workspace_id": WID, "status": "blocked", "changed_at": NOW, "seat": "Research"},
        words={"age": "today", "status": "blocked"},
        evidence=(contracts.Evidence("kanban/a.md", "", False, None),),
        related_chat=None,
        related_ref="kanban/a.md",
        deadline=None,
        shown_elsewhere=("kanban",),
        rules_evidence={"status": "blocked"},
        origin_session=None,
        work_allowed=(),
    )
    assert [c.words["status"] for c in produced] == [
        "blocked",
        "ready for review",
        "in progress",
        "waiting",
    ]
    assert [c.facts.get("seat") for c in produced] == ["Research", None, None, "research"]
    assert [c.item_key for c in produced[2:]] == [
        card_key("card_stale", "c"),
        card_key("card_stale", "d"),
    ]
    assert produced[3].rules_evidence == {"status": "todo"}
    assert produced[3].facts["changed_at"] == NOW - timedelta(days=5)
    assert produced[3].words["age"] == words.age_words(NOW - timedelta(days=5), NOW)


def test_a_cards_age_is_the_owners_day_not_its_utc_date() -> None:
    """`age_words` gets the aware change instant, read on the user's wall clock."""
    tokyo = timezone(timedelta(hours=9))
    changed = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)  # 08:30 on the 21st in Tokyo
    obs = kanban_observation(Card("kanban/a.md", "A", "blocked", (), changed))
    [candidate] = [
        c
        for c in detect(obs, settings(), (), datetime(2026, 10, 21, 18, 0, tzinfo=tokyo))
        if c.type in contracts.CARDS
    ]
    assert candidate.words["age"] == "today"


@pytest.mark.parametrize(
    ("status", "threshold"), [("working", timedelta(days=1)), ("todo", timedelta(days=3))]
)
def test_stale_thresholds(status: str, threshold: timedelta) -> None:
    at = kanban_observation(card("a", status, age=threshold))
    before = kanban_observation(card("a", status, age=threshold - timedelta(seconds=1)))
    assert [c.type for c in card_candidates(at)] == ["card_stale"]
    assert card_candidates(before) == []


def test_selection_keeps_listed_cards() -> None:
    blocked = [card(f"{n:02}", age=timedelta(hours=1)) for n in range(1, 11)]
    assert names(kanban_observation(*blocked)) == ["01", "02", "03", "04", "05", "06"]

    listed = {card_key("card_needs_you", name): "card_needs_you" for name in ("07", "08")}
    assert names(kanban_observation(*blocked, open_cards=listed)) == [
        "07",
        "08",
        "01",
        "02",
        "03",
        "04",
    ]
    edited = [
        replace(one, title="Renamed", changed=NOW) if one.ref == "kanban/07.md" else one
        for one in blocked
    ]
    assert names(kanban_observation(*edited, open_cards=listed)) == [
        "07",
        "08",
        "01",
        "02",
        "03",
        "04",
    ]

    # 01 is listed but unread this cycle, and a new card sorts first: 01 keeps its place, so
    # five read cards are chosen beside it and the new card waits.
    shown = {card_key("card_needs_you", f"{n:02}"): "card_needs_you" for n in range(1, 7)}
    returning = [card("00"), *blocked[1:]]
    unread = ("kanban/01.md",)
    assert names(kanban_observation(*returning, unread=unread, open_cards=shown)) == [
        "02",
        "03",
        "04",
        "05",
        "06",
    ]
    # 02 is done: its place is free, and the new card takes it.
    freed = [card("00"), replace(blocked[1], status="done"), *blocked[2:]]
    assert names(kanban_observation(*freed, unread=unread, open_cards=shown)) == [
        "03",
        "04",
        "05",
        "06",
        "00",
    ]


def test_at_most_three_stale_cards_each_type_counting_only_its_own_kept_items() -> None:
    stale = [card(f"s{n}", "working", age=timedelta(days=2)) for n in range(5)]
    blocked = [card(f"b{n}") for n in range(8)]
    produced = card_candidates(kanban_observation(*stale, *blocked))
    assert [c.type for c in produced] == ["card_needs_you"] * 6 + ["card_stale"] * 3
    assert names(kanban_observation(*stale)) == ["s0", "s1", "s2"]

    kept = {card_key("card_stale", "s9"): "card_stale"}
    obs = kanban_observation(*stale, *blocked, unread=("kanban/s9.md",), open_cards=kept)
    assert [c.type for c in card_candidates(obs)] == ["card_needs_you"] * 6 + ["card_stale"] * 2


def test_kanban_keep() -> None:
    needs, stale = "card_needs_you", "card_stale"
    open_cards = {
        card_key(needs, "read"): needs,
        card_key(stale, "read"): stale,  # the same card, listed while it was stale
        card_key(needs, "unread"): needs,
        card_key(stale, "unread"): stale,
        card_key(needs, "gone"): needs,
    }
    read, unread = card("read", "done"), ("kanban/unread.md", "kanban/never-listed.md")
    assert (
        detectors.kanban_keep(replace(kanban_observation(), open_cards=open_cards)) == frozenset()
    )
    nothing = replace(kanban_observation(), kanban=None, open_cards=open_cards)
    assert detectors.kanban_keep(nothing) == frozenset()

    complete = kanban_observation(read, unread=unread, open_cards=open_cards)
    assert detectors.kanban_keep(complete) == {card_key(needs, "unread"), card_key(stale, "unread")}
    partial = kanban_observation(read, unread=unread, complete=False, open_cards=open_cards)
    assert detectors.kanban_keep(partial) == {
        card_key(needs, "unread"),
        card_key(stale, "unread"),
        card_key(needs, "gone"),
    }


def gather_kanban(
    vault: Path, monkeypatch: pytest.MonkeyPatch, kanban: bool
) -> tuple[Observation, int, list[list[str]], list[str]]:
    """`gather` over `vault` with `kanban`, and the number of scans, the Kanban refs of each
    `metadata` call and every Kanban ref fully read."""
    owner = workspaces(vault)
    reader = VaultService(owner)
    scans: list[str] = []
    metadata: list[list[str]] = []
    reads: list[str] = []
    scan, read_metadata, read = reader.scan, reader.metadata, reader.read

    def counting_scan(workspace_id: str, **kwargs: Any) -> ScanResult:
        scans.append(workspace_id)
        return scan(workspace_id, **kwargs)

    def counting_metadata(workspace_id: str, refs: list[str]) -> MetadataBatch:
        on_kanban = [ref for ref in refs if ref.startswith("kanban/")]
        if on_kanban:
            metadata.append(on_kanban)
        return read_metadata(workspace_id, refs)

    def counting_read(workspace_id: str, ref: str) -> Note:
        if ref.startswith("kanban/"):
            reads.append(ref)
        return read(workspace_id, ref)

    monkeypatch.setattr(reader, "scan", counting_scan)
    monkeypatch.setattr(reader, "metadata", counting_metadata)
    monkeypatch.setattr(reader, "read", counting_read)
    decisions = cast(DecisionService, FakeDecisions())
    obs = gather(owner, reader, decisions, None, TODAY, kanban)
    return obs, len(scans), metadata, reads


def test_gather_reads_kanban_on_the_shared_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for number in range(130):
        write(vault / "kanban" / f"n{number:03}.md", "---\nstatus: blocked\n---\n")
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        "---\ntype: project\nstatus: active\n---\n# Alpha\nOverview.\n",
    )
    obs, scans, metadata, reads = gather_kanban(vault, monkeypatch, True)
    _, scans_off, _, _ = gather_kanban(vault, monkeypatch, False)
    assert scans == scans_off
    assert [len(refs) for refs in metadata] == [32, 32, 32, 32]
    assert reads == []
    assert obs.kanban is not None
    assert [one.ref for one in obs.kanban.cards] == [f"kanban/n{n:03}.md" for n in range(128)]
    assert (obs.kanban.unread, obs.kanban.complete) == (("kanban/n128.md", "kanban/n129.md"), True)
    assert [goal.ref for goal in obs.goals] == ["10-projects/alpha/alpha.md"]
    assert "kanban_partial" in obs.failures
    assert "kanban_unknown" not in obs.failures


def test_an_unread_card_marks_a_complete_kanban_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(vault / "kanban" / "a.md", "---\nstatus: blocked\n---\n")
    write(vault / "kanban" / "b.md", "---\nstatus: todo\nstatus: todo\n---\n")
    obs, _, _, _ = gather_kanban(vault, monkeypatch, True)
    assert obs.kanban is not None
    assert (obs.kanban.unread, obs.kanban.complete) == (("kanban/b.md",), True)
    assert "kanban_partial" in obs.failures

    write(vault / "kanban" / "b.md", "---\nstatus: todo\n---\n")
    obs, _, _, _ = gather_kanban(vault, monkeypatch, True)
    assert not {"kanban_partial", "kanban_unknown"} & set(obs.failures)


def test_a_whole_listing_over_the_cap_closes_a_gone_card_and_keeps_one_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for number in range(129):  # n128 is listed, past the 128 notes read
        write(vault / "kanban" / f"n{number:03}.md", "---\nstatus: blocked\n---\n")
    obs, _, _, _ = gather_kanban(vault, monkeypatch, True)
    assert obs.kanban is not None
    assert (obs.kanban.unread, obs.kanban.complete) == (("kanban/n128.md",), True)
    needs = "card_needs_you"
    open_cards = {
        card_key(needs, "deleted"): needs,  # not listed: certainly gone
        card_key(needs, "n128"): needs,  # listed, past the cap: unknown
        card_key(needs, "n000"): needs,  # read and still qualifying
    }
    listed = replace(obs, open_cards=open_cards)
    assert detectors.kanban_keep(listed) == {card_key(needs, "n128")}
    assert "n000" in names(listed)


def test_kanban_off_reads_and_lists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(vault / "kanban" / "a.md", "---\nstatus: blocked\n---\n")
    obs, _, metadata, reads = gather_kanban(vault, monkeypatch, False)
    assert (obs.kanban, metadata, reads) == (None, [], [])
    assert not {"kanban_partial", "kanban_unknown"} & set(obs.failures)
    assert card_candidates(obs) == []

    on_kanban = kanban_observation(card("a"), card("b", "working", age=timedelta(days=9)))
    assert card_candidates(on_kanban, **{"decision.watch.kanban": False}) == []
    assert len(card_candidates(on_kanban)) == 2


def test_a_failed_shared_scan_keeps_every_card_item(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    reader = cast(VaultService, BrokenReader())
    obs = gather(
        workspaces(vault), reader, cast(DecisionService, FakeDecisions()), None, TODAY, True
    )
    assert obs.kanban == KanbanRead((), (), False)
    assert "kanban_unknown" in obs.failures
    assert "kanban_partial" not in obs.failures
    open_cards = {
        card_key("card_needs_you", "a"): "card_needs_you",
        card_key("card_stale", "b"): "card_stale",
    }
    listed = replace(obs, open_cards=open_cards)
    assert detectors.kanban_keep(listed) == frozenset(open_cards)
    assert card_candidates(listed) == []


# ---- the vault's Look items -------------------------------------------------------------------


def look_types(obs: Observation, **overrides: Any) -> list[str]:
    return [
        c.type for c in detect(obs, settings(**overrides), (), NOW) if c.type in contracts.LOOKS
    ]


def test_vault_looks_need_vault_health() -> None:
    obs = rich_observation(look=_VAULT_LOOK)
    assert look_types(obs) == ["vault_recovery", "memory_drift"]
    assert look_types(obs, **{"decision.watch.vault_health": False}) == []
    assert look_types(replace(obs, look=None)) == []  # the cycle did not read the Look
    assert look_types(replace(obs, policy="protected")) == []  # `looks` lists only ordinary vaults


def code_types(obs: Observation, **overrides: Any) -> list[contracts.Candidate]:
    return [c for c in detect(obs, settings(**overrides), (), NOW) if c.type == "code_look"]


def test_code_looks_need_the_look_grant_and_follow_the_policy() -> None:
    obs = replace(rich_observation(), code=_CODE_LOOK)
    for grant in ("ask", "allowed"):
        found = code_types(obs, **{"decision.grant.look": grant})
        assert [c.facts["folder_id"] for c in found] == ["code-1"], grant
        assert found[0].work_allowed == ("prepare",), grant
    assert code_types(obs, **{"decision.grant.look": "off"}) == []
    assert code_types(replace(obs, code=None)) == []  # the cycle did not read the folders
    # `detect` keeps the item under every policy; the cut takes only what the policy forbids.
    assert code_types(replace(obs, policy="protected"))[0].work_allowed == ("prepare",)
    assert code_types(replace(obs, policy="existing_readonly"))[0].work_allowed == ()
    assert code_types(replace(obs, policy=None))[0].work_allowed == ()


_STALLED_GOAL = Goal("10-projects/alpha/alpha.md", "Alpha", ("alpha",), NOW - timedelta(days=20))
_PLAN_TYPES = {"stalled_project", "overdue_tasks"}


def plan_types(obs: Observation, **overrides: Any) -> set[str]:
    return {c.type for c in detect(obs, settings(**overrides), (), NOW) if c.type in _PLAN_TYPES}


def test_project_plans_list_only_while_their_grant_policy_and_watch_allow() -> None:
    late = TaskItem(ref=TASKS_REF, line=1, text="Pay", due=TODAY - timedelta(days=5), tags=None)
    base = tasks_observation(late)
    obs = replace(base, goals=(_STALLED_GOAL,), policy="ordinary")
    assert plan_types(obs) == _PLAN_TYPES
    assert plan_types(obs, **{"decision.grant.project_plan": "off"}) == set()
    assert plan_types(replace(obs, policy="existing_readonly")) == set()
    assert plan_types(replace(obs, policy=None)) == set()
    assert plan_types(obs, **{"decision.watch.tasks": False}) == {"stalled_project"}
    assert plan_types(replace(obs, policy="protected")) == _PLAN_TYPES


_DRAFT_TYPES = {"context_handoff", "source_note"}


def draft_types(obs: Observation, **overrides: Any) -> set[str]:
    found = detect(obs, settings(**overrides), (), NOW)
    return {c.type for c in found if c.type in _DRAFT_TYPES}


def test_message_drafts_list_only_while_prepare_drafts_and_the_policy_allow() -> None:
    chat = str(uuid.uuid4())
    hints = replace(
        rich_page(),
        context=contracts.ContextHint(chat, 80),
        sources=contracts.SourceHint(chat, str(uuid.uuid4()), True),
    )
    obs = replace(rich_observation(), page=hints, policy="ordinary")
    assert draft_types(obs, **{"decision.grant.prepare": "off"}) == set()
    for grant in ("ask", "allowed"):
        assert draft_types(obs, **{"decision.grant.prepare": grant}) == _DRAFT_TYPES, grant
    assert draft_types(replace(obs, policy="protected")) == _DRAFT_TYPES
    assert draft_types(replace(obs, policy="existing_readonly")) == set()
    assert draft_types(replace(obs, policy=None)) == set()
    assert draft_types(replace(obs, kind="folder", policy=None)) == set()
    assert draft_types(replace(obs, kind="chat_only", policy=None)) == set()
    assert draft_types(obs, **{"decision.launcher_session": chat}) == set()


# ---- every produced candidate still renders (templates.py) --------------


def test_every_candidate_type_renders_through_templates_on_the_rules_path() -> None:
    # Every type except the two beat-cached self-improvement ones (retro_due, question_tune):
    # those come from `improve.from_rows` over learning rows, never from `detect`; the seat
    # types come from `seat_candidates` instead.
    # The app Look types come from `looks.app_candidates` over the app reader, which the cycle
    # awaits after `detect`; the second looks come from `second_looks.offer` and `finding`, which
    # the engine adds after `detect`.
    hints = observation_with_improve_hints()
    assert hints.vault is not None
    overdue = TaskItem(ref=TASKS_REF, line=9, text="Pay", due=TODAY - timedelta(days=5), tags=None)
    tasks = replace(hints.vault.tasks, items=(*hints.vault.tasks.items, overdue))
    assert hints.page is not None
    drafts_page = replace(
        hints.page,
        context=contracts.ContextHint(str(uuid.uuid4()), 80),
        sources=contracts.SourceHint(str(uuid.uuid4()), str(uuid.uuid4()), False),
    )
    produced = detect(
        replace(
            hints,
            page=drafts_page,
            policy="ordinary",
            vault=replace(hints.vault, tasks=tasks, inbox=replace(hints.vault.inbox, count=5)),
            inbox=contracts.InboxLook(None, {}, {}),
            inbox_listed=(("00-inbox/old.md", stamp(NOW - timedelta(days=1))),),
            code=_CODE_LOOK,
            checks=contracts.CheckLook(
                (contracts.CheckFolder("code-1", "app", "a" * 40, "waiting"),)
            ),
            goals=(_STALLED_GOAL,),
        ),
        settings(),
        (mission("m-1"),),
        NOW,
    )
    assert {c.type for c in produced} == (
        set(contracts.CANDIDATES)
        - {"retro_due", "question_tune"}
        - set(contracts.SEATS)
        - set(contracts.APP_LOOKS)
        - set(contracts.SECOND_LOOKS)
    )
    seat_types = {c.type for c in SEAT_ITEMS}
    assert seat_types == set(contracts.SEATS)
    app_items = looks.app_candidates(_APP_LOOK, WID, NOW)
    assert {c.type for c in app_items} == set(contracts.APP_LOOKS)
    second_items = (
        second_looks.offer(
            contracts.SecondLookHint(
                str(uuid.uuid4()), str(uuid.uuid4()), "long_session", "Garden plan", ("a.md",)
            ),
            WID,
        ),
        second_looks.finding(
            contracts.Finding("m-1", WID, str(uuid.uuid4()), "correction_look", "flaw", "Garden")
        ),
    )
    assert all(item is not None for item in second_items)
    plan_finding = project_plans.finding(
        contracts.Finding("m-2", WID, str(uuid.uuid4()), "stalled_project", "plan", "Alpha")
    )
    for candidate in (
        *produced,
        *SEAT_ITEMS,
        *app_items,
        *filter(None, second_items),
        plan_finding,
    ):
        verdict = rules.decide(candidate.type, candidate.rules_evidence, NOW, rules.Prefs())
        outcome = contracts.Outcome(
            level="list",
            timing="now",
            defer_until=None,
            work="none",
            kind=None,
            reason=f"rules:{verdict.reason}",
            terms={},
            blocked_by=(),
        )
        body = templates.render(candidate, outcome, {}, mission=None, grants={})
        assert body.what


# ---- seat items -------------------------------------------------------------------------------

SEAT_PLAN = SeatPlan(
    asks=(SeatAsk("routine-ask", "research", "att-1", "ses-1", True),),
    working=(SeatWorking("routine-work", "research", "att-2", "ses-2"),),
    offers=(SeatOffer("routine-offer", "research", False, "cards-hash"),),
    reports=(SeatReport("routine-rep", "research", "att-3", "ses-3", "failed"),),
    holds=(SeatHold("routine-hold", "research", "limit"),),
)
SEAT_ITEMS = detectors.seat_candidates(SEAT_PLAN, "ws-1", "2026-05-04")


def test_seat_candidates_build_each_type_from_the_plan() -> None:
    assert [c.type for c in SEAT_ITEMS] == [
        "seat_waiting",
        "seat_working",
        "seat_start",
        "seat_report",
        "seat_report",
    ]
    assert [c.item_key for c in SEAT_ITEMS] == [
        contracts.item_key("seat_waiting", "att-1"),
        contracts.item_key("seat_working", "att-2"),
        contracts.item_key("seat_start", "routine-offer", "cards-hash"),
        contracts.item_key("seat_report", "att-3"),
        contracts.item_key("seat_report", "routine-hold", "limit", "2026-05-04"),
    ]
    waiting, working, start, report, hold = SEAT_ITEMS
    assert waiting.facts == {
        "workspace_id": "ws-1",
        "routine_id": "routine-ask",
        "session_id": "ses-1",
        "seat": "research",
        "question": True,
    }
    assert working.facts["session_id"] == "ses-2"
    assert start.facts["handoff"] is False
    assert report.facts["reason"] == "failed" and report.facts["session_id"] == "ses-3"
    assert hold.facts["reason"] == "limit" and "session_id" not in hold.facts
    for item in SEAT_ITEMS:
        assert item.title == words.type_words(item.type)
        assert item.words == {} and item.evidence == () and item.rules_evidence == {}
        assert item.related_chat is None and item.related_ref is None
        assert item.deadline is None and item.work_allowed == ()
        assert item.shown_elsewhere == ("routines",)


def test_seat_candidates_send_no_seat_name_to_the_model() -> None:
    for item in SEAT_ITEMS:
        sent = canonical_json(built_state(item).state)
        assert "research" not in sent
        assert not any(rid in sent for rid in ("routine-ask", "routine-work", "routine-hold"))
