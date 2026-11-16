"""No two tracked paths may differ only in letter case. Windows and macOS see one file where Linux
sees two, so a check run on Linux passes while the Windows build picks the wrong module: once
`import "./BotIcon"` found `botIcon.ts` instead of `BotIcon.tsx`. A module's extension is
dropped first, because an import names the module without it.
"""

from __future__ import annotations

import re
from pathlib import Path

from viki_agent import winproc

REPO = Path(__file__).resolve().parents[1]

_MODULE_SUFFIX = re.compile(r"\.[cm]?[jt]sx?$")


def case_clashes(paths: list[str]) -> set[tuple[str, str]]:
    """Pairs of folders, files or module names in `paths` that are equal ignoring letter case."""
    seen: dict[str, str] = {}
    clashes: set[tuple[str, str]] = set()
    for path in paths:
        parts = _MODULE_SUFFIX.sub("", path).split("/")
        for depth in range(1, len(parts) + 1):
            name = "/".join(parts[:depth])
            first = seen.setdefault(name.lower(), name)
            if first != name:
                clashes.add((first, name))
    return clashes


def test_case_clashes_finds_a_module_whose_name_differs_only_in_case() -> None:
    paths = ["web/Bots/BotIcon.tsx", "web/Bots/botIcon.ts", "web/Bots/BotIcon.css"]
    assert case_clashes(paths) == {("web/Bots/BotIcon", "web/Bots/botIcon")}


def test_case_clashes_finds_folders_and_ignores_distinct_names() -> None:
    assert case_clashes(["a/Docs/x.md", "a/docs/y.md"]) == {("a/Docs", "a/docs")}
    assert case_clashes(["a/BotIcon.tsx", "a/BotIcon.css", "a/botIconDesign.ts"]) == set()


def test_no_tracked_paths_differ_only_in_letter_case() -> None:
    tracked = [name for name in winproc.git(REPO, "ls-files", "-z").split("\0") if name]
    assert case_clashes(tracked) == set()
