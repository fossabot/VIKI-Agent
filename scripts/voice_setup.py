"""Voice setup: list, check or install the pinned dictation models and backends.

    uv run python scripts/voice_setup.py [--list | --check] [--model ID]
        [--backend auto|cpu|cuda|metal]

Setup-only: the server never imports this script. Every request goes through `voice_fetch`, and
every file lands under `<data>/voice/` only after its size and SHA-256 match the pin.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import voice_fetch
import voice_machine

from viki_agent import config, protection
from viki_agent.voice import voice_models

_MB = 1_000_000
_GIB = 1024**3


def _mb(size: int) -> str:
    return f"{size / _MB:.1f} MB"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice_setup.py", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", action="store_true", help="show the machine, the choices, no request"
    )
    mode.add_argument("--check", action="store_true", help="verify the installed files")
    parser.add_argument("--model", help="a model id from --list; default: the recommendation")
    parser.add_argument("--backend", default="auto", help="auto, cpu, cuda or metal")
    return parser


def _refusal(model: str | None, backend: str) -> str | None:
    """Why the flags name no choice of this machine, or None; the message lists the choices."""
    if model is not None and model not in voice_models.MODELS:
        return f"unknown model {model!r}; choose one of: {', '.join(voice_models.MODELS)}"
    offered = voice_models.offered(sys.platform, platform.machine())
    if backend != "auto" and backend not in offered:
        return f"backend {backend!r} is not offered here; choose one of: auto, {', '.join(offered)}"
    return None


def _print_machine(machine: voice_machine.Machine) -> None:
    gpu = machine.nvidia
    print(f"os: {machine.platform}")
    print(f"architecture: {machine.arch}")
    print(f"cpus: {machine.cpus}")
    print(f"memory: {machine.memory / _GIB:.1f} GB")
    if gpu is None:
        print("nvidia gpu: none")
    else:
        driver = f"{gpu.cuda_version // 1000}.{gpu.cuda_version % 1000 // 10}"
        print(f"nvidia gpu: {gpu.name}, {gpu.memory / _GIB:.1f} GB, driver CUDA {driver}")
    print(f"vc++ runtime: {'yes' if machine.vc_runtime else 'no'}")
    if gpu is not None and not machine.vc_runtime:
        # The cuda backend needs it and the setup does not install it: the user may.
        print("the cuda backend needs the Microsoft Visual C++ 2015-2022 Redistributable (x64)")


def _state(directory: Path, files: tuple[voice_models.PinnedFile, ...]) -> str:
    return "installed" if voice_models.present(directory, files) else "not installed"


def _list(data: Path, probe: Callable[[], voice_machine.Machine]) -> int:
    machine = probe()
    _print_machine(machine)
    print("models:")
    for model_id, model in voice_models.MODELS.items():
        files = voice_models.model_files(model_id)
        size = sum(pin.size for pin in files)
        state = _state(voice_models.model_dir(data, model_id), files)
        print(f"  {model_id}  {_mb(size)}  {len(model.languages)} languages  {state}")
    print("backends:")
    for backend_id in voice_models.offered(sys.platform, platform.machine()):
        backend = voice_models.BACKENDS[backend_id]
        size = backend.archive.size if backend.archive is not None else 0
        state = _state(voice_models.backend_dir(data, backend_id), backend.files)
        print(f"  {backend_id}  {_mb(size)}  {state}")
    cpu_model = voice_models.RANKING["cpu"][0]
    files = voice_models.model_files(cpu_model)
    state = _state(voice_models.model_dir(data, cpu_model), files)
    print(f"cpu fallback: {cpu_model} ({_mb(sum(pin.size for pin in files))}) {state}")
    backend_id, model_id = voice_machine.recommend(machine)
    print(f"recommended: --model {model_id} --backend {backend_id}")
    return 0


def _should_verify(directory: Path) -> bool:
    try:
        directory.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass  # An inspection failure belongs in verify's report, not the uninstalled choices.
    return True


def _check(data: Path) -> int:
    items: list[tuple[str, Path, tuple[voice_models.PinnedFile, ...]]] = []
    for model_id in voice_models.MODELS:
        files = voice_models.model_files(model_id)
        directory = voice_models.model_dir(data, model_id)
        if _should_verify(directory):
            items.append((model_id, directory, files))
    models_installed = bool(items)
    for backend_id, backend in voice_models.BACKENDS.items():
        directory = voice_models.backend_dir(data, backend_id)
        if backend.files and _should_verify(directory):
            items.append((backend_id, directory, backend.files))
    failed = False
    for item_id, directory, files in items:
        problem = voice_models.verify(directory, files)
        print(f"ok {item_id}" if problem is None else f"{item_id}: {problem}")
        failed = failed or problem is not None
    if not models_installed:
        print("no model installed; see --list")
    return 1 if failed or not models_installed else 0


def _make_private_folders(data: Path, folders: list[Path]) -> bool:
    # Parents first, then the private leaf, as the launcher does for `<data>`: creating the
    # parents through the leaf would publish a broadly inherited folder. A folder that already
    # exists must be private already, as `assets.py` requires: the worker loads DLLs from here.
    try:
        data.parent.mkdir(parents=True, exist_ok=True)
        for folder in (data, *folders):
            if not protection.create_private_directory(folder):
                protection.require_private_directory(folder)
    except protection.ProtectionError, OSError:
        print("cannot create a private folder, or an existing one is not private", file=sys.stderr)
        return False
    return True


def _step(verb: str, pin: voice_models.PinnedFile, action: Callable[[], str | None]) -> bool:
    print(f"{verb} {pin.name} ({_mb(pin.size)})")
    try:
        problem = action()
    except OSError:  # extract and unlink raise on a full disk or a locked target
        problem = "write_failed"
    if problem is not None:
        print(f"{pin.name}: {problem}", file=sys.stderr)
        return False
    print(f"ok {pin.name} ({_mb(pin.size)})")
    return True


def _fetch(
    pin: voice_models.PinnedFile, directory: Path, opener: Callable[[str], BinaryIO]
) -> bool:
    return _step(
        "fetching",
        pin,
        lambda: voice_fetch.fetch(
            pin.url, pin.name, pin.size, pin.sha256, directory, opener=opener
        ),
    )


def _install_backend(
    backend: voice_models.Backend,
    archive: voice_models.PinnedFile,
    directory: Path,
    opener: Callable[[str], BinaryIO],
) -> bool:
    if not _fetch(archive, directory, opener):
        return False
    path = directory / archive.name
    for pin in backend.files:
        if not _step(
            "extracting",
            pin,
            lambda pin=pin: voice_fetch.extract(
                path, pin.member, pin.name, pin.size, pin.sha256, directory
            ),
        ):
            # The archive stays: it verified, so a rerun extracts without downloading again.
            return False
    return _step("removing", archive, lambda: path.unlink())


def _install(
    data: Path,
    model_id: str,
    backend_id: str,
    opener: Callable[[str], BinaryIO],
) -> int:
    backend = voice_models.BACKENDS[backend_id]
    backend_folder = voice_models.backend_dir(data, backend_id)
    voice = data / "voice"
    # A GPU backend that fails falls back to the CPU ranking's model: fetch it beside the pair's.
    cpu_model = voice_models.RANKING["cpu"][0]
    models = [model_id] if backend_id == "cpu" or cpu_model == model_id else [model_id, cpu_model]
    folders = [voice, voice / "models", *(voice_models.model_dir(data, m) for m in models)]
    if backend.files:
        folders += [voice / "backends", backend_folder]
    if not _make_private_folders(data, folders):
        return 1
    for model in models:
        for pin in voice_models.model_files(model):
            if not _fetch(pin, voice_models.model_dir(data, model), opener):
                return 1
    archive = backend.archive
    unverified = voice_models.verify(backend_folder, backend.files) is not None
    if (
        archive is not None
        and unverified
        and not _install_backend(backend, archive, backend_folder, opener)
    ):
        return 1
    print(f"uv run python scripts/voice_bench.py --smoke --model {model_id} --backend {backend_id}")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    opener: Callable[[str], BinaryIO] = voice_fetch.open_https,
    probe: Callable[[], voice_machine.Machine] = voice_machine.probe,
) -> int:
    args = _parser().parse_args(argv)
    try:
        data = config.data_dir(os.environ)
    except config.ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    refusal = _refusal(args.model, args.backend)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    if args.list:
        return _list(data, probe)
    if args.check:
        return _check(data)
    backend_id, model_id = args.backend, args.model
    if backend_id == "auto" or model_id is None:
        chosen = None if backend_id == "auto" else backend_id
        recommended_backend, recommended_model = voice_machine.recommend(probe(), chosen)
        backend_id = recommended_backend
        model_id = recommended_model if model_id is None else model_id
    return _install(data, model_id, backend_id, opener)


if __name__ == "__main__":
    sys.exit(main())
