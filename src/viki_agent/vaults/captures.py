"""Read-only inbox capture listing: `00-inbox` previews and `10-projects` slugs for one vault.

Every kept capture is read once through `VaultService.read`; its real size is then charged
against a per-note cap and a shared byte budget for the whole call, so no shown preview is ever
built from a note over the cap, and one listing stays within the budget plus the one read that
found it already spent. Nothing here writes a vault, a lease or a store; the only vault touches
are `VaultService.scan` (entries only, no content) and `VaultService.read` (one bounded
open-read-close per kept capture, the same primitive `ProposalService._note` reads a note
through).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from viki_agent.automation.routines import stamp
from viki_agent.vaults.vault import NoteEntry, VaultError, VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MAX_CAPTURES = 100
MAX_CAPTURE_BYTES = 262_144
MAX_READ_BYTES = 4_194_304
PREVIEW_CHARS = 600
MAX_PROJECTS = 100

_INBOX = "00-inbox"
_PROJECTS = "10-projects"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FRONTMATTER_SCAN_LINES = 257  # the same window `vault._frontmatter` scans for a closing `---`


class CaptureError(Exception):
    """A typed refusal. `extra` carries only ids and limits, never note text."""

    def __init__(self, code: str, **extra: Any) -> None:
        self.code = code
        self.extra = extra
        super().__init__(code)


@dataclass(frozen=True)
class Capture:
    ref: str
    revision: str | None
    modified: str
    captured: str | None
    preview: str
    cut: bool
    unread: str | None


@dataclass(frozen=True)
class CaptureListing:
    workspace_id: str
    captures: tuple[Capture, ...]
    projects: tuple[str, ...]
    partial: bool
    omitted: int


def _modified_stamp(mtime: float) -> str:
    """The entry's mtime as `routines.stamp`'s ISO form.

    Epoch arithmetic, not `datetime.fromtimestamp`, which on Windows raises an untyped `OSError`
    for an ordinary pre-1970 mtime (`vault._stamp` carries the same note). Past year 9999, or
    before year 1, that arithmetic overflows instead of raising `OSError`; clamped to the
    client's STAMP pattern rather than raised, so one such file never fails the whole listing.
    """
    try:
        return stamp(_EPOCH + timedelta(seconds=mtime))
    except OverflowError:
        return "9999-12-31T23:59:59.999Z" if mtime > 0 else "0001-01-01T00:00:00.000Z"


def _captured_value(frontmatter: dict[str, str]) -> str | None:
    value = frontmatter.get("captured")
    return value if value is not None and 1 <= len(value) <= 64 else None


def _preview(text: str) -> tuple[str, bool]:
    """The first `PREVIEW_CHARS` characters of the body, frontmatter stripped when present.

    `keepends=True` and a running character offset, not a line rejoin, so the body is an exact
    substring of `text` (a trailing newline, or its absence, survives the strip untouched).
    """
    lines = text.splitlines(keepends=True)
    body = text
    if lines and lines[0].strip() == "---":
        offset = len(lines[0])
        for line in lines[1:_FRONTMATTER_SCAN_LINES]:
            offset += len(line)
            if line.strip() == "---":
                body = text[offset:]
                break
    return body[:PREVIEW_CHARS], len(body) > PREVIEW_CHARS


def _unread(entry: NoteEntry, reason: str) -> Capture:
    """A capture whose content was never read: only what the scan itself already knew."""
    return Capture(
        ref=entry.ref,
        revision=None,
        modified=_modified_stamp(entry.modified),
        captured=None,
        preview="",
        cut=False,
        unread=reason,
    )


class CaptureService:
    """List one vault's `00-inbox` captures and `10-projects` slugs. Reads only."""

    def __init__(self, workspaces: WorkspaceService, vault: VaultService) -> None:
        self._workspaces = workspaces
        self._vault = vault

    def check_active(self, workspace_id: str) -> None:
        """Refuse an id that is not this app's currently active vault. No scan, no read."""
        active = self._workspaces.resolve_active().workspace  # cached once per launch, no store
        if active.workspace_id == workspace_id and active.kind == "vault":
            return
        try:
            workspace = self._workspaces.get(workspace_id, kind=None)
        except WorkspaceError:
            raise CaptureError("unknown_workspace") from None
        if workspace.kind != "vault":
            raise CaptureError("not_a_vault")
        if self._workspaces.resolve_active().workspace.workspace_id != workspace_id:
            raise CaptureError("not_active_vault")

    def list(self, workspace_id: str) -> CaptureListing:
        self.check_active(workspace_id)
        try:
            scan = self._vault.scan(workspace_id)
        except VaultError as exc:
            raise CaptureError(exc.code) from None

        inbox: list[NoteEntry] = []
        projects: set[str] = set()
        for entry in scan.entries:
            parts = entry.ref.split("/")
            if parts[0].casefold() == _INBOX:
                inbox.append(entry)
            elif len(parts) >= 3 and parts[0].casefold() == _PROJECTS:
                projects.add(parts[1])
        inbox.sort(key=lambda item: item.ref.casefold())
        omitted = max(0, len(inbox) - MAX_CAPTURES)

        remaining = MAX_READ_BYTES
        budget_cut = False
        captures: list[Capture] = []
        for entry in inbox[:MAX_CAPTURES]:
            if entry.bytes > MAX_CAPTURE_BYTES:
                captures.append(_unread(entry, "too_large"))
                continue
            if entry.bytes > remaining:
                budget_cut = True
                captures.append(_unread(entry, "budget"))
                continue
            try:
                note = self._vault.read(workspace_id, entry.ref)
            except VaultError as exc:
                if exc.code == "missing_note":
                    continue
                captures.append(_unread(entry, exc.code))
                continue
            # Charge the real bytes just read, never the scan's stale size: a note can grow
            # between the scan and this read, and only what was actually read is true.
            remaining -= note.byte_size
            if note.byte_size > MAX_CAPTURE_BYTES:
                captures.append(_unread(entry, "too_large"))
                continue
            if remaining < 0:
                budget_cut = True
                captures.append(_unread(entry, "budget"))
                continue
            preview, cut = _preview(note.text)
            captures.append(
                Capture(
                    ref=entry.ref,
                    revision=note.sha256,
                    modified=_modified_stamp(entry.modified),
                    captured=_captured_value(note.frontmatter),
                    preview=preview,
                    cut=cut,
                    unread=None,
                )
            )

        return CaptureListing(
            workspace_id=workspace_id,
            captures=tuple(captures),
            projects=tuple(sorted(projects, key=str.casefold)[:MAX_PROJECTS]),
            partial=scan.partial or omitted > 0 or budget_cut,
            omitted=omitted,
        )
