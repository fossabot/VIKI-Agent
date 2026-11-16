"""The name guards: the app's former name and the former Kanban name stay out of every tracked or
untracked path and every line of its file, with no exemption.

The app's former name matches in any letter case, with a hyphen, underscore, space or nothing
between its two words; the second word alone, as in the `desktop/` folder, passes. The former
Kanban name matches capitalised anywhere, and in lower or upper case where no ASCII letter
precedes it, so ordinary words that end in it (keyboard, clipboard, dashboard) pass. Both needles
are assembled from parts, so this file holds neither name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from viki_agent import winproc

REPO = Path(__file__).resolve().parents[1]

_APP_FIRST, _APP_SECOND = "vi" + "ki", "desk" + "top"  # assembled so this file holds no literal
_FORMER = "bo" + "ard"  # assembled so this file holds no literal
_FORMER_NAMES = re.compile(
    "(?i:" + _APP_FIRST + "[-_ ]?" + _APP_SECOND + ")"
    "|" + _FORMER.capitalize() + "|(?<![A-Za-z])(?:" + _FORMER + "|" + _FORMER.upper() + ")"
)


def former_names(text: str) -> list[int]:
    """1-based line numbers in `text` that hold either former name."""
    return [i for i, line in enumerate(text.splitlines(), 1) if _FORMER_NAMES.search(line)]


def former_name_offenders(root: Path, names: Iterable[str]) -> dict[str, list[int]]:
    """Per listed name whose `root / name` is a regular file: `0` when the name holds a former
    name, then the 1-based lines of its text that do. A name missing on disk is skipped, and a
    name with no hit is left out."""
    offenders: dict[str, list[int]] = {}
    for name in names:
        path = root / name
        if not path.is_file():
            continue  # staged deletion: no bytes left to scan
        # errors="replace" so a committed binary is still scanned and can never crash the guard.
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = ([0] if _FORMER_NAMES.search(name) else []) + former_names(text)
        if hits:
            offenders[name] = hits
    return offenders


def _listed_files(root: Path = REPO) -> list[str]:
    listing = winproc.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [name for name in listing.split("\0") if name]


def test_no_path_or_line_holds_a_former_name() -> None:
    assert former_name_offenders(REPO, _listed_files()) == {}


def test_the_former_app_name_rule_on_synthetic_text() -> None:
    """The rule on text no scan of the real repo can accidentally satisfy: the two words with any
    separator or none, in any letter case, are flagged; either word alone passes."""
    first, second = _APP_FIRST, _APP_SECOND
    flagged = [
        first + "-" + second,
        first + "_" + second,
        first + " " + second,
        first + second,
        first.upper() + " " + second.capitalize(),
        first.capitalize() + second.capitalize(),
        "launch-" + first + "-" + second + ".exe",
    ]
    for text in flagged:
        assert former_names("ordinary\n" + text + "\n") == [2], text
    ordinary = [
        second,
        second + "/main.mjs",
        "the " + second + " window",
        first + "-agent",
        first.upper() + " Agent",
        first + "-" + "laptop",
    ]
    for text in ordinary:
        assert former_names(text) == [], text


def test_the_former_kanban_name_rule_on_synthetic_text() -> None:
    """The rule on text no scan of the real repo can accidentally satisfy: the capitalised form
    is flagged anywhere, the lower- and upper-case forms after a non-letter; words that end in
    them pass."""
    f, cap = _FORMER, _FORMER.capitalize()
    flagged = [
        f + "/",
        "run" + cap,
        cap + "Read",
        "Bot" + cap,
        "UI" + cap,
        "watch_" + f,
        "STARTER_" + f.upper() + "_FILE",
        "decision.watch." + f,
        ".icon-" + f,
        f + "s",
        "1" + f,
        "docs/" + f + ".md",
    ]
    for text in flagged:
        assert former_names("ordinary\n" + text + "\n") == [2], text
    ordinary = [
        "keyboard",
        "Keyboard",
        "KeyboardEvent",
        "KEYBOARD",
        "clipboard",
        "ClipboardEvent",
        "dashboard",
        "Dashboard",
        "onboarding",
        "Onboarding",
        "onboard",
        "aboard",
        "artboard",
        "storyboard",
        "docs/keyboard-shortcuts.md",
    ]
    for text in ordinary:
        assert former_names(text) == [], text


def test_the_listing_includes_untracked_files_and_leaves_out_ignored_ones(tmp_path: Path) -> None:
    winproc.git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "new.md").write_text("new\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("ignored\n", encoding="utf-8")
    assert set(_listed_files(tmp_path)) == {".gitignore", "new.md"}


def test_the_scan_reports_hits_and_skips_missing_paths(tmp_path: Path) -> None:
    app = _APP_FIRST + "-" + _APP_SECOND
    (tmp_path / "hit.md").write_text("ordinary\n" + _FORMER + "/\n", encoding="utf-8")
    (tmp_path / (_FORMER + ".md")).write_text("ordinary\n", encoding="utf-8")
    (tmp_path / "keyboard.md").write_text("keyboard\n", encoding="utf-8")
    (tmp_path / "app.json").write_text('{"entrypoint": "' + app + '"}\n', encoding="utf-8")
    (tmp_path / (app + ".log")).write_text("ordinary\n", encoding="utf-8")
    names = ["hit.md", _FORMER + ".md", "keyboard.md", "gone/" + _FORMER + ".md"]
    names += ["app.json", app + ".log"]
    assert former_name_offenders(tmp_path, names) == {
        "hit.md": [2],
        _FORMER + ".md": [0],
        "app.json": [1],
        app + ".log": [0],
    }
