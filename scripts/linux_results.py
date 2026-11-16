"""Read a Linux suite's copied ``/out`` and compare the head's result with the base's.

``scripts/linux_check.py`` copies each container's ``/out`` directory to
``runs/<stamp>/<side>-<suite>/`` and hands that path to :func:`suite_failures` as ``out``.
The result is a :class:`Failures`: the ids a suite's first attempt showed failing, and the ids
a serial rerun inside the same container still shows failing
— for most steps a subset of ``first``, but a vitest rerun replays whole files (``scripts/linux/
run.sh``'s ``check_unit_rerun``/``e2e_e2e_rerun``), so it can fail an id the first run passed;
``still`` then holds that id too, even though it is absent from ``first``.
:attr:`Failures.confirmed` (``first & still``) is the ids that failed on both of a suite's runs:
the only ones a head can add as new. :func:`compare` turns a head ``Failures`` and an optional
base ``Failures`` into a :class:`Verdict` of new, known and flaky ids, where an id the base fails
on either its first run or its rerun counts as the base's::

    failed = head.first | head.still
    new    = head.confirmed - (base.first | base.still)
    known  = failed & (base.first | base.still)
    flaky  = failed - head.confirmed - (base.first | base.still)

A head id failing on only one of its two runs passed the other one, so it can only be known (the
base shares it, on either of its own runs) or flaky (the base does not), never new — only a
:attr:`~Failures.confirmed` id, one that failed both of the head's runs, can be new.

A plain step (lint, typecheck, build, setup, desktop's own test/smoke/e2e, package's and
compile's steps) has no sub-breakdown: its id is ``<suite> <step>`` and never leaves ``still``,
because it is never rerun. Three steps do have a breakdown and can change an id's standing on
rerun: a suite's ``python`` step (``<suite> python: <check.py label>`` and, for the pytest label,
``<suite> <pytest node id>``, from the copied ``lastfailed`` caches, whose rerun replays only
``first``'s ids) and ``check``'s ``unit`` / ``e2e``'s ``e2e`` steps (``<suite> <relpath> >
<fullName>``, from the vitest json reports, where ``still`` need not be a subset of ``first``).

A report that a failing step should produce, but that is missing, unreadable (including a
byte no UTF-8 decoder accepts), empty or otherwise unusable, never reads as a pass:
:func:`suite_failures` falls back to a marker id (``<suite> <step>: failed with no test listed``,
or ``<suite> python: no result line`` when even ``python.log``'s result line cannot be read) so the
step still counts as failed. A missing, malformed or incomplete ``out/steps.jsonl`` raises
:class:`ResultsError`, since it cannot establish which suite steps ran.

A suite that ran past its time limit (``scripts/linux_check.py``'s ``SUITE_LIMIT_S``) never
produces a report at all; :func:`timeout_id` is the marker standing in for it, and
:func:`is_timeout_id` tells :func:`compare` to always treat that marker as new while it is still
failing, never as known or flaky, even when the base's own run times out too and so happens to
produce the identical marker: a base run can never tell us the head would have passed given more
time.

A suite whose ``setup`` step failed never ran its tests either: ``<suite> setup`` is then its
only id. :func:`did_not_run` recognizes both markers, and :func:`compare` treats the setup one
the same way as the timeout one: new while it is still failing, even when the base's setup
fails too, because a base that could not be set up says nothing about the tests the head never
ran. ``scripts/linux_check.py`` refuses a base result holding either marker instead of comparing
against it or caching it.

A step that failed without naming one test is no evidence about any test either: a pytest stage
killed at its time limit and a collection that crashed on an import both leave the same
``<suite> python: failed with no test listed``. :func:`named_no_test` recognizes those step
markers, and :func:`no_evidence` joins them with the two above: :func:`compare` counts every
such id as new while it is still failing, never as known or flaky, and ``scripts/linux_check.py``
refuses a base result holding one. The other steps of that suite did run, so their ids are
compared as usual. A plain step has no breakdown at all, so one that fails on both sides, for
whatever reason, still counts as known.

Stdlib only; every file is read as UTF-8.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_TMP_WORK_PREFIX = "/tmp/work/"

# (suite, step) pairs whose report is a vitest json file. Every other failing step is
# generic: an id of its own, with no further breakdown (desktop's own "e2e" step included, since
# unlike the "e2e" suite's "e2e" step it runs with no reporter and writes no json).
_VITEST_STEPS: dict[tuple[str, str], tuple[str, str]] = {
    ("check", "unit"): ("unit.json", "unit-rerun.json"),
    ("e2e", "e2e"): ("e2e.json", "e2e-rerun.json"),
}

# Mandatory steps in scripts/linux/run.sh order; reruns may immediately follow their own step.
_SUITE_STEPS = {
    "check": ("setup", "python", "lint", "typecheck", "unit", "build"),
    "e2e": ("setup", "e2e"),
    "desktop": ("setup", "lint", "test", "smoke", "e2e"),
    "package": ("setup", "dist", "packaged-test"),
    "compile": ("setup", "compile", "worker-smoke", "app-smoke"),
    "oldgit": ("setup", "python"),
}
_RERUN_STEPS = {
    "check": ("python-rerun", "unit-rerun"),
    "e2e": ("e2e-rerun",),
    "oldgit": ("python-rerun",),
}

_NO_RESULT_LINE = "python: no result line"
_NO_TEST_LISTED = "failed with no test listed"

_TIMEOUT_INFIX = ": timed out after "
_TIMEOUT_SUFFIX = " min"

# A failed setup's id, in the plain-step form suite_failures builds; it is always the suite's
# only id, since _read_steps accepts no step after a failed setup.
_SETUP_IDS = frozenset(f"{suite} setup" for suite in _SUITE_STEPS)

# Every id a step with a breakdown falls back to when it failed without naming a test, in the
# forms _python_failures and _vitest_failures build. Exact ids, not a suffix test: a vitest id
# embeds a test's own name, which may end in anything.
_PYTHON_SUITES = tuple(suite for suite, steps in _SUITE_STEPS.items() if "python" in steps)
_NO_TEST_IDS = frozenset(
    [f"{suite} {_NO_RESULT_LINE}" for suite in _PYTHON_SUITES]
    + [f"{suite} python: {_NO_TEST_LISTED}" for suite in _PYTHON_SUITES]
    + [f"{suite} {step}: {_NO_TEST_LISTED}" for suite, step in _VITEST_STEPS]
)

# The base cache version: 2 added rerun-only failures; 3 requires complete suite step reports;
# 4 drops the files that could hold a base whose setup failed or that timed out; 5 drops the
# ones that could hold a base step that failed without naming a test.
CACHE_VERSION = 5


class ResultsError(Exception):
    """``out/steps.jsonl`` is missing, malformed or does not establish a complete suite."""


@dataclass(frozen=True)
class Failures:
    """The ids a suite's copied ``/out`` showed failing: on the first try, and after a rerun."""

    first: frozenset[str]
    still: frozenset[str]

    @property
    def confirmed(self) -> frozenset[str]:
        """``first & still``: the ids that failed on the first run and were not cleared by the
        rerun. An id ``still`` holds that ``first`` does not (a vitest rerun can fail an id the
        first run passed) is never confirmed; :func:`compare` still weighs it, as known or
        flaky, just never as new."""
        return self.first & self.still


