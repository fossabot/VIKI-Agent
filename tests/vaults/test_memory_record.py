from __future__ import annotations

import hashlib
import json

import pytest

from viki_agent.vaults.memory_record import (
    NOT_A_RECORD,
    Fields,
    Invalid,
    NoteSource,
    Record,
    SessionSource,
    compose,
    effective,
    parse,
)

ID = "0b1c2d3e-0000-4000-8000-000000000001"
OTHER_ID = "0b1c2d3e-0000-4000-8000-000000000002"
DIGEST = "ab" * 32
CREATED = "2026-10-02T09:30:00.123Z"
BASE = [
    f"id: {ID}",
    "kind: assistant",
    "scope: vault",
    "status: approved",
    f"created: {CREATED}",
]


def _fields(**changes: object) -> Fields:
    values: dict[str, object] = {
        "id": ID,
        "kind": "assistant",
        "scope": "vault",
        "status": "approved",
        "created": CREATED,
    }
    values.update(changes)
    return Fields(**values)  # type: ignore[arg-type]


def _text(*head: str, claim: str = "The owner prefers tea.", eol: str = "\n") -> str:
    """A record whose head lines are written by hand, so a test can break the grammar."""
    lines = ["---", "viki_memory: 1", *(head or BASE), "---", claim]
    return eol.join(lines) + eol


def _good() -> str:
    return _text()


def _refused(text: str, reason: str) -> None:
    assert parse(text) == Invalid(reason)


def _record(text: str) -> Record:
    parsed = parse(text)
    assert isinstance(parsed, Record), parsed
    return parsed


def test_compose_then_parse_round_trips_byte_for_byte() -> None:
    sources = (NoteSource("people/Ada.md", DIGEST), SessionSource(OTHER_ID))
    fields = _fields(
        status="retired",
        reviewed="2026-10-03T00:00:00.000Z",
        creator="claude",
        sources=sources,
        supersedes=OTHER_ID,
        retired_by="0b1c2d3e-0000-4000-8000-000000000003",
    )
    claim = "Likes crème brûlée.\n\n---\nA second paragraph with a fence line above.  "

    text = compose(fields, claim)

    parsed = _record(text)
    assert (parsed.fields, parsed.claim) == (fields, claim)
    assert parsed.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert compose(parsed.fields, parsed.claim) == text
    assert text.startswith("---\nviki_memory: 1\n") and "\r" not in text
    assert not text.startswith("﻿")


def test_compose_writes_the_fixed_field_order_and_a_one_line_json_array() -> None:
    fields = _fields(
        reviewed=CREATED,
        creator="owner",
        sources=(NoteSource("a.md", DIGEST), SessionSource(OTHER_ID)),
        supersedes=OTHER_ID,
    )

    head = compose(fields, "x").split("\n")[1:-3]

    assert [line.split(":")[0] for line in head] == [
        "viki_memory",
        "id",
        "kind",
        "scope",
        "status",
        "created",
        "reviewed",
        "creator",
        "sources",
        "supersedes",
    ]
    listed = json.loads(head[8].removeprefix("sources: "))
    assert listed == [{"note": "a.md", "sha256": DIGEST}, {"session": OTHER_ID}]


@pytest.mark.parametrize(
    ("fields", "claim", "reason"),
    [
        (_fields(creator="a\nkind: owner"), "x", "duplicate_key"),
        (_fields(creator="a\nsupersedes: " + OTHER_ID), "x", "not_canonical"),
        (_fields(kind="Owner"), "x", "bad_value"),
        (_fields(), "", "bad_claim"),
        (_fields(), "x\n", "not_canonical"),
        (_fields(), "a\r\nb", "not_canonical"),
        (_fields(), "a\rb", "bad_claim"),
        (_fields(), "x" * 16_385, "bad_claim"),
    ],
)
def test_compose_refuses_what_would_not_read_back_as_given(
    fields: Fields, claim: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        compose(fields, claim)


def test_a_valid_record_has_no_optional_fields_to_find() -> None:
    parsed = _record(_good())
    assert parsed.fields == _fields()
    assert parsed.claim == "The owner prefers tea."


# --- what is not a record ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "A plain note.\n",
        "",
        "---\nkind: owner\n---\nbody\n",
        " ---\nviki_memory: 1\n---\nbody\n",
        "---\nviki_memory: 2\n---\nbody\n",
        "---\nviki_memory:  1\n---\nbody\n",
        "---\nkind: owner\n---\nviki_memory: 1\n---\n",
        "---\nkind: owner\n" + "x\n" * 30 + "viki_memory: 1\n",
    ],
)
def test_a_file_without_the_marker_in_its_frontmatter_is_an_ordinary_note(text: str) -> None:
    _refused(text, NOT_A_RECORD)


