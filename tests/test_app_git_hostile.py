"""The app's own Git reads in a folder nobody has trusted: the vault chip, a note's history and
the identity probe. Whatever the folder's repository settings name, those reads start no program,
fetch nothing and write nothing. Each case runs the real reader first, then shows the setting is
live with the same Git command run plainly, outside `winproc.untrusted_git`."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from viki_agent import rails, winproc
from viki_agent.vaults.vault_git import note_history
from viki_agent.workspaces import canonical_root, probe_identity

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
SIGNATURE = (
    "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----"
)


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Git scope variable makes every reader stand down before it runs anything.
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _git(cwd: Path, *args: str) -> str:
    """Setup only, outside the readers under test."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _repo(root: Path, commits: int = 1) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        _git(root, "config", key, value)
    for number in range(commits):
        (root / "note.md").write_text(f"# note {number}\n", encoding="utf-8", newline="\n")
        _git(root, "add", "--", "note.md")
        _git(root, "commit", "-q", "-m", f"edit {number}")
    return root


def _command(marker: Path, tail: str = "") -> str:
    """A shell command a setting names: it leaves `marker` behind."""
    return f"echo ran > '{marker.as_posix()}'{tail}"


def _plain(root: Path, *args: str) -> None:
    """The reader's command without `winproc.untrusted_git`: it shows the setting is live. A
    command that fails can still have started the program first."""
    with contextlib.suppress(RuntimeError):
        winproc.git(root, *args)


