"""Pure, bounded artifact observations derived from structured native tool data.

This module does not read a path or interpret a shell command.  Its path is display metadata only,
and its digest describes the exact native result bytes passed by the adapter, not a target file.
"""

from __future__ import annotations

import codecs
import hashlib
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

MAX_ARTIFACT_ID_CHARS = 160
MAX_ARTIFACT_ID_BYTES = 512
MAX_ARTIFACT_PATH_CHARS = 512
MAX_ARTIFACT_PATH_BYTES = 2_048
MAX_ARTIFACT_RESULT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_PREVIEW_BYTES = 2_048
MAX_ARTIFACT_PREVIEW_LINES = 40

ArtifactKind = Literal["file", "file_change", "diff", "test"]
ArtifactOperation = Literal["read", "write", "edit", "test"]
ArtifactObservation = Literal["result", "error", "unknown"]
PathState = Literal["workspace_relative", "unknown"]

_PRIVATE_COMPONENTS = frozenset(
    {
        ".git",
        ".claude",
        ".codex",
        ".viki",
        "credentials",
        "credentials.json",
        ".credentials.json",
        "secrets.json",
        ".env",
        ".env.local",
    }
)
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class ArtifactError(RuntimeError):
    def __init__(self, code: Literal["result_too_large"]) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, kw_only=True)
class ArtifactStart:
    artifact_id: str
    kind: ArtifactKind
    operation: ArtifactOperation
    path: str
    path_state: PathState


@dataclass(frozen=True, kw_only=True)
class ArtifactDetailRef:
    entry_uuid: str
    tool_use_id: str
    source_revision: str


@dataclass(frozen=True, kw_only=True)
class ArtifactResult:
    artifact_id: str
    observation: ArtifactObservation
    byte_size: int
    sha256: str
    preview: str
    truncated: bool
    source_revision: str = ""
    detail_ref: ArtifactDetailRef | None = None


def _bounded_text(value: str, *, chars: int, encoded: int) -> bool:
    if not 1 <= len(value) <= chars or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= encoded
    except UnicodeEncodeError:
        return False


def _private(component: str) -> bool:
    lowered = component.casefold()
    return lowered in _PRIVATE_COMPONENTS or lowered.startswith(".env.")


def _ordinary_component(component: str) -> bool:
    if (
        not component
        or component in {".", ".."}
        or component[-1] in " ."
        or any(ord(character) < 32 for character in component)
        or ":" in component
        or _private(component)
    ):
        return False
    return component.split(".", 1)[0].upper() not in _RESERVED_STEMS


def _posix_key(component: str) -> str:
    """A name as the filesystem compares it: exactly on Linux; on macOS, whose default APFS
    volume ignores case and Unicode normalization, casefolded after NFC: workspaces.root_key's
    rule, restated because workspaces.py imports this module back through events.py."""
    if sys.platform == "darwin":
        return unicodedata.normalize("NFC", component).casefold()
    return component


