"""Compare a scratch capture run with the committed fixtures: labels, key shapes, a mapper
replay and a history pass.

Offline: it never spawns a process, calls a provider, or reads the vault or credentials. It reads
a run folder written by `scripts/capture.py` (`summary.json` and one `<key>.jsonl` per scenario,
each `{scenario, dir, t_ms, frame}`), the sibling committed fixtures
under `tests/fixtures/claude/` and `tests/contracts/events/` (found from this file's own path,
the same layout), and the scratch transcripts under the run's own provider project folder, each
open-read-close with `encoding="utf-8"`, and writes only `<run_dir>/compare.json`, then prints
its path.

    uv run --directory <repo> python scripts/compare_capture.py <run_dir>

Only labels, key shapes, event and entry types, statuses and counts enter the report; no text
from a frame or a transcript entry does. Model-driven wording, counts or tool choice are not
compared; a label the fixture has and the run lacks is.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from capture import keys_shape  # the sibling in scripts/, which is sys.path[0] here

from viki_agent import catalog, config
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.drivers.claude.transport import CONTROL_TYPES
from viki_agent.events import history_item

Frame = dict[str, Any]
Entry = dict[str, Any]

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "claude"
CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "tests" / "contracts" / "events"

# The history path's `entry()` (mapper.py) branches on these `type` values; every other entry
# type falls through unhandled.
HANDLED_ENTRY_TYPES = frozenset({"user", "assistant", "system"})

# Scenarios whose history item type sequence is checked against a history golden's own `events`.
HISTORY_GOLDENS: dict[str, str] = {
    "06": "history-subagent-explore.json",
    "28": "history-background-agent.json",
}

# Scenario key -> the fixture stems it is judged against (tests/fixtures/claude/<stem>.jsonl).
# "12", "13" and "29" to "33" have none: they are judged by hand, not against a fixture.
FIXTURES: dict[str, tuple[str, ...]] = {
    "01": ("text-two-turns",),
    "06": ("subagent-explore",),
    "07": ("ask-user-question",),
    "08": ("webfetch-denied",),
    "09": ("interrupt-mid-stream",),
    "10": ("interrupt-before-start",),
    "11": ("resume", "fork"),
    "12": (),
    "13": (),
    "22": ("thinking",),
    "26": ("plan-mode-exit",),
    "27": ("subagent-permission",),
    "28": ("background-agent",),
    "29": (),
    "30": (),
    "31": (),
    "32": (),
    "33": (),
}


def label(frame: Frame) -> str:
    """A content-free label: what kind of frame this is, never what it says."""
    kind = frame.get("type", "")
    if kind == "stream_event":
        event = frame.get("event") or {}
        out = f"stream_event/{event.get('type', '')}"
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type"):
            out += f"/{delta['type']}"
        return out
    if kind in ("assistant", "user"):
        content = (frame.get("message") or {}).get("content") or []
        block_types = sorted({b.get("type", "") for b in content if isinstance(b, dict)})
        return f"{kind}:" + "+".join(block_types)
    if kind == "result":
        return f"result/{frame.get('subtype') or ''}/{frame.get('terminal_reason') or ''}"
    return f"{kind}/{frame.get('subtype') or ''}"


def _load_lines(path: Path) -> tuple[list[Entry], int]:
    """The parsed entries and how many lines were skipped: a line that fails to parse, or is
    JSON but not an object, costs that line and not the report, as in `_unhandled_entry_types`."""
    entries: list[Entry] = []
    skipped = 0
    with path.open("rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace")
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError, RecursionError:
                entry = None
            if not isinstance(entry, dict):
                skipped += 1
                continue
            entries.append(entry)
    return entries, skipped


def _out_entries(lines: list[Entry]) -> list[Entry]:
    return [
        entry
        for entry in lines
        if entry.get("dir") == "out" and entry.get("frame", {}).get("type") not in CONTROL_TYPES
    ]


def _out_frames(lines: list[Entry]) -> list[Frame]:
    return [entry["frame"] for entry in _out_entries(lines)]


def _split_hooks(frames: list[Frame]) -> tuple[list[Frame], dict[str, int]]:
    """A `system/hook_*` frame is set apart, counted per label, from the compared frames: the
    scratch folder has no project hooks, but the user-level ones may still fire."""
    kept: list[Frame] = []
    hooks: dict[str, int] = {}
    for frame in frames:
        frame_label = label(frame)
        if frame_label.startswith("system/hook_"):
            hooks[frame_label] = hooks.get(frame_label, 0) + 1
        else:
            kept.append(frame)
    return kept, hooks


def _first_by_label(frames: list[Frame]) -> dict[str, Frame]:
    first: dict[str, Frame] = {}
    for frame in frames:
        first.setdefault(label(frame), frame)
    return first


def _join(path: str, key: str) -> str:
    return f"{path}/{key}" if path else key


def _shape_diff(fixture_shape: Any, run_shape: Any, path: str = "") -> dict[str, list[str]]:
    """Dict keys added and removed between two `keys_shape` trees, by path."""
    added: list[str] = []
    removed: list[str] = []
    if isinstance(fixture_shape, dict) and isinstance(run_shape, dict):
        added += [_join(path, k) for k in run_shape if k not in fixture_shape]
        removed += [_join(path, k) for k in fixture_shape if k not in run_shape]
        for key in fixture_shape:
            if key in run_shape:
                sub = _shape_diff(fixture_shape[key], run_shape[key], _join(path, key))
                added += sub["added"]
                removed += sub["removed"]
    return {"added": added, "removed": removed}


def _initialize_reply(lines: list[Entry]) -> dict[str, Any] | None:
    """The initialize control_request's control_response, the payload one level in."""
    for entry in lines:
        if entry.get("dir") != "out":
            continue
        frame = entry.get("frame") or {}
        if frame.get("type") != "control_response":
            continue
        response = frame.get("response")
        if isinstance(response, dict) and isinstance(response.get("response"), dict):
            return response["response"]
    return None


