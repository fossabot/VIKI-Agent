"""`parse_skill_text` (validate) and `NativeAssetInspector.read_skill` (read), independently:
a skill whose frontmatter would fail validation still reads, since `read_skill` is a raw,
content-agnostic fetch and never calls `parse_skill_text` itself."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from tests.profiles.test_native_assets import _inspector, _root, _write
from viki_agent.profiles.native_assets import (
    MAX_FILE_BYTES,
    NativeAssetError,
    SkillHead,
    parse_skill_text,
)
from viki_agent.winproc import run

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows handle safety")


# ---- parse_skill_text --------------------------------------------------------------------


def test_parse_skill_text_returns_the_head_with_unsupported_fields() -> None:
    head = parse_skill_text(
        "---\nname: good-skill\ndescription: Does a thing\nlicense: MIT\n---\nbody"
    )
    assert head == SkillHead("good-skill", "Does a thing", ("license",))


def test_parse_skill_text_requires_frontmatter() -> None:
    with pytest.raises(NativeAssetError, match="missing_frontmatter"):
        parse_skill_text("no frontmatter here")


def test_parse_skill_text_propagates_a_frontmatter_error_as_malformed_metadata() -> None:
    with pytest.raises(NativeAssetError, match="malformed_metadata"):
        parse_skill_text("---\nname: good\nname: again\ndescription: Does a thing\n---\nbody")


def test_parse_skill_text_requires_a_name() -> None:
    with pytest.raises(NativeAssetError, match="missing_name"):
        parse_skill_text("---\ndescription: Does a thing\n---\nbody")


def test_parse_skill_text_requires_a_non_empty_name() -> None:
    # an empty value is a well-formed (if empty) scalar, refused by `_plain` as malformed, not
    # treated as the key being absent
    with pytest.raises(NativeAssetError, match="malformed_metadata"):
        parse_skill_text("---\nname:\ndescription: Does a thing\n---\nbody")


@pytest.mark.parametrize(
    "name",
    (
        "Bad_Name",  # uppercase and an underscore: outside [a-z0-9-]
        "a" * 65,  # one character past the 64 bound, though shaped like a valid name
        "settings",  # shaped like a valid name, but a denied stem under `_safe_name`
    ),
)
def test_parse_skill_text_refuses_a_badly_shaped_name(name: str) -> None:
    with pytest.raises(NativeAssetError, match="invalid_name"):
        parse_skill_text(f"---\nname: {name}\ndescription: Does a thing\n---\nbody")


@pytest.mark.parametrize("name", ("synced", "anthropic-skills"))
def test_parse_skill_text_refuses_a_reserved_name(name: str) -> None:
    with pytest.raises(NativeAssetError, match="reserved_name"):
        parse_skill_text(f"---\nname: {name}\ndescription: Does a thing\n---\nbody")


def test_parse_skill_text_requires_a_description() -> None:
    with pytest.raises(NativeAssetError, match="missing_description"):
        parse_skill_text("---\nname: good-skill\n---\nbody")


def test_parse_skill_text_accepts_a_description_at_the_exact_character_bound() -> None:
    head = parse_skill_text("---\nname: good-skill\ndescription: " + "a" * 1536 + "\n---\nbody")
    assert len(head.description) == 1536


@pytest.mark.parametrize(
    "description",
    (
        "a" * 1537,
        "\u3042" * 1537,  # multibyte characters: the bound counts characters, not UTF-8 bytes
    ),
)
def test_parse_skill_text_refuses_an_overlong_description(description: str) -> None:
    with pytest.raises(NativeAssetError, match="long_description"):
        parse_skill_text(f"---\nname: good-skill\ndescription: {description}\n---\nbody")


# ---- NativeAssetInspector.read_skill ------------------------------------------------------


def test_read_skill_digest_matches_inspect(tmp_path: Path) -> None:
    body = "---\nname: Good\ndescription: Checks work\n---\nSKILL BODY"
    _write(tmp_path / ".claude" / "skills" / "good" / "SKILL.md", body)
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    catalog = inspector.inspect(source)
    result = inspector.read_skill(source, "good")

    assert result is not None
    text, digest = result
    assert text == body
    assert len(catalog.assets) == 1
    assert digest == catalog.assets[0].digest == hashlib.sha256(body.encode()).hexdigest()


def test_read_skill_reads_raw_bytes_regardless_of_frontmatter_validity(tmp_path: Path) -> None:
    """`read_skill` never calls `parse_skill_text`: content a proposal would refuse still reads."""
    _write(tmp_path / ".claude" / "skills" / "junk" / "SKILL.md", "not even frontmatter")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    result = inspector.read_skill(source, "junk")

    assert result is not None
    assert result[0] == "not even frontmatter"
    with pytest.raises(NativeAssetError, match="missing_frontmatter"):
        parse_skill_text(result[0])


def test_read_skill_returns_none_for_an_absent_skills_folder(tmp_path: Path) -> None:
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    assert inspector.read_skill(source, "missing") is None


def test_read_skill_returns_none_for_an_absent_file(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills" / "empty").mkdir(parents=True)
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    assert inspector.read_skill(source, "empty") is None


def test_read_skill_refuses_an_oversized_file(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "skills" / "big" / "SKILL.md", b"x" * (MAX_FILE_BYTES + 1))
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    with pytest.raises(NativeAssetError, match="oversized"):
        inspector.read_skill(source, "big")


def test_read_skill_refuses_a_hardlinked_file(tmp_path: Path) -> None:
    skill = tmp_path / ".claude" / "skills" / "linked"
    skill.mkdir(parents=True)
    _write(skill / "original.md", "body")
    os.link(skill / "original.md", skill / "SKILL.md")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    with pytest.raises(NativeAssetError, match="unsafe_entry"):
        inspector.read_skill(source, "linked")


def test_read_skill_refuses_a_symlinked_skill_folder(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "SKILL.md", "---\nname: Good\n---\nbody")
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    link = tmp_path / ".claude" / "skills" / "linked"
    created = run(
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(outside)],
        cwd=tmp_path,
    )
    if created.returncode != 0:
        pytest.skip("directory junctions unavailable")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    with pytest.raises(NativeAssetError, match="unsafe_entry"):
        inspector.read_skill(source, "linked")


def test_read_skill_strips_the_bom_but_hashes_the_raw_bytes(tmp_path: Path) -> None:
    body = "---\nname: Good\ndescription: Checks work\n---\nbody"
    raw = body.encode("utf-8-sig")
    _write(tmp_path / ".claude" / "skills" / "good" / "SKILL.md", raw)
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    result = inspector.read_skill(source, "good")

    assert result is not None
    text, digest = result
    assert text == body
    assert not text.startswith("﻿")
    assert digest == hashlib.sha256(raw).hexdigest()


def test_read_skill_refuses_invalid_utf8(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "skills" / "bad" / "SKILL.md", b"\xff")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    with pytest.raises(NativeAssetError, match="invalid_encoding"):
        inspector.read_skill(source, "bad")


def test_read_skill_refuses_an_invalid_name_before_any_read(tmp_path: Path) -> None:
    # no .claude directory exists at all: a filesystem-first check would fail some other way
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    with pytest.raises(NativeAssetError, match="invalid_name"):
        inspector.read_skill(source, "../escape")


def test_read_skill_detects_a_root_swapped_mid_call(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "skills" / "one" / "SKILL.md", "---\nname: Good\n---\nbody")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"
    other = tmp_path / "other"
    other.mkdir()
    owner.changed_root = _root(other)

    with pytest.raises(NativeAssetError, match="source_changed"):
        inspector.read_skill(source, "one")
