"""Scripted stand-in for the voice-pack setup child: `fake_pack_setup.py install --data <dir>`.

Prints the lines of `FAKE_PACK_SETUP_LINES` (a JSON list of strings), one per line, flushed after
each; then, while `FAKE_PACK_SETUP_HOLD` is set, sleeps (until killed); then exits with
`int(FAKE_PACK_SETUP_EXIT or 0)`. With `FAKE_PACK_SETUP_LOG` set, appends its argv as one JSON
line to that file first. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    log = os.environ.get("FAKE_PACK_SETUP_LOG")
    if log:
        with open(log, "a", encoding="ascii") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")
    lines = json.loads(os.environ.get("FAKE_PACK_SETUP_LINES", "[]"))
    out = sys.stdout.buffer
    for line in lines:
        out.write(line.encode("ascii") + b"\n")
        out.flush()
    if os.environ.get("FAKE_PACK_SETUP_HOLD"):
        time.sleep(600)
    return int(os.environ.get("FAKE_PACK_SETUP_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())