def _initialize_diff(run_lines: list[Entry], fixture_lines: list[Entry]) -> dict[str, Any]:
    run_init = _initialize_reply(run_lines)
    fixture_init = _initialize_reply(fixture_lines)
    if run_init is None or fixture_init is None:
        return {"run_found": run_init is not None, "fixture_found": fixture_init is not None}
    return {
        "keys": _shape_diff(fixture_init, run_init),
        "models": {
            "run": sorted({m.get("value") for m in run_init.get("models", []) if m.get("value")}),
            "fixture": sorted(
                {m.get("value") for m in fixture_init.get("models", []) if m.get("value")}
            ),
        },
        "commands_count": {
            "run": len(run_init.get("commands", [])),
            "fixture": len(fixture_init.get("commands", [])),
        },
    }


class _Clock:
    """A mutable clock, set from each frame's `t_ms` before it is fed to the mapper, exactly as
    `test_golden` (tests/acceptance/test_mapper_accept.py) replays a fixture."""

    now = 0.0

    def __call__(self) -> float:
        return self.now


def _mapper_replay(entries: list[Entry], vault: str) -> dict[str, Any]:
    """Feed `entries`' frames through a fresh Mapper, as the acceptance golden replay does.
    Never raises: an exception mid-replay is reported by its type name and stops that replay."""
    clock = _Clock()
    mapper = Mapper(vault=vault, mode="auto", clock=clock)
    errors: list[str] = []
    turn_done: list[str] = []
    for entry in entries:
        clock.now = entry.get("t_ms", 0) / 1000
        try:
            events = mapper.frame(entry["frame"])
        except Exception as exc:  # noqa: BLE001 - reported by name, never raised
            return {"exception": type(exc).__name__, "errors": errors, "turn_done": turn_done}
        for event in events:
            item = history_item(event)
            if item["type"] == "error":
                errors.append(item["data"]["code"])
            elif item["type"] == "turn.done":
                turn_done.append(item["data"]["status"])
    return {"exception": None, "errors": errors, "turn_done": turn_done}


def _mapper_diff(run_lines: list[Entry], fixture_lines: list[Entry], vault: str) -> dict[str, Any]:
    run_result = _mapper_replay(_out_entries(run_lines), vault)
    fixture_result = _mapper_replay(_out_entries(fixture_lines), "%VAULT%")
    diff: dict[str, Any] = {}
    if run_result["exception"]:
        diff["run_exception"] = run_result["exception"]
    if fixture_result["exception"]:
        diff["fixture_exception"] = fixture_result["exception"]
    if run_result["errors"]:
        diff["run_errors"] = run_result["errors"]
    if fixture_result["errors"]:
        diff["fixture_errors"] = fixture_result["errors"]
    if run_result["turn_done"] != fixture_result["turn_done"]:
        diff["turn_done"] = {"run": run_result["turn_done"], "fixture": fixture_result["turn_done"]}
    return diff


def _unhandled_entry_types(path: Path) -> tuple[set[str], bool, int]:
    """The raw transcript's entry `type` values `Mapper.entry` does not handle, whether any entry
    is an `ai-title` one, and how many lines did not parse to an object (skipped, as
    `read_transcript` skips them). A missing transcript reports none of these."""
    if not path.exists():
        return set(), False, 0
    unhandled: set[str] = set()
    ai_title = False
    skipped = 0
    with path.open("rb") as fh:
        for raw in fh:
            # Decoded per line, as catalog.py reads: a tear inside a multi-byte character, or a
            # line that is JSON but not an object, costs that line and not the report.
            line = raw.decode("utf-8", errors="replace")
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError, RecursionError:
                entry = None
            if not isinstance(entry, dict):
                skipped += 1
                continue
            kind = entry.get("type")
            if kind == "ai-title":
                ai_title = True
            if kind not in HANDLED_ENTRY_TYPES:
                unhandled.add(str(kind))
    return unhandled, ai_title, skipped


