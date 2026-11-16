"""Scrub a raw `scripts/capture.py` capture into a committable fixture.

Usage: uv run --directory <repo> python scripts/scrub_fixture.py <raw.jsonl> <fixture.jsonl>
           --vault PATH [--process N | --keep-types user,assistant]

`--vault` (required) names the root scrubbed to `%VAULT%` and `%VAULT-SLUG%`: for a
scripts/capture.py run, its `scratch` folder.

Replaces account identity fields and opaque signatures by key, then the vault path, the home
directory, a synced plugin folder's account ids, e-mail addresses, the user's account name and
their private words (see PRIVATE_FILE) anywhere in the text, and refuses
to write if anything on the banned list survives, in a line or joined across the streamed
deltas of one content block. Only captures of sessions that `scripts/capture.py` created, with
scripted prompts, may be scrubbed and committed; never the user's own sessions.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

HOME = Path.home()
OWNER = HOME.name
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[A-Za-z]{2,}")
# Words that identify you beyond your account name, such as a family name or an employer, stay in
# a git-ignored JSON file, so the guard never publishes what it protects:
#   {"replace": {"<word>": "<placeholder>"},
#    "starter_words": ["<word>"],
#    "media_collisions": {"<path>": {"sha256": "<file>", "spans": [[start, end, "<sha256>"]]}}}
# A `replace` word is scrubbed to its placeholder and banned as a whole word in any letter case.
# tests/vaults/test_starter.py bans `starter_words` anywhere in the vault starter set, and
# tests/test_fixtures_clean.py admits `media_collisions`: reviewed chance matches of a `replace`
# word inside committed media. Set VIKI_PRIVATE_WORDS to the file's path to share one file between
# worktrees; otherwise it is .private-words.json at the repository root. Without it, only the
# account-name, path, e-mail and token checks run.
PRIVATE_FILE = Path(
    os.environ.get("VIKI_PRIVATE_WORDS")
    or Path(__file__).resolve().parents[1] / ".private-words.json"
)


def _load_private(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


PRIVATE = _load_private(PRIVATE_FILE)
PRIVATE_WORDS: dict[str, str] = dict(PRIVATE.get("replace", {}))


def private_word_pattern() -> re.Pattern[str] | None:
    """PRIVATE_WORDS as one whole-word, any-case pattern, or None when there are none."""
    return _whole_words(tuple(PRIVATE_WORDS))


@functools.cache
def _whole_words(words: tuple[str, ...]) -> re.Pattern[str] | None:
    if not words:
        return None
    # Whole words, so random ids and base64 never trip a short one.
    return re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)


ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "anthropic.com", "noreply.github.com")
# A CI runner's account name is ordinary English text, not the user's identity: it must neither
# fail the guard nor be rewritten to "owner" the way the user's own name is.
GENERIC_ACCOUNTS = frozenset(
    {"root", "runner", "runneradmin", "user", "admin", "ubuntu", "vscode", "codespace"}
)
# Substrings, checked case-insensitively against the JSON text (escaped backslashes count too).
# The four-backslash form is a path inside a JSON string that is itself inside JSON text (e.g. a
# streamed tool-call argument): the CLI escapes the path once for its own JSON, then the capture
# line escapes that again, so one real backslash surfaces as four in the committed text.
# The user's account name is checked separately (scrub_text, problems), not listed here, since
# a generic CI account must skip that check while the fixed entries below always apply.
BANNED = (
    "c:\\\\users",
    "c:/users",
    "c:\\users",
    "c:\\\\\\\\users",
    ".credentials",
    "sk-ant-",
)
# A leaked bearer token: 20+ characters of RFC 6750's b64token set (letters, digits and -._~+/)
# after "Bearer ", with optional "=" padding, wide enough to catch a JWT (dot-separated base64
# segments), a `ghp_` token or a base64 value using +, / and = padding: the committed-file
# guard's backstop for an MCP header value that escaped _redact_mcp_entry. problems() reports a
# match cut to its first 12 characters, never the whole token; the scrub is the real guard.
BEARER_TOKEN = re.compile(r"[Bb]earer [A-Za-z0-9._~+/-]{20,}=*")
# Claude Code's project-folder slug of a Windows path (~/.claude/projects/C--Users-...): the one
# path form with no entry on BANNED, and the form the username rule alone leaves half standing.
# Fixtures scrubbed before %VAULT-SLUG% existed carry the username-anonymised C--Users-owner-...,
# which is not an offence — the name is already gone from it, only the public folder layout is left.
SLUG_PATH = re.compile(r"\bC--Users-(?!owner\b)", re.I)
# The same slug on POSIX: a home directory has no drive letter, so the CLI's project folder
# instead spells /home/<name> (Linux) or /Users/<name> (macOS, and some distros use /var/home/).
SLUG_PATH_POSIX = re.compile(r"projects[\\/]+-(?:var-)?(?:home|Users)-(?!owner\b)", re.I)
# Scenario 23 of scripts/capture.py forks one of the user's own sessions, so its capture may
# never be committed, scrubbed or not (CLAUDE.md rule 9). capture.py puts this marker in that
# file's name and main() refuses any raw file carrying it, so the rule is more than a comment.
DO_NOT_COMMIT = "DO-NOT-COMMIT"
# Values replaced by key wherever they appear (the initialize reply's `account` block; thinking
# signatures are opaque base64 that replay never needs and that can spell anything).
BY_KEY = {
    "email": "owner@example.com",
    "organization": "Example Org",
    "organizationName": "Example Org",
    "accountUuid": "00000000-0000-4000-8000-000000000000",
    "organizationUuid": "00000000-0000-4000-8000-000000000001",
    "signature": "signature-placeholder",
}
# Claude Code keeps a claude.ai-synced plugin under ~/.claude/plugins/synced/<uuid>_<uuid>/: the
# folder names the account's ids, so they get BY_KEY's placeholders wherever the path appears
# (2.1.281's system/init `plugins` list), in any of its escaped or forward-slashed forms.
SYNCED_PLUGIN = re.compile(
    r"(plugins[\\/]+synced[\\/]+)"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.I,
)
SYNCED_PLACEHOLDER = f"{BY_KEY['organizationUuid']}_{BY_KEY['accountUuid']}"
# The app's server-name prefix (`SERVER_PREFIX`). An app server is one of source `dynamic`
# with this prefix, and only an app server keeps its url and args.
APP_SERVER_PREFIX = "viki_"
MCP_VALUE_PLACEHOLDER = "mcp-secret-placeholder"


def _redact_mcp_entry(entry: object) -> object:
    """One `mcp_status` reply's server entry: every `config.env` or `config.headers` value
    replaced by a fixed placeholder. An app server is one whose `source` is `dynamic` and whose
    name starts with `viki_`; only an app server keeps its `config.url` and `config.args` as they
    are. A non-app server's `config.url`, and each entry of its `config.args`, is replaced by the
    same placeholder too."""
    if not isinstance(entry, dict) or not isinstance(entry.get("config"), dict):
        return entry
    config = dict(entry["config"])
    for key in ("env", "headers"):
        values = config.get(key)
        if isinstance(values, dict):
            config[key] = {k: MCP_VALUE_PLACEHOLDER for k in values}
    name = str(entry.get("name", ""))
    is_app = entry.get("source") == "dynamic" and name.startswith(APP_SERVER_PREFIX)
    if not is_app:
        if "url" in config:
            config["url"] = MCP_VALUE_PLACEHOLDER
        args = config.get("args")
        if isinstance(args, list):
            config["args"] = [MCP_VALUE_PLACEHOLDER for _ in args]
    return {**entry, "config": config}


def _redact(value: object) -> object:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in BY_KEY and isinstance(v, str):
                out[k] = BY_KEY[k]
            elif k == "mcpServers" and isinstance(v, list):
                out[k] = [_redact_mcp_entry(_redact(e)) for e in v]
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def scrub_line(line: str, vault: Path) -> str:
    """One capture line: redact by key, then scrub the text."""
    return scrub_text(json.dumps(_redact(json.loads(line)), ensure_ascii=False), vault)


def _path_forms(path: Path | PurePosixPath, name: str, platform: str) -> list[tuple[str, str]]:
    """Every way a path appears inside JSON text, with what replaces it. On Windows: escaped
    twice (a JSON string inside a JSON string, such as a streamed tool input's `partial_json`),
    escaped once, raw and forward-slashed, plus Claude Code's project-folder slug
    (C--Users-...). Longer forms come first so the longest match wins. On POSIX a path never
    contains a backslash, so there is nothing to escape: only the raw form and the slug. The slug
    is a path form like the others, but it gets its own placeholder: `%VAULT%` means the path, and
    a fixture that replayed `%VAULT%` where a slug belongs would name a folder that does not exist.
    """
    raw = str(path)
    slug = "".join(ch if ch.isalnum() else "-" for ch in raw)
    if platform == "win32":
        forms = [
            raw.replace("\\", "\\\\\\\\"),
            raw.replace("\\", "\\\\"),
            raw,
            raw.replace("\\", "/"),
        ]
    else:
        forms = [raw]
    return [(form, f"%{name}%") for form in forms] + [(slug, f"%{name}-SLUG%")]


def _is_foreign(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    return not any(
        domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_EMAIL_DOMAINS
    )


def scrub_text(text: str, vault: Path) -> str:
    for base, name in ((vault, "VAULT"), (HOME, "HOME")):  # vault first: it is inside HOME
        for form, placeholder in _path_forms(base, name, sys.platform):
            text = re.sub(re.escape(form), placeholder, text, flags=re.I)
    text = SYNCED_PLUGIN.sub(lambda m: m[1] + SYNCED_PLACEHOLDER, text)
    text = EMAIL.sub(lambda m: "owner@example.com" if _is_foreign(m[0]) else m[0], text)
    if OWNER.lower() not in GENERIC_ACCOUNTS:
        text = re.sub(re.escape(OWNER), "owner", text, flags=re.I)
    if pattern := private_word_pattern():
        placeholders = {word.casefold(): value for word, value in PRIVATE_WORDS.items()}
        text = pattern.sub(lambda m: placeholders.get(m[0].casefold(), "Owner"), text)
    return text


def problems(text: str) -> list[str]:
    lowered = text.lower()
    found = [b for b in BANNED if b in lowered]
    if OWNER.lower() not in GENERIC_ACCOUNTS and OWNER.lower() in lowered:
        found.append(OWNER.lower())
    if pattern := private_word_pattern():
        found += [m[0] for m in pattern.finditer(text)]
    found += [m[0] for m in SLUG_PATH.finditer(text)]
    found += [m[0] for m in SLUG_PATH_POSIX.finditer(text)]
    found += [m[0][:12] + "…" for m in BEARER_TOKEN.finditer(text)]
    found += [
        m[1] + m[2][:8] + "…"
        for m in SYNCED_PLUGIN.finditer(text)
        if m[2].lower() != SYNCED_PLACEHOLDER
    ]
    return found + [m[0] for m in EMAIL.finditer(text) if _is_foreign(m[0])]


def joined_block_problems(lines: list[str]) -> list[str]:
    """Problems visible only once a content block's streamed deltas are put back together.

    A CLI stream can split one string over consecutive `content_block_delta` frames of the same
    block (subagent-explore.jsonl lines 17-18 once split an account name this way); each
    line scrubs and passes `problems()` on its own half, so the guard must also check the whole.
    """
    runs: dict[tuple[object, object], list[str]] = {}
    texts: list[str] = []

    def _flush(key: tuple[object, object]) -> None:
        if pieces := runs.pop(key, None):
            texts.append("".join(pieces))

    for line in lines:
        frame = json.loads(line).get("frame", {})
        event = frame.get("event", {})
        etype = event.get("type")
        key = (frame.get("session_id"), event.get("index"))
        if etype == "content_block_start":
            _flush(key)  # a new block reusing this index starts a fresh run
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            piece = delta.get("partial_json") if delta.get("type") == "input_json_delta" else None
            piece = delta.get("text") if delta.get("type") == "text_delta" else piece
            piece = delta.get("thinking") if delta.get("type") == "thinking_delta" else piece
            if piece is not None:
                runs.setdefault(key, []).append(piece)
        elif etype == "content_block_stop":
            _flush(key)
    for key in list(runs):  # a run still open at EOF (a truncated capture) counts too
        _flush(key)
    return [p for text in texts for p in problems(text)]


def process_lines(lines: list[str], process: int) -> list[str]:
    """The lines of the `process`-th claude process in a capture (1-based). A capture file can
    hold several processes (respawns); each one starts by sending `initialize`."""
    count, kept = 0, []
    for line in lines:
        entry = json.loads(line)
        frame = entry.get("frame", {})
        if entry.get("dir") == "in" and frame.get("request", {}).get("subtype") == "initialize":
            count += 1
        if count == process:
            kept.append(line)
    return kept


def kept_entries(lines: list[str], keep_types: frozenset[str]) -> list[str]:
    """The transcript entries of `keep_types`, each `parentUuid` pointing past the dropped
    entries to its nearest kept ancestor, so the fixture's chain stays whole as in a real
    transcript (where no parent is ever missing)."""
    entries = [json.loads(line) for line in lines]
    parent = {e["uuid"]: e.get("parentUuid") for e in entries if isinstance(e.get("uuid"), str)}
    kept_uuids = {e.get("uuid") for e in entries if e.get("type") in keep_types}
    kept = []
    for entry in entries:
        if entry.get("type") not in keep_types:
            continue
        up, seen = entry.get("parentUuid"), set()
        while up is not None and up not in kept_uuids and up not in seen:
            seen.add(up)
            up = parent.get(up)
        if "parentUuid" in entry:
            entry["parentUuid"] = up
        kept.append(json.dumps(entry, ensure_ascii=False))
    return kept


def main(
    raw: Path, dest: Path, vault: Path, process: int = 1, keep_types: frozenset[str] = frozenset()
) -> int:
    if DO_NOT_COMMIT in raw.name:
        msg = f"refusing to scrub {raw.name}: it records one of the owner's own sessions"
        print(msg, file=sys.stderr)
        return 1
    lines = [line for line in raw.read_text(encoding="utf-8").splitlines() if line.strip()]
    # A transcript keeps whole entries of these types; a capture keeps one claude process.
    lines = kept_entries(lines, keep_types) if keep_types else process_lines(lines, process)
    scrubbed = [scrub_line(line, vault) for line in lines]
    for line in scrubbed:
        json.loads(line)  # scrubbing must never break a frame
    leftover = {p for line in scrubbed for p in problems(line)}
    leftover |= set(joined_block_problems(scrubbed))
    leftover = sorted(leftover)
    if leftover:
        print(f"refusing to write {dest}: still contains {leftover}", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    # LF on every platform: Windows text mode would otherwise write CRLF into the fixture.
    dest.write_text("\n".join(scrubbed) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {dest} ({len(scrubbed)} frames)")
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Scrub a raw capture into a fixture.")
    parser.add_argument("raw", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--process", type=int, default=1, help="which claude process (1-based)")
    parser.add_argument(
        "--keep-types",
        default="",
        help="for transcripts: comma-separated entry types to keep (e.g. user,assistant); "
        "attachments carry vault instructions and the system prompt, so never keep those",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        required=True,
        help="the root scrubbed to %%VAULT%% (a scratch capture's scratch folder)",
    )
    args = parser.parse_args(argv)
    keep = frozenset(t for t in args.keep_types.split(",") if t)
    return main(args.raw, args.dest, args.vault, args.process, keep)


if __name__ == "__main__":
    sys.exit(cli())
