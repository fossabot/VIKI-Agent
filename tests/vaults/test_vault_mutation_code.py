"""The code-folder grammar of the mutation primitives."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from viki_agent.domain import CanonicalRoot, RootIdentity, Workspace
from viki_agent.vaults import vault_mutation as native
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
    VaultPublication,
)
from viki_agent.workspaces import root_key


def _workspace(
    root: Path,
    *,
    kind: str = "code",
    policy: str = "ordinary",
    persistent: bool = True,
    key: str | None = None,
) -> Workspace:
    resolved = root.resolve()
    info = resolved.stat()
    return Workspace(
        str(uuid.uuid4()),
        kind,  # type: ignore[arg-type]
        CanonicalRoot(
            key or root_key(resolved), str(resolved), f"{info.st_dev}:{info.st_ino}", False
        ),
        "scratch",
        policy,  # type: ignore[arg-type]
        4,
        1,
        "opened",
        "unknown",
        None,
        RootIdentity((), "common-dir-key", {}, True, None, "2026-10-03T00:00:00.000Z"),
        None,
        "none" if kind == "code" else "complete",
        persistent,
        "2026-10-03T00:00:00.000Z",
        "2026-10-03T00:00:00.000Z",
    )


def _code_root(path: Path) -> OrdinaryMutationRoot:
    return OrdinaryMutationRoot.from_trusted_code_folder(_workspace(path))


def _vault_root(path: Path) -> OrdinaryMutationRoot:
    return OrdinaryMutationRoot.from_admitted_workspace(_workspace(path, kind="vault"))


@pytest.mark.parametrize(
    "ref", ["src/app.py", "Makefile", "CLAUDE.md", "a/AGENTS.md", "日本語.txt", ".gitignore"]
)
def test_a_code_ref_takes_any_suffix_and_the_instruction_files(ref: str) -> None:
    assert native.code_ref(ref) == tuple(ref.split("/"))


@pytest.mark.parametrize(
    "ref",
    [
        ".git/config",
        "a/.GIT/hooks/x",
        ".env",
        ".env.local",
        "sub/.npmrc",
        "id_rsa",
        "k.pem",
        ".claude/settings.json",
        ".viki-stage-x",
        "a/.VIKI-Backup-x.tmp",
    ],
)
def test_a_code_ref_refuses_private_names_and_the_writer_s_own_files(ref: str) -> None:
    with pytest.raises(MutationError) as caught:
        native.code_ref(ref)
    assert caught.value.code == "excluded_path"


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "a//b",
        "a/./b",
        "../a",
        "/a",
        "C:/a",
        "a\\b",
        "con.txt",
        "a.",
        "a ",
        "a?b",
        "a\x00b",
        "x" * 513,
        "a/" + "x" * 256,
    ],
)
def test_a_code_ref_refuses_names_windows_cannot_hold(ref: str) -> None:
    with pytest.raises(MutationError) as caught:
        native.code_ref(ref)
    assert caught.value.code == "invalid_ref"


def test_a_512_character_code_ref_is_admitted() -> None:
    parts = ("x" * 255, "y" * 254, "z")
    assert len("/".join(parts)) == 512
    assert native.code_ref("/".join(parts)) == parts


def test_a_code_ref_the_posix_volume_cannot_hold_is_refused() -> None:
    """Off Windows a part over NAME_MAX (255 UTF-8 bytes) is refused; Windows counts UTF-16
    units instead, so it still admits the 86-character CJK name."""
    cjk = "日" * 86 + ".py"  # 261 UTF-8 bytes, 178 UTF-16 bytes
    if sys.platform == "win32":
        assert native.code_ref(cjk) == (cjk,)
        return
    with pytest.raises(MutationError) as caught:
        native.code_ref(cjk)
    assert caught.value.code == "invalid_ref"


def test_a_trusted_code_folder_is_a_code_root(tmp_path: Path) -> None:
    root = _code_root(tmp_path)
    assert root.kind == "code"
    assert root.path == Path(str(tmp_path.resolve()))
    assert root.git_common_dir == "common-dir-key"
    assert _vault_root(tmp_path).kind == "vault"


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "vault"},
        {"policy": "existing_readonly"},
        {"policy": "protected"},
        {"persistent": False},
    ],
)
def test_only_a_trusted_persistent_code_folder_is_a_code_root(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(MutationError) as caught:
        OrdinaryMutationRoot.from_trusted_code_folder(_workspace(tmp_path, **changes))
    assert caught.value.code == "ordinary_workspace_required"


def test_a_code_folder_whose_root_moved_is_stale(tmp_path: Path) -> None:
    with pytest.raises(MutationError) as caught:
        OrdinaryMutationRoot.from_trusted_code_folder(_workspace(tmp_path, key="elsewhere"))
    assert caught.value.code == "stale_root"


async def test_a_code_root_takes_a_code_ref_and_a_vault_root_does_not(tmp_path: Path) -> None:
    code, vault = _code_root(tmp_path), _vault_root(tmp_path)
    async with MutationCoordinator().lease(code, ("a.py", "CLAUDE.md")) as lease:
        assert lease.require_target(code, "a.py") == ("a.py",)
        assert lease.require_target(code, "CLAUDE.md") == ("CLAUDE.md",)
    for ref, code_name in (("a.py", "invalid_ref"), ("CLAUDE.md", "excluded_path")):
        with pytest.raises(MutationError) as caught:
            async with MutationCoordinator().lease(vault, (ref,)):
                pass
        assert caught.value.code == code_name, ref
    async with MutationCoordinator().lease(vault, ()) as lease:
        for ref, code_name in (("a.py", "invalid_ref"), ("CLAUDE.md", "excluded_path")):
            with pytest.raises(MutationError) as caught:
                lease.require_target(vault, ref)
            assert caught.value.code == code_name, ref
    with pytest.raises(MutationError) as caught:
        async with MutationCoordinator().lease(code, (".git/x",)):
            pass
    assert caught.value.code == "excluded_path"
    assert list(tmp_path.iterdir()) == []


async def test_a_code_root_creates_and_discards_with_the_code_grammar(tmp_path: Path) -> None:
    root, publication = _code_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, ("CLAUDE.md", "a.py")) as lease:
        staged = publication.stage(lease, root, "CLAUDE.md", publication.reserve(), b"# rules\n")
        assert publication.publish_create(lease, staged).ref == "CLAUDE.md"
        other = publication.stage(lease, root, "a.py", publication.reserve(), b"x = 1\n")
        assert publication.discard(lease, other)
    assert [path.name for path in tmp_path.iterdir()] == ["CLAUDE.md"]
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"# rules\n"


async def test_a_code_root_keeps_the_two_mib_cap_for_an_image_name(tmp_path: Path) -> None:
    root, publication = _code_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, ("logo.png",)) as lease:
        with pytest.raises(MutationError) as caught:
            publication.stage(
                lease,
                root,
                "logo.png",
                publication.reserve(),
                b"x" * (native.MAX_PUBLICATION_BYTES + 1),
            )
    assert caught.value.code == "invalid_content"
    assert list(tmp_path.iterdir()) == []


async def test_a_code_root_answers_no_attachment_hash(tmp_path: Path) -> None:
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "a.png").write_bytes(b"image")
    root = _code_root(tmp_path)
    async with MutationCoordinator().lease(root, ("attachments/a.png",)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().attachment_sha256(lease, root, "attachments/a.png")
    assert caught.value.code == "invalid_ref"