# --- refusals ----------------------------------------------------------------------------------


def test_a_repeated_key_is_refused() -> None:
    _refused(_text(*BASE, "kind: owner"), "duplicate_key")


def test_a_repeated_marker_is_refused() -> None:
    _refused(_text(*BASE, "viki_memory: 1"), "duplicate_key")


def test_a_repeated_key_is_refused_whatever_its_value() -> None:
    _refused(_text(*BASE, "created: not even a time"), "duplicate_key")


@pytest.mark.parametrize(
    "extra",
    [
        ["  folded continuation"],
        ["\tfolded continuation"],
        [""],
        ["# a comment"],
        ["note:no space after the colon"],
        ["Note: upper case key"],
        ["creator:"],
        ["creator: "],
    ],
)
def test_a_line_that_is_not_one_key_and_value_is_refused(extra: list[str]) -> None:
    _refused(_text(*BASE, *extra), "bad_line")


def test_a_folded_value_is_refused() -> None:
    folded = [*BASE, 'sources: [{"session":', f'  "{OTHER_ID}"}}]']
    _refused(_text(*folded), "bad_line")


def test_an_unknown_field_is_refused() -> None:
    _refused(_text(*BASE, "tags: a"), "unknown_key")


@pytest.mark.parametrize("missing", ["id", "kind", "scope", "status", "created"])
def test_a_missing_required_field_is_refused(missing: str) -> None:
    _refused(_text(*[line for line in BASE if not line.startswith(missing + ":")]), "missing_key")


def test_a_frontmatter_without_a_closing_fence_in_time_is_unclosed() -> None:
    _refused("---\nviki_memory: 1\n" + "\n".join(BASE) + "\nclaim\n", "unclosed")
    padded = "---\nviki_memory: 1\n" + "x: 1\n" * 18 + "---\nclaim\n"
    _refused(padded, "unclosed")


def test_a_closing_fence_must_be_the_whole_line() -> None:
    _refused(_good().replace("\n---\n", "\n--- \n"), "unclosed")


@pytest.mark.parametrize(
    "line",
    [
        "id: 0B1C2D3E-0000-4000-8000-000000000001",
        "id: not-a-uuid",
        f"id: {ID} ",
        f"id: {{{ID}}}",
    ],
)
def test_a_non_canonical_id_is_refused(line: str) -> None:
    _refused(_text(line, *BASE[1:]), "bad_id")


@pytest.mark.parametrize(
    "line",
    ["kind: Owner", "kind: user", "scope: global", "status: Approved", "status: approved "],
)
def test_a_value_outside_its_set_is_refused(line: str) -> None:
    key = line.split(":")[0]
    _refused(_text(*[line if b.startswith(key + ":") else b for b in BASE]), "bad_value")


@pytest.mark.parametrize(
    "created",
    [
        "2026-10-02T09:30:00Z",
        "2026-10-02T09:30:00.123+00:00",
        "2026-10-02 09:30:00.123Z",
        "2026-13-02T09:30:00.123Z",
        "2026-10-02T25:30:00.123Z",
        "2026-10-02T09:30:00.123z",
        "2026-10-02T09:30:00.12Z",
        "２０２６-10-02T09:30:00.123Z",
    ],
)
def test_a_timestamp_that_is_not_the_utc_millisecond_form_is_refused(created: str) -> None:
    _refused(_text(*BASE[:-1], f"created: {created}"), "bad_time")
    _refused(_text(*BASE, f"reviewed: {created}"), "bad_time")


@pytest.mark.parametrize("creator", ["x" * 65, "a\tb", "a b", "a‮b", " a", "a "])
def test_a_creator_that_is_long_or_not_printable_is_refused(creator: str) -> None:
    _refused(_text(*BASE, f"creator: {creator}"), "bad_value")


def test_a_creator_of_64_characters_is_admitted() -> None:
    assert _record(_text(*BASE, "creator: " + "x" * 64)).fields.creator == "x" * 64


@pytest.mark.parametrize("key", ["supersedes", "retired_by"])
def test_a_link_to_another_record_must_be_a_canonical_id(key: str) -> None:
    _refused(_text(*BASE[:3], "status: retired", BASE[4], f"{key}: nope"), "bad_value")


