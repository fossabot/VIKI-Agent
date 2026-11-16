"""The code save service: the editor's text becomes bytes in the file's own encoding and newline,
and reaches the publication service only after every refusal that needs no write."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from tests.app.test_workspaces import COMMAND, protected_at, service_at, store_at
from tests.vaults.test_vault import short_name
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_save import CodeSaver, Encoding, Newline, SaveResult, encode_text
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    MutationCoordinator,
    MutationError,
)
from viki_agent.vaults.vault_publication import (
    REQUEST_LEASE_WAIT_S,
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    VaultPublicationService,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

ENCODINGS: tuple[Encoding, ...] = ("utf-8", "utf-8-bom", "utf-16-le", "utf-16-be")
NEWLINES: tuple[Newline, ...] = ("lf", "crlf", "cr", "none")
# What the file holds before the test, byte for byte: a BOM and a codec the test names itself.
BOM_AND_CODEC = {
    "utf-8": (b"", "utf-8"),
    "utf-8-bom": (b"\xef\xbb\xbf", "utf-8"),
    "utf-16-le": (b"\xff\xfe", "utf-16-le"),
    "utf-16-be": (b"\xfe\xff", "utf-16-be"),
}
BREAK = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(folder: Path) -> set[str]:
    """Every entry under the folder, so a stage or backup left behind shows by name."""
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*")}


class StubPublications:
    """Records `save_code` calls and answers `saved`, or raises what it was given."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.raises = raises

    async def save_code(self, *args: Any, **kwargs: Any) -> PublicationOutcome:
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return PublicationOutcome("operation", "saved", cleanup_pending=True)


@dataclass
class Env:
    service: WorkspaceService
    saver: CodeSaver
    files: CodeFiles
    journal: PublicationJournal
    publications: StubPublications | VaultPublicationService
    workspace_id: str
    revision: int
    physical_id: str
    root: Path

    def save(
        self,
        ref: str,
        text: str,
        *,
        encoding: Encoding = "utf-8",
        newline: Newline = "lf",
        expected: str | None = None,
    ) -> SaveResult:
        return asyncio.run(
            self.saver.save(
                self.workspace_id,
                self.revision,
                self.physical_id,
                ref,
                text,
                encoding=encoding,
                newline=newline,
                expected_sha256=expected,
            )
        )


def _env(tmp_path: Path, *, trust: bool = True, stub: StubPublications | None = None) -> Env:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = tmp_path / "repo"
    root.mkdir()
    if trust:
        workspace_id = trusted(service, root)
    else:
        workspace_id = service.register_existing(COMMAND, str(root), kind="code")[0].workspace_id
    code = service.get(workspace_id, kind="code")
    journal = PublicationJournal(store)
    publications: StubPublications | VaultPublicationService
    if stub is not None:
        publications = stub
    else:
        resolver = WorkspaceMutationResolver(service, git_available=lambda: False)
        publications = VaultPublicationService(
            journal, functools.partial(resolver, kind=None), MutationCoordinator()
        )
    saver = CodeSaver(service, cast(Any, publications))
    return Env(
        service,
        saver,
        CodeFiles(service),
        journal,
        publications,
        workspace_id,
        code.policy_revision,
        code.root.physical_id,
        root,
    )


# --- encode_text ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("newline", "expected"),
    [("lf", "a\nb\nc\n"), ("crlf", "a\r\nb\r\nc\r\n"), ("cr", "a\rb\rc\r"), ("none", "a\nb\nc\n")],
)
def test_every_break_of_the_text_becomes_the_files_newline(newline: Newline, expected: str) -> None:
    # CRLF, a lone CR and LF are all editor breaks; none of them doubles when converted.
    text = "a\r\nb\rc\n"
    assert encode_text(text, encoding="utf-8", newline=newline) == expected.encode()