@dataclass(frozen=True)
class Verdict:
    """``compare``'s result: ids new on the head, known to the base too, or flaky on the head."""

    new: frozenset[str]
    known: frozenset[str]
    flaky: frozenset[str]


def suite_failures(suite: str, out: Path) -> Failures:
    """The ``Failures`` a suite's copied ``/out`` directory shows, by the module docstring's id
    rules. Raises :class:`ResultsError` when ``out/steps.jsonl`` is invalid or incomplete; a
    missing or unusable per-step report fails closed into a marker id instead, so one broken
    report does not stop the rest of the comparison."""
    first: set[str] = set()
    still: set[str] = set()
    for step in _read_steps(suite, out):
        name = step["step"]
        if not isinstance(name, str):
            raise ResultsError(f"{out / 'steps.jsonl'}: step name is not a string: {name!r}")
        if name.endswith("-rerun"):
            continue  # a rerun's own exit code is not a failure; only its report feeds `still`
        if step["exit"] == 0:
            continue
        if name == "python":
            step_first, step_still = _python_failures(suite, out)
        elif (suite, name) in _VITEST_STEPS:
            first_name, rerun_name = _VITEST_STEPS[suite, name]
            step_first, step_still = _vitest_failures(suite, name, out, first_name, rerun_name)
        else:
            marker = f"{suite} {name}"
            step_first, step_still = {marker}, {marker}
        first |= step_first
        still |= step_still
    return Failures(first=frozenset(first), still=frozenset(still))


def timeout_id(suite: str, minutes: int) -> str:
    """The marker id standing in for a suite that ran past its time limit on one side
    (``<suite>: timed out after <minutes> min``). :func:`is_timeout_id` recognizes exactly what
    this builds."""
    return f"{suite}{_TIMEOUT_INFIX}{minutes}{_TIMEOUT_SUFFIX}"