def _workspace_path(raw: object, workspace: str | Path) -> tuple[str, PathState]:
    if not isinstance(raw, str) or not _bounded_text(
        raw, chars=MAX_ARTIFACT_PATH_CHARS, encoded=MAX_ARTIFACT_PATH_BYTES
    ):
        return "", "unknown"

    if sys.platform == "win32":
        candidate = PureWindowsPath(raw.replace("/", "\\"))
        root = PureWindowsPath(str(workspace).replace("/", "\\"))
        if not root.is_absolute() or root.drive.startswith("\\"):
            return "", "unknown"

        if candidate.is_absolute():
            if (
                candidate.drive.startswith("\\")
                or candidate.drive.casefold() != root.drive.casefold()
            ):
                return "", "unknown"
            root_parts = root.parts
            parts = candidate.parts
            if len(parts) <= len(root_parts) or tuple(
                part.casefold() for part in parts[: len(root_parts)]
            ) != tuple(part.casefold() for part in root_parts):
                return "", "unknown"
            relative = parts[len(root_parts) :]
        else:
            # A rooted path without a drive ("\outside") and a drive-relative path ("C:file")
            # both escape the meaning of "relative to this workspace".
            if candidate.root or candidate.drive:
                return "", "unknown"
            relative = candidate.parts
    else:
        candidate = PurePosixPath(raw)
        root = PurePosixPath(str(workspace))
        if not root.is_absolute():
            return "", "unknown"
        if candidate.is_absolute():
            root_parts = root.parts
            parts = candidate.parts
            if len(parts) <= len(root_parts) or tuple(
                _posix_key(part) for part in parts[: len(root_parts)]
            ) != tuple(_posix_key(part) for part in root_parts):
                return "", "unknown"
            relative = parts[len(root_parts) :]
        else:
            relative = candidate.parts

    if not relative or any(not _ordinary_component(part) for part in relative):
        return "", "unknown"
    display = "/".join(relative)
    if not _bounded_text(display, chars=MAX_ARTIFACT_PATH_CHARS, encoded=MAX_ARTIFACT_PATH_BYTES):
        return "", "unknown"
    return display, "workspace_relative"


def classify_artifact(
    artifact_id: str,
    tool: str,
    tool_input: object,
    *,
    workspace: str | Path,
) -> ArtifactStart | None:
    """Classify exact structured Read/Write/Edit input, otherwise make no artifact claim."""
    if not _bounded_text(
        artifact_id, chars=MAX_ARTIFACT_ID_CHARS, encoded=MAX_ARTIFACT_ID_BYTES
    ) or not isinstance(tool_input, dict):
        return None

    typed: dict[str, Any] = tool_input
    if not isinstance(typed.get("file_path"), str):
        return None
    if tool == "Read":
        kind: ArtifactKind = "file"
        operation: ArtifactOperation = "read"
    elif tool == "Write" and isinstance(typed.get("content"), str):
        kind, operation = "file_change", "write"
    elif (
        tool == "Edit"
        and isinstance(typed.get("old_string"), str)
        and isinstance(typed.get("new_string"), str)
    ):
        kind, operation = "diff", "edit"
    else:
        return None

    path, path_state = _workspace_path(typed.get("file_path"), workspace)
    return ArtifactStart(
        artifact_id=artifact_id,
        kind=kind,
        operation=operation,
        path=path,
        path_state=path_state,
    )


def _clip_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _preview(payload: bytes) -> tuple[str, bool]:
    byte_cut = len(payload) > MAX_ARTIFACT_PREVIEW_BYTES
    prefix = payload[:MAX_ARTIFACT_PREVIEW_BYTES]
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(prefix, final=not byte_cut).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    line_cut = len(lines) > MAX_ARTIFACT_PREVIEW_LINES
    if line_cut:
        text = "\n".join(lines[:MAX_ARTIFACT_PREVIEW_LINES])
    text, encoding_cut = _clip_utf8(text, MAX_ARTIFACT_PREVIEW_BYTES)
    truncated = byte_cut or line_cut or encoding_cut
    if truncated:
        text, _ = _clip_utf8(text, MAX_ARTIFACT_PREVIEW_BYTES - len("…".encode()))
        text += "…"
    return text, truncated


def observe_result(
    artifact: ArtifactStart, payload: bytes, *, is_error: bool | None
) -> ArtifactResult:
    """Describe bounded native result bytes without claiming target-file or test state."""
    if len(payload) > MAX_ARTIFACT_RESULT_BYTES:
        raise ArtifactError("result_too_large")
    preview, truncated = _preview(payload)
    observation: ArtifactObservation = (
        "unknown" if is_error is None else "error" if is_error else "result"
    )
    return ArtifactResult(
        artifact_id=artifact.artifact_id,
        observation=observation,
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        preview=preview,
        truncated=truncated,
    )
