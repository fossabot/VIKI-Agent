"""The Nuitka argv compile.py builds, and the build/main wrappers around it."""

from __future__ import annotations

import importlib.metadata
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_product  # noqa: E402
import compile as compile_script  # noqa: E402
import licence_inventory  # noqa: E402
import prepare_magpie  # noqa: E402

from viki_agent import compiled  # noqa: E402

# compile.py is the one script allowed to import subprocess (TID251's scripts/* ignore); tests/*
# is not, so fakes reach it through the module compile.py already imported, never importing it
# here directly.
_subprocess = compile_script.subprocess

_WINDOWS_ONLY = {
    "--windows-console-mode=disable",
    "--experimental=force-dependencies-pefile",
}

# This venv may itself be an editable install of viki-agent, so every `build()` call below
# passes an explicit `distribution=` fake rather than depending on how it was installed.
_NOT_EDITABLE = {"url": "file:///x", "dir_info": {}}


class _FakeDistribution:
    """Structurally matches `compile.py`'s `_Distribution` Protocol: just `read_text`."""

    def __init__(self, text: str | None) -> None:
        self._text = text

    def read_text(self, filename: str) -> str | None:
        return self._text if filename == "direct_url.json" else None


def _distribution(direct_url: str | dict[str, object] | None) -> Callable[[str], _FakeDistribution]:
    """A fake `importlib.metadata.distribution` reporting the given `direct_url.json` text."""
    text = (
        direct_url if direct_url is None or isinstance(direct_url, str) else json.dumps(direct_url)
    )

    def fake(name: str) -> _FakeDistribution:
        assert name == "viki-agent"
        return _FakeDistribution(text)

    return fake


def _boom(*args: object, **kwargs: object):
    raise AssertionError("stage/run must not run when the editable check refuses first")


def _no_inventory(dist_dir: Path, out_file: Path) -> Path:
    """A fake `inventory=` that skips real licence collection for tests exercising other
    behaviour: `build`'s default would otherwise run the real `licence_inventory.write` against
    whatever Python happens to run pytest, which has no reason to have Nuitka installed."""
    del dist_dir
    return out_file


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_nuitka_argv_per_platform(tmp_path: Path, platform: str) -> None:
    repo = tmp_path / "repo"
    out = tmp_path / "out"

    argv = compile_script.nuitka_argv(repo, out, platform, "python3")

    assert argv[:3] == ["python3", "-m", "nuitka"]
    assert "--mode=standalone" in argv
    assert not any("onefile" in arg for arg in argv)
    assert not any("--assume-yes-for-downloads" in arg for arg in argv)
    assert f"--output-dir={out}" in argv
    assert "--output-folder-name=viki-agent" in argv
    assert "--output-filename=viki-agent" in argv
    assert (
        f"--include-data-dir={repo / 'src' / 'viki_agent' / 'resources'}=viki_agent/resources"
        in argv
    )
    assert argv.count("--include-distribution-metadata=viki-agent") == 1
    assert argv.count("--include-distribution-metadata=pywhispercpp") == 1
    assert f"--report={out / 'viki-agent-report.xml'}" in argv
    assert "--lto=no" in argv
    assert "--remove-output" in argv
    assert argv[-1] == str(repo / "src" / "viki_agent_main.py")

    icon_flag = f"--windows-icon-from-ico={repo / 'frontend' / 'public' / 'favicon.ico'}"
    if platform == "win32":
        assert set(argv) >= _WINDOWS_ONLY
        assert icon_flag in argv
    else:
        assert _WINDOWS_ONLY.isdisjoint(argv)
        assert icon_flag not in argv


def test_build_defaults_to_the_real_stage_and_subprocess_run() -> None:
    assert compile_script.build.__kwdefaults__ == {
        "run": _subprocess.run,
        "stage": build_product.stage,
        "distribution": importlib.metadata.distribution,
        "inventory": licence_inventory.write,
        "magpie": prepare_magpie.stage,
    }


