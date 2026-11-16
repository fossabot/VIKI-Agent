"""The review of a checkout, against real repositories: each change and kind, the digests a commit
compares, the caps, the integration and remote fields, and the view of one path."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Callable, Collection
from pathlib import Path

import pytest

from tests.workspace_code.test_code_checkout import BRANCH, Rig, _head, _rig
from tests.workspace_code.test_code_git import _commit, _git, _repo, needs_git
from viki_agent import osfs, winproc
from viki_agent.code import code_review
from viki_agent.code.code_files import CodeError, FileText
from viki_agent.code.code_review import (
    MAX_REVIEW_ENTRIES,
    CodeReview,
    Entries,
    ReviewEntry,
    ReviewView,
    digest,
)

# built at run time so the fixture guard, `tests/test_fixtures_clean.py`, does not read these
# remote URLs as e-mail addresses
AT = "@"

BIG = "line\n" * 230_000  # 1.1 MiB


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable (each turns the gate into `git_unavailable`), and an empty
    home, so the user's global Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


def _review(rig: Rig) -> CodeReview:
    return CodeReview(rig.store, rig.git)


def _found(
    rig: Rig, worktree_id: str | None = None, *, only: Collection[str] | None = None
) -> Entries:
    return _review(rig).entries(rig.checkout(worktree_id), only=only)


def _entries(rig: Rig, worktree_id: str | None = None) -> dict[str, ReviewEntry]:
    return {entry.path: entry for entry in _found(rig, worktree_id).items}


def _kinds(rig: Rig) -> dict[str, tuple[str, str]]:
    return {path: (entry.change, entry.kind) for path, entry in _entries(rig).items()}


def _blob(root: Path, path: str) -> str:
    return _git(root, "rev-parse", f"HEAD:{path}").strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_object(root: Path, text: str) -> str:
    source = root.parent / "blob-source"
    source.write_text(text, encoding="utf-8")
    return _git(root, "hash-object", "-w", str(source)).strip()


def _link_dir(target: Path, link: Path) -> None:
    """A directory link: a junction on Windows, where a symlink needs a privilege."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _count_git(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def counting(root: Path, *args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", counting)
    return calls


# --- the digest ---------------------------------------------------------------------------------


def test_digest_is_the_sha256_of_path_head_blob_and_worktree_bytes() -> None:
    blob, sha = "b" * 40, "c" * 64
    assert (
        digest("a.txt", blob, sha) == hashlib.sha256(f"a.txt\0{blob}\0{sha}".encode()).hexdigest()
    )
    assert (
        digest("ä/a.txt", None, None) == hashlib.sha256("ä/a.txt\0-\0absent".encode()).hexdigest()
    )


