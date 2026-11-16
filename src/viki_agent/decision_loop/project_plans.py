"""The project plans' pure parts: the stalled-project and overdue-tasks offers, and the finding
for a plan that waits.

Facts read: `stalled_projects` reads each `Goal` (`ref`, `title`, `tags`, `touched`, the newest
change of any note in the project's folder) and `overdue_tasks` each `TaskItem` of `TaskFacts.items`
(`ref`, `text`, `due`, `tags`). A project is stalled once `now - touched` reaches `PROJECT_STALL`; a
task is worth checking once `today - due` reaches `OVERDUE_CHECK_AFTER`. Nothing else is read: no
note text, no frontmatter beyond what `Goal` already holds.

Keys: a stalled project's names its ref and the stamp of `touched`, so one stall is offered once
and a later change makes a new stall; an overdue file's names its ref and the sorted "due text" of
its overdue tasks, so it is offered again only when that set changes.

Never-send and `admitted`: a note is left out unless `second_looks.admitted` takes its ref (the
rule the brief applies too), and a project or tasks file is left out when `state._rules` cannot be
trusted (an unusable list) or `state._held` holds it back: the goal by its ref, tags and title, a
task by its ref, tags and text. The plan chat also reads the file and the project's other notes,
so two more rules hold a plan back. A never-send folder at or under the project's folder
`10-projects/<slug>/` (the folder itself or one inside it) holds the project and its tasks file.
One held task, overdue or not, leaves out its whole file. Unknown tags (None) are held back under
any tag rule. The gap that remains: a tag on a note or task line VIKI does not list is not seen,
since VIKI reads no more of the vault.

What the model is sent: `state.build_state` sends the type words and `title`, plus `words`,
evidence and a `10-projects/` `related_ref` when they exist. The offers and the finding leave
`words` and `evidence` empty and `related_ref` None and set `title` to the type words, so the model
sees the kind and nothing that names a project, note, chat, mission or workspace; those ride only
in `facts`, which `templates` reads for the user's own sentence.

Pure: no I/O, no clock read (`now` and `today` are passed in) and nothing logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from pathlib import PurePosixPath

from viki_agent.automation.brief import Goal, TaskFacts, TaskItem
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import words
from viki_agent.decision_loop.contracts import (
    MAX_PROJECT_PLANS,
    OVERDUE_CHECK_AFTER,
    PROJECT_STALL,
    Candidate,
    DecisionSettings,
    Finding,
    item_key,
)
from viki_agent.decision_loop.second_looks import admitted
from viki_agent.decision_loop.state import _held, _Rules, _rules, _segments


def stalled_projects(
    goals: Sequence[Goal], scope: str, now: datetime, settings: DecisionSettings
) -> list[Candidate]:
    """One offer per goal none of whose notes changed for `PROJECT_STALL`: its `touched` is set,
    its ref admitted and neither it nor its title held back by never-send, nor a never-send folder
    inside its project folder. Most recently touched
    first, ties by ref, at most `MAX_PROJECT_PLANS`. `facts`: "workspace_id" (the scope), "ref" and
    "project" (the goal's title, else its folder's name)."""
    rules = _rules(settings.never_send, settings.never_send_invalid)
    if rules is None:
        return []
    stalled = [
        (goal, goal.touched)
        for goal in goals
        if goal.touched is not None
        and now - goal.touched >= PROJECT_STALL
        and admitted(goal.ref)
        and not _held(rules, goal.ref, goal.tags, goal.title or "")
        and not _folder_inside(rules, goal.ref)
    ]
    stalled.sort(key=lambda pair: (-pair[1].timestamp(), pair[0].ref))
    return [
        _offer(
            "stalled_project",
            item_key("stalled_project", goal.ref, stamp(touched)),
            scope,
            goal.ref,
            goal.title or _folder(goal.ref),
        )
        for goal, touched in stalled[:MAX_PROJECT_PLANS]
    ]


def overdue_tasks(
    tasks: TaskFacts, scope: str, today: date, settings: DecisionSettings
) -> list[Candidate]:
    """One offer per tasks file with an item `OVERDUE_CHECK_AFTER` or more past due, in the order
    the files first appear in `tasks.items`. A file is left out when its ref is not admitted,
    never-send holds back any of its items (overdue or not) or a never-send folder lies inside its
    project folder. `facts`: "workspace_id", "ref" and "project" (the
    folder's name)."""
    rules = _rules(settings.never_send, settings.never_send_invalid)
    if rules is None:
        return []
    by_file: dict[str, list[TaskItem]] = {}
    held_files: set[str] = set()
    for item in tasks.items:
        if _held(rules, item.ref, item.tags, item.text):
            held_files.add(item.ref)
        if today - item.due >= OVERDUE_CHECK_AFTER:
            by_file.setdefault(item.ref, []).append(item)
    return [
        _offer(
            "overdue_tasks",
            item_key("overdue_tasks", ref, *sorted(f"{i.due.isoformat()} {i.text}" for i in items)),
            scope,
            ref,
            _folder(ref),
        )
        for ref, items in by_file.items()
        if admitted(ref) and ref not in held_files and not _folder_inside(rules, ref)
    ]


def finding(found: Finding) -> Candidate:
    """The item for a project plan that waits in chat `found.session_id`; it starts no work.
    `found.title` is the project's name, which rides in `facts["project"]` only."""
    title = words.type_words(found.look)
    return Candidate(
        item_key=item_key(found.look, found.mission_id, "found"),
        type=found.look,
        title=title,
        facts={
            "workspace_id": found.scope,
            "found": found.kind,
            "session": found.session_id,
            "project": found.title,
        },
        words={},
        evidence=(),
        related_chat=found.session_id,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"found": found.kind},
        origin_session=None,
        work_allowed=(),
    )


def _folder_inside(rules: _Rules, ref: str) -> bool:
    """Whether a never-send folder is the project folder of `ref` (`10-projects/<slug>/`) or lies
    inside it. The plan reads the project's other notes, so such a folder holds the plan back."""
    project = _segments(ref)[:2]
    return any(folder[: len(project)] == project for folder in rules.folders)


def _folder(ref: str) -> str:
    """The name of the project folder a ref sits in."""
    return PurePosixPath(ref).parent.name


def _offer(candidate_type: str, key: str, scope: str, ref: str, project: str) -> Candidate:
    title = words.type_words(candidate_type)
    return Candidate(
        item_key=key,
        type=candidate_type,
        title=title,
        facts={"workspace_id": scope, "ref": ref, "project": project},
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={},
        origin_session=None,
        work_allowed=("prepare",),
    )
