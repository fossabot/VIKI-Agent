"""VIKI Agent's compiled entry point (ADR-054).

``scripts/compile.py`` compiles this file with Nuitka into the ``viki-agent``/``viki-agent.exe``
binary; it is never a module inside the ``viki_agent`` package. Argv led by
``--viki-worker <name>`` (see ``viki_agent.compiled.WORKER_FLAG``) runs that one worker's `main`
in this same process and returns its exit code; any other argv — ``[]``, ``--dev`` and
``--window-host=<pid>`` included — reaches the ordinary app, `viki_agent.__main__.main`,
unchanged. Keeping the dispatcher here rather than inside the package lets each worker branch
import its module lazily with a static, per-branch import Nuitka can follow, and keeps the
package's own import-boundary tests intact: no module under `viki_agent/` may import
`voice_pack_setup`, and only the worker itself may import the voice engine.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from viki_agent import compiled

UNKNOWN_WORKER_EXIT = 2


def main(argv: Sequence[str]) -> int:
    if list(argv[:1]) != [compiled.WORKER_FLAG]:
        from viki_agent.__main__ import main as app

        return app(list(argv))
    name = argv[1] if len(argv) > 1 else ""
    rest = list(argv[2:])
    if name == "voice":
        from viki_agent.voice.voice_worker import main as worker_main
    elif name == "media":
        from viki_agent.media.media_worker import main as worker_main
    elif name == "voice-pack-setup":
        from viki_agent.voice.voice_pack_setup import main as worker_main
    else:
        return UNKNOWN_WORKER_EXIT
    return worker_main(rest)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
