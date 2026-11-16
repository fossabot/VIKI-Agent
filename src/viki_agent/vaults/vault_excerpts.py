"""What a dispatch does with the excerpt parts of one durable turn.

`resolve_excerpts` reads each attached span again, immediately before the turn is echoed and sent,
and refuses the whole turn when a note changed under a part: the bytes that reach `claude` are the
bytes the chip previewed, or nothing at all. Each refusal names the part it is about (`index`).

`joined_text` is the one place the order is decided: the user's own words come first, so the
stream's 16,384-character clip can only eat the end of the citations, never the question.
`check_block_budget` keeps one turn's citations inside their character budget and the joined text
inside the turn's byte limit, before anything is emitted or sent.

Every function here reads notes through `VaultService.excerpt_at`, so a caller holding the event
loop must run them in a thread.
"""

from __future__ import annotations

from collections.abc import Sequence

from viki_agent.conversations.turn_parts import (
    EXCERPT_BLOCK_CHARS,
    ExcerptPart,
    render_excerpt_block,
)
from viki_agent.domain import CanonicalRoot
from viki_agent.turn_input import MAX_TEXT_BYTES
from viki_agent.vaults.vault import VaultError, VaultService


def resolve_excerpts(
    vault: VaultService, active_workspace_id: str, parts: Sequence[ExcerptPart]
) -> list[str]:
    """One rendered block per part, in the turn's order. Blocking: one root resolve, then one
    bounded note read per part.

    Refusals, each with the part's `index`: `excerpt_wrong_vault` for a part of another vault,
    `excerpt_stale` for a note that changed under the span, `excerpt_unavailable` for anything the
    reader refused.
    """
    blocks: list[str] = []
    root: CanonicalRoot | None = None
    for index, part in enumerate(parts):
        if part.vault_id != active_workspace_id:
            raise VaultError("excerpt_wrong_vault", index=index)
        try:
            if root is None:  # once for the call, after the first part's vault check
                root = vault.active_root(active_workspace_id)
            excerpt = vault.excerpt_at(
                root, active_workspace_id, part.ref, part.start_line, part.end_line
            )
        except VaultError as exc:
            # A span now past the end of the note is the note changing, not a broken request.
            code = "excerpt_stale" if exc.code == "invalid_range" else "excerpt_unavailable"
            raise VaultError(code, index=index) from exc
        if excerpt.part["sha256"] != part.sha256:
            raise VaultError("excerpt_stale", index=index)
        # Rendered from the part's own ref, so the block is the one the chip showed the user.
        blocks.append(render_excerpt_block(part.ref, part.start_line, part.end_line, excerpt.text))
    return blocks


def joined_text(text: str, blocks: Sequence[str]) -> str:
    """The text one turn sends: the user's words first, then one block per excerpt."""
    return "\n".join([t for t in (text, *blocks) if t])


def check_block_budget(text: str, blocks: Sequence[str]) -> None:
    """Refuse `excerpts_too_large` before any emit or send: a turn's citations stay parseable and
    its text stays inside the limit every durable row and provider write already has."""
    if sum(len(block) for block in blocks) > EXCERPT_BLOCK_CHARS:
        raise VaultError("excerpts_too_large")
    if len(joined_text(text, blocks).encode("utf-8")) > MAX_TEXT_BYTES:
        raise VaultError("excerpts_too_large")
