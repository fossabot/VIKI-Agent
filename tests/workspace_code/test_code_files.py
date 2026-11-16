"""The code-folder reader, all in `tmp_path`."""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from tests.app.test_workspaces import COMMAND, make_junction, service_at
from viki_agent import osfs
from viki_agent.code import code_files
from viki_agent.code.code_files import CodeError, CodeFiles, Entry, is_private, split_ref

Repo = tuple[CodeFiles, str, Path]

REFUSED = (
    "/abs/x.py",
    "/",
    "C:/x.py",
    "C:x.py",
    "c:",
    "\\\\host\\share\\x.py",
    "//host/share/x.py",
    "\\x.py",
    "a\\b.py",
    "a:b.py",
    "a/b.py:stream",
    "a\x00b.py",
    "a\x01b.py",
    "a\x1fb.py",
    "a\x7fb.py",
    "a\x85b.py",
    "a\ud800.py",
    "a//b.py",
    "a/./b.py",
    "a/../b.py",
    "..",
    ".",
    "a/",
    "CON",
    "con.txt",
    "src/Nul.py",
    "lpt9.log",
    "COM1",
    "name.",
    "name ",
    "x" * (code_files.MAX_REF_CHARS + 1),
)
PRIVATE = (
    ".git",
    ".git/config",
    ".GIT/HEAD",
    "src/.claude/settings.json",
    ".viki",
    ".env",
    ".env.production",
    "credentials.json",
    ".git-credentials",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    "home/.ssh/config",
    ".aws/credentials",
    ".gnupg",
    "id_rsa",
    "ID_ED25519",
    "keys/server.PEM",
    "tls.key",
    "cert.p12",
    "cert.pfx",
    "putty.ppk",
    "vault.kdbx",
)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    service = service_at(tmp_path)
    workspace, _ = service.register_existing(COMMAND, str(root), kind="code")
    return CodeFiles(service), workspace.workspace_id, root


