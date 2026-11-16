"""Reading a Linux suite's copied ``/out`` and comparing the head's result with the base's.

Builds fixture ``/out`` folders by hand (no Docker, no real pytest/vitest run), then checks the
ids ``suite_failures`` derives from them and the verdict ``compare`` derives from two
``Failures``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from linux_results import (  # noqa: E402
    CACHE_VERSION,
    Failures,
    ResultsError,
    Verdict,
    compare,
    did_not_run,
    from_json,
    is_timeout_id,
    named_no_test,
    no_evidence,
    suite_failures,
    timeout_id,
    to_json,
)


def _write_steps(out: Path, *records: dict[str, object]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records]
    (out / "steps.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _step(name: str, exit_code: int, seconds: int = 1) -> dict[str, object]:
    return {"step": name, "exit": exit_code, "seconds": seconds}


SUITE_STEPS = {
    "check": ("setup", "python", "lint", "typecheck", "unit", "build"),
    "e2e": ("setup", "e2e"),
    "desktop": ("setup", "lint", "test", "smoke", "e2e"),
    "package": ("setup", "dist", "packaged-test"),
    "compile": ("setup", "compile", "worker-smoke", "app-smoke"),
    "oldgit": ("setup", "python"),
}


def _write_suite(out: Path, suite: str, *records: dict[str, object]) -> None:
    overrides = {str(record["step"]): record for record in records}
    complete = []
    for name in SUITE_STEPS[suite]:
        complete.append(overrides.get(name, _step(name, 0)))
        if rerun := overrides.get(f"{name}-rerun"):
            complete.append(rerun)
    _write_steps(out, *complete)


@pytest.mark.parametrize("suite", SUITE_STEPS)
def test_every_suite_requires_all_its_steps(tmp_path: Path, suite: str) -> None:
    records = [_step(name, 0) for name in SUITE_STEPS[suite]]
    _write_steps(tmp_path, *records)
    assert suite_failures(suite, tmp_path) == Failures(frozenset(), frozenset())
    for missing in range(len(records)):
        _write_steps(tmp_path, *records[:missing], *records[missing + 1 :])
        with pytest.raises(ResultsError):
            suite_failures(suite, tmp_path)


@pytest.mark.parametrize("records", [(), (_step("setup", 0),)])
def test_empty_or_setup_only_success_is_not_a_pass(
    tmp_path: Path, records: tuple[dict[str, object], ...]
) -> None:
    _write_steps(tmp_path, *records)
    with pytest.raises(ResultsError):
        suite_failures("check", tmp_path)


@pytest.mark.parametrize("suite", SUITE_STEPS)
def test_failed_setup_needs_no_later_steps(tmp_path: Path, suite: str) -> None:
    _write_steps(tmp_path, _step("setup", 1))
    failed = frozenset({f"{suite} setup"})
    assert suite_failures(suite, tmp_path) == Failures(failed, failed)


@pytest.mark.parametrize("exit_code", [None, False, True, "0", 0.0, -1, 256])
def test_invalid_shell_exit_code_is_not_a_step(tmp_path: Path, exit_code: object) -> None:
    records = [_step(name, 0) for name in SUITE_STEPS["check"]]
    records[1]["exit"] = exit_code
    _write_steps(tmp_path, *records)
    with pytest.raises(ResultsError):
        suite_failures("check", tmp_path)


@pytest.mark.parametrize(
    "names",
    [
        ("setup", "e2e", "e2e"),
        ("setup", "e2e", "unknown"),
        ("e2e", "setup"),
        ("setup", "e2e-rerun", "e2e"),
        ("setup", "e2e", "python-rerun"),
    ],
)
def test_duplicate_unexpected_or_out_of_order_steps_are_rejected(
    tmp_path: Path, names: tuple[str, ...]
) -> None:
    _write_steps(tmp_path, *(_step(name, 0) for name in names))
    with pytest.raises(ResultsError):
        suite_failures("e2e", tmp_path)


def test_failed_setup_cannot_be_followed_by_more_steps(tmp_path: Path) -> None:
    _write_steps(tmp_path, _step("setup", 1), _step("e2e", 0))
    with pytest.raises(ResultsError):
        suite_failures("e2e", tmp_path)


def test_from_json_rejects_results_before_step_completeness_validation() -> None:
    with pytest.raises(ResultsError):
        from_json({"version": 2, "first": [], "still": []})


def test_from_json_rejects_results_that_could_hold_a_base_that_never_ran() -> None:
    """Version 3 files were written for a base whose setup failed or that timed out too."""
    with pytest.raises(ResultsError):
        from_json({"version": 3, "first": ["package setup"], "still": ["package setup"]})


def test_from_json_rejects_results_that_could_hold_a_base_step_that_named_no_test() -> None:
    """Version 4 files were written for a base whose pytest stage died without a result too."""
    marker = "check python: failed with no test listed"
    with pytest.raises(ResultsError):
        from_json({"version": 4, "first": [marker], "still": [marker]})


def _vitest_report(*files: tuple[str, list[tuple[str, str]]]) -> dict[str, object]:
    """A vitest-json-shaped report: each file is ``(name, [(fullName, status), ...])``."""
    test_results = []
    for name, assertions in files:
        failed = any(status == "failed" for _, status in assertions)
        test_results.append(
            {
                "name": name,
                "status": "failed" if failed else "passed",
                "assertionResults": [
                    {"fullName": full_name, "status": status} for full_name, status in assertions
                ],
            }
        )
    return {"testResults": test_results}


# -- generic steps: no sub-breakdown, the step itself is the id ----------------------------------


def test_generic_step_failure_is_its_own_id_in_first_and_still(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "desktop", _step("setup", 0), _step("lint", 1))
    result = suite_failures("desktop", out)
    assert result == Failures(first=frozenset({"desktop lint"}), still=frozenset({"desktop lint"}))


def test_passing_steps_contribute_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "package", _step("setup", 0), _step("dist", 0), _step("packaged-test", 0))
    result = suite_failures("package", out)
    assert result == Failures(first=frozenset(), still=frozenset())


def test_rerun_step_exit_code_is_ignored(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("setup", 0), _step("python-rerun", 1))
    result = suite_failures("check", out)
    assert result == Failures(first=frozenset(), still=frozenset())


# -- steps.jsonl itself missing or unreadable: always a hard error -------------------------------


def test_missing_steps_jsonl_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ResultsError):
        suite_failures("check", out)


def test_malformed_steps_jsonl_line_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "steps.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(ResultsError):
        suite_failures("check", out)


def test_steps_jsonl_line_missing_exit_key_raises(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "steps.jsonl").write_text(json.dumps({"step": "lint"}) + "\n", encoding="utf-8")
    with pytest.raises(ResultsError):
        suite_failures("check", out)


def test_steps_jsonl_with_an_undecodable_byte_raises(tmp_path: Path) -> None:
    """A byte no UTF-8 decoder accepts must fail closed (ResultsError) like any other
    unreadable steps.jsonl, not crash with an uncaught UnicodeDecodeError."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "steps.jsonl").write_bytes(b'{"step": "lint", "exit": 1}\n\xff\xfe\n')
    with pytest.raises(ResultsError):
        suite_failures("check", out)


