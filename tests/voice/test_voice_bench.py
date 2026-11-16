"""voice_bench's pure helpers: no engine, no GPU, no network."""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import voice_bench  # noqa: E402


def test_normalize_drops_punctuation_and_folds_width_and_case() -> None:
    assert voice_bench.normalize("Hello, World! It's ＡＢＣ.", "word") == [
        "hello",
        "world",
        "its",
        "abc",
    ]
    assert voice_bench.normalize("今日は、 晴れ。", "char") == list("今日は晴れ")


def test_error_rate_counts_words_for_english_and_characters_for_japanese() -> None:
    assert voice_bench.error_rate("The cat sat.", "the cat sat", "word") == 0
    assert voice_bench.error_rate("the cat sat down", "the cat sit", "word") == 0.5
    assert voice_bench.error_rate("今日は晴れです。", "今日は雨です", "char") == 2 / 7
    assert voice_bench.error_rate("", "", "word") == 0
    assert voice_bench.error_rate("", "text", "word") == 1


def test_edit_distance() -> None:
    assert voice_bench.edit_distance("", "") == 0
    assert voice_bench.edit_distance("abc", "") == 3
    assert voice_bench.edit_distance("kitten", "sitting") == 3
    assert voice_bench.edit_distance(["a", "b"], ["a", "c", "b"]) == 1


def test_percentile_ranks_a_miss_last() -> None:
    values = [1.0, 2.0, 3.0, 4.0, math.inf]
    assert voice_bench.percentile(values, 50) == 3.0
    assert voice_bench.percentile(values, 80) == 4.0
    assert voice_bench.percentile(values, 95) == math.inf
    assert voice_bench.percentile([], 50) is None


def _frame(level: int) -> bytes:
    return struct.pack("<320h", *([level] * 320))


def test_onset_is_the_first_frame_above_minus_35_dbfs() -> None:
    assert voice_bench.onset_ms(bytes(640 * 10)) is None
    tone = _frame(2000)  # -24 dBFS
    assert voice_bench.onset_ms(bytes(640 * 10) + tone + tone) == 200
    quiet = _frame(300)  # -41 dBFS: under the threshold
    assert voice_bench.onset_ms(quiet * 5) is None


def test_memory_used_reads_the_first_gpu_line() -> None:
    assert voice_bench.memory_used_mib("1481\n") == 1481
    assert voice_bench.memory_used_mib(" 812 \n 20\n") == 812
    assert voice_bench.memory_used_mib("[N/A]\n") is None
    assert voice_bench.memory_used_mib("") is None


def test_child_pid_is_the_python_child_of_the_parent() -> None:
    rows = [
        (100, 1, "explorer.exe"),
        (200, 100, "python.exe"),  # the venv launcher
        (201, 200, "conhost.exe"),
        (202, 200, "Python.exe"),  # its child, the worker
        (300, 999, "python.exe"),
    ]
    assert voice_bench.child_pid(rows, 200) == 202
    assert voice_bench.child_pid(rows, 202) is None
    assert voice_bench.child_pid([], 200) is None


def test_select_items_takes_the_first_n_per_language_and_quiet_kind() -> None:
    items = [
        {"wav": "ja0", "kind": "speech", "language": "ja"},
        {"wav": "ja1", "kind": "speech", "language": "ja"},
        {"wav": "en0", "kind": "speech", "language": "en"},
        {"wav": "fr0", "kind": "speech", "language": "fr"},
        {"wav": "sw0", "kind": "switch", "language": "auto"},
        {"wav": "s0", "kind": "silence", "language": "auto"},
        {"wav": "s1", "kind": "silence", "language": "auto"},
        {"wav": "n0", "kind": "noise", "language": "auto"},
    ]
    chosen = voice_bench.select_items(items, ["ja", "en"], 1)
    assert [item["wav"] for item in chosen] == ["ja0", "en0", "s0", "n0"]
    assert len(voice_bench.select_items(items, ["ja", "en"], None)) == 6


def test_summarize_counts_a_miss_in_the_percentile_and_text_on_quiet_items() -> None:
    def row(kind: str, language: str, onset: float | None, release: float | None) -> dict:
        return {
            "kind": kind,
            "language": language,
            "onset_partial_s": onset,
            "release_done_s": release,
            "error": 0.1,
            "partials": 1 if kind == "noise" else 0,
            "finals": 0,
            "capture_error": None,
        }

    rows = [
        row("speech", "ja", 1.0, 0.2),
        row("speech", "ja", None, 16.0),
        row("noise", "auto", None, 0.1),
    ]
    summary = voice_bench.summarize(rows, latency=True)
    ja = summary["languages"]["ja"]
    assert ja["onset_partial_p50_s"] == 1.0
    assert ja["onset_partial_p95_s"] is None  # a miss
    assert ja["release_done_misses"] == 1
    assert ja["mean_error_pct"] == 10.0
    assert summary["silence_and_noise"] == {
        "captures": 1,
        "nonempty_finals": 0,
        "nonempty_partials": 1,
    }
    assert (
        "onset_partial_p95_s" not in voice_bench.summarize(rows, latency=False)["languages"]["ja"]
    )
