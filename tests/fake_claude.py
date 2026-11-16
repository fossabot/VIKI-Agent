"""fake_claude: the replay engine as a real process, for transport and end-to-end tests.

Usage:  python fake_claude.py <the argv claude would get>
Env:    FAKE_CLAUDE_SCRIPT  JSONL script (see replay.py), required
        FAKE_CLAUDE_STATE   directory of completed sessions; enables --resume checks
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from replay import Replayer, load_script  # noqa: E402

FAKE_VERSION = "2.1.259"


def main() -> None:
    # `--version` is a probe, not a session: the launcher asks the CLI its version before it
    # starts anything (it shows in the status bar), and under an end-to-end run that question
    # reaches this script. Answering in the real CLI's format means the e2e harness needs no
    # special case for a version it would otherwise see empty.
    if "--version" in sys.argv[1:]:
        print(f"{FAKE_VERSION} (Claude Code)")
        return

    stdout, stderr = sys.stdout.buffer, sys.stderr.buffer

    def out(data: bytes) -> None:
        stdout.write(data)
        stdout.flush()

    def err(data: bytes) -> None:
        stderr.write(data)
        stderr.flush()

    def exit_now(code: int) -> None:
        stdout.flush()
        stderr.flush()
        os._exit(code)

    def spawn_sleeper(inherit: bool) -> int:
        """A long sleeper; with `inherit`, it keeps copies of our stdio handles, the way a
        child started with bInheritHandles does, so our stdout pipe outlives our exit. On POSIX
        a child always keeps file descriptors 0 to 2, so only Windows tells the two cases apart."""
        sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
        if sys.platform == "win32":
            return subprocess.Popen(
                sleeper, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=not inherit
            ).pid
        return subprocess.Popen(sleeper, close_fds=not inherit).pid

    state = os.environ.get("FAKE_CLAUDE_STATE")
    replayer = Replayer(
        load_script(Path(os.environ["FAKE_CLAUDE_SCRIPT"])),
        out=out,
        err=err,
        exit=exit_now,
        argv=sys.argv[1:],
        state_dir=Path(state) if state else None,
        spawn=spawn_sleeper,
        cwd=Path.cwd(),
    )
    replayer.boot()
    for line in sys.stdin.buffer:
        if line.strip():
            replayer.feed(json.loads(line))
    replayer.stdin_closed()


if __name__ == "__main__":
    main()