# -- the "python" step: check.py's own FAILED line, expanded for its pytest label ----------------


def test_python_step_non_pytest_label(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text("some output\nFAILED: ruff check\n", encoding="utf-8")
    result = suite_failures("check", out)
    expected = frozenset({"check python: ruff check"})
    assert result == Failures(first=expected, still=expected)


def test_python_step_pytest_label_expands_node_ids_no_rerun_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_text(
        json.dumps({"tests/test_a.py::test_x": True, "tests/test_b.py::test_y": True}),
        encoding="utf-8",
    )
    result = suite_failures("check", out)
    expected = frozenset({"check tests/test_a.py::test_x", "check tests/test_b.py::test_y"})
    # no pytest-still.json: no rerun evidence, so every first id stays conservatively in still
    assert result == Failures(first=expected, still=expected)


def test_python_step_pytest_still_drops_a_fixed_node_id(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1), _step("python-rerun", 0))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_text(
        json.dumps({"tests/test_a.py::test_x": True, "tests/test_b.py::test_y": True}),
        encoding="utf-8",
    )
    (out / "pytest-still.json").write_text(
        json.dumps({"tests/test_b.py::test_y": True}), encoding="utf-8"
    )
    result = suite_failures("check", out)
    assert result.first == frozenset(
        {"check tests/test_a.py::test_x", "check tests/test_b.py::test_y"}
    )
    assert result.still == frozenset({"check tests/test_b.py::test_y"})


