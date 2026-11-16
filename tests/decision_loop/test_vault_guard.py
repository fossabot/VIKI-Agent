"""vault_guard.writable: the refs a Decision change proposal may name. Each refused row
is named for what it exercises; the accepted rows are refs the detectors build. Every row runs
through `writable` itself, so an exception fails it as surely as a wrong answer."""

from __future__ import annotations

import pytest

from viki_agent.decision_loop.vault_guard import writable

ACCEPTED = {
    "project-tasks": "10-projects/x/tasks.md",
    "archive-target": "90-archive/x tasks done 2026.md",
    "knowledge-note": "30-knowledge/A note.md",
    "claim-title": "30-knowledge/Deny rules hold even in bypass mode.md",
    "root-note": "Home.md",
    "markdown-suffix": "40-sources/Paper.markdown",
    "upper-suffix": "50-maps/Second Brain MOC.MD",
    "daily-note": "60-calendar/2026/2026-10-23.md",
    "japanese-name": "30-knowledge/メモの書き方.md",
    # Only the root's `system/`, `_templates/` and `00-inbox/` are refused.
    "system-below-root": "10-projects/system/notes.md",
    "inbox-below-root": "30-knowledge/00-inbox/habits.md",
    "raw-in-a-name": "30-knowledge/raw notes_raw.md",
}

INSTRUCTION_FILES = {
    "agents-root": "AGENTS.md",
    "agents-lower": "agents.md",
    "agents-override": "AGENTS.override.md",
    "claude-deep": "10-projects/x/CLAUDE.md",
    "claude-local-any-case": "30-knowledge/a/b/Claude.Local.md",
    "gemini": "notes/GEMINI.md",
}

# Each row but the first three is refused only once its parts are folded.
TRAILING_DOT_AND_FULLWIDTH = {
    "agents-trailing-dot": "AGENTS.md.",
    "claude-trailing-space": "CLAUDE.md ",
    "claude-local-dots-and-spaces": "10-projects/x/claude.local.md. . ",
    "agents-fullwidth": "ＡＧＥＮＴＳ.md",
    "claude-fullwidth-deep": "30-knowledge/ＣＬＡＵＤＥ.md",
    "raw-trailing-dot": "_raw./x.md",
    "raw-fullwidth-deep": "40-sources/＿ｒａｗ/x.md",
    "system-trailing-space": "system /memory/user.md",
    "system-fullwidth": "ｓｙｓｔｅｍ/memory/user.md",
    "templates-trailing-dot": "_templates./note.md",
    "inbox-fullwidth-digits": "００-inbox/x.md",
    "hidden-fullwidth-dot": "．claude/x.md",
    "part-of-space-and-dot": "notes/ ./x.md",
}

# Characters a reader of the draft cannot see: leading spaces and format characters.
INVISIBLE = {
    "agents-leading-space": " AGENTS.md",
    "agents-ideographic-space": "\u3000AGENTS.md",
    "hidden-after-leading-space": "notes/ .claude/x.md",
    "agents-soft-hyphen": "AGEN\u00adTS.md",
    "claude-zero-width-space": "CLAUDE\u200b.md",
    "raw-zero-width-joiner": "\u200d_raw/x.md",
    "system-bidi-mark": "system\u200e/memory/user.md",
    "part-of-format-characters": "notes/\u200b\u2060/x.md",
}

# A folded part that holds a separator would read as more than one part, a stream or a drive.
FOLDED_SEPARATORS = {
    "agents-behind-fullwidth-slash": "notes／AGENTS.md",
    "raw-behind-fullwidth-slash": "x／_raw/y.md",
    "memory-behind-fullwidth-slash": "system／memory/user.md",
    "claude-behind-fullwidth-backslash": "notes＼CLAUDE.md",
    "agents-stream-fullwidth-colon": "AGENTS.md：x.md",
    "drive-fullwidth-colon": "C：/x.md",
    "parent-fullwidth-dots": "a／．．／．．／AGENTS.md",
}

HIDDEN_AND_PRIVATE = {
    "claude-dir": ".claude/settings.md",
    "claude-dir-deep": "notes/.claude/agents/x.md",
    "git-dir": ".git/config.md",
    "codex-dir": ".codex/x.md",
    "viki-dir": ".viki/vault.md",
    "env": ".env",
    "env-variant": "notes/.env.local.md",
    "credentials-dir": "credentials/x.md",
    "stage-prefix": "notes/.viki-stage-1.md",
    "hidden-dir": ".obsidian/workspace.md",
    "hidden-note": "notes/.hidden.md",
}

RAW_ANYWHERE = {
    "raw-root": "_raw/x.md",
    "raw-upper": "_RAW/x.md",
    "raw-nested": "_raw/sub/x.md",
    "raw-under-sources": "40-sources/_raw/x.md",
    "raw-deep": "a/b/c/_raw/d.md",
}

ROOT_FOLDERS = {
    "memory": "system/memory/user.md",
    "system": "system/log.md",
    "memory-any-case": "System/Memory/preferences.md",
    "templates": "_templates/note.md",
    "inbox": "00-inbox/2026-10-23 idea.md",
    "inbox-upper": "00-INBOX/x.md",
}

SHORT_NAMES = {
    "claude": "CLAUDE~1.MD",
    "agents-deep": "notes/AGENTS~1.MD",
    "system": "SYSTEM~1/memory/user.md",
    "raw": "_RAW~1/x.md",
    "any-note": "x~2.md",
    "fullwidth": "ＣＬＡＵＤＥ～１.md",
}

