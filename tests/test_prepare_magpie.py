from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_magpie as magpie  # noqa: E402


def _string(data: bytes) -> bytes:
    length = len(data)
    prefix = bytearray()
    while length >= 128:
        prefix.append((length & 127) | 128)
        length >>= 7
    return bytes(prefix) + bytes([length]) + data


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    key = "example.invalid/dependency@v1.2.3"
    license_text = b"synthetic dependency MIT"
    source = {f"{key}/LICENSE": license_text}
    hashes = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(source.items())
    )
    checksum = "h1:" + base64.b64encode(hashlib.sha256(hashes.encode()).digest()).decode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in source.items():
            archive.writestr(name, data)
    modules = (
        b"path\tgithub.com/yetone/magpie\n"
        b"mod\tgithub.com/yetone/magpie\t(devel)\t\n"
        + f"dep\texample.invalid/dependency\tv1.2.3\t{checksum}\n".encode()
    )
    binary = (
        b"\xff Go buildinf:"
        + b"\x08\x02"
        + bytes(16)
        + _string(b"go1.26.8")
        + _string(bytes(16) + modules + bytes(16))
    )
    files = {
        "binary": binary,
        "LICENSE": b"synthetic MIT",
        "THIRD_PARTY_NOTICES": b"notices",
        "go": b"Go BSD",
        "v1.2.3.zip": buffer.getvalue(),
    }

    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setitem(
        magpie.RELEASE,
        "assets",
        {
            "win32/x64": {
                "name": "magpie-cli-windows-amd64.exe",
                "size": len(binary),
                "sha256": digest(binary),
            }
        },
    )
    monkeypatch.setitem(
        magpie.LICENSES,
        "go",
        {
            "version": "go1.26.8",
            "license": "BSD-3-Clause",
            "sha256": digest(files["go"]),
        },
    )
    monkeypatch.setitem(
        magpie.LICENSES,
        "modules",
        {
            key: {"sum": checksum, "license": "MIT", "texts": {"LICENSE": digest(license_text)}},
        },
    )
    notices = (
        f"==== Go go1.26.8 - BSD-3-Clause\n\nGo BSD\n\n"
        f"==== {key} - MIT\n\n---- {key}/LICENSE ----\nsynthetic dependency MIT\n"
    )
    monkeypatch.setitem(
        magpie.RELEASE, "dependency_notices", {"win32/x64": digest(notices.encode())}
    )
    monkeypatch.setitem(
        magpie.RELEASE,
        "notices",
        {name: digest(files[name]) for name in ("LICENSE", "THIRD_PARTY_NOTICES")},
    )
    inventory = magpie.sbom(binary, {"sha256": digest(binary)})
    canonical = (json.dumps(inventory, sort_keys=True, indent=2) + "\n").encode()
    monkeypatch.setitem(magpie.RELEASE, "sbom_sha256", {"win32/x64": digest(canonical)})
    return files


def test_prepare_checks_downloads_and_records_actual_binary_modules(
    tmp_path: Path, release: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    def fetch(url: str, limit: int) -> bytes:
        requests.append(url)
        name = url.rsplit("/", 1)[-1]
        data = release["go"] if "/golang/go/" in url else release.get(name, release["binary"])
        assert len(data) <= limit
        return data

    directory = tmp_path / "magpie"
    magpie.prepare(directory, "win32", "x64", fetch=fetch)
    manifest = magpie.validate(directory, "win32", "x64")
    assert manifest["source_commit"] == magpie.SOURCE_COMMIT
    assert manifest["binary"] == "magpie.exe"
    assert len(requests) == 5
    assert requests[-1] == "https://proxy.golang.org/example.invalid/dependency/@v/v1.2.3.zip"
    bom = json.loads((directory / "sbom-magpie.cdx.json").read_text(encoding="utf-8"))
    assert [(c["name"], c["version"]) for c in bom["components"]] == [
        ("example.invalid/dependency", "v1.2.3")
    ]
    magpie.prepare(directory, "win32", "x64", fetch=lambda *_: pytest.fail("already verified"))
    monkeypatch.setattr(magpie.sys, "platform", "win32")
    monkeypatch.setattr(magpie, "host_arch", lambda: "x64")
    repo = tmp_path / "repo"
    (repo / "dist").mkdir(parents=True)
    directory.rename(repo / "dist" / "magpie")
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    magpie.stage(repo, compiled)
    assert magpie.validate(compiled / "magpie", "win32", "x64", compressed=True) == manifest
    assert not (compiled / "magpie" / "magpie.exe").exists()
    compressed = compiled / "magpie" / "magpie.gz"
    assert gzip.decompress(compressed.read_bytes()) == release["binary"]
    compressed.write_bytes(gzip.compress(b"changed", mtime=0))
    with pytest.raises(ValueError, match="checksum"):
        magpie.validate(compiled / "magpie", "win32", "x64", compressed=True)
    compressed.write_bytes(gzip.compress(b"x" * 100_000, mtime=0))
    with pytest.raises(ValueError, match="checksum"):
        magpie.validate(compiled / "magpie", "win32", "x64", compressed=True)


def test_bad_download_leaves_no_usable_bundle(tmp_path: Path, release: dict[str, bytes]) -> None:
    directory = tmp_path / "magpie"
    with pytest.raises(ValueError, match="checksum"):
        magpie.prepare(directory, "win32", "x64", fetch=lambda *_: b"bad download")
    assert not directory.exists()
    assert list(tmp_path.iterdir()) == []


def test_unknown_target_and_non_go_binary_refuse() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        magpie.asset("win32", "arm64")
    with pytest.raises(ValueError, match="Go build"):
        magpie.sbom(b"not a Go binary", {"sha256": "unused"})


def test_dependency_sources_must_match_the_executables_module_checksum(
    release: dict[str, bytes],
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example.invalid/dependency@v1.2.3/LICENSE", "changed license")
    manifest = {"sha256": hashlib.sha256(release["binary"]).hexdigest()}
    components = magpie.sbom(release["binary"], manifest)["components"]
    with pytest.raises(ValueError, match="module checksum"):
        magpie.dependency_notices(
            components,
            lambda url, _: release["go"] if "/golang/go/" in url else buffer.getvalue(),
        )