def is_timeout_id(id_: str) -> bool:
    """Whether ``id_`` is a :func:`timeout_id` marker. :func:`compare` uses this so a timeout id
    is never known or flaky, even when both sides time out on the same suite and so produce the
    identical marker."""
    return _TIMEOUT_INFIX in id_ and id_.endswith(_TIMEOUT_SUFFIX)


def did_not_run(id_: str) -> bool:
    """Whether ``id_`` stands for a suite that never ran its tests on one side: a
    :func:`timeout_id` marker, or ``<suite> setup``, the id of a failed setup step. Neither is
    evidence about any test, so :func:`compare` never counts one as known or flaky, and
    ``scripts/linux_check.py`` neither runs a base for one nor accepts a base holding one."""
    return is_timeout_id(id_) or id_ in _SETUP_IDS


def named_no_test(id_: str) -> bool:
    """Whether ``id_`` is the marker of a step that failed without naming one test: a ``python``
    step with no result line or no failing node id, or a vitest step whose report is missing,
    unreadable or lists nothing failed. The suite's other steps did run, so this is not
    :func:`did_not_run`; but two such markers say nothing about each other, because whatever
    stopped the step (a time limit, a kill, a crash before the first test) leaves the same id."""
    return id_ in _NO_TEST_IDS


def no_evidence(id_: str) -> bool:
    """Whether ``id_`` is a marker that tells nothing about any test: :func:`did_not_run` or
    :func:`named_no_test`. :func:`compare` never counts one as known or flaky, and
    ``scripts/linux_check.py`` neither runs a base for one nor accepts a base holding one."""
    return did_not_run(id_) or named_no_test(id_)


def compare(head: Failures, base: Failures | None) -> Verdict:
    """The compare rule, including for an id failing on only one of the head's two runs
    (a vitest rerun replays whole files, so it can fail an id the first run passed, or the id can
    be cleared by the rerun): only :attr:`Failures.confirmed` ids, which failed on both
    of the head's runs, can count as new. With no base, there is nothing to compare: every
    confirmed id counts as new, and every other failing id (``head.first | head.still``, minus the
    confirmed ones) counts as flaky.

    The base's failure set is ``base.first | base.still``, not ``base.first`` alone: an id main
    passed first and failed only on its rerun still means main shares the failure, so a head id
    failing on only one of its two runs is known when the base shares it this way too, and flaky,
    never new, when the base does not.

    A :func:`no_evidence` marker (a timeout, a failed setup, or a step that failed without
    naming a test) is exempt from ``known`` and ``flaky``: while it is still failing on the head
    it always counts as new, never known, even when the base's own run produces the identical
    marker string."""
    base_failed = (base.first | base.still) if base is not None else frozenset()
    failed = head.first | head.still
    new = head.confirmed - base_failed
    known = failed & base_failed
    flaky = failed - head.confirmed - base_failed
    blind = frozenset(id_ for id_ in head.first if no_evidence(id_))
    return Verdict(
        new=new | (blind & head.still),
        known=known - blind,
        flaky=flaky - blind,
    )


def to_json(failures: Failures) -> dict[str, object]:
    """``failures`` as the sorted, versioned structure the base cache file stores."""
    return {
        "version": CACHE_VERSION,
        "first": sorted(failures.first),
        "still": sorted(failures.still),
    }


def from_json(data: dict[str, object]) -> Failures:
    """The inverse of :func:`to_json`. Raises :class:`ResultsError` for any other version, or a
    cache whose ``first``/``still`` are not lists."""
    if data.get("version") != CACHE_VERSION:
        raise ResultsError(f"unsupported base cache version: {data.get('version')!r}")
    first = data.get("first")
    still = data.get("still")
    if not isinstance(first, list) or not isinstance(still, list):
        raise ResultsError(f"malformed base cache, first/still are not lists: {data!r}")
    return Failures(first=frozenset(first), still=frozenset(still))


def _read_steps(suite: str, out: Path) -> list[dict[str, object]]:
    path = out / "steps.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ResultsError(f"missing steps.jsonl: {path}") from exc
    steps: list[dict[str, object]] = []
    names: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResultsError(f"{path}:{lineno}: not JSON: {line!r}") from exc
        if not isinstance(record, dict) or "step" not in record or "exit" not in record:
            raise ResultsError(f"{path}:{lineno}: not a step record: {line!r}")
        name, exit_code = record["step"], record["exit"]
        if not isinstance(name, str) or name in names:
            raise ResultsError(f"{path}:{lineno}: invalid or duplicate step: {name!r}")
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise ResultsError(f"{path}:{lineno}: invalid shell exit code: {exit_code!r}")
        if name.endswith("-rerun") and (
            name not in _RERUN_STEPS.get(suite, ()) or not names or names[-1] != name[:-6]
        ):
            raise ResultsError(f"{path}:{lineno}: unexpected rerun: {name!r}")
        names.append(name)
        steps.append(record)
    expected = _SUITE_STEPS.get(suite)
    if expected is None:
        raise ResultsError(f"unknown suite: {suite!r}")
    if steps and names[0] == "setup" and steps[0]["exit"] != 0:
        expected = ("setup",)
    if tuple(name for name in names if not name.endswith("-rerun")) != expected:
        raise ResultsError(f"{path}: incomplete or unexpected {suite} steps: {names!r}")
    return steps


