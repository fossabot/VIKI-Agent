"""Detect candidates (`gather` and `detect`): one cycle's read of the active workspace and
the page, turned into the `Candidate`s the arbiter and the Decision model reason about.

`gather`, with the vault-change read it leaves to run (`Observation.read_changes`), is the only
place in this module that touches I/O, and only through `reader`, `decisions` and `catalog` (each
already bounded the way `brief.py`, `DecisionService` and `NativeAssetInspector` bound them),
besides `workspaces` for the active workspace and its policy row (`WorkspaceService.get`); it
never raises; a part it could not read narrows the result through `Observation.failures` instead.

`detect` is pure: no I/O, no clock read (`now` is passed in, the user's own local time). It ports
the earlier engine's evidence shapes into `Candidate.rules_evidence`, so `rules.decide` reaches
the same verdict for a topic that the earlier engine would have reached today; `tasks_due`
is the one topic that fans out, one `Candidate` per kept task, since each is its own actionable
thing with its own note and due date -- three of `improve.py`'s vault-change builders (note_link,
note_superseded, tasks_ticked) fan out the same way, one `Candidate` per pair or per ticked file
in `obs.changes`; the Kanban cards fan out too, one `Candidate` per chosen card. `Candidate.facts`/
`Candidate.words` instead feed `templates.render`: the exact keys it reads are documented on each
helper below; no value in `words` (or the `facts` values other than "workspace_id",
"routine_id", a card's "changed_at" and its "seat", the user's own word) ever carries a digit.
`seat_candidates` builds the routine-seat items from a `SeatPlan`, outside `detect`.
The vault's policy (`Observation.policy`) only narrows what `detect` answers: vault changes come
out only in an ordinary vault, and every candidate's `work_allowed` is cut to `POLICY_WORK`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from viki_agent.automation.brief import Goal, VaultFacts, read_goals, read_vault_facts
from viki_agent.automation.kanban import Card, KanbanRead, read_kanban
from viki_agent.automation.routines import parse_stamp
from viki_agent.decision_loop import (
    contracts,
    improve,
    inbox_triage,
    looks,
    message_drafts,
    project_plans,
    rules,
    saved_checks,
    words,
)
from viki_agent.decision_loop.seats import SeatPlan
from viki_agent.decision_loop.vault_watch import VaultChanges, read_changes
from viki_agent.domain import Workspace
from viki_agent.profiles.native_assets import SKILL_NAME, NativeAssetCatalog
from viki_agent.vaults import captures
from viki_agent.vaults.decisions import DecisionService
from viki_agent.vaults.vault import ScanResult, VaultError, VaultService
from viki_agent.workspaces import WorkspaceService

if TYPE_CHECKING:
    # Only for the type hint, as `vault_watch` holds it.
    from viki_agent.decision_loop.changes import Held

_ROUTINE_SOON_WORD = "soon"
_ROUTINE_LATER_WORD = "later"
_SKILL_NAME_CHARS = 64  # the longest skill name `native_assets` admits with `SKILL_NAME`
# How long a card in each stale status sits unchanged before it reads stale.
_STALE_AFTER: Mapping[str, timedelta] = {
    "working": contracts.CARD_STALE_WORKING,
    "todo": contracts.CARD_STALE_TODO,
}
# A card's status as the user reads it (`words["status"]`).
_STATUS_WORDS: Mapping[str, str] = {
    "blocked": "blocked",
    "review": "ready for review",
    "working": "in progress",
    "todo": "waiting",
}
_SEAT_PREFIX = "owner/"
_CARD_CAPS = (
    ("card_needs_you", contracts.MAX_CARDS_NEED_YOU),
    ("card_stale", contracts.MAX_CARDS_STALE),
)
# The work VIKI may start or offer in a vault of each policy (ADR-051's amendment): plans
# alone in the protected vault, none in a read-only one or when the policy is unknown.
POLICY_WORK: Mapping[str, tuple[str, ...]] = {
    "ordinary": ("prepare", "run"),
    "protected": ("prepare",),
}


@dataclass(frozen=True)
class Observation:
    """One cycle's inputs, read once. `scope` is the active workspace id ("app" when even that
    could not be resolved); `kind` is "vault", "folder" or "chat_only" (`ActiveWorkspace.kind`
    "code" reads as "folder"). `vault` and `goals` are set only for a vault workspace that is not
    chat-only; `approvals` holds the decision inbox's raw items (then too, else empty); `page` is
    the client's last presence report, None before the first one arrives. `failures` names, in no
    particular order, the parts `gather` could not read; it is empty when everything was read.
    `changes` is the `vault_watch.read_changes` result once that read has run. `gather` does not
    run it: for a vault cycle whose shared scan succeeded it leaves `read_changes`, that read over
    the scan and the ticked tasks, to run given what the scope's change rows hold back
    (`changes.Held`; None holds nothing back), so the engine can read the rows first. Both
    are None otherwise, including outside a vault workspace. `skills` is the vault's own skills
    for skill_suggest, each already `improve.skill_words`'d (name, its own and its description's
    tokens) from the catalog's "skill" entries whose name fits `native_assets.SKILL_NAME` in at
    most 64 characters, in the catalog's order -- tokenized here, in this worker thread, so
    `pick_skill` never does on the event loop `detect` runs on. `gather` reads them only for
    a vault cycle whose page reports `suggest`; they are empty otherwise. `kanban` is
    Kanban as `read_kanban` read it over the shared scan, `KanbanRead((), (), False)` when that
    scan failed, and None when `gather` was not asked to read it or outside a vault workspace.
    `open_cards` maps each open Kanban card item's key to its type (one of `contracts.CARDS`);
    `gather` leaves it empty and its caller fills it, for `kanban_keep` and the card selection.
    `policy` is the vault's policy as its registration holds it at this cycle
    (`WorkspaceService.get`; an unstored `VIKI_VAULT` vault keeps its own), None outside a vault
    workspace or when it could not be read (no vault change, no work); `gather` sets it on every
    path, and the default serves hand-built observations only. `look` is the vault's Look read
    (`VaultLookReader.read`) once that read has run. `gather` does not run it: for a vault cycle
    given a `look` reader it leaves `read_look`, that read over the shared scan (None when the scan
    failed) and whether the vault is ordinary, for the engine to run while Vault health is on.
    Both are None otherwise. `code` is the engine's code-folder read (`CodeHost.read`); `gather`
    leaves it None. `checks` is the engine's saved-check read (`CheckHost.read`); `gather` leaves
    it None. `inbox_listed` is the vault's `00-inbox` captures as (ref, modified stamp) pairs, the
    population and stamps `CaptureService.list` shows: `gather` takes them from the shared scan
    (no further read) once it succeeded, and leaves them empty otherwise. `inbox` is the engine's
    inbox-host read (`InboxHost.read`); `gather` leaves it None."""

    scope: str
    kind: str
    vault: VaultFacts | None
    goals: tuple[Goal, ...]
    approvals: tuple[Mapping[str, Any], ...]
    page: contracts.PageFacts | None
    failures: tuple[str, ...]
    changes: VaultChanges | None = None
    read_changes: Callable[[Held | None], VaultChanges] | None = field(
        default=None, compare=False, repr=False
    )
    skills: tuple[improve.SkillWords, ...] = ()
    kanban: KanbanRead | None = None
    open_cards: Mapping[str, str] = field(default_factory=dict)
    policy: str | None = "ordinary"
    look: contracts.VaultLook | None = None
    read_look: Callable[[], contracts.VaultLook] | None = field(
        default=None, compare=False, repr=False
    )
    code: contracts.CodeLook | None = None
    checks: contracts.CheckLook | None = None
    inbox_listed: tuple[tuple[str, str], ...] = ()
    inbox: contracts.InboxLook | None = None


def gather(
    workspaces: WorkspaceService,
    reader: VaultService,
    decisions: DecisionService,
    page: contracts.PageFacts | None,
    today: date,
    kanban: bool = False,
    *,
    catalog: Callable[[str], NativeAssetCatalog] | None = None,
    look: Callable[[str, ScanResult | None, bool], contracts.VaultLook] | None = None,
) -> Observation:
    """This cycle's `Observation`. Blocking reads go only through `reader` (`read_vault_facts`,
    then one `reader.scan` shared by `read_goals` and the vault-change read it leaves to run,
    `Observation.read_changes`) and `decisions.inbox`, for a vault workspace that is not
    chat-only (the earlier engine's own gate); a "folder" or chat-only workspace gets neither,
    same as the earlier engine. `catalog` (`NativeAssetInspector.inspect`, None without one) is
    read once, for the vault's own skills (`Observation.skills`, each tokenized here through
    `improve.skill_words`), only in such a vault cycle whose `page` reports `suggest` (a
    vault chat in view whose last turn finished). Never raises: a failure anywhere is recorded
    in `failures` and leaves that part empty instead -- a failed shared scan leaves `goals`
    empty and no vault-change read, so it appends both
    "goals_unknown" and "changes_unknown"; a catalog that raises leaves no skills and appends
    "skills_unknown", and a partial one keeps the skills it read and appends
    "skills_partial". With `kanban`, the same vault cycle reads Kanban over the shared
    scan (`read_kanban`: at most four bounded `metadata` calls, no full read) and appends
    "kanban_partial" when that listing is incomplete or a card went unread; a failed shared scan
    leaves `KanbanRead((), (), False)` and appends "kanban_unknown". A vault cycle reads the
    vault's policy first (`_policy`) and appends "policy_unknown" when it cannot. `look`
    (`VaultLookReader.read`) is never called here: a vault cycle given one leaves
    `Observation.read_look`, that read over the shared scan (None when it failed) and whether the
    policy is "ordinary" (the memory part), for the cycle to run in a worker thread. A vault
    cycle whose shared scan succeeded also lists the inbox from it (`Observation.inbox_listed`),
    which costs no read."""
    try:
        active = workspaces.resolve_active()
        scope = active.workspace.workspace_id
        if active.chat_only:
            kind = "chat_only"
        else:
            kind = "vault" if active.workspace.kind == "vault" else "folder"
    except Exception:  # the active workspace itself could not be resolved
        return Observation(
            "app", "chat_only", None, (), (), page, ("workspace_unknown",), policy=None
        )

    vault: VaultFacts | None = None
    goals: tuple[Goal, ...] = ()
    approvals: tuple[Mapping[str, Any], ...] = ()
    pending: Callable[[Held | None], VaultChanges] | None = None
    skills: tuple[improve.SkillWords, ...] = ()
    kanban_read: KanbanRead | None = None
    policy: str | None = None
    pending_look: Callable[[], contracts.VaultLook] | None = None
    inbox_listed: tuple[tuple[str, str], ...] = ()
    failures: list[str] = []
    if kind == "vault":
        policy = _policy(workspaces, active.workspace)
        if policy is None:
            failures.append("policy_unknown")
        vault = read_vault_facts(reader, scope, today)  # never raises itself
        if vault.tasks.partial:
            failures.append("tasks_partial")
        if vault.inbox.partial:
            failures.append("inbox_partial")
        scan: ScanResult | None = None
        try:
            scan = reader.scan(scope)
        except VaultError:
            failures.append("goals_unknown")
            failures.append("changes_unknown")
            if kanban:
                kanban_read = KanbanRead((), (), False)
                failures.append("kanban_unknown")
        else:
            inbox_listed = _inbox_listing(scan)
            goals = read_goals(reader, scope, scan)
            ticked = vault.tasks.ticked
            pending = functools.partial(read_changes, reader, scope, scan, today, ticked)
            if kanban:
                kanban_read = read_kanban(reader, scope, scan)  # never raises itself
                if not kanban_read.complete or kanban_read.unread:
                    failures.append("kanban_partial")
        if look is not None:
            pending_look = functools.partial(look, scope, scan, policy == "ordinary")
        try:
            approvals = tuple(decisions.inbox()["items"])
        except Exception:  # any failure leaves the count unknown, never the cycle failed
            failures.append("proposals_unknown")
        if catalog is not None and page is not None and page.suggest is not None:
            try:
                found = catalog(f"claude:workspace:{scope}")
                skills = tuple(
                    improve.skill_words(asset.name, asset.description or "")
                    for asset in found.assets
                    if asset.kind == "skill" and _is_skill_name(asset.name)
                )
            except Exception:  # a refused or changed source, anything: no skills this cycle
                failures.append("skills_unknown")
            else:
                if found.partial:
                    failures.append("skills_partial")
    return Observation(
        scope,
        kind,
        vault,
        goals,
        approvals,
        page,
        tuple(failures),
        None,
        pending,
        skills,
        kanban_read,
        policy=policy,
        read_look=pending_look,
        inbox_listed=inbox_listed,
    )


def _inbox_listing(scan: ScanResult) -> tuple[tuple[str, str], ...]:
    """The first `captures.MAX_CAPTURES` `00-inbox` entries of `scan` by ref casefold, each with
    its modified stamp: the same entries and stamps `CaptureService.list` lists, so a suggestion
    answered at a stamp is current exactly when the capture shows it."""
    inbox = sorted(
        (entry for entry in scan.entries if entry.ref.split("/")[0].casefold() == captures._INBOX),
        key=lambda entry: entry.ref.casefold(),
    )
    return tuple(
        (entry.ref, captures._modified_stamp(entry.modified))
        for entry in inbox[: captures.MAX_CAPTURES]
    )


def _policy(workspaces: WorkspaceService, workspace: Workspace) -> str | None:
    """The vault's policy as its registration holds it now, read again each cycle so a change
    made mid-launch applies; an unstored workspace (`VIKI_VAULT`) has no row and keeps its own.
    None when the row cannot be read: the strictest reading."""
    if not workspace.persistent:
        return workspace.policy
    try:
        return workspaces.get(workspace.workspace_id).policy
    except Exception:  # any failure reads as no policy: no vault change, no work
        return None


def detects_changes(settings: contracts.DecisionSettings, policy: str | None) -> bool:
    """Whether `detect` builds vault changes (`contracts.CHANGES`) under `settings` in a vault
    of `policy`: only in an ordinary vault, while `decision.watch.improve` is on and
    `limit_changes` is above 0, the note pairs under `watch_vault_changes` and ticked tasks under
    `watch_tasks`. Pure."""
    return policy == "ordinary" and (
        settings.watch_improve
        and settings.limit_changes > 0
        and (settings.watch_vault_changes or settings.watch_tasks)
    )


def detect(
    obs: Observation,
    settings: contracts.DecisionSettings,
    missions_ended: Sequence[contracts.MissionRecord],
    now: datetime,
) -> list[contracts.Candidate]:
    """Today's candidates, in `contracts.CANDIDATES` order: the six watch-gated topics, each only
    when its `decision.watch.*` switch is on (and, for `tasks_due`/`inbox`/`uncommitted`, only
    with vault facts to read), plus one `mission_report` per entry of `missions_ended`, in the
    order given - unconditional, since no `decision.watch.*` switch names missions yet - then,
    while `decision.watch.kanban` is on and Kanban was read, the chosen Kanban cards (`_cards`),
    then, while `decision.watch.vault_health` is on and the vault's Look was read, its items
    (`looks.vault_candidates`, ordinary vaults only), then, while `decision.grant.look` is not
    off and the code folders were read (`obs.code`), the `code_look` items
    (`looks.code_candidates`), then, while `decision.grant.project_plan` is not off and the
    policy's work includes prepare (`POLICY_WORK`; none for a None policy), the project plans:
    `project_plans.stalled_projects`, then, while `decision.watch.tasks` is on and there are vault
    facts, `project_plans.overdue_tasks`, then, while `decision.grant.prepare` is not off and the
    policy's work includes prepare, the message drafts (`message_drafts.offers`), then, while
    `decision.grant.check` is not off and the saved checks were read (`obs.checks`), the
    `commit_checks` items (`saved_checks.candidates`), then, while `decision.grant.inbox_triage` is
    not off, `decision.watch.inbox` is on, there are vault facts, the inbox host was read
    (`obs.inbox`) and the policy's work includes prepare, the `inbox_triage` item
    (`inbox_triage.candidates`), then, while `decision.watch.improve` is on,
    `improve.skill_offer`, `improve.skill_suggest` and `improve.learn_source`
    (`contracts.IMPROVE`'s own order; its other two beat-cached members, retro_due and
    question_tune, come from `improve.from_rows` instead, never gathered per request). Still
    under `watch_improve`, and only while `settings.limit_changes > 0` (no point asking
    when nothing could be filed) and only in an ordinary vault (`detects_changes`),
    `improve.note_link` and `improve.note_superseded` run under
    `watch_vault_changes` and `improve.tasks_ticked` under `watch_tasks`. Every candidate's
    `work_allowed` is then cut to what `obs.policy` allows (`POLICY_WORK`; none for another
    policy or None), in the same order with every other field kept. Pure: no I/O, no clock
    read; `improve.skill_suggest` only tokenizes the chat's title, intersecting it with the
    skills' own tokens `gather` already built."""
    candidates: list[contracts.Candidate] = []
    if settings.watch_approvals:
        approvals = _approvals(obs)
        if approvals is not None:
            candidates.append(approvals)
    if settings.watch_tasks and obs.vault is not None:
        candidates.extend(_tasks_due(obs, now))
    if settings.watch_chats:
        agent_runs = _agent_runs(obs)
        if agent_runs is not None:
            candidates.append(agent_runs)
    if settings.watch_inbox and obs.vault is not None:
        inbox = _inbox(obs, now)
        if inbox is not None:
            candidates.append(inbox)
    if settings.watch_vault_changes and obs.vault is not None:
        uncommitted = _uncommitted(obs)
        if uncommitted is not None:
            candidates.append(uncommitted)
    if settings.watch_routines:
        routine = _routine(obs, now)
        if routine is not None:
            candidates.append(routine)
    candidates.extend(_mission_report(mission, obs.scope) for mission in missions_ended)
    if settings.watch_kanban and obs.kanban is not None:
        candidates.extend(_cards(obs, now))
    if settings.watch_vault_health and obs.look is not None:
        candidates.extend(looks.vault_candidates(obs.look, obs.scope, obs.policy, now))
    if settings.grant_look != "off" and obs.code is not None:
        candidates.extend(looks.code_candidates(obs.code, obs.scope, now))
    if settings.grant_project_plan != "off" and "prepare" in POLICY_WORK.get(obs.policy or "", ()):
        candidates.extend(project_plans.stalled_projects(obs.goals, obs.scope, now, settings))
        if settings.watch_tasks and obs.vault is not None:
            candidates.extend(
                project_plans.overdue_tasks(obs.vault.tasks, obs.scope, now.date(), settings)
            )
    if settings.grant_prepare != "off" and "prepare" in POLICY_WORK.get(obs.policy or "", ()):
        candidates.extend(message_drafts.offers(obs.page, obs.scope, settings.launcher_session))
    if settings.grant_check != "off" and obs.checks is not None:
        candidates.extend(saved_checks.candidates(obs.checks, obs.scope))
    if (
        settings.grant_inbox_triage != "off"
        and settings.watch_inbox
        and obs.vault is not None
        and obs.inbox is not None
        and "prepare" in POLICY_WORK.get(obs.policy or "", ())
    ):
        candidates.extend(
            inbox_triage.candidates(
                obs.inbox, obs.inbox_listed, obs.vault.inbox, obs.scope, now, settings
            )
        )
    if settings.watch_improve:
        skill = improve.skill_offer(obs)
        if skill is not None:
            candidates.append(skill)
        suggestion = improve.skill_suggest(obs)
        if suggestion is not None:
            candidates.append(suggestion)
        learn = improve.learn_source(obs)
        if learn is not None:
            candidates.append(learn)
        if detects_changes(settings, obs.policy):
            if settings.watch_vault_changes:
                candidates.extend(improve.note_link(obs))
                candidates.extend(improve.note_superseded(obs))
            if settings.watch_tasks:
                candidates.extend(improve.tasks_ticked(obs))
    allowed = POLICY_WORK.get(obs.policy, ()) if obs.policy is not None else ()
    return [
        replace(c, work_allowed=tuple(w for w in c.work_allowed if w in allowed))
        for c in candidates
    ]


