"""`TurnInput` with excerpt parts: wire format 3, and the format 2 bytes that must not move.

A stored turn is durable, so the two halves of the contract are pinned here: a part-free turn
encodes exactly the bytes it did before format 3 existed (the `parts` key appears only when the
turn has parts), and `decode` takes format 2's two keys or format 3's three and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import pytest

from viki_agent.conversations.turn_parts import EXCERPTS_PER_TURN, ExcerptPart, part_data
from viki_agent.turn_input import Attachment, TurnInput, TurnInputError, claude_accepts, coerce

VAULT_ID = "0f1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
SHA = hashlib.sha256(b"note text").hexdigest()


def part(ref: str = "notes/area/note.md", start: int = 10, end: int = 20) -> ExcerptPart:
    return ExcerptPart(VAULT_ID, ref, start, end, SHA)


def attachment() -> Attachment:
    return Attachment(str(uuid.uuid4()), "image/png", 4, 1, 1, "synthetic.png")


def wire(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "vault_excerpt",
        "vault_id": VAULT_ID,
        "ref": "notes/area/note.md",
        "start_line": 10,
        "end_line": 20,
        "sha256": SHA,
    }
    body.update(overrides)
    return body


def body(parts: Any) -> bytes:
    """A format 3 turn body as the client would send it, with this row's `parts` value."""
    return json.dumps({"text": "look", "attachments": [], "parts": parts}).encode("utf-8")


def test_part_round_trip() -> None:
    turn = TurnInput("look", parts=(part(), part("notes/b.md", 1, 5)))
    restored = TurnInput.decode(turn.encode())
    assert restored == turn
    assert restored.parts[1].ref == "notes/b.md"
    assert restored.encode() == turn.encode()


def test_wire_format_counts_the_richest_field() -> None:
    assert TurnInput("hello").wire_format == 1
    assert TurnInput("hello", attachments=(attachment(),)).wire_format == 2
    assert TurnInput("hello", parts=(part(),)).wire_format == 3
    assert TurnInput("hello", attachments=(attachment(),), parts=(part(),)).wire_format == 3


def test_a_part_free_turn_encodes_byte_identically() -> None:
    """The format 2 bytes, written out in full: `data()` adds `parts` only when parts exist, so a
    stored row from before format 3 re-encodes to itself."""
    assert TurnInput("hello").encode() == b'{"text":"hello","attachments":[]}'
    assert TurnInput("hello").data() == {"text": "hello", "attachments": []}
    with_image = TurnInput("hello", attachments=(attachment(),))
    assert b'"parts"' not in with_image.encode()
    assert TurnInput.decode(with_image.encode()) == with_image
    assert b'"parts"' in TurnInput("hello", parts=(part(),)).encode()


def test_a_v3_body_with_a_bad_kind() -> None:
    with pytest.raises(TurnInputError):
        TurnInput.decode(body([wire(kind="vault_note")]))


@pytest.mark.parametrize(
    "bad",
    [
        wire(vault_id=VAULT_ID.upper()),  # non-canonical uuid
        wire(start_line=21, end_line=20),  # start > end
        wire(start_line=1, end_line=201),  # a 201-line span
        wire(ref="notes/area/.claude/settings.md"),  # an excluded ref at depth
        wire(sha256=SHA[:-1] + "G"),  # a non-hex sha
        wire(extra=1),
        {"kind": "vault_excerpt"},
        "vault_excerpt",
        None,
    ],
)
def test_a_v3_body_the_part_parser_refuses(bad: Any) -> None:
    with pytest.raises(TurnInputError):
        TurnInput.decode(body([bad]))


def test_a_non_list_parts() -> None:
    for parts in ({"0": wire()}, wire(), "parts", 3, None):
        with pytest.raises(TurnInputError):
            TurnInput.decode(body(parts))


def test_decode_takes_only_the_two_known_key_sets() -> None:
    for raw in (
        b'{"text":"hi","attachments":[],"parts":[],"extra":1}',
        b'{"text":"hi","parts":[]}',
        b'{"text":"hi"}',
        b'{"attachments":[],"parts":[]}',
        b"[]",
    ):
        with pytest.raises(TurnInputError):
            TurnInput.decode(raw)
    assert TurnInput.decode(b'{"text":"hi","attachments":[],"parts":[]}') == TurnInput("hi")


def test_nine_parts() -> None:
    nine = tuple(part(f"notes/n{index}.md") for index in range(EXCERPTS_PER_TURN + 1))
    assert len(nine) == 9
    with pytest.raises(TurnInputError):
        TurnInput("look", parts=nine)
    with pytest.raises(TurnInputError):
        TurnInput.decode(body([part_data(value) for value in nine]))
    assert len(TurnInput("look", parts=nine[:EXCERPTS_PER_TURN]).parts) == 8


def test_a_duplicate_span() -> None:
    with pytest.raises(TurnInputError):
        TurnInput("look", parts=(part(), part()))
    with pytest.raises(TurnInputError):
        TurnInput.decode(body([wire(), wire()]))
    # The same note at two spans is not a duplicate.
    assert len(TurnInput("look", parts=(part(), part(start=30, end=40))).parts) == 2


def test_parts_must_be_a_tuple_of_parts() -> None:
    for parts in ([part()], (wire(),), (None,), part()):
        with pytest.raises(TurnInputError):
            TurnInput("look", parts=parts)  # type: ignore[arg-type]


def test_the_text_rules_are_unchanged() -> None:
    assert TurnInput("héllo").text_bytes == 6
    assert TurnInput("hello", parts=(part(),)).sendable is True
    assert TurnInput("", parts=(part(),)).sendable is False
    assert coerce("hello") == TurnInput("hello")
    assert claude_accepts(TurnInput("hello", parts=(part(),))) is True
