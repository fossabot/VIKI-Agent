"""Stage a completed frontend build and verify its contents in a product wheel."""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

import release_files

from viki_agent import winproc

REPO = Path(__file__).resolve().parents[1]
_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8}\.[a-z0-9]+\Z")
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.IGNORECASE | re.DOTALL)
_SAFE_SUFFIXES = {
    ".avif",
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".svg",
    ".ttf",
    ".wasm",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
}
_WHEEL_UI = "viki_agent/resources/ui/"


def _checked(path: Path, repo: Path) -> None:
    if not path.is_relative_to(repo):
        raise ValueError(f"product path outside repository: {path}")
    current = repo
    for part in path.relative_to(repo).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"link in product assets: {current}")
    if path.resolve() != path:
        raise ValueError(f"product path is not canonical: {path}")


def check_resources(repo: Path) -> None:
    """Reject extra non-UI resources and aliases before a build copies any of them."""
    repo = repo.resolve(strict=True)
    resources = repo / "src" / "viki_agent" / "resources"
    _checked(resources, repo)
    if not resources.exists():
        return
    for path in resources.rglob("*"):
        _checked(path, repo)
        relative = path.relative_to(resources).as_posix()
        if relative == "ui" or relative.startswith("ui/"):
            continue
        problem = release_files.resource_path_problem(relative, directory=path.is_dir())
        if problem or not (path.is_dir() or path.is_file()):
            raise ValueError(f"unexpected product resource: {relative}")


class _EntryLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        for key, value in attrs:
            if key not in {"src", "href"} or not value:
                continue
            if path := _local_path(value, "index.html"):
                self.paths.add(path)


def _local_path(value: str, source: str) -> str | None:
    url = urlsplit(value.strip())
    if url.scheme or url.netloc or not url.path:
        return None
    path = unquote(url.path)
    if "\\" in path:
        raise ValueError(f"unsafe asset reference: {value}")
    relative = (
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(source), path)
    )
    normalized = posixpath.normpath(relative)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"unsafe asset reference: {value}")
    return None if normalized == "." else normalized


def stage(repo: Path) -> Path:
    """Validate a completed frontend build and copy it into the packaged UI folder.

    This is the checking-and-copying half of `build`, split out so `scripts/compile.py` can
    stage the UI for a Nuitka build without also invoking `uv build`. Returns the packaged UI
    folder (`src/viki_agent/resources/ui`), replacing whatever it held before. Raises ValueError
    when the frontend build is missing or incomplete, or contains a file the product must not
    ship: one outside `repo`, a symlink/junction, or an unexpected asset.
    """
    repo = repo.resolve(strict=True)
    check_resources(repo)
    source = repo / "frontend" / "dist"
    icon = repo / "frontend" / "public" / "favicon.ico"
    stage_dir = repo / "src" / "viki_agent" / "resources" / "ui"
    for path in (source, icon, stage_dir):
        _checked(path, repo)
    if not (source / "index.html").is_file():
        raise ValueError(f"missing completed frontend build: {source / 'index.html'}")
    if not icon.is_file():
        raise ValueError(f"missing product icon: {icon}")

    public = repo / "frontend" / "public"
    files: list[Path] = []
    for path in source.rglob("*"):
        _checked(path, repo)
        relative = path.relative_to(source).as_posix()
        if release_files.private_path_problem(relative):
            raise ValueError(f"unexpected frontend build file: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unexpected frontend build file: {relative}")
        copied = public / path.relative_to(source)
        if copied.exists() or copied.is_symlink() or copied.is_junction():
            _checked(copied, repo)
        generated = relative.startswith("assets/") and _ASSET.fullmatch(path.name)
        public_copy = copied.is_file() and path.read_bytes() == copied.read_bytes()
        if any(part.startswith(".") for part in path.relative_to(source).parts) or (
            relative != "index.html"
            and relative != "favicon.ico"
            and (
                (
                    path.suffix.lower() not in _SAFE_SUFFIXES
                    and relative
                    not in (
                        "licenses/libraries-dev.txt",
                        "licenses/syncopate.txt",
                        "LICENSE.txt",
                        "THIRD_PARTY_NOTICES.txt",
                    )
                )
                or not (generated or public_copy)
            )
        ):
            raise ValueError(f"unexpected frontend build file: {relative}")
        files.append(path)
    links = _EntryLinks()
    links.feed((source / "index.html").read_text(encoding="utf-8"))
    if not any(path.endswith(".js") for path in links.paths):
        raise ValueError("missing referenced frontend JavaScript")
    available = {path.relative_to(source).as_posix() for path in files} | {"favicon.ico"}
    references = set(links.paths)
    for path in files:
        if path.suffix.lower() != ".css":
            continue
        css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for match in _CSS_URL.finditer(css):
            if local := _local_path(match.group(2), path.relative_to(source).as_posix()):
                references.add(local)
    if missing := references - available:
        raise ValueError(f"missing referenced frontend assets: {sorted(missing)}")

    if stage_dir.exists():
        for path in stage_dir.rglob("*"):
            _checked(path, repo)
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    for path in files:
        target = stage_dir / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    (stage_dir / "favicon.ico").write_bytes(icon.read_bytes())
    return stage_dir