def test_python_step_pytest_still_empty_means_all_flaky(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1), _step("python-rerun", 0))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_text(
        json.dumps({"tests/test_a.py::test_x": True}), encoding="utf-8"
    )
    (out / "pytest-still.json").write_text("{}", encoding="utf-8")
    result = suite_failures("check", out)
    assert result.first == frozenset({"check tests/test_a.py::test_x"})
    assert result.still == frozenset()


def test_python_log_missing_is_fail_closed_marker(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    result = suite_failures("check", out)
    marker = frozenset({"check python: no result line"})
    assert result == Failures(first=marker, still=marker)


def test_python_log_without_a_failed_line_is_fail_closed_marker(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text("some crash before any result line\n", encoding="utf-8")
    result = suite_failures("check", out)
    marker = frozenset({"check python: no result line"})
    assert result == Failures(first=marker, still=marker)


def test_python_log_with_an_undecodable_byte_is_fail_closed_marker(tmp_path: Path) -> None:
    """A byte no UTF-8 decoder accepts must fail closed like a missing python.log, not crash
    with an uncaught UnicodeDecodeError."""
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_bytes(b"FAILED: ruff check\n\xff\xfe\n")
    result = suite_failures("check", out)
    marker = frozenset({"check python: no result line"})
    assert result == Failures(first=marker, still=marker)


def test_python_pytest_label_with_empty_report_is_fail_closed_not_a_pass(tmp_path: Path) -> None:
    """``pytest-first.json == {}`` must not be read as nothing failed, even though check.py's
    own FAILED line says the pytest stage failed."""
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_text("{}", encoding="utf-8")
    result = suite_failures("check", out)
    marker = frozenset({"check python: failed with no test listed"})
    assert result == Failures(first=marker, still=marker)


def test_python_pytest_label_with_missing_report_is_fail_closed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    result = suite_failures("check", out)
    marker = frozenset({"check python: failed with no test listed"})
    assert result == Failures(first=marker, still=marker)


def test_pytest_first_json_with_an_undecodable_byte_is_fail_closed(tmp_path: Path) -> None:
    """A byte no UTF-8 decoder accepts must fail closed like a missing
    pytest-first.json, not crash with an uncaught UnicodeDecodeError."""
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text(
        "FAILED: pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_bytes(b"\xff\xfe not valid utf-8")
    result = suite_failures("check", out)
    marker = frozenset({"check python: failed with no test listed"})
    assert result == Failures(first=marker, still=marker)


def test_python_step_mixes_pytest_and_plain_labels(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("python", 1))
    (out / "python.log").write_text(
        "FAILED: ruff check, pytest -n auto --maxprocesses 12 --dist load\n", encoding="utf-8"
    )
    (out / "pytest-first.json").write_text(
        json.dumps({"tests/test_a.py::test_x": True}), encoding="utf-8"
    )
    result = suite_failures("check", out)
    expected = frozenset({"check python: ruff check", "check tests/test_a.py::test_x"})
    assert result == Failures(first=expected, still=expected)


# -- a non-"check" suite's "python" step (the "oldgit" suite) gets its own prefix, not "check" ---


def test_python_step_pytest_label_uses_the_suites_own_prefix(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "oldgit", _step("python", 1))
    (out / "python.log").write_text("FAILED: pytest tests/workspace_code\n", encoding="utf-8")
    (out / "pytest-first.json").write_text(
        json.dumps({"tests/workspace_code/test_code_range.py::test_x": True}), encoding="utf-8"
    )
    result = suite_failures("oldgit", out)
    expected = frozenset({"oldgit tests/workspace_code/test_code_range.py::test_x"})
    assert result == Failures(first=expected, still=expected)


def test_python_log_missing_uses_the_suites_own_prefix(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "oldgit", _step("python", 1))
    result = suite_failures("oldgit", out)
    marker = frozenset({"oldgit python: no result line"})
    assert result == Failures(first=marker, still=marker)


# -- vitest-backed steps: check's "unit", e2e's "e2e" ---------------------------------------------


def test_unit_step_assertion_ids_relative_to_tmp_work(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1))
    report = _vitest_report(
        ("/tmp/work/frontend/src/foo.test.ts", [("does x", "failed"), ("does y", "passed")])
    )
    (out / "unit.json").write_text(json.dumps(report), encoding="utf-8")
    result = suite_failures("check", out)
    expected = frozenset({"check frontend/src/foo.test.ts > does x"})
    # no unit-rerun.json: no evidence of a pass, so it stays in still
    assert result == Failures(first=expected, still=expected)


def test_unit_step_rerun_drops_an_assertion_that_now_passes(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1), _step("unit-rerun", 0))
    first_report = _vitest_report(
        ("/tmp/work/frontend/src/foo.test.ts", [("does x", "failed"), ("does y", "failed")])
    )
    rerun_report = _vitest_report(
        ("/tmp/work/frontend/src/foo.test.ts", [("does x", "passed"), ("does y", "failed")])
    )
    (out / "unit.json").write_text(json.dumps(first_report), encoding="utf-8")
    (out / "unit-rerun.json").write_text(json.dumps(rerun_report), encoding="utf-8")
    result = suite_failures("check", out)
    assert result.first == frozenset(
        {"check frontend/src/foo.test.ts > does x", "check frontend/src/foo.test.ts > does y"}
    )
    assert result.still == frozenset({"check frontend/src/foo.test.ts > does y"})


def test_unit_step_rerun_missing_keeps_everything_in_still(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1), _step("unit-rerun", 0))
    report = _vitest_report(("/tmp/work/frontend/src/foo.test.ts", [("does x", "failed")]))
    (out / "unit.json").write_text(json.dumps(report), encoding="utf-8")
    result = suite_failures("check", out)
    expected = frozenset({"check frontend/src/foo.test.ts > does x"})
    assert result == Failures(first=expected, still=expected)