def kanban_keep(obs: Observation) -> frozenset[str]:
    """The open card item keys (`obs.open_cards`) this cycle must keep as they are, since it
    cannot tell whether their card still qualifies: with no Kanban read, none (the cycle does not
    look at cards); with a whole listing, the keys of the listed cards it did not read, including
    those past the read cap, since a card the whole listing lacks is gone; with a partial one,
    every key but those of the cards it read, since a card it did not list may still be there.
    Pure."""
    kanban = obs.kanban
    if kanban is None:
        return frozenset()
    if kanban.complete:
        unread = {contracts.item_key(t, ref) for ref in kanban.unread for t in contracts.CARDS}
        return frozenset(key for key in obs.open_cards if key in unread)
    read = {contracts.item_key(t, card.ref) for card in kanban.cards for t in contracts.CARDS}
    return frozenset(key for key in obs.open_cards if key not in read)


# ---- one candidate per topic ---------------------------------------------------------------------


def _approvals(obs: Observation) -> contracts.Candidate | None:
    """`rules_evidence` counts every workspace's pending proposals, as the inbox lists them;
    `evidence` holds only those whose `origin.workspace_id` is `obs.scope`, since each label
    opens its ref in that workspace: each one's ref, and its title as the excerpt, but none for
    a vault-change proposal (kind "change_proposal"). `facts`: "workspace_id" only;
    `templates._what` always uses one fixed sentence."""
    native = obs.page.native_waiting if obs.page is not None else 0
    pending = [item for item in obs.approvals if item.get("attention") is True]
    rules_evidence: Mapping[str, object] = {"native": native, "proposals": len(pending)}
    if not rules.present("approvals", rules_evidence):
        return None
    evidence = tuple(
        contracts.Evidence(
            ref=_str_or_none(item.get("ref")),
            # A change row's title names a second note its ref does not cover, out of reach of
            # a never-send folder: its ref alone goes.
            excerpt=""
            if item.get("kind") == "change_proposal"
            else _str_or_none(item.get("title")) or "",
            outside=False,
            tags=None,
        )
        for item in pending
        if _origin_workspace(item) == obs.scope
    )
    title = words.type_words("approvals")
    return contracts.Candidate(
        item_key=contracts.item_key("approvals", obs.scope),
        type="approvals",
        title=title,
        facts={"workspace_id": obs.scope},
        words={},
        evidence=evidence,
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=("approvals",),
        rules_evidence=rules_evidence,
        origin_session=None,
        work_allowed=(),
    )


