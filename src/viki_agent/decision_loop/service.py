"""The Decision loop's service: the views and actions behind the `/api/v2/decision/*` routes, each
for the active workspace's scope (`WorkspaceService.resolve_active`), on the user's day as this
computer's clock tells it.

Every method is async and answers the view its route sends, built by `wire.py`: `launcher` and
`open_launcher` (which first awaits `engine.recheck()`) answer the Launcher, and so does
`feedback`; `report`, `page`, `preference`, `save_key` and `remove_key` answer the Page;
`delete_all` answers `{deleted}`, the scope's episode count -- the same number the page's
`data.episodes` shows -- though it deletes every item and call, and the learned snapshot, too;
`data` answers the stored-data summary; `records` lists one kind, `record` shows one decision or
suggestion, and `delete` removes selected data and answers `{deleted, kept}`;
`test` answers the TestResult; `calls` the Calls list and `call` one CallDetail, its answers
decoded and labelled by the playbook; `missions` and `stop_missions` the Page's missions block;
`start_mission` and `stop_mission` a Mission; `start_seat` and `stop_seat` the missions block, as
`stop_missions` does (which also stops every seat run and every check run), and so do `run_checks`,
`stop_checks` and `suggest_inbox`; `create_proposal` and `act_proposal` answer
`{proposal}`, a question proposal's Approvals inbox item, and `proposal` its inspector's view;
`act_change` answers `{change, draft}` (`wire.change_act_view`), a vault-change proposal's
Approvals inbox item as the act left it and the draft an approval answers; `presence` and
`observe` answer nothing.

The launcher lists what the user should see now. It leaves out silent items, vault changes
(`contracts.CHANGES`: the engine files each as a change proposal, which Approvals lists and the
`approvals` item counts), items timed "at" until their time, and items held "in_context" until
their chat or note opens: one the presence tracker logged as opened since this service last
looked, or one open now. That release lasts for
the item's episode, so a later decision that holds the item again hides it again. An item held
"after_focus", or timed "at" once its time has come, lists no louder than a badge until a cycle
decides it again, since the cycle applies the hourly nudge cap. A chat-only workspace lists no
items. The page's open count is the launcher's. The launcher's `review` is the weekly check card,
`DecisionLoopStats.review`'s (due, count) for the sample the page lists as `held_back`, and its
`plans_waiting` how many of VIKI's chats have a plan waiting for the user
(`MissionService.plans_waiting`, read on the event loop, where the watches change).

Feedback names a shown item, and so its latest episode, or a held-back episode. The item must be
open: "useful" leaves it acted, unless its work can still start, which leaves it open: its body
offers a start (`_STARTS`), or its episode's work is prepare or run under an Allowed grant
(`cycle.work_grants`) and no mission holds its key yet (`MissionService.taken`), as VIKI's own
start needs the item open. "wrong" and "mute" leave it dismissed, and "later" keeps it open,
timed at now + `LATER`. A held value labels its episode alone. The store keeps each label, mute
and later.
The user's own mission start passes `by_owner`, so a grant of "ask" lets it through; it names an
open item.

The page's and the missions block's `missions.hold` is the plan brake's (`plan_brake.plan_hold`)
over the newest plan reading the `plan_reading` port answers, read in a worker thread; before this
launch's first reading it holds with no end, as VIKI's own starts are held then. It only names the
hold, and the user's own mission start runs while it holds. That start needs a vault whose live
policy allows its work (`detectors.POLICY_WORK`), as the user's other starts read it.

Stop all (`stop_missions`) tells the engine first (`engine.note_stop_all`), so a cycle in flight
starts nothing more, then stops the missions, the seat runs and the check runs, each even when one
before it failed; the first failure is then its answer.

The user's Start on a seat (`start_seat`) goes to the `seats` port (`SeatHost`) and, like the
user's mission start, runs while the plan brake holds and in quiet hours. It needs Decision on
and not paused, a team grant of "ask" or "allowed", a vault that is not chat-only and whose live
policy is "ordinary", complete facts, and a routine `seats.startable` accepts; any other case is
`not_applicable`. The port's claim enforces the daily limit `limit_team`. Without a port the seat
routes answer `unavailable`. `missions.team` and the launcher's `started` read the port, too.

The user's Run checks (`run_checks`) goes to the `checks` port (`CheckHost`) and, like the seat
start, runs while the plan brake holds and in quiet hours. It needs Decision on and not paused, a
check grant of "ask" or "allowed", a vault that is not chat-only and whose live policy is
"ordinary", a supported port, a fresh look at the code folder that finds its checks waiting on the
current commit, and the folder commit's open item (the one the day's limit counted); any other
case is `not_applicable`. A start that begins no check is `busy`, and the port refuses a moved
commit with `not_applicable`. Without a port the check routes answer `unavailable`; Stop all also
stops every check run VIKI started. `missions.checks` reads the port and is null unless it is
supported.

The user's Suggest destinations (`suggest_inbox`) goes to the `inbox` port (`InboxHost`) and, like
Run checks, runs while the plan brake holds and in quiet hours. It needs Decision on and not
paused, an inbox grant of "ask" or "allowed", a vault that is not chat-only and whose live policy
allows prepare (`detectors.POLICY_WORK`), and the scope's open inbox-suggestion item under the
given key whose kind is "suggest" (the one the day's limit counted); any other case is
`not_applicable`. The port refuses a busy suggester with `busy` and a state it saw with
`not_applicable`. Without a port the route answers `unavailable`. It starts no mission: the
inbox suggestion run is not VIKI's chat, so Stop all does not reach it, and `missions.inbox` counts
the items raised today and reads no port.

The page's model calls (its shadow calls among them), tokens and held-back count for the day are
the day counter's (`DecisionLoopStore.usage_on`) and its cost is the measured spend (`spent_on`),
each summed over every scope as the daily cap counts it; its `changes.today` is the day's
vault-change proposals over every scope, the count their daily limit holds
(`ChangeProposals.today`). Its last call is the newest kept call record whose body opens. Its
learning block (`wire.learning_view`) reads the scope's learning rows since its last Reset
(`learn_since`), with the calibration and question rates over them,
and the shadow comparison over the decisions since then whose answers carry a shadow summary,
silent ones included (`DecisionLoopStats.shadow_rows`; `calibration.py`), its stored snapshot
as `learning.parse` reads it (one that does not read back is none) and its λ. Reset (the
preference "reset_learning") forgets the snapshot and λ and restarts learning from that moment;
the decisions and their labels stay.

Question wording proposals are the engine's (`engine.proposals`, a `QuestionProposals`), kept
per workspace. The user proposes a wording that `playbook.check_question` takes, in a workspace
that is not chat-only; the engine's cycles take it as their trial from the next one on. A
proposal's view shows the wording in
use for its question, as the cycles ask it (`QuestionProposals.wording`), beside the proposed
one, and `tried`: `calibration.trial` over the scope's decisions that carry a trial summary
(`DecisionLoopStats.trial_rows`) from the proposal's creation to its first approval or
rejection, or until now while it is open.

Vault-change proposals are the engine's too (`engine.changes`, a `ChangeProposals`), filed by
its cycles. When an act moves a change's state, the same work labels the episode the change's
item was filed with, from the page: "useful" for an approval, "wrong" for a rejection. Draft
again, an approval of an approved change, writes nothing and labels nothing. A label the store
refuses, as when Delete all took its episode, is skipped and the act stands.

The store, the agenda, the stats, the key store, the proposals and the missions' reads run in
worker threads, and so does building the learning block; the engine, the presence tracker and the
missions' start and stop run on the event loop. Delete all, decision deletion, Reset and an act
on a question or change proposal run through `engine.between_cycles`, which halts a running cycle
and runs them
under the engine's cycle lock while no cycle runs, so none commits in the middle of one, leaves
an item on a deleted episode or asks with a wording an act has just replaced, and no cycle files
a change beside an act. Delete all, decision deletion and Reset also drop the
scope's self-improvement candidates the engine's beat kept (`engine.forget_improve`), there under
the lock, so no candidate counted from rows read before them reaches a cycle. Each write but
`observe` wakes the engine: a presence change with "context", a mission start or stop with
"mission", the others with "change". Refusals are the components' `DecisionLoopError` and
`MissionError` codes. No view holds the key or a model id: a version is `wire.display_version`
output. Nothing here logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.decision_loop import (
    arbiter,
    calibration,
    detectors,
    learning,
    playbook,
    second_looks,
    wire,
)
from viki_agent.decision_loop.agenda import MARKS, CallEntry, DecisionAgenda
from viki_agent.decision_loop.changes import ChangeRecord
from viki_agent.decision_loop.contracts import (
    CHANGES,
    FINDING_KINDS,
    INBOX_TRIAGE,
    LATER,
    LEVELS,
    MESSAGE_DRAFTS,
    PROJECT_PLANS,
    SAVED_CHECKS,
    SECOND_LOOK_WINDOW,
    SECOND_LOOKS,
    CallBody,
    CallRecord,
    CheckFolder,
    CheckHost,
    DecisionLoopError,
    DecisionSettings,
    InboxHost,
    ItemRecord,
    MissionRecord,
    Presence,
    SeatHost,
    SeatRoutine,
    SeatTeam,
    item_key,
)
from viki_agent.decision_loop.cycle import decode_answers, work_grants
from viki_agent.decision_loop.engine import DecisionLoopEngine
from viki_agent.decision_loop.key_store import DecisionKeyStore
from viki_agent.decision_loop.missions import MissionService
from viki_agent.decision_loop.plan_brake import Hold, PlanReading, no_reading, plan_hold
from viki_agent.decision_loop.presence import Opened, PresenceTracker
from viki_agent.decision_loop.proposals import ProposalRecord
from viki_agent.decision_loop.seats import startable
from viki_agent.decision_loop.stats import DecisionLoopStats
from viki_agent.decision_loop.store import DecisionLoopStore, Usage
from viki_agent.decision_loop.wire import FeedbackRequest
from viki_agent.workspaces import WorkspaceService

STARTED_DAYS = 30  # the launcher's `started`: the chats VIKI started in this many days
REPORT_TRIGGER = "change"  # the trigger a user's report is recorded with, outside any cycle

_CLOSES = {"useful": "acted", "wrong": "dismissed", "mute": "dismissed"}  # value -> item state
# The actions that start an item's work; Useful leaves an item offering one open.
_STARTS = frozenset({"start_prepare", "start_run", "start_seat", "run_checks", "suggest_inbox"})
_RANK = {level: rank for rank, level in enumerate(LEVELS)}
# The timings that held a decided level back. The cycle applies the hourly nudge cap, so until
# one decides such an item again it lists no louder than a badge.
_HELD = ("at", "after_focus")


@dataclass(frozen=True)
class _LauncherReads:
    """What the launcher reads in a worker thread."""

    settings: DecisionSettings
    items: list[ItemRecord]
    working: int
    started: list[str]
    review: tuple[bool, int]


@dataclass(frozen=True)
class _PageReads:
    """What the page reads in a worker thread, its learning block built there too."""

    settings: DecisionSettings
    items: list[ItemRecord]
    key_saved: bool
    parts: dict[str, Any]
    calls: list[CallEntry]
    spent_micro_usd: int
    usage: Usage
    learning: dict[str, Any]
    running: list[MissionRecord]
    prepare_today: int
    run_today: int
    look_today: int
    second_look_today: int
    project_plan_today: int
    message_draft_today: int
    inbox_today: int
    check_today: int | None
    check_running: int
    changes_today: int
    hold: Hold | None
    team: SeatTeam | None


class DecisionLoopService:
    """The Decision routes' views and actions for the active workspace; see the module
    docstring. `read_settings` reads the Decision settings; `now` is read once per operation."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        engine: DecisionLoopEngine,
        store: DecisionLoopStore,
        agenda: DecisionAgenda,
        stats: DecisionLoopStats,
        keys: DecisionKeyStore,
        missions: MissionService,
        presence: PresenceTracker,
        read_settings: Callable[[], DecisionSettings],
        *,
        now: Callable[[], datetime],
        plan_reading: PlanReading = no_reading,
        seats: SeatHost | None = None,
        checks: CheckHost | None = None,
        inbox: InboxHost | None = None,
    ) -> None:
        self._plan_reading = plan_reading
        self._seats = seats
        self._checks = checks
        self._inbox = inbox
        self._workspaces = workspaces
        self._engine = engine
        self._store = store
        self._agenda = agenda
        self._stats = stats
        self._keys = keys
        self._missions = missions
        self._presence = presence
        self._read_settings = read_settings
        self._now = now
        self._opened = 0  # the presence tracker's opened-log token this service has read up to
        self._released: set[tuple[str, str]] = set()  # (item_key, episode_id) shown in context

    # ---- the launcher --------------------------------------------------------------------------

    async def launcher(self) -> dict[str, Any]:
        """The launcher now."""
        return await self._launcher(self._local())

    async def open_launcher(self) -> dict[str, Any]:
        """One cycle now, as long as the engine's recheck allows, then the launcher."""
        await self._engine.recheck()
        return await self._launcher(self._local())

    async def presence(self, window: str, presence: Presence) -> None:
        """Merge one window's report; a change the tracker counts wakes the engine."""
        if self._presence.report(window, presence):
            self._engine.wake("context")

    async def feedback(self, request: FeedbackRequest) -> dict[str, Any]:
        """Record the user's feedback as the module docstring says; answer the launcher."""
        local = self._local()
        await asyncio.to_thread(self._feedback, request, local)
        self._engine.wake("change")
        return await self._launcher(local)

    async def observe(self, event: str, item_keys: Sequence[str]) -> None:
        """Stamp a page event ("shown", "opened" or "acted") on the listed items and on their
        latest episodes. The agenda stamps "shown" and "opened"; keys the scope lacks are
        skipped."""
        await asyncio.to_thread(self._observe, event, tuple(item_keys), self._local())

    # ---- the page ------------------------------------------------------------------------------

    async def page(self) -> dict[str, Any]:
        """The Decision page now."""
        return await self._page(self._local())

    async def report(self, topic: str, source: str) -> dict[str, Any]:
        """Record the user's "you should have told me" about `topic`; answer the page."""
        local = self._local()
        await asyncio.to_thread(self._report, topic, source, local)
        self._engine.wake("change")
        return await self._page(local)

    async def preference(self, name: str, candidate: str | None) -> dict[str, Any]:
        """Unmute `candidate` ("unmute"), note the review as seen ("review_seen", no candidate)
        or Reset ("reset_learning", no candidate): between the engine's cycles, forget the
        scope's learned snapshot and λ and restart learning now. "try_again" (a second look's or
        a project plan's type) and "raise_again" (a kind of finding) restart that count. Answer
        the page."""
        local = self._local()
        if name == "reset_learning" and candidate is None:
            await self._engine.between_cycles(lambda: self._reset_learning(local))
        else:
            await asyncio.to_thread(self._preference, name, candidate, local)
        self._engine.wake("change")
        return await self._page(local)

    async def data(self) -> dict[str, Any]:
        """The active scope's stored-data summary and key status."""
        local = self._local()
        scope, _chat_only = self._scope()
        parts, saved, changed_at = await asyncio.to_thread(
            lambda: (
                self._stats.data_summary(scope, local),
                self._keys.saved(),
                self._keys.changed_at(),
            )
        )
        return wire.data_summary_view(parts, now=local, key_saved=saved, key_changed_at=changed_at)

    async def records(self, query: wire.DataQuery) -> dict[str, Any]:
        """One bounded page of decisions or suggestions, or the scope's kept calls."""
        scope, _chat_only = self._scope()
        if query.kind == "decisions":
            rows = await asyncio.to_thread(
                self._store.decision_records,
                scope,
                topic=query.topic,
                shown=query.shown,
                answer=query.answer,
                since=query.since,
                until=query.until,
                before=query.before,
                limit=wire.MAX_DATA_ROWS,
            )
            return wire.data_records_view(query.kind, rows)
        if query.kind == "suggestions":
            rows = await asyncio.to_thread(
                self._agenda.item_records,
                scope,
                topic=query.topic,
                state=query.state,
                since=query.since,
                until=query.until,
                before=query.before,
                limit=wire.MAX_DATA_ROWS,
            )
            return wire.data_records_view(query.kind, rows)
        if query.kind == "calls":
            calls = await asyncio.to_thread(self._agenda.calls, scope)
            return wire.data_calls_view(
                [call for call in calls if query.outcome is None or call.outcome == query.outcome]
            )
        raise DecisionLoopError("invalid_request")

    async def record(self, kind: str, record_id: str) -> dict[str, Any]:
        """One decision or suggestion detail, or `unknown_item`."""
        scope, _chat_only = self._scope()
        if kind == "decisions":
            found = await asyncio.to_thread(
                self._stats.decision_record, scope, record_id, agenda=self._agenda
            )
            if found is not None:
                return wire.data_decision_view(found)
        elif kind == "suggestions":
            found = await asyncio.to_thread(self._agenda.item_detail, scope, record_id)
            if found is not None:
                return wire.data_suggestion_view(found)
        raise DecisionLoopError("unknown_item")

    async def delete(self, query: wire.DataQuery) -> dict[str, int]:
        """Delete selected data, preserving held rows; decisions run between cycles."""
        scope, _chat_only = self._scope()
        if query.kind == "decisions":
            deleted, kept = await self._engine.between_cycles(
                lambda: self._delete_decisions(scope, query)
            )
        elif query.kind == "suggestions":
            deleted, kept = await asyncio.to_thread(
                self._agenda.delete_items,
                scope,
                ids=query.ids,
                topic=query.topic,
                state=query.state,
                since=query.since,
                until=query.until,
            )
        elif query.kind == "calls":
            if query.ids is None:
                raise DecisionLoopError("invalid_request")
            deleted = await asyncio.to_thread(self._agenda.delete_calls, scope, query.ids)
            kept = 0
        else:
            raise DecisionLoopError("invalid_request")
        self._engine.wake("change")
        return wire.data_deleted_view(deleted, kept)

    async def delete_all(self) -> dict[str, int]:
        """Delete the scope's episodes, items and call records, and its learned snapshot,
        between the engine's cycles; answer how many episodes went, the count the page asked
        about deleting. λ, the other preferences, the missions and the question and change
        proposals stay, and so does the wording in use: a trial's numbers go with the
        episodes."""
        deleted = await self._engine.between_cycles(self._delete_all)
        self._released.clear()
        self._engine.wake("change")
        return {"deleted": deleted}

    async def save_key(self, key: str) -> dict[str, Any]:
        """Save the user's key, once it passes `wire.parse_key`'s rule (a refusal is the code
        alone); answer the page."""
        local = self._local()
        wire.parse_key({"key": key})
        await asyncio.to_thread(self._keys.save, key)
        self._engine.wake("change")
        return await self._page(local)

    async def remove_key(self) -> dict[str, Any]:
        """Remove the saved key, if any; answer the page."""
        local = self._local()
        await asyncio.to_thread(self._keys.remove)
        self._engine.wake("change")
        return await self._page(local)

    async def test(self) -> dict[str, Any]:
        """Test the saved key with the engine's one fixed question; answer the result."""
        return wire.test_view(await self._engine.test_key())

    # ---- the Context tab -----------------------------------------------------------------------

    async def calls(self) -> dict[str, Any]:
        """The scope's kept model calls, newest first, and how its latest decision was made."""
        settings, calls, latest = await asyncio.to_thread(self._calls_reads, self._local())
        return wire.calls_view(settings, calls, latest=latest)

    async def call(self, call_id: str) -> dict[str, Any]:
        """One kept call of the scope; `unknown_item` when there is none or it does not open."""
        record = await asyncio.to_thread(self._call, call_id)
        return wire.call_detail_view(
            record,
            questions=_questions(record.body),
            largest_question=_largest_question(record.body),
        )

    # ---- missions ------------------------------------------------------------------------------

    async def missions(self) -> dict[str, Any]:
        """The page's missions block: VIKI's running chats, today's starts, its looks apart,
        and the limits."""
        return await asyncio.to_thread(self._missions_block, self._local())

    async def start_mission(self, item_key: str, work: str) -> dict[str, Any]:
        """The user starts `work` ("prepare" or "run") for one open item; answer the mission.
        `not_applicable` outside a vault whose live policy allows `work`; `MissionService.start`
        refuses as its codes say, a grant of "off" among them."""
        scope, item = await asyncio.to_thread(self._mission_to_start, item_key, work)
        leave_out = ()
        if item.candidate in SECOND_LOOKS:
            leave_out = await asyncio.to_thread(self._leave_out, scope)
        mission = await self._missions.start(scope, item, work, by_owner=True, leave_out=leave_out)
        self._engine.wake("mission")
        return wire.mission_view(mission)

    async def stop_mission(self, mission_id: str) -> dict[str, Any]:
        """Stop one of VIKI's chats; answer it as stored after."""
        mission = await self._missions.stop(mission_id)
        self._engine.wake("mission")
        return wire.mission_view(mission)

    async def stop_missions(self) -> dict[str, Any]:
        """Stop every one of VIKI's running chats, seat runs and check runs; answer the missions
        block, or raise the first stop's failure once every stop has run."""
        self._engine.note_stop_all()
        stops: list[Callable[[], Awaitable[object]]] = [self._missions.stop_all]
        if self._seats is not None:
            stops.append(self._seats.stop_all)
        if self._checks is not None:
            stops.append(self._checks.stop_all)
        failure: Exception | None = None
        for stop in stops:
            # An unreadable store fails the missions' stop, yet a check run is cancelled from
            # memory: one failure must not leave the others running.
            try:
                await stop()
            except Exception as exc:
                failure = failure or exc
        self._engine.wake("mission")
        if failure is not None:
            raise failure
        return await self.missions()

    async def start_seat(self, routine_id: str) -> dict[str, Any]:
        """The user starts the routine `routine_id` for its seat; answer the missions block.
        Refused as the module docstring says; the port's claim refuses with `limit`, `busy`,
        `not_applicable` or `unavailable`."""
        if self._seats is None:
            raise DecisionLoopError("unavailable")
        local = self._local()
        day = local.date().isoformat()
        settings, routine, newest = await asyncio.to_thread(
            self._seat_to_start, self._seats, routine_id, local, day
        )
        await self._seats.start(
            routine.routine_id,
            routine.revision,
            newest=newest,
            cards=None,  # no card list: the user chose the seat, not a set of cards
            limit=settings.limit_team,
            day=day,
        )
        self._engine.wake("mission")
        return await self.missions()

    async def stop_seat(self, routine_id: str) -> dict[str, Any]:
        """Interrupt the routine's open run, if it has a live one; answer the missions block."""
        if self._seats is None:
            raise DecisionLoopError("unavailable")
        await self._seats.stop(routine_id)
        self._engine.wake("mission")
        return await self.missions()

    async def run_checks(self, workspace_id: str) -> dict[str, Any]:
        """The user runs the saved checks of the code folder `workspace_id` that have not run on
        its current commit; answer the missions block. Refused as the module docstring says."""
        if self._checks is None:
            raise DecisionLoopError("unavailable")
        folder = await asyncio.to_thread(
            self._checks_to_run, self._checks, workspace_id, self._local()
        )
        if await self._checks.start(workspace_id, folder.head, folder.pending) == 0:
            raise DecisionLoopError("busy")  # every check was running or refused already
        self._engine.wake("mission")
        return await self.missions()

    async def suggest_inbox(self, item_key: str) -> dict[str, Any]:
        """The user starts the inbox suggestion run for the open item `item_key`; answer the
        missions block. Refused as the module docstring says; the port refuses with `busy`,
        `not_applicable` or `unavailable`."""
        if self._inbox is None:
            raise DecisionLoopError("unavailable")
        scope = await asyncio.to_thread(self._triage_to_start, item_key, self._local())
        await self._inbox.start(scope, item_key)
        self._engine.wake("mission")
        return await self.missions()

    async def stop_checks(self, workspace_id: str) -> dict[str, Any]:
        """Stop the check runs VIKI started in the code folder `workspace_id`, if any are live;
        answer the missions block."""
        if self._checks is None:
            raise DecisionLoopError("unavailable")
        await self._checks.stop(workspace_id)
        self._engine.wake("mission")
        return await self.missions()

    # ---- question wording proposals ------------------------------------------------------------

    async def create_proposal(self, name: str, question: Mapping[str, object]) -> dict[str, Any]:
        """The user proposes `question` as the wording of question `name`; answer `{proposal}`,
        open. Refused in this order: invalid_request unless `playbook.check_question` takes it;
        not_applicable in a chat-only workspace, while the scope holds an open proposal, or when
        it is the wording in use (`QuestionProposals.create`)."""
        try:
            checked = playbook.check_question(name, question)
        except ValueError:
            raise DecisionLoopError("invalid_request") from None
        record = await asyncio.to_thread(self._create_proposal, name, checked, self._local())
        self._engine.wake("change")
        return {"proposal": self._engine.proposals.item(record)}

    async def proposal(self, proposal_id: str) -> dict[str, Any]:
        """One question proposal of the scope for its inspector (see the module docstring);
        `unknown_item` when the scope holds none by that id or its wording does not open."""
        return await asyncio.to_thread(self._proposal, proposal_id)

    async def act_proposal(
        self, proposal_id: str, action: str, revision: str, reason: str | None
    ) -> dict[str, Any]:
        """Approve or reject one question proposal at the `revision` the user saw, between the
        engine's cycles; answer `{proposal}` as the act left it. Refusals are
        `QuestionProposals.act`'s: a revision that moved is not_applicable."""
        local = self._local()
        record = await self._engine.between_cycles(
            lambda: self._act_proposal(proposal_id, action, revision, reason, local)
        )
        self._engine.wake("change")
        return {"proposal": self._engine.proposals.item(record)}

    # ---- vault-change proposals ----------------------------------------------------------------

    async def act_change(
        self, proposal_id: str, action: str, revision: str, reason: str | None
    ) -> dict[str, Any]:
        """Approve, draft again or reject one vault-change proposal at the `revision` the user
        saw, between the engine's cycles, and label its episode as the module docstring says;
        answer `{change, draft}` (`wire.change_act_view`). Refusals are `ChangeProposals.act`'s:
        a revision that moved is not_applicable."""
        local = self._local()
        record, draft = await self._engine.between_cycles(
            lambda: self._act_change(proposal_id, action, revision, reason, local)
        )
        self._engine.wake("change")
        return wire.change_act_view(self._engine.changes.item(record), draft)

    # ---- views ---------------------------------------------------------------------------------

    async def _launcher(self, local: datetime) -> dict[str, Any]:
        reads = await asyncio.to_thread(self._launcher_reads, local)
        status = await self._engine.status()
        return wire.launcher_view(
            reads.settings,
            self._visible(reads.items, local),
            now=local,
            model_state=status.state,
            checked_at=status.checked_at,
            working=reads.working,
            nudge=self._engine.nudge,
            glow=self._engine.glow,
            started=reads.started,
            review=reads.review,
            plans_waiting=self._missions.plans_waiting(),  # read on the loop, where watches change
        )

    async def _page(self, local: datetime) -> dict[str, Any]:
        reads = await asyncio.to_thread(self._page_reads, local)
        status = await self._engine.status()
        parts = reads.parts
        usage = reads.usage
        return wire.page_view(
            reads.settings,
            now=local,
            key_saved=reads.key_saved,
            model_state=status.state,
            last_error=status.last_error,
            last_test=status.last_test,
            checked_at=status.checked_at,
            open_items=len(self._visible(reads.items, local)),
            decisions_today=parts["today"]["decisions"],
            model_calls_today=usage.calls,
            tokens_today=usage.tokens,
            spent_micro_usd=reads.spent_micro_usd,
            last_call=next((call for call in reads.calls if call.body is not None), None),
            held_back_today=usage.held_back,
            muted=parts["muted"],
            held_back=parts["held_back"],
            recent=parts["recent"],
            counts=parts["counts"],
            running=reads.running,
            prepare_today=reads.prepare_today,
            run_today=reads.run_today,
            look_today=reads.look_today,
            second_look_today=reads.second_look_today,
            project_plan_today=reads.project_plan_today,
            message_draft_today=reads.message_draft_today,
            inbox_today=reads.inbox_today,
            check_today=reads.check_today,
            check_running=reads.check_running,
            episodes=parts["data"]["episodes"],
            oldest=parts["data"]["oldest"],
            learning=reads.learning,
            shadow_calls_today=usage.shadow_calls,
            changes_today=reads.changes_today,
            hold=reads.hold,
            team=reads.team,
        )

    def _visible(self, items: Sequence[ItemRecord], now: datetime) -> list[ItemRecord]:
        """The open items the user should see at `now`, loudest and then oldest first; see the
        module docstring. Runs on the event loop: it reads the presence tracker."""
        here = self._here()
        at = stamp(now)
        released: set[tuple[str, str]] = set()
        shown: list[ItemRecord] = []
        for item in items:
            if item.level == "silent" or item.candidate in CHANGES:
                continue
            if item.timing == "at" and item.defer_until is not None and item.defer_until > at:
                continue
            if item.timing == "in_context":
                mark = (item.item_key, item.episode_id)
                if mark not in self._released and not _places(item) & here:
                    continue
                released.add(mark)
            if item.timing in _HELD and _RANK[item.level] > _RANK["badge"]:
                item = replace(item, level="badge")
            shown.append(item)
        self._released = released
        shown.sort(key=lambda item: (-_RANK[item.level], item.created_at, item.item_key))
        return shown

    def _here(self) -> set[Opened]:
        """The chats and notes opened since this service last looked, and the ones open now."""
        self._opened, opened = self._presence.opened_since(self._opened)
        current = self._presence.current()
        here: set[Opened] = set(opened)
        if current.chat_id is not None:
            here.add(current.chat_id)
        if current.note_ref is not None:
            here.add(current.note_ref)
        return here

    # ---- worker-thread reads and writes --------------------------------------------------------

    def _local(self) -> datetime:
        return self._now().astimezone()

    def _scope(self) -> tuple[str, bool]:
        """The active workspace's id, which is the scope, and whether it is chat-only."""
        active = self._workspaces.resolve_active()
        return active.workspace.workspace_id, active.chat_only

    def _open_items(self, scope: str, chat_only: bool) -> list[ItemRecord]:
        return [] if chat_only else self._agenda.open_items(scope)[0]

    def _launcher_reads(self, local: datetime) -> _LauncherReads:
        scope, chat_only = self._scope()
        team = self._team(local)
        started = self._missions.started_since(local - timedelta(days=STARTED_DAYS))
        if team is not None:
            started = _started(started, team.sessions)
        return _LauncherReads(
            settings=self._read_settings(),
            items=self._open_items(scope, chat_only),
            working=len(self._missions.running()),
            started=started,
            review=self._stats.review(scope, local),
        )

    def _team(self, local: datetime) -> SeatTeam | None:
        """The seat port's counts for the user's day and the sessions of the last
        `STARTED_DAYS` days; None without a port."""
        if self._seats is None:
            return None
        return self._seats.team(local.date().isoformat(), local - timedelta(days=STARTED_DAYS))

    def _page_reads(self, local: datetime) -> _PageReads:
        scope, chat_only = self._scope()
        day = local.date().isoformat()
        settings = self._read_settings()
        check_today, check_running = self._check_counts(scope, day)
        return _PageReads(
            settings=settings,
            items=self._open_items(scope, chat_only),
            key_saved=self._keys.saved(),
            parts=self._stats.page_parts(scope, local, agenda=self._agenda),
            calls=self._agenda.calls(scope),
            spent_micro_usd=self._store.spent_on(day),
            usage=self._store.usage_on(day),
            learning=self._learning(scope, settings),
            running=self._missions.running(),
            prepare_today=self._missions.count_today("prepare", day),
            run_today=self._missions.count_today("run", day),
            look_today=self._missions.count_looks(day),
            second_look_today=self._missions.count_second_looks(day),
            project_plan_today=self._missions.count_project_plans(day),
            message_draft_today=self._stats.drafts_today(scope, day, ())[0],
            inbox_today=self._inbox_today(scope, day),
            check_today=check_today,
            check_running=check_running,
            changes_today=self._engine.changes.today(day),
            hold=self._hold(settings, local),
            team=self._team(local),
        )

    def _hold(self, settings: DecisionSettings, local: datetime) -> Hold | None:
        """The plan brake's hold on VIKI's own starts: with no reading yet this launch, one with
        no end, since VIKI cannot tell how much of the plan is left until a reading comes."""
        reading = self._plan_reading()
        return Hold(None) if reading is None else plan_hold(reading, settings.plan_brake, local)

    def _learning(self, scope: str, settings: DecisionSettings) -> dict[str, Any]:
        """The page's learning block: the scope's rows since its last Reset (`learn_since`)
        and what they show, the shadow's own rows (silent ones included) beside them, its
        stored snapshot and λ, and what the second looks, the project plans, the message
        drafts, the saved checks and the inbox suggestions yielded."""
        since = self._store.learn_since(scope)
        rows = self._stats.learning_rows(scope, since=since)
        shadowed = self._stats.shadow_rows(scope, since=since)
        return wire.learning_view(
            settings,
            learning.parse(self._store.get_json(scope, "learned")),
            labels=len(learning.labelled(rows)),
            since=since,
            lam=arbiter.lambda_of(self._store.get_json(scope, "lambda")),
            calibration=calibration.calibration(rows),
            questions=calibration.questions(rows),
            shadow=calibration.shadow(shadowed, settings.shadow),
            second_looks=self._yields(scope, settings, since, SECOND_LOOKS, "second_looks"),
            project_plans=self._yields(scope, settings, since, PROJECT_PLANS, "project_plans"),
            message_drafts=self._yields(scope, settings, since, MESSAGE_DRAFTS, "message_drafts"),
            saved_checks=self._yields(scope, settings, since, SAVED_CHECKS, "saved_checks"),
            inbox_triage=self._yields(scope, settings, since, INBOX_TRIAGE, "inbox_triage"),
        )

    def _yields(
        self,
        scope: str,
        settings: DecisionSettings,
        learn_since: datetime | None,
        family: tuple[str, ...],
        name: str,
    ) -> second_looks.SecondLookYields:
        """What the scope's `family` of types (`SECOND_LOOKS`, `PROJECT_PLANS`, `MESSAGE_DRAFTS`,
        `SAVED_CHECKS` or `INBOX_TRIAGE`) yielded over the engine's window: the later of
        `SECOND_LOOK_WINDOW` back and the last Reset (`learn_since`). `name` is the preference
        holding its restarts. Project plans, message drafts, saved checks and inbox suggestions
        have no finding kinds; all but the plans have no mission, so their rows come from
        `DecisionLoopStats.draft_rows`. A saved check or inbox suggestion item that closed
        unanswered is no miss (`closed=""`): only an explicit answer counts. An inbox suggestion
        run that found nothing leaves its item "done", which reads "nothing"
        (`done="nothing"`)."""
        since = self._now() - SECOND_LOOK_WINDOW
        if learn_since is not None:
            since = max(since, learn_since)
        kinds = FINDING_KINDS if family == SECOND_LOOKS else ()
        if family == MESSAGE_DRAFTS:
            rows = self._stats.draft_rows(scope, since)
        elif family == SAVED_CHECKS:
            rows = self._stats.draft_rows(scope, since, types=family, closed="")
        elif family == INBOX_TRIAGE:
            rows = self._stats.draft_rows(scope, since, types=family, closed="", done="nothing")
        else:
            rows = self._missions.second_look_rows(scope, since, types=family)
        return second_looks.yields(
            rows,
            second_looks.restarts(self._store.get_json(scope, name), looks=family, kinds=kinds),
            self._stats.wrong_reasons(scope, family, since),
            learning=settings.learning,
            looks=family,
            kinds=kinds,
        )

    def _leave_out(self, scope: str) -> tuple[str, ...]:
        """The kinds of finding the scope's second looks leave out now (paused)."""
        yielded = self._yields(
            scope,
            self._read_settings(),
            self._store.learn_since(scope),
            SECOND_LOOKS,
            "second_looks",
        )
        return second_looks.paused_kinds(yielded)

    def _feedback(self, request: FeedbackRequest, now: datetime) -> None:
        """Label the episode, then leave the item as the value says; exactly one of the item
        key and the episode id is set."""
        scope, _chat_only = self._scope()
        item: ItemRecord | None = None
        if request.item_key is not None and request.episode_id is None:
            item = self._agenda.item(scope, request.item_key)
            if item is None:
                raise DecisionLoopError("unknown_item")
            if item.state != "open":
                raise DecisionLoopError("not_applicable")
            episode_id = item.episode_id
        elif request.episode_id is not None and request.item_key is None:
            episode_id = request.episode_id
        else:
            raise DecisionLoopError("invalid_request")
        self._store.feedback(
            scope,
            episode_id=episode_id,
            value=request.value,
            reason=request.reason,
            source=request.source,
            now=now,
        )
        if item is None:
            return
        if request.value == "later":
            self._agenda.set_state(scope, item.item_key, "open", now, defer_until=now + LATER)
        # Useful only labels an item whose work can still start: a start needs the item open.
        elif request.value in _CLOSES and not (
            request.value == "useful" and self._can_start(scope, item)
        ):
            self._agenda.set_state(scope, item.item_key, _CLOSES[request.value], now)

    def _can_start(self, scope: str, item: ItemRecord) -> bool:
        """Whether the open `item`'s work can still start, so Useful leaves it open: its body
        offers a start (`_STARTS`), or its latest decision gave it work prepare or run under an
        Allowed grant (`cycle.work_grants`) and no mission holds its key yet, so VIKI's own start,
        which needs the item open, may still run it."""
        if any(action.type in _STARTS for action in item.body.actions):
            return True
        with self._store._transaction(write=False) as connection:  # the item stores no work
            row = connection.execute(
                "SELECT work FROM decision_episodes WHERE scope=? AND episode_id=?",
                (scope, item.episode_id),
            ).fetchone()
        work = None if row is None else row[0]
        if work not in ("prepare", "run"):
            return False
        if work_grants(item.candidate, self._read_settings()).get(work) != "allowed":
            return False
        return not self._missions.taken([item.item_key])

    def _observe(self, event: str, keys: tuple[str, ...], now: datetime) -> None:
        if event not in wire.OBSERVE_EVENTS:
            raise DecisionLoopError("invalid_request")
        scope, _chat_only = self._scope()
        found = [self._agenda.item(scope, key) for key in keys]
        episodes = list(dict.fromkeys(item.episode_id for item in found if item is not None))
        if event in MARKS:
            self._agenda.mark(scope, keys, event, now)
        if episodes:
            self._store.observe(scope, event=event, episode_ids=episodes, now=now)

    def _report(self, topic: str, source: str, now: datetime) -> None:
        scope, _chat_only = self._scope()
        self._store.report(
            scope,
            topic=topic,
            source=source,
            trigger=REPORT_TRIGGER,
            now=now,
            day=now.date().isoformat(),
        )

    def _preference(self, name: str, candidate: str | None, now: datetime) -> None:
        scope, _chat_only = self._scope()
        if name == "unmute" and candidate is not None:
            self._store.unmute(scope, candidate, now)
        elif name == "review_seen" and candidate is None:
            self._store.mark_review_seen(scope, now)
        elif name in ("try_again", "raise_again") and candidate is not None:
            # A project-plan, message-draft, saved-check or inbox-suggestion type has its own
            # preference and no kinds; a second look's two restarts share "second_looks". The other
            # preferences stay as they are.
            own = {
                "project_plans": PROJECT_PLANS,
                "message_drafts": MESSAGE_DRAFTS,
                "saved_checks": SAVED_CHECKS,
                "inbox_triage": INBOX_TRIAGE,
            }
            held_name = next(
                (n for n, family in own.items() if name == "try_again" and candidate in family),
                "second_looks",
            )
            held = self._store.get_json(scope, held_name)
            try:
                if held_name != "second_looks":
                    value = second_looks.restarted(
                        held, look=candidate, now=now, looks=own[held_name], kinds=()
                    )
                elif name == "try_again":
                    value = second_looks.restarted(held, look=candidate, now=now)
                else:
                    value = second_looks.restarted(held, kind=candidate, now=now)
            except ValueError:
                raise DecisionLoopError("invalid_request") from None
            self._store.set_json(scope, held_name, value, now)
        else:
            raise DecisionLoopError("invalid_request")

    def _reset_learning(self, now: datetime) -> None:
        """Forget the scope's learned snapshot and λ, and learn from `now` on. The beat's
        self-improvement candidates go first, so a failed write leaves none behind either."""
        scope, _chat_only = self._scope()
        self._engine.forget_improve(scope)
        self._store.forget_learning(scope, restart_at=now)

    def _delete_all(self) -> int:
        """Delete every item and call and the learned snapshot too; answer the episode count
        alone, which always equals `DecisionLoopStats.page_parts`'s `data.episodes` (both count
        the scope's `decision_episodes` rows, with no other filter, at the same moment). The
        beat's self-improvement candidates go first, as in `_reset_learning`."""
        scope, _chat_only = self._scope()
        self._engine.forget_improve(scope)
        episodes = self._store.delete_episodes(scope)
        self._agenda.delete_all(scope)
        self._store.forget_learning(scope, restart_at=None)
        return episodes

    def _delete_decisions(self, scope: str, query: wire.DataQuery) -> tuple[int, int]:
        """Drop the beat's candidates, then delete and forget learning only if rows went."""
        self._engine.forget_improve(scope)
        deleted, kept = self._store.delete_decisions(
            scope,
            ids=query.ids,
            topic=query.topic,
            shown=query.shown,
            answer=query.answer,
            since=query.since,
            until=query.until,
        )
        if deleted:
            self._store.forget_learning(scope, restart_at=None)
        return deleted, kept

    def _calls_reads(self, local: datetime) -> tuple[DecisionSettings, list[CallEntry], str | None]:
        """The settings, the scope's kept calls and the policy of its newest decision."""
        scope, _chat_only = self._scope()
        recent = self._stats.page_parts(scope, local)["recent"]
        latest = recent[0]["policy"] if recent else None
        return self._read_settings(), self._agenda.calls(scope), latest

    def _call(self, call_id: str) -> CallRecord:
        scope, _chat_only = self._scope()
        return self._agenda.call(scope, call_id)

    def _check_counts(self, scope: str, day: str) -> tuple[int | None, int]:
        """The new commits checked today and the check runs VIKI started that are still going;
        no count (None) unless the port is supported."""
        if self._checks is None or not self._checks.supported():
            return None, 0
        today = self._stats.drafts_today(scope, day, (), types=SAVED_CHECKS)[0]
        return today, self._checks.running()

    def _inbox_today(self, scope: str, day: str) -> int:
        """The inbox suggestion items raised today in any workspace, whatever their state: the
        count the daily limit holds."""
        return self._stats.drafts_today(scope, day, (), types=INBOX_TRIAGE)[0]

    def _missions_block(self, local: datetime) -> dict[str, Any]:
        day = local.date().isoformat()
        settings = self._read_settings()
        scope = self._scope()[0]
        check_today, check_running = self._check_counts(scope, day)
        return wire.missions_view(
            self._missions.running(),
            settings,
            prepare_today=self._missions.count_today("prepare", day),
            run_today=self._missions.count_today("run", day),
            look_today=self._missions.count_looks(day),
            second_look_today=self._missions.count_second_looks(day),
            project_plan_today=self._missions.count_project_plans(day),
            inbox_today=self._inbox_today(scope, day),
            check_today=check_today,
            check_running=check_running,
            hold=self._hold(settings, local),
            team=self._team(local),
        )

    def _seat_to_start(
        self, seats: SeatHost, routine_id: str, local: datetime, day: str
    ) -> tuple[DecisionSettings, SeatRoutine, str | None]:
        """The settings, the routine the user's Start may claim and the id of its newest run
        (the claim's compare-and-set), or `not_applicable`. The plan brake and quiet hours are
        not checked: the user is present. The vault's policy is read live, as a cycle does."""
        active = self._workspaces.resolve_active()
        settings = self._read_settings()
        paused = settings.paused_until is not None and settings.paused_until > local
        if (
            active.workspace.kind != "vault"
            or active.chat_only
            or not settings.enabled
            or paused
            or settings.grant_team not in ("ask", "allowed")
            or detectors._policy(self._workspaces, active.workspace) != "ordinary"
        ):
            raise DecisionLoopError("not_applicable")
        facts = seats.facts(active.workspace.workspace_id, None, day)
        routine = startable(facts, routine_id)
        if not facts.complete or routine is None:  # an unreadable routine may be a rival
            raise DecisionLoopError("not_applicable")
        newest = facts.newest.get(routine_id)
        return settings, routine, None if newest is None else newest.attempt_id

    def _checks_to_run(self, checks: CheckHost, workspace_id: str, local: datetime) -> CheckFolder:
        """The code folder `workspace_id` as one fresh look finds it when the user's Run checks
        may start its checks, or `not_applicable`. The plan brake and quiet hours are not checked:
        the user is present. The vault's policy is read live, as a cycle does. The folder's
        current commit needs its open item: the one the day's limit counted."""
        active = self._workspaces.resolve_active()
        settings = self._read_settings()
        paused = settings.paused_until is not None and settings.paused_until > local
        if (
            not checks.supported()
            or active.workspace.kind != "vault"
            or active.chat_only
            or not settings.enabled
            or paused
            or settings.grant_check not in ("ask", "allowed")
            or detectors._policy(self._workspaces, active.workspace) != "ordinary"
        ):
            raise DecisionLoopError("not_applicable")
        folder = checks.folder(workspace_id, local)
        if folder is None or folder.phase != "waiting" or not folder.pending:
            raise DecisionLoopError("not_applicable")
        item = self._agenda.item(
            active.workspace.workspace_id, item_key("commit_checks", workspace_id, folder.head)
        )
        if item is None or item.state != "open":
            raise DecisionLoopError("not_applicable")
        return folder

    def _triage_to_start(self, item_key: str, local: datetime) -> str:
        """The scope when the user's Suggest destinations may start the run for `item_key`, or
        `not_applicable`. The plan brake and quiet hours are not checked: the user is present.
        The vault's policy is read live, as a cycle does. The item must be open and a suggestion
        (a running, ready or failed one offers no start), the one the day's limit counted."""
        active = self._workspaces.resolve_active()
        settings = self._read_settings()
        paused = settings.paused_until is not None and settings.paused_until > local
        policy = detectors._policy(self._workspaces, active.workspace) or ""
        if (
            active.workspace.kind != "vault"
            or active.chat_only
            or not settings.enabled
            or paused
            or settings.grant_inbox_triage not in ("ask", "allowed")
            or "prepare" not in detectors.POLICY_WORK.get(policy, ())
        ):
            raise DecisionLoopError("not_applicable")
        scope = active.workspace.workspace_id
        item = self._agenda.item(scope, item_key)
        if (
            item is None
            or item.state != "open"
            or item.candidate not in INBOX_TRIAGE
            or item.kind != "suggest"
        ):
            raise DecisionLoopError("not_applicable")
        return scope

    def _mission_to_start(self, item_key: str, work: str) -> tuple[str, ItemRecord]:
        """The scope and its open item under `item_key`: `unknown_item` when the scope has none,
        `not_applicable` when it is closed or the active workspace is no vault whose policy,
        read live as a cycle does, allows `work`: an item raised before the policy changed
        still offers the work it no longer may."""
        active = self._workspaces.resolve_active()
        scope = active.workspace.workspace_id
        item = self._agenda.item(scope, item_key)
        if item is None:
            raise DecisionLoopError("unknown_item")
        policy = detectors._policy(self._workspaces, active.workspace) or ""
        if (
            item.state != "open"
            or active.workspace.kind != "vault"
            or work not in detectors.POLICY_WORK.get(policy, ())
        ):
            raise DecisionLoopError("not_applicable")
        return scope, item

    def _create_proposal(
        self, name: str, question: Mapping[str, object], now: datetime
    ) -> ProposalRecord:
        scope, chat_only = self._scope()
        if chat_only:
            raise DecisionLoopError("not_applicable")
        return self._engine.proposals.create(scope, name, question, now)

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        """The inspector's view: the proposal, the wording in use for its question and how the
        proposed one fared over its trial, from its creation to its first decision."""
        scope, _chat_only = self._scope()
        proposals = self._engine.proposals
        record = proposals.get(scope, proposal_id)
        if record.question is None:  # `get` always fills it; without one there is nothing to show
            raise DecisionLoopError("unknown_item")
        live, _version, _trial = proposals.wording(scope)
        decided = [parse_stamp(at) for at in (record.approved_at, record.rejected_at) if at]
        rows = self._stats.trial_rows(
            scope, since=parse_stamp(record.created_at), until=min(decided, default=None)
        )
        return wire.question_proposal_view(
            proposals.item(record),
            current=live.get(record.name) or playbook.questions()[record.name],
            proposed=record.question,
            tried=calibration.trial(rows, record.name),
        )

    def _act_proposal(
        self, proposal_id: str, action: str, revision: str, reason: str | None, now: datetime
    ) -> ProposalRecord:
        scope, _chat_only = self._scope()
        return self._engine.proposals.act(scope, proposal_id, action, revision, reason, now)

    def _act_change(
        self, proposal_id: str, action: str, revision: str, reason: str | None, now: datetime
    ) -> tuple[ChangeRecord, str | None]:
        """The act, then, when it moved the change's state, its episode's label. Only an
        approval or a rejection of an open change moves the revision; Draft again leaves it
        where the user saw it."""
        scope, _chat_only = self._scope()
        record, draft = self._engine.changes.act(scope, proposal_id, action, revision, reason, now)
        if record.revision != revision:
            with contextlib.suppress(DecisionLoopError):
                self._store.feedback(
                    scope,
                    episode_id=record.episode_id,
                    value="useful" if action == "approve" else "wrong",
                    reason=None,
                    source="page",
                    now=now,
                )
        return record, draft