MALFORMED = {
    "empty": "",
    "parent": "..",
    "parent-first": "../x.md",
    "parent-inside": "notes/../AGENTS.md",
    "current-first": "./x.md",
    "current-inside": "notes/./x.md",
    "absolute": "/x.md",
    "empty-part": "notes//x.md",
    "folder-only": "notes/",
    "backslash": "notes\\x.md",
    "drive-colon": "C:/x.md",
    "stream-colon": "x.md:y.md",
    "nul": "x\x00.md",
    "too-long": "a" * 510 + ".md",
}

CONTROL_AND_SURROGATES = {
    "tab": "notes/x\t.md",
    "newline": "notes/x\n.md",
    "unit-separator": "notes/x\x1f.md",
    "delete": "notes/x\x7f.md",
    "c1-first": "notes/x\x80.md",
    "c1-next-line": "notes/x\x85.md",
    "c1-last": "notes/x\x9f.md",
    "line-separator": "notes/x\N{LINE SEPARATOR}.md",
    "paragraph-separator": "notes/x\N{PARAGRAPH SEPARATOR}.md",
    "high-surrogate": "notes/x\ud800.md",
    "low-surrogate": "notes/\udc80y.md",
}

# Characters that would end a draft's quoting (a code span, a YAML string, a wikilink target),
# as written and full-width (which NFKC folds to the same character), in a folder part and in the
# file part.
QUOTE_BREAKERS = {
    "backtick": ("`", "｀"),
    "double-quote": ('"', "＂"),
    "open-bracket": ("[", "［"),
    "close-bracket": ("]", "］"),
    "pipe": ("|", "｜"),
    "hash": ("#", "＃"),
    "caret": ("^", "＾"),
}
DRAFT_QUOTING = {
    f"{name}-{form}-{where}": template.format(character)
    for name, (written, fullwidth) in QUOTE_BREAKERS.items()
    for form, character in (("written", written), ("fullwidth", fullwidth))
    for where, template in (("folder", "40-sources/a{}b/x.md"), ("file", "40-sources/a{}b.md"))
}
# A name that, as a note_link's A, would close the draft's code span and name AGENTS.md.
DRAFT_QUOTING["review-code-span-escape"] = "40-sources/Clip`, in `AGENTS.md` too, and in `x.md"

NON_NOTE_SUFFIX = {
    "text": "notes/x.txt",
    "none": "notes/x",
    "backup": "notes/x.md.bak",
    "json": "settings.json",
    "upper-then-text": "notes/x.MD.txt",
    # A note suffix only once folded: the reader lists neither as a note.
    "trailing-dot": "notes/x.md.",
    "fullwidth-suffix": "notes/x.ｍｄ",
}


class _Ref(str):
    """A str subclass: only an exact str is taken, since a subclass may answer any method."""


NOT_STR = {
    "none": None,
    "int": 0,
    "float": 1.5,
    "bool": True,
    "bytes": b"30-knowledge/A note.md",
    "bytearray": bytearray(b"30-knowledge/A note.md"),
    "list": ["30-knowledge/A note.md"],
    "tuple": ("30-knowledge/A note.md",),
    "dict": {"ref": "30-knowledge/A note.md"},
    "object": object(),
    "str-subclass": _Ref("30-knowledge/A note.md"),
}


@pytest.mark.parametrize("ref", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepts_note_refs(ref: str) -> None:
    assert writable(ref) is True


@pytest.mark.parametrize("ref", INSTRUCTION_FILES.values(), ids=INSTRUCTION_FILES.keys())
def test_refuses_instruction_files(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize(
    "ref", TRAILING_DOT_AND_FULLWIDTH.values(), ids=TRAILING_DOT_AND_FULLWIDTH.keys()
)
def test_refuses_trailing_dot_and_fullwidth(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", INVISIBLE.values(), ids=INVISIBLE.keys())
def test_refuses_invisible_characters(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", FOLDED_SEPARATORS.values(), ids=FOLDED_SEPARATORS.keys())
def test_refuses_folded_separators(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", HIDDEN_AND_PRIVATE.values(), ids=HIDDEN_AND_PRIVATE.keys())
def test_refuses_hidden_and_private(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", RAW_ANYWHERE.values(), ids=RAW_ANYWHERE.keys())
def test_refuses_raw_at_any_depth(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", ROOT_FOLDERS.values(), ids=ROOT_FOLDERS.keys())
def test_refuses_root_folders(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", SHORT_NAMES.values(), ids=SHORT_NAMES.keys())
def test_refuses_short_name_aliases(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", MALFORMED.values(), ids=MALFORMED.keys())
def test_refuses_malformed_refs(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", CONTROL_AND_SURROGATES.values(), ids=CONTROL_AND_SURROGATES.keys())
def test_refuses_control_characters_and_surrogates(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", DRAFT_QUOTING.values(), ids=DRAFT_QUOTING.keys())
def test_refuses_draft_quote_breakers(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("ref", NON_NOTE_SUFFIX.values(), ids=NON_NOTE_SUFFIX.keys())
def test_refuses_non_note_suffix(ref: str) -> None:
    assert writable(ref) is False


@pytest.mark.parametrize("value", NOT_STR.values(), ids=NOT_STR.keys())
def test_refuses_non_str(value: object) -> None:
    assert writable(value) is False
