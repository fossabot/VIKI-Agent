"""Programs a folder's own Git settings name, in a worktree whose files a Bot wrote.

A relative path in the folder's config (`filter.tidy.clean = sh tools/tidy.sh`) resolves inside
whichever worktree Git runs in, so in a Bot copy it names a file the Bot can edit while the
settings, and their fingerprint, stay as the user left them. Each case edits such a program in
a worktree to touch a marker and runs the app's own Git there: a Bot copy leaves no marker,
and an ordinary worktree, or plain Git in the same tree, leaves one, which proves the program
was one command away. Real Git in synthetic folders."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.workspace_code.test_bot_copy_release import _released
from tests.workspace_code.test_code_checkout import BRANCH, STAMP, WORKTREE, Rig, _head, _rig
from tests.workspace_code.test_code_commit import _approval, _commit
from tests.workspace_code.test_code_git import _commit as _commit_all
from tests.workspace_code.test_code_git import _git, needs_git
from tests.workspace_code.test_code_git_nested import _snapshot
from tests.workspace_code.test_git_guard import isolated_git  # noqa: F401 (autouse: no host config)
from tests.workspace_code.test_session_roots import bot_copy
from tests.workspace_code.test_worktree_sweep import PAST, _age, _archive, _verdicts, baseline
from tests.workspace_code.test_worktree_sweep import Rig as SweepRig
from tests.workspace_code.test_worktree_sweep import _rig as _sweep_rig
from viki_agent import rails
from viki_agent.code import worktree_probe
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import (
    HARDENING,
    MAX_BOT_COPY_SETTINGS,
    CodeGit,
    _asked,
    _bot_copy_options,
    _from_the_copy,
)
from viki_agent.code.code_review import CodeReview
from viki_agent.code.worktrees import Worktree

pytestmark = needs_git

HOOKS = ("-c", "core.hooksPath=hooks")
TRANSPORTS = ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext")
LFS_OFFLINE = ("lfs.url=file:///dev/null/no-download", "lfs.skipdownloaderrors=true")
Settings = Callable[[Path], None]


def _program(path: Path, marker: Path, then: str) -> None:
    """A shell program at `path` that touches `marker`, then runs `then`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n{then}\n".encode())
    path.chmod(0o755)  # Git starts a signing program itself, without a shell


def _folder(tmp_path: Path, settings: Settings) -> Rig:
    """A folder whose own Git settings `settings` wrote before its baseline, read by one CodeGit
    that knows the store's Bot copies, as the server builds it."""
    rig = _rig(tmp_path)
    rig.git = CodeGit(
        rig.workspaces, CodeFiles(rig.workspaces), git_available=lambda: True,
        bot_code_store=rig.store,
    )  # fmt: skip
    settings(rig.folder)
    assert rig.store.insert_bot_code_baseline(
        {
            "workspace_id": rig.workspace_id,
            "git_fingerprint": rig.git.settings_fingerprint(rig.folder),
            "created_at": STAMP,
        }
    )
    return rig


def _tree(rig: Rig, *, bot: bool) -> Path:
    """One worktree of the folder, a Bot copy with `bot`."""
    _, tree = rig.worktree()
    if bot:
        bot_copy(rig.store, rig.workspace_id, WORKTREE)
    return tree


def _tidy(variable: str = "clean") -> Settings:
    """The user's own filter driver over `*.txt`, its program a path inside the work tree."""

    def settings(folder: Path) -> None:
        (folder / "tools").mkdir()
        (folder / "tools" / "tidy.sh").write_bytes(b"#!/bin/sh\ncat\n")
        (folder / ".gitattributes").write_bytes(b"*.txt filter=tidy\n")
        _git(folder, "config", f"filter.tidy.{variable}", "sh tools/tidy.sh")
        _commit_all(folder, "tidy filter")

    return settings


def _touch(path: Path) -> None:
    """The same bytes at another time, so a `status` reads the file through its filter. Without
    it Git reads an unchanged file only while its entry is as new as the index, which a second
    ticking over between a checkout and its index write ends."""
    os.utime(path, ns=(1, 1))


