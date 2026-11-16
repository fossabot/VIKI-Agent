"""Permanent scalar/header contracts; tags and headings have separate serial witnesses."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from viki_agent.vaults.note_metadata import extract_display


def header(text: str):
    return extract_display(f"---\n{text}\n---".encode(), complete=True)


def test_exact_fields_authored_title_precedence_and_source_bytes_are_preserved():
    source = (
        '\ufeff---\r\nname: Backup\r\ntitle: "  SKILL  "\r\ntype: note\r\nstatus: approved\r\n---'
    ).encode()
    original = bytes(source)
    result = extract_display(source, complete=True)
    assert set(asdict(result)) == {
        "state",
        "frontmatter_state",
        "heading_state",
        "display_title",
        "title_source",
        "tags",
        "type",
        "status",
    }
    assert (result.frontmatter_state, result.display_title, result.title_source) == (
        "ready",
        "  SKILL  ",
        "title",
    )
    assert (result.tags, result.type, result.status) == ([], "note", "approved")
    expected = (
        "unsupported"
        if "unsupported" in (result.frontmatter_state, result.heading_state)
        else "truncated"
        if "truncated" in (result.frontmatter_state, result.heading_state)
        else "ready"
    )
    assert result.state == expected  # Allows the separately owned heading stage to advance.
    assert source == original


def test_empty_title_uses_name_and_json_preserves_literal_codepoints():
    result = header('title: "   "\nname: "<img>é\u2028名</img>"\ntype: " "\nstatus:')
    assert (result.display_title, result.title_source) == ("<img>é\u2028名</img>", "name")
    assert (result.type, result.status) == (None, None)
    assert (
        header("title: Équipe 2 (North)/Run_book.md").display_title
        == "Équipe 2 (North)/Run_book.md"
    )


def test_exact_delimiters_and_eof_versus_prefix_closure():
    for opening in (" ---", "--- "):
        assert (
            extract_display(
                f"{opening}\ntitle: Hidden\n---".encode(), complete=True
            ).frontmatter_state
            == "absent"
        )
    assert extract_display(b"---\rtitle: Hidden\n---", complete=True).frontmatter_state == "absent"
    assert header("title: A").display_title == "A"
    for complete, expected in ((True, "unsupported"), (False, "truncated")):
        result = extract_display(b"---\ntitle: A\n--- ", complete=complete)
        assert result.frontmatter_state == expected and result.display_title is None
    assert extract_display(b"", complete=False).frontmatter_state == "truncated"
    assert extract_display(b"", complete=True).frontmatter_state == "absent"


def test_non_eof_partial_line_is_discarded_but_only_trailing_utf8_cut_is_tolerated():
    assert extract_display(b"---\ntitle: A\n---", complete=False).frontmatter_state == "truncated"
    prefix = b"---\ntitle: A\n---\npartial \xe3"
    assert extract_display(prefix, complete=False).display_title == "A"
    with pytest.raises(UnicodeDecodeError):
        extract_display(prefix, complete=True)


@pytest.mark.parametrize(
    "prefix", [b"---\ntitle: A\n---\npartial \xff", b"---\ntitle: A\n---\n" + b"\n" * 256 + b"\xff"]
)
def test_invalid_utf8_cannot_hide_in_discarded_partial_or_line_surplus(prefix):
    with pytest.raises(UnicodeDecodeError):
        extract_display(prefix, complete=False)


def test_byte_and_complete_line_caps_do_not_admit_an_unobserved_closing_marker():
    exact = b"---\n#" + b"a" * (32768 - 9) + b"\n---"
    assert len(exact) == 32768
    assert extract_display(exact, complete=True).frontmatter_state == "ready"
    assert extract_display(exact + b"\nbody", complete=True).frontmatter_state == "truncated"
    assert (
        extract_display(b"---\n" + b"\n" * 254 + b"---", complete=True).frontmatter_state == "ready"
    )
    assert (
        extract_display(b"---\n" + b"\n" * 255 + b"---", complete=True).frontmatter_state
        == "truncated"
    )


@pytest.mark.parametrize(
    "value",
    [
        "'title'",
        "123",
        "2026-10-04",
        "TRUE",
        "null",
        "yes",
        "a # comment",
        "&anchor",
        "*alias",
        "|",
        "-title",
        "a,b",
    ],
)
def test_unsupported_scalar_clears_all_header_values(value):
    result = header(f"name: Backup\ntype: note\nstatus: approved\ntitle: {value}")
    assert result.frontmatter_state == "unsupported"
    assert (result.display_title, result.title_source, result.tags, result.type, result.status) == (
        None,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("value", ["\x00", "\x7f", "\x85", "\ud800", "a\nb"])
def test_json_controls_and_surrogates_are_unsupported(value):
    assert header(f"title: {json.dumps(value)}").frontmatter_state == "unsupported"


@pytest.mark.parametrize(
    "field,limit", [("title", 512), ("name", 512), ("type", 64), ("status", 64)]
)
def test_scalar_bounds_count_unicode_codepoints(field, limit):
    value = "🧠" * limit
    result = header(f"{field}: {json.dumps(value)}")
    assert result.frontmatter_state == "ready"
    assert (result.display_title if field in {"title", "name"} else getattr(result, field)) == value
    assert header(f"{field}: {json.dumps(value + 'x')}").frontmatter_state == "unsupported"


@pytest.mark.parametrize(
    "body",
    [
        "title: A\ntitle: B",
        "name: A\nname: B",
        "  title: Nested",
        "title: A\nmalformed",
        'title: A\ntags: ["tag", 2]',
    ],
)
def test_duplicate_malformed_and_ambiguous_headers_discard_authored_values(body):
    result = header(body)
    assert result.frontmatter_state == "unsupported"
    assert result.display_title is None and result.tags is None
