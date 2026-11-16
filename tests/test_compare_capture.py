"""compare_capture.py: labels, key shapes and the report it writes."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

TESTS = Path(__file__).parent
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import compare_capture  # noqa: E402

from viki_agent import config  # noqa: E402

label = compare_capture.label


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _out(scenario: str, frame: dict) -> dict:
    return {"scenario": scenario, "dir": "out", "t_ms": 1, "frame": frame}


def _in(scenario: str, frame: dict) -> dict:
    return {"scenario": scenario, "dir": "in", "t_ms": 1, "frame": frame}


# --- label() --------------------------------------------------------------------------------


def test_label_default_type_subtype():
    assert label({"type": "system", "subtype": "init"}) == "system/init"
    assert label({"type": "system"}) == "system/"


def test_label_stream_event_with_and_without_delta():
    assert label({"type": "stream_event", "event": {"type": "message_start"}}) == (
        "stream_event/message_start"
    )
    frame = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta"}},
    }
    assert label(frame) == "stream_event/content_block_delta/text_delta"


def test_label_assistant_and_user_block_types():
    frame = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use"}, {"type": "text"}]},
    }
    assert label(frame) == "assistant:text+tool_use"
    frame = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
    assert label(frame) == "user:tool_result"


def test_label_result_with_and_without_terminal_reason():
    frame = {"type": "result", "subtype": "success", "terminal_reason": "completed"}
    assert label(frame) == "result/success/completed"
    assert label({"type": "result", "subtype": "error_max_turns"}) == "result/error_max_turns/"


# --- compare() --------------------------------------------------------------------------------


def _scenario_setup(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    fixtures_dir = tmp_path / "fixtures"
    run_dir = tmp_path / "run"
    monkeypatch.setattr(compare_capture, "FIXTURES_DIR", fixtures_dir)
    return fixtures_dir, run_dir


def test_added_removed_and_shared_key_added_at_depth_two(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    shared_fixture = {"type": "system", "subtype": "shared", "a": {"one": 1}}
    shared_run = {"type": "system", "subtype": "shared", "a": {"one": 1, "two": 2}}
    removed_frame = {"type": "system", "subtype": "removed"}
    added_frame = {"type": "system", "subtype": "added"}

    _write(fixtures_dir / "fx.jsonl", [_out("X", shared_fixture), _out("X", removed_frame)])
    _write(run_dir / "X.jsonl", [_out("X", shared_run), _out("X", added_frame)])

    report = compare_capture.compare(run_dir)
    entry = report["X"]
    assert entry["added"] == ["system/added"]
    assert entry["removed"] == ["system/removed"]
    assert entry["keys_diff"] == {"system/shared": {"added": ["a/two"], "removed": []}}


def test_hook_frames_set_apart(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    ordinary = {"type": "system", "subtype": "init"}
    hook = {"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"}

    _write(fixtures_dir / "fx.jsonl", [_out("X", ordinary), _out("X", hook)])
    _write(run_dir / "X.jsonl", [_out("X", ordinary), _out("X", hook), _out("X", hook)])

    report = compare_capture.compare(run_dir)
    entry = report["X"]
    # the hook label never shows in added/removed/keys_diff: it is set apart
    assert entry["added"] == []
    assert entry["removed"] == []
    assert entry["keys_diff"] == {}
    assert entry["hooks"] == {
        "run": {"system/hook_started": 2},
        "fixture": {"system/hook_started": 1},
    }


def test_control_frames_excluded_from_comparison(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    control = {"type": "control_response", "response": {"subtype": "success"}}
    ordinary = {"type": "system", "subtype": "init"}

    _write(fixtures_dir / "fx.jsonl", [_out("X", ordinary)])
    _write(run_dir / "X.jsonl", [_out("X", ordinary), _out("X", control)])

    report = compare_capture.compare(run_dir)
    entry = report["X"]
    assert entry["added"] == []
    assert entry["removed"] == []


def test_baseline_scenario_reports_m0b(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})

    report = compare_capture.compare(run_dir)
    assert report["12"] == {"pass": None, "error": None, "baseline": "M0b"}


def test_missing_key_file_is_reported_not_a_crash(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})
    _write(fixtures_dir / "fx.jsonl", [_out("X", {"type": "system", "subtype": "init"})])
    run_dir.mkdir(parents=True)  # no X.jsonl inside

    report = compare_capture.compare(run_dir)
    assert report["X"]["missing"] is True


def test_pass_and_error_read_from_summary(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})
    run_dir.mkdir(parents=True)
    summary = {"results": {"12": {"title": "t", "pass": False, "error": "TimeoutError: x"}}}
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    report = compare_capture.compare(run_dir)
    assert report["12"]["pass"] is False
    assert report["12"]["error"] == "TimeoutError: x"


def test_initialize_reply_diff_for_scenario_01(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"01": ("fx",)})

    def init_response(models: list[str], commands: int) -> dict:
        return {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req_1",
                "response": {
                    "models": [{"value": m} for m in models],
                    "commands": [{}] * commands,
                },
            },
        }

    _write(
        fixtures_dir / "fx.jsonl",
        [_in("01", {"type": "control_request"}), _out("01", init_response(["sonnet"], 2))],
    )
    _write(
        run_dir / "01.jsonl",
        [_in("01", {"type": "control_request"}), _out("01", init_response(["sonnet", "opus"], 2))],
    )

    report = compare_capture.compare(run_dir)
    init = report["01"]["initialize"]
    assert init["models"] == {"run": ["opus", "sonnet"], "fixture": ["sonnet"]}
    assert init["commands_count"] == {"run": 2, "fixture": 2}


# --- mapper replay ------------------------------------------------------------------------------


def test_mapper_replay_reports_an_error_event(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    ordinary = {"type": "system", "subtype": "init"}
    frame_error = {"type": "viki.frame_error", "reason": "too_large"}

    _write(fixtures_dir / "fx.jsonl", [_out("X", ordinary)])
    _write(run_dir / "X.jsonl", [_out("X", ordinary), _out("X", frame_error)])

    report = compare_capture.compare(run_dir)
    assert report["X"]["mapper"] == {"run_errors": ["frame_error"]}


def test_mapper_replay_reports_a_turn_done_diff(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    fixture_result = {"type": "result", "subtype": "success", "is_error": False}
    run_result = {"type": "result", "subtype": "success", "is_error": True}

    _write(fixtures_dir / "fx.jsonl", [_out("X", fixture_result)])
    _write(run_dir / "X.jsonl", [_out("X", run_result)])

    report = compare_capture.compare(run_dir)
    assert report["X"]["mapper"]["turn_done"] == {"run": ["error"], "fixture": ["success"]}


def test_mapper_replay_with_no_differences_is_empty(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    same = {"type": "result", "subtype": "success", "is_error": False}
    _write(fixtures_dir / "fx.jsonl", [_out("X", same)])
    _write(run_dir / "X.jsonl", [_out("X", same)])

    report = compare_capture.compare(run_dir)
    assert report["X"]["mapper"] == {}


def test_mapper_replay_exception_is_reported_not_raised(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    class RaisingMapper:
        def __init__(self, **_kwargs) -> None:
            pass

        def frame(self, _frame):
            raise RuntimeError("boom")

    monkeypatch.setattr(compare_capture, "Mapper", RaisingMapper)

    frame = {"type": "system", "subtype": "init"}
    _write(fixtures_dir / "fx.jsonl", [_out("X", frame)])
    _write(run_dir / "X.jsonl", [_out("X", frame)])

    report = compare_capture.compare(run_dir)
    assert report["X"]["mapper"]["run_exception"] == "RuntimeError"
    assert report["X"]["mapper"]["fixture_exception"] == "RuntimeError"


# --- history pass --------------------------------------------------------------------------------


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def _stub_history_run(
    tmp_path: Path, monkeypatch, key: str, entries: list[dict]
) -> tuple[Path, str]:
    """A run folder whose scratch project holds `entries` for one session, with `config.load`
    stubbed to a synthetic `claude_home` under `tmp_path`: the tests' own scratch, never the
    user's `~/.claude`."""
    run_dir = tmp_path / "run"
    claude_home = tmp_path / "claude_home"
    # A short, synthetic root: only its sanitized name matters (catalog.project_dir), and a
    # long one built from tmp_path's own nested folders can push the transcript path past
    # Windows's MAX_PATH.
    scratch_root = Path("C:/scratch")
    sid = str(uuid.uuid4())
    run_dir.mkdir(parents=True)
    summary = {"results": {key: {"session_ids": [sid]}}, "scratch_root": str(scratch_root)}
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    project_dir = compare_capture.catalog.project_dir(claude_home, scratch_root)
    _write_transcript(project_dir / f"{sid}.jsonl", entries)
    cfg = config.Config(
        vault=None, claude_home=claude_home, cli=None, edge=None, data_dir=tmp_path / "data"
    )
    monkeypatch.setattr(compare_capture.config, "load", lambda: cfg)
    return run_dir, sid


