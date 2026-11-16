"""Vault facts the earlier brief needed: inbox freshness and soon-due project tasks, read once
through the bounded, read-only `VaultService` and turned into small frozen facts the policy
layer can reason about without touching the filesystem itself. The same reads also carry the
oldest inbox captures' own excerpts and tags, each kept task's tags (its tasks.md's own joined
with its project overview's), and, through the separate `read_goals`, the user's active-project
goals, each with when its project folder last changed (`Goal.touched`, taken from the same scan
with no extra read).

Every read goes through `reader` (a `VaultService`); nothing here opens a file, logs, or raises
for an ordinary vault problem — a `VaultError` anywhere in reach only narrows what gets reported,
through `partial`, and never propagates out of `read_vault_facts`. `read_goals` skips a failed or
non-qualifying read the same way, with nothing of its own to mark partial.
"""

from __future__ import annotations

import hashlib
import heapq
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from viki_agent.automation.routines import stamp
from viki_agent.decision_loop.state import inline_tags
from viki_agent.vaults.vault import Note, NoteEntry, ScanResult, VaultError, VaultService

MAX_INBOX_READS = 20
MAX_INBOX_NOTE_BYTES = 65536
MAX_INBOX_ITEMS = 3
INBOX_EXCERPT_CHARS = 2000
MAX_TASK_FILES = 32
MAX_TASK_ITEMS = 3
MAX_GOAL_READS = 16
MAX_GOALS = 8
SOON_DAYS = 7
TEXT_MAX = 160

_INBOX_PREFIX = "00-inbox/"
_TASKS_REF = re.compile(r"^10-projects/[^/]+/tasks\.md$")
_PROJECT_OVERVIEW_REF = re.compile(r"^10-projects/([^/]+)/\1\.md$")
_TASK_LINE = re.compile(r"^\s*[-*+] \[ \] (.*)$")
_TICKED_LINE = re.compile(r"^\s*[-*+] \[[xX]\] ")
_DATE_TOKEN = re.compile(r"📅 (\d{4}-\d{2}-\d{2})")
_PLACEHOLDER = re.compile(r"^<[^<>]*>$")


@dataclass(frozen=True)
class InboxItem:
    ref: str
    title: str | None
    captured: str
    excerpt: str
    tags: tuple[str, ...] | None


@dataclass(frozen=True)
class InboxFacts:
    count: int
    oldest: str | None
    checked: int
    undated: int
    partial: bool
    items: tuple[InboxItem, ...] = ()


@dataclass(frozen=True)
class TaskItem:
    ref: str
    line: int
    text: str
    due: date
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TickedFile:
    ref: str
    count: int
    digest: str
    tags: tuple[str, ...] | None


@dataclass(frozen=True)
class TaskFacts:
    overdue: int
    today: int
    soon: int
    items: tuple[TaskItem, ...]
    files: int
    files_key: str
    partial: bool
    ticked: tuple[TickedFile, ...] = ()


@dataclass(frozen=True)
class VaultFacts:
    inbox: InboxFacts
    tasks: TaskFacts


@dataclass(frozen=True)
class Goal:
    ref: str
    title: str | None
    tags: tuple[str, ...] | None
    # Newest modification of any note under the project's folder; None when the scan was partial
    # (a newer note may be unseen) or the stamp is not a usable instant.
    touched: datetime | None = None


def captured_at(value: str | None) -> datetime | None:
    """A `captured:` frontmatter value as an aware instant, or None when it can't be trusted:
    unparsable, with no UTC offset, or so close to year 1 or year 9999 that its UTC form (what
    `stamp()` needs) overflows — fail closed rather than guess the user's local zone."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        parsed.astimezone(UTC)
    except OverflowError:
        return None
    return parsed


def _fenced_lines(lines: list[str]) -> list[bool]:
    """True for every line inside a ``` or ~~~ fence, delimiter lines included, plus every line
    of a multi-line HTML comment — an unclosed `<!--` line through the next line that contains
    `-->` — so a checklist line quoted inside a fenced example, or commented out, is never parsed
    as a real task."""
    fenced = [False] * len(lines)
    inside = False
    in_comment = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_comment:
            fenced[i] = True
            in_comment = "-->" not in stripped
            continue
        if not inside and stripped.startswith("<!--") and "-->" not in stripped:
            fenced[i] = True
            in_comment = True
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced[i] = True
            inside = not inside
        else:
            fenced[i] = inside
    return fenced


def _clean_text(text: str) -> str:
    """Backticks, `**` and wikilink brackets removed, whitespace collapsed."""
    cleaned = text.replace("`", "").replace("**", "").replace("[[", "").replace("]]", "")
    return " ".join(cleaned.split())


