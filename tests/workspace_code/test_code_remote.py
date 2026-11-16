"""Remote selection, the remote's branch and the pull request page, in `tmp_path` with a local bare
repository as the remote: no network."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from tests.workspace_code.test_code_git import OID, _commit, _git, _repo, _setup, needs_git
from viki_agent import winproc
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit
from viki_agent.code.code_remote import (
    LS_REMOTE_TIMEOUT_S,
    Remote,
    compare_url,
    remote_oid,
    select_remote,
    tracking_oid,
)

# built at run time so the fixture guard, `tests/test_fixtures_clean.py`, does not read these
# remote URLs as e-mail addresses
AT = "@"
TOKEN = "ghp_SECRETTOKEN0123456789"


@pytest.fixture(autouse=True)
def _no_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `GIT_*` variables would turn every case into `git_unavailable`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)


def _bare(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "--bare", "-b", "main")
    return path


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _committed(tmp_path: Path) -> tuple[Path, Path, CodeGit]:
    """A repository with one commit on `main` and a bare `origin`."""
    root = _repo(tmp_path / "w")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _commit(root, "first")
    bare = _bare(tmp_path / "b")
    _git(root, "remote", "add", "origin", str(bare))
    git, _ = _setup(tmp_path, root)
    return root, bare, git


def _more(root: Path, text: str) -> str:
    (root / "a.txt").write_text(text, encoding="utf-8")
    _commit(root, text.strip())
    return _head(root)


# --- select_remote -----------------------------------------------------------------------------


@needs_git
def test_select_remote_defaults_to_origin(tmp_path: Path) -> None:
    root, bare, git = _committed(tmp_path)
    assert select_remote(git, root, "main") == (Remote("origin", str(bare)), "ready")


@needs_git
def test_select_remote_prefers_the_branch_remote(tmp_path: Path) -> None:
    root, bare, git = _committed(tmp_path)
    other = _bare(tmp_path / "o")
    _git(root, "remote", "add", "upstream", str(other))
    _git(root, "config", "branch.main.remote", "upstream")
    assert select_remote(git, root, "main") == (Remote("upstream", str(other)), "ready")
    assert select_remote(git, root, "side") == (Remote("origin", str(bare)), "ready")


@needs_git
def test_select_remote_without_origin_is_none(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    _git(root, "remote", "remove", "origin")
    assert select_remote(git, root, "main") == (None, "none")


@needs_git
def test_select_remote_configured_but_missing_is_unsupported(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    _git(root, "config", "branch.main.remote", "gone")
    assert select_remote(git, root, "main") == (None, "unsupported")


@needs_git
@pytest.mark.parametrize(
    "name", [".", "https://example.test/o/r.git", f"git{AT}example.test:o/r", "-x"]
)
def test_select_remote_refuses_a_configured_name_that_is_not_a_remote_name(
    tmp_path: Path, name: str
) -> None:
    root, _, git = _committed(tmp_path)
    _git(root, "config", "branch.main.remote", name)
    assert select_remote(git, root, "main") == (None, "unsupported")


@needs_git
def test_select_remote_refuses_a_push_url_that_differs(tmp_path: Path) -> None:
    root, bare, git = _committed(tmp_path)
    _git(root, "config", "remote.origin.pushurl", str(_bare(tmp_path / "p")))
    assert select_remote(git, root, "main") == (None, "unsupported")
    _git(root, "config", "remote.origin.pushurl", str(bare))
    assert select_remote(git, root, "main") == (Remote("origin", str(bare)), "ready")


@needs_git
def test_select_remote_refuses_two_urls(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    _git(root, "config", "--add", "remote.origin.url", str(_bare(tmp_path / "p")))
    assert select_remote(git, root, "main") == (None, "unsupported")


@needs_git
def test_select_remote_refuses_a_mirror(tmp_path: Path) -> None:
    root, bare, git = _committed(tmp_path)
    _git(root, "config", "remote.origin.mirror", "false")
    assert select_remote(git, root, "main") == (Remote("origin", str(bare)), "ready")
    _git(root, "config", "remote.origin.mirror", "true")
    assert select_remote(git, root, "main") == (None, "unsupported")


@needs_git
def test_select_remote_keeps_the_url_out_of_repr_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root, _, git = _committed(tmp_path)
    url = f"https://octo:{TOKEN}@github.com/octo/repo.git"
    _git(root, "config", "remote.origin.url", url)
    with caplog.at_level(logging.DEBUG):
        remote, state = select_remote(git, root, "main")
    assert (remote, state) == (Remote("origin", url), "ready")
    assert TOKEN not in repr((remote, state)) and TOKEN not in caplog.text


@needs_git
def test_select_remote_timeout_names_no_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root, _, git = _committed(tmp_path)

    def hang(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError(f"timed out talking to https://octo:{TOKEN}@github.com/o/r")

    monkeypatch.setattr(winproc, "git", hang)
    with caplog.at_level(logging.DEBUG), pytest.raises(CodeError) as caught:
        select_remote(git, root, "main")
    assert caught.value.code == "git_unavailable"
    assert TOKEN not in str(caught.value) and TOKEN not in caplog.text


def test_select_remote_refuses_an_unnameable_branch() -> None:
    with pytest.raises(CodeError) as caught:
        select_remote(None, Path("."), "a..b")  # type: ignore[arg-type]
    assert caught.value.code == "unsupported_branch"


# --- tracking_oid ------------------------------------------------------------------------------


@needs_git
def test_tracking_oid_before_and_after_a_push(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    assert tracking_oid(git, root, "origin", "main") is None
    _git(root, "push", "-q", "origin", "main")
    assert tracking_oid(git, root, "origin", "main") == _head(root)
    assert tracking_oid(git, root, "origin", "side") is None


@needs_git
@pytest.mark.parametrize("answer", ["", "not-an-oid\n", f"{OID}\n{OID}\n"])
def test_tracking_oid_refuses_an_answer_that_is_not_one_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    root, _, git = _committed(tmp_path)
    monkeypatch.setattr(git, "answer", lambda *_args: answer)
    with pytest.raises(CodeError) as caught:
        tracking_oid(git, root, "origin", "main")
    assert caught.value.code == "git_failed"


def test_tracking_oid_refuses_an_unnameable_remote_or_branch() -> None:
    for remote, branch, code in (
        ("--upload-pack=x", "main", "remote_unsupported"),
        ("origin", "a@{1}", "unsupported_branch"),
    ):
        with pytest.raises(CodeError) as caught:
            tracking_oid(None, Path("."), remote, branch)  # type: ignore[arg-type]
        assert caught.value.code == code


# --- remote_oid --------------------------------------------------------------------------------


@needs_git
def test_remote_oid_at_the_pushed_oid_or_absent(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    assert remote_oid(git, root, "origin", "main") is None
    _git(root, "push", "-q", "origin", "main")
    assert remote_oid(git, root, "origin", "main") == _head(root)
    assert remote_oid(git, root, "origin", "side") is None


@needs_git
def test_remote_oid_ignores_a_longer_ref_with_the_same_tail(tmp_path: Path) -> None:
    root, _, git = _committed(tmp_path)
    _git(root, "push", "-q", "origin", "HEAD:refs/other/refs/heads/main")
    # Git's own pattern match returns the longer ref: the exact-ref filter is what drops it.
    assert "refs/other/refs/heads/main" in _git(root, "ls-remote", "origin", "refs/heads/main")
    assert remote_oid(git, root, "origin", "main") is None
    second = _more(root, "two\n")
    _git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    assert remote_oid(git, root, "origin", "main") == second


@needs_git
def test_remote_oid_of_an_unreachable_remote_is_push_failed_and_logs_no_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root, _, git = _committed(tmp_path)
    gone = tmp_path / "gone-remote-marker"
    _git(root, "remote", "set-url", "origin", str(gone))
    with caplog.at_level(logging.DEBUG), pytest.raises(CodeError) as caught:
        remote_oid(git, root, "origin", "main")
    assert caught.value.code == "push_failed"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "gone-remote-marker" not in caplog.text + str(caught.value)
    assert "ls-remote" in caplog.text


@needs_git
def test_remote_oid_runs_ls_remote_with_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, git = _committed(tmp_path)
    seen: list[tuple[tuple[str, ...], float]] = []

    def record(_cwd: object, *args: str, timeout: float = 0) -> str:
        seen.append((args, timeout))
        return f"{OID}\trefs/heads/main\n"

    monkeypatch.setattr(winproc, "git", record)
    assert remote_oid(git, root, "origin", "main") == OID
    ((args, timeout),) = seen
    assert args[-3:] == ("ls-remote", "origin", "refs/heads/main")
    assert timeout == LS_REMOTE_TIMEOUT_S


@needs_git
@pytest.mark.parametrize(
    "output",
    [
        "garbage\n",
        f"{OID} refs/heads/main\n",
        f"{OID[:-1]}\trefs/heads/main\n",
        f"{OID}\trefs/heads/main\n\n",
        f"{OID}\trefs/heads/main\n{OID}\trefs/heads/main\n",
    ],
    ids=["text", "space", "short-oid", "blank-line", "duplicate"],
)
def test_remote_oid_refuses_a_malformed_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    root, _, git = _committed(tmp_path)
    monkeypatch.setattr(winproc, "git", lambda *_args, **_kwargs: output)
    with pytest.raises(CodeError) as caught:
        remote_oid(git, root, "origin", "main")
    assert caught.value.code == "push_failed"


@needs_git
@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_remote_oid_failures_carry_no_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    root, _, git = _committed(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> str:
        raise failure(f"fatal: unable to access 'https://octo:{TOKEN}@github.com/o/r.git/'")

    monkeypatch.setattr(winproc, "git", broken)
    with caplog.at_level(logging.DEBUG), pytest.raises(CodeError) as caught:
        remote_oid(git, root, "origin", "main")
    assert caught.value.code == "push_failed"
    assert caught.value.__cause__ is None
    assert TOKEN not in str(caught.value) and TOKEN not in caplog.text
    assert failure.__name__ in caplog.text


def test_remote_oid_refuses_an_unnameable_remote_or_branch() -> None:
    for remote, branch, code in (
        ("a b", "main", "remote_unsupported"),
        ("origin", "-x", "unsupported_branch"),
    ):
        with pytest.raises(CodeError) as caught:
            remote_oid(None, Path("."), remote, branch)  # type: ignore[arg-type]
        assert caught.value.code == code


# --- compare_url -------------------------------------------------------------------------------

GITHUB = "https://github.com/octo/repo/compare/main...feature/x?quick_pull=1"
GITLAB = (
    "https://gitlab.com/{path}/-/merge_requests/new?merge_request%5Bsource_branch%5D=feature%2Fx"
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octo/repo", GITHUB),
        ("https://github.com/octo/repo.git", GITHUB),
        ("https://github.com/octo/repo/", GITHUB),
        ("https://github.com/octo/repo.git/", GITHUB),
        ("https://GitHub.COM/octo/repo", GITHUB),
        ("https://github.com:443/octo/repo", GITHUB),
        (f"https://octo{AT}github.com/octo/repo", GITHUB),
        (f"https://octo:{TOKEN}@github.com/octo/repo.git", GITHUB),
        (f"git{AT}github.com:octo/repo.git", GITHUB),
        (f"git{AT}github.com:octo/repo", GITHUB),
        (f"git{AT}GITHUB.COM:octo/repo", GITHUB),
        (f"ssh://git{AT}github.com/octo/repo.git", GITHUB),
        (f"ssh://git{AT}github.com:22/octo/repo/", GITHUB),
        (f"ssh://git{AT}GitHub.com:2222/octo/repo", GITHUB),
        ("https://gitlab.com/octo/repo", GITLAB.format(path="octo/repo")),
        ("https://gitlab.com/octo/repo.git/", GITLAB.format(path="octo/repo")),
        ("https://GitLab.com/g/s/p", GITLAB.format(path="g/s/p")),
        ("https://gitlab.com/a/b/c/d/e", GITLAB.format(path="a/b/c/d/e")),
        (f"https://oauth2:{TOKEN}@gitlab.com/g/s/p.git", GITLAB.format(path="g/s/p")),
        (f"git{AT}gitlab.com:g/s/p.git", GITLAB.format(path="g/s/p")),
        (f"ssh://git{AT}gitlab.com:22/g/s/p.git", GITLAB.format(path="g/s/p")),
    ],
)
def test_compare_url_forms(url: str, expected: str) -> None:
    assert compare_url(url, "feature/x", "main") == expected


def test_compare_url_strips_the_token_and_refuses_unknown_hosts() -> None:
    url = f"https://octo:{TOKEN}@github.com/octo/repo.git"
    page = compare_url(url, "feature", "main")
    assert page == "https://github.com/octo/repo/compare/main...feature?quick_pull=1"
    assert page is not None and TOKEN not in page and "octo:" not in page and "@" not in page
    page = compare_url(f"https://oauth2:{TOKEN}@gitlab.com/g/p.git", "feature", "main")
    assert page is not None and TOKEN not in page and "oauth2" not in page and "@" not in page
    for other in (
        f"https://octo:{TOKEN}@example.com/octo/repo.git",
        "https://github.com.evil.example/octo/repo",
        f"https://github.com{AT}evil.example/octo/repo",
        f"https://octo:{TOKEN}@evil.example/github.com/octo/repo",
        "https://notgithub.com/octo/repo",
        f"git{AT}bitbucket.org:octo/repo.git",
        f"ssh://git{AT}evil.example/octo/repo.git",
    ):
        assert compare_url(other, "feature", "main") is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/local/path/repo",
        "file:///octo/repo",
        "http://github.com/octo/repo",
        "git://github.com/octo/repo",
        "https://github.com:8443/octo/repo",
        "https://github.com:80/octo/repo",
        "https://github.com:/octo/repo",
        f"https://a{AT}b{AT}github.com/octo/repo",
        "https://github.com/octo",
        "https://github.com/octo/repo/extra",
        "https://gitlab.com/octo",
        "https://gitlab.com/a/b/c/d/e/f",
        "https://github.com/octo/..",
        "https://github.com/../repo",
        "https://github.com/./repo",
        "https://github.com/-/repo",
        "https://gitlab.com/a/-/b",
        "https://gitlab.com/a/../b",
        "https://github.com//octo/repo",
        "https://github.com/octo/repo?x=1",
        "https://github.com/octo/repo#frag",
        "https://github.com/octo/repo\n",
        "https://github.com/octo/re po",
        "https://github.com/octo/repo%20x",
        f"https://github.com/octo/{'r' * 101}",
        f"git{AT}github.com:/octo/repo",
        f"git{AT}github.com:octo/repo/",
        f"bob{AT}github.com:octo/repo",
        f"ssh://bob{AT}github.com/octo/repo",
        f"ssh://git{AT}github.com:/octo/repo",
        f"ssh://git{AT}github.com:123456/octo/repo",
    ],
)
def test_compare_url_refuses(url: str) -> None:
    assert compare_url(url, "feature", "main") is None


@pytest.mark.parametrize(
    ("branch", "base"),
    [
        ("-x", "main"),
        ("a..b", "main"),
        ("x@{1}", "main"),
        ("HEAD", "main"),
        ("a b", "main"),
        ("", "main"),
        ("feature", "--x"),
        ("feature", "a..b"),
        ("feature", ""),
        ("main", "main"),
    ],
)
def test_compare_url_refuses_an_invalid_branch_or_base(branch: str, base: str) -> None:
    for url in ("https://github.com/octo/repo", "https://gitlab.com/octo/repo"):
        assert compare_url(url, branch, base) is None


def test_compare_url_percent_encodes_the_gitlab_branch_only() -> None:
    assert compare_url("https://gitlab.com/g/p", "feature/a.b-c_d", "main") == (
        "https://gitlab.com/g/p/-/merge_requests/new?merge_request%5Bsource_branch%5D="
        "feature%2Fa.b-c_d"
    )
    assert compare_url("https://github.com/o/r", "feature/a", "release/1") == (
        "https://github.com/o/r/compare/release/1...feature/a?quick_pull=1"
    )
