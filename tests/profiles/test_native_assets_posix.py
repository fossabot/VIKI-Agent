"""Native assets on POSIX: every read walks directory descriptors from the verified root."""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

from tests.profiles.test_native_assets import (
    LISTED_AGENTS,
    LISTED_DESCRIPTIONS,
    LISTED_WITH_A_MODEL,
    REFUSED_AGENTS,
    REFUSED_SKILLS,
    _workspace,
    _Workspaces,
    _write,
)
from tests.vaults.test_vault_posix import canonical
from viki_agent.profiles import native_assets as native_assets_module
from viki_agent.profiles.native_assets import (
    MAX_FILE_BYTES,
    NativeAssetError,
    NativeAssetInspector,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX descriptor walk")


def _inspector(path: Path, *, home: Path | None = None) -> tuple[NativeAssetInspector, str]:
    workspace = dataclasses.replace(_workspace(path), root=canonical(path.resolve()))
    inspector = NativeAssetInspector(
        _Workspaces(workspace), claude_home=canonical(home.resolve()) if home else None
    )
    return inspector, f"claude:workspace:{workspace.workspace_id}"


def test_an_agent_and_a_skill_are_read_and_every_descriptor_is_closed(tmp_path: Path) -> None:
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    _write(
        workspace / ".claude" / "agents" / "reviewer.md",
        "---\nname: Reviewer\ndescription: Does work\n---\nbody",
    )
    _write(workspace / ".claude" / "skills" / "simple" / "SKILL.md", "---\nname: Simple\n---\n")
    _write(
        home / "agents" / "user.md",
        "---\nname: User agent\ndescription: Does work\n---\nbody",
    )
    _write(home / "skills" / "one" / "SKILL.md", "---\nname: User skill\n---\nbody")
    inspector, source = _inspector(workspace, home=home)
    before = len(os.listdir("/dev/fd"))

    result = inspector.inspect(source)
    user = inspector.inspect("claude:user")

    assert [(asset.relative_id, asset.name) for asset in result.assets] == [
        ("agents/reviewer.md", "Reviewer"),
        ("skills/simple/SKILL.md", "Simple"),
    ]
    assert not result.partial
    assert [asset.relative_id for asset in user.assets] == ["agents/user.md", "skills/one/SKILL.md"]
    assert not user.partial
    assert len(os.listdir("/dev/fd")) == before


def test_a_symlinked_skill_folder_and_an_oversized_agent_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "SKILL.md", "---\nname: Outside\n---\nsecret")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "linked").symlink_to(outside, target_is_directory=True)
    _write(tmp_path / ".claude" / "agents" / "large.md", b"x" * (MAX_FILE_BYTES + 1))
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert result.assets == ()
    assert result.partial_reasons == ("oversized", "unsafe_entry")
    assert result.scanned_bytes == 0


def test_a_symlinked_agents_folder_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.md", "secret")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agents").symlink_to(outside, target_is_directory=True)
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert (result.assets, result.partial_reasons) == ((), ("unsafe_entry",))
    assert result.omitted == 0


def test_a_fifo_named_like_an_agent_or_a_skill_does_not_block(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    agents = tmp_path / ".claude" / "agents"
    _write(agents / "ok.md", "---\nname: Ok\ndescription: Does work\n---\nbody")
    os.mkfifo(agents / "fifo.md")
    (tmp_path / ".claude" / "skills" / "piped").mkdir(parents=True)
    os.mkfifo(tmp_path / ".claude" / "skills" / "piped" / "SKILL.md")
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert [asset.relative_id for asset in result.assets] == ["agents/ok.md"]
    assert result.partial_reasons == ("unsafe_entry",)


def test_a_file_swapped_after_its_lstat_is_changed(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    _write(tmp_path / "a.md", "one")
    _write(tmp_path / "b.md", "two")
    other = os.stat(tmp_path / "b.md")
    folder = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert native_assets_module._read_exact(folder, "a.md", os.stat(tmp_path / "a.md")) == (
            b"one"
        )
        with pytest.raises(NativeAssetError, match="changed"):
            native_assets_module._read_exact(folder, "a.md", other)
    finally:
        os.close(folder)


@pytest.mark.parametrize("frontmatter", REFUSED_AGENTS)
def test_a_refused_agent_is_malformed_metadata_on_posix(tmp_path: Path, frontmatter: str) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


@pytest.mark.parametrize(("frontmatter", "unsupported", "tools"), LISTED_AGENTS)
def test_a_listed_agent_lists_on_posix(
    tmp_path: Path, frontmatter: str, unsupported: tuple[str, ...], tools: tuple[str, ...]
) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert [asset.name for asset in result.assets] == ["Good"]
    assert result.assets[0].description == "Checks work"
    assert result.assets[0].model is None
    assert result.assets[0].tools == tools
    assert result.assets[0].unsupported_fields == unsupported
    assert not result.partial


@pytest.mark.parametrize(("kind", "text", "name", "model"), LISTED_WITH_A_MODEL)
def test_an_entry_lists_with_the_model_the_cli_applies_on_posix(
    tmp_path: Path, kind: str, text: str, name: str, model: str | None
) -> None:
    _write(
        tmp_path / ".claude" / ("agents/one.md" if kind == "agent" else "skills/one/SKILL.md"),
        text,
    )
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert [(asset.name, asset.model) for asset in result.assets] == [(name, model)]
    assert not result.partial


@pytest.mark.parametrize(("frontmatter", "description"), LISTED_DESCRIPTIONS)
def test_an_agent_lists_with_the_description_the_cli_reads_on_posix(
    tmp_path: Path, frontmatter: str, description: str
) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert [(asset.name, asset.description) for asset in result.assets] == [("Good", description)]
    assert not result.partial


@pytest.mark.parametrize("text", REFUSED_SKILLS)
def test_a_refused_skill_is_malformed_metadata_on_posix(tmp_path: Path, text: str) -> None:
    _write(tmp_path / ".claude" / "skills" / "one" / "SKILL.md", text)
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


def test_an_agent_with_name_and_description_lists_on_posix(tmp_path: Path) -> None:
    _write(
        tmp_path / ".claude" / "agents" / "one.md",
        "---\nname: Good\ndescription: Checks work\n---\nbody",
    )
    inspector, source = _inspector(tmp_path)

    result = inspector.inspect(source)

    assert [asset.name for asset in result.assets] == ["Good"]
    assert result.assets[0].description == "Checks work"


def test_user_source_lists_only_its_allowlist_on_posix(tmp_path: Path) -> None:
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _write(home / "agents" / "user.md", "---\nname: User agent\ndescription: Helps\n---\nbody")
    _write(home / "skills" / "one" / "SKILL.md", "---\nname: User skill\n---\nbody")
    _write(home / "projects" / "transcript.md", "private")
    inspector, _ = _inspector(workspace, home=home)

    result = inspector.inspect("claude:user")

    assert result.source_scope == "user"
    assert [asset.relative_id for asset in result.assets] == [
        "agents/user.md",
        "skills/one/SKILL.md",
    ]
    assert not result.partial
