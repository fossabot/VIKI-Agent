"""The inbox-triage item's pure part: one item per state of the inbox, from the first capture
without a suggested destination through the suggestion run that fills it.

Facts read: the inbox listing the cycle's shared scan gave (`Observation.inbox_listed`: each
capture's ref and modified stamp, the population and stamps `CaptureService.list` uses), the
`InboxLook` the inbox host remembers (its latest run, the stamps each capture was suggested at and
the item keys already started) and the `inbox` item's own facts (`InboxFacts`: the count and the
oldest `captured`). No capture text is read here.

Key: the listed state, every capture's ref and stamp, so a new or edited capture is a new item and
the same state keeps one item through its phases. A state is run once: the key is remembered as
started, and a run that began after the newest change has seen every capture, so one it left
unanswered, or one that failed, is not run again.

Phases (`contracts.TRIAGE_PHASES`): "waiting" before a run, "running" while it goes, "ready" once
it is done, "nothing" when it found nothing usable (`no_suggestions`) and "failed" otherwise,
including a start the host refused. A started key's phase comes from its own run (the latest run
when its id is the one the key was started with). Once another run has replaced it, the phase
comes from the kept suggestions: "ready" when any listed capture is still answered at its listed
stamp, else "failed".

Never send fails closed: the run's prompt carries capture text VIKI composes, the project names
under the projects folder, and capture tags are unread, so an unusable list, any tag rule, a
folder rule over a listed capture or a folder rule at or under the projects folder leaves no item.

What the model is sent: `state.build_state` sends the type words and `title`, plus `words`,
evidence and a `related_ref` when they exist. The item leaves `words` and `evidence` empty and
`related_ref` None and sets `title` to the type words, so the model sees the kind and nothing that
names a capture, its stamp, the phase, the scope or the key; those ride only in `facts` (the scope
and the phase), which `templates` reads for the user's own sentence.

Pure: no I/O, no clock read (`now` is passed in) and nothing logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from viki_agent.automation.brief import InboxFacts
from viki_agent.automation.routines import parse_stamp
from viki_agent.decision_loop import rules, words
from viki_agent.decision_loop.contracts import (
    INBOX_SETTLE,
    Candidate,
    DecisionSettings,
    InboxLook,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.state import _held, _rules, _segments
from viki_agent.vaults import captures


def candidates(
    look: InboxLook,
    listed: Sequence[tuple[str, str]],
    inbox: InboxFacts,
    scope: str,
    now: datetime,
    settings: DecisionSettings,
) -> list[Candidate]:
    """Zero or one `inbox_triage` item for the listed captures `listed` (ref, modified stamp).
    None for an empty listing or when never-send holds the run back. `facts`: "workspace_id" (the
    scope) and "phase". Only a waiting item may do work ("prepare")."""
    if not listed or _never_send_holds(listed, settings):
        return []
    key = state_key(scope, listed)
    phase = _phase(look, listed, inbox, key, now)
    if phase is None:
        return []
    title = words.type_words("inbox_triage")
    return [
        Candidate(
            item_key=key,
            type="inbox_triage",
            title=title,
            facts={"workspace_id": scope, "phase": phase},
            words={},
            evidence=(),
            related_chat=None,
            related_ref=None,
            deadline=None,
            shown_elsewhere=(),
            rules_evidence={},
            origin_session=None,
            work_allowed=("prepare",) if phase == "waiting" else (),
        )
    ]


def state_key(scope: str, listed: Sequence[tuple[str, str]]) -> str:
    """The item key of the inbox state `listed` (ref, modified stamp) in `scope`."""
    return item_key("inbox_triage", scope, canonical_json([[ref, stamp] for ref, stamp in listed]))


def _never_send_holds(listed: Sequence[tuple[str, str]], settings: DecisionSettings) -> bool:
    """Whether the run must not start: the list cannot be trusted, a tag rule exists (capture tags
    are not read, so no capture is known clear of it), a folder rule holds a listed capture or a
    folder rule sits at or under the projects folder, whose project names the prompt carries."""
    never_send = _rules(settings.never_send, settings.never_send_invalid)
    if never_send is None or never_send.tags:
        return True
    if any(folder[:1] == _segments(captures._PROJECTS) for folder in never_send.folders):
        return True
    return any(_held(never_send, ref, (), "") for ref, _ in listed)


def _phase(
    look: InboxLook,
    listed: Sequence[tuple[str, str]],
    inbox: InboxFacts,
    key: str,
    now: datetime,
) -> str | None:
    """The state's phase, None when it raises no item."""
    run = look.run
    running = run is not None and run.state == "running"
    if key in look.started:
        run_id = look.started[key]
        if run_id == "":
            return "failed"
        if run is None or run.run_id != run_id:
            # Another run has replaced this key's own run as the latest: the suggestions it left
            # kept are the only trace of how it went.
            answered = any(look.answered.get(ref) == stamp for ref, stamp in listed)
            return "ready" if answered else "failed"
        if running:
            return "running"
        if run.state == "done":
            return "ready"
        if run.state == "failed" and run.reason == "no_suggestions":
            return "nothing"
        return "failed"
    if running:  # while any run goes, no new item is raised
        return None
    newest = max(parse_stamp(stamp) for _, stamp in listed)
    oldest = None if inbox.oldest is None else parse_stamp(inbox.oldest)
    pressing = rules.decide("inbox", {"count": inbox.count, "oldest": oldest}, now, rules.Prefs())
    unanswered = any(look.answered.get(ref) != stamp for ref, stamp in listed)
    # A run that began after the newest change listed every capture, so what it left unanswered,
    # or a run that failed, is not run again for this state.
    seen = run is not None and parse_stamp(run.started_at) >= newest
    settled = now - newest >= INBOX_SETTLE
    due = pressing.reason == "inbox_pressure" and unanswered and settled and not seen
    return "waiting" if due else None
