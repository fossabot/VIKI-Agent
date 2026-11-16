"""Synthetic standalone trees refuse private inputs before their contents are read."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_files  # noqa: E402

from viki_agent import winproc  # noqa: E402

_CERTIFICATES = (
    b"# synthetic public bundle\n-----BEGIN CERTIFICATE-----\n"
    b"U3ludGhldGlj\n-----END CERTIFICATE-----\n"
)


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        "config/.ENV.production",
        "production.env",
        "production.env.local",
        "credentials.json",
        "." + "credentials",
        "config/client-credentials.json",
        "state.json",
        "sealing-key-id",
        "session.db",
        "store.sqlite",
        "store.sqlite3-wal",
        "store.db-shm",
        "store.sqlite-journal",
        "private.pem",
        "client.key",
        "signing.p8",
        "cert.p12",
        "cert.pfx",
        "debug.log",
        "assets/app.js.map",
        "nested/../file",
        "/absolute",
        "directory\\file",
        "C:/file",
    ],
)
def test_private_path_names(relative: str) -> None:
    assert release_files.private_path_problem(relative)


@pytest.mark.parametrize(
    "relative",
    [
        "support.dll",
        "data.bin",
        "dot-claude/settings.json",
        ".claude/agents/review.md",
        "certifi/cacert.pem",
    ],
)
def test_public_names(relative: str) -> None:
    assert release_files.private_path_problem(relative) is None


def test_compiled_tree_accepts_public_files_and_certificate_bundle(tmp_path: Path) -> None:
    (tmp_path / "support.dll").write_bytes(b"public binary")
    bundle = tmp_path / "certifi" / "cacert.pem"
    bundle.parent.mkdir()
    bundle.write_bytes(_CERTIFICATES)
    assert set(release_files.check_compiled_tree(tmp_path)) == {tmp_path / "support.dll", bundle}


def test_compiled_tree_accepts_lexical_parent_segments(tmp_path: Path) -> None:
    (tmp_path / "unused").mkdir()
    root = tmp_path / "dist"
    root.mkdir()
    binary = root / "binary"
    binary.write_bytes(b"public binary")
    assert release_files.check_compiled_tree(tmp_path / "unused" / ".." / "dist") == [binary]


@pytest.mark.parametrize("relative", [".env.local", "state.json", "data.sqlite-wal", "private.pem"])
def test_compiled_private_files_are_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    (tmp_path / relative).write_bytes(b"synthetic private payload")

    def fail_open(*args: object, **kwargs: object):
        raise AssertionError("private payload must not be read")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(ValueError, match="private release filename"):
        release_files.check_compiled_tree(tmp_path)


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"synthetic secret",
        _CERTIFICATES + b"-----BEGIN PRIVATE KEY-----\nU3ludGhldGlj\n-----END PRIVATE KEY-----",
        _CERTIFICATES
        + b"# -----BEGIN RSA PRIVATE KEY-----\n# U3ludGhldGlj\n# -----END RSA PRIVATE KEY-----",
        b"x" * (release_files.CERTIFICATE_LIMIT + 1),
    ],
    ids=["empty", "text", "private-key", "commented-key", "oversized"],
)
def test_certificate_exception_requires_bounded_certificate_only_pem(
    tmp_path: Path, contents: bytes
) -> None:
    bundle = tmp_path / "certifi" / "cacert.pem"
    bundle.parent.mkdir()
    bundle.write_bytes(contents)
    with pytest.raises(ValueError, match="public certificate bundle") as caught:
        release_files.check_compiled_tree(tmp_path)
    assert "synthetic secret" not in str(caught.value)


@pytest.mark.parametrize("spaces", [1000, 2000])
def test_malformed_space_heavy_certificate_is_refused(spaces: int) -> None:
    for payload in (
        b"-----BEGIN CERTIFICATE-----\n" + b" " * spaces + b"!",
        b"-----BEGIN CERTIFICATE-----\n" + b" " * spaces + b"-----END CERTIFICATE-----\n",
    ):
        assert (
            release_files.certificate_bundle_problem(payload) == "invalid public certificate bundle"
        )


@pytest.mark.parametrize(
    "body", [b"U3lu=\ndGhldGlj", b"U3l", b"U3lu dGhldGlj", b"-----BEGIN CERTIFICATE-----"]
)
def test_certificate_line_grammar_refuses_malformed_body(body: bytes) -> None:
    payload = b"-----BEGIN CERTIFICATE-----\n" + body + b"\n-----END CERTIFICATE-----\n"
    assert release_files.certificate_bundle_problem(payload) == "invalid public certificate bundle"


def test_certificate_line_grammar_accepts_multiple_commented_crlf_blocks() -> None:
    payload = (_CERTIFICATES + b"\n# another public certificate\n" + _CERTIFICATES).replace(
        b"\n", b"\r\n"
    )
    assert release_files.certificate_bundle_problem(payload) is None


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    if directory and sys.platform == "win32":
        result = winproc.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], cwd=link.parent)
        assert result.returncode == 0, result.stderr
        return
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation unavailable")


def test_compiled_framework_links_are_contained_without_recursing(tmp_path: Path) -> None:
    version = tmp_path / "Framework" / "Versions" / "A"
    version.mkdir(parents=True)
    binary = version / "binary"
    binary.write_bytes(b"public binary")
    _link(version.parent / "Current", version, directory=True)
    assert release_files.check_compiled_tree(tmp_path) == [binary]


def test_compiled_contained_file_link_is_accepted(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(b"public binary")
    alias = tmp_path / "alias"
    _link(alias, binary)
    assert set(release_files.check_compiled_tree(tmp_path)) == {binary, alias}


@pytest.mark.parametrize("kind", ["outside", "root", "dangling", "private", "directory"])
def test_compiled_links_refuse_unsafe_targets(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "dist"
    root.mkdir()
    target = tmp_path / "outside" if kind == "outside" else root / "target"
    if kind == "root":
        target = root
    elif kind == "directory":
        target = tmp_path
    elif kind == "private":
        target = root / "private.pem"
        target.write_bytes(b"synthetic private payload")
    elif kind != "dangling":
        target.write_bytes(b"public binary")
    _link(root / "alias", target, directory=kind in {"root", "directory"})
    with pytest.raises((ValueError, OSError)):
        release_files.check_compiled_tree(root)


def test_compiled_root_alias_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    _link(alias, target, directory=True)
    with pytest.raises(ValueError, match="root"):
        release_files.check_compiled_tree(alias)
