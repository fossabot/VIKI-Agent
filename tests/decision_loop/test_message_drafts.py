"""message_drafts.py: the handoff and literature-note offers, their keys and what the model is
sent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from viki_agent.decision_loop import contracts, message_drafts, words
from viki_agent.decision_loop.contracts import (
    ContextHint,
    DecisionSettings,
    PageFacts,
    SourceHint,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.state import History, Now, Owner, build_state

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SCOPE = "scope-x"
SESSION = "fe07f4bf-5447-4a8d-9d58-b68385409b99"
OTHER = "0b1c2d3e-5447-4a8d-9d58-b68385409b99"
TURN = "a1b2c3d4-5447-4a8d-9d58-b68385409b99"
LAUNCHER = "11111111-5447-4a8d-9d58-b68385409b99"


def _page(context: ContextHint | None = None, sources: SourceHint | None = None) -> PageFacts:
    return PageFacts(0, 0, 0, "clean", None, None, None, None, context=context, sources=sources)


def _handoff(percent: int, session: str = SESSION) -> list[contracts.Candidate]:
    return message_drafts.offers(_page(context=ContextHint(session, percent)), SCOPE, LAUNCHER)


def _source(
    notes: bool = False, session: str = SESSION, turn: str = TURN
) -> list[contracts.Candidate]:
    return message_drafts.offers(_page(sources=SourceHint(session, turn, notes)), SCOPE, LAUNCHER)


def test_a_handoff_is_offered_from_seventy_percent() -> None:
    assert contracts.CONTEXT_NEARLY_FULL == 70
    assert _handoff(69) == []
    for percent in (70, 75, 100):
        (offer,) = _handoff(percent)
        assert offer.type == "context_handoff"


def test_a_handoff_offer_carries_its_facts_and_nothing_for_the_model() -> None:
    (offer,) = _handoff(75)
    assert offer.title == words.type_words("context_handoff")
    assert dict(offer.facts) == {"workspace_id": SCOPE, "session": SESSION}
    assert dict(offer.words) == {}
    assert offer.evidence == ()
    assert offer.related_chat == SESSION
    assert offer.related_ref is None
    assert offer.deadline is None
    assert offer.shown_elsewhere == ("chat",)
    assert dict(offer.rules_evidence) == {}
    assert offer.origin_session is None
    assert offer.work_allowed == ()


@pytest.mark.parametrize("notes", [True, False])
def test_a_source_note_is_offered_for_a_reply_that_fetched_pages(notes: bool) -> None:
    (offer,) = _source(notes)
    assert offer.type == "source_note"
    assert offer.title == words.type_words("source_note")
    assert dict(offer.facts) == {"workspace_id": SCOPE, "session": SESSION, "notes": notes}
    assert dict(offer.words) == {}
    assert offer.evidence == ()
    assert offer.related_chat == SESSION
    assert offer.related_ref is None
    assert offer.deadline is None
    assert offer.shown_elsewhere == ("chat",)
    assert dict(offer.rules_evidence) == {}
    assert offer.origin_session is None
    assert offer.work_allowed == ()


def test_both_offers_come_in_message_draft_order() -> None:
    page = _page(ContextHint(SESSION, 80), SourceHint(SESSION, TURN, False))
    found = message_drafts.offers(page, SCOPE, LAUNCHER)
    assert tuple(o.type for o in found) == contracts.MESSAGE_DRAFTS


def test_no_message_draft_for_vikis_own_chat_or_without_a_page() -> None:
    assert message_drafts.offers(None, SCOPE, LAUNCHER) == []
    assert message_drafts.offers(_page(), SCOPE, LAUNCHER) == []
    assert _handoff(90, session=LAUNCHER) == []
    assert _source(session=LAUNCHER) == []
    # An empty launcher session (none known) matches no chat.
    assert len(message_drafts.offers(_page(context=ContextHint(SESSION, 90)), SCOPE, "")) == 1


def test_message_draft_keys_name_the_chat_and_the_reply() -> None:
    (handoff,) = _handoff(75)
    assert handoff.item_key == item_key("context_handoff", SESSION)
    assert _handoff(95)[0].item_key == handoff.item_key
    assert _handoff(75, session=OTHER)[0].item_key != handoff.item_key
    (source,) = _source()
    assert source.item_key == item_key("source_note", SESSION, TURN)
    assert _source(notes=True)[0].item_key == source.item_key
    assert _source(turn=OTHER)[0].item_key != source.item_key
    assert _source(session=OTHER)[0].item_key != source.item_key
    assert source.item_key != handoff.item_key


def test_a_message_draft_sends_only_its_kind_to_the_model() -> None:
    page = _page(ContextHint(SESSION, 75), SourceHint(SESSION, TURN, False))
    now = Now(None, False, False, NOW, 0)
    sends = zip(
        message_drafts.offers(page, SCOPE, LAUNCHER),
        ("handoff for a chat that is nearly full", "literature notes for fetched web pages"),
        strict=True,
    )
    for candidate, type_words in sends:
        settings = DecisionSettings()
        sent = canonical_json(build_state(candidate, Owner(), now, History(), settings).state)
        assert type_words in sent
        # The source note's type words hold the word "notes", so the key is what is checked.
        for private in (SESSION, TURN, SCOPE, "75", '"notes"', "workspace_id", "session"):
            assert private not in sent, private