def _tasks_due(obs: Observation, now: datetime) -> list[contracts.Candidate]:
    """One `Candidate` per kept task, all sharing the same aggregate `rules_evidence` (the
    judgement is topic-wide, same as the earlier engine). `facts`: "workspace_id", "overdue"
    (this task's own, not the aggregate); `words`: "due" and "deadline" (the same word:
    `due_words` without its "due " prefix, since `templates._what`/`_why` both prepend their own
    "due"/"is due ")."""
    assert obs.vault is not None
    tasks = obs.vault.tasks
    rules_evidence: Mapping[str, object] = {
        "overdue": tasks.overdue,
        "today": tasks.today,
        "soon": tasks.soon,
    }
    if not rules.present("tasks_due", rules_evidence):
        return []
    today = now.date()
    built: list[contracts.Candidate] = []
    for item in tasks.items:
        due_word = words.due_words(item.due, now).removeprefix("due ")
        deadline = datetime.combine(item.due, time.max, tzinfo=now.tzinfo)
        evidence = (contracts.Evidence(item.ref, item.text, False, item.tags),)
        built.append(
            contracts.Candidate(
                item_key=contracts.item_key("tasks_due", item.ref, item.text),
                type="tasks_due",
                title=item.text,
                facts={"workspace_id": obs.scope, "overdue": item.due < today},
                words={"due": due_word, "deadline": due_word},
                evidence=evidence,
                related_chat=None,
                related_ref=item.ref,
                deadline=deadline,
                shown_elsewhere=(),
                rules_evidence=rules_evidence,
                origin_session=None,
                work_allowed=("prepare", "run"),
            )
        )
    return built