def _read_json_object(path: Path) -> dict[str, object] | None:
    """A best-effort JSON object read: ``None`` when missing, unreadable or not an object."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _last_nonempty_line(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def _python_failures(suite: str, out: Path) -> tuple[set[str], set[str]]:
    """The ``<suite> python: …`` / ``<suite> <node id>`` ids ``python.log``'s result line and the
    lastfailed caches it names show failing."""
    line = _last_nonempty_line(out / "python.log")
    if line is None or not line.startswith("FAILED: "):
        marker = f"{suite} {_NO_RESULT_LINE}"
        return {marker}, {marker}
    labels = [label for label in line[len("FAILED: ") :].split(", ") if label]
    if not labels:
        marker = f"{suite} {_NO_RESULT_LINE}"
        return {marker}, {marker}
    first: set[str] = set()
    still: set[str] = set()
    for label in labels:
        if not label.startswith("pytest "):
            marker = f"{suite} python: {label}"
            first.add(marker)
            still.add(marker)
            continue
        first_failed = _read_json_object(out / "pytest-first.json")
        node_ids = sorted(first_failed) if first_failed else None
        if not node_ids:
            # the step's FAILED line says pytest failed, but pytest-first.json is missing or
            # empty: never read that as a pass, fail closed instead.
            marker = f"{suite} python: {_NO_TEST_LISTED}"
            first.add(marker)
            still.add(marker)
            continue
        first |= {f"{suite} {node_id}" for node_id in node_ids}
        still_failed = _read_json_object(out / "pytest-still.json")
        if still_failed is None:
            still |= {f"{suite} {node_id}" for node_id in node_ids}  # no rerun evidence: keep all
        else:
            still |= {f"{suite} {node_id}" for node_id in still_failed}
    return first, still


def _vitest_failures(
    suite: str, step: str, out: Path, first_name: str, rerun_name: str
) -> tuple[set[str], set[str]]:
    """The ``<suite> <relpath> > <fullName>`` / ``<suite> <relpath>`` ids a vitest json report
    and its rerun report show failing."""
    first_statuses = _vitest_statuses(_read_json_object(out / first_name), suite)
    first = {id_ for id_, status in first_statuses.items() if status == "failed"}
    if not first:
        # the step's exit code says it failed, but the report lists nothing failing: never read
        # that as a pass, fail closed instead.
        marker = f"{suite} {step}: {_NO_TEST_LISTED}"
        return {marker}, {marker}
    rerun_statuses = _vitest_statuses(_read_json_object(out / rerun_name), suite)
    # an id absent from the rerun report is not evidence it passed: still drops a first id only
    # when the rerun report shows it passed, so a missing or unreadable rerun report keeps
    # everything. It also gains every id the rerun report itself shows failed, in first or not:
    # the rerun replays whole files, so it can fail an id the first run passed.
    still = {id_ for id_ in first if rerun_statuses.get(id_, "failed") == "failed"}
    still |= {id_ for id_, status in rerun_statuses.items() if status == "failed"}
    return first, still


def _vitest_statuses(data: dict[str, object] | None, suite: str) -> dict[str, str]:
    """Every id a vitest json report can produce, mapped to its reported status."""
    statuses: dict[str, str] = {}
    if data is None:
        return statuses
    results = data.get("testResults")
    if not isinstance(results, list):
        return statuses
    for result in results:
        if not isinstance(result, dict):
            continue
        relpath = _relpath(result.get("name"))
        assertions = result.get("assertionResults")
        any_failed = False
        if isinstance(assertions, list):
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    continue
                full_name = assertion.get("fullName")
                status = assertion.get("status")
                if isinstance(full_name, str) and isinstance(status, str):
                    statuses[f"{suite} {relpath} > {full_name}"] = status
                    any_failed = any_failed or status == "failed"
        if not any_failed:
            # a failed file with no failed assertion of its own gives the file id.
            status = result.get("status")
            if isinstance(status, str):
                statuses[f"{suite} {relpath}"] = status
    return statuses


def _relpath(name: object) -> str:
    if not isinstance(name, str):
        return str(name)
    if name.startswith(_TMP_WORK_PREFIX):
        return name[len(_TMP_WORK_PREFIX) :]
    return name
