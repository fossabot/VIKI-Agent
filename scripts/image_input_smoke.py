"""One native image proof, in disposable nonsensitive scratch.

No tool permission is granted. The report contains only version, synthetic image hash, event
counts, the transcript evidence read back from the CLI's own project folder, and pass/fail
observations. Never use a real user image with this probe. Before any spawn, the billing
preflight reads only the user-level and managed settings and %APPDATA%\\Anthropic. The turn runs
in a temporary scratch folder removed on exit; on a find, the transcript's bytes are copied under
the app's own captures folder (never the repo). Nothing else is written anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import json
import struct
import sys
import tempfile
import time
import uuid
import zlib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from viki_agent import catalog, config, events, winproc
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.turn_input import Attachment, TurnInput

TURN_TIMEOUT_S = 90


def synthetic_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixels = b"".join(b"\0" + b"\xff\x00\x00" * 32 + b"\x00\x00\xff" * 32 for _ in range(32))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _not_found_evidence() -> dict[str, Any]:
    return {
        "transcript_found": False,
        "transcript_prompt_blocks": [],
        "transcript_image_inline": False,
        "transcript_versions": [],
    }


def _scan_transcript(lines: Iterable[str], png: bytes) -> dict[str, Any]:
    """The four transcript_* fields for `lines`, already read (`transcript_found` always True)."""
    sha = hashlib.sha256(png).hexdigest()
    prompt_blocks: list[Any] = []
    found_prompt = False
    image_inline = False
    versions: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        version = entry.get("version")
        if isinstance(version, str):
            versions.add(version)
        if entry.get("type") != "user":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if not found_prompt:
            prompt_blocks = [block.get("type") for block in content if isinstance(block, dict)]
            found_prompt = True
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            source = block.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                continue
            data = source.get("data")
            if not isinstance(data, str):
                continue
            try:
                decoded = base64.b64decode(data, validate=True)
            except binascii.Error, ValueError:
                continue
            if hashlib.sha256(decoded).hexdigest() == sha:
                image_inline = True
    return {
        "transcript_found": True,
        "transcript_prompt_blocks": prompt_blocks,
        "transcript_image_inline": image_inline,
        "transcript_versions": sorted(versions),
    }


def read_transcript(path: Path, png: bytes) -> dict[str, Any]:
    """The transcript's evidence for the synthetic image: one open-read-close, unreadable lines
    skipped. A missing file gives found false and empty values."""
    if not path.exists():
        return _not_found_evidence()
    with path.open(encoding="utf-8") as fh:
        return _scan_transcript(fh, png)


async def main() -> int:
    png = synthetic_png()
    observed: Counter[str] = Counter()
    text: list[str] = []
    terminal = asyncio.Event()
    result = "unconfirmed"
    cfg = config.load()
    if cfg.cli is None or cfg.claude_config_dir_set:
        raise RuntimeError("native provider unavailable")
    with tempfile.TemporaryDirectory(prefix="viki-p2-image-") as tmp:
        scratch = Path(tmp)
        problems = config.billing_problems(dataclasses.replace(cfg, vault=scratch))
        if problems:
            print("Billing preflight failed:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2
        version = winproc.run([str(cfg.cli), "--version"], cwd=scratch, timeout=10).stdout.strip()

        async def resolve(_attachment: Attachment) -> bytes:
            return png

        def emit(event: events.Event) -> None:
            nonlocal result
            observed[event.TYPE] += 1
            if isinstance(event, events.TextDone):
                text.append(event.text[:8192])
            if isinstance(event, events.PermissionRequested):
                driver.respond(event.tool_use_id, PermissionAnswer(False))
            if isinstance(event, events.TurnDone):
                result = event.status
                terminal.set()
            if isinstance(event, events.Error):
                result = event.code
                terminal.set()

        launch = Launch(str(uuid.uuid4()))
        driver = ClaudeDriver(
            launch,
            mode="default",
            effort="low",
            model=None,
            config=DriverConfig(
                cli=(str(cfg.cli),),
                vault=scratch,
                env=winproc.child_env(config.ENTRYPOINT),
                cli_version=version,
                resolve_attachment=resolve,
            ),
            emit=emit,
        )
        try:
            async with asyncio.timeout(TURN_TIMEOUT_S):
                await driver.open()
                image = Attachment(
                    str(uuid.uuid4()), "image/png", len(png), 64, 32, "synthetic.png"
                )
                await driver.send(
                    TurnInput(
                        "Do not use tools. Look at this synthetic image. Reply only with the "
                        "dominant color on the left and then the dominant color on the right, "
                        "in that order.",
                        (image,),
                    )
                )
                await terminal.wait()
        except TimeoutError:
            result = "timeout"
        finally:
            await driver.close()

        transcript = catalog.project_dir(cfg.claude_home, scratch) / f"{launch.session_id}.jsonl"
        # Printed before any copy attempt, so a not-found transcript still says where the CLI's
        # project folder should have been.
        print(f"scratch root: {scratch}", file=sys.stderr)
        transcript_error: str | None = None
        if not transcript.exists():
            evidence = _not_found_evidence()
        else:
            try:
                raw = transcript.read_bytes()
                evidence = _scan_transcript(raw.decode("utf-8").splitlines(), png)
            except Exception as exc:
                transcript_error = type(exc).__name__
                evidence = _not_found_evidence()
            else:
                # A copy failure must not discard the scan's result: it runs in its own try,
                # whose failure only sets the error name.
                try:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    dest = cfg.captures_dir / f"{stamp}-image-smoke" / transcript.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
                    print(f"raw copy: {dest}", file=sys.stderr)
                except Exception as exc:
                    transcript_error = type(exc).__name__
        answer = " ".join(text).lower()
        print(
            json.dumps(
                {
                    "cli_version": version,
                    "session_id": launch.session_id,
                    "synthetic_sha256": hashlib.sha256(png).hexdigest(),
                    "image_bytes": len(png),
                    "terminal": result,
                    "events": dict(observed),
                    "left_red_right_blue": "red" in answer
                    and "blue" in answer
                    and answer.find("red") < answer.find("blue"),
                    "transcript_error": transcript_error,
                    **evidence,
                }
            )
        )
    return 0 if result == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
