"""The code reader: real git in `tmp_path`, an injected clock, `os.utime` for ages."""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.app.test_workspaces import service_at
from tests.workspace_code.test_code_git import (
    _commit,  # pyright: ignore[reportPrivateUsage]
    _count_git,  # pyright: ignore[reportPrivateUsage]
    _git,  # pyright: ignore[reportPrivateUsage]
    _repo,  # pyright: ignore[reportPrivateUsage]
    needs_git,
)
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code import code_git
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import Changes, CodeGit
from viki_agent.decision_loop import code_look
from viki_agent.decision_loop.code_look import CodeLookReader
from viki_agent.decision_loop.contracts import CODE_LOOK_EVERY, CodeFolder, CodeLook
from viki_agent.workspaces import WorkspaceService

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DAY = 86400.0
HOUR = timedelta(hours=1)
HANG_GUARD_S = 5  # only for events that fire in milliseconds


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _age(path: Path, days: float) -> datetime:
    """Set `path`'s modification time `days` before NOW; the time as the reader answers it."""
    stamp = NOW.timestamp() - days * DAY
    os.utime(path, (stamp, stamp))
    return datetime.fromtimestamp(stamp, UTC)


def _reader(
    tmp_path: Path, *roots: Path
) -> tuple[CodeLookReader, CodeGit, WorkspaceService, list[str]]:
    service = service_at(tmp_path)
    ids = [trusted(service, root) for root in roots]
    git = CodeGit(service, CodeFiles(service), git_available=lambda: True)
    return CodeLookReader(service, git), git, service, ids


def _tracked(root: Path, *names: str) -> None:
    """Commit each name, then change it, so each is a tracked, modified file."""
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("one\n", encoding="utf-8")
    _commit(root, "first")
    for name in names:
        (root / name).write_text("two\n", encoding="utf-8")


@needs_git
def test_a_folders_tracked_changes_report_their_newest_change(tmp_path: Path) -> None:
    root = _repo(tmp_path / "garden-app")
    _tracked(root, "a.txt", "src/b c.txt", "d.txt")
    _git(root, "mv", "d.txt", "e.txt")
    _age(root / "a.txt", 3)
    newest = _age(root / "src" / "b c.txt", 2)
    _age(root / "e.txt", 5)
    reader, _, _, [workspace_id] = _reader(tmp_path, root)

    assert reader.read(NOW) == CodeLook((CodeFolder(workspace_id, "garden-app", newest),))


@needs_git
def test_untracked_deleted_and_submodule_entries_carry_no_time(tmp_path: Path) -> None:
    root = _repo(tmp_path / "mixed")
    _tracked(root, "a.txt", "gone.txt")
    sub = _repo(root / "sub")
    (sub / "s.txt").write_text("s\n", encoding="utf-8")
    _commit(sub, "sub")
    _git(root, "add", "sub")
    _git(root, "commit", "-q", "-m", "add sub")
    (sub / "s.txt").write_text("s2\n", encoding="utf-8")
    _commit(sub, "sub again")
    (root / "gone.txt").unlink()
    (root / "fresh.txt").write_text("new\n", encoding="utf-8")
    old = _age(root / "a.txt", 4)
    for fresh in (root / "fresh.txt", root / "sub", root / "sub" / "s.txt"):
        _age(fresh, 0)
    only_loose = _repo(tmp_path / "loose")
    _tracked(only_loose, "x.txt")
    _commit(only_loose, "x2")
    (only_loose / "u.txt").write_text("u\n", encoding="utf-8")
    reader, git, _, [mixed_id, loose_id] = _reader(tmp_path, root, only_loose)

    kinds = {entry.path: entry.kind for entry in git.changes(mixed_id).entries}
    assert kinds == {
        "a.txt": "tracked",
        "gone.txt": "tracked",
        "sub": "submodule",
        "fresh.txt": "untracked",
    }
    looks = [reader.read(NOW), reader.read(NOW)]
    assert looks[1].folders == (CodeFolder(mixed_id, "mixed", old),)
    assert loose_id not in [folder.workspace_id for folder in looks[1].folders or ()]
    assert looks[1].failures == ()