def test_build_stages_runs_nuitka_and_reports_the_binary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"
    staged: list[Path] = []

    def fake_stage(given_repo: Path) -> Path:
        staged.append(given_repo)
        return given_repo / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        assert cwd == repo
        assert check is False
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        (dist / "support.dll").write_bytes(b"dll")
        return _subprocess.CompletedProcess(argv, 0)

    binary = compile_script.build(
        repo,
        out,
        run=fake_run,
        stage=fake_stage,
        distribution=_distribution(_NOT_EDITABLE),
        inventory=_no_inventory,
        magpie=lambda *_: None,
    )

    assert staged == [repo]
    assert binary == dist / compiled.APP_BINARY
    out_text = capsys.readouterr().out
    assert "MB" in out_text
    assert "2 files" in out_text


def test_build_refuses_a_nonzero_nuitka_exit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        return _subprocess.CompletedProcess(argv, 1)

    with pytest.raises(RuntimeError, match="nuitka exited 1"):
        compile_script.build(
            repo,
            tmp_path / "out",
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
        )


def test_build_refuses_a_missing_binary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        out.mkdir(parents=True, exist_ok=True)
        return _subprocess.CompletedProcess(argv, 0)

    with pytest.raises(RuntimeError, match="missing compiled binary"):
        compile_script.build(
            repo,
            out,
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
        )


def test_build_proceeds_without_a_direct_url_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"
    staged: list[Path] = []

    def fake_stage(given_repo: Path) -> Path:
        staged.append(given_repo)
        return given_repo

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        return _subprocess.CompletedProcess(argv, 0)

    binary = compile_script.build(
        repo,
        out,
        run=fake_run,
        stage=fake_stage,
        distribution=_distribution(None),
        inventory=_no_inventory,
        magpie=lambda *_: None,
    )

    assert staged == [repo]
    assert binary == dist / compiled.APP_BINARY


def test_build_writes_the_licence_inventory_after_the_binary_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"
    calls: list[tuple[Path, Path]] = []

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        return _subprocess.CompletedProcess(argv, 0)

    def fake_inventory(dist_dir: Path, out_file: Path) -> Path:
        calls.append((dist_dir, out_file))
        return out_file

    compile_script.build(
        repo,
        out,
        run=fake_run,
        stage=lambda r: r,
        distribution=_distribution(_NOT_EDITABLE),
        inventory=fake_inventory,
        magpie=lambda *_: None,
    )

    assert calls == [(dist, out / "viki-agent-licences.json")]


@pytest.mark.parametrize("error", [RuntimeError("nuitka is not installed"), OSError("disk full")])
def test_build_surfaces_an_inventory_refusal(tmp_path: Path, error: RuntimeError | OSError) -> None:
    """`main` only catches OSError/ValueError/RuntimeError (see `test_main_reports_a_refusal`),
    so whatever `inventory` raises must reach `build`'s caller unchanged, as that same type."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        return _subprocess.CompletedProcess(argv, 0)

    def fake_inventory(dist_dir: Path, out_file: Path) -> Path:
        del dist_dir, out_file
        raise error

    with pytest.raises(type(error), match=str(error)):
        compile_script.build(
            repo,
            out,
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
            inventory=fake_inventory,
            magpie=lambda *_: None,
        )


def test_build_deletes_a_stale_inventory_when_the_inventory_step_raises(tmp_path: Path) -> None:
    """A refused compile must never leave behind an inventory `npm run dist` would trust,
    even when one from an earlier, now-stale build still sits in `out_dir`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"
    stale = out / "viki-agent-licences.json"
    out.mkdir(parents=True)
    stale.write_text("stale inventory from an earlier build", encoding="utf-8")

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        return _subprocess.CompletedProcess(argv, 0)

    def fake_inventory(dist_dir: Path, out_file: Path) -> Path:
        del dist_dir, out_file
        raise RuntimeError("nuitka is not installed")

    with pytest.raises(RuntimeError, match="nuitka is not installed"):
        compile_script.build(
            repo,
            out,
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
            inventory=fake_inventory,
            magpie=lambda *_: None,
        )

    assert not stale.exists()


