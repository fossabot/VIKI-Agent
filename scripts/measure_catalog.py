"""Measure the catalog's read budgets and print the table they were chosen from.

    uv run --directory <repo> python scripts/measure_catalog.py --out <dir> --mib 200 --largest 96
    uv run --directory <repo> python scripts/measure_catalog.py --real <vault>

By default it writes a synthetic corpus under `--out` (invented Japanese and English text from
tests/synthetic_transcripts.py — never a transcript of the user's) and times a listing, a cold
parse, a page, concurrent parses and the worst-case search over it, with heap figures from
tracemalloc. It writes nothing outside `--out`.

`--real` reads the user's own `~/.claude` transcripts of the sessions run in `<vault>` instead,
to settle the two numbers a synthetic corpus cannot: the heap multipliers on real transcripts and
whether a tail window of the largest one yields the same newest prompts as a whole parse. A file
above the whole-read budget has no whole-parse measurement or comparison. It opens files
read-only, writes nothing at all, and prints sizes, timings and counts only — never a title, a
prompt, a path or a session id.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import gc
import io
import json
import logging
import shutil
import statistics
import struct
import sys
import tempfile
import threading
import time
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))  # the one generator lives beside the tests that use it

from synthetic_transcripts import ABSENT, session_id, write_corpus  # noqa: E402

from viki_agent import catalog as cat  # noqa: E402
from viki_agent import config, protection  # noqa: E402
from viki_agent.catalog import Catalog, Event, project_dir  # noqa: E402
from viki_agent.media import assets as assets_mod  # noqa: E402
from viki_agent.media.assets import AssetService, DecodedImage  # noqa: E402
from viki_agent.storage import SessionStore  # noqa: E402

VAULT = Path("C:/Vault")
MIB = 2**20
IMAGE_BYTES = 5 * MIB  # the native cap on one history image (mapper.py's _HISTORY_IMAGE_MAX_BYTES)


def row(measurement: str, value: str) -> None:
    print(f"| {measurement} | {value} |", flush=True)


def timed[T](call: Callable[[], T], *, repeat: int = 5) -> tuple[T, float, float]:
    """(last result, p50, p95) over `repeat` runs."""
    samples: list[float] = []
    result: T | None = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = call()
        samples.append(time.perf_counter() - start)
    samples.sort()
    p95 = samples[min(len(samples) - 1, round(0.95 * (len(samples) - 1)))]
    assert result is not None
    return result, statistics.median(samples), p95


@dataclass(frozen=True)
class Parse:
    read_s: float  # read + json.loads of every line
    chain_s: float  # the leaf-and-walk
    entries: int
    chain: int
    peak_x: float  # heap peak during the parse, as a multiple of the bytes parsed
    held_x: float  # heap still held by the chain afterwards, same units


def _parse(path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], float, float]:
    """(entries, chain, read+parse s, chain s) — the phases of a cold parse of `path`."""
    start = time.perf_counter()
    entries = cat._read_entries(path)  # noqa: SLF001 - measuring this module's own internals
    read_s = time.perf_counter() - start
    chain_types = cat._CHAIN_TYPES  # noqa: SLF001
    typed = [e for e in entries if e.get("type") in chain_types and isinstance(e.get("uuid"), str)]
    start = time.perf_counter()
    chain = cat._build_chain(typed)  # noqa: SLF001
    return entries, chain, read_s, time.perf_counter() - start


def parse_phases(path: Path) -> Parse | None:
    """One whole parse of `path`, timed, and a second one measured; None above the read cap.

    Two passes because tracemalloc hooks every allocation and roughly triples the cost of a
    parse this allocation-heavy: timing and heap taken together would each be wrong. Heap from
    tracemalloc rather than RSS, because the allocator's own retention is not what the budgets
    are chosen against."""
    size = path.stat().st_size
    if size > cat.FULL_READ_BYTES:
        return None
    gc.collect()
    entries, chain, read_s, chain_s = _parse(path)
    counts = (len(entries), len(chain))
    del entries, chain
    gc.collect()

    tracemalloc.start()
    entries, chain, _, _ = _parse(path)
    _, peak = tracemalloc.get_traced_memory()
    del entries
    gc.collect()
    held, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del chain
    return Parse(read_s, chain_s, counts[0], counts[1], peak / size, held / size)


def concurrent_reads(home: Path, ids: list[str]) -> tuple[float, list[float]]:
    """Two cold reads at once through one Catalog: wall time and each call's own time."""
    reader = Catalog(home, VAULT)
    each: list[float] = []
    lock = threading.Lock()

    def one(session: str) -> None:
        begin = time.perf_counter()
        reader.history(session)
        with lock:
            each.append(time.perf_counter() - begin)

    threads = [threading.Thread(target=one, args=(session,)) for session in ids]
    start = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.perf_counter() - start, sorted(each)