def test_retired_by_belongs_to_a_retired_record_only() -> None:
    _refused(_text(*BASE, f"retired_by: {OTHER_ID}"), "bad_value")
    retired = [*BASE[:3], "status: retired", BASE[4], f"retired_by: {OTHER_ID}"]
    assert _record(_text(*retired)).fields.retired_by == OTHER_ID


# --- sources -----------------------------------------------------------------------------------


def _sources(value: str) -> str:
    return _text(*BASE, f"sources: {value}")


def _note(ref: str = "a.md", digest: object = DIGEST) -> str:
    return json.dumps({"note": ref, "sha256": digest})


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[",
        "{}",
        f"{_note()}",
        "['a']",
        "[1]",
        "[null]",
        "[[]]",
        "[NaN]",
        "[Infinity]",
        "[" * 3000,
        '[{"session": 1}]',
        '[{"session": "' + ID + '", "note": "a.md"}]',
        '[{"note": "a.md"}]',
        f'[{{"note": "a.md", "sha256": "{DIGEST}", "extra": 1}}]',
        '[{"session": "' + ID + '", "session": "' + ID + '"}]',
        f'[{{"note": "a.md", "note": "b.md", "sha256": "{DIGEST}"}}]',
        f"[{_note(digest=DIGEST.upper())}]",
        f"[{_note(digest=DIGEST[:-1])}]",
        f"[{_note(digest=1)}]",
        f"[{_note(ref='../a.md')}]",
        f"[{_note(ref='/a.md')}]",
        f"[{_note(ref='a//b.md')}]",
        f"[{_note(ref='a/./b.md')}]",
        f"[{_note(ref='a\\b.md')}]",
        f"[{_note(ref='C:a.md')}]",
        f"[{_note(ref='a.txt')}]",
        f"[{_note(ref='a\x00.md')}]",
        f"[{_note(ref='a\n.md')}]",
        f"[{_note(ref='a/' + 'b' * 512 + '.md')}]",
        f"[{_note(ref='')}]",
        '[{"session": "NOT-A-UUID"}]',
        '[{"session": "' + ID.upper() + '"}]',
        '[{"session": "\\ud800"}]',
    ],
)
def test_sources_that_are_not_a_strict_json_array_of_the_two_shapes_are_refused(
    value: str,
) -> None:
    _refused(_sources(value), "bad_sources")


def test_seventeen_sources_are_refused_and_sixteen_are_admitted() -> None:
    session = f'{{"session": "{ID}"}}'
    assert len(_record(_sources("[" + ", ".join([session] * 16) + "]")).fields.sources) == 16
    _refused(_sources("[" + ", ".join([session] * 17) + "]"), "bad_sources")


def test_an_empty_array_and_both_source_shapes_are_admitted() -> None:
    assert _record(_sources("[]")).fields.sources == ()
    both = _sources(f'[{_note("notes/café.markdown")}, {{"session": "{ID}"}}]')
    assert _record(both).fields.sources == (
        NoteSource("notes/café.markdown", DIGEST),
        SessionSource(ID),
    )


# --- the claim and the whole-record bounds -----------------------------------------------------


@pytest.mark.parametrize("claim", ["", "   ", "\n\n", "\t"])
def test_a_blank_claim_is_refused(claim: str) -> None:
    _refused(_text(claim=claim), "bad_claim")


def test_a_missing_body_is_refused() -> None:
    _refused("\n".join(["---", "viki_memory: 1", *BASE, "---"]), "bad_claim")


def test_the_claim_is_bounded_in_bytes_not_characters() -> None:
    at_bound = "あ" * 5461 + "a"  # 16,384 bytes
    assert len(at_bound.encode()) == 16_384
    assert _record(_text(claim=at_bound)).claim == at_bound
    _refused(_text(claim=at_bound + "a"), "bad_claim")
    _refused(_text(claim="あ" * 5462), "bad_claim")


def test_trailing_newlines_are_not_part_of_the_claim() -> None:
    assert _record(_text(claim="x\n\n\n")).claim == "x"


def test_a_record_over_32768_bytes_is_refused() -> None:
    _refused(_text(claim="x\n" + " " * 33_000), "too_large")


