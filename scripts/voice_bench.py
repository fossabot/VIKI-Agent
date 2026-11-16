"""Voice bench: the engine-start smoke and the shipped-path measurement of model/backend pairs.

    uv run python scripts/voice_bench.py --smoke [--model ID] [--backend ID]
    uv run python scripts/voice_bench.py <fixtures dir> [--fast] [--languages a,b]
        [--per-language N] [--repeat N] [--contention N] [--out <json>] [--show-text]
        [--model ID] [--backend ID] [--tcp-poll]

Dev tooling: the server never imports it. It starts one `VoiceEngine` on `<data>/voice/` (so
`VIKI_DATA_DIR` picks the folder), feeds fixtures as 20 ms frames, and prints one JSON report:
start time, worker memory, GPU memory, and per language the error and the latency percentiles. A
miss counts as +infinity in a percentile and prints as null. Texts print only with `--show-text`;
the only file written is `--out`. An item whose language the model does not list is fed as "auto"
(the row's `fed_language` says so) and still scored in its own language.

Fixtures are `<dir>/manifest.json` with `{"items": [{"wav", "language", "reference", "kind"}]}` and
16 kHz mono s16le WAVs beside it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import math
import os
import shutil
import socket
import statistics
import sys
import threading
import time
import unicodedata
import uuid
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from viki_agent import config, winproc
from viki_agent.voice import voice_engine, voice_models, voice_stream
from viki_agent.voice.voice_engine import VoiceError, VoiceResult

FRAME_S = voice_stream.FRAME_SAMPLES / voice_stream.SAMPLE_RATE
FRAME_BYTES = voice_stream.FRAME_SAMPLES * 2
LEAD_FRAMES = 50  # 1.0 s of zeros before the clip, and the same after it
ONSET_DBFS = -35
ONSET_LIMIT_S = 10  # no text this soon after the onset is a miss
DONE_LIMIT_S = 15  # no `done` this soon after release is a miss
CHAR_LANGUAGES = frozenset({"zh", "ja", "yue", "th"})
SMI = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
MiB = 1024 * 1024

# --- pure helpers -----------------------------------------------------------------------------


def normalize(text: str, unit: str) -> list[str]:
    """NFKC, casefold, no Unicode punctuation; characters without whitespace, or words."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(c for c in folded if not unicodedata.category(c).startswith("P"))
    return list("".join(kept.split())) if unit == "char" else kept.split()


def edit_distance(a: Sequence[Any], b: Sequence[Any]) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]


def error_rate(reference: str, hypothesis: str, unit: str) -> float:
    """Levenshtein over the units, divided by the reference's length."""
    ref, hyp = normalize(reference, unit), normalize(hypothesis, unit)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest rank; a miss is `math.inf`, which ranks last."""
    if not values:
        return None
    ranked = sorted(values)
    return ranked[max(0, math.ceil(p / 100 * len(ranked)) - 1)]


def onset_ms(pcm: bytes) -> int | None:
    """Start of the clip's first 20 ms frame above -35 dBFS, or None."""
    for start in range(0, len(pcm) - 1, FRAME_BYTES):
        if voice_stream._level_dbfs(pcm[start : start + FRAME_BYTES]) > ONSET_DBFS:
            return start // FRAME_BYTES * round(FRAME_S * 1000)
    return None


def memory_used_mib(csv_text: str) -> int | None:
    """GPU 0's `memory.used` from `nvidia-smi --format=csv,noheader,nounits`, or None."""
    lines = csv_text.strip().splitlines()
    try:
        return int(lines[0].strip())
    except IndexError, ValueError:
        return None


def child_pid(rows: Sequence[tuple[int, int, str]], parent: int) -> int | None:
    """The first python child of `parent` in (pid, parent pid, image name) rows."""
    return next(
        (pid for pid, ppid, name in rows if ppid == parent and name.lower().startswith("python")),
        None,
    )


