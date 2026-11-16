"""Display tags and opaque unknown blocks, independent of later heading support."""

from __future__ import annotations

import json

import pytest

from viki_agent.vaults.note_metadata import extract_display


def header(body: str, *, complete: bool = True):
    return extract_display(f"---\n{body}\n---".encode(), complete=complete)


def discarded(body: str, *, complete: bool = True, state: str = "unsupported"):
    result = header(
        f"title: Visible\nname: Backup\ntype: note\nstatus: approved\n{body}", complete=complete
    )
    assert (
        result.frontmatter_state,
        result.display_title,
        result.title_source,
        result.tags,
        result.type,
        result.status,
    ) == (state, None, None, None, None, None)


@pytest.mark.parametrize(
    "scalar,expected", [("Work 2", "Work 2"), ('"a,b"', "a,b"), ('"  名  "', "  名  ")]
)
def test_supported_scalar_tag_is_one_literal_item(scalar, expected):
    result = header(f"name: Backup\ntags: {scalar}")
    assert result.frontmatter_state == "ready"
    assert result.tags == [expected] and result.display_title == "Backup"


def test_json_array_preserves_order_case_combining_and_html_looking_text():
    tags = ["Work", "work", "<img>é\u2028名</img>"]
    source = f"---\ntitle: SKILL\ntags: {json.dumps(tags, ensure_ascii=False)}\n---".encode()
    original = bytes(source)
    result = extract_display(source, complete=True)
    assert (result.frontmatter_state, result.display_title, result.tags) == ("ready", "SKILL", tags)
    assert source == original
    assert header("tags: []").tags == []


def test_flow_list_of_plain_scalars_is_ready():
    result = header("name: Backup\ntags: [Work, note, draft]")
    assert result.frontmatter_state == "ready"
    assert (result.tags, result.display_title) == (["Work", "note", "draft"], "Backup")
    assert header("tags: [ Work , note ]").tags == ["Work", "note"]
    assert header("tags: [a, b-c, 名]").tags == ["a", "b-c", "名"]


def test_exact_list_allows_blank_comments_then_returns_to_top_level_fields():
    result = header('tags:\n  - "  Work  "\n\n  # comment\n  - 名\nname: Backup\nstatus: approved')
    assert result.frontmatter_state == "ready"
    assert (result.tags, result.display_title, result.status) == (
        ["  Work  ", "名"],
        "Backup",
        "approved",
    )


@pytest.mark.parametrize(
    "body",
    [
        "tags:",
        'tags: " "',
        "tags: a,b",
        "tags: true",
        "tags: 'tag'",
        'tags: ["tag", 2]',
        'tags: ["tag", false]',
        'tags: ["tag", "tag"]',
        r'tags: ["\u007f"]',
        r'tags: ["\ud800"]',
        "tags:\n - A",
        "tags:\n   - A",
        "tags:\n\t- A",
        "tags:\n  - A\n    - B",
        "tags:\n  title: Hidden",
        "tags:\n  - A\n  - A",
        "tags: []\ntags: []",
        "  title: Hidden",
        "bad?key: ignored",
        "A" * 65 + ": ignored",
        "unknown: inline\nmalformed boundary",
        "tags: [Work",
        'tags: [Work, "x"]',
        "tags: [Work, Work]",
        "tags: [Work,]",
        "tags: [a, true]",
        "tags: [a, 1]",
        "tags: [a, null]",
        "tags: [a, -b]",
        f"tags: [a, {'a' * 65}]",
        "tags: [a, \t]",
        "tags: [a, [b]]",
        "tags: ['a', b]",
    ],
)
def test_unsupported_or_ambiguous_structure_clears_every_authored_field(body):
    discarded(body)


def test_tag_count_and_unicode_codepoint_limits_are_exact():
    tags = ["🧠" * 63 + chr(ord("A") + index) for index in range(16)]
    result = header(f"tags: {json.dumps(tags)}")
    assert result.frontmatter_state == "ready" and result.tags == tags
    discarded(f"tags: {json.dumps(tags + ['seventeenth'])}")
    discarded(f"tags: {json.dumps(['🧠' * 65])}")
    items = "\n".join(f"  - {json.dumps(tag)}" for tag in tags)
    assert header(f"tags:\n{items}").tags == tags
    discarded(f"tags:\n{items}\n  - seventeenth")
    discarded(f"tags:\n  - {json.dumps('🧠' * 65)}")


def test_flow_list_tag_count_limit_is_exact():
    sixteen = ", ".join(f"T{index}" for index in range(16))
    assert header(f"tags: [{sixteen}]").tags == [f"T{index}" for index in range(16)]
    seventeen = ", ".join(f"T{index}" for index in range(17))
    discarded(f"tags: [{seventeen}]")


@pytest.mark.parametrize(
    "unknown",
    [
        'other: |\n  title: Hidden\n  tags: ["hidden"]\n    status: active',
        "other: inline\n\tname: Hidden\n\ttype: secret\n  ---",
        "Other_42: any\n  nested:\n    title: Hidden",
        "A" * 64 + ": ignored",
    ],
)
def test_unknown_blocks_do_not_promote_nested_recognized_words(unknown):
    result = header(f'name: "  Visible  "\n{unknown}\nstatus: approved')
    assert result.frontmatter_state == "ready"
    assert (result.display_title, result.title_source, result.tags, result.type, result.status) == (
        "  Visible  ",
        "name",
        [],
        None,
        "approved",
    )


def test_prefix_cut_list_discards_fields_without_claiming_absence():
    discarded("tags:\n  - A", complete=False, state="truncated")


def test_deeply_nested_bounded_json_is_unsupported_metadata():
    nested = "[" * 5000 + '"tag"' + "]" * 5000
    assert len(nested.encode()) < 32768
    discarded(f"tags: {nested}")


def test_tags_json_recursion_failure_is_classified_without_losing_note_readability(monkeypatch):
    calls = []

    def recurse(value):
        calls.append(value)
        raise RecursionError

    monkeypatch.setattr("viki_agent.vaults.note_metadata.json.loads", recurse)
    discarded('tags: ["tag"]')
    assert calls
