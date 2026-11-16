from __future__ import annotations

import hashlib
import sys

import pytest

from viki_agent.conversations.artifacts import (
    MAX_ARTIFACT_ID_CHARS,
    MAX_ARTIFACT_PREVIEW_BYTES,
    MAX_ARTIFACT_PREVIEW_LINES,
    MAX_ARTIFACT_RESULT_BYTES,
    ArtifactError,
    classify_artifact,
    observe_result,
)

ROOT = r"C:\Vault"


def test_only_exact_structured_file_tools_create_typed_artifacts() -> None:
    read = classify_artifact("tool-read", "Read", {"file_path": "notes/a.md"}, workspace=ROOT)
    write = classify_artifact(
        "tool-write", "Write", {"file_path": "notes/a.md", "content": "new"}, workspace=ROOT
    )
    edit = classify_artifact(
        "tool-edit",
        "Edit",
        {"file_path": "notes/a.md", "old_string": "old", "new_string": "new"},
        workspace=ROOT,
    )
    assert read is not None and (read.kind, read.operation) == ("file", "read")
    assert write is not None and (write.kind, write.operation) == ("file_change", "write")
    assert edit is not None and (edit.kind, edit.operation) == ("diff", "edit")


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "pytest"}),
        ("PowerShell", {"command": "git diff"}),
        ("mcp__files__read", {"file_path": "notes/a.md"}),
        ("Write", {"file_path": "notes/a.md"}),
        ("Edit", {"file_path": "notes/a.md", "old_string": "old"}),
        ("Read", "notes/a.md"),
    ],
)
def test_shell_text_and_malformed_shapes_make_no_artifact_claim(
    tool: str, tool_input: object
) -> None:
    assert classify_artifact("tool-1", tool, tool_input, workspace=ROOT) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path spellings")
def test_display_paths_are_lexical_workspace_relative_metadata() -> None:
    assert sys.platform == "win32"
    relative = classify_artifact("tool-1", "Read", {"file_path": r"Notes\Area.md"}, workspace=ROOT)
    absolute = classify_artifact(
        "tool-2", "Read", {"file_path": r"c:\vault\Notes\Area.md"}, workspace=ROOT
    )
    missing = classify_artifact(
        "tool-3", "Read", {"file_path": r"does-not-exist\note.md"}, workspace=ROOT
    )
    assert relative is not None and relative.path == "Notes/Area.md"
    assert absolute is not None and absolute.path == "Notes/Area.md"
    assert missing is not None and missing.path_state == "workspace_relative"


@pytest.mark.parametrize(
    "path",
    [
        r"..\outside.md",
        r"C:\other\note.md",
        r"\outside.md",
        r"C:relative.md",
        r"\\server\share\note.md",
        r".git\config",
        r"Notes\.ENV.production",
        r"Notes\credentials.json",
        r"Notes\CON.txt",
        "Notes/trailing. /file.md",
        "Notes/zero\x00name.md",
        "",
    ],
)
def test_unsafe_or_private_paths_are_redacted_without_dropping_the_observation(path: str) -> None:
    artifact = classify_artifact("tool-1", "Read", {"file_path": path}, workspace=ROOT)
    assert artifact is not None
    assert (artifact.path, artifact.path_state) == ("", "unknown")


def test_invalid_ids_make_no_descriptor() -> None:
    assert classify_artifact("", "Read", {"file_path": "a.md"}, workspace=ROOT) is None
    assert (
        classify_artifact(
            "x" * (MAX_ARTIFACT_ID_CHARS + 1),
            "Read",
            {"file_path": "a.md"},
            workspace=ROOT,
        )
        is None
    )
    assert classify_artifact("bad\ud800", "Read", {"file_path": "a.md"}, workspace=ROOT) is None


def test_result_digest_and_size_cover_complete_original_bytes_before_preview_changes() -> None:
    artifact = classify_artifact("tool-1", "Read", {"file_path": "a.md"}, workspace=ROOT)
    assert artifact is not None
    payload = b"line one\r\nline two\xff"
    result = observe_result(artifact, payload, is_error=False)
    assert result.observation == "result"
    assert result.byte_size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.preview == "line one\nline two\ufffd"
    assert not result.truncated


def test_preview_is_bounded_by_encoded_bytes_and_lines_with_typed_truncation() -> None:
    artifact = classify_artifact("tool-1", "Read", {"file_path": "a.md"}, workspace=ROOT)
    assert artifact is not None
    by_bytes = observe_result(artifact, ("é" * MAX_ARTIFACT_PREVIEW_BYTES).encode(), is_error=None)
    assert by_bytes.observation == "unknown"
    assert by_bytes.truncated
    assert len(by_bytes.preview.encode()) <= MAX_ARTIFACT_PREVIEW_BYTES
    assert by_bytes.preview.endswith("…")

    payload = "\n".join(f"line {index}" for index in range(MAX_ARTIFACT_PREVIEW_LINES + 1)).encode()
    by_lines = observe_result(artifact, payload, is_error=True)
    assert by_lines.observation == "error"
    assert by_lines.truncated
    assert len(by_lines.preview.removesuffix("…").splitlines()) == MAX_ARTIFACT_PREVIEW_LINES


def test_oversized_result_is_typed_instead_of_partially_hashed() -> None:
    artifact = classify_artifact("tool-1", "Read", {"file_path": "a.md"}, workspace=ROOT)
    assert artifact is not None
    with pytest.raises(ArtifactError, match="result_too_large") as raised:
        observe_result(artifact, b"x" * (MAX_ARTIFACT_RESULT_BYTES + 1), is_error=False)
    assert raised.value.code == "result_too_large"