def test_unit_step_failed_file_with_no_failed_assertion_gives_the_file_id(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1))
    report = {
        "testResults": [
            {
                "name": "/tmp/work/frontend/src/foo.test.ts",
                "status": "failed",
                "assertionResults": [{"fullName": "does x", "status": "passed"}],
            }
        ]
    }
    (out / "unit.json").write_text(json.dumps(report), encoding="utf-8")
    result = suite_failures("check", out)
    expected = frozenset({"check frontend/src/foo.test.ts"})
    assert result == Failures(first=expected, still=expected)


def test_e2e_step_uses_the_e2e_report_names(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "e2e", _step("e2e", 1))
    report = _vitest_report(("/tmp/work/frontend/e2e/smoke.spec.ts", [("loads", "failed")]))
    (out / "e2e.json").write_text(json.dumps(report), encoding="utf-8")
    result = suite_failures("e2e", out)
    expected = frozenset({"e2e frontend/e2e/smoke.spec.ts > loads"})
    assert result == Failures(first=expected, still=expected)


def test_desktop_e2e_step_is_generic_not_vitest(tmp_path: Path) -> None:
    """Only check's "unit" step and e2e's "e2e" step read vitest json; desktop's own "e2e"
    step is a plain npm script with no reporter, so it is a generic id."""
    out = tmp_path / "out"
    _write_suite(out, "desktop", _step("setup", 0), _step("e2e", 1))
    result = suite_failures("desktop", out)
    expected = frozenset({"desktop e2e"})
    assert result == Failures(first=expected, still=expected)


def test_unit_json_missing_is_fail_closed_marker(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1))
    result = suite_failures("check", out)
    marker = frozenset({"check unit: failed with no test listed"})
    assert result == Failures(first=marker, still=marker)


