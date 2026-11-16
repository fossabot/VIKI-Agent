"""The integration range and the shared Git readers, real Git in `tmp_path` and scripted Git output
for every malformed shape."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at
from tests.workspace_code.test_code_git import _git, _refusal, _repo, needs_git
from viki_agent import winproc
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import ARGV_PATH_CHARS, GIT_TIMEOUT_S, CodeGit, chunks
from viki_agent.code.code_range import (
    MAX_BLOB_BYTES,
    MAX_COMMITS,
    MAX_RANGE_PATHS,
    MAX_SUBJECT_CHARS,
    _filters_from_index,
    filters,
    integration,
    name_kind,
    nul_fields,
    range_paths,
    tree_entries,
)

A = "a" * 40
B = "b" * 40
Answer = str | Exception | Callable[[tuple[str, ...]], str]
Call = tuple[tuple[str, ...], Path | None]
UNUSED = Path("unused")


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _code_git(tmp_path: Path) -> CodeGit:
    service = service_at(tmp_path)
    return CodeGit(service, CodeFiles(service), git_available=lambda: True)


@pytest.fixture
def git(tmp_path: Path) -> CodeGit:
    return _code_git(tmp_path)


def _script(monkeypatch: pytest.MonkeyPatch, **answers: Answer) -> list[Call]:
    """Answer each Git call by its subcommand (`raw` and `numstat` for the two `diff`s, `cached`
    for `check-attr --cached`), and record its arguments with the `index` it was given."""
    calls: list[Call] = []

    def fake(
        _self: CodeGit,
        _root: Path,
        *args: str,
        timeout: float = GIT_TIMEOUT_S,
        index: Path | None = None,
    ) -> str:
        calls.append((args, index))
        key = ("raw" if "--raw" in args else "numstat") if args[0] == "diff" else args[0]
        if args[:2] == ("check-attr", "--cached"):
            key = "cached"
        answer = answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer(args) if callable(answer) else answer

    monkeypatch.setattr(CodeGit, "git", fake)
    return calls


def _commit_staged(root: Path, message: str) -> str:
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    return _commit_staged(root, message)


def _blob(tmp_path: Path, root: Path, text: str) -> str:
    source = tmp_path / "blob-source"
    source.write_text(text, encoding="utf-8")
    return _git(root, "hash-object", "-w", str(source)).strip()


# --- chunks, nul_fields, name_kind ----------------------------------------------------------


def test_chunks_group_paths_by_their_argv_cost() -> None:
    assert list(chunks([])) == []
    assert list(chunks(["ab", "cd", "ef"], 6)) == [["ab", "cd"], ["ef"]]
    assert list(chunks(["ab", "cd", "ef"], 9)) == [["ab", "cd", "ef"]]
    assert list(chunks(["ab", "cd", "ef"], 2)) == [["ab"], ["cd"], ["ef"]]


def test_chunks_fill_a_group_to_the_limit_and_split_one_over() -> None:
    assert ARGV_PATH_CHARS == 16_000
    exact = ["x" * 7_999, "y" * 7_999]
    assert [len(group) for group in chunks(exact)] == [2]
    assert [len(group) for group in chunks(["x" * 7_999, "y" * 8_000])] == [1, 1]


def test_nul_fields_read_terminated_output_and_refuse_a_tail() -> None:
    assert nul_fields("") == []
    assert nul_fields("a\0b c\0") == ["a", "b c"]
    assert nul_fields("\0") == [""]
    with pytest.raises(ValueError, match="unterminated"):
        nul_fields("a\0b")


def test_name_kind_names_what_the_code_reader_refuses() -> None:
    assert name_kind(".env") == "private"
    assert name_kind("src/.git/config") == "private"
    assert name_kind("aux.c") == "unsupported_name"
    assert name_kind("src/a.py") is None
    assert name_kind("AGENTS.md") is None


# --- tree_entries and filters on real Git ---------------------------------------------------


@needs_git
def test_tree_entries_reads_blobs_trees_and_leaves_absent_paths_out(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    (root / "d").mkdir()
    (root / "d" / "x.txt").write_text("abc", encoding="utf-8")
    first = _commit_all(root, "one")
    (root / "later.txt").write_text("l", encoding="utf-8")
    _commit_all(root, "two")
    git = _code_git(tmp_path)
    found = tree_entries(git, root, "HEAD", ["d/x.txt", "nope.txt", "later.txt"])
    assert set(found) == {"d/x.txt", "later.txt"}
    directory = tree_entries(git, root, "HEAD", ["d"])["d"]
    assert (directory.type, directory.size) == ("tree", None)
    assert (found["d/x.txt"].mode, found["d/x.txt"].type, found["d/x.txt"].size) == (
        "100644",
        "blob",
        3,
    )
    assert set(tree_entries(git, root, first, ["d/x.txt", "later.txt"])) == {"d/x.txt"}
    assert tree_entries(git, root, "HEAD", []) == {}


@needs_git
def test_filters_read_the_asked_source_and_not_the_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    (root / ".gitattributes").write_text("*.lfs filter=viki-test\n", encoding="utf-8")
    (root / "a.lfs").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    head = _commit_all(root, "one")
    (root / ".gitattributes").write_text("", encoding="utf-8")
    git = _code_git(tmp_path)
    paths = ["a.lfs", "b.txt"]
    assert filters(git, root, paths, source=head) == {"a.lfs": "viki-test", "b.txt": "unspecified"}
    assert filters(git, root, paths) == {"a.lfs": "unspecified", "b.txt": "unspecified"}
    assert filters(git, root, []) == {}


def test_long_argument_lists_split_into_chunks(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit
) -> None:
    paths = [f"{index:03d}" + "x" * 497 for index in range(40)]
    asked = lambda args: args[args.index("--") + 1 :]  # noqa: E731
    calls = _script(
        monkeypatch,
        **{
            "ls-tree": "",
            "check-attr": lambda args: "".join(f"{p}\0filter\0unspecified\0" for p in asked(args)),
        },
    )
    assert tree_entries(git, UNUSED, "HEAD", paths) == {}
    assert len(filters(git, UNUSED, paths)) == 40
    for subcommand in ("ls-tree", "check-attr"):
        groups = [asked(args) for args, _ in calls if args[0] == subcommand]
        assert [len(group) for group in groups] == [31, 9]
        assert all(sum(len(p) + 1 for p in group) <= ARGV_PATH_CHARS for group in groups)


def test_paths_follow_dash_dash_and_revisions_are_never_options(
    monkeypatch: pytest.MonkeyPatch,
    git: CodeGit,
) -> None:
    calls = _script(monkeypatch, **{"ls-tree": ""})
    tree_entries(git, UNUSED, "HEAD", ["-rf", "--output=x"])
    assert calls == [(("ls-tree", "-l", "-z", "HEAD", "--", "-rf", "--output=x"), None)]
    for bad in ("--output=x", "-x", "main", "HEAD~1", ""):
        with pytest.raises(ValueError, match="revision"):
            tree_entries(git, UNUSED, bad, ["a"])
        with pytest.raises(ValueError, match="revision"):
            filters(git, UNUSED, ["a"], source=bad)
        with pytest.raises(ValueError, match="revision"):
            _filters_from_index(git, UNUSED, ["a"], bad)
        with pytest.raises(ValueError, match="revision"):
            range_paths(git, UNUSED, bad, A)
        with pytest.raises(ValueError, match="revision"):
            range_paths(git, UNUSED, A, bad)
    with pytest.raises(ValueError, match="revision"):
        integration(git, UNUSED, "HEAD~1")
    assert len(calls) == 1


# --- every reader refuses unterminated or unknown output ------------------------------------

TREE = f"100644 blob {A}      12\tf.txt\0"


def test_tree_entries_read_the_ls_tree_record_shape(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit
) -> None:
    _script(
        monkeypatch, **{"ls-tree": f"{TREE}040000 tree {B}      -\td\x00160000 commit {A} -\ts\0"}
    )
    found = tree_entries(git, UNUSED, "HEAD", ["f.txt", "d", "s"])
    assert (found["f.txt"].oid, found["f.txt"].size) == (A, 12)
    assert (found["d"].type, found["d"].size) == ("tree", None)
    assert (found["s"].mode, found["s"].type, found["s"].size) == ("160000", "commit", None)


@pytest.mark.parametrize(
    "raw",
    [
        TREE.removesuffix("\0"),
        f"100644 blob {A}      12 extra\tf.txt\0",
        f"100644 blob {A}\tf.txt\0",
        f"10064 blob {A}      12\tf.txt\0",
        f"100689 blob {A}      12\tf.txt\0",
        f"100644 tag {A}      12\tf.txt\0",
        f"100644 blob {A[:39]}      12\tf.txt\0",
        f"100644 blob {A.upper()}      12\tf.txt\0",
        f"100644 blob {A}       -\tf.txt\0",
        f"040000 tree {A}      12\tf.txt\0",
        f"100644 blob {A}      1x\tf.txt\0",
        f"100644 blob {A} 12 f.txt\0",
        f"100644 blob {A}      12\t\0",
        f"100644 blob {A}      12\tother.txt\0",
        TREE + TREE,
    ],
)
def test_tree_entries_refuse_malformed_output(
    monkeypatch: pytest.MonkeyPatch, raw: str, git: CodeGit
) -> None:
    _script(monkeypatch, **{"ls-tree": raw})
    assert _refusal(lambda: tree_entries(git, UNUSED, "HEAD", ["f.txt"])) == "git_failed"


MALFORMED_FILTERS = [
    "a.txt\0filter\0lfs",
    "a.txt\0filter\0",
    "a.txt\0filter\0lfs\0b.txt\0filter\0",
    "a.txt\0text\0lfs\0",
    "other.txt\0filter\0lfs\0",
    "a.txt\0filter\0\0",
    "a.txt\0filter\0lfs\0a.txt\0filter\0lfs\0",
    "a.txt\0filter\0lfs\0b.txt\0filter\0lfs\0",
]


@pytest.mark.parametrize("raw", MALFORMED_FILTERS)
def test_filters_refuse_malformed_output(
    monkeypatch: pytest.MonkeyPatch, raw: str, git: CodeGit
) -> None:
    calls = _script(monkeypatch, **{"check-attr": raw})
    assert _refusal(lambda: filters(git, UNUSED, ["a.txt"], source=A)) == "git_failed"
    assert [args[0] for args, _ in calls] == ["check-attr"]


def test_filters_refuse_an_asked_path_left_unanswered(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit
) -> None:
    _script(monkeypatch, **{"check-attr": "a.txt\0filter\0lfs\0"})
    assert _refusal(lambda: filters(git, UNUSED, ["a.txt", "b.txt"])) == "git_failed"


def _raw(path: str = "f.txt", status: str = "M", modes: str = "100644 100644") -> str:
    return f":{modes} {A} {B} {status}\0{path}\0"


def _range(
    monkeypatch: pytest.MonkeyPatch,
    git: CodeGit,
    *,
    raw: str | None = None,
    numstat: str | None = None,
    size: str = "12",
    attribute: str = "unspecified",
    path: str = "f.txt",
) -> tuple[tuple[tuple[str, str, str], ...], bool]:
    """`range_paths` over scripted output: one delta, its numstat, size and `filter` value."""
    _script(
        monkeypatch,
        raw=_raw(path) if raw is None else raw,
        numstat=f"1\t1\t{path}\0" if numstat is None else numstat,
        **{
            "ls-tree": f"100644 blob {A} {size.rjust(8)}\t{path}\0",
            "check-attr": f"{path}\0filter\0{attribute}\0",
        },
    )
    found, more = range_paths(git, UNUSED, A, B)
    return tuple((p.path, p.change, p.kind) for p in found), more


@pytest.mark.parametrize(
    "raw",
    [
        _raw().removesuffix("\0"),
        _raw()[:-1] + "\0\0",
        f":100644 100644 {A} {B} M\0",
        _raw()[1:],
        _raw(status="R"),
        _raw(status="U"),
        _raw(status="X"),
        _raw(status="MM"),
        _raw(modes="10064 100644"),
        _raw(modes="100689 100644"),
        f":100644 100644 {A[:39]} {B} M\0f.txt\0",
        f":100644 100644 {A} {B.upper()} M\0f.txt\0",
        _raw(path=""),
        _raw() + _raw(),
    ],
)
def test_range_paths_refuse_malformed_raw_output(
    monkeypatch: pytest.MonkeyPatch, raw: str, git: CodeGit
) -> None:
    _script(monkeypatch, raw=raw)
    assert _refusal(lambda: range_paths(git, UNUSED, A, B)) == "git_failed"


@pytest.mark.parametrize(
    "numstat",
    [
        "1\t1\tf.txt",
        "1\t1\0",
        "1\t1\t\0",
        "a\tb\tf.txt\0",
        "-\t1\tf.txt\0",
        "1\t-\tf.txt\0",
        "1\t1\tf.txt\0" * 2,
        "1\t1\tz.txt\0",
        "1 1 f.txt\0",
    ],
)
def test_range_paths_refuse_malformed_numstat_output(
    monkeypatch: pytest.MonkeyPatch, numstat: str, git: CodeGit
) -> None:
    _script(monkeypatch, raw=_raw(), numstat=numstat)
    assert _refusal(lambda: range_paths(git, UNUSED, A, B)) == "git_failed"


def test_a_failing_git_call_is_git_failed_and_no_call_follows(
    monkeypatch: pytest.MonkeyPatch,
    git: CodeGit,
) -> None:
    calls = _script(monkeypatch, raw=RuntimeError("git diff failed"))
    assert _refusal(lambda: range_paths(git, UNUSED, A, B)) == "git_failed"
    assert len(calls) == 1


def test_an_empty_range_has_no_paths_and_asks_for_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
    git: CodeGit,
) -> None:
    calls = _script(monkeypatch, raw="")
    assert range_paths(git, UNUSED, A, B) == ((), False)
    assert len(calls) == 1


# --- a commit's filters when Git refuses --source (before 2.40), on scripted output ---------


def test_a_refused_source_reads_the_commit_through_one_temporary_index(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit
) -> None:
    paths = [f"{index:03d}" + "x" * 497 for index in range(40)]
    asked = lambda args: args[args.index("--") + 1 :]  # noqa: E731
    calls = _script(
        monkeypatch,
        **{
            "check-attr": RuntimeError("git check-attr failed: error: unknown option"),
            "read-tree": "",
            "cached": lambda args: "".join(f"{p}\0filter\0lfs\0" for p in asked(args)),
        },
    )

    assert filters(git, UNUSED, paths, source=A) == dict.fromkeys(paths, "lfs")

    (refused, no_index), (read, index), *cached = calls
    assert refused[:2] == ("check-attr", f"--source={A}") and no_index is None
    assert read == ("read-tree", A)
    assert index is not None and index.is_absolute() and index.name == "index"
    assert index.parent.name.startswith("viki-attributes-") and not index.parent.exists()
    assert [args[:5] for args, _ in cached] == [
        ("check-attr", "--cached", "-z", "filter", "--")
    ] * 2
    assert [len(asked(args)) for args, _ in cached] == [31, 9]
    assert {used for _, used in cached} == {index}


@pytest.mark.parametrize(
    "failure", [TimeoutError("slow"), OSError("no git")], ids=["timeout", "oserror"]
)
def test_a_timeout_or_other_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit, failure: Exception
) -> None:
    calls = _script(monkeypatch, **{"check-attr": failure, "read-tree": "", "cached": ""})
    assert _refusal(lambda: filters(git, UNUSED, ["a.txt"], source=A)) == "git_unavailable"
    assert [args[0] for args, _ in calls] == ["check-attr"]


@pytest.mark.parametrize("raw", MALFORMED_FILTERS)
def test_filters_refuse_malformed_cached_output(
    monkeypatch: pytest.MonkeyPatch, raw: str, git: CodeGit
) -> None:
    refused = RuntimeError("git check-attr failed: error: unknown option")
    calls = _script(monkeypatch, **{"check-attr": refused, "read-tree": "", "cached": raw})
    assert _refusal(lambda: filters(git, UNUSED, ["a.txt"], source=A)) == "git_failed"
    assert [args[:2] for args, _ in calls] == [
        ("check-attr", f"--source={A}"),
        ("read-tree", A),
        ("check-attr", "--cached"),
    ]


def test_an_answered_range_runs_no_read_tree(monkeypatch: pytest.MonkeyPatch, git: CodeGit) -> None:
    calls = _script(
        monkeypatch,
        raw=_raw(),
        numstat="1\t1\tf.txt\0",
        **{"ls-tree": TREE, "check-attr": "f.txt\0filter\0unspecified\0"},
    )

    assert [entry.kind for entry in range_paths(git, UNUSED, A, B)[0]] == ["text"]
    assert [(args[0], index) for args, index in calls] == [
        ("diff", None),
        ("diff", None),
        ("ls-tree", None),
        ("ls-tree", None),
        ("check-attr", None),
    ]
    assert calls[-1][0][1] == f"--source={B}"


# --- a commit's filters through a temporary index, on real Git ------------------------------

# Commit H's answers in `_attribute_matrix`: `--source` (Git 2.40 and later) and the temporary
# index both give exactly these.
MATRIX = {
    "a.lfs": "viki-root",
    "m.mac": "viki-macro",
    "unset.txt": "unset",
    "bang.txt": "unspecified",
    "plain.txt": "unspecified",
    "info.txt": "viki-info",
    "global.txt": "viki-global",
    "sub/q.n": "viki-nested",
    "sub/override.lfs": "unset",
    "sub/uses-bad.txt": "unspecified",
    "sub/deeper/x.txt": "viki-deeper",
    "other/y.lfs": "viki-other",
    "other/keep.txt": "unspecified",
    "dirattr/f.lfs": "viki-root",
    "link/z.lfs": "viki-root",
    "absent/n.lfs": "viki-root",
    "Sub/q.n": "unspecified",
}
ROOT_ATTRIBUTES = (
    "[attr]lfsish filter=viki-macro\n*.lfs filter=viki-root\n*.mac lfsish\n"
    "unset.txt -filter\nbang.txt !filter\n"
)


def _attribute_matrix(tmp_path: Path) -> tuple[Path, str]:
    """A repository and its commit H: root, nested, macro, unset and `!` attributes, a folder
    named `.gitattributes` and a link named so. After H come attributes H lacks: info,
    `core.attributesFile`, a staged root rule, and on disk an edited and a deleted file."""
    root = _repo(tmp_path / "r")
    files = {
        ".gitattributes": ROOT_ATTRIBUTES,
        "sub/.gitattributes": (
            "*.n filter=viki-nested\noverride.lfs -filter\n[attr]bad filter=viki-bad\n"
            "uses-bad.txt bad\n"
        ),
        "sub/deeper/.gitattributes": "x.txt filter=viki-deeper\n",
        "other/.gitattributes": "y.lfs filter=viki-other\n",
        "dirattr/.gitattributes/inner.txt": "inner\n",
    }
    files.update((path, f"{path}\n") for path in MATRIX if path not in ("absent/n.lfs", "Sub/q.n"))
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    link = _blob(tmp_path, root, "../.gitattributes")
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{link},link/.gitattributes")
    head = _commit_staged(root, "H")
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text(
        "info.txt filter=viki-info\n", encoding="utf-8"
    )
    personal = tmp_path / "global-attributes"
    personal.write_text("global.txt filter=viki-global\n", encoding="utf-8")
    _git(root, "config", "core.attributesFile", personal.as_posix())
    (root / ".gitattributes").write_text(
        ROOT_ATTRIBUTES + "*.txt filter=viki-staged\n", encoding="utf-8"
    )
    _git(root, "add", ".gitattributes")
    (root / ".gitattributes").write_text("*.lfs filter=viki-worktree\n", encoding="utf-8")
    (root / "sub" / ".gitattributes").unlink()
    return root, head


@needs_git
@pytest.mark.parametrize("sparse", [False, True], ids=["plain", "cone-sparse"])
def test_a_commits_filters_read_alike_from_the_commit_and_a_temporary_index(
    tmp_path: Path, sparse: bool
) -> None:
    root, head = _attribute_matrix(tmp_path)
    if sparse:
        _git(root, "sparse-checkout", "init", "--cone")
        _git(root, "sparse-checkout", "set", "sub")
        _git(root, "config", "index.sparse", "true")
        assert not (root / "other" / "y.lfs").exists()  # control: the cone left other/ out
    git = _code_git(tmp_path)

    assert _filters_from_index(git, root, list(MATRIX), head) == MATRIX
    assert filters(git, root, list(MATRIX), source=head) == MATRIX


def _git_listing(root: Path) -> list[str]:
    """Every path under `.git`, its objects aside."""
    git_dir = root / ".git"
    return sorted(
        path.relative_to(git_dir).as_posix()
        for path in git_dir.rglob("*")
        if path.relative_to(git_dir).parts[0] != "objects"
    )


@needs_git
def test_a_temporary_index_leaves_the_repository_and_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = _attribute_matrix(tmp_path)
    _git(root, "config", "core.splitIndex", "true")
    _git(root, "update-index", "--split-index")
    listing, index = _git_listing(root), (root / ".git" / "index").read_bytes()
    assert any(name.startswith("sharedindex.") for name in listing)  # control: a split index
    git = _code_git(tmp_path)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    seen: list[tuple[bool, Path]] = []
    real_run = winproc.run

    def spy(argv: list[str], **options: Any) -> Any:
        child = options.get("env") or {}
        seen.append(("GIT_INDEX_FILE" in os.environ, Path(child.get("GIT_INDEX_FILE", ""))))
        return real_run(argv, **options)

    monkeypatch.setattr(winproc, "run", spy)

    assert _filters_from_index(git, root, list(MATRIX), head) == MATRIX
    assert list(temp_root.iterdir()) == []
    assert _refusal(lambda: _filters_from_index(git, root, ["a.lfs"], B)) == "git_failed"
    assert list(temp_root.iterdir()) == []

    assert _git_listing(root) == listing
    assert (root / ".git" / "index").read_bytes() == index
    # read-tree and check-attr, then the failing read-tree: each in a folder under temp_root.
    assert [(leaked, used.parent.parent) for leaked, used in seen] == [(False, temp_root)] * 3
    assert "GIT_INDEX_FILE" not in os.environ


@needs_git
def test_a_temporary_index_runs_no_fsmonitor_hook(tmp_path: Path) -> None:
    # Index calls carry HARDENING too: without it, the repo's own fsmonitor command runs.
    root, head = _attribute_matrix(tmp_path)
    marker = tmp_path / "hook-ran"
    _git(root, "config", "core.fsmonitor", f"echo ran > '{marker.as_posix()}'")
    git = _code_git(tmp_path)

    assert _filters_from_index(git, root, list(MATRIX), head) == MATRIX
    assert not marker.exists()

    # The hook is live: a status without the hardening runs it.
    winproc.git(root, "status", "--porcelain")
    assert marker.exists()


# --- the kind order, on scripted output -----------------------------------------------------


@pytest.mark.parametrize(
    ("options", "kind"),
    [
        ({}, "text"),
        ({"path": ".env"}, "private"),
        ({"path": "aux.c"}, "unsupported_name"),
        ({"path": ".env", "raw": _raw(".env", modes="100644 120000")}, "private"),
        ({"path": "aux.c", "raw": _raw("aux.c", modes="100644 160000")}, "unsupported_name"),
        ({"raw": _raw(modes="000000 160000", status="A")}, "submodule"),
        ({"raw": _raw(modes="100644 120000", status="T")}, "symlink"),
        ({"raw": _raw(modes="120000 100644", status="T")}, "symlink"),
        ({"raw": _raw(status="T")}, "other"),
        ({"numstat": ""}, "other"),
        ({"numstat": "-\t-\tf.txt\0", "size": str(MAX_BLOB_BYTES + 1)}, "binary"),
        ({"size": str(MAX_BLOB_BYTES)}, "text"),
        ({"size": str(MAX_BLOB_BYTES + 1)}, "too_large"),
        ({"size": str(MAX_BLOB_BYTES + 1), "attribute": "lfs"}, "too_large"),
        ({"attribute": "lfs"}, "filtered"),
        ({"attribute": "unset"}, "text"),
    ],
)
def test_the_first_matching_kind_wins(
    monkeypatch: pytest.MonkeyPatch, git: CodeGit, options: dict[str, str], kind: str
) -> None:
    found, more = _range(monkeypatch, git, **options)
    assert [entry[2] for entry in found] == [kind]
    assert more is False


def test_a_status_letter_gives_the_change(monkeypatch: pytest.MonkeyPatch, git: CodeGit) -> None:
    for status, change in (("A", "added"), ("M", "modified"), ("D", "deleted")):
        found, _ = _range(monkeypatch, git, raw=_raw(status=status))
        assert found == (("f.txt", change, "text"),)


# --- each kind from a real commit pair ------------------------------------------------------


@needs_git
def test_every_kind_from_a_real_commit_pair(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    # Git for Windows refuses the name `aux.c` unless NTFS protection is off.
    _git(root, "config", "core.protectNTFS", "false")
    for name in ("m.txt", "d.txt", "t.txt"):
        (root / name).write_text("one\n", encoding="utf-8")
    (root / "bigdel.txt").write_text("line\n" * 230_000, encoding="utf-8")
    base = _commit_all(root, "base")
    (root / "m.txt").write_text("two\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    (root / ".env").write_text("K=v\n", encoding="utf-8")
    (root / "bin.dat").write_bytes(b"a\0b\n")
    (root / "big.txt").write_text("line\n" * 230_000, encoding="utf-8")
    (root / "x.lfs").write_text("pointer\n", encoding="utf-8")
    (root / ".gitattributes").write_text("*.lfs filter=viki-test\n", encoding="utf-8")
    (root / "d.txt").unlink()
    (root / "bigdel.txt").unlink()
    _git(root, "add", "-A")
    link = _blob(tmp_path, root, "target")
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{link},link")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{base},sub")
    _git(root, "update-index", "--add", "--cacheinfo", f"100644,{link},aux.c")
    _git(root, "update-index", "--cacheinfo", f"120000,{link},t.txt")
    head = _commit_staged(root, "head")
    (root / ".gitattributes").unlink()

    found, more = range_paths(_code_git(tmp_path), root, base, head)

    assert more is False
    assert {entry.path: (entry.change, entry.kind) for entry in found} == {
        "m.txt": ("modified", "text"),
        "d.txt": ("deleted", "text"),
        "bigdel.txt": ("deleted", "too_large"),
        "new.txt": ("added", "text"),
        ".env": ("added", "private"),
        "bin.dat": ("added", "binary"),
        "big.txt": ("added", "too_large"),
        "x.lfs": ("added", "filtered"),
        ".gitattributes": ("added", "text"),
        "link": ("added", "symlink"),
        "sub": ("added", "submodule"),
        "aux.c": ("added", "unsupported_name"),
        "t.txt": ("modified", "symlink"),
    }


@needs_git
def test_a_link_in_the_range_is_not_reviewable_text(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    base = _commit_all(root, "base")
    link = _blob(tmp_path, root, "a.txt")
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{link},link")
    head = _commit_staged(root, "link")

    found, _ = range_paths(_code_git(tmp_path), root, base, head)

    assert [(entry.path, entry.kind) for entry in found] == [("link", "symlink")]


@needs_git
def test_a_range_caps_its_paths_at_500(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    (root / "first.txt").write_text("f\n", encoding="utf-8")
    base = _commit_all(root, "base")
    for index in range(MAX_RANGE_PATHS):
        (root / f"f{index:03d}.txt").write_text("x\n", encoding="utf-8")
    exact = _commit_all(root, "500")
    (root / "zz.txt").write_text("z\n", encoding="utf-8")
    over = _commit_all(root, "501")
    git = _code_git(tmp_path)

    found, more = range_paths(git, root, base, exact)
    assert (len(found), more) == (500, False)

    found, more = range_paths(git, root, base, over)
    assert (len(found), more) == (500, True)
    assert found[0].path == "f000.txt"
    assert "zz.txt" not in {entry.path for entry in found}


# --- integration ----------------------------------------------------------------------------


def _folder_with_history(tmp_path: Path) -> tuple[CodeGit, Path, str]:
    """`main` at c1 (c0 + b.txt); returns the c0 oid."""
    root = _repo(tmp_path / "r")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    c0 = _commit_all(root, "c0")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    _commit_all(root, "c1")
    return _code_git(tmp_path), root, c0


@needs_git
def test_integration_of_a_fast_forward(tmp_path: Path) -> None:
    git, root, _ = _folder_with_history(tmp_path)
    target = git.head(root)
    _git(root, "switch", "-q", "-c", "feature")
    (root / "f1.txt").write_text("1\n", encoding="utf-8")
    _commit_all(root, "first feature")
    (root / "f2.txt").write_text("2\n", encoding="utf-8")
    tip = _commit_all(root, "second feature")
    _git(root, "switch", "-q", "main")

    result = integration(git, root, tip)

    assert (result.target_branch, result.target_oid) == ("main", target)
    assert (result.merge_base, result.fast_forward) == (target, True)
    assert (result.ahead, result.behind) == (2, 0)
    assert [commit.subject for commit in result.commits] == ["second feature", "first feature"]
    assert result.commits_truncated is False
    assert [(p.path, p.change, p.kind) for p in result.paths] == [
        ("f1.txt", "added", "text"),
        ("f2.txt", "added", "text"),
    ]
    assert result.paths_truncated is False


@needs_git
def test_integration_of_a_head_behind_the_folder(tmp_path: Path) -> None:
    git, root, c0 = _folder_with_history(tmp_path)

    result = integration(git, root, c0)

    assert (result.merge_base, result.fast_forward) == (c0, False)
    assert (result.ahead, result.behind) == (0, 1)
    assert result.commits == ()
    assert [(p.path, p.change) for p in result.paths] == [("b.txt", "deleted")]


@needs_git
def test_integration_of_diverged_histories(tmp_path: Path) -> None:
    git, root, c0 = _folder_with_history(tmp_path)
    _git(root, "switch", "-q", "-c", "side", c0)
    (root / "s.txt").write_text("s\n", encoding="utf-8")
    tip = _commit_all(root, "side")
    _git(root, "switch", "-q", "main")

    result = integration(git, root, tip)

    assert (result.merge_base, result.fast_forward) == (c0, False)
    assert (result.ahead, result.behind) == (1, 1)
    assert [commit.subject for commit in result.commits] == ["side"]
    assert {(p.path, p.change) for p in result.paths} == {("b.txt", "deleted"), ("s.txt", "added")}


@needs_git
def test_integration_of_unrelated_histories_has_no_merge_base(tmp_path: Path) -> None:
    git, root, _ = _folder_with_history(tmp_path)
    _git(root, "switch", "-q", "--orphan", "other")
    (root / "o.txt").write_text("o\n", encoding="utf-8")
    tip = _commit_all(root, "orphan")
    _git(root, "switch", "-q", "-f", "main")

    result = integration(git, root, tip)

    assert (result.merge_base, result.fast_forward) == (None, False)
    assert (result.ahead, result.behind) == (1, 2)
    assert [commit.subject for commit in result.commits] == ["orphan"]
    assert ("o.txt", "added", "text") in {(p.path, p.change, p.kind) for p in result.paths}


@needs_git
def test_integration_in_a_detached_folder_has_no_target_branch(tmp_path: Path) -> None:
    git, root, c0 = _folder_with_history(tmp_path)
    _git(root, "switch", "-q", "--detach")

    result = integration(git, root, c0)

    assert result.target_branch is None
    assert result.target_oid == git.head(root)


@needs_git
def test_integration_refuses_a_branch_name_viki_cannot_name(tmp_path: Path) -> None:
    git, root, c0 = _folder_with_history(tmp_path)
    _git(root, "switch", "-q", "-c", "a+b")

    assert _refusal(lambda: integration(git, root, c0)) == "unsupported_branch"


@needs_git
def test_integration_of_a_folder_without_commits_is_no_commits(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")

    assert _refusal(lambda: integration(_code_git(tmp_path), root, A)) == "no_commits"


@needs_git
def test_51_commits_list_50_and_cut_subjects(tmp_path: Path) -> None:
    git, root, _ = _folder_with_history(tmp_path)
    _git(root, "switch", "-q", "-c", "many")
    for index in range(MAX_COMMITS):
        _commit_staged(root, f"n{index}")
    tip = _commit_staged(root, "x" * 250)
    just_fits = _git(root, "rev-parse", "HEAD~1").strip()
    _git(root, "switch", "-q", "main")

    result = integration(git, root, tip)
    assert (len(result.commits), result.commits_truncated, result.ahead) == (50, True, 51)
    assert result.commits[0].subject == "x" * MAX_SUBJECT_CHARS
    assert result.commits[-1].subject == "n1"

    result = integration(git, root, just_fits)
    assert (len(result.commits), result.commits_truncated, result.ahead) == (50, False, 50)


# --- integration on scripted output ---------------------------------------------------------


def _integration_script(monkeypatch: pytest.MonkeyPatch, **answers: Answer) -> None:
    base: dict[str, Answer] = {
        "symbolic-ref": "refs/heads/main\n",
        "rev-parse": f"{A}\n",
        "merge-base": f"{A}\n",
        "rev-list": "0\t1\n",
        "log": f"{B}\0subject\0",
        "raw": "",
    }
    _script(monkeypatch, **(base | answers))


def test_integration_reads_scripted_output(monkeypatch: pytest.MonkeyPatch, git: CodeGit) -> None:
    _integration_script(monkeypatch)

    result = integration(git, UNUSED, B)

    assert result.commits == (type(result.commits[0])(B, "subject"),)
    assert (result.fast_forward, result.ahead, result.behind) == (True, 1, 0)


@pytest.mark.parametrize(
    "answers",
    [
        {"merge-base": f"{A}\n{B}\n"},
        {"merge-base": "xyz\n"},
        {"merge-base": ""},
        {"rev-list": "1 2\n"},
        {"rev-list": "1\t2"},
        {"rev-list": "1\t2\n3\n"},
        {"rev-list": "a\tb\n"},
        {"rev-list": "1\t2\t3\n"},
        {"rev-list": RuntimeError("git rev-list failed")},
        {"log": f"{B}\0subject"},
        {"log": f"{B}\0"},
        {"log": "xyz\0subject\0"},
        {"log": f"{B}\0subject\0" * (MAX_COMMITS + 2)},
        {"log": RuntimeError("git log failed")},
        {"raw": f":100644 100644 {A} {B} R\0f.txt\0"},
    ],
)
def test_integration_refuses_malformed_output(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Answer], git: CodeGit
) -> None:
    _integration_script(monkeypatch, **answers)

    assert _refusal(lambda: integration(git, UNUSED, B)) == "git_failed"


def test_integration_refuses_an_unnameable_branch_before_any_other_call(
    monkeypatch: pytest.MonkeyPatch,
    git: CodeGit,
) -> None:
    _integration_script(monkeypatch, **{"symbolic-ref": "refs/heads/a+b\n"})

    assert _refusal(lambda: integration(git, UNUSED, B)) == "unsupported_branch"