def _agent_runs(obs: Observation) -> contracts.Candidate | None:
    """`facts`: "workspace_id", "working", "review" (booleans; the counts live in
    `rules_evidence` for `rules.decide`). Already shown wherever the page counts runs."""
    page = obs.page
    if page is None:
        return None
    rules_evidence: Mapping[str, object] = {
        "working": page.runs_working,
        "review": page.runs_review,
    }
    if not rules.present("agent_runs", rules_evidence):
        return None
    title = words.type_words("agent_runs")
    return contracts.Candidate(
        item_key=contracts.item_key("agent_runs", obs.scope),
        type="agent_runs",
        title=title,
        facts={
            "workspace_id": obs.scope,
            "working": page.runs_working > 0,
            "review": page.runs_review > 0,
        },
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=("runs",),
        rules_evidence=rules_evidence,
        origin_session=None,
        work_allowed=(),
    )


def _inbox(obs: Observation, now: datetime) -> contracts.Candidate | None:
    """One aggregate `Candidate` (its evidence holds every kept item); `related_ref` is the
    oldest kept item's ref, so a tag never-send rule reads that item's own tags rather
    than blocking the topic outright. `facts`: "workspace_id", "partial"; `words`: "age" (the
    oldest kept item), left out when no item could be dated."""
    assert obs.vault is not None
    inbox = obs.vault.inbox
    oldest = None if inbox.oldest is None else parse_stamp(inbox.oldest)
    rules_evidence: Mapping[str, object] = {"count": inbox.count, "oldest": oldest}
    if not rules.present("inbox", rules_evidence):
        return None
    evidence = tuple(
        contracts.Evidence(item.ref, item.excerpt, False, item.tags) for item in inbox.items
    )
    related_ref = inbox.items[0].ref if inbox.items else None
    key = contracts.item_key("inbox", related_ref if related_ref is not None else obs.scope)
    title = words.type_words("inbox")
    word_map = {"age": words.age_words(oldest, now)} if oldest is not None else {}
    return contracts.Candidate(
        item_key=key,
        type="inbox",
        title=title,
        facts={"workspace_id": obs.scope, "partial": inbox.partial},
        words=word_map,
        evidence=evidence,
        related_chat=None,
        related_ref=related_ref,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence=rules_evidence,
        origin_session=None,
        work_allowed=(),
    )