def code(call: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(CodeError) as refused:
        call(*args, **kwargs)
    return refused.value.code


def link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        make_junction(link, target)
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("this volume refuses symlinks")


# --- refs ---------------------------------------------------------------------------------


def test_split_ref_accepts_plain_refs() -> None:
    assert split_ref("") == ()
    assert split_ref("a.py") == ("a.py",)
    assert split_ref("src/.github/Main Ünïcode.py") == ("src", ".github", "Main Ünïcode.py")
    assert split_ref("CONSOLE.md/ok") == ("CONSOLE.md", "ok")
    assert split_ref("x" * code_files.MAX_REF_CHARS) == ("x" * code_files.MAX_REF_CHARS,)


@pytest.mark.parametrize("ref", REFUSED)
def test_split_ref_refuses_every_unsafe_shape(ref: str) -> None:
    assert code(lambda: split_ref(ref)) == "invalid_path"


@pytest.mark.parametrize("ref", PRIVATE)
def test_private_parts_are_private_at_any_depth_and_case(ref: str) -> None:
    assert is_private(split_ref(ref))


def test_near_private_names_are_not_private() -> None:
    for ref in (".github/workflows", ".gitignore", "env", "id_rsa.pub", "keys.md", "src/pem"):
        assert not is_private(split_ref(ref)), ref


def test_refused_refs_never_touch_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grammar and private refusals happen before the workspace lookup or any filesystem call."""

    def touched(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("a refused ref reached the workspace service or the filesystem")

    class Untouchable:
        get = current_root = staticmethod(touched)

    files = CodeFiles(Untouchable())  # type: ignore[arg-type]
    workspace_id = str(uuid.uuid4())
    cases = [(ref, "invalid_path") for ref in REFUSED] + [(ref, "private_path") for ref in PRIVATE]
    # The context undoes the patches before pytest reports a failure, which itself stats files.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", touched)
        patch.setattr(Path, "stat", touched)
        patch.setattr(os, "stat", touched)
        patch.setattr(os, "scandir", touched)
        patch.setattr(osfs, "read_regular", touched)
        for ref, expected in cases:
            assert code(files.resolve, workspace_id, ref, want="file") == expected, ref
            assert code(files.resolve, workspace_id, ref, want="dir") == expected, ref
            assert code(files.tree, workspace_id, ref) == expected, ref
            assert code(files.read, workspace_id, ref) == expected, ref
            assert files.open_target(workspace_id, ref) is None, ref


# --- workspace --------------------------------------------------------------------------


def test_unknown_ids_and_vault_ids_are_unknown(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "note.md").write_text("# n\n", encoding="utf-8")
    service = service_at(tmp_path)
    vault, _ = service.register_existing(COMMAND, str(notes))
    files = CodeFiles(service)
    for workspace_id in (str(uuid.uuid4()), vault.workspace_id):
        assert code(files.read, workspace_id, "note.md") == "unknown_workspace"
        assert code(files.tree, workspace_id) == "unknown_workspace"
        assert files.open_target(workspace_id, "note.md") is None


def test_a_vanished_root_is_unavailable(repo: Repo) -> None:
    files, workspace_id, root = repo
    root.rename(root.with_name("moved"))
    assert code(lambda: files.tree(workspace_id)) == "root_unavailable"
    assert files.open_target(workspace_id, "a.py") is None


# --- tree -------------------------------------------------------------------------------


def test_tree_lists_sorted_with_private_names_hidden(repo: Repo) -> None:
    files, workspace_id, root = repo
    for directory in ("b_dir", "A_dir", ".git", ".ssh", "src"):
        (root / directory).mkdir()
    for name, body in (("z.py", b"zz"), ("B.txt", b"b"), ("a.md", b""), ("src/m.py", b"m")):
        (root / name).write_bytes(body)
    for name in (".env", "server.pem", "id_rsa", ".npmrc"):
        (root / name).write_bytes(b"secret")
    if sys.platform != "win32":
        for name in ("back\\slash", "co:lon", "trailing."):
            (root / name).write_bytes(b"x")

    listing = files.tree(workspace_id)
    assert listing.path == "" and not listing.truncated
    assert listing.entries == (
        Entry("A_dir", "dir", None),
        Entry("b_dir", "dir", None),
        Entry("src", "dir", None),
        Entry("a.md", "file", 0),
        Entry("B.txt", "file", 1),
        Entry("z.py", "file", 2),
    )
    assert files.tree(workspace_id, "src").entries == (Entry("m.py", "file", 1),)
    assert code(lambda: files.tree(workspace_id, "z.py")) == "not_a_directory"
    assert code(lambda: files.tree(workspace_id, "missing")) == "not_found"


def test_tree_holds_at_most_max_entries_and_says_truncated(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, workspace_id, root = repo
    monkeypatch.setattr(code_files, "MAX_ENTRIES", 3)
    for name in ("d.py", "c.py", ".env", "b.py"):
        (root / name).write_bytes(b"")
    exact = files.tree(workspace_id)
    assert [entry.name for entry in exact.entries] == ["b.py", "c.py", "d.py"]
    assert not exact.truncated  # a hidden name does not count
    (root / "a.py").write_bytes(b"")
    over = files.tree(workspace_id)
    assert len(over.entries) == 3 and over.truncated
    names = [entry.name for entry in over.entries]
    assert names == sorted(names)


def test_links_are_listed_as_links_and_an_outside_link_is_refused(
    tmp_path: Path, repo: Repo
) -> None:
    files, workspace_id, root = repo
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"print(1)\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    link_dir(root / "out", outside)
    link_dir(root / "inner", root / "src")
    link_dir(root / "gitlink", root / ".git")

    assert files.tree(workspace_id).entries == (
        Entry("src", "dir", None),
        Entry("gitlink", "link", None),
        Entry("inner", "link", None),
        Entry("out", "link", None),
    )
    assert code(lambda: files.read(workspace_id, "out/secret.txt")) == "invalid_path"
    assert code(lambda: files.tree(workspace_id, "out")) == "invalid_path"
    assert files.open_target(workspace_id, "out/secret.txt") is None
    assert code(lambda: files.read(workspace_id, "gitlink/config")) == "private_path"
    assert files.open_target(workspace_id, "gitlink/config") is None
    assert files.read(workspace_id, "inner/a.py").text == "print(1)\n"


# --- read -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "encoding", "newline", "text"),
    [
        (b"a\nb\n", "utf-8", "lf", "a\nb\n"),
        ("é\r\nü\r\n".encode(), "utf-8", "crlf", "é\r\nü\r\n"),
        (b"\xef\xbb\xbfa\r\nb", "utf-8-bom", "crlf", "a\r\nb"),
        (b"\xff\xfe" + "a\rb\r".encode("utf-16-le"), "utf-16-le", "cr", "a\rb\r"),
        (b"\xfe\xff" + "a\nb\r\nc".encode("utf-16-be"), "utf-16-be", "mixed", "a\nb\r\nc"),
        (b"\xff\xfe" + "a\r\nb\rc\n".encode("utf-16-le"), "utf-16-le", "mixed", "a\r\nb\rc\n"),
        (b"one line", "utf-8", "none", "one line"),
        (b"", "utf-8", "none", ""),
    ],
)
def test_text_decodes_with_encoding_newline_and_raw_sha256(
    repo: Repo, raw: bytes, encoding: str, newline: str, text: str
) -> None:
    files, workspace_id, root = repo
    (root / "f.txt").write_bytes(raw)
    preview = files.read(workspace_id, "f.txt")
    assert (preview.path, preview.size, preview.reason) == ("f.txt", len(raw), None)
    assert preview.sha256 == hashlib.sha256(raw).hexdigest()
    assert (preview.encoding, preview.newline, preview.text) == (encoding, newline, text)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"PK\x03\x04\x00\x00", "binary"),
        (b"\xef\xbb\xbfa\x00b", "binary"),
        (b"x" * code_files.BINARY_SNIFF_BYTES + b"\xff\x00", "unsupported_encoding"),
        (b"caf\xe9", "unsupported_encoding"),
        (b"\xed\xa0\x80", "unsupported_encoding"),  # an encoded lone surrogate
        (b"\xff\xfea\x00b", "unsupported_encoding"),  # odd UTF-16 length
        (b"\xff\xfe\x00\xd8a\x00", "unsupported_encoding"),  # a lone UTF-16 surrogate
    ],
)
def test_binary_and_unsupported_text_give_a_reason_and_no_text(
    repo: Repo, raw: bytes, reason: str
) -> None:
    files, workspace_id, root = repo
    (root / "f.bin").write_bytes(raw)
    preview = files.read(workspace_id, "f.bin")
    assert (preview.reason, preview.text, preview.encoding, preview.newline) == (
        reason,
        None,
        None,
        None,
    )
    assert preview.sha256 == hashlib.sha256(raw).hexdigest()


def test_over_the_preview_limit_is_too_large_without_a_read(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, workspace_id, root = repo
    limit = code_files.MAX_PREVIEW_BYTES
    (root / "edge.txt").write_bytes(b"a" * limit)
    assert files.read(workspace_id, "edge.txt").text == "a" * limit
    (root / "big.txt").write_bytes(b"a" * (limit + 1))

    def no_read(*_args: Any, **_kwargs: Any) -> bytes:
        pytest.fail("a file over the limit was read")

    monkeypatch.setattr(osfs, "read_regular", no_read)
    preview = files.read(workspace_id, "big.txt")
    assert preview == code_files.FileText("big.txt", limit + 1, None, None, None, None, "too_large")


def test_a_file_that_grows_after_the_stat_is_too_large(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, workspace_id, root = repo
    (root / "grows.txt").write_bytes(b"small")
    calls: list[tuple[int, bool]] = []

    def grown(
        _path: Any,
        limit: int,
        *,
        follow_symlinks: bool = True,
        pre_read: Callable[[int], None] | None = None,
    ) -> bytes:
        calls.append((limit, follow_symlinks))
        return b"a" * limit

    monkeypatch.setattr(osfs, "read_regular", grown)
    preview = files.read(workspace_id, "grows.txt")
    assert calls == [(code_files.MAX_PREVIEW_BYTES + 1, False)]
    assert (preview.reason, preview.sha256, preview.text) == ("too_large", None, None)


def test_read_refuses_directories_and_missing_files(repo: Repo) -> None:
    files, workspace_id, root = repo
    (root / "src").mkdir()
    (root / "a.py").write_bytes(b"")
    assert code(lambda: files.read(workspace_id, "src")) == "not_a_file"
    assert code(lambda: files.read(workspace_id, "")) == "not_a_file"
    assert code(lambda: files.read(workspace_id, "missing.py")) == "not_found"
    assert code(lambda: files.read(workspace_id, "a.py/below")) == "not_found"


@pytest.mark.parametrize("replacement", ["parent", "root", "private", "file"])
def test_final_open_refuses_a_replaced_path_before_reading_bytes(
    tmp_path: Path, repo: Repo, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    files, workspace_id, root = repo
    directory = root / "src"
    directory.mkdir()
    (directory / "a.txt").write_bytes(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_bytes(b"outside synthetic sentinel\n")
    reads: list[int] = []
    real_read = osfs.read_regular

    if sys.platform == "win32":
        real_open = open

        def spy_open(*args: Any, **kwargs: Any) -> BinaryIO:
            source = real_open(*args, **kwargs)
            read = source.read

            def spy_read(size: int = -1) -> bytes:
                reads.append(size)
                return read(size)

            source.read = spy_read  # type: ignore[method-assign]
            return source

        monkeypatch.setattr(osfs, "open", spy_open, raising=False)
    else:
        real_os_read = os.read

        def spy_os_read(fd: int, size: int) -> bytes:
            reads.append(size)
            return real_os_read(fd, size)

        monkeypatch.setattr(os, "read", spy_os_read)

    def swap(
        path: str | os.PathLike[str],
        limit: int,
        *,
        follow_symlinks: bool = True,
        pre_read: Callable[[int], None] | None = None,
    ) -> bytes:
        if replacement == "root":
            old_root = tmp_path / "old-root"
            root.rename(old_root)
            (root / "src").mkdir(parents=True)
            os.link(old_root / "src" / "a.txt", root / "src" / "a.txt")
        elif replacement == "private":
            private = root / ".ssh"
            directory.rename(private)
            (private / "a.txt").write_bytes(b"private synthetic sentinel\n")
            link_dir(directory, private)
        elif replacement == "file":
            old_file = directory / "a.txt"
            old_file.rename(tmp_path / "old-a.txt")
            old_file.write_bytes(b"replacement file sentinel\n")
        else:
            directory.rename(tmp_path / "old-src")
            link_dir(directory, outside)
        if pre_read is None:
            return real_read(path, limit, follow_symlinks=follow_symlinks)
        return real_read(path, limit, follow_symlinks=follow_symlinks, pre_read=pre_read)

    monkeypatch.setattr(osfs, "read_regular", swap)
    assert code(files.read, workspace_id, "src/a.txt") in ("invalid_path", "private_path")
    assert reads == []


def test_a_smaller_read_limit_never_hashes_a_partial_file_and_charges_no_stat_only_refusal(
    repo: Repo,
) -> None:
    _, _, root = repo
    (root / "a.txt").write_bytes(b"abcdef")
    consumed: list[int] = []
    shown = code_files.read_at(root, "a.txt", max_bytes=5, on_read=consumed.append)
    assert shown == code_files.FileText("a.txt", 6, None, None, None, None, "too_large")
    assert consumed == []


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive macOS volume control")
def test_read_at_accepts_the_same_root_with_different_case_on_macos(tmp_path: Path) -> None:
    root = tmp_path / "MixedCaseRoot"
    (root / "MixedCaseDir").mkdir(parents=True)
    (root / "MixedCaseDir" / "File.txt").write_bytes(b"same file\n")
    alternate = tmp_path / "mixedcaseroot"
    if not alternate.is_dir():
        pytest.skip("this volume is case-sensitive")
    assert code_files.read_at(alternate, "mixedcasedir/file.txt").text == "same file\n"


# --- open_target ------------------------------------------------------------------------


def test_open_target_is_the_resolved_file_or_none(repo: Repo) -> None:
    files, workspace_id, root = repo
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"")
    (root / ".env").write_bytes(b"secret")
    target = files.open_target(workspace_id, "src/a.py")
    assert target == (root / "src" / "a.py").resolve(strict=True)
    assert files.open_target(workspace_id, "") == root.resolve(strict=True)
    for ref in ("src", ".env", "missing.py", "../repo/src/a.py", "C:/x.py"):
        assert files.open_target(workspace_id, ref) is None, ref