@needs_git
def test_a_digest_follows_the_worktree_bytes(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    target = rig.folder / "a.txt"
    blob = _blob(rig.folder, "a.txt")
    target.write_text("changed\n", encoding="utf-8")
    first = _entries(rig)["a.txt"]
    assert first == ReviewEntry("a.txt", "modified", "text", digest("a.txt", blob, _sha(target)))

    target.write_text("changed again\n", encoding="utf-8")
    second = _entries(rig)["a.txt"]
    assert second.digest == digest("a.txt", blob, _sha(target))
    assert second.digest != first.digest


@needs_git
def test_modified_added_deleted_and_untracked_text_carry_their_digests(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    folder = rig.folder
    a_blob, b_blob = _blob(folder, "a.txt"), _blob(folder, "dir/b.txt")
    (folder / "a.txt").write_text("alpha 2\n", encoding="utf-8")
    (folder / "dir" / "b.txt").unlink()
    (folder / "added.txt").write_text("added\n", encoding="utf-8")
    _git(folder, "add", "--", "added.txt")
    (folder / "new.txt").write_text("new\n", encoding="utf-8")

    found = _found(rig)

    assert found.items == (
        ReviewEntry("a.txt", "modified", "text", digest("a.txt", a_blob, _sha(folder / "a.txt"))),
        ReviewEntry(
            "added.txt", "added", "text", digest("added.txt", None, _sha(folder / "added.txt"))
        ),
        ReviewEntry("dir/b.txt", "deleted", "text", digest("dir/b.txt", b_blob, None)),
        ReviewEntry("new.txt", "added", "text", digest("new.txt", None, _sha(folder / "new.txt"))),
    )
    assert (found.truncated, found.unmerged) == (False, False)


@needs_git
def test_a_clean_checkout_has_nothing_to_review(tmp_path: Path) -> None:
    assert _found(_rig(tmp_path)) == Entries((), False, False)


# --- every kind ---------------------------------------------------------------------------------


@needs_git
def test_every_kind_in_one_checkout(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    folder = rig.folder
    # Git for Windows refuses the name `aux.c` unless NTFS protection is off.
    _git(folder, "config", "core.protectNTFS", "false")
    (folder / "big.txt").write_text(BIG, encoding="utf-8")
    for name in ("bin.txt", "staged.txt", "kept.txt", "f.lfs"):
        (folder / name).write_text(f"{name}\n", encoding="utf-8")
    (folder / "tree").mkdir()
    (folder / "tree" / "x.txt").write_text("x\n", encoding="utf-8")
    (folder / ".gitattributes").write_text("*.lfs filter=viki-test\n", encoding="utf-8")
    _commit(folder, "kinds")
    head = _head(folder)
    x_blob = _blob(folder, "tree/x.txt")

    nested = _repo(folder / "nested")
    (nested / "n.txt").write_text("n\n", encoding="utf-8")
    _commit(nested, "nested")
    (folder / ".env").write_text("K=v\n", encoding="utf-8")
    blob = _hash_object(folder, "target")
    _git(folder, "update-index", "--add", "--cacheinfo", f"100644,{blob},aux.c")
    _git(folder, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
    _git(folder, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    _git(folder, "rm", "-q", "--cached", "--", "kept.txt")
    (folder / "staged.txt").write_text("staged 2\n", encoding="utf-8")
    _git(folder, "add", "--", "staged.txt")
    (folder / "staged.txt").write_text("staged.txt\n", encoding="utf-8")
    (folder / "gone.txt").write_text("gone\n", encoding="utf-8")
    _git(folder, "add", "--", "gone.txt")
    (folder / "gone.txt").unlink()
    (folder / "tree" / "x.txt").unlink()
    (folder / "tree").rmdir()
    (folder / "tree").write_text("a file now\n", encoding="utf-8")
    (folder / "big.txt").write_text("small\n", encoding="utf-8")
    (folder / "bin.txt").write_bytes(b"a\0b\n")
    (folder / "ubin.dat").write_bytes(b"a\0b")
    (folder / "enc.txt").write_bytes(b"\x80abc")
    (folder / "huge.txt").write_text(BIG, encoding="utf-8")
    (folder / "f.lfs").write_text("pointer 2\n", encoding="utf-8")
    (folder / "new.lfs").write_text("pointer\n", encoding="utf-8")
    (folder / "ok.txt").write_text("ok\n", encoding="utf-8")

    found = _entries(rig)

    assert {path: (entry.change, entry.kind) for path, entry in found.items()} == {
        "nested/": ("added", "nested_repository"),
        ".env": ("added", "private"),
        "aux.c": ("added", "unsupported_name"),
        "sub": ("added", "submodule"),
        "link": ("added", "symlink"),
        "kept.txt": ("modified", "other"),  # removed from the index, left on disk
        "staged.txt": ("modified", "other"),  # a staged change reverted on disk
        "gone.txt": ("added", "other"),  # neither in HEAD nor on disk
        "tree": ("modified", "other"),  # a tree in HEAD
        "tree/x.txt": ("deleted", "text"),
        "big.txt": ("modified", "too_large"),  # the HEAD blob
        "bin.txt": ("modified", "binary"),  # numstat `-`
        "ubin.dat": ("added", "binary"),
        "enc.txt": ("added", "binary"),  # unsupported_encoding
        "huge.txt": ("added", "too_large"),  # on disk
        "f.lfs": ("modified", "filtered"),
        "new.lfs": ("added", "filtered"),
        "ok.txt": ("added", "text"),
    }
    assert found["tree/x.txt"].digest == digest("tree/x.txt", x_blob, None)
    assert {path for path, entry in found.items() if entry.digest is not None} == {
        "tree/x.txt",
        "ok.txt",
    }


@needs_git
def test_a_conflicted_merge_is_unmerged_and_in_progress(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    folder = rig.folder
    _git(folder, "switch", "-q", "-c", "side")
    (folder / "a.txt").write_text("side\n", encoding="utf-8")
    _commit(folder, "side")
    _git(folder, "switch", "-q", "main")
    (folder / "a.txt").write_text("main\n", encoding="utf-8")
    _commit(folder, "main")
    assert winproc.run(["git", "merge", "side"], cwd=folder).returncode != 0

    found = _found(rig)
    assert [(entry.path, entry.kind, entry.digest) for entry in found.items] == [
        ("a.txt", "unmerged", None)
    ]
    assert found.unmerged is True
    assert _review(rig).review(rig.workspace_id, None).in_progress is True


@needs_git
def test_a_link_on_disk_is_a_symlink(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    folder = rig.folder
    elsewhere = rig.short / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b.txt").write_text("beta\n", encoding="utf-8")
    (elsewhere / "t.txt").write_text("t\n", encoding="utf-8")
    shutil.rmtree(folder / "dir")
    _link_dir(elsewhere, folder / "dir")  # a tracked folder replaced by a link
    _link_dir(elsewhere, folder / "jn")  # an untracked link

    found = _kinds(rig)

    linked = {path: kind for path, kind in found.items() if path.split("/")[0] in ("dir", "jn")}
    assert {path.split("/")[0] for path in linked} == {"dir", "jn"}
    assert {kind for _, kind in linked.values()} == {"symlink"}
    if sys.platform != "win32":  # POSIX lists the replaced tracked folder as its own path
        assert found["dir"] == ("modified", "symlink")


@needs_git
@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        (CodeError("private_path"), "private"),
        (CodeError("invalid_path"), "other"),
        (CodeError("not_a_file"), "other"),
        (CodeError("not_found"), "other"),
        (FileText("new.txt", 2_000_000, None, None, None, None, "too_large"), "too_large"),
        (FileText("new.txt", 3, "e" * 64, None, None, None, "binary"), "binary"),
        (FileText("new.txt", 3, "e" * 64, None, None, None, "unsupported_encoding"), "binary"),
        (FileText("new.txt", 3, "e" * 64, "utf-8", "lf", "ab\n", None), "text"),
    ],
)
def test_the_readers_outcomes_map_to_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: CodeError | FileText,
    kind: str,
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "new.txt").write_text("ab\n", encoding="utf-8")

    def read(
        root: Path,
        ref: str,
        *,
        max_bytes: int,
        on_read: Callable[[int], None],
    ) -> FileText:
        if isinstance(outcome, CodeError):
            raise outcome
        return outcome

    monkeypatch.setattr(code_review, "read_at", read)
    entry = _entries(rig)["new.txt"]
    assert entry.kind == kind
    assert entry.digest == (digest("new.txt", None, "e" * 64) if kind == "text" else None)


@needs_git
def test_an_unavailable_read_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "new.txt").write_text("ab\n", encoding="utf-8")

    def read(
        root: Path,
        ref: str,
        *,
        max_bytes: int,
        on_read: Callable[[int], None],
    ) -> FileText:
        raise CodeError("unavailable")

    monkeypatch.setattr(code_review, "read_at", read)
    with pytest.raises(CodeError) as refused:
        _found(rig)
    assert refused.value.code == "unavailable"


@needs_git
def test_malformed_status_output_is_git_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    checkout = rig.checkout()
    monkeypatch.setattr(rig.git, "git", lambda root, *args, timeout=5.0: "1 bad")
    with pytest.raises(CodeError) as refused:
        _review(rig).entries(checkout)
    assert refused.value.code == "git_failed"


# --- caps ---------------------------------------------------------------------------------------


@needs_git
def test_501_changes_list_500_and_only_lists_exactly_the_asked_paths(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    for index in range(MAX_REVIEW_ENTRIES + 1):
        (rig.folder / f"f{index:03d}.txt").write_text("x\n", encoding="utf-8")

    found = _found(rig)
    assert (len(found.items), found.truncated) == (500, True)
    assert found.items[0].path == "f000.txt"
    assert "f500.txt" not in {entry.path for entry in found.items}

    asked = _found(rig, only={"f500.txt", "f001.txt", "a.txt", "nope.txt"})
    assert [entry.path for entry in asked.items] == ["f001.txt", "f500.txt"]
    assert {entry.kind for entry in asked.items} == {"text"}
    assert asked.truncated is False


@needs_git
def test_reads_stop_at_the_review_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "huge.txt").write_text(BIG, encoding="utf-8")
    for name in ("r1.txt", "r2.txt", "r3.txt"):
        (rig.folder / name).write_text("0123456789", encoding="utf-8")
    monkeypatch.setattr(code_review, "MAX_REVIEW_READ_BYTES", 25)
    read: list[str] = []
    real = osfs.read_regular

    def spy(
        path: str | os.PathLike[str],
        limit: int,
        *,
        follow_symlinks: bool = True,
        pre_read: Callable[[int], None] | None = None,
    ) -> bytes:
        read.append(Path(path).name)
        return real(path, limit, follow_symlinks=follow_symlinks, pre_read=pre_read)

    monkeypatch.setattr(osfs, "read_regular", spy)

    assert _kinds(rig) == {
        "huge.txt": ("added", "too_large"),
        "r1.txt": ("added", "text"),
        "r2.txt": ("added", "text"),
        "r3.txt": ("added", "too_large"),
    }
    assert read == ["r1.txt", "r2.txt"]


@needs_git
def test_the_review_charges_actual_bytes_after_growth_and_stops_after_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    for name in ("r1.txt", "r2.txt", "r3.txt", "r4.txt"):
        (rig.folder / name).write_bytes(b"x")
    monkeypatch.setattr(code_review, "MAX_REVIEW_READ_BYTES", 25)
    real = osfs.read_regular
    consumed: list[int] = []

    def grow(
        path: str | os.PathLike[str],
        limit: int,
        *,
        follow_symlinks: bool = True,
        pre_read: Callable[[int], None] | None = None,
    ) -> bytes:
        Path(path).write_bytes(b"0123456789")
        if pre_read is None:
            data = real(path, limit, follow_symlinks=follow_symlinks)
        else:
            data = real(path, limit, follow_symlinks=follow_symlinks, pre_read=pre_read)
        consumed.append(len(data))
        return data

    monkeypatch.setattr(osfs, "read_regular", grow)
    entries = _entries(rig)
    assert {path: entry.kind for path, entry in entries.items()} == {
        "r1.txt": "text",
        "r2.txt": "text",
        "r3.txt": "too_large",
        "r4.txt": "too_large",
    }
    assert consumed == [10, 10, 6]  # only the final growth-detection byte exceeds the budget
    assert entries["r3.txt"].digest is None and entries["r4.txt"].digest is None
    assert entries["r1.txt"].digest == digest(
        "r1.txt", None, hashlib.sha256(b"0123456789").hexdigest()
    )


@needs_git
def test_a_stat_only_budget_refusal_does_not_charge_unread_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    for name, data in (("r1.txt", b"x"), ("r2.txt", b"ab"), ("r3.txt", b"cde")):
        (rig.folder / name).write_bytes(data)
    monkeypatch.setattr(code_review, "MAX_REVIEW_READ_BYTES", 5)
    real = code_review._on_disk

    def grow(root: Path, path: str) -> tuple[os.stat_result | None, bool]:
        info, linked = real(root, path)
        if path == "r1.txt":
            (root / path).write_bytes(b"0123456789")
        return info, linked

    monkeypatch.setattr(code_review, "_on_disk", grow)
    assert _kinds(rig) == {
        "r1.txt": ("added", "too_large"),
        "r2.txt": ("added", "text"),
        "r3.txt": ("added", "text"),
    }


@needs_git
@pytest.mark.parametrize("raw", [b"a\0b", b"\xffab"])
def test_binary_and_decode_failures_still_charge_the_review_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "r1.txt").write_bytes(b"x")
    (rig.folder / "r2.txt").write_bytes(b"abc")
    (rig.folder / "r3.txt").write_bytes(b"de")
    monkeypatch.setattr(code_review, "MAX_REVIEW_READ_BYTES", 5)
    real = code_review._on_disk

    def grow(root: Path, path: str) -> tuple[os.stat_result | None, bool]:
        info, linked = real(root, path)
        if path == "r1.txt":
            (root / path).write_bytes(raw)
        return info, linked

    monkeypatch.setattr(code_review, "_on_disk", grow)
    assert _kinds(rig) == {
        "r1.txt": ("added", "binary"),
        "r2.txt": ("added", "too_large"),
        "r3.txt": ("added", "text"),
    }


# --- the review ---------------------------------------------------------------------------------


@needs_git
def test_a_worktrees_review_carries_its_integration_and_the_folders_none(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id, path = rig.worktree()
    (path / "a.txt").write_text("worktree\n", encoding="utf-8")
    _commit(path, "in the worktree")
    (path / "w.txt").write_text("uncommitted\n", encoding="utf-8")
    review = _review(rig)

    found = review.review(rig.workspace_id, worktree_id)

    assert (found.worktree_id, found.branch, found.head_oid) == (worktree_id, BRANCH, _head(path))
    assert [(entry.path, entry.kind) for entry in found.entries] == [("w.txt", "text")]
    assert (found.truncated, found.in_progress) == (False, False)
    assert found.integration is not None
    assert (found.integration.target_branch, found.integration.target_oid) == (
        "main",
        _head(rig.folder),
    )
    assert (found.integration.fast_forward, found.integration.ahead) == (True, 1)
    assert [(p.path, p.change, p.kind) for p in found.integration.paths] == [
        ("a.txt", "modified", "text")
    ]
    assert (found.remote, found.remote_state, found.remote_oid) == (None, "none", None)
    assert found.pull_request_url is None

    folder = review.review(rig.workspace_id, None)
    assert (folder.worktree_id, folder.branch, folder.integration) == (None, "main", None)
    assert folder.entries == ()


@needs_git
def test_a_local_bare_origin_is_ready_with_its_tracking_oid_and_no_page(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    bare = rig.short / "origin.git"
    _git(rig.short, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(rig.folder, "remote", "add", "origin", str(bare))
    _git(rig.folder, "push", "-q", "origin", "main")
    worktree_id, path = rig.worktree()
    _git(path, "push", "-q", "origin", BRANCH)
    review = _review(rig)

    folder = review.review(rig.workspace_id, None)
    assert (folder.remote, folder.remote_state, folder.remote_oid) == (
        "origin",
        "ready",
        _head(rig.folder),
    )
    assert folder.pull_request_url is None

    worktree = review.review(rig.workspace_id, worktree_id)
    assert (worktree.remote, worktree.remote_state, worktree.remote_oid) == (
        "origin",
        "ready",
        _head(path),
    )
    assert worktree.pull_request_url is None  # a local path names no host


@needs_git
def test_a_worktree_on_a_known_host_gets_its_pull_request_page(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "remote", "add", "origin", f"https://user:token{AT}github.com/o/r.git")
    worktree_id, _ = rig.worktree()
    review = _review(rig)

    worktree = review.review(rig.workspace_id, worktree_id)
    assert (worktree.remote, worktree.remote_state, worktree.remote_oid) == (
        "origin",
        "ready",
        None,
    )
    assert (
        worktree.pull_request_url == f"https://github.com/o/r/compare/main...{BRANCH}?quick_pull=1"
    )
    assert review.review(rig.workspace_id, None).pull_request_url is None  # the folder's own


@needs_git
def test_a_detached_head_has_no_remote(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "remote", "add", "origin", "https://github.com/o/r.git")
    _git(rig.folder, "switch", "-q", "--detach")
    found = _review(rig).review(rig.workspace_id, None)
    assert (found.branch, found.remote, found.remote_state) == (None, None, "none")


# --- the view -----------------------------------------------------------------------------------


@needs_git
def test_the_view_of_a_tracked_file_is_its_diff(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text("alpha 2\n", encoding="utf-8")
    found = _review(rig).view(rig.workspace_id, "a.txt", None, integration=False)
    assert found.diff is not None and "-alpha\n+alpha 2" in found.diff
    assert (found.path, found.content, found.reason) == ("a.txt", None, None)


@needs_git
def test_the_view_refuses_a_tracked_parent_link_outside_the_checkout(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "dir").rename(rig.short / "original-dir")
    outside = rig.short / "outside"
    outside.mkdir()
    (outside / "b.txt").write_bytes(b"outside synthetic sentinel\n")
    _link_dir(outside, rig.folder / "dir")

    with pytest.raises(CodeError) as refused:
        _review(rig).view(rig.workspace_id, "dir/b.txt", None, integration=False)
    assert refused.value.code == "invalid_path"


@needs_git
def test_the_view_of_a_new_file_is_its_content(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "new.txt").write_bytes(b"new\n")
    found = _review(rig).view(rig.workspace_id, "new.txt", None, integration=False)
    assert found == ReviewView("new.txt", None, "new\n", None)


@needs_git
def test_the_view_of_a_range_path_is_the_diff_between_the_heads(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id, path = rig.worktree()
    (path / "a.txt").write_text("worktree\n", encoding="utf-8")
    _commit(path, "in the worktree")
    (path / "a.txt").write_text("not committed\n", encoding="utf-8")

    found = _review(rig).view(rig.workspace_id, "a.txt", worktree_id, integration=True)

    assert found.diff is not None and "-alpha\n+worktree" in found.diff
    assert "not committed" not in found.diff
    with pytest.raises(ValueError, match="worktree"):
        _review(rig).view(rig.workspace_id, "a.txt", None, integration=True)


@needs_git
def test_a_side_over_1_mib_is_too_large(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.folder / "a.txt").write_text(BIG, encoding="utf-8")
    (rig.folder / "huge.txt").write_text(BIG, encoding="utf-8")
    review = _review(rig)
    assert review.view(rig.workspace_id, "a.txt", None, integration=False) == ReviewView(
        "a.txt", None, None, "too_large"
    )
    assert review.view(rig.workspace_id, "huge.txt", None, integration=False) == ReviewView(
        "huge.txt", None, None, "too_large"
    )


@needs_git
@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("", "invalid_path"),
        ("a/../b", "invalid_path"),
        (".env", "private_path"),
        ("src/.git/config", "private_path"),
    ],
)
def test_the_view_checks_the_ref_before_any_lookup_or_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, code: str
) -> None:
    rig = _rig(tmp_path)
    calls = _count_git(rig, monkeypatch)
    looked_up: list[str] = []
    monkeypatch.setattr(rig.workspaces, "get", lambda *args, **kwargs: looked_up.append("get"))
    with pytest.raises(CodeError) as refused:
        _review(rig).view(rig.workspace_id, path, None, integration=False)
    assert refused.value.code == code
    assert (calls, looked_up) == ([], [])