def build(repo: Path, out_dir: Path) -> Path:
    repo = repo.resolve(strict=True)
    source = repo / "frontend" / "dist"
    stage_dir = repo / "src" / "viki_agent" / "resources" / "ui"
    out_dir = out_dir.resolve()
    if out_dir.is_relative_to(source) or out_dir.is_relative_to(stage_dir):
        raise ValueError("wheel output overlaps product assets")
    stage_dir = stage(repo)

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".viki-wheel-", dir=out_dir) as temporary:
        artifact_dir = Path(temporary)
        if artifact_dir.resolve().parent != out_dir:
            raise ValueError("wheel temporary folder escaped output directory")
        result = winproc.run(
            ["uv", "build", "--wheel", "--offline", "--out-dir", str(artifact_dir)],
            cwd=repo,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(
                f"uv build failed with exit {result.returncode}: {result.stderr.strip()}"
            )
        wheels = list(artifact_dir.glob("viki_agent-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(
                f"expected one viki_agent wheel in {artifact_dir}, found {len(wheels)}"
            )
        wheel = wheels[0]
        staged = {
            _WHEEL_UI + path.relative_to(stage_dir).as_posix(): path.read_bytes()
            for path in stage_dir.rglob("*")
            if path.is_file()
        }
        with ZipFile(wheel) as archive:
            for member in archive.infolist():
                name = member.filename
                if problem := release_files.private_path_problem(name):
                    raise ValueError(f"{problem}: {name}")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"link in release wheel: {name}")
                metadata = name.split("/", 1)[0]
                if (
                    not member.is_dir()
                    and not name.startswith("viki_agent/resources/")
                    and not name.endswith(".py")
                    and not (metadata.startswith("viki_agent-") and metadata.endswith(".dist-info"))
                ):
                    raise ValueError(f"unexpected release wheel file: {name}")
                if name.startswith("viki_agent/resources/"):
                    relative = name.removeprefix("viki_agent/resources/").removesuffix("/")
                    if relative and (
                        problem := release_files.resource_path_problem(
                            relative, directory=member.is_dir()
                        )
                    ):
                        raise ValueError(f"{problem}: {name}")
            members = [
                name
                for name in archive.namelist()
                if name.startswith(_WHEEL_UI) and not name.endswith("/")
            ]
            if len(members) != len(staged) or set(members) != set(staged):
                raise RuntimeError("wheel UI member list differs from staged assets")
            for name, expected in staged.items():
                if archive.read(name) != expected:
                    raise RuntimeError(f"wheel UI bytes differ from staged asset: {name}")
        target = out_dir / wheel.name
        wheel.replace(target)
        return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "dist")
    args = parser.parse_args(argv)
    try:
        wheel = build(REPO, args.out_dir)
    except (BadZipFile, OSError, ValueError, RuntimeError) as exc:
        print(f"product build refused: {exc}", file=sys.stderr)
        return 2
    print(wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
