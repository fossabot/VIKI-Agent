"""Build VIKI Agent into one standalone, no-Python-required folder with Nuitka (ADR-054).

Refuses before staging when the ``viki-agent`` distribution is installed editable: an
editable install's RECORD lists no package files, so
``--include-distribution-metadata=viki-agent`` would be fatal. Run from a non-editable
environment instead::

    UV_PROJECT_ENVIRONMENT=<folder outside the repo> uv run --no-editable --no-dev --group
      build python scripts/compile.py

Stages the checked frontend build (`build_product.stage`), then runs, from the repo root::

    python -m nuitka --mode=standalone --output-dir=<out> --output-folder-name=viki-agent
      --output-filename=viki-agent
      --include-data-dir=<repo>/src/viki_agent/resources=viki_agent/resources
      --include-distribution-metadata=viki-agent --include-distribution-metadata=pywhispercpp
      --report=<out>/viki-agent-report.xml --lto=no --remove-output
      [Windows only: --windows-console-mode=disable
       --windows-icon-from-ico=<repo>/frontend/public/favicon.ico
       --experimental=force-dependencies-pefile]
      <repo>/src/viki_agent_main.py

Prepare the pinned Magpie sidecar with ``python scripts/prepare_magpie.py`` first. A successful
compile copies its verified bundle beside the backend in ``magpie/`` before recording licences.

Needs a compiler already on the machine: MSVC 2022 or later (or Zig) on Windows, Xcode's clang
on macOS, gcc or clang plus patchelf on Linux. Never passes ``--assume-yes-for-downloads``:
Nuitka's own Windows DLL analysis would otherwise prompt to fetch Dependency Walker, which fails
non-interactively outside a terminal; the pefile flag above uses Nuitka's bundled reader instead,
so a build never triggers an unplanned download.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import build_product
import licence_inventory
import prepare_magpie
import release_files

from viki_agent import compiled

REPO = Path(__file__).resolve().parents[1]


def nuitka_argv(repo: Path, out_dir: Path, platform: str, python: str) -> list[str]:
    """The ``python -m nuitka`` argv that builds the standalone folder.

    ``platform`` is a ``sys.platform`` value: only ``"win32"`` adds the GUI-subsystem, app-icon
    and bundled-pefile-DLL-reader flags, so a build never prompts to download Dependency
    Walker. The entry point, `src/viki_agent_main.py`, is always the last argument.
    """
    out = str(out_dir)
    argv = [
        python,
        "-m",
        "nuitka",
        "--mode=standalone",
        f"--output-dir={out}",
        "--output-folder-name=viki-agent",
        "--output-filename=viki-agent",
        f"--include-data-dir={repo / 'src' / 'viki_agent' / 'resources'}=viki_agent/resources",
        "--include-distribution-metadata=viki-agent",
        "--include-distribution-metadata=pywhispercpp",
        f"--report={out_dir / 'viki-agent-report.xml'}",
        "--lto=no",
        "--remove-output",
    ]
    if platform == "win32":
        argv += [
            "--windows-console-mode=disable",
            f"--windows-icon-from-ico={repo / 'frontend' / 'public' / 'favicon.ico'}",
            "--experimental=force-dependencies-pefile",
        ]
    argv.append(str(repo / "src" / "viki_agent_main.py"))
    return argv


class _Distribution(Protocol):
    def read_text(self, filename: str, /) -> str | None: ...


def _refuse_if_editable(distribution: Callable[[str], _Distribution]) -> None:
    """Raise when PEP 610 metadata says the ``viki-agent`` distribution is installed editable.

    Nuitka's ``--include-distribution-metadata=viki-agent`` needs package files an editable
    install's RECORD never lists, so a build must run in a non-editable environment. No
    ``direct_url.json`` means the install is not editable. JSON that does not parse, or does not
    carry the shape this check expects, means it cannot tell either way, so it refuses with its
    own reason rather than guessing.
    """
    text = distribution("viki-agent").read_text("direct_url.json")
    if text is None:
        return
    try:
        editable = bool(json.loads(text).get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(
            "cannot tell whether viki-agent is installed editable: direct_url.json did not "
            f"parse as expected: {exc}"
        ) from exc
    if editable:
        raise RuntimeError(
            "viki-agent is installed editable; build from a non-editable environment: "
            "UV_PROJECT_ENVIRONMENT=<folder outside the repo> uv run --no-editable --no-dev "
            "--group build python scripts/compile.py"
        )


def build(
    repo: Path,
    out_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    stage: Callable[[Path], Path] = build_product.stage,
    distribution: Callable[[str], _Distribution] = importlib.metadata.distribution,
    inventory: Callable[[Path, Path], Path] = licence_inventory.write,
    magpie: Callable[[Path, Path], None] = prepare_magpie.stage,
) -> Path:
    """Stage the UI, run Nuitka, and return the compiled binary's path.

    Refuses first when `viki-agent` is installed editable; see `_refuse_if_editable`.
    `magpie` stages the verified sidecar before the inventory step.
    Deletes any stale `viki-agent-licences.json` left by an earlier build before Nuitka runs,
    so a compile that refuses or fails here never leaves behind an inventory `npm run dist`
    would trust. Raises RuntimeError when Nuitka exits non-zero or the expected binary is
    missing afterward, and whatever RuntimeError or OSError `inventory` raises once the binary
    is confirmed (ADR-055: a build that cannot record its own licence facts must not produce
    installable output). `run`, `stage`, `distribution` and `inventory` are injectable for
    tests; production leaves them as `subprocess.run`, `build_product.stage`,
    `importlib.metadata.distribution` and `licence_inventory.write`. Nuitka's own output is
    inherited rather than captured, so a failing build's diagnostics reach the caller directly
    instead of being swallowed.
    """
    _refuse_if_editable(distribution)
    (out_dir / "viki-agent-licences.json").unlink(missing_ok=True)
    build_product.check_resources(repo)
    stage(repo)
    argv = nuitka_argv(repo, out_dir, sys.platform, sys.executable)
    started = time.monotonic()
    result = run(argv, cwd=repo, check=False)
    elapsed = time.monotonic() - started
    if result.returncode:
        raise RuntimeError(f"nuitka exited {result.returncode}")
    binary = out_dir / "viki-agent.dist" / compiled.APP_BINARY
    if not binary.is_file():
        raise RuntimeError(f"missing compiled binary: {binary}")
    magpie(repo, binary.parent)
    files = release_files.check_compiled_tree(binary.parent)
    inventory(out_dir / "viki-agent.dist", out_dir / "viki-agent-licences.json")
    size_mb = sum(path.stat().st_size for path in files) / 1_000_000
    print(f"nuitka build: {elapsed:.1f}s, {size_mb:.1f} MB, {len(files)} files")
    return binary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "dist" / "compiled")
    args = parser.parse_args(argv)
    try:
        binary = build(REPO, args.out_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"compile refused: {exc}", file=sys.stderr)
        return 2
    print(binary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