def _off(*settings: str) -> tuple[str, ...]:
    return tuple(part for setting in settings for part in ("-c", setting))


# ---- a filter driver ---------------------------------------------------------------------------


@pytest.mark.parametrize("bot", [True, False])
@pytest.mark.parametrize("step", ["inspect", "review", "view", "snapshot", "commit"])
async def test_a_filter_program_edited_in_a_worktree_runs_only_outside_a_bot_copy(
    tmp_path: Path, step: str, bot: bool
) -> None:
    rig = _folder(tmp_path, _tidy())
    fingerprint = rig.git.settings_fingerprint(rig.folder)
    tree = _tree(rig, bot=bot)
    marker = rig.short / "ran"
    # File edits alone, as a chat makes them without a prompt: no Git command, no setting.
    _program(tree / "tools" / "tidy.sh", marker, "cat")
    (tree / "a.txt").write_text("edited in the tree\n", encoding="utf-8")
    _touch(tree / "dir" / "b.txt")
    review = CodeReview(rig.store, rig.git)

    if step == "inspect":
        facts = worktree_probe.inspect(rig.git, tree)
        # Without its filter, an archive could not hold the changed file as Git would store it.
        assert facts.readable and facts.changes == 2 and facts.filtered is bot
    elif step == "review":
        shown = review.review(rig.workspace_id, WORKTREE)
        assert {entry.path: entry.kind for entry in shown.entries} == {
            "a.txt": "filtered",
            "tools/tidy.sh": "text",
        }
    elif step == "view":
        shown_diff = review.view(rig.workspace_id, "a.txt", WORKTREE, integration=False).diff
        assert shown_diff is not None and "+edited in the tree" in shown_diff
    elif step == "snapshot":
        assert _snapshot(rig) == (_head(tree), 1)
    else:
        approval = _approval(rig, "tools/tidy.sh", worktree_id=WORKTREE)
        result = await _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE)
        assert _head(tree) == result.head_oid

    assert rig.git.settings_fingerprint(rig.folder) == fingerprint
    assert marker.exists() is not bot


async def _cleanup_tree(
    tmp_path: Path, *, bot: bool, variable: str = "clean"
) -> tuple[SweepRig, Worktree]:
    """The cleanup's rig over a folder with the tidy driver: a released Bot copy with `bot`, else
    an ordinary worktree."""
    rig = _sweep_rig(tmp_path)
    _tidy(variable)(rig.repo)
    baseline(rig)
    return rig, await _released(rig) if bot else await rig.create()


@pytest.mark.parametrize("bot", [True, False])
@pytest.mark.parametrize("act", ["plan", "sweep", "remove"])
async def test_the_cleanup_runs_no_filter_program_of_a_bot_copy_and_keeps_what_it_cannot_archive(
    tmp_path: Path, act: str, bot: bool
) -> None:
    rig, worktree = await _cleanup_tree(tmp_path, bot=bot)
    tree = Path(worktree.path)
    marker = tmp_path / "ran"
    _program(tree / "tools" / "tidy.sh", marker, "cat")
    (tree / "a.txt").write_text("edited in the tree\n", encoding="utf-8")
    if not bot:
        _age(tree, PAST)  # idle long enough to be swept; a released copy skips that wait
    marker.unlink(missing_ok=True)  # the setup's own Git ran the filter

    if act == "remove":
        workspace = rig.workspaces.get(rig.workspace_id, kind="code")
        removal = await rig.service.remove(
            worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
        )
        assert (removal.outcome, removal.reason, removal.changes) == ("kept", "dirty", 2)
    else:
        decisions = await (rig.service.plan() if act == "plan" else rig.service.sweep())
        # An archive made without the filter would not hold `a.txt` as Git stores it: kept.
        verdict = ("keep", "filtered") if bot else ("archive", "dirty")
        assert _verdicts(decisions) == {worktree.worktree_id: verdict}
        archived = act == "sweep" and not bot
        assert tree.exists() is not archived
        assert bool(_archive(rig, worktree.worktree_id)) is archived

    assert marker.exists() is not bot


