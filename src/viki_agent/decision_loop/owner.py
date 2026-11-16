"""Rebuilds the user's side of the Decision state (`state.Owner`) from stored history.

`fill` takes the user's goals as they stand (kept untouched) and replaces the rest -- `recent`,
`muted_or_wrong` and `recent_feedback` -- from recently changed vault notes and the scope's
`entry='decision'` rows (`stats.DecisionLoopStats.owner_rows`, read by the caller), resolving
each row's `item_key` to a title through `title_of`. A key `title_of` no longer knows (the item
is gone, or the caller holds it back) is left out; every title that lands in the result is cut
to `state.py`'s own caps and to 120 characters -- never an excerpt, never note text.

Pure: no I/O, no clock reads, nothing logged; it imports no store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING

from viki_agent.decision_loop.state import (
    MUTED_OR_WRONG,
    RECENT,
    RECENT_FEEDBACK,
    Mention,
    Owner,
    Past,
)

if TYPE_CHECKING:
    from viki_agent.decision_loop.stats import OwnerRows

_TITLE_CHARS = 120


def fill(
    base: Owner,
    rows: OwnerRows,
    title_of: Callable[[str], Mention | None],
    changed: Sequence[Mention],
) -> Owner:
    """`base` with its goals kept and the rest rebuilt: `recent` is `changed` then `rows.seen`'s
    titles (via `title_of`), distinct by (title, ref), up to `state.RECENT`; `muted_or_wrong` is
    `rows.marked`'s titles up to `state.MUTED_OR_WRONG`; `recent_feedback` is `rows.feedback` as
    `Past`s up to `state.RECENT_FEEDBACK`. Order is preserved throughout (newest first, as *rows*
    and *changed* already run); a key `title_of` answers None for is skipped, uncounted."""
    seen = _resolve((item_key for item_key, _level in rows.seen), title_of)
    recent = _dedupe((*changed, *seen))[:RECENT]
    muted_or_wrong = list(_resolve(rows.marked, title_of))[:MUTED_OR_WRONG]
    recent_feedback = list(_past_mentions(rows.feedback, title_of))[:RECENT_FEEDBACK]
    return Owner(
        goals=base.goals,
        recent=tuple(_cut(mention) for mention in recent),
        muted_or_wrong=tuple(_cut(mention) for mention in muted_or_wrong),
        recent_feedback=tuple(
            Past(_cut(past.item), past.shown_as, past.outcome) for past in recent_feedback
        ),
    )


def _resolve(keys: Iterable[str], title_of: Callable[[str], Mention | None]) -> Iterator[Mention]:
    """Each key in *keys*, in order, as the `Mention` `title_of` answers for it; a key with no
    title is left out."""
    for key in keys:
        mention = title_of(key)
        if mention is not None:
            yield mention


def _past_mentions(
    rows: Iterable[tuple[str, str, str]], title_of: Callable[[str], Mention | None]
) -> Iterator[Past]:
    """Each (item_key, level, feedback) row, in order, as a `Past` whose `item` is the `Mention`
    `title_of` answers for its key; a key with no title is left out."""
    for item_key, level, feedback in rows:
        mention = title_of(item_key)
        if mention is not None:
            yield Past(mention, level, feedback)


def _dedupe(mentions: Iterable[Mention]) -> list[Mention]:
    """*mentions*, first occurrence of each distinct (title, ref) pair kept, in order."""
    seen: set[tuple[str, str | None]] = set()
    kept: list[Mention] = []
    for mention in mentions:
        key = (mention.title, mention.ref)
        if key not in seen:
            seen.add(key)
            kept.append(mention)
    return kept


def _cut(mention: Mention) -> Mention:
    """*mention* with its title cut to `_TITLE_CHARS` characters; its ref rides along unchanged.
    A title actually cut carries unknown tags from here on (`tags=None`): a never-send tag
    written inline past the cut would otherwise go unseen, so `state._held` must hold the entry
    back -- as it already does for any unknown-tagged entry -- rather than let the cut title
    through unchecked. An uncut title keeps its own tags, known or not."""
    if len(mention.title) <= _TITLE_CHARS:
        return mention
    return Mention(mention.title[:_TITLE_CHARS], mention.ref, None)
