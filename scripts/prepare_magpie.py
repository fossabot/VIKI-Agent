"""Download the pinned Magpie sidecar for a build; never executed by the running app.

Run before scripts/compile.py. The stock CLI release includes Magpie's web UI. Downloads
are checked against the reviewed release's size and SHA-256 before they become build inputs.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
RELEASE = json.loads(
    (REPO / "desktop" / "build" / "magpie-release.json").read_text(encoding="utf-8")
)
LICENSES = json.loads(
    (REPO / "desktop" / "build" / "magpie-licenses.json").read_text(encoding="utf-8")
)
VERSION: str = RELEASE["version"]
SOURCE_COMMIT: str = RELEASE["source_commit"]
FILES = {
    "magpie.json",
    "LICENSE",
    "THIRD_PARTY_NOTICES",
    "GO-DEPENDENCY-NOTICES.txt",
    "sbom-magpie.cdx.json",
}


def host_arch() -> str:
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )


def asset(platform_name: str, arch: str) -> dict:
    try:
        return RELEASE["assets"][f"{platform_name}/{arch}"]
    except KeyError:
        raise ValueError(f"unsupported Magpie build target: {platform_name}/{arch}") from None


def download(url: str, limit: int) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Magpie download exceeds its expected size")
    return data


def _checked(data: bytes, expected: str, name: str, size: int | None = None) -> bytes:
    if (size is not None and len(data) != size) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Magpie checksum mismatch: {name}")
    return data


def _go_string(data: bytes, offset: int) -> tuple[bytes, int]:
    length = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        length |= (byte & 127) << shift
        if byte < 128:
            end = offset + length
            if length > 1_000_000 or end > len(data):
                break
            return data[offset:end], end
    raise ValueError("invalid Magpie Go build information")


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def dependency_notices(components: list[dict], fetch: Callable[[str, int], bytes]) -> str:
    go = LICENSES["go"]
    data = fetch(f"https://raw.githubusercontent.com/golang/go/{go['version']}/LICENSE", 100_000)
    _checked(data, go["sha256"], "Go LICENSE")

    def module(component: dict) -> str:
        key = f"{component['name']}@{component['version']}"
        row = LICENSES["modules"].get(key)
        checksum = component["properties"][0]["value"]
        if not row or row["sum"] != checksum:
            raise ValueError(f"unreviewed Magpie Go dependency: {key}")

        def escaped(value: str) -> str:
            return "".join("!" + c.lower() if c.isupper() else c for c in value)

        url = (
            f"https://proxy.golang.org/{escaped(component['name'])}/@v/"
            f"{escaped(component['version'])}.zip"
        )
        with zipfile.ZipFile(io.BytesIO(fetch(url, 100_000_000))) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 500_000_000:
                raise ValueError("oversized Magpie dependency source")
            digest = hashlib.sha256()
            for name in sorted(archive.namelist()):
                content_hash = hashlib.sha256(archive.read(name)).hexdigest()
                digest.update(f"{content_hash}  {name}\n".encode())
            if "h1:" + base64.b64encode(digest.digest()).decode() != checksum:
                raise ValueError(f"Magpie Go module checksum mismatch: {key}")
            texts = []
            for name, expected in row["texts"].items():
                text = _checked(archive.read(f"{key}/{name}"), expected, f"{key}/{name}")
                texts.append(f"---- {key}/{name} ----\n{_text(text)}")
        if row.get("supplemental_text"):
            texts.append(row["supplemental_text"])
        return f"==== {key} - {row['license']}\n\n" + "\n\n".join(texts)

    with ThreadPoolExecutor(max_workers=4) as pool:
        texts = list(pool.map(module, components))
    return (
        f"==== Go {go['version']} - {go['license']}\n\n{_text(data)}\n\n"
        + "\n\n".join(texts)
        + "\n"
    )


def sbom(data: bytes, manifest: dict) -> dict:
    """Inventory the actual executable's inline Go build info (Go 1.18+ format)."""
    offset = data.find(b"\xff Go buildinf:")
    if offset < 0 or offset + 32 > len(data) or not data[offset + 15] & 2:
        raise ValueError("missing Magpie Go build information")
    go_version, offset = _go_string(data, offset + 32)
    framed, _ = _go_string(data, offset)
    if len(framed) < 33 or framed[-17] != 10:
        raise ValueError("invalid Magpie Go module information")
    modules = framed[16:-16].decode("utf-8").splitlines()
    if "path\tgithub.com/yetone/magpie" not in modules:
        raise ValueError("unexpected Magpie Go module path")
    root = f"pkg:golang/github.com/yetone/magpie@v{VERSION}"
    components: list[dict] = []
    for row in modules:
        fields = row.split("\t")
        if fields[0] == "=>":
            raise ValueError("unreviewed Magpie Go module replacement")
        if fields[0] != "dep":
            continue
        if len(fields) != 4 or not fields[2].startswith("v"):
            raise ValueError("invalid Magpie Go dependency")
        name, version, checksum = fields[1:]
        license_row = LICENSES["modules"].get(f"{name}@{version}")
        if not license_row or license_row["sum"] != checksum:
            raise ValueError(f"unreviewed Magpie Go dependency: {name}@{version}")
        purl = f"pkg:golang/{quote(name, safe='/')}@{quote(version, safe='')}"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "bom-ref": purl,
                "purl": purl,
                "properties": [{"name": "go:module:sum", "value": checksum}],
                "licenses": [{"expression": license_row["license"]}],
            }
        )
    if not components:
        raise ValueError("missing Magpie Go dependencies")
    if go_version.decode() != LICENSES["go"]["version"]:
        raise ValueError("unreviewed Magpie Go toolchain")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Magpie",
                "version": VERSION,
                "bom-ref": root,
                "purl": root,
                "licenses": [{"license": {"id": "MIT"}}],
                "hashes": [{"alg": "SHA-256", "content": manifest["sha256"]}],
                "properties": [
                    {"name": "go:version", "value": go_version.decode("utf-8")},
                    {"name": "source:commit", "value": SOURCE_COMMIT},
                ],
            }
        },
        "components": components,
        "dependencies": [{"ref": root, "dependsOn": [c["bom-ref"] for c in components]}],
    }