@pytest.mark.parametrize("bot", [True, False])
async def test_removing_a_clean_bot_copy_runs_no_filter_program_of_its_own(
    tmp_path: Path, bot: bool
) -> None:
    """`git worktree remove` checks the tree with a `status` of its own, started inside it."""
    rig, worktree = await _cleanup_tree(tmp_path, bot=bot)
    tree = Path(worktree.path)
    marker = tmp_path / "ran"
    _program(tree / "tools" / "tidy.sh", marker, "cat")
    _git(tree, "-c", "filter.tidy.clean=", "commit", "-q", "-a", "-m", "work in the tree")
    _touch(tree / "a.txt")
    assert not marker.exists()

    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    removal = await rig.service.remove(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )

    assert removal.outcome == "removed" and not tree.exists()
    assert marker.exists() is not bot


@pytest.mark.parametrize(
    ("variable", "changed", "verdict"),
    [
        ("clean", True, ("archive", "dirty")),  # nothing changed is under the driver
        ("smudge", True, ("keep", "filtered")),  # a checkout of the archive would skip the driver
        ("smudge", False, ("remove", "merged")),  # a removal stores nothing
    ],
)
async def test_a_released_copy_is_archived_only_when_the_archive_needs_no_filter(
    tmp_path: Path, variable: str, changed: bool, verdict: tuple[str, str]
) -> None:
    rig, worktree = await _cleanup_tree(tmp_path, bot=True, variable=variable)
    tree = Path(worktree.path)
    marker = tmp_path / "ran"
    if changed:
        _program(tree / "tools" / "tidy.sh", marker, "cat")
        (tree / "notes.md").write_text("notes\n", encoding="utf-8")

    decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == {worktree.worktree_id: verdict}
    assert tree.exists() is (verdict[0] == "keep")
    archive = _archive(rig, worktree.worktree_id)
    assert bool(archive) is (verdict[0] == "archive")
    if archive:
        assert _git(rig.repo, "show", f"{archive}:notes.md") == "notes\n"
    assert not marker.exists()


@pytest.mark.parametrize("bot", [True, False])
async def test_restoring_an_archive_runs_no_filter_program_of_a_bot_copy(
    tmp_path: Path, bot: bool
) -> None:
    """A checkout runs a driver's smudge command on each file it writes, by then with the
    archive's own version of the program back on disk."""
    rig, worktree = await _cleanup_tree(tmp_path, bot=bot)
    tree = Path(worktree.path)
    marker = tmp_path / "ran"
    _program(tree / "tools" / "tidy.sh", marker, "cat")
    # Sorted after `tools/`: the checkout writes it once the program is there.
    (tree / "zz.txt").write_text("under the driver\n", encoding="utf-8")
    _git(tree, "-c", "filter.tidy.clean=", "add", "zz.txt")
    _git(tree, "-c", "filter.tidy.clean=", "commit", "-q", "-m", "work in the tree")
    if not bot:
        _age(tree, PAST)
    decisions = await (rig.service.release_copies() if bot else rig.service.sweep())
    assert _verdicts(decisions) == {worktree.worktree_id: ("archive", "dirty")}
    # The user gives the driver a smudge command and confirms the folder's settings.
    reviewed = rig.git.settings_fingerprint(rig.repo)
    _git(rig.repo, "config", "filter.tidy.smudge", "sh tools/tidy.sh")
    assert rig.store.replace_bot_code_baseline(
        rig.workspace_id, reviewed, rig.git.settings_fingerprint(rig.repo), STAMP
    )
    marker.unlink(missing_ok=True)  # the setup's own Git ran the filter

    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    restored = await rig.service.restore(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )

    assert restored.state == "ready"
    assert (tree / "zz.txt").read_text(encoding="utf-8") == "under the driver\n"
    assert marker.exists() is not bot