def test_unit_json_with_no_failures_listed_is_fail_closed_not_a_pass(tmp_path: Path) -> None:
    """``{"testResults": []}`` must not be read as nothing failed, even though the "unit"
    step's own exit code says it failed."""
    out = tmp_path / "out"
    _write_suite(out, "check", _step("unit", 1))
    (out / "unit.json").write_text(json.dumps({"testResults": []}), encoding="utf-8")
    result = suite_failures("check", out)
    marker = frozenset({"check unit: failed with no test listed"})
    assert result == Failures(first=marker, still=marker)


# -- a vitest rerun replays whole files, so it can fail an id (Y) the first run passed -------


def test_vitest_rerun_only_failure_joins_still_though_absent_from_first(tmp_path: Path) -> None:
    """``e2e.json`` fails X and passes Y in the same file; ``e2e-rerun.json`` fails both. `first`
    must stay just {X} (Y never failed on the first run), but `still` must gain Y even though Y
    is absent from `first`."""
    out = tmp_path / "out"
    _write_suite(out, "e2e", _step("e2e", 1), _step("e2e-rerun", 0))
    first_report = _vitest_report(
        ("/tmp/work/frontend/e2e/smoke.spec.ts", [("x", "failed"), ("y", "passed")])
    )
    rerun_report = _vitest_report(
        ("/tmp/work/frontend/e2e/smoke.spec.ts", [("x", "failed"), ("y", "failed")])
    )
    (out / "e2e.json").write_text(json.dumps(first_report), encoding="utf-8")
    (out / "e2e-rerun.json").write_text(json.dumps(rerun_report), encoding="utf-8")
    result = suite_failures("e2e", out)
    x_id = "e2e frontend/e2e/smoke.spec.ts > x"
    y_id = "e2e frontend/e2e/smoke.spec.ts > y"
    assert result.first == frozenset({x_id})
    assert result.still >= frozenset({x_id, y_id})


def test_rerun_only_failure_the_base_also_rerun_fails_is_known_not_new(tmp_path: Path) -> None:
    """The real-run case, end to end through `suite_failures` and `compare`: the base (main)
    passed an id (Y) on its first run and failed it only on its rerun; the head fails Y on both
    of its runs. `compare` must call Y known, since the base shares it via `still`, never new."""
    base_out = tmp_path / "base-e2e"
    _write_suite(base_out, "e2e", _step("e2e", 1), _step("e2e-rerun", 0))
    base_first = _vitest_report(
        ("/tmp/work/frontend/e2e/smoke.spec.ts", [("z", "failed"), ("y", "passed")])
    )
    base_rerun = _vitest_report(
        ("/tmp/work/frontend/e2e/smoke.spec.ts", [("z", "passed"), ("y", "failed")])
    )
    (base_out / "e2e.json").write_text(json.dumps(base_first), encoding="utf-8")
    (base_out / "e2e-rerun.json").write_text(json.dumps(base_rerun), encoding="utf-8")
    base = suite_failures("e2e", base_out)

    head_out = tmp_path / "head-e2e"
    _write_suite(head_out, "e2e", _step("e2e", 1), _step("e2e-rerun", 0))
    head_first = _vitest_report(("/tmp/work/frontend/e2e/smoke.spec.ts", [("y", "failed")]))
    head_rerun = _vitest_report(("/tmp/work/frontend/e2e/smoke.spec.ts", [("y", "failed")]))
    (head_out / "e2e.json").write_text(json.dumps(head_first), encoding="utf-8")
    (head_out / "e2e-rerun.json").write_text(json.dumps(head_rerun), encoding="utf-8")
    head = suite_failures("e2e", head_out)

    verdict = compare(head, base)
    y_id = "e2e frontend/e2e/smoke.spec.ts > y"
    assert y_id in verdict.known
    assert y_id not in verdict.new


# -- compare: the verdict rule, with and without a base -------------------------------------------


def test_compare_with_no_base_is_new_and_flaky_from_head_alone() -> None:
    head = Failures(first=frozenset({"a", "b"}), still=frozenset({"a"}))
    verdict = compare(head, None)
    assert verdict == Verdict(new=frozenset({"a"}), known=frozenset(), flaky=frozenset({"b"}))