def _uncommitted(obs: Observation) -> contracts.Candidate | None:
    """`facts`: "workspace_id" only; `templates._what` always uses one fixed sentence."""
    assert obs.vault is not None
    page = obs.page
    if page is None or page.git_state != "known" or page.git_dirty is None:
        return None
    rules_evidence: Mapping[str, object] = {"dirty": page.git_dirty}
    if not rules.present("uncommitted", rules_evidence):
        return None
    title = words.type_words("uncommitted")
    return contracts.Candidate(
        item_key=contracts.item_key("uncommitted", obs.scope),
        type="uncommitted",
        title=title,
        facts={"workspace_id": obs.scope},
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence=rules_evidence,
        origin_session=None,
        work_allowed=(),
    )


def _routine(obs: Observation, now: datetime) -> contracts.Candidate | None:
    """`facts`: "workspace_id", "routine_id" (opens it), "paused"; `words`: "next_run" - "soon"
    within `rules.ROUTINE_SOON` of `now`, else "later" (the same threshold the rules read, so the
    word never disagrees with the verdict)."""
    page = obs.page
    if page is None or page.routine_id is None or page.next_run is None:
        return None
    rules_evidence: Mapping[str, object] = {
        "routine_id": page.routine_id,
        "paused": page.routines_paused,
        "next_run": page.next_run,
    }
    if not rules.present("routine", rules_evidence):
        return None
    soon = page.next_run - now <= rules.ROUTINE_SOON
    title = words.type_words("routine")
    return contracts.Candidate(
        item_key=contracts.item_key("routine", obs.scope),
        type="routine",
        title=title,
        facts={
            "workspace_id": obs.scope,
            "routine_id": page.routine_id,
            "paused": bool(page.routines_paused),
        },
        words={"next_run": _ROUTINE_SOON_WORD if soon else _ROUTINE_LATER_WORD},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence=rules_evidence,
        origin_session=None,
        work_allowed=(),
    )


