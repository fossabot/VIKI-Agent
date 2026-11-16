"""fake_voicebox: a stand-in for Voicebox 0.5.0's server, for the voice pack's process tests.

Usage:  python fake_voicebox.py --host H --port P --data-dir D --parent-pid N
Env:    FAKE_VOICEBOX_MODE  ok (the default) | exit_at_start (exit 3 before listening) |
                            never_ready (/health answers 503) |
                            crash_after_ready (exit about 0.3 s after the first healthy answer)
        FAKE_VOICEBOX_LOG   a file that gets one JSON line: the argv after the script, and the
                            HF_HUB_OFFLINE, VOICEBOX_MODELS_DIR and HF_HOME variables

Stdlib only. Profiles live in memory with uuid4 ids; /generate/stream answers a short silent WAV.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGGED_ENV = ("HF_HUB_OFFLINE", "VOICEBOX_MODELS_DIR", "HF_HOME")
HEALTH = {
    "status": "healthy",
    "gpu_available": True,
    "backend_variant": "cuda",
    "vram_used_mb": 0,
    "gpu_compatibility_warning": None,
}


def silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24_000)
        out.writeframes(b"\x00\x00" * 2_400)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    mode = os.environ.get("FAKE_VOICEBOX_MODE", "ok")
    log = os.environ.get("FAKE_VOICEBOX_LOG")
    if log:
        line = {"argv": sys.argv[1:], "env": {name: os.environ.get(name) for name in LOGGED_ENV}}
        with open(log, "a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")
    if mode == "exit_at_start":
        return 3

    profiles: list[dict[str, object]] = []
    lock = threading.Lock()
    healthy_once = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # quiet: nothing reads the fake's output

        def send_body(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, data: object) -> None:
            self.send_body(status, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:
            if self.path == "/health" and mode == "never_ready":
                self.send_json(503, {"detail": "starting"})
            elif self.path == "/health":
                if mode == "crash_after_ready" and not healthy_once.is_set():
                    healthy_once.set()
                    threading.Timer(0.3, os._exit, (1,)).start()
                self.send_json(200, HEALTH)
            elif self.path == "/profiles":
                with lock:
                    self.send_json(200, list(profiles))
            else:
                self.send_json(404, {"detail": "Not Found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/profiles":
                with lock:
                    if any(profile["name"] == body.get("name") for profile in profiles):
                        self.send_json(400, {"detail": "A profile with this name already exists"})
                        return
                    created = {"id": str(uuid.uuid4()), **body}
                    profiles.append(created)
                self.send_json(200, created)
            elif self.path == "/generate/stream":
                self.send_body(200, silent_wav(), "audio/wav")
            else:
                self.send_json(404, {"detail": "Not Found"})

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