def validate(directory: Path, platform_name: str, arch: str, *, compressed: bool = False) -> dict:
    expected = asset(platform_name, arch)
    binary = "magpie.exe" if platform_name == "win32" else "magpie"
    payload = "magpie.gz" if compressed else binary
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("link in Magpie bundle")
    if {entry.name for entry in directory.iterdir()} != FILES | {payload}:
        raise ValueError("unexpected or missing Magpie bundle files")
    for entry in directory.iterdir():
        if entry.is_symlink() or entry.is_junction() or not entry.is_file():
            raise ValueError("link or directory in Magpie bundle")
    manifest = json.loads((directory / "magpie.json").read_text(encoding="utf-8"))
    if manifest != {
        "version": VERSION,
        "source_commit": SOURCE_COMMIT,
        "platform": platform_name,
        "arch": arch,
        "sha256": expected["sha256"],
        "binary": binary,
    }:
        raise ValueError("Magpie manifest differs from the pinned release")
    if compressed:
        if (directory / payload).stat().st_size > expected["size"] + 65_536:
            raise ValueError("oversized Magpie compressed payload")
        with gzip.open(directory / payload, "rb") as stream:
            data = stream.read(expected["size"] + 1)
    else:
        data = (directory / payload).read_bytes()
    data = _checked(data, expected["sha256"], binary, expected["size"])
    for name, digest in RELEASE["notices"].items():
        _checked((directory / name).read_bytes(), digest, name)
    _checked(
        (directory / "GO-DEPENDENCY-NOTICES.txt").read_bytes(),
        RELEASE["dependency_notices"][f"{platform_name}/{arch}"],
        "GO-DEPENDENCY-NOTICES.txt",
    )
    inventory = _checked(
        (directory / "sbom-magpie.cdx.json").read_bytes(),
        RELEASE["sbom_sha256"][f"{platform_name}/{arch}"],
        "sbom-magpie.cdx.json",
    )
    if json.loads(inventory) != sbom(data, manifest):
        raise ValueError("Magpie SBOM differs from its executable")
    return manifest


def prepare(
    directory: Path,
    platform_name: str,
    arch: str,
    *,
    fetch: Callable[[str, int], bytes] = download,
) -> Path:
    expected = asset(platform_name, arch)
    binary = "magpie.exe" if platform_name == "win32" else "magpie"
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        validate(directory, platform_name, arch)
        return directory
    with tempfile.TemporaryDirectory(prefix="magpie-build-", dir=directory.parent) as temporary:
        staging = Path(temporary)
        url = (
            f"https://github.com/yetone/magpie-releases/releases/download/v{VERSION}/"
            + expected["name"]
        )
        data = _checked(fetch(url, expected["size"]), expected["sha256"], binary, expected["size"])
        (staging / binary).write_bytes(data)
        (staging / binary).chmod(0o755)
        for name, digest in RELEASE["notices"].items():
            url = f"https://raw.githubusercontent.com/yetone/magpie/{SOURCE_COMMIT}/{name}"
            (staging / name).write_bytes(_checked(fetch(url, 1_000_000), digest, name))
        manifest = {
            "version": VERSION,
            "source_commit": SOURCE_COMMIT,
            "platform": platform_name,
            "arch": arch,
            "sha256": expected["sha256"],
            "binary": binary,
        }
        bom = sbom(data, manifest)
        notices = dependency_notices(bom["components"], fetch).encode("utf-8")
        _checked(notices, RELEASE["dependency_notices"][f"{platform_name}/{arch}"], "Go notices")
        (staging / "GO-DEPENDENCY-NOTICES.txt").write_bytes(notices)
        for name, value in (
            ("magpie.json", manifest),
            ("sbom-magpie.cdx.json", bom),
        ):
            (staging / name).write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        validate(staging, platform_name, arch)
        staging.rename(directory)
    return directory


def stage(repo: Path, compiled_dir: Path) -> None:
    source = repo / "dist" / "magpie"
    manifest = validate(source, sys.platform, host_arch())
    target = compiled_dir / "magpie"
    if target.exists() or target.is_symlink():
        raise ValueError("compiled output already contains a Magpie bundle")
    # Preserve upstream bytes through platform signing of the surrounding application.
    shutil.copytree(source, target, ignore=lambda *_: [manifest["binary"]])
    (target / "magpie.gz").write_bytes(
        gzip.compress((source / manifest["binary"]).read_bytes(), mtime=0)
    )
    validate(target, sys.platform, host_arch(), compressed=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("win32", "darwin", "linux"), default=sys.platform)
    parser.add_argument("--arch", choices=("x64", "arm64"), default=host_arch())
    parser.add_argument("--out-dir", type=Path, default=REPO / "dist" / "magpie")
    args = parser.parse_args(argv)
    try:
        prepare(args.out_dir, args.platform, args.arch)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Magpie preparation refused: {exc}", file=sys.stderr)
        return 2
    print(f"Magpie {VERSION}: {args.platform}/{args.arch} verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
