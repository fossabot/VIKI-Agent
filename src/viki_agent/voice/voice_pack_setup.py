"""The voice pack's setup child process.

Fetches, verifies and unpacks the pinned Voicebox server and its Qwen3-TTS and Kokoro models
under ``<data>/voice/pack``, then records both installed model revisions. Run as::

    python -I -B voice_pack_setup.py install --data <dir> [--repair]

Compiled, that is instead ``<app> --viki-worker voice-pack-setup install --data <dir> [--repair]``.

``-I`` keeps this script's own folder off ``sys.path``, so sibling modules are reached through
the installed package (``from viki_agent import ...``), never a bare same-folder import.

Progress and the outcome are a line protocol on stdout: ASCII JSON, one object per line, flushed
after each write. While a step runs: ``{"type":"progress","step":...,"done":...,"total":...}``,
throttled to at most once per 0.25s (the first and last of a step always land). At the end,
exactly one of ``{"type":"done"}`` or ``{"type":"error","code":...}``, the code one of
``voice_pack.SETUP_CODES``. ``--repair`` clears a possibly-broken unpacked server tree first, so
a damaged install can be rebuilt without re-fetching the model.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from viki_agent import protection
from viki_agent.voice import voice_pack, voice_pack_fetch, voice_pack_unpack

_GIB = 1024**3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice_pack_setup.py", description=__doc__)
    parser.add_argument("action", choices=["install"])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--repair", action="store_true")
    return parser


def _is_regular_file(path: Path) -> bool:
    """Whether `path` is a regular, non-reparse file, from lstat alone (no hashing)."""
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISREG(info.st_mode) and not reparse


def _present_bytes(path: Path) -> int:
    """`path`'s size if it is already a regular file on disk, else 0."""
    return path.stat().st_size if _is_regular_file(path) else 0


def _disk_check(
    data_dir: Path,
    archives: tuple[voice_pack.PinnedFile, ...],
    files: tuple[voice_pack.PinnedFile, ...],
    kokoro: tuple[voice_pack.PinnedFile, ...],
    unpacked: int,
    disk_free: Callable[[Path], int | None],
) -> str | None:
    """`"disk_full"` if free space is known to fall short of what install still needs, run
    before any request. With the server exe in place the server step is skipped, so only the
    models count; bytes already on disk (a pin's final or `.part` file) count against the total
    so a resumed install does not need the same space twice."""
    need = sum(pin.size for pin in files) + sum(pin.size for pin in kokoro) + _GIB
    present = 0
    if not _is_regular_file(voice_pack.server_dir(data_dir) / voice_pack.SERVER_EXE):
        need += sum(pin.size for pin in archives) + unpacked
        downloads = voice_pack.downloads_dir(data_dir)
        for pin in archives:
            present += _present_bytes(downloads / pin.name)
            present += _present_bytes(downloads / (pin.name + ".part"))
    snapshot = voice_pack.snapshot_dir(data_dir)
    for pin in files:
        present += _present_bytes(snapshot / pin.name)
        present += _present_bytes(snapshot / (pin.name + ".part"))
    kokoro_snapshot = voice_pack.kokoro_snapshot_dir(data_dir)
    for pin in kokoro:
        present += _present_bytes(kokoro_snapshot / pin.name)
        present += _present_bytes(kokoro_snapshot / (pin.name + ".part"))
    free = disk_free(data_dir)
    return "disk_full" if free is not None and free < need - present else None


def _rmtree_if_real_dir(path: Path) -> None:
    """Delete `path` if it is a real directory and not a reparse point; a missing path is fine.
    Anything else sketchy at `path` raises, which surfaces as `write_failed`."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if reparse or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"refusing to remove a reparse point or non-directory: {path}")
    shutil.rmtree(path)


def _throttled(
    emit: Callable[[dict[str, object]], None], step: str, total: int
) -> Callable[[int], None]:
    """One step's progress line, throttled to at most 1/0.25s; the first and last calls always
    emit, however close together."""
    last_emitted: float | None = None

    def progress(done: int) -> None:
        nonlocal last_emitted
        now = time.monotonic()
        if last_emitted is None or done >= total or now - last_emitted >= 0.25:
            last_emitted = now
            emit({"type": "progress", "step": step, "done": done, "total": total})

    return progress


def _offset(progress: Callable[[int], None], base: int) -> Callable[[int], None]:
    """Shifts one pin's own 0..size progress by the bytes already done earlier in the step."""
    return lambda done: progress(base + done)