def select_items(
    items: Sequence[dict[str, Any]], languages: Sequence[str], limit: int | None
) -> list[dict[str, Any]]:
    """The first `limit` speech items per language and silence and noise items per kind."""
    taken: dict[str, int] = {}
    chosen = []
    for item in items:
        kind, language = item["kind"], item["language"]
        if kind == "speech" and language in languages:
            group = language
        elif kind in ("silence", "noise"):
            group = kind
        else:
            continue
        if limit is None or taken.get(group, 0) < limit:
            taken[group] = taken.get(group, 0) + 1
            chosen.append(item)
    return chosen


def _miss(value: float | None, limit: float) -> float:
    return math.inf if value is None or value > limit else value


def _finite(value: float | None) -> float | None:
    return None if value is None or math.isinf(value) else round(value, 3)


def summarize(rows: Sequence[dict[str, Any]], *, latency: bool) -> dict[str, Any]:
    """Per language: the mean error and the latency percentiles; for silence and noise: the text."""
    languages: dict[str, Any] = {}
    speech = [row for row in rows if row["kind"] == "speech"]
    for language in sorted({row["language"] for row in speech}):
        own = [row for row in speech if row["language"] == language]
        entry: dict[str, Any] = {
            "n": len(own),
            "mean_error_pct": round(100 * statistics.fmean(r["error"] for r in own), 2),
        }
        if latency:
            onset = [_miss(r["onset_partial_s"], ONSET_LIMIT_S) for r in own]
            release = [_miss(r["release_done_s"], DONE_LIMIT_S) for r in own]
            entry["onset_partial_misses"] = sum(math.isinf(v) for v in onset)
            entry["release_done_misses"] = sum(math.isinf(v) for v in release)
            for name, values in (("onset_partial", onset), ("release_done", release)):
                entry[f"{name}_p50_s"] = _finite(percentile(values, 50))
                entry[f"{name}_p95_s"] = _finite(percentile(values, 95))
        languages[language] = entry
    quiet = [row for row in rows if row["kind"] in ("silence", "noise")]
    return {
        "languages": languages,
        "silence_and_noise": {
            "captures": len(quiet),
            "nonempty_finals": sum(r["finals"] for r in quiet),
            "nonempty_partials": sum(r["partials"] for r in quiet),
        },
        "capture_errors": sorted({r["capture_error"] for r in rows if r["capture_error"]}),
    }


