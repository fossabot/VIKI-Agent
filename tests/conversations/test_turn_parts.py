"""The `vault_excerpt` part: its published caps, its parser over an untrusted body, and the one
renderer both the preview and the dispatched turn use.

The parser is the only gate between a browser-supplied part and a durable turn row, so every row
here is a refusal the gate must keep: a foreign kind, a non-canonical vault id, a reversed or
oversized span, a private ref at any depth, and a digest that is not 64 lowercase hex.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from viki_agent.conversations import turn_parts
from viki_agent.conversations.turn_parts import (
    EXCERPT_BLOCK_CHARS,
    EXCERPT_MAX_BYTES,
    EXCERPT_MAX_LINES,
    EXCERPTS_PER_TURN,
    ExcerptPart,
    TurnInputError,
    parse_excerpt_part,
    part_data,
    render_excerpt_block,
)

VAULT_ID = "0f1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
SHA = hashlib.sha256(b"note text").hexdigest()


def wire(**overrides: Any) -> dict[str, Any]:
    """A valid part body, with the row's one deviation applied."""
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


def test_published_caps() -> None:
    assert (EXCERPT_MAX_LINES, EXCERPT_MAX_BYTES) == (200, 32768)
    assert (EXCERPTS_PER_TURN, EXCERPT_BLOCK_CHARS) == (8, 12_000)


def test_part_round_trip() -> None:
    part = parse_excerpt_part(wire())
    assert part == ExcerptPart(VAULT_ID, "notes/area/note.md", 10, 20, SHA)
    data = part_data(part)
    assert list(data) == ["kind", "vault_id", "ref", "start_line", "end_line", "sha256"]
    assert data == wire()
    assert parse_excerpt_part(data) == part


def test_bad_kind() -> None:
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(kind="vault_note"))
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(kind=None))


@pytest.mark.parametrize(
    "value",
    [
        VAULT_ID.upper(),
        VAULT_ID.replace("-", ""),
        "{" + VAULT_ID + "}",
        f"urn:uuid:{VAULT_ID}",
        VAULT_ID[:-1] + "z",
        "",
        None,
        1,
    ],
)
def test_non_canonical_uuid(value: object) -> None:
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(vault_id=value))


def test_start_after_end() -> None:
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(start_line=21, end_line=20))


def test_line_span_bounds() -> None:
    assert parse_excerpt_part(wire(start_line=1, end_line=200)).end_line == 200
    with pytest.raises(TurnInputError):  # 201 lines
        parse_excerpt_part(wire(start_line=1, end_line=201))
    for body in (
        wire(start_line=0, end_line=5),
        wire(start_line=1, end_line=1_000_001),
        wire(start_line=True, end_line=20),
        wire(start_line=10, end_line=20.0),
        wire(start_line="10", end_line="20"),
    ):
        with pytest.raises(TurnInputError):
            parse_excerpt_part(body)


@pytest.mark.parametrize(
    "ref",
    [
        "notes/.git/config.md",
        "notes/area/.claude/settings.md",
        "area/.viki/vault.json",
        "notes/area/credentials.json",
        "notes/area/.env.local",
        "notes/area/.env.production",
        "../outside.md",
        "/absolute.md",
        "notes//note.md",
        "notes/./note.md",
        "C:/notes/note.md",
        "notes\\note.md",
        "notes/note\x00.md",
        "",
        None,
    ],
)
def test_unsafe_or_private_ref(ref: object) -> None:
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(ref=ref))


@pytest.mark.parametrize(
    "value",
    [SHA.upper(), SHA[:-1], SHA + "a", SHA[:-1] + "g", "", None, 0],
)
def test_non_hex_sha(value: object) -> None:
    with pytest.raises(TurnInputError):
        parse_excerpt_part(wire(sha256=value))


def test_exact_key_set() -> None:
    bodies: list[Any] = [
        wire(extra=1),
        {key: value for key, value in wire().items() if key != "sha256"},
        {},
        [wire()],
        "vault_excerpt",
        None,
    ]
    for body in bodies:
        with pytest.raises(TurnInputError):
            parse_excerpt_part(body)


def test_fence_grows_past_the_longest_backtick_run() -> None:
    text = "before\n```python\ncode\n```\nafter"
    block = render_excerpt_block("notes/area/note.md", 3, 7, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert block == (
        f"[vault excerpt] notes/area/note.md, lines 3-7, sha256 {digest[:12]}\n````\n{text}\n````"
    )
    assert len(digest[:12]) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", digest[:12])


def test_fence_floor_is_three_backticks() -> None:
    for text, fence in (("plain text", "```"), ("a ` b", "```"), ("``inline``", "```")):
        assert render_excerpt_block("n.md", 1, 1, text).split("\n")[1] == fence
    assert render_excerpt_block("n.md", 1, 1, "x `````").split("\n")[1] == "``````"


def test_render_opens_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renderer formats text it was handed; the module names no reader or store."""
    source = Path(turn_parts.__file__).read_text(encoding="utf-8")
    assert "vault_api" not in source
    assert "storage" not in source
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("opened a file"))
    render_excerpt_block("notes/area/note.md", 1, 2, "one\ntwo")


def test_uuid_helper_matches_the_stdlib_canonical_form() -> None:
    generated = str(uuid.uuid4())
    assert parse_excerpt_part(wire(vault_id=generated)).vault_id == generated


@pytest.mark.parametrize(
    "first", ["viki_agent.vaults.vault", "viki_agent.events", "viki_agent.turn_input"]
)
def test_the_deferred_vault_import_keeps_the_cycle_shut(first: str) -> None:
    """`turn_parts` reaches `safe_ref` inside the ref check, never at module scope: `vault` reaches
    `turn_input` through `conversation_search` and `events`, and `turn_input` imports `turn_parts`,
    so a module-level edge here raises `ImportError` on whichever of these a suite imports first.
    The row imports with the package's modules evicted, then puts the session's own back."""
    saved = {name: module for name, module in sys.modules.items() if name.startswith("viki_agent")}
    try:
        for name in saved:
            del sys.modules[name]
        assert importlib.import_module(first).__name__ == first
    finally:
        for name in [n for n in sys.modules if n.startswith("viki_agent")]:
            del sys.modules[name]
        sys.modules.update(saved)