def _mission_report(mission: contracts.MissionRecord, scope: str) -> contracts.Candidate:
    """`title` is the type's fixed words, not `mission.title`: that is the originating item's own
    title (a task's text), and this candidate has no `related_ref` or evidence for never-send to
    hold it back by. `facts`: "workspace_id", "state" (one of `contracts.MISSION_STATES`) and
    "reason" (the mission's own end or ready reason, e.g. "ready", "deadline", "owner"; never
    sent to the model - `build_state` reads `words`, not `facts` - only `templates._what` reads
    it to pick the sentence). A prepare mission's first waiting plan card is reported still
    "running", with reason "ready"; its `item_key` carries an extra "ready" part so that
    report and the mission's later, real end report are two separate items, never one
    overwriting the other. Reason "nothing" (a second look that found nothing) also adds
    `facts["chat"]`: "" for an empty title or `contracts.UNTITLED_CHAT`, else `mission.title`,
    the same blank-means-"your chat" convention `second_looks.finding` uses."""
    title = words.type_words("mission_report")
    parts = ("mission_report", mission.mission_id)
    if mission.reason == "ready":
        parts = (*parts, "ready")
    facts: dict[str, object] = {
        "workspace_id": scope,
        "state": mission.state,
        "reason": mission.reason,
    }
    if mission.reason == "nothing":
        facts["chat"] = "" if mission.title in ("", contracts.UNTITLED_CHAT) else mission.title
    return contracts.Candidate(
        item_key=contracts.item_key(*parts),
        type="mission_report",
        title=title,
        facts=facts,
        words={},
        evidence=(),
        related_chat=mission.session_id,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"state": mission.state},
        origin_session=mission.session_id,
        work_allowed=(),
    )


