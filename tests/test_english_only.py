"""Guards `src/viki_agent` against Japanese text: VIKI Agent's UI is English only.

Every `.py` module is scanned for string constants in the Japanese/CJK character class. A
match must be a documented exception in `DATA`: text the app matches in what users or tools
write (half-width voicing marks folded for search, a transcript noise pattern, or a
routing-line regex), never a translation of something the app shows.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
REPO = TESTS.parent
SRC = REPO / "src" / "viki_agent"

# Written as code-point ranges, not literal characters, so this block stays plain ASCII.
_JAPANESE_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xFF00, 0xFFEF),
)
_JAPANESE_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _JAPANESE_RANGES) + "]"
)

DATA: dict[str, dict[str, str]] = {
    "conversations/conversation_search.py": {
        "ﾞﾟ": "half-width voiced marks folded for Japanese search",
    },
    "voice/voice_stream.py": {
        r"(?:\s|♪|\[[^\]]*\]|\([^)]*\)|（[^）]*）|【[^】]*】)*": (
            "drops ♪ and bracketed sound tags from transcripts"
        ),
    },
    "drivers/claude/mapper.py": {
        r"▸ routing: (?P<agent>[^→]+) → \[?(?P<model>[^(（]*?[^(（\s])\]?\s*"
        r"[\(（](?P<reason>.*)[\)）]": (
            "routing lines with full-width brackets; frozen tests/acceptance/test_mapper_accept.py"
        ),
    },
}


def _docstring_constants(tree: ast.AST) -> set[ast.Constant]:
    """The `Constant` node of every module, class and function docstring in `tree`."""
    nodes: set[ast.Constant] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        stmt = node.body[0]
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            nodes.add(stmt.value)
    return nodes


def japanese_strings(source: str) -> list[tuple[int, str]]:
    """Every `ast.Constant` string in `source` (f-string literal parts included) that holds
    text in the Japanese/CJK class, excluding module, class and function docstrings."""
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        if isinstance(node.value, str)
        if node not in docstrings
        if _JAPANESE_RE.search(node.value)
    ]


_SELF_TEST_SOURCE = '''"""Module docstring 日本語."""


class C:
    """Class docstring 日本語."""

    def f(self):
        """Function docstring 日本語."""
        # a comment 日本語
        plain = "定数日本語"
        templated = f"prefix {plain}文字列日本語"
        return plain, templated
'''


def test_detector_finds_constants_and_fstring_parts_but_not_docstrings_or_comments() -> None:
    values = {value for _, value in japanese_strings(_SELF_TEST_SOURCE)}
    assert values == {"定数日本語", "文字列日本語"}


MODULES = sorted(SRC.rglob("*.py"))


def test_scans_at_least_120_modules_including_main_py() -> None:
    assert len(MODULES) >= 120
    assert "__main__.py" in {m.name for m in MODULES}


@pytest.mark.parametrize("module", MODULES, ids=[m.relative_to(SRC).as_posix() for m in MODULES])
def test_module_has_no_undocumented_japanese(module: Path) -> None:
    rel = module.relative_to(SRC).as_posix()
    allowed = DATA.get(rel, {})
    found = japanese_strings(module.read_text(encoding="utf-8"))
    extra = [(lineno, value) for lineno, value in found if value not in allowed]
    assert not extra, f"{rel}: Japanese text outside DATA: {extra[:5]}"


def test_every_data_entry_still_occurs_in_its_file() -> None:
    for rel, entries in DATA.items():
        source = (SRC / rel).read_text(encoding="utf-8")
        for value, reason in entries.items():
            assert value in source, f"{rel}: DATA entry no longer found: {value!r}"
            assert reason.strip(), f"{rel}: DATA entry needs a reason: {value!r}"