@pytest.mark.parametrize("bot", [True, False])
def test_the_status_bars_git_chip_runs_no_filter_program_of_a_bot_copy(
    tmp_path: Path, bot: bool
) -> None:
    """The server reads a code folder's chip with `HARDENING` and `CodeGit.operation`, and a copy
    can be registered as a code folder of its own."""
    rig = _folder(tmp_path, _tidy())
    tree = _tree(rig, bot=bot)
    marker = rig.short / "ran"
    _program(tree / "tools" / "tidy.sh", marker, "cat")
    (tree / "a.txt").write_text("edited in the tree\n", encoding="utf-8")
    _touch(tree / "dir" / "b.txt")

    chip = rails.git_chip(tree, hardening=HARDENING, operation=rig.git.operation)

    assert (chip.state, chip.dirty) == ("known", 2)
    assert marker.exists() is not bot


# ---- a diff program, an fsmonitor hook, a signing program ---------------------------------------


def _differ(key: str) -> Settings:
    def settings(folder: Path) -> None:
        (folder / "tools").mkdir()
        (folder / "tools" / "show.sh").write_bytes(b'#!/bin/sh\ncat "$1"\n')
        (folder / ".gitattributes").write_bytes(b"*.txt diff=tool\n")
        _git(folder, "config", key, "sh tools/show.sh")
        _commit_all(folder, "diff program")

    return settings


@pytest.mark.parametrize("key", ["diff.tool.textconv", "diff.tool.command", "diff.external"])
def test_no_diff_program_runs_for_a_review_or_a_file_view(tmp_path: Path, key: str) -> None:
    rig = _folder(tmp_path, _differ(key))
    tree = _tree(rig, bot=True)
    marker = rig.short / "ran"
    _program(tree / "tools" / "show.sh", marker, 'cat "$1"')
    (tree / "a.txt").write_text("edited in the tree\n", encoding="utf-8")
    review = CodeReview(rig.store, rig.git)

    shown = review.review(rig.workspace_id, WORKTREE)
    assert {entry.path: entry.kind for entry in shown.entries} == {
        "a.txt": "text",
        "tools/show.sh": "text",
    }
    shown_diff = review.view(rig.workspace_id, "a.txt", WORKTREE, integration=False).diff
    assert shown_diff is not None and "+edited in the tree" in shown_diff
    assert not marker.exists()

    _git(tree, "diff", "HEAD", "--", "a.txt")  # Git's default, without the app's diff options
    assert marker.exists()


def _watcher(folder: Path) -> None:
    (folder / "tools").mkdir()
    (folder / "tools" / "watch.sh").write_bytes(b"#!/bin/sh\nexit 1\n")
    _git(folder, "config", "core.fsmonitor", "sh tools/watch.sh")
    _commit_all(folder, "fsmonitor hook")


def test_no_fsmonitor_hook_runs_for_a_look_at_a_bot_copy(tmp_path: Path) -> None:
    rig = _folder(tmp_path, _watcher)
    tree = _tree(rig, bot=True)
    marker = rig.short / "ran"
    _program(tree / "tools" / "watch.sh", marker, "exit 1")

    assert worktree_probe.inspect(rig.git, tree).changes == 1
    assert CodeReview(rig.store, rig.git).review(rig.workspace_id, WORKTREE).entries
    assert not marker.exists()

    _git(tree, "status", "--porcelain")  # Git's default, without the app's own options
    assert marker.exists()


def _signer(use: str) -> Settings:
    """The user's own signing program, a path inside the work tree, and the setting `use` that
    makes Git start it."""

    def settings(folder: Path) -> None:
        (folder / "tools").mkdir()
        (folder / "tools" / "sign").write_bytes(b"#!/bin/sh\nexit 1\n")
        _git(folder, "config", "gpg.program", "tools/sign")
        _commit_all(folder, "signing program")
        _git(folder, "config", use, "true")

    return settings


