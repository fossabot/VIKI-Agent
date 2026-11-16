"""Does a browser's `--app` window survive a same-origin navigation to a URL carrying a
per-launch token in its fragment, and does that token ever reach a process's argv?

A real launch must never put its token on a command line: `/proc/<pid>/cmdline` or `ps` would
show it to every other account on the machine. The launcher instead opens a throwaway
HTML file that redirects the browser, through a `<meta http-equiv=refresh>`, to a URL whose
fragment carries the token. This script proves two things on whatever browser this host
actually has: that the redirected window keeps its app frame (no tab strip, no address bar), and
that the fragment survives the navigation intact. The report's `token_in_argv` field re-checks the
argv this script itself built for `winproc.spawn()` -- never `/proc/<pid>/cmdline` or `ps`, which
nothing here queries -- so it only confirms the token was kept out of the browser's own command
line as typed here, not that the OS's own view of the running process agrees.

Not a pytest file, and never imported by anything under src/: it drives a real browser process,
prints one JSON report line, and exits 0 only when every check in that line passed.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from viki_agent import config, winproc

_TIMEOUT_DEFAULT_S = 20.0
_TREE_END_TIMEOUT_S = 10.0
_POLL_S = 0.1
_HOLD_S = 10.0

_PAGE = (
    b'<!doctype html><meta charset="utf-8">'
    b"<p>S-1: redirected</p>"
    b"<script>fetch('/echo', {method: 'POST', body: location.hash});</script>"
)


class _EchoState:
    """The one fragment the served page ever echoes back, shared with the server thread."""

    echoed: str | None = None


def _make_handler(state: _EchoState) -> type[BaseHTTPRequestHandler]:
    """A handler bound to `state` by closure, so the server needs no subclass of its own."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep stdout to this script's one JSON line

        def do_GET(self) -> None:
            if self.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)

        def do_POST(self) -> None:
            if self.path != "/echo":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            state.echoed = self.rfile.read(length).decode("utf-8", errors="replace")
            self.send_response(204)
            self.end_headers()

    return _Handler


def _write_redirect(tmp: Path, target: str) -> Path:
    """A private `open-<random>.html` in `tmp` whose meta refresh sends the browser to `target`."""
    path = tmp / f"open-{secrets.token_hex(16)}.html"
    page = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0;url={html.escape(target)}">'
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, page.encode("utf-8"))
    finally:
        os.close(fd)
    return path


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    browser = (
        Path(args.browser) if args.browser else config._browser_for(sys.platform, os.environ)[0]
    )
    if browser is None:
        print(json.dumps({"error": "no browser found"}), file=sys.stderr)
        return 2

    state = _EchoState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    tmp = Path(tempfile.mkdtemp())
    child: winproc.Child | None = None
    try:
        token = secrets.token_urlsafe(32)
        target = f"http://127.0.0.1:{port}/#token={token}"
        redirect = _write_redirect(tmp, target)
        browser_argv = [
            str(browser),
            f"--user-data-dir={tmp / 'profile'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--app={redirect.as_uri()}",
            *(["--headless=new"] if args.headless else []),
        ]
        token_in_argv = any(token in part for part in browser_argv)

        start = time.monotonic()
        child = await winproc.spawn(
            browser_argv, cwd=tmp, env=winproc.scrub_env(os.environ), pipes=False
        )
        navigated = False
        fragment_ok = False
        deadline = start + args.timeout
        while time.monotonic() < deadline and not navigated:
            if state.echoed is not None:
                navigated = True
                fragment_ok = state.echoed == f"#token={token}"
            else:
                await asyncio.sleep(_POLL_S)
        seconds = time.monotonic() - start

        if not args.headless:
            print(
                "S-1: the window stays open for 10 s. Check that it has no tab strip and no "
                'address bar, and that the page reads "S-1: redirected".',
                file=sys.stderr,
            )
            await asyncio.sleep(_HOLD_S)

        child.kill_tree()
        tree_ended = True
        try:
            async with asyncio.timeout(_TREE_END_TIMEOUT_S):
                await child.wait_tree()
        except TimeoutError:
            tree_ended = False
        child.release()
    finally:
        if child is not None:
            child.kill_tree()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)

    report = {
        "os": sys.platform,
        "browser": browser.name,
        "headless": args.headless,
        "navigated": navigated,
        "fragment_ok": fragment_ok,
        "seconds": round(seconds, 3),
        "tree_ended": tree_ended,
        "token_in_argv": token_in_argv,
    }
    print(json.dumps(report))
    return 0 if navigated and fragment_ok and tree_ended and not token_in_argv else 1


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser",
        default="",
        help="browser executable; else the app's own discovery (VIKI_EDGE first, snap and "
        "flatpak launchers skipped)",
    )
    parser.add_argument(
        "--headless", action="store_true", help="pass --headless=new to the browser"
    )
    parser.add_argument(
        "--timeout", type=float, default=_TIMEOUT_DEFAULT_S, help="seconds to wait for the echo"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