@pytest.mark.parametrize(
    ("encoding", "bom", "codec"),
    [(name, bom, codec) for name, (bom, codec) in BOM_AND_CODEC.items()],
)
def test_the_bom_and_codec_follow_the_encoding(encoding: Encoding, bom: bytes, codec: str) -> None:
    text = "héllo 日本語 \U0001f600"
    assert encode_text(text, encoding=encoding, newline="lf") == bom + text.encode(codec)


def test_a_lone_surrogate_is_invalid_content() -> None:
    for encoding in ENCODINGS:
        with pytest.raises(CodeError) as refused:
            encode_text("a\ud800b", encoding=encoding, newline="lf")
        assert refused.value.code == "invalid_content"


@pytest.mark.parametrize(
    ("encoding", "newline"),
    [("latin-1", "lf"), ("utf-8-sig", "lf"), ("", "lf"), ("utf-8", "mixed"), ("utf-8", "LF")],
)
def test_an_encoding_or_newline_outside_the_sets_is_invalid_body(
    encoding: str, newline: str
) -> None:
    with pytest.raises(CodeError) as refused:
        encode_text("x", encoding=cast(Any, encoding), newline=cast(Any, newline))
    assert refused.value.code == "invalid_body"


# --- refusals before a write (every platform, a stub behind the saver) ------------------------


@pytest.mark.parametrize(
    ("ref", "code"),
    [
        (".git/config", "private_path"),
        (".env", "private_path"),
        ("src/id_rsa", "private_path"),
        (".viki-stage-x", "private_path"),
        ("a//b", "invalid_path"),
        ("", "invalid_path"),
    ],
)
def test_a_private_or_malformed_ref_is_refused_and_nothing_is_written(
    tmp_path: Path, ref: str, code: str
) -> None:
    stub = StubPublications()
    env = _env(tmp_path, stub=stub)
    before = _files(env.root)
    with pytest.raises(CodeError) as refused:
        env.save(ref, "x")
    assert refused.value.code == code
    assert (stub.calls, _files(env.root)) == ([], before)


def test_an_untrusted_folder_needs_trust(tmp_path: Path) -> None:
    stub = StubPublications()
    env = _env(tmp_path, trust=False, stub=stub)
    with pytest.raises(CodeError) as refused:
        env.save("a.py", "x")
    assert refused.value.code == "trust_required"
    assert stub.calls == []


def test_a_code_folder_bound_protected_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    vault = tmp_path / "VIKI"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    protected_at(service, vault)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    (root / "a.txt").write_bytes(b"before\n")
    code = service.register_existing(COMMAND, str(root), kind="code")[0]
    assert code.policy == "protected"
    journal = PublicationJournal(store)
    resolver = WorkspaceMutationResolver(service, git_available=lambda: False)
    publications = VaultPublicationService(
        journal, functools.partial(resolver, kind=None), MutationCoordinator()
    )
    saver = CodeSaver(service, cast(Any, publications))
    with pytest.raises(CodeError) as refused:
        asyncio.run(
            saver.save(
                code.workspace_id,
                code.policy_revision,
                code.root.physical_id,
                "a.txt",
                "after\n",
                encoding="utf-8",
                newline="lf",
                expected_sha256=_sha(b"before\n"),
            )
        )
    assert refused.value.code == "trust_required"
    assert (root / "a.txt").read_bytes() == b"before\n"
    assert _files(root) == {"CLAUDE.md", "a.txt"}
    assert journal.list_pending() == []


def test_a_vault_id_is_an_unknown_workspace(tmp_path: Path) -> None:
    stub = StubPublications()
    env = _env(tmp_path, stub=stub)
    vault_root = tmp_path / "notes"
    vault_root.mkdir()
    vault, _ = env.service.register_existing(COMMAND, str(vault_root))
    env.workspace_id = vault.workspace_id
    with pytest.raises(CodeError) as refused:
        env.save("a.py", "x")
    assert refused.value.code == "unknown_workspace"
    assert stub.calls == []


