"""The saved-check item's pure part: one item per code folder commit whose saved checks the
user has run in that folder before.

Facts read: `CheckLook.folders` (`CheckFolder`: the folder's id and name, its HEAD, the phase of its
checks at that commit and the ids of those with no run there), read by the check host. Nothing else
is read: no check name, command or output.

Keys: the folder and the commit, not the scope, so a folder's new commit is a new item and the same
commit keeps one item through its phases.

What the model is sent: `state.build_state` sends the type words and `title`, plus `words`, evidence
and a `related_ref` when they exist. The item leaves `words` and `evidence` empty and `related_ref`
None and sets `title` to the type words, so the model sees the kind and nothing that names a folder,
commit, check or phase; those ride only in `facts`, which `templates` reads for the user's own
sentence and the Open in Code and Run checks actions.

Pure: no I/O, no clock read and nothing logged.
"""

from __future__ import annotations

from viki_agent.decision_loop import words
from viki_agent.decision_loop.contracts import (
    MAX_CHECK_FOLDERS,
    Candidate,
    CheckLook,
    item_key,
)


def candidates(look: CheckLook, scope: str) -> list[Candidate]:
    """The `commit_checks` items of `look`'s folders in id order, at most `MAX_CHECK_FOLDERS`; none
    for a look that could not list them. `facts`: "workspace_id" (the scope), "folder_id",
    "folder" (its name), "head", "phase" and "pending". Only a waiting folder may do work ("run");
    `shown_elsewhere` stays empty because no other page says a commit has no run."""
    title = words.type_words("commit_checks")
    folders = sorted(look.folders or (), key=lambda folder: folder.workspace_id)
    return [
        Candidate(
            item_key=item_key("commit_checks", folder.workspace_id, folder.head),
            type="commit_checks",
            title=title,
            facts={
                "workspace_id": scope,
                "folder_id": folder.workspace_id,
                "folder": folder.name,
                "head": folder.head,
                "phase": folder.phase,
                "pending": folder.pending,
            },
            words={},
            evidence=(),
            related_chat=None,
            related_ref=None,
            deadline=None,
            shown_elsewhere=(),
            rules_evidence={},
            origin_session=None,
            work_allowed=("run",) if folder.phase == "waiting" else (),
        )
        for folder in folders[:MAX_CHECK_FOLDERS]
    ]
