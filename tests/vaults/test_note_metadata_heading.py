"""First supported H1, bounded fences and independent metadata coverage."""

from __future__ import annotations

import pytest

from viki_agent.vaults.note_metadata import extract_display


def project(text: str, *, complete: bool = True):
    return extract_display(text.encode(), complete=complete)


@pytest.mark.parametrize("separator", [" ", "\t"])
def test_h1_preserves_literal_remainder_and_source_bytes(separator):
    title = " **é** <img>名</img> #  "
    source = f"\ufeff#{separator}{title}\r\n# Later".encode()
    original = bytes(source)
    result = extract_display(source, complete=True)
    assert (result.state, result.heading_state, result.display_title, result.title_source) == (
        "ready",
        "ready",
        title,
        "h1",
    )
    assert result.frontmatter_state == "absent" and result.tags == []
    assert source == original


@pytest.mark.parametrize(
    "text", ["# ", "#   ", "#\t\tbad", "# bad\x00", "# bad\x85", "# " + "🧠" * 513]
)
def test_first_recognized_invalid_h1_blocks_a_later_better_title(text):
    result = project(f"{text}\n# Later")
    assert result.heading_state == result.state == "unsupported"
    assert result.display_title is None and result.title_source is None


def test_h1_codepoint_bound_and_literal_inline_escapes():
    title = "🧠" * 512
    assert project(f"# {title}").display_title == title
    assert project(r"# \ud800 **raw**").display_title == r"\ud800 **raw**"
    assert project("# é\u2028名").display_title == "é\u2028名"


@pytest.mark.parametrize(
    "ignored", ["#NoSpace", "## H2", " # Indented", "    # Code", "\t# Code", "    ```"]
)
def test_only_column_zero_ascii_separator_h1_is_selected(ignored):
    assert project(f"{ignored}\n# First\n# Later").display_title == "First"


@pytest.mark.parametrize(
    "header,title,source",
    [
        ("title: SKILL\nname: Backup", "SKILL", "title"),
        ('title: " "\nname: Backup', "Backup", "name"),
        ("title:\nname:", "Body", "h1"),
    ],
)
def test_header_precedence_and_header_comments_do_not_supply_h1(header, title, source):
    result = project(f"---\n# Header comment\n{header}\n---\n# Body")
    assert result.heading_state == "ready" and result.frontmatter_state == "ready"
    assert (result.display_title, result.title_source) == (title, source)


@pytest.mark.parametrize(
    "text,complete,state",
    [
        ("---\ntitle: 'bad'\n---\n# Body", True, "unsupported"),
        ("---\ntitle: A\n# Body\n", True, "unsupported"),
        ("---\ntitle: A\n# Body\n", False, "truncated"),
    ],
)
def test_untrusted_header_prevents_heading_fallback(text, complete, state):
    result = project(text, complete=complete)
    assert result.frontmatter_state == result.heading_state == result.state == state
    assert (result.display_title, result.title_source, result.tags, result.type, result.status) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_closed_header_coverage_is_independent_of_an_unresolved_heading():
    result = project("---\ntags: []\n---\nbody\n", complete=False)
    assert (result.frontmatter_state, result.tags, result.heading_state, result.state) == (
        "ready",
        [],
        "truncated",
        "truncated",
    )
    result = project("---\ntitle: Author\ntype: note\n---\n```\n# Hidden")
    assert (result.heading_state, result.state, result.display_title, result.tags, result.type) == (
        "unsupported",
        "unsupported",
        "Author",
        [],
        "note",
    )


@pytest.mark.parametrize(
    "text,state",
    [
        ("", "absent"),
        ("ordinary text", "absent"),
        ("---\n---", "absent"),
        ('---\ntags: ["a"]\n---', "ready"),
        ("---\nstatus: active\n---", "ready"),
    ],
)
def test_eof_without_h1_has_exact_value_correlation(text, state):
    result = project(text)
    assert result.heading_state == "absent" and result.state == state
    assert result.display_title is None and result.title_source is None


@pytest.mark.parametrize(
    "text",
    [
        "```lang\n# Hidden\n```\n# Visible",
        "  ~~~~opaque\n# Hidden\n   ~~~~~ \t\n# Visible",
        "```opaque`info\n# Hidden\n```\n# Visible",
        "````\n# Hidden\n```\n# Still hidden\n````\n# Visible",
        "~~~\n# Hidden\n```\n# Still hidden\n~~~\n# Visible",
        "```\n# Hidden\n```tail\n# Still hidden\n```\n# Visible",
        "```\n    ```\n# Hidden\n```\n# Visible",
    ],
)
def test_fence_contents_and_unqualified_closers_never_become_labels(text):
    result = project(text)
    assert (result.heading_state, result.display_title) == ("ready", "Visible")


@pytest.mark.parametrize("fence", ["```", "~~~"])
@pytest.mark.parametrize("complete,state", [(True, "unsupported"), (False, "truncated")])
def test_unclosed_fence_before_h1_is_unknown_at_a_cut_and_unsupported_at_eof(
    fence, complete, state
):
    result = project(f"{fence}\n# Hidden\n", complete=complete)
    assert result.heading_state == result.state == state
    assert result.frontmatter_state == "absent" and result.tags == []
    assert result.display_title is None


def test_first_supported_h1_stops_before_later_unclosed_fence():
    result = project("# First\n```\n# Hidden", complete=False)
    assert (result.heading_state, result.state, result.display_title) == ("ready", "ready", "First")


def test_final_eof_line_and_byte_line_caps_have_honest_heading_coverage():
    exact = b"body\n" + b"a" * (32768 - 12) + b"\n# Last"
    assert len(exact) == 32768
    assert extract_display(exact, complete=True).display_title == "Last"
    for prefix, complete in [(exact, False), (exact + b"\nmore", True)]:
        result = extract_display(prefix, complete=complete)
        assert result.heading_state == result.state == "truncated" and result.display_title is None
    assert extract_display(b"\n" * 255 + b"# Last", complete=True).display_title == "Last"
    assert extract_display(b"\n" * 255 + b"# Last\n", complete=False).display_title == "Last"
    assert extract_display(b"\n" * 256 + b"# Last", complete=True).heading_state == "truncated"


@pytest.mark.parametrize("source", [b"# \xed\xa0\x80", b"# Safe\ninvalid \xff"])
def test_invalid_encoding_cannot_supply_a_heading_or_hide_after_one(source):
    with pytest.raises(UnicodeDecodeError):
        extract_display(source, complete=True)