def test_compare_known_excludes_a_shared_id_from_new(tmp_path: Path) -> None:
    """`compare` must use `base`: ignoring it would report `b` as new, and would also report
    `a` as new instead of known, since it never subtracts `base.first`."""
    head = Failures(first=frozenset({"a", "b"}), still=frozenset({"a", "b"}))
    base = Failures(first=frozenset({"a"}), still=frozenset({"a"}))
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset({"b"}), known=frozenset({"a"}), flaky=frozenset())


def test_compare_flaky_excludes_ids_the_base_also_fails(tmp_path: Path) -> None:
    head = Failures(first=frozenset({"a", "b", "c"}), still=frozenset({"a"}))
    base = Failures(first=frozenset({"c"}), still=frozenset({"c"}))
    verdict = compare(head, base)
    # a: still failing on head, and base does not share it -> new.
    # b: in head.first, not in head.still, not in base.first -> flaky.
    # c: in head.first, not in head.still, but IS in base.first -> known, not flaky, not new.
    assert verdict == Verdict(new=frozenset({"a"}), known=frozenset({"c"}), flaky=frozenset({"b"}))


def test_compare_new_is_a_still_id_the_base_does_not_share(tmp_path: Path) -> None:
    head = Failures(first=frozenset({"a"}), still=frozenset({"a"}))
    base = Failures(first=frozenset(), still=frozenset())
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset({"a"}), known=frozenset(), flaky=frozenset())


def test_compare_known_includes_an_id_the_base_only_fails_on_rerun() -> None:
    """The base's failure set is `base.first | base.still`, not `base.first` alone: an id main
    passed first and failed only when rerun still means main shares the failure, so it must be
    known, not new."""
    head = Failures(first=frozenset({"x"}), still=frozenset({"x"}))
    base = Failures(first=frozenset(), still=frozenset({"x"}))
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset(), known=frozenset({"x"}), flaky=frozenset())


# -- an id failing on only one of the head's two runs is never new, only known or flaky -----


def test_head_rerun_only_failure_with_no_base_is_flaky_not_new() -> None:
    """Y failed only on the head's rerun (absent from `first`): with no base to share it, it is
    flaky, never new."""
    head = Failures(first=frozenset(), still=frozenset({"check nodeY"}))
    verdict = compare(head, None)
    assert verdict == Verdict(new=frozenset(), known=frozenset(), flaky=frozenset({"check nodeY"}))


def test_head_rerun_only_failure_the_base_does_not_share_is_flaky_not_new() -> None:
    """Y failed only on the head's rerun; the base fails nodeX (confirmed on both sides) but never
    fails Y at all, so Y is flaky, never new."""
    head = Failures(
        first=frozenset({"check nodeX"}), still=frozenset({"check nodeX", "check nodeY"})
    )
    base = Failures(first=frozenset({"check nodeX"}), still=frozenset({"check nodeX"}))
    verdict = compare(head, base)
    assert verdict == Verdict(
        new=frozenset(), known=frozenset({"check nodeX"}), flaky=frozenset({"check nodeY"})
    )


def test_confirmed_is_first_and_still() -> None:
    failures = Failures(first=frozenset({"a", "b"}), still=frozenset({"b", "c"}))
    assert failures.confirmed == frozenset({"b"})


# -- timeout markers: always new while still failing, never known or flaky ------------------


def test_timeout_id_has_the_c1_wording() -> None:
    assert timeout_id("check", 90) == "check: timed out after 90 min"


def test_is_timeout_id_recognizes_the_marker_and_rejects_ordinary_ids() -> None:
    assert is_timeout_id(timeout_id("desktop", 90)) is True
    assert is_timeout_id("desktop lint") is False
    assert is_timeout_id("check tests/test_a.py::test_x") is False


def test_compare_counts_a_timeout_shared_by_both_sides_as_new_not_known() -> None:
    """A suite that times out on both sides shares one marker id, which must never be read as
    known just because both sides produced the identical string - a base run can never tell
    us the head would have passed given more time."""
    marker = timeout_id("check", 90)
    head = Failures(first=frozenset({marker}), still=frozenset({marker}))
    base = Failures(first=frozenset({marker}), still=frozenset({marker}))
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset({marker}), known=frozenset(), flaky=frozenset())


