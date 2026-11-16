"""compiled.py's install_dir/worker_argv contract and viki_agent_main.main's dispatch (ADR-054):
source argv is unchanged, compiled argv is the app binary's own --viki-worker subcommand, an
unknown or missing worker name exits 2 without calling anything, and any argv not led by the
worker flag reaches viki_agent.__main__.main unchanged. The last test proves the module-level
import stays package-plus-compiled only, so Nuitka never follows a worker import before
--viki-worker is actually given."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import viki_agent_main
from viki_agent import compiled, winproc
from viki_agent.media import media_worker
from viki_agent.voice import voice_pack_setup, voice_worker

_WORKER_MODULES = {
    "voice": voice_worker,
    "media": media_worker,
    "voice-pack-setup": voice_pack_setup,
}


def _must_not_run(_args: list[str]) -> int:
    raise AssertionError("must not be called for this argv")


def test_worker_modules_cover_every_compiled_worker_name() -> None:
    assert frozenset(_WORKER_MODULES) == compiled.WORKERS


# --- compiled.install_dir -----------------------------------------------------------------


def test_install_dir_from_a_checkout_file(tmp_path: Path) -> None:
    module_file = tmp_path / "src" / "viki_agent" / "compiled.py"
    module_file.parent.mkdir(parents=True)
    module_file.touch()
    assert compiled.install_dir(module_file, False) == tmp_path


def test_install_dir_from_a_compiled_file(tmp_path: Path) -> None:
    module_file = tmp_path / "viki_agent" / "compiled.py"
    module_file.parent.mkdir(parents=True)
    module_file.touch()
    assert compiled.install_dir(module_file, True) == tmp_path


# --- compiled.worker_argv ------------------------------------------------------------------


def test_source_argv_is_isolated_python_with_the_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compiled, "COMPILED", False)
    script = Path("/repo/src/viki_agent/voice/voice_worker.py")
    argv = compiled.worker_argv(
        "voice", ["small-q8_0", "cpu"], python="/usr/bin/python3.14", script=script
    )
    assert argv == ["/usr/bin/python3.14", "-I", "-B", str(script), "small-q8_0", "cpu"]


def test_compiled_worker_argv_starts_the_app_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(compiled, "COMPILED", True)
    monkeypatch.setattr(compiled, "INSTALL_DIR", tmp_path)
    argv = compiled.worker_argv("media", ["x/y"], python=sys.executable, script=Path("unused.py"))
    assert argv == [str(tmp_path / compiled.APP_BINARY), "--viki-worker", "media", "x/y"]


@pytest.mark.parametrize("compiled_mode", [False, True])
def test_worker_argv_refuses_an_unknown_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, compiled_mode: bool
) -> None:
    monkeypatch.setattr(compiled, "COMPILED", compiled_mode)
    monkeypatch.setattr(compiled, "INSTALL_DIR", tmp_path)
    with pytest.raises(ValueError):
        compiled.worker_argv("nope", [], python=sys.executable, script=tmp_path / "x.py")


# --- viki_agent_main.main dispatch ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_WORKER_MODULES))
def test_dispatch_runs_the_named_worker_with_the_rest_of_argv(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    calls: list[list[str]] = []

    def recorder(rest: list[str]) -> int:
        calls.append(rest)
        return 42

    monkeypatch.setattr(_WORKER_MODULES[name], "main", recorder)
    monkeypatch.setattr("viki_agent.__main__.main", _must_not_run)
    exit_code = viki_agent_main.main(["--viki-worker", name, "a", "b"])
    assert calls == [["a", "b"]]
    assert exit_code == 42


@pytest.mark.parametrize(
    "argv", [["--viki-worker"], ["--viki-worker", "nope"], ["--viki-worker", "../x"]]
)
def test_unknown_or_missing_worker_name_exits_2_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(voice_worker, "main", _must_not_run)
    monkeypatch.setattr(media_worker, "main", _must_not_run)
    monkeypatch.setattr(voice_pack_setup, "main", _must_not_run)
    monkeypatch.setattr("viki_agent.__main__.main", _must_not_run)
    assert viki_agent_main.main(argv) == viki_agent_main.UNKNOWN_WORKER_EXIT == 2


def test_tuple_argv_still_dispatches_to_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tuple argv must not skip the worker check: `argv[:1]` of a tuple is itself a tuple,
    which compares unequal to the one-element list `[WORKER_FLAG]` regardless of content, so the
    dispatcher has to normalise it to a list before comparing."""
    calls: list[list[str]] = []

    def recorder(rest: list[str]) -> int:
        calls.append(rest)
        return 0

    monkeypatch.setattr(media_worker, "main", recorder)
    monkeypatch.setattr("viki_agent.__main__.main", _must_not_run)
    assert viki_agent_main.main(("--viki-worker", "media", "x")) == 0
    assert calls == [["x"]]


@pytest.mark.parametrize(
    "argv",
    [[], ["--dev"], ["--window-host=123"], ["--window-host=1", "--viki-worker", "media"]],
)
def test_non_worker_argv_reaches_the_app_unchanged(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    calls: list[list[str]] = []

    def fake_app(received: list[str]) -> int:
        calls.append(received)
        return 0

    monkeypatch.setattr("viki_agent.__main__.main", fake_app)
    assert viki_agent_main.main(argv) == 0
    assert calls == [list(argv)]


# --- import hygiene -------------------------------------------------------------------------


def test_importing_viki_agent_main_loads_no_worker_module(tmp_path: Path) -> None:
    """Merely importing the entry module — never calling main() — must not pull in
    viki_agent.__main__ or any of the three worker modules: Nuitka only has to follow the one
    branch --viki-worker actually selects, each of which imports lazily inside main(). Dispatching
    an unknown name, including one that is itself a real importable module path
    (`viki_agent.__main__`), must not import anything either — the unknown branch returns 2
    without ever calling `importlib.import_module` on the name it was given."""
    code = (
        "import sys\n"
        "import viki_agent_main\n"
        "results = [\n"
        "    viki_agent_main.main(['--viki-worker', 'nope']),\n"
        "    viki_agent_main.main(['--viki-worker', 'viki_agent.__main__']),\n"
        "]\n"
        "names = [m for m in sys.modules if m == 'viki_agent' or m.startswith('viki_agent.')]\n"
        "print(','.join(str(r) for r in results))\n"
        "print(','.join(sorted(names)))\n"
    )
    result = winproc.run([sys.executable, "-I", "-B", "-c", code], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "2,2"
    assert lines[1] == "viki_agent,viki_agent.compiled"