@pytest.mark.parametrize("bot", [True, False])
async def test_a_signing_program_inside_a_bot_copy_is_not_run_and_the_commit_is_refused(
    tmp_path: Path, bot: bool
) -> None:
    rig = _folder(tmp_path, _signer("commit.gpgsign"))
    tree = _tree(rig, bot=bot)
    marker = rig.short / "ran"
    _program(tree / "tools" / "sign", marker, "exit 1")
    (tree / "a.txt").write_text("edited in the tree\n", encoding="utf-8")
    before = _head(tree)

    approval = _approval(rig, "a.txt", worktree_id=WORKTREE)
    with pytest.raises(CodeError, match="^commit_refused$"):
        await _commit(rig, approval, branch=BRANCH, worktree_id=WORKTREE)

    assert _head(tree) == before
    assert marker.exists() is not bot


def _signed_head(tree: Path) -> str:
    """Move the tree's branch to a copy of its head commit that carries a signature."""
    head, _, body = _git(tree, "cat-file", "commit", "HEAD").partition("\n\n")
    signature = (
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAd\n -----END PGP SIGNATURE-----"
    )
    source = tree.parent / "signed-commit"
    source.write_bytes(f"{head}\n{signature}\n\n{body}".encode())
    oid = _git(tree, "hash-object", "-t", "commit", "-w", source.as_posix()).strip()
    _git(tree, "update-ref", f"refs/heads/{BRANCH}", oid)
    return oid


def test_no_signature_program_runs_for_a_log_the_app_reads(tmp_path: Path) -> None:
    rig = _folder(tmp_path, _signer("log.showSignature"))
    tree = _tree(rig, bot=False)  # the app's own option: every code folder and worktree
    marker = rig.short / "ran"
    _program(tree / "tools" / "sign", marker, "exit 1")
    oid = _signed_head(tree)

    assert rig.git.output(tree, "log", "-1", "--format=%H") == f"{oid}\n"
    assert not marker.exists()

    _git(tree, "log", "-1", "--format=%H")  # Git's default follows `log.showSignature`
    assert marker.exists()


# ---- a promisor remote ------------------------------------------------------------------------


def _partial(folder: Path) -> None:
    """As a partial clone leaves a folder: an old blob absent, and a promisor remote to fetch it
    from whose upload program is a path inside the work tree."""
    (folder / "tools").mkdir()
    (folder / "tools" / "upload.sh").write_bytes(b'#!/bin/sh\nexec git-upload-pack "$@"\n')
    (folder / "a.txt").write_text("second\n", encoding="utf-8")
    _commit_all(folder, "second")
    origin = folder.parent / "origin.git"
    _git(folder.parent, "clone", "-q", "--bare", "--no-hardlinks", str(folder), origin.name)
    for key, value in (
        ("remote.origin.url", origin.as_posix()),
        ("remote.origin.promisor", "true"),
        ("remote.origin.uploadpack", "sh tools/upload.sh"),
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
    ):
        _git(folder, "config", key, value)
    old = _git(folder, "rev-parse", "HEAD~1:a.txt").strip()
    loose = folder / ".git" / "objects" / old[:2] / old[2:]
    loose.chmod(stat.S_IWRITE | stat.S_IREAD)  # Git writes objects read-only
    loose.unlink()


@pytest.mark.parametrize("bot", [True, False])
def test_a_missing_object_starts_no_fetch_from_a_bot_copy(tmp_path: Path, bot: bool) -> None:
    rig = _folder(tmp_path, _partial)
    tree = _tree(rig, bot=bot)
    marker = rig.short / "ran"
    _program(tree / "tools" / "upload.sh", marker, 'exec git-upload-pack "$@"')
    old = _git(rig.folder, "rev-parse", "HEAD~1:a.txt").strip()

    if bot:
        with pytest.raises(CodeError, match="^git_failed$"):
            rig.git.output(tree, "cat-file", "-p", old)
    else:
        assert rig.git.output(tree, "cat-file", "-p", old) == "alpha\n"

    assert marker.exists() is not bot