def test_a_lone_surrogate_is_refused_before_the_service(tmp_path: Path) -> None:
    stub = StubPublications()
    env = _env(tmp_path, stub=stub)
    with pytest.raises(CodeError) as refused:
        env.save("a.py", "a\ud800b")
    assert refused.value.code == "invalid_content"
    assert stub.calls == []


def test_the_encoded_size_decides_the_cap(tmp_path: Path) -> None:
    stub = StubPublications()
    env = _env(tmp_path, stub=stub)
    with pytest.raises(CodeError) as refused:
        env.save("a.py", "a" * (MAX_PUBLICATION_BYTES + 1))
    assert refused.value.code == "too_large"
    # Characters under the cap still encode past it: a BOM and two bytes each.
    with pytest.raises(CodeError) as refused:
        env.save("a.py", "a" * (MAX_PUBLICATION_BYTES // 2), encoding="utf-16-le")
    assert refused.value.code == "too_large"
    assert stub.calls == []
    assert env.save("a.py", "a" * MAX_PUBLICATION_BYTES).status == "saved"
    assert len(stub.calls) == 1


@pytest.mark.parametrize(
    "error", [MutationError("vault_locked"), PublicationError("journal_unavailable")]
)
def test_a_service_error_passes_through_unchanged(tmp_path: Path, error: Exception) -> None:
    env = _env(tmp_path, stub=StubPublications(raises=error))
    with pytest.raises(type(error)) as refused:
        env.save("a.py", "x")
    assert refused.value is error


def test_the_stub_receives_the_encoded_bytes_and_the_caller_hash(tmp_path: Path) -> None:
    stub = StubPublications()
    env = _env(tmp_path, stub=stub)
    result = env.save("src/a.py", "é\n", encoding="utf-16-be", newline="crlf", expected="f" * 64)
    content = b"\xfe\xff\x00\xe9\x00\r\x00\n"
    assert stub.calls == [
        (
            (env.workspace_id, env.revision, env.physical_id, "src/a.py", content),
            {"expected_sha256": "f" * 64, "wait_s": REQUEST_LEASE_WAIT_S},
        )
    ]
    assert result == SaveResult("operation", "saved", _sha(content), True)


# --- the real write path ------------------------------------------------------------


def _file_text(newline: Newline) -> str:
    if newline == "none":
        return "alpha"
    return BREAK[newline].join(["alpha", "beta", "gamma", ""])


@pytest.mark.parametrize("newline", NEWLINES)
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_a_changed_line_saves_byte_exact(
    encoding: Encoding, newline: Newline, tmp_path: Path
) -> None:
    env = _env(tmp_path)
    bom, codec = BOM_AND_CODEC[encoding]
    before = _file_text(newline)
    (env.root / "a.txt").write_bytes(bom + before.encode(codec))

    read = env.files.read(env.workspace_id, "a.txt")
    assert (read.encoding, read.newline) == (encoding, newline)
    assert read.text is not None and read.sha256 is not None
    editor = read.text.replace("\r\n", "\n").replace("\r", "\n")
    assert "\r" not in editor
    edited = (
        editor.replace("alpha", "ALPHA") if newline == "none" else editor.replace("beta", "BETA")
    )

    result = env.save(
        "a.txt",
        edited,
        encoding=cast(Encoding, read.encoding),
        newline=cast(Newline, read.newline),
        expected=read.sha256,
    )

    after = (
        before.replace("alpha", "ALPHA") if newline == "none" else before.replace("beta", "BETA")
    )
    expected = bom + after.encode(codec)
    assert (env.root / "a.txt").read_bytes() == expected
    assert result.status == "saved" and result.sha256 == _sha(expected)
    assert env.journal.list_pending() == []
    assert _files(env.root) == {"a.txt"}


def test_crlf_utf16_round_trips_byte_exact(tmp_path: Path) -> None:
    env = _env(tmp_path)
    original = b"\xff\xfe" + "one\r\ntwo\r\n".encode("utf-16-le")
    (env.root / "a.txt").write_bytes(original)
    read = env.files.read(env.workspace_id, "a.txt")
    assert read.text is not None and read.sha256 is not None

    unchanged = env.save(
        "a.txt",
        read.text.replace("\r\n", "\n"),
        encoding="utf-16-le",
        newline="crlf",
        expected=read.sha256,
    )
    assert unchanged.status == "no_change"
    assert (env.root / "a.txt").read_bytes() == original

    edited = env.save(
        "a.txt", "one\nTWO\n", encoding="utf-16-le", newline="crlf", expected=read.sha256
    )
    assert edited.status == "saved"
    assert (env.root / "a.txt").read_bytes() == b"\xff\xfe" + "one\r\nTWO\r\n".encode("utf-16-le")


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_unchanged_text_is_no_change_and_leaves_the_bytes(
    encoding: Encoding, tmp_path: Path
) -> None:
    env = _env(tmp_path)
    bom, codec = BOM_AND_CODEC[encoding]
    original = bom + "alpha\r\nbeta\r\n".encode(codec)
    (env.root / "a.txt").write_bytes(original)
    read = env.files.read(env.workspace_id, "a.txt")
    assert read.text is not None and read.sha256 is not None

    result = env.save(
        "a.txt",
        read.text.replace("\r\n", "\n"),
        encoding=encoding,
        newline="crlf",
        expected=read.sha256,
    )

    assert (result.status, result.sha256) == ("no_change", _sha(original))
    assert (env.root / "a.txt").read_bytes() == original
    assert env.journal.list_pending() == []
    assert _files(env.root) == {"a.txt"}


def test_a_changed_file_is_a_conflict_and_stays_as_it_is(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.txt").write_bytes(b"read by the editor\n")
    stale = _sha(b"read by the editor\n")
    (env.root / "a.txt").write_bytes(b"changed on disk\n")

    result = env.save("a.txt", "from the editor\n", expected=stale)

    assert (result.status, result.sha256) == ("conflict", None)
    assert (env.root / "a.txt").read_bytes() == b"changed on disk\n"
    assert env.journal.list_pending() == []
    assert _files(env.root) == {"a.txt"}  # no .viki-stage-* or .viki-backup-* entry


def test_no_hash_creates_an_absent_name_and_conflicts_on_an_existing_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    created = env.save("new.py", "print(1)\n")
    assert created.status == "saved" and created.sha256 == _sha(b"print(1)\n")
    assert (env.root / "new.py").read_bytes() == b"print(1)\n"

    again = env.save("new.py", "print(2)\n")
    assert (again.status, again.sha256) == ("conflict", None)
    assert (env.root / "new.py").read_bytes() == b"print(1)\n"
    assert env.journal.list_pending() == []
    assert _files(env.root) == {"new.py"}


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names exist only on NTFS")
def test_a_short_name_of_a_private_folder_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / ".git").mkdir()
    (env.root / ".git" / "config").write_bytes(b"[core]\n")
    (env.root / ".claude").mkdir()
    git, claude = short_name(env.root / ".git"), short_name(env.root / ".claude")
    if git == ".git" or claude == ".claude":
        pytest.skip("this volume makes no 8.3 names")

    with pytest.raises(MutationError) as replace:
        env.save(f"{git}/config", "[core]\nbare = true\n", expected=_sha(b"[core]\n"))
    with pytest.raises(MutationError) as create:
        env.save(f"{claude}/x", "x\n")

    # The route answers both as 403 private_path (test_code_save_api.py pins the rename).
    assert (replace.value.code, create.value.code) == ("excluded_path", "excluded_path")
    assert (env.root / ".git" / "config").read_bytes() == b"[core]\n"
    assert _files(env.root) == {".git", ".git/config", ".claude"}
    assert env.journal.list_pending() == []