# ---- helpers -----------------------------------------------------------------------------------


def _started(missions: Sequence[str], sessions: Sequence[str]) -> list[str]:
    """The launcher's `started`: up to half of `wire.MAX_STARTED` for each of the missions' chats
    and the decision sessions, newest first, and any share one leaves unused for the other.
    Neither list carries its start times, and a month of missions must not push every Bot run's
    chat off the list."""
    kept = list(missions[: max(wire.MAX_STARTED // 2, wire.MAX_STARTED - len(sessions))])
    return [*kept, *sessions[: wire.MAX_STARTED - len(kept)]]


def _places(item: ItemRecord) -> set[Opened]:
    """The chats and the notes, as (workspace_id, ref), that an item names in its evidence and
    its actions."""
    places: set[Opened] = set()
    for entry in (*item.body.evidence, *item.body.actions):
        if entry.session_id is not None:
            places.add(entry.session_id)
        if entry.workspace_id is not None and entry.ref is not None:
            places.add((entry.workspace_id, entry.ref))
    return places


def _questions(body: CallBody) -> list[tuple[str, str, str]]:
    """The call's answers as (name, label, answer text) in the playbook's order, own questions
    last; none when the call has none or they do not read back. An answer whose shape its
    question cannot word is left out."""
    if body.answers_json is None:
        return []
    try:
        answers, _reversal = decode_answers(body.answers_json)
    except ValueError:
        return []
    shown: list[tuple[str, str, str]] = []
    for name, label in playbook.LABELS.items():
        answer = answers.get(name)
        if answer is None:
            continue
        try:
            shown.append((name, label, playbook.answer_text(name, answer)))
        except TypeError, KeyError:
            # A score answer to a question without one; for an own question that is a KeyError,
            # since the playbook's ten hold no entry to read its levels from.
            continue
    return shown


def _largest_question(body: CallBody) -> int:
    """The estimated tokens of the call's state with the largest single question it asked: the
    split's state parts plus that question as the questions map of a request of its own."""
    state = sum(tokens for part, tokens in body.split if part != "questions")
    try:
        asked = json.loads(body.questions_text)
    except ValueError, RecursionError:
        asked = None
    questions = asked if isinstance(asked, dict) else {}
    sizes = [
        playbook.estimate_tokens(playbook.questions_text({name: question}))
        for name, question in questions.items()
    ]
    return state + max(sizes, default=0)