def _stat_dirty(path: Path) -> None:
    """Same bytes, older mtime, as a copied or synced tree has: `status` hashes the file again,
    through any clean filter."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns - 5_000_000_000))


def _signed(root: Path, *prefix: str) -> None:
    """HEAD rewritten with a signature header, which `log.showSignature` hands to the
    `gpg.program` the folder names. A hostile folder carries such a commit itself."""
    head, _, body = _git(root, *prefix, "cat-file", "commit", "HEAD").partition("\n\n")
    raw = root.parent / f"{root.name}-commit.txt"
    raw.write_bytes(f"{head}\n{SIGNATURE}\n\n{body}".encode())
    oid = _git(root, *prefix, "hash-object", "-t", "commit", "-w", raw.as_posix()).strip()
    _git(root, *prefix, "update-ref", "refs/heads/main", oid)


def _verifier(root: Path, marker: Path, *prefix: str) -> None:
    """A signature program shipped in the folder and named by a relative path."""
    tool = root / "verify"
    tool.write_text(f"#!/bin/sh\n{_command(marker)}\nexit 1\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    _git(root, *prefix, "config", "log.showSignature", "true")
    _git(root, *prefix, "config", "gpg.program", "./verify")


def _objects(root: Path) -> int:
    return sum(1 for path in (root / ".git" / "objects").rglob("*") if path.is_file())


# --- the vault chip ---------------------------------------------------------------------------


@needs_git
def test_the_vault_chip_runs_no_fsmonitor_hook(tmp_path: Path) -> None:
    vault = _repo(tmp_path / "vault")
    marker = tmp_path / "ran"
    _git(vault, "config", "core.fsmonitor", _command(marker, "; exit 1"))

    chip = rails.rails_read(vault, tmp_path / "project").git

    assert (chip.state, chip.branch, chip.dirty) == ("known", "main", 0)
    assert not marker.exists()
    _plain(vault, "status", "--porcelain")
    assert marker.exists()


@needs_git
@pytest.mark.parametrize("named_by", ["gitattributes", "info-attributes", "required-process"])
def test_the_vault_chip_runs_no_filter_the_folder_names(tmp_path: Path, named_by: str) -> None:
    vault = _repo(tmp_path / "vault")
    marker = tmp_path / "ran"
    if named_by == "gitattributes":
        (vault / ".gitattributes").write_text("*.md filter=x\n", encoding="utf-8", newline="\n")
        _git(vault, "add", ".gitattributes")
        _git(vault, "commit", "-q", "-m", "attributes")
    else:
        # Read whatever `--attr-source` says, and a plain file of an unpacked folder.
        (vault / ".git" / "info").mkdir(exist_ok=True)
        attributes = vault / ".git" / "info" / "attributes"
        attributes.write_text("*.md filter=x\n", encoding="utf-8", newline="\n")
    if named_by == "required-process":
        _git(vault, "config", "filter.x.process", _command(marker, "; exit 1"))
        _git(vault, "config", "filter.x.required", "true")
    else:
        _git(vault, "config", "filter.x.clean", _command(marker, "; cat"))
    _stat_dirty(vault / "note.md")

    chip = rails.git_chip(vault)

    assert (chip.state, chip.dirty) == ("known", 0)
    assert not marker.exists()
    _plain(vault, "status", "--porcelain")
    assert marker.exists()


@needs_git
def test_the_vault_chip_stays_out_of_a_submodule_and_its_own_filters(tmp_path: Path) -> None:
    origin = _repo(tmp_path / "origin")
    (origin / ".gitattributes").write_text("*.md filter=z\n", encoding="utf-8", newline="\n")
    _git(origin, "add", ".gitattributes")
    _git(origin, "commit", "-q", "-m", "attributes")
    vault = _repo(tmp_path / "vault")
    _git(vault, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    _git(vault, "commit", "-q", "-m", "submodule")
    marker = tmp_path / "ran"
    _git(vault / "sub", "config", "filter.z.clean", _command(marker, "; cat"))
    _stat_dirty(vault / "sub" / "note.md")

    chip = rails.git_chip(vault)

    assert (chip.state, chip.dirty) == ("known", 0)
    assert not marker.exists()
    # The untrusted read switches off only this folder's drivers: a status that went into the
    # submodule would still start the submodule's own.
    with winproc.untrusted_git():
        winproc.git(vault, "status", "--porcelain")
    assert marker.exists()


# --- the chip and a note's history ------------------------------------------------------------


@needs_git
def test_neither_the_chip_nor_a_note_history_starts_a_signature_program(tmp_path: Path) -> None:
    vault = _repo(tmp_path / "vault", commits=2)
    marker = tmp_path / "ran"
    _signed(vault)
    _verifier(vault, marker)

    chip = rails.git_chip(vault)
    history = note_history(vault, "note.md")

    assert chip.state == "known" and chip.committed_at
    assert history.state == "ready" and len(history.commits) == 2
    assert not marker.exists()
    _plain(vault, "log", "-1", "--format=%cI")
    assert marker.exists()
    marker.unlink()
    _plain(vault, "--literal-pathspecs", "log", "-n", "20", "--format=%h", "--", "note.md")
    assert marker.exists()


@needs_git
@pytest.mark.parametrize("reader", ["chip", "history"])
def test_a_missing_object_starts_no_promisor_fetch_and_writes_no_pack(
    tmp_path: Path, reader: str
) -> None:
    """A promisor remote's `uploadpack` is a shell command, and the folder's own settings allow
    the file transport by name; a lazy fetch would run it and write packs into the folder."""
    origin = _repo(tmp_path / "origin", commits=2)
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(origin), "vault")
    vault = tmp_path / "vault"
    head = _git(vault, "rev-parse", "HEAD").strip()
    loose = vault / ".git" / "objects" / head[:2] / head[2:]
    loose.chmod(stat.S_IWRITE)  # Git writes objects read-only, which Windows will not unlink
    loose.unlink()
    marker = tmp_path / "ran"
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.uploadpack", _command(marker, "; git-upload-pack")),
        ("protocol.file.allow", "always"),
    ):
        _git(vault, "config", key, value)
    before = _objects(vault)

    if reader == "chip":
        assert rails.git_chip(vault).state == "unknown"
    else:
        assert note_history(vault, "note.md").state == "unknown"

    assert not marker.exists()
    assert _objects(vault) == before
    _plain(vault, "log", "-1", "--format=%cI")
    assert marker.exists() and _objects(vault) > before


@needs_git
def test_a_folder_without_a_git_entry_is_no_repository_to_the_app(tmp_path: Path) -> None:
    """The repository's files moved up into the folder, `.git` gone: Git opens that layout of
    plain files as a repository, so its settings are anyone's who wrote the folder."""
    vault = _repo(tmp_path / "vault", commits=2)
    _signed(vault)
    for entry in list((vault / ".git").iterdir()):
        shutil.move(str(entry), str(vault / entry.name))
    (vault / ".git").rmdir()
    bare = ("--git-dir=.",)
    _git(vault, *bare, "config", "core.bare", "false")
    _git(vault, *bare, "config", "core.worktree", ".")
    marker = tmp_path / "ran"
    _verifier(vault, marker, *bare)
    _git(vault, *bare, "config", "core.fsmonitor", _command(marker, "; exit 1"))

    chip = rails.git_chip(vault)
    history = note_history(vault, "note.md")
    identity = probe_identity(canonical_root(str(vault), deny=()), git_available=True)

    assert (chip.branch, chip.dirty) == ("", None) and chip.state != "known"
    assert history.state == "unknown"
    assert not identity.established and identity.git_root_oids == ()
    assert not marker.exists()
    _plain(vault, "log", "-1", "--format=%cI")
    assert marker.exists()