# --- Windows probes: worker memory, process table, TCP table --------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TH32CS_SNAPPROCESS = 2
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _TCP_TABLE_OWNER_PID_ALL = 5
    _AF_INET6 = 23  # Windows' value
    _ERROR_INSUFFICIENT_BUFFER = 122
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _ProcessEntry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class _MemoryCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ],
        ]

    def _fn(name: str, restype: Any, *argtypes: Any, dll: Any = _kernel32) -> Any:
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    _Snapshot = _fn("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    _First = _fn("Process32FirstW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _Next = _fn("Process32NextW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _OpenProcess = _fn(
        "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    )
    _CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _GetProcessMemoryInfo = _fn(
        "K32GetProcessMemoryInfo",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(_MemoryCounters),
        wintypes.DWORD,
    )
    _GetExtendedTcpTable = _fn(
        "GetExtendedTcpTable",
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.PDWORD,
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
        dll=ctypes.WinDLL("iphlpapi", use_last_error=True),
    )

    def process_rows() -> list[tuple[int, int, str]]:
        snapshot = _Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot is None or snapshot == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error(), "CreateToolhelp32Snapshot")
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            rows = []
            found = _First(snapshot, ctypes.byref(entry))
            while found:
                rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                found = _Next(snapshot, ctypes.byref(entry))
            return rows
        finally:
            _CloseHandle(snapshot)

    def process_memory(pid: int) -> dict[str, float] | None:
        """The process's working set, peak working set and peak commit, in MiB."""
        handle = _OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
        if not handle:
            return None
        try:
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not _GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return {
                "working_set_mib": round(counters.WorkingSetSize / MiB, 1),
                "peak_working_set_mib": round(counters.PeakWorkingSetSize / MiB, 1),
                "peak_commit_mib": round(counters.PeakPagefileUsage / MiB, 1),
            }
        finally:
            _CloseHandle(handle)

    def tcp_owner_pids() -> list[int]:
        """The owning pid of every TCP row (any state), IPv4 then IPv6."""
        pids: list[int] = []
        # a row is 6 (v4) or 14 (v6) DWORDs and its owning pid is the last one
        for family, row_dwords in ((socket.AF_INET, 6), (_AF_INET6, 14)):
            size = wintypes.DWORD(0)
            buffer = ctypes.create_string_buffer(0)
            for _ in range(4):
                rc = _GetExtendedTcpTable(
                    buffer, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_ALL, 0
                )
                if rc == 0:
                    break
                if rc != _ERROR_INSUFFICIENT_BUFFER:
                    raise ctypes.WinError(rc, "GetExtendedTcpTable")
                buffer = ctypes.create_string_buffer(size.value)
            else:
                raise ctypes.WinError(_ERROR_INSUFFICIENT_BUFFER, "GetExtendedTcpTable")
            dwords = (wintypes.DWORD * (len(buffer) // 4)).from_buffer(buffer)
            pids += [dwords[i * row_dwords + row_dwords] for i in range(dwords[0])]
        return pids

    def children_peak_rss_mib() -> float | None:
        return None  # Windows reads the worker's own counters in process_memory

else:
    import resource

    def process_rows() -> list[tuple[int, int, str]]:
        return []

    def process_memory(pid: int) -> dict[str, float] | None:
        return None

    def tcp_owner_pids() -> list[int]:
        return []

    def children_peak_rss_mib() -> float | None:
        """After close: the largest child's peak RSS (bytes on macOS, KiB elsewhere)."""
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return round(peak / (MiB if sys.platform == "darwin" else 1024), 1)


# --- measurement helpers ----------------------------------------------------------------------


class TcpWatch:
    """Polls the TCP table every 50 ms for a row owned by a spawned worker or its python child."""

    def __init__(self) -> None:
        self.roots: set[int] = set()
        self.polls = 0
        self.rows: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            roots = tuple(self.roots)
            table = process_rows()
            watched = {*roots, *(p for r in roots if (p := child_pid(table, r)) is not None)}
            self.rows += [pid for pid in tcp_owner_pids() if pid in watched]
            self.polls += 1


def _smi() -> str:
    try:
        return winproc.run(SMI, cwd=Path.cwd(), timeout=5).stdout
    except Exception:  # missing or hung nvidia-smi (TimeoutExpired: test_winproc bans the name)
        return ""


class GpuSampler:
    """The GPU's `memory.used` about every 250 ms, as a peak over the baseline taken first.

    WDDM reports no per-process figure, so other programs on the GPU add noise."""

    def __init__(self) -> None:
        self.baseline: int | None = None
        self.samples: list[int] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.baseline = memory_used_mib(await asyncio.to_thread(_smi))
        if self.baseline is not None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            used = memory_used_mib(await asyncio.to_thread(_smi))
            if used is not None:
                self.samples.append(used)
            await asyncio.sleep(0.25)

    async def stop(self) -> dict[str, Any] | None:
        if self._task is None or self.baseline is None:
            return None
        self._task.cancel()
        await asyncio.wait([self._task])
        peak = max(self.samples, default=self.baseline)
        return {
            "baseline_mib": self.baseline,
            "peak_mib": peak,
            "peak_delta_mib": peak - self.baseline,
            "samples": len(self.samples),
        }


def worker_memory(launcher_pid: int) -> dict[str, float] | None:
    """The real worker's memory: on Windows `launcher_pid` is the venv launcher and the worker
    is its python child."""
    return process_memory(child_pid(process_rows(), launcher_pid) or launcher_pid)


def hash_seconds(data: Path, engine: voice_engine.VoiceEngine) -> float:
    """One SHA-256 pass over the running pair's files (warm page cache)."""
    assert engine.model_id is not None and engine.backend is not None
    started = time.perf_counter()
    voice_models.verify(
        voice_models.model_dir(data, engine.model_id), voice_models.model_files(engine.model_id)
    )
    voice_models.verify(
        voice_models.backend_dir(data, engine.backend), voice_models.BACKENDS[engine.backend].files
    )
    return time.perf_counter() - started


# --- captures ---------------------------------------------------------------------------------


@dataclasses.dataclass
class Capture:
    sent: list[float] = dataclasses.field(default_factory=list)  # send time of each frame
    results: list[tuple[float, VoiceResult]] = dataclasses.field(default_factory=list)
    released: float = 0.0  # send time of `E`
    done: float | None = None
    error: str | None = None


def frames_of(pcm: bytes) -> list[bytes]:
    padded = pcm + bytes(-len(pcm) % FRAME_BYTES)
    return [padded[i : i + FRAME_BYTES] for i in range(0, len(padded), FRAME_BYTES)]


async def run_capture(
    engine: voice_engine.VoiceEngine,
    language: str,
    frames: Sequence[bytes],
    *,
    fast: bool,
    wait_s: float,
) -> Capture:
    """Feed `frames` on a monotonic 20 ms schedule (`fast`: as fast as accepted), send `E`, and
    wait up to `wait_s` for `done`."""
    loop = asyncio.get_running_loop()
    capture_id = str(uuid.uuid4())
    capture = Capture()
    finished = asyncio.Event()

    async def collect() -> None:
        while True:
            result = await engine.results.get()
            arrived = loop.time()
            if result.kind == "error":
                capture.error = result.code
            elif result.capture_id == capture_id:
                capture.results.append((arrived, result))
                if result.kind != "done":
                    continue
                capture.done = arrived
            else:
                continue
            finished.set()
            return

    await engine.begin(capture_id, language)
    collector = asyncio.create_task(collect())
    try:
        started = loop.time()
        for index, frame in enumerate(frames):
            if not fast:
                await asyncio.sleep(max(0.0, started + index * FRAME_S - loop.time()))
            while True:
                try:
                    await engine.audio(capture_id, frame)
                    break
                except VoiceError as exc:
                    if exc.code != "overloaded" or not fast:
                        raise
                    await asyncio.sleep(0.005)  # a full write buffer: wait for the worker
            capture.sent.append(loop.time())
        await engine.end(capture_id)
        capture.released = loop.time()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_s):
                await finished.wait()
    finally:
        collector.cancel()
        await asyncio.wait([collector])
    return capture


def measure(
    item: dict[str, Any], pcm: bytes, capture: Capture, error: str | None
) -> dict[str, Any]:
    texts = [r.text for _, r in capture.results if r.kind == "final"]
    first = min((at for at, r in capture.results if r.kind != "done" and r.text), default=None)
    onset = onset_ms(pcm)
    index = LEAD_FRAMES + (onset or 0) // round(FRAME_S * 1000)
    onset_sent = capture.sent[index] if onset is not None and index < len(capture.sent) else None
    unit = "char" if item["language"] in CHAR_LANGUAGES else "word"
    hypothesis = " ".join(texts)
    return {
        "wav": item["wav"],
        "kind": item["kind"],
        "language": item["language"],
        "onset_partial_s": None if first is None or onset_sent is None else first - onset_sent,
        "release_done_s": None if capture.done is None else capture.done - capture.released,
        "error": error_rate(item["reference"], hypothesis, unit) if item["kind"] == "speech" else 0,
        "partials": sum(1 for _, r in capture.results if r.kind == "partial" and r.text),
        "finals": sum(1 for text in texts if text),
        "capture_error": error or capture.error or (None if capture.done else "no_done"),
        "hypothesis": hypothesis,
        "reference": item["reference"],
    }


async def run_items(
    engine: voice_engine.VoiceEngine, directory: Path, items: Sequence[dict[str, Any]], args: Any
) -> tuple[list[dict[str, Any]], list[float], int]:
    """Every pass over `items`; the rows, the worker's working set after each capture (MiB), and
    how many times the worker had to be restarted."""
    rows: list[dict[str, Any]] = []
    working_sets: list[float] = []
    restarts = 0
    for number in range(args.repeat):
        for item in items:
            pcm = read_wav(directory / item["wav"])
            frames = (
                [bytes(FRAME_BYTES)] * LEAD_FRAMES
                + frames_of(pcm)
                + [bytes(FRAME_BYTES)] * LEAD_FRAMES
            )
            error = None
            # a language the model does not list can only be fed as "auto"
            allowed = voice_models.MODELS[engine.model_id or ""].languages
            fed_language = item["language"] if item["language"] in allowed else "auto"
            try:
                capture = await run_capture(
                    engine,
                    fed_language,
                    frames,
                    fast=args.fast,
                    wait_s=voice_engine.FINAL_TIMEOUT_S,
                )
            except VoiceError as exc:
                capture, error = Capture(), exc.code
            row = measure(item, pcm, capture, error)
            row["pass"] = number
            row["fed_language"] = fed_language
            rows.append(row)
            print(
                f"{len(rows)} {item['wav']} {row['capture_error'] or 'ok'}",
                file=sys.stderr,
                flush=True,
            )
            if row["capture_error"]:
                # a worker that missed `done` or died holds an open capture: start a fresh one
                await engine.close()
                await engine.start()
                restarts += 1
            elif engine.pid is not None and (memory := worker_memory(engine.pid)) is not None:
                working_sets.append(memory["working_set_mib"])
    return rows, working_sets, restarts


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError(f"{path.name} is not 16 kHz mono s16le")
        return wav.readframes(wav.getnframes())


# --- the two modes ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice_bench.py", description=__doc__)
    parser.add_argument("fixtures", nargs="?", type=Path, help="the fixtures folder")
    parser.add_argument("--smoke", action="store_true", help="start the engine, one silent capture")
    parser.add_argument("--model", help="a model id; default: the best installed")
    parser.add_argument("--backend", default="auto", help="auto, cpu, cuda or metal")
    parser.add_argument(
        "--fast", action="store_true", help="feed as fast as accepted: accuracy only"
    )
    parser.add_argument("--languages", help="comma-separated codes; default: the model's list")
    parser.add_argument("--per-language", type=int, help="first N items per language and kind")
    parser.add_argument("--repeat", type=int, default=1, help="passes over the set, one session")
    parser.add_argument("--contention", type=int, default=0, help="busy processes during the run")
    parser.add_argument("--out", type=Path, help="write the report and every row here as JSON")
    parser.add_argument("--show-text", action="store_true", help="print and write the texts")
    parser.add_argument("--tcp-poll", action="store_true", help="poll the TCP table for the worker")
    return parser


async def _smoke(engine: voice_engine.VoiceEngine) -> dict[str, Any]:
    frames = [bytes(FRAME_BYTES)] * LEAD_FRAMES  # 1 s of zeros
    capture = await run_capture(
        engine, "auto", frames, fast=True, wait_s=voice_engine.FINAL_TIMEOUT_S
    )
    counts = {kind: 0 for kind in ("partial", "final", "done")}
    for _, result in capture.results:
        counts[result.kind] += 1
    return {
        "done_within_final_timeout": capture.done is not None,
        "done_after_release_s": None
        if capture.done is None
        else _finite(capture.done - capture.released),
        "results": counts,
        "capture_error": capture.error,
    }


async def _bench(
    engine: voice_engine.VoiceEngine, args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    assert engine.model_id is not None
    manifest = json.loads((args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    languages = (
        args.languages.split(",")
        if args.languages
        else voice_models.MODELS[engine.model_id].languages
    )
    items = select_items(manifest["items"], languages, args.per_language)
    if not items:
        raise SystemExit("no fixture matches --languages")
    busy = [
        await winproc.spawn(
            [sys.executable, "-c", "while True: pass"],
            cwd=Path.cwd(),
            env=dict(os.environ),
            pipes=False,
        )
        for _ in range(args.contention)
    ]
    try:
        rows, working_sets, restarts = await run_items(engine, args.fixtures, items, args)
    finally:
        for child in busy:
            child.kill_tree()
            child.release()
    report: dict[str, Any] = {
        "captures": len(rows),
        "restarts": restarts,
        "contention": args.contention,
        "working_set_mib_after_each_capture": working_sets,
        **summarize(rows, latency=not args.fast),
    }
    if not args.show_text:
        for row in rows:
            del row["hypothesis"], row["reference"]
    return report, rows


async def _run(args: argparse.Namespace, data: Path) -> int:
    watch = TcpWatch() if args.tcp_poll else None
    gpu = GpuSampler() if args.backend != "cpu" and shutil.which(SMI[0]) else None

    async def spawn(argv: Sequence[str], **kwargs: Any) -> winproc.Child:
        child = await winproc.spawn(argv, **kwargs)
        assert watch is not None
        watch.roots.add(child.pid)
        return child

    engine = voice_engine.VoiceEngine(
        data,
        model_id=args.model,
        backend=args.backend,
        ready_timeout_s=600,
        spawn=spawn if watch is not None else winproc.spawn,
    )
    report: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    gpu_report = None
    on_cuda = False
    try:
        if watch is not None:
            watch.start()
        if gpu is not None:
            await gpu.start()
        started = time.perf_counter()
        await engine.start()
        report = {
            "engine": voice_models.ENGINE,
            "version": voice_models.ENGINE_VERSION,
            "model": engine.model_id,
            "backend": engine.backend,
            "device": engine.device,
            "fallback": dataclasses.asdict(engine.fallback) if engine.fallback else None,
            "start_s": round(time.perf_counter() - started, 3),
            "hash_s": round(await asyncio.to_thread(hash_seconds, data, engine), 3),
        }
        if args.smoke:
            report |= await _smoke(engine)
        else:
            extra, rows = await _bench(engine, args)
            report |= extra
        if engine.pid is not None:
            report["worker_memory"] = worker_memory(engine.pid)
        on_cuda = engine.backend == "cuda"
    except VoiceError as exc:
        print(f"error: {exc.code} {exc.detail or ''}".rstrip())
        return 1
    finally:
        await engine.close()
        gpu_report = await gpu.stop() if gpu is not None else None
        if watch is not None:
            watch.stop()
    if gpu_report is not None and on_cuda:
        report["gpu_memory"] = gpu_report
    if watch is not None:
        report["tcp_poll"] = {"polls": watch.polls, "rows_for_worker": len(watch.rows)}
    if (rss := children_peak_rss_mib()) is not None:
        report["peak_rss_children_mib"] = rss
    print(json.dumps(report, indent=2))
    if args.show_text:
        for row in rows:
            print(row["wav"], "|", row["reference"], "|", row["hypothesis"])
    if args.out is not None:
        args.out.write_text(
            json.dumps({"report": report, "rows": rows}, indent=2), encoding="utf-8"
        )
    return 1 if args.smoke and not report["done_within_final_timeout"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.smoke == (args.fixtures is not None):
        parser.error("give --smoke or a fixtures folder, not both and not neither")
    if args.repeat < 1 or args.contention < 0 or (args.per_language or 1) < 1:
        parser.error("--repeat and --per-language must be at least 1, --contention at least 0")
    if args.tcp_poll and sys.platform != "win32":
        parser.error("--tcp-poll reads the Windows TCP table")
    try:
        data = config.data_dir(os.environ)
    except config.ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    return asyncio.run(_run(args, data))


if __name__ == "__main__":
    sys.exit(main())
