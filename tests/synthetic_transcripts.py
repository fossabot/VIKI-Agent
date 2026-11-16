"""Synthetic transcripts in the shape the CLI writes, for the read-budget tests and the
measurement script.

Every string here is invented — none of it is copied or paraphrased from anyone's real
transcripts, vault or notes (AGENTS.md hard rule 9). The shape is the one `catalog.py` parses: user
prompts, `tool_use` / `tool_result` pairs (where most of a real transcript's bytes live),
assistant replies, all chained through `parentUuid`, with an `ai-title` metadata line last.

tests/test_catalog_budgets.py builds KB-scale files from this and patches the budgets down to
match; scripts/measure_catalog.py builds the MiB-scale corpus its numbers come from. One
generator, so the shape a budget was measured against is the shape the tests exercise.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

# Invented filler in English and Japanese. The search tests need text that folds (ASCII) and
# text that cannot (Japanese), so both are here.
JAPANESE = [
    "常駐エージェントの設計案を三つに絞ってから決めたい。",
    "置き忘れを防ぐ仕組みをノートの手順としてまとめておく。",
    "圧縮前のやり取りを要約して、続きから再開してほしい。",
    "買い物の控え: 牛乳、卵、食パン。木曜までに。",
    "この画面では古い実行内容の一部を省略して表示している。",
    "判断待ちの項目に締切を入れて、順番を決め直す。",
]
ENGLISH = [
    "Draft the morning brief and link the retro gate summary.",
    "Search the vault for agent notes and list the files you used.",
    "Explain why the chain cache is bounded by bytes and not by files.",
    "Remember the dentist on Thursday; move the standup if it clashes.",
    "Answer in three bullets and finish with one short conclusion.",
]
# A phrase that appears in no filler sentence, so a test can search for a needle it planted.
ABSENT = "zzzz-no-such-token"


@dataclass(frozen=True)
class Written:
    """A transcript on disk and what a reader should find in it."""

    path: Path
    session_id: str
    prompts: list[str]  # the user's prompts, oldest first
    title: str  # the ai-title line's aiTitle


def session_id(n: int) -> str:
    """A canonical transcript filename stem. The reader treats anything else as missing."""
    return f"{n:08x}-1111-4111-8111-111111111111"


def write_transcript(
    folder: Path,
    sid: str,
    *,
    target_bytes: int,
    rng: random.Random,
    entrypoint: str = "viki-agent",
    filler: tuple[int, int] = (40, 160),
    started: int = 1_760_000_000,
) -> Written:
    """One transcript of at least `target_bytes`, written a whole turn at a time.

    `filler` is the sentence count of a tool result, which is what sets the size of a turn: the
    budget tests want turns of a few hundred bytes so a KB-scale window holds a known number of
    them, and the measurement corpus wants the several-KB turns a real session has."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    prompts: list[str] = []
    count = 0
    last: str | None = None
    written = 0
    stamp = started

    with path.open("w", encoding="utf-8", newline="\n") as fh:

        def emit(kind: str, **fields: object) -> str:
            nonlocal count, last, written, stamp
            count += 1
            stamp += 1
            uid = f"{sid[:8]}-0000-4000-8000-{count:012x}"
            line = json.dumps(
                {
                    "parentUuid": last,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": "C:\\Vault",
                    "sessionId": sid,
                    "version": "2.1.266",
                    "entrypoint": entrypoint,
                    "type": kind,
                    "uuid": uid,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp)) + ".000Z",
                    **fields,
                },
                ensure_ascii=False,
            )
            fh.write(line + "\n")
            written += len(line.encode("utf-8")) + 1
            last = uid
            return uid

        while written < target_bytes:
            # Numbered first and zero-padded, so a test can say which prompts a tail window kept
            # and which it lost: an unpadded number would make prompt 3 a substring of prompt 35.
            prompt = (
                f"通し番号 {len(prompts) + 1:04d}: {rng.choice(JAPANESE)} {rng.choice(ENGLISH)}"
            )
            prompts.append(prompt)
            emit("user", message={"role": "user", "content": prompt})
            tool_id = f"toolu_{count:06d}"
            emit(
                "assistant",
                message={
                    "id": f"msg_{count:06d}",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Read",
                            "input": {"file_path": f"C:\\Vault\\30-knowledge\\note-{count}.md"},
                        }
                    ],
                },
            )
            body = " ".join(rng.choice(JAPANESE + ENGLISH) for _ in range(rng.randint(*filler)))
            emit(
                "user",
                message={
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": body}],
                },
                toolUseResult={"type": "text", "file": {"numLines": 20}},
            )
            reply = " ".join(rng.choice(JAPANESE + ENGLISH) for _ in range(rng.randint(5, 40)))
            emit(
                "assistant",
                message={
                    "id": f"msg_{count:06d}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": reply}],
                },
            )
        # The whole first group, not four characters of it: two transcripts in one corpus differ
        # only past the fourth, and a test that needs one session's title to name one session
        # cannot say so if they collide.
        title = f"合成セッション {sid[:8]}"
        fh.write(
            json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid}, ensure_ascii=False)
            + "\n"
        )
    return Written(path=path, session_id=sid, prompts=prompts, title=title)


def write_corpus(
    folder: Path,
    *,
    total_mib: int,
    largest_mib: int,
    seed: int = 20260916,
    filler: tuple[int, int] = (40, 160),
) -> list[Written]:
    """A corpus of one `largest_mib` transcript and smaller ones up to `total_mib` in all —
    the shape the read budgets were measured against."""
    rng = random.Random(seed)
    sizes = [largest_mib]
    remaining = total_mib - largest_mib
    while remaining > 0:
        sizes.append(min(remaining, rng.choice([1, 2, 4, 8, 16])))
        remaining -= sizes[-1]
    return [
        write_transcript(
            folder,
            session_id(i),
            target_bytes=mib * 2**20,
            rng=rng,
            filler=filler,
            started=1_760_000_000 + i * 86_400,
        )
        for i, mib in enumerate(sizes)
    ]
