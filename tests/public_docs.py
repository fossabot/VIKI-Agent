"""Published Markdown inventory and local-link checks for documentation tests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from viki_agent import winproc

LINK_TARGET = re.compile(
    r"""\]\(\s*(<[^>\n]+>|[^\s)]+)"""
    r"""(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"""
)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")


@dataclass(frozen=True)
class Inventory:
    paths: frozenset[Path]
    docs: tuple[Path, ...]


def inventory(repo: Path) -> Inventory:
    """Use Git's pending-commit file set, omitting deleted and external paths."""
    root = repo.resolve()
    names = winproc.git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(
        "\0"
    )
    paths = frozenset(
        path
        for name in names
        if name
        if (path := (root / name).resolve()).is_relative_to(root) and path.is_file()
    )
    docs = tuple(
        sorted(
            (
                path
                for path in paths
                if path.suffix.lower() == ".md"
                and (path.parent == root or path.relative_to(root).parts[0] == "docs")
            ),
            key=lambda path: (path.parent != root, path.relative_to(root).as_posix()),
        )
    )
    return Inventory(paths, docs)


def strip_code(text: str) -> str:
    """Code fences and spans do not contain Markdown links."""
    return INLINE_CODE.sub("", FENCED_CODE.sub("", text))


def link_failures(repo: Path, published: Inventory) -> list[tuple[str, str]]:
    root = repo.resolve()
    failures: list[tuple[str, str]] = []
    for doc in published.docs:
        for raw in LINK_TARGET.findall(strip_code(doc.read_text(encoding="utf-8"))):
            target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
            if SCHEME.match(target) or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            candidate = (doc.parent / unquote(target)).resolve()
            valid = candidate in published.paths or (
                candidate.is_dir()
                and candidate.is_relative_to(root)
                and any(path.is_relative_to(candidate) for path in published.paths)
            )
            if not valid:
                failures.append((doc.relative_to(root).as_posix(), target))
    return failures
