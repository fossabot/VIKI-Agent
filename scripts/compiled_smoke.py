"""Smoke-test the compiled app's workers (ADR-054).

    uv run --directory <repo> --group build python scripts/compiled_smoke.py [--app <binary>]

Runs each worker (media, voice, voice-pack-setup) straight from `scripts/compile.py`'s output,
through the same per-OS env allowlist, Job/address-space limits and argv prefix a real launch
uses (`media_worker._worker_env`/`_worker_argv_prefix`, `winproc.spawn`), and checks: no bundled
Claude Code ships inside the folder; a compiled run ignores `PYTHONPATH`, user site and
`PYTHONSTARTUP`; the media, avatar, voice and voice-pack-setup workers behave correctly,
the voice worker identically to its source counterpart; on Windows, the binary is a GUI
subsystem PE whose inherited pipes carry the worker protocols.

Prints one line per check, `PASS <name>` or `FAIL <name>: <reason>`, and exits 0 only when every
check passed. Never starts the binary without `--viki-worker`: that argv alone would start the
real app and, on this machine, wake whichever VIKI Agent already holds the single-instance lock
and port 8765.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from viki_agent import winproc
from viki_agent.compiled import APP_BINARY, WORKER_FLAG
from viki_agent.media import assets, avatars, media_worker
from viki_agent.voice import voice_models, voice_worker

REPO = Path(__file__).resolve().parents[1]
DEFAULT_APP = REPO / "dist" / "compiled" / "viki-agent.dist" / APP_BINARY
VOICE_WORKER_SCRIPT = Path(voice_worker.__file__)

_BUNDLED_CLI_NAMES = frozenset({"claude", "claude.exe"})
# Relative to the dist folder (the binary's own parent); scripts/compile.py's
# --include-data-dir stages viki_agent/resources there verbatim (ADR-054, build_product.stage).
_REQUIRED_RESOURCES = (
    Path("viki_agent/resources/ui/index.html"),
    Path("viki_agent/resources/ui/favicon.ico"),
    Path("viki_agent/resources/starter/Home.md"),
    Path("viki_agent/resources/welcome.en.md"),
)


# --- helpers reused by scripts/compiled_smoke_app.py --------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int) -> bytes:
    """An 8-bit RGB PNG of `width` x `height` all-zero pixels, built without Pillow so this
    script needs no image decoder of its own. Each scanline is streamed through one zlib
    compressor rather than buffered whole, so the 20-40 megapixel fixtures the avatar and media
    checks below use never hold their full pixel buffer in memory at once."""
    if width < 1 or height < 1:
        raise ValueError("png size must be positive")
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    compressor = zlib.compressobj()
    row = bytes(1 + width * 3)  # filter byte 0 (None), then all-zero RGB pixels
    compressed = bytearray()
    for _ in range(height):
        compressed += compressor.compress(row)
    compressed += compressor.flush()
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", bytes(compressed))
        + _chunk(b"IEND", b"")
    )


def fake_voice_models(voice_dir: str | os.PathLike[str]) -> None:
    """Zero-filled, correctly-sized stand-ins for `small-q8_0`'s pinned weights and the VAD,
    under `<voice_dir>/models/small-q8_0`. `voice_models.present` (what the worker's own
    `_admit` checks before it starts) looks only at byte size, never content, so this needs
    none of the real multi-hundred-MB download for the worker to admit the model as installed."""
    model_dir = Path(voice_dir) / "models" / "small-q8_0"
    model_dir.mkdir(parents=True, exist_ok=True)
    model = voice_models.MODELS["small-q8_0"]
    for pin in (model.weights, voice_models.VAD):
        with (model_dir / pin.name).open("wb") as handle:
            handle.truncate(pin.size)


def _plant_marker(path: Path, markers_dir: Path, name: str) -> None:
    """A tiny script at `path` whose only effect, imported or exec'd, is to create
    `markers_dir/name`."""
    target = (markers_dir / name).as_posix()
    source = f"import pathlib\npathlib.Path({target!r}).write_bytes(b'')\n"
    path.write_text(source, encoding="utf-8")


def isolation_probe(root: str | os.PathLike[str]) -> dict[str, str]:
    """Env overrides that would leave a trace under `root/markers/` if a compiled run honoured
    `PYTHONPATH`, user site or `PYTHONSTARTUP`: a `sitecustomize.py` on a `PYTHONPATH`
    folder, a `usercustomize.py` under the venv Python's own `site.getusersitepackages()` for a
    `PYTHONUSERBASE` folder here, and a `PYTHONSTARTUP` file. `markers()` reports what actually
    fired. A compiled worker loads no `site` module at all (Nuitka standalone's `no_site`) and
    is never the interactive interpreter, so none of the three should ever run; this probe would
    still catch a regression (a future Nuitka build re-enabling site processing, or a worker
    launch site dropping its env allowlist) instead of passing silently.
    """
    root = Path(root)
    markers_dir = root / "markers"
    markers_dir.mkdir(parents=True, exist_ok=True)

    pythonpath_dir = root / "pythonpath"
    pythonpath_dir.mkdir(exist_ok=True)
    _plant_marker(pythonpath_dir / "sitecustomize.py", markers_dir, "pythonpath")

    userbase = root / "userbase"
    userbase.mkdir(exist_ok=True)
    # site.py caches USER_BASE/USER_SITE the first time anything calls getusersitepackages(),
    # which main() already does at interpreter start-up regardless of -I/-s; a child started
    # with PYTHONUSERBASE already in ITS environment is the only way to make that first call see
    # it, so the override goes on this process's os.environ for the one spawn, then comes off.
    previous = os.environ.get("PYTHONUSERBASE")
    os.environ["PYTHONUSERBASE"] = str(userbase)
    try:
        done = winproc.run(
            [sys.executable, "-I", "-B", "-c", "import site; print(site.getusersitepackages())"],
            cwd=root,
            timeout=15,
        )
    finally:
        if previous is None:
            os.environ.pop("PYTHONUSERBASE", None)
        else:
            os.environ["PYTHONUSERBASE"] = previous
    if done.returncode != 0:
        raise RuntimeError(f"could not compute the user site-packages folder: {done.stderr}")
    user_site = Path(done.stdout.strip())
    user_site.mkdir(parents=True, exist_ok=True)
    _plant_marker(user_site / "usercustomize.py", markers_dir, "userbase")

    startup = root / "startup.py"
    _plant_marker(startup, markers_dir, "pythonstartup")

    return {
        "PYTHONPATH": str(pythonpath_dir),
        "PYTHONUSERBASE": str(userbase),
        "PYTHONSTARTUP": str(startup),
    }


def markers(root: str | os.PathLike[str]) -> list[str]:
    """The names of any isolation-probe markers that actually appeared under `root`."""
    directory = Path(root) / "markers"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir())


async def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    stdin: bytes = b"",
    memory_limit_bytes: int | None = None,
    timeout_s: float = 60,
) -> tuple[int, bytes, bytes]:
    """Run `argv` to completion through `winproc.spawn`, feeding `stdin` and collecting both
    streams. On a `timeout_s` hang the whole tree is killed and released, never raised, so one
    wedged check cannot hang the rest of the smoke; the returned exit code then reflects the
    kill (Windows: the Job's `terminate(1)`; POSIX: SIGKILL)."""
    child = await winproc.spawn(
        list(argv), cwd=cwd, env=dict(env), memory_limit_bytes=memory_limit_bytes
    )
    try:
        communication = asyncio.ensure_future(child.process.communicate(input=stdin))
        try:
            async with asyncio.timeout(timeout_s):
                stdout, stderr = await asyncio.shield(communication)
        except TimeoutError:
            child.kill_tree()
            stdout, stderr = await communication
        assert child.process.returncode is not None
        return child.process.returncode, stdout, stderr
    finally:
        child.release()


def bundled_cli_files(dist: str | os.PathLike[str]) -> list[Path]:
    """Every regular file under `dist` that would ship Claude Code: inside a
    `claude_agent_sdk/_bundled` folder, or named exactly `claude` or `claude.exe` (case-folded,
    never a prefix match, so the product's own `CLAUDE.template.md` is left alone)."""
    dist = Path(dist)
    found: list[Path] = []
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(dist).parts
        under_bundled = any(
            parts[index : index + 2] == ("claude_agent_sdk", "_bundled")
            for index in range(len(parts) - 1)
        )
        if under_bundled or path.name.lower() in _BUNDLED_CLI_NAMES:
            found.append(path)
    return found


def pe_subsystem(exe: str | os.PathLike[str]) -> int:
    """The `Subsystem` field of `exe`'s PE optional header: 2 is GUI, 3 is console. The field
    sits at the same offset (68) in both the PE32 and PE32+ optional header layouts, so the
    format's own magic number is read but never branched on."""
    path = Path(exe)
    with path.open("rb") as handle:
        dos = handle.read(64)
        if len(dos) < 64 or dos[:2] != b"MZ":
            raise ValueError(f"not a PE file: {path}")
        (header_offset,) = struct.unpack_from("<I", dos, 0x3C)
        handle.seek(header_offset)
        if handle.read(4) != b"PE\x00\x00":
            raise ValueError(f"not a PE file: {path}")
        coff = handle.read(20)
        (size_of_optional_header,) = struct.unpack_from("<H", coff, 16)
        optional = handle.read(size_of_optional_header)
        if len(optional) < 70:
            raise ValueError(f"optional header too small: {path}")
        (subsystem,) = struct.unpack_from("<H", optional, 68)
        return subsystem


# --- the checks ----------------------------------------------------------------------------


def _compiled_env(probe_env: Mapping[str, str]) -> dict[str, str]:
    env = media_worker._worker_env(sys.platform, os.environ)
    env.update(probe_env)
    return env


async def _check_dist_scan(app: Path) -> tuple[bool, str]:
    dist = app.parent
    bundled = bundled_cli_files(dist)
    if bundled:
        return False, f"bundled Claude Code present: {[str(p) for p in bundled]}"
    missing = [str(relative) for relative in _REQUIRED_RESOURCES if not (dist / relative).is_file()]
    if missing:
        return False, f"missing packaged resources: {missing}"
    files = [path for path in dist.rglob("*") if path.is_file()]
    size_mb = sum(path.stat().st_size for path in files) / 1_000_000
    print(f"dist: {len(files)} files, {size_mb:.1f} MB")
    return True, ""


async def _check_pe_subsystem(app: Path) -> tuple[bool, str]:
    if sys.platform != "win32":
        return True, "skipped (not Windows)"
    subsystem = pe_subsystem(app)
    if subsystem != 2:
        return False, f"subsystem {subsystem}, expected 2 (GUI)"
    return True, ""


async def _check_unknown_worker(app: Path, env: Mapping[str, str]) -> tuple[bool, str]:
    code, stdout, _stderr = await run([str(app), WORKER_FLAG, "nope"], cwd=app.parent, env=env)
    if code != 2:
        return False, f"exit {code}, expected 2"
    if stdout:
        return False, f"stdout not empty: {stdout!r}"
    return True, ""


async def _check_media_inspect(
    app: Path, root: Path, probe_env: Mapping[str, str]
) -> tuple[bool, str]:
    media_dir = root / "media"
    media_dir.mkdir(exist_ok=True)
    (media_dir / "source").write_bytes(png(16, 16))
    argv = [
        str(app),
        WORKER_FLAG,
        "media",
        *media_worker._worker_argv_prefix(sys.platform, assets.WORKER_MEMORY_BYTES),
        "source",
    ]
    code, stdout, stderr = await run(
        argv,
        cwd=media_dir,
        env=_compiled_env(probe_env),
        memory_limit_bytes=assets.WORKER_MEMORY_BYTES,
    )
    if code != 0:
        return False, f"exit {code}: {stderr[-400:]!r}"
    try:
        answer = json.loads(stdout)
    except ValueError:
        return False, f"not JSON: {stdout!r}"
    if (
        answer.get("ok") is not True
        or answer.get("media_type") != "image/png"
        or answer.get("width") != 16
        or answer.get("height") != 16
    ):
        return False, f"unexpected answer: {answer}"
    return True, ""


async def _check_avatar_transform(
    app: Path, root: Path, probe_env: Mapping[str, str]
) -> tuple[bool, str]:
    avatar_dir = root / "avatar"
    avatar_dir.mkdir(exist_ok=True)
    (avatar_dir / "source").write_bytes(png(5000, 4000))
    argv = [
        str(app),
        WORKER_FLAG,
        "media",
        *media_worker._worker_argv_prefix(sys.platform, avatars.WORKER_ADDRESS_SPACE_BYTES),
        "source",
        "--avatar",
        "0",
        "0",
        "4000",
        "0",
    ]
    code, stdout, stderr = await run(
        argv,
        cwd=avatar_dir,
        env=_compiled_env(probe_env),
        memory_limit_bytes=avatars.WORKER_MEMORY_BYTES,
    )
    if code != 0:
        return False, f"exit {code}: {stderr[-400:]!r}"
    try:
        answer = json.loads(stdout)
    except ValueError:
        return False, f"not JSON: {stdout!r}"
    variants = answer.get("variants", [])
    if answer.get("ok") is not True or "master" not in answer or len(variants) != 5:
        return False, f"unexpected answer: {answer}"
    return True, ""


async def _check_voice_worker(
    app: Path, root: Path, probe_env: Mapping[str, str]
) -> tuple[bool, str]:
    voice_dir = root / "voice"
    voice_dir.mkdir(exist_ok=True)
    fake_voice_models(voice_dir)
    memory = voice_models.WORKER_MEMORY_BYTES["cpu"]

    compiled_code, compiled_out, _compiled_err = await run(
        [str(app), WORKER_FLAG, "voice", "small-q8_0", "cpu"],
        cwd=voice_dir,
        env=_compiled_env(probe_env),
        memory_limit_bytes=memory,
    )
    source_code, source_out, _source_err = await run(
        [sys.executable, "-I", "-B", str(VOICE_WORKER_SCRIPT), "small-q8_0", "cpu"],
        cwd=voice_dir,
        env=media_worker._worker_env(sys.platform, os.environ),
        memory_limit_bytes=memory,
    )
    if compiled_code == 3:
        return False, f"compiled exited 3 (engine_unavailable): {compiled_out!r}"
    if compiled_code != source_code:
        return False, f"exit differs: compiled {compiled_code} vs source {source_code}"
    compiled_first = compiled_out.splitlines()[0] if compiled_out else b""
    source_first = source_out.splitlines()[0] if source_out else b""
    if compiled_first != source_first:
        return False, f"first line differs: compiled {compiled_first!r} vs source {source_first!r}"
    return True, ""


async def _check_voice_pack_setup_help(app: Path, env: Mapping[str, str]) -> tuple[bool, str]:
    code, stdout, _stderr = await run(
        [str(app), WORKER_FLAG, "voice-pack-setup", "--help"], cwd=app.parent, env=env
    )
    if code != 0:
        return False, f"exit {code}, expected 0"
    if b"usage: voice_pack_setup.py" not in stdout:
        return False, f"unexpected help text: {stdout[:200]!r}"
    return True, ""


async def _check_markers(isolation_root: Path) -> tuple[bool, str]:
    found = markers(isolation_root)
    if found:
        return False, f"unexpected markers fired: {found}"
    return True, ""


async def _run_all(app: Path, root: Path) -> list[tuple[str, bool, str]]:
    base_env = media_worker._worker_env(sys.platform, os.environ)
    isolation_root = root / "isolation"
    probe_env = isolation_probe(isolation_root)

    checks = [
        ("dist_scan", _check_dist_scan(app)),
        ("pe_subsystem", _check_pe_subsystem(app)),
        ("unknown_worker", _check_unknown_worker(app, base_env)),
        ("media_inspect", _check_media_inspect(app, root, probe_env)),
        ("avatar_transform", _check_avatar_transform(app, root, probe_env)),
        ("voice_worker", _check_voice_worker(app, root, probe_env)),
        ("voice_pack_setup_help", _check_voice_pack_setup_help(app, _compiled_env(probe_env))),
        ("isolation_markers", _check_markers(isolation_root)),
    ]
    results: list[tuple[str, bool, str]] = []
    for name, coro in checks:
        try:
            ok, detail = await coro
        except Exception as exc:  # one check's failure must never crash the rest of the smoke
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    args = parser.parse_args(argv)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="viki-compiled-smoke-") as scratch:
        results = asyncio.run(_run_all(args.app, Path(scratch)))
    elapsed = time.monotonic() - started
    for name, ok, detail in results:
        print(f"PASS {name}" if ok else f"FAIL {name}: {detail}")
    print(f"compiled smoke: {elapsed:.1f}s")
    return 0 if all(ok for _name, ok, _detail in results) else 1


if __name__ == "__main__":
    sys.exit(main())