def test_history_reports_unhandled_types_and_ai_title(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ()})
    secret = "marker-only-the-owner-would-recognise"
    entries = [
        {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-09-11T08:00:00.000Z",
        },
        {"type": "ai-title", "title": secret},
        {"type": "summary", "summary": secret},
    ]
    run_dir, sid = _stub_history_run(tmp_path, monkeypatch, "X", entries)

    report = compare_capture.compare(run_dir)
    history = report["X"]["history"][0]
    assert history["session_id"] == sid
    assert history["unhandled_entry_types"] == ["ai-title", "summary"]
    assert history["ai_title"] is True
    assert history["skipped_lines"] == 0
    assert secret not in json.dumps(report)


def test_history_skips_a_torn_last_line_and_counts_it(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ()})
    entries = [
        {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-09-11T08:00:00.000Z",
        }
    ]
    run_dir, sid = _stub_history_run(tmp_path, monkeypatch, "X", entries)
    cfg = compare_capture.config.load()
    transcript = (
        compare_capture.catalog.project_dir(cfg.claude_home, Path("C:/scratch")) / f"{sid}.jsonl"
    )
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('\n{"type": "user", "message": {"rol')  # cut mid-object, as a killed CLI leaves

    report = compare_capture.compare(run_dir)
    history = report["X"]["history"][0]
    assert history["unhandled_entry_types"] == []
    assert history["skipped_lines"] == 1