def _cards(obs: Observation, now: datetime) -> list[contracts.Candidate]:
    """The Kanban cards chosen this cycle, those that need the user before the stale ones. Per
    type, the kept items (`kanban_keep`) of that type count against its cap first, so a
    listed card that comes back unread never loses its place; the qualifying cards whose items
    are open come next, then the rest, both in Kanban order, so an open card stays chosen over a
    new one that sorts before it."""
    assert obs.kanban is not None
    kept = kanban_keep(obs)
    chosen: list[contracts.Candidate] = []
    for card_type, cap in _CARD_CAPS:
        room = max(0, cap - sum(1 for key in kept if obs.open_cards[key] == card_type))
        found = [
            _card(card, card_type, obs.scope, now)
            for card in obs.kanban.cards
            if _qualifies(card, card_type, now)
        ]
        found.sort(key=lambda candidate: candidate.item_key not in obs.open_cards)  # stable
        chosen.extend(found[:room])
    return chosen


def _qualifies(card: Card, card_type: str, now: datetime) -> bool:
    """Whether `card` is a `card_type` item at `now`: `rules.present` admits its status, matched
    exactly, and a stale card has sat unchanged at least its status's `_STALE_AFTER`."""
    if not rules.present(card_type, {"status": card.status}):
        return False
    if card_type == "card_needs_you":
        return True
    stale_after = _STALE_AFTER.get(card.status or "")
    return stale_after is not None and now - card.changed >= stale_after


