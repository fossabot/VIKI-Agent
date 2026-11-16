"""Tests for scripts/image_input_smoke.py: the transcript reader, main()'s preflight refusals, the
timeout and unreadable-transcript paths, a full run with a fake driver (the scratch root, the
driver's working directory, the single copy, the two stderr lines), and a copy that fails without
losing the scan it already made.

Never spawns the real claude.exe: winproc.run and ClaudeDriver are spied on and must not be
called once a preflight refusal has already decided the outcome (importing the script the way
tests/test_fixtures_clean.py imports capture.py).
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import image_input_smoke as p2_image_smoke  # noqa: E402

from viki_agent import catalog, config, events  # noqa: E402

PNG = p2_image_smoke.synthetic_png()
OTHER_PNG = b"\x89PNG\r\n\x1a\nnot-the-same-bytes"


def _write(path: Path, entries: list[Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for entry in entries:
            fh.write(entry if isinstance(entry, str) else json.dumps(entry))
            fh.write("\n")


def _user_entry(content: list[dict[str, Any]], version: str = "2.1.281") -> dict[str, Any]:
    return {"type": "user", "version": version, "message": {"role": "user", "content": content}}


def _image_block(data: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": encoded},
    }


def test_read_transcript_finds_the_synthetic_image_inline(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write(
        path,
        [_user_entry([{"type": "text", "text": "look"}, _image_block(PNG)])],
    )
    evidence = p2_image_smoke.read_transcript(path, PNG)
    assert evidence == {
        "transcript_found": True,
        "transcript_prompt_blocks": ["text", "image"],
        "transcript_image_inline": True,
        "transcript_versions": ["2.1.281"],
    }


def test_read_transcript_rejects_a_different_image(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write(path, [_user_entry([_image_block(OTHER_PNG)])])
    evidence = p2_image_smoke.read_transcript(path, PNG)
    assert evidence["transcript_image_inline"] is False
    assert evidence["transcript_found"] is True


def test_read_transcript_with_no_image_block(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write(path, [_user_entry([{"type": "text", "text": "no picture here"}])])
    evidence = p2_image_smoke.read_transcript(path, PNG)
    assert evidence["transcript_prompt_blocks"] == ["text"]
    assert evidence["transcript_image_inline"] is False


def test_read_transcript_missing_file(tmp_path: Path) -> None:
    evidence = p2_image_smoke.read_transcript(tmp_path / "missing.jsonl", PNG)
    assert evidence == {
        "transcript_found": False,
        "transcript_prompt_blocks": [],
        "transcript_image_inline": False,
        "transcript_versions": [],
    }


def test_read_transcript_collects_distinct_versions_and_skips_unreadable_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    _write(
        path,
        [
            "not json at all",
            _user_entry([{"type": "text", "text": "one"}], version="2.1.281"),
            {"type": "assistant", "version": "2.1.259", "message": {"content": []}},
            _user_entry([_image_block(PNG)], version="2.1.281"),
        ],
    )
    evidence = p2_image_smoke.read_transcript(path, PNG)
    assert evidence["transcript_found"] is True
    assert evidence["transcript_versions"] == ["2.1.259", "2.1.281"]
    # The first user entry with list content sets the prompt blocks; a later one leaves it alone.
    assert evidence["transcript_prompt_blocks"] == ["text"]
    assert evidence["transcript_image_inline"] is True


def _cfg(tmp_path: Path) -> config.Config:
    return config.Config(
        vault=None,
        claude_home=tmp_path / ".claude",
        cli=tmp_path / "claude.exe",
        edge=None,
        data_dir=tmp_path / "data",
    )


def test_main_returns_2_on_a_failed_billing_preflight_without_spawning_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: _cfg(tmp_path))
    monkeypatch.setattr(
        p2_image_smoke.config, "billing_problems", lambda *a, **k: ["fake: apiKeyHelper is set"]
    )

    def _refuse_run(*_a: object, **_k: object) -> object:
        raise AssertionError("winproc.run must not be called when the billing preflight fails")

    class _RefuseDriver:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise AssertionError("ClaudeDriver must not be constructed when preflight fails")

    monkeypatch.setattr(p2_image_smoke.winproc, "run", _refuse_run)
    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _RefuseDriver)

    code = asyncio.run(p2_image_smoke.main())
    assert code == 2
    assert "apiKeyHelper" in capsys.readouterr().err


def _fake_run(*_a: object, **_k: object) -> SimpleNamespace:
    return SimpleNamespace(stdout="2.1.281\n")


def test_main_prints_the_line_when_the_turn_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: _cfg(tmp_path))
    monkeypatch.setattr(p2_image_smoke.config, "billing_problems", lambda *a, **k: [])
    monkeypatch.setattr(p2_image_smoke.winproc, "run", _fake_run)
    monkeypatch.setattr(p2_image_smoke, "TURN_TIMEOUT_S", 0.01)

    closed: list[bool] = []

    class _HangingDriver:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def open(self) -> None:
            return None

        async def send(self, _turn: object) -> None:
            await asyncio.Event().wait()  # never resolves; the outer timeout cancels it

        def respond(self, *_a: object, **_k: object) -> bool:
            return True

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _HangingDriver)

    code = asyncio.run(p2_image_smoke.main())

    assert closed == [True]
    line = json.loads(capsys.readouterr().out.strip())
    assert line["terminal"] == "timeout"
    assert code == 1


def test_main_prints_the_line_when_the_transcript_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(p2_image_smoke.config, "billing_problems", lambda *a, **k: [])
    monkeypatch.setattr(p2_image_smoke.winproc, "run", _fake_run)

    class _BadTranscriptDriver:
        def __init__(self, launch: Any, *, config: Any, emit: Any, **_k: object) -> None:
            self._launch = launch
            self._vault = config.vault
            self._emit = emit

        async def open(self) -> None:
            project = catalog.project_dir(cfg.claude_home, self._vault)
            project.mkdir(parents=True, exist_ok=True)
            (project / f"{self._launch.session_id}.jsonl").write_bytes(b"\xff\xfe\xfd")

        async def send(self, _turn: object) -> None:
            self._emit(events.TurnDone(status="success", usage=events.Usage()))

        def respond(self, *_a: object, **_k: object) -> bool:
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _BadTranscriptDriver)

    code = asyncio.run(p2_image_smoke.main())

    line = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert line["terminal"] == "success"
    assert line["transcript_found"] is False
    assert line["transcript_error"] == "UnicodeDecodeError"


def test_main_runs_end_to_end_with_a_fake_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins the preflight's scratch root, the driver's working directory and the single copy
    under the captures folder, end to end with a fake driver."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: cfg)

    seen_vaults: list[Path | None] = []

    def _billing_problems(passed_cfg: config.Config, *a: object, **k: object) -> list[str]:
        seen_vaults.append(passed_cfg.vault)
        return []

    monkeypatch.setattr(p2_image_smoke.config, "billing_problems", _billing_problems)
    monkeypatch.setattr(p2_image_smoke.winproc, "run", _fake_run)

    driver_vaults: list[Path] = []

    class _FakeDriver:
        def __init__(self, launch: Any, *, config: Any, emit: Any, **_k: object) -> None:
            self._launch = launch
            self._vault = config.vault
            self._emit = emit
            driver_vaults.append(config.vault)

        async def open(self) -> None:
            project = catalog.project_dir(cfg.claude_home, self._vault)
            project.mkdir(parents=True, exist_ok=True)
            transcript = project / f"{self._launch.session_id}.jsonl"
            _write(
                transcript,
                [_user_entry([{"type": "text", "text": "look"}, _image_block(PNG)])],
            )

        async def send(self, _turn: object) -> None:
            self._emit(events.TextDone(block_id="b1", text="left red, right blue"))
            self._emit(events.TurnDone(status="success", usage=events.Usage()))

        def respond(self, *_a: object, **_k: object) -> bool:
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _FakeDriver)

    code = asyncio.run(p2_image_smoke.main())

    assert code == 0
    captured = capsys.readouterr()
    line = json.loads(captured.out.strip())
    assert line["terminal"] == "success"
    assert line["left_red_right_blue"] is True
    assert line["transcript_found"] is True
    assert line["transcript_image_inline"] is True
    assert line["transcript_prompt_blocks"] == ["text", "image"]
    assert line["transcript_error"] is None

    scratch = seen_vaults[0]
    assert scratch is not None and scratch != cfg.vault  # the scratch root, not cfg.vault (None)
    assert driver_vaults[0] == scratch  # the driver's working directory is that same root

    copies = list(cfg.captures_dir.glob("*-image-smoke/*.jsonl"))
    assert len(copies) == 1
    assert copies[0].name == f"{line['session_id']}.jsonl"

    # The scratch root and the raw copy's destination are named on stderr, never in the report.
    assert f"scratch root: {scratch}" in captured.err
    assert f"raw copy: {copies[0]}" in captured.err


def test_main_prints_scratch_root_even_when_the_transcript_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scratch root reaches stderr before the copy, so a missing transcript still says where
    the CLI's project folder should have been."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(p2_image_smoke.config, "billing_problems", lambda *a, **k: [])
    monkeypatch.setattr(p2_image_smoke.winproc, "run", _fake_run)

    class _NoTranscriptDriver:
        def __init__(self, launch: Any, *, config: Any, emit: Any, **_k: object) -> None:
            self._emit = emit

        async def open(self) -> None:
            return None  # no project folder created: the transcript never exists

        async def send(self, _turn: object) -> None:
            self._emit(events.TurnDone(status="success", usage=events.Usage()))

        def respond(self, *_a: object, **_k: object) -> bool:
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _NoTranscriptDriver)

    code = asyncio.run(p2_image_smoke.main())

    assert code == 0
    err = capsys.readouterr().err
    assert "scratch root: " in err


def test_main_keeps_the_scan_when_the_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copy failure (disk full, a lock) must not turn a found, scanned transcript into a
    not-found one; the copy runs in its own try, whose failure only sets the error name."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(p2_image_smoke.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(p2_image_smoke.config, "billing_problems", lambda *a, **k: [])
    monkeypatch.setattr(p2_image_smoke.winproc, "run", _fake_run)

    class _FakeDriver:
        def __init__(self, launch: Any, *, config: Any, emit: Any, **_k: object) -> None:
            self._launch = launch
            self._vault = config.vault
            self._emit = emit

        async def open(self) -> None:
            project = catalog.project_dir(cfg.claude_home, self._vault)
            project.mkdir(parents=True, exist_ok=True)
            transcript = project / f"{self._launch.session_id}.jsonl"
            _write(
                transcript,
                [_user_entry([{"type": "text", "text": "look"}, _image_block(PNG)])],
            )

        async def send(self, _turn: object) -> None:
            self._emit(events.TurnDone(status="success", usage=events.Usage()))

        def respond(self, *_a: object, **_k: object) -> bool:
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(p2_image_smoke, "ClaudeDriver", _FakeDriver)

    def _raise_write_bytes(self: Path, _data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _raise_write_bytes)

    code = asyncio.run(p2_image_smoke.main())

    assert code == 0
    line = json.loads(capsys.readouterr().out.strip())
    assert line["transcript_found"] is True
    assert line["transcript_image_inline"] is True
    assert line["transcript_error"] == "OSError"