def test_history_skips_a_non_object_line_and_a_multi_byte_tear(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ()})
    entries = [
        {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-09-11T08:00:00.000Z",
        }
    ]
    run_dir, sid = _stub_history_run(tmp_path, monkeypatch, "X", entries)
    cfg = compare_capture.config.load()
    transcript = (
        compare_capture.catalog.project_dir(cfg.claude_home, Path("C:/scratch")) / f"{sid}.jsonl"
    )
    with transcript.open("ab") as fh:
        fh.write(b'\n[]\n{"type": "assistant", "message": {"content": "left \xe2\x86')

    report = compare_capture.compare(run_dir)
    history = report["X"]["history"][0]
    assert history["skipped_lines"] == 2
    assert history["unhandled_entry_types"] == []


def test_load_lines_skips_a_deeply_nested_line_and_counts_it(tmp_path):
    path = tmp_path / "deep.jsonl"
    user = {"type": "user"}
    system = {"type": "system"}
    with path.open("wb") as fh:
        fh.write(json.dumps(user).encode("utf-8") + b"\n")
        fh.write(b"[" * 100000 + b"\n")
        fh.write(json.dumps(system).encode("utf-8") + b"\n")
    entries, skipped = compare_capture._load_lines(path)
    assert entries == [user, system]
    assert skipped == 1


def test_unhandled_entry_types_skips_a_deeply_nested_line_and_counts_it(tmp_path):
    path = tmp_path / "deep.jsonl"
    with path.open("wb") as fh:
        fh.write(json.dumps({"type": "user"}).encode("utf-8") + b"\n")
        fh.write(b"[" * 100000 + b"\n")
        fh.write(json.dumps({"type": "system"}).encode("utf-8") + b"\n")
    unhandled, ai_title, skipped = compare_capture._unhandled_entry_types(path)
    assert unhandled == set()
    assert ai_title is False
    assert skipped == 1


