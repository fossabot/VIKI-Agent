"""Catalog measurements run on synthetic files and keep to the production read caps."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import measure_catalog  # noqa: E402
from synthetic_transcripts import Written, session_id, write_transcript  # noqa: E402

from viki_agent import catalog, config  # noqa: E402


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Written:
    home = tmp_path / "home"
    vault = measure_catalog.VAULT
    written = write_transcript(
        catalog.project_dir(home, vault),
        session_id(1),
        target_bytes=30_000,
        rng=random.Random(1),
        filler=(1, 2),
    )
    cfg = config.Config(
        vault=vault, claude_home=home, cli=None, edge=None, data_dir=tmp_path / "data"
    )
    monkeypatch.setattr(measure_catalog.config, "load", lambda: cfg)
    monkeypatch.setattr(measure_catalog, "_silence_library_warnings", lambda: None)
    monkeypatch.setattr(catalog, "TAIL_WINDOW_BYTES", 5000)
    return written


def test_parse_phases_does_not_measure_a_tail_as_a_whole_file(
    corpus: Written, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "FULL_READ_BYTES", 1000)

    def refuse_parse(path: Path):
        raise AssertionError("over-budget whole parse must be unavailable")

    monkeypatch.setattr(measure_catalog, "_parse", refuse_parse)
    assert measure_catalog.parse_phases(corpus.path) is None


def test_real_measurement_compares_a_complete_baseline(
    corpus: Written, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    limit = corpus.path.stat().st_size
    monkeypatch.setattr(catalog, "FULL_READ_BYTES", limit)
    measure_catalog.measure_real(measure_catalog.VAULT)
    output = capsys.readouterr().out
    assert f"whole {len(corpus.prompts)} prompts (complete)" in output
    assert "newest 20 identical: False" in output
    assert "unavailable" not in output
    assert limit == catalog.FULL_READ_BYTES


def test_real_measurement_skips_an_over_budget_whole_baseline(
    corpus: Written, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(catalog, "FULL_READ_BYTES", 1000)
    measure_catalog.measure_real(measure_catalog.VAULT)
    output = capsys.readouterr().out
    assert "whole-parse measurement unavailable" in output
    assert "comparison unavailable" in output
    assert "FULL_READ_BYTES" in output
    assert "newest 20 identical" not in output
    assert "heap during" not in output
    assert catalog.FULL_READ_BYTES == 1000


def test_real_measurement_skips_a_partial_baseline(
    corpus: Written, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    limit = corpus.path.stat().st_size
    monkeypatch.setattr(catalog, "FULL_READ_BYTES", limit)
    calls: list[int] = []

    def partial(reader: catalog.Catalog, session: str) -> catalog.HistoryPage:
        assert session == corpus.session_id
        calls.append(catalog.FULL_READ_BYTES)
        return catalog.HistoryPage([], None, "partial", "tail_budget")

    monkeypatch.setattr(measure_catalog.Catalog, "history", partial)
    measure_catalog.measure_real(measure_catalog.VAULT)
    output = capsys.readouterr().out
    assert "comparison unavailable" in output
    assert "partial" in output
    assert "newest 20 identical" not in output
    assert calls == [limit]
    assert limit == catalog.FULL_READ_BYTES


def test_synthetic_measurement_does_not_label_a_tail_as_a_whole_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_transcript(
        catalog.project_dir(tmp_path / "home", measure_catalog.VAULT),
        session_id(1),
        target_bytes=30_000,
        rng=random.Random(1),
        filler=(1, 2),
    )
    monkeypatch.setattr(catalog, "FULL_READ_BYTES", 1000)
    monkeypatch.setattr(catalog, "TAIL_WINDOW_BYTES", 5000)
    measure_catalog.measure_synthetic(tmp_path, 1, 1)
    output = capsys.readouterr().out
    assert "whole-parse measurement unavailable" in output
    assert "FULL_READ_BYTES" in output
    assert "ms per MiB parsed" not in output
    assert "heap during that parse" not in output