def _card(card: Card, card_type: str, scope: str, now: datetime) -> contracts.Candidate:
    """One Kanban card's `Candidate`, keyed by its type and ref, so an edit keeps its item.
    `facts`: "workspace_id", "status" (the card's own), "changed_at" (its aware change time) and,
    when a tag starts "owner/" with something after it, "seat" (the first such tag's rest, as
    written); `words`: "age" (`words.age_words` of the change time, on the user's day) and
    "status". Its evidence names the ref with unknown tags and no excerpt, so any tag rule
    leaves the card to the rules and its tags stay out of the evidence."""
    status = card.status or ""
    facts: dict[str, object] = {
        "workspace_id": scope,
        "status": status,
        "changed_at": card.changed,
    }
    seats = (tag[len(_SEAT_PREFIX) :] for tag in card.tags if tag.startswith(_SEAT_PREFIX))
    seat = next((rest for rest in seats if rest), None)
    if seat is not None:
        facts["seat"] = seat
    evidence = (contracts.Evidence(card.ref, "", False, None),)
    return contracts.Candidate(
        item_key=contracts.item_key(card_type, card.ref),
        type=card_type,
        title=card.title,
        facts=facts,
        words={"age": words.age_words(card.changed, now), "status": _STATUS_WORDS[status]},
        evidence=evidence,
        related_chat=None,
        related_ref=card.ref,
        deadline=None,
        shown_elsewhere=("kanban",),
        rules_evidence={"status": status},
        origin_session=None,
        work_allowed=(),
    )


def seat_candidates(plan: SeatPlan, scope: str, day: str) -> list[contracts.Candidate]:
    """The seat items of one cycle's `SeatPlan`, in the order waiting, working, offers, reports,
    holds. A Start offer is keyed by its routine and its cards' hash, so a new or changed card
    makes a new offer however the user answered the last one; a hold is a `seat_report` keyed
    by its routine, reason and `day`, so one reports once a day. `facts` are what `templates`
    reads: "workspace_id", "routine_id", "seat" (the user's own word, or None), and per type
    "session_id" and "question" (waiting), "session_id" (working), "handoff" (start),
    "session_id" and "reason" (a run report; a hold has no session).
    The seat, the routine and the session stay out of `words`, `evidence` and the related
    fields, so `build_state` sends the model only the type: nothing here names them."""

    def build(
        card_type: str, key_part: str, facts: Mapping[str, object], *more: str
    ) -> contracts.Candidate:
        title = words.type_words(card_type)
        return contracts.Candidate(
            item_key=contracts.item_key(card_type, key_part, *more),
            type=card_type,
            title=title,
            facts={"workspace_id": scope, **facts},
            words={},
            evidence=(),
            related_chat=None,
            related_ref=None,
            deadline=None,
            shown_elsewhere=("routines",),
            rules_evidence={},
            origin_session=None,
            work_allowed=(),
        )

    built = [
        build(
            "seat_waiting",
            ask.attempt_id,
            {
                "routine_id": ask.routine_id,
                "session_id": ask.session_id,
                "seat": ask.seat,
                "question": ask.question,
            },
        )
        for ask in plan.asks
    ]
    built += [
        build(
            "seat_working",
            run.attempt_id,
            {"routine_id": run.routine_id, "session_id": run.session_id, "seat": run.seat},
        )
        for run in plan.working
    ]
    built += [
        build(
            "seat_start",
            offer.routine_id,
            {"routine_id": offer.routine_id, "seat": offer.seat, "handoff": offer.handoff},
            offer.cards,
        )
        for offer in plan.offers
    ]
    built += [
        build(
            "seat_report",
            report.attempt_id,
            {
                "routine_id": report.routine_id,
                "session_id": report.session_id,
                "seat": report.seat,
                "reason": report.reason,
            },
        )
        for report in plan.reports
    ]
    built += [
        build(
            "seat_report",
            hold.routine_id,
            {"routine_id": hold.routine_id, "seat": hold.seat, "reason": hold.reason},
            hold.reason,
            day,
        )
        for hold in plan.holds
    ]
    return built


# ---- small helpers ---------------------------------------------------------------------------


def _is_skill_name(name: str) -> bool:
    """Whether a catalog's skill `name` fits `native_assets.SKILL_NAME` in at most
    `_SKILL_NAME_CHARS` characters, the rule the page's own used names follow too."""
    return len(name) <= _SKILL_NAME_CHARS and SKILL_NAME.fullmatch(name) is not None


def _origin_workspace(item: Mapping[str, Any]) -> str | None:
    """The workspace an inbox item came from (`origin.workspace_id`), None when it names none."""
    origin = item.get("origin")
    return _str_or_none(origin.get("workspace_id")) if isinstance(origin, Mapping) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
