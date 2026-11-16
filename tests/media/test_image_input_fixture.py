"""The live image-input smoke's scrubbed transcript, replayed through the history mapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import UserMessage
from viki_agent.turn_input import Attachment

FIXTURE = Path(__file__).parents[1] / "fixtures" / "transcripts" / "image-input.jsonl"
SHA256 = "a2d099ed969bdc91a4df7c65ba324846b930de9a7853c2c9fddb4bdb93f6a8ca"
BYTE_SIZE = 114


def _attachment(*value: object) -> Attachment:
    digest, media_type, size, name = cast(tuple[str, str, int, str], value)
    return Attachment(
        f"{digest[:8]}-0000-4000-8000-000000000000", media_type, size, 1, 1, name, source="history"
    )


def test_the_smoke_transcript_yields_exactly_one_image_attachment() -> None:
    calls: list[tuple[str, str, int, str]] = []

    def resolve(*value: object) -> Attachment:
        calls.append(cast(tuple[str, str, int, str], value))
        return _attachment(*value)

    mapper = Mapper(vault="C:/Vault", mode="default", history_media=resolve)
    entries = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    messages: list[UserMessage] = []
    for entry in entries:
        for event in mapper.entry(cast(dict[str, Any], entry)):
            if isinstance(event, UserMessage):
                messages.append(event)

    attachments = [a for message in messages for a in message.attachments]
    assert len(attachments) == 1
    assert attachments[0].byte_size == BYTE_SIZE
    assert calls == [(SHA256, "image/png", BYTE_SIZE, "history-image.png")]