@needs_git
def test_a_private_path_a_truncated_status_or_an_outside_path_reads_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for cause in ("private", "truncated", "outside"):
        root = _repo(tmp_path / cause)
        _tracked(root, "a.txt", "b.txt", ".npmrc" if cause == "private" else "c.txt")
        outside = tmp_path / f"{cause}-outside.txt"
        outside.write_text("out\n", encoding="utf-8")
        if cause == "outside":
            try:
                os.symlink(outside, root / "link")
            except OSError, NotImplementedError:
                continue  # this account cannot make links
            _commit(root, "link")
            (root / "link").unlink()
            os.symlink(tmp_path / f"{cause}-other.txt", root / "link")
        for name in ("a.txt", "b.txt"):
            _age(root / name, 3)
        with monkeypatch.context() as patch:
            if cause == "truncated":
                patch.setattr(code_git, "MAX_CHANGES", 1)
            reader, _, _, _ = _reader(tmp_path / f"service-{cause}", root)
            # Ordinary changes beside the bad entry must not stand in for it.
            assert reader.read(NOW) == CodeLook((), ("code_partial",)), cause


@needs_git
def test_one_folder_is_read_per_call_and_each_at_most_every_half_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [_repo(tmp_path / name) for name in ("one", "two")]
    for root in roots:
        _tracked(root, "a.txt")
    _age(roots[0] / "a.txt", 2)
    _age(roots[1] / "a.txt", 3)
    reader, _, _, ids = _reader(tmp_path, *roots)
    calls = _count_git(monkeypatch)

    assert len(reader.read(NOW).folders or ()) == 1 and len(calls) == 2
    both = reader.read(NOW)
    assert len(both.folders or ()) == 2 and len(calls) == 4

    # Both folders are memoized: a change since is not seen, and git does not run.
    newer = _age(roots[0] / "a.txt", 1)
    assert reader.read(NOW + CODE_LOOK_EVERY - timedelta(seconds=1)) == both
    assert len(calls) == 4

    # At the interval one folder is read per call, then none until the next interval.
    reader.read(NOW + CODE_LOOK_EVERY)
    assert len(calls) == 6
    final = reader.read(NOW + CODE_LOOK_EVERY)
    assert len(calls) == 8
    assert reader.read(NOW + CODE_LOOK_EVERY) == final and len(calls) == 8
    assert {folder.workspace_id: folder.newest for folder in final.folders or ()} == {
        ids[0]: newer,
        ids[1]: _age(roots[1] / "a.txt", 3),
    }


@needs_git
def test_the_read_writes_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path / "quiet")
    _tracked(root, "a.txt", "b.txt", "same.txt")
    newest = _age(root / "a.txt", 2)
    _age(root / "b.txt", 5)
    # Same bytes, new stat time: the case in which a status refresh would rewrite the index.
    (root / "same.txt").write_text("one\n", encoding="utf-8")
    _age(root / "same.txt", 6)
    reader, _, _, [workspace_id] = _reader(tmp_path, root)

    def snapshot() -> dict[str, tuple[int, bytes | None]]:
        found: dict[str, tuple[int, bytes | None]] = {}
        for folder, dirs, files in os.walk(root):
            for name in [*dirs, *files]:
                path = Path(folder, name)
                stat = path.lstat()
                body = path.read_bytes() if path.is_file() and not path.is_symlink() else None
                found[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, body)
        return found

    before = snapshot()
    assert before[".git/index"][1]
    assert reader.read(NOW).folders == (CodeFolder(workspace_id, "quiet", newest),)
    assert snapshot() == before


