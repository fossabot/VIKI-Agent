"""Stand-in for the app's browser window (VIKI_EDGE), for the compiled app smoke (ADR-054).

From the app's point of view this behaves exactly like the real browser it spawns as a window:
`window.py`'s WindowManager tracks a window only by waiting for its whole process tree to exit
(`Child.wait_tree`), so this never has to speak any protocol back to the app. It only has to
prove which argv it was launched with, and to go away on command.

Usage, exactly as the app invokes a browser (`__main__.edge_argv`/`window_argv`):

    stand_in_browser.py --user-data-dir=<profile> [...other Edge-style flags...] --app=<url>

Writes its own argv (everything after argv[0]) as JSON to `<profile>/stand-in.argv.json`
(a temp file first, then `os.replace`, so a reader never sees a partial write), then polls
`<profile>/quit` every POLL_S seconds and exits 0 the moment that file appears. If `quit` never
appears, it gives up and exits 0 anyway after HANG_GUARD_S seconds, so a smoke run that crashed
before it could signal this stand-in -- or killed it on a path that, on POSIX, escaped the app's
own process group by leading its own session (`winproc.spawn`'s `start_new_session=True`) --
can never wedge a CI runner forever.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HANG_GUARD_S = 600.0
POLL_S = 0.1
ARGV_FILE_NAME = "stand-in.argv.json"
QUIT_FILE_NAME = "quit"


def profile_dir(argv: list[str]) -> Path:
    """The `--user-data-dir=` value among `argv`; ValueError when it is missing, exactly as a
    real browser invocation always carries one (`edge_argv`)."""
    for arg in argv:
        if arg.startswith("--user-data-dir="):
            return Path(arg.split("=", 1)[1])
    raise ValueError("no --user-data-dir= argument")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    profile = profile_dir(args)
    profile.mkdir(parents=True, exist_ok=True)
    final = profile / ARGV_FILE_NAME
    temp = profile / f"{ARGV_FILE_NAME}.tmp"
    temp.write_text(json.dumps(args), encoding="utf-8")
    os.replace(temp, final)

    quit_file = profile / QUIT_FILE_NAME
    deadline = time.monotonic() + HANG_GUARD_S
    while not quit_file.exists() and time.monotonic() < deadline:
        time.sleep(POLL_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