def _golden_history_types(key: str) -> list[str] | None:
    name = HISTORY_GOLDENS.get(key)
    if name is None:
        return None
    data = json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))
    return [event["type"] for event in data["events"]]


def _history_report(
    catalog_obj: catalog.Catalog, session_id: str, project_dir: Path, golden_types: list[str] | None
) -> dict[str, Any]:
    page = catalog_obj.history(session_id)
    unhandled, ai_title, skipped = _unhandled_entry_types(project_dir / f"{session_id}.jsonl")
    entry: dict[str, Any] = {
        "session_id": session_id,
        "status": page.status,
        "unhandled_entry_types": sorted(unhandled),
        "ai_title": ai_title,
        "skipped_lines": skipped,
    }
    if golden_types is not None:
        entry["history_types"] = {
            "run": [history_item(event)["type"] for event in page.events],
            "golden": golden_types,
        }
    return entry


def _scenario_report(
    key: str,
    fixture_names: tuple[str, ...],
    run_dir: Path,
    results: dict[str, Any],
    catalog_obj: catalog.Catalog | None,
    project_dir: Path | None,
    scratch_root: str,
) -> dict[str, Any]:
    result = results.get(key, {})
    entry: dict[str, Any] = {"pass": result.get("pass"), "error": result.get("error")}
    if catalog_obj is not None and project_dir is not None:
        golden_types = _golden_history_types(key)
        entry["history"] = [
            _history_report(catalog_obj, sid, project_dir, golden_types)
            for sid in result.get("session_ids", [])
        ]
    if not fixture_names:
        entry["baseline"] = "M0b"
        run_path = run_dir / f"{key}.jsonl"
        if run_path.exists():
            run_lines, skipped_lines = _load_lines(run_path)
            if skipped_lines:
                entry["skipped_lines"] = skipped_lines
            entry["mapper"] = _mapper_replay(_out_entries(run_lines), scratch_root or "%VAULT%")
        return entry
    run_path = run_dir / f"{key}.jsonl"
    if not run_path.exists():
        entry["missing"] = True
        return entry
    run_lines, skipped_lines = _load_lines(run_path)
    if skipped_lines:
        entry["skipped_lines"] = skipped_lines
    run_frames, run_hooks = _split_hooks(_out_frames(run_lines))

    fixture_lines: list[Entry] = []
    fixture_frames: list[Frame] = []
    fixture_hooks: dict[str, int] = {}
    for name in fixture_names:
        lines, _fixture_skipped = _load_lines(FIXTURES_DIR / f"{name}.jsonl")
        fixture_lines += lines
        frames, hooks = _split_hooks(_out_frames(lines))
        fixture_frames += frames
        for hook_label, count in hooks.items():
            fixture_hooks[hook_label] = fixture_hooks.get(hook_label, 0) + count

    run_first = _first_by_label(run_frames)
    fixture_first = _first_by_label(fixture_frames)
    shared = sorted(set(run_first) & set(fixture_first))
    keys_diff: dict[str, dict[str, list[str]]] = {}
    for shared_label in shared:
        diff = _shape_diff(
            keys_shape(fixture_first[shared_label]), keys_shape(run_first[shared_label])
        )
        if diff["added"] or diff["removed"]:
            keys_diff[shared_label] = diff

    entry["added"] = sorted(set(run_first) - set(fixture_first))
    entry["removed"] = sorted(set(fixture_first) - set(run_first))
    entry["keys_diff"] = keys_diff
    entry["hooks"] = {"run": run_hooks, "fixture": fixture_hooks}
    if key == "01":
        entry["initialize"] = _initialize_diff(run_lines, fixture_lines)
    entry["mapper"] = _mapper_diff(run_lines, fixture_lines, scratch_root or "%VAULT%")
    return entry


def compare(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    results: dict[str, Any] = {}
    scratch_root = ""
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        results = summary.get("results", {})
        scratch_root = summary.get("scratch_root", "")
    catalog_obj: catalog.Catalog | None = None
    project_dir: Path | None = None
    if scratch_root:
        cfg = config.load()
        root = Path(scratch_root)
        catalog_obj = catalog.Catalog(cfg.claude_home, root)
        project_dir = catalog.project_dir(cfg.claude_home, root)
    return {
        key: _scenario_report(
            key, fixture_names, run_dir, results, catalog_obj, project_dir, scratch_root
        )
        for key, fixture_names in FIXTURES.items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: compare_capture.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    report = compare(run_dir)
    out_path = run_dir / "compare.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
