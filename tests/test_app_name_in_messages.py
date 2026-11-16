"""Every reopen/restart/relaunch message names the app VIKI Agent, never bare VIKI."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Scanned line by line (see _violations): a verb and "VIKI" split across a wrapped line is a
# known miss this pattern cannot catch.
VIOLATION = re.compile(
    r"(?i)\b(?:open|reopen|restart|relaunch)(?:ing|ed|s)?\s+VIKI\b(?!\s+Agent)|VIKI can['’]t start:"
)

ACCEPT_DIR = ("frontend", "src", "accept")


def _is_test_file(path: Path) -> bool:
    name = path.name
    return ".test." in name or ".accept." in name or name.startswith("test_")


def _scanned_files() -> list[Path]:
    files = sorted((REPO / "src" / "viki_agent").rglob("*.py"))
    frontend = REPO / "frontend" / "src"
    for ext in ("*.ts", "*.tsx"):
        files += sorted(frontend.rglob(ext))
    kept = []
    for path in files:
        if _is_test_file(path):
            continue
        if path.relative_to(REPO).parts[: len(ACCEPT_DIR)] == ACCEPT_DIR:
            continue
        kept.append(path)
    return kept


def _violations(path: Path) -> list[str]:
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if VIOLATION.search(line):
            hits.append(f"{path.relative_to(REPO).as_posix()}:{lineno}")
    return hits


def test_reopen_restart_messages_name_viki_agent() -> None:
    hits = [hit for path in _scanned_files() for hit in _violations(path)]
    assert not hits, f"say VIKI Agent, not VIKI: {hits}"


def test_violation_pattern_matches_what_it_should() -> None:
    assert VIOLATION.search("Can't authenticate. Open VIKI again.")
    assert VIOLATION.search("restart VIKI; if it still fails")
    assert VIOLATION.search("then reopen VIKI.")
    assert VIOLATION.search("relaunch VIKI now")
    assert VIOLATION.search("VIKI can't start: its data folder")
    assert VIOLATION.search("Restarting VIKI finishes most of them.")
    assert not VIOLATION.search("Open VIKI Agent again.")
    assert not VIOLATION.search("restart VIKI Agent; if it still fails")
    assert not VIOLATION.search("VIKI Agent can't start: its data folder")
    assert not VIOLATION.search("VIKI cannot enforce this, so it blocks launch.")
    assert not VIOLATION.search("Routines run only while VIKI is open.")
    assert not VIOLATION.search("Restarting VIKI Agent finishes most of them.")