def test_compare_never_puts_a_timeout_id_in_flaky() -> None:
    """Defensive: even a timeout id present only in `first` (not `still`, which never happens via
    the real _side_result but compare must not rely on that) must not surface as flaky - there
    is no rerun-cleared-it evidence for a marker a rerun never produces."""
    marker = timeout_id("check", 90)
    head = Failures(first=frozenset({marker}), still=frozenset())
    verdict = compare(head, None)
    assert verdict == Verdict(new=frozenset(), known=frozenset(), flaky=frozenset())


def test_compare_timeout_mixed_with_a_real_failure_still_compares_the_real_one() -> None:
    """A mix of a timeout and an ordinary id: the timeout always counts as new, and the ordinary
    id is still compared against the base normally."""
    marker = timeout_id("check", 90)
    head = Failures(
        first=frozenset({marker, "check nodeX"}), still=frozenset({marker, "check nodeX"})
    )
    base = Failures(first=frozenset({"check nodeX"}), still=frozenset({"check nodeX"}))
    verdict = compare(head, base)
    assert verdict == Verdict(
        new=frozenset({marker}), known=frozenset({"check nodeX"}), flaky=frozenset()
    )


# -- a failed setup: the suite never ran, so it is new like a timeout, never known ----------


@pytest.mark.parametrize("suite", SUITE_STEPS)
def test_a_failed_setup_is_the_one_id_of_a_suite_that_did_not_run(
    tmp_path: Path, suite: str
) -> None:
    """Tied to the id `suite_failures` really builds, so the two cannot drift apart."""
    _write_steps(tmp_path, _step("setup", 1))
    (setup_id,) = suite_failures(suite, tmp_path).confirmed
    assert did_not_run(setup_id) is True


def test_did_not_run_covers_timeouts_and_rejects_ids_of_steps_that_ran() -> None:
    assert did_not_run(timeout_id("package", 90)) is True
    assert did_not_run("package dist") is False
    assert did_not_run("check python: no result line") is False
    assert did_not_run("check tests/test_setup.py::test_setup") is False
    assert did_not_run("nosuchsuite setup") is False


@pytest.mark.parametrize("suite", SUITE_STEPS)
def test_compare_counts_a_setup_failure_shared_by_both_sides_as_new_not_known(suite: str) -> None:
    """A setup that fails on both sides shares one id, but the base's failure is no evidence
    that the head's tests, which never ran, would pass: it must not read as main's failure."""
    setup_id = f"{suite} setup"
    head = Failures(first=frozenset({setup_id}), still=frozenset({setup_id}))
    base = Failures(first=frozenset({setup_id}), still=frozenset({setup_id}))
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset({setup_id}), known=frozenset(), flaky=frozenset())


# -- a step that failed without naming a test: new like a failed setup, never known ---------


def _no_result_line(out: Path, suite: str) -> None:
    _write_suite(out, suite, _step("python", 1))


def _pytest_listed_nothing(out: Path, suite: str) -> None:
    """What a pytest stage killed at its time limit leaves: the FAILED line, no failing node."""
    _write_suite(out, suite, _step("python", 1))
    (out / "python.log").write_text("FAILED: pytest -q -n auto --dist load\n", encoding="utf-8")
    (out / "pytest-first.json").write_text("{}", encoding="utf-8")


def _vitest_listed_nothing(out: Path, suite: str) -> None:
    step = {"check": "unit", "e2e": "e2e"}[suite]
    _write_suite(out, suite, _step(step, 1))
    (out / f"{step}.json").write_text(json.dumps({"testResults": []}), encoding="utf-8")