def test_history_types_compare_against_the_golden_for_named_scenarios(tmp_path, monkeypatch):
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    monkeypatch.setattr(compare_capture, "CONTRACTS_DIR", contracts_dir)
    monkeypatch.setattr(compare_capture, "HISTORY_GOLDENS", {"X": "golden.json"})
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ()})
    golden = {"events": [{"type": "user.message", "data": {}}, {"type": "text.done", "data": {}}]}
    (contracts_dir / "golden.json").write_text(json.dumps(golden), encoding="utf-8")

    entries = [
        {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-09-11T08:00:00.000Z",
        }
    ]
    run_dir, _sid = _stub_history_run(tmp_path, monkeypatch, "X", entries)

    report = compare_capture.compare(run_dir)
    history_types = report["X"]["history"][0]["history_types"]
    assert history_types == {
        "run": ["user.message"],
        "golden": ["user.message", "text.done"],
    }


def test_no_summary_scratch_root_omits_history(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})
    run_dir.mkdir(parents=True)

    report = compare_capture.compare(run_dir)
    assert "history" not in report["12"]


# --- main() -----------------------------------------------------------------------------------


def test_main_writes_only_compare_json(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "fixtures"
    run_dir = tmp_path / "run"
    monkeypatch.setattr(compare_capture, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})
    run_dir.mkdir(parents=True)
    before = set(run_dir.iterdir())

    exit_code = compare_capture.main([str(run_dir)])

    assert exit_code == 0
    after = set(run_dir.iterdir())
    assert after - before == {run_dir / "compare.json"}
    written = json.loads((run_dir / "compare.json").read_text(encoding="utf-8"))
    assert written == {"12": {"pass": None, "error": None, "baseline": "M0b"}}


# --- fixture-less run replay ---------------------------------------------------------------------


def test_baseline_scenario_reports_a_run_mapper_exception(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})

    class RaisingMapper:
        def __init__(self, **_kwargs) -> None:
            pass

        def frame(self, _frame):
            raise RuntimeError("boom")

    monkeypatch.setattr(compare_capture, "Mapper", RaisingMapper)

    _write(run_dir / "12.jsonl", [_out("12", {"type": "system", "subtype": "init"})])

    report = compare_capture.compare(run_dir)
    assert report["12"]["mapper"]["exception"] == "RuntimeError"
    assert report["12"]["baseline"] == "M0b"


def test_baseline_scenario_run_replay_has_no_exception(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})

    _write(
        run_dir / "12.jsonl",
        [
            _out("12", {"type": "system", "subtype": "init"}),
            _out("12", {"type": "result", "subtype": "success", "is_error": False}),
        ],
    )

    report = compare_capture.compare(run_dir)
    assert report["12"]["mapper"]["exception"] is None
    assert report["12"]["mapper"]["turn_done"] == ["success"]


def test_run_file_counts_torn_and_non_object_lines(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"X": ("fx",)})

    shared = {"type": "system", "subtype": "init"}
    _write(fixtures_dir / "fx.jsonl", [_out("X", shared)])
    run_dir.mkdir(parents=True, exist_ok=True)
    raw = [
        json.dumps(_out("X", shared), ensure_ascii=False),
        '{"type": "system", "subtype": "cut',
        "[1, 2, 3]",
    ]
    (run_dir / "X.jsonl").write_text("\n".join(raw) + "\n", encoding="utf-8")

    report = compare_capture.compare(run_dir)
    entry = report["X"]
    assert entry["skipped_lines"] == 2
    assert entry["keys_diff"] == {}


def test_baseline_scenario_counts_torn_and_non_object_lines(tmp_path, monkeypatch):
    fixtures_dir, run_dir = _scenario_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(compare_capture, "FIXTURES", {"12": ()})

    shared = {"type": "system", "subtype": "init"}
    run_dir.mkdir(parents=True, exist_ok=True)
    raw = [
        json.dumps(_out("12", shared), ensure_ascii=False),
        '{"type": "system", "subtype": "cut',
        "[1, 2, 3]",
    ]
    (run_dir / "12.jsonl").write_text("\n".join(raw) + "\n", encoding="utf-8")

    report = compare_capture.compare(run_dir)
    entry = report["12"]
    assert entry["skipped_lines"] == 2
    assert entry["mapper"]["exception"] is None
