from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from tests.vaults.test_vault import service
from viki_agent.vaults import vault as vault_module


@pytest.mark.parametrize("method", ["search", "backlinks"])
@pytest.mark.parametrize("failure", ["invalid_utf8", "projection", "none"])
def test_scan_charges_failed_decodes_and_preserves_one_body_overread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, failure: str
) -> None:
    raw = b"x" * 9 + (b"\xff" if failure == "invalid_utf8" else b"x")
    for index in range(5):
        (tmp_path / f"n{index}.md").write_bytes(raw)
    (tmp_path / "target.md").write_bytes(b"target")
    vault = service(tmp_path)
    reader_name = "_read_note_handle" if sys.platform == "win32" else "_read_note_at"
    original = getattr(vault_module, reader_name)
    actual: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> tuple[bytes, float]:
        data, modified = original(*args, **kwargs)
        actual.append(len(data))
        return data, modified

    def invalid_projection(*_args: Any, **_kwargs: Any) -> None:
        # The exception's one-byte object is not the ten-byte body already read.
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic projection failure")

    monkeypatch.setattr(vault_module, reader_name, counted)
    monkeypatch.setattr(vault_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(vault_module, "MAX_SCAN_BYTES", 25)
    if failure == "projection":
        monkeypatch.setattr(vault_module, "extract_display", invalid_projection)
    result = vault.search("w", "zz") if method == "search" else vault.backlinks("w", "target.md")

    assert actual == [10, 10, 10]  # The crossing body is read, then omitted.
    assert sum(actual) == 30 and result.scanned_bytes == 20
    assert not result
    assert result.partial
    if failure == "none":
        assert result.partial_reasons == ("byte_limit",)
        assert result.omitted == 4
    else:
        reason = "unreadable" if method == "search" else "changed"
        assert result.partial_reasons == ("byte_limit", reason)
        assert result.omitted == 6
