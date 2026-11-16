"""Shipped docs cite files and symbols, never line numbers: a line cite rots with the next edit."""

from __future__ import annotations

import re
from pathlib import Path

import public_docs

REPO = Path(__file__).resolve().parents[1]

CODE_EXT = "py|ts|tsx|css|js|json|toml|yml|md"
LINE_CITE = re.compile(rf"\b[\w./~-]+\.(?:{CODE_EXT}):\d+")
BARE_FRAGMENT = re.compile(r"`:\d+(?:[-,]\d+)*`")
ADR_LINE_CITE = re.compile(r"\bADR-\d+:\d+")
PATTERNS = (LINE_CITE, BARE_FRAGMENT, ADR_LINE_CITE)


def _line_cites(text: str) -> list[str]:
    return [m.group(0) for pattern in PATTERNS for m in pattern.finditer(text)]


def test_shipped_docs_carry_no_line_cites() -> None:
    hits = [
        f"{doc.relative_to(REPO).as_posix()}: {cite}"
        for doc in public_docs.inventory(REPO).docs
        for cite in _line_cites(doc.read_text(encoding="utf-8"))
    ]
    assert not hits, f"cite the file and symbol, not a line number: {hits[:10]}"


def test_line_cite_patterns_match_what_they_should() -> None:
    assert _line_cites("see `server.py:668-670,783` and ADR-017:154 and `:63`")
    assert not _line_cites("see `server.py`, `tests/test_x.py::test_y` and `file.py:<line>`")