def test_build_deletes_a_stale_inventory_when_nuitka_fails(tmp_path: Path) -> None:
    """A failed (not just refused) compile must also leave no inventory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    stale = out / "viki-agent-licences.json"
    out.mkdir(parents=True)
    stale.write_text("stale inventory from an earlier build", encoding="utf-8")

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        return _subprocess.CompletedProcess(argv, 1)

    with pytest.raises(RuntimeError, match="nuitka exited 1"):
        compile_script.build(
            repo,
            out,
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
        )

    assert not stale.exists()


def test_build_refuses_an_editable_viki_agent_install(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    editable = _distribution({"url": "file:///x", "dir_info": {"editable": True}})

    with pytest.raises(RuntimeError) as excinfo:
        compile_script.build(repo, tmp_path / "out", run=_boom, stage=_boom, distribution=editable)

    assert str(excinfo.value) == (
        "viki-agent is installed editable; build from a non-editable environment: "
        "UV_PROJECT_ENVIRONMENT=<folder outside the repo> uv run --no-editable --no-dev "
        "--group build python scripts/compile.py"
    )


def test_build_refuses_a_malformed_direct_url_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    malformed = _distribution("{not valid json")

    with pytest.raises(RuntimeError, match="cannot tell whether viki-agent is installed editable"):
        compile_script.build(repo, tmp_path / "out", run=_boom, stage=_boom, distribution=malformed)


def test_main_reports_the_editable_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_build(repo: Path, out_dir: Path) -> Path:
        del repo, out_dir
        raise RuntimeError(
            "viki-agent is installed editable; build from a non-editable environment: "
            "UV_PROJECT_ENVIRONMENT=<folder outside the repo> uv run --no-editable --no-dev "
            "--group build python scripts/compile.py"
        )

    monkeypatch.setattr(compile_script, "build", fake_build)
    assert compile_script.main([]) == 2
    assert capsys.readouterr().err.strip() == (
        "compile refused: viki-agent is installed editable; build from a non-editable "
        "environment: UV_PROJECT_ENVIRONMENT=<folder outside the repo> uv run --no-editable "
        "--no-dev --group build python scripts/compile.py"
    )


def test_main_prints_the_binary_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    binary = tmp_path / "viki-agent.dist" / compiled.APP_BINARY

    def fake_build(repo: Path, out_dir: Path) -> Path:
        del repo, out_dir
        return binary

    monkeypatch.setattr(compile_script, "build", fake_build)
    assert compile_script.main(["--out-dir", str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out.strip() == str(binary)


def test_main_reports_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_build(repo: Path, out_dir: Path) -> Path:
        del repo, out_dir
        raise RuntimeError("nuitka exited 1")

    monkeypatch.setattr(compile_script, "build", fake_build)
    assert compile_script.main([]) == 2
    assert "compile refused: nuitka exited 1" in capsys.readouterr().err


def test_build_refuses_non_ui_private_resources_before_nuitka(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    resource = repo / "src" / "viki_agent" / "resources" / ".env.local"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"synthetic private resource")
    with pytest.raises(ValueError, match="resource"):
        compile_script.build(
            repo,
            tmp_path / "out",
            run=_boom,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
            inventory=_no_inventory,
            magpie=lambda *_: None,
        )


def test_build_refuses_private_compiled_output_before_inventory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    dist = out / "viki-agent.dist"

    def fake_run(argv: list[str], *, cwd: Path, check: bool):
        del cwd, check
        dist.mkdir(parents=True)
        (dist / compiled.APP_BINARY).write_bytes(b"binary")
        (dist / "state.json").write_bytes(b"synthetic private state")
        return _subprocess.CompletedProcess(argv, 0)

    with pytest.raises(ValueError, match="release"):
        compile_script.build(
            repo,
            out,
            run=fake_run,
            stage=lambda r: r,
            distribution=_distribution(_NOT_EDITABLE),
            inventory=_boom,
            magpie=lambda *_: None,
        )