# Every (suite, step) with a breakdown, in each way it can fail without naming a test.
NO_TEST_REPORTS = [
    ("check", _no_result_line, "check python: no result line"),
    ("oldgit", _no_result_line, "oldgit python: no result line"),
    ("check", _pytest_listed_nothing, "check python: failed with no test listed"),
    ("oldgit", _pytest_listed_nothing, "oldgit python: failed with no test listed"),
    ("check", _vitest_listed_nothing, "check unit: failed with no test listed"),
    ("e2e", _vitest_listed_nothing, "e2e e2e: failed with no test listed"),
]
NO_TEST_IDS = [marker for _, _, marker in NO_TEST_REPORTS]


@pytest.mark.parametrize(("suite", "write", "marker"), NO_TEST_REPORTS, ids=NO_TEST_IDS)
def test_a_step_that_failed_without_naming_a_test_is_no_evidence(
    tmp_path: Path, suite: str, write: Callable[[Path, str], None], marker: str
) -> None:
    """Tied to the ids `suite_failures` really builds, so the two cannot drift apart. The
    suite's other steps ran, so it is not a suite that did not run."""
    write(tmp_path, suite)
    assert suite_failures(suite, tmp_path).confirmed == frozenset({marker})
    assert named_no_test(marker) is True
    assert no_evidence(marker) is True
    assert did_not_run(marker) is False


def test_no_evidence_covers_a_suite_that_did_not_run_and_rejects_ids_that_name_a_test() -> None:
    assert no_evidence("package setup") is True
    assert no_evidence(timeout_id("package", 90)) is True
    assert named_no_test("package setup") is False
    for id_ in (
        "check tests/test_a.py::test_x",
        "check python: ruff check",
        "check lint",
        # A vitest id embeds the test's own name, which may end like a marker.
        "check frontend/src/a.unit.test.ts > says: failed with no test listed",
        "e2e frontend/e2e/smoke.spec.ts > python: no result line",
        # desktop's e2e step has no report, and no suite of that name has a python step.
        "desktop e2e: failed with no test listed",
        "desktop python: no result line",
    ):
        assert (named_no_test(id_), no_evidence(id_)) == (False, False), id_


@pytest.mark.parametrize("marker", NO_TEST_IDS)
def test_compare_counts_a_no_test_marker_shared_by_both_sides_as_new_not_known(
    marker: str,
) -> None:
    """A base whose pytest stage was killed at its time limit and a head whose collection
    crashed leave one id; the base's says nothing about why the head's step named no test."""
    head = Failures(first=frozenset({marker}), still=frozenset({marker}))
    base = Failures(first=frozenset({marker}), still=frozenset({marker}))
    verdict = compare(head, base)
    assert verdict == Verdict(new=frozenset({marker}), known=frozenset(), flaky=frozenset())


def test_compare_no_test_marker_mixed_with_a_real_failure_still_compares_the_real_one() -> None:
    marker = "check unit: failed with no test listed"
    head = Failures(
        first=frozenset({marker, "check nodeX"}), still=frozenset({marker, "check nodeX"})
    )
    base = Failures(
        first=frozenset({marker, "check nodeX"}), still=frozenset({marker, "check nodeX"})
    )
    verdict = compare(head, base)
    assert verdict == Verdict(
        new=frozenset({marker}), known=frozenset({"check nodeX"}), flaky=frozenset()
    )


# -- to_json / from_json ---------------------------------------------------------------------------


def test_to_json_sorts_and_versions() -> None:
    failures = Failures(first=frozenset({"b", "a"}), still=frozenset({"a"}))
    assert to_json(failures) == {"version": CACHE_VERSION, "first": ["a", "b"], "still": ["a"]}


def test_from_json_round_trips() -> None:
    failures = Failures(first=frozenset({"a", "b"}), still=frozenset({"a"}))
    assert from_json(to_json(failures)) == failures


def test_from_json_rejects_version_1() -> None:
    """linux_check._load_base treats an unsupported cache version as a cache miss."""
    assert CACHE_VERSION != 1
    with pytest.raises(ResultsError):
        from_json({"version": 1, "first": [], "still": []})


def test_from_json_rejects_a_missing_version() -> None:
    with pytest.raises(ResultsError):
        from_json({"first": [], "still": []})


def test_from_json_rejects_a_non_list_first() -> None:
    with pytest.raises(ResultsError):
        from_json({"version": CACHE_VERSION, "first": "oops", "still": []})