@pytest.mark.parametrize("bot", [True, False])
def test_an_inherited_protocol_allow_list_opens_no_fetch_from_a_bot_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot: bool
) -> None:
    """Git lets `GIT_ALLOW_PROTOCOL` outrank `protocol.allow`: one the app inherited must not
    reopen the transport a Bot copy's options refuse. The ordinary tree shows it allows `file`."""
    rig = _folder(tmp_path, _partial)
    tree = _tree(rig, bot=bot)
    marker = rig.short / "ran"
    _program(tree / "tools" / "upload.sh", marker, 'exec git-upload-pack "$@"')
    old = _git(rig.folder, "rev-parse", "HEAD~1:a.txt").strip()
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    if bot:
        with pytest.raises(CodeError, match="^git_failed$"):
            rig.git.output(tree, "cat-file", "-p", old)
    else:
        assert rig.git.output(tree, "cat-file", "-p", old) == "alpha\n"

    assert marker.exists() is not bot


def test_a_remote_check_from_a_bot_copy_still_reaches_its_remote(tmp_path: Path) -> None:
    """A copy can be registered as a code folder of its own, where Push and its check run."""
    rig = _folder(tmp_path, _partial)
    tree = _tree(rig, bot=True)

    answer = rig.git.git(tree, "ls-remote", "origin", "refs/heads/main")

    assert answer.split() == [_head(rig.folder), "refs/heads/main"]


# ---- what the options are made from -------------------------------------------------------------


def test_a_bot_copys_options_switch_off_what_its_settings_would_start() -> None:
    found = _bot_copy_options(
        {
            "filter.tidy.clean": "sh tools/tidy.sh",
            "filter.lfs.clean": "git-lfs clean -- %f",
            "filter.lfs.smudge": "git-lfs smudge -- %f",
            "filter.lfs.process": "git-lfs filter-process",
            "filter.unset.clean": "",
            "protocol.custom.allow": "always",
            "protocol.file.allow": "always",
            "gpg.program": "tools/sign",
            "gpg.x509.program": "gpgsm",
            "gpg.ssh.defaultkeycommand": "tools/key --newest",
        },
        HOOKS,
        "gitdir: elsewhere",
    )

    # Git LFS's own commands stay, told to download nothing: Git starts `git-lfs` from PATH,
    # never from the copy.
    assert found.unfiltered == ("tidy",) and not found.rewrites
    assert found.local == (
        *HOOKS,
        *_off(
            *("filter.tidy.clean=", "filter.tidy.smudge=", "filter.tidy.process="),
            "filter.tidy.required=false",
            *LFS_OFFLINE,
            *("gpg.program=", "gpg.ssh.defaultkeycommand="),
        ),
    )
    assert found.options == (
        *found.local,
        *_off("protocol.allow=never"),
        *_off(*(f"protocol.{name}.allow=never" for name in (*TRANSPORTS, "custom"))),
    )
    assert found.pointer == "gitdir: elsewhere"


@pytest.mark.parametrize(
    ("commands", "unfiltered", "rewrites"),
    [
        ({"clean": "sh tools/tidy.sh"}, True, False),
        ({"clean": "sh tools/tidy.sh", "smudge": "cat"}, True, False),
        ({"smudge": "sh tools/tidy.sh"}, True, True),
        ({"process": "sh tools/tidy.sh"}, True, True),
        ({"clean": "git-lfs clean -- %f", "smudge": "sh tools/tidy.sh"}, True, True),
        ({"clean": "git-lfs clean -- %f", "smudge": "git-lfs smudge --skip -- %f"}, False, False),
        ({"clean": "git-lfs clean -- %f; sh tools/tidy.sh"}, True, False),
        ({"clean": ""}, False, False),
    ],
)
def test_a_driver_is_switched_off_unless_every_command_is_git_lfs_own(
    commands: dict[str, str], unfiltered: bool, rewrites: bool
) -> None:
    settings = {f"filter.x.{variable}": command for variable, command in commands.items()}
    found = _bot_copy_options(settings, HOOKS, "")
    assert found.unfiltered == (("x",) if unfiltered else ())
    assert found.rewrites is rewrites
    # Git LFS is told to stay offline only where a driver of its own is left on.
    left_on = any(commands.values()) and not unfiltered
    assert (found.local == (*HOOKS, *_off(*LFS_OFFLINE))) is left_on


