"""Review fields: best-effort reads where any ambiguity collapses just that field to None."""

from __future__ import annotations

import re

import pytest

from viki_agent.vaults import note_metadata
from viki_agent.vaults.note_metadata import ReviewFields, extract_review


def review(text: str, *, complete: bool = True) -> ReviewFields:
    return extract_review(text.encode(), complete=complete)


def test_full_header_and_summary_paragraph_are_read():
    result = review(
        "---\n"
        "confidence: high\n"
        "created: 2026-08-18\n"
        'modified: "2026-10-20"\n'
        "---\n"
        "# Title\n\n"
        "## Summary\n"
        "First line.\n"
        "Second line.\n\n"
        "More body.\n"
    )
    assert result == ReviewFields("high", "2026-08-18", "2026-10-20", "First line. Second line.")


def test_no_header_reads_summary_from_the_whole_document():
    result = review("# Title\n\n## Summary\nPlain text here.\n")
    assert result == ReviewFields(None, None, None, "Plain text here.")


def test_unterminated_header_nulls_everything_including_summary():
    result = review("---\nconfidence: high\ncreated: 2026-08-18\n# never closes")
    assert result == ReviewFields(None, None, None, None)


def test_duplicate_key_nulls_only_that_field():
    result = review(
        "---\nconfidence: high\nconfidence: low\ncreated: 2026-08-18\n---\n## Summary\nText.\n"
    )
    assert (result.confidence, result.created) == (None, "2026-08-18")


def test_indented_lines_are_not_top_level_and_do_not_supply_fields():
    result = review("---\nother: |\n  created: 2026-08-18\n---\n")
    assert result.created is None


def test_header_fields_resolve_even_without_any_trailing_body():
    result = review("---\nconfidence: high\n---")
    assert (result.confidence, result.summary) == ("high", None)


def test_date_value_tolerates_surrounding_spaces_and_one_quote_pair():
    assert review("---\ncreated:   2026-08-18  \n---\n").created == "2026-08-18"
    assert review('---\ncreated: "2026-08-18"\n---\n').created == "2026-08-18"


@pytest.mark.parametrize(
    "value",
    ["2026-13-40", "2026-02-30", "08-18-2026", "2026/08/18", "bad", '"2026-08-18', '2026-08-18"'],
)
def test_malformed_or_impossible_dates_are_none(value):
    assert review(f"---\ncreated: {value}\n---\n").created is None


def test_confidence_accepts_quoted_and_plain_scalars():
    assert review("---\nconfidence: high\n---\n").confidence == "high"
    assert review('---\nconfidence: "  high  "\n---\n').confidence == "  high  "


@pytest.mark.parametrize("value", ["'quoted-wrong'", "123", "true", "-bad", "a,b"])
def test_unsupported_confidence_scalar_is_none(value):
    assert review(f"---\nconfidence: {value}\n---\n").confidence is None


def test_summary_heading_is_case_insensitive_with_trailing_spaces():
    assert review("---\n---\n##   SUMMARY   \nHello world.\n").summary == "Hello world."


def test_summary_skips_leading_blanks_then_stops_at_next_blank():
    result = review("---\n---\n## Summary\n\n\nFirst.\nSecond.\n\nIgnored paragraph.\n")
    assert result.summary == "First. Second."


def test_summary_stops_at_next_heading_or_fence_without_a_blank_line():
    assert review("---\n---\n## Summary\nFirst.\n# Next heading\n").summary == "First."
    assert review("---\n---\n## Summary\nFirst.\n```\ncode\n```\n").summary == "First."


def test_summary_ignores_a_decoy_heading_inside_a_fenced_block():
    result = review("---\n---\n```\n## Summary\nhidden\n```\n## Summary\nReal text.\n")
    assert result.summary == "Real text."


def test_summary_strips_one_leading_list_or_quote_marker_per_line():
    result = review("---\n---\n## Summary\n- First\n* Second\n> Third\n")
    assert result.summary == "First Second Third"


def test_summary_resolves_wikilinks_and_drops_emphasis_markup():
    result = review(
        "---\n---\n## Summary\nSee [[Target]] and [[Target|Display]], **bold** __x__ `code`.\n"
    )
    assert result.summary == "See Target and Display, bold x code."


def test_summary_link_patterns_exclude_brackets_from_every_class():
    # Each capturing/negated class must exclude `[` and `]`, or a failed match on a bracket-heavy
    # line backtracks to the end of the line instead of stopping at the next one.
    negated = re.compile(r"\[\^(?:\\.|[^\]\\])*\]")
    for pattern in (note_metadata.SUMMARY_LINK_ALIAS, note_metadata.SUMMARY_LINK_PLAIN):
        classes = negated.findall(pattern.pattern)
        assert classes
        for cls in classes:
            assert all(re.fullmatch(cls, ch) is None for ch in "[]"), cls


def test_summary_resolves_a_wikilink_nested_inside_another():
    result = review("---\n---\n## Summary\n[[a [[b|c]] d]]\n")
    assert result.summary == "a c d"


def test_summary_on_a_long_run_of_open_brackets_stays_exact_and_fast():
    result = review("---\n---\n## Summary\n" + "[[" * 16000 + "\n")
    assert result.summary == "[" * 200 + "…"


def test_summary_collapses_whitespace():
    result = review("---\n---\n## Summary\nA   B\tC\n")
    assert result.summary == "A B C"


def test_summary_with_remaining_control_character_is_none():
    result = review("---\n---\n## Summary\nbad\x00text\n")
    assert result.summary is None


def test_summary_without_a_late_space_hard_cuts_at_200():
    text = "x" * 250
    result = review(f"---\n---\n## Summary\n{text}\n")
    assert result.summary == "x" * 200 + "…"


def test_summary_with_a_late_space_cuts_there_instead():
    words = " ".join("word" for _ in range(60))
    result = review(f"---\n---\n## Summary\n{words}\n")
    # The window is text[:200]; its last space is after a 40th "word" (index 199), so the cut
    # keeps exactly 40 words and drops the rest, rather than hard-cutting mid-window at 200.
    assert result.summary == " ".join(["word"] * 40) + "…"


def test_no_summary_heading_or_empty_summary_body_is_none():
    assert review("---\n---\nNo heading here.\n").summary is None
    assert review("---\n---\n## Summary\n").summary is None
    assert review("---\n---\n## Summary\n\n\n").summary is None


def test_invalid_utf8_raises_the_same_unicode_decode_error_as_extract_display():
    with pytest.raises(UnicodeDecodeError):
        extract_review(b"---\ntitle: A\n---\npartial \xff", complete=False)