def prompts_of(events: list[Event]) -> list[str]:
    return [getattr(e, "text", "") for e in events if getattr(e, "TYPE", "") == "user.message"]


def measure_synthetic(out: Path, total_mib: int, largest_mib: int) -> None:
    home = out / "home"
    folder = project_dir(home, VAULT)
    if not list(folder.glob("*.jsonl")):
        print(f"writing ~{total_mib} MiB of synthetic transcripts under {out} ...", flush=True)
        write_corpus(folder, total_mib=total_mib, largest_mib=largest_mib)
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    total = sum(p.stat().st_size for p in files)
    biggest = files[0]
    big_mib = biggest.stat().st_size / MIB
    print(f"python {sys.version.split()[0]}")
    print(f"corpus: {len(files)} files, {total / MIB:.1f} MiB, largest {big_mib:.1f} MiB")
    print(
        f"budgets: full {cat.FULL_READ_BYTES / MIB:.0f} MiB, "
        f"tail {cat.TAIL_WINDOW_BYTES / MIB:.0f} MiB, "
        f"head {cat.HEAD_WINDOW_BYTES / MIB:.0f} MiB, permits {cat.PARSE_PERMITS}"
    )
    print()
    print("| measurement | value |")
    print("|---|---|")

    reader = Catalog(home, VAULT)
    start = time.perf_counter()
    cold = reader.list_sessions()
    cold_s = time.perf_counter() - start
    _, warm_p50, _ = timed(reader.list_sessions)
    row(
        "`list_sessions()` cold / warm",
        f"{cold_s:.3f} s / {warm_p50 * 1000:.1f} ms ({len(cold.sessions)} rows, {cold.state})",
    )

    parse = parse_phases(biggest)
    if parse is None:
        row("whole-parse measurement unavailable", "largest file exceeds FULL_READ_BYTES")
    else:
        row(
            f"cold parse of the largest file ({big_mib:.0f} MiB)",
            f"read+parse {parse.read_s:.3f} s, chain {parse.chain_s:.3f} s, "
            f"{parse.entries} entries, {parse.chain} in chain",
        )
        row("ms per MiB parsed", f"{(parse.read_s + parse.chain_s) * 1000 / big_mib:.1f}")
        row(
            "heap during that parse / retained after it",
            f"{parse.peak_x:.2f}x / {parse.held_x:.2f}x the bytes parsed",
        )

    fresh = Catalog(home, VAULT)
    sid = biggest.stem
    start = time.perf_counter()
    page = fresh.history(sid)
    history_s = time.perf_counter() - start
    _, page_p50, _ = timed(lambda: fresh.history(sid))
    row(
        "`history()` cold / warm on it",
        f"{history_s:.3f} s / {page_p50 * 1000:.1f} ms ({len(page.events)} events, {page.status})",
    )

    wall, each = concurrent_reads(home, [sid, sid])
    row("2 concurrent cold reads, the same file", f"wall {wall:.3f} s, each {each}")
    medium = [f.stem for f in files[1:3]]
    sizes = [round((folder / f"{s}.jsonl").stat().st_size / MIB, 1) for s in medium]
    wall, each = concurrent_reads(home, medium)
    row(f"2 concurrent cold reads, {sizes} MiB", f"wall {wall:.3f} s, each {each}")

    warm = Catalog(home, VAULT)
    warm.list_sessions()
    for query in ("通し番号", "dentist thursday", ABSENT):
        found, p50, p95 = timed(lambda q=query: warm.run_search(q), repeat=7)
        row(
            f"`run_search({query!r})` p50 / p95",
            f"{p50:.3f} s / {p95:.3f} s ({len(found.hits)} hits, {found.state}, "
            f"{found.scanned}/{found.total} scanned)",
        )

    # The worst case for one file: the prefilter's probe matches thousands of lines, so each is
    # parsed and mapped, and the whole query never matches any of them.
    listed = next(r for r in cold.sessions if r.session_id == sid)
    needle, probe = b"dentist thursday", b"thursday"
    start = time.perf_counter()
    warm._search_transcript(  # noqa: SLF001
        biggest, biggest.stat().st_size, listed, needle, probe, probe, "dentist thursday", 3
    )
    row(f"one worst-case file scan ({big_mib:.0f} MiB)", f"{time.perf_counter() - start:.3f} s")
    print()
    print(f"SEARCH_DEADLINE_S is {cat.SEARCH_DEADLINE_S} s. It is met by stopping between")
    print("sessions, not by finishing: a p95 above it is a visible partial result, a measured")
    print("gap rather than passing evidence.")