def _lfs(folder: Path) -> None:
    """Git LFS's own three commands, as `git lfs install` writes them."""
    for variable, command in (
        ("clean", "git-lfs clean -- %f"),
        ("smudge", "git-lfs smudge -- %f"),
        ("process", "git-lfs filter-process"),
    ):
        _git(folder, "config", f"filter.lfs.{variable}", command)


@pytest.mark.parametrize("bot", [True, False])
def test_git_lfs_own_commands_stay_in_a_bot_copy_and_download_nothing(
    tmp_path: Path, bot: bool
) -> None:
    """Git LFS downloads an object its store lacks from the server `.lfsconfig` names, a file
    of the copy's own, unless Git's configuration names one: that outranks the file."""
    rig = _folder(tmp_path, _lfs)
    tree = _tree(rig, bot=bot)

    def used(setting: str) -> str | None:
        """What Git answers the app's own commands in `tree`; None when it is not set."""
        found = rig.git.answer(tree, "config", "--get", setting)
        return None if found is None else found.rstrip("\n")

    assert used("filter.lfs.clean") == "git-lfs clean -- %f"
    assert used("filter.lfs.smudge") == "git-lfs smudge -- %f"
    assert used("filter.lfs.process") == "git-lfs filter-process"
    assert used("lfs.url") == ("file:///dev/null/no-download" if bot else None)
    assert used("lfs.skipdownloaderrors") == ("true" if bot else None)
    assert rig.git.unfiltered(tree) == ((), False)


@pytest.mark.parametrize(
    "name", ["filter.a=b.clean", f"filter.a{chr(0xFFFD)}b.smudge", "protocol.a=b.allow"]
)
def test_a_name_no_option_can_spell_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="no option"):
        _bot_copy_options({name: "x"}, HOOKS, "")


def test_more_drivers_than_one_command_line_switches_off_are_refused() -> None:
    def drivers(count: int) -> dict[str, str]:
        return {f"filter.d{number}.clean": "x" for number in range(count)}

    assert len(_bot_copy_options(drivers(MAX_BOT_COPY_SETTINGS), HOOKS, "").unfiltered) == 32
    with pytest.raises(ValueError, match="no option"):
        _bot_copy_options(drivers(MAX_BOT_COPY_SETTINGS + 1), HOOKS, "")


@pytest.mark.parametrize(
    ("program", "inside"),
    [
        ("gpg", False),  # a bare name comes from PATH
        ("tools/sign", True),
        ("./sign", True),
        ("../sign", True),
        ("~/sign", True),  # Git expands no tilde here: a folder named `~` inside the tree
        (str(Path(sys.executable).resolve()), False),
        ("tools\\sign", sys.platform == "win32"),  # elsewhere a backslash is part of a name
    ],
)
def test_a_program_is_the_copys_when_its_path_is_relative(program: str, inside: bool) -> None:
    assert _from_the_copy(program) is inside


def test_a_settings_answer_counts_only_with_the_querys_own_setting() -> None:
    assert _asked("filter.x.clean\none\0filter.x.clean\ntwo\0viki.asked\n1\0") == {
        "filter.x.clean": "two"  # the last value is the one Git uses
    }
    assert _asked("viki.asked\n1\0") == {}
    for raw in ("", "filter.x.clean\none\0", "viki.asked\n1", "viki.asked\n0\0"):
        with pytest.raises(ValueError, match="unanswered|unterminated"):
            _asked(raw)


def test_git_is_unavailable_in_a_bot_copy_whose_driver_no_option_can_switch_off(
    tmp_path: Path,
) -> None:
    def settings(folder: Path) -> None:
        _git(folder, "config", "filter.a=b.clean", "sh tools/tidy.sh")

    rig = _folder(tmp_path, settings)
    tree = _tree(rig, bot=True)

    with pytest.raises(CodeError, match="^git_unavailable$"):
        rig.git.head(tree)
    assert rig.git.head(rig.folder) == _head(rig.folder)  # the folder runs it as its owner set it
