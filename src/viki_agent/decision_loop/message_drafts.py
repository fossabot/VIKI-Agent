"""The message-draft offers' pure part: a handoff for a nearly full chat and literature notes for
the web pages a reply fetched.

Facts read: `PageFacts.context` (`ContextHint`: the chat and its whole context percent) and
`PageFacts.sources` (`SourceHint`: the chat, the reply's turn id and whether the reply read a note
in the vault), both reported by the chat in view. Nothing else is read: no chat text, no URL, no
note ref.

Keys: a handoff's names the chat, so one chat is offered one handoff; a source note's names the
chat and the reply, so each reply that fetched pages is offered once.

What the model is sent: `state.build_state` sends the type words and `title`, plus `words`, evidence
and a `related_ref` when they exist. The offers leave `words` and `evidence` empty and `related_ref`
None and set `title` to the type words, so the model sees the kind and nothing that names a chat,
reply, workspace or percent; those ride only in `facts` (and `related_chat`), which `templates`
reads for the user's own sentence and the draft's chat.

Pure: no I/O, no clock read and nothing logged. The caller gates by grant and policy and asks
whether the chat is the user's; this module only skips VIKI's own chat (`launcher_session`).
"""

from __future__ import annotations

from viki_agent.decision_loop import words
from viki_agent.decision_loop.contracts import (
    CONTEXT_NEARLY_FULL,
    Candidate,
    PageFacts,
    item_key,
)


def offers(page: PageFacts | None, scope: str, launcher_session: str) -> list[Candidate]:
    """The `context_handoff` offer when the chat in view's context share reaches
    `CONTEXT_NEARLY_FULL`, then the `source_note` offer when its last reply fetched pages, in
    `MESSAGE_DRAFTS` order. None for a missing page or for `launcher_session`. `facts`:
    "workspace_id" (the scope) and "session"; a source note adds "notes"."""
    if page is None:
        return []
    found: list[Candidate] = []
    context = page.context
    if context and context.session != launcher_session and context.percent >= CONTEXT_NEARLY_FULL:
        found.append(
            _offer(
                "context_handoff",
                item_key("context_handoff", context.session),
                {"workspace_id": scope, "session": context.session},
                context.session,
            )
        )
    sources = page.sources
    if sources and sources.session != launcher_session:
        found.append(
            _offer(
                "source_note",
                item_key("source_note", sources.session, sources.turn),
                {"workspace_id": scope, "session": sources.session, "notes": sources.notes},
                sources.session,
            )
        )
    return found


def _offer(candidate_type: str, key: str, facts: dict[str, object], session: str) -> Candidate:
    title = words.type_words(candidate_type)
    return Candidate(
        item_key=key,
        type=candidate_type,
        title=title,
        facts=facts,
        words={},
        evidence=(),
        related_chat=session,
        related_ref=None,
        deadline=None,
        shown_elsewhere=("chat",),
        rules_evidence={},
        origin_session=None,
        work_allowed=(),
    )