def _silence_library_warnings() -> None:
    """catalog/config log a path at WARNING when they skip an unreadable file (config.py's
    `log.warning("ignoring unreadable %s: ...", path, ...)`). Nothing here installs a handler
    for the `viki_agent` logger, so with none configured Python's own fallback would print
    that path to stderr — the one way this script could break its own "no path" promise."""
    pkg_log = logging.getLogger("viki_agent")
    pkg_log.addHandler(logging.NullHandler())
    pkg_log.propagate = False


def measure_real(vault: Path) -> None:
    """Read-only over the user's home. Numbers only: no title, prompt, path or session id."""
    _silence_library_warnings()
    cfg = config.load()
    folder = project_dir(cfg.claude_home, vault)
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        print("no transcripts in the vault's project folder")
        return
    total = sum(p.stat().st_size for p in files)
    biggest = files[0]
    over = sum(1 for p in files if p.stat().st_size > cat.FULL_READ_BYTES)
    print(f"python {sys.version.split()[0]}")
    print(f"corpus: {len(files)} files, {total / MIB:.1f} MiB")
    print(f"largest {biggest.stat().st_size / MIB:.1f} MiB; over FULL_READ_BYTES: {over}")

    parse = parse_phases(biggest)
    if parse is None:
        print("largest file: whole-parse measurement unavailable (above FULL_READ_BYTES)")
        print("newest 20 comparison unavailable: whole file exceeds FULL_READ_BYTES")
        return
    print(
        f"largest file: read+parse {parse.read_s:.3f} s, chain {parse.chain_s:.3f} s, "
        f"{parse.entries} entries, {parse.chain} in chain"
    )
    print(
        f"heap during that parse {parse.peak_x:.2f}x its bytes, retained after {parse.held_x:.2f}x"
    )

    # Risk 7: does a tail window of the largest transcript give the same newest prompts as a
    # whole parse? Counts and one boolean — no prompt is printed, compared against a file, or kept.
    whole = Catalog(cfg.claude_home, vault).history(biggest.stem)
    if whole.status != "complete":
        print(f"newest 20 comparison unavailable: whole baseline is {whole.status}")
        return
    original = cat.FULL_READ_BYTES
    try:
        cat.FULL_READ_BYTES = 0  # force the window path onto a file that fits whole today
        windowed = Catalog(cfg.claude_home, vault).history(biggest.stem)
    finally:
        cat.FULL_READ_BYTES = original
    same = prompts_of(whole.events)[-20:] == prompts_of(windowed.events)[-20:]
    print(
        f"newest page: whole {len(prompts_of(whole.events))} prompts ({whole.status}), "
        f"tail window {len(prompts_of(windowed.events))} ({windowed.status}); "
        f"newest 20 identical: {same}"
    )


if sys.platform == "win32":

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]

    def peak_pagefile_usage() -> int:
        """This process's PeakPagefileUsage in bytes, via GetProcessMemoryInfo."""
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = _kernel32.GetCurrentProcess()
        if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.PeakPagefileUsage

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]

    def free_physical_bytes() -> int:
        """Free physical memory in bytes, via GlobalMemoryStatusEx."""
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return status.ullAvailPhys