def _install_server(
    data_dir: Path,
    archives: tuple[voice_pack.PinnedFile, ...],
    opener: Callable[[str, int], voice_pack_fetch.Response],
    emit: Callable[[dict[str, object]], None],
) -> str | None:
    """Fetch and unpack the server archives into `s/`, if its exe is not already there."""
    total = sum(pin.size for pin in archives)
    downloads = voice_pack.downloads_dir(data_dir)
    fetch_progress = _throttled(emit, "server", total)
    done = 0
    for pin in archives:
        code = voice_pack_fetch.fetch(
            pin, downloads, opener=opener, progress=_offset(fetch_progress, done)
        )
        if code is not None:
            return code
        done += pin.size

    partial = voice_pack.partial_dir(data_dir)
    _rmtree_if_real_dir(partial)

    unpack_progress = _throttled(emit, "unpack", total)
    done = 0
    for pin in archives:
        code = voice_pack_unpack.extract(
            downloads / pin.name, partial, progress=_offset(unpack_progress, done)
        )
        if code is not None:
            return code
        done += pin.size

    server = voice_pack.server_dir(data_dir)
    # A server tree without its exe is broken: os.replace cannot rename onto a non-empty folder.
    _rmtree_if_real_dir(server)
    os.replace(partial, server)
    for pin in archives:
        (downloads / pin.name).unlink()
    return None if _is_regular_file(server / voice_pack.SERVER_EXE) else "bad_archive"


def _install_models(
    data_dir: Path,
    files: tuple[voice_pack.PinnedFile, ...],
    kokoro: tuple[voice_pack.PinnedFile, ...],
    opener: Callable[[str, int], voice_pack_fetch.Response],
    emit: Callable[[dict[str, object]], None],
) -> str | None:
    """Fetch the Qwen3 model files into the pinned snapshot directory, then Kokoro's into its
    own, both counted under one "model" progress total."""
    total = sum(pin.size for pin in files) + sum(pin.size for pin in kokoro)
    progress = _throttled(emit, "model", total)
    done = 0
    snapshot = voice_pack.snapshot_dir(data_dir)
    for pin in files:
        code = voice_pack_fetch.fetch(
            pin, snapshot, opener=opener, progress=_offset(progress, done)
        )
        if code is not None:
            return code
        done += pin.size
    kokoro_snapshot = voice_pack.kokoro_snapshot_dir(data_dir)
    for pin in kokoro:
        code = voice_pack_fetch.fetch(
            pin, kokoro_snapshot, opener=opener, progress=_offset(progress, done)
        )
        if code is not None:
            return code
        done += pin.size
    return None


def _install(
    data_dir: Path,
    *,
    repair: bool,
    opener: Callable[[str, int], voice_pack_fetch.Response],
    archives: tuple[voice_pack.PinnedFile, ...],
    files: tuple[voice_pack.PinnedFile, ...],
    kokoro: tuple[voice_pack.PinnedFile, ...] = voice_pack.KOKORO_FILES,
    unpacked: int,
    disk_free: Callable[[Path], int | None],
    emit: Callable[[dict[str, object]], None],
) -> str | None:
    pack = voice_pack.pack_dir(data_dir)
    try:
        for folder in (pack.parent, pack):
            if not protection.create_private_directory(folder):
                protection.require_private_directory(folder)
    except protection.ProtectionError, OSError:
        return "private_folder"

    # Repair first, so the disk check counts the server step it brings back.
    if repair:
        _rmtree_if_real_dir(voice_pack.server_dir(data_dir))
        _rmtree_if_real_dir(voice_pack.partial_dir(data_dir))

    disk_code = _disk_check(data_dir, archives, files, kokoro, unpacked, disk_free)
    if disk_code is not None:
        return disk_code

    if not _is_regular_file(voice_pack.server_dir(data_dir) / voice_pack.SERVER_EXE):
        code = _install_server(data_dir, archives, opener, emit)
        if code is not None:
            return code

    code = _install_models(data_dir, files, kokoro, opener, emit)
    if code is not None:
        return code

    voice_pack_unpack.write_ref(voice_pack.repo_dir(data_dir), voice_pack.MODEL_REVISION)
    voice_pack_unpack.write_ref(voice_pack.kokoro_repo_dir(data_dir), voice_pack.KOKORO_REVISION)
    return None


def main(
    argv: list[str] | None = None,
    *,
    opener: Callable[[str, int], voice_pack_fetch.Response] = voice_pack_fetch.open_range,
    out: TextIO = sys.stdout,
    archives: tuple[voice_pack.PinnedFile, ...] = voice_pack.ARCHIVES,
    files: tuple[voice_pack.PinnedFile, ...] = voice_pack.MODEL_FILES,
    kokoro: tuple[voice_pack.PinnedFile, ...] = voice_pack.KOKORO_FILES,
    unpacked: int = voice_pack.UNPACKED_BYTES,
    disk_free: Callable[[Path], int | None] = voice_pack.free_bytes,
) -> int:
    args = _parser().parse_args(argv)

    def emit(obj: dict[str, object]) -> None:
        out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        out.flush()

    try:
        code = _install(
            args.data,
            repair=args.repair,
            opener=opener,
            archives=archives,
            files=files,
            kokoro=kokoro,
            unpacked=unpacked,
            disk_free=disk_free,
            emit=emit,
        )
    except Exception:
        code = "write_failed"
    if code is not None:
        emit({"type": "error", "code": code})
        return 1
    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