@pytest.mark.parametrize("failure", ["git", "other", "unreadable"])
@needs_git
def test_a_failed_read_lists_the_folder_as_unknown_and_logs_no_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    root = _repo(tmp_path / "secret-folder")
    _tracked(root, "a.txt")
    reader, git, _, _ = _reader(tmp_path, root)

    def broken(*_args: object, **_kwargs: object) -> object:
        if failure == "other":
            raise ValueError(f"boom in {root}")
        return Changes(state="unknown")

    if failure == "git":

        def refuse(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError(f"boom in {root}")

        monkeypatch.setattr(winproc, "git", refuse)
    else:
        monkeypatch.setattr(git, "changes", broken)

    with caplog.at_level(logging.WARNING):
        look = reader.read(NOW)

    assert look == CodeLook((), ("code_partial",))
    ours = [r for r in caplog.records if r.name == code_look.__name__]
    assert len(ours) == 1 and ours[0].getMessage().startswith("decision code look failed: ")
    text = " ".join(record.getMessage() for record in caplog.records)
    assert str(root) not in text and "boom" not in text


@needs_git
def test_a_folder_that_is_not_a_repository_is_a_fact_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    reader, _, _, _ = _reader(tmp_path, plain)

    with caplog.at_level(logging.WARNING):
        assert reader.read(NOW) == CodeLook(())

    assert caplog.records == []


@needs_git
def test_untrusted_and_removed_folders_run_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, removed, untrusted = (_repo(tmp_path / name) for name in ("kept", "removed", "untrusted"))
    for root in (kept, removed, untrusted):
        _tracked(root, "a.txt")
        _age(root / "a.txt", 2)
    reader, _, service, [kept_id, removed_id] = _reader(tmp_path, kept, removed)
    service.register_existing("11111111-1111-4111-8111-111111111111", str(untrusted), kind="code")
    calls = _count_git(monkeypatch)

    for _ in range(3):
        reader.read(NOW)
    assert len(calls) == 4  # two trusted folders, rev-parse and status each
    service.remove(removed_id)
    after = reader.read(NOW + CODE_LOOK_EVERY)

    assert [folder.workspace_id for folder in after.folders or ()] == [kept_id]
    assert len(calls) == 6  # only the kept folder, once more


@needs_git
def test_an_untrusted_folder_is_not_eligible_and_is_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The reader's own filter: CodeGit would refuse the folder, which would read as a failure."""
    untrusted = _repo(tmp_path / "untrusted")
    _tracked(untrusted, "a.txt")
    reader, _, service, _ = _reader(tmp_path)
    service.register_existing("11111111-1111-4111-8111-111111111111", str(untrusted), kind="code")

    with caplog.at_level(logging.WARNING):
        looks = [reader.read(NOW), reader.read(NOW + CODE_LOOK_EVERY)]

    assert looks == [CodeLook(()), CodeLook(())]
    assert caplog.records == []


def test_a_list_failure_answers_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reader, _, service, _ = _reader(tmp_path)

    def broken(**_kwargs: object) -> list[object]:
        raise OSError(f"cannot list {tmp_path}")

    monkeypatch.setattr(service, "list", broken)
    with caplog.at_level(logging.WARNING):
        assert reader.read(NOW) == CodeLook(None, ("code_unknown",))

    assert [record.getMessage() for record in caplog.records] == [
        "decision code look failed: OSError"
    ]


def _hours_old(path: Path, hours: float) -> datetime:
    return _age(path, hours / 24)


@needs_git
def test_a_folder_committed_before_a_sleep_is_not_listed_from_its_old_read(
    tmp_path: Path,
) -> None:
    roots = [_repo(tmp_path / name) for name in ("one", "two")]
    for root in roots:
        _tracked(root, "a.txt")
        _hours_old(root / "a.txt", 20)
    reader, _, _, ids = _reader(tmp_path, *roots)
    by_id = dict(zip(ids, roots, strict=True))
    first, second = sorted(ids)
    reader.read(NOW)
    reader.read(NOW + timedelta(minutes=5))
    _commit(by_id[second], "second")  # the user commits, then the laptop sleeps
    wake = NOW + 10 * HOUR

    woken = reader.read(wake)

    # The first folder's idle crossing is pending, so its read comes first; the second folder's
    # ten-hour-old read is not a basis for a look.
    assert [folder.workspace_id for folder in woken.folders or ()] == [first]
    assert [folder.workspace_id for folder in reader.read(wake).folders or ()] == [first]


@needs_git
def test_a_folder_edited_before_its_crossing_is_not_listed_from_the_old_read(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "busy")
    _tracked(root, "a.txt")
    _hours_old(root / "a.txt", 23 + 50 / 60)
    reader, _, _, _ = _reader(tmp_path, root)
    assert reader.read(NOW) == CodeLook(())
    edited = NOW + timedelta(minutes=2)
    os.utime(root / "a.txt", (edited.timestamp(), edited.timestamp()))

    # The memo's change is now a day old, but the read that made it saw the change still fresh.
    assert reader.read(NOW + timedelta(minutes=11)) == CodeLook(())
    assert reader.read(NOW + CODE_LOOK_EVERY) == CodeLook(())


@needs_git
def test_an_unedited_folder_is_listed_from_the_fresh_read_that_saw_it_idle(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "quiet")
    _tracked(root, "a.txt")
    newest = _hours_old(root / "a.txt", 23 + 50 / 60)
    reader, _, _, [workspace_id] = _reader(tmp_path, root)
    assert reader.read(NOW) == CodeLook(())

    assert reader.read(NOW + CODE_LOOK_EVERY) == CodeLook(
        (CodeFolder(workspace_id, "quiet", newest),)
    )


@needs_git
def test_a_read_older_than_the_interval_does_not_list_its_folder(tmp_path: Path) -> None:
    roots = [_repo(tmp_path / name) for name in ("one", "two")]
    for root in roots:
        _tracked(root, "a.txt")
        _age(root / "a.txt", 3)
    reader, _, _, ids = _reader(tmp_path, *roots)
    reader.read(NOW)
    assert len(reader.read(NOW).folders or ()) == 2

    late = NOW + CODE_LOOK_EVERY + timedelta(seconds=1)
    assert [folder.workspace_id for folder in reader.read(late).folders or ()] == [min(ids)]
    assert len(reader.read(late).folders or ()) == 2


@needs_git
def test_a_folder_whose_crossing_is_pending_is_read_before_other_due_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [_repo(tmp_path / name) for name in ("one", "two", "three")]
    for root in roots:
        _tracked(root, "a.txt")
    reader, git, _, ids = _reader(tmp_path, *roots)
    by_id = dict(zip(ids, roots, strict=True))
    early, middle, late = sorted(ids)
    _hours_old(by_id[early] / "a.txt", 2)
    _hours_old(by_id[middle] / "a.txt", 2)
    _hours_old(by_id[late] / "a.txt", 23)
    for minute in range(3):
        reader.read(NOW + timedelta(minutes=minute))
    read: list[str] = []
    real = git.changes

    def recording(workspace_id: str) -> Changes:
        read.append(workspace_id)
        return real(workspace_id)

    monkeypatch.setattr(git, "changes", recording)
    now = NOW + timedelta(minutes=90)  # the third folder's change turns a day old

    listed = reader.read(now)
    assert read == [late]
    assert [folder.workspace_id for folder in listed.folders or ()] == [late]

    reader.read(now)
    reader.read(now)
    assert read == [late, early, middle]  # then the rest, oldest read first


@needs_git
def test_a_failed_re_read_does_not_list_the_folder_from_its_earlier_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "flaky")
    _tracked(root, "a.txt")
    _age(root / "a.txt", 3)
    reader, git, _, _ = _reader(tmp_path, root)
    assert len(reader.read(NOW).folders or ()) == 1

    monkeypatch.setattr(git, "changes", lambda *_args, **_kwargs: Changes(state="unknown"))

    assert reader.read(NOW + CODE_LOOK_EVERY) == CodeLook((), ("code_partial",))


def test_a_second_read_waits_for_a_read_still_inside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted cycle's read can still run as the next cycle reads."""
    folders = [tmp_path / name for name in ("one", "two")]
    for folder in folders:
        folder.mkdir()
    reader, git, _, ids = _reader(tmp_path, *folders)
    inside, second_inside, release = threading.Event(), threading.Event(), threading.Event()
    read: list[str] = []

    def held(workspace_id: str) -> Changes:
        read.append(workspace_id)
        if len(read) == 1:
            inside.set()
            release.wait(HANG_GUARD_S)
        else:
            second_inside.set()
        return Changes(state="ready")

    monkeypatch.setattr(git, "changes", held)
    threads = [threading.Thread(target=reader.read, args=(NOW,)) for _ in range(2)]
    threads[0].start()
    assert inside.wait(HANG_GUARD_S)
    threads[1].start()
    # Without the lock the second read starts at once, on the folder the first still holds.
    second_inside.wait(0.05)
    held_read = list(read)
    release.set()
    for thread in threads:
        thread.join(HANG_GUARD_S)

    assert held_read == [min(ids)]
    assert read == sorted(ids)
