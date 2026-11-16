"""Scripted stand-in for the voice worker: `fake_voice_worker.py <model id> <backend id>`.

The mode is the model id. Stdlib only; it parses the `>cI` stdin frames itself
and writes one ASCII JSON object per line to stdout, flushed after each line.
"""

from __future__ import annotations

import json
import struct
import sys
import time
from typing import BinaryIO

PARTIAL_EVERY_BYTES = 16_000
OTHER_CAPTURE = "00000000-0000-4000-8000-000000000000"


def read_exact(stream: BinaryIO, size: int) -> bytes | None:
    data = stream.read(size)
    return data if len(data) == size else None


def main() -> int:
    model, backend = sys.argv[1], sys.argv[2]
    mode = model
    stdin, out, err = sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer

    def emit(raw: bytes) -> None:
        if mode == "fake-stderr":
            err.write(b"e" * (1 << 20))
            err.flush()
        out.write(raw + b"\n")
        out.flush()

    def send(obj: dict[str, object]) -> None:
        emit(json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii"))

    if mode == "fake-engine-missing":
        send({"type": "error", "code": "engine_unavailable"})
        return 3
    if mode == "fake-model-error":
        send({"type": "error", "code": "model_unavailable"})
        return 4
    if mode == "fake-hang-ready":
        while stdin.read(4096):
            pass
        return 0
    if mode == "fake-gpu-fails" and backend != "cpu":
        send({"type": "error", "code": "backend_unavailable", "detail": "no_device"})
        return 5

    version = "0.0.0" if mode == "fake-wrong-version" else "1.5.1"
    send(
        {
            "type": "ready",
            "engine": "pywhispercpp",
            "engine_version": version,
            "model": model,
            "backend": backend,
            "device": None if backend == "cpu" else "Fake GPU",
        }
    )
    if mode == "fake-no-read":
        time.sleep(600)
        return 0

    capture = ""
    total = 0
    partials = 0
    seen_audio = False
    while header := read_exact(stdin, 5):
        kind, length = struct.unpack(">cI", header)
        payload = read_exact(stdin, length) if length else b""
        if payload is None:
            return 0
        if kind == b"B":
            capture = json.loads(payload)["capture"]
            total = partials = 0
        elif kind == b"A":
            first = not seen_audio
            seen_audio = True
            if first and mode == "fake-garbage":
                emit(b"not json")
                continue
            if first and mode == "fake-long-line":
                send(
                    {
                        "type": "partial",
                        "capture": capture,
                        "segment": 0,
                        "revision": 1,
                        "text": "x" * 70_000,
                    }
                )
                continue
            if first and mode == "fake-wrong-capture":
                send(
                    {
                        "type": "partial",
                        "capture": OTHER_CAPTURE,
                        "segment": 0,
                        "revision": 1,
                        "text": "partial-1",
                    }
                )
                continue
            if first and mode == "fake-crash":
                return 1
            if first and mode == "fake-gpu-crash" and backend != "cpu":
                return 1
            total += len(payload)
            while partials < total // PARTIAL_EVERY_BYTES:
                partials += 1
                send(
                    {
                        "type": "partial",
                        "capture": capture,
                        "segment": 0,
                        "revision": partials,
                        "text": f"partial-{partials}",
                    }
                )
        elif kind == b"E":
            send({"type": "final", "capture": capture, "segment": 0, "text": f"final-{total}"})
            if mode != "fake-no-done":
                send({"type": "done", "capture": capture})
        elif kind == b"X":
            send({"type": "cancelled", "capture": capture})
    return 0


if __name__ == "__main__":
    sys.exit(main())