def test_the_record_is_bounded_to_199_lines() -> None:
    head = 2 + len(BASE) + 1  # the fence, the marker, the fields and the closing fence
    body = "\n".join(["x"] * (199 - head))
    assert _record(_text(claim=body)).claim == body
    assert _record(_text(claim=body)[:-1]).claim == body  # without a final newline
    _refused(_text(claim=body + "\nx"), "too_large")
    _refused(_text(claim=body + "\nx")[:-1], "too_large")


# --- encodings and line ends -------------------------------------------------------------------


def test_crlf_line_ends_are_accepted_on_read() -> None:
    lf = _record(_good())
    crlf_text = _text(eol="\r\n")
    crlf = _record(crlf_text)
    assert (crlf.fields, crlf.claim) == (lf.fields, lf.claim)
    assert crlf.sha256 == hashlib.sha256(crlf_text.encode()).hexdigest() != lf.sha256


def test_mixed_line_ends_are_accepted_when_each_is_lf_or_crlf() -> None:
    mixed = _good().replace("\nkind", "\r\nkind", 1)
    assert _record(mixed).fields == _fields()


def test_a_leading_bom_is_accepted_and_counted_in_the_hash() -> None:
    bom_text = "﻿" + _good()
    parsed = _record(bom_text)
    assert parsed.fields == _fields()
    assert parsed.sha256 == hashlib.sha256(bom_text.encode("utf-8")).hexdigest()
    assert parsed.sha256 != _record(_good()).sha256


def test_a_bom_anywhere_else_is_refused() -> None:
    _refused("﻿﻿" + _good(), NOT_A_RECORD)
    _refused(_text(*BASE, "creator: a﻿b"), "bad_value")
    _refused(_text(*BASE, "﻿creator: x"), "bad_line")


def test_a_lone_carriage_return_is_refused_in_the_head_and_in_the_claim() -> None:
    _refused(_text(*BASE, "creator: a\rb"), "bad_line")
    _refused(_good().replace("tea.", "te\ra."), "bad_claim")


@pytest.mark.parametrize("separator", [" ", " ", "\x0b", "\x0c", "\x85"])
def test_unicode_line_separators_do_not_split_lines(separator: str) -> None:
    # `str.splitlines` would; a hand-written reader that used it would see a second field here.
    _refused(_text(*BASE[:2], f"scope: vault{separator}x: 1", *BASE[3:]), "bad_value")


def test_text_that_is_not_utf_8_encodable_is_refused() -> None:
    # Invalid bytes are refused upstream; a lone surrogate is what is left of them in a str.
    _refused(_text(claim="a\udc80b"), "encoding")


# --- effective state ---------------------------------------------------------------------------


def _state(status: str, kind: str = "assistant", *, approved: str | None = "same") -> str:
    changes = {} if status != "retired" else {"retired_by": OTHER_ID}
    text = compose(_fields(status=status, kind=kind, **changes), "claim")
    record = _record(text)
    return effective(record, record.sha256 if approved == "same" else approved)


@pytest.mark.parametrize(
    ("status", "kind", "approved", "expected"),
    [
        ("retired", "assistant", "same", "retired"),
        ("retired", "assistant", None, "retired"),
        ("retired", "owner", "other", "retired"),
        ("retired", "inference", "same", "retired"),
        ("approved", "assistant", "same", "active"),
        ("approved", "owner", "same", "active"),
        ("approved", "summary", "same", "active"),
        ("approved", "assistant", None, "pending"),
        ("approved", "assistant", "other", "pending"),
        ("approved", "inference", "same", "pending"),
        ("approved", "inference", None, "pending"),
        ("approved", "inference", "other", "pending"),
        ("candidate", "assistant", "same", "candidate"),
        ("candidate", "assistant", None, "candidate"),
        ("candidate", "inference", "other", "candidate"),
    ],
)
def test_the_effective_state_table(
    status: str, kind: str, approved: str | None, expected: str
) -> None:
    assert _state(status, kind, approved=approved) == expected


def test_an_unparseable_record_is_invalid_whatever_was_approved() -> None:
    assert effective(Invalid("bad_line"), "anything") == "invalid"
    assert effective(Invalid("bad_line"), None) == "invalid"
    assert effective(parse(_text(*BASE, "kind: owner")), DIGEST) == "invalid"


def test_an_edit_to_one_byte_makes_an_approved_record_pending_again() -> None:
    approved = _record(compose(_fields(), "claim"))
    edited = _record(compose(_fields(), "claim."))
    assert effective(approved, approved.sha256) == "active"
    assert effective(edited, approved.sha256) == "pending"