def _cut(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters, with "…" last if cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body_text(text: str) -> str:
    """`text` after its closing frontmatter `---` line, unchanged; the whole text when there is
    no frontmatter block, empty when one opens but never closes."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :])
    return ""


def tags_of(note: Note) -> tuple[str, ...] | None:
    """Lower-cased frontmatter tags with any leading `#` stripped, as the display parser reads
    them: `()` for a note with none, whether `tags: []` or no `tags` key at all (as in most
    tasks.md files and inbox captures, which are schema-exempt). `None` when they are unknown:
    the display parser could not read the frontmatter, or it found no frontmatter where the
    plain key reader still found a `tags` key (a "--- " opening line, say)."""
    display = note.display_metadata
    if display is None or display.tags is None:
        return None
    if display.frontmatter_state == "absent" and "tags" in note.frontmatter:
        return None
    return tuple(tag.removeprefix("#").lower() for tag in display.tags)


def parse_tasks(text: str, today: date) -> list[tuple[int, str, date]]:
    """Open (unchecked) checkbox lines outside fenced code blocks that carry a first
    `📅 YYYY-MM-DD` due date no later than `today + SOON_DAYS`: `(1-based line, display text,
    due date)`. An invalid calendar date skips the line; a line with no date token at all is not
    a dated task; a line whose display text is nothing but one untouched `<placeholder>` (a
    template's `<next action>`) is skipped too."""
    lines = text.split("\n")
    fenced = _fenced_lines(lines)
    horizon = today + timedelta(days=SOON_DAYS)
    items: list[tuple[int, str, date]] = []
    for i, raw in enumerate(lines):
        if fenced[i]:
            continue
        match = _TASK_LINE.match(raw.removesuffix("\r"))
        if match is None:
            continue
        content = match.group(1)
        found = _DATE_TOKEN.search(content)
        if found is None:
            continue
        try:
            due = date.fromisoformat(found.group(1))
        except ValueError:
            continue
        if due > horizon:
            continue
        display = _clean_text(content[: found.start()] + content[found.end() :])
        if _PLACEHOLDER.match(display):
            continue
        items.append((i + 1, _cut(display, TEXT_MAX), due))
    return items


def parse_ticked(text: str) -> list[str]:
    """Checked (`- [x]` / `- [X]`) checkbox lines outside fenced code blocks (the same fence
    rule `parse_tasks` uses, so a checked box quoted inside a fenced example or a comment is
    never counted), in original order, each with any trailing `\\r` stripped."""
    lines = text.split("\n")
    fenced = _fenced_lines(lines)
    ticked: list[str] = []
    for i, raw in enumerate(lines):
        if fenced[i]:
            continue
        line = raw.removesuffix("\r")
        if _TICKED_LINE.match(line):
            ticked.append(line)
    return ticked


def _files_key(pairs: Iterable[tuple[str, str]]) -> str:
    joined = "\n".join(f"{ref}\t{sha}" for ref, sha in pairs)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


_EMPTY_INBOX = InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=True)
_EMPTY_TASKS = TaskFacts(
    overdue=0, today=0, soon=0, items=(), files=0, files_key=_files_key(()), partial=True
)


def _inbox_item(entry: NoteEntry, note: Note, at: datetime) -> InboxItem:
    display = note.display_metadata
    title = display.display_title if display is not None else None
    excerpt = _body_text(note.text)[:INBOX_EXCERPT_CHARS]
    return InboxItem(entry.ref, title, stamp(at), excerpt, tags_of(note))


def _inbox_facts(reader: VaultService, workspace_id: str, scan: ScanResult) -> InboxFacts:
    # `scan.entries` already arrives sorted by `ref.casefold()` (VaultService._scan_root), so
    # filtering keeps "the first 20 by ref" without a second sort.
    entries = [e for e in scan.entries if e.ref.casefold().startswith(_INBOX_PREFIX)]
    count = len(entries)
    checked = entries[:MAX_INBOX_READS]
    undated = 0
    read_failed = False
    oldest_at: datetime | None = None
    # Every dated, successfully-read note in `checked`: at most MAX_INBOX_READS, so collecting
    # them (rather than streaming through a heap) is cheap, and `items` below re-reads nothing.
    dated: list[tuple[datetime, NoteEntry, Note]] = []
    for entry in checked:
        if entry.bytes > MAX_INBOX_NOTE_BYTES:
            undated += 1
            continue
        try:
            note = reader.read(workspace_id, entry.ref)
        except VaultError:
            read_failed = True
            continue
        at = captured_at(note.frontmatter.get("captured"))
        if at is None:
            undated += 1
            continue
        if oldest_at is None or at < oldest_at:
            oldest_at = at
        dated.append((at, entry, note))
    partial = scan.partial or read_failed or count > MAX_INBOX_READS
    oldest = stamp(oldest_at) if oldest_at is not None else None
    kept = sorted(dated, key=lambda d: (d[0], d[1].ref))[:MAX_INBOX_ITEMS]
    items = tuple(_inbox_item(entry, note, at) for at, entry, note in kept)
    return InboxFacts(count, oldest, len(checked), undated, partial, items)


def _task_overview_tags(
    reader: VaultService, workspace_id: str, scan: ScanResult, items: tuple[TaskItem, ...]
) -> dict[str, tuple[str, ...] | None]:
    """Each kept item's project overview's tags (`10-projects/<slug>/<slug>.md`): its frontmatter
    tags, then each inline tag of its whole already-read text once -- read once per distinct
    project among `items`, at most `MAX_TASK_ITEMS` extra reads, since `items` holds no more
    than that. A project with no overview has no tags, `()`: the read finds no such note and
    `scan` lists none in another letter case either. Any other failed read, or frontmatter tags
    that are themselves unknown, leaves that project's tags unknown, `None`."""
    tags_by_slug: dict[str, tuple[str, ...] | None] = {}
    for item in items:
        slug = item.ref.split("/")[1]
        if slug in tags_by_slug:
            continue
        ref = f"10-projects/{slug}/{slug}.md"
        try:
            note = reader.read(workspace_id, ref)
        except VaultError as error:
            missing = error.code == "missing_note" and not _listed(scan, ref)
            tags_by_slug[slug] = () if missing else None
            continue
        own = tags_of(note)
        tags_by_slug[slug] = (
            own if own is None else tuple(dict.fromkeys((*own, *inline_tags(note.text))))
        )
    return tags_by_slug


def _listed(scan: ScanResult, ref: str) -> bool:
    """Whether `scan` lists `ref` in any letter case."""
    key = ref.casefold()
    return any(entry.ref.casefold() == key for entry in scan.entries)


def _joined_tags(
    own: tuple[str, ...] | None, project: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """A task's tags: its tasks.md's own, then its project overview's, each once; None (unknown)
    when either is unknown."""
    if own is None or project is None:
        return None
    return tuple(dict.fromkeys((*own, *project)))


def _ticked_tags(
    own: tuple[str, ...] | None,
    slug: str,
    tags_by_slug: dict[str, tuple[str, ...] | None],
    scan: ScanResult,
) -> tuple[str, ...] | None:
    """A ticked file's tags, joined with its project overview's the same way a kept task's are,
    without any extra read: the overview's tags already in `tags_by_slug` when this project had
    a kept item (so its overview was read for that); `()` (no overview, so `own` alone is already
    complete) when `scan` lists no overview for `slug` in any case; otherwise unknown (`None`),
    so a never-send tag rule on an unread overview still holds the file back rather than risk
    sending it."""
    if slug in tags_by_slug:
        return _joined_tags(own, tags_by_slug[slug])
    if not _listed(scan, f"10-projects/{slug}/{slug}.md"):
        return _joined_tags(own, ())
    return None


def _task_facts(
    reader: VaultService, workspace_id: str, scan: ScanResult, today: date
) -> TaskFacts:
    # Same pre-sorted invariant as the inbox entries above.
    candidates = [e for e in scan.entries if _TASKS_REF.match(e.ref.casefold())][:MAX_TASK_FILES]
    pairs: list[tuple[str, str]] = []
    ticked: list[TickedFile] = []
    read_failed = False
    counts = {"overdue": 0, "today": 0, "soon": 0}

    def parsed_items() -> Iterable[TaskItem]:
        nonlocal read_failed
        for entry in candidates:
            try:
                note = reader.read(workspace_id, entry.ref)
            except VaultError:
                read_failed = True
                continue
            pairs.append((entry.ref, note.sha256))
            own_tags = tags_of(note)  # joined with the overview's below, once the best are known
            ticked_lines = parse_ticked(note.text)  # same read as above: never a second open
            # TickedFile's own tags also gain the file's inline tags (an inline never-send tag
            # may sit on some other line than the ticked ones, which TickedFile's own evidence
            # excerpt is too short to reach); TaskItem's `own_tags` above is left as it is, since
            # its own excerpt is the task's own line, whose inline tags state already reads.
            ticked_tags = (
                own_tags
                if own_tags is None
                else tuple(dict.fromkeys((*own_tags, *inline_tags(note.text))))
            )
            ticked.append(
                TickedFile(
                    entry.ref,
                    len(ticked_lines),
                    hashlib.sha256("\n".join(ticked_lines).encode("utf-8")).hexdigest(),
                    ticked_tags,
                )
            )
            for line, line_text, due in parse_tasks(note.text, today):
                if due < today:
                    counts["overdue"] += 1
                elif due == today:
                    counts["today"] += 1
                else:
                    counts["soon"] += 1
                yield TaskItem(entry.ref, line, line_text, due, own_tags)

    def bucket_key(item: TaskItem) -> tuple[int, date, str, int]:
        bucket = 0 if item.due < today else 1 if item.due == today else 2
        return (bucket, item.due, item.ref, item.line)

    # The byte cap bounds the bytes read, not the parsed form: 32 capped files can still parse into
    # millions of TaskItems. `nsmallest` walks this generator once and keeps only the best
    # MAX_TASK_ITEMS in memory, instead of materializing every item and sorting three times.
    best = tuple(heapq.nsmallest(MAX_TASK_ITEMS, parsed_items(), key=bucket_key))
    tags_by_slug = _task_overview_tags(reader, workspace_id, scan, best)
    best = tuple(
        replace(item, tags=_joined_tags(item.tags, tags_by_slug.get(item.ref.split("/")[1])))
        for item in best
    )
    ticked_files = tuple(
        replace(tf, tags=_ticked_tags(tf.tags, tf.ref.split("/")[1], tags_by_slug, scan))
        for tf in ticked
    )
    return TaskFacts(
        overdue=counts["overdue"],
        today=counts["today"],
        soon=counts["soon"],
        items=best,
        files=len(candidates),
        files_key=_files_key(pairs),
        partial=scan.partial or read_failed,
        ticked=ticked_files,
    )


def read_vault_facts(reader: VaultService, workspace_id: str, today: date) -> VaultFacts:
    """Inbox freshness and soon-due project tasks, from one `reader.scan`. A `VaultError` there
    gives empty, all-partial facts; this function itself never raises."""
    try:
        scan = reader.scan(workspace_id)
    except VaultError:
        return VaultFacts(inbox=_EMPTY_INBOX, tasks=_EMPTY_TASKS)
    return VaultFacts(
        inbox=_inbox_facts(reader, workspace_id, scan),
        tasks=_task_facts(reader, workspace_id, scan, today),
    )


def _instant(stamp: float | None) -> datetime | None:
    try:
        return None if stamp is None else datetime.fromtimestamp(stamp, UTC)
    except OverflowError, OSError, ValueError:
        return None


def read_goals(reader: VaultService, workspace_id: str, scan: ScanResult) -> tuple[Goal, ...]:
    """Up to `MAX_GOALS` active projects, newest-modified first: project overviews
    (`10-projects/<slug>/<slug>.md`) whose frontmatter reads `type: project` and
    `status: active`, from at most `MAX_GOAL_READS` reads of the given `scan` (never re-scanned
    here). A failed read, or an overview that isn't an active project, is skipped rather than
    raised; reading stops as soon as `MAX_GOALS` qualify. Each goal's `touched` is the newest
    `modified` of any scanned note under its project folder (nested, any letter case), or None
    when the scan is partial or that stamp is not finite or cannot become a datetime."""
    newest: dict[str, float] = {}
    for entry in scan.entries:
        parts = entry.ref.casefold().split("/")
        if len(parts) >= 3 and parts[0] == "10-projects":
            newest[parts[1]] = max(newest.get(parts[1], entry.modified), entry.modified)
    candidates = sorted(
        (entry for entry in scan.entries if _PROJECT_OVERVIEW_REF.match(entry.ref.casefold())),
        key=lambda entry: entry.modified,
        reverse=True,
    )[:MAX_GOAL_READS]
    goals: list[Goal] = []
    for entry in candidates:
        if len(goals) >= MAX_GOALS:
            break
        try:
            note = reader.read(workspace_id, entry.ref)
        except VaultError:
            continue
        display = note.display_metadata
        if display is None or display.type != "project" or display.status != "active":
            continue
        slug = note.ref.casefold().split("/")[1]
        goals.append(
            Goal(
                note.ref,
                display.display_title,
                tags_of(note),
                None if scan.partial else _instant(newest.get(slug)),
            )
        )
    return tuple(goals)