else:
    import os
    import resource

    def peak_pagefile_usage() -> int:
        """Peak resident size in bytes (POSIX has no page-file counter)."""
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )

    def free_physical_bytes() -> int:
        """Free physical memory in bytes (on macOS an upper bound: total physical memory)."""
        if sys.platform == "darwin":
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _build_png(index: int, target_bytes: int) -> bytes:
    """A valid grayscale PNG of about `target_bytes`, built with zlib/struct only (no Pillow).

    IDAT is a stored (uncompressed) zlib block, so building it is a memcpy, not real
    compression work, and every image is the same predictable size."""
    width = 4095
    height = max(1, (target_bytes - 4096) // (width + 1))  # 4096 B headroom for chunk overhead
    raw = bytes(width + 1) * height  # one zero filter byte + zero pixels, per row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    compressor = zlib.compressobj(level=0)
    idat = compressor.compress(raw) + compressor.flush()
    comment = _png_chunk(b"tEXt", f"cmt\x00image-{index}".encode("ascii"))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + comment
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _write_image_transcript(folder: Path, sid: str, images: list[bytes], turns: int) -> None:
    """`turns` user turns, each carrying every image in `images`, in the CLI's transcript shape."""
    folder.mkdir(parents=True, exist_ok=True)
    encoded = [base64.b64encode(image).decode("ascii") for image in images]
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
        for data in encoded
    ]
    count = 0
    last: str | None = None
    stamp = 1_760_000_000
    with (folder / f"{sid}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:

        def emit(kind: str, **fields: object) -> None:
            nonlocal count, last, stamp
            count += 1
            stamp += 1
            uid = f"{sid[:8]}-0000-4000-8000-{count:012x}"
            fh.write(
                json.dumps(
                    {
                        "parentUuid": last,
                        "isSidechain": False,
                        "userType": "external",
                        "cwd": "C:\\Vault",
                        "sessionId": sid,
                        "version": "2.1.266",
                        "entrypoint": "viki-agent",
                        "type": kind,
                        "uuid": uid,
                        "timestamp": (
                            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp)) + ".000Z"
                        ),
                        **fields,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            last = uid

        for turn in range(turns):
            emit("user", message={"role": "user", "content": blocks})
            emit(
                "assistant",
                message={
                    "id": f"msg_{count:06d}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"received turn {turn}"}],
                },
            )
        fh.write(
            json.dumps(
                {"type": "ai-title", "aiTitle": "images", "sessionId": sid}, ensure_ascii=False
            )
            + "\n"
        )


@contextlib.contextmanager
def _unprotected_assets() -> Iterator[None]:
    """The measurement's own store and blobs live under a plain TemporaryDirectory, not a real
    private ACL: patch the ACL checks the same way the asset tests do (tests/media/test_assets.py's
    `service_parts` fixture), so this measures decode and lookup cost, not Windows ACL
    plumbing."""
    protected_names = (
        "require_private_directory",
        "require_private_file",
        "create_private_directory",
    )
    saved = {name: getattr(protection, name) for name in protected_names}
    saved_store_check = assets_mod.require_private_store
    protection.require_private_directory = lambda path: None
    protection.require_private_file = lambda path: None
    protection.create_private_directory = lambda path: (path.mkdir(exist_ok=True), True)[1]
    assets_mod.require_private_store = lambda store: None
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(protection, name, fn)
        assets_mod.require_private_store = saved_store_check


async def _stub_decoder(_stage: Path) -> DecodedImage:
    """No Pillow here: the app's own image worker decodes bytes, this only needs dimensions."""
    return DecodedImage("image/png", 64, 64)


async def _publish_images(service: AssetService, images: list[bytes]) -> None:
    for index, image in enumerate(images):

        async def one(data: bytes = image) -> AsyncIterator[bytes]:
            yield data

        await service.upload(one(), name=f"history-image-{index}.png", declared_bytes=len(image))


@dataclass(frozen=True)
class ImageCase:
    unbound_time_s: float
    unbound_peak: int
    unbound_pagefile: int
    unbound_status: str
    unbound_reason: str
    bound_time_s: float
    bound_peak: int
    bound_pagefile: int
    bound_status: str
    bound_reason: str


def _timed_history(make_catalog: Callable[[], Catalog], sid: str) -> tuple[float, int, str, str]:
    """(time_s, tracemalloc peak bytes, page status, page reason) of one cold `history()` read,
    each on a fresh Catalog so nothing is served from an in-process cache."""
    gc.collect()
    start = time.perf_counter()
    page = make_catalog().history(sid)
    elapsed = time.perf_counter() - start
    gc.collect()

    tracemalloc.start()
    make_catalog().history(sid)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    return elapsed, peak, page.status, page.reason


def _measure_case(root: Path, images: list[bytes], turns: int) -> ImageCase:
    home = root / "home"
    folder = project_dir(home, VAULT)
    sid = session_id(0)
    _write_image_transcript(folder, sid, images, turns)

    private_root = root / "private"
    private_root.mkdir(parents=True)
    with _unprotected_assets():
        store = SessionStore(root / "state.sqlite3", lambda: {})
        service = AssetService(private_root / "assets", store, "a" * 64, decoder=_stub_decoder)
        asyncio.run(_publish_images(service, images))

        def unbound() -> Catalog:
            return Catalog(home, VAULT)

        def bound() -> Catalog:
            catalog = Catalog(home, VAULT)
            catalog.bind_history_media(service.find_by_digest)
            return catalog

        unbound_time, unbound_peak, unbound_status, unbound_reason = _timed_history(unbound, sid)
        unbound_pagefile = peak_pagefile_usage()
        bound_time, bound_peak, bound_status, bound_reason = _timed_history(bound, sid)
        bound_pagefile = peak_pagefile_usage()

    return ImageCase(
        unbound_time_s=unbound_time,
        unbound_peak=unbound_peak,
        unbound_pagefile=unbound_pagefile,
        unbound_status=unbound_status,
        unbound_reason=unbound_reason,
        bound_time_s=bound_time,
        bound_peak=bound_peak,
        bound_pagefile=bound_pagefile,
        bound_status=bound_status,
        bound_reason=bound_reason,
    )


def _page_status(status: str, reason: str) -> str:
    """'complete', or 'partial (tail_budget)': the page's own status plus why, when it has one."""
    return f"{status} ({reason})" if reason else status


def _print_images_table(case: ImageCase, turns: int, images_per_turn: int) -> None:
    print(f"python {sys.version.split()[0]}")
    raw_mib = turns * images_per_turn * IMAGE_BYTES / MIB
    print(
        f"page: {turns} turns x {images_per_turn} images x {IMAGE_BYTES / MIB:.0f} MiB "
        f"({raw_mib:.0f} MiB before base64)"
    )
    print()
    print("| measurement | value |")
    print("|---|---|")
    row("unbound: page status", _page_status(case.unbound_status, case.unbound_reason))
    row("unbound: `history()` time", f"{case.unbound_time_s:.3f} s")
    row("unbound: tracemalloc peak", f"{case.unbound_peak / MIB:.1f} MiB")
    row(
        "unbound: peak memory (PeakPagefileUsage on Windows, peak RSS on POSIX;"
        " process peak so far)",
        f"{case.unbound_pagefile / MIB:.1f} MiB",
    )
    row("bound: page status", _page_status(case.bound_status, case.bound_reason))
    row("bound: `history()` time", f"{case.bound_time_s:.3f} s")
    row("bound: tracemalloc peak", f"{case.bound_peak / MIB:.1f} MiB")
    row(
        "bound: peak memory (PeakPagefileUsage on Windows, peak RSS on POSIX; process peak so far)",
        f"{case.bound_pagefile / MIB:.1f} MiB",
    )


def _image_byte_estimate(turns: int, images_per_turn: int) -> tuple[int, int, int]:
    """(build_bytes, per_turn_bytes, write_bytes): what one turn of images costs in memory while
    `_write_image_transcript` runs, and what the run leaves on disk.

    Memory: the images list and its base64 copy are held for the whole write (raw + encoded).
    Each `emit()` call then briefly holds three more encoded-sized copies at once: the turn's
    `json.dumps` string, its `+ "\\n"` concatenation, and the UTF-8 bytes `fh.write` encodes it
    to. So the build peak is about `raw + 4 * encoded` (about 25 MiB per 5 MiB image, measured).

    Disk: `measure_images` keeps a `small` case (up to 2 turns) and a `full` case (`turns`
    turns) alive under the same TemporaryDirectory at once, each writing its own transcript
    (`per_turn_bytes` per turn) and publishing the raw images once into its own asset store. So
    the run's disk footprint is about `(turns + 2) * per_turn_bytes` for the two transcripts
    plus `2 * raw_bytes` for the two asset stores' raw copies.
    """
    raw_bytes = images_per_turn * IMAGE_BYTES
    encoded_bytes = raw_bytes * 4 // 3  # base64 overhead
    build_bytes = raw_bytes + 4 * encoded_bytes
    write_bytes = (turns + 2) * encoded_bytes + 2 * raw_bytes
    return build_bytes, encoded_bytes, write_bytes


def measure_images(turns: int, images_per_turn: int) -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    # The build (the raw+encoded images) and the write (the transcript file) both scale
    # directly with the arguments, unlike a real read, which the catalog always caps. Check
    # them before building anything, not after a run has already spent the memory or the disk.
    build_bytes, per_turn_bytes, write_bytes = _image_byte_estimate(turns, images_per_turn)
    free_mem = free_physical_bytes()
    if build_bytes > free_mem // 2:
        raise SystemExit(
            f"memory guard: {images_per_turn} images need about {build_bytes / MIB:.0f} MiB to "
            f"build, over half the {free_mem / MIB:.0f} MiB free. Lower --images-per-turn."
        )
    free_disk = shutil.disk_usage(tempfile.gettempdir()).free
    if write_bytes > free_disk // 2:
        raise SystemExit(
            f"disk guard: {turns} turns x {images_per_turn} images need about "
            f"{write_bytes / MIB:.0f} MiB on disk, over half the {free_disk / MIB:.0f} MiB "
            f"free. Lower --turns or --images-per-turn."
        )

    images = [_build_png(index, IMAGE_BYTES) for index in range(images_per_turn)]
    with tempfile.TemporaryDirectory(prefix="viki-measure-images-") as tmp:
        tmp_path = Path(tmp)
        small_turns = min(2, turns)
        small = _measure_case(tmp_path / "small", images, small_turns)
        if small_turns >= turns:
            _print_images_table(small, turns, images_per_turn)
            return

        # A real read never parses more than the catalog's own budget: a whole file under
        # FULL_READ_BYTES, or a TAIL_WINDOW_BYTES tail past that. Project the read's
        # peak against that cap, not against turns directly, or the guard would refuse a page a
        # real read handles fine by falling back to the tail window.
        read_cap = write_bytes if write_bytes <= cat.FULL_READ_BYTES else cat.TAIL_WINDOW_BYTES
        small_file_bytes = max(small_turns * per_turn_bytes, 1)
        projected_peak = small.bound_peak * (read_cap / small_file_bytes)
        if projected_peak > free_mem / 2:
            print(
                f"memory guard: {small_turns} turns already used "
                f"{small.bound_peak / MIB:.0f} MiB (tracemalloc peak, bound case); the read the "
                f"catalog would do at this page's size projects to {projected_peak / MIB:.0f} "
                f"MiB, over half the {free_mem / MIB:.0f} MiB free. Showing the {small_turns}-turn "
                f"run instead of the full size."
            )
            _print_images_table(small, small_turns, images_per_turn)
            return

        full = _measure_case(tmp_path / "full", images, turns)
        _print_images_table(full, turns, images_per_turn)


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # the table names a Japanese query
    parser = argparse.ArgumentParser(description="Measure catalog read budgets.")
    parser.add_argument("--out", type=Path, help="folder for the synthetic corpus")
    parser.add_argument("--mib", type=int, default=200, help="total corpus size")
    parser.add_argument("--largest", type=int, default=96, help="size of the biggest transcript")
    parser.add_argument(
        "--real",
        type=Path,
        metavar="VAULT",
        help="read the owner's ~/.claude transcripts of the sessions run in VAULT instead, "
        "read-only, printing numbers only",
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help=(
            "measure a synthetic worst-case history image page, in its own temporary directory "
            "(needs a shallow %%TEMP%%: a deep one can push the temporary asset store's paths, "
            "41 to 57 characters longer than the transcript path, past Windows' 260-character "
            "MAX_PATH first)"
        ),
    )
    parser.add_argument("--turns", type=int, default=20, help="turns in the synthetic image page")
    parser.add_argument(
        "--images-per-turn", type=int, default=10, help="images carried by each turn"
    )
    args = parser.parse_args()
    if args.images:
        measure_images(args.turns, args.images_per_turn)
        return 0
    if args.real is not None:
        # Absolute, as the CLI names the transcript folder; links are left to project_dir().
        measure_real(args.real.absolute())
        return 0
    if args.out is None:
        parser.error("--out is required: nothing is written outside it")
    measure_synthetic(args.out, args.mib, args.largest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
